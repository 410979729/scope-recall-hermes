"""LanceDB boundary for the P08 retrieval pipeline.

The adapter deliberately has no LanceDB/PyArrow import.  A caller injects the
already-selected vector store (the Windows implementation is the existing
``ProcessLanceVectorStore`` helper) and a query embedding port.  Construction
is therefore side-effect free: it does not open a database, create a table,
load native libraries, or contact a model service.

Lance rows are only an index projection.  Search returns ``CandidateRef``
metadata; the production pipeline hydrates and authorizes the object from
SQLite before any content can be used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

from .._internal.recall.deadline import RequestDeadline, using_request_deadline
from ..contracts import TrustedContext
from ..core.recall_policy import SPACE_ID
from ..core.retrieval import CandidateRef, SearchContext


_OBJECT_KINDS = frozenset({"event", "claim", "episode", "artifact", "reference"})
_MAX_DIMENSIONS = 32768
_MAX_METADATA_BYTES = 16384
_PARTITION_VERSION = "p08-v1"


def physical_partition_scope_id(
    *,
    agent_id: str,
    installation_id: str,
    embedding_space: str,
    logical_scope_id: str,
    project_id: str | None,
    branch_id: str | None,
) -> str:
    """Deterministic Lance ``scope_id`` partition bound to trusted identity."""
    payload = {
        "agent_id": agent_id,
        "branch_id": branch_id,
        "embedding_space": embedding_space,
        "installation_id": installation_id,
        "logical_scope_id": logical_scope_id,
        "project_id": project_id,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"{_PARTITION_VERSION}-{digest}"


def _project_branch_combinations(trusted: TrustedContext) -> tuple[tuple[str | None, str | None], ...]:
    project_options: list[str | None] = [trusted.project_id] if trusted.project_id is not None else [None]
    if trusted.project_id is not None:
        project_options.append(None)
    branch_options: list[str | None] = [trusted.branch_id] if trusted.branch_id is not None else [None]
    if trusted.branch_id is not None:
        branch_options.append(None)
    seen: set[tuple[str | None, str | None]] = set()
    combos: list[tuple[str | None, str | None]] = []
    for project_id in project_options:
        for branch_id in branch_options:
            key = (project_id, branch_id)
            if key not in seen:
                seen.add(key)
                combos.append(key)
    return tuple(combos)


class QueryEmbeddingPort(Protocol):
    """Explicit query-only embedding dependency."""

    def embed_query(self, text: str, *, remaining_seconds: float) -> Sequence[float]: ...


@dataclass(frozen=True)
class LanceVectorRecord:
    """Validated metadata-plus-vector input for an explicit index write.

    ``content`` is intentionally absent.  The index writer stores an empty
    compatibility payload for legacy Lance schemas; SQLite remains the only
    source of answerable text.
    """

    object_kind: str
    object_ref: str
    object_revision: int
    vector_id: str
    embedding_space: str
    embedding: tuple[float, ...]
    scope_id: str
    agent_id: str
    installation_id: str
    project_id: str | None = None
    branch_id: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.object_kind not in _OBJECT_KINDS:
            raise ValueError("invalid object_kind")
        for name, value, maximum in (
            ("object_ref", self.object_ref, 240),
            ("vector_id", self.vector_id, 512),
            ("embedding_space", self.embedding_space, 128),
            ("scope_id", self.scope_id, 240),
            ("agent_id", self.agent_id, 240),
            ("installation_id", self.installation_id, 240),
        ):
            if type(value) is not str or not value.strip() or len(value) > maximum:
                raise ValueError(f"invalid {name}")
        if type(self.object_revision) is not int or self.object_revision < 1:
            raise ValueError("invalid object_revision")
        if type(self.embedding) is not tuple or not 1 <= len(self.embedding) <= _MAX_DIMENSIONS:
            raise ValueError("invalid embedding")
        for value in self.embedding:
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError("embedding must contain finite numbers")
        for name, value in (("project_id", self.project_id), ("branch_id", self.branch_id)):
            if value is not None and (type(value) is not str or not value.strip() or len(value) > 240):
                raise ValueError(f"invalid {name}")
        if self.updated_at is not None and (type(self.updated_at) is not str or len(self.updated_at) > 80):
            raise ValueError("invalid updated_at")


class LanceIndexWriter:
    """Explicit, bounded projection writer; never used by read-only search."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def upsert(self, record: LanceVectorRecord) -> None:
        self.upsert_records((record,))

    def upsert_records(self, records: Iterable[LanceVectorRecord]) -> None:
        rows = []
        for record in records:
            if not isinstance(record, LanceVectorRecord):
                raise TypeError("records must be LanceVectorRecord values")
            metadata = {
                "object_kind": record.object_kind,
                "object_ref": record.object_ref,
                "object_revision": record.object_revision,
                "vector_id": record.vector_id,
                "embedding_space": record.embedding_space,
                "agent_id": record.agent_id,
                "installation_id": record.installation_id,
                "project_id": record.project_id,
                "branch_id": record.branch_id,
                "logical_scope_id": record.scope_id,
            }
            physical_scope_id = physical_partition_scope_id(
                agent_id=record.agent_id,
                installation_id=record.installation_id,
                embedding_space=record.embedding_space,
                logical_scope_id=record.scope_id,
                project_id=record.project_id,
                branch_id=record.branch_id,
            )
            rows.append(
                {
                    "id": record.vector_id,
                    "scope_id": physical_scope_id,
                    # These columns keep the projection readable by the
                    # existing fixed Lance schema.  They are not answer text.
                    "source": record.object_ref,
                    "target": json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "content": "",
                    "summary": "",
                    "updated_at": record.updated_at or datetime.now(timezone.utc).isoformat(),
                    "vector": list(record.embedding),
                }
            )
        if rows:
            # The injected store owns the actual native write and its existing
            # helper/lease semantics.  This method does not create a table or
            # attempt a rebuild when the target is unavailable.
            self._store.upsert_records(rows)


