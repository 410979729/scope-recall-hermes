"""P1-04: bounded, recoverable capture intents under a blocked writer."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import scope_recall.capture as capture
import scope_recall.capture_outcomes as capture_outcomes
from scope_recall.capture_control import (
    CONTROL_QUEUE_MAXSIZE,
    new_write_control_queue,
    wake_writer,
)
from scope_recall.capture_intents import (
    DEFAULT_CAPTURE_QUEUE_CAPACITY,
    capture_intent_report,
    persist_capture_intent,
    queue_capacity,
    release_stale_processing,
    unconsumed_depth,
)
from scope_recall.capture_llm import Candidate
from scope_recall.capture_outcomes import RECEIPT_FILENAME, sidecar_path
from scope_recall.governance import ExtractionCandidate
from scope_recall.provider import ScopeRecallMemoryProvider
from scope_recall.sql_store import ensure_schema

_CONTENT = (
    "Durable capture intent %s must remain queued when the single writer "
    "or embedding backend is blocked."
)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


_TURN_TEXT = (
    "Durable capture intent turn text must remain queued when the single "
    "writer or embedding backend is blocked."
)


def _stub_provider(conn: sqlite3.Connection | None, *, capacity: int = 8) -> object:
    provider = type("IntentProvider", (), {})()
    provider._lock = threading.RLock()
    provider._write_queue = new_write_control_queue()
    provider._writer_thread = None
    provider._stop = threading.Event()
    provider._maintenance_stop = threading.Event()
    provider._shutdown_requested = threading.Event()
    provider._writer_lifecycle_lock = threading.RLock()
    provider._writer_failed_writes = 0
    provider._writer_reported_failures = 0
    provider._writer_last_error_type = ""
    provider._last_relation_rebuild_drain = 0.0
    provider._capture_queue_rejected = 0
    provider._capture_queue_deferred = 0
    provider._write_wakeup = threading.Event()
    provider._truth_writer_role = "owner"
    provider._session_id = "intent-session"
    provider._db_path = None
    provider._storage_dir = None
    provider._config = {
        "capture_queue_capacity": capacity,
        "relation_extraction_enabled": False,
        "min_capture_length": 20,
        "capture_llm": {"enabled": True, "min_user_chars": 20, "min_assistant_chars": 20},
        "per_turn_extraction": {"enabled": True},
        "capture_raw_user": True,
        "capture_assistant": True,
    }
    if conn is not None:
        provider._require_conn = lambda: conn
        provider._conn = conn
        try:
            for row in conn.execute("PRAGMA database_list").fetchall():
                if str(row[1]) == "main" and row[2]:
                    provider._db_path = Path(str(row[2]))
                    provider._storage_dir = Path(str(row[2])).parent
                    break
        except sqlite3.Error:
            pass
    return provider


def test_queue_capacity_is_explicit_and_clamped() -> None:
    assert queue_capacity(None) == DEFAULT_CAPTURE_QUEUE_CAPACITY
    assert queue_capacity({"capture_queue_capacity": 8}) == 8
    assert queue_capacity({"capture_queue_capacity": 1}) == 8
    assert queue_capacity({"capture_queue_capacity": 99_000}) == 4096
    assert queue_capacity(None) != CONTROL_QUEUE_MAXSIZE
    assert queue_capacity({"capture_queue_capacity": 8}) != CONTROL_QUEUE_MAXSIZE


def test_persist_beyond_capacity_returns_explicit_rejected(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    accepted = []
    rejected = []
    for index in range(20):
        result = persist_capture_intent(
            conn,
            content=_CONTENT % index,
            source="test",
            target="memory",
            session_id="cap",
            capacity=8,
        )
        if result["status"] == "accepted":
            accepted.append(result)
        elif result["status"] == "rejected":
            rejected.append(result)
        else:
            raise AssertionError(result)
    conn.commit()

    assert len(accepted) == 8
    assert len(rejected) == 12
    assert all(item["reason"] == "queue_full" for item in rejected)
    assert unconsumed_depth(conn) == 8
    report = capture_intent_report(conn, capacity=8)
    assert report["depth"] == 8
    assert report["capacity"] == 8
    assert report["rejected"] == 12
    assert report["deferred"] == 0
    assert report["oldest_age_seconds"] >= 0.0
    dumped = json.dumps(report)
    assert str(tmp_path) not in dumped
    assert "C:\\" not in dumped
    assert "/Users/" not in dumped
    conn.close()


def test_duplicate_unconsumed_intent_coalesces(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    first = persist_capture_intent(
        conn,
        content=_CONTENT % "same",
        source="test",
        target="memory",
        session_id="cap",
        capacity=8,
    )
    second = persist_capture_intent(
        conn,
        content=_CONTENT % "same",
        source="test",
        target="memory",
        session_id="cap",
        capacity=8,
    )
    conn.commit()
    assert first["status"] == "accepted"
    assert second == {
        "status": "coalesced",
        "reason": "duplicate_unconsumed",
        "intent_id": first["intent_id"],
        "depth": 1,
        "capacity": 8,
    }
    assert unconsumed_depth(conn) == 1
    conn.close()


def test_persist_defers_when_truth_lock_is_held(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    provider = _stub_provider(conn, capacity=8)
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with provider._lock:
            held.set()
            release.wait(timeout=2.0)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert held.wait(timeout=1.0)
    started = time.monotonic()
    result = capture.enqueue_store(
        provider,
        content=_CONTENT % "busy",
        source="test",
        target="memory",
        session_id="cap",
    )
    elapsed = time.monotonic() - started
    release.set()
    worker.join(timeout=2.0)

    assert result["status"] == "deferred"
    assert result["reason"] == "truth_writer_busy"
    assert elapsed < 1.0
    assert result.get("durable_accounted") is True
    report = capture.capture_queue_report(provider)
    assert report["deferred"] >= 1
    assert unconsumed_depth(conn) == 0
    conn.close()


def _live_provider(tmp_path: Path, *, capacity: int = 8):
    from plugins.memory import load_memory_provider

    config = tmp_path / "scope-recall" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "capture_queue_capacity": capacity,
                "relation_extraction_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_memory_provider("scope-recall")
    assert loaded is not None
    loaded.initialize(
        "capture-intent",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="intent-user",
        chat_id="intent-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )
    loaded._config["capture_queue_capacity"] = capacity
    return loaded


def _shutdown(provider, release: threading.Event | None = None) -> None:
    if release is not None:
        release.set()
    last_error: Exception | None = None
    for _ in range(3):
        try:
            provider.shutdown(timeout=2.0)
            return
        except RuntimeError as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error


def test_enqueue_far_beyond_capacity_under_blocked_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path, capacity=8)
    started = threading.Event()
    release = threading.Event()
    original = capture.store_now

    def blocked_store_now(provider_arg, **kwargs):
        started.set()
        assert release.wait(timeout=5.0)
        return original(provider_arg, **kwargs)

    monkeypatch.setattr(capture, "store_now", blocked_store_now)
    monkeypatch.setitem(
        provider._writer_thread._target.__globals__, "store_now", blocked_store_now
    )
    try:
        first = capture.enqueue_store(
            provider,
            content=_CONTENT % 0,
            source="test",
            target="memory",
            session_id=provider._session_id,
        )
        assert first["status"] == "accepted"
        assert started.wait(timeout=2.0)

        statuses: list[str] = []
        for index in range(1, 25):
            result = capture.enqueue_store(
                provider,
                content=_CONTENT % index,
                source="test",
                target="memory",
                session_id=provider._session_id,
            )
            assert result["status"] in {"accepted", "coalesced", "rejected", "deferred"}
            statuses.append(result["status"])

        report = capture.capture_queue_report(provider)
        assert report["capacity"] == 8
        assert report["capacity"] != CONTROL_QUEUE_MAXSIZE
        assert report["depth"] <= 8
        assert provider._write_queue.maxsize == CONTROL_QUEUE_MAXSIZE
        assert provider._write_queue.qsize() <= provider._write_queue.maxsize
        assert statuses.count("accepted") <= 7
        assert statuses.count("rejected") + statuses.count("deferred") >= 16
        assert "accepted" in statuses or first["status"] == "accepted"
        dumped = json.dumps(report)
        assert str(tmp_path) not in dumped
        assert str(provider._db_path) not in dumped
    finally:
        release.set()
        _shutdown(provider, release)


def test_bounded_shutdown_leaves_durable_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path, capacity=8)
    started = threading.Event()
    release = threading.Event()
    original = capture.store_now

    def blocked_store_now(provider_arg, **kwargs):
        started.set()
        assert release.wait(timeout=5.0)
        return original(provider_arg, **kwargs)

    monkeypatch.setattr(capture, "store_now", blocked_store_now)
    monkeypatch.setitem(
        provider._writer_thread._target.__globals__, "store_now", blocked_store_now
    )
    try:
        accepted = 0
        for index in range(4):
            result = capture.enqueue_store(
                provider,
                content=_CONTENT % f"shutdown-{index}",
                source="test",
                target="memory",
                session_id=provider._session_id,
            )
            if result["status"] == "accepted":
                accepted += 1
        assert accepted >= 1
        assert started.wait(timeout=2.0)

        started_at = time.monotonic()
        with pytest.raises(RuntimeError, match="did not acknowledge"):
            capture.shutdown_writer(provider, timeout=0.2)
        assert time.monotonic() - started_at < 1.5
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert capture.capture_queue_report(provider)["depth"] >= 1
    finally:
        release.set()
        _shutdown(provider, release)


def test_restart_replays_unconsumed_intents_idempotently(tmp_path: Path) -> None:
    from scope_recall.maintenance_ops import memory_db_path

    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}, "capture_queue_capacity": 16}),
        encoding="utf-8",
    )
    db_path = memory_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for index in range(3):
        result = persist_capture_intent(
            conn,
            content=_CONTENT % f"restart-{index}",
            source="test",
            target="memory",
            session_id="restart",
            capacity=16,
        )
        assert result["status"] == "accepted"
    for index in range(5):
        padding = persist_capture_intent(
            conn,
            content=_CONTENT % f"pad-{index}",
            source="test",
            target="memory",
            session_id="restart-pad",
            capacity=8,
        )
        assert padding["status"] == "accepted"
    rejected = persist_capture_intent(
        conn,
        content=_CONTENT % "restart-rejected",
        source="test",
        target="memory",
        session_id="restart-rejected",
        capacity=8,
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "queue_full"
    conn.commit()
    assert unconsumed_depth(conn) == 8
    conn.close()

    provider = _live_provider(tmp_path, capacity=16)
    try:
        assert provider.flush(timeout=3.0) is True
        with provider._lock:
            memories = provider._require_conn().execute(
                "SELECT COUNT(*) FROM memories WHERE session_id = ?",
                ("restart",),
            ).fetchone()[0]
            depth = unconsumed_depth(provider._require_conn())
        assert int(memories) == 3
        assert depth == 0
        report = capture.capture_queue_report(provider)
        assert report["rejected"] >= 1
        with provider._lock:
            rejected_rows = provider._require_conn().execute(
                "SELECT COUNT(*) FROM memories WHERE session_id = ?",
                ("restart-rejected",),
            ).fetchone()[0]
        assert int(rejected_rows) == 0

        with provider._lock:
            conn = provider._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            release_stale_processing(conn)
            replay = persist_capture_intent(
                conn,
                content=_CONTENT % "restart-0",
                source="test",
                target="memory",
                session_id="restart",
                capacity=16,
            )
            conn.commit()
        assert replay["status"] == "accepted"
        assert provider.flush(timeout=3.0) is True
        with provider._lock:
            memories_after = provider._require_conn().execute(
                "SELECT COUNT(*) FROM memories WHERE session_id = ?",
                ("restart",),
            ).fetchone()[0]
        assert int(memories_after) == 3
    finally:
        _shutdown(provider)


def test_store_now_can_skip_vector_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _live_provider(tmp_path, capacity=8)
    replayed = {"n": 0}

    def mark_replay(_provider, **_kwargs):
        replayed["n"] += 1
        return {"claimed": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(capture, "replay_vector_outbox", mark_replay)
    try:
        capture.store_now(
            provider,
            content=_CONTENT % "sync-replay",
            source="test",
            target="memory",
            session_id=provider._session_id,
            replay_vector=False,
        )
        assert replayed["n"] == 0
        capture.store_now(
            provider,
            content=_CONTENT % "sync-replay-on",
            source="test",
            target="memory",
            session_id=provider._session_id,
        )
        assert replayed["n"] == 1
    finally:
        _shutdown(provider)


def test_enqueue_persists_while_vector_replay_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path, capacity=8)
    entered = threading.Event()
    release = threading.Event()

    def blocked_replay(_provider, **_kwargs):
        entered.set()
        release.wait(timeout=5.0)
        return {"claimed": 0, "completed": 0, "failed": 0}

    monkeypatch.setattr(capture, "replay_vector_outbox", blocked_replay)
    worker = threading.Thread(
        target=lambda: capture.store_now(
            provider,
            content=_CONTENT % "replay-block",
            source="test",
            target="memory",
            session_id=provider._session_id,
        )
    )
    try:
        worker.start()
        assert entered.wait(timeout=2.0)
        started = time.monotonic()
        result = capture.enqueue_store(
            provider,
            content=_CONTENT % "during-replay",
            source="test",
            target="memory",
            session_id=provider._session_id,
        )
        assert time.monotonic() - started < 1.0
        assert result["status"] in {"accepted", "coalesced"}
        report = capture.capture_queue_report(provider)
        assert report["depth"] >= 1 or result["status"] == "coalesced"
        assert str(tmp_path) not in json.dumps(report)
    finally:
        release.set()
        worker.join(timeout=2.0)
        _shutdown(provider, release)


def test_write_queue_maxsize_is_finite_and_racy_puts_never_exceed_it() -> None:
    provider = ScopeRecallMemoryProvider()
    work_queue = provider._write_queue
    assert work_queue.maxsize == CONTROL_QUEUE_MAXSIZE
    assert work_queue.maxsize > 0
    assert work_queue.maxsize != queue_capacity(provider._config)

    overflow: list[int] = []

    def hammer() -> None:
        for _ in range(200):
            wake_writer(provider)
            size = work_queue.qsize()
            if size > work_queue.maxsize:
                overflow.append(size)

    threads = [threading.Thread(target=hammer) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
        assert thread.is_alive() is False
    assert overflow == []
    assert work_queue.qsize() <= work_queue.maxsize
    while True:
        try:
            work_queue.put_nowait({"kind": "drain"})
        except queue.Full:
            break
    assert work_queue.qsize() == work_queue.maxsize
    with pytest.raises(queue.Full):
        work_queue.put_nowait({"kind": "drain"})


def _invoke_production_caller(kind: str, provider: object) -> None:
    if kind == "enqueue_store":
        capture.enqueue_and_observe(
            provider,
            caller="enqueue_store",
            content=_TURN_TEXT,
            source="test",
            target="memory",
            session_id=getattr(provider, "_session_id", "intent-session"),
        )
        return
    if kind == "capture_turn_llm_candidates":
        capture.capture_turn_llm_candidates(
            provider,
            clean_user=_TURN_TEXT,
            clean_assistant=_TURN_TEXT,
            user_allowed=True,
            assistant_allowed=True,
            extract_fn=lambda *_args, **_kwargs: [
                Candidate(content=_TURN_TEXT, target="memory")
            ],
        )
        return
    if kind == "capture_turn_fallbacks.regex":
        provider._config["capture_raw_user"] = False
        provider._config["capture_assistant"] = False
        capture.capture_turn_fallbacks(
            provider,
            clean_user=_TURN_TEXT,
            clean_assistant=_TURN_TEXT,
            user_allowed=True,
            assistant_allowed=True,
            llm_extracted=False,
            capture_policy_blocked=False,
            min_capture=20,
            extract_candidates_fn=lambda _text: [
                ExtractionCandidate(
                    content=_TURN_TEXT,
                    target="user",
                    category="fact",
                    confidence=0.9,
                )
            ],
        )
        return
    if kind == "capture_turn_fallbacks.raw_user":
        provider._config["per_turn_extraction"] = {"enabled": False}
        provider._config["capture_assistant"] = False
        capture.capture_turn_fallbacks(
            provider,
            clean_user=_TURN_TEXT,
            clean_assistant=_TURN_TEXT,
            user_allowed=True,
            assistant_allowed=True,
            llm_extracted=False,
            capture_policy_blocked=False,
            min_capture=20,
            extract_candidates_fn=lambda _text: [],
        )
        return
    if kind == "capture_turn_fallbacks.assistant":
        provider._config["per_turn_extraction"] = {"enabled": False}
        provider._config["capture_raw_user"] = False
        capture.capture_turn_fallbacks(
            provider,
            clean_user=_TURN_TEXT,
            clean_assistant=_TURN_TEXT,
            user_allowed=True,
            assistant_allowed=True,
            llm_extracted=False,
            capture_policy_blocked=False,
            min_capture=20,
            extract_candidates_fn=lambda _text: [],
        )
        return
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "caller",
    [
        "enqueue_store",
        "capture_turn_llm_candidates",
        "capture_turn_fallbacks.regex",
        "capture_turn_fallbacks.raw_user",
        "capture_turn_fallbacks.assistant",
    ],
)
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("accepted", "persisted"),
        ("rejected", "queue_full"),
        ("deferred", "sqlite_busy"),
    ],
)
def test_production_callers_handle_enqueue_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    status: str,
    reason: str,
) -> None:
    conn = _conn(tmp_path)
    provider = _stub_provider(conn, capacity=8)
    seen: list[tuple[str, str, str]] = []
    original_handle = capture.handle_capture_enqueue

    def fake_enqueue(*_args, **_kwargs):
        return {
            "status": status,
            "reason": reason,
            "intent_id": 11 if status == "accepted" else None,
            "depth": 1 if status == "accepted" else 0,
            "capacity": 8,
            "durable_accounted": True,
        }

    def tracking_handle(current, result, *, caller: str):
        handled = original_handle(current, result, caller=caller)
        seen.append(
            (
                str(handled.get("status") or ""),
                str(handled.get("reason") or ""),
                str(getattr(current, "_last_capture_enqueue", {}).get("caller") or ""),
            )
        )
        return handled

    monkeypatch.setattr(capture, "enqueue_store", fake_enqueue)
    monkeypatch.setattr(capture, "handle_capture_enqueue", tracking_handle)
    _invoke_production_caller(caller, provider)
    assert seen, f"{caller} ignored the enqueue result"
    assert all(item[0] == status and item[1] == reason for item in seen)
    last = getattr(provider, "_last_capture_enqueue")
    assert last["status"] == status
    assert last["reason"] == reason
    assert last["caller"] == caller
    conn.close()


def test_enqueue_store_real_accepted_rejected_deferred(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    provider = _stub_provider(conn, capacity=8)
    accepted = capture.enqueue_store(
        provider,
        content=_CONTENT % "real-accepted",
        source="test",
        target="memory",
        session_id="real",
    )
    assert accepted["status"] == "accepted"
    assert int(accepted["intent_id"] or 0) > 0
    assert accepted["capacity"] == 8
    assert accepted["capacity"] != provider._write_queue.maxsize

    for index in range(7):
        result = capture.enqueue_store(
            provider,
            content=_CONTENT % f"real-fill-{index}",
            source="test",
            target="memory",
            session_id="real",
        )
        assert result["status"] == "accepted"

    rejected = capture.enqueue_store(
        provider,
        content=_CONTENT % "real-rejected",
        source="test",
        target="memory",
        session_id="real",
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "queue_full"
    assert rejected.get("durable_accounted") is True
    assert unconsumed_depth(conn) == 8

    holder = sqlite3.connect(provider._db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        deferred = capture.enqueue_store(
            provider,
            content=_CONTENT % "real-busy",
            source="test",
            target="memory",
            session_id="real-busy",
        )
        assert time.monotonic() - started < 1.0
    finally:
        holder.rollback()
        holder.close()
    assert deferred["status"] == "deferred"
    assert deferred["reason"] == "sqlite_busy"
    assert deferred.get("durable_accounted") is True
    assert deferred["status"] != "accepted"
    report = capture.capture_queue_report(provider)
    assert report["rejected"] >= 1
    assert report["deferred"] >= 1
    assert str(tmp_path) not in json.dumps(report)
    conn.close()


def test_durable_rejected_deferred_survive_provider_reconstruction(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    provider = _stub_provider(conn, capacity=8)
    db_path = Path(provider._db_path)
    for index in range(8):
        result = persist_capture_intent(
            conn,
            content=_CONTENT % f"recon-{index}",
            source="test",
            target="memory",
            session_id="recon",
            capacity=8,
        )
        assert result["status"] == "accepted"
    overflow = persist_capture_intent(
        conn,
        content=_CONTENT % "recon-overflow",
        source="test",
        target="memory",
        session_id="recon-overflow",
        capacity=8,
    )
    assert overflow["status"] == "rejected"
    conn.commit()
    conn.close()

    first = _stub_provider(sqlite3.connect(db_path), capacity=8)
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        busy = capture.enqueue_store(
            first,
            content=_CONTENT % "recon-busy",
            source="test",
            target="memory",
            session_id="recon-busy",
        )
    finally:
        holder.rollback()
        holder.close()
    assert busy["status"] == "deferred"
    assert busy["reason"] == "sqlite_busy"
    first._conn.close()

    rebuilt = _stub_provider(sqlite3.connect(db_path), capacity=8)
    report = capture.capture_queue_report(rebuilt)
    assert report["rejected"] >= 1
    assert report["deferred"] >= 1
    assert report["depth"] == 8
    assert unconsumed_depth(rebuilt._conn) == 8
    dumped = json.dumps(report)
    assert str(tmp_path) not in dumped
    receipt = sidecar_path(rebuilt)
    if receipt is not None and receipt.is_file():
        text = receipt.read_text(encoding="utf-8")
        assert "Durable capture" not in text
        assert "recon-busy" not in text
        assert "recon-overflow" not in text
        assert RECEIPT_FILENAME in receipt.name
    rebuilt._conn.close()


def test_flush_shutdown_bounded_when_control_queue_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path, capacity=8)
    started = threading.Event()
    release = threading.Event()
    original = capture.store_now

    def blocked_store_now(provider_arg, **kwargs):
        started.set()
        assert release.wait(timeout=5.0)
        return original(provider_arg, **kwargs)

    monkeypatch.setattr(capture, "store_now", blocked_store_now)
    monkeypatch.setitem(
        provider._writer_thread._target.__globals__, "store_now", blocked_store_now
    )
    try:
        first = capture.enqueue_store(
            provider,
            content=_CONTENT % "full-control",
            source="test",
            target="memory",
            session_id=provider._session_id,
        )
        assert first["status"] == "accepted"
        assert started.wait(timeout=2.0)
        work_queue = provider._write_queue
        assert work_queue.maxsize == CONTROL_QUEUE_MAXSIZE
        while True:
            try:
                work_queue.put_nowait({"kind": "drain"})
            except queue.Full:
                break
        assert work_queue.qsize() == work_queue.maxsize
        started_at = time.monotonic()
        assert capture.flush_writer(provider, timeout=0.2) is False
        assert time.monotonic() - started_at < 1.0
        started_at = time.monotonic()
        with pytest.raises(RuntimeError, match="did not acknowledge"):
            capture.shutdown_writer(provider, timeout=0.2)
        assert time.monotonic() - started_at < 1.5
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert capture.capture_queue_report(provider)["depth"] >= 1
        assert work_queue.qsize() <= work_queue.maxsize
    finally:
        release.set()
        _shutdown(provider, release)


def test_fallback_store_hint_is_bounded_and_never_accepted() -> None:
    provider = _stub_provider(None, capacity=8)
    provider._truth_writer_role = "owner"
    result = capture.enqueue_store(
        provider,
        content=_TURN_TEXT,
        source="test",
        target="memory",
        session_id="fallback",
    )
    assert result["status"] != "accepted"
    assert result["status"] in {"deferred", "rejected"}
    assert provider._write_queue.maxsize == CONTROL_QUEUE_MAXSIZE
    assert provider._write_queue.qsize() <= provider._write_queue.maxsize


def test_enqueue_observer_does_not_double_count_failed_receipt_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _stub_provider(None, capacity=8)
    attempts: list[tuple[str, str]] = []

    monkeypatch.setattr(
        capture,
        "persist_from_provider",
        lambda *_args, **_kwargs: {
            "status": "deferred",
            "reason": "sqlite_busy",
            "intent_id": None,
            "depth": 0,
            "capacity": 8,
            "durable_accounted": False,
        },
    )

    def fail_receipt(_provider, *, status: str, reason: str, count: int = 1) -> bool:
        assert count == 1
        attempts.append((status, reason))
        return False

    monkeypatch.setattr(capture_outcomes, "record_sanitized_outcome", fail_receipt)
    result = capture.enqueue_and_observe(
        provider,
        caller="double-count-regression",
        content=_TURN_TEXT,
        source="test",
        target="memory",
        session_id="double-count-regression",
    )

    assert attempts == [("deferred", "sqlite_busy")]
    assert result["status"] == "deferred"
    assert result["durable_accounted"] is False
    assert "_outcome_accounting_attempted" not in result


def test_rejected_outcome_without_write_authority_never_writes_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn(tmp_path)
    provider = _stub_provider(conn, capacity=8)
    provider._truth_writer_role = "reader"

    def forbidden_truth_write(*_args, **_kwargs) -> bool:
        raise AssertionError("rejected work must not reopen and mutate the truth DB")

    monkeypatch.setattr(
        capture_outcomes,
        "_record_on_sqlite_file",
        forbidden_truth_write,
    )

    assert capture_outcomes.record_sanitized_outcome(
        provider,
        status="rejected",
        reason="write_authority",
    ) is True
    receipt = sidecar_path(provider)
    assert receipt is not None and receipt.is_file()
    assert "write_authority" in receipt.read_text(encoding="utf-8")
    conn.close()


def test_enqueue_holds_lifecycle_guard_through_intent_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn(tmp_path)
    provider = _stub_provider(conn, capacity=8)
    lifecycle_held = False

    @contextmanager
    def lifecycle_guard(_provider):
        nonlocal lifecycle_held
        assert lifecycle_held is False
        lifecycle_held = True
        try:
            yield
        finally:
            lifecycle_held = False

    def persist_while_guarded(*_args, **_kwargs):
        assert lifecycle_held is True
        return {
            "status": "coalesced",
            "reason": "duplicate_unconsumed",
            "intent_id": 1,
            "depth": 1,
            "capacity": 8,
        }

    monkeypatch.setattr(capture, "_writer_lifecycle_lock", lifecycle_guard)
    monkeypatch.setattr(capture, "persist_from_provider", persist_while_guarded)

    result = capture.enqueue_store(
        provider,
        content=_CONTENT % "lifecycle-guard",
        source="test",
        target="memory",
        session_id="lifecycle-guard",
    )

    assert result["status"] == "coalesced"
    assert lifecycle_held is False
    conn.close()
