"""Explicit, operator-attested replay of host records through the normal capture boundary."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from pathlib import Path

from scope_recall.adapters.shared_entries import read_attachment, read_shared_payload
from scope_recall.contracts import ContractError
from scope_recall.core import private
from scope_recall.core.entry_control import capture_refusal
from scope_recall.core.events import prepare_capture

from . import transcript
from .boundary import recorded_source_event
from .config import CONFIG_FILENAME, load_codex_config, load_shared_client
from .handler import CodexHookHandler
from .ledger import _session
from .ledger_state import _authorization_time, local_authorized_at
from .record_reader import RecordLines, _person_words, _without_command_records
from .session_marks import record_turns


def _moment(value: str) -> str:
    return _authorization_time(value)


def _configuration(host: str, home: Path):
    """Read the current binding and its original capture authorization without modifying checkpoints."""
    attachment = read_attachment(home)
    if attachment is not None:
        config = load_shared_client(home, host)
        registry = read_shared_payload(config.data_directory)
        entry = next(row for row in registry["entries"] if row["entry_id"] == config.entry_id)
        protected = [Path(row["home"]).resolve() for row in registry["entries"]]
        return config, _moment(entry["attached_at"]), protected
    if host != "codex":
        raise ValueError("entry_not_attached")
    config = load_codex_config(home / CONFIG_FILENAME)
    since = local_authorized_at(config, seconds=10, persist=False)
    if since is None:
        raise ValueError("local_authorization_unavailable")
    return config, since, [home]


def _files(host: str, records: Path, protected: list[Path]) -> list[Path]:
    """Find records below a declared host directory, excluding library and entry roots and links."""
    expected = "sessions" if host == "codex" else "projects"
    if records.name.lower() != expected or not records.is_dir():
        raise ValueError(f"records_must_be_{expected}_directory")
    if any(records == root or records.is_relative_to(root) for root in protected):
        raise ValueError("records_inside_entry_or_store")
    pattern = "*/*/*/rollout-*.jsonl" if host == "codex" else "*/*.jsonl"
    found = []
    for path in sorted(records.glob(pattern), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(records) or any(
            resolved == root or resolved.is_relative_to(root) for root in protected
        ):
            raise ValueError("record_outside_declared_directory")
        if host == "codex":
            parts = path.relative_to(records).parts
            if not (len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2):
                continue
            if not all(part.isascii() and part.isdigit() for part in parts[:3]):
                continue
        found.append(path)
    return found


def _fingerprint(host, home, records, since, until, files):
    """Bind the approval to parameters and complete bytes of every matching record."""
    manifest = []
    for path in files:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("records_changed_repreview")
        manifest.append([path.relative_to(records).as_posix(), after.st_size, after.st_mtime_ns, digest.hexdigest()])
    raw = json.dumps([host, str(home), str(records), since, until, manifest], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _increment(counts, reason, amount=1):
    counts[reason] = counts.get(reason, 0) + amount


def _preview_page(hook, session, audience, page, rejected):
    """Inspect current capture policy and duplicate witnesses without writing the store."""
    lines = [(end, _person_words(entry)) for end, entry in page] if hook.host == "claude-code" else page
    lines = _without_command_records(lines)
    entries = [entry for _, entry in lines if entry is not None]
    turns = record_turns(hook.config, session, entries) if hook.host == "workbuddy" else {}
    held = hook.record_reader._already_held(session, audience, entries, turns, None, time.monotonic() + 30)
    accepted = []
    for entry, duplicate in zip(entries, held, strict=True):
        if duplicate:
            _increment(rejected, "duplicate")
            continue
        bound = hook.record_reader._bind_entry(entry, audience)
        if bound is None:
            _increment(rejected, "access_denied")
            continue
        origin = "human_direct" if entry.role == "user" else "assistant_visible"
        context = hook.context(bound, session, origin)
        event = recorded_source_event(
            installation_id=hook.config.installation_id,
            host=hook.host,
            session_id=session,
            entry_id=entry.entry_id,
            role=entry.role,
            text=entry.text,
            occurred_at=entry.occurred_at,
            recorded_at=hook.clock.utc_now(),
        )
        try:
            prepared = prepare_capture(event, context)
        except ContractError as exc:
            _increment(rejected, exc.code)
            continue
        if prepared.rejection:
            _increment(rejected, prepared.rejection)
            continue
        with hook.core.storage.read(context, remaining_seconds=10) as tx:
            reason = capture_refusal(tx, context)
            if reason is None and private.capture_blocked(tx, context, prepared):
                reason = "private_mode"
            if reason is None:
                try:
                    tx.sources.refuse_under_a_deleted_key(prepared.events, scope_id=bound.capture_scope_id)
                except ContractError as exc:
                    reason = exc.code
        if reason:
            _increment(rejected, reason)
        else:
            accepted.append(entry)
    return accepted


def _read_record(hook, path, since, until, authorized, *, execute, rejected):
    identity = _session(hook.config, path)
    if identity is None:
        _increment(rejected, "invalid_session")
        return None, {}
    session, cwd = identity
    audience = hook.audience_of({"cwd": cwd})
    if audience is None:
        _increment(rejected, "access_denied")
        return None, {}
    hook.bind_project_root({"cwd": cwd}, audience)
    offset, directory = 0, None
    times, workdirs, count, outcomes = [], set(), 0, {}
    size = path.stat().st_size
    while offset < size:
        page = transcript.read(
            path, offset, rows=transcript.PARSERS[hook.host], directory=directory, session_id=session
        )
        if not page:
            _increment(rejected, "incomplete_record")
            break
        end = page[-1][0]
        selected = []
        for position, entry in page:
            if entry is not None:
                at = _moment(entry.occurred_at)
                if at < since or (until is not None and at > until):
                    entry = None
                elif at < authorized:
                    _increment(rejected, "before_authorization")
                    entry = None
            selected.append((position, entry))
        if execute:
            remote = RecordLines(offset, selected)
            hook.record_reader.read(
                session, audience, {"transcript_path": str(path)}, time.monotonic() + 30, remote=remote
            )
            for outcome, number in remote.outcomes.items():
                _increment(outcomes, outcome, number)
            if remote.through is None or remote.through <= offset:
                raise ValueError("capture_incomplete")
            end = remote.through
        else:
            accepted = _preview_page(hook, session, audience, selected, rejected)
            count += len(accepted)
            times.extend(entry.occurred_at for entry in accepted)
            workdirs.update(entry.cwd for entry in accepted if entry.cwd)
        directory = page.directory_at(end)
        offset = end
    if execute:
        return None, outcomes
    return {
        "id": session,
        "cwd": sorted(workdirs),
        "since": min(times) if times else None,
        "until": max(times) if times else None,
        "events": count,
    }, outcomes


def replay(host: str, home: Path, records: Path, since: str, until: str | None, *, preview_id: str | None = None):
    """Preview or replay a declared host directory; only confirmed execution writes captures, never cursors."""
    if not home.is_absolute() or not records.is_absolute():
        raise ValueError("absolute_paths_required")
    home, records = home.resolve(), records.resolve()
    since = _moment(since)
    until = _moment(until) if until else None
    if until is not None and until < since:
        raise ValueError("until_before_since")
    config, authorized, protected = _configuration(host, home)
    protected.extend([home, config.data_directory.resolve()])
    files = _files(host, records, protected)
    digest = _fingerprint(host, home, records, since, until, files)
    if preview_id is not None and preview_id != digest:
        raise ValueError("records_changed_repreview")
    rejected, sessions, outcomes = {}, [], {}
    with closing(CodexHookHandler(config)) as hook:
        for path in files:
            detail, counts = _read_record(
                hook, path, since, until, authorized, execute=preview_id is not None, rejected=rejected
            )
            if detail is not None and detail["events"]:
                sessions.append(detail)
            for key, value in counts.items():
                _increment(outcomes, key, value)
    if preview_id is None:
        if _fingerprint(host, home, records, since, until, _files(host, records, protected)) != digest:
            raise ValueError("records_changed_repreview")
        return {
            "status": "preview",
            "preview_id": digest,
            "sessions": len(sessions),
            "events": sum(item["events"] for item in sessions),
            "session_details": sessions,
            "since": min((item["since"] for item in sessions), default=None),
            "until": max((item["until"] for item in sessions), default=None),
            "rejected": rejected,
            "rejected_total": sum(rejected.values()),
        }
    return {"status": "completed", "written": outcomes.get("inserted", 0), "outcomes": outcomes, "rejected": rejected}
