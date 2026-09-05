"""Issue #58 process-wide idle writer handoff regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.memory import load_memory_provider

import scope_recall._internal.runtime.writer_handoff as writer_handoff_module
from scope_recall.config import DEFAULT_CONFIG, validate_config_override
from scope_recall.capture import enqueue_store
from writer_lease import (
    TRUTH_WRITER_LEASE_FILENAME,
    TRUTH_WRITER_LEASE_INFO_FILENAME,
    TruthWriterLease,
    process_writer_handoff_state,
    truth_writer_process_snapshot,
)
from scope_recall._internal.runtime.writer_handoff import (
    _idle_veto,
    _perform_idle_handoff,
    active_truth_work,
    idle_release_seconds,
    maybe_schedule_idle_writer_handoff,
    note_user_activity,
    writer_handoff_status,
)
from scope_recall._internal.runtime.peer_recovery import live_providers_for_database

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HANDOFF_DETAILS_OUTPUT_ENV = "SCOPE_RECALL_WRITER_HANDOFF_DETAILS_OUTPUT"
_HANDOFF_DETAILS_SCHEMA_VERSION = "scope-recall.writer-lease-handoff-details.v1"
_HANDOFF_STAGES = [
    "initial_owner",
    "peer_reader",
    "idle_fence",
    "all_work_quiescent",
    "os_lease_released",
    "peer_promoted",
    "peer_write_committed",
    "former_owner_remains_reader",
]


@pytest.fixture(autouse=True)
def _disable_ambient_writer_handoff_scheduler(monkeypatch):
    """Keep direct handoff regressions isolated from the live writer loop.

    Tests that exercise scheduling retain the imported production function,
    while the writer loop's dynamic import sees this no-op for the duration of
    each test.  This prevents a background handoff from racing a direct
    ``_perform_idle_handoff`` call after ``_make_idle`` moves the clock.
    """

    monkeypatch.setattr(
        writer_handoff_module,
        "maybe_schedule_idle_writer_handoff",
        lambda _provider: False,
    )


def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    return provider


def _write_config(hermes_home: Path, *, idle_release_seconds: float = 1800.0) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "relation_extraction_enabled": False,
                "writer_lease": {
                    "idle_release_seconds": idle_release_seconds,
                },
            }
        ),
        encoding="utf-8",
    )


def _initialize(provider, hermes_home: Path, session: str) -> None:
    provider.initialize(
        session,
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="handoff-user",
        chat_id="handoff-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _make_idle(*providers) -> None:
    for provider in providers:
        observed = time.monotonic() - idle_release_seconds(provider) - 1.0
        with provider._writer_handoff_activity_lock:
            provider._writer_handoff_last_user_activity = observed
            provider._writer_handoff_last_truth_activity = observed


def test_preflight_activity_veto_never_restarts_or_fences_healthy_writer(tmp_path, monkeypatch, caplog):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "preflight-veto")
        original_writer = provider._writer_thread

        def unexpected_resume(_providers):
            raise AssertionError("preflight never quiesced the writer")

        monkeypatch.setattr(writer_handoff_module, "_abort_quiesced_handoff", unexpected_resume)
        monkeypatch.setattr(writer_handoff_module, "_idle_veto", lambda *_a, **_k: "recent_truth_activity")
        _perform_idle_handoff(provider, process_writer_handoff_state(tmp_path / "scope-recall"))

        assert provider._truth_writer_role == "owner"
        assert not provider._truth_writes_blocked()
        assert provider._writer_thread is original_writer
        assert original_writer.is_alive()
        assert not writer_handoff_status(provider)["operator_action_required"]
        assert "idle writer handoff did not complete" not in caplog.text
    finally:
        provider.shutdown()


def _write_handoff_details(
    *,
    idle_seconds: float,
    process_count: int,
    same_process_provider_count: int,
    stages: list[str],
    simultaneous_writer_observed: bool,
    accepted_work_lost: bool,
    released_counts: dict[str, int],
) -> None:
    """Write the content-free release rehearsal details when requested."""

    requested = os.environ.get(_HANDOFF_DETAILS_OUTPUT_ENV, "").strip()
    if not requested:
        return
    artifact_sha256 = os.environ.get("SCOPE_RECALL_ARTIFACT_SHA256", "").strip()
    assert len(artifact_sha256) == 64
    assert all(character in "0123456789abcdef" for character in artifact_sha256)
    payload = {
        "schema_version": _HANDOFF_DETAILS_SCHEMA_VERSION,
        "writer_artifact_sha256": artifact_sha256,
        "idle_release_seconds": idle_seconds,
        "process_count": process_count,
        "same_process_provider_count": same_process_provider_count,
        "stages": list(stages),
        "simultaneous_writer_observed": simultaneous_writer_observed,
        "accepted_work_lost": accepted_work_lost,
        "holder_count_after_release": released_counts[
            "same_process_holder_count"
        ],
        "connection_pin_count_after_release": released_counts[
            "connection_pin_count"
        ],
        "result": "passed",
    }
    output = Path(requested)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _start_external_handoff_writer(storage: Path) -> subprocess.Popen[str]:
    script = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from plugins.memory import load_memory_provider

        storage = Path({str(storage)!r})
        provider = load_memory_provider("scope-recall")
        if provider is None:
            raise SystemExit(4)
        provider.initialize(
            "handoff-child",
            hermes_home=str(storage.parent),
            platform="desktop",
            user_id="handoff-user",
            chat_id="handoff-child-chat",
            agent_identity="tester",
            agent_workspace="hermes",
            agent_context="primary",
        )
        print("FIRST:" + provider._truth_writer_role, flush=True)
        if sys.stdin.readline().strip() != "promote":
            raise SystemExit(2)
        provider.on_turn_start(2, "promote the desktop-like reader")
        print("SECOND:" + provider._truth_writer_role, flush=True)
        receipt = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {{
                    "content": "The child reader committed after exact OS lease promotion.",
                    "target": "ops",
                }},
            )
        )
        if not receipt.get("id"):
            print("FAILED:" + json.dumps(receipt, sort_keys=True), flush=True)
            raise SystemExit(5)
        print("COMMITTED", flush=True)
        if sys.stdin.readline().strip() != "release":
            raise SystemExit(3)
        provider.shutdown()
        print("RELEASED", flush=True)
        """
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_process_wide_idle_handoff_allows_real_second_process_commit(tmp_path):
    """A1/A2 demote together; B writes; A stays reader until B releases."""

    _write_config(tmp_path)
    first = _provider()
    second = _provider()
    child: subprocess.Popen[str] | None = None
    observed_process_ids = {os.getpid()}
    observed_stages: list[str] = []
    writer_role_observations: list[tuple[bool, bool]] = []

    def observe_writer_roles(*, child_role: str) -> None:
        parent_process_writer = any(
            provider._truth_writer_role == "owner" for provider in (first, second)
        )
        child_process_writer = child_role == "owner"
        writer_role_observations.append(
            (parent_process_writer, child_process_writer)
        )
        assert not (parent_process_writer and child_process_writer)

    try:
        _initialize(first, tmp_path, "handoff-first")
        _initialize(second, tmp_path, "handoff-second")
        storage = tmp_path / "scope-recall"
        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 2,
            "connection_pin_count": 2,
        }
        observed_stages.append("initial_owner")
        observe_writer_roles(child_role="not_started")
        accepted = enqueue_store(
            first,
            content=(
                "The accepted capture must commit before the process-wide writer "
                "handoff releases its authority."
            ),
            source="turn-user",
            target="ops",
            session_id="handoff-first",
        )
        assert accepted["status"] == "accepted"
        assert first.flush(timeout=3.0) is True
        child = _start_external_handoff_writer(storage)
        assert child.pid != os.getpid()
        observed_process_ids.add(child.pid)
        assert len(observed_process_ids) == 2
        assert child.stdout is not None
        assert child.stdin is not None
        first_child_role = child.stdout.readline().strip()
        assert first_child_role == "FIRST:reader"
        observed_stages.append("peer_reader")
        observe_writer_roles(child_role=first_child_role.removeprefix("FIRST:"))
        same_process_providers = set(live_providers_for_database(first))
        assert same_process_providers == {first, second}
        same_process_provider_count = len(same_process_providers)
        assert same_process_provider_count == 2

        _make_idle(first, second)
        state = process_writer_handoff_state(storage)
        _perform_idle_handoff(first, state)

        assert first._truth_writer_role == "reader"
        assert second._truth_writer_role == "reader"
        assert state.handoff_generation == 1
        observed_stages.append("idle_fence")
        for provider in (first, second):
            assert provider._writer_thread is None
            assert provider._write_queue.empty()
            assert provider._capture_queue_processing == 0
            assert provider._writer_handoff_active_truth_work == 0
        observed_stages.append("all_work_quiescent")
        released_counts = truth_writer_process_snapshot(storage)
        assert released_counts == {
            "same_process_holder_count": 0,
            "connection_pin_count": 0,
        }
        observed_stages.append("os_lease_released")
        observe_writer_roles(child_role="reader")
        assert writer_handoff_status(first)["successful_handoff_count"] == 1

        child.stdin.write("promote\n")
        child.stdin.flush()
        second_child_role = child.stdout.readline().strip()
        assert second_child_role == "SECOND:owner"
        observed_stages.append("peer_promoted")
        observe_writer_roles(child_role=second_child_role.removeprefix("SECOND:"))
        committed = child.stdout.readline().strip()
        if committed != "COMMITTED":
            assert child.stderr is not None
            pytest.fail(
                f"{committed}\n{child.stderr.read()}" or "child provider did not commit"
            )
        observed_stages.append("peer_write_committed")

        first.on_turn_start(2, "reader must not overlap child writer")
        assert first._truth_writer_role == "reader"
        observed_stages.append("former_owner_remains_reader")
        observe_writer_roles(child_role="owner")
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "RELEASED"
        assert child.wait(timeout=10) == 0

        first.on_turn_start(3, "promote after child release")
        assert first._truth_writer_role == "owner"
        observe_writer_roles(child_role="released")
        stored_contents = {
            str(row[0])
            for row in first._conn.execute(
                "SELECT content FROM memories WHERE content LIKE ? OR content LIKE ?",
                ("The accepted capture%", "The child reader%"),
            ).fetchall()
        }
        accepted_content = (
            "The accepted capture must commit before the process-wide writer handoff "
            "releases its authority."
        )
        child_content = "The child reader committed after exact OS lease promotion."
        assert stored_contents == {accepted_content, child_content}
        accepted_work_lost = accepted_content not in stored_contents
        assert accepted_work_lost is False
        simultaneous_writer_observed = any(
            parent_writer and child_writer
            for parent_writer, child_writer in writer_role_observations
        )
        assert simultaneous_writer_observed is False
        assert observed_stages == _HANDOFF_STAGES
        _write_handoff_details(
            idle_seconds=idle_release_seconds(first),
            process_count=len(observed_process_ids),
            same_process_provider_count=same_process_provider_count,
            stages=observed_stages,
            simultaneous_writer_observed=simultaneous_writer_observed,
            accepted_work_lost=accepted_work_lost,
            released_counts=released_counts,
        )
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for provider in (second, first):
            try:
                provider.shutdown()
            except Exception:
                pass


