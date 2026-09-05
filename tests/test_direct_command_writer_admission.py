"""Direct command calls must share the tool writer-admission boundary."""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.memory import load_memory_provider

from scope_recall import memory_ops, write_kernel
from scope_recall.fact_actions import EvolutionAction, EvolutionProposal
from scope_recall._internal.application.memory_commands import (
    ArchiveMemoriesRequest,
    DedupeMemoriesRequest,
    DeleteMemoriesRequest,
    DeleteMemoriesResult,
    FactOwnedMemoryIdsRequest,
    FeedbackMemoryRequest,
    GovernMemoriesRequest,
    MergeMemoriesRequest,
    PrivacyPurgeRequest,
    ReviewMemoryCandidateRequest,
)
from scope_recall._internal.runtime.command_adapter import ProviderCommandAdapter
from scope_recall._internal.runtime.tool_port import ProviderToolRuntimeAdapter
from scope_recall._internal.runtime.writer_handoff import (
    _idle_veto,
    current_truth_work_started_before_fence,
    idle_release_seconds,
)
from scope_recall.memory_mutation import (
    MemoryMutationService,
    MemoryMutationTransactionError,
)
from scope_recall.tooling import ScopeRecallToolService
from scope_recall.write_kernel import (
    COMMAND_CAPTURE_BARRIER_MISSING,
    WRITE_AUTHORITY_BUSY,
    command_write_access,
)
from scope_recall.writer_lease import process_writer_handoff_state


