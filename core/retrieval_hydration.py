"""Hydration: a recall candidate turned into the object a packet carries.

The event, claim, episode or artifact a candidate names, with its evidence, the entries it came in through and
whether its sources are still live; the hydrating half of ``retrieval_storage.RetrievalStorage``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import timedelta, timezone
from typing import cast

from ..contracts import (
    ENTRY_LABELS_MAX_ITEMS,
    SOURCE_CONTEXTS_MAX_ITEMS,
    ContractError,
    EntryLabel,
    SourceContext,
    bounded_source_context,
)
from . import lineage
from .claim_storage import parse_source_ref
from .claims import select_effective, select_proposal
from .episodes import source_origin
from .recall_policy import applicability, in_time_window, parse_time
from .resume_compaction import resume_evidence_refs
from .retrieval import STALE_RESUME_GAPS, CandidateRef, RetrievedObject, SearchContext
from .visibility import CLOSED_INTENTION_STATES, allowed, allowed_refs

#: Modes in which a delivered source must still be live, not merely visible.
LIVE_MODES = frozenset({"auto", "current", "method"})


_HEAD_TABLES = {
    "episode": ("episodes", "episode_id"),
    "artifact": ("artifacts", "artifact_id"),
    "reference": ("reference_bindings", "reference_id"),
}


_RETAINED_ARTIFACT_STATES = frozenset({"retained_artifact", "described_artifact", "reference_only"})


#: Origins that are a first-hand record rather than an echo of the system's own
#: output. Matches the set already used by consolidation result validation.
_FIRST_HAND_ORIGINS = frozenset({"human_direct", "tool_observation"})


def source_key(ref: str, revision: int) -> str:
    return f"{ref}@{revision}"


def _is_source_ref(value: object) -> bool:
    try:
        parse_source_ref(cast(str, value))
    except ContractError:
        return False
    return True


def _prefetch(tx, pairs) -> None:
    """Load these source versions together, where the transaction can (``Transaction.prefetch_sources``)."""
    prefetch = getattr(tx, "prefetch_sources", None)
    if prefetch is not None:
        prefetch(tuple(pairs))


def _source_live(tx, ref: str, revision: int) -> bool:
    try:
        tx.claims.require_live_source(ref, revision)
    except ContractError:
        return False
    return True


def _has_first_hand_root(tx, evidence: Iterable[str]) -> bool:
    """Whether any evidence root is first-hand rather than the system's own echo.

    Admitting unpromoted proposals to recall must not admit claims derived only
    from assistant output or re-injected memory: that closes a loop in which the
    assistant's own words come back as remembered facts.  A promoted claim has
    already passed qualification, so this applies only to proposals.  Uses
    ``source_origin`` so an imported record is judged by its verified original
    lineage, not by the fact that it arrived through import.
    """
    for ref in evidence:
        try:
            source_ref, source_revision = parse_source_ref(ref)
        except ContractError:
            continue
        source = tx.source(source_ref, source_revision)
        if source is not None and source_origin(source) in _FIRST_HAND_ORIGINS:
            return True
    return False


def evidence_source_contexts(tx, evidence: Iterable[str]) -> list[SourceContext]:
    """Distinct, bounded source contexts of the sources behind ``evidence`` refs."""
    collected: list[SourceContext] = []
    for ref in evidence:
        try:
            source_ref, source_revision = parse_source_ref(ref)
        except ContractError:
            continue
        source = tx.source(source_ref, source_revision)
        if source is None:
            continue
        context = bounded_source_context(source.event.get("source_context"))
        if context is None or context in collected:
            continue
        collected.append(context)
        if len(collected) >= SOURCE_CONTEXTS_MAX_ITEMS:
            break
    return collected


def _source_contexts_metadata(contexts: list[SourceContext]) -> tuple[tuple[str, str], ...]:
    if not contexts:
        return ()
    return (("source_contexts", json.dumps(contexts, ensure_ascii=False, separators=(",", ":"))),)


def evidence_entries(tx, evidence: Iterable[str]) -> list[EntryLabel]:
    """The distinct entries behind ``evidence`` refs, ordered by id; none outside a shared store."""
    if tx.context.binding.installation_kind != "shared":
        return []
    collected: list[EntryLabel] = []
    for ref in evidence:
        try:
            source_ref, source_revision = parse_source_ref(ref)
        except ContractError:
            continue
        source = tx.source(source_ref, source_revision)
        label = tx.entry_label(source.entry_id) if source is not None else None
        if label is None or label in collected:
            continue
        collected.append(label)
        if len(collected) >= ENTRY_LABELS_MAX_ITEMS:
            break
    return sorted(collected, key=lambda label: label["id"])


def _entries_metadata(labels: list[EntryLabel]) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return (("entries", json.dumps(labels, ensure_ascii=False, separators=(",", ":"))),)


#: How far back a reply's turn is looked for.  A turn that ran longer than this
#: is judged from its last two hours only.
_TURN_LOOKBACK = timedelta(hours=2)


#: Memory lookups in one turn that make its reply a restatement.  Replies that
#: tested recall ran 3 to 15 of them; replies that used memory to answer a
#: question mostly ran one or two, and those stay evidence.
RECALL_ECHO_MIN_LOOKUPS = 3


#: What a lookup that returned memory looks like: recall items, entity
#: statements, profile sections.  A status lookup returns none of them.
_MEMORY_RESULT = (
    "instr(content,'\"items\":')>0 OR instr(content,'\"statements\":')>0 OR instr(content,'\"sections\":')>0"
)


def _second_precision(moment) -> str:
    """A bound that compares correctly with stored ISO times, whatever their fraction and zone suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def recall_echo(tx, source) -> bool:
    """Whether an assistant reply restates what memory lookups in its turn returned.

    an agent that tests its own recall and reports the queries and what came back
    has the report come back first for those very queries, above the evidence
    it quoted.  The turn is the stretch since the session's last user message,
    which the host captures before any tool runs, and the reply is dated as the
    store can best tell (``witnessed_at``), since that report's own row carried
    a day-old time.
    """
    if source.event.get("origin") != "assistant_visible":
        return False
    stamp = tx.sources.witnessed_at(source)
    if stamp is None:
        return False
    try:
        at = parse_time(stamp)
    except ContractError:
        return False
    conn = tx._check()
    upper = _second_precision(at + timedelta(seconds=1))
    lower = _second_precision(at - _TURN_LOOKBACK)
    started = conn.execute(
        """SELECT max(occurred_at) FROM source_events WHERE scope_id=? AND occurred_at>=? AND occurred_at<?
           AND session_id=? AND role='user' AND origin!='memory_reinjection'""",
        (source.scope_id, lower, upper, source.session_id),
    ).fetchone()[0]
    lookups = conn.execute(
        f"""SELECT count(*) FROM (SELECT 1 FROM source_events WHERE scope_id=? AND occurred_at>=? AND occurred_at<?
            AND session_id=? AND origin='memory_reinjection' AND ({_MEMORY_RESULT}) LIMIT ?)""",
        (source.scope_id, started or lower, upper, source.session_id, RECALL_ECHO_MIN_LOOKUPS),
    ).fetchone()[0]
    return lookups >= RECALL_ECHO_MIN_LOOKUPS


