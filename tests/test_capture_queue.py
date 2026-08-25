"""Bounded process-local capture queue and mutation-barrier contracts."""

from __future__ import annotations

import json
import queue
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import scope_recall.capture as capture
import scope_recall.memory_ops as memory_ops
from scope_recall.capture_filters import sanitize_capture_text, sanitize_structured_value
from scope_recall.config import DEFAULT_CONFIG
from scope_recall.models import RuntimeScope
from scope_recall.provider import ScopeRecallMemoryProvider
from scope_recall.sql_store import ensure_schema

_CONTENT = (
    "Bounded process-local capture %s must preserve its enqueue-time authorization "
    "and must never survive as a durable intent payload."
)


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _QueueProvider:
    def __init__(self, *, capacity: int = 8, storage_dir: Path | None = None) -> None:
        self._config = {
            "capture_queue_capacity": capacity,
            "min_capture_length": 20,
        }
        self._lock = threading.RLock()
        self._writer_lifecycle_lock = threading.RLock()
        self._capture_submission_lock = threading.RLock()
        self._write_queue: queue.Queue[Any] = queue.Queue(maxsize=capacity)
        self._writer_thread: Any = _AliveThread()
        self._stop = threading.Event()
        self._maintenance_stop = threading.Event()
        self._shutdown_requested = threading.Event()
        self._truth_writer_role = "owner"
        self._writer_failed_writes = 0
        self._writer_reported_failures = 0
        self._writer_last_error_type = ""
        self._capture_queue_rejected = 0
        self._capture_queue_deferred = 0
        self._capture_queue_processing = 0
        self._capture_enqueue_receipts: list[dict[str, Any]] = []
        self._session_id = "queue-session"
        self._scope = RuntimeScope(
            platform="cli",
            user_id="user-a",
            chat_id="chat-a",
            agent_identity="agent-a",
            agent_workspace="workspace-a",
        )
        self._scope_id = "local-scope-a"
        self._shared_scope_id = "shared-scope-a"
        self._shared_pool_scope_id = "shared-pool-a"
        self._storage_dir = storage_dir
        self._db_path = None



def _live_provider(tmp_path: Path, *, capacity: int = 8) -> ScopeRecallMemoryProvider:
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
    loaded = ScopeRecallMemoryProvider()
    loaded.initialize(
        "capture-queue",
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="user-a",
        chat_id="chat-a",
        agent_identity="agent-a",
        agent_workspace="workspace-a",
        agent_context="primary",
    )
    return loaded



def _shutdown(provider: ScopeRecallMemoryProvider, release: threading.Event | None = None) -> None:
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



def _active_content_count(conn: sqlite3.Connection, content: str) -> int:
    count = 0
    for row in conn.execute("SELECT content, metadata FROM memories").fetchall():
        if str(row[0]) != content:
            continue
        try:
            metadata = json.loads(str(row[1] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        lifecycle = str(metadata.get("lifecycle") or "active").strip().lower()
        if lifecycle not in {"archived", "deleted", "obsolete", "rejected", "superseded"}:
            count += 1
    return count



def test_fresh_schema_has_no_capture_tables_or_durable_intent_module(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "memory.sqlite3")
    ensure_schema(conn)
    capture_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'capture_intent%'"
        ).fetchall()
    }
    conn.close()

    assert capture_tables == set()
    assert not Path(capture.__file__).with_name("capture_intents.py").exists()



def test_provider_queue_uses_configured_structural_maxsize() -> None:
    provider = ScopeRecallMemoryProvider()

    assert provider._write_queue.maxsize == int(DEFAULT_CONFIG["capture_queue_capacity"])
    assert provider._write_queue.maxsize > 0



def test_enqueue_sanitizes_payload_before_process_local_acceptance() -> None:
    provider = _QueueProvider(capacity=8)
    data_url = "data:image/png;base64," + ("A" * 4096)
    raw = (
        "Keep the deployment rollback rule before "
        + data_url
        + " and preserve the final instruction after "
        + "[Image attached at: C:\\Users\\Alice\\image_cache\\img_123.png]."
    )
    metadata = {
        "note": raw,
        "nested": {"path": "C:\\Users\\Alice\\private\\secret.txt"},
        "api_key": "sk-" + "example-not-a-real-token-1234567890",
    }
    expected_metadata, _ = sanitize_structured_value(metadata)

    result = capture.enqueue_store(
        provider,
        content=raw,
        source="test",
        target="general",
        session_id="sanitized",
        metadata=metadata,
    )
    job = provider._write_queue.get_nowait()

    assert result["status"] == "accepted"
    assert result["reason"] == "queued"
    assert job["content"] == sanitize_capture_text(raw)
    assert job["metadata"] == expected_metadata
    dumped = json.dumps(job, ensure_ascii=False, default=str)
    assert data_url not in dumped
    assert "C:\\Users\\Alice" not in dumped