class _OwnedRLock:
    """Small observable RLock used to assert command lock classification."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner = 0
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            ident = threading.get_ident()
            if self._owner == ident:
                self._depth += 1
            else:
                self._owner = ident
                self._depth = 1
        return acquired

    def release(self) -> None:
        if self._owner != threading.get_ident() or self._depth <= 0:
            raise RuntimeError("cannot release an un-owned test lock")
        self._depth -= 1
        if self._depth == 0:
            self._owner = 0
        self._lock.release()

    def __enter__(self) -> _OwnedRLock:
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()

    @property
    def owned(self) -> bool:
        return self._owner == threading.get_ident() and self._depth > 0


class _CommandHost:
    """Minimum structural host for command-boundary classification tests."""

    def __init__(self, *, writer: bool = True) -> None:
        self._truth_writer_role = "owner" if writer else "reader"
        self._writer_handoff_fenced = False
        self._shutdown_requested = threading.Event()
        self._writer_lifecycle_lock = _OwnedRLock()
        self._capture_submission_lock = _OwnedRLock()
        self._writer_handoff_activity_lock = threading.RLock()
        self._storage_dir = None
        self._conn = None
        self._config: dict[str, object] = {}

    def _truth_writes_blocked(self) -> bool:
        return (
            self._shutdown_requested.is_set()
            or self._truth_writer_role != "owner"
            or self._writer_handoff_fenced
        )


class _FactCommandHost(_CommandHost):
    """Structural tool-port host with an inert query connection."""

    def __init__(self, *, writer: bool = True) -> None:
        super().__init__(writer=writer)
        self._lock = threading.RLock()
        self._conn = object()
        self._scope = SimpleNamespace(
            platform="cli",
            user_id="fact-user",
            chat_id="fact-chat",
            thread_id="fact-thread",
            gateway_session_key="fact-gateway",
            agent_identity="fact-agent",
            agent_workspace="fact-workspace",
        )
        self._session_id = "fact-session"
        self._scope_id = "fact-scope"
        self._shared_scope_id = "fact-shared"
        self._shared_pool_scope_id = "fact-pool"
        self._accessible_scope_ids = [self._scope_id]
        self._writable_scope_ids = [self._scope_id]

    def _require_conn(self) -> object:
        return self._conn


def _write_config(hermes_home: Path) -> None:
    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "relation_extraction_enabled": False,
                "writer_lease": {"idle_release_seconds": 30},
            }
        ),
        encoding="utf-8",
    )


@contextmanager
def _initialized_provider(tmp_path: Path, session_id: str) -> Iterator[Any]:
    _write_config(tmp_path)
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    provider.initialize(
        session_id,
        hermes_home=str(tmp_path),
        platform="cli",
        user_id="direct-command-user",
        chat_id="direct-command-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )
    try:
        yield provider
    finally:
        state = process_writer_handoff_state(tmp_path / "scope-recall")
        with state.lock:
            state.handoff_fenced = False
        provider._writer_handoff_fenced = False
        provider.shutdown()


def _store_seed(provider: Any, content: str) -> str:
    memory_id, stored, _message = provider.store_now(
        content=content,
        source="manual",
        target="memory",
        session_id="direct-command-seed",
    )
    assert stored is True
    return str(memory_id)


def _assert_admitted_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_target: object,
    patch_name: str,
    invoke: Callable[[ProviderCommandAdapter], object],
    result: object,
    capture_barrier: bool,
) -> None:
    host = _CommandHost()
    adapter = ProviderCommandAdapter(host)
    observed = False

    def operation(*_args: object, **_kwargs: object) -> object:
        nonlocal observed
        observed = True
        assert host._writer_handoff_active_truth_work == 1
        assert host._writer_lifecycle_lock.owned is True
        assert host._capture_submission_lock.owned is capture_barrier
        return result

    monkeypatch.setattr(patch_target, patch_name, operation)
    assert invoke(adapter) == result
    assert observed is True
    assert host._writer_handoff_active_truth_work == 0
    assert host._writer_lifecycle_lock.owned is False
    assert host._capture_submission_lock.owned is False


def _execute_fact_proposal(
    adapter: ProviderToolRuntimeAdapter,
    *,
    dry_run: bool,
) -> object:
    return adapter.execute_fact_proposal(
        proposal=EvolutionProposal(action=EvolutionAction.NOOP),
        lane="tool",
        run_id="fact-run",
        source_key="fact-source",
        trusted_scope_id="fact-scope",
        writable_scope_ids=("fact-scope",),
        actor="fact-actor",
        source="tool-test",
        target="memory",
        content="Bounded fact proposal.",
        metadata={},
        dry_run=dry_run,
        provenance_refs=(),
    )


def test_direct_command_update_started_before_fence_commits_and_vetoes_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _initialized_provider(tmp_path, "direct-update-pre-fence") as provider:
        memory_id = _store_seed(provider, "Original direct command content.")
        gateway = provider._composition.command_port._gateway
        gateway_module = importlib.import_module(type(gateway).__module__)
        gateway_memory_ops = gateway_module.memory_ops
        original_update = gateway_memory_ops.update_memory
        entered = threading.Event()
        release = threading.Event()
        results: list[tuple[bool, str, str]] = []
        failures: list[BaseException] = []

        def blocked_update(*args: object, **kwargs: object) -> tuple[bool, str, str]:
            entered.set()
            assert provider._writer_handoff_active_truth_work == 1
            assert current_truth_work_started_before_fence(provider) is True
            if not release.wait(timeout=5.0):
                raise TimeoutError("test did not release the admitted update")
            return original_update(*args, **kwargs)

        monkeypatch.setattr(gateway_memory_ops, "update_memory", blocked_update)

        def run_update() -> None:
            try:
                results.append(
                    provider.command_update_memory(
                        memory_id,
                        "Updated after the handoff fence was raised.",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        worker = threading.Thread(target=run_update)
        worker.start()
        try:
            assert entered.wait(timeout=3.0)
            old = time.monotonic() - idle_release_seconds(provider) - 1.0
            with provider._writer_handoff_activity_lock:
                provider._writer_handoff_last_user_activity = old
                provider._writer_handoff_last_truth_activity = old
            assert (
                _idle_veto(
                    provider,
                    now=time.monotonic(),
                    writer_may_be_stopped=False,
                )
                == "truth_work_active"
            )

            state = process_writer_handoff_state(tmp_path / "scope-recall")
            with state.lock:
                state.handoff_fenced = True
            release.set()
            worker.join(timeout=5.0)
            assert not worker.is_alive()
            assert failures == []
            assert results and results[0][0] is True
            row = provider._require_conn().execute(
                "SELECT content FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            assert row is not None
            assert row["content"] == "Updated after the handoff fence was raised."
        finally:
            release.set()
            worker.join(timeout=5.0)


def test_direct_command_update_started_after_fence_is_rejected(
    tmp_path: Path,
) -> None:
    with _initialized_provider(tmp_path, "direct-update-post-fence") as provider:
        original = "A post-fence direct update must not change this row."
        memory_id = _store_seed(provider, original)
        conn = provider._require_conn()
        before_changes = conn.total_changes
        state = process_writer_handoff_state(tmp_path / "scope-recall")
        with state.lock:
            state.handoff_fenced = True

        with pytest.raises(RuntimeError, match=f"^{WRITE_AUTHORITY_BUSY}$"):
            provider.command_update_memory(memory_id, "Forbidden replacement.")

        row = conn.execute(
            "SELECT content FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        assert row is not None
        assert row["content"] == original
        assert conn.total_changes == before_changes


def test_direct_command_merge_preserves_capture_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"merged": True}
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="merge_memories",
        invoke=lambda adapter: adapter.merge(
            MergeMemoriesRequest("target", ("source",))
        ),
        result=expected,
        capture_barrier=True,
    )


def test_direct_command_archive_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"archived": 1}
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="archive_memories",
        invoke=lambda adapter: adapter.archive(ArchiveMemoriesRequest(("memory",))),
        result=expected,
        capture_barrier=True,
    )


def test_direct_command_delete_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = DeleteMemoriesResult(
        requested_ids=("memory",),
        deleted_ids=("memory",),
        skipped_ids=(),
        deleted_count=1,
        vector_pending=False,
        companion_erasure_pending=False,
        data_retained=False,
        mutation_applied=True,
    )
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="delete_memories_result",
        invoke=lambda adapter: adapter.delete(DeleteMemoriesRequest(("memory",))),
        result=expected,
        capture_barrier=True,
    )


def test_direct_command_feedback_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"feedback": True}
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="feedback_memory",
        invoke=lambda adapter: adapter.feedback(
            FeedbackMemoryRequest(memory_id="memory", rating="up")
        ),
        result=expected,
        capture_barrier=False,
    )


def test_direct_command_govern_apply_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"governed": 1}
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="govern_memories",
        invoke=lambda adapter: adapter.govern(GovernMemoriesRequest(dry_run=False)),
        result=expected,
        capture_barrier=False,
    )


def test_direct_command_dedupe_apply_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"deduped": 1}
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="dedupe_memories",
        invoke=lambda adapter: adapter.dedupe(DedupeMemoriesRequest(dry_run=False)),
        result=expected,
        capture_barrier=True,
    )


def test_direct_command_repair_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"repaired": True}
    _assert_admitted_call(
        monkeypatch,
        patch_target=memory_ops,
        patch_name="repair_vector",
        invoke=lambda adapter: adapter.repair(),
        result=expected,
        capture_barrier=False,
    )


def test_direct_command_purge_deny_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall import privacy_purge

    expected = {"status": "denied"}
    _assert_admitted_call(
        monkeypatch,
        patch_target=privacy_purge,
        patch_name="run_privacy_purge",
        invoke=lambda adapter: adapter.purge(
            PrivacyPurgeRequest(
                action="deny",
                operation_id="operation",
                confirmation="confirmation",
            )
        ),
        result=expected,
        capture_barrier=True,
    )


def test_direct_command_purge_erase_is_accounted_as_truth_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall import privacy_purge

    expected = {"status": "completed"}
    _assert_admitted_call(
        monkeypatch,
        patch_target=privacy_purge,
        patch_name="run_privacy_purge",
        invoke=lambda adapter: adapter.purge(
            PrivacyPurgeRequest(
                action="erase",
                operation_id="operation",
                confirmation="confirmation",
            )
        ),
        result=expected,
        capture_barrier=True,
    )


def test_direct_command_read_only_modes_do_not_require_write_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall import privacy_purge

    host = _CommandHost(writer=False)
    adapter = ProviderCommandAdapter(host)
    observed: list[str] = []

    def assert_read_only(label: str, result: object) -> Callable[..., object]:
        def operation(*_args: object, **_kwargs: object) -> object:
            observed.append(label)
            assert getattr(host, "_writer_handoff_active_truth_work", 0) == 0
            assert host._writer_lifecycle_lock.owned is False
            assert host._capture_submission_lock.owned is False
            return result

        return operation

    monkeypatch.setattr(
        memory_ops,
        "govern_memories",
        assert_read_only("govern", {"read_only": True}),
    )
    monkeypatch.setattr(
        memory_ops,
        "dedupe_memories",
        assert_read_only("dedupe", {"read_only": True}),
    )
    monkeypatch.setattr(
        memory_ops,
        "fact_owned_memory_ids",
        assert_read_only("fact_owned", ["memory"]),
    )
    monkeypatch.setattr(
        privacy_purge,
        "run_privacy_purge",
        assert_read_only("purge", {"read_only": True}),
    )

    assert adapter.govern(GovernMemoriesRequest(dry_run=True)) == {
        "read_only": True
    }
    assert adapter.dedupe(DedupeMemoriesRequest(dry_run=True)) == {
        "read_only": True
    }
    assert adapter.fact_owned(FactOwnedMemoryIdsRequest(("memory",))) == [
        "memory"
    ]
    assert adapter.purge(PrivacyPurgeRequest(action="plan")) == {
        "read_only": True
    }
    assert adapter.purge(PrivacyPurgeRequest(action="status")) == {
        "read_only": True
    }
    assert observed == ["govern", "dedupe", "fact_owned", "purge", "purge"]


def test_candidate_apply_uses_existing_writer_and_capture_barrier(monkeypatch):
    _assert_admitted_call(
        monkeypatch, patch_target=memory_ops, patch_name="review_memory_candidate",
        invoke=lambda adapter: adapter.review_candidate(ReviewMemoryCandidateRequest(
            memory_id="candidate", action="promote", dry_run=False,
        )), result={"applied": True}, capture_barrier=True,
    )


def test_candidate_plan_is_read_only_but_apply_requires_writer(monkeypatch):
    adapter = ProviderCommandAdapter(_CommandHost(writer=False))
    seen = []

    def review(*args, **kwargs):
        seen.append(kwargs["dry_run"])
        return {"dry_run": True}

    monkeypatch.setattr(memory_ops, "review_memory_candidate", review)
    assert adapter.review_candidate(ReviewMemoryCandidateRequest(
        memory_id="candidate", action="archive",
    )) == {"dry_run": True}
    with pytest.raises(RuntimeError, match=WRITE_AUTHORITY_BUSY):
        adapter.review_candidate(ReviewMemoryCandidateRequest(
            memory_id="candidate", action="archive", dry_run=False,
        ))
    assert seen == [True]


def test_memory_mutation_service_rejects_fenced_unadmitted_mutation() -> None:
    conn = sqlite3.connect(":memory:")

    class FencedHost:
        _lock = threading.RLock()
        require_called = False

        @staticmethod
        def _truth_writes_blocked() -> bool:
            return True

        def _require_conn(self) -> sqlite3.Connection:
            self.require_called = True
            return conn

    host = FencedHost()
    try:
        with pytest.raises(
            MemoryMutationTransactionError,
            match="requires admitted truth-writer authority",
        ):
            with MemoryMutationService(host).transaction():
                pytest.fail("fenced mutation unexpectedly entered a transaction")
        assert host.require_called is False
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.parametrize("role", ["reader", "unknown"])
def test_memory_mutation_service_rejects_unclassified_non_owner(role: str) -> None:
    """A host cannot become a writer by omitting the optional blocked probe."""

    conn = sqlite3.connect(":memory:")

    class UnclassifiedHost:
        _lock = threading.RLock()
        _truth_writer_role = role
        require_called = False

        def _require_conn(self) -> sqlite3.Connection:
            self.require_called = True
            return conn

    host = UnclassifiedHost()
    try:
        for admit in (False, True):
            authority = (
                write_kernel._admitted_truth_mutation(host)
                if admit
                else nullcontext()
            )
            with authority:
                with pytest.raises(
                    MemoryMutationTransactionError,
                    match="requires admitted truth-writer authority",
                ):
                    with MemoryMutationService(host).transaction():
                        pytest.fail("a non-owner entered a durable transaction")
        assert host.require_called is False
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_private_admission_surface_rejects_public_bool_and_wrong_token() -> None:
    capture_module = importlib.import_module(f"{memory_ops.__package__}.capture")
    host = _CommandHost()
    store_kwargs = {
        "content": "A rejected accepted-capture bypass.",
        "source": "manual",
        "target": "memory",
        "session_id": "private-admission",
    }

    assert "admitted_truth_mutation" not in write_kernel.__all__
    assert "truth_mutation_is_admitted" not in write_kernel.__all__
    assert not hasattr(write_kernel, "admitted_truth_mutation")
    assert not hasattr(write_kernel, "truth_mutation_is_admitted")
    with pytest.raises(TypeError, match="complete_accepted_capture"):
        capture_module.store_now(
            host,
            complete_accepted_capture=True,
            **store_kwargs,
        )
    with pytest.raises(RuntimeError, match=f"^{WRITE_AUTHORITY_BUSY}$"):
        capture_module.store_now(
            host,
            _accepted_capture_token=object(),
            **store_kwargs,
        )
    assert getattr(host, "_writer_handoff_active_truth_work", 0) == 0


def test_accepted_capture_commits_after_shutdown_and_handoff_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item accepted before fencing retains a bounded mutation admission."""

    with _initialized_provider(tmp_path, "accepted-capture-after-fence") as provider:
        capture_adapter_module = importlib.import_module(
            type(provider._composition.capture._gateway).__module__
        )
        capture_module = importlib.import_module(
            capture_adapter_module.flush_writer.__module__
        )
        capture_mutation_module = importlib.import_module(
            f"{capture_module.__package__}.memory_mutation"
        )
        original_store_now = capture_module.store_now
        entered = threading.Event()
        release = threading.Event()
        token_after_store: list[bool] = []
        state = process_writer_handoff_state(tmp_path / "scope-recall")

        def delayed_store_now(*args: object, **kwargs: object) -> object:
            if kwargs.get("_accepted_capture_token") is (
                capture_module._ACCEPTED_CAPTURE_DRAIN_TOKEN
            ):
                entered.set()
                if not release.wait(timeout=5.0):
                    raise TimeoutError("test did not release the accepted capture")
                try:
                    return original_store_now(*args, **kwargs)
                finally:
                    token_after_store.append(
                        capture_module.write_kernel_mod._truth_mutation_is_admitted(
                            provider
                        )
                    )
            return original_store_now(*args, **kwargs)

        monkeypatch.setattr(capture_module, "store_now", delayed_store_now)
        try:
            result = capture_module.enqueue_store(
                provider,
                content="An accepted capture must survive the published fence.",
                source="turn-user",
                target="memory",
                session_id="accepted-capture-after-fence",
            )
            assert result["status"] == "accepted"
            assert entered.wait(timeout=3.0)

            with state.lock:
                state.handoff_fenced = True
            provider._writer_handoff_fenced = True
            provider._shutdown_requested.set()
            assert provider._truth_writes_blocked() is True

            release.set()
            assert provider.flush(timeout=5.0) is True
            row = provider._require_conn().execute(
                "SELECT COUNT(*) FROM memories WHERE content = ?",
                ("An accepted capture must survive the published fence.",),
            ).fetchone()
            assert row is not None
            assert int(row[0]) == 1
            assert token_after_store == [False]

            with pytest.raises(
                RuntimeError,
                match="requires admitted truth-writer authority",
            ):
                with capture_mutation_module.MemoryMutationService(
                    provider
                ).transaction():
                    pytest.fail("the post-capture unadmitted mutation was accepted")
        finally:
            release.set()
            provider._shutdown_requested.clear()
            provider._writer_handoff_fenced = False
            with state.lock:
                state.handoff_fenced = False


