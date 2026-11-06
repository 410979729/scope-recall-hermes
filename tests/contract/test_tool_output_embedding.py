"""Tool output embedding is optional without losing captured text."""

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from scope_recall.adapters.clients.handler import _hook_core
from scope_recall.contracts import ImportProvenance, import_source_fingerprint
from scope_recall.core.admission import AdmissionPolicy, decide
from scope_recall.core.index_rebuild import queue_import_embeddings
from scope_recall.runtime.instance import build_runtime_instance
from scope_recall.runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig
from v11_support import source_event


def _vector(tmp_path, **changes):
    return {
        "storage_dir": str(tmp_path / "vectors"),
        "table_name": "TEST_vectors",
        "dimensions": 8,
        "test_injection_override": True,
        **changes,
    }


def _runtime(tmp_path, **changes):
    raw = {
        "binding": {
            "agent_id": "TEST-agent",
            "installation_id": "TEST-installation",
            "data_directory": str(tmp_path / "data"),
            "scope_ids": ["TEST-scope"],
            "test_mode": True,
        },
        "session_id": "TEST-session",
        "allowed_scope_ids": ["TEST-scope"],
        "vector": _vector(tmp_path, **changes),
    }
    instance = build_runtime_instance(RuntimeInstanceConfig.from_mapping(raw))
    instance.core.initialize()
    return instance


def _work(core, ref):
    with sqlite3.connect(core.storage.path) as conn:
        return {row[0] for row in conn.execute("SELECT work_type FROM work_items WHERE subject_ref=?", (ref,))}


@pytest.mark.parametrize("imported", [False, True])
def test_disabled_keeps_tool_sources_searchable_without_work(tmp_path, imported):
    instance = _runtime(tmp_path, tool_output_embedding=False)
    try:
        core, ctx = instance.core, instance.config.context()
        origin = "imported" if imported else "tool_observation"
        event = source_event(
            source_event_key="TEST-tool",
            origin=origin,
            role="tool",
            content="TEST zirconium build result 42",
            **({"source_original_origin": "tool_observation"} if imported else {}),
        )
        saved = core.record_event(replace(ctx, actor_origin=origin), event, scope_id="TEST-scope", remaining_seconds=10)
        ref = saved.event_refs[0].ref
        assert saved.admission == ("admission_source_only:tool_output_not_embedded",)
        assert _work(core, ref) == set()
        with sqlite3.connect(core.storage.path) as conn:
            assert conn.execute("SELECT count(*) FROM source_events WHERE event_id=?", (ref,)).fetchone()[0] == 1
        assert ref in {source.ref for source in core.search_sources(ctx, "zirconium")}
    finally:
        instance.close()


@pytest.mark.parametrize(
    "origin,role,expected",
    [("human_direct", "user", {"embed", "consolidate"}), ("assistant_visible", "assistant", {"embed"})],
)
def test_disabled_keeps_other_sources_scheduled(tmp_path, origin, role, expected):
    instance = _runtime(tmp_path, tool_output_embedding=False)
    try:
        core, ctx = instance.core, instance.config.context()
        event = source_event(
            source_event_key="TEST-other", origin=origin, role=role, content="TEST substantive message"
        )
        saved = core.record_event(replace(ctx, actor_origin=origin), event, scope_id="TEST-scope", remaining_seconds=10)
        assert _work(core, saved.event_refs[0].ref) == expected
    finally:
        instance.close()


def test_default_schedules_tool_embedding(tmp_path):
    instance = _runtime(tmp_path)
    try:
        core, ctx = instance.core, instance.config.context()
        event = source_event(
            source_event_key="TEST-tool", origin="tool_observation", role="tool", content="TEST substantive tool result"
        )
        saved = core.record_event(
            replace(ctx, actor_origin="tool_observation"), event, scope_id="TEST-scope", remaining_seconds=10
        )
        assert _work(core, saved.event_refs[0].ref) == {"embed"}
    finally:
        instance.close()


