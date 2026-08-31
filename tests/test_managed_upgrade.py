"""Focused safety contracts for the first-party managed updater."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scope_recall.managed_upgrade import (
    ACTIVATING,
    COMPLETE,
    FAILED_SAFE,
    MANUAL_RECOVERY_REQUIRED,
    RESTARTING,
    STAGED,
    JOURNAL_SCHEMA,
    ManagedUpgradeError,
    UpgradeSeams,
    _os_file_lock,
    _read_events,
    _seal,
    auto_update,
    canonical_json_bytes,
    hash_candidate_tree,
    home_lock_path,
    main,
    operation_dir,
    prepare,
    register_cli,
    resume,
    run_worker,
    status,
)
from scope_recall.stable_update import canonical_tree_manifest

SECRET = "ghp_" + "abcdefghijklmnopqrstuvwxyzABCD"


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _plugin(root: Path, version: str, marker: str) -> Path:
    return _write_tree(
        root,
        {
            "plugin.yaml": f"name: scope-recall\nversion: {version}\n",
            "__init__.py": "# package\n",
            "installer.py": "# candidate installer\n",
            "provider.py": f"MARKER = {marker!r}\n",
            "config.json": "{}\n",
        },
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes-home"
    _plugin(home / "plugins" / "scope-recall", "2.0.0", "installed")
    return home


def _candidate(tmp_path: Path) -> Path:
    return _plugin(tmp_path / "candidate", "2.0.1", "candidate")


def _prepare(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    expected = str(hash_candidate_tree(candidate)["sha256"])
    payload = prepare(
        hermes_home=home,
        candidate=candidate,
        expected_tree_sha256=expected,
        operation_id="op-alpha-1",
    )
    return home, candidate, payload


class FakeGateway:
    def __init__(self, home: Path, *, running: bool = True, pid: int = 4242) -> None:
        self.home = home.resolve()
        self.running = running
        self.pid = pid
        self.starts = 0
        self.stops = 0
        self.last_args: list[str] = []
        self.last_env: dict[str, str] = {}

    def identify(self, **_kwargs: object) -> dict[str, Any] | None:
        if not self.running:
            return None
        return {"hermes_home": str(self.home), "pid": self.pid}

    def pause(self, **_kwargs: object) -> dict[str, Any]:
        old_pid = self.pid
        self.running = False
        return {"pausing": True, "pid": old_pid, "drain_timeout": 1}

    def stop(self, **kwargs: object) -> dict[str, Any]:
        self.stops += 1
        self.last_args = list(kwargs["args"])  # type: ignore[arg-type]
        self.last_env = dict(kwargs["env"])  # type: ignore[arg-type]
        self.running = False
        return {"ok": True}

    def start(self, **kwargs: object) -> dict[str, Any]:
        self.starts += 1
        self.last_args = list(kwargs["args"])  # type: ignore[arg-type]
        self.last_env = dict(kwargs["env"])  # type: ignore[arg-type]
        self.running = True
        self.pid += 1
        return {"ok": True}

    def alive(self, pid: int) -> bool:
        return self.running and pid == self.pid


def _preflight(**_kwargs: object) -> dict[str, Any]:
    return {"ok": True, "read_only": True}


def _success_install(**kwargs: object) -> dict[str, Any]:
    home = Path(str(kwargs["hermes_home"]))
    candidate = Path(str(kwargs["candidate"]))
    target = home / "plugins" / "scope-recall"
    shutil.rmtree(target)
    shutil.copytree(candidate, target)
    return {
        "ok": True,
        "activated": True,
        "mutation_started": True,
        "safe_to_restart_previous": False,
        "activation_transaction": {
            "status": "committed",
            "automatic_rollback": False,
        },
    }


def _seams(gateway: FakeGateway, **overrides: Any) -> UpgradeSeams:
    values: dict[str, Any] = {
        "preflight": _preflight,
        "install": _success_install,
        "gateway_identify": gateway.identify,
        "gateway_pause": gateway.pause,
        "gateway_stop": gateway.stop,
        "gateway_start": gateway.start,
        "pid_alive": gateway.alive,
    }
    values.update(overrides)
    return UpgradeSeams(**values)


def test_candidate_is_frozen_and_source_toctou_does_not_change_worker(tmp_path: Path) -> None:
    home, source, prepared = _prepare(tmp_path)
    assert prepared["state"] == STAGED
    (source / "provider.py").write_text("MUTATED = True\n", encoding="utf-8")
    gateway = FakeGateway(home)
    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway),
    )
    assert result["state"] == COMPLETE
    installed = (home / "plugins" / "scope-recall" / "provider.py").read_text()
    assert "candidate" in installed
    assert "MUTATED" not in installed


def test_expected_hash_and_staged_hash_are_fail_closed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    with pytest.raises(ManagedUpgradeError, match="candidate_hash_mismatch"):
        prepare(
            hermes_home=home,
            candidate=candidate,
            expected_tree_sha256="0" * 64,
        )

    home, _, _ = _prepare(tmp_path / "second")
    op_dir = operation_dir(home, "op-alpha-1")
    (op_dir / "candidate" / "provider.py").write_text("tampered\n", encoding="utf-8")
    payload = run_worker(hermes_home=home, operation_id="op-alpha-1")
    assert payload["state"] == FAILED_SAFE
    assert payload["reason_code"] == "candidate_hash_mismatch"


def test_tree_hash_is_identical_to_official_stable_source_algorithm(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    # These names were historically filtered by the installer. The release
    # identity must nevertheless cover every archive byte-bearing file.
    _write_tree(
        candidate,
        {
            ".git/config": "not-a-live-repository\n",
            ".env": "not-a-secret\n",
            "tests/covered.py": "COVERED = True\n",
            "__pycache__/covered.pyc": "opaque\n",
        },
    )
    managed = hash_candidate_tree(candidate)
    stable = canonical_tree_manifest(candidate)
    assert managed["sha256"] == stable["tree_sha256"]
    assert managed["file_count"] == stable["file_count"]


def test_symlink_candidate_is_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    try:
        (candidate / "linked.py").symlink_to(candidate / "provider.py")
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(ManagedUpgradeError, match="candidate_symlink_forbidden"):
        hash_candidate_tree(candidate)
    assert not (home / "scope-recall" / "upgrades" / "operations").exists()


def test_home_wide_os_lock_blocks_a_second_operation(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)
    with _os_file_lock(home_lock_path(home), "held"):
        with pytest.raises(ManagedUpgradeError, match="home_upgrade_locked"):
            run_worker(
                hermes_home=home,
                operation_id="op-alpha-1",
                seams=_seams(gateway),
            )


def test_operation_os_lock_blocks_duplicate_worker(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    op_dir = operation_dir(home, "op-alpha-1")
    gateway = FakeGateway(home)
    with _os_file_lock(op_dir / "operation.lock", "held"):
        with pytest.raises(ManagedUpgradeError, match="operation_locked"):
            run_worker(
                hermes_home=home,
                operation_id="op-alpha-1",
                seams=_seams(gateway),
            )


def test_fsynced_pending_transition_is_recovered_before_resume(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    op_dir = operation_dir(home, "op-alpha-1")
    previous = _read_events(op_dir)[-1]
    pending = _seal(
        {
            "at": "2026-08-30T00:00:00+00:00",
            "data": {},
            "operation_id": "op-alpha-1",
            "previous_sha256": previous["sha256"],
            "reason_code": "managed_preflight_passed",
            "schema": JOURNAL_SCHEMA,
            "seq": 2,
            "state": "PREFLIGHTED",
        }
    )
    (op_dir / "journal.pending.json").write_bytes(canonical_json_bytes(pending) + b"\n")
    with pytest.raises(ManagedUpgradeError, match="transition_pending"):
        status(hermes_home=home, operation_id="op-alpha-1")
    gateway = FakeGateway(home)
    result = resume(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway),
    )
    assert result["state"] == COMPLETE


def test_real_pause_ack_must_match_exact_gateway_pid_and_home(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def fake_pause(**_kwargs: object) -> dict[str, Any]:
        return {"pausing": True, "pid": gateway.pid + 99, "drain_timeout": 1}

    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway, gateway_pause=fake_pause),
    )
    assert result["state"] == MANUAL_RECOVERY_REQUIRED
    assert result["reason_code"] == "gateway_pause_pid_mismatch"
    assert gateway.starts == 0

    other_home = tmp_path / "other-home"
    home2, _, _ = _prepare(tmp_path / "mismatch")
    bad_gateway = FakeGateway(other_home)
    result2 = run_worker(
        hermes_home=home2,
        operation_id="op-alpha-1",
        seams=_seams(bad_gateway),
    )
    assert result2["state"] == MANUAL_RECOVERY_REQUIRED
    assert result2["reason_code"] == "gateway_identity_mismatch"


def test_fallback_stop_and_start_are_exact_home_and_never_all(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home, running=False)
    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway),
    )
    assert result["state"] == COMPLETE
    assert gateway.stops == 1
    assert gateway.starts == 1
    assert gateway.last_env["HERMES_HOME"] == str(home.resolve())
    assert "HERMES_PROFILE" not in gateway.last_env
    assert "--all" not in gateway.last_args
    assert "--all-profiles" not in gateway.last_args


def test_candidate_package_owns_preflight_install_and_resume_imports(tmp_path: Path) -> None:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    (candidate / "installer.py").write_text(
        """from pathlib import Path
