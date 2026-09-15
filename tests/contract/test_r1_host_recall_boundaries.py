"""R1 thread-3 host identity, replay, and automatic-context contracts."""
from __future__ import annotations

from dataclasses import replace
import inspect
import itertools
import json

import pytest

from scope_recall.adapters.codex import install_codex_scope_recall
from scope_recall.adapters.codex.identity import resolve_runtime_audience as codex_audience
from scope_recall.adapters.codex.identity import trusted_context as codex_context
from scope_recall.adapters.hermes import bind_hermes_identity, install_hermes_scope_recall
from scope_recall.adapters.runtime_wiring import write_ephemeral_worker_config
from scope_recall.contracts import ContractError, TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore, capture_inbox
from scope_recall.core.background_context import _subject_visible_to_current_principal
from scope_recall.core.read_views import _profile_subject
from scope_recall.core.retrieval import SearchContext
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance
from tests.contract.test_v11_claims import Clock, accept, capture, draft
from tests.v11_support import context, recall_request, source_event


def test_hermes_binds_stable_opaque_human_and_task_scope(tmp_path):
    home = tmp_path / "TEST-hermes"
    home.mkdir()
    install_hermes_scope_recall(
        home,
        agent_id="TEST-agent",
        platform="cli",
        user_id="TEST-user",
        agent_workspace="TEST-workspace",
        test_mode=False,
    )
    kwargs = {
        "hermes_home": str(home),
        "platform": "cli",
        "agent_context": "primary",
        "agent_identity": "TEST-agent",
        "agent_workspace": "TEST-workspace",
        "user_id": "TEST-user",
    }
    first = bind_hermes_identity("TEST-session-a", **kwargs).trusted_context()
    second = bind_hermes_identity("TEST-session-b", **kwargs).trusted_context()

    assert first.source_principal == second.source_principal
    assert first.source_principal is not None
    assert first.source_principal.kind == "human"
    assert first.source_principal.resolution == "verified"
    assert first.source_principal.principal_ref.startswith("principal:hermes-human:v1:")
    assert "TEST-user" not in first.source_principal.principal_ref
    assert first.project_id is second.project_id is None
    assert first.task_anchor != second.task_anchor


def test_codex_keeps_human_unresolved_but_binds_project_and_task(tmp_path):
    root = tmp_path / "TEST-codex"
    project = tmp_path / "TEST-project"
    project.mkdir()
    config, _core = install_codex_scope_recall(root, project_root=project, test_mode=True)
    audience = codex_audience(config, str(project))
    first = codex_context(config, audience, session_id="TEST-session-a")
    second = codex_context(config, audience, session_id="TEST-session-b")

    assert first.source_principal == TrustedSourcePrincipal("human", "unresolved")
    assert first.source_principal.principal_ref is None
    assert first.project_id is second.project_id is None
    assert first.task_anchor != second.task_anchor


def test_replay_preserves_original_principal_and_rechecks_current_rights(tmp_path):
    base = context(tmp_path / "TEST-replay")
    principal = TrustedSourcePrincipal("human", "verified", "principal:TEST-user")
    original = replace(base, source_principal=principal, project_id="TEST-project")
    core = MemoryCore(CoreConfig(base.binding))
    core.initialize()
    event = source_event(source_event_key="TEST-principal-replay", content="TEST replay identity")
    capture_inbox.enqueue(
        core.storage,
        core.clock,
        original,
        event,
        scope_id="TEST-scope",
        host_scope={"audience": "TEST-original"},
    )

    receipts = capture_inbox.replay_inbox(
        core.storage,
        core.clock,
        replace(original, source_principal=None),
        authorize=lambda raw: base.allowed_scope_ids if raw == {"audience": "TEST-original"} else frozenset(),
    )
    assert len(receipts) == 1 and receipts[0].durability == "persisted"
    stored = core.source_by_event_key(original, "TEST-principal-replay", 1)
    assert stored is not None
    assert stored.event["source_principal"] == principal.to_payload()

    revoked = source_event(source_event_key="TEST-principal-revoked", content="TEST revoked replay")
    capture_inbox.enqueue(
        core.storage,
        core.clock,
        original,
        revoked,
        scope_id="TEST-scope",
        host_scope={"audience": "TEST-original"},
    )
    cancelled = capture_inbox.replay_inbox(
        core.storage,
        core.clock,
        original,
        authorize=lambda _raw: frozenset(),
    )
    assert len(cancelled) == 1 and cancelled[0].disposition == "cancelled"
    assert core.source_by_event_key(original, "TEST-principal-revoked", 1) is None


