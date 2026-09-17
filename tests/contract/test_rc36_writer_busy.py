"""A pass that finds the truth writer held is busy, and a busy pass is retried after a pause.

On 2026-09-17, while a maintenance command paged epsilon's embedding queue, a worker pass raised
TruthWriterBusyError. The pass exited as failed, and a failed pass stops the supervisor until the next
autostart wake five minutes later. Nothing had failed: another writer held the database.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
import json
import sqlite3

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.writer_lease import TruthWriterBusyError
from scope_recall.runtime import worker_entry
from scope_recall.runtime.scheduling import BUSY_BACKOFF_SECONDS, SupervisorControl, supervise
from tests.contract.test_finite_supervisor import NOW, fixture, queue
from tests.contract.test_runtime_worker_entry import _binding, _config_payload, _write_config


@pytest.mark.parametrize("error", [TruthWriterBusyError(),
                                   sqlite3.OperationalError("database is locked")])
def test_a_pass_that_finds_the_writer_held_is_busy_not_failed(tmp_path, monkeypatch, error):
    binding = _binding(tmp_path / "data")
    MemoryCore(CoreConfig(binding)).initialize()
    config_path = _write_config(tmp_path / "worker.json", _config_payload(binding))

    def held(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(worker_entry, "_drain_once", held)
    output = StringIO()

    assert worker_entry.run_worker(config_path, output=output) == 75
    payload = json.loads(output.getvalue())
    assert payload["status"] == "busy"
    assert payload["capability_gaps"] == ["worker_writer_busy"]


def test_a_busy_pass_is_retried_after_a_pause_not_at_once(tmp_path):
    core, cfg, path = fixture(tmp_path)
    queue(core, cfg, ref="TEST-now")
    elapsed = [0.0]
    calls = []

    def drain(_remaining):
        calls.append(elapsed[0])
        if len(calls) == 1:
            return 75, {"status": "busy"}
        with core.storage.write(cfg.context()) as tx:
            done = tx._check(write=True).execute("UPDATE work_items SET state='done' WHERE state='pending'").rowcount
        return 0, {"completed": done}

    assert supervise(path, drain, clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                     utc_now=lambda: NOW + timedelta(seconds=elapsed[0])) == 0

    # The third drain is the usual follow-up after progress, one worker interval later.
    assert calls == [0.0, BUSY_BACKOFF_SECONDS, BUSY_BACKOFF_SECONDS + cfg.worker_min_interval_seconds]
    assert SupervisorControl(cfg).read()["state"] == "idle"
