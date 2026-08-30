"""Persisted writer-handoff telemetry is bounded, stale-safe, and non-authoritative."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from scope_recall._internal.runtime.writer_handoff import (
    initialize_writer_handoff_activity,
    maybe_schedule_idle_writer_handoff,
    note_user_activity,
    note_writer_promotion_succeeded,
    note_writer_shutdown_succeeded,
    writer_handoff_status,
)
import scope_recall._internal.runtime.writer_handoff as writer_handoff_module
from scope_recall.writer_lease import (
    WRITER_HANDOFF_OBSERVABILITY_FIELDS,
    WRITER_HANDOFF_TELEMETRY_FILENAME,
    WRITER_HANDOFF_TELEMETRY_SCHEMA_VERSION,
    TruthWriterLease,
    process_writer_handoff_state,
    publish_writer_handoff_telemetry,
    read_writer_handoff_telemetry,
    writer_handoff_telemetry_view,
)

ROOT = Path(writer_handoff_module.__file__).resolve().parents[2]
DOCTOR = ROOT / "scripts" / "doctor.py"
DASHBOARD = ROOT / "scripts" / "report.dashboard.py"


def _payload(
    *,
    epoch: str,
    sequence: int,
    observed_at: datetime | None = None,
    writer_role: str = "owner",
) -> dict[str, object]:
    return {
        "schema_version": WRITER_HANDOFF_TELEMETRY_SCHEMA_VERSION,
        "authority_epoch": epoch,
        "event_sequence": sequence,
        "event_kind": "owner_activated",
        "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "fresh_for_seconds": 300.0,
        "writer_role": writer_role,
        "writer_lease_scope": "process-wide-os-lock",
        "idle_release_enabled": True,
        "idle_release_seconds": 1800.0,
        "last_user_activity_age_seconds": 1.25,
        "last_truth_activity_age_seconds": 2.5,
        "same_process_holder_count": 1,
        "connection_pin_count": 1,
        "demotion_in_progress": False,
        "successful_handoff_count": 3,
        "last_handoff_at": "",
        "last_handoff_reason_code": "",
        "last_handoff_failure_code": "",
        "release_uncertain": False,
        "operator_action_required": False,
    }


def _load_dashboard():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_writer_handoff_telemetry_dashboard", DASHBOARD
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_authority_epoch_fences_delayed_old_reader_update(tmp_path):
    storage = tmp_path / "scope-recall"
    first = _payload(epoch="a" * 32, sequence=1)
    second = _payload(epoch="b" * 32, sequence=1)

    assert publish_writer_handoff_telemetry(
        storage, first, claim_authority_epoch=True
    )
    assert publish_writer_handoff_telemetry(
        storage, second, claim_authority_epoch=True
    )

    delayed = {**first, "event_sequence": 2, "event_kind": "handoff_succeeded"}
    assert not publish_writer_handoff_telemetry(
        storage, delayed, claim_authority_epoch=False
    )
    result = read_writer_handoff_telemetry(storage)
    assert result["status"] == "fresh"
    assert result["snapshot"]["authority_epoch"] == "b" * 32


def test_initial_epoch_claim_linearizes_with_final_lease_release(
    monkeypatch, tmp_path
):
    """A final OS-lease release cannot overtake an unpublished epoch claim."""

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    lease = TruthWriterLease(storage, role="provider")
    assert lease.acquire()["status"] == "acquired"
    provider = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config={"writer_lease": {"idle_release_seconds": 1800.0}},
    )
    initialize_writer_handoff_activity(provider, reset=True)
    publish_started = threading.Event()
    allow_publish = threading.Event()
    release_finished = threading.Event()
    errors: list[BaseException] = []
    real_publish = writer_handoff_module.publish_writer_handoff_telemetry

    def blocked_publish(*args, **kwargs):
        assert kwargs.get("claim_authority_epoch") is True
        publish_started.set()
        assert allow_publish.wait(timeout=1.0)
        return real_publish(*args, **kwargs)

    def promote() -> None:
        try:
            note_writer_promotion_succeeded(provider)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def release() -> None:
        try:
            lease.release()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            release_finished.set()

    monkeypatch.setattr(
        writer_handoff_module,
        "publish_writer_handoff_telemetry",
        blocked_publish,
    )
    promotion_thread = threading.Thread(target=promote, daemon=True)
    release_thread = threading.Thread(target=release, daemon=True)
    try:
        promotion_thread.start()
        assert publish_started.wait(timeout=1.0)
        release_thread.start()
        assert not release_finished.wait(timeout=0.1)
        assert lease.acquired is True
        allow_publish.set()
        promotion_thread.join(timeout=1.0)
        release_thread.join(timeout=1.0)
        assert not promotion_thread.is_alive()
        assert not release_thread.is_alive()
        assert errors == []
        assert lease.acquired is False
        snapshot = read_writer_handoff_telemetry(storage)["snapshot"]
        assert snapshot["event_kind"] == "owner_activated"
    finally:
        allow_publish.set()
        promotion_thread.join(timeout=1.0)
        release_thread.join(timeout=1.0)
        if lease.acquired:
            lease.release()


class _ObservedActivityRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner = 0
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._owner and self._owner != threading.get_ident():
            self.waiting.set()
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._owner = threading.get_ident()
        return acquired

    def release(self) -> None:
        self._lock.release()
        self._owner = 0

    def __enter__(self):
        assert self.acquire()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.release()


def test_epoch_claim_never_takes_activity_lock_under_process_state_lock(tmp_path):
    """The claim path follows activity -> state and cannot deadlock handoff."""

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    lease = TruthWriterLease(storage, role="provider")
    assert lease.acquire()["status"] == "acquired"
    provider = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config={"writer_lease": {"idle_release_seconds": 1800.0}},
    )
    initialize_writer_handoff_activity(provider, reset=True)
    activity_lock = _ObservedActivityRLock()
    provider._writer_handoff_activity_lock = activity_lock
    state = process_writer_handoff_state(storage)
    errors: list[BaseException] = []

    def promote() -> None:
        try:
            note_writer_promotion_succeeded(provider)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=promote, daemon=True)
    try:
        with activity_lock:
            thread.start()
            assert activity_lock.waiting.wait(timeout=1.0)
            # Once promotion is waiting for the activity snapshot it must not
            # already own state.lock, otherwise idle handoff can ABBA deadlock.
            assert state.lock.acquire(timeout=0.2)
            state.lock.release()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert errors == []
    finally:
        thread.join(timeout=1.0)
        if lease.acquired:
            lease.release()


def test_delayed_activity_cannot_overwrite_final_shutdown_with_owner_snapshot(
    monkeypatch, tmp_path
):
    """A status captured before shutdown cannot be paired with a later sequence."""

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    lease = TruthWriterLease(storage, role="provider")
    assert lease.acquire()["status"] == "acquired"
    provider = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config={"writer_lease": {"idle_release_seconds": 1800.0}},
    )
    initialize_writer_handoff_activity(provider, reset=True)
    note_writer_promotion_succeeded(provider)
    status_captured = threading.Event()
    allow_activity = threading.Event()
    captured: list[dict[str, object]] = []
    errors: list[BaseException] = []
    real_status = writer_handoff_module.writer_handoff_status
    activity_thread: threading.Thread
    blocked_once = False

    def controlled_status(target):
        nonlocal blocked_once
        status = real_status(target)
        if threading.current_thread() is activity_thread and not blocked_once:
            blocked_once = True
            captured.append(dict(status))
            status_captured.set()
            assert allow_activity.wait(timeout=1.0)
        return status

    def record_activity() -> None:
        try:
            note_user_activity(provider)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    monkeypatch.setattr(
        writer_handoff_module,
        "writer_handoff_status",
        controlled_status,
    )
    activity_thread = threading.Thread(target=record_activity, daemon=True)
    try:
        activity_thread.start()
        assert status_captured.wait(timeout=1.0)
        assert captured[0]["writer_role"] == "owner"
        assert captured[0]["same_process_holder_count"] == 1

        lease.release()
        provider._truth_writer_lease = None
        provider._truth_writer_role = "unknown"
        note_writer_shutdown_succeeded(provider)
        shutdown = read_writer_handoff_telemetry(storage)["snapshot"]
        assert shutdown["writer_role"] == "unknown"
        assert shutdown["same_process_holder_count"] == 0

        allow_activity.set()
        activity_thread.join(timeout=1.0)
        assert not activity_thread.is_alive()
        assert errors == []
        final = read_writer_handoff_telemetry(storage)["snapshot"]
        assert final["writer_role"] == "unknown"
        assert final["same_process_holder_count"] == 0
    finally:
        allow_activity.set()
        activity_thread.join(timeout=1.0)
        if lease.acquired:
            lease.release()


def test_missing_invalid_and_stale_snapshots_are_explicitly_unobserved(tmp_path):
    storage = tmp_path / "scope-recall"
    missing = writer_handoff_telemetry_view(storage)
    assert missing["snapshot_kind"] == "offline_config_only"
    assert missing["runtime_state_observed"] is False
    assert missing["telemetry_status"] == "missing"
    assert set(WRITER_HANDOFF_OBSERVABILITY_FIELDS) <= set(missing)
    assert missing["writer_role"] is None
    assert missing["idle_release_seconds"] == 1800.0

    storage.mkdir(parents=True)
    invalid = _payload(epoch="c" * 32, sequence=1)
    invalid["local_path"] = str(tmp_path)
    (storage / WRITER_HANDOFF_TELEMETRY_FILENAME).write_text(
        json.dumps(invalid), encoding="utf-8"
    )
    invalid_view = writer_handoff_telemetry_view(storage)
    assert invalid_view["telemetry_status"] == "invalid"
    assert invalid_view["runtime_state_observed"] is False
    assert invalid_view["writer_role"] is None

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    stale = _payload(epoch="d" * 32, sequence=1, observed_at=old)
    assert publish_writer_handoff_telemetry(
        storage, stale, claim_authority_epoch=True
    )
    stale_view = writer_handoff_telemetry_view(storage)
    assert stale_view["telemetry_status"] == "stale"
    assert stale_view["runtime_state_observed"] is False
    assert stale_view["freshness"]["age_seconds"] >= 7000.0
    assert stale_view["writer_role"] is None


def test_fresh_snapshot_activity_ages_advance_at_read_time(tmp_path):
    storage = tmp_path / "scope-recall"
    observed_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    snapshot = _payload(
        epoch="f" * 32,
        sequence=1,
        observed_at=observed_at,
    )
    assert publish_writer_handoff_telemetry(
        storage, snapshot, claim_authority_epoch=True
    )

    view = writer_handoff_telemetry_view(
        storage,
        now=observed_at + timedelta(seconds=120),
    )

    assert view["freshness"]["age_seconds"] == 120.0
    assert view["last_user_activity_age_seconds"] == 121.25
    assert view["last_truth_activity_age_seconds"] == 122.5


def test_runtime_writes_only_real_activity_or_state_events(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    lease = TruthWriterLease(storage, role="provider")
    assert lease.acquire()["status"] == "acquired"
    provider = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config={"writer_lease": {"idle_release_seconds": 1800.0}},
    )
    initialize_writer_handoff_activity(provider, reset=True)

    try:
        note_writer_promotion_succeeded(provider)
        path = storage / WRITER_HANDOFF_TELEMETRY_FILENAME
        activated = json.loads(path.read_text(encoding="utf-8"))
        assert activated["event_kind"] == "owner_activated"
        assert activated["writer_role"] == "owner"
        assert activated["same_process_holder_count"] == 1

        note_user_activity(provider)
        activity = json.loads(path.read_text(encoding="utf-8"))
        assert activity["event_kind"] == "user_activity"
        assert activity["event_sequence"] > activated["event_sequence"]

        before_probe = path.read_bytes()
        writer_handoff_status(provider)
        assert maybe_schedule_idle_writer_handoff(provider) is False
        assert path.read_bytes() == before_probe
    finally:
        lease.release()
        provider._truth_writer_lease = None
        provider._truth_writer_role = "unknown"

    note_writer_shutdown_succeeded(provider)
    shutdown = json.loads(path.read_text(encoding="utf-8"))
    assert shutdown["event_kind"] == "writer_shutdown"
    assert shutdown["writer_role"] == "unknown"
    assert shutdown["same_process_holder_count"] == 0


def test_telemetry_failure_never_changes_writer_authority(monkeypatch, tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    lease = TruthWriterLease(storage, role="provider")
    assert lease.acquire()["status"] == "acquired"
    provider = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config={"writer_lease": {"idle_release_seconds": 1800.0}},
    )
    initialize_writer_handoff_activity(provider, reset=True)

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise OSError("telemetry unavailable")

    monkeypatch.setattr(
        "scope_recall._internal.runtime.writer_handoff.publish_writer_handoff_telemetry",
        unavailable,
    )
    note_writer_promotion_succeeded(provider)
    note_user_activity(provider)

    assert lease.acquired is True
    assert provider._truth_writer_role == "owner"
    assert writer_handoff_status(provider)["writer_role"] == "owner"
    assert not (storage / WRITER_HANDOFF_TELEMETRY_FILENAME).exists()
    lease.release()


def test_same_process_holders_share_epoch_until_final_release(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    first_lease = TruthWriterLease(storage, role="provider")
    second_lease = TruthWriterLease(storage, role="provider")
    assert first_lease.acquire()["status"] == "acquired"
    assert second_lease.acquire()["status"] == "acquired"
    config = {"writer_lease": {"idle_release_seconds": 1800.0}}
    first = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=first_lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config=config,
    )
    second = SimpleNamespace(
        _storage_dir=storage,
        _truth_writer_lease=second_lease,
        _truth_writer_role="owner",
        _truth_writer_owner={},
        _config=config,
    )
    initialize_writer_handoff_activity(first, reset=True)
    initialize_writer_handoff_activity(second, reset=True)
    note_writer_promotion_succeeded(first)
    initial = read_writer_handoff_telemetry(storage)["snapshot"]
    note_writer_promotion_succeeded(second)
    joined = read_writer_handoff_telemetry(storage)["snapshot"]
    assert joined["authority_epoch"] == initial["authority_epoch"]
    assert joined["same_process_holder_count"] == 2

    first_lease.release()
    first._truth_writer_lease = None
    first._truth_writer_role = "unknown"
    note_writer_shutdown_succeeded(first)
    remaining = read_writer_handoff_telemetry(storage)["snapshot"]
    assert remaining["event_kind"] == "provider_shutdown"
    assert remaining["writer_role"] == "owner"
    assert remaining["same_process_holder_count"] == 1
    assert remaining["authority_epoch"] == initial["authority_epoch"]

    second_lease.release()
    second._truth_writer_lease = None
    second._truth_writer_role = "unknown"
    note_writer_shutdown_succeeded(second)
    released = read_writer_handoff_telemetry(storage)["snapshot"]
    assert released["event_kind"] == "writer_shutdown"
    assert released["same_process_holder_count"] == 0


def test_doctor_and_dashboard_expose_all_fresh_persisted_fields(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    storage = hermes_home / "scope-recall"
    snapshot = _payload(epoch="e" * 32, sequence=1)
    assert publish_writer_handoff_telemetry(
        storage, snapshot, claim_authority_epoch=True
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(DOCTOR),
            "--json",
            "--source-root",
            str(ROOT),
            "--hermes-home",
            str(hermes_home),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.stdout.strip(), (
        "doctor subprocess returned empty stdout "
        f"(exit code {completed.returncode})"
    )
    doctor_payload = json.loads(completed.stdout)
    doctor_handoff = doctor_payload["runtime"]["writer_handoff"]
    assert doctor_handoff["snapshot_kind"] == "persisted_runtime_state"
    assert doctor_handoff["runtime_state_observed"] is True
    assert doctor_handoff["telemetry_authoritative"] is False
    assert doctor_handoff["telemetry_status"] == "fresh"
    assert set(WRITER_HANDOFF_OBSERVABILITY_FIELDS) <= set(doctor_handoff)

    dashboard = _load_dashboard()
    dashboard_handoff = dashboard._writer_handoff_config_summary(
        {"writer_lease": {"idle_release_seconds": 1800.0}}, storage
    )
    assert dashboard_handoff["snapshot_kind"] == "persisted_runtime_state"
    assert dashboard_handoff["runtime_state_observed"] is True
    assert dashboard_handoff["freshness"]["status"] == "fresh"
    advancing_age_fields = {
        "last_user_activity_age_seconds",
        "last_truth_activity_age_seconds",
    }
    stable_fields = set(WRITER_HANDOFF_OBSERVABILITY_FIELDS) - advancing_age_fields
    assert {field: dashboard_handoff[field] for field in stable_fields} == {
        field: snapshot[field] for field in stable_fields
    }
    for field in advancing_age_fields:
        assert dashboard_handoff[field] >= snapshot[field]
        assert dashboard_handoff[field] <= snapshot[field] + 5.0

    serialized = json.dumps(dashboard_handoff, ensure_ascii=False)
    assert str(tmp_path) not in serialized