def _occurred_metadata(stamp: str | None) -> tuple[tuple[str, str], ...]:
    return (("occurred_at", stamp),) if stamp else ()


def _newest(stamps: Iterable[str | None]) -> str | None:
    """The latest of several ISO-8601 times; unparseable ones are skipped."""
    newest: tuple[object, str] | None = None
    for stamp in stamps:
        if not stamp:
            continue
        try:
            parsed = parse_time(stamp)
        except ContractError:
            continue
        if newest is None or parsed > newest[0]:
            newest = (parsed, stamp)
    return newest[1] if newest else None


def _claim_content(version) -> str:
    """Keep the complete qualified payload available to the packet compiler."""
    return json.dumps(version.payload, ensure_ascii=False, sort_keys=True)


def _claim_statement_content(version) -> str:
    """What a reader is shown for a claim: its payload without the quoted spans.

    The spans are verbatim evidence, and a tool-derived claim quotes escaped
    JSON: two such claims can use most of a 4096-unit packet and push the
    answering messages out.  The item's evidence_refs already name every
    source, the full payload stays in metadata, and nothing on the read path
    matches against the quotes.
    """
    return json.dumps(
        {key: value for key, value in version.payload.items() if key != "evidence_spans"},
        ensure_ascii=False,
        sort_keys=True,
    )


