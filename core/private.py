"""Operator-controlled private intervals and the single capture, recall and outbound policy.

A private interval is metadata only: its target kind, the session id or normalized project
path it covers, its mode, and its UTC start and end.  No message, prompt, attachment body,
question or model answer is written to this module's table.

Capture is judged at the event's own occurrence time, so a replayed, retried or late
transcript event is refused even after the interval has ended.  Recall and background
outbound work are judged at the current time: a full interval stops both, while a
read-only interval stops only capture.  Overlapping intervals resolve to the stricter
mode; closing one target never closes another.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..contracts import ContractError, utc_timestamp

if TYPE_CHECKING:
    from .storage import SQLiteStorage

TABLE = "private_intervals"
TARGETS = ("session", "project", "global")
MODES = ("full", "read-only")
MODE_NONE = "none"
#: A target's project path and a capture's project path must survive a Windows drive letter,
#: both separators, redundant separators and case.  Longer paths are refused rather than cut.
MAX_PROJECT_PATH = 1024
MAX_SESSION_ID = 240
_DURATION = re.compile(r"([1-9][0-9]{0,5})([smhd])\Z")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _text(value: object, field: str, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ContractError("INPUT_INVALID", field)
    return value.strip()


def normalize_project_path(value: object) -> str:
    """The comparable form of an absolute project path.

    Windows paths use forward slashes and case folding, so ``C:\\Work\\Project``,
    ``c:/work/project`` and a child of either compare alike.  POSIX paths keep their
    case and only normalize separators and redundant components.  Matching always
    compares whole path components, never a raw string prefix, so ``project2`` is not
    under ``project``.
    """
    text = _text(value, "project", MAX_PROJECT_PATH)
    if "\x00" in text:
        raise ContractError("INPUT_INVALID", "project")
    windows = bool(_WINDOWS_ABSOLUTE.match(text) or "\\" in text or text.startswith("//"))
    if windows:
        candidate = text.replace("/", "\\")
        if not ntpath.isabs(candidate):
            raise ContractError("INPUT_INVALID", "project")
        return ntpath.normpath(candidate).replace("\\", "/").casefold()
    candidate = text.replace("\\", "/")
    if not posixpath.isabs(candidate):
        raise ContractError("INPUT_INVALID", "project")
    return posixpath.normpath(candidate)


def project_contains(parent: str, child: str) -> bool:
    """Whether ``child`` is ``parent`` or a descendant of it, by whole path components."""
    root = normalize_project_path(parent)
    path = normalize_project_path(child)
    if path == root:
        return True
    return path.startswith(root if root.endswith("/") else root + "/")


def parse_duration(value: object) -> int:
    """Seconds in a short duration: ``30m``, ``12h`` or ``7d``."""
    match = _DURATION.fullmatch(value) if type(value) is str else None
    if match is None:
        raise ContractError("INPUT_INVALID", "for")
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 365 * 86400:
        raise ContractError("INPUT_INVALID", "for")
    return seconds


def _now(clock) -> str:
    return utc_timestamp(clock.utc_now())


def _after(start: str, seconds: int) -> str:
    return (datetime.fromisoformat(start) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _zulu(value: str | None) -> str | None:
    return None if value is None else value.replace("+00:00", "Z")


def _event_time(event) -> str | None:
    raw = event.get("occurred_at") or event.get("recorded_at")
    try:
        return utc_timestamp(raw)
    except ContractError:
        return None


def _covers(interval, at: str) -> bool:
    return interval["starts_at"] <= at and (interval["ends_at"] is None or interval["ends_at"] > at)


def _strictest(modes) -> str | None:
    if "full" in modes:
        return "full"
    if "read-only" in modes:
        return "read-only"
    return None


def mode_at(intervals, at: str) -> str | None:
    """The stricter mode covering ``at``, or None when no interval covers it."""
    return _strictest(interval["mode"] for interval in intervals if _covers(interval, at))


def capture_mode(intervals, events) -> str | None:
    """The mode that refuses this capture, judged at each event's own time.

    Any part of a multi-part message being private refuses the whole capture.  A time
    the store cannot read fails closed when any interval targets this scope.
    """
    found = []
    for event in events:
        at = _event_time(event)
        if at is None:
            if intervals:
                return _strictest(interval["mode"] for interval in intervals) or "full"
            continue
        mode = mode_at(intervals, at)
        if mode is not None:
            found.append(mode)
    return _strictest(found)


def _project_for(context) -> str | None:
    value = getattr(context, "project_root", None)
    if value is None:
        return None
    try:
        return normalize_project_path(value)
    except ContractError:
        return None


def _target_matches(interval, context, project: str | None) -> bool:
    kind = interval["target_kind"]
    if kind == "global":
        return True
    if kind == "session":
        return interval["session_id"] == context.session_id
    return project is not None and interval["project_path"] is not None and project_contains(
        interval["project_path"], project
    )


def matching_intervals(tx, context) -> list[dict]:
    """Every interval that could target this context, oldest first.

    Project intervals are compared in Python so a rule for ``C:\\Work`` covers
    ``C:\\Work\\Project`` without a string prefix test.  The table is bounded by
    explicit operator actions, never by captured content.
    """
    project = _project_for(context)
    rows = tx._check().execute(
        """SELECT interval_id,target_kind,session_id,project_path,mode,starts_at,ends_at
           FROM private_intervals
           WHERE target_kind='global' OR (target_kind='session' AND session_id=?) OR target_kind='project'
           ORDER BY interval_id""",
        (context.session_id,),
    ).fetchall()
    return [
        {key: row[key] for key in row.keys()}
        for row in rows
        if _target_matches(row, context, project)
    ]


def active_intervals(tx, now: str) -> list[dict]:
    """Every interval active at ``now``, for background work that reads sources directly."""
    rows = tx._check().execute(
        """SELECT interval_id,target_kind,session_id,project_path,mode,starts_at,ends_at
           FROM private_intervals WHERE starts_at<=? AND (ends_at IS NULL OR ends_at>?)
           ORDER BY interval_id""",
        (now, now),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _source_field(source, name: str):
    if hasattr(source, "keys"):
        return source[name]
    return getattr(source, name, None)


def _source_matches(interval, source) -> bool:
    kind = interval["target_kind"]
    if kind == "global":
        return True
    if kind == "session":
        return interval["session_id"] == _source_field(source, "session_id")
    project = _source_field(source, "project_root")
    return project is not None and interval["project_path"] is not None and project_contains(
        interval["project_path"], project
    )


def source_blocked(intervals, source) -> bool:
    """Whether a full interval covers this stored source's session or project now."""
    return any(
        interval["mode"] == "full" and _source_matches(interval, source) for interval in intervals
    )


