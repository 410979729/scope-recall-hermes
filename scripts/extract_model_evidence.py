"""Safely extract the commit-bound P17 model evidence archive."""

from __future__ import annotations

import argparse
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


MAX_ENTRIES = 4096
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


class EvidenceArchiveError(ValueError):
    """The evidence archive is unsafe or does not satisfy the bundle contract."""


def _validate_member(name: str, seen: set[str]) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise EvidenceArchiveError(f"unsafe model evidence archive path: {name!r}")
    path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive or ".." in path.parts:
        raise EvidenceArchiveError(f"unsafe model evidence archive path: {name!r}")
    normalized = str(path)
    key = normalized.casefold()
    if key in seen:
        raise EvidenceArchiveError(f"duplicate model evidence archive path: {name!r}")
    seen.add(key)
    return normalized


def extract_model_evidence(archive: Path, bundle: Path, *, source_sha: str) -> Path:
    """Extract one exact-HEAD archive into a new bundle directory."""

    if len(source_sha) != 40 or any(char not in "0123456789abcdef" for char in source_sha):
        raise EvidenceArchiveError("source_sha must be a lowercase 40-character commit hash")
    expected_name = f"scope-recall-model-evidence-{source_sha}.zip"
    if archive.name != expected_name:
        raise EvidenceArchiveError(f"evidence archive is not bound to current HEAD: {archive.name!r}")
    if not archive.is_file() or archive.is_symlink():
        raise EvidenceArchiveError("evidence archive is missing or not a regular file")
    archive = archive.resolve()
    if bundle.exists():
        raise EvidenceArchiveError("evidence bundle destination already exists")

    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(archive) as archive_file:
            entries = archive_file.infolist()
            if len(entries) > MAX_ENTRIES:
                raise EvidenceArchiveError("model evidence archive has too many entries")
            for info in entries:
                _validate_member(info.filename, seen)
                mode = (info.external_attr >> 16) & 0o177777
                if stat.S_ISLNK(mode):
                    raise EvidenceArchiveError(f"symlink in model evidence archive: {info.filename!r}")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise EvidenceArchiveError("model evidence archive is too large")
            bundle.mkdir(parents=True)
            archive_file.extractall(bundle)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        if bundle.exists():
            for child in sorted(bundle.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            bundle.rmdir()
        raise EvidenceArchiveError(f"model evidence archive extraction failed: {type(exc).__name__}") from exc

    receipt = bundle / "formal-receipt.json"
    if not receipt.is_file() or receipt.is_symlink():
        raise EvidenceArchiveError("model evidence bundle is missing formal-receipt.json")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = extract_model_evidence(args.archive, args.bundle, source_sha=args.source_sha)
    except EvidenceArchiveError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"verified model evidence archive {args.archive.name} for source {args.source_sha}")
    print(f"formal receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
