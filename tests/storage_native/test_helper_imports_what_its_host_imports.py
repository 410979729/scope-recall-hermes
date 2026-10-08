"""The native helper imports its dependencies from where its host does, and one that cannot start says why (#176).

Hermes Desktop's package manager boots a bundled CPython and puts the environment it built on PYTHONPATH, so no
environment owns the interpreter.  The helper runs isolated (``-I``), which drops PYTHONPATH: it died at
``import jsonschema`` before its first answer, every embed and every vector search failed as ``worker_failed``, and
its stderr went nowhere.  The host here is an environment with no packages of its own, given this suite's
site-packages on PYTHONPATH and this checkout as a directory plugin, so the interpreter hint of #139 resolves
nothing.  LanceDB is not needed: the import the helper died on is ``jsonschema``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import scope_recall
from scope_recall.vector import lance_native, process_store

_IGNORED_FROM_ENVIRONMENT = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}
_ABSENT = "scope_recall_176_absent_module"
_windows_helper = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the helper process is the Windows vector store (vector/store.py)",
)
_HELPER_DRIVER = """\
import importlib.util, json, sys, types
from pathlib import Path

package = types.ModuleType("scope_recall")
package.__path__ = [sys.argv[1]]
sys.modules["scope_recall"] = package
from scope_recall.vector.lance_native import native_import_is_safe
from scope_recall.vector.process_store import ProcessLanceVectorStore

answer = {"expected": all(importlib.util.find_spec(name) is not None for name in ("lancedb", "pyarrow"))}
store = ProcessLanceVectorStore(Path(sys.argv[2]), table_name="memories", dimensions=2)
try:
    answer["available"] = store.is_available()
except RuntimeError as exc:
    answer["error"] = str(exc).split(";")[0]
finally:
    store.close()
answer["rehearsal"] = native_import_is_safe()
print(json.dumps(answer))
"""
_START_DRIVER = """\
import json, sys, time, types

package = types.ModuleType("scope_recall")
package.__path__ = [sys.argv[1]]
sys.modules["scope_recall"] = package
from scope_recall.runtime.instance import _helper_start_failure
from scope_recall.vector import lance_native

lance_native._HELPER_DEPENDENCIES = ()  # the launch #176 met: nothing of the host's path handed over
print(json.dumps({"line": _helper_start_failure(time.monotonic() + 60.0)}))
"""


def _environment(**extra: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key.upper() not in _IGNORED_FROM_ENVIRONMENT}
    environment.update(extra)
    return environment


def _site_holding(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    assert spec is not None and spec.submodule_search_locations, name
    return Path(next(iter(spec.submodule_search_locations))).resolve().parent


def _run(python: Path, driver: str, work: Path, *arguments: str) -> dict:
    script = work / "driver.py"
    script.write_text(driver, encoding="utf-8")
    done = subprocess.run(
        [str(python), "-B", str(script), str(Path(next(iter(scope_recall.__path__))).resolve()), *arguments],
        env=_environment(PYTHONPATH=str(_site_holding("jsonschema"))),
        cwd=str(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert done.returncode == 0, f"driver failed:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def empty_host(tmp_path_factory) -> Path:
    """An interpreter with no packages of its own: whatever it imports beyond the standard library comes from
    PYTHONPATH.  CI installs this suite's packages into its base interpreter, so the base cannot play this part."""
    root = tmp_path_factory.mktemp("host") / "env"
    made = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], capture_output=True, timeout=180)
    assert made.returncode == 0, made.stderr
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    alone = subprocess.run(
        [str(python), "-I", "-c", "import jsonschema"], env=_environment(), capture_output=True, timeout=60
    )
    assert alone.returncode != 0, "an environment made without packages has jsonschema"
    return python


def _helper_answers(answer: dict) -> None:
    assert "error" not in answer, f"the helper could not start where its host could: {answer}"
    assert answer["available"] == answer["expected"], answer
    assert answer["rehearsal"] == answer["available"], f"the rehearsal and the helper disagree: {answer}"


def test_a_host_given_its_packages_on_pythonpath_hands_them_to_the_helper(empty_host, tmp_path) -> None:
    _helper_answers(_run(empty_host, _HELPER_DRIVER, tmp_path, str(tmp_path / "lancedb")))


def test_the_same_host_on_the_base_interpreter(tmp_path) -> None:
    """Closer to Hermes Desktop, which boots a bare interpreter; possible only where the base has no jsonschema."""
    base = next(
        (
            candidate
            for candidate in (Path(sys.base_prefix) / "python.exe", Path(sys.base_prefix) / "bin" / "python3")
            if candidate.is_file()
        ),
        None,
    )
    if base is None:
        pytest.skip(f"no interpreter beside sys.base_prefix {sys.base_prefix!r}")
    alone = subprocess.run(
        [str(base), "-I", "-c", "import jsonschema"], env=_environment(), capture_output=True, timeout=60
    )
    if alone.returncode == 0:
        pytest.skip("this base interpreter has jsonschema itself")
    _helper_answers(_run(base, _HELPER_DRIVER, tmp_path, str(tmp_path / "lancedb")))


