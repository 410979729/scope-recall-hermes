"""Cross-process serialization contract for physical vector companion writes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from scope_recall.vector_mutation_guard import advisory_file_lock


def _wait_for(path: Path, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for marker: {path}")


def test_advisory_file_lock_is_reentrant_in_one_thread(tmp_path: Path) -> None:
    lock_path = tmp_path / "vector-mutation.lock"
    with advisory_file_lock(lock_path):
        with advisory_file_lock(lock_path):
            assert lock_path.exists()


def test_advisory_file_lock_serializes_independent_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "vector-mutation.lock"
    attempted = tmp_path / "child-attempted"
    entered = tmp_path / "child-entered"
    child = """
from pathlib import Path
import sys
from scope_recall.vector_mutation_guard import advisory_file_lock
lock_path, attempted, entered = map(Path, sys.argv[1:])
attempted.write_text('attempted', encoding='utf-8')
with advisory_file_lock(lock_path):
    entered.write_text('entered', encoding='utf-8')
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + env.get("PYTHONPATH", "")

    with advisory_file_lock(lock_path):
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(lock_path), str(attempted), str(entered)],
            env=env,
        )
        _wait_for(attempted)
        time.sleep(0.1)
        assert not entered.exists()

    completed = process.wait(timeout=3)
    assert completed == 0
    assert entered.read_text(encoding="utf-8") == "entered"


class _SharedDbVectorProvider:
    """Minimal runtime surface for cross-provider serialization tests."""

    def __init__(self, storage_dir: Path) -> None:
        self._storage_dir = storage_dir
        self._db_path = storage_dir / "memory.sqlite3"
        self._vector_generation_id = "generation-a"
        self._vector_store = object()
        self._embedder = object()
        self._vector_config: dict[str, object] = {}
        self._scope_id = "scope-a"
        self._lock = threading.RLock()
        self._connection = object()

    def _require_conn(self):
        return self._connection


def _run_two_threads(target_a, target_b) -> None:
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def run(target) -> None:
        try:
            start.wait(timeout=2)
            target()
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(target_a,), daemon=True),
        threading.Thread(target=run, args=(target_b,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not errors


class _OverlapProbe:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum = 0

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.12)
        with self._lock:
            self.active -= 1


def test_replay_serializes_truth_outbox_sequence_for_shared_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scope_recall.vector_runtime as vector_runtime

    probe = _OverlapProbe()
    providers = [_SharedDbVectorProvider(tmp_path) for _ in range(2)]

    def fake_replay(*args, **kwargs):
        del args, kwargs
        probe.enter()
        return {"claimed": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(vector_runtime, "replay_committed_vector_events", fake_replay)
    _run_two_threads(
        lambda: vector_runtime.replay_vector_outbox(providers[0]),
        lambda: vector_runtime.replay_vector_outbox(providers[1]),
    )

    assert probe.maximum == 1


def test_bounded_reconciliation_serializes_schema_bookkeeping_for_shared_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scope_recall.vector_runtime as vector_runtime

    probe = _OverlapProbe()
    providers = [_SharedDbVectorProvider(tmp_path) for _ in range(2)]

    monkeypatch.setattr(
        vector_runtime,
        "probe_truth_database_connection",
        lambda _conn: {"ok": True, "status": "ok"},
    )
    monkeypatch.setattr(
        vector_runtime,
        "replay_vector_outbox",
        lambda *args, **kwargs: {
            "claimed": 0,
            "completed": 0,
            "failed": 0,
        },
    )

    def fake_backlog(*args, **kwargs):
        del args, kwargs
        probe.enter()
        return {"replayable": 1, "dead_letter": 0}

    monkeypatch.setattr(vector_runtime, "vector_outbox_backlog_status", fake_backlog)
    monkeypatch.setattr(
        vector_runtime,
        "vector_reconciliation_state",
        lambda *args, **kwargs: {},
    )
    _run_two_threads(
        lambda: vector_runtime.run_bounded_vector_reconciliation(providers[0]),
        lambda: vector_runtime.run_bounded_vector_reconciliation(providers[1]),
    )

    assert probe.maximum == 1


def test_unavailable_vector_runtime_does_not_create_guard_file(tmp_path: Path) -> None:
    import scope_recall.vector_runtime as vector_runtime

    provider = _SharedDbVectorProvider(tmp_path)
    provider._vector_generation_id = ""

    assert vector_runtime.replay_vector_outbox(provider) == {
        "claimed": 0,
        "completed": 0,
        "failed": 0,
    }
    assert vector_runtime.run_bounded_vector_reconciliation(provider)["status"] == "unavailable"
    assert not (tmp_path / ".vector-mutation.lock").exists()