def test_extra_connection_pin_vetoes_process_handoff(tmp_path):
    _write_config(tmp_path)
    first = _provider()
    second = _provider()
    extra: TruthWriterLease | None = None
    try:
        _initialize(first, tmp_path, "pin-first")
        _initialize(second, tmp_path, "pin-second")
        storage = tmp_path / "scope-recall"
        extra = TruthWriterLease(storage, role="truth_connection")
        assert extra.acquire()["status"] == "acquired"
        assert set(live_providers_for_database(first)) == {first, second}
        _make_idle(first, second)

        state = process_writer_handoff_state(storage)
        _perform_idle_handoff(first, state)

        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"
        assert first._writer_thread is not None and first._writer_thread.is_alive()
        assert second._writer_thread is not None and second._writer_thread.is_alive()
        status = writer_handoff_status(first)
        assert status["last_failure_code"] == "connection_pin_count_mismatch"
        assert status["release_uncertain"] is False
    finally:
        if extra is not None and extra.acquired:
            extra.release()
        for provider in (second, first):
            try:
                provider.shutdown()
            except Exception:
                pass


def test_active_same_process_peer_vetoes_process_handoff(tmp_path):
    _write_config(tmp_path)
    first = _provider()
    second = _provider()
    try:
        _initialize(first, tmp_path, "peer-idle")
        _initialize(second, tmp_path, "peer-active")
        _make_idle(first)
        note_user_activity(second)
        storage = tmp_path / "scope-recall"

        _perform_idle_handoff(first, process_writer_handoff_state(storage))

        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 2,
            "connection_pin_count": 2,
        }
        assert writer_handoff_status(first)["last_failure_code"] == (
            "recent_user_activity"
        )
    finally:
        for provider in (second, first):
            try:
                provider.shutdown()
            except Exception:
                pass


