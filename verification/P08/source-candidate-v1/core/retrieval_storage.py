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
from typing import Iterable

from ..contracts import ContractError
from .claim_storage import parse_source_ref
from .claims import canonical_time, select_effective
from .delete_storage import canonical
from .recall_policy import RecallPolicy, applicability, in_time_window, meaningful_query_terms, query_is_relevant
from .retrieval import CandidateRef, CollectionQuery, PageCursor, RetrievedObject, SearchContext
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
        return tx.status().memory_epoch

    def lexical(self, tx, context: SearchContext, *, limit: int) -> tuple[CandidateRef, ...]:
        from .events import query_terms

        terms = meaningful_query_terms(context.query)
        if not terms:
            return ()
        scopes = tuple(sorted(context.trusted_context.allowed_scope_ids))
        term_marks, scope_marks = _marks(terms), _marks(scopes)
        current = "" if context.mode in {"history", "as_of"} else "AND NOT EXISTS (SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)"
        as_of = ""
        params: list[object] = [*terms, *scopes, context.trusted_context.project_id, context.trusted_context.branch_id]
        if context.as_of is not None:
            as_of = " AND (e.occurred_at IS NULL OR e.occurred_at<=?)"
            params.append(context.as_of)
        rows = tx._check().execute(
            f"""SELECT e.event_id,e.source_revision,COUNT(DISTINCT p.term) AS hits
                FROM lexical_projection p JOIN source_events e
                ON e.event_id=p.event_id AND e.source_revision=p.source_revision
                WHERE p.term IN ({term_marks}) AND e.scope_id IN ({scope_marks})
                  AND e.read_blocked=0 AND (e.project_id IS NULL OR e.project_id=?)
                  AND (e.branch_id IS NULL OR e.branch_id=?)
                  AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='event'
                      AND b.object_ref=e.event_id AND b.read_blocked=1)
                  {current}{as_of}
                GROUP BY e.event_id,e.source_revision
                ORDER BY hits DESC,e.occurred_at DESC,e.event_id,e.source_revision DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return tuple(
            CandidateRef("event", row["event_id"], row["source_revision"], "lexical", rank=index, lexical_score=float(row["hits"]))
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
            candidates.append(CandidateRef(kind, identity, revision, "exact_ref", rank=len(candidates) + 1))
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
               FROM evidence_links WHERE object_ref=?
               UNION SELECT dependency_kind,dependency_ref,dependency_revision
               FROM object_dependencies WHERE object_kind=? AND object_ref=?
               UNION SELECT object_kind,object_ref,object_revision
               FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?
               ORDER BY object_kind,object_ref,object_revision LIMIT ?""",
            (candidate.ref, candidate.ref, candidate.kind, candidate.ref, candidate.kind, candidate.ref, limit),
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
            return RetrievedObject(candidate.ref, candidate.revision, "event", obj.event["content"], obj.event["origin"], status, applicability(context, obj.project_id, obj.branch_id), (_source_key(candidate.ref, candidate.revision),), "direct_report" if obj.event["origin"] == "human_direct" else "observed", True, ("event",))

        if candidate.kind == "claim":
            versions = tx.claims.versions(candidate.ref)
            version = next((item for item in versions if item.revision == candidate.revision), None)
            if version is None or (automatic and version.suppressed):
                return None
            instant = context.as_of or context.now
            effective = select_effective(
                versions,
                instant,
                as_of=context.mode == "as_of",
            )
            if context.mode in {"auto", "current", "method", "as_of"}:
                if effective is None or effective.revision != version.revision:
                    return None
            intention = version.payload.get("intention") if version.payload.get("kind") == "intention" else None
            if intention and intention.get("state") in {"completed", "cancelled", "expired"}:
                if context.mode in {"auto", "current", "method"}:
                    return None
            evidence = self._evidence(tx, "claim", candidate.ref, candidate.revision, context)
            if evidence is None:
                return None
            # The public value object has a deliberately small temporal status
            # vocabulary; retain proposal state in metadata while keeping it
            # historical so a proposal cannot be mistaken for current truth.
            status = "historical" if version.state == "proposed" else "disputed" if version.state == "disputed" else "current" if version.revision == version.current_revision else "historical"
            origins = []
            for ref in evidence:
                source_ref, source_revision = parse_source_ref(ref)
                source = tx.source(source_ref, source_revision)
                if source is not None:
                    origins.append(source.event["origin"])
            origin = next((item for item in origins if item == "human_direct"), origins[0] if origins else "origin_unknown")
            metadata = (("payload_json", _content_for_claim(version)), ("state", version.state), ("basis", version.basis))
            return RetrievedObject(candidate.ref, candidate.revision, "procedure" if version.payload.get("kind") == "procedure" else "claim", _content_for_claim(version), origin, status, applicability(context, version.project_id, version.branch_id), evidence, version.basis, True, ("claim",), metadata=metadata)

        repositories = {"episode": tx.episodes, "artifact": tx.artifacts, "reference": tx.references}
        obj = repositories[candidate.kind].get(candidate.ref, candidate.revision if context.mode in {"history", "as_of"} else None)
        if obj is None or (automatic and obj.suppressed) or obj.revision != candidate.revision:
            return None
        object_gaps = getattr(obj, "gaps", ())
        if context.mode in {"auto", "current", "method"} and any(gap in object_gaps for gap in ("resume_requires_rebuild", "source_version_changed")):
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
            status = "current" if obj.revision == candidate.revision else "historical"
            origin = "derived_summary"
        elif candidate.kind == "artifact":
            content = obj.label
            basis = "observed"
            origin = "observed"
            status = "current" if obj.revision == candidate.revision else "historical"
            if obj.retention_state not in {"retained_artifact", "described_artifact", "reference_only"}:
                status = "unknown"
        else:
            content = json.dumps(obj.payload, ensure_ascii=False, sort_keys=True)
            basis = "derived_summary"
            status = "current" if obj.revision == candidate.revision else "historical"
            origin = "derived_summary"
            if obj.payload.get("resolution") in {"ambiguous", "unresolved"}:
                status = "unknown"
        return RetrievedObject(candidate.ref, candidate.revision, candidate.kind, content, origin, status, applicability(context, obj.project_id, obj.branch_id), evidence, basis, True, (candidate.kind,), metadata=(("gaps", json.dumps(tuple(object_gaps))),))

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
            else:
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
