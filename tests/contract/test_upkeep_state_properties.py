"""Generated backup and deletion-mirror interruptions preserve independent durable checkpoints."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from scope_recall.core import deletion_mirror
from scope_recall.core.backup_primitives import BackupError
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import check_deletion_mirror
from scope_recall.runtime import backup_upkeep as upkeep
from test_claims import capture
from test_deletion import authorize, request

from tests.queue_state_support import QUEUE_SETTINGS, advance, check_transitions, state_table, store_factory


class UpkeepStates(RuleBasedStateMachine):
    """Exercise real scheduler metadata and real tombstones with bounded snapshot I/O injected."""

    def __init__(self, factory, patcher):
        super().__init__()
        self.core, self.ctx = factory()
        self.patch = patcher.context().__enter__()
        self.directory = self.ctx.binding.data_directory
        self.destination = self.directory / "TEST-backups"
        (self.directory / "backup-config.json").write_text(json.dumps({"backup_dir": str(self.destination)}))
        self.capable = True
        self.failure = "none"
        self.publications = 0
        self.sequence = 0
        self.synced_sequence = None
        self.previous = {}
        self.mirror = deletion_mirror._catchup
        self.patch.setattr(deletion_mirror, "_catchup", self.catchup)
        self.patch.setattr(upkeep, "create_backup_bundle", self.publish)
        self.patch.setattr(upkeep, "rotate_backup_bundles", self.rotate)

    def teardown(self):
        self.patch.undo()

    def catchup(self, *args, **kwargs):
        if not self.capable:
            raise OSError("TEST destination unavailable")
        result = self.mirror(*args, **kwargs)
        self.synced_sequence = self.sequence
        return result

    def publish(self, **kwargs):
        if self.failure in {"publication", "deadline"}:
            raise BackupError("TEST deadline exceeded" if self.failure == "deadline" else "TEST publication failed")
        self.publications += 1
        return {"manifest_sha256": "TEST-digest"}

    def rotate(self, *args, **kwargs):
        if self.failure == "retention":
            raise BackupError("TEST retention failed")
        return SimpleNamespace(retained_total=self.publications)

    @rule(configured=st.booleans())
    def configuration(self, configured):
        (self.directory / "backup-config.json").write_text(
            json.dumps({"backup_dir": str(self.destination)} if configured else {})
        )

    @rule(available=st.booleans())
    def capability(self, available):
        self.capable = available

    @rule()
    def delete(self):
        self.sequence += 1
        source = capture(self.core, self.ctx, f"TEST source {self.sequence}")
        authorize(self.core, self.ctx, source)
        self.core.forget(self.ctx, request(source), remaining_seconds=10)

    @rule(failure=st.sampled_from(["none", "publication", "retention", "deadline"]))
    def backup(self, failure):
        self.failure = failure
        upkeep.backup_if_due(self.core.storage, None, now=self.now(), available_seconds=30)

    @rule()
    def mirror_attempt(self):
        upkeep.mirror_if_due(self.core.storage, None, now=self.now(), available_seconds=10)

    @rule(seconds=st.integers(0, 100000))
    def clock_advance(self, seconds):
        advance(self.core, seconds)

    @rule()
    def operator_repair(self):
        self.capable, self.failure = True, "none"
        upkeep._save_state(self.directory, {"backup_blocked": False})

    def now(self):
        return datetime.fromisoformat(self.core.clock.utc_now().replace("Z", "+00:00"))

    @invariant()
    def upkeep_always_has_a_wake_or_a_diagnosed_stop(self):
        state = upkeep.read_backup_state(self.directory)
        configured = deletion_mirror.configured_backup_dir(self.directory) is not None
        assert (upkeep.next_backup_wake(self.directory, self.now()) is not None) == configured
        if not configured:
            report = DoctorReport("hermes", "ok")
            check_deletion_mirror(report, self.directory)
            assert any(item["name"] == "deletion_mirror" for item in report.checks)
        daily, _mirror_due = upkeep._due_times(state, self.now())
        backup_state = (
            "unconfigured"
            if not configured
            else "blocked"
            if state.get("backup_blocked")
            else "retention"
            if state.get("retention_pending")
            else "due"
            if daily <= self.now()
            else "waiting"
        )
        mirror_state = (
            "unconfigured"
            if not configured
            else "pending"
            if deletion_mirror.mirror_pending(self.directory, self.destination)
            else "caught_up"
        )
        current = {"backup": backup_state, "mirror": mirror_state}
        for key, heading in (("backup", "Backup upkeep"), ("mirror", "Deletion mirror")):
            if key in self.previous:
                check_transitions(state_table(heading), {key: self.previous[key]}, {key: current[key]})
        self.previous = current
        if state.get("backup_blocked"):
            assert "backup" in upkeep.backup_job_problem(self.directory)
        if state.get("retention_pending"):
            assert state.get("last_bundle") and state.get("last_daily_backup_utc")
        if state.get("last_retention_error"):
            assert state["retention_pending"]
        if self.synced_sequence == self.sequence and deletion_mirror.read_mirror_lag(self.directory) is None:
            assert deletion_mirror.mirror_pending(self.directory, self.destination) == 0
        assert {"due", "publication", "retention", "waiting", "blocked", "unconfigured"} == set(
            state_table("Backup upkeep")
        )
        assert {"pending", "caught_up", "unconfigured"} == set(state_table("Deletion mirror"))


def test_backup_and_mirror_state_sequences(tmp_path, monkeypatch):
    factory = store_factory(tmp_path)
    run_state_machine_as_test(lambda: UpkeepStates(factory, monkeypatch), settings=QUEUE_SETTINGS)


def test_unconfigured_upkeep_has_no_job_or_wake_and_doctor_explains_it(tmp_path, monkeypatch):
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.maintenance.doctor_store import check_deletion_mirror

    machine = UpkeepStates(store_factory(tmp_path), monkeypatch)
    try:
        machine.configuration(False)
        assert upkeep.backup_if_due(machine.core.storage, None, now=machine.now(), available_seconds=30) is None
        assert machine.publications == 0
        report = DoctorReport("hermes", "ok")
        check_deletion_mirror(report, machine.directory)
        assert any(
            item["result"] == "not_configured" or "backup directory removed" in item.get("detail", "")
            for item in report.checks
        )
        machine.upkeep_always_has_a_wake_or_a_diagnosed_stop()
        assert machine.previous == {"backup": "unconfigured", "mirror": "unconfigured"}
        machine.configuration(True)
        machine.upkeep_always_has_a_wake_or_a_diagnosed_stop()
        assert upkeep.next_backup_wake(machine.directory, machine.now()) is not None
    finally:
        machine.teardown()