def test_writer_loop_automatically_schedules_idle_handoff(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "automatic-schedule")
        _make_idle(provider)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and provider._truth_writer_role == "owner":
            time.sleep(0.05)
        assert provider._truth_writer_role == "reader"
        assert writer_handoff_status(provider)["successful_handoff_count"] == 1
    finally:
        provider.shutdown()


def test_wall_clock_rollback_cannot_extend_process_cooldown(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "monotonic-cooldown")
        _make_idle(provider)
        storage = tmp_path / "scope-recall"
        state = process_writer_handoff_state(storage)
        with state.lock:
            state.last_handoff_at = "2999-01-01T00:00:00+00:00"
            state.last_handoff_monotonic = time.monotonic() - 31.0
        provider._writer_handoff_last_probe = 0.0

        assert maybe_schedule_idle_writer_handoff(provider) is True
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and provider._truth_writer_role == "owner":
            time.sleep(0.01)
        assert provider._truth_writer_role == "reader"
    finally:
        provider.shutdown()


def test_activity_generation_change_aborts_and_resumes_all_writers(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    first = _provider()
    second = _provider()
    try:
        _initialize(first, tmp_path, "generation-first")
        _initialize(second, tmp_path, "generation-second")
        _make_idle(first, second)
        import scope_recall.capture as capture_module

        original = capture_module.quiesce_writer_for_handoff
        injected = False

        def quiesce_then_race(peer, timeout=3.0):
            nonlocal injected
            original(peer, timeout=timeout)
            if not injected:
                injected = True
                note_user_activity(first)

        monkeypatch.setattr(
            capture_module, "quiesce_writer_for_handoff", quiesce_then_race
        )
        state = process_writer_handoff_state(tmp_path / "scope-recall")
        _perform_idle_handoff(first, state)

        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"
        assert first._writer_thread is not None and first._writer_thread.is_alive()
        assert second._writer_thread is not None and second._writer_thread.is_alive()
        status = writer_handoff_status(first)
        assert status["last_failure_code"] == "recent_user_activity"
        assert status["release_uncertain"] is False
    finally:
        for provider in (second, first):
            try:
                provider.shutdown()
            except Exception:
                pass


def test_capture_enqueue_racing_idle_fence_aborts_without_losing_work(
    tmp_path, monkeypatch
):
    """A capture begun under the fence must win and commit exactly once."""

    _write_config(tmp_path)
    provider = _provider()
    capture_thread: threading.Thread | None = None
    outcome: list[dict[str, object]] = []
    try:
        _initialize(provider, tmp_path, "capture-fence-race")
        _make_idle(provider)
        import scope_recall.capture as capture_module

        original_quiesce = capture_module.quiesce_writer_for_handoff
        injected = False

        def quiesce_then_enqueue(peer, timeout=3.0):
            nonlocal capture_thread, injected
            original_quiesce(peer, timeout=timeout)
            if injected:
                return
            injected = True

            def submit() -> None:
                outcome.append(
                    enqueue_store(
                        provider,
                        content="The capture racing the idle fence must be committed.",
                        source="turn-user",
                        target="ops",
                        session_id="capture-fence-race",
                    )
                )

            capture_thread = threading.Thread(target=submit, daemon=True)
            capture_thread.start()
            deadline = time.monotonic() + 1.0
            while (
                time.monotonic() < deadline
                and provider._writer_handoff_active_truth_work == 0
            ):
                time.sleep(0.005)
            assert provider._writer_handoff_active_truth_work == 1

        monkeypatch.setattr(
            capture_module, "quiesce_writer_for_handoff", quiesce_then_enqueue
        )
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))
        assert capture_thread is not None
        capture_thread.join(timeout=3.0)

        assert len(outcome) == 1
        accepted = outcome[0]
        assert accepted["status"] == "accepted"
        assert accepted["reason"] == "queued"
        assert accepted["intent_id"] is None
        # The consumer may remove the one item before enqueue_store samples
        # qsize(), so both snapshots are valid.  The flush and row-count checks
        # below prove the durable exactly-once behavior this race exercises.
        assert accepted["depth"] in {0, 1}
        assert int(accepted["capacity"]) >= 1
        assert provider.flush(timeout=3.0) is True
        assert provider._truth_writer_role == "owner"
        assert writer_handoff_status(provider)["last_failure_code"] == (
            "truth_work_active"
        )
        count = provider._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content = ?",
            ("The capture racing the idle fence must be committed.",),
        ).fetchone()[0]
        assert count == 1
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        if capture_thread is not None:
            capture_thread.join(timeout=3.0)
        provider.shutdown()


