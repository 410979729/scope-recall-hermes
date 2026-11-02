"""On-demand health candidates over the owner's existing fact records."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from ..contracts import ContractError
from .claims import evidence_refs, valid_at
from .entity_candidates import entity_candidates
from .entity_names import pending_names
from .management_query import partition
from .mutate import health_pair_kind, management_claims_private
from .recall_log_storage import cutoff
from .recall_log_writer import read_health
from .secret_patterns import structure_contains_secret_like_text
from .semantic_proposals import merge_candidates
from .visibility import allowed


def observation(core, deadline):
    """Read retained use metadata; uncertain history is never treated as unused."""
    path = core.recall_diagnostics.path
    days = core.recall_diagnostics.store.policy.retention_days
    result: dict[str, Any] = dict(
        days=days, start=None, end=core.clock.utc_now(), incomplete=True, eligible=False, used=set()
    )
    if not path.is_file():
        return result
    health = read_health(path)
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            row = conn.execute("SELECT days,health_since,capacity_loss_at FROM retention WHERE singleton=1").fetchone()
            days = row[0] if row else days
            result["days"] = days
            start = cutoff(result["end"], days)
            first = conn.execute("SELECT min(timestamp) FROM attempts WHERE timestamp>=?", (start,)).fetchone()
            if not first or not first[0] or not row or not row[1]:
                return result
            result["start"] = first[0]
            begin = datetime.fromisoformat(first[0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(result["end"].replace("Z", "+00:00"))
            coverage = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            loss = datetime.fromisoformat(row[2].replace("Z", "+00:00")) if row[2] else None
            if coverage > begin or (loss and loss >= begin):
                return result
            writer = core.recall_diagnostics.writer
            local_health = writer.health()
            if (
                not {"dropped_full", "write_failures", "written", "queued"}.issubset(health)
                or health.get("queued", 0)
                or writer.queue.unfinished_tasks
                or any(
                    local_health.get(key, 0) > writer.published.get(key, 0)
                    for key in ("dropped_full", "write_failures")
                )
            ):
                return result
            if health.get("dropped_full", 0) or health.get("write_failures", 0):
                stamp = health.get("last_loss_at")
                if not stamp or datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= begin:
                    return result
            if end - begin < timedelta(days=7):
                result["incomplete"] = False
                return result
            for (raw,) in conn.execute("SELECT payload FROM attempts WHERE timestamp>=?", (first[0],)):
                if time.monotonic() >= deadline:
                    break
                record = json.loads(raw)
                for item in record.get("items", ()):
                    if item.get("kind") == "claim" and item.get("reason") in {"delivered", "overflow"}:
                        result["used"].add(item.get("ref"))
            else:
                result["eligible"] = True
                result["incomplete"] = False
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return result
    return result


def _cursor(cursor):
    if cursor is None:
        return "", "facts", ""
    try:
        if type(cursor) is not str or len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (
            type(value) is not list
            or len(value) != 3
            or any(type(v) is not str for v in value)
            or value[1] not in {"facts", "entities", "unused", "done"}
            or len(value[0]) > 240
            or len(value[2]) > 240
        ):
            raise ValueError
        if value[1] == "entities" and value[2] and not value[2].isdecimal():
            raise ValueError
        return tuple(value)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ContractError("INPUT_INVALID", "cursor") from None


def _encode(ref, phase, marker):
    return (
        base64.urlsafe_b64encode(json.dumps([ref, phase, marker], separators=(",", ":")).encode()).decode().rstrip("=")
    )


def _visible(tx, clock, ref, revision) -> Any:
    version = tx.claims.version(ref, revision)
    if (
        version is None
        or version.current_revision != revision
        or version.state != "active"
        or version.suppressed
        or not valid_at(version, clock.utc_now(), historical=False)
    ):
        return None
    if (
        not allowed(tx, "claim", ref, automatic=True)
        or management_claims_private(tx, clock, (version,))
        or structure_contains_secret_like_text(version.payload)
    ):
        return None
    if version.payload["kind"] == "observation":
        from .reflection_validity import reflection_live

        if not reflection_live(tx, version):
            return None
    try:
        sources = tx.claims.live_sources(evidence_refs(version.payload), version.scope_id)
        if any(s.suppressed or not allowed(tx, "event", s.ref, automatic=True) for s in sources):
            return None
    except ContractError:
        return None
    return version


def _entity_issues(tx, core, version):
    """Nominate only names whose existing identity decision is still pending."""
    from .entity_resolution import live_entity

    now = core.clock.utc_now()
    try:
        live_entity(tx, version.ref, version.revision, now)
    except ContractError:
        return []
    result = []
    for name in pending_names(tx, version):
        if name != version.payload["subject"] and any(
            name in span["quote"] for span in version.payload["evidence_spans"]
        ):
            result.append(dict(facts=[asdict(version), asdict(version)], name=name))
        for candidate in entity_candidates(tx, version, now=now, name=name):
            other = _visible(tx, core.clock, candidate["ref"], candidate["revision"])
            if other:
                try:
                    live_entity(tx, other.ref, other.revision, now)
                except ContractError:
                    continue
                result.append(dict(facts=[asdict(version), asdict(other)], name=name))
    return result


def _unused(version, log):
    recorded = datetime.fromisoformat(version.recorded_from.replace("Z", "+00:00"))
    began = datetime.fromisoformat(log["start"].replace("Z", "+00:00")) if log["start"] else None
    return bool(log["eligible"] and began and version.ref not in log["used"] and recorded < began)


def _remaining_issues(view, core, version, phase, marker, result, log, limits):
    """Continue the entity and unused phases without losing a partial page's position."""
    limit, deadline = limits
    if phase == "entities":
        entities = _entity_issues(view, core, version)
        for index in range(int(marker or "0"), len(entities)):
            if _full(result, limit, deadline):
                return "entities", str(index), True
            result["entities"].append(entities[index])
        phase, marker = "unused", ""
    if phase == "unused" and _unused(version, log):
        if _full(result, limit, deadline):
            return "unused", "", True
        result["unused"].append(dict(facts=[asdict(version)]))
    return phase, marker, False


