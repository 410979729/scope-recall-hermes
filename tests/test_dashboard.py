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
        return {"journal": {"enabled": True}, "vector": {"enabled": True, "backend": "lancedb", "fallback_backend": "sqlite-bruteforce", "index_general": True}}

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
        return ({"pyproject_version": "1.6.0"}, {"ok": True}, [])

    @staticmethod
    def sqlite_report(hermes_home):
        return (
            {
                "memory_count": 42,
                "schema_migrations": {"current": True, "version": 4},
                "candidate_debt": {"count": 3, "oldest_age_hours": 12.5},
                "memory_quality_lint": {"active_hits": 2, "high_severity": 1},
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
                "entries": {"unprocessed": 0},
                "digest_health": {"status": "degraded", "retry_exhausted_rejections": 1, "recovery_queue": {"retry_exhausted_candidates": 2, "dead_letter_candidates": 4}},
            },
            {"ok": True},
            ["journal degraded"],
        )

    @staticmethod
    def experience_report(hermes_home):
        return (
            {
                "promotion_funnel": {"needs_review": 5, "promoted": 2, "duplicate_groups": [{"title": "dup"}]},
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
                "status": "fallback_ready",
                "backend": backend,
                "ready": True,
                "primary": {"status": "needs_repair", "error": "No module named 'lancedb'"},
                "fallback_backend": fallback_backend,
                "fallback": {"status": "ready", "backend": fallback_backend, "row_count": 831},
            },
            {"ok": True, "failures": []},
            [],
        )


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
    assert payload["summary"]["memory_quality_active_hits"] == 2
    assert payload["summary"]["fact_freshness_needs_live_check"] == 3
    assert payload["summary"]["fact_freshness_expired"] == 1
    assert payload["summary"]["fact_freshness_total"] == 11
    assert payload["sections"]["candidate_debt"]["count"] == 3
    assert payload["sections"]["memory_quality_lint"]["high_severity"] == 1
    assert payload["sections"]["schema_migration"]["current"] is True
    assert payload["sections"]["freshness"]["by_status"]["expired"] == 1
    assert payload["trend"]["journal_unprocessed"]["delta"] == -9
    assert payload["trend"]["candidate_debt_count"]["delta"] == 2


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
    assert payload["summary"]["vector_status"] == "fallback_ready"
    assert payload["summary"]["vector_backend"] == "lancedb"
    assert payload["sections"]["vector"]["fallback_backend"] == "sqlite-bruteforce"
    assert payload["sections"]["vector"]["fallback"]["status"] == "ready"
