"""Independent reflection ceilings and gradual historical discovery in SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict


class ReflectionUsage(TypedDict):
    """A content-free scope counter reported identically by scheduling and doctor."""

    scope_id: str
    day: str
    calls: int
    limit: int
    resets_at: str


DAILY_CALL_LIMIT = 4
BACKFILL_PAGE_SECONDS = 900


def utc(value):
    """Normalize the injected business clock before deriving a shared UTC day."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def day_reset(now):
    """The next UTC midnight, without resetting the external budget ledger."""
    return (
        (utc(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        .isoformat()
        .replace("+00:00", "Z")
    )


def reflection_usage(tx, *, now) -> list[ReflectionUsage]:
    """Return authorized scopes' content-free daily usage, including unused scopes."""
    day = utc(now).date().isoformat()
    return [
        ReflectionUsage(
            scope_id=scope,
            day=day,
            calls=(
                tx._check()
                .execute("SELECT calls FROM reflection_daily_usage WHERE scope_id=? AND day=?", (scope, day))
                .fetchone()
                or [0]
            )[0],
            limit=DAILY_CALL_LIMIT,
            resets_at=day_reset(now),
        )
        for scope in sorted(tx.context.allowed_scope_ids)
    ]


def scope_available(tx, scope, *, now):
    """Read the independent hard ceiling; the model budget remains a separate gate."""
    return next(row["calls"] < row["limit"] for row in reflection_usage(tx, now=now) if row["scope_id"] == scope)


def reserve_call(tx, scope, *, now):
    """Atomically consume one call before dispatch, shared by generation and support.

    Reservations are never refunded: a process dying after sending must not
    create another opportunity to spend the same call on restart.
    """
    tx._scope(scope)
    conn = tx._check(write=True)
    day = utc(now).date().isoformat()
    conn.execute("INSERT OR IGNORE INTO reflection_daily_usage VALUES(?,?,0)", (scope, day))
    return (
        conn.execute(
            "UPDATE reflection_daily_usage SET calls=calls+1 WHERE scope_id=? AND day=? AND calls<?",
            (scope, day, DAILY_CALL_LIMIT),
        ).rowcount
        == 1
    )


def discovery_ready(tx, audience, *, now):
    """Honor a persisted historical page delay across worker restarts."""
    row = tx._check().execute("SELECT next_at FROM reflection_discovery WHERE audience=?", (audience,)).fetchone()
    return row is None or row[0] is None or utc(row[0]) <= utc(now)


def discovery_page(tx, audience, position, *, now):
    """Remember the initial historical end and delay only pages before that end."""
    conn = tx._check(write=True)
    conn.execute(
        (
            "INSERT OR IGNORE INTO reflection_discovery(audience,history_end) SELECT ?,coalesce"
            "(max(rowid),0) FROM source_events"
        ),
        (audience,),
    )
    end = conn.execute("SELECT history_end FROM reflection_discovery WHERE audience=?", (audience,)).fetchone()[0]
    ctx = tx.context
    more = conn.execute(
        "SELECT 1 FROM source_events WHERE rowid>? AND rowid<=? AND scope_id IN ("
        + ",".join("?" for _ in ctx.allowed_scope_ids)
        + ") AND project_id IS ? AND branch_id IS ? AND role='user' LIMIT 1",
        (position, end, *sorted(ctx.allowed_scope_ids), ctx.project_id, ctx.branch_id),
    ).fetchone()
    next_at = (utc(now) + timedelta(seconds=BACKFILL_PAGE_SECONDS)).isoformat().replace("+00:00", "Z") if more else None
    conn.execute("UPDATE reflection_discovery SET next_at=? WHERE audience=?", (next_at, audience))


def finish_generation(tx, key, *, empty):
    """Two empty batches double the new-evidence requirement, capped at 64 events."""
    conn = tx._check(write=True)
    if not empty:
        conn.execute("UPDATE reflection_groups SET empty_streak=0,required_sources=8 WHERE group_key=?", (key,))
        return
    conn.execute(
        (
            "UPDATE reflection_groups SET empty_streak=empty_streak+1,required_sources=min(64,8"
            "*(1 << min(3,empty_streak))) WHERE group_key=?"
        ),
        (key,),
    )
