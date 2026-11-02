"""Exact host memory comparisons preserve separate statements."""

import pytest


@pytest.mark.parametrize(
    ("statement", "entry", "expected"),
    [
        ("TEST palette is blue", "TEST   palette is blue", True),
        ("publish blue icons", "do not publish blue icons", False),
        ("TEST palette is blue", "TEST palette is red", False),
        ("TEST palette is blue", "test palette is blue", False),
    ],
)
def test_host_entry_requires_whole_normalized_statement(statement, entry, expected):
    from scope_recall.core.recall_restatement import same_statement

    assert same_statement(statement, entry) is expected


def test_markdown_entries_do_not_join_separate_facts():
    from scope_recall.adapters.host_memory import memory_entries

    text = "# Preferences\n- TEST palette is blue\n- TEST never publish\n\nTEST project uses Python.\n"
    assert memory_entries(text) == ("TEST palette is blue", "TEST never publish", "TEST project uses Python.")


def test_read_host_memory_uses_enabled_and_old_files_only(tmp_path):
    import os
    import time

    from scope_recall.adapters.host_memory import read_host_memory

    home = tmp_path / "TEST-hermes"
    (home / "memories").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "memory:\n  memory_enabled: true\n  user_profile_enabled: false\n", encoding="utf-8"
    )
    memory = home / "memories" / "MEMORY.md"
    profile = home / "memories" / "USER.md"
    memory.write_text("- TEST old fact\n", encoding="utf-8")
    profile.write_text("- TEST disabled fact\n", encoding="utf-8")
    started = time.time() - 10
    os.utime(home / "config.yaml", (started - 10, started - 10))
    os.utime(memory, (started - 10, started - 10))
    os.utime(profile, (started - 10, started - 10))
    args = dict(project=tmp_path, session_started=started, deadline=time.monotonic() + 2, environment={})
    assert read_host_memory("hermes", home, **args) == ("TEST old fact",)
    os.utime(memory, (started + 1, started + 1))
    assert read_host_memory("hermes", home, **args) == ()
    assert read_host_memory("hermes", home, **{**args, "session_started": None}) == ()
    assert read_host_memory("hermes", home, **{**args, "deadline": time.monotonic() - 1}) == ()


def test_hermes_session_start_is_read_from_its_session_store(tmp_path):
    import os
    import sqlite3
    import time

    from scope_recall.adapters.host_memory import read_host_memory

    home = tmp_path / "TEST-hermes-store"
    (home / "memories").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "memory:\n  memory_enabled: true\n  user_profile_enabled: false\n", encoding="utf-8"
    )
    path = home / "memories" / "MEMORY.md"
    path.write_text("- TEST stable host fact\n", encoding="utf-8")
    started = time.time() - 5
    os.utime(home / "config.yaml", (started - 5, started - 5))
    os.utime(path, (started - 5, started - 5))
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL)")
        db.execute("INSERT INTO sessions VALUES (?,?)", ("TEST-session", started))
    assert read_host_memory(
        "hermes",
        home,
        project=tmp_path,
        session_started=None,
        session_id="TEST-session",
        deadline=time.monotonic() + 2,
        environment={},
    ) == ("TEST stable host fact",)
    assert (
        read_host_memory(
            "hermes",
            home,
            project=tmp_path,
            session_started=None,
            session_id="TEST-other",
            deadline=time.monotonic() + 2,
            environment={},
        )
        == ()
    )


def test_claim_without_conditions_matches_its_complete_statement():
    import json

    from scope_recall.core.recall_restatement import host_memory_duplicate
    from scope_recall.core.retrieval import RetrievedObject

    payload = {"subject": "TEST project", "predicate": "palette", "value_text": "amber", "conditions": []}
    obj = RetrievedObject(
        "TEST-claim", 1, "claim", json.dumps(payload), "human_direct", "current", "", (), "direct_report", True
    )
    assert host_memory_duplicate(obj, ("TEST project palette amber",))
    payload["conditions"] = ["only indoors"]
    obj = RetrievedObject(
        "TEST-claim", 1, "claim", json.dumps(payload), "human_direct", "current", "", (), "direct_report", True
    )
    assert not host_memory_duplicate(obj, ("TEST project palette amber",))


def test_wrapped_paragraph_does_not_create_an_unconditional_entry():
    from scope_recall.adapters.host_memory import memory_entries

    assert "TEST palette is amber" not in memory_entries("TEST palette is amber\nonly indoors")

    assert "TEST palette is amber" not in memory_entries("- TEST palette is amber\n  only indoors")


def test_settings_modified_after_session_do_not_attest_host_switch(tmp_path):
    import os
    import time

    from scope_recall.adapters.host_memory import read_host_memory

    home = tmp_path / "TEST-late-config"
    (home / "memories").mkdir(parents=True)
    settings = home / "config.yaml"
    settings.write_text("memory:\n  memory_enabled: true\n  user_profile_enabled: false\n", encoding="utf-8")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("- TEST old fact\n", encoding="utf-8")
    started = time.time() - 10
    os.utime(memory, (started - 5, started - 5))
    os.utime(settings, (started + 1, started + 1))
    assert (
        read_host_memory(
            "hermes", home, project=tmp_path, session_started=started, deadline=time.monotonic() + 2, environment={}
        )
        == ()
    )


def test_adjacent_complete_sentences_are_separate_entries():
    from scope_recall.adapters.host_memory import memory_entries

    assert memory_entries("TEST first fact.\nTEST second fact.") == ("TEST first fact.", "TEST second fact.")


