"""A runtime instance's configuration: the vector store's, and the instance's own with its bounds, checked
as it is read."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Any

from ..contracts import ContractError, InstanceBinding, Origin, TrustedContext
from ..core.recall_policy import EMBEDDING_SPACE, RecallPolicy, embedding_space_id
from .auxiliary import AuxiliaryRuntimeConfig
from .validation import (
    absolute_path,
    identifier,
    mapping,
    member,
    strict_bool,
    strict_float,
    strict_int,
)

_RUNTIME_ORIGINS: frozenset[Origin] = frozenset({"human_direct", "tool_observation", "external_document", "imported"})


#: Claude Code runs the Codex adapter as an entry of a shared store (``adapters/clients/config.py``).
_HOST_ADAPTERS = frozenset({"hermes", "codex", "claude-code", "workbuddy", "dsh"})


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
    #: Days a tool output's vector is kept after its source entered the store;
    #: 0 keeps every vector (``runtime/vector_retention.py``).  The text, the
    #: lexical index and everything derived from the source are never expired.
    tool_output_retention_days: int = 180

    def __post_init__(self) -> None:
        member("vector_backend", self.backend, _VECTOR_BACKENDS)
        if not self.storage_dir.is_absolute():
            raise ValueError("vector_storage_dir_must_be_absolute")
        identifier("vector_table_name", self.table_name)
        strict_int("vector_dimensions", self.dimensions, minimum=1, maximum=8192)
        member("vector_metric", self.metric, ("cosine",))
        strict_bool("vector_test_injection_override", self.test_injection_override)
        strict_int("vector_tool_output_retention_days", self.tool_output_retention_days, minimum=0, maximum=36500)

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
            tool_output_retention_days=raw.get("tool_output_retention_days", 180),
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
    # A pass's own bound, matching the core's (``core/worker.py``: 1..1000).  Held
    # at 32 while every embedding was its own request and every vector its own
    # commit; now that a group shares both, what a bigger pass amortises is the
    # cost of starting a pass at all -- measured at 8 of the 18 seconds a pass of
    # two hundred took.  Model-bound work keeps its own per-pass bounds
    # (``candidate_batch_limit``, and the deadline for consolidation), so this
    # number decides how much cheap work shares one start, not how much money one
    # pass may spend.
    "max_items": (1, 1000),
    "daily_work_limit": (0, 1_000_000),
    "max_auto_recoveries": (0, 4),
    "supervisor_max_drains": (1, 1024),
    "storage_budget_bytes": (0, 1 << 50),
}


#: Fields assembled from nested mappings rather than copied from the top level.
_COMPOSED_FIELDS = frozenset({"binding", "allowed_scope_ids", "auxiliary", "vector"})


#: ``resident_recall_minutes``: none kept, up to a day.
RESIDENT_RECALL_MINUTES_BOUNDS = (0, 1440)


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
    #: arrival rate is not conservative, it is a permanent leak.  Measured on a
    #: 3.1 instance: 1.4 work items per captured source (an embed and, for
    #: most, a consolidation) plus 1.2 evaluations per candidate, so even the
    #: old 256 default could not drain a busy day's output.
    daily_work_limit: int = 0
    auto_retry_cooldown_seconds: float = 3600.0
    max_auto_recoveries: int = 2
    worker_min_interval_seconds: float = 30.0
    supervisor_enabled: bool = True
    supervisor_seconds: float = 21600.0
    supervisor_max_drains: int = 256
    #: Bytes the store and its vectors may occupy before the doctor reports
    #: ``storage_budget_exceeded``; 0 sets no budget.  Nothing is deleted for it.
    storage_budget_bytes: int = 0
    #: Minutes the resident prompt recall server of a client attached to a shared store stays up without a recall
    #: (``adapters/clients/resident_entry``); 0 keeps none.  Unset, the client's default applies
    #: (``adapters/clients/local_endpoint.RESIDENT_DEFAULT_MINUTES``).
    resident_recall_minutes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstanceBinding):
            raise ValueError("binding")
        identifier("session_id", self.session_id)
        if (
            type(self.allowed_scope_ids) is not frozenset
            or not self.allowed_scope_ids
            or not self.allowed_scope_ids <= self.binding.scope_ids
        ):
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
        if self.resident_recall_minutes is not None:
            low, high = RESIDENT_RECALL_MINUTES_BOUNDS
            strict_int("resident_recall_minutes", self.resident_recall_minutes, minimum=low, maximum=high)
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
        route = getattr(self.auxiliary, "embedding", None) if self.auxiliary is not None else None
        return route.space() if route is not None else dict(EMBEDDING_SPACE)

    def embedding_space_id(self) -> str:
        """Digest of the active space, which is also the vector directory name.

        Naming a different model changes this, which moves the store and refuses
        the old vectors rather than comparing across incompatible geometries.
        """
        return embedding_space_id(self.embedding_space())

    def recall_policy(self):
        """The admission policy recall runs with, bound to this instance's embedding space.

        The vector ports search partitions of, and stamp candidates with,
        ``embedding_space_id()``.  Admission has to compare against that same
        digest; against the shipped default every hit of a named route is refused.
        """
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
            installation_kind=binding_raw.get("installation_kind", "local"),
        )
        aux_raw = raw.get("auxiliary")
        if aux_raw is None:
            aux_raw = {"external_embedding": False, "external_consolidation": False}
        vector_raw = raw.get("vector")
        plain = {
            item.name: raw.get(item.name, None if item.default is MISSING else item.default)
            for item in fields(cls)
            if item.name not in _COMPOSED_FIELDS
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
