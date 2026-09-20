"""Traceable episode skeleton and versioned resume state in the owning transaction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from ..contracts import ContractError
from . import lineage
from .claim_storage import parse_source_ref
from .claims import canonical_time
from .delete_storage import canonical
from .episodes import (
    TOPIC_BREAK,
    qualify_resume,
    source_origin,
    source_watermark,
    state_from_sources,
    supported_work_goal,
)
from .visibility import allowed


@dataclass(frozen=True)
class Episode:
    ref: str
    revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    state: str
    resume: dict | None
    evidence_refs: tuple[str, ...]
    source_watermark: str
    unprocessed_events: int
    suppressed: bool
    gaps: tuple[str, ...]
    needs_revalidation: bool


def _cited_pairs(resume) -> tuple[tuple[str, int], ...]:
    """The source versions a resume cites, once each, in document order."""
    from .resume_compaction import resume_evidence_refs

    pairs = []
    for ref in dict.fromkeys(resume_evidence_refs(resume)):
        try:
            pairs.append(parse_source_ref(ref))
        except (ContractError, ValueError):
            continue
    return tuple(pairs)


def _source_states(conn, pairs) -> dict:
    """Visibility, liveness and capture gaps of the given source versions, in one query."""
    if not pairs:
        return {}
    marks = ",".join("(?,?)" for _ in pairs)
    rows = conn.execute(
        f"""SELECT s.event_id,s.source_revision,s.read_blocked,s.scope_id,s.project_id,s.branch_id,s.capture_gaps_json,
               EXISTS(SELECT 1 FROM source_events n WHERE n.source_group_key=s.source_group_key
                      AND n.source_revision>s.source_revision) AS superseded
            FROM source_events s WHERE (s.event_id,s.source_revision) IN ({marks})""",
        [value for pair in pairs for value in pair],
    ).fetchall()
    return {(row["event_id"], row["source_revision"]): row for row in rows}


class Episodes:
    def __init__(self, tx):
        self.tx = tx

    def get(self, ref, revision=None) -> Episode | None:
        conn, ctx = self.tx._check(), self.tx.context
        if not allowed(self.tx, "episode", ref):
            return None
        scopes = sorted(ctx.allowed_scope_ids)
        row = conn.execute(
            f"""SELECT e.*,v.* FROM episodes e JOIN episode_versions v ON v.episode_id=e.episode_id
            AND v.revision=COALESCE(?,e.current_revision) WHERE e.episode_id=? AND e.read_blocked=0
            AND e.scope_id IN ({",".join("?" for _ in scopes)})
            AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)""",
            (revision, ref, *scopes, ctx.project_id, ctx.branch_id),
        ).fetchone()
        if row is None:
            return None
        links = lineage.evidence(conn, "episode", ref, row["revision"])
        refs = tuple(f"{r[0]}@{r[1]}" for r in links)
        resume = json.loads(row["resume_json"]) if row["resume_json"] else None
        # Once there is a resume its gaps are those of what it cites: the resume
        # is derived from those sources, and an uncited member that changed
        # does not invalidate it.  Without one every member counts.  Either way
        # the sources are judged in one query, not loaded one by one: a
        # 200-member episode cost 200 source loads per read, on every listing.
        judged = _cited_pairs(resume) if resume else tuple((r[0], r[1]) for r in links)
        gaps = []
        states = _source_states(conn, judged)
        for key in judged:
            state = states.get(key)
            if state is None or not self._visible(state):
                return None
            if state["superseded"] or (state["project_id"], state["branch_id"]) != (ctx.project_id, ctx.branch_id):
                gaps.append("source_version_changed")
            gaps.extend(json.loads(state["capture_gaps_json"]))
        pending = conn.execute(
            "SELECT count(*) FROM episode_events WHERE episode_id=? AND sequence>?",
            (ref, row["processed_sequence"]),
        ).fetchone()[0]
        if pending:
            gaps.append("unprocessed_events")
        changed = (
            row["environment_revision"] is not None
            and ctx.environment_revision != row["environment_revision"]
        )
        if resume:
            for progress in resume["verified_progress"]:
                for proof in progress["evidence_refs"]:
                    captured = conn.execute(
                        "SELECT environment_revision FROM episode_events WHERE episode_id=? AND source_ref=? AND source_revision=?",
                        (ref, *parse_source_ref(proof)),
                    ).fetchone()
                    if captured is None or captured[0] != ctx.environment_revision:
                        changed = True
        if changed:
            gaps.append("environment_needs_revalidation")
        if (
            gaps
            and row["resume_json"] is not None
            and any(g != "environment_needs_revalidation" for g in gaps)
        ):
            gaps.append("resume_requires_rebuild")
        return Episode(
            ref,
            row["revision"],
            row["scope_id"],
            row["project_id"],
            row["branch_id"],
            row["state"],
            resume,
            refs,
            row["source_watermark"],
            pending,
            bool(row["suppressed"]),
            tuple(dict.fromkeys(gaps)),
            changed,
        )

    def _visible(self, state) -> bool:
        """What ``Transaction.source`` requires before it returns a source at all."""
        ctx = self.tx.context
        return (allowed(self.tx, "event", state["event_id"]) and not state["read_blocked"]
                and state["scope_id"] in ctx.allowed_scope_ids
                and state["project_id"] in (None, ctx.project_id) and state["branch_id"] in (None, ctx.branch_id))

    def list(self, *, limit=200):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "episode_limit")
        ctx = self.tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        rows = (
            self.tx._check()
            .execute(
                f"""SELECT episode_id FROM episodes WHERE scope_id IN ({",".join("?" for _ in scopes)})
            AND read_blocked=0 AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)
            ORDER BY episode_id LIMIT ?""",
                (*scopes, ctx.project_id, ctx.branch_id, limit),
            )
            .fetchall()
        )
        return tuple(item for r in rows if (item := self.get(r[0])) is not None)

    def source_episode(self, ref, revision):
        row = (
            self.tx._check()
            .execute(
                "SELECT episode_id FROM episode_events WHERE source_ref=? AND source_revision=?",
                (ref, revision),
            )
            .fetchone()
        )
        return self.get(row[0]) if row else None

    def _latest_occurrence(self, ref):
        rows = self.tx._check().execute(
            """SELECT s.occurred_at FROM episode_events ee JOIN source_events s
            ON s.event_id=ee.source_ref AND s.source_revision=ee.source_revision WHERE ee.episode_id=?""",
            (ref,),
        )
        occurrences: list[str] = []
        for row in rows:
            if not row[0]:
                continue
            occurred_at = canonical_time(row[0])
            if occurred_at is not None:
                occurrences.append(occurred_at)
        return max(occurrences, default=None)

    def _series_for(self, source) -> tuple[str, str]:
        """The episode series a new source joins, and the anchor kind it implies.

        A task anchor names the series outright.  Without one, an exact source
        version relation (a display snapshot shared with an existing episode)
        can bridge sessions, never a title; otherwise the source continues the
        session's latest episode unless a human turn breaks the topic.
        """
        conn, ctx = self.tx._check(write=True), self.tx.context
        prefix = [ctx.binding.installation_id, source.scope_id, source.project_id, source.branch_id]
        if ctx.task_anchor is not None:
            return hashlib.sha256(canonical([*prefix, "task", ctx.task_anchor]).encode()).hexdigest(), "task" if ctx.task_anchor else "session"
        snapshot = source.event.get("display_snapshot")
        if snapshot and snapshot["order"] == "observed" and snapshot["items"]:
            pairs = " OR ".join(
                "(json_extract(d.value,'$.artifact_ref')=? AND json_extract(d.value,'$.revision')=?)"
                for _ in snapshot["items"]
            )
            params = [x for item in snapshot["items"] for x in (item["artifact_ref"], item["revision"])]
            candidates = conn.execute(
                f"""SELECT DISTINCT e.series_key,e.anchor_kind FROM episode_events ee
                JOIN episodes e ON e.episode_id=ee.episode_id JOIN source_events s ON s.event_id=ee.source_ref AND s.source_revision=ee.source_revision
                JOIN json_each(s.extra_json,'$.display_snapshot.items') d
                WHERE e.scope_id=? AND e.project_id IS ? AND e.branch_id IS ? AND e.read_blocked=0 AND s.read_blocked=0
                AND NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=s.source_group_key AND newer.source_revision>s.source_revision)
                AND ({pairs}) LIMIT 2""",
                (source.scope_id, source.project_id, source.branch_id, *params),
            ).fetchall()
            if len(candidates) == 1:
                return candidates[0]["series_key"], candidates[0]["anchor_kind"]
        previous = conn.execute(
            """SELECT e.series_key,e.episode_id FROM episode_events ee JOIN source_events s
            ON s.event_id=ee.source_ref AND s.source_revision=ee.source_revision JOIN episodes e ON e.episode_id=ee.episode_id
            WHERE s.session_id=? AND s.scope_id=? AND s.project_id IS ? AND s.branch_id IS ? AND e.read_blocked=0
            ORDER BY ee.sequence DESC LIMIT 1""",
            (source.session_id, source.scope_id, source.project_id, source.branch_id),
        ).fetchone()
        topic_break = source_origin(source) == "human_direct" and TOPIC_BREAK.search(source.event["content"])
        if previous and not topic_break:
            return previous["series_key"], "session"
        return hashlib.sha256(canonical([*prefix, "session", source.session_id, source.ref]).encode()).hexdigest(), "session"

    def _segment_index(self, series: str) -> int:
        """Episodes roll to a new segment once the current one holds 200 events."""
        segment = self.tx._check(write=True).execute(
            """SELECT e.segment_index,(SELECT count(*) FROM episode_events ee WHERE ee.episode_id=e.episode_id) AS count
            FROM episodes e WHERE e.series_key=? ORDER BY e.segment_index DESC LIMIT 1""",
            (series,),
        ).fetchone()
        if segment is None:
            return 0
        return segment["segment_index"] + (1 if segment["count"] >= 200 else 0)

    def attach(self, source, now):
        conn, ctx = self.tx._check(write=True), self.tx.context
        if self.source_episode(source.ref, source.revision):
            return
        series, kind = self._series_for(source)
        segment_index = self._segment_index(series)
        anchor = hashlib.sha256(canonical([series, segment_index]).encode()).hexdigest()
        ref = "episode-" + hashlib.sha256(anchor.encode()).hexdigest()
        if not allowed(self.tx, "episode", ref):
            raise ContractError("SOURCE_MISSING", "episode_unavailable")
        row = conn.execute("SELECT current_revision FROM episodes WHERE anchor_key=?", (anchor,)).fetchone()
        previous = self.get(ref) if row else None
        if row is None:
            conn.execute(
                "INSERT INTO episodes(episode_id,scope_id,project_id,branch_id,anchor_key,anchor_kind,series_key,segment_index) VALUES (?,?,?,?,?,?,?,?)",
                (ref, source.scope_id, source.project_id, source.branch_id, anchor, kind, series, segment_index),
            )
        revision = (row[0] + 1) if row else 1
        state = state_from_sources((source,), previous=previous.state if previous else "unknown")
        if previous:
            latest = self._latest_occurrence(ref)
            occurred_at = canonical_time(source.event["occurred_at"])
            if latest and (occurred_at is None or occurred_at < latest):
                state = previous.state
        prior = conn.execute(
            "SELECT processed_sequence,resume_json,source_watermark,environment_revision FROM episode_versions WHERE episode_id=? AND revision=?",
            (ref, row[0]),
        ).fetchone() if row else None
        conn.execute(
            "INSERT INTO episode_versions(episode_id,revision,state,resume_json,source_watermark,processed_sequence,recorded_at,environment_revision) VALUES (?,?,?,?,?,?,?,?)",
            (
                ref, revision, state,
                prior["resume_json"] if prior else None,
                prior["source_watermark"] if prior else source_watermark(()),
                prior["processed_sequence"] if prior else 0,
                now,
                prior["environment_revision"] if prior and prior["resume_json"] else ctx.environment_revision,
            ),
        )
        conn.execute(
            "UPDATE episodes SET current_revision=?,suppressed=max(suppressed,?) WHERE episode_id=?",
            (revision, int(source.suppressed), ref),
        )
        conn.execute(
            "INSERT INTO episode_events(episode_id,source_ref,source_revision,membership,environment_revision) VALUES (?,?,?,?,?)",
            (ref, source.ref, source.revision, "anchored" if kind != "session" else "provisional", ctx.environment_revision),
        )
        # One lineage row per source, at the revision it entered: a revision's
        # evidence is every row at or below it, so nothing is copied forward.
        lineage.link(conn, "episode", ref, revision, source.ref, source.revision)

    def sources(self, ref, *, after_sequence=0, limit=32):
        if self.get(ref) is None:
            raise ContractError("SOURCE_MISSING")
        if (
            type(limit) is not int
            or not 1 <= limit <= 200
            or type(after_sequence) is not int
            or after_sequence < 0
        ):
            raise ContractError("INPUT_INVALID", "episode_page")
        rows = (
            self.tx._check()
            .execute(
                "SELECT sequence,source_ref,source_revision FROM episode_events WHERE episode_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (ref, after_sequence, limit + 1),
            )
            .fetchall()
        )
        items = tuple(
            (r[0], s)
            for r in rows[:limit]
            if (s := self.tx.source(r[1], r[2])) is not None
        )
        return items, (rows[limit - 1][0] if len(rows) > limit else None)

    def _resume_sources(self, proposal, scope_id):
        """Resolve the cited sources and the single live episode they all belong to."""
        ctx = self.tx.context
        sources = [self.tx.source(*parse_source_ref(ref)) for ref in proposal["evidence_refs"]]
        if any(s is None or (s.scope_id, s.project_id, s.branch_id) != (scope_id, ctx.project_id, ctx.branch_id) for s in sources):
            raise ContractError("SOURCE_MISSING")
        for source in sources:
            self.tx.claims.require_live_source(source.ref, source.revision)
        episodes: set[str] = set()
        for source in sources:
            episode = self.source_episode(source.ref, source.revision)
            if episode is None:
                raise ContractError("SOURCE_MISSING")
            episodes.add(episode.ref)
        if len(episodes) != 1:
            raise ContractError("DERIVATION_INVALID", "episode_membership_ambiguous")
        return sources, next(iter(episodes))

    def _require_artifact_evidence(self, payload, sources) -> None:
        """Every artifact version a resume names must be observed by, or cited from, its evidence."""
        for artifact in payload["artifact_refs"]:
            target, revision = parse_source_ref(artifact)
            item = self.tx.artifacts.get(target, revision)
            if item is None:
                raise ContractError("SOURCE_MISSING")
            exact_version = dict(artifact_ref=target, revision=revision)
            observed = any(exact_version in s.event.get("display_snapshot", {}).get("items", []) for s in sources)
            if not observed and not set(item.evidence_refs).intersection(payload["evidence_refs"]):
                raise ContractError("DERIVATION_INVALID", "artifact_version_evidence")

    def _processed_sequence(self, ref: str, current_revision: int, declared: set[str]) -> int:
        """Advance the processed watermark over the leading events the resume declares."""
        conn = self.tx._check(write=True)
        prior = conn.execute(
            "SELECT processed_sequence FROM episode_versions WHERE episode_id=? AND revision=?",
            (ref, current_revision),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT sequence,source_ref,source_revision FROM episode_events WHERE episode_id=? AND sequence>? ORDER BY sequence",
            (ref, prior),
        ).fetchall()
        processed = prior
        for row in rows:
            if f"{row[1]}@{row[2]}" not in declared:
                break
            processed = row[0]
        return processed

    def _resume_state(self, ref: str, sources, current) -> str:
        """A resume built from evidence older than the latest event cannot move the state."""
        latest = self._latest_occurrence(ref)
        used_times = [t for s in sources if s.event["occurred_at"] if (t := canonical_time(s.event["occurred_at"])) is not None]
        if latest and (not used_times or max(used_times) < latest):
            return current.state
        return state_from_sources(sources, has_goal=True, previous=current.state)

    def apply_resume(self, proposal, scope_id, now):
        conn, ctx = self.tx._check(write=True), self.tx.context
        sources, ref = self._resume_sources(proposal, scope_id)
        current = self.get(ref)
        if current is None:
            raise ContractError("SOURCE_MISSING")
        if proposal["episode_ref"] not in {None, ref}:
            raise ContractError("DERIVATION_INVALID", "episode_membership")
        qualify_resume(proposal, sources)
        if not ctx.task_anchor and not supported_work_goal(proposal, sources):
            raise ContractError("DERIVATION_INVALID", "work_goal_unconfirmed")
        payload = dict(proposal, episode_ref=ref)
        if current.resume == payload:
            return current
        self._require_artifact_evidence(payload, sources)
        revision = current.revision + 1
        processed = self._processed_sequence(ref, current.revision, set(payload["evidence_refs"]))
        state = self._resume_state(ref, sources, current)
        conn.execute(
            "INSERT INTO episode_versions VALUES (?,?,?,?,?,?,?,?)",
            (ref, revision, state, canonical(payload), payload["source_watermark"], processed, now, ctx.environment_revision),
        )
        conn.execute("UPDATE episodes SET current_revision=? WHERE episode_id=?", (revision, ref))
        for source in sources:
            # A cited source is a member and already carries its lineage row;
            # the write covers a converted episode that never had one.
            lineage.link_unless_carried(conn, "episode", ref, revision, source.ref, source.revision)
        for artifact in payload["artifact_refs"]:
            target, version = parse_source_ref(artifact)
            lineage.depend_unless_carried(conn, "episode", ref, revision, "artifact", target, version)
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        return self.get(ref)