@pytest.mark.parametrize("host", ["claude-code", "codex"])
def test_truncated_host_entry_is_not_a_complete_statement(tmp_path, host):
    import json
    import os
    import time

    from scope_recall.adapters.host_memory import _token_prefix, memory_entries, read_host_memory

    home = tmp_path / "TEST-home"
    project = tmp_path / "TEST-project"
    home.mkdir()
    project.mkdir()
    if host == "claude-code":
        memory = home / "memory" / "MEMORY.md"
        memory.parent.mkdir()
        settings = home / "settings.json"
        settings.write_text(json.dumps({"autoMemoryEnabled": True, "autoMemoryDirectory": str(memory.parent)}))
        text = "- TEST early\n" + "# filler\n" * 198 + "- TEST partial\n  only indoors\n"
        partial = "TEST partial"
    else:
        memory = home / "memories" / "memory_summary.md"
        memory.parent.mkdir()
        settings = home / "config.toml"
        settings.write_text('[features]\nmemories = true\n[memories]\nuse_memories = true\nversion = "v1"\n')
        text = "- TEST early\n- " + "a" * 11000 + " only indoors\n"
        partial = memory_entries(_token_prefix(text, 2500))[-1]
    memory.write_bytes(text.encode("utf-8"))
    started = time.time() - 10
    for path in (settings, memory):
        os.utime(path, (started - 5, started - 5))
    entries = read_host_memory(
        host, home, project=project, session_started=started, deadline=time.monotonic() + 2, environment={}
    )
    assert "TEST early" in entries
    assert partial not in entries


def test_claude_default_project_directory_and_project_overrides(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-claude"
    project = tmp_path / "TEST-project"
    home.mkdir()
    (project / ".claude").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": False}), encoding="utf-8")
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(project.resolve()))
    memory = home / "projects" / slug / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("TEST synthetic", encoding="utf-8")
    state = inspect_host_memory("claude-code", home, project=project, environment={})
    assert state["switches"]["autoMemoryEnabled"] is True
    assert state["files"][0]["path"] == str(memory)


def test_session_start_mark_is_specific_and_never_moves_forward(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scope_recall.adapters.clients.session_marks import mark_session_start, session_start_time

    config = SimpleNamespace(data_directory=tmp_path)
    now = [1000.0]
    monkeypatch.setattr("scope_recall.adapters.clients.session_marks.time.time", lambda: now[0])
    assert session_start_time(config, "codex", "TEST-a") is None
    mark_session_start(config, "codex", "TEST-a")
    now[0] = 2000.0
    mark_session_start(config, "codex", "TEST-a")
    assert session_start_time(config, "codex", "TEST-a") == 1000.0
    assert session_start_time(config, "claude-code", "TEST-a") is None
    assert session_start_time(config, "codex", "TEST-b") is None


def test_claude_project_policy_precedence_and_invalid_settings(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    project = tmp_path / "TEST-project"
    policy_root = tmp_path / "TEST-program-files"
    home.mkdir()
    (project / ".claude").mkdir(parents=True)
    policy = policy_root / "ClaudeCode" / "managed-settings.json"
    policy.parent.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    (project / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryEnabled": False}), encoding="utf-8")
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    policy.write_text(json.dumps({"autoMemoryEnabled": False}), encoding="utf-8")
    env = {"ProgramFiles": str(policy_root)}
    assert (
        inspect_host_memory("claude-code", home, project=project, environment=env)["switches"]["autoMemoryEnabled"]
        is False
    )
    policy.write_text("{invalid", encoding="utf-8")
    assert (
        inspect_host_memory("claude-code", home, project=project, environment=env)["switches"]["autoMemoryEnabled"]
        is None
    )


def test_claude_git_subdirectory_uses_repository_project_memory(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    repo = tmp_path / "TEST-repo"
    nested = repo / "nested"
    home.mkdir()
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(repo.resolve()))
    expected = home / "projects" / slug / "memory" / "MEMORY.md"
    state = inspect_host_memory("claude-code", home, project=nested, environment={})
    assert state["files"][0]["path"] == str(expected)


def test_claude_managed_dropins_override_project_settings(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    project = tmp_path / "TEST-project"
    policy_root = tmp_path / "TEST-program-files"
    home.mkdir()
    project.mkdir()
    dropins = policy_root / "ClaudeCode" / "managed-settings.d"
    dropins.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    (dropins / "10-memory.json").write_text(json.dumps({"autoMemoryEnabled": False}), encoding="utf-8")
    state = inspect_host_memory("claude-code", home, project=project, environment={"ProgramFiles": str(policy_root)})
    assert state["switches"]["autoMemoryEnabled"] is False


def test_claude_repository_directory_block_is_not_treated_as_loaded(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    project = tmp_path / "TEST-project"
    home.mkdir()
    (project / ".claude").mkdir(parents=True)
    (home / "settings.json").write_text(
        json.dumps({"autoMemoryEnabled": True, "permissions": {"blockReadsOutsideWorkingDirectories": True}}),
        encoding="utf-8",
    )
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(tmp_path / "TEST-memory")}), encoding="utf-8"
    )
    state = inspect_host_memory("claude-code", home, project=project, environment={})
    assert state["switches"]["autoMemoryEnabled"] is None


def test_claude_worktree_uses_common_repository_memory(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    repo = tmp_path / "TEST-repo"
    worktree = tmp_path / "TEST-worktree"
    gitdir = repo / ".git" / "worktrees" / "TEST-worktree"
    home.mkdir()
    gitdir.mkdir(parents=True)
    worktree.mkdir()
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text("gitdir: " + str(gitdir) + "\n", encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(repo.resolve()))
    expected = home / "projects" / slug / "memory" / "MEMORY.md"
    state = inspect_host_memory("claude-code", home, project=worktree, environment={})
    assert state["files"][0]["path"] == str(expected)