def test_configuration_accepts_only_booleans(tmp_path):
    assert VectorRuntimeConfig.from_mapping(_vector(tmp_path)).tool_output_embedding is True
    assert (
        VectorRuntimeConfig.from_mapping(_vector(tmp_path, tool_output_embedding=False)).tool_output_embedding is False
    )
    for bad in (0, 1, "false", None, []):
        with pytest.raises(ValueError, match="vector_tool_output_embedding"):
            VectorRuntimeConfig.from_mapping(_vector(tmp_path, tool_output_embedding=bad))


def test_embedding_setting_applies_when_admission_policy_is_disabled():
    event = source_event(origin="tool_observation", role="tool", content="TEST substantive tool result")
    decision = decide(None, event, None, AdmissionPolicy(enabled=False, tool_output_embedding=False))
    assert (decision.disposition, decision.reason) == ("source_only", "tool_output_not_embedded")


def test_hook_core_reads_the_bound_runtime_setting_before_capture(tmp_path):
    instance = _runtime(tmp_path, tool_output_embedding=False)
    try:
        config = instance.config
        from scope_recall.runtime.instance_config import configuration_value

        path = tmp_path / "runtime.json"
        path.write_text(json.dumps(configuration_value(config)), encoding="utf-8")
        hook_config = SimpleNamespace(to_binding=lambda: config.binding)
        core = _hook_core(hook_config, None, runtime_config_path=path)
        ctx = config.context()
        event = source_event(
            source_event_key="TEST-hook-tool", origin="tool_observation", role="tool", content="TEST cobalt tool result"
        )
        saved = core.record_event(
            replace(ctx, actor_origin="tool_observation"), event, scope_id="TEST-scope", remaining_seconds=10
        )
        assert saved.admission == ("admission_source_only:tool_output_not_embedded",)
        assert _work(core, saved.event_refs[0].ref) == set()
    finally:
        instance.close()


def test_changing_setting_keeps_previously_queued_work(tmp_path):
    instance = _runtime(tmp_path)
    try:
        core, ctx = instance.core, instance.config.context()
        event = source_event(
            source_event_key="TEST-old", origin="tool_observation", role="tool", content="TEST previous tool output"
        )
        old = core.record_event(
            replace(ctx, actor_origin="tool_observation"), event, scope_id="TEST-scope", remaining_seconds=10
        )
        core.config = replace(core.config, admission_policy=AdmissionPolicy(tool_output_embedding=False))
        event = source_event(
            source_event_key="TEST-new", origin="tool_observation", role="tool", content="TEST later tool output"
        )
        new = core.record_event(
            replace(ctx, actor_origin="tool_observation"), event, scope_id="TEST-scope", remaining_seconds=10
        )
        assert _work(core, old.event_refs[0].ref) == {"embed"}
        assert _work(core, new.event_refs[0].ref) == set()
    finally:
        instance.close()


def test_hook_rejects_another_bindings_setting(tmp_path):
    instance = _runtime(tmp_path, tool_output_embedding=False)
    try:
        from scope_recall.runtime.instance_config import configuration_value

        path = tmp_path / "other-runtime.json"
        raw = configuration_value(instance.config)
        raw["binding"]["installation_id"] = "TEST-other"
        path.write_text(json.dumps(raw), encoding="utf-8")
        hook_config = SimpleNamespace(to_binding=lambda: instance.config.binding)
        core = _hook_core(hook_config, None, runtime_config_path=path)
        assert core.config.admission_policy.tool_output_embedding is True
    finally:
        instance.close()


def test_import_backfill_respects_the_recorded_no_embedding_decision(tmp_path):
    instance = _runtime(tmp_path, tool_output_embedding=False)
    try:
        core, ctx = instance.core, instance.config.context()
        event = source_event(
            source_event_key="TEST-import-tool",
            origin="imported",
            source_original_origin="tool_observation",
            role="assistant",
            content="TEST imported titanium tool output",
        )
        provenance = ImportProvenance("tool_observation", "a" * 64, frozenset({import_source_fingerprint(event)}))
        saved = core.record_event(
            replace(ctx, actor_origin="imported", import_provenance=provenance),
            event,
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        ref = saved.event_refs[0].ref
        assert saved.admission == ("admission_source_only:tool_output_not_embedded",)
        assert queue_import_embeddings(core.storage, ctx)["queued"] == 0
        assert _work(core, ref) == set()
    finally:
        instance.close()
