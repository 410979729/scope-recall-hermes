"""Shared trusted runtime glue contracts."""
from __future__ import annotations

from pathlib import Path
import json
import pytest

from scope_recall.adapters.runtime_wiring import GAP_UNCONFIGURED, attach_trusted_host_runtime
from scope_recall.contracts import InstanceBinding


def test_host_context_keeps_canonical_diagnostics_as_evidence_metadata():
    from scope_recall.adapters.runtime_wiring import RECALL_CONTEXT_GUIDANCE, render_host_recall_context

    canonical = '{"gaps":["budget_token_cap"],"items":[{"content":"keep the report and checklist together"}],"status":"partial","unmet_needs":["expandable"]}'
    rendered = render_host_recall_context(canonical)
    guidance, payload = rendered.split("\n", 1)
    assert guidance == RECALL_CONTEXT_GUIDANCE
    assert "not user facts or task requirements" in guidance
    assert payload == canonical
    assert json.loads(payload)["gaps"] == ["budget_token_cap"]
    assert render_host_recall_context(None) == render_host_recall_context("") == ""


def test_memory_times_reach_the_model_in_the_hosts_zone():
    """Stored in UTC, shown in the zone the host tells its model it is in, offset included.

    A model shown 06:52:03+00:00, whose prompt named only the date and its
    zone, told the user it was 6:52 in the morning; the host's clock read 2:52.
    """
    from datetime import datetime, timedelta, timezone

    from scope_recall.adapters.runtime_wiring import render_host_recall_context
    from scope_recall.adapters.tool_common import envelope
    from scope_recall.core.recall_budget import canonical_render_json

    said = "TEST told at 2026-09-23T06:52:03Z"
    packet = {"items": [{"content": said, "occurred_at": "2026-09-23T06:52:03.250000Z"}, {"content": "TEST undated"}],
              "status": "ok"}
    new_york, shanghai = timezone(timedelta(hours=-4)), timezone(timedelta(hours=8))

    def shown(zone):
        return json.loads(render_host_recall_context(canonical_render_json(packet), zone=zone).split("\n", 1)[1])["items"]

    assert shown(new_york)[0]["occurred_at"] == "2026-09-23T02:52:03.250000-04:00"
    assert shown(shanghai)[0]["occurred_at"] == "2026-09-23T14:52:03.250000+08:00"
    assert shown(new_york)[0]["content"] == said, "what a memory says is never rewritten"
    assert "occurred_at" not in shown(new_york)[1]
    here = datetime(2026, 9, 23, 6, 52, 3, 250000, tzinfo=timezone.utc).astimezone().isoformat()
    assert shown(None)[0]["occurred_at"] == here, "a host without a zone of its own gets this machine's"

    result = envelope("TEST-reply", {
        "as_of": "2026-09-23T06:52:03Z",
        "items": [{"occurred_at": "2026-09-23T06:52:03+00:00", "valid_from": "2026-09-01T04:00:00Z", "valid_to": None,
                   "recorded_at": "TEST not a time"}],
    }, origin="memory_reinjection", zone=new_york)["result"]
    assert result["as_of"] == result["items"][0]["occurred_at"] == "2026-09-23T02:52:03-04:00"
    assert result["items"][0]["valid_from"] == "2026-09-01T00:00:00-04:00"
    assert (result["items"][0]["valid_to"], result["items"][0]["recorded_at"]) == (None, "TEST not a time")


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
