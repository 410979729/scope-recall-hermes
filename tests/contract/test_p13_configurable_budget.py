"""P13 trusted recall and hook budget boundaries."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from scope_recall.adapters.codex.config import install_codex_scope_recall
from scope_recall.adapters.codex.handler import CodexHookHandler
from scope_recall.adapters.codex.mcp_server import build_server
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter, install_hermes_scope_recall
from scope_recall.adapters.runtime_wiring import TrustedHostRuntime
from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.composition import CoreConfig, MemoryCore
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance
from v11_support import recall_request


@dataclass
class FixedClock:
    value: float = 100.0

    def utc_now(self) -> str:
        return "2026-09-06T12:00:00Z"

    def monotonic(self) -> float:
        return self.value


def _binding(tmp_path: Path) -> InstanceBinding:
    return InstanceBinding(
        "TEST-budget-agent",
        "TEST-budget-installation",
        tmp_path / "data",
        frozenset({"TEST-budget-scope"}),
        True,
    )


def _runtime_raw(binding: InstanceBinding, **changes):
    raw = {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(binding.data_directory),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": True,
        },
        "session_id": "TEST-budget-session",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "actor_origin": "human_direct",
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    raw.update(changes)
    return raw


def _core_with_clock(tmp_path: Path, *, auto_seconds: float = 4.0):
    binding = _binding(tmp_path)
    clock = FixedClock()
    core = MemoryCore(CoreConfig(binding, auto_recall_seconds=auto_seconds), clock=clock)
    context = TrustedContext(binding, "TEST-budget-session", binding.scope_ids, "human_direct")
    return core, context, clock


def test_auto_budget_uses_trusted_core_config_and_shorter_caller_deadline_wins(tmp_path):
    core, context, clock = _core_with_clock(tmp_path)
    seen = []

    class Pipeline:
        def search(self, search_context):
            seen.append(search_context)
            return object()

    core.recall_pipeline = Pipeline()
    core.recall(context, recall_request(), deadline_seconds=None)
    assert seen[-1].deadline == 104.0
    core.recall(context, recall_request(), deadline_seconds=1.25)
    assert seen[-1].deadline == 101.25

    core.recall(
        context,
        recall_request(mode="history"),
        deadline_seconds=None,
    )
    assert seen[-1].deadline == 102.0
    assert clock.value == 100.0


def test_omitted_auto_and_hook_budgets_default_to_five_and_six(tmp_path):
    binding = _binding(tmp_path)
    core = MemoryCore(CoreConfig(binding), clock=FixedClock())
    assert core.config.auto_recall_seconds == 5.0
    constructed = RuntimeInstanceConfig(
        binding=binding,
        session_id="TEST-budget-session",
        allowed_scope_ids=binding.scope_ids,
    )
    mapped = RuntimeInstanceConfig.from_mapping(_runtime_raw(binding))
    assert constructed.auto_recall_seconds == mapped.auto_recall_seconds == 5.0
    assert constructed.hook_processing_seconds == mapped.hook_processing_seconds == 6.0
    instance = build_runtime_instance(mapped)
    try:
        assert instance.core.config.auto_recall_seconds == 5.0
        host = TrustedHostRuntime(core=instance.core, _runtime=instance)
        assert host.hook_processing_seconds == 6.0
    finally:
        instance.close()
    fallback = TrustedHostRuntime(core=core)
    assert fallback._hook_processing_seconds == 6.0
    assert fallback.hook_processing_seconds == 6.0

    project = tmp_path / "project"
    project.mkdir()
    _, codex_core = install_codex_scope_recall(
        tmp_path / "install", project_root=project, test_mode=True
    )
    assert codex_core.config.auto_recall_seconds == 5.0
    assert TrustedHostRuntime(core=codex_core).hook_processing_seconds == 6.0
    _, hermes_core = install_hermes_scope_recall(
        tmp_path / "hermes-home",
        agent_id="TEST-hermes-budget-agent",
        platform="cli",
        user_id="TEST-hermes-budget-user",
        agent_workspace="TEST-hermes-budget-workspace",
        test_mode=True,
    )
    assert hermes_core.config.auto_recall_seconds == 5.0


def test_explicit_shorter_auto_budget_is_honored_and_clamps_caller(tmp_path):
    core, context, clock = _core_with_clock(tmp_path, auto_seconds=1.5)
    seen = []

    class Pipeline:
        def search(self, search_context):
            seen.append(search_context)
            return object()

    core.recall_pipeline = Pipeline()
    core.recall(context, recall_request(), deadline_seconds=None)
    assert seen[-1].deadline == 101.5
    core.recall(context, recall_request(), deadline_seconds=0.4)
    assert seen[-1].deadline == 100.4
    core.recall(context, recall_request(), deadline_seconds=4.0)
    assert seen[-1].deadline == 101.5
    config = RuntimeInstanceConfig.from_mapping(
        _runtime_raw(core.config.binding, auto_recall_seconds=1.5, hook_processing_seconds=2.0)
    )
    assert config.auto_recall_seconds == 1.5
    assert config.hook_processing_seconds == 2.0
    instance = build_runtime_instance(config)
    try:
        assert instance.core.config.auto_recall_seconds == 1.5
        host = TrustedHostRuntime(core=instance.core, _runtime=instance)
        assert host.hook_processing_seconds == 2.0
    finally:
        instance.close()


@pytest.mark.parametrize("value", [True, 0, -0.1, 5.0001, math.nan, math.inf])
def test_core_auto_budget_rejects_invalid_values(tmp_path, value):
    with pytest.raises(ValueError):
        CoreConfig(_binding(tmp_path), auto_recall_seconds=value)


def test_runtime_config_injects_core_budget_and_validates_hook_cover(tmp_path):
    binding = _binding(tmp_path)
    config = RuntimeInstanceConfig.from_mapping(
        _runtime_raw(binding, auto_recall_seconds=4.0, hook_processing_seconds=5.0)
    )
    instance = build_runtime_instance(config)
    try:
        assert instance.core.config.auto_recall_seconds == 4.0
        host = TrustedHostRuntime(core=instance.core, _runtime=instance)
        assert host.hook_processing_seconds == 5.0
    finally:
        instance.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("auto_recall_seconds", True),
        ("auto_recall_seconds", math.inf),
        ("hook_processing_seconds", False),
        ("hook_processing_seconds", 6.1),
    ],
)
def test_runtime_budget_fields_reject_non_finite_bool_and_out_of_range(tmp_path, field, value):
    binding = _binding(tmp_path)
    with pytest.raises(ValueError):
        RuntimeInstanceConfig.from_mapping(_runtime_raw(binding, **{field: value}))


def test_runtime_rejects_hook_budget_shorter_than_auto(tmp_path):
    binding = _binding(tmp_path)
    with pytest.raises(ValueError, match="must_cover"):
        RuntimeInstanceConfig.from_mapping(
            _runtime_raw(binding, auto_recall_seconds=4.0, hook_processing_seconds=3.0)
        )


def test_codex_hook_reads_verified_runtime_budget_and_ignores_payload(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config, core = install_codex_scope_recall(
        tmp_path / "install", project_root=project, test_mode=True
    )
    host = TrustedHostRuntime(core=core, _hook_processing_seconds=5.0)
    handler = CodexHookHandler(config, host_runtime=host, clock=FixedClock())
    assert handler._hook_budget() == 5.0
    # Hook payloads never become a budget/configuration channel.
    assert handler._hook_budget() == 5.0

    deadlines = {}
    handler._session_start = lambda session_id, audience, deadline: deadlines.setdefault("start", deadline) or {}
    handler._user_prompt_submit = (
        lambda session_id, audience, payload, deadline: deadlines.setdefault("prompt", deadline) or {}
    )
    base = {"session_id": "TEST-budget-session", "cwd": str(project)}
    handler.handle_payload({**base, "hook_event_name": "SessionStart"})
    handler.handle_payload({**base, "hook_event_name": "UserPromptSubmit", "turn_id": "turn-1", "prompt": "x"})
    assert deadlines["start"] == 102.0
    assert deadlines["prompt"] == 105.0
    handler.close()
    started_handler = CodexHookHandler(
        config,
        host_runtime=host,
        clock=FixedClock(),
        hook_started_at=101.0,
    )
    started_handler._user_prompt_submit = (
        lambda session_id, audience, payload, deadline: deadlines.setdefault("started", deadline) or {}
    )
    started_handler.handle_payload(
        {**base, "hook_event_name": "UserPromptSubmit", "turn_id": "turn-2", "prompt": "x"}
    )
    assert deadlines["started"] == 106.0
    started_handler.close()


def test_codex_explicit_mcp_tool_uses_five_second_ceiling(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config, real_core = install_codex_scope_recall(
        tmp_path / "install", project_root=project, test_mode=True
    )
    calls = []

    class ProbeCore:
        config = real_core.config

        def memory_epoch(self, context):
            # The adapter reads the epoch back after compiling a packet so a
            # mutation racing the read-only compiler cannot hand a host a stale
            # result. A stub without it skipped that guard entirely and the
            # explicit five-second ceiling below was never actually exercised.
            return 0

        def recall_packet(self, context, request, **kwargs):
            calls.append(kwargs["deadline_seconds"])
            return {
                "protocol_version": "1.1",
                "request_id": request["request_id"],
                "status": "no_match",
                "memory_epoch": 0,
                "items": [],
                "gaps": [],
                "diagnostic_ref": None,
                "answerability": "unknown",
                "coverage": "complete_for_query",
                "unmet_needs": [],
            }

        def status(self, context):
            return SimpleNamespace(memory_epoch=0)

    host = TrustedHostRuntime(core=ProbeCore())
    server = build_server(config, workspace=project, host_runtime=host)
    tool = server.server._tool_manager.get_tool("recall")
    result = tool.fn(
        SimpleNamespace(request_context=SimpleNamespace(meta={})),
        "1.1",
        "unrelated query",
        "history",
        6,
        1200,
    )
    assert result["result"]["status"] == "no_match"
    assert calls == [5.0]


def test_hermes_explicit_tool_uses_five_second_ceiling(tmp_path):
    home = tmp_path / "hermes-home"
    binding, real_core = install_hermes_scope_recall(
        home,
        agent_id="TEST-hermes-budget-agent",
        platform="cli",
        user_id="TEST-hermes-budget-user",
        agent_workspace="TEST-hermes-budget-workspace",
        test_mode=True,
    )
    provider = ScopeRecallHermesAdapter(core=real_core, clock=FixedClock())
    provider.initialize(
        "TEST-hermes-budget-session",
        hermes_home=str(home),
        platform="cli",
        agent_context="primary",
        agent_identity=binding.agent_id,
        agent_workspace="TEST-hermes-budget-workspace",
        user_id="TEST-hermes-budget-user",
        parent_session_id="",
    )
    calls = []

    class ProbeCore:
        config = real_core.config

        def memory_epoch(self, context):
            # The adapter reads the epoch back after compiling a packet so a
            # mutation racing the read-only compiler cannot hand a host a stale
            # result. A stub without it skipped that guard entirely and the
            # explicit five-second ceiling below was never actually exercised.
            return 0

        def recall_packet(self, context, request, **kwargs):
            calls.append(kwargs["deadline_seconds"])
            return {
                "protocol_version": "1.1",
                "request_id": request["request_id"],
                "status": "no_match",
                "memory_epoch": 0,
                "items": [],
                "gaps": [],
                "diagnostic_ref": None,
                "answerability": "unknown",
                "coverage": "complete_for_query",
                "unmet_needs": [],
            }

        def status(self, context):
            return SimpleNamespace(memory_epoch=0)

    provider._core = ProbeCore()
    provider._handle_recall(
        {
            "protocol_version": "1.1",
            "query": "unrelated query",
            "mode": "history",
            "max_items": 6,
            "budget_tokens": 1200,
        }
    )
    assert calls == [5.0]
    provider.shutdown()