def test_core_has_no_hermes_authorizer_and_runtime_selects_explicit_adapter(tmp_path):
    assert "hermes_authorizer" not in inspect.getsource(capture_inbox)
    binding = context(tmp_path / "TEST-runtime").binding
    for adapter in ("hermes", "codex"):
        config = RuntimeInstanceConfig(
            binding=binding,
            session_id="TEST-session",
            allowed_scope_ids=binding.scope_ids,
            host_adapter=adapter,
            drain_seconds=1.0,
            request_seconds=1.0,
            auto_recall_seconds=1.0,
            hook_processing_seconds=1.0,
            lease_seconds=1.0,
        )
        runtime = build_runtime_instance(config)
        try:
            assert runtime._ingress_authorizer is not None
            assert runtime._ingress_authorizer.__module__.endswith(f".{adapter}.authorization")
        finally:
            runtime.close()


def test_ephemeral_worker_keeps_host_and_exact_partition(tmp_path):
    base = context(tmp_path / "TEST-worker")
    base.binding.data_directory.mkdir(parents=True)
    payload = {
        "binding": {
            "agent_id": base.binding.agent_id,
            "installation_id": base.binding.installation_id,
            "data_directory": str(base.binding.data_directory),
            "scope_ids": sorted(base.binding.scope_ids),
            "test_mode": True,
        },
        "session_id": "TEST-old",
        "allowed_scope_ids": sorted(base.binding.scope_ids),
        "request_seconds": 1.0,
        "drain_seconds": 1.0,
        "auto_recall_seconds": 1.0,
        "hook_processing_seconds": 1.0,
        "lease_seconds": 1.0,
    }
    config_path = base.binding.data_directory / "runtime-config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    worker_path = write_ephemeral_worker_config(
        config_path,
        session_id="TEST-new",
        allowed_scope_ids=base.binding.scope_ids,
        expected_binding=base.binding,
        expected_partition=("TEST-project", "TEST-branch"),
        host_adapter="codex",
        project_id="TEST-project",
        branch_id="TEST-branch",
    )
    try:
        worker = RuntimeInstanceConfig.from_mapping(json.loads(worker_path.read_text(encoding="utf-8")))
        assert worker.host_adapter == "codex"
        assert (worker.project_id, worker.branch_id) == ("TEST-project", "TEST-branch")
        assert worker.session_id == "TEST-new"
    finally:
        worker_path.unlink(missing_ok=True)


def test_fresh_codex_runtime_replays_with_codex_authority(tmp_path):
    root = tmp_path / "TEST-codex-replay"
    project = tmp_path / "TEST-codex-project"
    project.mkdir()
    config, core = install_codex_scope_recall(root, project_root=project, test_mode=True)
    audience = codex_audience(config, str(project))
    original = codex_context(config, audience, session_id="TEST-capture")
    event = source_event(source_event_key="TEST-codex-worker-replay", content="TEST codex replay")
    capture_inbox.enqueue(
        core.storage,
        core.clock,
        original,
        event,
        scope_id=audience.capture_scope_id,
        host_scope={"cwd": str(project)},
    )
    runtime = build_runtime_instance(
        RuntimeInstanceConfig(
            binding=config.to_binding(),
            session_id="TEST-worker",
            allowed_scope_ids=audience.allowed_scope_ids,
            host_adapter="codex",
            request_seconds=2.0,
            drain_seconds=2.0,
            auto_recall_seconds=1.0,
            hook_processing_seconds=1.0,
            lease_seconds=2.0,
        )
    )
    try:
        runtime.drain(purge_only=True, remaining_seconds=2.0)
        assert len(runtime.ingress_receipts) == 1
        assert runtime.ingress_receipts[0].durability == "persisted"
        stored = runtime.core.source_by_event_key(original, "TEST-codex-worker-replay", 1)
        assert stored is not None
        assert stored.event["source_principal"] == {"kind": "human", "resolution": "unresolved"}
    finally:
        runtime.close()


