"""Tests for dashboard response shape, output-file behavior, and vector-fallback health classification.

Dashboard contracts feed release readiness and operator health checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "scripts" / "report.dashboard.py"


def _load_dashboard():
    spec = importlib.util.spec_from_file_location("scope_recall_dashboard_test", DASHBOARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDoctor:
    @staticmethod
    def load_runtime_config(source_root, hermes_home):
        return {
            "journal": {"enabled": True},
            "vector": {
                "enabled": True,
                "backend": "lancedb",
                "fallback_backend": "sqlite-bruteforce",
                "index_general": True,
            },
            "writer_lease": {"idle_release_seconds": 1800.0},
        }

    @staticmethod
    def journal_enabled_from_config(config):
        return True

    @staticmethod
    def vector_enabled_from_config(config):
        return True

    @staticmethod
    def vector_backend_from_config(config):
        return "lancedb"

    @staticmethod
    def vector_fallback_backend_from_config(config):
        return "sqlite-bruteforce"

    @staticmethod
    def _index_general_enabled(config):
        return True

    @staticmethod
    def expected_embedder_from_config(config):
        return {"provider": "fixture"}

    @staticmethod
    def source_report(source_root):
        return ({"pyproject_version": "1.8.0"}, {"ok": True}, [])

    @staticmethod
    def sqlite_report(hermes_home):
        return (
            {
                "memory_count": 42,
                "schema_migrations": {"current": True, "version": 4},
                "candidate_debt": {"count": 3, "oldest_age_hours": 12.5},
                "memory_quality_lint": {"active_hits": 2, "high_severity": 1},
                "relation_containment": {
                    "status": "blocked",
                    "state": "blocked",
                    "scope_count": 2,
                    "scopes": [
                        {
                            "scope_id": "scope-a",
                            "state": "degraded",
                            "pending": 3,
                            "processing": 0,
                            "retry": 1,
                            "poisoned": 0,
                            "operator_action_required": False,
                            "auto_recoverable": True,
                            "stale_generation_count": 1,
                            "oldest_age_seconds": 12.5,
                        },
                        {
                            "scope_id": "scope-b",
                            "state": "blocked",
                            "pending": 1,
                            "processing": 0,
                            "retry": 0,
                            "poisoned": 1,
                            "operator_action_required": True,
                            "auto_recoverable": False,
                            "stale_generation_count": 1,
                            "oldest_age_seconds": 20.0,
                        },
                    ],
                },
                "relation_frequency_index": {
                    "status": "debt",
                    "dirty_memories": 4,
                    "focus_pending": 2,
                },
                "relation_rebuild_queue": {"status": "debt", "unresolved": 1},
            },
            {"ok": False, "failures": ["candidate debt"]},
            ["review candidates"],
        )

    @staticmethod
    def memory_secret_report(hermes_home):
        return ({"active_secret_like_count": 0}, {"ok": True}, [])

    @staticmethod
    def journal_report(hermes_home, *, enabled, journal_config):
        return (
            {
                "entries": {"unprocessed": 0, "oldest_unprocessed": "2026-07-01T00:00:00+00:00"},
                "backlog": {"oldest_unprocessed_age_hours": 7.5, "unprocessed_by_role": {"user": 1, "assistant": 2, "tool": 3}},
                "digest_health": {
                    "status": "degraded",
                    "retry_exhausted_rejections": 1,
                    "dead_letter_categories": {"auth": 6},
                    "recovery_queue": {"retry_exhausted_candidates": 2, "dead_letter_candidates": 4},
                },
            },
            {"ok": True},
            ["journal degraded"],
        )

    @staticmethod
    def experience_report(hermes_home):
        return (
            {
                "promotion_funnel": {
                    "needs_review": 5,
                    "promoted": 2,
                    "quarantined": 4,
                    "duplicate_groups": [{"title": "dup"}],
                    "feedback": {"stale": 1, "misleading": 2, "unresolved_stale": 0, "unresolved_misleading": 1},
                },
                "maturity": {
                    "feedback": {"stale": 1, "misleading": 2, "unresolved_stale": 0, "unresolved_misleading": 1},
                    "promoted_total": 2,
                    "promoted_missing_replay_cases": 2,
                },
                "fact_freshness": {"needs_live_check": 3, "by_status": {"expired": 1, "current": 7}, "tracked_facts": 11},
            },
            {"ok": True},
            [],
        )

    @staticmethod
    def nightly_digest_report(hermes_home):
        return ({"status": "ok"}, {"ok": True}, [])

    @staticmethod
    def vector_report(hermes_home, *, expected_embedder, backend, fallback_backend, index_general):
        assert fallback_backend == "sqlite-bruteforce"
        assert index_general is True
        return ({"status": "ready", "backend": backend, "fallback_backend": fallback_backend}, {"ok": True}, [])

    @staticmethod
    def disabled_vector_report():
        return ({"status": "disabled"}, {"ok": True}, [])


class FakeFallbackReadyDoctor(FakeDoctor):
    @staticmethod
    def sqlite_report(hermes_home):
        return ({"memory_count": 42, "schema_migrations": {"current": True, "version": 4}}, {"ok": True}, [])

    @staticmethod
    def memory_candidate_debt_report(hermes_home):
        return ({"candidate_count": 0, "oldest_age_hours": 0}, {"ok": True}, [])

    @staticmethod
    def memory_quality_lint_report(hermes_home):
        return ({"active_lint_hits": 0, "high_severity": 0}, {"ok": True}, [])

    @staticmethod
    def journal_report(hermes_home, *, enabled, journal_config):
        return ({"entries": {"unprocessed": 0}, "digest_health": {"status": "ok", "recovery_queue": {}}}, {"ok": True}, [])

    @staticmethod
    def experience_report(hermes_home):
        return ({"promotion_funnel": {"needs_review": 0, "promoted": 0, "duplicate_groups": []}, "fact_freshness": {"needs_live_check": 0, "expired": 0, "total": 0}}, {"ok": True}, [])

    @staticmethod
    def vector_report(hermes_home, *, expected_embedder, backend, fallback_backend, index_general):
        assert backend == "lancedb"
        assert fallback_backend == "sqlite-bruteforce"
        assert index_general is True
        return (
            {
                "state": "ready",
                "status": "ready",
                "reason_code": "fallback_ready",
                "diagnostic_status": "fallback_ready",
                "backend": backend,
                "ready": True,
                "primary": {"status": "needs_repair", "error": "No module named 'lancedb'"},
                "fallback_backend": fallback_backend,
                "fallback": {"status": "ready", "backend": fallback_backend, "row_count": 831},
            },
            {"ok": True, "failures": []},
            [],
        )


class FakeConfigErrorDoctor(FakeFallbackReadyDoctor):
    @staticmethod
    def load_runtime_config(source_root, hermes_home):
        return {
            "journal": {"enabled": True},
            "vector": {"enabled": False},
            "_config_load_errors": [
                {"path": str(hermes_home / "scope-recall" / "config.json"), "kind": "json_decode", "message": "invalid json"}
            ],
        }


def test_dashboard_payload_has_schema_severity_sections_and_trend(monkeypatch, tmp_path):
    dashboard = _load_dashboard()
    monkeypatch.setattr(dashboard, "_load_doctor", lambda: FakeDoctor)
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"summary": {"journal_unprocessed": 9, "candidate_debt_count": 1}}), encoding="utf-8")

    payload = dashboard.build_dashboard(tmp_path / "src", tmp_path / "home", previous_path=previous)

    assert payload["schema_version"] == "dashboard_report.v1"
    assert payload["severity"] == "FAIL"
    assert payload["ok"] is False
    assert payload["summary"]["candidate_debt_count"] == 3
    assert payload["summary"]["journal_unprocessed_oldest_at"] == "2026-07-01T00:00:00+00:00"
    assert payload["summary"]["journal_unprocessed_oldest_age_hours"] == 7.5
    assert payload["summary"]["journal_dead_letter_auth"] == 6
    assert payload["summary"]["journal_unprocessed_user"] == 1
    assert payload["summary"]["journal_unprocessed_assistant"] == 2
    assert payload["summary"]["journal_unprocessed_tool"] == 3
    assert payload["summary"]["memory_quality_active_hits"] == 2
    assert payload["summary"]["memory_feedback_stale"] == 1
    assert payload["summary"]["memory_feedback_misleading"] == 2
    assert payload["summary"]["memory_feedback_unresolved_misleading"] == 1
    assert payload["summary"]["experience_quarantined"] == 4
    assert payload["summary"]["experience_promoted_missing_replay_cases"] == 2
    assert payload["summary"]["fact_freshness_needs_live_check"] == 3
    assert payload["summary"]["fact_freshness_expired"] == 1
    assert payload["summary"]["fact_freshness_total"] == 11
    assert payload["summary"]["config_load_errors"] == 0
    assert payload["summary"]["relation_state"] == "blocked"
    assert payload["summary"]["relation_scope_count"] == 2
    assert payload["summary"]["relation_pending"] == 4
    assert payload["summary"]["relation_retry"] == 1
    assert payload["summary"]["relation_poisoned"] == 1
    assert payload["summary"]["relation_operator_action_scopes"] == 1
    assert payload["summary"]["relation_auto_recoverable_scopes"] == 1
    assert payload["summary"]["relation_stale_generation_count"] == 2
    assert payload["summary"]["relation_oldest_age_seconds"] == 20.0
    assert payload["summary"]["relation_legacy_unresolved"] == 1
    assert payload["summary"]["relation_frequency_dirty"] == 4
    assert payload["summary"]["relation_focus_pending"] == 2
    assert payload["sections"]["config_load"] == {"errors": []}
    assert payload["sections"]["candidate_debt"]["count"] == 3
    assert payload["sections"]["memory_quality_lint"]["high_severity"] == 1
    assert payload["sections"]["schema_migration"]["current"] is True
    assert payload["sections"]["relation_containment"]["state"] == "blocked"
    assert payload["sections"]["relation_frequency_index"]["dirty_memories"] == 4
    assert payload["sections"]["relation_rebuild_queue"]["unresolved"] == 1
    assert payload["sections"]["freshness"]["by_status"]["expired"] == 1
    assert payload["summary"]["writer_lease_scope"] == "process-wide-os-lock"
    assert payload["summary"]["writer_idle_release_enabled"] is True
    assert payload["summary"]["writer_idle_release_seconds"] == 1800.0
    assert payload["summary"]["writer_live_counters_source"] == "scope_recall_stats"
    handoff = payload["sections"]["writer_handoff"]
    assert handoff["snapshot_kind"] == "offline_config_only"
    assert handoff["runtime_state_observed"] is False
    assert handoff["live_counters"]["observed"] is False
    assert handoff["live_counters"]["source"] == "scope_recall_stats"
    assert "last_handoff_failure_code" in handoff["live_counters"]["fields"]
    assert payload["trend"]["journal_unprocessed"]["delta"] == -9
    assert payload["trend"]["candidate_debt_count"]["delta"] == 2
    assert "Relation operator-action scopes: `1`" in dashboard.render_markdown(payload)
    assert "Writer lease scope: `process-wide-os-lock`" in dashboard.render_markdown(payload)


def test_dashboard_reports_disabled_idle_release_as_offline_config_only(
    monkeypatch, tmp_path
):
    dashboard = _load_dashboard()

    class DisabledIdleReleaseDoctor(FakeFallbackReadyDoctor):
        @staticmethod
        def load_runtime_config(source_root, hermes_home):
            config = FakeFallbackReadyDoctor.load_runtime_config(
                source_root, hermes_home
            )
            config["writer_lease"] = {"idle_release_seconds": 0}
            return config

    monkeypatch.setattr(dashboard, "_load_doctor", lambda: DisabledIdleReleaseDoctor)

    payload = dashboard.build_dashboard(tmp_path / "src", tmp_path / "home")

    handoff = payload["sections"]["writer_handoff"]
    assert handoff["idle_release_enabled"] is False
    assert handoff["idle_release_seconds"] == 0.0
    assert handoff["runtime_state_observed"] is False
    assert "writer_role" not in handoff


def test_dashboard_surfaces_runtime_config_load_errors(monkeypatch, tmp_path):
    dashboard = _load_dashboard()
    monkeypatch.setattr(dashboard, "_load_doctor", lambda: FakeConfigErrorDoctor)

    payload = dashboard.build_dashboard(tmp_path / "src", tmp_path / "home")

    assert payload["ok"] is False
    assert payload["severity"] == "FAIL"
    assert payload["checks"]["config_load"]["ok"] is False
    assert payload["summary"]["config_load_errors"] == 1
    assert payload["sections"]["config_load"]["errors"][0]["kind"] == "json_decode"
    assert any("config" in item.lower() for item in payload["recommendations"])


def test_dashboard_cli_writes_output_file(monkeypatch, tmp_path):
    dashboard = _load_dashboard()
    monkeypatch.setattr(dashboard, "_load_doctor", lambda: FakeDoctor)
    output = tmp_path / "dashboard.json"

    exit_code = dashboard.main([
        "--hermes-home",
        str(tmp_path / "home"),
        "--source-root",
        str(tmp_path / "src"),
        "--output",
        str(output),
        "--format",
        "json",
    ])

    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "dashboard_report.v1"
    assert payload["severity"] == "FAIL"


def test_dashboard_treats_configured_sqlite_vector_fallback_as_healthy(monkeypatch, tmp_path):
    dashboard = _load_dashboard()
    monkeypatch.setattr(dashboard, "_load_doctor", lambda: FakeFallbackReadyDoctor)

    payload = dashboard.build_dashboard(tmp_path / "src", tmp_path / "home")

    assert payload["ok"] is True
    assert payload["severity"] == "OK"
    assert payload["checks"]["vector_companion"] == {"ok": True, "failures": []}
    assert payload["summary"]["vector_status"] == "ready"
    assert payload["summary"]["vector_backend"] == "lancedb"
    assert payload["sections"]["vector"]["fallback_backend"] == "sqlite-bruteforce"
    assert payload["sections"]["vector"]["fallback"]["status"] == "ready"
