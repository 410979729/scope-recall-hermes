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
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..contracts import ContractError, utc_timestamp

if TYPE_CHECKING:
    from .storage import SQLiteStorage

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


def normalize_project_path(value: object, *, resolve_links: bool = True) -> str:
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
        # Resolve on the directory's owner only. A POSIX server cannot resolve a
        # client's Windows junction; that client binds its canonical cwd before sending.
        if resolve_links and os.name == "nt":
            candidate = os.path.realpath(candidate)
        if candidate.lower().startswith("\\\\?\\unc\\"):
            candidate = "\\\\" + candidate[8:]
        elif candidate.startswith("\\\\?\\"):
            candidate = candidate[4:]
        return ntpath.normpath(candidate).replace("\\", "/").casefold()
    candidate = text.replace("\\", "/")
    if not posixpath.isabs(candidate):
        raise ContractError("INPUT_INVALID", "project")
    return posixpath.normpath(os.path.realpath(candidate) if resolve_links and os.name != "nt" else candidate)


def project_contains(parent: str, child: str) -> bool:
    """Whether ``child`` is ``parent`` or a descendant of it, by whole path components."""
    root = normalize_project_path(parent, resolve_links=False)
    path = normalize_project_path(child, resolve_links=False)
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
    raw = event.get("occurred_at")
    try:
        return utc_timestamp(raw)
    except ContractError:
        return None


def _covers(interval, at: str) -> bool:
    return interval["starts_at"] <= at and (interval["ends_at"] is None or interval["ends_at"] > at)


def _strictest(modes) -> str | None:
    modes = set(modes)
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
            try:
                recorded = utc_timestamp(event.get("recorded_at"))
            except ContractError:
                recorded = None
            # Receipt time is an upper bound, never evidence of when words were
            # spoken. Any earlier interval could have covered an undated event.
            possible = [row["mode"] for row in intervals if recorded is None or row["starts_at"] <= recorded]
            if possible:
                return _strictest(possible)
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
        return normalize_project_path(value, resolve_links=False)
    except ContractError:
        return None


def _target_matches(interval, context, project: str | None) -> bool:
    kind = interval["target_kind"]
    if kind == "global":
        return True
    if kind == "session":
        return interval["session_id"] == context.session_id
    return (
        project is not None
        and interval["project_path"] is not None
        and project_contains(interval["project_path"], project)
    )


def _target_keys(context):
    """Exact indexed keys, including ancestors of the already bound directory."""
    project = _project_for(context)
    targets = [("global", None, None), ("session", context.session_id, None)]
    while project:
        targets.append(("project", None, project))
        parent = (
            ntpath.dirname(project).replace("\\", "/")
            if _WINDOWS_ABSOLUTE.match(project) or project.startswith("//")
            else posixpath.dirname(project)
        )
        if parent == project:
            break
        project = parent
    return targets


def _event_bounds(events):
    """Distinct occurrence instants or receipt bounds for undated events."""
    bounds = set()
    for event in events:
        at = _event_time(event)
        try:
            recorded = utc_timestamp(event.get("recorded_at")) if at is None else None
        except ContractError:
            recorded = None
        bounds.add((at, recorded))
    return list(bounds) or [(None, None)]


def _matching_query_keys(targets, events):
    bounds = _event_bounds(events)
    target_values = ",".join("(?,?,?)" for _target in targets)
    time_values = ",".join("(?,?)" for _bound in bounds)
    # Separate open and closed ranges let SQLite seek past closed history in
    # the target index. CROSS JOIN keeps the small request keys outside that seek.
    select = """SELECT p.* FROM moments m CROSS JOIN targets t
        JOIN private_intervals p INDEXED BY private_target
        ON p.target_kind=t.kind AND p.session_id IS t.session AND p.project_path IS t.project"""
    query = f"""WITH targets(kind,session,project) AS (VALUES {target_values}),
        moments(at,recorded) AS (VALUES {time_values})
        {select} WHERE m.at IS NOT NULL AND p.ends_at IS NULL AND p.starts_at<=m.at
        UNION {select} WHERE m.at IS NOT NULL AND p.ends_at>m.at AND p.starts_at<=m.at
        UNION {select} WHERE m.at IS NULL AND (m.recorded IS NULL OR p.starts_at<=m.recorded)"""
    return query, [value for target in targets for value in target] + [value for bound in bounds for value in bound]


def _matching_query(context, events):
    return _matching_query_keys(_target_keys(context), events)


def matching_intervals(tx, context, *, now: str | None = None, events=()) -> list[dict]:
    """Read only indexed targets and intervals covering the requested instants.

    Receipt times of undated events bound possible history without inventing an
    occurrence time. One read serves every part of a capture or metadata batch.
    """
    query, parameters = _matching_query(context, [{"occurred_at": now}] if now is not None else events)
    return [dict(row) for row in tx._check().execute(query, parameters)]


def recall_state(tx, context, now: str, *, after: int | None = None):
    """Current mode and a rule marker; a new full rule invalidates an in-flight recall.

    New rules are read by their primary key, so closing one before release cannot
    revive a decision made before it, and closed history is never scanned.
    """
    mode = mode_at(matching_intervals(tx, context, now=now), now)
    invalidated, marker = rules_changed(tx, context, after=after)
    return ("full" if invalidated else mode), marker


def rule_revision(tx) -> int:
    """The latest opened rule; closing an existing rule cannot make capture stricter."""
    return tx._check().execute("SELECT coalesce(max(interval_id),0) FROM private_intervals").fetchone()[0]