def test_queue_full_and_writer_unavailable_are_explicit_and_nonpersistent(
    tmp_path: Path,
) -> None:
    provider = _QueueProvider(capacity=1, storage_dir=tmp_path)
    first = capture.enqueue_store(
        provider,
        content=_CONTENT % "first",
        source="test",
        target="general",
        session_id="queue-full",
    )
    second = capture.enqueue_store(
        provider,
        content=_CONTENT % "second",
        source="test",
        target="general",
        session_id="queue-full",
    )
    provider._writer_thread = None
    deferred = capture.enqueue_store(
        provider,
        content=_CONTENT % "deferred",
        source="test",
        target="general",
        session_id="writer-unavailable",
    )

    assert first["status"] == "accepted"
    assert second == {
        "status": "rejected",
        "reason": "queue_full",
        "intent_id": None,
        "depth": 1,
        "capacity": 1,
    }
    assert deferred["status"] == "deferred"
    assert deferred["reason"] == "writer_unavailable"
    assert not (tmp_path / "capture_outcome_receipts.json").exists()



def test_enqueue_holds_submission_and_lifecycle_through_check_and_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _QueueProvider(capacity=2)
    submission_held = False
    lifecycle_held = False

    @contextmanager
    def submission_guard(_provider: Any):
        nonlocal submission_held
        submission_held = True
        try:
            yield
        finally:
            submission_held = False

    @contextmanager
    def lifecycle_guard(_provider: Any):
        nonlocal lifecycle_held
        lifecycle_held = True
        try:
            yield
        finally:
            lifecycle_held = False

    class GuardedQueue(queue.Queue[Any]):
        def put_nowait(self, item: Any) -> None:
            assert submission_held is True
            assert lifecycle_held is True
            super().put_nowait(item)

    provider._write_queue = GuardedQueue(maxsize=2)
    monkeypatch.setattr(capture, "_capture_submission_lock", submission_guard)
    monkeypatch.setattr(capture, "_writer_lifecycle_lock", lifecycle_guard)

    result = capture.enqueue_store(
        provider,
        content=_CONTENT % "guarded",
        source="test",
        target="general",
        session_id="guarded",
    )

    assert result["status"] == "accepted"
    assert submission_held is False
    assert lifecycle_held is False



def test_positive_write_authority_is_required_before_enqueue() -> None:
    provider = _QueueProvider(capacity=2)
    provider._truth_writer_role = "reader"

    result = capture.enqueue_store(
        provider,
        content=_CONTENT % "reader",
        source="test",
        target="general",
        session_id="reader",
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "write_authority"
    assert provider._write_queue.empty()



def test_accepted_job_uses_enqueue_time_authorization_after_provider_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path)
    original_store_now = capture.store_now
    entered = threading.Event()
    release = threading.Event()
    content = _CONTENT % "identity-switch"
    original_scope_id = provider._scope_id
    original_user_id = provider._scope.user_id
    original_agent_identity = provider._scope.agent_identity

    def blocked_store_now(provider_arg: Any, **kwargs: Any):
        entered.set()
        assert release.wait(timeout=5.0)
        return original_store_now(provider_arg, **kwargs)

    monkeypatch.setattr(capture, "store_now", blocked_store_now)
    try:
        result = capture.enqueue_store(
            provider,
            content=content,
            source="turn-user",
            target="general",
            session_id="identity-switch",
        )
        assert result["status"] == "accepted"
        assert entered.wait(timeout=2.0)

        provider._scope = RuntimeScope(
            platform="cli",
            user_id="user-b",
            chat_id="chat-b",
            agent_identity="agent-b",
            agent_workspace="workspace-b",
            agent_context="primary",
        )
        provider._scope_id = "local-scope-b"
        provider._shared_scope_id = "shared-scope-b"
        provider._shared_pool_scope_id = "shared-pool-b"
        release.set()
        assert provider.flush(timeout=3.0) is True

        with provider._lock:
            row = provider._require_conn().execute(
                "SELECT scope_id, user_id, agent_identity FROM memories WHERE session_id = ?",
                ("identity-switch",),
            ).fetchone()
        assert row is not None
        assert str(row["scope_id"]) == original_scope_id
        assert str(row["user_id"]) == original_user_id
        assert str(row["agent_identity"]) == original_agent_identity
        assert provider._write_queue.empty()
    finally:
        release.set()
        _shutdown(provider, release)


