"""Trusted runtime composition for the bounded background worker.

Construction opens nothing: SQLite is opened only by ``status``/``recall``/
``drain`` and a vector companion only when an operation explicitly asks.  No
model or host supplied request field is accepted as identity.

The one construction side effect is this process's running-code record
(``runtime/running_code.py``): construction is the moment a process has
demonstrably loaded the package and bound itself to this instance, the write
is one small file that can neither block nor fail the caller, and it acquires
no lock and opens no store.
"""
from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields, replace
from functools import partial
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from ..contracts import ContractError, InstanceBinding, Origin, TrustedContext
from ..core.composition import CoreConfig, MemoryCore
from ..core.storage import SQLiteStorage
from ..core.retrieval import SearchContext
from ..core.deadline import RequestDeadline, using_request_deadline
from .auxiliary import AuxiliaryRuntimeConfig, build_auxiliary_runtime
from .embedding_retry import embed_with_one_retry
from .running_code import record_running_code
from .validation import (
    absolute_path,
    identifier,
    mapping,
    member,
    strict_bool,
    strict_float,
    strict_int,
    utc_now,
)
from .vector_upkeep import compact_if_due


_RUNTIME_ORIGINS: frozenset[Origin] = frozenset(
    {"human_direct", "tool_observation", "external_document", "imported"}
)
_HOST_ADAPTERS = frozenset({"hermes", "codex"})
_VECTOR_BACKENDS = frozenset({"lancedb", "sqlite-bruteforce"})


@dataclass(frozen=True)
class VectorRuntimeConfig:
    backend: str
    storage_dir: Path
    table_name: str
    dimensions: int
    metric: str = "cosine"
    #: Low-dimensional stores are permitted only for an explicitly injected
    #: test seam; formal configuration is fixed to the approved embedding space.
    test_injection_override: bool = False

    def __post_init__(self) -> None:
        member("vector_backend", self.backend, _VECTOR_BACKENDS)
        if not self.storage_dir.is_absolute():
            raise ValueError("vector_storage_dir_must_be_absolute")
        identifier("vector_table_name", self.table_name)
        strict_int("vector_dimensions", self.dimensions, minimum=1, maximum=8192)
        member("vector_metric", self.metric, ("cosine",))
        strict_bool("vector_test_injection_override", self.test_injection_override)

    @classmethod
    def from_mapping(cls, raw: object) -> "VectorRuntimeConfig":
        raw = mapping("vector_mapping_required", raw)
        return cls(
            backend=raw.get("backend", "lancedb"),
            storage_dir=absolute_path("vector_storage_dir", raw.get("storage_dir")),
            table_name=raw.get("table_name"),
            dimensions=raw.get("dimensions"),
            metric=raw.get("metric", "cosine"),
            test_injection_override=raw.get("test_injection_override", False),
        )


#: Closed bounds per field.  Seconds accept ``int`` or ``float``; counts reject ``bool``.
_SECONDS_BOUNDS = {
    "request_seconds": (0.001, 45.0),
    "drain_seconds": (0.001, 120.0),
    "auto_recall_seconds": (0.001, 5.0),
    "hook_processing_seconds": (0.001, 6.0),
    "auto_retry_cooldown_seconds": (60, 86400),
    "worker_min_interval_seconds": (1, 3600),
    "supervisor_seconds": (1, 86400),
}
_COUNT_BOUNDS = {
    "max_items": (1, 32),
    "daily_work_limit": (0, 1_000_000),
    "max_auto_recoveries": (0, 4),
    "supervisor_max_drains": (1, 1024),
}
#: Fields assembled from nested mappings rather than copied from the top level.
_COMPOSED_FIELDS = frozenset({"binding", "allowed_scope_ids", "auxiliary", "vector"})