def _claim_status(version, current_effective: bool) -> str:
    if version.state == "proposed":
        return "historical"
    if version.state == "disputed":
        return "disputed"
    if current_effective or version.revision == version.current_revision:
        return "current"
    return "historical"


def _versioned_body(kind: str, obj) -> tuple[str, str, bool]:
    """Content, basis/origin, and whether the object's status is unknowable."""
    if kind == "episode":
        return (
            json.dumps(obj.resume or {"state": obj.state}, ensure_ascii=False, sort_keys=True),
            "derived_summary",
            False,
        )
    if kind == "artifact":
        return obj.label, "observed", obj.retention_state not in _RETAINED_ARTIFACT_STATES
    payload = json.dumps(obj.payload, ensure_ascii=False, sort_keys=True)
    return payload, "derived_summary", obj.payload.get("resolution") in {"ambiguous", "unresolved"}


class RetrievalHydration:
    """The hydrating half of ``RetrievalStorage``: candidates in, packet objects out."""

    def _evidence(self, tx, kind: str, ref: str, revision: int, context: SearchContext) -> tuple[str, ...] | None:
        """The object's evidence refs, or ``None`` when any of them is not deliverable in this mode."""
        return self._deliverable(tx, tuple(lineage.evidence(tx._check(), kind, ref, revision)), context)

    def _deliverable(self, tx, pairs, context: SearchContext) -> tuple[str, ...] | None:
        """The refs of these source versions, or ``None`` when one is not deliverable in this mode.

        They are loaded together first (``Transaction.prefetch_sources``): what follows reads each of them again.
        """
        _prefetch(tx, pairs)
        refs = []
        for source_ref, source_revision in pairs:
            source = tx.source(source_ref, source_revision)
            if source is None or (source.suppressed and context.mode == "auto"):
                return None
            if context.mode in LIVE_MODES and not _source_live(tx, source.ref, source.revision):
                return None
            refs.append(f"{source_ref}@{source_revision}")
        return tuple(refs)

    def prefetch(self, tx, candidates, context: SearchContext) -> None:
        """Load what hydrating these candidates reads first, together: their visibility, the events' rows and the
        claims' versions (``Transaction.remembered``).  Each was read on its own, and in a busy Hermes gateway every
        statement and row waited for the GIL, so a recall ran past its deadline in every stage (3.7.7)."""
        if not getattr(tx, "remembers", False):
            return
        by_kind: dict[str, list[CandidateRef]] = {}
        for candidate in candidates:
            by_kind.setdefault(candidate.kind, []).append(candidate)
        for kind, items in by_kind.items():
            allowed_refs(tx, kind, (item.ref for item in items), automatic=context.mode == "auto")
        _prefetch(tx, ((item.ref, item.revision) for item in by_kind.get("event", ())))
        if "claim" in by_kind:
            tx.claims.prefetch_versions(item.ref for item in by_kind["claim"])

    def hydrate(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        if not allowed(tx, candidate.kind, candidate.ref, automatic=context.mode == "auto"):
            return None
        load = {"event": self._hydrate_event, "claim": self._hydrate_claim}.get(candidate.kind, self._hydrate_versioned)
        return load(tx, candidate, context)

    def _hydrate_event(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        source = tx.source(candidate.ref, candidate.revision)
        if source is None or (context.mode == "auto" and source.suppressed):
            return None
        if context.mode in LIVE_MODES and not _source_live(tx, candidate.ref, candidate.revision):
            return None
        if not in_time_window(source.event.get("occurred_at"), context):
            return None
        newer_sql = "SELECT 1 FROM source_events newer WHERE newer.source_group_key=(SELECT source_group_key FROM source_events WHERE event_id=? AND source_revision=?) AND newer.source_revision>?"
        newer_params: list[object] = [candidate.ref, candidate.revision, candidate.revision]
        if context.as_of is not None:
            newer_sql += " AND (newer.occurred_at IS NULL OR newer.occurred_at<=?)"
            newer_params.append(context.as_of)
        superseded = tx._check().execute(newer_sql, newer_params).fetchone() is not None
        event = source.event
        context_meta = bounded_source_context(event.get("source_context"))
        return RetrievedObject(
            candidate.ref,
            candidate.revision,
            "event",
            event["content"],
            event["origin"],
            "historical" if superseded else "current",
            applicability(context, source.project_id, source.branch_id),
            (source_key(candidate.ref, candidate.revision),),
            "direct_report" if event["origin"] == "human_direct" else "observed",
            True,
            ("event",),
            metadata=(
                *_source_contexts_metadata([context_meta] if context_meta is not None else []),
                *_entries_metadata([label] if (label := tx.entry_label(source.entry_id)) is not None else []),
                *_occurred_metadata(tx.sources.witnessed_at(source)),
                *((("recall_echo", "true"),) if context.mode in LIVE_MODES and recall_echo(tx, source) else ()),
            ),
        )

    def _hydrate_claim(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        versions = tx.claims.versions(candidate.ref)
        version = next((item for item in versions if item.revision == candidate.revision), None)
        if version is None or (context.mode == "auto" and version.suppressed):
            return None
        instant = context.as_of or context.now
        effective = select_effective(versions, instant, as_of=context.mode == "as_of")
        admitted_proposal = False
        if context.mode != "history":
            if effective is not None:
                if effective.revision != version.revision:
                    return None
            else:
                # A claim qualification never promoted has no effective version,
                # which on a real instance is almost the whole derived layer.
                # Admit the proposal head instead; it stays labelled below
                # (temporal_status "historical", claim_state and
                # qualification_reason in metadata) so it cannot be mistaken
                # for settled truth.  as_of stays excluded: a proposal answers
                # nothing about a past instant.
                proposal = None if context.mode == "as_of" else select_proposal(versions, instant)
                if proposal is None or proposal.revision != version.revision:
                    return None
                admitted_proposal = True
        payload = version.payload
        intention = payload.get("intention") if payload.get("kind") == "intention" else None
        if intention and intention.get("state") in CLOSED_INTENTION_STATES and context.mode in LIVE_MODES:
            return None
        evidence = self._evidence(tx, "claim", candidate.ref, candidate.revision, context)
        if evidence is None:
            return None
        if admitted_proposal and not _has_first_hand_root(tx, evidence):
            # Derived only from the system's own echo and never promoted:
            # recalling it would let the assistant's output become memory.
            return None
        current_effective = (
            context.mode in LIVE_MODES and effective is not None and effective.revision == version.revision
        )
        origins = []
        witnessed = []
        for ref in evidence:
            source = tx.source(*parse_source_ref(ref))
            if source is not None:
                origins.append(source.event["origin"])
                witnessed.append(tx.sources.witnessed_at(source))
        origin = next((item for item in origins if item == "human_direct"), origins[0] if origins else "origin_unknown")
        metadata = (
            ("payload_json", _claim_content(version)),
            ("state", version.state),
            ("basis", version.basis),
            # Which gate rejected it, so a reader can tell a weak quote from an
            # unasserted question or a missing condition.
            ("qualification_reason", version.reason),
            *_source_contexts_metadata(evidence_source_contexts(tx, evidence)),
            # Every entry any of its evidence came in through.
            *_entries_metadata(evidence_entries(tx, evidence)),
            # A claim was last said when its newest evidence was.
            *_occurred_metadata(_newest(witnessed)),
        )
        return RetrievedObject(
            candidate.ref,
            candidate.revision,
            "procedure" if payload.get("kind") == "procedure" else "claim",
            _claim_statement_content(version),
            origin,
            _claim_status(version, current_effective),
            applicability(context, version.project_id, version.branch_id),
            evidence,
            version.basis,
            True,
            ("claim",),
            metadata=metadata,
        )

    def _hydrate_versioned(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        """Episodes, artifacts and references: head-revision objects with evidence links."""
        repositories = {"episode": tx.episodes, "artifact": tx.artifacts, "reference": tx.references}
        obj = repositories[candidate.kind].get(
            candidate.ref, candidate.revision if context.mode in {"history", "as_of"} else None
        )
        if obj is None or (context.mode == "auto" and obj.suppressed) or obj.revision != candidate.revision:
            return None
        table, key = _HEAD_TABLES[candidate.kind]
        head = tx._check().execute(f"SELECT current_revision FROM {table} WHERE {key}=?", (candidate.ref,)).fetchone()
        if head is None:
            return None
        object_gaps = getattr(obj, "gaps", ())
        if context.mode in LIVE_MODES and any(gap in object_gaps for gap in STALE_RESUME_GAPS):
            return None
        if candidate.kind == "episode" and getattr(obj, "resume", None):
            # A resume is derived from what it cites.  The members are its
            # context, not its evidence, and a 200-member list is not a packet
            # item (fits_packet_schema): the cited versions are what is checked
            # for delivery and what the packet carries.
            refs = tuple(
                dict.fromkeys(
                    ref for ref in resume_evidence_refs(obj.resume) if type(ref) is str and _is_source_ref(ref)
                )
            )
            evidence = self._deliverable(tx, tuple(parse_source_ref(ref) for ref in refs), context)
        else:
            evidence = self._evidence(tx, candidate.kind, candidate.ref, candidate.revision, context)
        if evidence is None:
            return None
        content, basis, status_unknown = _versioned_body(candidate.kind, obj)
        if status_unknown:
            status = "unknown"
        else:
            status = "current" if candidate.revision == head["current_revision"] else "historical"
        metadata = [("gaps", json.dumps(tuple(object_gaps), ensure_ascii=False))]
        metadata.extend(_source_contexts_metadata(evidence_source_contexts(tx, evidence)))
        metadata.extend(_entries_metadata(evidence_entries(tx, evidence)))
        if candidate.kind == "episode":
            metadata.extend(self._episode_source_metadata(tx, candidate.ref, obj))
        applies = applicability(context, obj.project_id, obj.branch_id)
        if "environment_needs_revalidation" in object_gaps:
            status = "historical"
            applies += "; environment_needs_revalidation"
        return RetrievedObject(
            candidate.ref,
            candidate.revision,
            candidate.kind,
            content,
            basis,
            status,
            applies,
            evidence,
            basis,
            True,
            (candidate.kind,),
            metadata=tuple(metadata),
        )

    @staticmethod
    def _episode_source_metadata(tx, episode_id: str, obj) -> list[tuple[str, str]]:
        """Trusted event order and fresh text for the refs this resume retains.

        The episode table may hold more events than the relation budget; a late
        correction must not disappear merely because an unrelated earlier event
        consumed a LIMIT.  Only the schema's 32 retained refs are queried, by
        *versioned* pair, so no other revision of a source is ever read.
        """
        retained = [
            ref
            for ref in dict.fromkeys((*obj.evidence_refs, *resume_evidence_refs(obj.resume)))
            if type(ref) is str and _is_source_ref(ref)
        ][:32]
        rows = []
        texts: dict[str, str] = {}
        if retained:
            pairs = [parse_source_ref(ref) for ref in retained]
            pair_marks = ",".join("(?, ?)" for _ in pairs)
            # One row, ordered here: each row read on its own waited for the GIL in a busy gateway (3.7.7).
            rows = sorted(
                json.loads(
                    tx._check()
                    .execute(
                        f"""SELECT json_group_array(json_object('sequence',sequence,'source_ref',source_ref,
                       'source_revision',source_revision)) FROM (
                   SELECT sequence,source_ref,source_revision
                   FROM episode_events
                   WHERE episode_id=? AND (source_ref,source_revision) IN ({pair_marks}))""",
                        (episode_id, *(value for pair in pairs for value in pair)),
                    )
                    .fetchone()[0]
                ),
                key=lambda row: row["sequence"],
            )
            _prefetch(tx, ((row["source_ref"], row["source_revision"]) for row in rows))
            for row in rows:
                source = tx.source(row["source_ref"], row["source_revision"])
                if source is not None:
                    texts[source_key(str(row["source_ref"]), int(row["source_revision"]))] = str(
                        source.event.get("content", "")
                    )
        order = [[str(row["source_ref"]), int(row["source_revision"]), int(row["sequence"])] for row in rows]
        return [
            ("source_order", json.dumps(order, ensure_ascii=False, separators=(",", ":"))),
            ("source_texts", json.dumps(texts, ensure_ascii=False, separators=(",", ":"))),
        ]
