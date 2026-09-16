"""Small P13 fault probes; no model/API and no production paths."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.file_lock import advisory_file_lock
from tests.v11_support import context, source_event


def test_p13_bad_schema_is_rejected_before_durable_write(tmp_path):
    ctx = context(tmp_path / "bad-schema")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    bad = source_event(protocol_version="0.0", source_event_key="TEST-P13/bad-schema")
    with pytest.raises(ContractError):
        core.record_event(ctx, bad, scope_id="TEST-scope", remaining_seconds=1)


def test_p13_hook_origin_mismatch_is_rejected(tmp_path):
    ctx = context(tmp_path / "hook-trust")
    core = MemoryCore(CoreConfig(ctx.binding))
    core.initialize()
    event = source_event(source_event_key="TEST-P13/hook-trust", origin="human_direct")
    forged = ctx.__class__(ctx.binding, ctx.session_id, ctx.allowed_scope_ids, "assistant_visible")
    with pytest.raises(ContractError):
        core.record_event(forged, event, scope_id="TEST-scope", remaining_seconds=1)


def test_p13_distinct_thread_lock_timeout_is_bounded(tmp_path):
    lock_path = tmp_path / "writer.lock"
    held = Event()
    release = Event()

    def holder():
        with advisory_file_lock(lock_path, timeout_seconds=2):
            held.set()
            release.wait(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(holder)
        assert held.wait(2)
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            with advisory_file_lock(lock_path, timeout_seconds=0.05):
                pass
        elapsed = time.perf_counter() - started
        release.set()
        future.result(timeout=2)
    assert elapsed < 0.5


def test_p13_data_directory_file_rejects_write_setup(tmp_path):
    blocked = tmp_path / "data-is-a-file"
    blocked.write_text("TEST disk boundary", encoding="utf-8")
    ctx = context(blocked)
    core = MemoryCore(CoreConfig(ctx.binding))
    with pytest.raises((ContractError, OSError)):
        core.initialize()