def test_automatic_background_uses_latest_trusted_task_context(tmp_path):
    base = replace(
        context(tmp_path / "TEST-auto-context"),
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    core = MemoryCore(CoreConfig(base.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    source = capture(core, base, "写文案时 TEST-project 偏好简洁。")
    claim = accept(
        core,
        base,
        draft(
            source,
            value="简洁",
            kind="preference",
            predicate="表达偏好",
            statement_kind="assertion",
            conditions=["写文案时"],
        ),
    ).items[0]

    unrelated = recall_request(query="继续当前任务")
    assert claim.ref not in {item["ref"] for item in core.recall_packet(base, unrelated)["items"]}
    current_task = replace(base, recent_messages=("当前正在写文案",))
    selected = core.recall_packet(current_task, unrelated)
    assert claim.ref in {item["ref"] for item in selected["items"]}

    # A later task message replaces, rather than accumulates with, the old
    # context, so an ended exception cannot leak into the next task.
    ended = replace(base, recent_messages=("当前正在核对数据库迁移",))
    assert claim.ref not in {item["ref"] for item in core.recall_packet(ended, unrelated)["items"]}


def test_c2_current_subject_is_bound_only_from_verified_c1_principal(tmp_path):
    base = context(tmp_path / "TEST-c2-consumer")
    alice = replace(
        base,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-alice",
        ),
    )
    bob = replace(
        base,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-bob",
        ),
    )
    unresolved = replace(base, source_principal=TrustedSourcePrincipal("human", "unresolved"))

    assert _profile_subject(alice, "我") == "principal:TEST-alice"
    assert _profile_subject(alice, "current_user") == "principal:TEST-alice"
    assert _profile_subject(alice, "TEST-project") == "TEST-project"
    with pytest.raises(ContractError, match="IDENTITY_UNBOUND"):
        _profile_subject(unresolved, "user")

    request = recall_request(query="继续当前任务")
    alice_search = SearchContext.from_request(
        request,
        alice,
        now="2026-09-12T12:00:00Z",
        deadline=100.0,
    )
    bob_search = replace(alice_search, trusted_context=bob)
    unresolved_search = replace(alice_search, trusted_context=unresolved)
    assert _subject_visible_to_current_principal("principal:TEST-alice", alice_search)
    assert not _subject_visible_to_current_principal("principal:TEST-alice", bob_search)
    assert not _subject_visible_to_current_principal("principal:TEST-alice", unresolved_search)
    assert not _subject_visible_to_current_principal("unresolved-source:TEST", alice_search)
    assert not _subject_visible_to_current_principal("user", alice_search)
    assert _subject_visible_to_current_principal("TEST-project", unresolved_search)


def test_entity_current_subject_reads_only_the_verified_principal(tmp_path):
    base = context(tmp_path / "TEST-c2-entity-consumer")
    alice = replace(
        base,
        source_principal=TrustedSourcePrincipal(
            "human", "verified", principal_ref="principal:TEST-alice",
        ),
    )
    core = MemoryCore(CoreConfig(base.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    source = capture(core, alice, "principal:TEST-alice 配色 蓝色。")
    claim = accept(
        core,
        alice,
        draft(
            source,
            subject="principal:TEST-alice",
            value="蓝色",
            kind="fact",
            predicate="配色",
            statement_kind="assertion",
        ),
    ).items[0]

    result = core.entity(alice, {
        "protocol_version": "1.1",
        "request_id": "TEST-current-principal-entity",
        "subject": "我",
        "action": "related",
        "direction": "outgoing",
        "max_items": 8,
        "budget_tokens": 2048,
    })

    assert result["subject"] == "我"
    assert result["resolved_subject"] == "principal:TEST-alice"
    assert [item["ref"] for item in result["statements"]] == [claim.ref]
