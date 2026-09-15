"""Read-only SQLite collection, hydration, and collection pagination.

This module is intentionally coupled to the existing ``Transaction`` surface,
which remains the only authority for source visibility and versioned objects.
No function here writes, schedules work, increments counters, or releases text
outside the trusted context.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Iterable, cast

from ..contracts import (
    SOURCE_CONTEXTS_MAX_ITEMS,
    ContractError,
    SourceContext,
    bounded_source_context,
)
from .claim_storage import parse_source_ref
from .claims import canonical_time, select_effective, select_proposal
from .delete_storage import canonical
from .episodes import source_origin
from .recall_policy import applicability, in_time_window, meaningful_query_terms, query_is_relevant
from .retrieval import CandidateRef, CollectionQuery, ObjectKind, PageCursor, RetrievedObject, SearchContext
from .visibility import allowed


def _collection_as_of(value: str | None) -> str | None:
    return canonical_time(value) if value is not None else None


def scope_digest(context) -> str:
    payload = [
        context.binding.agent_id,
        context.binding.installation_id,
        sorted(context.allowed_scope_ids),
        context.project_id,
        context.branch_id,
    ]
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _marks(values: Iterable[object]) -> str:
    values = tuple(values)
    if not values:
        raise ContractError("ACCESS_DENIED", "scope")
    return ",".join("?" for _ in values)


def _source_key(ref: str, revision: int) -> str:
    return f"{ref}@{revision}"


#: Origins that are a first-hand record rather than an echo of the system's own
#: output. Matches the set already used by consolidation result validation.
_FIRST_HAND_ORIGINS = frozenset({"human_direct", "tool_observation"})


def _has_first_hand_root(tx, evidence: Iterable[str]) -> bool:
    """Whether any evidence root is first-hand rather than the system's own echo.

    Admitting unpromoted proposals to recall must not admit claims derived only
    from assistant output or re-injected memory: that closes a loop in which the
    assistant's own words come back as remembered facts. A promoted claim has
    already passed qualification, so this applies only to proposals.

    Uses ``source_origin`` so an imported record is judged by its verified
    original lineage, not by the fact that it arrived through import.
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


#: A term in at least this share of all sources tells the ranker nothing: it
#: matches most of the corpus, so it separates nothing while costing the longest
#: posting list in the index. Measured on tianshu, ten terms cleared this bar —
#: `tool`, `summary`, `omitted`, `execution`, `output_chars`, `output_preview`,
#: `terminal`, `exit_code`, `status`, `0` — every one a JSON field name from
#: tool-observation envelopes rather than anything a person wrote, and together
#: 10.3% of the whole index. A query containing one of them had to walk an
#: 18,000-row posting list to learn nothing.
_LEXICAL_DF_FRACTION = 0.10
#: Floor so a young or small instance is never pruned: on a corpus of thirty
#: sources, "10% of everything" is three, and ordinary words would vanish.
_LEXICAL_DF_FLOOR = 64


def _discriminating_terms(tx, terms: tuple[str, ...]) -> tuple[str, ...]:
    """Drop query terms too common to separate anything.

    Document frequency is read once for the query's own terms, which is a
    clustered range scan: ``lexical_projection`` is WITHOUT ROWID keyed on
    ``(term, event_id, source_revision)``.

    If every term is that common the query keeps its rarest ones: answering from
    a weak signal beats answering from none, and the caller still sees whatever
    the vector and recent channels contribute.
    """
    conn = tx._check()
    marks = ",".join("?" for _ in terms)
    frequencies = {
        row[0]: int(row[1])
        for row in conn.execute(
            f"SELECT term,COUNT(*) FROM lexical_projection WHERE term IN ({marks}) GROUP BY term",
            terms,
        ).fetchall()
    }
    if not frequencies:
        return terms
    corpus = int(conn.execute("SELECT COUNT(*) FROM source_events").fetchone()[0] or 0)
    ceiling = max(_LEXICAL_DF_FLOOR, int(corpus * _LEXICAL_DF_FRACTION))
    kept = tuple(term for term in terms if frequencies.get(term, 0) < ceiling)
    if kept:
        return kept
    rarest = min(frequencies.values())
    return tuple(term for term in terms if frequencies.get(term, 0) == rarest) or terms


