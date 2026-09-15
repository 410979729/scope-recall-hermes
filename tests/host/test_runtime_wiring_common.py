"""Shared trusted runtime glue contracts."""
from __future__ import annotations

from pathlib import Path
import json
import pytest

from scope_recall.adapters.runtime_wiring import GAP_UNCONFIGURED, attach_trusted_host_runtime
from scope_recall.contracts import InstanceBinding


def test_host_context_keeps_canonical_diagnostics_as_evidence_metadata():
    from scope_recall.adapters.runtime_wiring import RECALL_CONTEXT_GUIDANCE, render_host_recall_context

    canonical = '{"status":"partial","gaps":["budget_token_cap"],"unmet_needs":["expandable"],"items":[{"content":"keep the report and checklist together"}]}'
    rendered = render_host_recall_context(canonical)
    guidance, payload = rendered.split("\n", 1)
    assert guidance == RECALL_CONTEXT_GUIDANCE
    assert "not user facts or task requirements" in guidance
    assert payload == canonical
    assert json.loads(payload)["gaps"] == ["budget_token_cap"]
    assert render_host_recall_context(None) == render_host_recall_context("") == ""


def _runtime_payload(binding: InstanceBinding) -> dict:
    return {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(binding.data_directory),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
        },
        "session_id": "TEST-session",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "actor_origin": "human_direct",
        "owner_id": "TEST-owner",
        "request_seconds": 5.0,
        "drain_seconds": 5.0,
        "max_items": 1,
        "lease_seconds": 5.0,
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }


def test_optional_runtime_attach_does_not_initialize_missing_basic_storage(tmp_path: Path):
    data = tmp_path / "uninstalled-data"
    binding = InstanceBinding(
        agent_id="TEST-agent",
        installation_id="TEST-installation",
        data_directory=data,
        scope_ids=frozenset({"TEST-scope"}),
        test_mode=True,
    )
    attached = attach_trusted_host_runtime(
        config_path=tmp_path / "missing-runtime.json",
        expected_binding=binding,
        session_id="TEST-session",
        allowed_scope_ids=binding.scope_ids,
    )
    assert GAP_UNCONFIGURED in attached.capability_gaps
    assert not (data / "memory.sqlite3").exists()
    attached.close()


def test_default_runtime_config_is_only_loaded_from_binding_data_directory(tmp_path: Path):
    data = tmp_path / "owned-data"
    data.mkdir()
    binding = InstanceBinding("TEST-agent", "TEST-installation", data, frozenset({"TEST-scope"}), True)
    (data / "runtime-config.json").write_text(json.dumps(_runtime_payload(binding)), encoding="utf-8")
    attached = attach_trusted_host_runtime(
        config_path=None,
        expected_binding=binding,
        session_id="TEST-session",
        allowed_scope_ids=binding.scope_ids,
    )
    assert attached.configured
    assert attached._config_path == (data / "runtime-config.json").resolve()
    attached.close()


def test_default_runtime_config_rejects_foreign_binding(tmp_path: Path):
    data = tmp_path / "owned-data"
    data.mkdir()
    binding = InstanceBinding("TEST-agent", "TEST-installation", data, frozenset({"TEST-scope"}), True)
    foreign = dict(_runtime_payload(binding), binding={**_runtime_payload(binding)["binding"], "agent_id": "OTHER-agent"})
    (data / "runtime-config.json").write_text(json.dumps(foreign), encoding="utf-8")
    attached = attach_trusted_host_runtime(
        config_path=None,
        expected_binding=binding,
        session_id="TEST-session",
        allowed_scope_ids=binding.scope_ids,
    )
    assert not attached.configured
    assert "capability_gap:trusted_runtime_binding_mismatch" in attached.capability_gaps
    attached.close()


def test_default_runtime_config_rejects_symlink_when_supported(tmp_path: Path):
    data = tmp_path / "owned-data"
    data.mkdir()
    foreign = tmp_path / "foreign-runtime.json"
    binding = InstanceBinding("TEST-agent", "TEST-installation", data, frozenset({"TEST-scope"}), True)
    foreign.write_text(json.dumps(_runtime_payload(binding)), encoding="utf-8")
    candidate = data / "runtime-config.json"
    try:
        candidate.symlink_to(foreign)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    attached = attach_trusted_host_runtime(
        config_path=None,
        expected_binding=binding,
        session_id="TEST-session",
        allowed_scope_ids=binding.scope_ids,
    )
    assert not attached.configured
    assert "capability_gap:trusted_runtime_invalid" in attached.capability_gaps
    attached.close()