def capture_blocked(tx, context, prepared) -> bool:
    """Reuse an ingress decision only while no new rule could invalidate it."""
    if prepared.private_revision is not None and prepared.private_revision == rule_revision(tx):
        return False
    return capture_mode(matching_intervals(tx, context, events=prepared.events), prepared.events) is not None


def rules_changed(tx, context, *, after: int | None = None):
    """Return whether a new full rule targets this decision and the latest rule marker."""
    conn = tx._check()
    marker = rule_revision(tx)
    if after is not None and marker > after:
        project = _project_for(context)
        for row in conn.execute("SELECT * FROM private_intervals WHERE interval_id>? AND mode='full'", (after,)):
            if _target_matches(row, context, project):
                return True, marker
    return False, marker


@dataclass(frozen=True)
class CaptureGuard:
    """One hook's content-free interval snapshot for bounded capture deferral.

    Durable capture always checks SQLite again in its own transaction. A batch of retry
    or shutdown copies shares this snapshot instead of opening a connection per event.
    """

    intervals: tuple[dict, ...]

    @classmethod
    def read(cls, tx, observations):
        """Read relevant intervals once for a bounded batch spanning sessions."""
        targets, events = set(), []
        for context, observed in observations:
            targets.update(_target_keys(context))
            events.extend(observed)
        if not targets:
            return cls(())
        query, parameters = _matching_query_keys(list(targets), events)
        return cls(tuple(dict(row) for row in tx._check().execute(query, parameters)))

    def blocked(self, context, events) -> bool:
        """Judge the batch member at its event time with the Core target policy."""
        project = _project_for(context)
        intervals = [row for row in self.intervals if _target_matches(row, context, project)]
        return capture_mode(intervals, events) is not None


def active_intervals(tx, now: str) -> list[dict]:
    """Every interval active at ``now``, for background work that reads sources directly."""
    now = utc_timestamp(now)
    rows = (
        tx._check()
        .execute(
            """SELECT interval_id,target_kind,session_id,project_path,mode,starts_at,ends_at
           FROM private_intervals WHERE ends_at IS NULL AND starts_at<=?
           UNION ALL SELECT interval_id,target_kind,session_id,project_path,mode,starts_at,ends_at
           FROM private_intervals WHERE ends_at>? AND starts_at<=? ORDER BY interval_id""",
            (now, now, now),
        )
        .fetchall()
    )
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
    return (
        project is not None
        and interval["project_path"] is not None
        and project_contains(interval["project_path"], project)
    )


def source_blocked(intervals, source) -> bool:
    """Whether full mode covers a source; None asks only about a whole-store interval."""
    return any(interval["mode"] == "full" and _source_matches(interval, source) for interval in intervals)


def filter_sources(intervals, sources):
    """The sources a full interval does not cover, in their original order."""
    return tuple(source for source in sources if not source_blocked(intervals, source))


def outbound_blocked(storage, clock, context, *, sources=(), claims=()) -> bool:
    """Recheck full mode immediately before a provider request, sharing one rule read.

    No subjects asks about global full mode, including content-free warm-up.
    Derived statements are checked against their own evidence as well as the
    sources explicitly selected for the request.
    """
    with storage.read(context, remaining_seconds=1.0) as tx:
        intervals = active_intervals(tx, clock.utc_now())
        return (
            source_blocked(intervals, None)
            or any(source_blocked(intervals, source) for source in sources)
            or any(claim_blocked(tx, intervals, claim) for claim in claims)
        )


def work_blocked(storage, clock, context, item) -> bool:
    """Whether a full interval covers the subject of one worker item now.

    Candidate evaluation checks the candidate's provenance and its selected
    evidence. Purge and projection rebuild are local and never blocked.
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
            if claim_blocked(tx, intervals, evaluation.candidate):
                return True
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
    """Whether a full interval covers any source that a claim version rests on.

    A claim has no session of its own, so it is private when one of its evidence
    sources is: sending the derived statement would send what that source said.
    """
    if source_blocked(intervals, None):
        return True
    rows = (
        tx._check()
        .execute(
            """SELECT e.session_id,e.project_root FROM evidence_links l
           JOIN source_events e ON e.event_id=l.source_ref AND e.source_revision=l.source_revision
           WHERE l.object_kind='claim' AND l.object_ref=? AND l.object_revision=?""",
            (claim.ref, claim.revision),
        )
        .fetchall()
    )
    for row in rows:
        if any(interval["mode"] == "full" and _source_matches(interval, row) for interval in intervals):
            return True
    return False


def notice_for_mode(mode: object) -> str | None:
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
        if project is not None:
            raise ContractError("INPUT_INVALID", "target")
        return _text(session_id, "session", MAX_SESSION_ID), None
    if target == "project":
        if session_id is not None:
            raise ContractError("INPUT_INVALID", "target")
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
    if duration_seconds is not None and (type(duration_seconds) is not int or not 1 <= duration_seconds <= 365 * 86400):
        raise ContractError("INPUT_INVALID", "for")
    session_id, project_path = _valid_target(target, session, project)
    if duration_seconds is not None and target != "global":
        raise ContractError("INPUT_INVALID", "for")
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
        row = conn.execute("SELECT * FROM private_intervals WHERE interval_id=?", (cursor.lastrowid,)).fetchone()
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
                "UPDATE private_intervals SET ends_at=? WHERE interval_id=?",
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
