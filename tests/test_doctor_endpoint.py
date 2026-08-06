from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scope_recall.doctor_endpoint import endpoint_policy_report

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_endpoint_policy_report_rejects_enabled_capture_unsafe_endpoint() -> None:
    payload, check, recommendations = endpoint_policy_report(
        {
            "capture_llm": {
                "enabled": True,
                "base_url": "file:///blocked-provider?api_key=must-not-appear",
                "allow_insecure_endpoint": False,
            },
            "vector": {"enabled": False},
        }
    )

    serialized = json.dumps(
        {"payload": payload, "check": check, "recommendations": recommendations},
        ensure_ascii=False,
    )
    assert check == {"ok": False, "checked": 1, "invalid": 1}
    assert payload["surfaces"] == [
        {
            "surface": "capture_llm",
            "enabled": True,
            "ok": False,
            "endpoint": "file://missing-host",
            "error": "unsafe endpoint scheme file; only HTTP(S) is supported",
        }
    ]
    assert recommendations == [
        "Fix unsafe Scope Recall provider endpoints before capture, digest, reflection, or embedding runs."
    ]
    assert "blocked-provider" not in serialized
    assert "must-not-appear" not in serialized
    assert "api_key" not in serialized


def test_doctor_cli_checks_inherited_journal_transport_and_redacts_path(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "home"
    runtime_dir = hermes_home / "scope-recall"
    runtime_dir.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  provider: blocked\n"
        "  model: blocked-model\n"
        "providers:\n"
        "  blocked:\n"
        "    base_url: file:///blocked-provider\n",
        encoding="utf-8",
    )
    (runtime_dir / "config.json").write_text(
        json.dumps(
            {
                "journal": {
                    "enabled": True,
                    "background_digest_enabled": True,
                    "extractor": "llm",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plugin_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(plugin_root / "scripts" / "doctor.py"),
            "--json",
            "--source-root",
            str(plugin_root),
            "--hermes-home",
            str(hermes_home),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["checks"]["endpoint_policy"]["ok"] is False
    surface = next(
        item
        for item in payload["runtime"]["endpoint_policy"]["surfaces"]
        if item["surface"] == "journal"
    )
    assert surface == {
        "surface": "journal",
        "enabled": True,
        "ok": False,
        "endpoint": "file://missing-host",
        "error": "unsafe endpoint scheme file; only HTTP(S) is supported",
    }
    assert "blocked-provider" not in result.stdout


@pytest.mark.parametrize(
    "path_marker",
    (
        "sk-test-marker",
        "token=test-marker",
        "opaque-test-marker",
    ),
)
def test_endpoint_policy_report_does_not_echo_arbitrary_configured_path_segments(
    path_marker: str,
) -> None:
    payload, check, _recommendations = endpoint_policy_report(
        {
            "capture_llm": {
                "enabled": True,
                "base_url": f"https://api.example.test/tenant/{path_marker}",
            }
        }
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert check == {"ok": True, "checked": 1, "invalid": 0}
    assert payload["surfaces"][0]["endpoint"] == (
        "https://api.example.test/v1/chat/completions"
    )
    assert path_marker not in serialized


@pytest.mark.parametrize(
    ("runtime_config", "surface"),
    [
        (
            {
                "journal": {
                    "enabled": True,
                    "extractor": "llm",
                    "endpoint": "file:///journal?refresh_token=must-not-appear",
                }
            },
            "journal",
        ),
        (
            {
                "reflection": {
                    "enabled": True,
                    "provider": "openai-compatible",
                    "model": "reviewer",
                    "base_url": "file:///reflection?id_token=must-not-appear",
                }
            },
            "reflection",
        ),
        (
            {
                "vector": {
                    "enabled": True,
                    "embedder": {
                        "provider": "openai-compatible",
                        "base_url": "file:///primary?api_key=must-not-appear",
                    },
                    "fallback_embedder": {"provider": "local-hash"},
                }
            },
            "vector.embedder",
        ),
        (
            {
                "vector": {
                    "enabled": True,
                    "embedder": {"provider": "local-hash"},
                    "fallback_embedder": {
                        "provider": "minimax",
                        "base_url": "file:///fallback?token=must-not-appear",
                    },
                }
            },
            "vector.fallback_embedder",
        ),
    ],
)
def test_endpoint_policy_report_checks_each_enabled_network_surface(
    runtime_config: dict[str, object],
    surface: str,
) -> None:
    payload, check, _recommendations = endpoint_policy_report(runtime_config)

    serialized = json.dumps(payload, ensure_ascii=False)
    assert check == {"ok": False, "checked": 1, "invalid": 1}
    assert payload["surfaces"][0]["surface"] == surface
    assert payload["surfaces"][0]["ok"] is False
    assert "must-not-appear" not in serialized


def test_endpoint_policy_report_inherits_journal_provider_transport_without_credentials(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  provider: blocked\n"
        "  model: reviewer\n"
        "providers:\n"
        "  blocked:\n"
        "    base_url: file:///blocked-provider\n",
        encoding="utf-8",
    )

    payload, check, recommendations = endpoint_policy_report(
        {"journal": {"enabled": True, "extractor": "llm"}},
        hermes_home=hermes_home,
    )

    assert check == {"ok": False, "checked": 1, "invalid": 1}
    assert payload["surfaces"][0]["surface"] == "journal"
    assert payload["surfaces"][0]["endpoint"] == "file://missing-host"
    assert recommendations


def test_endpoint_policy_report_uses_provider_transport_for_reflection(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "providers:\n"
        "  blocked:\n"
        "    base_url: file:///blocked-reflection-provider\n",
        encoding="utf-8",
    )

    payload, check, _recommendations = endpoint_policy_report(
        {
            "reflection": {
                "enabled": True,
                "provider": "blocked",
                "model": "reviewer",
            }
        },
        hermes_home=hermes_home,
    )

    assert check == {"ok": False, "checked": 1, "invalid": 1}
    assert payload["surfaces"][0]["surface"] == "reflection"
    assert payload["surfaces"][0]["endpoint"] == "file://missing-host"


def test_endpoint_policy_report_resolves_vector_base_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BLOCKED_VECTOR_BASE_URL", "file:///blocked-vector")

    payload, check, _recommendations = endpoint_policy_report(
        {
            "vector": {
                "enabled": True,
                "embedder": {
                    "provider": "openai-compatible",
                    "base_url_env": "BLOCKED_VECTOR_BASE_URL",
                },
            }
        }
    )

    assert check == {"ok": False, "checked": 1, "invalid": 1}
    assert payload["surfaces"][0]["surface"] == "vector.embedder"
    assert payload["surfaces"][0]["endpoint"] == "file://missing-host"


def test_doctor_cli_resolves_vector_base_url_env_from_runtime_config(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "home"
    runtime_dir = hermes_home / "scope-recall"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "enabled": True,
                    "embedder": {
                        "provider": "openai-compatible",
                        "base_url_env": "BLOCKED_VECTOR_BASE_URL",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BLOCKED_VECTOR_BASE_URL"] = "file:///blocked-vector"

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(PLUGIN_ROOT / "scripts" / "doctor.py"),
            "--json",
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(hermes_home),
        ],
        cwd=PLUGIN_ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["checks"]["config_load"] == {"ok": True, "errors": []}
    assert report["checks"]["endpoint_policy"] == {
        "ok": False,
        "checked": 2,
        "invalid": 1,
    }
    surface = next(
        item
        for item in report["runtime"]["endpoint_policy"]["surfaces"]
        if item["surface"] == "vector.embedder"
    )
    assert surface == {
        "surface": "vector.embedder",
        "enabled": True,
        "ok": False,
        "endpoint": "file://missing-host",
        "error": "unsafe endpoint scheme file; only HTTP(S) is supported",
    }


def test_endpoint_policy_report_skips_disabled_or_local_surfaces() -> None:
    payload, check, recommendations = endpoint_policy_report(
        {
            "capture_llm": {"enabled": False, "base_url": "file:///disabled"},
            "journal": {
                "enabled": True,
                "extractor": "heuristic",
                "endpoint": "file:///unused",
            },
            "reflection": {
                "enabled": True,
                "provider": "",
                "model": "",
                "base_url": "file:///unconfigured",
            },
            "vector": {
                "enabled": True,
                "embedder": {"provider": "local-hash"},
                "fallback_embedder": {"provider": "sentence-transformers"},
            },
        }
    )

    assert payload == {"surfaces": []}
    assert check == {"ok": True, "checked": 0, "invalid": 0}
    assert recommendations == []


def test_doctor_cli_fails_endpoint_policy_for_unsafe_capture_config(tmp_path: Path) -> None:
    hermes_home = tmp_path / "home"
    config_dir = hermes_home / "scope-recall"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "capture_llm": {
                    "enabled": True,
                    "base_url": "file:///blocked-provider?api_key=must-not-appear",
                },
                "journal": {"enabled": False},
                "vector": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            str(PLUGIN_ROOT / "scripts" / "doctor.py"),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(hermes_home),
        ],
        cwd=PLUGIN_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(proc.stdout)
    serialized = json.dumps(report, ensure_ascii=False)

    assert proc.returncode == 1
    assert report["checks"]["endpoint_policy"] == {
        "ok": False,
        "checked": 1,
        "invalid": 1,
    }
    assert report["runtime"]["endpoint_policy"]["surfaces"][0]["surface"] == "capture_llm"
    assert "must-not-appear" not in serialized
    assert "api_key" not in serialized