@dataclass(frozen=True)
class RuntimeInstanceConfig:
    binding: InstanceBinding
    session_id: str
    allowed_scope_ids: frozenset[str]
    actor_origin: Origin = "human_direct"
    project_id: str | None = None
    branch_id: str | None = None
    host_adapter: str | None = None
    owner_id: str = "scope-recall-worker"
    request_seconds: float = 45.0
    drain_seconds: float = 120.0
    auto_recall_seconds: float = 5.0
    hook_processing_seconds: float = 6.0
    max_items: int = 32
    lease_seconds: float = 60.0
    auxiliary: AuxiliaryRuntimeConfig | None = None
    vector: VectorRuntimeConfig | None = None
    vector_threshold: float | None = None
    #: Queue items a day may attempt; 0 means no cap.  This is not the spend
    #: guard: money, calls and tokens are governed by the auxiliary ledger
    #: (``runtime/model_budget.py``) before every request.  A cap below the
    #: arrival rate is not conservative, it is a permanent leak: a lightly used
    #: instance produced ~15 work items per captured source, so the old 256
    #: default could never drain its own daily output.
    daily_work_limit: int = 0
    auto_retry_cooldown_seconds: float = 3600.0
    max_auto_recoveries: int = 2
    worker_min_interval_seconds: float = 30.0
    supervisor_enabled: bool = True
    supervisor_seconds: float = 21600.0
    supervisor_max_drains: int = 256

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstanceBinding):
            raise ValueError("binding")
        identifier("session_id", self.session_id)
        if (type(self.allowed_scope_ids) is not frozenset or not self.allowed_scope_ids
                or not self.allowed_scope_ids <= self.binding.scope_ids):
            raise ContractError("ACCESS_DENIED")
        member("actor_origin", self.actor_origin, _RUNTIME_ORIGINS)
        identifier("project_id", self.project_id, required=False)
        identifier("branch_id", self.branch_id, required=False)
        if self.host_adapter is not None:
            member("host_adapter", self.host_adapter, _HOST_ADAPTERS)
        identifier("owner_id", self.owner_id)
        for name, (low, high) in _SECONDS_BOUNDS.items():
            strict_float(name, getattr(self, name), minimum=low, maximum=high)
        for name, (low, high) in _COUNT_BOUNDS.items():
            strict_int(name, getattr(self, name), minimum=low, maximum=high)
        strict_bool("supervisor_enabled", self.supervisor_enabled)
        if self.hook_processing_seconds < self.auto_recall_seconds:
            raise ValueError("hook_processing_seconds_must_cover_auto_recall")
        strict_float("lease_seconds", self.lease_seconds, minimum=self.request_seconds, maximum=3600.0)
        # The policy ``build_runtime_instance`` constructs, checked while the
        # config loads: a bad threshold, or an embedding route that describes no
        # valid space, is an invalid configuration rather than a failed build.
        self.recall_policy()
        self._check_vector_binding()

    def _check_vector_binding(self) -> None:
        vector = self.vector
        if vector is None:
            return
        if vector.test_injection_override:
            if not self.binding.test_mode:
                raise ContractError("VECTOR_TEST_OVERRIDE_FORBIDDEN")
            return
        if vector.dimensions != self.embedding_space()["dimensions"]:
            raise ContractError("VECTOR_DIMENSIONS_MISMATCH")
        expected_root = (self.binding.data_directory / "vectors" / self.embedding_space_id()).resolve()
        if vector.storage_dir.resolve() != expected_root:
            raise ContractError("VECTOR_STORAGE_OUTSIDE_BINDING")

    def embedding_space(self) -> dict:
        """The embedding space this instance uses; the shipped default when no route names one."""
        from ..core.recall_policy import EMBEDDING_SPACE

        route = getattr(self.auxiliary, "embedding", None) if self.auxiliary is not None else None
        return route.space() if route is not None else dict(EMBEDDING_SPACE)

    def embedding_space_id(self) -> str:
        """Digest of the active space, which is also the vector directory name.

        Naming a different model changes this, which moves the store and refuses
        the old vectors rather than comparing across incompatible geometries.
        """
        from ..core.recall_policy import embedding_space_id

        return embedding_space_id(self.embedding_space())

    def recall_policy(self):
        """The admission policy recall runs with, bound to this instance's embedding space.

        The vector ports search partitions of, and stamp candidates with,
        ``embedding_space_id()``.  Admission has to compare against that same
        digest; against the shipped default every hit of a named route is refused.
        """
        from ..core.recall_policy import RecallPolicy

        return RecallPolicy(vector_threshold=self.vector_threshold, embedding_space_id=self.embedding_space_id())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RuntimeInstanceConfig":
        raw = mapping("runtime_config_mapping_required", raw)
        binding_raw = mapping("binding_mapping_required", raw.get("binding"))
        binding = InstanceBinding(
            agent_id=identifier("agent_id", binding_raw.get("agent_id")),
            installation_id=identifier("installation_id", binding_raw.get("installation_id")),
            data_directory=absolute_path("data_directory", binding_raw.get("data_directory")),
            scope_ids=frozenset(binding_raw.get("scope_ids") or ()),
            test_mode=binding_raw.get("test_mode", False),
        )
        aux_raw = raw.get("auxiliary")
        if aux_raw is None:
            aux_raw = {"external_embedding": False, "external_consolidation": False}
        vector_raw = raw.get("vector")
        plain = {
            item.name: raw.get(item.name, None if item.default is MISSING else item.default)
            for item in fields(cls) if item.name not in _COMPOSED_FIELDS
        }
        return cls(
            binding=binding,
            allowed_scope_ids=frozenset(raw.get("allowed_scope_ids") or ()),
            auxiliary=AuxiliaryRuntimeConfig.from_mapping(aux_raw),
            vector=None if vector_raw is None else VectorRuntimeConfig.from_mapping(vector_raw),
            **plain,
        )

    def context(self) -> TrustedContext:
        return TrustedContext(
            binding=self.binding,
            session_id=self.session_id,
            allowed_scope_ids=self.allowed_scope_ids,
            actor_origin=self.actor_origin,
            project_id=self.project_id,
            branch_id=self.branch_id,
        )