def test_direct_tool_write_started_before_fence_commits_and_vetoes_handoff(
    tmp_path, monkeypatch
):
    """A tool already inside write_access is ordered before idle demotion."""

    _write_config(tmp_path)
    provider = _provider()
    entered = threading.Event()
    release_tool = threading.Event()
    response: list[dict[str, object]] = []
    tool_thread: threading.Thread | None = None
    handoff_thread: threading.Thread | None = None
    try:
        _initialize(provider, tmp_path, "direct-tool-race")
        _make_idle(provider)
        original_handler = provider._tool_service._handle_store

        def blocked_handler(args):
            entered.set()
            assert release_tool.wait(timeout=3.0)
            return original_handler(args)

        monkeypatch.setattr(provider._tool_service, "_handle_store", blocked_handler)

        def run_tool() -> None:
            response.append(
                json.loads(
                    provider.handle_tool_call(
                        "scope_recall_store",
                        {
                            "content": "The direct tool write won the idle fence race.",
                            "target": "ops",
                        },
                    )
                )
            )

        tool_thread = threading.Thread(target=run_tool, daemon=True)
        tool_thread.start()
        assert entered.wait(timeout=2.0)
        storage = tmp_path / "scope-recall"
        state = process_writer_handoff_state(storage)
        handoff_thread = threading.Thread(
            target=_perform_idle_handoff, args=(provider, state), daemon=True
        )
        handoff_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with state.lock:
                if state.handoff_fenced:
                    break
            time.sleep(0.005)
        with state.lock:
            assert state.handoff_fenced is True

        release_tool.set()
        tool_thread.join(timeout=3.0)
        handoff_thread.join(timeout=3.0)

        assert tool_thread.is_alive() is False
        assert handoff_thread.is_alive() is False
        assert response and response[0].get("id")
        assert provider._truth_writer_role == "owner"
        assert writer_handoff_status(provider)["last_failure_code"] == (
            "recent_user_activity"
        )
        count = provider._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content = ?",
            ("The direct tool write won the idle fence race.",),
        ).fetchone()[0]
        assert count == 1
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        release_tool.set()
        if tool_thread is not None:
            tool_thread.join(timeout=3.0)
        if handoff_thread is not None:
            handoff_thread.join(timeout=3.0)
        provider.shutdown()


def test_activity_after_final_check_linearizes_after_release_and_promotes(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    provider = _provider()
    started = threading.Event()
    completed = threading.Event()
    activity_thread: threading.Thread | None = None
    try:
        _initialize(provider, tmp_path, "final-linearization")
        _make_idle(provider)
        import scope_recall._internal.runtime.writer_handoff as handoff_module

        original_close = handoff_module._close_writer_resources

        def close_while_user_turn_arrives(peers):
            nonlocal activity_thread

            def run_turn() -> None:
                started.set()
                provider.on_turn_start(9, "activity at the handoff linearization point")
                completed.set()

            activity_thread = threading.Thread(target=run_turn, daemon=True)
            activity_thread.start()
            assert started.wait(timeout=1.0)
            time.sleep(0.05)
            assert completed.is_set() is False
            original_close(peers)

        monkeypatch.setattr(
            handoff_module, "_close_writer_resources", close_while_user_turn_arrives
        )
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))
        assert activity_thread is not None
        activity_thread.join(timeout=3.0)

        assert completed.is_set() is True
        assert provider._truth_writer_role == "owner"
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        if activity_thread is not None:
            activity_thread.join(timeout=3.0)
        provider.shutdown()


