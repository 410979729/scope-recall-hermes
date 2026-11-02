"""Host memory discovery uses only explicitly selected synthetic homes."""

import json

import pytest


@pytest.mark.parametrize(
    ("host", "configuration", "files", "switches"),
    [
        (
            "codex",
            ("config.toml", "[memories]\ngenerate_memories = false\nuse_memories = true\n"),
            ("memories/memory_summary.md",),
            {"generate_memories": False, "use_memories": True},
        ),
        (
            "hermes",
            ("config.yaml", "memory:\n  memory_enabled: true\n  user_profile_enabled: false\n"),
            ("memories/MEMORY.md", "memories/USER.md"),
            {"memory_enabled": True, "user_profile_enabled": False},
        ),
    ],
)
def test_known_host_settings_and_file_sizes_are_reported(tmp_path, host, configuration, files, switches):
    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    home.mkdir()
    (home / configuration[0]).write_text(configuration[1], encoding="utf-8")
    for name in files:
        file = home / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic memory", encoding="utf-8")
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    state = inspect_host_memory(host, home, project=tmp_path / "TEST-project", environment={})
    assert state["switches"] == switches
    assert {row["path"]: row["bytes"] for row in state["files"]} == {
        str(home / name): len(b"synthetic memory") for name in files
    }
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("host", ["workbuddy", "dsh"])
def test_undocumented_hosts_report_unknown_without_inventing_files(tmp_path, host):
    from scope_recall.adapters.host_memory import inspect_host_memory

    state = inspect_host_memory(host, tmp_path, project=tmp_path, environment={})
    assert state["status"] == "unknown"
    assert state["files"] == []


def test_doctor_reports_memory_even_without_a_store(tmp_path):
    from scope_recall.maintenance.doctor import run_doctor

    home = tmp_path / "TEST-hermes"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("synthetic", encoding="utf-8")
    (home / "config.yaml").write_text("memory:\n  memory_enabled: true\n", encoding="utf-8")
    report = run_doctor(host="hermes", instance_root=home).to_dict()
    assert report["host_memory"]["switches"]["memory_enabled"] is True
    assert report["host_memory"]["files"][0]["bytes"] == len(b"synthetic")
    json.dumps(report)


def test_codex_default_selects_the_existing_version(tmp_path):
    from scope_recall.adapters.host_memory import inspect_host_memory

    (tmp_path / "config.toml").write_text("[memories]\nuse_memories = true\n", encoding="utf-8")
    (tmp_path / "memories_v2").mkdir()
    (tmp_path / "memories_v2" / "memory_summary.md").write_text("synthetic", encoding="utf-8")
    state = inspect_host_memory("codex", tmp_path, project=tmp_path, environment={})
    assert state["files"][0]["path"] == str(tmp_path / "memories_v2" / "memory_summary.md")
    (tmp_path / "memories").mkdir()
    state = inspect_host_memory("codex", tmp_path, project=tmp_path, environment={})
    assert state["files"][0]["path"] == str(tmp_path / "memories" / "memory_summary.md")


def test_codex_invalid_memory_section_remains_unknown(tmp_path):
    from scope_recall.adapters.host_memory import inspect_host_memory

    (tmp_path / "config.toml").write_text('memories = "invalid"\n', encoding="utf-8")
    state = inspect_host_memory("codex", tmp_path, project=tmp_path, environment={})
    assert state["status"] == "unknown"
    assert state["switches"] == {"generate_memories": None, "use_memories": None}
    assert state["files"] == []


def test_claude_configured_directory_is_reported_without_reading_topics(tmp_path):
    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-claude"
    directory = tmp_path / "TEST-memory"
    home.mkdir()
    directory.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"autoMemoryEnabled": True, "autoMemoryDirectory": str(directory)}), encoding="utf-8"
    )
    (directory / "MEMORY.md").write_text("synthetic", encoding="utf-8")
    (directory / "topic.md").write_text("on demand", encoding="utf-8")
    state = inspect_host_memory("claude-code", home, project=tmp_path, environment={})
    assert state["switches"] == {"autoMemoryEnabled": True}
    assert state["files"] == [{"path": str(directory / "MEMORY.md"), "bytes": 9, "state": "present"}]
    disabled = inspect_host_memory(
        "claude-code", home, project=tmp_path, environment={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    )
    assert disabled["switches"]["autoMemoryEnabled"] is False


@pytest.mark.parametrize("feature", ["memories", "memory_tool"])
def test_codex_memory_feature_switch_is_reported(tmp_path, feature):
    from scope_recall.adapters.host_memory import inspect_host_memory

    (tmp_path / "config.toml").write_text(f"[features]\n{feature} = false\n", encoding="utf-8")
    state = inspect_host_memory("codex", tmp_path, project=tmp_path, environment={})
    assert state["feature_switch"] is False


@pytest.mark.parametrize("host", ["claude-code", "codex", "hermes", "workbuddy", "dsh"])
def test_doctor_exposes_each_host_memory_state(tmp_path, host):
    from scope_recall.maintenance.doctor import run_doctor

    home = tmp_path / ("TEST-" + host)
    project = tmp_path / "TEST-project"
    home.mkdir()
    project.mkdir()
    if host == "claude-code":
        (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    elif host == "codex":
        (home / "config.toml").write_text(
            "[features]\nmemories = true\n[memories]\nuse_memories = true\n", encoding="utf-8"
        )
    elif host == "hermes":
        (home / "config.yaml").write_text("memory:\n  memory_enabled: true\n", encoding="utf-8")
    state = run_doctor(host=host, instance_root=home, project_root=project).to_dict()["host_memory"]
    assert state["recommendation"]
    if host in {"workbuddy", "dsh"}:
        assert state["status"] == "unknown" and state["files"] == []
    else:
        assert state["status"] == "configured" and state["files"]
