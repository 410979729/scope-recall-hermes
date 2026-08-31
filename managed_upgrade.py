"""Crash-recoverable, exact-home managed upgrades for Scope Recall.

The worker and its candidate are frozen below ``HERMES_HOME/scope-recall`` so
replacing the live plugin cannot replace the code performing the upgrade.  The
append-only journal is authoritative; its small receipt is only a projection.
No memory, configuration value, subprocess output, or exception message is
written to either document.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

JOURNAL_SCHEMA = "scope-recall.managed-upgrade.v1"
SUPPORT_RECEIPT_SCHEMA = "scope-recall.managed-upgrade-support.v1"
OPERATIONS_RELATIVE = Path("scope-recall") / "upgrades" / "operations"
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
MAX_OPERATION_SCAN = 1024
MAX_STABLE_STAGE_ATTEMPTS = 3
STABLE_STAGE_RETRY_DELAYS_SECONDS = (1.0, 2.0)

STAGED = "STAGED"
PREFLIGHTED = "PREFLIGHTED"
QUIESCED = "QUIESCED"
ACTIVATING = "ACTIVATING"
RESTARTING = "RESTARTING"
COMPLETE = "COMPLETE"
FAILED_SAFE = "FAILED_SAFE"
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"
TERMINAL_STATES = frozenset({COMPLETE, FAILED_SAFE, MANUAL_RECOVERY_REQUIRED})
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STAGED: frozenset({PREFLIGHTED, FAILED_SAFE, MANUAL_RECOVERY_REQUIRED}),
    PREFLIGHTED: frozenset({QUIESCED, FAILED_SAFE, MANUAL_RECOVERY_REQUIRED}),
    QUIESCED: frozenset({ACTIVATING, MANUAL_RECOVERY_REQUIRED}),
    ACTIVATING: frozenset({RESTARTING, MANUAL_RECOVERY_REQUIRED}),
    RESTARTING: frozenset(
        {COMPLETE, FAILED_SAFE, MANUAL_RECOVERY_REQUIRED}
    ),
    COMPLETE: frozenset(),
    FAILED_SAFE: frozenset(),
    MANUAL_RECOVERY_REQUIRED: frozenset(),
}

_EVENT_DATA_KEYS = frozenset(
    {
        "automatic_rollback",
        "candidate_file_count",
        "candidate_tree_sha256",
        "gateway_pid",
        "installer_ok",
        "previous_version",
        "restart_target",
        "safe_to_restart_previous",
        "target_version",
        "worker_pid",
    }
)


def _io_path(path: str | os.PathLike[str] | Path) -> str:
    """Return an absolute Win32 extended path for internal filesystem I/O."""

    raw = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if os.name != "nt" or raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _path_exists(path: Path) -> bool:
    return os.path.exists(_io_path(path))


def _path_is_file(path: Path) -> bool:
    return os.path.isfile(_io_path(path))


def _path_is_dir(path: Path) -> bool:
    return os.path.isdir(_io_path(path))


def _make_dirs(path: Path, *, exist_ok: bool = True) -> None:
    os.makedirs(_io_path(path), exist_ok=exist_ok)


def _unlink(path: Path, *, missing_ok: bool = False) -> None:
    try:
        os.unlink(_io_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def _rmtree(path: Path) -> None:
    shutil.rmtree(_io_path(path), ignore_errors=True)


class ManagedUpgradeError(RuntimeError):
    """A privacy-safe managed-upgrade failure."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


@dataclass
class UpgradeSeams:
    """Narrow test seams; production leaves every member unset."""

    preflight: Callable[..., dict[str, Any]] | None = None
    install: Callable[..., dict[str, Any]] | None = None
    resume_install: Callable[..., dict[str, Any]] | None = None
    stable_stage: Callable[..., dict[str, Any]] | None = None
    gateway_identify: Callable[..., dict[str, Any] | None] | None = None
    gateway_pause: Callable[..., dict[str, Any] | None] | None = None
    gateway_stop: Callable[..., dict[str, Any]] | None = None
    gateway_start: Callable[..., dict[str, Any]] | None = None
    pid_alive: Callable[[int], bool] | None = None
    spawn: Callable[..., Any] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unsigned(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "sha256"}


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    sealed = _unsigned(document)
    sealed["sha256"] = canonical_json_hash(sealed)
    return sealed


