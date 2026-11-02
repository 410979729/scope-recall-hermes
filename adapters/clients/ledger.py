"""Reconcile installed clients' session ledgers through the normal capture boundary.

The ledger is the durable payload. Checkpoints store offsets and bounded failure codes only. Recovery re-reads current
entry grants, deletion fences and private intervals; neither a remembered directory nor a checkpoint grants access.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ...contracts import ContractError
from ...core.failure_policy import capture_retryable
from ...core.file_lock import advisory_file_lock
from ...core.inbox_rules import deferral, deferred_until, given_up, replayable
from . import transcript
from .config import CONFIG_FILENAME, CodexInstallationConfig, SharedClientConfig, load_codex_config, shared_clients
from .handler import CodexHookHandler
from .ledger_state import checkpoint, local_authorized_at, save_checkpoint
from .record_reader import RecordLines


def _records(config: SharedClientConfig):
    """Enumerate only record locations belonging to the installed entry's declared home."""
    if config.host == "codex":
        yield from (config.home / "sessions").glob("*/*/*/rollout-*.jsonl")
    elif config.host in ("claude-code", "workbuddy"):
        yield from (config.home / "projects").glob("*/*.jsonl")


def _session(config: CodexInstallationConfig | SharedClientConfig, path: Path) -> tuple[str, str] | None:
    if isinstance(config, SharedClientConfig) and config.host == "claude-code":
        return path.stem, str(config.home)
    if isinstance(config, SharedClientConfig) and config.host == "workbuddy":
        session = transcript.workbuddy_session(path)
        return (session, str(config.home)) if session is not None else None
    try:
        with path.open("rb") as stream:
            row = json.loads(stream.readline(65537))
        payload = row.get("payload", {})
        if row.get("type") == "session_meta" and type(payload.get("id")) is str:
            return payload["id"], str(payload.get("cwd") or getattr(config, "home", config.data_directory))
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _reconcile(
    config: CodexInstallationConfig | SharedClientConfig, record: Path, attached_at: str, deadline: float
) -> str:
    identity = _session(config, record)
    if identity is None:
        return "skipped"
    session, cwd = identity
    home = config.home if isinstance(config, SharedClientConfig) else config.data_directory
    host = config.host if isinstance(config, SharedClientConfig) else "codex"
    cursor = transcript.Cursor(home, session, record)
    saved = {}
    try:
        value = json.loads(cursor.path.read_text(encoding="utf-8"))
        saved = value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        pass
    if saved.get("size") == record.stat().st_size and cursor.load() == record.stat().st_size:
        return "completed"
    previous = saved.get("last_error_code")
    if not replayable(previous, datetime.now(UTC)):
        return (
            "given_up"
            if given_up(previous)
            else ("deferred" if deferred_until(previous, datetime.now(UTC)) is not None else "stopped")
        )
    with closing(CodexHookHandler(config)) as hook:
        audience = hook.audience_of({"cwd": cwd})
        if audience is None:
            return "stopped"
        hook.bind_project_root({"cwd": cwd}, audience)
        start = cursor.load()
        page = transcript.read(
            record, start, rows=transcript.PARSERS[host], directory=cursor.directory(), session_id=session
        )
        lines = page
        # Installation authorizes future capture, not automatic import of history from before attachment.
        began = datetime.fromisoformat(attached_at.replace("Z", "+00:00")).astimezone(UTC)
        lines = [
            (
                end,
                entry
                if entry is None or datetime.fromisoformat(entry.occurred_at.replace("Z", "+00:00")) >= began
                else None,
            )
            for end, entry in lines
        ]
        remote = RecordLines(start, lines)
        hook.record_reader.read(session, audience, {"transcript_path": str(record)}, deadline, remote=remote)
        through = remote.through if remote.through is not None else start
        cursor.save(through, directory=page.directory_at(through))
        code = hook.diagnostics.capture_error_detail
        done = through == record.stat().st_size
        state = "completed" if done else "ready"
        stored_code = None
        if not done and code:
            stored_code = (
                code
                if not capture_retryable(code)
                else deferral(previous, ContractError(code), datetime.now(UTC), path="replay")
            )
            state = "stopped" if not capture_retryable(code) else ("given_up" if given_up(stored_code) else "deferred")
        cursor.save_state(last_error_code=stored_code, size=record.stat().st_size)
        return state


