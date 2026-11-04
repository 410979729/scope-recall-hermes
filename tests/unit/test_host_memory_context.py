"""Exact host memory comparisons preserve separate statements."""

import os
import tempfile

import pytest

#: Off Windows, a session in a repository subdirectory tells the checkout from the user's home by ``HOME``.
USER_ENV = {"HOME": os.path.join(tempfile.gettempdir(), "TEST-user-home")}


@pytest.fixture(autouse=True)
def no_machine_policy(monkeypatch):
    """Keep the test machine's own Claude Code administrator settings (a fixed system path off Windows) out."""
    from scope_recall.adapters import host_memory

    monkeypatch.setattr(host_memory, "_claude_policy_directory", lambda env: None)


@pytest.mark.parametrize(
    ("text", "complete"),
    [
        ("# Only in staging\n- TEST deployment is permitted.", "Only in staging TEST deployment is permitted."),
        ("- Only in staging\n  - TEST deployment is permitted.", "Only in staging TEST deployment is permitted."),
        ("TEST deployment is permitted.\nOnly in staging.", "TEST deployment is permitted. Only in staging."),
        (
            "# TEST project\n## Only in staging\n- Cluster A\n  - Window B\n    - TEST deployment is permitted.",
            "TEST project Only in staging Cluster A Window B TEST deployment is permitted.",
        ),
    ],
)
def test_host_statement_keeps_all_markdown_context(text, complete):
    from scope_recall.adapters.host_memory import memory_entries

    entries = memory_entries(text)
    assert "TEST deployment is permitted." not in entries
    assert complete in entries


@pytest.mark.parametrize(
    "field", ["valid_from", "valid_to", "occurred_at", "procedure", "intention", "alias", "new_qualifier"]
)
def test_claim_with_unrepresented_qualification_is_not_duplicate(field):
    import json

    from scope_recall.core.recall_restatement import host_memory_duplicate
    from scope_recall.core.retrieval import RetrievedObject

    payload = {
        "subject": "TEST project",
        "predicate": "palette",
        "value_text": "amber",
        "conditions": [],
        field: "TEST",
    }
    obj = RetrievedObject(
        "TEST-claim",
        1,
        "claim",
        json.dumps(payload),
        "human_direct",
        "current",
        "",
        (),
        "direct_report",
        True,
        metadata=(("state", "active"),),
    )
    assert not host_memory_duplicate(obj, ("TEST project palette amber",))


def test_host_comparison_normalizes_candidate_once(monkeypatch):
    import scope_recall.core.recall_restatement as restatement
    from scope_recall.core.retrieval import RetrievedObject

    calls = []
    normalize = restatement.normalized_statement
    monkeypatch.setattr(restatement, "normalized_statement", lambda text: calls.append(text) or normalize(text))
    obj = RetrievedObject(
        "TEST-event", 1, "event", "TEST unmatched", "human_direct", "current", "", (), "direct_report", True
    )
    assert not restatement.host_memory_duplicate(obj, frozenset({"TEST a", "TEST b", "TEST c"}))
    assert calls == ["TEST unmatched"]


def test_unstructured_claim_cannot_prove_absence_of_qualifiers():
    from scope_recall.core.recall_restatement import host_memory_duplicate
    from scope_recall.core.retrieval import RetrievedObject

    sentence = "TEST project palette amber"
    obj = RetrievedObject(
        "TEST-claim",
        1,
        "claim",
        sentence,
        "human_direct",
        "current",
        "",
        (),
        "direct_report",
        True,
        metadata=(("state", "active"),),
    )
    assert not host_memory_duplicate(obj, frozenset({sentence}))


