"""Desktop principal fallback contracts for single-operator Hermes Desktop.

Desktop sessions often omit ``user_id``. Scope Recall must still activate with a
profile-local opaque principal, while non-Desktop platforms remain fail-closed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider
import scope_recall.desktop_principal as desktop_principal_module
from scope_recall.desktop_principal import resolve_desktop_principal

DISABLED_MISSING_PRINCIPAL = "disabled_missing_principal"


def test_persisted_corrupt_principal_fails_closed_without_overwrite(tmp_path: Path) -> None:
    hermes_home = tmp_path / "corrupt-home"
    principal_path = hermes_home / "scope-recall" / "desktop-principal.id"
    principal_path.parent.mkdir(parents=True)
    corrupt = "not-a-principal\n"
    principal_path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(ValueError, match="persistent Desktop principal"):
        resolve_desktop_principal(hermes_home=hermes_home)

    assert principal_path.read_text(encoding="utf-8") == corrupt



def test_empty_persisted_principal_fails_closed(tmp_path: Path) -> None:
    hermes_home = tmp_path / "empty-home"
    principal_path = hermes_home / "scope-recall" / "desktop-principal.id"
    principal_path.parent.mkdir(parents=True)
    principal_path.write_bytes(b"")

    with pytest.raises(ValueError, match="persistent Desktop principal"):
        resolve_desktop_principal(hermes_home=hermes_home)

    assert principal_path.read_bytes() == b""



def test_persisted_invalid_utf8_principal_fails_closed(tmp_path: Path) -> None:
    hermes_home = tmp_path / "invalid-utf8-home"
    principal_path = hermes_home / "scope-recall" / "desktop-principal.id"
    principal_path.parent.mkdir(parents=True)
    invalid_utf8 = b"\xff\xfe\n"
    principal_path.write_bytes(invalid_utf8)

    with pytest.raises(UnicodeDecodeError):
        resolve_desktop_principal(hermes_home=hermes_home)

    assert principal_path.read_bytes() == invalid_utf8



def test_persisted_unreadable_principal_fails_closed(tmp_path: Path) -> None:
    hermes_home = tmp_path / "unreadable-home"
    principal_path = hermes_home / "scope-recall" / "desktop-principal.id"
    principal_path.parent.mkdir(parents=True)
    principal_path.mkdir()

    with pytest.raises(OSError):
        resolve_desktop_principal(hermes_home=hermes_home)

    assert principal_path.is_dir()


def test_transient_fast_path_permission_error_retries_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / "transient-permission-home"
    principal_path = hermes_home / "scope-recall" / "desktop-principal.id"
    principal_path.parent.mkdir(parents=True)
    persisted = "srdesk_0123456789abcdef0123456789abcdef"
    principal_path.write_text(persisted + "\n", encoding="utf-8")
    real_read = desktop_principal_module._read_principal_file
    attempts = 0

    def transient_read(path: Path) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "transient Windows sharing denial", str(path))
        return real_read(path)

    monkeypatch.setattr(
        desktop_principal_module,
        "_read_principal_file",
        transient_read,
    )

    resolved = resolve_desktop_principal(hermes_home=hermes_home)

    assert resolved == persisted
    assert attempts == 2
    assert principal_path.read_text(encoding="utf-8") == persisted + "\n"



def test_persisted_explicit_principal_shape_is_rejected(tmp_path: Path) -> None:
    hermes_home = tmp_path / "explicit-shaped-file-home"
    principal_path = hermes_home / "scope-recall" / "desktop-principal.id"
    principal_path.parent.mkdir(parents=True)
    persisted_override = "operator-desktop-main\n"
    principal_path.write_text(persisted_override, encoding="utf-8")

    with pytest.raises(ValueError, match="persistent Desktop principal"):
        resolve_desktop_principal(hermes_home=hermes_home)

    assert principal_path.read_text(encoding="utf-8") == persisted_override



def test_failed_atomic_publish_cleans_unique_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / "replace-failure-home"
    storage_dir = hermes_home / "scope-recall"
    replace_calls: list[tuple[Path, Path]] = []

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        replace_calls.append((Path(source), Path(destination)))
        raise OSError("atomic replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="atomic replace failed"):
        resolve_desktop_principal(hermes_home=hermes_home)

    assert len(replace_calls) == 1
    temporary_path, destination_path = replace_calls[0]
    assert temporary_path.parent == storage_dir
    assert temporary_path != destination_path
    assert temporary_path.name.startswith(".desktop-principal.id.")
    assert temporary_path.suffix == ".tmp"
    assert not temporary_path.exists()
    assert not destination_path.exists()



def test_post_replace_durability_failure_keeps_principal_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / "post-replace-failure-home"
    storage_dir = hermes_home / "scope-recall"
    principal_path = storage_dir / "desktop-principal.id"

    def fail_directory_sync(path: Path) -> None:
        assert path == storage_dir
        raise OSError("directory durability failed")

    monkeypatch.setattr(
        desktop_principal_module, "_sync_directory", fail_directory_sync
    )

    with pytest.raises(OSError, match="directory durability failed"):
        resolve_desktop_principal(hermes_home=hermes_home)

    persisted = principal_path.read_text(encoding="utf-8").removesuffix("\n")
    retry = resolve_desktop_principal(hermes_home=hermes_home)
    assert retry == persisted
    assert not list(storage_dir.glob("*.tmp"))



def test_first_create_flushes_and_fsyncs_file_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    principal = resolve_desktop_principal(hermes_home=tmp_path / "fsync-home")

    assert principal.startswith("srdesk_")
    assert fsync_calls



def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    return provider


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_desktop_missing_user_id_gets_stable_opaque_principal(tmp_path: Path) -> None:
    hermes_home = tmp_path / "desktop-home"
    provider = _provider()
    try:
        provider.initialize(
            "desktop-session",
            hermes_home=str(hermes_home),
            platform="desktop",
            user_id="",
            chat_id="",
            agent_identity="desktop-agent",
            agent_workspace="hermes",
        )
        assert provider.runtime_status == "active"
        assert provider.is_available() is True
        principal = provider._scope.user_id
        assert principal
        assert principal != ""
        assert principal.startswith("srdesk_")
        assert len(principal) == len("srdesk_") + 32
        assert set(principal.removeprefix("srdesk_")) <= set("0123456789abcdef")
        # Opaque: no host path leakage.
        assert "Users" not in principal
        assert "\\" not in principal
        assert "/" not in principal
        assert ":" not in principal
        assert provider._conn is not None
        assert (hermes_home / "scope-recall" / "memory.sqlite3").is_file()
    finally:
        provider.shutdown()

    # Persistence across restarts within the same profile.
    provider2 = _provider()
    try:
        provider2.initialize(
            "desktop-session-2",
            hermes_home=str(hermes_home),
            platform="desktop",
            user_id="",
            agent_identity="desktop-agent",
            agent_workspace="hermes",
        )
        assert provider2._scope.user_id == principal
    finally:
        provider2.shutdown()


def test_desktop_principal_is_isolated_per_hermes_home(tmp_path: Path) -> None:
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    principals: list[str] = []
    for home in (home_a, home_b):
        provider = _provider()
        try:
            provider.initialize(
                f"session-{home.name}",
                hermes_home=str(home),
                platform="desktop",
                user_id="",
                agent_identity="desktop-agent",
                agent_workspace="hermes",
            )
            principals.append(provider._scope.user_id)
        finally:
            provider.shutdown()
    assert principals[0]
    assert principals[1]
    assert principals[0] != principals[1]


def test_explicit_desktop_principal_config_overrides_generated_value(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "explicit-home"
    _write_config(
        hermes_home,
        {
            "identity": {"desktop_principal": "operator-desktop-main"},
            "vector": {"enabled": False},
        },
    )
    provider = _provider()
    try:
        provider.initialize(
            "desktop-explicit",
            hermes_home=str(hermes_home),
            platform="desktop",
            user_id="",
            agent_identity="desktop-agent",
            agent_workspace="hermes",
        )
        assert provider.runtime_status == "active"
        assert provider._scope.user_id == "operator-desktop-main"
    finally:
        provider.shutdown()


def test_non_desktop_missing_principal_still_fail_closed(tmp_path: Path) -> None:
    hermes_home = tmp_path / "telegram-home"
    provider = _provider()
    try:
        provider.initialize(
            "telegram-missing",
            hermes_home=str(hermes_home),
            platform="telegram",
            user_id="",
            chat_id="chat-a",
            agent_identity="desktop-agent",
            agent_workspace="hermes",
        )
        assert provider.runtime_status == DISABLED_MISSING_PRINCIPAL
        assert provider.is_available() is False
        assert not hermes_home.exists()
    finally:
        provider.shutdown()


def test_concurrent_desktop_principal_first_create_converges(tmp_path: Path) -> None:
    _provider()  # Preload the provider factory before any concurrent work.
    hermes_home = tmp_path / "concurrent-home"
    results: list[str] = []
    errors: list[BaseException] = []
    worker_count = 20
    barrier = threading.Barrier(worker_count)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(resolve_desktop_principal(hermes_home=hermes_home))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert not errors
    assert len(results) == worker_count
    assert len(set(results)) == 1
    assert results[0]
    persisted = (hermes_home / "scope-recall" / "desktop-principal.id").read_text(
        encoding="utf-8"
    ).removesuffix("\n")
    assert persisted == results[0]