def test_completed_capture_payload_is_released_by_writer(tmp_path: Path) -> None:
    provider = _live_provider(tmp_path)
    try:
        result = capture.enqueue_store(
            provider,
            content=_CONTENT % "released-after-completion",
            source="turn-user",
            target="general",
            session_id="released-after-completion",
            metadata={"transient": "capture-payload-marker"},
        )
        assert result["status"] == "accepted"
        assert provider.flush(timeout=3.0) is True

        thread = provider._writer_thread
        assert thread is not None and thread.ident is not None
        frame = sys._current_frames()[thread.ident]
        while frame is not None and frame.f_code.co_name != "writer_loop":
            frame = frame.f_back
        assert frame is not None
        assert frame.f_locals.get("job") is None
        assert provider._write_queue.empty()
    finally:
        _shutdown(provider)


def test_failed_capture_job_does_not_starve_later_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path)
    original_store_now = capture.store_now
    calls = 0

    def fail_first_store(provider_arg: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("deterministic poison capture")
        return original_store_now(provider_arg, **kwargs)

    monkeypatch.setattr(capture, "store_now", fail_first_store)
    try:
        first = capture.enqueue_store(
            provider,
            content=_CONTENT % "poison-first",
            source="turn-user",
            target="general",
            session_id="poison-first",
        )
        second = capture.enqueue_store(
            provider,
            content=_CONTENT % "healthy-second",
            source="turn-user",
            target="general",
            session_id="healthy-second",
        )
        assert first["status"] == second["status"] == "accepted"
        assert provider.flush(timeout=3.0) is False
        with provider._lock:
            sessions = {
                str(row[0])
                for row in provider._require_conn()
                .execute(
                    "SELECT session_id FROM memories WHERE session_id IN (?, ?)",
                    ("poison-first", "healthy-second"),
                )
                .fetchall()
            }
        assert sessions == {"healthy-second"}
        assert provider.flush(timeout=3.0) is True
    finally:
        _shutdown(provider)


@pytest.mark.parametrize("mutation", ["archive", "hard_delete"])
def test_processing_capture_finishes_before_forget_and_cannot_revive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    provider = _live_provider(tmp_path)
    content = _CONTENT % mutation
    memory_id, inserted, _ = capture.store_now(
        provider,
        content=content,
        source="turn-user",
        target="general",
        session_id="before-forget",
    )
    assert inserted is True
    original_store_now = capture.store_now
    entered = threading.Event()
    release = threading.Event()
    mutation_done = threading.Event()
    mutation_errors: list[BaseException] = []

    def blocked_store_now(provider_arg: Any, **kwargs: Any):
        entered.set()
        assert release.wait(timeout=5.0)
        return original_store_now(provider_arg, **kwargs)

    def run_mutation() -> None:
        try:
            if mutation == "archive":
                memory_ops.archive_memories(provider, [memory_id])
            else:
                memory_ops.delete_memories(provider, [memory_id])
        except BaseException as exc:  # pragma: no cover - asserted below
            mutation_errors.append(exc)
        finally:
            mutation_done.set()

    monkeypatch.setattr(capture, "store_now", blocked_store_now)
    try:
        queued = capture.enqueue_store(
            provider,
            content=content,
            source="turn-user",
            target="general",
            session_id="accepted-before-forget",
        )
        assert queued["status"] == "accepted"
        assert entered.wait(timeout=2.0)

        forget_thread = threading.Thread(target=run_mutation, name=f"forget-{mutation}")
        forget_thread.start()
        completed_before_capture = mutation_done.wait(timeout=0.2)
        release.set()
        forget_thread.join(timeout=3.0)

        assert completed_before_capture is False
        assert forget_thread.is_alive() is False
        assert mutation_errors == []
        assert provider.flush(timeout=3.0) is True
        with provider._lock:
            active = _active_content_count(provider._require_conn(), content)
        assert active == 0
    finally:
        release.set()
        _shutdown(provider, release)


@pytest.mark.parametrize("tool_name", ["scope_recall_merge", "scope_recall_memory"])
def test_processing_capture_finishes_before_tool_merge_and_cannot_revive_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    provider = _live_provider(tmp_path)
    target_content = _CONTENT % f"merge-target-{tool_name}"
    source_content = _CONTENT % f"merge-source-{tool_name}"
    target_id, target_inserted, _ = capture.store_now(
        provider,
        content=target_content,
        source="turn-user",
        target="general",
        session_id="merge-target",
    )
    source_id, source_inserted, _ = capture.store_now(
        provider,
        content=source_content,
        source="turn-user",
        target="general",
        session_id="merge-source",
    )
    assert target_inserted is source_inserted is True
    original_store_now = capture.store_now
    writer_entered = threading.Event()
    release_writer = threading.Event()
    merge_done = threading.Event()
    merge_payload: dict[str, Any] = {}
    merge_errors: list[BaseException] = []

    def paused_store_now(provider_arg: Any, **kwargs: Any):
        writer_entered.set()
        assert release_writer.wait(timeout=5.0)
        return original_store_now(provider_arg, **kwargs)

    def run_merge() -> None:
        try:
            args: dict[str, Any] = {
                "target_id": target_id,
                "source_ids": [source_id],
            }
            if tool_name == "scope_recall_memory":
                args["action"] = "merge"
            merge_payload.update(json.loads(provider.handle_tool_call(tool_name, args)))
        except BaseException as exc:  # pragma: no cover - asserted below
            merge_errors.append(exc)
        finally:
            merge_done.set()

    monkeypatch.setattr(capture, "store_now", paused_store_now)
    merge_thread = threading.Thread(target=run_merge, name=f"tool-merge-{tool_name}")
    try:
        queued = capture.enqueue_store(
            provider,
            content=source_content,
            source="turn-user",
            target="general",
            session_id="accepted-before-merge",
        )
        assert queued["status"] == "accepted"
        assert writer_entered.wait(timeout=2.0)

        merge_thread.start()
        completed_while_writer_paused = merge_done.wait(timeout=0.2)
        release_writer.set()
        merge_thread.join(timeout=3.0)

        assert completed_while_writer_paused is False
        assert merge_thread.is_alive() is False
        assert merge_errors == []
        assert merge_payload.get("merged") is True
        assert provider.flush(timeout=3.0) is True
        with provider._lock:
            active_source = _active_content_count(
                provider._require_conn(), source_content
            )
        assert active_source == 0
    finally:
        release_writer.set()
        _shutdown(provider, release_writer)


@pytest.mark.parametrize("tool_case", ["forget", "dedupe"])
def test_tool_memory_mutation_and_enqueue_do_not_invert_submission_lifecycle_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_case: str
) -> None:
    """Public archive/delete tools enter capture barrier before lifecycle."""

    provider = _live_provider(tmp_path)
    provider._config["maintenance_tools_enabled"] = True
    memory_id, inserted, _ = capture.store_now(
        provider,
        content=_CONTENT % "tool-forget-lock-order",
        source="turn-user",
        target="general",
        session_id="tool-forget-lock-order",
    )
    assert inserted is True
    original_submission = capture._capture_submission_lock
    original_lifecycle = capture._writer_lifecycle_lock
    enqueue_holds_submission = threading.Event()
    allow_enqueue_lifecycle = threading.Event()
    forget_waiting_submission = threading.Event()
    enqueue_errors: list[BaseException] = []
    forget_payload: dict[str, Any] = {}

    @contextmanager
    def observed_submission(current: Any):
        if (
            current is provider
            and threading.current_thread().name == "tool-memory-mutation-lock-order"
        ):
            forget_waiting_submission.set()
        with original_submission(current):
            yield

    @contextmanager
    def controlled_lifecycle(current: Any):
        if (
            current is provider
            and threading.current_thread().name == "capture-enqueue-lock-order"
        ):
            enqueue_holds_submission.set()
            assert allow_enqueue_lifecycle.wait(timeout=2.0)
            acquired = current._writer_lifecycle_lock.acquire(timeout=0.25)
            if not acquired:
                raise RuntimeError("submission/lifecycle lock order inverted")
            try:
                yield
            finally:
                current._writer_lifecycle_lock.release()
            return
        with original_lifecycle(current):
            yield

    def run_enqueue() -> None:
        try:
            capture.enqueue_store(
                provider,
                content=_CONTENT % "concurrent-enqueue-lock-order",
                source="turn-user",
                target="general",
                session_id="concurrent-enqueue-lock-order",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            enqueue_errors.append(exc)

    def run_mutation() -> None:
        tool_name = "scope_recall_forget"
        tool_args: dict[str, Any] = {
            "ids": [memory_id],
            "reason": "lock-order regression",
        }
        if tool_case == "dedupe":
            tool_name = "scope_recall_dedupe"
            tool_args = {"dry_run": False}
        forget_payload.update(
            json.loads(provider.handle_tool_call(tool_name, tool_args))
        )

    monkeypatch.setattr(capture, "_capture_submission_lock", observed_submission)
    monkeypatch.setattr(capture, "_writer_lifecycle_lock", controlled_lifecycle)
    enqueue_thread = threading.Thread(
        target=run_enqueue, name="capture-enqueue-lock-order"
    )
    forget_thread = threading.Thread(
        target=run_mutation, name="tool-memory-mutation-lock-order"
    )
    try:
        enqueue_thread.start()
        assert enqueue_holds_submission.wait(timeout=2.0)
        forget_thread.start()
        assert forget_waiting_submission.wait(timeout=2.0)
        allow_enqueue_lifecycle.set()
        enqueue_thread.join(timeout=3.0)
        forget_thread.join(timeout=3.0)

        assert enqueue_thread.is_alive() is False
        assert forget_thread.is_alive() is False
        assert enqueue_errors == []
        if tool_case == "forget":
            assert forget_payload.get("archived") == 1
        else:
            assert forget_payload.get("dry_run") is False
    finally:
        allow_enqueue_lifecycle.set()
        _shutdown(provider)



def test_forgetting_run_waits_for_accepted_capture_before_archiving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dry-run forgetting tool cannot be undone by accepted capture."""

    provider = _live_provider(tmp_path)
    provider._config["maintenance_tools_enabled"] = True
    content = "Assistant scratch prose accepted before automatic forgetting."
    _memory_id, inserted, _ = capture.store_now(
        provider,
        content=content,
        source="turn-assistant",
        target="general",
        session_id="forgetting-existing",
    )
    assert inserted is True
    original_store_now = capture.store_now
    writer_entered = threading.Event()
    release_writer = threading.Event()
    forgetting_done = threading.Event()
    forgetting_payload: dict[str, Any] = {}

    def paused_store_now(provider_arg: Any, **kwargs: Any):
        writer_entered.set()
        assert release_writer.wait(timeout=5.0)
        return original_store_now(provider_arg, **kwargs)

    def run_forgetting_tool() -> None:
        try:
            forgetting_payload.update(
                json.loads(
                    provider.handle_tool_call(
                        "scope_recall_forgetting_run",
                        {"dry_run": False, "limit": 20},
                    )
                )
            )
        finally:
            forgetting_done.set()

    monkeypatch.setattr(capture, "store_now", paused_store_now)
    forgetting_thread = threading.Thread(
        target=run_forgetting_tool, name="automatic-forgetting-tool"
    )
    try:
        queued = capture.enqueue_store(
            provider,
            content=content,
            source="turn-assistant",
            target="general",
            session_id="accepted-before-forgetting",
        )
        assert queued["status"] == "accepted"
        assert writer_entered.wait(timeout=2.0)

        forgetting_thread.start()
        completed_while_writer_paused = forgetting_done.wait(timeout=0.2)
        release_writer.set()
        forgetting_thread.join(timeout=3.0)

        assert completed_while_writer_paused is False
        assert forgetting_thread.is_alive() is False
        assert forgetting_payload.get("archived", 0) >= 1
        assert provider.flush(timeout=3.0) is True
        with provider._lock:
            active = _active_content_count(provider._require_conn(), content)
        assert active == 0
    finally:
        release_writer.set()
        _shutdown(provider, release_writer)



def test_enqueue_waits_for_forget_mutation_barrier_then_uses_new_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _live_provider(tmp_path)
    existing_id, inserted, _ = capture.store_now(
        provider,
        content=_CONTENT % "barrier-existing",
        source="turn-user",
        target="general",
        session_id="barrier-existing",
    )
    assert inserted is True
    original_archive_truth = memory_ops._archive_memories_truth
    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    mutation_done = threading.Event()
    enqueue_done = threading.Event()
    enqueue_result: dict[str, Any] = {}

    def blocked_archive_truth(*args: Any, **kwargs: Any):
        mutation_entered.set()
        assert release_mutation.wait(timeout=5.0)
        return original_archive_truth(*args, **kwargs)

    def run_mutation() -> None:
        try:
            memory_ops.archive_memories(provider, [existing_id])
        finally:
            mutation_done.set()

    def run_enqueue() -> None:
        try:
            enqueue_result.update(
                capture.enqueue_store(
                    provider,
                    content=_CONTENT % "after-barrier",
                    source="turn-user",
                    target="general",
                    session_id="after-barrier",
                )
            )
        finally:
            enqueue_done.set()

    monkeypatch.setattr(memory_ops, "_archive_memories_truth", blocked_archive_truth)
    mutation_thread = threading.Thread(target=run_mutation, name="forget-barrier")
    enqueue_thread = threading.Thread(target=run_enqueue, name="enqueue-during-forget")
    try:
        mutation_thread.start()
        assert mutation_entered.wait(timeout=2.0)
        enqueue_thread.start()
        completed_during_mutation = enqueue_done.wait(timeout=0.2)
        release_mutation.set()
        mutation_thread.join(timeout=3.0)
        enqueue_thread.join(timeout=3.0)

        assert completed_during_mutation is False
        assert mutation_done.is_set()
        assert enqueue_thread.is_alive() is False
        assert enqueue_result["status"] == "accepted"
        assert provider.flush(timeout=3.0) is True
    finally:
        release_mutation.set()
        _shutdown(provider)



def test_shutdown_cannot_publish_between_enqueue_check_and_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _QueueProvider(capacity=4)
    put_entered = threading.Event()
    release_put = threading.Event()
    enqueue_done = threading.Event()
    shutdown_done = threading.Event()

    class PausingQueue(queue.Queue[Any]):
        def put_nowait(self, item: Any) -> None:
            if isinstance(item, dict) and item.get("kind") == "store":
                put_entered.set()
                assert release_put.wait(timeout=5.0)
            super().put_nowait(item)

    provider._write_queue = PausingQueue(maxsize=4)

    def run_enqueue() -> None:
        try:
            capture.enqueue_store(
                provider,
                content=_CONTENT % "shutdown-atomic",
                source="test",
                target="general",
                session_id="shutdown-atomic",
            )
        finally:
            enqueue_done.set()

    def run_shutdown() -> None:
        try:
            capture.shutdown_writer(provider, timeout=0.5)
        except RuntimeError:
            pass
        finally:
            shutdown_done.set()

    enqueue_thread = threading.Thread(target=run_enqueue, name="atomic-enqueue")
    shutdown_thread = threading.Thread(target=run_shutdown, name="atomic-shutdown")
    enqueue_thread.start()
    assert put_entered.wait(timeout=2.0)
    shutdown_thread.start()
    time.sleep(0.1)
    published_during_put = provider._shutdown_requested.is_set()
    release_put.set()
    enqueue_thread.join(timeout=2.0)
    shutdown_thread.join(timeout=2.0)

    assert published_during_put is False
    assert enqueue_done.is_set()
    assert shutdown_done.is_set()



def test_vector_maintenance_runs_when_relation_extraction_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _QueueProvider(capacity=2)
    provider._config["relation_extraction_enabled"] = False
    provider._vector_ready = False
    provider._vector_store = object()
    provider._embedder = object()
    provider._vector_generation_id = "generation-a"
    calls: list[str] = []

    monkeypatch.setattr(
        "scope_recall.vector_runtime.run_bounded_vector_reconciliation",
        lambda _provider: calls.append("vector")
        or {"status": "ready", "claimed": 0, "completed": 0, "failed": 0},
    )

    capture._drain_relation_rebuild_debt(provider)

    assert calls == ["vector"]
