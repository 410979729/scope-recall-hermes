"""Owner-authorized streaming reads for a portable memory export.

The local management authority owns who may read and which records are visible.
This module returns records in bounded pages and one fixed read snapshot; the
package format (JSONL, Markdown, hashes and the zip writer) lives in
``maintenance/memory_export.py``.  A fact, episode or attachment is selected
only when every source version it cites is itself visible and passes the same
literal filters the management inventory uses, so no exported citation points
outside the package.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from ..contracts import ContractError, TrustedContext, utc_timestamp
from . import lineage
from .claim_storage import parse_source_ref
from .delete_storage import retraction_after
from .management_query import MemoryFilter, source_metadata
from .source_records import StoredSource

#: Identities one source page loads before their rows are materialized.
_SOURCE_PAGE = 200
#: Object identities one claim, episode or artifact page reads.
_OBJECT_PAGE = 200
_MESSAGE_ORDER = (
    "e.scope_id,e.project_id,e.branch_id,e.entry_id,e.source_group_key,e.source_revision,e.segment_index,e.event_id"
)
#: A source version still has to answer through the normal transaction read,
#: which applies the owner scope, project, branch and deletion fences.
_SOURCE_ORDER = {
    "ref": "e.event_id, e.source_revision",
    "session": f"e.session_id, COALESCE(e.occurred_at, e.recorded_at), {_MESSAGE_ORDER}",
    "month": (
        "substr(COALESCE(e.occurred_at, e.recorded_at),1,7), e.session_id, "
        f"COALESCE(e.occurred_at, e.recorded_at), {_MESSAGE_ORDER}"
    ),
}


def _marks(values) -> str:
    return ",".join("?" for _ in values)


def utc_iso(value: str | None) -> str | None:
    """A stored timestamp as a UTC ``...Z`` string; ``None`` stays absent."""
    return None if value is None else utc_timestamp(value).replace("+00:00", "Z")


def _month(stamp: str) -> str:
    return utc_timestamp(stamp)[:7]


@dataclass(frozen=True)
class ClaimExport:
    """One selected claim and a single-use stream of its non-current revisions."""

    current: dict
    history: Iterator[dict]


@dataclass(frozen=True)
class AttachmentExport:
    """One retained artifact selected for the package, with the bytes read by the normal artifact boundary."""

    artifact_ref: str
    revision: int
    media_type: str
    sha256: str
    data: bytes
    suppressed: bool


class MemoryExport:
    """A snapshot-bound, page-at-a-time export reader for one management authority.

    Use it as a context manager.  Records are yielded only while the read
    transaction is open. ``publication`` checks that no deletion or suppression
    was recorded after this snapshot began and fences concurrent retractions
    until the writer has closed the completed package.
    """

    def __init__(self, core, context: TrustedContext, filters: MemoryFilter) -> None:
        if not isinstance(filters, MemoryFilter):
            raise ContractError("INPUT_INVALID", "filters")
        if filters.kind not in {None, "event", "claim", "episode"}:
            raise ContractError("INPUT_INVALID", "kind")
        self.core = core
        self.context = context
        self.filters = filters
        self.memory_epoch = 0
        self.exported_at = ""
        self._transaction = None
        self._opened = None
        self._labels: dict[str, dict] = {}

    def __enter__(self) -> MemoryExport:
        opened = self.core.storage.read(self.context)
        tx = opened.__enter__()
        success = False
        try:
            self.memory_epoch = tx.memory_epoch()
            self.exported_at = utc_timestamp(self.core.clock.utc_now()).replace("+00:00", "Z")
            entries = tx._check().execute("SELECT * FROM entries")
            self._labels = {row["entry_id"]: dict(row) for row in entries}
            success = True
        finally:
            if not success:
                opened.__exit__(None, None, None)
        self._opened = opened
        self._transaction = tx
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        opened, self._opened = self._opened, None
        self._transaction = None
        if opened is not None:
            opened.__exit__(exc_type, exc, tb)
        return False

    def _open(self):
        if self._transaction is None:
            raise ContractError("STORAGE_UNAVAILABLE", "export_closed")
        return self._transaction

    def verify(self) -> None:
        """Refuse publication when a deletion or suppression happened after the snapshot began."""
        scopes = {self.filters.scope} if self.filters.scope is not None else self.context.allowed_scope_ids
        with self.core.storage.read(self.context) as tx:
            if retraction_after(tx._check(), scopes, self.memory_epoch):
                raise ContractError("VERSION_CONFLICT", "memory_epoch")

    @contextmanager
    def publication(self):
        """Fence deletion until the verified manifest and archive footer are complete."""
        scopes = {self.filters.scope} if self.filters.scope is not None else self.context.allowed_scope_ids
        with self.core.storage.write(self.context) as tx:
            if retraction_after(tx._check(), scopes, self.memory_epoch):
                raise ContractError("VERSION_CONFLICT", "memory_epoch")
            yield

    def _partition_clauses(self, alias: str) -> tuple[list[str], list[object]]:
        """A store-wide owner context reads every stored partition; a scoped context keeps its old project rule."""
        clauses: list[str] = []
        params: list[object] = []
        if self.context.project_id is not None:
            clauses.append(f"({alias}.project_id IS NULL OR {alias}.project_id=?)")
            params.append(self.context.project_id)
        if self.context.branch_id is not None:
            clauses.append(f"({alias}.branch_id IS NULL OR {alias}.branch_id=?)")
            params.append(self.context.branch_id)
        return clauses, params

    def _view(self, tx, project: str | None, branch: str | None):
        """A repository view for one stored partition, sharing the outer connection and snapshot."""
        return tx.with_context(replace(self.context, project_id=project, branch_id=branch))

    # -- sources ---------------------------------------------------------

    def _matches(self, source: StoredSource) -> bool:
        if self.filters.scope is not None and source.scope_id != self.filters.scope:
            return False
        return self.filters.source_matches(source, self._labels, self.context.binding.agent_id)

    def _source_identities(self, order: str):
        """Page source identities in one order, then load each through a view of its own partition."""
        tx = self._open()
        scopes = sorted(self.context.allowed_scope_ids)
        clauses = ["e.read_blocked=0", f"e.scope_id IN ({_marks(scopes)})"]
        partition, partition_params = self._partition_clauses("e")
        clauses.extend(partition)
        params: list[object] = [*scopes, *partition_params]
        if self.filters.scope is not None:
            clauses.append("e.scope_id=?")
            params.append(self.filters.scope)
        sql = (
            "SELECT e.event_id AS event_id,e.source_revision AS source_revision,"
            "e.project_id AS project_id,e.branch_id AS branch_id "
            f"FROM source_events e WHERE {' AND '.join(clauses)} ORDER BY {_SOURCE_ORDER[order]}"
        )
        cursor = tx._check().execute(sql, params)
        while page := cursor.fetchmany(_SOURCE_PAGE):
            rows = [dict(row) for row in page]
            grouped: dict[tuple[str | None, str | None], list[tuple[str, int]]] = {}
            for row in rows:
                key = (row["project_id"], row["branch_id"])
                grouped.setdefault(key, []).append((row["event_id"], int(row["source_revision"])))
            views = {}
            for key, pairs in grouped.items():
                view = self._view(tx, *key)
                view.prefetch_sources(pairs)
                views[key] = view
            for row in rows:
                view = views[(row["project_id"], row["branch_id"])]
                source = view.source(row["event_id"], int(row["source_revision"]))
                if source is not None and self._matches(source):
                    yield source

    def _agent(self, source: StoredSource) -> str:
        label = self._labels.get(source.entry_id, {})
        return label.get("display_name", self.context.binding.agent_id)

    def _source_record(self, source: StoredSource) -> dict:
        event = source.event
        return {
            "kind": "source",
            "ref": source.ref,
            "revision": source.revision,
            "scope": source.scope_id,
            "project": source.project_id,
            "branch": source.branch_id,
            "session": source.session_id,
            "agent": self._agent(source),
            "entry_id": source.entry_id,
            "role": event["role"],
            "origin": event["origin"],
            "occurred_at": utc_iso(event["occurred_at"]),
            "recorded_at": utc_iso(event["recorded_at"]),
            "time_precision": event["time_precision"],
            "content": event["content"],
            "content_sha256": source.content_sha256,
            "suppressed": source.suppressed,
            "segment": event.get("segment"),
            "capture_gaps": list(source.capture_gaps),
        }

    def iter_sources(self) -> Iterator[dict]:
        """Every visible source version matching the filters, by ref."""
        for source in self._source_identities("ref"):
            yield self._source_record(source)

    def iter_sources_by_session(self) -> Iterator[tuple[str, dict]]:
        """The same sources, grouped and ordered by session and then occurrence time."""
        for source in self._source_identities("session"):
            yield source.session_id, self._source_record(source)

    def iter_sources_by_month(self) -> Iterator[tuple[str, dict]]:
        """The same sources, grouped and ordered by UTC month and then time."""
        for source in self._source_identities("month"):
            stamp = source.event["occurred_at"] or source.event["recorded_at"]
            yield _month(stamp), self._source_record(source)

    # -- claims ----------------------------------------------------------

    def _claim_page(self, after: str) -> list[tuple[str, str | None, str | None]]:
        tx = self._open()
        scopes = sorted(self.context.allowed_scope_ids)
        clauses = ["read_blocked=0", f"scope_id IN ({_marks(scopes)})", "claim_id>?"]
        partition, partition_params = self._partition_clauses("claims")
        clauses.extend(partition)
        params: list[object] = [*scopes, after, *partition_params]
        if self.filters.scope is not None:
            clauses.append("scope_id=?")
            params.append(self.filters.scope)
        rows = (
            tx._check()
            .execute(
                f"SELECT claim_id,project_id,branch_id FROM claims WHERE {' AND '.join(clauses)} "
                f"ORDER BY claim_id LIMIT {_OBJECT_PAGE}",
                params,
            )
            .fetchall()
        )
        return [(row["claim_id"], row["project_id"], row["branch_id"]) for row in rows]

    def _provenance(self, source: StoredSource) -> dict:
        metadata = source_metadata(source, self._labels, self.context.binding.agent_id)
        return {
            "ref": metadata["ref"],
            "revision": metadata["revision"],
            "entry_id": metadata["entry_id"],
            "agent": metadata["agent"],
            "session": metadata["session"],
            "occurred_at": utc_iso(source.event["occurred_at"]),
            "recorded_at": utc_iso(source.event["recorded_at"]),
        }

    def _claim_sources(self, view, version) -> list[dict] | None:
        """Resolve one revision's evidence without retaining the claim's history."""
        pairs = lineage.evidence(view._check(), "claim", version.ref, version.revision)
        if not pairs:
            return None
        view.prefetch_sources(pairs)
        sources = []
        for pair in pairs:
            source = view.source(*pair)
            if source is None or not self._matches(source):
                return None
            sources.append(self._provenance(source))
        return sources

    @staticmethod
    def _claim_versions(view, ref: str):
        """Read bounded identity pages and hydrate one visible revision at a time."""
        cursor = view._check().execute("SELECT revision FROM claim_versions WHERE claim_id=? ORDER BY revision", (ref,))
        while page := cursor.fetchmany(_OBJECT_PAGE):
            for row in page:
                version = view.claims.version(ref, row[0])
                if version is not None:
                    yield version

    def _claim_history(self, view, ref: str, current: int) -> Iterator[dict]:
        for version in self._claim_versions(view, ref):
            if version.revision != current:
                sources = self._claim_sources(view, version)
                if sources is not None:
                    yield self._claim_record(version, "fact_history", sources)

    @staticmethod
    def _claim_record(version, kind: str, sources: list[dict]) -> dict:
        payload = {key: value for key, value in version.payload.items() if key != "evidence_spans"}
        return {
            "kind": kind,
            "ref": version.ref,
            "revision": version.revision,
            "scope": version.scope_id,
            "project": version.project_id,
            "branch": version.branch_id,
            "claim_kind": str(payload.get("kind") or ""),
            "subject": str(payload.get("subject") or ""),
            "predicate": str(payload.get("predicate") or ""),
            "value": str(payload.get("value_text") or ""),
            "conditions": list(payload.get("conditions") or ()),
            "statement_kind": payload.get("statement_kind"),
            "payload": payload,
            "state": version.state,
            "basis": version.basis,
            "qualification_reason": version.reason,
            "valid_from": utc_iso(version.valid_from),
            "valid_to": utc_iso(version.valid_to),
            "recorded_from": utc_iso(version.recorded_from),
            "recorded_to": utc_iso(version.recorded_to),
            "replaces_revision": version.replaces_revision,
            "conflict_revisions": list(version.conflict_revisions),
            "suppressed": version.suppressed,
            "sources": sources,
        }

    def iter_claims(self) -> Iterator[ClaimExport]:
        """Selected claims with their real head and a stream of all other revisions.

        Selection scans evidence first so a filtered package never carries a
        partial claim. The second pass streams history in the same snapshot;
        neither pass aggregates all versions of a frequently revised claim.
        """
        if self.filters.kind not in {None, "claim"}:
            return
        tx = self._open()
        after = ""
        while True:
            page = self._claim_page(after)
            if not page:
                return
            for ref, project, branch in page:
                view = self._view(tx, project, branch)
                revision = view.claims.current_revision(ref)
                if revision is None:
                    continue
                head = view.claims.version(ref, revision)
                if head is None or not all(
                    self._claim_sources(view, version) is not None for version in self._claim_versions(view, ref)
                ):
                    continue
                sources = self._claim_sources(view, head)
                if sources is not None:
                    yield ClaimExport(
                        self._claim_record(head, "fact", sources), self._claim_history(view, ref, revision)
                    )
            after = page[-1][0]
            if len(page) < _OBJECT_PAGE:
                return

    # -- episodes and attachments ---------------------------------------

    def _evidence_sources(self, view, refs) -> list[dict] | None:
        """Resolve ``ref@revision`` evidence through a partition view, or None when one is outside the package."""
        pairs = [parse_source_ref(ref) for ref in refs]
        if not pairs:
            return None
        view.prefetch_sources(pairs)
        sources = []
        for pair in pairs:
            source = view.source(*pair)
            if source is None or not self._matches(source):
                return None
            sources.append(self._provenance(source))
        return sources

    def _episode_page(self, after: str) -> list[tuple[str, str | None, str | None]]:
        tx = self._open()
        scopes = sorted(self.context.allowed_scope_ids)
        clauses = ["read_blocked=0", f"scope_id IN ({_marks(scopes)})", "episode_id>?"]
        partition, partition_params = self._partition_clauses("episodes")
        clauses.extend(partition)
        params: list[object] = [*scopes, after, *partition_params]
        if self.filters.scope is not None:
            clauses.append("scope_id=?")
            params.append(self.filters.scope)
        rows = (
            tx._check()
            .execute(
                f"SELECT episode_id,project_id,branch_id FROM episodes WHERE {' AND '.join(clauses)} "
                f"ORDER BY episode_id LIMIT {_OBJECT_PAGE}",
                params,
            )
            .fetchall()
        )
        return [(row["episode_id"], row["project_id"], row["branch_id"]) for row in rows]

    def iter_episodes(self) -> Iterator[dict]:
        """Selected episode heads in ref order, with all their cited sources."""
        if self.filters.kind not in {None, "episode"}:
            return
        tx = self._open()
        after = ""
        while True:
            page = self._episode_page(after)
            if not page:
                return
            for ref, project, branch in page:
                view = self._view(tx, project, branch)
                episode = view.episodes.get(ref)
                if episode is None:
                    continue
                sources = self._evidence_sources(view, episode.evidence_refs)
                if sources is None:
                    continue
                yield {
                    "kind": "episode",
                    "ref": episode.ref,
                    "revision": episode.revision,
                    "scope": episode.scope_id,
                    "project": episode.project_id,
                    "branch": episode.branch_id,
                    "state": episode.state,
                    "suppressed": episode.suppressed,
                    "source_watermark": episode.source_watermark,
                    "resume": episode.resume,
                    "gaps": list(episode.gaps),
                    "sources": sources,
                }
            after = page[-1][0]
            if len(page) < _OBJECT_PAGE:
                return

    def _artifact_page(self, after: str) -> list[tuple[str, str | None, str | None]]:
        tx = self._open()
        scopes = sorted(self.context.allowed_scope_ids)
        clauses = ["read_blocked=0", f"scope_id IN ({_marks(scopes)})", "artifact_id>?"]
        partition, partition_params = self._partition_clauses("artifacts")
        clauses.extend(partition)
        params: list[object] = [*scopes, after, *partition_params]
        if self.filters.scope is not None:
            clauses.append("scope_id=?")
            params.append(self.filters.scope)
        rows = (
            tx._check()
            .execute(
                f"SELECT artifact_id,project_id,branch_id FROM artifacts WHERE {' AND '.join(clauses)} "
                f"ORDER BY artifact_id LIMIT {_OBJECT_PAGE}",
                params,
            )
            .fetchall()
        )
        return [(row["artifact_id"], row["project_id"], row["branch_id"]) for row in rows]

    def iter_attachments(self) -> Iterator[AttachmentExport]:
        """Selected retained artifact heads in ref order, with their bytes."""
        tx = self._open()
        after = ""
        while True:
            page = self._artifact_page(after)
            if not page:
                return
            for ref, project, branch in page:
                view = self._view(tx, project, branch)
                artifact = view.artifacts.get(ref)
                if artifact is None or artifact.retention_state != "retained_artifact" or artifact.blob is None:
                    continue
                sources = self._evidence_sources(view, artifact.evidence_refs)
                if sources is None:
                    continue
                opened, data = view.artifacts.open(ref, artifact.revision)
                digest = opened.sha256 if opened.sha256 is not None else artifact.blob.sha256
                yield AttachmentExport(
                    opened.ref,
                    opened.revision,
                    opened.media_type,
                    digest,
                    data,
                    opened.suppressed,
                )
            after = page[-1][0]
            if len(page) < _OBJECT_PAGE:
                return