def _local_ledgers(binding, deadline: float) -> dict[str, int]:
    """Recover a local installation even when a hook died before publishing its locator."""
    result: dict[str, int] = {}
    try:
        config_path = binding.data_directory.parent / CONFIG_FILENAME
        if not config_path.exists():
            return result
        config = load_codex_config(config_path)
        if config.to_binding() != binding:
            return result
        records = set()
        for locator in (binding.data_directory / "capture-ledgers").glob("*.json"):
            try:
                payload = json.loads(locator.read_text(encoding="utf-8"))
                if (
                    isinstance(payload, dict)
                    and payload.get("config") == str(config.config_path)
                    and isinstance(payload.get("record"), str)
                    and Path(payload["record"]).is_absolute()
                ):
                    records.add(Path(payload["record"]))
            except (OSError, ValueError, KeyError):
                continue
        configured = os.environ.get("CODEX_HOME", "")
        native = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".codex"
        records.update((native / "sessions").glob("*/*/*/rollout-*.jsonl"))
        since = local_authorized_at(config, seconds=max(0.001, deadline - time.monotonic()))
        if since is None:
            return {"stopped": 1}
        return _scan(binding, ((config, record, since) for record in sorted(records, key=str)), deadline)
    except (ContractError, OSError, RuntimeError, ValueError):
        result["deferred"] = result.get("deferred", 0) + 1
    return result


def _shared_records(binding):
    """Use a stable order so the content-free scan checkpoint spans entries and files."""
    for config, attached_at in sorted(shared_clients(binding), key=lambda item: item[0].entry_id):
        if config.host != "dsh":
            for record in sorted(_records(config), key=str):
                yield config, record, attached_at


def _scan_record(config, record: Path, since: str, deadline: float) -> str:
    home = config.home if isinstance(config, SharedClientConfig) else config.data_directory
    try:
        with advisory_file_lock(home / "scope-recall" / "transcript-replay.lock", timeout_seconds=0):
            return _reconcile(config, record, since, deadline)
    except (ContractError, OSError, RuntimeError, ValueError, TimeoutError):
        return "deferred"


def _scan(binding, records, deadline: float) -> dict[str, int]:
    result: dict[str, int] = {}
    saved = checkpoint(binding.data_directory, "scan.json")
    after = saved.get("after") if saved.get("installation_id") == binding.installation_id else None
    after = after if type(after) is str else ""

    def save(position):
        save_checkpoint(
            binding.data_directory, "scan.json", dict(installation_id=binding.installation_id, after=position)
        )

    for config, record, since in records:
        entry = config.entry_id if isinstance(config, SharedClientConfig) else "local"
        key = entry + "\0" + str(record)
        if key <= after:
            continue
        if time.monotonic() >= deadline:
            result["pending_scan"] = 1
            return result
        state = _scan_record(config, record, since, deadline)
        result[state] = result.get(state, 0) + 1
        if state == "ready":
            # A partial file keeps its place; deferred files yield to later entries until their backoff expires.
            result["pending_scan"] = 1
            return result
        after = key
        save(after)
    save(None)
    return result


def reconcile_store(binding, *, seconds: float = 2.0) -> dict[str, int]:
    """Replay bounded pages under current grants, retaining a library-wide scan position when time expires."""
    deadline = time.monotonic() + seconds
    try:
        with advisory_file_lock(binding.data_directory / "capture-ledgers" / "scan.lock", timeout_seconds=0):
            if binding.installation_kind != "shared":
                return _local_ledgers(binding, deadline)
            return _scan(binding, _shared_records(binding), deadline)
    except (ContractError, OSError, RuntimeError, ValueError, TimeoutError):
        return {"deferred": 1}


def retry_given_up(binding, *, limit: int, dry_run: bool, include_terminal: bool = False) -> dict[str, int]:
    """Reopen bounded given-up ledger checkpoints under the owner's existing retry command."""
    roots = (
        [binding.data_directory]
        if binding.installation_kind != "shared"
        else [config.home for config, _attached in shared_clients(binding)]
    )
    count = 0
    reopened = 0
    for home in roots:
        for path in (home / "scope-recall/transcripts").glob("*.json"):
            if count >= limit:
                return {"given_up": count, "reopened": reopened}
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                code = saved.get("last_error_code")
                if not (given_up(code) or code == "ACCESS_DENIED" or (include_terminal and code is not None)):
                    continue
                count += 1
                if not dry_run:
                    saved["last_error_code"] = None
                    pending = path.with_name(f"{path.stem}.{os.getpid()}.retry.tmp")
                    pending.write_text(json.dumps(saved), encoding="utf-8")
                    pending.replace(path)
                    reopened += 1
            except (OSError, ValueError):
                continue
    return {"given_up": count, "reopened": reopened}
