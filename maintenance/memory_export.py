"""Write a portable memory package from one owner-authorized export reader.

The package is a directory, or a zip when the target ends in ``.zip``.  It
contains the streaming JSONL datasets, readable Markdown grouped by session and
UTC month, optional retained attachments, and a manifest with a SHA-256 for
every artifact.  The manifest is written last, only after the reader accepts the
snapshot with no deletion or suppression recorded since it began.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import IO, Protocol

from scope_recall._version import __version__
from scope_recall.contracts import ContractError
from scope_recall.core.management_export import AttachmentExport, ClaimExport, MemoryExport, utc_iso

from .backup import first_link

PACKAGE_FORMAT = "scope-recall-export/1"
_ARTIFACT_REF = re.compile(r"artifact-[0-9a-f]{64}\Z")
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "text/plain": ".txt",
}


def _safe_path(value: str) -> PurePosixPath:
    """A package member path, never absolute and never leaving its package."""
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value:
        raise ContractError("INPUT_INVALID", "package_path")
    return path


class _HashingWriter:
    """A binary sink that hashes and counts exactly what reaches the artifact."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes = 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.bytes += len(data)
        return self.stream.write(data)


class _Jsonl:
    """One compact UTF-8 JSON object per line."""

    def __init__(self, stream: _HashingWriter) -> None:
        self.stream = stream

    def write(self, record: dict) -> None:
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.stream.write(text.encode("utf-8") + b"\n")


class _Package(Protocol):
    path: Path
    artifacts: list[dict]

    def open(self, name: str) -> IO[bytes]: ...

    def close(self) -> None: ...

    def abort(self) -> None: ...


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def _remove_created(path: Path, identity: tuple[int, int], *, directory: bool = False) -> None:
    """Remove exactly an export-owned file or empty directory, never traverse an output tree."""
    if not os.path.lexists(path):
        return
    if first_link(path) is not None or _identity(path) != identity:
        raise ContractError("OUTPUT_INVALID", "output_changed_during_export")
    if directory:
        path.rmdir()
    else:
        path.unlink()


class _DirectoryPackage:
    """Package members as files under one newly created directory."""

    def __init__(self, root: Path) -> None:
        self.path = root
        self.artifacts: list[dict] = []
        self._files: list[tuple[Path, tuple[int, int]]] = []
        self._directories = [(root, _identity(root))]

    def open(self, name: str) -> IO[bytes]:
        target = self.path.joinpath(*_safe_path(name).parts)
        if first_link(target) is not None:
            raise ContractError("OUTPUT_INVALID", "output_link")
        for parent in reversed(target.parents):
            if self.path in parent.parents and not parent.exists():
                parent.mkdir(mode=0o700)
                self._directories.append((parent, _identity(parent)))
        stream = target.open("xb")
        self._files.append((target, _identity(target)))
        return stream

    def close(self) -> None:
        return None

    def abort(self) -> None:
        for path, identity in reversed(self._files):
            _remove_created(path, identity)
        for path, identity in reversed(self._directories):
            _remove_created(path, identity, directory=True)