def filter_sources(intervals, sources):
    """The sources a full interval does not cover, in their original order."""
    return tuple(source for source in sources if not source_blocked(intervals, source))


def work_blocked(storage, clock, context, item) -> bool:
    """Whether a full interval covers the subject of one worker item now.

    Candidate evaluation has no source of its own, so its recorded evidence is
    checked instead.  Purge and projection rebuild are local and never blocked.
    """
    if item.work_type in {"purge", "rebuild_projection"}:
        return False
    now = utc_timestamp(clock.utc_now())
    with storage.read(context, remaining_seconds=1.0) as tx:
        intervals = active_intervals(tx, now)
        if not intervals:
            return False
        if item.work_type == "evaluate_candidate":
            evaluation = tx.candidates.evaluation(item.subject_revision)
            if evaluation is None:
                return False
            for ref, revision in evaluation.evidence_refs:
                source = tx.source(ref, revision)
                if source is not None and source_blocked(intervals, source):
                    return True
            return False
        if item.subject_ref.startswith("claim-"):
            claim = tx.claims.version(item.subject_ref, item.subject_revision)
            return claim is not None and claim_blocked(tx, intervals, claim)
        source = tx.source(item.subject_ref, item.subject_revision)
        return source is not None and source_blocked(intervals, source)


def claim_blocked(tx, intervals, claim) -> bool:
    """Whether a full interval covers every source that a claim version rests on.

    A claim has no session of its own, so it is private when one of its evidence
    sources is: sending the derived statement would send what that source said.
    """
    if any(interval["mode"] == "full" and interval["target_kind"] == "global" for interval in intervals):
        return True
    rows = tx._check().execute(
        """SELECT e.session_id,e.project_root FROM evidence_links l
           JOIN source_events e ON e.event_id=l.source_ref AND e.source_revision=l.source_revision
           WHERE l.object_kind='claim' AND l.object_ref=? AND l.object_revision=?""",
        (claim.ref, claim.revision),
    ).fetchall()
    for row in rows:
        if any(
            interval["mode"] == "full" and _source_matches(interval, row) for interval in intervals
        ):
            return True
    return False


def notice_for_mode(mode: str | None) -> str | None:
    """The one line a host injects so the model can say plainly that it is not remembering."""
    if mode == "full":
        return (
            "Scope Recall private mode is on: this conversation is not being remembered. "
            "Tell the user plainly that this conversation is not being remembered."
        )
    if mode == "read-only":
        return (
            "Scope Recall private mode (read-only) is on: this conversation is not being remembered; "
            "existing memories may still be recalled. Tell the user plainly that it is not being remembered."
        )
    return None


def _row_payload(row, now: str) -> dict:
    return {
        "rule_id": int(row["interval_id"]),
        "target": row["target_kind"],
        "session": row["session_id"],
        "project": row["project_path"],
        "mode": row["mode"],
        "started_at": _zulu(row["starts_at"]),
        "ends_at": _zulu(row["ends_at"]),
        "active": _covers(row, now),
    }