def test_resource_close_failure_retains_authority_and_never_reports_reader(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    state = None

    class FailingVector:
        def close(self):
            raise RuntimeError("injected_vector_close_failure")

    try:
        _initialize(provider, tmp_path, "close-failure")
        provider._vector_store = FailingVector()
        _make_idle(provider)
        storage = tmp_path / "scope-recall"
        state = process_writer_handoff_state(storage)
        _perform_idle_handoff(provider, state)

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "owner"
        assert status["process_state"] == "OWNER"
        assert status["last_failure_code"] == "writer_resource_close_failed"
        assert status["release_uncertain"] is False
        assert status["operator_action_required"] is False
        assert provider._truth_writer_lease is not None
        assert provider._truth_writer_lease.acquired is True
        assert provider._conn is not None
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert provider._truth_writes_blocked() is False
    finally:
        provider._vector_store = None
        provider.shutdown()


def test_writer_connection_close_failure_restores_healthy_owner(
    tmp_path, monkeypatch
):
    """A pager close failure must retain authority and resume the old writer."""

    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "writer-close-failure")
        _make_idle(provider)
        original_close = provider._close_published_connection
        failed = False

        def fail_once(conn, *, context, reraise=True):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("injected_writer_close_failure")
            return original_close(conn, context=context, reraise=reraise)

        monkeypatch.setattr(provider, "_close_published_connection", fail_once)
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "owner"
        assert status["process_state"] == "OWNER"
        assert status["last_failure_code"] == "writer_resource_close_failed"
        assert status["release_uncertain"] is False
        assert status["operator_action_required"] is False
        assert provider._conn is not None
        assert provider._conn.execute("SELECT 1").fetchone()[0] == 1
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        provider.shutdown()


def test_connection_pin_close_failure_is_retried_before_owner_restore(
    tmp_path, monkeypatch
):
    """A close-then-pin-release error must not strand a fake healthy role."""

    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "connection-pin-close-failure")
        _make_idle(provider)
        connection_lease = provider._conn._truth_writer_lease
        assert connection_lease is not None
        original_release = connection_lease.release
        failed = False

        def fail_once():
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("injected_connection_pin_close_failure")
            original_release()

        monkeypatch.setattr(connection_lease, "release", fail_once)
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "owner"
        assert status["process_state"] == "OWNER"
        assert status["last_failure_code"] == "writer_resource_close_failed"
        assert status["release_uncertain"] is False
        assert status["operator_action_required"] is False
        assert provider._conn is not None
        assert provider._conn.execute("SELECT 1").fetchone()[0] == 1
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        provider.shutdown()


def test_os_lease_release_failure_fences_process_and_requires_restart(
    tmp_path, monkeypatch
):
    """An uncertain OS release can never be published as reader or writer."""

    _write_config(tmp_path)
    provider = _provider()
    lease = None
    original_release = None
    try:
        _initialize(provider, tmp_path, "os-release-failure")
        _make_idle(provider)
        lease = provider._truth_writer_lease
        assert lease is not None and lease.acquired
        original_release = lease.release

        def fail_release():
            raise RuntimeError("injected_os_lease_close_failure")

        monkeypatch.setattr(lease, "release", fail_release)
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "unknown"
        assert provider._writer_handoff_fenced is True
        assert status["process_state"] == "RELEASE_UNCERTAIN"
        assert status["release_uncertain"] is True
        assert status["operator_action_required"] is True
        assert status["last_failure_code"] == "os_lease_release_failed"
        assert provider._conn is None
        assert provider._writer_thread is None
        assert lease.acquired is True
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 0,
        }
        assert provider._truth_writes_blocked() is True
    finally:
        if lease is not None and original_release is not None:
            monkeypatch.setattr(lease, "release", original_release)
        provider.shutdown()


def test_writer_restore_failure_stays_owner_degraded_and_fenced(
    tmp_path, monkeypatch
):
    """Failed recovery while authority is retained requires operator action."""

    _write_config(tmp_path)
    provider = _provider()

    class FailingVector:
        def close(self):
            raise RuntimeError("injected_vector_close_before_restore")

    try:
        _initialize(provider, tmp_path, "writer-restore-failure")
        provider._vector_store = FailingVector()
        monkeypatch.setattr(
            provider,
            "_initialize_writer_runtime",
            lambda: (_ for _ in ()).throw(
                RuntimeError("injected_writer_restore_failure")
            ),
        )
        _make_idle(provider)
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "unknown"
        assert provider._writer_handoff_fenced is True
        assert status["process_state"] == "OWNER_DEGRADED"
        assert status["release_uncertain"] is False
        assert status["operator_action_required"] is True
        assert status["last_failure_code"] == "writer_restore_failed"
        assert provider._conn is None
        assert provider._writer_thread is None
        assert provider._truth_writer_lease is not None
        assert provider._truth_writer_lease.acquired is True
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 0,
        }
        assert provider._truth_writes_blocked() is True
    finally:
        provider._vector_store = None
        provider.shutdown()


