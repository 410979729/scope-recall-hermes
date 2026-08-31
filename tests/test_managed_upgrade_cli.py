"""Hermes plugin CLI contracts for the zero-choice stable updater."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scope_recall import cli, managed_upgrade


def test_active_plugin_update_derives_exact_home_and_dispatches(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "one-home"
    plugin_cli = home / "plugins" / "scope-recall" / "cli.py"
    plugin_cli.parent.mkdir(parents=True)
    plugin_cli.write_text("# test location\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(plugin_cli))
    calls: list[dict[str, object]] = []

    def update(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "state": "STAGED", "operation_id": "automatic-op"}

    monkeypatch.setattr(managed_upgrade, "auto_update", update)
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(["update"])

    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["operation_id"] == "automatic-op"
    assert calls == [{"hermes_home": home.resolve()}]


def test_plugin_update_cli_offers_no_url_repository_or_candidate_flags() -> None:
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(["update"])
    assert args.scope_recall_command_name == "update"
    assert not hasattr(args, "url")
    assert not hasattr(args, "repository")
    assert not hasattr(args, "candidate")


def test_installed_cli_update_is_a_zero_choice_auto_alias(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(managed_upgrade, "main", run)
    assert cli.main(["update", "--hermes-home", "C:/Hermes", "--json"]) == 0
    assert calls == [["auto", "--hermes-home", "C:/Hermes", "--json"]]


def test_installed_cli_update_accepts_no_home_flag(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        managed_upgrade,
        "main",
        lambda args: calls.append(args) or 0,
    )

    assert cli.main(["update", "--json"]) == 0
    assert calls == [["auto", "--json"]]


def test_managed_auto_uses_hermes_home_environment(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "configured-home"
    calls: list[Path] = []
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        managed_upgrade,
        "auto_update",
        lambda *, hermes_home, operation_id=None: calls.append(
            managed_upgrade.resolve_automatic_home(hermes_home)
        )
        or {"ok": True},
    )

    assert managed_upgrade.main(["auto", "--json"]) == 0
    assert calls == [home.resolve()]


def test_automatic_home_derives_from_active_plugin_location(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "active-home"
    module = home / "plugins" / "scope-recall" / "managed_upgrade.py"
    module.parent.mkdir(parents=True)
    module.write_text("# location authority\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(managed_upgrade, "__file__", str(module))

    assert managed_upgrade.resolve_automatic_home() == home.resolve()


def test_automatic_home_refuses_unproven_platform_default(
    monkeypatch, tmp_path: Path
) -> None:
    module = tmp_path / "site-packages" / "scope_recall" / "managed_upgrade.py"
    module.parent.mkdir(parents=True)
    module.write_text("# not an active plugin\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(managed_upgrade, "__file__", str(module))

    with pytest.raises(
        managed_upgrade.ManagedUpgradeError,
        match="hermes_home_unbound",
    ):
        managed_upgrade.resolve_automatic_home()


def test_unbound_cli_returns_exact_no_guess_action(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    module = tmp_path / "site-packages" / "scope_recall" / "managed_upgrade.py"
    module.parent.mkdir(parents=True)
    module.write_text("# not an active plugin\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(managed_upgrade, "__file__", str(module))

    assert managed_upgrade.main(["auto", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason_code"] == "hermes_home_unbound"
    assert payload["next_action_code"] == "run_from_active_hermes_home"
    assert payload["upgrade_complete"] is False
    assert "support_receipt" not in payload