def test_admitted_truth_mutation_token_is_cleared_after_exception() -> None:
    host = _CommandHost()

    with pytest.raises(LookupError, match="mutation-body-failed"):
        with write_kernel._admitted_truth_mutation(host):
            assert write_kernel._truth_mutation_is_admitted(host) is True
            raise LookupError("mutation-body-failed")

    assert write_kernel._truth_mutation_is_admitted(host) is False


def test_public_store_reuses_one_active_truth_unit_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _initialized_provider(tmp_path, "store-single-admission") as provider:
        gateway = provider._composition.command_port._gateway
        gateway_module = importlib.import_module(type(gateway).__module__)
        gateway_memory_ops = gateway_module.memory_ops
        original_mark_conflicts = gateway_memory_ops._mark_conflicts_for_memory
        active_counts: list[int] = []

        def observed_mark_conflicts(*args: object, **kwargs: object) -> object:
            active_counts.append(provider._writer_handoff_active_truth_work)
            return original_mark_conflicts(*args, **kwargs)

        monkeypatch.setattr(
            gateway_memory_ops,
            "_mark_conflicts_for_memory",
            observed_mark_conflicts,
        )
        generation_before = provider._writer_handoff_activity_generation

        memory_id, stored, _outcome = provider.store_now(
            content="A normal store uses one reentrant command admission.",
            source="manual",
            target="memory",
            session_id="store-single-admission",
        )

        assert memory_id
        assert stored is True
        assert active_counts == [1]
        assert provider._writer_handoff_active_truth_work == 0
        assert (
            provider._writer_handoff_activity_generation - generation_before
        ) == 2


