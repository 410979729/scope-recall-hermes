"""Focused ownership and fail-closed tests for digest observability."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import scope_recall.nightly_digest as nightly_digest
import scripts.doctor as doctor
from scope_recall.config import CONFIG_ENUM_VALUES, DEFAULT_CONFIG, load_runtime_config
from scope_recall.curation_observability import curation_status_projection
from scope_recall.nightly_digest import DigestOptions


ROOT = Path(__file__).resolve().parents[1]


def test_curation_owner_is_validated_and_defaults_internal(tmp_path: Path) -> None:
    assert DEFAULT_CONFIG["curation"] == {"owner": "internal"}
    assert CONFIG_ENUM_VALUES["curation.owner"] == frozenset(
        {"internal", "external", "manual"}
    )
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    (storage / "config.json").write_text(
        json.dumps({"curation": {"owner": "external"}}),
        encoding="utf-8",
    )

    loaded = load_runtime_config(ROOT, storage)

    assert loaded["curation"]["owner"] == "external"
    assert not loaded.get("_config_load_errors")


def test_external_projection_keeps_three_lanes_distinct() -> None:
    projected = curation_status_projection(
        {"curation": {"owner": "external"}, "journal": {"enabled": True}},
        journal_digest={
            "enabled": True,
            "last_status": "ready",
            "last_started": 12.0,
            "last_finished": 13.0,
        },
        nightly_digest={"enabled": True, "status": "degraded"},
    )

    assert projected["authoritative_owner"] == "external"
    assert projected["journal_digest"]["enabled"] is True
    assert projected["journal_digest"]["authoritative_for_curation"] is False
    assert projected["nightly_digest_legacy"]["last_status"] == "disabled_by_owner"
    assert projected["nightly_digest_legacy"]["authoritative_for_curation"] is False
    assert projected["external_curation"] == {
        "enabled": True,
        "owner": "hermes_nightly_memory_curation",
        "last_started": None,
        "last_finished": None,
        "last_status": "unobserved",
        "last_error_code": "",
        "authoritative_for_curation": True,
        "status_observed": False,
    }


def test_journal_error_without_reason_uses_content_free_stable_code() -> None:
    projected = curation_status_projection(
        {"curation": {"owner": "internal"}},
        journal_digest={
            "last_status": "error",
            "last_error": "raw provider response must not escape",
            "consecutive_failures": 3,
        },
        nightly_digest=None,
    )

    journal = projected["journal_digest"]
    assert journal["last_status"] == "error"
    assert journal["last_error_code"] == "journal_digest_error"
    assert "raw provider response" not in json.dumps(projected)


def test_nightly_digest_external_owner_exits_before_any_side_effect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    config_path = storage / "config.json"
    config_path.write_text(
        json.dumps({"curation": {"owner": "external"}}),
        encoding="utf-8",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("owner gate ran after a side-effect boundary")

    monkeypatch.setattr(nightly_digest, "resolve_session_db", forbidden)
    monkeypatch.setattr(nightly_digest, "resolve_llm_config", forbidden)
    monkeypatch.setattr(nightly_digest, "TruthWriterLease", forbidden)

    result = nightly_digest.run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=date(2026, 8, 30),
        )
    )

    assert result == {
        "ok": True,
        "status": "disabled_by_owner",
        "owner": "external",
        "reason_code": "curation_owner_is_not_internal",
        "digest_date": "2026-08-30",
        "sessions": 0,
    }
    assert sorted(path.name for path in storage.iterdir()) == [config_path.name]


def test_doctor_external_owner_disables_legacy_lane_without_false_health(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "curation": {"owner": "external"},
                "vector": {"enabled": False},
                "journal": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Doctor inspected an inactive legacy nightly lane")

    monkeypatch.setattr(doctor, "nightly_digest_report", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "doctor.py",
            "--source-root",
            str(ROOT),
            "--hermes-home",
            str(hermes_home),
            "--json",
        ],
    )

    doctor.main()
    payload = json.loads(capsys.readouterr().out)

    assert payload["checks"]["nightly_digest"] == {"ok": True, "failures": []}
    assert payload["runtime"]["nightly_digest"]["status"] == "disabled_by_owner"
    assert payload["runtime"]["curation"]["authoritative_owner"] == "external"
    assert (
        payload["runtime"]["curation"]["external_curation"]["last_status"]
        == "unobserved"
    )
