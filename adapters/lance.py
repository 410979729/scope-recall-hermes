"""LanceDB boundary for the retrieval pipeline.

The adapter has no LanceDB/PyArrow import.  A caller injects the already
selected vector store and an embedding port, so construction is side-effect
free: nothing is opened, created, loaded or contacted.

Lance rows are only an index projection.  Search returns ``CandidateRef``
metadata; the pipeline hydrates and authorizes the object from SQLite before
any content can be used.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from ..contracts import ContractError, TrustedContext
from ..core.deadline import RequestDeadline, using_request_deadline
from ..core.recall_policy import SPACE_ID, claim_embedding_text
from ..core.retrieval import CandidateRef, SearchContext
from ..core.storage import StoredSource

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
    return f"{_PARTITION_VERSION}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _project_branch_combinations(trusted: TrustedContext) -> tuple[tuple[str | None, str | None], ...]:
    """The bound project/branch first, then each widened to the global partition."""
    projects = (trusted.project_id, None) if trusted.project_id is not None else (None,)
    branches = (trusted.branch_id, None) if trusted.branch_id is not None else (None,)
    return tuple((project, branch) for project in projects for branch in branches)


class QueryEmbeddingPort(Protocol):
    def embed_query(self, text: str, *, remaining_seconds: float) -> Sequence[float]: ...


class SourceEmbeddingPort(Protocol):
    def embed_source(self, source: StoredSource, *, remaining_seconds: float) -> Sequence[float]: ...


def _embedding_method(port: Any, name: str) -> Callable[..., Sequence[float]]:
    """``port.<name>`` when it exists, else the port itself when it is callable."""
    method = getattr(port, name, None)
    if callable(method):
        return method
    if callable(port):
        return port
    raise TypeError(f"{name[len('embed_'):]}_embedding must be callable or expose {name}")


def _embed(method: Callable[..., Sequence[float]], subject: Any, remaining_seconds: float) -> Sequence[float]:
    """Call an embedding method, passing the budget only when it is accepted.

    Base embedders take only text; injected ports may take the remaining
    budget.  The signature is inspected first so a TypeError raised by the
    embedding itself is never mistaken for a signature mismatch and retried.
    """
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "remaining_seconds" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    ):
        return method(subject, remaining_seconds=remaining_seconds)
    return method(subject)


def _finite_embedding(vector: Any) -> tuple[float, ...]:
    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)) or not vector:
        raise ContractError("DERIVATION_INVALID", "empty_embedding")
    values = tuple(float(value) for value in vector)
    if any(not math.isfinite(value) for value in values):
        raise ContractError("DERIVATION_INVALID", "nonfinite_embedding")
    return values


@dataclass(frozen=True)
class PreparedSourceEmbedding:
    source_ref: str
    source_revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    vector_id: str
    embedding_space: str
    embedding: tuple[float, ...]


class LanceEmbedPort:
    """Projects one source or claim version into the index through the fenced writer."""

    def __init__(
        self,
        store: Any,
        source_embedding: SourceEmbeddingPort | Callable[..., Sequence[float]],
        *,
        agent_id: str,
        installation_id: str,
        embedding_space: str,
        writer: LanceIndexWriter | None = None,
    ) -> None:
        self._embed_source = _embedding_method(source_embedding, "embed_source")
        if type(agent_id) is not str or not agent_id or type(installation_id) is not str or not installation_id:
            raise ValueError("trusted identity is required")
        if type(embedding_space) is not str or not embedding_space:
            raise ValueError("embedding_space is required")
        self._source_embedding = source_embedding
        self._embedding_space = embedding_space
        self._writer = writer or LanceIndexWriter(store)
        self._agent_id = agent_id
        self._installation_id = installation_id

    def prepare_source(self, source: StoredSource, *, remaining_seconds: float = 1.0) -> PreparedSourceEmbedding:
        return self._prepare(source, lambda budget: _embed(self._embed_source, source, budget), remaining_seconds)

    def prepare_sources(self, sources: Sequence[StoredSource], *,
                        remaining_seconds: float = 1.0) -> tuple[PreparedSourceEmbedding, ...]:
        """Prepare many sources with one embedding request, in order.

        Each prepared vector is published on its own fence exactly as a single
        one is; sharing the request changes what the provider is asked, not
        what the store is told.  A port whose embedding cannot batch falls back
        to one request each, so nothing depends on the capability.
        """
        subjects = list(sources)
        if not subjects:
            return ()
        if remaining_seconds <= 0:
            raise ContractError("DEADLINE_EXCEEDED")
        batch = getattr(self._source_embedding, "embed_sources", None)
        if not callable(batch) or len(subjects) == 1:
            return tuple(self.prepare_source(source, remaining_seconds=remaining_seconds) for source in subjects)
        vectors = batch(subjects, remaining_seconds=remaining_seconds)
        if len(vectors) != len(subjects):
            raise ContractError("DERIVATION_INVALID", "embedding_batch_shape")
        return tuple(
            PreparedSourceEmbedding(
                source_ref=source.ref,
                source_revision=source.revision,
                scope_id=source.scope_id,
                project_id=source.project_id,
                branch_id=source.branch_id,
                vector_id=f"p10:{source.ref}@{source.revision}:{self._embedding_space}",
                embedding_space=self._embedding_space,
                embedding=_finite_embedding(vector),
            )
            for source, vector in zip(subjects, vectors)
        )

    def prepare_claim(self, claim: Any, *, remaining_seconds: float = 1.0) -> PreparedSourceEmbedding:
        """Embed one claim version so search can reach the derived layer.

        The embedded text is the rendered assertion rather than the stored
        JSON payload, so the vector carries meaning, not field names.
        """
        def embed(budget: float) -> Sequence[float]:
            embed_text = getattr(self._source_embedding, "embed_text", None)
            if embed_text is None:
                raise ContractError("DERIVATION_INVALID", "claim_embedding_unsupported")
            return embed_text(claim_embedding_text(claim.payload), remaining_seconds=budget)

        return self._prepare(claim, embed, remaining_seconds)

    def _prepare(
        self, subject: Any, embed: Callable[[float], Sequence[float]], remaining_seconds: float,
    ) -> PreparedSourceEmbedding:
        if remaining_seconds <= 0:
            raise ContractError("DEADLINE_EXCEEDED")
        return PreparedSourceEmbedding(
            source_ref=subject.ref,
            source_revision=subject.revision,
            scope_id=subject.scope_id,
            project_id=subject.project_id,
            branch_id=subject.branch_id,
            vector_id=f"p10:{subject.ref}@{subject.revision}:{self._embedding_space}",
            embedding_space=self._embedding_space,
            embedding=_finite_embedding(embed(remaining_seconds)),
        )

    def publish_source(
        self,
        prepared: PreparedSourceEmbedding,
        *,
        source: StoredSource,
        lease_token: int,
        lease_owner: str,
        lease_guard: Callable[[], bool],
        remaining_seconds: float = 1.0,
    ) -> None:
        self._publish("event", prepared, source, "prepared_source", lease_token, lease_owner, lease_guard, remaining_seconds)

    def publish_claim(
        self,
        prepared: PreparedSourceEmbedding,
        *,
        claim: Any,
        lease_token: int,
        lease_owner: str,
        lease_guard: Callable[[], bool],
        remaining_seconds: float = 1.0,
    ) -> None:
        self._publish("claim", prepared, claim, "prepared_claim", lease_token, lease_owner, lease_guard, remaining_seconds)

    def _publish(
        self,
        object_kind: str,
        prepared: PreparedSourceEmbedding,
        subject: Any,
        subject_detail: str,
        lease_token: int,
        lease_owner: str,
        lease_guard: Callable[[], bool],
        remaining_seconds: float,
    ) -> None:
        if not isinstance(prepared, PreparedSourceEmbedding):
            raise ContractError("INPUT_INVALID", "prepared_embedding")
        if (prepared.source_ref, prepared.source_revision) != (subject.ref, subject.revision):
            raise ContractError("VERSION_CONFLICT", subject_detail)
        if prepared.scope_id != subject.scope_id or (prepared.project_id, prepared.branch_id) != (subject.project_id, subject.branch_id):
            raise ContractError("ACCESS_DENIED", "prepared_scope")
        if type(lease_token) is not int or lease_token < 1 or type(lease_owner) is not str or not lease_owner:
            raise ContractError("INPUT_INVALID", "lease")
        if remaining_seconds <= 0:
            raise ContractError("DEADLINE_EXCEEDED")
        record = LanceVectorRecord(
            object_kind,
            prepared.source_ref,
            prepared.source_revision,
            prepared.vector_id,
            prepared.embedding_space,
            prepared.embedding,
            prepared.scope_id,
            self._agent_id,
            self._installation_id,
            prepared.project_id,
            prepared.branch_id,
        )
        # The guard runs on this thread during the helper's lock-held
        # handshake; the pipe reader never touches SQLite.
        if not self._writer.upsert_fenced(record, guard=lease_guard, remaining_seconds=remaining_seconds):
            raise ContractError("VERSION_CONFLICT", "publication_fence")


@dataclass(frozen=True)
class LanceVectorRecord:
    """Validated metadata-plus-vector input for an explicit index write.

    ``content`` is intentionally absent: the row carries an empty payload and
    SQLite remains the only source of answerable text.
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