def test_govern_acquires_query_connection_under_query_lock() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT,
            source TEXT,
            target TEXT,
            content TEXT,
            updated_at TEXT,
            metadata TEXT
        )
        """
    )

    class GovernHost:
        def __init__(self) -> None:
            self.lock = _OwnedRLock()
            self.connection_calls = 0

        def query_lock(self) -> _OwnedRLock:
            return self.lock

        def query_connection(self) -> sqlite3.Connection:
            assert self.lock.owned is True
            self.connection_calls += 1
            return conn

    host = GovernHost()
    try:
        result = memory_ops.govern_memories(
            host,
            dry_run=True,
            scope_only=False,
        )
        assert result["total"] == 0
        assert host.connection_calls == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("scope_recall_govern", {}),
        ("scope_recall_govern", {"dry_run": True}),
        ("scope_recall_dedupe", {}),
        ("scope_recall_dedupe", {"dry_run": True}),
    ],
)
def test_tool_service_dry_run_governance_does_not_request_write_access(
    tool_name: str,
    args: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ScopeRecallToolService(_CommandHost(writer=False))
    write_access_calls: list[bool] = []

    @contextmanager
    def fail_if_requested(*, capture_barrier: bool) -> Iterator[bool]:
        write_access_calls.append(capture_barrier)
        raise AssertionError("reader dry-run requested write access")
        yield False  # pragma: no cover - contextmanager shape only

    monkeypatch.setattr(service._port, "write_access", fail_if_requested)
    monkeypatch.setattr(
        service,
        "_invoke_handler",
        lambda *_args: '{"read_only": true}',
    )

    assert json.loads(service.handle(tool_name, args)) == {"read_only": True}
    assert write_access_calls == []


def test_fact_proposal_dry_run_is_read_only_on_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall import fact_evolution

    host = _FactCommandHost(writer=False)
    host._writer_handoff_fenced = True
    adapter = ProviderToolRuntimeAdapter(host)
    expected = object()
    calls = 0

    def execute_pipeline(*_args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        assert kwargs["dry_run"] is True
        assert getattr(host, "_writer_handoff_active_truth_work", 0) == 0
        assert host._writer_lifecycle_lock.owned is False
        assert write_kernel._truth_mutation_is_admitted(host) is False
        return expected

    monkeypatch.setattr(
        fact_evolution,
        "execute_pipeline_proposal",
        execute_pipeline,
    )

    assert _execute_fact_proposal(adapter, dry_run=True) is expected
    assert calls == 1


def test_fact_proposal_apply_reenters_command_gate_and_rejects_reader_or_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scope_recall import fact_evolution

    host = _FactCommandHost()
    adapter = ProviderToolRuntimeAdapter(host)
    expected = object()
    calls = 0

    def execute_pipeline(*_args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        assert kwargs["dry_run"] is False
        assert host._writer_handoff_active_truth_work == 1
        assert host._writer_lifecycle_lock.owned is True
        assert write_kernel._truth_mutation_is_admitted(host) is True
        return expected

    monkeypatch.setattr(
        fact_evolution,
        "execute_pipeline_proposal",
        execute_pipeline,
    )

    with command_write_access(host, user_initiated=True):
        assert _execute_fact_proposal(adapter, dry_run=False) is expected
        assert host._writer_handoff_active_truth_work == 1
    assert calls == 1

    host._truth_writer_role = "reader"
    with pytest.raises(RuntimeError, match=f"^{WRITE_AUTHORITY_BUSY}$"):
        _execute_fact_proposal(adapter, dry_run=False)
    assert calls == 1

    host._truth_writer_role = "owner"
    host._writer_handoff_fenced = True
    with pytest.raises(RuntimeError, match=f"^{WRITE_AUTHORITY_BUSY}$"):
        _execute_fact_proposal(adapter, dry_run=False)
    assert calls == 1


def test_nested_command_gate_preserves_barrier_and_propagates_exceptions() -> None:
    host = _CommandHost()

    with command_write_access(host, capture_barrier=True):
        assert host._writer_handoff_active_truth_work == 1
        assert host._capture_submission_lock.owned is True
        with command_write_access(host, capture_barrier=True):
            assert host._writer_handoff_active_truth_work == 1
            assert host._capture_submission_lock.owned is True
    assert host._writer_handoff_active_truth_work == 0
    assert host._capture_submission_lock.owned is False

    with command_write_access(host):
        with pytest.raises(
            RuntimeError,
            match=f"^{COMMAND_CAPTURE_BARRIER_MISSING}$",
        ):
            with command_write_access(host, capture_barrier=True):
                pytest.fail("nested barrier classifier drift was accepted")
        assert host._writer_handoff_active_truth_work == 1

    with pytest.raises(LookupError, match="command-body-failed"):
        with command_write_access(host, capture_barrier=True):
            raise LookupError("command-body-failed")
    assert host._writer_handoff_active_truth_work == 0
    assert host._writer_lifecycle_lock.owned is False
    assert host._capture_submission_lock.owned is False
    assert write_kernel._truth_mutation_is_admitted(host) is False


def test_public_provider_command_surface_uses_unified_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _initialized_provider(tmp_path, "public-command-surface") as provider:
        gateway = provider._composition.command_port._gateway
        gateway_module = importlib.import_module(type(gateway).__module__)
        gateway_memory_ops = gateway_module.memory_ops
        gateway_privacy_purge = importlib.import_module(
            f"{gateway_memory_ops.__package__}.privacy_purge"
        )
        observed: list[str] = []

        def admitted_result(
            label: str,
            result: object,
            *,
            capture_barrier: bool,
        ) -> Callable[..., object]:
            def operation(*_args: object, **_kwargs: object) -> object:
                observed.append(label)
                assert provider._writer_handoff_active_truth_work == 1
                assert provider._writer_lifecycle_lock._is_owned() is True
                assert (
                    provider._capture_submission_lock._is_owned()
                    is capture_barrier
                )
                assert (
                    gateway_module.write_kernel._truth_mutation_is_admitted(provider)
                    is True
                )
                return result

            return operation

        delete_result = DeleteMemoriesResult(
            requested_ids=("delete",),
            deleted_ids=("delete",),
            skipped_ids=(),
            deleted_count=1,
            vector_pending=False,
            companion_erasure_pending=False,
            data_retained=False,
            mutation_applied=True,
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "merge_memories",
            admitted_result("merge", {"merged": True}, capture_barrier=True),
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "archive_memories",
            admitted_result("archive", {"archived": 1}, capture_barrier=True),
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "delete_memories_result",
            admitted_result("delete", delete_result, capture_barrier=True),
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "feedback_memory",
            admitted_result("feedback", {"feedback": True}, capture_barrier=False),
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "govern_memories",
            admitted_result("govern", {"governed": 1}, capture_barrier=False),
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "dedupe_memories",
            admitted_result("dedupe", {"deduped": 1}, capture_barrier=True),
        )
        monkeypatch.setattr(
            gateway_memory_ops,
            "repair_vector",
            admitted_result("repair", {"repaired": True}, capture_barrier=False),
        )
        monkeypatch.setattr(
            gateway_privacy_purge,
            "run_privacy_purge",
            admitted_result("purge", {"purged": True}, capture_barrier=True),
        )

        assert provider.command_merge_memories("target", ["source"]) == {
            "merged": True
        }
        assert provider.command_archive_memories(["archive"]) == {"archived": 1}
        assert provider.command_delete_memories(["delete"]) == 1
        assert provider.command_feedback_memory(
            memory_id="feedback",
            rating="up",
        ) == {"feedback": True}
        assert provider.command_govern_memories(dry_run=False) == {"governed": 1}
        assert provider.command_dedupe_memories(dry_run=False) == {"deduped": 1}
        assert provider.command_repair_vector() == {"repaired": True}
        assert provider._composition.command_port.purge(
            PrivacyPurgeRequest(
                action="erase",
                operation_id="operation",
                confirmation="confirmation",
            )
        ) == {"purged": True}
        assert observed == [
            "merge",
            "archive",
            "delete",
            "feedback",
            "govern",
            "dedupe",
            "repair",
            "purge",
        ]