class _LazyVectorPort:
    """Request-local vector facade for direct Core and Runtime calls.

    Construction never opens Lance.  The first real SearchContext opens only
    an existing companion under that request's absolute deadline, then keeps
    the trusted identity and scope supplied by that context all the way to
    ``LanceVectorPort``.  A disabled query embedding route returns no vector
    candidates; it never exposes the raw native store as a Core port.
    """

    def __init__(self, instance: "RuntimeInstance") -> None:
        self._instance = instance

    def search(self, context: SearchContext, *, limit: int, remaining_seconds: float):
        if not isinstance(context, SearchContext):
            raise TypeError("context must be SearchContext")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be between 1 and 200')
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)):
            raise ValueError('remaining_seconds must be finite')
        now = time.monotonic()
        remaining = min(float(remaining_seconds), context.deadline - now)
        if remaining <= 0.0:
            return ()
        effective_deadline = min(context.deadline, now + float(remaining_seconds))
        context = replace(context, deadline=effective_deadline)
        deadline = RequestDeadline.from_absolute(effective_deadline, now=now)
        prepared: list[tuple[str, Any]] = []

        def embed_while_opening() -> None:
            from ..adapters.lance import LanceVectorPort

            embedding = getattr(self._instance.auxiliary, "query_embedding", None)
            if embedding is None:
                return
            embedding_remaining = deadline.remaining()
            if embedding_remaining <= 0:
                raise TimeoutError("query embedding stage deadline exhausted")
            adapter = LanceVectorPort(None, embedding,
                                      expected_embedding_space=self._instance.config.embedding_space_id())
            # A connection that failed in milliseconds costs the whole
            # semantic channel otherwise; a rejected request is not retried.
            vector = embed_with_one_retry(
                lambda seconds: adapter._embed_query(context.query, seconds),
                budget_seconds=embedding_remaining,
                remaining=deadline.remaining,
            )
            prepared.append((context.query, vector))

        with using_request_deadline(deadline):
            port = self._instance._ensure_vector_port(allow_create=False, deadline=effective_deadline,
                                                      during_open=embed_while_opening)
            if port is None:
                return ()
            remaining = min(remaining, deadline.remaining())
            if remaining <= 0.0:
                return ()
            if prepared:
                return port.search(context, limit=limit, remaining_seconds=remaining, _prepared_query=prepared[0])
            return port.search(context, limit=limit, remaining_seconds=remaining)