def _record_row(record: LanceVectorRecord) -> dict[str, Any]:
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
    return {
        "id": record.vector_id,
        "scope_id": physical_partition_scope_id(
            agent_id=record.agent_id,
            installation_id=record.installation_id,
            embedding_space=record.embedding_space,
            logical_scope_id=record.scope_id,
            project_id=record.project_id,
            branch_id=record.branch_id,
        ),
        "source": record.object_ref,
        "target": json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "content": "",
        "summary": "",
        "updated_at": record.updated_at or datetime.now(timezone.utc).isoformat(),
        "vector": list(record.embedding),
    }


class LanceIndexWriter:
    """Explicit, bounded projection writer; never used by read-only search."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def upsert_records(self, records: Iterable[LanceVectorRecord]) -> None:
        rows = [_record_row(record) for record in records]
        if rows:
            # The store owns the native write and its lease semantics; nothing
            # here creates a table or rebuilds when the target is unavailable.
            self._store.upsert_records(rows)

    def upsert_fenced(self, record: LanceVectorRecord, *, guard: Callable[[], bool], remaining_seconds: float) -> bool:
        method = getattr(self._store, "fenced_upsert_records", None)
        if not callable(method):
            raise ContractError("STORAGE_UNAVAILABLE", "fenced_upsert_unsupported")
        return bool(method([_record_row(record)], guard=guard, remaining_seconds=remaining_seconds))


class LancePurgePort:
    """Single native, lock-held active purge for one trusted installation."""

    def __init__(self, store: Any, *, embedding_spaces: Iterable[str], agent_id: str, installation_id: str) -> None:
        spaces = tuple(embedding_spaces)
        if not spaces or any(type(space) is not str or not space.strip() for space in spaces):
            raise ValueError("at least one governed embedding space is required")
        if type(agent_id) is not str or not agent_id or type(installation_id) is not str or not installation_id:
            raise ValueError("trusted purge identity is required")
        self._store = store
        self._embedding_spaces = frozenset(spaces)
        self._agent_id = agent_id
        self._installation_id = installation_id

    def purge_active(self, operation_id: str, *, receipt: dict, remaining_seconds: float) -> bool:
        if type(operation_id) is not str or not operation_id:
            raise ContractError("INPUT_INVALID", "operation_id")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)) or remaining_seconds <= 0:
            raise ContractError("DEADLINE_EXCEEDED")
        if not isinstance(receipt, Mapping):
            return False
        members = receipt.get("physical_members")
        scopes = receipt.get("scope_ids")
        purge = getattr(self._store, "purge_governed_members", None)
        if not members or not scopes or not callable(purge):
            return False
        try:
            # Only opaque identities cross this boundary.  Deletion covers every
            # revision, including old versions still present in the active table.
            project_id, branch_id = receipt.get("project_id"), receipt.get("branch_id")
            partitions = [
                {
                    "scope_id": scope,
                    "embedding_space": space,
                    "physical_scope_id": physical_partition_scope_id(
                        agent_id=self._agent_id, installation_id=self._installation_id,
                        embedding_space=space, logical_scope_id=scope,
                        project_id=project_id, branch_id=branch_id,
                    ),
                }
                for scope in scopes for space in sorted(self._embedding_spaces)
            ]
            return purge(
                members=[{"kind": entry["kind"], "ref": entry["ref"]} for entry in members],
                agent_id=self._agent_id, installation_id=self._installation_id, partitions=partitions,
                project_id=project_id, branch_id=branch_id, remaining_seconds=remaining_seconds,
            ) is True
        except Exception:
            # A malformed inventory or an uncertain native outcome leaves the
            # durable purge work recoverable.
            return False


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
        self._embed_query_method = _embedding_method(query_embedding, "embed_query")
        if type(expected_embedding_space) is not str or not expected_embedding_space.strip():
            raise ValueError("expected_embedding_space must be non-empty")
        self._store = store
        self._expected_embedding_space = expected_embedding_space
        self._clock = clock if clock is not None else time.monotonic

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float,
               _prepared_query: tuple[str, Sequence[float]] | None = None) -> tuple[CandidateRef, ...]:
        """Search each trusted physical partition and return only validated metadata.

        The store receives only deterministic ``p08-v1-*`` partition literals
        derived from trusted identity, never a model-controlled predicate.
        Post-filtering stays mandatory because a faulty or test store may
        ignore that predicate.
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
        deadline = min(context.deadline, now + float(remaining_seconds))
        # The helper reads this request-local deadline to cap its lock and RPC
        # waits; no worker or future is created here.
        with using_request_deadline(RequestDeadline.from_absolute(deadline, now=now)):
            query_vector = self._query_vector(context, deadline, _prepared_query)
            hits = self._partition_hits(context, query_vector, limit, deadline) if query_vector else []
        return _ranked(hits, limit)

    def _embed_query(self, query: str, remaining_seconds: float) -> Sequence[float]:
        return _embed(self._embed_query_method, query, remaining_seconds)

    def _query_vector(
        self, context: SearchContext, deadline: float, prepared: tuple[str, Sequence[float]] | None,
    ) -> list[float]:
        remaining = deadline - self._clock()
        if remaining <= 0:
            return []
        if prepared is None:
            vector = self._embed_query(context.query, remaining)
        else:
            query, vector = prepared
            if query != context.query:
                raise ValueError("prepared embedding belongs to a different query")
        if not vector:
            return []
        if len(vector) > _MAX_DIMENSIONS:
            raise ValueError("query embedding is too large")
        if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in vector):
            raise ValueError("query embedding must contain finite numbers")
        return [float(value) for value in vector]

    def _partition_hits(
        self, context: SearchContext, query_vector: list[float], limit: int, deadline: float,
    ) -> list[CandidateRef]:
        """Query each (scope, project, branch) partition while the budget can cover one more."""
        trusted = context.trusted_context
        binding = trusted.binding
        hits: list[CandidateRef] = []
        # The slowest completed partition is the reserve the next one needs:
        # request-local, so concurrent requests are not coupled and no fixed
        # timeout is invented.
        reserve = 0.0
        for logical_scope_id in sorted(trusted.allowed_scope_ids):
            for project_id, branch_id in _project_branch_combinations(trusted):
                if deadline - self._clock() <= reserve:
                    return hits
                partition = physical_partition_scope_id(
                    agent_id=binding.agent_id,
                    installation_id=binding.installation_id,
                    embedding_space=self._expected_embedding_space,
                    logical_scope_id=logical_scope_id,
                    project_id=project_id,
                    branch_id=branch_id,
                )
                started = self._clock()
                rows = self._store.search(query_vector, scope_id=partition, limit=limit)
                reserve = max(reserve, self._clock() - started)
                for row in rows or ():
                    candidate = self._candidate_from_row(row, trusted, partition, logical_scope_id)
                    if candidate is not None:
                        hits.append(candidate)
        return hits

    def _candidate_from_row(
        self, row: Any, trusted: TrustedContext, requested_scope: str, logical_scope_id: str,
    ) -> CandidateRef | None:
        if not isinstance(row, Mapping):
            return None
        metadata: dict[str, Any] = dict(row)
        # The writer stores its metadata as JSON in ``target``; explicit
        # columns win, so a table with first-class metadata columns also works.
        encoded = row.get("target")
        if isinstance(encoded, str) and len(encoded.encode("utf-8")) <= _MAX_METADATA_BYTES:
            try:
                decoded = json.loads(encoded)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                for key, value in decoded.items():
                    metadata.setdefault(key, value)
        binding = trusted.binding
        if (
            metadata.get("scope_id") != requested_scope
            or metadata.get("logical_scope_id") != logical_scope_id
            or metadata.get("agent_id") != binding.agent_id
            or metadata.get("installation_id") != binding.installation_id
            or metadata.get("embedding_space") != self._expected_embedding_space
            or not _context_value_matches(metadata, "project_id", trusted.project_id)
            or not _context_value_matches(metadata, "branch_id", trusted.branch_id)
        ):
            return None
        kind = metadata.get("object_kind")
        ref = metadata.get("object_ref")
        revision = metadata.get("object_revision")
        vector_id = metadata.get("vector_id")
        if kind not in _OBJECT_KINDS or type(ref) is not str or type(revision) is not int or type(vector_id) is not str:
            return None
        score = _score_from_row(row)
        if score is None:
            return None
        try:
            return CandidateRef(
                kind, ref, revision, "vector",
                vector_score=score, vector_id=vector_id, embedding_space=self._expected_embedding_space,
            )
        except Exception:
            return None