def _valid_target(target: str, session_id: str | None, project: str | None) -> tuple[str | None, str | None]:
    if target not in TARGETS:
        raise ContractError("INPUT_INVALID", "target")
    if target == "session":
        return _text(session_id, "session", MAX_SESSION_ID), None
    if target == "project":
        return None, normalize_project_path(project)
    if session_id is not None or project is not None:
        raise ContractError("INPUT_INVALID", "target")
    return None, None


def _same_target(row, target: str, session_id: str | None, project: str | None) -> bool:
    if row["target_kind"] != target:
        return False
    if target == "session":
        return row["session_id"] == session_id
    if target == "project":
        return row["project_path"] == project
    return True


def open_interval(
    storage: SQLiteStorage,
    clock,
    context,
    *,
    target: str,
    session: str | None = None,
    project: str | None = None,
    mode: str = "full",
    duration_seconds: int | None = None,
) -> dict:
    """Open one interval and return it; an identical open rule is returned instead of duplicated."""
    if mode not in MODES:
        raise ContractError("INPUT_INVALID", "mode")
    if duration_seconds is not None and (
        type(duration_seconds) is not int or not 1 <= duration_seconds <= 365 * 86400
    ):
        raise ContractError("INPUT_INVALID", "for")
    session_id, project_path = _valid_target(target, session, project)
    started = _now(clock)
    ends = None if duration_seconds is None else _after(started, duration_seconds)
    with storage.write(context, remaining_seconds=5) as tx:
        conn = tx._check(write=True)
        for row in conn.execute("SELECT * FROM private_intervals WHERE mode=? ORDER BY interval_id", (mode,)):
            if _same_target(row, target, session_id, project_path) and row["ends_at"] is None:
                return _row_payload(row, started)
        cursor = conn.execute(
            """INSERT INTO private_intervals(target_kind,session_id,project_path,mode,starts_at,ends_at,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (target, session_id, project_path, mode, started, ends, started),
        )
        row = conn.execute(
            "SELECT * FROM private_intervals WHERE interval_id=?", (cursor.lastrowid,)
        ).fetchone()
        return _row_payload(row, started)


def close_interval(
    storage: SQLiteStorage,
    clock,
    context,
    *,
    target: str,
    session: str | None = None,
    project: str | None = None,
) -> dict:
    """Close every active interval of exactly this target; other targets stay in force."""
    session_id, project_path = _valid_target(target, session, project)
    now = _now(clock)
    with storage.write(context, remaining_seconds=5) as tx:
        conn = tx._check(write=True)
        rows = [
            row
            for row in conn.execute("SELECT * FROM private_intervals ORDER BY interval_id")
            if _same_target(row, target, session_id, project_path) and _covers(row, now)
        ]
        for row in rows:
            conn.execute(
                "UPDATE private_intervals SET ends_at=? WHERE interval_id=? AND ends_at IS NULL",
                (now, row["interval_id"]),
            )
        closed = []
        for row in rows:
            payload = {key: row[key] for key in row.keys()}
            payload["ends_at"] = now
            closed.append(_row_payload(payload, now))
        return {"closed": len(rows), "rules": closed}


def status(
    storage: SQLiteStorage,
    clock,
    context,
    *,
    session: str | None = None,
    project: str | None = None,
) -> dict:
    """Current effective mode and the active rules for one target, or for the whole store."""
    now = _now(clock)
    session_id = _text(session, "session", MAX_SESSION_ID) if session is not None else None
    project_path = normalize_project_path(project) if project is not None else None
    with storage.read(context) as tx:
        rows = active_intervals(tx, now)
    if session_id is not None or project_path is not None:
        rows = [
            row
            for row in rows
            if row["target_kind"] == "global"
            or (row["target_kind"] == "session" and row["session_id"] == session_id)
            or (
                row["target_kind"] == "project"
                and project_path is not None
                and row["project_path"] is not None
                and project_contains(row["project_path"], project_path)
            )
        ]
    global_mode = _strictest(row["mode"] for row in rows if row["target_kind"] == "global")
    mode = global_mode if session_id is None and project_path is None else _strictest(row["mode"] for row in rows)
    return {
        "mode": mode or MODE_NONE,
        "global_mode": global_mode or MODE_NONE,
        "rules": [_row_payload(row, now) for row in rows],
        "active_rules": len(rows),
    }


def list_intervals(storage: SQLiteStorage, clock, context, *, active_only: bool = True) -> dict:
    """Every stored interval, or only those active now, without reading captured content."""
    now = _now(clock)
    with storage.read(context) as tx:
        rows = [
            {key: row[key] for key in row.keys()}
            for row in tx._check().execute("SELECT * FROM private_intervals ORDER BY interval_id")
        ]
    if active_only:
        rows = [row for row in rows if _covers(row, now)]
    return {"rules": [_row_payload(row, now) for row in rows], "active_rules": sum(_covers(row, now) for row in rows)}