def test_process_fence_refuses_new_same_process_join(tmp_path):
    storage = tmp_path / "scope-recall"
    owner = TruthWriterLease(storage, role="provider")
    assert owner.acquire()["status"] == "acquired"
    state = process_writer_handoff_state(storage)
    try:
        with state.lock:
            state.handoff_fenced = True
        blocked = TruthWriterLease(storage, role="provider").acquire()
        assert blocked == {"status": "busy", "scope": "process_handoff", "owner": {}}
    finally:
        with state.lock:
            state.handoff_fenced = False
        owner.release()


def test_truth_work_started_after_process_fence_cannot_inherit_old_authority(
    tmp_path,
):
    _write_config(tmp_path)
    provider = _provider()
    state = None
    try:
        _initialize(provider, tmp_path, "post-fence-work")
        state = process_writer_handoff_state(tmp_path / "scope-recall")
        with state.lock:
            state.handoff_fenced = True
        with active_truth_work(provider):
            assert provider._truth_writes_blocked() is True
    finally:
        if state is not None:
            with state.lock:
                state.handoff_fenced = False
        provider.shutdown()


def test_handoff_thread_cannot_join_a_new_named_holder(tmp_path):
    storage = tmp_path / "scope-recall"
    state = process_writer_handoff_state(storage)
    with state.lock:
        state.handoff_fenced = True
        state.handoff_thread_id = threading.get_ident()
    try:
        result = TruthWriterLease(storage, role="provider").acquire()
        assert result == {"status": "busy", "scope": "process_handoff", "owner": {}}
    finally:
        with state.lock:
            state.handoff_fenced = False
            state.handoff_thread_id = 0


def test_handoff_recovery_pin_cannot_create_missing_authority(tmp_path):
    storage = tmp_path / "scope-recall"
    state = process_writer_handoff_state(storage)
    with state.lock:
        state.handoff_fenced = True
        state.handoff_thread_id = threading.get_ident()
    try:
        result = TruthWriterLease(storage, role="truth_connection").acquire()
        assert result == {
            "status": "busy",
            "scope": "process_handoff_recovery_missing_authority",
            "owner": {},
        }
        assert not (storage / TRUTH_WRITER_LEASE_FILENAME).exists()
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 0,
            "connection_pin_count": 0,
        }
    finally:
        with state.lock:
            state.handoff_fenced = False
            state.handoff_thread_id = 0


def test_only_handoff_thread_may_join_existing_recovery_pin(tmp_path):
    storage = tmp_path / "scope-recall"
    owner = TruthWriterLease(storage, role="provider")
    assert owner.acquire()["status"] == "acquired"
    state = process_writer_handoff_state(storage)
    recovery = TruthWriterLease(storage, role="truth_connection")
    other_result: list[dict[str, object]] = []
    with state.lock:
        state.handoff_fenced = True
        state.handoff_thread_id = threading.get_ident()
    try:
        assert recovery.acquire()["scope"] == "same_process_handoff_recovery"

        def other_thread_join() -> None:
            other_result.append(
                TruthWriterLease(storage, role="truth_connection").acquire()
            )

        thread = threading.Thread(target=other_thread_join)
        thread.start()
        thread.join(timeout=2.0)
        assert other_result == [
            {"status": "busy", "scope": "process_handoff", "owner": {}}
        ]
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        with state.lock:
            state.handoff_fenced = False
            state.handoff_thread_id = 0
        if recovery.acquired:
            recovery.release()
        owner.release()


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _VetoQueue:
    def __init__(self, *, pending: bool = False) -> None:
        self._pending = pending

    def empty(self) -> bool:
        return not self._pending


class _VetoConnection:
    def __init__(self, *, in_transaction: bool = False) -> None:
        self.in_transaction = in_transaction


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("foreground", "foreground_busy"),
        ("queue", "capture_queue_pending"),
        ("capture", "capture_processing"),
        ("digest", "digest_active"),
        ("truth_work", "truth_work_active"),
        ("relation_maintenance", "truth_work_active"),
        ("vector_replay", "truth_work_active"),
        ("purge", "truth_work_active"),
        ("transaction", "transaction_open"),
    ],
)
def test_every_busy_surface_vetoes_idle_handoff(condition, expected):
    now = time.monotonic()
    provider = SimpleNamespace(
        _config={"writer_lease": {"idle_release_seconds": 30}},
        _truth_writer_role="owner",
        _shutdown_requested=threading.Event(),
        _writer_handoff_activity_lock=threading.RLock(),
        _writer_handoff_last_user_activity=now - 31,
        _writer_handoff_last_truth_activity=now - 31,
        _writer_handoff_activity_generation=0,
        _writer_handoff_active_truth_work=0,
        _writer_handoff_last_probe=0.0,
        _writer_handoff_fenced=False,
        _foreground_busy_count=0,
        _write_queue=_VetoQueue(),
        _capture_queue_processing=0,
        _journal_digest_thread=None,
        _writer_thread=_AliveThread(),
        _lock=threading.RLock(),
        _conn=_VetoConnection(),
    )
    if condition == "foreground":
        provider._foreground_busy_count = 1
    elif condition == "queue":
        provider._write_queue = _VetoQueue(pending=True)
    elif condition == "capture":
        provider._capture_queue_processing = 1
    elif condition == "digest":
        provider._journal_digest_thread = _AliveThread()
    elif condition in {"truth_work", "relation_maintenance", "vector_replay", "purge"}:
        provider._writer_handoff_active_truth_work = 1
    elif condition == "transaction":
        provider._conn = _VetoConnection(in_transaction=True)

    assert _idle_veto(provider, now=now, writer_may_be_stopped=False) == expected