def test_a_helper_handed_nothing_is_said_to_miss_jsonschema(empty_host, tmp_path) -> None:
    """The start-up run once more names the module the helper died on in #176, and nothing longer."""
    line = _run(empty_host, _START_DRIVER, tmp_path)["line"]

    assert line == "ModuleNotFoundError: No module named 'jsonschema'"


def _failing(*_arguments: str) -> list[str]:
    return [sys.executable, "-I", "-B", "-c", f"import {_ABSENT}"]


@_windows_helper
def test_a_pass_whose_helper_cannot_start_names_the_module(tmp_path, monkeypatch) -> None:
    """The gap names the fault and ``worker_error``, which the doctor shows, the module."""
    from io import StringIO

    import scope_recall.core.worker as core_worker
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.runtime import worker_entry
    from test_runtime_worker_entry import _binding, _config_payload, _write_config

    monkeypatch.setattr(process_store, "_spare", None)
    monkeypatch.setattr(process_store, "_worker_command", _failing)
    monkeypatch.setattr(lance_native, "helper_command", _failing)
    monkeypatch.setattr(
        core_worker, "drain_worker", lambda *args, **kwargs: core_worker.WorkerReceipt(0, 0, 0, 0, 0, 0, 0, True, ())
    )
    binding = _binding(tmp_path / "data")
    MemoryCore(CoreConfig(binding)).initialize()
    payload = _config_payload(
        binding,
        vector={
            "backend": "lancedb",
            "storage_dir": str(tmp_path / "vectors"),
            "table_name": "TEST-176",
            "dimensions": 2,
            "test_injection_override": True,
        },
    )
    output = StringIO()
    assert worker_entry.run_worker(_write_config(tmp_path / "worker.json", payload), output=output) == 0
    receipt = json.loads(output.getvalue())
    status = json.loads((binding.data_directory / "runtime-worker-status.json").read_text(encoding="utf-8"))

    assert "vector_unavailable:RuntimeError:worker_failed" in receipt["capability_gaps"], receipt
    assert status["worker_error"] == f"vector helper: ModuleNotFoundError: No module named '{_ABSENT}'", status


class _Embedding:
    def embed_query(self, text: str, *, remaining_seconds: float):
        return (1.0, 0.0)

    def embed_source(self, source, *, remaining_seconds: float):
        return (1.0, 0.0)


@_windows_helper
def test_a_recall_never_runs_the_helper_s_start_up(tmp_path, monkeypatch) -> None:
    """A recall that meets a helper that cannot start reports the gap and starts nothing more: the start-up is run
    again by the drain, off the request path that carries memory text and off any prompt's time."""
    from scope_recall.contracts import InstanceBinding
    from scope_recall.runtime import instance as instance_module
    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
    from scope_recall.runtime.instance import (
        RuntimeInstanceConfig,
        VectorRuntimeConfig,
        build_runtime_instance,
        default_vector_factory,
    )

    replays: list[float] = []
    monkeypatch.setattr(lance_native, "helper_start_failure", lambda timeout: replays.append(timeout))
    # runtime.instance takes the probe at import.
    monkeypatch.setattr(instance_module, "helper_start_failure", lambda timeout: replays.append(timeout))
    monkeypatch.setattr(process_store, "_spare", None)
    monkeypatch.setattr(process_store, "_worker_command", _failing)
    binding = InstanceBinding(
        "TEST-176-agent", "TEST-176-installation", tmp_path / "truth", frozenset({"TEST-scope"}), True
    )
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="TEST-176-session",
        allowed_scope_ids=binding.scope_ids,
        request_seconds=45.0,
        drain_seconds=120.0,
        max_items=32,
        lease_seconds=60.0,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping({"external_embedding": False, "external_consolidation": False}),
        vector=VectorRuntimeConfig(
            backend="lancedb",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-176",
            dimensions=2,
            test_injection_override=True,
        ),
    )
    instance = build_runtime_instance(config, vector_factory=default_vector_factory)
    try:
        instance.core.initialize()
        instance.auxiliary = replace(instance.auxiliary, query_embedding=_Embedding(), source_embedding=_Embedding())
        (tmp_path / "vectors" / "lancedb" / "TEST-176.lance").mkdir(parents=True)
        result = instance.recall(
            {
                "protocol_version": "1.1",
                "request_id": "TEST-176-recall",
                "query": "TEST 只有向量能找到的问题",
                "mode": "current",
                "max_items": 6,
                "budget_tokens": 1200,
            }
        )
        recalled = list(replays)
        drained = instance._open_vector_for_drain(time.monotonic() + 60.0, 60.0)
    finally:
        instance.close()

    assert "vector_error:RuntimeError:worker_failed" in result.gaps, result.gaps
    assert recalled == [], "a recall ran the helper's start-up again"
    # The same dead helper met by the drain is replayed once: the hook above is the one that would have been called.
    assert drained == ("vector_unavailable:RuntimeError:worker_failed",) and len(replays) == 1, (drained, replays)