def _open_store(resource: Any, *, allow_create: bool, deadline: float | None, during_open) -> None:
    """Open a companion store; ``during_open`` overlaps an existing-index open when the store can."""
    opener = getattr(resource, "open" if allow_create else "open_existing", None)
    overlap = getattr(resource, "open_existing_with_work", None)
    if not allow_create and during_open is not None and callable(overlap):
        opener = partial(overlap, during_open)
    if not callable(opener):
        return
    if deadline is None:
        opener()
        return
    with using_request_deadline(RequestDeadline.from_absolute(deadline, now=time.monotonic())):
        opener()


def _close_quietly(resource: Any) -> None:
    closer = getattr(resource, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


@dataclass
class RuntimeInstance:
    config: RuntimeInstanceConfig
    core: MemoryCore
    auxiliary: Any
    _vector_factory: Callable[[VectorRuntimeConfig], Any] | None = None
    _vector_port: Any = None
    _vector_store: Any = None
    _default_embed: Any = None
    _default_purge: Any = None
    _ingress_authorizer: Callable[[object], frozenset[str]] | None = None
    _owned_resources: list[Any] = field(default_factory=list)
    _closed: bool = False
    background_gaps: tuple[str, ...] = ()
    ingress_receipts: tuple[Any, ...] = ()
    #: Receipt of the vector compaction this drain ran, or ``None``.
    vector_compaction: dict | None = None
    #: Work types this drain left alone, each with the held model and when its
    #: hold ends (runtime/model_budget.py ``provider_holds``).
    provider_holds: dict = field(default_factory=dict)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("runtime_instance_closed")

    def _ensure_vector_port(self, *, allow_create: bool = False, deadline: float | None = None,
                            during_open=None) -> Any:
        """Open the companion store once and compose the Core ports over it.

        Query paths pass ``allow_create=False`` and may only open an existing
        index; explicit background maintenance is the sole path allowed to
        create one.  Injected VectorPort values stay untouched for
        deterministic tests and alternate backends.
        """
        self._ensure_open()
        if self._vector_store is not None:
            if getattr(self._vector_store, "requires_reopen", False):
                _open_store(self._vector_store, allow_create=allow_create, deadline=deadline,
                            during_open=during_open)
            return self._vector_port
        if self.config.vector is None or self._vector_factory is None:
            return None
        resource = self._vector_factory(self.config.vector)
        self._owned_resources.append(resource)
        try:
            _open_store(resource, allow_create=allow_create, deadline=deadline, during_open=during_open)
        except Exception:
            self._owned_resources.pop()
            _close_quietly(resource)
            raise
        self._compose_ports(resource)
        return self._vector_port

    def _compose_ports(self, resource: Any) -> None:
        """Core consumes ports that turn trusted SearchContext values into
        partitioned native queries; the raw store is never exposed as one.
        Purge is a local native operation and stays available even when both
        embedding routes are disabled."""
        from ..adapters.lance import LanceEmbedPort, LancePurgePort, LanceVectorPort

        binding = self.config.binding
        space_id = self.config.embedding_space_id()
        port = None
        query_embedding = getattr(self.auxiliary, "query_embedding", None)
        if query_embedding is not None:
            port = LanceVectorPort(resource, query_embedding, expected_embedding_space=space_id)
            source_embedding = getattr(self.auxiliary, "source_embedding", None)
            if source_embedding is not None:
                self._default_embed = LanceEmbedPort(
                    resource, source_embedding, agent_id=binding.agent_id,
                    installation_id=binding.installation_id, embedding_space=space_id,
                )
        self._default_purge = LancePurgePort(
            resource, embedding_spaces=(space_id,),
            agent_id=binding.agent_id, installation_id=binding.installation_id,
        )
        self._vector_store = resource
        self._vector_port = port

    def status(self, *, include_admission: bool = True) -> Any:
        self._ensure_open()
        return self.core.status(self.config.context(), include_admission=include_admission)

    def memory_epoch(self) -> int:
        """Read the authority fence without counting the backlog.

        A pass used to call ``status()`` to prove its database was the bound
        one, which cost six whole-store aggregates and a JSON scan of every
        source -- measured at 4.4 seconds on an instance with 150,000 queued
        items, for a value that was discarded.  The epoch read proves the same
        binding from one indexed row.
        """
        self._ensure_open()
        return self.core.memory_epoch(self.config.context())

    def recall(self, request: Mapping[str, Any], *, current_source_refs: tuple[str, ...] = ()) -> Any:
        return self.core.recall(self.config.context(), dict(request), current_source_refs=current_source_refs,
                                deadline_seconds=self.config.request_seconds)

    def drain(self, *, embed: Any = None, purge: Any = None, consolidation: Any = None,
              max_items: int | None = None, purge_only: bool = False,
              remaining_seconds: float | None = None) -> Any:
        self._ensure_open()
        budget = self.config.drain_seconds if remaining_seconds is None else remaining_seconds
        strict_float("remaining_seconds", budget, minimum=.001, maximum=self.config.drain_seconds)
        deadline = time.monotonic() + budget
        ingress_gaps = self._replay_ingress(budget)
        self.background_gaps = (*ingress_gaps, *self._open_vector_for_drain(deadline, budget))
        # Upkeep comes before the queue, not after it: on a busy instance the
        # budget is gone by the time the queue drains, so upkeep at the end is
        # upkeep that only ever runs when it is not needed.
        self.vector_compaction = compact_if_due(
            self._vector_store, self.config.vector,
            available_seconds=max(0.0, deadline - time.monotonic()),
        )
        from ..core.worker import WorkerConfig, drain_worker

        model, candidate = self._consolidation_ports(consolidation)
        limit = self.config.request_seconds
        effective_embed = embed if embed is not None else self._default_embed
        effective_purge = purge if purge is not None else self._default_purge
        from .model_budget import provider_holds
        self.provider_holds = {} if purge_only else provider_holds(self.config.auxiliary)
        return drain_worker(
            self.core.storage, self.core.clock, self.config.context(),
            config=WorkerConfig(owner_id=self.config.owner_id,
                                max_items=self.config.max_items if max_items is None else max_items,
                                lease_seconds=self.config.lease_seconds,
                                auto_retry_cooldown_seconds=self.config.auto_retry_cooldown_seconds,
                                max_auto_recoveries=self.config.max_auto_recoveries,
                                purge_only=purge_only,
                                admission_policy=self.core.config.admission_policy,
                                # The bound the model and embedding ports below are clamped to.
                                request_seconds=limit,
                                held_work_types=frozenset(self.provider_holds)),
            remaining_seconds=max(.001, deadline - time.monotonic()),
            consolidation=model,
            candidate=candidate,
            embed=_BoundedEmbed(effective_embed, limit) if effective_embed is not None else None,
            purge=_BoundedPurge(effective_purge, limit) if effective_purge is not None else None,
        )

    def _replay_ingress(self, budget: float) -> tuple[str, ...]:
        """Persist captured inbox payloads.  Ingress never spends the model-work budget."""
        from ..core.capture_inbox import replay_inbox, resolve_conflicted_ingress

        self.ingress_receipts = ()
        try:
            context = self.config.context()
            if self._ingress_authorizer is None:
                return self._unauthorized_ingress_gaps(context, budget)
            options = dict(authorize=self._ingress_authorizer,
                           admission_policy=self.core.config.admission_policy,
                           remaining_seconds=min(2.0, budget / 4))
            self.ingress_receipts = tuple(replay_inbox(self.core.storage, self.core.clock, context, **options))
            # A key-collided capture is invisible to the replay above, which
            # only retries failures that might clear by themselves; without
            # this it stays in the inbox forever, captured but never stored.
            self.ingress_receipts += tuple(
                resolve_conflicted_ingress(self.core.storage, self.core.clock, context, **options)
            )
            return ()
        except (ContractError, OSError, RuntimeError, ValueError):
            return ("capture_gap:durable_ingress_pending",)

    def _unauthorized_ingress_gaps(self, context: TrustedContext, budget: float) -> tuple[str, ...]:
        """Without an authorizer, pending ingress can only be reported, not replayed."""
        scopes = tuple(sorted(context.allowed_scope_ids))
        with self.core.storage.read(context, remaining_seconds=min(1.0, budget / 8)) as tx:
            pending = tx._check().execute(
                f"""SELECT 1 FROM capture_inbox
                WHERE scope_id IN ({','.join('?' for _ in scopes)})
                  AND project_id IS ? AND branch_id IS ? LIMIT 1""",
                (*scopes, context.project_id, context.branch_id),
            ).fetchone()
        if pending is None:
            return ()
        return ("capture_gap:durable_ingress_authorizer_unconfigured", "capture_gap:durable_ingress_pending")

    def _open_vector_for_drain(self, deadline: float, budget: float) -> tuple[str, ...]:
        """Open (or create) the index for this pass; a failure is a gap, never a stopped drain.

        Optional indexing failure cannot suppress a healthy independent
        consolidation route.  Purge stays pending unless acknowledged.
        """
        vector_deadline = min(deadline, time.monotonic() + min(self.config.request_seconds, budget / 4))
        try:
            self._ensure_vector_port(allow_create=True, deadline=vector_deadline)
        except Exception as exc:
            from ..vector.process_store import NativeVectorPathError

            if isinstance(exc, NativeVectorPathError):
                return (NativeVectorPathError.code,)
            return (f"vector_unavailable:{type(exc).__name__}",)
        return ()

    def _consolidation_ports(self, consolidation: Any) -> tuple[Any, Any]:
        """The pass's bounded consolidation model and, when it offers one, candidate evaluator."""
        model = consolidation
        if model is None:
            from ..adapters.codex_cli import CodexCliConsolidationAdapter
            from ..core.worker import build_consolidation_model

            port = self.core.consolidation
            # One CLI allowance is shared by consolidation and candidate
            # evaluation within this pass; no new scheduler, lease or queue.
            if isinstance(port, CodexCliConsolidationAdapter):
                port = port.for_pass()
            model = build_consolidation_model(port)
        limit = self.config.request_seconds
        candidate = (
            _BoundedCandidate(model, limit)
            if callable(getattr(model, "evaluate_candidate", None)) else None
        )
        return (_BoundedConsolidation(model, limit) if model is not None else None), candidate

    def retry_failed(
        self,
        work_ids,
        *,
        operation_id: str,
        expected_memory_epoch: int | None = None,
        operator_context: TrustedContext | None = None,
    ) -> tuple[Any, ...]:
        """Request a bounded, explicitly named maintenance retry.

        The storage transaction performs all identity/scope/source/epoch
        checks.  This method only exposes that controlled mutation to the
        worker entry point; it does not accept host/model identity or SQL.
        The caller may follow it with one normal bounded ``drain``.
        """
        self._ensure_open()
        maintenance_context = self.config.context() if operator_context is None else operator_context
        if maintenance_context.binding != self.config.binding:
            raise ContractError("IDENTITY_UNBOUND")
        if not maintenance_context.allowed_scope_ids <= self.config.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED")
        with self.core.storage.write(
            maintenance_context, remaining_seconds=self.config.request_seconds
        ) as tx:
            return tx.work.operator_retry_failed(
                work_ids,
                now=utc_now(),
                operation_id=operation_id,
                expected_memory_epoch=expected_memory_epoch,
                max_items=min(8, self.config.max_items),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in reversed(self._owned_resources):
            _close_quietly(resource)
        self._owned_resources.clear()
        self._vector_store = None
        self._vector_port = None
        self._default_embed = None
        self._default_purge = None


def default_vector_factory(config: VectorRuntimeConfig) -> Any:
    """Build an existing-companion store without opening or creating it."""
    from ..vector.store import build_vector_store

    return build_vector_store(
        config.backend,
        storage_dir=config.storage_dir,
        table_name=config.table_name,
        dimensions=config.dimensions,
        metric=config.metric,
    )


class _Bounded:
    """Forward ``methods`` to a port with ``remaining_seconds`` clamped to ``limit``.

    The durable worker owns the drain budget while every model or native
    boundary receives the stricter per-call budget; keeping the clamp at the
    runtime boundary stops a future worker port from turning the 45 second
    contract into 120.  Methods resolve through ``__getattr__`` so a port that
    lacks one (claims on an embed port) still looks like it lacks it: the
    worker probes for capabilities, and a plain method would always answer.
    """

    methods: tuple[str, ...] = ()

    def __init__(self, inner: Any, limit: float) -> None:
        self._inner = inner
        self._limit = limit

    def __getattr__(self, name: str):
        if name not in self.methods:
            raise AttributeError(name)
        target = getattr(self._inner, name, None)
        if target is None:
            raise AttributeError(name)
        limit = self._limit

        def bounded(*args, remaining_seconds=1.0, **kwargs):
            return target(*args, remaining_seconds=min(float(remaining_seconds), limit), **kwargs)

        return bounded


class _BoundedConsolidation(_Bounded):
    methods = ("propose",)


class _BoundedCandidate(_Bounded):
    methods = ("evaluate_candidate",)


class _BoundedEmbed(_Bounded):
    # prepare_sources is the batch of prepare_source, and it has to be listed
    # here or the worker probes for it, does not find it through this wrapper,
    # and asks for one document per request as if the capability did not exist.
    methods = ("prepare_source", "prepare_sources", "publish_source", "prepare_claim", "publish_claim")


class _BoundedPurge(_Bounded):
    methods = ("purge_active",)


def build_runtime_instance(
    config: RuntimeInstanceConfig,
    *,
    vector_factory: Callable[[VectorRuntimeConfig], Any] | None = None,
    vectors: Any = None,
    consolidation: Any = None,
) -> RuntimeInstance:
    if not isinstance(config, RuntimeInstanceConfig):
        raise TypeError("config must be RuntimeInstanceConfig")
    auxiliary = build_auxiliary_runtime(config.auxiliary) if config.auxiliary is not None else None
    core = MemoryCore(
        CoreConfig(config.binding, auto_recall_seconds=config.auto_recall_seconds),
        storage=SQLiteStorage(config.binding),
        vectors=vectors,
        consolidation=consolidation if consolidation is not None else getattr(auxiliary, "consolidation", None),
        retrieval_policy=config.recall_policy(),
    )
    ingress_authorizer = None
    if config.host_adapter == "hermes":
        from ..adapters.hermes.authorization import build_ingress_authorizer

        ingress_authorizer = build_ingress_authorizer(config.binding)
    elif config.host_adapter == "codex":
        from ..adapters.codex.authorization import build_ingress_authorizer

        ingress_authorizer = build_ingress_authorizer(config.binding)
    instance = RuntimeInstance(
        config=config,
        core=core,
        auxiliary=auxiliary,
        _vector_factory=vector_factory or (default_vector_factory if config.vector is not None else None),
        _vector_port=vectors,
        _vector_store=vectors,
        _ingress_authorizer=ingress_authorizer,
        _owned_resources=[resource for resource in (vectors, auxiliary) if resource is not None],
    )
    if vectors is None and config.vector is not None:
        facade = _LazyVectorPort(instance)
        core.vectors = facade
        core.recall_pipeline.vector_port = facade
    record_running_code(
        config.binding.data_directory,
        host_adapter=config.host_adapter,
        installation_id=config.binding.installation_id,
    )
    return instance


__all__ = [
    "RuntimeInstanceConfig",
    "RuntimeInstance",
    "VectorRuntimeConfig",
    "build_runtime_instance",
    "default_vector_factory",
]
