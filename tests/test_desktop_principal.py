"""Desktop principal fallback contracts for single-operator Hermes Desktop.

Desktop sessions often omit ``user_id``. Scope Recall must still activate with a
profile-local opaque principal, while non-Desktop platforms remain fail-closed.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from plugins.memory import load_memory_provider

DISABLED_MISSING_PRINCIPAL = "disabled_missing_principal"


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
    hermes_home = tmp_path / "concurrent-home"
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        provider = _provider()
        try:
            barrier.wait(timeout=5)
            provider.initialize(
                "desktop-concurrent",
                hermes_home=str(hermes_home),
                platform="desktop",
                user_id="",
                agent_identity="desktop-agent",
                agent_workspace="hermes",
            )
            results.append(provider._scope.user_id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            provider.shutdown()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert not errors
    assert len(results) == 4
    assert len(set(results)) == 1
    assert results[0]