def _context_value_matches(metadata: Mapping[str, Any], key: str, expected: str | None) -> bool:
    """A row bound to no project/branch matches any; a bound one must match exactly."""
    if key not in metadata:
        return False
    actual = metadata[key]
    return actual is None or actual == expected


def _score_from_row(row: Mapping[str, Any]) -> float | None:
    """A stored ``score``, else ``1 - _distance`` as the stores report it."""
    value = row.get("score")
    if value is None:
        distance = row.get("_distance")
        if type(distance) not in (int, float) or not math.isfinite(float(distance)):
            return None
        value = 1.0 - float(distance)
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return None
    return float(value)


def _ranked(hits: Iterable[CandidateRef], limit: int) -> tuple[CandidateRef, ...]:
    """Best score per projection, ordered and re-ranked for the pipeline.

    Different embedding spaces stay visible to the core policy: a stale-space
    row sharing an object identity with a valid row would otherwise hide the
    valid projection and erase the mismatch diagnostic.  Exact duplicate
    projections are collapsed.
    """
    best: dict[tuple[tuple[str, str, int], str | None, str | None], CandidateRef] = {}
    for candidate in hits:
        key = (candidate.key, candidate.vector_id, candidate.embedding_space)
        previous = best.get(key)
        if previous is None or (candidate.vector_score or -math.inf) > (previous.vector_score or -math.inf):
            best[key] = candidate
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
    return tuple(replace(candidate, rank=index) for index, candidate in enumerate(ordered, 1))


__all__ = [
    "LanceEmbedPort",
    "LanceIndexWriter",
    "LancePurgePort",
    "LanceVectorPort",
    "LanceVectorRecord",
    "PreparedSourceEmbedding",
    "QueryEmbeddingPort",
    "SourceEmbeddingPort",
    "physical_partition_scope_id",
]