def _validate_sealed(document: Any, reason_code: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ManagedUpgradeError(reason_code)
    digest = str(document.get("sha256") or "")
    if not SHA256_RE.fullmatch(digest):
        raise ManagedUpgradeError(reason_code)
    if canonical_json_hash(_unsigned(document)) != digest:
        raise ManagedUpgradeError(reason_code)
    return document


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(_io_path(path), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    """Publish a critical metadata file with Windows write-through semantics."""

    if os.name != "nt":
        os.replace(_io_path(source), _io_path(destination))
        _fsync_dir(destination.parent)
        return
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    if not move_file_ex(
        _io_path(source),
        _io_path(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    _make_dirs(path.parent)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=".sr.", dir=_io_path(path.parent)
    )
    temp = Path(raw_temp)
    try:
        try:
            os.chmod(temp, mode)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temp, path)
        # A successful rename is not a phase boundary until the final name can
        # be reopened, flushed, and proven byte-identical to the sealed input.
        with open(_io_path(path), "r+b") as published:
            actual = published.read()
            if actual != payload:
                raise OSError("durable metadata publish verification failed")
            published.flush()
            os.fsync(published.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _unlink(temp, missing_ok=True)


def _write_sealed(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    sealed = _seal(document)
    _atomic_bytes(path, canonical_json_bytes(sealed) + b"\n")
    return sealed


def _read_sealed(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        with open(_io_path(path), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedUpgradeError(reason_code) from exc
    return _validate_sealed(document, reason_code)


def _validate_operation_id(operation_id: str) -> str:
    token = str(operation_id or "")
    if not OPERATION_ID_RE.fullmatch(token) or ".." in token:
        raise ManagedUpgradeError("invalid_operation_id")
    return token


def resolve_explicit_home(hermes_home: str | os.PathLike[str] | Path) -> Path:
    raw = str(hermes_home or "").strip()
    if not raw:
        raise ManagedUpgradeError("hermes_home_required")
    return Path(raw).expanduser().resolve(strict=False)


def resolve_automatic_home(
    hermes_home: str | os.PathLike[str] | Path | None = None,
) -> Path:
    """Resolve the ordinary one-home install without scanning or guessing.

    An explicit argument wins.  An updater loaded from the active plugin can
    prove its home from ``.../plugins/scope-recall``; otherwise ``HERMES_HOME``
    is the only ambient authority.  Platform defaults are intentionally not
    guessed because a stale default home can coexist with an active profile.
    """

    explicit = str(hermes_home or "").strip()
    if explicit:
        return resolve_explicit_home(explicit)
    module_dir = Path(__file__).resolve().parent
    if module_dir.name == "scope-recall" and module_dir.parent.name == "plugins":
        return module_dir.parent.parent.resolve(strict=False)
    inherited = os.environ.get("HERMES_HOME", "").strip()
    if inherited:
        return resolve_explicit_home(inherited)
    raise ManagedUpgradeError("hermes_home_unbound")


def operations_root(hermes_home: Path) -> Path:
    return hermes_home.joinpath(*OPERATIONS_RELATIVE.parts)


def operation_dir(hermes_home: Path, operation_id: str) -> Path:
    operation_id = _validate_operation_id(operation_id)
    root = operations_root(hermes_home).resolve(strict=False)
    result = (root / operation_id).resolve(strict=False)
    try:
        result.relative_to(root)
    except ValueError as exc:
        raise ManagedUpgradeError("unsafe_operation_path") from exc
    return result


def home_lock_path(hermes_home: Path) -> Path:
    return operations_root(hermes_home).parent / "managed-upgrade.lock"


@contextmanager
def _os_file_lock(path: Path, reason_code: str) -> Iterator[None]:
    """Take a non-blocking OS lock which the kernel releases after a crash."""

    _make_dirs(path.parent)
    handle = open(_io_path(path), "a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ManagedUpgradeError(reason_code) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ManagedUpgradeError(reason_code) from exc
        locked = True
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


@contextmanager
def _operation_locks(home: Path, op_dir: Path) -> Iterator[None]:
    # One order everywhere prevents home/operation lock inversion.
    with _os_file_lock(home_lock_path(home), "home_upgrade_locked"):
        with _os_file_lock(op_dir / "operation.lock", "operation_locked"):
            yield


def _is_link(path: Path) -> bool:
    try:
        if os.path.islink(_io_path(path)):
            return True
        is_junction = getattr(path, "is_junction", None)
        if bool(is_junction and is_junction()):
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return bool(attributes & reparse_flag)
    except OSError:
        return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_io_path(path), "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def hash_candidate_tree(candidate: str | os.PathLike[str] | Path) -> dict[str, Any]:
    """Return the official canonical regular-file tree identity.

    The digest intentionally matches ``stable_update.canonical_tree_manifest``
    so a signed stable-release tree hash remains the expected hash at the
    activation boundary instead of being translated into a second identity.
    """

    supplied = Path(candidate).expanduser()
    if _is_link(supplied):
        raise ManagedUpgradeError("candidate_symlink_forbidden")
    try:
        root = Path(os.path.realpath(_io_path(supplied)))
    except OSError as exc:
        raise ManagedUpgradeError("candidate_unreadable") from exc
    if not _path_is_dir(root):
        raise ManagedUpgradeError("candidate_unreadable")

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(_io_path(current)), key=lambda entry: entry.name)
        except OSError as exc:
            raise ManagedUpgradeError("candidate_unreadable") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = os.path.relpath(entry.path, _io_path(root)).replace("\\", "/")
            folded = relative.casefold()
            if folded in seen or relative.startswith("/") or ".." in relative.split("/"):
                raise ManagedUpgradeError("candidate_path_collision")
            seen.add(folded)
            if entry.is_symlink() or _is_link(path):
                raise ManagedUpgradeError("candidate_symlink_forbidden")
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ManagedUpgradeError("candidate_special_file_forbidden")
            try:
                size = int(entry.stat(follow_symlinks=False).st_size)
            except OSError as exc:
                raise ManagedUpgradeError("candidate_unreadable") from exc
            records.append(
                {
                    "path": relative,
                    "sha256": _sha256_file(path),
                    "size_bytes": size,
                }
            )

    records.sort(key=lambda item: str(item["path"]))
    digest = canonical_json_hash(records)
    return {
        "sha256": digest,
        "tree_sha256": digest,
        "file_count": len(records),
        "entry_count": len(records),
    }


def _copy_candidate(source: Path, destination: Path) -> dict[str, Any]:
    try:
        shutil.copytree(
            _io_path(source),
            _io_path(destination),
            symlinks=True,
            copy_function=shutil.copy2,
        )
    except (OSError, shutil.Error) as exc:
        raise ManagedUpgradeError("candidate_stage_failed") from exc
    return hash_candidate_tree(destination)


def _manifest_field(plugin_dir: Path, field: str) -> str:
    manifest = plugin_dir / "plugin.yaml"
    if _is_link(manifest) or not _path_is_file(manifest):
        return ""
    try:
        with open(_io_path(manifest), encoding="utf-8", errors="strict") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeError):
        return ""
    prefix = f"{field}:"
    for raw in lines:
        line = raw.strip()
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version)
    if match is None:
        raise ManagedUpgradeError("invalid_candidate_version")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def validate_transition(current: str, target: str) -> None:
    if target not in LEGAL_TRANSITIONS.get(current, frozenset()):
        raise ManagedUpgradeError("illegal_transition")


def _journal_path(op_dir: Path) -> Path:
    return op_dir / "journal.jsonl"


def _pending_path(op_dir: Path) -> Path:
    return op_dir / "journal.pending.json"


def _read_events(op_dir: Path) -> list[dict[str, Any]]:
    path = _journal_path(op_dir)
    try:
        with open(_io_path(path), "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ManagedUpgradeError("journal_tampered") from exc
    if raw and not raw.endswith(b"\n"):
        raise ManagedUpgradeError("journal_tampered")
    events: list[dict[str, Any]] = []
    previous = ""
    for raw_line in raw.splitlines():
        if not raw_line:
            raise ManagedUpgradeError("journal_tampered")
        try:
            event = json.loads(raw_line.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ManagedUpgradeError("journal_tampered") from exc
        event = _validate_sealed(event, "journal_tampered")
        if int(event.get("seq") or 0) != len(events) + 1:
            raise ManagedUpgradeError("journal_tampered")
        if str(event.get("previous_sha256") or "") != previous:
            raise ManagedUpgradeError("journal_tampered")
        if str(event.get("state") or "") not in LEGAL_TRANSITIONS:
            raise ManagedUpgradeError("journal_tampered")
        data = event.get("data")
        if not isinstance(data, dict) or set(data) - _EVENT_DATA_KEYS:
            raise ManagedUpgradeError("journal_tampered")
        previous = str(event["sha256"])
        events.append(event)
    if not events:
        raise ManagedUpgradeError("journal_tampered")
    return events


def _recover_pending(op_dir: Path) -> None:
    """Finish the one fsynced transition which may straddle a crash."""

    pending_path = _pending_path(op_dir)
    if not _path_is_file(pending_path):
        return
    pending = _read_sealed(pending_path, "journal_tampered")
    expected = canonical_json_bytes(pending) + b"\n"
    journal_path = _journal_path(op_dir)
    try:
        with open(_io_path(journal_path), "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ManagedUpgradeError("journal_tampered") from exc
    if raw.endswith(expected):
        _unlink(pending_path, missing_ok=True)
        _fsync_dir(op_dir)
        return

    boundary = raw.rfind(b"\n") + 1
    prefix, tail = raw[:boundary], raw[boundary:]
    if tail and not expected.startswith(tail):
        raise ManagedUpgradeError("journal_tampered")
    if tail:
        _atomic_bytes(journal_path, prefix)
    existing = _read_events(op_dir)
    previous = str(existing[-1]["sha256"])
    if int(pending.get("seq") or 0) != len(existing) + 1:
        raise ManagedUpgradeError("journal_tampered")
    if str(pending.get("previous_sha256") or "") != previous:
        raise ManagedUpgradeError("journal_tampered")
    descriptor = os.open(_io_path(journal_path), os.O_WRONLY | os.O_APPEND)
    try:
        os.write(descriptor, expected)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _read_events(op_dir)
    _unlink(pending_path, missing_ok=True)
    _fsync_dir(op_dir)


def _safe_event_data(data: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key not in _EVENT_DATA_KEYS:
            raise ManagedUpgradeError("unsafe_journal_field")
        if not isinstance(value, (str, int, bool)) or isinstance(value, float):
            raise ManagedUpgradeError("unsafe_journal_field")
        result[key] = value
    return result


def _append_event(
    op_dir: Path,
    *,
    operation_id: str,
    state: str,
    reason_code: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not REASON_RE.fullmatch(reason_code):
        raise ManagedUpgradeError("invalid_reason_code")
    pending_path = _pending_path(op_dir)
    if _path_exists(pending_path):
        _recover_pending(op_dir)
    journal = _journal_path(op_dir)
    if _path_exists(journal):
        events = _read_events(op_dir)
        previous_state = str(events[-1]["state"])
        validate_transition(previous_state, state)
        previous = str(events[-1]["sha256"])
        sequence = len(events) + 1
    else:
        if state != STAGED:
            raise ManagedUpgradeError("illegal_transition")
        previous = ""
        sequence = 1
    event = _seal(
        {
            "at": _now(),
            "data": _safe_event_data(data),
            "operation_id": operation_id,
            "previous_sha256": previous,
            "reason_code": reason_code,
            "schema": JOURNAL_SCHEMA,
            "seq": sequence,
            "state": state,
        }
    )
    _atomic_bytes(pending_path, canonical_json_bytes(event) + b"\n")
    descriptor = os.open(
        _io_path(journal), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    try:
        os.write(descriptor, canonical_json_bytes(event) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _read_events(op_dir)
    _unlink(pending_path, missing_ok=True)
    _fsync_dir(op_dir)
    return event


def _read_plan(op_dir: Path, operation_id: str) -> dict[str, Any]:
    plan = _read_sealed(op_dir / "plan.json", "plan_tampered")
    if plan.get("schema") != JOURNAL_SCHEMA:
        raise ManagedUpgradeError("plan_tampered")
    if plan.get("operation_id") != operation_id:
        raise ManagedUpgradeError("plan_tampered")
    if plan.get("candidate_relative") != "candidate":
        raise ManagedUpgradeError("plan_tampered")
    if plan.get("managed_state_relative") != "private":
        raise ManagedUpgradeError("plan_tampered")
    if not SHA256_RE.fullmatch(str(plan.get("candidate_tree_sha256") or "")):
        raise ManagedUpgradeError("plan_tampered")
    return plan


def _receipt_payload(
    plan: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    return {
        "candidate_tree_sha256": plan["candidate_tree_sha256"],
        "operation_id": plan["operation_id"],
        "previous_version": plan["previous_version"],
        "reason_code": event["reason_code"],
        "schema": JOURNAL_SCHEMA,
        "state": event["state"],
        "target_version": plan["target_version"],
        "updated_at": event["at"],
        "journal_tail_sha256": event["sha256"],
    }


def _write_receipt(op_dir: Path, plan: dict[str, Any], event: dict[str, Any]) -> None:
    _write_sealed(op_dir / "receipt.json", _receipt_payload(plan, event))


def _transition(
    op_dir: Path,
    plan: dict[str, Any],
    state: str,
    reason_code: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = _append_event(
        op_dir,
        operation_id=str(plan["operation_id"]),
        state=state,
        reason_code=reason_code,
        data=data,
    )
    _write_receipt(op_dir, plan, event)
    return event


def _status_payload(plan: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    state = str(event["state"])
    if state == COMPLETE:
        outcome = "upgrade_complete"
        next_action = "none"
    elif state == MANUAL_RECOVERY_REQUIRED:
        outcome = "support_required"
        next_action = "submit_support_receipt"
    elif state == FAILED_SAFE:
        outcome = "upgrade_not_applied"
        next_action = "submit_support_receipt"
    else:
        outcome = "upgrade_in_progress"
        next_action = "rerun_same_update_command"
    payload = {
        "ok": state not in {FAILED_SAFE, MANUAL_RECOVERY_REQUIRED},
        "candidate_tree_sha256": plan["candidate_tree_sha256"],
        "operation_id": plan["operation_id"],
        "previous_version": plan["previous_version"],
        "reason_code": event["reason_code"],
        "state": state,
        "target_version": plan["target_version"],
        "terminal": state in TERMINAL_STATES,
        "outcome": outcome,
        "upgrade_complete": state == COMPLETE,
        "user_action_required": state != COMPLETE,
        "next_action_code": next_action,
    }
    if next_action == "submit_support_receipt":
        payload["support_receipt"] = {
            "schema": SUPPORT_RECEIPT_SCHEMA,
            "operation_id": plan["operation_id"],
            "state": state,
            "reason_code": event["reason_code"],
            "target_version": plan["target_version"],
            "journal_tail_sha256": event["sha256"],
        }
    return payload


def failure_payload(reason_code: str) -> dict[str, Any]:
    """Return a content-free failure contract for every public CLI boundary."""

    safe_reason = str(reason_code or "")
    if not REASON_RE.fullmatch(safe_reason):
        safe_reason = "managed_upgrade_internal_error"
    if safe_reason == "hermes_home_unbound":
        return {
            "ok": False,
            "reason_code": safe_reason,
            "outcome": "upgrade_not_applied",
            "upgrade_complete": False,
            "user_action_required": True,
            "next_action_code": "run_from_active_hermes_home",
        }
    return {
        "ok": False,
        "reason_code": safe_reason,
        "outcome": "support_required",
        "upgrade_complete": False,
        "user_action_required": True,
        "next_action_code": "submit_support_receipt",
        "support_receipt": {
            "schema": SUPPORT_RECEIPT_SCHEMA,
            "state": "NO_OPERATION",
            "reason_code": safe_reason,
        },
    }


def _verify_candidate(op_dir: Path, plan: dict[str, Any]) -> Path:
    candidate = op_dir / str(plan["candidate_relative"])
    actual = hash_candidate_tree(candidate)
    if actual["sha256"] != plan["candidate_tree_sha256"]:
        raise ManagedUpgradeError("candidate_hash_mismatch")
    if int(actual["file_count"]) != int(plan["candidate_file_count"]):
        raise ManagedUpgradeError("candidate_hash_mismatch")
    if _manifest_field(candidate, "name") != "scope-recall":
        raise ManagedUpgradeError("invalid_candidate_manifest")
    if _manifest_field(candidate, "version") != plan["target_version"]:
        raise ManagedUpgradeError("invalid_candidate_manifest")
    return candidate


def prepare(
    *,
    hermes_home: str | os.PathLike[str] | Path,
    candidate: str | os.PathLike[str] | Path,
    expected_tree_sha256: str,
    operation_id: str | None = None,
    detach: bool = False,
    seams: UpgradeSeams | None = None,
) -> dict[str, Any]:
    """Freeze a local candidate and optionally launch the external worker."""

    home = resolve_explicit_home(hermes_home)
    operation_id = _validate_operation_id(operation_id or uuid.uuid4().hex)
    expected = str(expected_tree_sha256 or "").lower()
    if not SHA256_RE.fullmatch(expected):
        raise ManagedUpgradeError("invalid_expected_tree_sha256")
    source = Path(candidate).expanduser()
    source_hash = hash_candidate_tree(source)
    if source_hash["sha256"] != expected:
        raise ManagedUpgradeError("candidate_hash_mismatch")
    source = Path(os.path.realpath(_io_path(source)))
    target_version = _manifest_field(source, "version")
    if _manifest_field(source, "name") != "scope-recall":
        raise ManagedUpgradeError("invalid_candidate_manifest")
    _version_tuple(target_version)
    previous_dir = home / "plugins" / "scope-recall"
    if _is_link(previous_dir):
        raise ManagedUpgradeError("installed_plugin_symlink_forbidden")
    previous_version = _manifest_field(previous_dir, "version")
    if previous_version and _version_tuple(target_version) < _version_tuple(previous_version):
        raise ManagedUpgradeError("candidate_downgrade_forbidden")

    root = operations_root(home)
    _make_dirs(root)
    final = operation_dir(home, operation_id)
    temp = root / f".{operation_id}.prep.{uuid.uuid4().hex[:8]}"
    with _os_file_lock(home_lock_path(home), "home_upgrade_locked"):
        if _path_exists(final):
            raise ManagedUpgradeError("operation_exists")
        _make_dirs(temp, exist_ok=False)
        try:
            with _os_file_lock(temp / "operation.lock", "operation_locked"):
                staged_hash = _copy_candidate(source, temp / "candidate")
                if staged_hash["sha256"] != expected:
                    raise ManagedUpgradeError("candidate_stage_mismatch")
                runner = temp / "runner" / "managed_upgrade.py"
                _make_dirs(runner.parent)
                shutil.copy2(_io_path(Path(__file__).resolve()), _io_path(runner))
                plan = _write_sealed(
                    temp / "plan.json",
                    {
                        "candidate_file_count": int(staged_hash["file_count"]),
                        "candidate_relative": "candidate",
                        "candidate_tree_sha256": expected,
                        "created_at": _now(),
                        "managed_state_relative": "private",
                        "operation_id": operation_id,
                        "previous_version": previous_version,
                        "runner_sha256": _sha256_file(runner),
                        "schema": JOURNAL_SCHEMA,
                        "target_version": target_version,
                    },
                )
                _make_dirs(temp / "private")
                event = _append_event(
                    temp,
                    operation_id=operation_id,
                    state=STAGED,
                    reason_code="candidate_staged",
                    data={
                        "candidate_file_count": int(staged_hash["file_count"]),
                        "candidate_tree_sha256": expected,
                        "previous_version": previous_version,
                        "target_version": target_version,
                    },
                )
                _write_receipt(temp, plan, event)
            os.replace(_io_path(temp), _io_path(final))
            _fsync_dir(root)
        finally:
            if _path_exists(temp):
                _rmtree(temp)

    payload = _status_payload(plan, event)
    payload["detached"] = False
    if detach:
        _spawn_worker(home, operation_id, seams or UpgradeSeams())
        payload["detached"] = True
        payload["background_worker_started"] = True
        payload["user_action_required"] = False
        payload["next_action_code"] = "wait_for_automatic_restart"
    return payload


def _incomplete_operation_ids(home: Path) -> list[str]:
    """Return bounded unresolved operations after atomic crash recovery.

    ``MANUAL_RECOVERY_REQUIRED`` remains unresolved: it may not be overwritten
    by a newly downloaded operation.  The home lock excludes a live worker
    while pending journal bytes are repaired and classified.
    """

    root = operations_root(home)
    if not _path_exists(root):
        return []
    with _os_file_lock(home_lock_path(home), "home_upgrade_locked"):
        if _is_link(root) or not _path_is_dir(root):
            raise ManagedUpgradeError("unsafe_operations_root")
        try:
            entries = sorted(os.scandir(_io_path(root)), key=lambda item: item.name)
        except OSError as exc:
            raise ManagedUpgradeError("operations_scan_failed") from exc
        if len(entries) > MAX_OPERATION_SCAN:
            raise ManagedUpgradeError("operation_scan_limit")
        incomplete: list[str] = []
        for entry in entries:
            # A power loss during prepare can leave a hidden, unpublished temp
            # directory.  It has no sealed operation identity and is never resumed.
            if entry.name.startswith("."):
                continue
            path = Path(entry.path)
            if (
                entry.is_symlink()
                or _is_link(path)
                or not entry.is_dir(follow_symlinks=False)
            ):
                raise ManagedUpgradeError("unsafe_operation_entry")
            operation_id = _validate_operation_id(entry.name)
            _recover_pending(path)
            plan = _read_plan(path, operation_id)
            events = _read_events(path)
            state = str(events[-1]["state"])
            if state not in {COMPLETE, FAILED_SAFE}:
                incomplete.append(str(plan["operation_id"]))
        return incomplete


def auto_update(
    *,
    hermes_home: str | os.PathLike[str] | Path | None = None,
    operation_id: str | None = None,
    seams: UpgradeSeams | None = None,
) -> dict[str, Any]:
    """Stage the fixed official stable release and launch one detached worker.

    This is the ordinary end-user entry point: there is no repository, URL,
    candidate path, checksum, migration choice, or repair choice to supply.
    """

    home = resolve_automatic_home(hermes_home)
    incomplete = _incomplete_operation_ids(home)
    if len(incomplete) > 1:
        raise ManagedUpgradeError("multiple_incomplete_operations")
    if incomplete:
        existing_id = incomplete[0]
        if operation_id is not None and operation_id != existing_id:
            raise ManagedUpgradeError("incomplete_operation_exists")
        payload = status(hermes_home=home, operation_id=existing_id)
        if payload.get("state") == MANUAL_RECOVERY_REQUIRED:
            payload["detached"] = False
            payload["reason_code"] = "unresolved_manual_operation"
            payload["next_action_code"] = "submit_support_receipt"
            return payload
        _spawn_worker(home, existing_id, seams or UpgradeSeams())
        payload["detached"] = True
        payload["background_worker_started"] = True
        payload["user_action_required"] = False
        payload["reason_code"] = "incomplete_operation_resumed"
        payload["next_action_code"] = "wait_for_automatic_restart"
        return payload

    previous_version = _actual_plugin_version(home)
    if not previous_version:
        return {
            "ok": False,
            "state": FAILED_SAFE,
            "terminal": True,
            "reason_code": "installed_plugin_missing",
            "outcome": "support_required",
            "upgrade_complete": False,
            "user_action_required": True,
            "next_action_code": "run_from_active_hermes_home",
        }
    runtime_seams = seams or UpgradeSeams()
    stage = runtime_seams.stable_stage
    if stage is None:
        try:
            if __package__:
                stable_module = importlib.import_module(f"{__package__}.stable_update")
            else:
                # Official source archives may invoke this file directly.
                # Load only its fixed sibling; never accept a caller-provided
                # module, repository, URL, or download implementation.
                sibling = Path(__file__).resolve().with_name("stable_update.py")
                if _is_link(sibling) or not _path_is_file(sibling):
                    raise ManagedUpgradeError("stable_stager_unavailable")
                spec = importlib.util.spec_from_file_location(
                    "_scope_recall_official_stable_update",
                    sibling,
                )
                if spec is None or spec.loader is None:
                    raise ManagedUpgradeError("stable_stager_unavailable")
                stable_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(stable_module)
            stage = stable_module.stage_latest_stable_update
        except ManagedUpgradeError:
            raise
        except (AttributeError, ImportError, OSError) as exc:
            raise ManagedUpgradeError("stable_stager_unavailable") from exc
    staged: dict[str, Any] = {}
    stage_attempts = 0
    retryable = False
    for stage_attempts in range(1, MAX_STABLE_STAGE_ATTEMPTS + 1):
        try:
            raw_staged = stage(
                cache_dir=home / "scope-recall" / "upgrades" / "cache",
                installed_version=previous_version,
            )
        except Exception as exc:
            # The public boundary returns only a fixed reason code.  Network
            # libraries and local paths must never leak through CLI output.
            raise ManagedUpgradeError("stable_stage_internal_error") from exc
        if not isinstance(raw_staged, dict):
            raise ManagedUpgradeError("stable_stage_invalid")
        staged = raw_staged
        if staged.get("ok") is True:
            break
        raw_error = staged.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        retryable = error.get("retryable") is True
        if not retryable or stage_attempts >= MAX_STABLE_STAGE_ATTEMPTS:
            break
        runtime_seams.sleep(STABLE_STAGE_RETRY_DELAYS_SECONDS[stage_attempts - 1])
    if staged.get("ok") is not True:
        raw_error = staged.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        code = str(error.get("code") or "stable_stage_failed").lower()
        safe_code = re.sub(r"[^a-z0-9._-]+", "_", code)[:70].strip("._-")
        payload: dict[str, Any] = {
            "ok": False,
            "state": FAILED_SAFE,
            "terminal": True,
            "reason_code": f"stable_{safe_code or 'stage_failed'}",
            "outcome": "retry_later" if retryable else "support_required",
            "upgrade_complete": False,
            "user_action_required": True,
            "retryable": retryable,
            "automatic_retry_attempts": stage_attempts,
            "next_action_code": (
                "rerun_same_update_command"
                if retryable
                else "submit_support_receipt"
            ),
        }
        if not retryable:
            payload["support_receipt"] = {
                "schema": SUPPORT_RECEIPT_SCHEMA,
                "state": "NO_OPERATION",
                "reason_code": payload["reason_code"],
            }
        return payload
    candidate_raw = staged.get("candidate_dir")
    expected = str(staged.get("tree_sha256") or "").lower()
    target_version = str(staged.get("version") or "")
    file_count = staged.get("file_count")
    if (
        not isinstance(candidate_raw, str)
        or not candidate_raw
        or not SHA256_RE.fullmatch(expected)
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
    ):
        raise ManagedUpgradeError("stable_stage_invalid")
    if _version_tuple(target_version) == _version_tuple(previous_version):
        return {
            "ok": True,
            "state": COMPLETE,
            "terminal": True,
            "reason_code": "already_current",
            "previous_version": previous_version,
            "target_version": target_version,
            "outcome": "upgrade_complete",
            "upgrade_complete": True,
            "user_action_required": False,
            "next_action_code": "none",
        }
    identity = hash_candidate_tree(Path(candidate_raw))
    if identity["sha256"] != expected or identity["file_count"] != file_count:
        raise ManagedUpgradeError("stable_stage_identity_mismatch")
    payload = prepare(
        hermes_home=home,
        candidate=Path(candidate_raw),
        expected_tree_sha256=expected,
        operation_id=operation_id,
        detach=True,
        seams=runtime_seams,
    )
    payload["background_worker_started"] = True
    payload["user_action_required"] = False
    payload["next_action_code"] = "wait_for_automatic_restart"
    return payload


def status(
    *,
    hermes_home: str | os.PathLike[str] | Path,
    operation_id: str,
) -> dict[str, Any]:
    home = resolve_explicit_home(hermes_home)
    operation_id = _validate_operation_id(operation_id)
    op_dir = operation_dir(home, operation_id)
    plan = _read_plan(op_dir, operation_id)
    if _path_exists(_pending_path(op_dir)):
        raise ManagedUpgradeError("transition_pending")
    events = _read_events(op_dir)
    return _status_payload(plan, events[-1])


def _load_candidate_installer(candidate: Path, operation_id: str) -> Any:
    package_name = f"_scope_recall_upgrade_{hashlib.sha256(operation_id.encode()).hexdigest()[:16]}"
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(package_name + "."):
            sys.modules.pop(name, None)
    init_path = candidate / "__init__.py"
    if not _path_is_file(init_path) or _is_link(init_path):
        raise ManagedUpgradeError("candidate_import_failed")
    spec = importlib.util.spec_from_file_location(
        package_name,
        init_path,
        submodule_search_locations=[str(candidate)],
    )
    if spec is None or spec.loader is None:
        raise ManagedUpgradeError("candidate_import_failed")
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    try:
        spec.loader.exec_module(package)
        installer = importlib.import_module(f"{package_name}.installer")
    except Exception as exc:
        raise ManagedUpgradeError("candidate_import_failed") from exc
    module_path = Path(str(getattr(installer, "__file__", ""))).resolve(strict=False)
    try:
        module_path.relative_to(candidate.resolve())
    except ValueError as exc:
        raise ManagedUpgradeError("candidate_import_failed") from exc
    return installer


def _call_preflight(
    seams: UpgradeSeams,
    *,
    home: Path,
    candidate: Path,
    operation_id: str,
) -> dict[str, Any]:
    if seams.preflight is not None:
        result = seams.preflight(
            hermes_home=home,
            candidate=candidate,
            operation_id=operation_id,
        )
    else:
        installer = _load_candidate_installer(candidate, operation_id)
        function = getattr(installer, "managed_upgrade_preflight", None)
        if not callable(function):
            raise ManagedUpgradeError("managed_preflight_unavailable")
        result = function(home, candidate_tree=candidate)
    if not isinstance(result, dict):
        raise ManagedUpgradeError("managed_preflight_invalid")
    if result.get("read_only") is not True:
        raise ManagedUpgradeError("managed_preflight_not_read_only")
    return result


def _call_install(
    seams: UpgradeSeams,
    *,
    home: Path,
    candidate: Path,
    operation_id: str,
    managed_state_dir: Path,
) -> dict[str, Any]:
    if seams.install is not None:
        result = seams.install(
            hermes_home=home,
            candidate=candidate,
            operation_id=operation_id,
            managed_state_dir=managed_state_dir,
        )
    else:
        installer = _load_candidate_installer(candidate, operation_id)
        result = installer.install(
            home,
            dry_run=False,
            force=True,
            activate=True,
            maintenance_mode=True,
            managed_upgrade=True,
            managed_state_dir=managed_state_dir,
        )
    if not isinstance(result, dict):
        raise ManagedUpgradeError("installer_result_invalid")
    return result


def _call_resume_install(
    seams: UpgradeSeams,
    *,
    home: Path,
    candidate: Path,
    operation_id: str,
    managed_state_dir: Path,
) -> dict[str, Any]:
    if seams.resume_install is not None:
        result = seams.resume_install(
            hermes_home=home,
            managed_state_dir=managed_state_dir,
            candidate=candidate,
            operation_id=operation_id,
        )
    else:
        installer = _load_candidate_installer(candidate, operation_id)
        function = getattr(installer, "resume_managed_upgrade", None)
        if not callable(function):
            raise ManagedUpgradeError("installer_resume_unavailable")
        result = function(
            managed_state_dir=managed_state_dir,
            hermes_home=home,
        )
    if not isinstance(result, dict):
        raise ManagedUpgradeError("installer_result_invalid")
    return result


def _transaction(result: dict[str, Any]) -> dict[str, Any]:
    raw = result.get("activation_transaction")
    if not isinstance(raw, dict):
        raw = result.get("transaction")
    return raw if isinstance(raw, dict) else {}


def _install_outcome(result: dict[str, Any]) -> str:
    transaction = _transaction(result)
    committed = str(transaction.get("status") or "") == "committed"
    if result.get("ok") is True and committed:
        return "committed"
    automatic = transaction.get("automatic_rollback") is True
    mutation_started = result.get("mutation_started") is True
    safe = result.get("safe_to_restart_previous") is True
    if safe and (automatic or not mutation_started):
        return "rolled_back_safe"
    return "ambiguous"


def _installer_event_data(result: dict[str, Any]) -> dict[str, Any]:
    transaction = _transaction(result)
    return {
        "automatic_rollback": transaction.get("automatic_rollback") is True,
        "installer_ok": result.get("ok") is True,
        "safe_to_restart_previous": result.get("safe_to_restart_previous") is True,
    }


def _gateway_control_identify(home: Path) -> dict[str, Any] | None:
    try:
        module = importlib.import_module("gateway.control_socket")
        identify_gateway = getattr(module, "identify_gateway")
    except (AttributeError, ImportError) as exc:
        raise ManagedUpgradeError("gateway_control_unavailable") from exc
    raw = identify_gateway(home)
    if raw is not None and not isinstance(raw, dict):
        raise ManagedUpgradeError("gateway_identity_invalid")
    return raw


def _gateway_control_pause(home: Path) -> dict[str, Any] | None:
    try:
        module = importlib.import_module("gateway.control_socket")
        pause_gateway_for_update = getattr(module, "pause_gateway_for_update")
    except (AttributeError, ImportError) as exc:
        raise ManagedUpgradeError("gateway_control_unavailable") from exc
    raw = pause_gateway_for_update(home)
    if raw is not None and not isinstance(raw, dict):
        raise ManagedUpgradeError("gateway_pause_invalid")
    return raw


def _identify_gateway(
    seams: UpgradeSeams, home: Path
) -> dict[str, Any] | None:
    if seams.gateway_identify is not None:
        raw = seams.gateway_identify(hermes_home=home)
    else:
        raw = _gateway_control_identify(home)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManagedUpgradeError("gateway_identity_invalid")
    try:
        reported_home = Path(str(raw.get("hermes_home") or "")).resolve(strict=False)
        pid = int(raw.get("pid") or 0)
    except (OSError, TypeError, ValueError) as exc:
        raise ManagedUpgradeError("gateway_identity_invalid") from exc
    if reported_home != home.resolve(strict=False) or pid <= 0:
        raise ManagedUpgradeError("gateway_identity_mismatch")
    return {"pid": pid}


def _gateway_command(action: str) -> list[str]:
    if action not in {"start", "stop"}:
        raise ManagedUpgradeError("unsafe_gateway_command")
    executable = shutil.which("hermes")
    args = (
        [executable, "gateway", action]
        if executable
        else [sys.executable, "-m", "hermes_cli.main", "gateway", action]
    )
    lowered = {str(token).casefold() for token in args}
    if "--all" in lowered or "--all-profiles" in lowered:
        raise ManagedUpgradeError("unsafe_gateway_command")
    return [str(token) for token in args]


def _gateway_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home.resolve(strict=False))
    for name in ("HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV"):
        env.pop(name, None)
    return env


def _default_gateway_subprocess(home: Path, action: str) -> dict[str, Any]:
    args = _gateway_command(action)
    try:
        completed = subprocess.run(
            args,
            cwd=home,
            env=_gateway_env(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
    except Exception:
        return {"ok": False}
    return {"ok": completed.returncode == 0}


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # On Windows ``os.kill(pid, 0)`` is a console control event, not a
        # harmless existence probe. Query a limited process handle instead.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code_process = kernel32.GetExitCodeProcess
        get_exit_code_process.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_exit_code_process.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            error = int(ctypes.get_last_error())
            if error == error_invalid_parameter:
                return False
            # Access denied proves presence; every other unknown result also
            # stays conservatively alive.
            return True
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code_process(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_exact_gateway_exit(
    seams: UpgradeSeams,
    home: Path,
    pid: int,
    timeout: float,
) -> bool:
    deadline = seams.monotonic() + max(1.0, min(timeout, 600.0))
    alive = seams.pid_alive or _pid_alive
    while seams.monotonic() < deadline:
        identity = _identify_gateway(seams, home)
        if not alive(pid) and identity is None:
            return True
        if identity is not None and int(identity["pid"]) != pid:
            raise ManagedUpgradeError("gateway_pid_changed")
        seams.sleep(0.05)
    return False


def _stop_gateway(seams: UpgradeSeams, home: Path) -> int:
    identity = _identify_gateway(seams, home)
    if identity is not None:
        pid = int(identity["pid"])
        pause = seams.gateway_pause
        ack = (
            pause(hermes_home=home)
            if pause is not None
            else _gateway_control_pause(home)
        )
        if ack is not None:
            if not isinstance(ack, dict):
                raise ManagedUpgradeError("gateway_pause_invalid")
            if int(ack.get("pid") or 0) != pid:
                raise ManagedUpgradeError("gateway_pause_pid_mismatch")
            if not (ack.get("pausing") is True or ack.get("already_stopping") is True):
                raise ManagedUpgradeError("gateway_pause_refused")
            timeout = float(ack.get("drain_timeout") or 30.0) + 15.0
            if not _wait_for_exact_gateway_exit(seams, home, pid, timeout):
                raise ManagedUpgradeError("gateway_exit_timeout")
            return pid
        stop = seams.gateway_stop
        result = (
            stop(
                hermes_home=home,
                args=_gateway_command("stop"),
                env=_gateway_env(home),
            )
            if stop is not None
            else _default_gateway_subprocess(home, "stop")
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ManagedUpgradeError("gateway_stop_failed")
        if not _wait_for_exact_gateway_exit(seams, home, pid, 90.0):
            raise ManagedUpgradeError("gateway_exit_timeout")
        return pid

    stop = seams.gateway_stop
    result = (
        stop(
            hermes_home=home,
            args=_gateway_command("stop"),
            env=_gateway_env(home),
        )
        if stop is not None
        else _default_gateway_subprocess(home, "stop")
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ManagedUpgradeError("gateway_stop_failed")
    if _identify_gateway(seams, home) is not None:
        raise ManagedUpgradeError("gateway_still_running")
    return 0


def _start_gateway(seams: UpgradeSeams, home: Path) -> dict[str, Any]:
    existing = _identify_gateway(seams, home)
    if existing is not None:
        raise ManagedUpgradeError("gateway_present_before_fresh_start")
    start = seams.gateway_start
    started = False
    attempted_start = False
    for attempt in range(3):
        existing = _identify_gateway(seams, home)
        if existing is not None:
            # The process appeared only after this function had proved absence
            # and issued at least one exact-home start command.
            if attempted_start:
                return existing
            raise ManagedUpgradeError("gateway_appeared_before_start")
        try:
            attempted_start = True
            result = (
                start(
                    hermes_home=home,
                    args=_gateway_command("start"),
                    env=_gateway_env(home),
                )
                if start is not None
                else _default_gateway_subprocess(home, "start")
            )
        except Exception:
            result = {"ok": False}
        if isinstance(result, dict) and result.get("ok") is True:
            started = True
            break
        if attempt < 2:
            seams.sleep(0.5 * (attempt + 1))
    if not started:
        existing = _identify_gateway(seams, home)
        if existing is not None:
            return existing
        raise ManagedUpgradeError("gateway_start_failed")
    deadline = seams.monotonic() + 45.0
    while seams.monotonic() < deadline:
        identity = _identify_gateway(seams, home)
        if identity is not None:
            return identity
        seams.sleep(0.05)
    raise ManagedUpgradeError("gateway_identify_timeout")


def _activation_handle(managed_state_dir: Path) -> Path:
    return managed_state_dir / "activation-transaction.json"


def _actual_plugin_version(home: Path) -> str:
    plugin = home / "plugins" / "scope-recall"
    if _is_link(plugin):
        raise ManagedUpgradeError("installed_plugin_symlink_forbidden")
    return _manifest_field(plugin, "version")


def _resume_activating(
    seams: UpgradeSeams,
    *,
    home: Path,
    op_dir: Path,
    plan: dict[str, Any],
    candidate: Path,
) -> dict[str, Any]:
    if _identify_gateway(seams, home) is not None:
        _stop_gateway(seams, home)
    if _identify_gateway(seams, home) is not None:
        raise ManagedUpgradeError("writer_active_during_recovery")
    managed_state_dir = op_dir / str(plan["managed_state_relative"])
    if not _path_is_file(_activation_handle(managed_state_dir)):
        # ACTIVATING is written before the candidate installer is imported.
        # A crash in that import window has no live mutation and therefore no
        # installer handle. Exact N-1 identity proves it is safe to invoke the
        # same frozen installer again; any other physical state stays closed.
        if _actual_plugin_version(home) != str(plan["previous_version"]):
            raise ManagedUpgradeError("activation_handle_missing")
        return _call_install(
            seams,
            home=home,
            candidate=candidate,
            operation_id=str(plan["operation_id"]),
            managed_state_dir=managed_state_dir,
        )
    actual = _actual_plugin_version(home)
    if actual not in {plan["previous_version"], plan["target_version"]}:
        raise ManagedUpgradeError("activation_version_ambiguous")
    return _call_resume_install(
        seams,
        home=home,
        candidate=candidate,
        operation_id=str(plan["operation_id"]),
        managed_state_dir=managed_state_dir,
    )


def _transition_after_install(
    op_dir: Path,
    plan: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if result.get("managed_retryable") is True:
        # The installer has a sealed non-terminal handle (for example commit
        # cleanup pending).  Preserve ACTIVATING so the same user command can
        # retry it; never reinterpret a durable commit decision as rollback.
        return _read_events(op_dir)[-1]
    outcome = _install_outcome(result)
    data = _installer_event_data(result)
    if outcome == "committed":
        data["restart_target"] = "candidate"
        return _transition(op_dir, plan, RESTARTING, "activation_committed", data)
    if outcome == "rolled_back_safe":
        data["restart_target"] = "previous"
        return _transition(op_dir, plan, RESTARTING, "automatic_rollback_complete", data)
    return _transition(
        op_dir,
        plan,
        MANUAL_RECOVERY_REQUIRED,
        "activation_outcome_ambiguous",
        data,
    )


def _resume_restarting(
    seams: UpgradeSeams,
    *,
    home: Path,
    op_dir: Path,
    plan: dict[str, Any],
    event: dict[str, Any],
    candidate: Path,
) -> dict[str, Any]:
    raw_data = event.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    target = str(data.get("restart_target") or "")
    expected_version = (
        str(plan["target_version"])
        if target == "candidate"
        else str(plan["previous_version"])
        if target == "previous"
        else ""
    )
    if not expected_version:
        raise ManagedUpgradeError("restart_target_ambiguous")

    # Never accept a process which appeared during activation or after a
    # previous crash. Re-quiesce it, then launch only after verifying the
    # durable target on disk. This makes a supervisor respawn safe without
    # pretending an un-attested process loaded the target.
    if _identify_gateway(seams, home) is not None:
        _stop_gateway(seams, home)
    if _identify_gateway(seams, home) is not None:
        raise ManagedUpgradeError("gateway_still_running_before_restart")

    managed_state_dir = op_dir / str(plan["managed_state_relative"])
    handle = _activation_handle(managed_state_dir)
    if _path_is_file(handle):
        result = _call_resume_install(
            seams,
            home=home,
            candidate=candidate,
            operation_id=str(plan["operation_id"]),
            managed_state_dir=managed_state_dir,
        )
        if _install_outcome(result) == "ambiguous":
            raise ManagedUpgradeError("installer_resume_ambiguous")
    if _actual_plugin_version(home) != expected_version:
        raise ManagedUpgradeError("restart_version_mismatch")
    identity = _start_gateway(seams, home)
    terminal = COMPLETE if target == "candidate" else FAILED_SAFE
    reason = "upgrade_complete" if terminal == COMPLETE else "previous_version_restarted"
    return _transition(
        op_dir,
        plan,
        terminal,
        reason,
        {"gateway_pid": int(identity["pid"]), "restart_target": target},
    )


def _run_locked(
    home: Path,
    operation_id: str,
    seams: UpgradeSeams,
) -> dict[str, Any]:
    op_dir = operation_dir(home, operation_id)
    with _operation_locks(home, op_dir):
        _recover_pending(op_dir)
        plan = _read_plan(op_dir, operation_id)
        events = _read_events(op_dir)
        event = events[-1]
        state = str(event["state"])
        if state in TERMINAL_STATES:
            # The event may have reached disk immediately before a crash which
            # prevented the non-authoritative receipt projection from updating.
            _write_receipt(op_dir, plan, event)
            return _status_payload(plan, event)

        try:
            candidate = _verify_candidate(op_dir, plan)
        except ManagedUpgradeError as exc:
            safe_without_restart = state == STAGED
            if state == PREFLIGHTED:
                # PREFLIGHTED is recorded before gateway pause begins. A crash
                # during pause can leave this state with the gateway already
                # down, so only a live exact-home identity proves FAILED_SAFE.
                try:
                    safe_without_restart = _identify_gateway(seams, home) is not None
                except ManagedUpgradeError:
                    safe_without_restart = False
            event = _transition(
                op_dir,
                plan,
                FAILED_SAFE if safe_without_restart else MANUAL_RECOVERY_REQUIRED,
                exc.reason_code,
            )
            return _status_payload(plan, event)

        try:
            if state == STAGED:
                if _actual_plugin_version(home) != str(plan["previous_version"]):
                    event = _transition(
                        op_dir,
                        plan,
                        FAILED_SAFE,
                        "installed_version_drift",
                    )
                    return _status_payload(plan, event)
                preflight = _call_preflight(
                    seams,
                    home=home,
                    candidate=candidate,
                    operation_id=operation_id,
                )
                if preflight.get("ok") is not True:
                    event = _transition(
                        op_dir,
                        plan,
                        FAILED_SAFE,
                        "managed_preflight_failed",
                    )
                    return _status_payload(plan, event)
                event = _transition(
                    op_dir,
                    plan,
                    PREFLIGHTED,
                    "managed_preflight_passed",
                )
                state = PREFLIGHTED

            if state == PREFLIGHTED:
                pid = _stop_gateway(seams, home)
                event = _transition(
                    op_dir,
                    plan,
                    QUIESCED,
                    "gateway_quiesced",
                    {"gateway_pid": pid},
                )
                state = QUIESCED

            if state == QUIESCED:
                if _identify_gateway(seams, home) is not None:
                    raise ManagedUpgradeError("writer_active_before_activation")
                event = _transition(
                    op_dir,
                    plan,
                    ACTIVATING,
                    "activation_started",
                    {"worker_pid": os.getpid()},
                )
                result = _call_install(
                    seams,
                    home=home,
                    candidate=candidate,
                    operation_id=operation_id,
                    managed_state_dir=op_dir / str(plan["managed_state_relative"]),
                )
                event = _transition_after_install(op_dir, plan, result)
                state = str(event["state"])

            elif state == ACTIVATING:
                result = _resume_activating(
                    seams,
                    home=home,
                    op_dir=op_dir,
                    plan=plan,
                    candidate=candidate,
                )
                event = _transition_after_install(op_dir, plan, result)
                state = str(event["state"])

            if state == RESTARTING:
                event = _resume_restarting(
                    seams,
                    home=home,
                    op_dir=op_dir,
                    plan=plan,
                    event=event,
                    candidate=candidate,
                )
        except ManagedUpgradeError as exc:
            current = _read_events(op_dir)[-1]
            if str(current["state"]) not in TERMINAL_STATES:
                current_state = str(current["state"])
                if current_state == RESTARTING:
                    # A start command may have succeeded even when its control
                    # acknowledgement was delayed. Keep the durable restart
                    # intent unresolved; the next run re-quiesces any process
                    # before a fresh exact-home start.
                    event = current
                else:
                    terminal = (
                        FAILED_SAFE
                        if current_state == STAGED
                        else MANUAL_RECOVERY_REQUIRED
                    )
                    event = _transition(
                        op_dir,
                        plan,
                        terminal,
                        exc.reason_code,
                    )
            else:
                event = current
        except Exception:
            current = _read_events(op_dir)[-1]
            if str(current["state"]) not in TERMINAL_STATES:
                current_state = str(current["state"])
                if current_state in {ACTIVATING, RESTARTING}:
                    # Both phases have durable installer/restart intent. A
                    # fresh invocation resumes that evidence instead of
                    # terminalizing a transient process or filesystem error.
                    event = current
                else:
                    terminal = (
                        FAILED_SAFE
                        if current_state == STAGED
                        else MANUAL_RECOVERY_REQUIRED
                    )
                    event = _transition(
                        op_dir,
                        plan,
                        terminal,
                        "unexpected_worker_failure",
                    )
            else:
                event = current
        return _status_payload(plan, event)


def run_worker(
    *,
    hermes_home: str | os.PathLike[str] | Path,
    operation_id: str,
    seams: UpgradeSeams | None = None,
) -> dict[str, Any]:
    home = resolve_explicit_home(hermes_home)
    return _run_locked(home, _validate_operation_id(operation_id), seams or UpgradeSeams())


def resume(
    *,
    hermes_home: str | os.PathLike[str] | Path,
    operation_id: str,
    seams: UpgradeSeams | None = None,
) -> dict[str, Any]:
    return run_worker(
        hermes_home=hermes_home,
        operation_id=operation_id,
        seams=seams,
    )


def _spawn_worker(home: Path, operation_id: str, seams: UpgradeSeams) -> None:
    op_dir = operation_dir(home, operation_id)
    plan = _read_plan(op_dir, operation_id)
    runner = op_dir / "runner" / "managed_upgrade.py"
    if _sha256_file(runner) != str(plan["runner_sha256"]):
        raise ManagedUpgradeError("runner_hash_mismatch")
    args = [
        sys.executable,
        str(runner),
        "worker",
        "--hermes-home",
        str(home),
        "--operation-id",
        operation_id,
        "--json",
    ]
    spawn = seams.spawn or subprocess.Popen
    kwargs: dict[str, Any] = {
        "cwd": op_dir,
        "env": _gateway_env(home),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        spawn(args, **kwargs)
    except Exception as exc:
        raise ManagedUpgradeError("worker_spawn_failed") from exc


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    home_required: bool = True,
) -> None:
    parser.add_argument(
        "--hermes-home",
        required=home_required,
        help=(
            "exact Hermes home"
            if home_required
            else "uses the active plugin home or HERMES_HOME; never guesses a home"
        ),
    )
    parser.add_argument("--json", action="store_true")


def register_cli(parent_parser: argparse.ArgumentParser) -> None:
    """Register under an existing Hermes plugin parser."""

    commands = parent_parser.add_subparsers(dest="managed_upgrade_command", required=True)
    auto_parser = commands.add_parser(
        "auto",
        help="download, verify, and install the latest official stable release",
    )
    _add_common(auto_parser, home_required=False)
    auto_parser.add_argument("--operation-id")

    prepare_parser = commands.add_parser("prepare", help="freeze and start an upgrade")
    _add_common(prepare_parser)
    prepare_parser.add_argument("--candidate", required=True)
    prepare_parser.add_argument("--expected-tree-sha256", required=True)
    prepare_parser.add_argument("--operation-id")
    prepare_parser.add_argument("--detach", action="store_true")

    for name in ("worker", "status", "resume"):
        parser = commands.add_parser(name)
        _add_common(parser)
        parser.add_argument("--operation-id", required=True)
    parent_parser.set_defaults(func=managed_upgrade_command)


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    command = str(args.managed_upgrade_command)
    if command == "auto":
        return auto_update(
            hermes_home=args.hermes_home,
            operation_id=args.operation_id,
        )
    if command == "prepare":
        return prepare(
            hermes_home=args.hermes_home,
            candidate=args.candidate,
            expected_tree_sha256=args.expected_tree_sha256,
            operation_id=args.operation_id,
            detach=bool(args.detach),
        )
    if command == "worker":
        return run_worker(
            hermes_home=args.hermes_home,
            operation_id=args.operation_id,
        )
    if command == "status":
        return status(
            hermes_home=args.hermes_home,
            operation_id=args.operation_id,
        )
    if command == "resume":
        return resume(
            hermes_home=args.hermes_home,
            operation_id=args.operation_id,
        )
    raise ManagedUpgradeError("unknown_command")


def managed_upgrade_command(args: argparse.Namespace) -> int:
    try:
        payload = _dispatch(args)
    except ManagedUpgradeError as exc:
        payload = failure_payload(exc.reason_code)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        payload = failure_payload("managed_upgrade_internal_error")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "managed-upgrade":
        args = args[1:]
    parser = argparse.ArgumentParser(prog="hermes-scope-recall managed-upgrade")
    register_cli(parser)
    try:
        parsed = parser.parse_args(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    return managed_upgrade_command(parsed)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