def _validate_page(limit, deadline_seconds):
    """Reject unbounded pages and unreasonable computation budgets."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(deadline_seconds) not in (int, float)
        or not 0 < deadline_seconds <= 30
    ):
        raise ContractError("INPUT_INVALID", "health_limit")


def health_page(core, context, *, limit=30, cursor=None, deadline_seconds=2.0):
    """Compute one bounded page in a fresh read transaction without storing results."""
    _validate_page(limit, deadline_seconds)
    source_ref, phase, marker = _cursor(cursor)
    deadline = time.monotonic() + deadline_seconds
    log = observation(core, deadline)
    result: dict[str, Any] = dict(
        entities=[],
        duplicates=[],
        contradictions=[],
        unused=[],
        observation_days=log["days"],
        observation_start=log["start"],
        observation_end=log["end"],
        records_incomplete=log["incomplete"],
        complete=False,
        next_cursor=None,
    )
    if time.monotonic() >= deadline:
        result["next_cursor"] = _encode(source_ref, phase, marker)
        return result
    with core.storage.read(context, remaining_seconds=max(0.001, deadline - time.monotonic())) as tx:
        conn = tx._check()
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            rows = conn.execute(
                "SELECT claim_id,current_revision,project_id,branch_id FROM claims "
                "WHERE scope_id IN (SELECT value FROM json_each(?)) AND read_blocked=0 AND suppressed=0 "
                "AND kind IN ('fact','preference','constraint','decision','observation') "
                "AND claim_id>=? ORDER BY claim_id LIMIT 65",
                (json.dumps(sorted(context.allowed_scope_ids)), source_ref),
            ).fetchall()
            for row in rows[:64]:
                ref = row[0]
                if ref != source_ref:
                    source_ref, phase, marker = ref, "facts", ""
                view = partition(tx, context, row[2], row[3])
                version = _visible(view, core.clock, ref, row[1])
                if version is None or phase == "done":
                    phase, marker = "done", ""
                    continue
                if phase == "facts":
                    candidates, next_ref = merge_candidates(
                        view,
                        version.payload,
                        version.scope_id,
                        now=core.clock.utc_now(),
                        paging=(marker or ref, 32),
                    )
                    for candidate in candidates:
                        if _full(result, limit, deadline):
                            result["next_cursor"] = _encode(ref, "facts", marker)
                            return result
                        other = _visible(view, core.clock, candidate["ref"], candidate["revision"])
                        kind = health_pair_kind(version, other) if other else None
                        if kind:
                            category = "duplicates" if kind == "duplicate" else "contradictions"
                            result[category].append(dict(facts=[asdict(version), asdict(other)]))
                        marker = candidate["ref"]
                    if next_ref:
                        result["next_cursor"] = _encode(ref, "facts", next_ref)
                        return result
                    phase, marker = "entities", "0"
                phase, marker, stopped = _remaining_issues(
                    view, core, version, phase, marker, result, log, (limit, deadline)
                )
                if stopped:
                    result["next_cursor"] = _encode(ref, phase, marker)
                    return result
                phase, marker = "done", ""
                if time.monotonic() >= deadline:
                    result["next_cursor"] = _encode(ref, phase, marker)
                    return result
            result["complete"] = len(rows) <= 64
            result["next_cursor"] = None if result["complete"] else _encode(source_ref, phase, marker)
        except sqlite3.OperationalError as exc:
            if "interrupt" not in str(exc).lower():
                raise
            result["next_cursor"] = _encode(source_ref, phase, marker)
        finally:
            conn.set_progress_handler(None, 0)
    return result


def _full(result, limit, deadline):
    return (
        time.monotonic() >= deadline
        or sum(len(result[name]) for name in ("entities", "duplicates", "contradictions", "unused")) >= limit
    )