def _source_contexts_metadata(contexts: list[SourceContext]) -> tuple[tuple[str, str], ...]:
    if not contexts:
        return ()
    return (("source_contexts", json.dumps(contexts, ensure_ascii=False, separators=(",", ":"))),)


def _event_source_contexts(event: dict) -> list[SourceContext]:
    context = bounded_source_context(event.get("source_context"))
    return [context] if context is not None else []


def _evidence_source_contexts(tx, evidence: tuple[str, ...]) -> list[SourceContext]:
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


def _content_for_claim(version) -> str:
    """Keep the complete qualified payload available to the packet compiler."""

    return json.dumps(version.payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class CollectionPage:
    items: tuple[RetrievedObject, ...]
    next_cursor: PageCursor | None
    coverage: str
    memory_epoch: int


class RetrievalStorage:
    """Typed read boundary used by one ``RetrievalPipeline`` instance."""

    def __init__(self, *, clock=None):
        self.clock = clock if clock is not None else time

    def _remaining(self, context: SearchContext) -> float:
        return context.deadline - self.clock.monotonic()

    def epoch(self, tx) -> int:
        return tx.memory_epoch()

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        terms = meaningful_query_terms(context.query)
        if not terms:
            return ()
        terms = _discriminating_terms(tx, terms)
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        term_marks, scope_marks = _marks(terms), _marks(scopes)
        current = "" if context.mode in {"history", "as_of"} else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        as_of = ""
        params: list[object] = [*terms, *scopes, context.trusted_context.project_id, context.trusted_context.branch_id]
        if context.as_of is not None:
            as_of = " AND (e.occurred_at IS NULL OR e.occurred_at<=?)"
            params.append(context.as_of)
        rows = tx._check().execute(
            f"""SELECT e.event_id,e.source_revision,COUNT(DISTINCT p.term) AS hits,
                       GROUP_CONCAT(DISTINCT hex(p.term)) AS matched_term_hexes
                FROM lexical_projection p JOIN source_events e
                ON e.event_id=p.event_id AND e.source_revision=p.source_revision
                WHERE p.term IN ({term_marks}) AND e.scope_id IN ({scope_marks})
                  AND e.read_blocked=0 AND (e.project_id IS NULL OR e.project_id=?)
                  AND (e.branch_id IS NULL OR e.branch_id=?)
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.event_id AND b.read_blocked=1)
                  {current}{as_of}
                GROUP BY e.event_id,e.source_revision
                ORDER BY CASE
                    WHEN e.role='tool' AND (
                        e.origin='memory_reinjection'
                        OR (e.origin='imported' AND e.source_original_origin='memory_reinjection')
                    ) THEN 1 ELSE 0
                END,
                hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return tuple(
            CandidateRef(
                "event",
                row["event_id"],
                row["source_revision"],
                "lexical",
                rank=index,
                lexical_score=float(row["hits"]),
                matched_query_terms=tuple(sorted(
                    bytes.fromhex(encoded).decode("utf-8")
                    for encoded in row["matched_term_hexes"].split(",")
                )),
            )
            for index, row in enumerate(rows, 1)
        )

    def exact(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        refs = list(context.focus_refs)
        import re

        refs.extend(re.findall(r"(?:event|claim|episode|artifact|reference)-[A-Za-z0-9._/-]+@\d+", context.query))
        seen: set[tuple[str, str, int]] = set()
        candidates: list[CandidateRef] = []
        for raw in refs:
            try:
                identity, version = raw.rsplit("@", 1)
                revision = int(version)
            except (ValueError, AttributeError):
                continue
            if revision < 1:
                continue
            kind = next((name for name in ("event", "claim", "episode", "artifact", "reference") if identity.startswith(name + "-")), None)
            if kind is None:
                continue
            key = (kind, identity, revision)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(CandidateRef(cast(ObjectKind, kind), identity, revision, "exact_ref", rank=len(candidates) + 1))
            if len(candidates) >= limit:
                break
        return tuple(candidates)

    def recent(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        trusted = context.trusted_context
        scopes = tuple(sorted(trusted.allowed_scope_ids))
        excluded = set(context.current_source_refs)
        rows = tx._check().execute(
            f"""SELECT e.event_id,e.source_revision,e.content,e.recorded_at
                FROM source_events e JOIN work_items w
                ON w.subject_ref=e.event_id AND w.subject_revision=e.source_revision
                WHERE w.work_type='consolidate' AND w.state IN ('pending','leased')
                  AND e.session_id=? AND e.scope_id IN ({_marks(scopes)})
                  AND (e.project_id IS NULL OR e.project_id=?)
                  AND (e.branch_id IS NULL OR e.branch_id=?)
                  AND e.read_blocked=0 AND e.suppressed=0
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.event_id AND (b.read_blocked=1 OR b.suppressed=1))
                ORDER BY e.recorded_at DESC,e.event_id,e.source_revision DESC LIMIT ?""",
            (trusted.session_id, *scopes, trusted.project_id, trusted.branch_id, limit * 4),
        ).fetchall()
        result = []
        for row in rows:
            key = _source_key(row["event_id"], row["source_revision"])
            if key in excluded or not query_is_relevant(context.query, row["content"]):
                continue
            result.append(CandidateRef("event", row["event_id"], row["source_revision"], "recent_raw", rank=len(result) + 1, lexical_score=1.0))
            if len(result) >= limit:
                break
        return tuple(result)

    def related(self, tx, candidate: CandidateRef, *, limit: int) -> tuple[CandidateRef, ...]:
        rows = tx._check().execute(
            """SELECT object_kind,object_ref,object_revision FROM evidence_links
               WHERE source_ref=? UNION SELECT 'event',source_ref,source_revision
               FROM evidence_links WHERE object_kind=? AND object_ref=?
               UNION SELECT dependency_kind,dependency_ref,dependency_revision
               FROM object_dependencies WHERE object_kind=? AND object_ref=?
               UNION SELECT object_kind,object_ref,object_revision
               FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?
               ORDER BY object_kind,object_ref,object_revision LIMIT ?""",
            (candidate.ref, candidate.kind, candidate.ref, candidate.kind, candidate.ref, candidate.kind, candidate.ref, limit),
        ).fetchall()
        result = []
        seen: set[tuple[str, str, int]] = set()
        for row in rows:
            kind = row["object_kind"]
            if kind not in {"event", "claim", "episode", "artifact", "reference"}:
                continue
            key = (kind, row["object_ref"], row["object_revision"])
            if key in seen or key == candidate.key:
                continue
            seen.add(key)
            result.append(CandidateRef(kind, row["object_ref"], row["object_revision"], "relation", rank=len(result) + 1, lexical_score=1.0))
            if len(result) >= limit:
                break
        return tuple(result)

    def _evidence(self, tx, kind: str, ref: str, revision: int, context: SearchContext) -> tuple[str, ...] | None:
        rows = tx._check().execute(
            "SELECT source_ref,source_revision FROM evidence_links WHERE object_kind=? AND object_ref=? AND object_revision=? ORDER BY source_ref,source_revision",
            (kind, ref, revision),
        ).fetchall()
        refs = []
        for row in rows:
            source = tx.source(row["source_ref"], row["source_revision"])
            if source is None:
                return None
            if source.suppressed and context.mode == "auto":
                return None
            if context.mode in {"auto", "current", "method"}:
                try:
                    tx.claims.require_live_source(source.ref, source.revision)
                except ContractError:
                    return None
            if not in_time_window(source.event.get("occurred_at"), context):
                return None
            refs.append(_source_key(source.ref, source.revision))
        return tuple(refs)

    def hydrate(self, tx, candidate: CandidateRef, context: SearchContext) -> RetrievedObject | None:
        automatic = context.mode == "auto"
        if not allowed(tx, candidate.kind, candidate.ref, automatic=automatic):
            return None
        if candidate.kind == "event":
            obj = tx.source(candidate.ref, candidate.revision)
            if obj is None or (automatic and obj.suppressed):
                return None
            if context.mode in {"auto", "current", "method"}:
                try:
                    tx.claims.require_live_source(candidate.ref, candidate.revision)
                except ContractError:
                    return None
            if not in_time_window(obj.event.get("occurred_at"), context):
                return None
            newer_sql = "SELECT 1 FROM source_events newer WHERE newer.source_group_key=(SELECT source_group_key FROM source_events WHERE event_id=? AND source_revision=?) AND newer.source_revision>?"
            newer_params = [candidate.ref, candidate.revision, candidate.revision]
            if context.as_of is not None:
                newer_sql += " AND (newer.occurred_at IS NULL OR newer.occurred_at<=?)"
                newer_params.append(context.as_of)
            current = not bool(tx._check().execute(newer_sql, newer_params).fetchone())
            status = "current" if current else "historical"
            return RetrievedObject(
                candidate.ref,
                candidate.revision,
                "event",
                obj.event["content"],
                obj.event["origin"],
                status,
                applicability(context, obj.project_id, obj.branch_id),
                (_source_key(candidate.ref, candidate.revision),),
                "direct_report" if obj.event["origin"] == "human_direct" else "observed",
                True,
                ("event",),
                metadata=_source_contexts_metadata(_event_source_contexts(obj.event)),
            )

        if candidate.kind == "claim":
            versions = tx.claims.versions(candidate.ref)
            version = next((item for item in versions if item.revision == candidate.revision), None)
            if version is None or (automatic and version.suppressed):
                return None
            instant = context.as_of or context.now
            admitted_proposal = False
            effective = select_effective(
                versions,
                instant,
                as_of=context.mode == "as_of",
            )
            if context.mode in {"auto", "current", "method", "as_of"}:
                if effective is not None:
                    if effective.revision != version.revision:
                        return None
                else:
                    # A claim qualification never promoted has no effective
                    # version, so this filter dropped it from every mode but
                    # history — which on a real instance is almost the whole
                    # derived layer (231 proposed against 11 active on tianshu).
                    # Admit the proposal head instead. It stays labelled below:
                    # temporal_status "historical", plus claim_state and
                    # qualification_reason in metadata, so it cannot be mistaken
                    # for settled truth. as_of stays excluded — a proposal
                    # answers nothing about a past instant.
                    proposal = None if context.mode == "as_of" else select_proposal(versions, instant)
                    if proposal is None or proposal.revision != version.revision:
                        return None
                    admitted_proposal = True
            intention = version.payload.get("intention") if version.payload.get("kind") == "intention" else None
            if intention and intention.get("state") in {"completed", "cancelled", "expired"}:
                if context.mode in {"auto", "current", "method"}:
                    return None
            evidence = self._evidence(tx, "claim", candidate.ref, candidate.revision, context)
            if evidence is None:
                return None
            if admitted_proposal and not _has_first_hand_root(tx, evidence):
                # Derived only from the system's own echo and never promoted:
                # recalling it would let the assistant's output become memory.
                return None
            # The public value object has a deliberately small temporal status
            # vocabulary; retain proposal state in metadata while keeping it
            # historical so a proposal cannot be mistaken for current truth.
            current_effective = context.mode in {"auto", "current", "method"} and effective is not None and effective.revision == version.revision
            status = "historical" if version.state == "proposed" else "disputed" if version.state == "disputed" else "current" if current_effective or version.revision == version.current_revision else "historical"
            origins = []
            for ref in evidence:
                source_ref, source_revision = parse_source_ref(ref)
                source = tx.source(source_ref, source_revision)
                if source is not None:
                    origins.append(source.event["origin"])
            origin = next((item for item in origins if item == "human_direct"), origins[0] if origins else "origin_unknown")
            metadata = (
                ("payload_json", _content_for_claim(version)),
                ("state", version.state),
                ("basis", version.basis),
                # Which gate rejected it. Without this a reader sees an
                # unpromoted claim and has no way to tell whether it was a weak
                # quote, an unasserted question, or a missing condition.
                ("qualification_reason", version.reason),
                *_source_contexts_metadata(_evidence_source_contexts(tx, evidence)),
            )
            return RetrievedObject(candidate.ref, candidate.revision, "procedure" if version.payload.get("kind") == "procedure" else "claim", _content_for_claim(version), origin, status, applicability(context, version.project_id, version.branch_id), evidence, version.basis, True, ("claim",), metadata=metadata)

        repositories = {"episode": tx.episodes, "artifact": tx.artifacts, "reference": tx.references}
        obj = repositories[candidate.kind].get(candidate.ref, candidate.revision if context.mode in {"history", "as_of"} else None)
        if obj is None or (automatic and obj.suppressed) or obj.revision != candidate.revision:
            return None
        table, key = {
            "episode": ("episodes", "episode_id"),
            "artifact": ("artifacts", "artifact_id"),
            "reference": ("reference_bindings", "reference_id"),
        }[candidate.kind]
        head = tx._check().execute(
            f"SELECT current_revision FROM {table} WHERE {key}=?", (candidate.ref,)
        ).fetchone()
        if head is None:
            return None
        status = "current" if candidate.revision == head["current_revision"] else "historical"
        object_gaps = getattr(obj, "gaps", ())
        if context.mode in {"auto", "current", "method"} and any(gap in object_gaps for gap in ("resume_requires_rebuild", "source_version_changed", "environment_needs_revalidation")):
            return None
        if not in_time_window(getattr(obj, "recorded_at", None), context):
            if context.as_of is not None or context.mode in {"current", "auto", "method"}:
                evidence = self._evidence(tx, candidate.kind, candidate.ref, candidate.revision, context)
                if evidence is None:
                    return None
        evidence = self._evidence(tx, candidate.kind, candidate.ref, candidate.revision, context)
        if evidence is None:
            return None
        if candidate.kind == "episode":
            content = json.dumps(obj.resume or {"state": obj.state}, ensure_ascii=False, sort_keys=True)
            basis = "derived_summary"
            origin = "derived_summary"
        elif candidate.kind == "artifact":
            content = obj.label
            basis = "observed"
            origin = "observed"
            if obj.retention_state not in {"retained_artifact", "described_artifact", "reference_only"}:
                status = "unknown"
        else:
            content = json.dumps(obj.payload, ensure_ascii=False, sort_keys=True)
            basis = "derived_summary"
            origin = "derived_summary"
            if obj.payload.get("resolution") in {"ambiguous", "unresolved"}:
                status = "unknown"
        metadata = [("gaps", json.dumps(tuple(object_gaps), ensure_ascii=False))]
        metadata.extend(_source_contexts_metadata(_evidence_source_contexts(tx, evidence)))
        if candidate.kind == "episode":
            # Preserve the bounded, trusted event order for the refs actually
            # retained by this resume.  The episode table may contain more
            # than the relation budget; a late correction must not disappear
            # merely because an unrelated earlier event consumed a LIMIT.
            # Keep the schema's 32-ref ceiling and query only those refs.
            retained_refs: list[str] = []

            def collect_resume_refs(value: object) -> None:
                if isinstance(value, dict):
                    for key in ("evidence_refs", "next_step_evidence_refs"):
                        raw_refs = value.get(key)
                        if isinstance(raw_refs, list):
                            for raw_ref in raw_refs:
                                if type(raw_ref) is str and raw_ref not in retained_refs:
                                    try:
                                        parse_source_ref(raw_ref)
                                    except (TypeError, ValueError, ContractError):
                                        continue
                                    retained_refs.append(raw_ref)
                    for child in value.values():
                        collect_resume_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        collect_resume_refs(child)

            for raw_ref in obj.evidence_refs:
                if type(raw_ref) is str and raw_ref not in retained_refs:
                    try:
                        parse_source_ref(raw_ref)
                    except (TypeError, ValueError, ContractError):
                        continue
                    retained_refs.append(raw_ref)
            collect_resume_refs(obj.resume)
            retained_refs = retained_refs[:32]
            order_rows = []
            source_texts: dict[str, str] = {}
            if retained_refs:
                retained_pairs = [parse_source_ref(ref) for ref in retained_refs]
                # Match the retained *versioned* evidence identities.  A
                # source_ref may have many historical revisions; filtering by
                # ref first would make SQLite read every one of those rows
                # before Python discarded them.  The resume schema allows at
                # most 32 refs, so this tuple predicate is a fixed bounded
                # query and preserves the pair-level fence.
                pair_marks = ",".join("(?, ?)" for _ in retained_pairs)
                order_rows = tx._check().execute(
                    f"""SELECT sequence,source_ref,source_revision
                       FROM episode_events
                       WHERE episode_id=? AND (source_ref,source_revision) IN ({pair_marks})
                       ORDER BY sequence""",
                    (candidate.ref, *(value for pair in retained_pairs for value in pair)),
                ).fetchall()
                retained_keys = set(retained_refs)
                order_rows = [
                    row for row in order_rows
                    if _source_key(str(row["source_ref"]), int(row["source_revision"])) in retained_keys
                ]
                for row in order_rows:
                    key = _source_key(str(row["source_ref"]), int(row["source_revision"]))
                    source = tx.source(row["source_ref"], row["source_revision"])
                    if source is not None:
                        source_texts[key] = str(source.event.get("content", ""))
            source_order = [
                [str(row["source_ref"]), int(row["source_revision"]), int(row["sequence"])]
                for row in order_rows
            ]
            metadata.append(("source_order", json.dumps(source_order, ensure_ascii=False, separators=(",", ":"))))
            metadata.append(("source_texts", json.dumps(source_texts, ensure_ascii=False, separators=(",", ":"))))
        applies = applicability(context, obj.project_id, obj.branch_id)
        if "environment_needs_revalidation" in object_gaps:
            status = "historical"
            applies += "; environment_needs_revalidation"
        return RetrievedObject(candidate.ref, candidate.revision, candidate.kind, content, origin, status, applies, evidence, basis, True, (candidate.kind,), metadata=tuple(metadata))

    def collection(self, tx, context: SearchContext, query: CollectionQuery, cursor: PageCursor | None = None) -> CollectionPage:
        epoch = self.epoch(tx)
        expected_digest = scope_digest(context.trusted_context)
        if query.scope_digest != expected_digest or query.memory_epoch != epoch:
            raise ContractError("VERSION_CONFLICT", "collection_epoch")
        if query.mode != context.mode or _collection_as_of(query.as_of) != context.as_of:
            raise ContractError("ACCESS_DENIED", "collection_context")
        allowed_fields = {
            "event": {"ref", "scope_id", "project_id", "branch_id", "revision"},
            "claim": {"ref", "scope_id", "project_id", "branch_id", "revision", "kind", "subject", "predicate", "state"},
            "episode": {"ref", "scope_id", "project_id", "branch_id", "revision", "state"},
            "artifact": {"ref", "scope_id", "project_id", "branch_id", "revision", "label"},
            "reference": {"ref", "scope_id", "project_id", "branch_id", "revision"},
        }[query.object_kind]
        if any(key not in allowed_fields for key, _ in query.where):
            raise ContractError("INPUT_INVALID", "collection_where")
        if cursor is not None:
            if (cursor.memory_epoch, cursor.scope_digest, cursor.project_id, cursor.branch_id, cursor.mode, cursor.as_of, cursor.filters, cursor.object_kind) != (epoch, expected_digest, context.trusted_context.project_id, context.trusted_context.branch_id, context.mode, context.as_of, query.where, query.object_kind):
                raise ContractError("VERSION_CONFLICT", "cursor")
        hydrated: list[RetrievedObject] = []
        scan_cursor = cursor
        last_candidate: CandidateRef | None = None
        dropped_on_page = False
        has_more = False
        deadline_exceeded = False
        scan_budget = min(256, max(query.page_size + 1, query.page_size * 4))
        scanned = 0
        budget_exhausted = False
        while len(hydrated) < query.page_size and scanned < scan_budget:
            if self._remaining(context) <= 0:
                deadline_exceeded = True
                break
            fetch_limit = min(query.page_size + 1, scan_budget - scanned)
            batch = self._collection_candidates(tx, context, query, scan_cursor, limit=fetch_limit)
            if not batch:
                break
            scanned += len(batch)
            has_more = len(batch) > query.page_size or (fetch_limit <= query.page_size and len(batch) >= fetch_limit)
            for candidate in batch[:query.page_size]:
                if self._remaining(context) <= 0:
                    deadline_exceeded = True
                    break
                last_candidate = candidate
                obj = self.hydrate(tx, candidate, context)
                if obj is None:
                    dropped_on_page = True
                else:
                    hydrated.append(obj)
                if len(hydrated) >= query.page_size:
                    break
            if last_candidate is not None:
                scan_cursor = PageCursor(
                    epoch,
                    expected_digest,
                    context.trusted_context.project_id,
                    context.trusted_context.branch_id,
                    context.mode,
                    context.as_of,
                    query.where,
                    (last_candidate.kind, last_candidate.ref, last_candidate.revision),
                    query.object_kind,
                )
            if deadline_exceeded:
                break
            if not has_more:
                break
            if scanned >= scan_budget:
                budget_exhausted = True
                break
        next_cursor = scan_cursor if has_more and last_candidate is not None else None
        if (budget_exhausted or deadline_exceeded) and last_candidate is not None:
            next_cursor = scan_cursor
        if next_cursor is not None or dropped_on_page or budget_exhausted or deadline_exceeded:
            coverage = "partial"
        elif cursor is not None:
            coverage = "partial"
        else:
            coverage = "complete_for_query"
        return CollectionPage(tuple(hydrated), next_cursor, coverage, epoch)

    def _collection_candidates(self, tx, context: SearchContext, query: CollectionQuery, cursor: PageCursor | None, *, limit: int | None = None) -> tuple[CandidateRef, ...]:
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        # Versioned objects must be enumerated from their version tables.  The
        # parent tables carry only the live head and have no version state;
        # using them for history silently omitted revisions and made state
        # filters either invalid or falsely complete.
        table_map = {
            "event": ("source_events e", "e.event_id", "e.source_revision", "e", "e", "e"),
            "claim": ("claims c JOIN claim_versions v ON v.claim_id=c.claim_id", "c.claim_id", "v.revision", "c", "v", "c"),
            "episode": ("episodes e JOIN episode_versions v ON v.episode_id=e.episode_id", "e.episode_id", "v.revision", "e", "v", "e"),
            "artifact": ("artifacts a JOIN artifact_versions v ON v.artifact_id=a.artifact_id", "a.artifact_id", "v.revision", "a", "v", "a"),
            "reference": ("reference_bindings b JOIN reference_versions v ON v.reference_id=b.reference_id", "b.reference_id", "v.revision", "b", "v", "b"),
        }
        table, ref_col, rev_col, parent_alias, version_alias, scope_alias = table_map[query.object_kind]
        filters = [f"{scope_alias}.scope_id IN ({_marks(scopes)})", f"{parent_alias}.read_blocked=0", f"({scope_alias}.project_id IS NULL OR {scope_alias}.project_id=?)", f"({scope_alias}.branch_id IS NULL OR {scope_alias}.branch_id=?)"]
        params: list[object] = [*scopes, context.trusted_context.project_id, context.trusted_context.branch_id]
        filters.append(f"NOT EXISTS (SELECT 1 FROM object_blocks ob WHERE ob.object_kind=? AND ob.object_ref={ref_col} AND ob.read_blocked=1)")
        params.append(query.object_kind)
        for key, value in query.where:
            column = {"ref": ref_col, "revision": rev_col}.get(key, key)
            if key not in {"ref", "revision"}:
                column = f"{version_alias if key in {'state', 'label'} else parent_alias}.{key}"
            filters.append(f"{column}=?")
            if key == "revision":
                try:
                    params.append(int(value))
                except (TypeError, ValueError) as exc:
                    raise ContractError("INPUT_INVALID", "collection_where") from exc
            else:
                params.append(value)
        if context.mode == "current":
            if query.object_kind == "event":
                filters.append("NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)")
            elif query.object_kind != "claim":
                filters.append(f"{rev_col}={parent_alias}.current_revision")
        elif context.mode == "as_of" and context.as_of is not None:
            if query.object_kind == "event":
                filters.append(f"({parent_alias}.occurred_at IS NULL OR {parent_alias}.occurred_at<=?)")
                params.append(context.as_of)
                filters.append(
                    "NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key "
                    "AND newer.source_revision>e.source_revision AND (newer.occurred_at IS NULL OR newer.occurred_at<=?))"
                )
                params.append(context.as_of)
            elif query.object_kind == "claim":
                # Keep candidate enumeration broad and let hydrate() apply
                # the single P05 select_effective temporal contract.
                pass
            else:
                filters.append(f"({version_alias}.recorded_at IS NULL OR {version_alias}.recorded_at<=?)")
                params.append(context.as_of)
        if cursor is not None:
            last = cursor.last_sort_key
            filters.append(f"({ref_col} > ? OR ({ref_col} = ? AND {rev_col} > ?))")
            params.extend((last[1], last[1], last[2]))
        order_expr = f"{ref_col}, {rev_col}"
        fetch_limit = limit if limit is not None else query.page_size + 1
        rows = tx._check().execute(f"SELECT {ref_col} AS ref_sort,{rev_col} AS revision_sort FROM {table} WHERE {' AND '.join(filters)} ORDER BY {order_expr} LIMIT ?", (*params, fetch_limit)).fetchall()
        return tuple(CandidateRef(query.object_kind, row["ref_sort"], int(row["revision_sort"]), "exact_ref", rank=index) for index, row in enumerate(rows, 1))