def test_quiesce_failure_restores_owner_without_releasing_authority(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "quiesce-failure")
        _make_idle(provider)
        import scope_recall.capture as capture_module

        monkeypatch.setattr(
            capture_module,
            "quiesce_writer_for_handoff",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected_quiesce_timeout")
            ),
        )
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        assert provider._truth_writer_role == "owner"
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
        status = writer_handoff_status(provider)
        assert status["last_failure_code"] == "quiesce_failed"
        assert status["release_uncertain"] is False
    finally:
        provider.shutdown()


def test_abort_resume_failure_is_owner_degraded_and_logs_only_fixed_codes(
    tmp_path, monkeypatch, caplog
):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "resume-failure-privacy")
        _make_idle(provider)
        import scope_recall.capture as capture_module

        original_quiesce = capture_module.quiesce_writer_for_handoff

        def quiesce_then_veto(peer, timeout=3.0):
            original_quiesce(peer, timeout=timeout)
            note_user_activity(provider)

        monkeypatch.setattr(
            capture_module, "quiesce_writer_for_handoff", quiesce_then_veto
        )
        monkeypatch.setattr(
            capture_module,
            "resume_writer_after_handoff",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError(str(tmp_path / "private" / "resume.sqlite3"))
            ),
        )
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "unknown"
        assert provider._writer_handoff_fenced is True
        assert status["process_state"] == "OWNER_DEGRADED"
        assert status["last_handoff_failure_code"] == "writer_resume_failed"
        assert status["release_uncertain"] is False
        assert status["operator_action_required"] is True
        assert provider._truth_writer_lease is not None
        assert provider._truth_writer_lease.acquired is True
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
        assert str(tmp_path) not in caplog.text
        assert "resume.sqlite3" not in caplog.text
        assert "Traceback" not in caplog.text
    finally:
        provider.shutdown()


def test_resume_waits_for_prior_sentinel_consumer_before_starting_replacement(
    monkeypatch,
):
    import scope_recall.capture as capture_module

    class PriorThread:
        def __init__(self) -> None:
            self.alive = True
            self.join_calls = 0

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float) -> None:
            assert timeout == 3.0
            self.join_calls += 1
            self.alive = False

    prior = PriorThread()
    stop = threading.Event()
    stop.set()
    maintenance_stop = threading.Event()
    maintenance_stop.set()
    provider = SimpleNamespace(
        _shutdown_requested=threading.Event(),
        _stop=stop,
        _maintenance_stop=maintenance_stop,
        _writer_thread=prior,
        _write_queue=_VetoQueue(),
    )

    def start_replacement(current) -> None:
        assert current._writer_thread is None
        assert current._stop.is_set() is False
        current._writer_thread = _AliveThread()

    monkeypatch.setattr(capture_module, "start_writer", start_replacement)
    capture_module.resume_writer_after_handoff(provider)

    assert prior.join_calls == 1
    assert provider._writer_thread is not prior
    assert provider._writer_thread.is_alive()
    assert provider._maintenance_stop.is_set() is False


def test_read_only_reopen_failure_never_resurrects_released_writer(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "reader-reopen-failure")
        _make_idle(provider)
        provider_module = sys.modules[type(provider).__module__]

        monkeypatch.setattr(
            provider_module,
            "open_readonly_truth_connection",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected_reader_reopen_failure")
            ),
        )
        storage = tmp_path / "scope-recall"
        _perform_idle_handoff(provider, process_writer_handoff_state(storage))

        assert provider._truth_writer_role == "reader"
        assert provider._truth_writer_lease is None
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 0,
            "connection_pin_count": 0,
        }
        status = writer_handoff_status(provider)
        assert status["process_state"] == "READER_DEGRADED"
        assert status["last_failure_code"] == "reader_reopen_failed"
        assert status["operator_action_required"] is True
        assert status["release_uncertain"] is False
    finally:
        provider.shutdown()


def test_failure_telemetry_never_exposes_exception_text_or_local_path(tmp_path):
    _write_config(tmp_path)
    provider = _provider()

    class LeakyVector:
        def close(self):
            raise RuntimeError(str(tmp_path / "private" / "truth.sqlite3"))

    try:
        _initialize(provider, tmp_path, "content-free-failure")
        provider._vector_store = LeakyVector()
        _make_idle(provider)
        _perform_idle_handoff(
            provider, process_writer_handoff_state(tmp_path / "scope-recall")
        )

        status = writer_handoff_status(provider)
        serialized = json.dumps(status, sort_keys=True)
        assert status["last_handoff_failure_code"] == (
            "writer_resource_close_failed"
        )
        assert str(tmp_path) not in serialized
        assert "truth.sqlite3" not in serialized
        assert "content-free-failure" not in serialized
    finally:
        provider._vector_store = None
        provider.shutdown()


