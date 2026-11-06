"""Setup discovery stays inside the selected user's home."""

import os
from pathlib import Path


def test_discovery_never_probes_outside_the_selected_fake_user(tmp_path, monkeypatch):
    from scope_recall.maintenance.setup_discovery import discover_hosts

    home = tmp_path / "TEST-user"
    home.mkdir()
    (home / ".claude").mkdir()
    env = {name: str(home) for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")}
    probed = []

    def confined(probe):
        # Reject a fallback before asking the filesystem whether it exists.  Discovery asks through these Path
        # methods; below them Python 3.14 on Windows answers with native calls that never reach os.stat.
        def check(path, *args, **kwargs):
            assert path.is_relative_to(home), f"discovery escaped the fake user: {path}"
            probed.append(path)
            return probe(path, *args, **kwargs)

        return check

    # Confined only while discovery runs: pytest probes its own files around the test.
    with monkeypatch.context() as patch:
        for name in ("exists", "is_dir", "is_file"):
            patch.setattr(Path, name, confined(getattr(Path, name)))
        hosts = discover_hosts(env)
    assert [item.host for item in hosts] == ["claude-code"]
    assert probed
    assert all(item.home.is_relative_to(home) for item in hosts)


def test_posix_discovery_uses_home_and_host_overrides(tmp_path):
    from scope_recall.maintenance.setup_discovery import host_locations

    home = tmp_path / "TEST-user"
    locations = host_locations({"HOME": str(home), "CODEX_HOME": str(home / "custom")}, platform="linux")
    assert locations["codex"] == home / "custom"
    assert locations["hermes"] == home / ".hermes"
    assert locations["dsh"] == home / ".dsh"


def test_repository_origin_normalization_never_exposes_url_credentials(tmp_path):
    from scope_recall.maintenance.setup_projects import remote_identity

    project = tmp_path / "TEST-repository"
    (project / ".git").mkdir(parents=True)
    config = project / ".git" / "config"
    for url in (
        "https://example.test/team/project.git",
        "ssh://git@example.test/team/project.git",
        "git@example.test:team/project.git",
    ):
        config.write_text('[remote "origin"]\nurl=' + url + "\n", encoding="utf-8")
        assert remote_identity(project) == "example.test/team/project"
    config.write_text('[remote "origin"]\nurl=https://TEST-token@example.test/team/project.git\n', encoding="utf-8")
    assert remote_identity(project) is None


def test_selected_scope_recall_without_any_installation_is_fresh(tmp_path):
    """An interrupted setup can leave scope-recall selected with nothing installed; running setup again starts fresh."""
    from scope_recall.maintenance.onboarding import inspect_installation

    home = tmp_path / "TEST-existing-hermes"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: scope-recall\n", encoding="utf-8")
    assert inspect_installation(home, host="hermes")["route"] == "fresh_install"


def test_selected_legacy_provider_without_its_database_is_preserved_for_repair(tmp_path):
    from scope_recall.maintenance.onboarding import inspect_installation

    home = tmp_path / "TEST-existing-hermes"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: lancepro\n", encoding="utf-8")
    result = inspect_installation(home, host="hermes")
    assert result["route"] == "repair_required"
    assert result["reason"] == "selected_provider_without_binding"
