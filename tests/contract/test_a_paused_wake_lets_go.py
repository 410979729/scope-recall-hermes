"""A wake that starts under an operator pause leaves at once, and a refused package step says why.

Found upgrading a live instance on 2026-09-21.  The operator pause was set and every supervisor had
left; then the gateway was stopped, and on its way out it launched one last detached wake with the
usual start delay (``worker_min_interval_seconds``, 120 s there).  A supervisor read the pause only
after that delay, and it runs from the package folder, so for two minutes a sleeping process held the
folder the upgrade was about to replace.  The package step refused, correctly, and printed
``{"state": "blocked", "error_type": "PackageUpgradeError"}``: no reason, for a refusal whose remedy is
to wait a moment and run it again.
"""
from __future__ import annotations

import json

import pytest

from scope_recall.maintenance import package_upgrade
from scope_recall.runtime import scheduling
from scope_recall.runtime.scheduling import PAUSE_POLL_SECONDS, WakePlan, supervise


class _Owner:
    def __exit__(self, *args):
        return False


def _run(tmp_path, monkeypatch, *, delay_seconds, enabled_at):
    """Supervise with a start delay; ``enabled_at(slept)`` is what the operator's switch says."""
    import scope_recall.runtime.resume_entry as resume_entry

    writes: list[dict] = []
    slept: list[float] = []
    drains: list[float] = []

    class Recorder:
        def __init__(self, config) -> None:
            self.value: dict = {"wake_revision": 1}

        def request(self) -> None:
            pass

        def read(self) -> dict:
            return dict(self.value)

        def update(self, **fields):
            self.value.update(fields)
            writes.append(dict(self.value))
            return dict(self.value)

        def close_if_unchanged(self, revision, **fields):
            self.update(accepting=False, **fields)
            return True

    class Config:
        supervisor_seconds = 21600.0
        supervisor_max_drains = 8
        worker_min_interval_seconds = 0
        drain_seconds = 5.0
        auto_retry_cooldown_seconds = 60.0

    config = Config()
    monkeypatch.setattr(scheduling, "load_config", lambda path: config)
    monkeypatch.setattr(scheduling, "SupervisorControl", Recorder)
    monkeypatch.setattr(scheduling, "_acquire_ownership", lambda control: _Owner())
    monkeypatch.setattr(resume_entry, "read_control", lambda cfg: {"enabled": enabled_at(sum(slept))})

    def drain_once(remaining):
        drains.append(sum(slept))
        return 0, {"completed": 0}

    code = supervise(tmp_path / "config.json", drain_once, delay_seconds=delay_seconds,
                     clock=lambda: sum(slept), sleep=slept.append,
                     planner=lambda cfg, *, now, unavailable_until: WakePlan(None, "idle", 0, 0, 0))
    return code, writes, slept, drains


def test_a_wake_started_under_a_pause_leaves_before_its_delay(tmp_path, monkeypatch):
    code, writes, slept, drains = _run(tmp_path, monkeypatch, delay_seconds=120.0, enabled_at=lambda t: False)
    assert code == 0 and not slept, f"it slept {sum(slept)} s holding the package folder"
    assert not drains
    assert writes[-1]["state"] == "paused" and writes[-1]["reason"] == "operator_pause"
    assert writes[-1]["accepting"] is False and writes[-1]["finished_at"]


def test_a_pause_set_during_the_delay_ends_it_within_one_poll(tmp_path, monkeypatch):
    code, writes, slept, drains = _run(tmp_path, monkeypatch, delay_seconds=120.0, enabled_at=lambda t: t < 12.0)
    assert code == 0 and not drains
    assert 12.0 <= sum(slept) <= 12.0 + PAUSE_POLL_SECONDS, slept
    assert writes[-1]["state"] == "paused"


def test_the_delay_is_kept_in_full_when_nothing_is_paused(tmp_path, monkeypatch):
    code, writes, slept, drains = _run(tmp_path, monkeypatch, delay_seconds=32.0, enabled_at=lambda t: True)
    assert code == 0
    assert drains == [32.0], "the first pass starts when the delay ends, not before and not after"
    assert max(slept) <= PAUSE_POLL_SECONDS


def test_no_delay_means_no_sleep_before_the_first_pass(tmp_path, monkeypatch):
    code, writes, slept, drains = _run(tmp_path, monkeypatch, delay_seconds=0.0, enabled_at=lambda t: True)
    assert code == 0 and drains == [0.0] and not slept


# -- the package step says why it refused -------------------------------------

def _blocked(monkeypatch, capsys, error) -> dict:
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(package_upgrade, "replace_package", refuse)
    code = package_upgrade.main(["--python", "p", "--wheel", "w", "--backup", "b", "--uv", "u", "--source-quiesced"])
    assert code == 3
    return json.loads(capsys.readouterr().out)


def test_a_held_package_folder_is_named_and_so_is_what_to_do(monkeypatch, capsys):
    out = _blocked(monkeypatch, capsys, package_upgrade.PackageUpgradeError(package_upgrade._LOCKED))
    assert out["state"] == "blocked" and out["host_restart_allowed"] is False
    assert out["reason"] == "installed_files_locked_or_not_replaceable"
    assert out["next_action"].startswith("nothing_changed_wait_")


def test_every_reason_the_step_raises_is_a_code_it_will_print():
    import inspect
    import re

    source = inspect.getsource(package_upgrade)
    codes = set(re.findall(r"PackageUpgradeError\('([^']+)'\)", source)) | {package_upgrade._LOCKED}
    assert len(codes) >= 12, codes
    for code in codes:
        assert package_upgrade._REASON_CODE.fullmatch(code), code


@pytest.mark.parametrize("error", [
    package_upgrade.PackageUpgradeError("C:\\somewhere\\python.exe must reference an existing file"),
    FileExistsError(17, "File exists", "C:\\somewhere\\backup"),
])
def test_a_message_that_may_carry_a_path_is_not_printed(monkeypatch, capsys, error):
    out = _blocked(monkeypatch, capsys, error)
    assert out == {"state": "blocked", "error_type": type(error).__name__, "host_restart_allowed": False}