class _ZipPackage:
    """Package members as entries of one newly created zip file."""

    def __init__(self, path: Path, handle: IO[bytes], archive: zipfile.ZipFile) -> None:
        self.path = path
        self.handle = handle
        self.archive = archive
        self.artifacts: list[dict] = []
        self._identity = _identity(path)

    def open(self, name: str) -> IO[bytes]:
        info = zipfile.ZipInfo(_safe_path(name).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        return self.archive.open(info, "w", force_zip64=True)

    def close(self) -> None:
        try:
            self.archive.close()
        finally:
            self.handle.close()

    def abort(self) -> None:
        try:
            self.close()
        finally:
            # Closing writes the zip footer and can fail on the same full disk
            # that interrupted a member. That must not skip removal of bodies.
            _remove_created(self.path, self._identity)


def _create_package(out: str | Path) -> _Package:
    """Create a new directory or zip target; an existing path is never overwritten."""
    path = Path(os.path.abspath(os.fspath(out)))
    if first_link(path) is not None:
        raise ContractError("OUTPUT_INVALID", "output_link")
    if os.path.lexists(path):
        raise ContractError("OUTPUT_EXISTS", "out")
    if path.suffix.lower() == ".zip":
        try:
            handle = path.open("xb")
        except FileExistsError:
            raise ContractError("OUTPUT_EXISTS", "out") from None
        except OSError as exc:
            raise ContractError("OUTPUT_INVALID", "out") from exc
        try:
            archive = zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        except (OSError, ValueError):
            handle.close()
            raise
        return _ZipPackage(path, handle, archive)
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise ContractError("OUTPUT_EXISTS", "out") from None
    except OSError as exc:
        raise ContractError("OUTPUT_INVALID", "out") from exc
    return _DirectoryPackage(path)


@contextmanager
def _artifact(package: _Package, name: str) -> Iterator[_HashingWriter]:
    """Write one artifact and record its digest only after the body completed."""
    member = _safe_path(name).as_posix()
    with package.open(member) as raw:
        writer = _HashingWriter(raw)
        yield writer
        package.artifacts.append(dict(path=member, sha256=writer.digest.hexdigest(), bytes=writer.bytes))


def _jsonl_artifact(package: _Package, name: str, records) -> int:
    count = 0
    with _artifact(package, name) as stream:
        writer = _Jsonl(stream)
        for record in records:
            writer.write(record)
            count += 1
    return count


def _heading(value: object) -> str:
    return " ".join(str(value).split())


def _session_path(session: str) -> str:
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
    return f"readable/sessions/session-{digest}.md"


def _month_path(month: str) -> str:
    return f"readable/months/{month}.md"


def _session_heading(session: str) -> str:
    return f"# Session {_heading(session)}\n\nTimes are UTC. Messages are shown exactly as stored.\n\n"


def _month_heading(month: str) -> str:
    return f"# {month}\n\nTimes are UTC. Messages are shown exactly as stored.\n\n"


def _source_markdown(record: dict, *, with_session: bool) -> str:
    where = f"session {_heading(record['session'])} · " if with_session else ""
    segment = record["segment"]
    part = f" · part {segment['index'] + 1}/{segment['total'] or 'unknown'}" if segment else ""
    return (
        f"## {record['occurred_at'] or record['recorded_at']} · {record['role']} · {record['origin']}{part}\n\n"
        f"`{record['ref']}@{record['revision']}` · {where}agent {_heading(record['agent'])} · "
        f"entry {_heading(record['entry_id'])}\n\n{record['content']}\n\n"
    )


def _session_record(record: dict) -> str:
    return _source_markdown(record, with_session=False)


def _month_record(record: dict) -> str:
    return _source_markdown(record, with_session=True)


def _write_source_groups(
    package: _Package,
    groups,
    path_for: Callable[[str], str],
    heading: Callable[[str], str],
    render: Callable[[dict], str],
    counts: dict,
) -> None:
    for key, items in itertools.groupby(groups, key=lambda item: item[0]):
        count = 0
        with _artifact(package, path_for(key)) as stream:
            stream.write(heading(key).encode("utf-8"))
            for _key, record in items:
                stream.write(render(record).encode("utf-8"))
                count += 1
        counts[key] = count


def _write_sessions(package: _Package, reader: MemoryExport, counts: dict) -> None:
    _write_source_groups(
        package,
        reader.iter_sources_by_session(),
        _session_path,
        _session_heading,
        _session_record,
        counts,
    )


def _write_months(package: _Package, reader: MemoryExport, counts: dict) -> None:
    _write_source_groups(package, reader.iter_sources_by_month(), _month_path, _month_heading, _month_record, counts)


def _write_facts(package: _Package, reader: MemoryExport) -> int:
    return _jsonl_artifact(package, "facts.jsonl", (claim.current for claim in reader.iter_claims()))


def _write_fact_history(package: _Package, reader: MemoryExport) -> int:
    return _jsonl_artifact(
        package,
        "fact_history.jsonl",
        (record for claim in reader.iter_claims() for record in claim.history),
    )


def _provenance_lines(record: dict) -> list[str]:
    lines = [
        f"- basis: {record['basis']}",
        f"- qualification: {record['qualification_reason']}",
        f"- valid: {record['valid_from'] or 'always'} to {record['valid_to'] or 'open'}",
        "- sources:",
    ]
    for source in record["sources"]:
        lines.append(
            f"  - `{source['ref']}@{source['revision']}` — {source['occurred_at']}, "
            f"session {_heading(source['session'])}, agent {_heading(source['agent'])}"
        )
    if record["suppressed"]:
        lines.append("- muted for automatic recall; visible to the owner")
    return lines


def _fact_body(record: dict, label: str) -> list[str]:
    lines = [
        f"### {label} — revision {record['revision']} — {record['state']}",
        "",
        record["value"] or "(no value)",
        "",
    ]
    lines.extend(_provenance_lines(record))
    lines.append("")
    return lines


def _fact_markdown(claim: ClaimExport) -> Iterator[str]:
    current = claim.current
    yield f"## {_heading(current['subject'])} {_heading(current['predicate'])}\n\n"
    yield "\n".join(_fact_body(current, "Current")) + "\n"
    for record in claim.history:
        yield "\n".join(_fact_body(record, "Non-current version")) + "\n"


def _write_readable_facts(package: _Package, reader: MemoryExport) -> None:
    with _artifact(package, "readable/facts.md") as stream:
        stream.write(b"# Remembered facts\n\nTimes are UTC. Each fact lists the source versions it rests on.\n\n")
        count = 0
        for claim in reader.iter_claims():
            for part in _fact_markdown(claim):
                stream.write(part.encode("utf-8"))
            count += 1
        if not count:
            stream.write(b"_No facts matched the export filters._\n")


def _index_groups(title: str, counts: dict, folder: str, path_for: Callable[[str], str]) -> list[str]:
    lines = [f"## {title}", ""]
    if not counts:
        lines.extend(["- None", ""])
        return lines
    for key in sorted(counts):
        name = path_for(key).rsplit("/", 1)[-1]
        lines.append(f"- [{_heading(key)}]({folder}/{name}) — {counts[key]} messages")
    lines.append("")
    return lines


def _write_index(package: _Package, reader: MemoryExport, sessions: dict, months: dict, counts: dict) -> None:
    lines = ["# Scope Recall export", "", f"Exported at {reader.exported_at} (UTC).", ""]
    lines.extend(_index_groups("Sessions", sessions, "sessions", _session_path))
    lines.extend(_index_groups("Months", months, "months", _month_path))
    lines.extend(
        [
            "## Remembered facts",
            "",
            f"[facts.md](facts.md) — {counts['facts']} current facts and {counts['fact_history']} earlier versions.",
            "",
        ]
    )
    with _artifact(package, "readable/index.md") as stream:
        stream.write("\n".join(lines).encode("utf-8"))


def _attachment_path(attachment: AttachmentExport) -> str:
    if _ARTIFACT_REF.fullmatch(attachment.artifact_ref) is None:
        raise ContractError("INTEGRITY_ERROR", "artifact_ref")
    extension = _EXTENSIONS.get(attachment.media_type, ".bin")
    return f"attachments/{attachment.artifact_ref}-r{attachment.revision}{extension}"


def _write_attachments(package: _Package, reader: MemoryExport) -> list[dict]:
    entries = []
    for attachment in reader.iter_attachments():
        name = _attachment_path(attachment)
        digest = hashlib.sha256(attachment.data).hexdigest()
        if digest != attachment.sha256:
            raise ContractError("INTEGRITY_ERROR", "attachment_sha256")
        with _artifact(package, name) as stream:
            stream.write(attachment.data)
        entries.append(
            {
                "artifact_ref": attachment.artifact_ref,
                "revision": attachment.revision,
                "media_type": attachment.media_type,
                "sha256": digest,
                "bytes": len(attachment.data),
                "path": name,
                "suppressed": attachment.suppressed,
            }
        )
    return entries


def _filter_payload(filters) -> dict:
    return {
        "kind": filters.kind,
        "scope": filters.scope,
        "agent": filters.agent,
        "session": filters.session,
        "since": utc_iso(filters.since),
        "until": utc_iso(filters.until),
    }


def _manifest(
    reader: MemoryExport,
    counts: dict,
    attachments: list[dict],
    with_attachments: bool,
    artifacts: list[dict],
) -> dict:
    return {
        "format": PACKAGE_FORMAT,
        "product": "scope-recall",
        "product_version": __version__,
        "exported_at": reader.exported_at,
        "memory_epoch": reader.memory_epoch,
        "scopes": sorted(reader.context.allowed_scope_ids),
        "filters": _filter_payload(reader.filters),
        "with_attachments": with_attachments,
        "counts": counts,
        "attachments": attachments,
        "artifacts": list(artifacts),
    }


def _write_manifest(
    package: _Package,
    reader: MemoryExport,
    counts: dict,
    attachments: list[dict],
    with_attachments: bool,
) -> None:
    manifest = _manifest(reader, counts, attachments, with_attachments, package.artifacts)
    with _artifact(package, "manifest.json") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")


def _write_package(package: _Package, reader: MemoryExport, with_attachments: bool) -> dict:
    counts = {"sources": 0, "facts": 0, "fact_history": 0, "episodes": 0, "attachments": 0}
    counts["sources"] = _jsonl_artifact(package, "sources.jsonl", reader.iter_sources())
    sessions: dict = {}
    _write_sessions(package, reader, sessions)
    months: dict = {}
    _write_months(package, reader, months)
    counts["facts"] = _write_facts(package, reader)
    counts["fact_history"] = _write_fact_history(package, reader)
    _write_readable_facts(package, reader)
    counts["episodes"] = _jsonl_artifact(package, "episodes.jsonl", reader.iter_episodes())
    attachments = _write_attachments(package, reader) if with_attachments else []
    counts["attachments"] = len(attachments)
    _write_index(package, reader, sessions, months, counts)
    with reader.publication():
        _write_manifest(package, reader, counts, attachments, with_attachments)
        package.close()
    return {
        "out": str(package.path),
        "package_format": PACKAGE_FORMAT,
        "zip": isinstance(package, _ZipPackage),
        "counts": counts,
    }


def write_export(reader: MemoryExport, out: str | Path, *, with_attachments: bool = False) -> dict:
    """Write one complete package and return its target, format and record counts.

    ``out`` must not exist. On failure, remove only the files and empty
    directories this invocation created, including any partially written body.
    """
    if not isinstance(reader, MemoryExport):
        raise ContractError("INPUT_INVALID", "reader")
    package = _create_package(out)
    complete = False
    try:
        result = _write_package(package, reader, with_attachments)
        complete = True
        return result
    finally:
        if not complete:
            package.abort()
