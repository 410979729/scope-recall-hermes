"""The owner's local sessions that bound no memory scope, kept beside the store where ``doctor`` reads them.

A session of the person at this machine (the CLI, Hermes Desktop, ``hermes --tui``) that binds no scope captures and
recalls nothing, and the host shows no one the line the adapter writes in its log (``session_binding``).  So a process
that binds one, or is refused one for want of the surface's approval, keeps a record of it in
``unbound-sessions/<pid>.json`` under the store's directory: the entry, the surface, the gap codes and since when,
never who spoke or what was said.  A later session of the owner on that surface that binds takes it out, and the
record of a process that has ended is not read (``runtime/process_probe``): a gateway started again once the surface
was approved has none.  A login nobody approved is a visitor's and no record.

The records are breadcrumbs: a failed write or deletion is retried on the next session event, and a record that
cannot be read is no finding.  Persistence failures never stop a session from binding.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...runtime.process_probe import is_same_process, probe_process

#: Directory under the store's data directory holding one record per process.
UNBOUND_SESSIONS_DIRNAME = "unbound-sessions"
RECORD_SCHEMA = "scope-recall.unbound-sessions.v1"
#: Records read in one pass: a store has a handful of host processes, and anything beyond is leftover.
MAX_RECORDS = 64
_MAX_RECORD_BYTES = 65536

_lock = threading.Lock()
#: This process's owner surfaces that bound no scope, by store directory, then by (entry, platform).
_held: dict[Path, dict[tuple[str, str], dict[str, Any]]] = {}
#: The last successfully persisted surfaces, separate from the desired contents in ``_held``.
#: Session values are replaced rather than mutated, so a shallow snapshot retains the persisted contents.
_persisted: dict[Path, dict[tuple[str, str], dict[str, Any]]] = {}


def note_owner_session(data_directory: Path, *, entry_id: str, platform: str, gaps: tuple[str, ...] | None) -> None:
    """Keep that the owner's session on ``platform`` bound no scope, with its ``gaps``; ``None``, that one bound.

    Persist only contents different from the last successful write; retry failures on the next event, even for
    another surface.  A diagnostic aid must not stop a session from binding.
    """
    store = Path(data_directory)
    key = (entry_id, platform)
    with _lock:
        held = _held.setdefault(store, {})
        before = held.get(key)
        if gaps is None:
            held.pop(key, None)
        elif before is None or before["gaps"] != list(gaps):
            since = before["since"] if before is not None else datetime.now(UTC).isoformat(timespec="seconds")
            held[key] = {"entry_id": entry_id, "platform": platform, "gaps": list(gaps), "since": since}
        if held != _persisted.get(store, {}) and _write(store, list(held.values())):
            _persisted[store] = held.copy()


def owner_sessions_unbound(data_directory: Path, *, entry_id: str) -> list[dict[str, Any]]:
    """The surfaces on which a live process bound the owner's sessions of ``entry_id`` (empty for a store of its own)
    to no scope, by platform: each with the gap codes and the earliest time recorded.  Read-only."""
    found: dict[str, dict[str, Any]] = {}
    for _path, record in _records(Path(data_directory) / UNBOUND_SESSIONS_DIRNAME):
        if record is None or not is_same_process(record["pid"], record["start_token"]):
            continue
        for item in record["sessions"]:
            if item["entry_id"] != entry_id:
                continue
            seen = found.setdefault(item["platform"], {"platform": item["platform"], "gaps": [], "since": ""})
            seen["gaps"] = list(dict.fromkeys([*seen["gaps"], *item["gaps"]]))
            seen["since"] = min(filter(None, (seen["since"], item["since"])), default="")
    return [found[platform] for platform in sorted(found)]


def _write(store: Path, sessions: list[dict[str, Any]]) -> bool:
    directory = store / UNBOUND_SESSIONS_DIRNAME
    target = directory / f"{os.getpid()}.json"
    try:
        if not sessions:
            target.unlink(missing_ok=True)
            return True
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": RECORD_SCHEMA,
            "pid": os.getpid(),
            "start_token": probe_process(os.getpid()).start_token,
            "sessions": sessions,
        }
        partial = directory / f"{os.getpid()}.json.partial"
        partial.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        return False
    _retire_ended(directory, keep=target)
    return True


def _retire_ended(directory: Path, *, keep: Path) -> None:
    """Remove the records of processes that have ended.  Best effort, bounded, never raises.

    A record that cannot be read is left as it is: this pass cannot tell whose it is, and a reader skips it."""
    for path, record in _records(directory):
        if path == keep or record is None or is_same_process(record["pid"], record["start_token"]):
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _records(directory: Path) -> Iterator[tuple[Path, dict[str, Any] | None]]:
    try:
        paths = sorted(directory.glob("*.json"))[:MAX_RECORDS]
    except OSError:
        return
    for path in paths:
        yield path, _read(path)


def _read(path: Path) -> dict[str, Any] | None:
    """A record as written, or ``None`` for anything else: a truncated or foreign file is no finding."""
    try:
        if path.stat().st_size > _MAX_RECORD_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != RECORD_SCHEMA:
        return None
    pid, token, sessions = raw.get("pid"), raw.get("start_token"), raw.get("sessions")
    if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF or not isinstance(token, (str, type(None))):
        return None
    if not isinstance(sessions, list):
        return None
    return {"pid": pid, "start_token": token, "sessions": [item for item in sessions if _is_session(item)]}


def _is_session(item: object) -> bool:
    return (
        isinstance(item, dict)
        and all(isinstance(item.get(name), str) for name in ("entry_id", "platform", "since"))
        and isinstance(item.get("gaps"), list)
        and all(isinstance(gap, str) for gap in item["gaps"])
    )


__all__ = ["MAX_RECORDS", "RECORD_SCHEMA", "UNBOUND_SESSIONS_DIRNAME", "note_owner_session", "owner_sessions_unbound"]