def test_claude_project_settings_follow_session_directory(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home, repo = tmp_path / "TEST-home", tmp_path / "TEST-repo"
    nested = repo / "nested"
    home.mkdir()
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude").mkdir()
    (nested / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}))
    (nested / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryEnabled": False}))
    state = inspect_host_memory("claude-code", home, project=nested, environment=USER_ENV)
    assert state["switches"]["autoMemoryEnabled"] is False


def test_host_memory_rejects_combined_file_size(tmp_path):
    import os
    import time

    from scope_recall.adapters.host_memory import read_host_memory

    home = tmp_path / "TEST-hermes"
    (home / "memories").mkdir(parents=True)
    settings = home / "config.yaml"
    settings.write_text("memory:\n  memory_enabled: true\n  user_profile_enabled: true\n")
    started = time.time() - 10
    for path in (settings, home / "memories" / "MEMORY.md", home / "memories" / "USER.md"):
        if path != settings:
            path.write_bytes(("a" * 40000).encode())
        os.utime(path, (started - 5, started - 5))
    assert not read_host_memory(
        "hermes", home, project=home, session_started=started, deadline=time.monotonic() + 2, environment=USER_ENV
    )


def test_host_context_expansion_stays_bounded(tmp_path):
    import os
    import time

    from scope_recall.adapters.host_memory import read_host_memory

    home = tmp_path / "TEST-hermes-expanded"
    (home / "memories").mkdir(parents=True)
    settings = home / "config.yaml"
    settings.write_text("memory:\n  memory_enabled: true\n  user_profile_enabled: false\n")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("# " + "h" * 10000 + "\n" + "- TEST fact\n" * 10)
    started = time.time() - 10
    for path in (settings, memory):
        os.utime(path, (started - 5, started - 5))
    assert not read_host_memory(
        "hermes", home, project=home, session_started=started, deadline=time.monotonic() + 2, environment=USER_ENV
    )


def test_host_reads_validated_configuration_only_once(tmp_path, monkeypatch):
    import os
    import time

    import scope_recall.adapters.host_memory as host_memory

    home = tmp_path / "TEST-hermes"
    (home / "memories").mkdir(parents=True)
    settings = home / "config.yaml"
    settings.write_text("memory:\n  memory_enabled: true\n  user_profile_enabled: false\n")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("- TEST fact\n")
    started = time.time() - 10
    for path in (settings, memory):
        os.utime(path, (started - 5, started - 5))
    reads = []
    original = host_memory._host_text

    def observe(path, session_start):
        if path == settings:
            reads.append(path)
        return original(path, session_start)

    monkeypatch.setattr(host_memory, "_host_text", observe)
    assert "TEST fact" in host_memory.read_host_memory(
        "hermes", home, project=home, session_started=started, deadline=time.monotonic() + 2, environment=USER_ENV
    )
    assert reads == [settings]


def test_configuration_changed_after_snapshot_does_not_enable_deduplication(tmp_path, monkeypatch):
    import os
    import time

    import scope_recall.adapters.host_memory as host_memory

    home = tmp_path / "TEST-hermes-race"
    (home / "memories").mkdir(parents=True)
    settings = home / "config.yaml"
    settings.write_text("memory:\n  memory_enabled: true\n  user_profile_enabled: false\n")
    memory = home / "memories" / "MEMORY.md"
    memory.write_text("- TEST fact\n")
    started = time.time() - 10
    for path in (settings, memory):
        os.utime(path, (started - 5, started - 5))
    original = host_memory._host_text

    def change_settings(path, session_start):
        if path == memory:
            settings.write_text("memory:\n  memory_enabled: false\n  user_profile_enabled: false\n")
            os.utime(settings, (started - 6, started - 6))
        return original(path, session_start)

    monkeypatch.setattr(host_memory, "_host_text", change_settings)
    assert not host_memory.read_host_memory(
        "hermes", home, project=home, session_started=started, deadline=time.monotonic() + 2, environment=USER_ENV
    )


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
    assert memory_entries(text) == (
        "Preferences TEST palette is blue",
        "Preferences TEST never publish",
        "Preferences TEST project uses Python.",
    )


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
    args = dict(project=tmp_path, session_started=started, deadline=time.monotonic() + 2, environment=USER_ENV)
    assert read_host_memory("hermes", home, **args) == frozenset({"TEST old fact"})
    os.utime(memory, (started + 1, started + 1))
    assert not read_host_memory("hermes", home, **args)
    assert not read_host_memory("hermes", home, **{**args, "session_started": None})
    assert not read_host_memory("hermes", home, **{**args, "deadline": time.monotonic() - 1})


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
        environment=USER_ENV,
    ) == frozenset({"TEST stable host fact"})
    assert (
        read_host_memory(
            "hermes",
            home,
            project=tmp_path,
            session_started=None,
            session_id="TEST-other",
            deadline=time.monotonic() + 2,
            environment=USER_ENV,
        )
        == frozenset()
    )