import shutil

def managed_upgrade_preflight(home, *, candidate_tree):
    return {"ok": True, "read_only": True}

def install(home, *, managed_state_dir, **kwargs):
    home = Path(home)
    source = Path(__file__).parent
    target = home / "plugins" / "scope-recall"
    shutil.rmtree(target)
    shutil.copytree(source, target)
    Path(managed_state_dir, "activation-transaction.json").write_text("sealed")
    return {
        "ok": True,
        "activated": True,
        "mutation_started": True,
        "safe_to_restart_previous": False,
        "activation_transaction": {
            "status": "committed", "automatic_rollback": False
        },
    }

def resume_managed_upgrade(*, managed_state_dir, hermes_home):
    assert Path(managed_state_dir, "activation-transaction.json").is_file()
    assert Path(hermes_home).is_dir()
    return {
        "ok": True,
        "safe_to_restart_previous": False,
        "activation_transaction": {
            "status": "committed", "automatic_rollback": False
        },
    }
""",
        encoding="utf-8",
    )
    expected = str(hash_candidate_tree(candidate)["sha256"])
    prepare(
        hermes_home=home,
        candidate=candidate,
        expected_tree_sha256=expected,
        operation_id="candidate-import",
    )
    gateway = FakeGateway(home)
    result = run_worker(
        hermes_home=home,
        operation_id="candidate-import",
        seams=UpgradeSeams(
            gateway_identify=gateway.identify,
            gateway_pause=gateway.pause,
            gateway_stop=gateway.stop,
            gateway_start=gateway.start,
            pid_alive=gateway.alive,
        ),
    )
    assert result["state"] == COMPLETE
    assert "version: 2.0.1" in (
        home / "plugins" / "scope-recall" / "plugin.yaml"
    ).read_text(encoding="utf-8")


def test_safe_automatic_rollback_restarts_only_previous_version(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def rolled_back(**_kwargs: object) -> dict[str, Any]:
        return {
            "ok": False,
            "activated": False,
            "mutation_started": True,
            "safe_to_restart_previous": True,
            "activation_transaction": {
                "status": "rolled_back",
                "automatic_rollback": True,
            },
        }

    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway, install=rolled_back),
    )
    assert result["state"] == FAILED_SAFE
    assert result["reason_code"] == "previous_version_restarted"
    assert gateway.running is True
    assert gateway.starts == 1
    assert "version: 2.0.0" in (
        home / "plugins" / "scope-recall" / "plugin.yaml"
    ).read_text(encoding="utf-8")


def test_ambiguous_installer_failure_never_guesses_outer_rollback(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def ambiguous(**_kwargs: object) -> dict[str, Any]:
        return {
            "ok": False,
            "mutation_started": True,
            "safe_to_restart_previous": False,
            "activation_transaction": {
                "status": "rollback_failed",
                "automatic_rollback": False,
            },
        }

    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway, install=ambiguous),
    )
    assert result["state"] == MANUAL_RECOVERY_REQUIRED
    assert gateway.starts == 0
    assert gateway.running is False


def test_activating_crash_resumes_only_through_installer_handle(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def crash_after_handle(**kwargs: object) -> dict[str, Any]:
        state_dir = Path(str(kwargs["managed_state_dir"]))
        (state_dir / "activation-transaction.json").write_text("sealed", encoding="utf-8")
        target = home / "plugins" / "scope-recall"
        shutil.rmtree(target)
        shutil.copytree(Path(str(kwargs["candidate"])), target)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(
            hermes_home=home,
            operation_id="op-alpha-1",
            seams=_seams(gateway, install=crash_after_handle),
        )
    assert status(hermes_home=home, operation_id="op-alpha-1")["state"] == ACTIVATING

    def resumed(**_kwargs: object) -> dict[str, Any]:
        return {
            "ok": True,
            "activated": True,
            "safe_to_restart_previous": False,
            "activation_transaction": {
                "status": "committed",
                "automatic_rollback": False,
            },
        }

    result = resume(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway, resume_install=resumed),
    )
    assert result["state"] == COMPLETE
    assert gateway.starts == 1


def test_activating_before_installer_intent_retries_from_exact_previous_identity(
    tmp_path: Path,
) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def crash_before_handle(**_kwargs: object) -> dict[str, Any]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(
            hermes_home=home,
            operation_id="op-alpha-1",
            seams=_seams(gateway, install=crash_before_handle),
        )
    result = resume(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway),
    )
    assert result["state"] == COMPLETE
    assert gateway.running is True
    assert gateway.starts == 1


def test_restarting_resume_uses_physical_version_and_gateway_identity(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def crash_after_start(**kwargs: object) -> dict[str, Any]:
        gateway.start(**kwargs)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(
            hermes_home=home,
            operation_id="op-alpha-1",
            seams=_seams(gateway, gateway_start=crash_after_start),
        )
    assert status(hermes_home=home, operation_id="op-alpha-1")["state"] == RESTARTING
    starts = gateway.starts
    result = resume(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway),
    )
    assert result["state"] == COMPLETE
    assert gateway.starts == starts + 1
    assert gateway.stops == 0


def test_journal_and_receipt_never_capture_exception_or_config_values(tmp_path: Path) -> None:
    home, _, _ = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def leaky_failure(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError(f"provider token={SECRET}; private memory text")

    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway, install=leaky_failure),
    )
    assert result["state"] == ACTIVATING
    op_dir = operation_dir(home, "op-alpha-1")
    for name in ("plan.json", "journal.jsonl", "receipt.json"):
        text = (op_dir / name).read_text(encoding="utf-8")
        assert SECRET not in text
        assert "private memory" not in text
        assert "config.yaml" not in text
        assert str(home) not in text


def test_cli_registration_and_commands_emit_machine_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home, _, _ = _prepare(tmp_path)
    parser = argparse.ArgumentParser()
    register_cli(parser)
    parsed = parser.parse_args(
        [
            "status",
            "--hermes-home",
            str(home),
            "--operation-id",
            "op-alpha-1",
            "--json",
        ]
    )
    assert parsed.managed_upgrade_command == "status"
    assert callable(parsed.func)
    code = main(
        [
            "managed-upgrade",
            "status",
            "--hermes-home",
            str(home),
            "--operation-id",
            "op-alpha-1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["state"] == STAGED


def test_prepare_can_spawn_the_frozen_detached_worker(tmp_path: Path) -> None:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    expected = str(hash_candidate_tree(candidate)["sha256"])
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spawn(args: list[str], **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return object()

    result = prepare(
        hermes_home=home,
        candidate=candidate,
        expected_tree_sha256=expected,
        operation_id="detached-op",
        detach=True,
        seams=UpgradeSeams(spawn=spawn),
    )
    assert result["detached"] is True
    assert calls
    args, kwargs = calls[0]
    assert Path(args[1]).is_file()
    assert args[2] == "worker"
    assert kwargs["env"]["HERMES_HOME"] == str(home.resolve())


def test_auto_update_has_no_source_or_policy_choices_and_starts_worker(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    identity = hash_candidate_tree(candidate)
    stage_calls: list[dict[str, Any]] = []
    spawn_calls: list[list[str]] = []

    def stage(**kwargs: Any) -> dict[str, Any]:
        stage_calls.append(kwargs)
        return {
            "ok": True,
            "candidate_dir": str(candidate),
            "version": "2.0.1",
            "tree_sha256": identity["tree_sha256"],
            "file_count": identity["file_count"],
        }

    def spawn(args: list[str], **_kwargs: Any) -> object:
        spawn_calls.append(args)
        return object()

    result = auto_update(
        hermes_home=home,
        operation_id="automatic-op",
        seams=UpgradeSeams(stable_stage=stage, spawn=spawn),
    )

    assert result["state"] == STAGED
    assert result["detached"] is True
    assert stage_calls == [
        {
            "cache_dir": home.resolve() / "scope-recall" / "upgrades" / "cache",
            "installed_version": "2.0.0",
        }
    ]
    assert spawn_calls[0][2] == "worker"


def test_auto_update_failure_is_content_free_and_already_current_is_noop(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    failed = auto_update(
        hermes_home=home,
        seams=UpgradeSeams(
            stable_stage=lambda **_kwargs: {
                "ok": False,
                "error": {
                    "code": "NETWORK_ERROR",
                    "private": "memory text and C:/secret/config.yaml",
                },
            }
        ),
    )
    assert failed["ok"] is False
    assert failed["state"] == FAILED_SAFE
    assert failed["terminal"] is True
    assert failed["reason_code"] == "stable_network_error"
    assert failed["outcome"] == "support_required"
    assert failed["upgrade_complete"] is False
    assert failed["next_action_code"] == "submit_support_receipt"
    assert failed["support_receipt"] == {
        "schema": "scope-recall.managed-upgrade-support.v1",
        "state": "NO_OPERATION",
        "reason_code": "stable_network_error",
    }
    assert "memory text" not in json.dumps(failed)
    assert "C:/secret" not in json.dumps(failed)

    candidate = _plugin(tmp_path / "current", "2.0.0", "same")
    identity = hash_candidate_tree(candidate)
    current = auto_update(
        hermes_home=home,
        seams=UpgradeSeams(
            stable_stage=lambda **_kwargs: {
                "ok": True,
                "candidate_dir": str(candidate),
                "version": "2.0.0",
                "tree_sha256": identity["tree_sha256"],
                "file_count": identity["file_count"],
            }
        ),
    )
    assert current["state"] == COMPLETE
    assert current["reason_code"] == "already_current"
    assert current["upgrade_complete"] is True


def test_auto_update_retries_retryable_staging_without_user_judgment(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidate = _candidate(tmp_path)
    identity = hash_candidate_tree(candidate)
    attempts = 0
    delays: list[float] = []

    def stage(**_kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return {
                "ok": False,
                "error": {"code": "NETWORK_ERROR", "retryable": True},
            }
        return {
            "ok": True,
            "candidate_dir": str(candidate),
            "version": "2.0.1",
            "tree_sha256": identity["tree_sha256"],
            "file_count": identity["file_count"],
        }

    result = auto_update(
        hermes_home=home,
        operation_id="retrying-op",
        seams=UpgradeSeams(
            stable_stage=stage,
            spawn=lambda *_args, **_kwargs: object(),
            sleep=delays.append,
        ),
    )

    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert result["state"] == STAGED
    assert result["outcome"] == "upgrade_in_progress"
    assert result["upgrade_complete"] is False
    assert result["background_worker_started"] is True
    assert result["user_action_required"] is False
    assert result["next_action_code"] == "wait_for_automatic_restart"


def test_auto_update_preserves_retryable_outcome_after_bounded_attempts(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    attempts = 0

    def unavailable(**_kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return {
            "ok": False,
            "error": {"code": "NETWORK_ERROR", "retryable": True},
        }

    result = auto_update(
        hermes_home=home,
        seams=UpgradeSeams(
            stable_stage=unavailable,
            sleep=lambda _delay: None,
        ),
    )

    assert attempts == 3
    assert result["outcome"] == "retry_later"
    assert result["retryable"] is True
    assert result["automatic_retry_attempts"] == 3
    assert result["next_action_code"] == "rerun_same_update_command"
    assert result["upgrade_complete"] is False


def test_auto_update_resumes_the_only_incomplete_operation_before_network(
    tmp_path: Path,
) -> None:
    home, _candidate_path, _prepared = _prepare(tmp_path)
    spawn_calls: list[list[str]] = []

    def forbidden_stage(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("resume must not contact the release service")

    def spawn(args: list[str], **_kwargs: Any) -> object:
        spawn_calls.append(args)
        return object()

    result = auto_update(
        hermes_home=home,
        seams=UpgradeSeams(stable_stage=forbidden_stage, spawn=spawn),
    )

    assert result["operation_id"] == "op-alpha-1"
    assert result["reason_code"] == "incomplete_operation_resumed"
    assert result["detached"] is True
    assert spawn_calls[0][2:4] == ["worker", "--hermes-home"]


def test_auto_update_repairs_partial_pending_transition_before_spawn(
    tmp_path: Path,
) -> None:
    home, _candidate_path, _prepared = _prepare(tmp_path)
    op_dir = operation_dir(home, "op-alpha-1")
    previous = _read_events(op_dir)[-1]
    pending = _seal(
        {
            "at": "2026-08-30T00:00:00+00:00",
            "data": {},
            "operation_id": "op-alpha-1",
            "previous_sha256": previous["sha256"],
            "reason_code": "managed_preflight_passed",
            "schema": JOURNAL_SCHEMA,
            "seq": 2,
            "state": "PREFLIGHTED",
        }
    )
    encoded = canonical_json_bytes(pending) + b"\n"
    (op_dir / "journal.pending.json").write_bytes(encoded)
    with (op_dir / "journal.jsonl").open("ab") as journal:
        journal.write(encoded[:37])
    spawn_calls: list[list[str]] = []

    result = auto_update(
        hermes_home=home,
        seams=UpgradeSeams(
            stable_stage=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("crash recovery must precede network")
            ),
            spawn=lambda args, **_kwargs: spawn_calls.append(args),
        ),
    )

    assert result["operation_id"] == "op-alpha-1"
    assert result["reason_code"] == "incomplete_operation_resumed"
    assert not (op_dir / "journal.pending.json").exists()
    assert _read_events(op_dir)[-1]["state"] == "PREFLIGHTED"
    assert spawn_calls[0][2] == "worker"


def test_auto_update_never_overwrites_unresolved_manual_operation(
    tmp_path: Path,
) -> None:
    home, _candidate_path, _prepared = _prepare(tmp_path)
    wrong_gateway = FakeGateway(tmp_path / "different-home")
    failed = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(wrong_gateway),
    )
    assert failed["state"] == MANUAL_RECOVERY_REQUIRED

    result = auto_update(
        hermes_home=home,
        seams=UpgradeSeams(
            stable_stage=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("manual operation must block network")
            ),
            spawn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("manual operation must not spawn")
            ),
        ),
    )

    assert result["state"] == MANUAL_RECOVERY_REQUIRED
    assert result["reason_code"] == "unresolved_manual_operation"
    assert result["detached"] is False
    assert result["next_action_code"] == "submit_support_receipt"
    assert result["support_receipt"]["operation_id"] == "op-alpha-1"
    assert result["support_receipt"]["journal_tail_sha256"]


def test_restart_command_failure_remains_resumable(
    tmp_path: Path,
) -> None:
    home, _candidate_path, _prepared = _prepare(tmp_path)
    gateway = FakeGateway(home)
    first = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(
            gateway,
            gateway_start=lambda **_kwargs: {"ok": False},
            sleep=lambda _delay: None,
        ),
    )
    assert first["state"] == RESTARTING
    assert first["outcome"] == "upgrade_in_progress"
    assert first["upgrade_complete"] is False
    assert first["user_action_required"] is True
    assert first["next_action_code"] == "rerun_same_update_command"
    assert gateway.running is False

    second = resume(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway),
    )
    assert second["state"] == COMPLETE
    assert gateway.running is True


def test_commit_cleanup_pending_remains_activating_then_commits(
    tmp_path: Path,
) -> None:
    home, _candidate_path, _prepared = _prepare(tmp_path)
    gateway = FakeGateway(home)

    def pending(**kwargs: object) -> dict[str, Any]:
        result = _success_install(**kwargs)
        state_dir = Path(str(kwargs["managed_state_dir"]))
        (state_dir / "activation-transaction.json").write_text(
            "sealed", encoding="utf-8"
        )
        return {
            **result,
            "ok": False,
            "managed_retryable": True,
            "activation_transaction": {
                "status": "commit_cleanup_pending",
                "automatic_rollback": False,
            },
        }

    first = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(gateway, install=pending),
    )
    assert first["state"] == ACTIVATING
    assert gateway.running is False

    second = resume(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=_seams(
            gateway,
            resume_install=lambda **_kwargs: {
                "ok": True,
                "safe_to_restart_previous": False,
                "activation_transaction": {
                    "status": "committed",
                    "automatic_rollback": False,
                },
            },
        ),
    )
    assert second["state"] == COMPLETE
    assert gateway.running is True


def test_gateway_restart_retries_bounded_transient_command_failures(
    tmp_path: Path,
) -> None:
    home, _candidate_path, _prepared = _prepare(tmp_path)
    gateway = FakeGateway(home)
    attempts = 0

    def flaky_start(**kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return {"ok": False}
        return gateway.start(**kwargs)

    seams = _seams(gateway, gateway_start=flaky_start, sleep=lambda _delay: None)
    result = run_worker(
        hermes_home=home,
        operation_id="op-alpha-1",
        seams=seams,
    )

    assert result["state"] == COMPLETE
    assert attempts == 3
