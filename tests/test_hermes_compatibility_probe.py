"""Fail-closed contracts for isolated Hermes source compatibility probes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "probe.hermes_compatibility.py"
SPEC = importlib.util.spec_from_file_location("hermes_compatibility_probe", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

BOUND_IDENTITY = {
    "commit": "1" * 40,
    "tree": "2" * 40,
    "clean": True,
}


def _source(path: Path, version: str) -> Path:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-agent"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    return path


def _bind_clean_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_git_identity", lambda _path: dict(BOUND_IDENTITY))


def test_version_mismatch_is_unknown_without_launching_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    hermes = _source(tmp_path / "hermes", "0.20.5")
    _bind_clean_sources(monkeypatch)

    def fail_run(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("a version-mismatched source must not launch")

    monkeypatch.setattr(probe, "_run_probe_command", fail_run)
    receipt = probe.build_probe_receipt(
        candidate_source=candidate,
        hermes_source=hermes,
        expected_hermes_version="0.20.6",
        active_hermes_home=tmp_path / "active",
    )

    assert receipt["result"] == "unknown"
    assert receipt["reason"] == "hermes_version_mismatch"
    assert receipt["support_matrix_changed"] is False
    assert receipt["active_instance_touched"] is False
    assert receipt["stages"] == {
        "candidate_install": "not_run",
        "provider_load": "not_run",
    }


def test_unbound_source_identity_is_unknown_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    hermes = _source(tmp_path / "hermes", "0.20.6")
    identities = iter(
        (
            {"commit": "unbound", "tree": "unbound", "clean": "unknown"},
            dict(BOUND_IDENTITY),
        )
    )
    monkeypatch.setattr(probe, "_git_identity", lambda _path: next(identities))
    monkeypatch.setattr(
        probe,
        "_run_probe_command",
        lambda *_args, **_kwargs: pytest.fail("unbound source must not launch"),
    )

    receipt = probe.build_probe_receipt(
        candidate_source=candidate,
        hermes_source=hermes,
        expected_hermes_version="0.20.6",
        active_hermes_home=tmp_path / "active",
    )

    assert receipt["result"] == "unknown"
    assert receipt["reason"] == "candidate_source_unbound"


@pytest.mark.parametrize(
    ("load_code", "load_classification", "expected"),
    (
        (0, "compatible", "compatible"),
        (7, "incompatible", "incompatible"),
        (9, "compatible", "unknown"),
    ),
)
def test_probe_classification_is_bound_to_process_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load_code: int,
    load_classification: str,
    expected: str,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    hermes = _source(tmp_path / "hermes", "0.20.6")
    active = tmp_path / "active"
    _bind_clean_sources(monkeypatch)
    calls: list[tuple[Sequence[str], Path, Mapping[str, str]]] = []

    def fake_run(
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> tuple[int, str]:
        calls.append((command, cwd, dict(env)))
        if len(calls) == 1:
            plugin_dir = Path(env["SCOPE_RECALL_PLUGIN_DIR"])
            assert plugin_dir.parent.is_dir()
            assert not plugin_dir.exists()
            return 0, json.dumps(
                {"stage": "candidate_install", "classification": "passed"}
            )
        return load_code, json.dumps(
            {"stage": "provider_contract", "classification": load_classification}
        )

    monkeypatch.setattr(probe, "_run_probe_command", fake_run)
    receipt = probe.build_probe_receipt(
        candidate_source=candidate,
        hermes_source=hermes,
        expected_hermes_version="0.20.6",
        active_hermes_home=active,
    )

    assert receipt["result"] == expected
    assert receipt["support_matrix_changed"] is False
    assert receipt["active_instance_touched"] is False
    assert receipt["candidate_source"] == BOUND_IDENTITY
    assert receipt["hermes_source"] == BOUND_IDENTITY
    assert len(calls) == 2
    for _command, boundary, env in calls:
        for name in (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "PIP_CACHE_DIR",
            "HERMES_HOME",
        ):
            assert Path(env[name]).is_relative_to(boundary)
            assert not Path(env[name]).is_relative_to(active)
    assert str(tmp_path) not in json.dumps(receipt, sort_keys=True)


def test_active_hermes_source_overlap_is_refused(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    active = tmp_path / "active"
    hermes = _source(active / "source", "0.20.6")

    with pytest.raises(
        probe.HermesCompatibilityProbeError,
        match="ACTIVE_HERMES_SOURCE_REFUSED",
    ):
        probe.build_probe_receipt(
            candidate_source=candidate,
            hermes_source=hermes,
            expected_hermes_version="0.20.6",
            active_hermes_home=active,
        )


def test_receipt_writer_refuses_path_outside_candidate(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()

    with pytest.raises(
        probe.HermesCompatibilityProbeError,
        match="outside the candidate source",
    ):
        probe._write_ignored(root, tmp_path / "receipt.json", {"result": "unknown"})


def test_last_json_object_ignores_non_json_diagnostics() -> None:
    payload = probe._last_json_object(
        'diagnostic text\n{"classification":"compatible","stage":"complete"}\n'
    )

    assert payload == {"classification": "compatible", "stage": "complete"}


def test_probe_script_path_entrypoint_is_importable() -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout
    assert "--hermes-source" in result.stdout
