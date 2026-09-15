"""Packaging tests for the bounded v11 clean wheel."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = REPO_ROOT / "packaging" / "v11-module-allowlist.json"
_SUBPROCESS_TIMEOUT_SECONDS = 60
_CREATE_FLAGS = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _load_allowlist() -> dict:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def _expected_wheel_members(allowlist: dict) -> set[str]:
    members = {f"scope_recall/{path}" for path in allowlist["python_modules"]}
    members.update(f"scope_recall/{path}" for path in allowlist["package_data"])
    return members


def _build_wheel(dist_dir: Path) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    uv = (
        os.environ.get("SCOPE_RECALL_UV")
        or shutil.which("uv")
        or r"F:\Agents\runtime\windows\hermes-tianji\bin\uv.exe"
    )
    completed = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=_CREATE_FLAGS,
    )
    if completed.returncode != 0:
        pytest.fail(
            "wheel build failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    wheels = sorted(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _outside_repo_cwd() -> Path:
    outside = Path(tempfile.gettempdir()) / "scope-recall-packaging-probe"
    outside.mkdir(parents=True, exist_ok=True)
    resolved = outside.resolve()
    assert not resolved.is_relative_to(REPO_ROOT.resolve())
    return resolved


def _run_clean_child(
    python: Path,
    *,
    cwd: Path,
    args: list[str],
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(python), "-I", "-B", *args],
        cwd=str(cwd),
        input=input,
        capture_output=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=_CREATE_FLAGS,
    )


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_wheel(tmp_path_factory.mktemp("wheel-build"))


@pytest.fixture(scope="module")
def installed_venv(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    venv_dir = tmp_path_factory.mktemp("clean-venv")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=_CREATE_FLAGS,
    )
    python = _venv_python(venv_dir)
    install_env = dict(os.environ)
    # The wrapper exposes test helper directories on PYTHONPATH for
    # --import-mode=importlib compatibility.  pip must not see the source
    # tree's generated egg-info, or it can mistake the wheel for an already
    # installed distribution and skip copying scope_recall into this clean
    # venv.  The child probes below stay -I and therefore remain source-free.
    install_env.pop("PYTHONPATH", None)
    install_env.pop("PYTHONHOME", None)
    install_env.pop("VIRTUAL_ENV", None)
    subprocess.run(
        [str(python), "-m", "pip", "install", str(built_wheel)],
        check=True,
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=_CREATE_FLAGS,
        env=install_env,
    )
    return {
        "python": python,
        "venv_dir": venv_dir,
        "wheel": built_wheel,
        "outside_cwd": _outside_repo_cwd(),
    }


def test_clean_v11_wheel_exact_allowlist(built_wheel: Path) -> None:
    allowlist = _load_allowlist()
    expected = _expected_wheel_members(allowlist)

    with zipfile.ZipFile(built_wheel) as archive:
        members = set(archive.namelist())

    dist_info = {name for name in members if ".dist-info/" in name}
    package_members = {name for name in members if name.startswith("scope_recall/")}

    assert package_members == expected
    assert dist_info, "wheel must include dist-info metadata"
    assert members == package_members | dist_info

    metadata_name = next(name for name in dist_info if name.endswith(".dist-info/METADATA"))
    with zipfile.ZipFile(built_wheel) as archive:
        metadata = archive.read(metadata_name).decode("utf-8")
    assert "Name: hermes-scope-recall" in metadata
    assert f"Version: {allowlist['package_version']}" in metadata


def test_clean_v11_wheel_has_no_private_or_legacy_artifacts(built_wheel: Path) -> None:
    allowlist = _load_allowlist()

    with zipfile.ZipFile(built_wheel) as archive:
        members = archive.namelist()

    for needle in allowlist["forbidden_wheel_substrings"]:
        hits = [name for name in members if needle in name.replace("\\", "/")]
        assert not hits, f"forbidden wheel artifact for {needle!r}: {hits}"


def test_clean_v11_wheel_entrypoint_and_register_delegate(installed_venv: dict[str, Path]) -> None:
    probe = _run_clean_child(
        installed_venv["python"],
        cwd=installed_venv["outside_cwd"],
        args=[
            "-c",
            (
                "import importlib.metadata, json, scope_recall; "
                "from scope_recall.distribution.hermes import register as wrapper_register; "
                "dist = importlib.metadata.distribution('hermes-scope-recall'); "
                "scripts = {entry.name: entry.value for entry in dist.entry_points if entry.group == 'console_scripts'}; "
                "providers = {entry.name: entry.value for entry in dist.entry_points if entry.group == 'hermes_agent.memory_providers'}; "
                "loaded = next(entry.load() for entry in dist.entry_points if entry.group == 'hermes_agent.memory_providers' and entry.name == 'scope-recall'); "
                "print(json.dumps({"
                "'console_scripts': scripts, "
                "'memory_providers': providers, "
                "'loaded_is_wrapper_register': loaded is wrapper_register, "
                "'scope_recall_all': list(scope_recall.__all__), "
                "'register_module': scope_recall.register.__module__"
                "}))"
            ),
        ],
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", errors="replace")
    receipt = json.loads(probe.stdout.decode("utf-8"))
    assert receipt["console_scripts"] == {
        "hermes-scope-recall": "scope_recall.maintenance.cli:main",
        "scope-recall": "scope_recall.maintenance.cli:main",
    }
    assert receipt["memory_providers"] == {
        "scope-recall": "scope_recall.distribution.hermes:register",
    }
    assert receipt["loaded_is_wrapper_register"] is True
    assert receipt["scope_recall_all"] == ["register"]
    assert receipt["register_module"] == "scope_recall"


def test_clean_v11_wheel_installed_runtime_recall(installed_venv: dict[str, Path], tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime-data"
    probe = _run_clean_child(
        installed_venv["python"],
        cwd=installed_venv["outside_cwd"],
        args=[
            "-c",
            (
                "import json, sys, time; "
                "from pathlib import Path; "
                "from scope_recall.contracts import InstanceBinding, TrustedContext; "
                "from scope_recall.core.composition import CoreConfig, MemoryCore; "
                "data_dir = Path(sys.argv[1]); "
                "binding = InstanceBinding('TEST-wheel-agent', 'TEST-wheel-installation', data_dir, frozenset({'TEST-scope'}), True); "
                "context = TrustedContext(binding, 'TEST-wheel-session', frozenset({'TEST-scope'}), 'human_direct'); "
                "from types import SimpleNamespace; "
                "clock = SimpleNamespace(utc_now=lambda: '2026-09-06T12:00:00Z', monotonic=time.monotonic); "
                "event = {"
                "'protocol_version': '1.1', "
                "'source_event_key': 'TEST-wheel/message-1', "
                "'source_revision': 1, "
                "'origin': 'human_direct', "
                "'role': 'user', "
                "'content': 'TEST packaging wheel stores durable source identity.', "
                "'occurred_at': '2026-09-06T12:00:00Z', "
                "'recorded_at': '2026-09-06T12:00:00Z', "
                "'time_precision': 'instant', "
                "'capture_state': 'complete', "
                "'evidence_refs': [], "
                "'dataset_id': 'SYNTHETIC_TEST_ONLY'"
                "}; "
                "core = MemoryCore(CoreConfig(binding), clock=clock); "
                "core.initialize(); "
                "capture = core.record_event(context, event, scope_id='TEST-scope', remaining_seconds=5); "
                "assert capture.disposition == 'inserted' and capture.event_refs, 'capture failed'; "
                "ref = capture.event_refs[0].ref; "
                "reopened = MemoryCore(CoreConfig(binding), clock=clock); "
                "reopened.initialize(); "
                "request = {"
                "'protocol_version': '1.1', "
                "'request_id': 'TEST-wheel-recall', "
                "'query': 'packaging wheel durable source identity', "
                "'mode': 'auto', "
                "'max_items': 6, "
                "'budget_tokens': 1200"
                "}; "
                "packet = reopened.recall_packet(context, request, deadline_seconds=5); "
                "prepared = reopened.prepare_recall_render(context, packet); "
                "source = reopened.source(context, ref, 1); "
                "print(json.dumps({"
                "'persisted_ref': ref, "
                "'source_event_key': source.event['source_event_key'] if source else None, "
                "'source_content': source.event['content'] if source else None, "
                "'packet_status': packet['status'], "
                "'packet_item_refs': [item['ref'] for item in packet.get('items', [])], "
                "'render_present': prepared.context is not None, "
                "'render_ref': prepared.render_ref"
                "}))"
            ),
            str(data_dir),
        ],
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", errors="replace")
    receipt = json.loads(probe.stdout.decode("utf-8"))
    assert receipt["source_event_key"] == "TEST-wheel/message-1"
    assert receipt["source_content"] == "TEST packaging wheel stores durable source identity."
    assert receipt["packet_status"] in {"ok", "partial"}
    assert receipt["persisted_ref"] in receipt["packet_item_refs"]
    assert receipt["render_present"] is True
    assert receipt["render_ref"] is not None


def test_clean_v11_wheel_installed_http_worker_protocol(installed_venv: dict[str, Path]) -> None:
    resolve = _run_clean_child(
        installed_venv["python"],
        cwd=installed_venv["outside_cwd"],
        args=[
            "-c",
            (
                "import json, sys; "
                "from pathlib import Path; "
                "import scope_recall.runtime; "
                "root = Path(scope_recall.runtime.__file__).resolve().parent; "
                "worker = root / '_http_worker.py'; "
                "assert worker.is_file(); "
                "assert root.is_relative_to(Path(sys.prefix).resolve()); "
                "print(json.dumps({'http_worker': str(worker)}))"
            ),
        ],
    )
    assert resolve.returncode == 0, resolve.stderr.decode("utf-8", errors="replace")
    worker_path = Path(json.loads(resolve.stdout.decode("utf-8"))["http_worker"])
    assert worker_path.is_file()
    assert not worker_path.resolve().is_relative_to(REPO_ROOT.resolve())

    malformed = json.dumps(
        {
            "url": "http://synthetic.invalid/not-https",
            "body_b64": "",
            "headers": {},
            "timeout_seconds": 1,
            "max_response_bytes": 1024,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    completed = subprocess.run(
        [str(installed_venv["python"]), "-I", "-B", str(worker_path)],
        cwd=str(installed_venv["outside_cwd"]),
        input=malformed,
        capture_output=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=_CREATE_FLAGS,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout.decode("utf-8"))
    assert result["ok"] is False
    assert result["error"] == "endpoint_invalid"
    assert completed.stderr == b""


def test_clean_v11_wheel_installed_worker_bootstrap_gate(installed_venv: dict[str, Path], tmp_path: Path) -> None:
    resolve = _run_clean_child(
        installed_venv["python"],
        cwd=installed_venv["outside_cwd"],
        args=[
            "-c",
            (
                "import json, sys; "
                "from pathlib import Path; "
                "import scope_recall.runtime; "
                "root = Path(scope_recall.runtime.__file__).resolve().parent; "
                "bootstrap = root / '_worker_bootstrap.py'; "
                "assert bootstrap.is_file(); "
                "assert root.is_relative_to(Path(sys.prefix).resolve()); "
                "print(json.dumps({'worker_bootstrap': str(bootstrap)}))"
            ),
        ],
    )
    assert resolve.returncode == 0, resolve.stderr.decode("utf-8", errors="replace")
    bootstrap_path = Path(json.loads(resolve.stdout.decode("utf-8"))["worker_bootstrap"])
    assert bootstrap_path.is_file()
    assert not bootstrap_path.resolve().is_relative_to(REPO_ROOT.resolve())

    missing_config = tmp_path / "missing-worker-config.json"
    assert not missing_config.exists()
    completed = subprocess.run(
        [str(installed_venv["python"]), "-I", "-B", str(bootstrap_path), str(missing_config)],
        cwd=str(installed_venv["outside_cwd"]),
        input=b"",
        capture_output=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        creationflags=_CREATE_FLAGS,
    )
    assert completed.returncode == 125
    assert completed.stdout == completed.stderr == b""


def test_clean_v11_wheel_hermes_plugin_yaml_semver(built_wheel: Path) -> None:
    allowlist = _load_allowlist()
    manifest_version = allowlist["package_version"].replace(".dev", "-dev.", 1)

    with zipfile.ZipFile(built_wheel) as archive:
        plugin_yaml = archive.read("scope_recall/adapters/hermes/plugin.yaml").decode("utf-8")

    assert re.search(rf"^version:\s*{re.escape(manifest_version)}\s*$", plugin_yaml, re.MULTILINE)