def test_successful_promotion_clears_recoverable_reader_degradation(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "reader-degraded-recovery")
        _make_idle(provider)
        storage = tmp_path / "scope-recall"
        state = process_writer_handoff_state(storage)
        _perform_idle_handoff(provider, state)
        assert provider._truth_writer_role == "reader"
        with state.lock:
            state.state = "READER_DEGRADED"
            state.operator_action_required = True
            state.last_handoff_failure_code = "reader_initialization_failed"

        provider.on_turn_start(4, "recover the degraded reader")

        status = writer_handoff_status(provider)
        assert provider._truth_writer_role == "owner"
        assert status["process_state"] == "OWNER"
        assert status["operator_action_required"] is False
        assert status["release_uncertain"] is False
        assert status["last_handoff_failure_code"] == ""
        assert status["last_failure_code"] == ""
        assert provider._conn is not None
        assert provider._writer_thread is not None
        assert provider._writer_thread.is_alive()
        assert truth_writer_process_snapshot(storage) == {
            "same_process_holder_count": 1,
            "connection_pin_count": 1,
        }
    finally:
        provider.shutdown()


def test_twenty_reader_writer_round_trips_do_not_leak_process_authority(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "round-trip")
        storage = tmp_path / "scope-recall"
        for turn in range(1, 21):
            _make_idle(provider)
            _perform_idle_handoff(provider, process_writer_handoff_state(storage))
            assert provider._truth_writer_role == "reader"
            assert provider._writer_thread is None
            assert provider._write_queue.empty()
            assert truth_writer_process_snapshot(storage) == {
                "same_process_holder_count": 0,
                "connection_pin_count": 0,
            }
            assert not (storage / TRUTH_WRITER_LEASE_INFO_FILENAME).exists()

            provider.on_turn_start(turn, "bounded round-trip probe")
            assert provider._truth_writer_role == "owner"
            assert provider._writer_thread is not None
            assert provider._writer_thread.is_alive()
            assert truth_writer_process_snapshot(storage) == {
                "same_process_holder_count": 1,
                "connection_pin_count": 1,
            }
        assert sum(
            thread.name == "scope-recall-writer" and thread.is_alive()
            for thread in threading.enumerate()
        ) == 1
    finally:
        provider.shutdown()


def test_stats_exposes_content_free_writer_handoff_observability(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "stats")
        handoff = provider._stats_payload()["truth_writer"]["handoff"]
        assert {
            "enabled",
            "writer_role",
            "writer_lease_scope",
            "idle_release_enabled",
            "idle_release_seconds",
            "user_idle_seconds",
            "truth_idle_seconds",
            "last_user_activity_age_seconds",
            "last_truth_activity_age_seconds",
            "active_truth_work",
            "process_state",
            "handoff_generation",
            "successful_handoff_count",
            "last_handoff_at",
            "last_reason_code",
            "last_handoff_reason_code",
            "last_failure_code",
            "last_handoff_failure_code",
            "release_uncertain",
            "operator_action_required",
            "same_process_holder_count",
            "connection_pin_count",
            "demotion_in_progress",
        } <= set(handoff)
        for key in (
            "user_idle_seconds",
            "truth_idle_seconds",
            "last_user_activity_age_seconds",
            "last_truth_activity_age_seconds",
        ):
            assert type(handoff[key]) in {int, float}
            assert handoff[key] >= 0
        assert (
            handoff["last_user_activity_age_seconds"]
            == handoff["user_idle_seconds"]
        )
        assert (
            handoff["last_truth_activity_age_seconds"]
            == handoff["truth_idle_seconds"]
        )
        serialized = json.dumps(handoff, sort_keys=True)
        assert str(tmp_path) not in serialized
        assert "handoff-user" not in serialized
        assert "handoff-chat" not in serialized
    finally:
        provider.shutdown()


@pytest.mark.parametrize("value", [0, 30, 1800, 86_400])
def test_idle_release_config_accepts_disabled_or_bounded_values(tmp_path, value):
    cleaned, errors = validate_config_override(
        {"writer_lease": {"idle_release_seconds": value}},
        DEFAULT_CONFIG,
        path=tmp_path / "config.json",
    )
    assert errors == []
    assert cleaned["writer_lease"]["idle_release_seconds"] == value


@pytest.mark.parametrize("value", [-1, 1, 29, 86_401, float("inf")])
def test_idle_release_config_rejects_ambiguous_or_unbounded_values(tmp_path, value):
    cleaned, errors = validate_config_override(
        {"writer_lease": {"idle_release_seconds": value}},
        DEFAULT_CONFIG,
        path=tmp_path / "config.json",
    )
    assert "writer_lease" not in cleaned
    assert len(errors) == 1
    assert errors[0]["kind"] == "invalid_value"


def test_user_activity_generation_veto_is_content_free(tmp_path):
    _write_config(tmp_path)
    provider = _provider()
    try:
        _initialize(provider, tmp_path, "activity")
        before = writer_handoff_status(provider)
        note_user_activity(provider)
        after = writer_handoff_status(provider)
        assert after["user_idle_seconds"] <= before["user_idle_seconds"]
        serialized = json.dumps(after, sort_keys=True)
        assert str(tmp_path) not in serialized
        assert "handoff-user" not in serialized
        assert "handoff-chat" not in serialized
    finally:
        provider.shutdown()