class LanceVectorPort:
    """Read-only Lance candidate source for :class:`RetrievalPipeline`."""

    def __init__(
        self,
        store: Any,
        query_embedding: QueryEmbeddingPort | Callable[..., Sequence[float]],
        *,
        expected_embedding_space: str = SPACE_ID,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not callable(query_embedding) and not callable(getattr(query_embedding, "embed_query", None)):
            raise TypeError("query_embedding must be callable or expose embed_query")
        if type(expected_embedding_space) is not str or not expected_embedding_space.strip():
            raise ValueError("expected_embedding_space must be non-empty")
        self._store = store
        self._query_embedding = query_embedding
        self._expected_embedding_space = expected_embedding_space
        self._clock = clock if clock is not None else time.monotonic

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float) -> tuple[CandidateRef, ...]:
        """Search each trusted physical partition and return only validated metadata.

        The store receives only deterministic ``p08-v1-*`` partition literals
        derived from trusted identity.  It never receives a model-controlled
        where expression.  Post-filtering remains mandatory because a faulty
        or test store may ignore that predicate.
        """
        if not isinstance(context, SearchContext):
            raise TypeError("context must be SearchContext")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)):
            raise ValueError("remaining_seconds must be finite")
        if remaining_seconds <= 0:
            return ()

        now = self._clock()
        effective_deadline = min(context.deadline, now + float(remaining_seconds))
        # The existing helper reads this request-local deadline to cap its
        # lock/RPC wait.  No worker or future is created here, and the helper
        # remains the single native execution path.
        with using_request_deadline(RequestDeadline.from_absolute(effective_deadline, now=now)):
            embedding_remaining = effective_deadline - self._clock()
            if embedding_remaining <= 0:
                return ()
            vector = self._embed_query(context.query, embedding_remaining)
            if not vector:
                return ()
            if len(vector) > _MAX_DIMENSIONS:
                raise ValueError("query embedding is too large")
            query_vector = []
            for value in vector:
                if type(value) not in (int, float) or not math.isfinite(float(value)):
                    raise ValueError("query embedding must contain finite numbers")
                query_vector.append(float(value))

            hits: list[CandidateRef] = []
            trusted = context.trusted_context
            binding = trusted.binding
            deadline_hit = False
            for logical_scope_id in sorted(trusted.allowed_scope_ids):
                if deadline_hit:
                    break
                for project_id, branch_id in _project_branch_combinations(trusted):
                    if effective_deadline - self._clock() <= 0:
                        deadline_hit = True
                        break
                    physical_scope_id = physical_partition_scope_id(
                        agent_id=binding.agent_id,
                        installation_id=binding.installation_id,
                        embedding_space=self._expected_embedding_space,
                        logical_scope_id=logical_scope_id,
                        project_id=project_id,
                        branch_id=branch_id,
                    )
                    rows = self._store.search(query_vector, scope_id=physical_scope_id, limit=limit)
                    for row in rows or ():
                        candidate = self._candidate_from_row(
                            row,
                            context,
                            requested_scope=physical_scope_id,
                            logical_scope_id=logical_scope_id,
                        )
                        if candidate is not None:
                            hits.append(candidate)

        # A native query can return duplicate projections.  Keep the best
        # score per object identity, then assign a stable pipeline rank.
        # Keep different embedding spaces visible to the core policy.  If a
        # stale-space row shares the same object identity as a valid row,
        # collapsing them here could hide the valid projection (and erase the
        # useful mismatch diagnostic).  Exact duplicate vector projections are
        # still collapsed.
        best: dict[tuple[tuple[str, str, int], str | None, str | None], CandidateRef] = {}
        for candidate in hits:
            dedupe_key = (candidate.key, candidate.vector_id, candidate.embedding_space)
            previous = best.get(dedupe_key)
            if previous is None or (candidate.vector_score or -math.inf) > (previous.vector_score or -math.inf):
                best[dedupe_key] = candidate
        ordered = sorted(
            best.values(),
            key=lambda item: (
                -(item.vector_score if item.vector_score is not None else -math.inf),
                item.kind,
                item.ref,
                item.revision,
                item.vector_id or "",
            ),
        )[:limit]
        return tuple(candidate if candidate.rank == index else _with_rank(candidate, index) for index, candidate in enumerate(ordered, 1))

    def _embed_query(self, query: str, remaining_seconds: float) -> Sequence[float]:
        method = getattr(self._query_embedding, "embed_query", None)
        if method is None:
            method = self._query_embedding
        # Base embedders historically accept only text; newer injected ports
        # may accept the remaining budget.  Inspect before calling so a
        # TypeError from the embedding implementation is never retried.
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "remaining_seconds" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            return method(query, remaining_seconds=remaining_seconds)
        return method(query)

    def _candidate_from_row(
        self,
        row: Mapping[str, Any],
        context: SearchContext,
        *,
        requested_scope: str,
        logical_scope_id: str,
    ) -> CandidateRef | None:
        if not isinstance(row, Mapping):
            return None
        metadata: dict[str, Any] = dict(row)
        # The explicit writer stores fixed-schema compatibility metadata in
        # target.  Explicit columns win, so this also supports a native table
        # with first-class metadata columns.
        encoded = row.get("target")
        if isinstance(encoded, str) and len(encoded.encode("utf-8")) <= _MAX_METADATA_BYTES:
            try:
                decoded = json.loads(encoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, dict):
                for key, value in decoded.items():
                    metadata.setdefault(key, value)

        if metadata.get("scope_id") != requested_scope:
            return None
        trusted = context.trusted_context
        binding = trusted.binding
        stored_logical_scope = metadata.get("logical_scope_id")
        if type(stored_logical_scope) is not str or stored_logical_scope != logical_scope_id:
            return None
        if stored_logical_scope not in trusted.allowed_scope_ids:
            return None
        if metadata.get("agent_id") != binding.agent_id or metadata.get("installation_id") != binding.installation_id:
            return None
        if metadata.get("embedding_space") != self._expected_embedding_space:
            return None
        if not _context_value_matches(metadata, "project_id", trusted.project_id):
            return None
        if not _context_value_matches(metadata, "branch_id", trusted.branch_id):
            return None

        kind = metadata.get("object_kind", metadata.get("object_type", metadata.get("kind")))
        ref = metadata.get(
            "object_ref",
            metadata.get("object_id", metadata.get("ref", metadata.get("source"))),
        )
        revision = metadata.get(
            "object_revision",
            metadata.get("object_version", metadata.get("revision", metadata.get("version"))),
        )
        vector_id = metadata.get("vector_id", metadata.get("id"))
        space = metadata.get("embedding_space", metadata.get("space"))
        if kind not in _OBJECT_KINDS or type(ref) is not str or type(vector_id) is not str or type(space) is not str:
            return None
        if type(revision) is not int:
            try:
                if isinstance(revision, str) and revision.isdigit():
                    revision = int(revision)
                else:
                    return None
            except (TypeError, ValueError):
                return None
        score = _score_from_row(row)
        if score is None:
            return None
        try:
            return CandidateRef(
                kind,
                ref,
                revision,
                "vector",
                vector_score=score,
                vector_id=vector_id,
                embedding_space=space,
            )
        except Exception:
            return None


def _context_value_matches(metadata: Mapping[str, Any], key: str, expected: str | None) -> bool:
    if key not in metadata:
        return False
    actual = metadata[key]
    return actual is None or actual == expected


def _score_from_row(row: Mapping[str, Any]) -> float | None:
    value = row.get("score", row.get("vector_score"))
    if value is None:
        distance = row.get("_distance", row.get("distance"))
        if distance is None:
            return None
        if type(distance) not in (int, float) or not math.isfinite(float(distance)):
            return None
        value = 1.0 - float(distance)
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return None
    return float(value)


def _with_rank(candidate: CandidateRef, rank: int) -> CandidateRef:
    return CandidateRef(
        candidate.kind,
        candidate.ref,
        candidate.revision,
        candidate.source,
        rank=rank,
        lexical_score=candidate.lexical_score,
        vector_score=candidate.vector_score,
        vector_id=candidate.vector_id,
        embedding_space=candidate.embedding_space,
        fusion_score=candidate.fusion_score,
    )


__all__ = [
    "LanceIndexWriter",
    "LanceVectorPort",
    "LanceVectorRecord",
    "QueryEmbeddingPort",
    "physical_partition_scope_id",
]