def test_claim_without_conditions_matches_its_complete_statement():
    import json

    from scope_recall.core.recall_restatement import host_memory_duplicate
    from scope_recall.core.retrieval import RetrievedObject

    payload = {"subject": "TEST project", "predicate": "palette", "value_text": "amber", "conditions": []}
    obj = RetrievedObject(
        "TEST-claim",
        1,
        "claim",
        json.dumps(payload),
        "human_direct",
        "current",
        "",
        (),
        "direct_report",
        True,
        metadata=(("state", "active"),),
    )
    assert host_memory_duplicate(obj, ("TEST project palette amber",))
    payload["conditions"] = ["only indoors"]
    obj = RetrievedObject(
        "TEST-claim",
        1,
        "claim",
        json.dumps(payload),
        "human_direct",
        "current",
        "",
        (),
        "direct_report",
        True,
        metadata=(("state", "active"),),
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
            "hermes",
            home,
            project=tmp_path,
            session_started=started,
            deadline=time.monotonic() + 2,
            environment=USER_ENV,
        )
        == frozenset()
    )


def test_adjacent_sentences_in_same_paragraph_are_one_entry():
    from scope_recall.adapters.host_memory import memory_entries

    assert memory_entries("TEST first fact.\nTEST second fact.") == ("TEST first fact. TEST second fact.",)


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
        host, home, project=project, session_started=started, deadline=time.monotonic() + 2, environment=USER_ENV
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
    state = inspect_host_memory("claude-code", home, project=project, environment=USER_ENV)
    assert state["switches"]["autoMemoryEnabled"] is True
    assert state["files"][0]["path"] == str(memory)


def test_claude_project_policy_precedence_and_invalid_settings(tmp_path, monkeypatch):
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
    policy_dir = policy_root / "ClaudeCode"
    monkeypatch.setattr("scope_recall.adapters.host_memory._claude_policy_directory", lambda env: policy_dir)
    env = USER_ENV
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
    state = inspect_host_memory("claude-code", home, project=nested, environment=USER_ENV)
    assert state["files"][0]["path"] == str(expected)


def test_claude_managed_dropins_override_project_settings(tmp_path, monkeypatch):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home = tmp_path / "TEST-home"
    project = tmp_path / "TEST-project"
    policy_root = tmp_path / "TEST-program-files"
    home.mkdir()
    project.mkdir()
    dropins = policy_root / "ClaudeCode" / "managed-settings.d"
    policy_dir = policy_root / "ClaudeCode"
    monkeypatch.setattr("scope_recall.adapters.host_memory._claude_policy_directory", lambda env: policy_dir)
    dropins.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    (dropins / "10-memory.json").write_text(json.dumps({"autoMemoryEnabled": False}), encoding="utf-8")
    state = inspect_host_memory("claude-code", home, project=project, environment=USER_ENV)
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
    state = inspect_host_memory("claude-code", home, project=project, environment=USER_ENV)
    assert state["switches"]["autoMemoryEnabled"] is None


def test_claude_outside_read_block_is_not_disabled_by_higher_setting(tmp_path):
    import json

    from scope_recall.adapters.host_memory import inspect_host_memory

    home, project = tmp_path / "TEST-home", tmp_path / "TEST-project"
    home.mkdir()
    (project / ".claude").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"permissions": {"blockReadsOutsideWorkingDirectories": True}}))
    (project / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "autoMemoryEnabled": True,
                "autoMemoryDirectory": str(tmp_path / "TEST-memory"),
                "permissions": {"blockReadsOutsideWorkingDirectories": False},
            }
        )
    )
    state = inspect_host_memory("claude-code", home, project=project, environment=USER_ENV)
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
    state = inspect_host_memory("claude-code", home, project=worktree, environment=USER_ENV)
    assert state["files"][0]["path"] == str(expected)


def test_claude_windows_worktree_settings_use_session_directory(tmp_path):
    import json
    import os

    from scope_recall.adapters.host_memory import inspect_host_memory

    if os.name != "nt":
        pytest.skip("Windows local settings use the session directory")
    home = tmp_path / "TEST-home"
    repo = tmp_path / "TEST-repo"
    worktree = tmp_path / "TEST-worktree"
    gitdir = repo / ".git" / "worktrees" / "TEST-worktree"
    home.mkdir()
    gitdir.mkdir(parents=True)
    (repo / ".claude").mkdir()
    (worktree / ".claude").mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (worktree / ".git").write_text("gitdir: " + str(gitdir) + "\n")
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryEnabled": False}))
    (worktree / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryEnabled": False}))
    (worktree / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryEnabled": True}))
    state = inspect_host_memory("claude-code", home, project=worktree, environment=USER_ENV)
    assert state["switches"]["autoMemoryEnabled"] is True
