"""Trusted runtime composition for the bounded background worker.

This module deliberately keeps construction side effect free.  SQLite is
opened only by ``status``/``recall``/``drain`` and a vector companion is opened
only when the corresponding operation explicitly asks for it.  No model or
host supplied request fields are accepted as identity.

One deliberate exception: construction writes this process's running-code
record (``runtime/running_code.py``).  Construction is the moment a process has
demonstrably loaded the package and bound itself to this instance, which is
exactly the fact the record states, and the write is one small file that can
neither block nor fail the caller.  It acquires no lock and opens no store, so
the property the paragraph above protects is intact.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Any, Callable, Literal, Mapping, overload

from ..contracts import ContractError, InstanceBinding, Origin, TrustedContext
from ..core.composition import CoreConfig, MemoryCore
from ..core.storage import SQLiteStorage
from ..core.retrieval import SearchContext
from .._internal.recall.deadline import RequestDeadline, using_request_deadline
from .auxiliary import AuxiliaryRuntimeConfig, build_auxiliary_runtime
from .running_code import record_running_code
from .vector_upkeep import compact_if_due


@overload
def _text(name: str, value: object, *, required: Literal[True] = True) -> str: ...


@overload
def _text(name: str, value: object, *, required: Literal[False]) -> str | None: ...


def _text(name: str, value: object, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str or not value.strip() or len(value) > 240:
        raise ValueError(name)
    return value


def _absolute(name: str, value: object) -> Path:
    if type(value) is not str or not value:
        raise ValueError(name)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name}_must_be_absolute")
    return path


def _strict_float(name: str, value: object, *, minimum: float, maximum: float) -> float:
    if type(value) is int:
        parsed = float(value)
    elif type(value) is float:
        parsed = value
    else:
        raise ValueError(name)
    if not math.isfinite(parsed):
        raise ValueError(name)
    if not minimum <= parsed <= maximum:
        raise ValueError(name)
    return parsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_positive_int(name: str, value: object) -> int:
    if type(value) is bool or type(value) is not int or value < 1:
        raise ValueError(name)
    return value


_RUNTIME_ORIGINS: frozenset[Origin] = frozenset(
    {"human_direct", "tool_observation", "external_document", "imported"}
)
_HOST_ADAPTERS = frozenset({"hermes", "codex"})


def _actor_origin(value: object, *, default: Origin = "human_direct") -> Origin:
    if value is None:
        raise ValueError("actor_origin")
    if value not in _RUNTIME_ORIGINS:
        raise ValueError("actor_origin")
    return value


@dataclass(frozen=True)
class VectorRuntimeConfig:
    backend: str
    storage_dir: Path
    table_name: str
    dimensions: int
    metric: str = "cosine"
    # Low-dimensional stores are permitted only for an explicitly injected
    # test seam.  Formal runtime configuration is fixed to the approved
    # embedding space below.
    test_injection_override: bool = False

    def __post_init__(self) -> None:
        if self.backend not in {"lancedb", "sqlite-bruteforce"}:
            raise ValueError("vector_backend")
        if not self.storage_dir.is_absolute():
            raise ValueError("vector_storage_dir_must_be_absolute")
        if type(self.table_name) is not str or not self.table_name.strip():
            raise ValueError("vector_table_name")
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 8192:
            raise ValueError("vector_dimensions")
        if self.metric != "cosine":
            raise ValueError("vector_metric")
        if type(self.test_injection_override) is not bool:
            raise ValueError("vector_test_injection_override")


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
    #: Queue items a day may attempt; 0 means no cap.
    #:
    #: This is NOT the spend guard. Money, calls and tokens are governed
    #: precisely by the auxiliary ledger (``runtime/model_budget.py``), which
    #: checks five independent caps inside a BEGIN IMMEDIATE transaction before
    #: every request. This counter only limits how many queue items are
    #: attempted and cannot see what any of them costs.
    #:
    #: The old default of 256 strangled ordinary instances. Measured on a lightly
    #: used tianshu: 127 natively captured sources in one day produced 1,913 work
    #: items — a 15x multiplier, mostly candidate re-evaluations. At 256 the
    #: instance processed 13% of its own daily output, so the backlog grew by
    #: ~1,650 items every day and could never drain, while two days of that real
    #: traffic cost $0.63 in total. A cap below the arrival rate is not
    #: conservative, it is a permanent leak.
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
        _text("session_id", self.session_id)
        if type(self.allowed_scope_ids) is not frozenset or not self.allowed_scope_ids or not self.allowed_scope_ids <= self.binding.scope_ids:
            raise ContractError("ACCESS_DENIED")
        if self.actor_origin not in _RUNTIME_ORIGINS:
            raise ValueError("actor_origin")
        for name, value in (("project_id", self.project_id), ("branch_id", self.branch_id)):
            if value is not None:
                _text(name, value)
        if self.host_adapter is not None and self.host_adapter not in _HOST_ADAPTERS:
            raise ValueError("host_adapter")
        _text("owner_id", self.owner_id)
        _strict_float("request_seconds", self.request_seconds, minimum=0.001, maximum=45.0)
        _strict_float("drain_seconds", self.drain_seconds, minimum=0.001, maximum=120.0)
        _strict_float("auto_recall_seconds", self.auto_recall_seconds, minimum=0.001, maximum=5.0)
        _strict_float("hook_processing_seconds", self.hook_processing_seconds, minimum=0.001, maximum=6.0)
        if self.hook_processing_seconds < self.auto_recall_seconds:
            raise ValueError("hook_processing_seconds_must_cover_auto_recall")
        if type(self.max_items) is bool or type(self.max_items) is not int or not 1 <= self.max_items <= 32:
            raise ValueError("max_items")
        # 0 means uncapped. The old floor of 1 made "no cap" unexpressible, so
        # every install carried some cap whether or not it wanted one.
        if type(self.daily_work_limit) is bool or type(self.daily_work_limit) is not int \
                or not 0 <= self.daily_work_limit <= 1000000:
            raise ValueError("daily_work_limit")
        if type(self.max_auto_recoveries) is not int or not 0 <= self.max_auto_recoveries <= 4:
            raise ValueError("max_auto_recoveries")
        _strict_float("auto_retry_cooldown_seconds", self.auto_retry_cooldown_seconds, minimum=60, maximum=86400)
        _strict_float("worker_min_interval_seconds", self.worker_min_interval_seconds, minimum=1, maximum=3600)
        if type(self.supervisor_enabled) is not bool:
            raise ValueError("supervisor_enabled")
        _strict_float("supervisor_seconds", self.supervisor_seconds, minimum=1, maximum=86400)
        if type(self.supervisor_max_drains) is not int or not 1 <= self.supervisor_max_drains <= 1024:
            raise ValueError("supervisor_max_drains")
        _strict_float("lease_seconds", self.lease_seconds, minimum=self.request_seconds, maximum=3600.0)
        if self.vector is not None and self.vector.test_injection_override and not self.binding.test_mode:
            raise ContractError("VECTOR_TEST_OVERRIDE_FORBIDDEN")
        if self.vector_threshold is not None:
            from ..core.recall_policy import RecallPolicy

            RecallPolicy(vector_threshold=self.vector_threshold)
        if self.vector is not None and not self.vector.test_injection_override:
            space = self.embedding_space()
            expected_root = (self.binding.data_directory / "vectors" / self.embedding_space_id()).resolve()
            actual_root = self.vector.storage_dir.resolve()
            if self.vector.dimensions != space["dimensions"]:
                raise ContractError("VECTOR_DIMENSIONS_MISMATCH")
            if actual_root != expected_root:
                raise ContractError("VECTOR_STORAGE_OUTSIDE_BINDING")

    def embedding_space(self) -> dict:
        """The embedding space this instance actually uses.

        Falls back to the shipped default when no route names one, so an
        existing installation resolves the same digest and keeps the vector
        directory it already has.
        """
        from ..core.recall_policy import EMBEDDING_SPACE

        route = getattr(self.auxiliary, "embedding", None) if self.auxiliary is not None else None
        return route.space() if route is not None else dict(EMBEDDING_SPACE)

    def embedding_space_id(self) -> str:
        """Digest of the active space, which is also the vector directory name.

        Naming a different model changes this, which moves the store and refuses
        the old vectors rather than comparing across incompatible geometries.
        That is what makes swapping embedding models safe.
        """
        from ..core.recall_policy import embedding_space_id

        return embedding_space_id(self.embedding_space())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RuntimeInstanceConfig":
        if not isinstance(raw, Mapping):
            raise ValueError("runtime_config_mapping_required")
        binding_raw = raw.get("binding")
        if not isinstance(binding_raw, Mapping):
            raise ValueError("binding_mapping_required")
        binding = InstanceBinding(
            agent_id=_text("agent_id", binding_raw.get("agent_id")),
            installation_id=_text("installation_id", binding_raw.get("installation_id")),
            data_directory=_absolute("data_directory", binding_raw.get("data_directory")),
            scope_ids=frozenset(binding_raw.get("scope_ids") or ()),
            test_mode=binding_raw.get("test_mode", False),
        )
        allowed = frozenset(raw.get("allowed_scope_ids") or ())
        aux_raw = raw.get("auxiliary")
        if aux_raw is None:
            aux = AuxiliaryRuntimeConfig.from_mapping({"external_embedding": False, "external_consolidation": False})
        else:
            aux = AuxiliaryRuntimeConfig.from_mapping(aux_raw)
        vector_raw = raw.get("vector")
        vector = None
        if vector_raw is not None:
            if not isinstance(vector_raw, Mapping):
                raise ValueError("vector_mapping_required")
            vector = VectorRuntimeConfig(
                backend=vector_raw.get("backend", "lancedb"),
                storage_dir=_absolute("vector_storage_dir", vector_raw.get("storage_dir")),
                table_name=_text("vector_table_name", vector_raw.get("table_name")),
                dimensions=_strict_positive_int("vector_dimensions", vector_raw.get("dimensions")),
                metric=vector_raw.get("metric", "cosine"),
                test_injection_override=vector_raw.get("test_injection_override", False),
            )
        return cls(
            binding=binding,
            session_id=_text("session_id", raw.get("session_id")),
            allowed_scope_ids=allowed,
            actor_origin=_actor_origin(raw.get("actor_origin", "human_direct")),
            project_id=raw.get("project_id"),
            branch_id=raw.get("branch_id"),
            host_adapter=raw.get("host_adapter"),
            owner_id=raw.get("owner_id", "scope-recall-worker"),
            request_seconds=raw.get("request_seconds", 45.0),
            drain_seconds=raw.get("drain_seconds", 120.0),
            auto_recall_seconds=raw.get("auto_recall_seconds", 5.0),
            hook_processing_seconds=raw.get("hook_processing_seconds", 6.0),
            max_items=raw.get("max_items", 32),
            lease_seconds=raw.get("lease_seconds", 60.0),
            auxiliary=aux,
            vector=vector,
            vector_threshold=raw.get("vector_threshold"),
            daily_work_limit=raw.get("daily_work_limit", 0),
            auto_retry_cooldown_seconds=raw.get("auto_retry_cooldown_seconds", 3600.0),
            max_auto_recoveries=raw.get("max_auto_recoveries", 2),
            worker_min_interval_seconds=raw.get("worker_min_interval_seconds", 30.0),
            supervisor_enabled=raw.get("supervisor_enabled", True),
            supervisor_seconds=raw.get("supervisor_seconds", 21600.0),
            supervisor_max_drains=raw.get("supervisor_max_drains", 256),
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


from .embedding_retry import embed_with_one_retry


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
        prepared = []
        def embed_while_opening():
            from ..adapters.lance import LanceVectorPort
            space_id = self._instance.config.embedding_space_id()
            embedding = getattr(self._instance.auxiliary, 'query_embedding', None)
            if embedding is not None:
                embedding_remaining = deadline.remaining()
                if embedding_remaining <= 0:
                    raise TimeoutError('query embedding stage deadline exhausted')
                adapter = LanceVectorPort(None, embedding, expected_embedding_space=space_id)
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
    _vector_facade: Any = None
    _ingress_authorizer: Callable[[object], frozenset[str]] | None = None
    _owned_resources: list[Any] = field(default_factory=list)
    _closed: bool = False
    background_gaps: tuple[str, ...] = ()
    #: Receipt of the vector compaction this drain ran, or ``None``.
    vector_compaction: dict | None = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("runtime_instance_closed")

    def _ensure_vector_port(self, *, allow_create: bool = False, deadline: float | None = None,
                            during_open=None) -> Any:
        def open_resource(resource) -> None:
            opener = getattr(resource, "open" if allow_create else "open_existing", None)
            overlap = getattr(resource, "open_existing_with_work", None)
            if not allow_create and during_open is not None and callable(overlap):
                def opener():
                    return overlap(during_open)

            if callable(opener):
                if deadline is None:
                    opener()
                else:
                    with using_request_deadline(
                        RequestDeadline.from_absolute(deadline, now=time.monotonic())
                    ):
                        opener()

        self._ensure_open()
        if self._vector_store is not None:
            if getattr(self._vector_store, "requires_reopen", False):
                open_resource(self._vector_store)
            return self._vector_port
        if self.config.vector is None or self._vector_factory is None:
            return None
        resource = self._vector_factory(self.config.vector)
        self._owned_resources.append(resource)
        try:
            open_resource(resource)
        except Exception:
            self._owned_resources.pop()
            closer = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            raise
        port = None
        # ``build_vector_store`` returns the existing native store, whereas
        # Core consumes the adapter that turns trusted SearchContext values
        # into partitioned native queries.  Compose that adapter lazily only
        # after the explicit existing-index open.  Injected VectorPort values
        # remain untouched for deterministic tests and alternate backends.
        query_embedding = getattr(self.auxiliary, "query_embedding", None)
        if query_embedding is not None and self.config.vector.backend in {"lancedb", "sqlite-bruteforce"}:
            from ..adapters.lance import LanceVectorPort

            space_id = self.config.embedding_space_id()
            port = LanceVectorPort(resource, query_embedding, expected_embedding_space=space_id)
            source_embedding = getattr(self.auxiliary, "source_embedding", None)
            if source_embedding is not None:
                from ..adapters.lance import LanceEmbedPort
                self._default_embed = LanceEmbedPort(
                    resource,
                    source_embedding,
                    agent_id=self.config.binding.agent_id,
                    installation_id=self.config.binding.installation_id,
                    embedding_space=space_id,
                )
        # Purge is a local native operation and must remain available even
        # when source/query embedding routes are disabled or unavailable.
        if self.config.vector.backend in {"lancedb", "sqlite-bruteforce"}:
            from ..adapters.lance import LancePurgePort

            self._default_purge = LancePurgePort(
                resource,
                embedding_spaces=(self.config.embedding_space_id(),),
                agent_id=self.config.binding.agent_id,
                installation_id=self.config.binding.installation_id,
            )
        self._vector_store = resource
        self._vector_port = port
        # Keep the facade installed so every later call still supplies its
        # own SearchContext identity/scope and deadline.  ``port`` is only the
        # private, already-open delegate.
        return port

    def status(self) -> Any:
        self._ensure_open()
        return self.core.status(self.config.context())

    def recall(self, request: Mapping[str, Any], *, current_source_refs: tuple[str, ...] = ()) -> Any:
        return self.core.recall(self.config.context(), dict(request), current_source_refs=current_source_refs,
                                deadline_seconds=self.config.request_seconds)

    def drain(self, *, embed: Any = None, purge: Any = None, consolidation: Any = None,
              max_items: int | None = None, purge_only: bool = False,
              remaining_seconds: float | None = None) -> Any:
        self._ensure_open()
        budget = self.config.drain_seconds if remaining_seconds is None else remaining_seconds
        _strict_float("remaining_seconds", budget, minimum=.001, maximum=self.config.drain_seconds)
        deadline = time.monotonic() + budget
        self.ingress_receipts = ()
        ingress_gaps = ()
        # Ingress persistence does not spend optional model-work budget.
        from ..core.capture_inbox import replay_inbox
        if self._ingress_authorizer is None:
            try:
                context = self.config.context()
                scopes = tuple(sorted(context.allowed_scope_ids))
                with self.core.storage.read(context, remaining_seconds=min(1.0, budget / 8)) as tx:
                    pending = tx._check().execute(
                        f"""SELECT 1 FROM capture_inbox
                        WHERE scope_id IN ({','.join('?' for _ in scopes)})
                          AND project_id IS ? AND branch_id IS ? LIMIT 1""",
                        (*scopes, context.project_id, context.branch_id),
                    ).fetchone()
                if pending is not None:
                    ingress_gaps = (
                        "capture_gap:durable_ingress_authorizer_unconfigured",
                        "capture_gap:durable_ingress_pending",
                    )
            except (ContractError, OSError, RuntimeError, ValueError):
                ingress_gaps = ("capture_gap:durable_ingress_pending",)
        else:
            try:
                self.ingress_receipts = replay_inbox(
                    self.core.storage,
                    self.core.clock,
                    self.config.context(),
                    authorize=self._ingress_authorizer,
                    admission_policy=self.core.config.admission_policy,
                    remaining_seconds=min(2.0, budget / 4),
                )
                # A key-collided capture is invisible to the replay above, which
                # only retries failures that might clear by themselves. Without
                # this the payload stays in the inbox permanently — captured,
                # never stored, and reported only as a doctor gap.
                from ..core.capture_inbox import resolve_conflicted_ingress

                self.ingress_receipts = tuple(self.ingress_receipts) + resolve_conflicted_ingress(
                    self.core.storage,
                    self.core.clock,
                    self.config.context(),
                    authorize=self._ingress_authorizer,
                    admission_policy=self.core.config.admission_policy,
                    remaining_seconds=min(2.0, budget / 4),
                )
            except (ContractError, OSError, RuntimeError, ValueError):
                ingress_gaps = ("capture_gap:durable_ingress_pending",)
        # Explicit background maintenance is the sole path allowed to create
        # an initial vector index.  Query paths always use open_existing.
        try:
            vector_deadline = min(deadline, time.monotonic() + min(self.config.request_seconds, budget / 4))
            self._ensure_vector_port(allow_create=True, deadline=vector_deadline)
            self.background_gaps = ingress_gaps
        except Exception as exc:
            from ..lance_process_store import NativeVectorPathError
            code = NativeVectorPathError.code if isinstance(exc, NativeVectorPathError) else f"vector_unavailable:{type(exc).__name__}"
            self.background_gaps = (*ingress_gaps, code)
            # Optional indexing failure cannot suppress a healthy independent
            # consolidation route. Purge stays pending unless acknowledged.
        # Vector upkeep comes before the queue, not after it: on a busy
        # instance the budget is gone by the time the queue drains, so upkeep
        # at the end is upkeep that only ever runs when it is not needed.
        self.vector_compaction = compact_if_due(
            self._vector_store,
            self.config.vector,
            available_seconds=max(0.0, deadline - time.monotonic()),
        )
        # The durable worker owns the overall drain budget, while every model
        # or native boundary receives the stricter per-call budget.  Keeping
        # this wrapper at the runtime boundary prevents a future worker port
        # from accidentally turning the 45 second contract into 120 seconds.
        from ..core.worker import WorkerConfig, build_consolidation_model, drain_worker

        model = consolidation
        if model is None:
            from ..adapters.codex_cli import CodexCliConsolidationAdapter

            port = self.core.consolidation
            # This allowance is shared by consolidation and candidate evaluation
            # within this pass; no new scheduler, lease or queue policy.
            if isinstance(port, CodexCliConsolidationAdapter):
                port = port.for_pass()
            model = build_consolidation_model(port)
        candidate_model = (
            _BoundedCandidate(model, self.config.request_seconds)
            if callable(getattr(model, "evaluate_candidate", None)) else None
        )
        if model is not None:
            model = _BoundedConsolidation(model, self.config.request_seconds)
        effective_embed = embed if embed is not None else self._default_embed
        effective_purge = purge if purge is not None else self._default_purge
        bounded_embed = _BoundedEmbed(effective_embed, self.config.request_seconds) if effective_embed is not None else None
        bounded_purge = _BoundedPurge(effective_purge, self.config.request_seconds) if effective_purge is not None else None
        return drain_worker(
            self.core.storage, self.core.clock, self.config.context(),
            config=WorkerConfig(owner_id=self.config.owner_id,
                                max_items=self.config.max_items if max_items is None else max_items,
                                lease_seconds=self.config.lease_seconds,
                                auto_retry_cooldown_seconds=self.config.auto_retry_cooldown_seconds,
                                max_auto_recoveries=self.config.max_auto_recoveries,
                                purge_only=purge_only,
                                admission_policy=self.core.config.admission_policy),
            remaining_seconds=max(.001, deadline - time.monotonic()),
            consolidation=model,
            candidate=candidate_model,
            embed=bounded_embed,
            purge=bounded_purge,
        )

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
                now=_utc_now(),
                operation_id=operation_id,
                expected_memory_epoch=expected_memory_epoch,
                max_items=min(8, self.config.max_items),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in reversed(self._owned_resources):
            closer = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        self._owned_resources.clear()
        self._vector_store = None
        self._vector_port = None
        self._vector_facade = None
        self._default_embed = None
        self._default_purge = None


def default_vector_factory(config: VectorRuntimeConfig) -> Any:
    """Build an existing-companion store without opening or creating it."""
    from ..vector_store import build_vector_store

    return build_vector_store(
        config.backend,
        storage_dir=config.storage_dir,
        table_name=config.table_name,
        dimensions=config.dimensions,
        metric=config.metric,
    )


class _BoundedConsolidation:
    def __init__(self, inner: Any, limit: float) -> None:
        self._inner = inner
        self._limit = limit

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
        repair = {"validation_feedback": validation_feedback} if validation_feedback is not None else {}
        return self._inner.propose(
            sources,
            episode_ref=episode_ref,
            remaining_seconds=min(float(remaining_seconds), self._limit),
            **repair,
        )


class _BoundedCandidate:
    def __init__(self, inner: Any, limit: float) -> None:
        self._inner = inner
        self._limit = limit

    def evaluate_candidate(self, candidate, sources, *, remaining_seconds=1.0, validation_feedback=None):
        repair = {"validation_feedback": validation_feedback} if validation_feedback is not None else {}
        return self._inner.evaluate_candidate(
            candidate, sources, remaining_seconds=min(float(remaining_seconds), self._limit),
            **repair,
        )


class _BoundedEmbed:
    def __init__(self, inner: Any, limit: float) -> None:
        self._inner = inner
        self._limit = limit

    def prepare_source(self, source, *, remaining_seconds=1.0):
        return self._inner.prepare_source(
            source, remaining_seconds=min(float(remaining_seconds), self._limit)
        )

    def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard,
                       remaining_seconds=1.0):
        return self._inner.publish_source(
            prepared,
            source=source,
            lease_token=lease_token,
            lease_owner=lease_owner,
            lease_guard=lease_guard,
            remaining_seconds=min(float(remaining_seconds), self._limit),
        )

    # Claims go through the same port. The worker decides whether a port supports
    # claims by probing for these, and it probes this wrapper rather than the port
    # inside it — so forwarding only the source pair made every claim report
    # model_unavailable while the real port supported it.
    #
    # Defined through __getattr__, not as plain methods: a plain method always
    # answers the probe, so a port that genuinely has no claim support would look
    # capable and fail later at call time. __getattr__ only fires when normal
    # lookup misses, which lets absence stay absent.
    def __getattr__(self, name):
        if name not in {"prepare_claim", "publish_claim"}:
            raise AttributeError(name)
        inner = getattr(self._inner, name, None)
        if inner is None:
            raise AttributeError(name)
        limit = self._limit

        def bounded(*args, remaining_seconds=1.0, **kwargs):
            return inner(*args, remaining_seconds=min(float(remaining_seconds), limit), **kwargs)

        return bounded


class _BoundedPurge:
    def __init__(self, inner: Any, limit: float) -> None:
        self._inner = inner
        self._limit = limit

    def purge_active(self, operation_id, *, receipt, remaining_seconds=1.0):
        return self._inner.purge_active(
            operation_id,
            receipt=receipt,
            remaining_seconds=min(float(remaining_seconds), self._limit),
        )


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
    from ..core.recall_policy import RecallPolicy

    core = MemoryCore(
        CoreConfig(config.binding, auto_recall_seconds=config.auto_recall_seconds),
        storage=SQLiteStorage(config.binding),
        vectors=vectors,
        consolidation=consolidation if consolidation is not None else getattr(auxiliary, "consolidation", None),
        retrieval_policy=RecallPolicy(vector_threshold=config.vector_threshold),
    )
    owned = [vectors] if vectors is not None else []
    ingress_authorizer = None
    if config.host_adapter == "hermes":
        from ..adapters.hermes.authorization import build_ingress_authorizer

        ingress_authorizer = build_ingress_authorizer(config.binding)
    elif config.host_adapter == "codex":
        from ..adapters.codex.authorization import build_ingress_authorizer

        ingress_authorizer = build_ingress_authorizer(config.binding)
    if auxiliary is not None:
        owned.append(auxiliary)
    instance = RuntimeInstance(
        config=config,
        core=core,
        auxiliary=auxiliary,
        _vector_factory=vector_factory or (default_vector_factory if config.vector is not None else None),
        _vector_port=vectors,
        _vector_store=vectors,
        _ingress_authorizer=ingress_authorizer,
        _owned_resources=owned,
    )
    if vectors is None and config.vector is not None:
        facade = _LazyVectorPort(instance)
        instance._vector_facade = facade
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
