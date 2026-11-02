"""Exercise the documented registration sequence in an isolated synthetic store."""

import sys


def test_setup_adds_runtime_configs_to_attached_remote_home(tmp_path, monkeypatch):
    from scope_recall.maintenance import setup
    from scope_recall.maintenance.shared import attach, init_shared

    store = tmp_path / "store"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = tmp_path / "homes" / "codex"
    home.mkdir(parents=True)
    init_shared(root=store, test_mode=True)
    attach(host="codex", instance_root=home, root=store, entry_id="codex", display_name="Codex")
    result = setup.main(
        [
            "--host",
            "codex",
            "--home",
            str(home),
            "--store",
            str(store),
            "--project",
            str(tmp_path),
            "--python",
            sys.executable,
            "--worker",
            "on-demand",
            "--yes",
            "--test-mode",
        ]
    )
    assert result == 0
    assert (store / "runtime-config.json").is_file()
    assert (home / "scope-recall" / "runtime-config.json").is_file()
