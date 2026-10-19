"""The shared store's operator commands, run the way an operator runs them.

A Hermes home is installed on its own, its store moved aside, and the home
attached to a shared store with the grants and routes that store had; then it
is checked, reinstalled over, detached, and the store copied and adopted.
Nothing here opens a real instance or a person's memory.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from scope_recall.adapters.clients import remote_client
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.hermes import HermesIdentityError, bind_hermes_identity, shared_entries
from scope_recall.adapters.hermes.shared_entries import CLIENT_HOSTS, read_attachment, read_shared_payload
from scope_recall.maintenance import cli, install_codex, install_dsh, install_workbuddy, shared, shared_run
from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.install import apply_install, apply_uninstall, plan_install, plan_uninstall
from scope_recall.maintenance.install_common import InstallError, InstallPlan
from scope_recall.maintenance.shared import main
from scope_recall.runtime import instance_config
from scope_recall.runtime.instance_config import RuntimeInstanceConfig
from scope_recall.runtime.model_budget import read_auxiliary_budget_status
from scope_recall.runtime.worker_entry import load_config

AGENT = "TEST-agent"


def _run(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _installed(tmp_path, name, *, workspace=None):
    """A Hermes home installed on its own, the way apply-install leaves one."""
    home = (tmp_path / f"TEST-{name}-home").resolve()
    plugin = (tmp_path / f"TEST-{name}-plugin" / "scope-recall").resolve()
    project = (tmp_path / f"TEST-{name}-project").resolve()
    plugin.mkdir(parents=True)
    project.mkdir()
    options = dict(
        host="hermes",
        target_plugin_dir=plugin,
        instance_root=home,
        project_root=project,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
        agent_workspace=workspace,
    )
    apply_install(plan_install(**options))
    return home, options


def _routes(home, *, model=None):
    """A runtime config with its own model routes, bound to the home's own store."""
    manifest = json.loads((home / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    embedding = {"credential_env": "TEST_EMBED_KEY"}
    if model is not None:
        embedding.update(model=model, endpoint="https://example.test/v1/embeddings", dimensions=64, dialect="openai")
    return {
        "binding": {
            "agent_id": manifest["agent_id"],
            "installation_id": manifest["installation_id"],
            "data_directory": manifest["data_directory"],
            "scope_ids": manifest["scope_ids"],
            "test_mode": manifest["test_mode"],
        },
        "session_id": "TEST-background",
        "allowed_scope_ids": manifest["scope_ids"],
        "owner_id": "TEST-worker",
        "auxiliary": {
            "external_embedding": False,
            "external_consolidation": False,
            "embedding": embedding,
            "installation_dir": manifest["data_directory"],
        },
    }


def _moved_aside(home, routes):
    (home / "scope-recall" / "runtime-config.json").write_text(json.dumps(routes), encoding="utf-8")
    archive = home / "scope-recall.local-TEST"
    (home / "scope-recall").rename(archive)
    return archive


def _attach(capsys, home, root, archive, entry, name):
    return _run(
        capsys,
        "attach",
        "--host",
        "hermes",
        "--instance-root",
        str(home),
        "--root",
        str(root),
        "--entry",
        entry,
        "--display-name",
        name,
        "--grants-from",
        str(archive / "installation.json"),
        "--runtime-config-from",
        str(archive / "runtime-config.json"),
    )


def _bind(home):
    return bind_hermes_identity(
        "TEST-session",
        hermes_home=str(home),
        platform="cli",
        agent_identity=AGENT,
        agent_workspace="hermes",
        user_id="local",
        agent_context="primary",
    )


@pytest.fixture
def root(tmp_path, capsys):
    store = (tmp_path / "TEST-shared").resolve()
    code, result = _run(capsys, "init-shared", "--root", str(store), "--agent-id", AGENT)
    assert (code, result["status"]) == (0, "initialized")
    return store


def test_a_home_moved_aside_attaches_with_the_grants_it_had(tmp_path, capsys, root):
    home, options = _installed(tmp_path, "agent3")
    own = json.loads((home / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    archive = _moved_aside(home, _routes(home))

    code, result = _attach(capsys, home, root, archive, "agent3", "天三")
    assert (code, result["status"], result["entry_id"]) == (0, "attached", "agent3")
    assert result["new_scopes"] == result["store_scopes"] == len(own["scope_ids"])
    assert Path(result["receipt"]).is_file()

    identity = _bind(home)
    assert identity.entry_id == "agent3" and identity.binding.installation_kind == "shared"
    assert identity.manifest.audiences == tuple(own["audiences"]), "the grants it had, unchanged"
    entry = load_config(home / "scope-recall" / "runtime-config.json")
    assert entry.binding == identity.binding
    worker = load_config(root / "runtime-config.json")
    assert worker.binding.scope_ids == frozenset(read_shared_payload(root)["scope_ids"])
    assert (worker.session_id, worker.owner_id) == ("shared-background", "shared-scope-recall-worker")
    # Every model request reserves in the spend ledger first, and nothing but an installer makes one.
    assert entry.auxiliary.ledger_path == home / "scope-recall" / "auxiliary-budget.sqlite3"
    assert worker.auxiliary.ledger_path == root / "auxiliary-budget.sqlite3"
    assert sorted(result["ledgers_created"]) == sorted(
        str(path) for path in (entry.auxiliary.ledger_path, worker.auxiliary.ledger_path)
    )
    for ledger in (entry.auxiliary.ledger_path, worker.auxiliary.ledger_path):
        assert read_auxiliary_budget_status(ledger) == {
            "ledger_exists": True,
            "requests": 0,
            "charge_micro_usd": 0,
            "meter_breach": False,
        }

    # After an upgrade the installer runs again over the attached home.
    installed = apply_install(plan_install(**options))
    assert installed.installation_id == identity.binding.installation_id

    report = run_doctor(host="hermes", instance_root=home, python_executable=Path(sys.executable))
    assert report.binding_ok and report.database_present
    assert report.shared_store == {"root": str(root), "entry_id": "agent3", "entry_name": "天三"}

    code, listing = _run(capsys, "entries", "--root", str(root))
    assert code == 0 and listing["store"] == "ok"
    assert [(row["entry_id"], row["pointer_present"]) for row in listing["entries"]] == [("agent3", True)]


def test_attach_refuses_a_home_whose_own_store_is_still_in_place(tmp_path, capsys, root):
    home, _options = _installed(tmp_path, "agent3")
    code, result = _run(
        capsys,
        "attach",
        "--host",
        "hermes",
        "--instance-root",
        str(home),
        "--root",
        str(root),
        "--entry",
        "agent3",
        "--display-name",
        "天三",
    )
    assert code == 2 and "still has its own store" in result["error"]
    assert read_shared_payload(root)["entries"] == []


def test_a_second_entry_must_use_the_worker_s_embedding_model(tmp_path, capsys, root):
    first, _options = _installed(tmp_path, "agent3")
    _attach(capsys, first, root, _moved_aside(first, _routes(first)), "agent3", "天三")
    second, _options = _installed(tmp_path, "agent2")
    archive = _moved_aside(second, _routes(second, model="TEST-other-embedding"))

    code, result = _attach(capsys, second, root, archive, "agent2", "天二")
    assert code == 2 and result["error"].startswith("embedding_space_differs")
    assert [entry["entry_id"] for entry in read_shared_payload(root)["entries"]] == ["agent3"]
    assert not (second / "scope-recall" / "attachment.json").exists()


def test_a_later_entry_widens_the_worker_and_keeps_its_routes(tmp_path, capsys, root):
    first, _options = _installed(tmp_path, "agent3")
    _attach(capsys, first, root, _moved_aside(first, _routes(first)), "agent3", "天三")
    before = json.loads((root / "runtime-config.json").read_text(encoding="utf-8"))
    second, _options = _installed(tmp_path, "agent2")
    code, result = _attach(capsys, second, root, _moved_aside(second, _routes(second)), "agent2", "天二")
    assert code == 0
    after = json.loads((root / "runtime-config.json").read_text(encoding="utf-8"))
    assert set(after["binding"]["scope_ids"]) == set(read_shared_payload(root)["scope_ids"])
    assert after["auxiliary"] == before["auxiliary"] and after["session_id"] == before["session_id"]


def test_detach_leaves_the_memories_and_the_record(tmp_path, capsys, root):
    home, _options = _installed(tmp_path, "agent3")
    _attach(capsys, home, root, _moved_aside(home, _routes(home)), "agent3", "天三")

    code, result = _run(capsys, "detach", "--instance-root", str(home))
    assert (code, result["status"], result["home_directory_left"]) == (0, "detached", False)
    assert not (home / "scope-recall").exists()
    assert any(
        Path(kept).name == "entry-auxiliary-budget.sqlite3"
        for kept in json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))["backups"]
    ), "the entry's spend record is kept"
    with pytest.raises(HermesIdentityError):
        _bind(home)
    record = read_shared_payload(root)["entries"][0]
    assert record["entry_id"] == "agent3" and record["detached_at"]
    code, listing = _run(capsys, "entries", "--root", str(root))
    assert listing["entries"][0]["pointer_present"] is False and listing["entries"][0]["first_seen"]


def test_a_copied_store_opens_only_after_adopt_and_takes_its_entries_from_new_homes(tmp_path, capsys, root):
    home, _options = _installed(tmp_path, "agent3")
    _attach(capsys, home, root, _moved_aside(home, _routes(home)), "agent3", "天三")
    copy = (tmp_path / "TEST-shared-moved").resolve()
    shutil.copytree(root, copy)

    code, listing = _run(capsys, "entries", "--root", str(copy))
    assert listing["store"] == "IDENTITY_UNBOUND:store_moved:run_adopt"
    code, result = _run(capsys, "adopt", "--root", str(copy))
    assert (code, result["status"]) == (0, "adopted")
    assert result["previous_directory"] == os.path.normcase(str(root)), "the store records its directory normcased"
    assert load_config(copy / "runtime-config.json").binding.data_directory == copy
    code, listing = _run(capsys, "entries", "--root", str(copy))
    assert listing["store"] == "ok"

    # The old home still points at the original store, so the copy takes the entry from a new home.
    new_home, _options = _installed(tmp_path, "agent3-new")
    code, result = _attach(capsys, new_home, copy, _moved_aside(new_home, _routes(new_home)), "agent3", "天三")
    assert code == 0 and _bind(new_home).binding.data_directory == copy
    assert _bind(home).binding.data_directory == root, "the original is untouched"


def test_init_refuses_a_directory_in_use_or_inside_an_agent_home(tmp_path, capsys):
    used = tmp_path / "TEST-used"
    used.mkdir()
    (used / "something").write_text("TEST", encoding="utf-8")
    code, result = _run(capsys, "init-shared", "--root", str(used))
    assert code == 2 and "new or empty" in result["error"]
    home, _options = _installed(tmp_path, "agent3")
    code, result = _run(capsys, "init-shared", "--root", str(home / "shared"))
    assert code == 2 and "inside an agent's home" in result["error"]


def _with_scopes(home, name, count):
    """An installation that has seen many conversations: each brings a scope of its own."""
    path = home / "scope-recall" / "installation.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for index in range(count):
        scope = f"conversation:TEST-{name}-{index:03d}-{'x' * 64}"
        manifest["scope_ids"].append(scope)
        manifest["audiences"].append(
            dict(
                manifest["audiences"][0],
                kind="conversation",
                chat_type="group",
                chat_id=f"TEST-group-{index:03d}",
                allowed_scope_ids=[scope],
                writable_scope_ids=[scope],
                capture_scope_id=scope,
            )
        )
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_a_worker_config_past_64_kb_still_takes_entries_and_detaches(tmp_path, capsys, root):
    """The worker's config lists every scope of the store twice, about 120 bytes each, so a store of a few hundred
    scopes gives it more than 64 KB: attach, detach and adopt still read and write it."""
    homes = []
    for name in ("agent3", "agent1", "agent5"):
        home, _options = _installed(tmp_path, name)
        _with_scopes(home, name, 120)
        code, result = _attach(capsys, home, root, _moved_aside(home, _routes(home)), name, name)
        assert (code, result["status"]) == (0, "attached"), result
        homes.append(home)
    assert (root / "runtime-config.json").stat().st_size > 65536
    assert len(load_config(root / "runtime-config.json").binding.scope_ids) == len(
        read_shared_payload(root)["scope_ids"]
    )
    code, result = _run(capsys, "detach", "--instance-root", str(homes[-1]))
    assert (code, result["status"]) == (0, "detached"), result
    copy = tmp_path / "TEST-moved"
    shutil.copytree(root, copy)
    code, result = _run(capsys, "adopt", "--root", str(copy))
    assert (code, result["status"]) == (0, "adopted"), result


def _hermes_pair(tmp_path, capsys, root, *, second_workspace=None):
    """Two Hermes entries of the store, the first one's routes the worker's."""
    first, _options = _installed(tmp_path, "agent3")
    _attach(capsys, first, root, _moved_aside(first, _routes(first)), "agent3", "天三")
    second, _options = _installed(tmp_path, "agent2", workspace=second_workspace)
    _attach(capsys, second, root, _moved_aside(second, _routes(second)), "agent2", "天二")
    return first, second


CLIENT_NAMES = {"codex": "Codex", "claude-code": "Claude Code", "workbuddy": "WorkBuddy", "dsh": "DeepSeek Harness"}


def _attach_client(capsys, root, home, *, host="claude-code", like="all", capture="agent3", routes=None):
    """``attach`` for a client entry named after its host; ``like`` or ``capture`` None leaves that option out."""
    argv = ["attach", "--host", host, "--instance-root", str(home), "--root", str(root)]
    argv += ["--entry", host, "--display-name", CLIENT_NAMES[host]]
    if like is not None:
        argv += ["--grants-like", like]
    if capture is not None:
        argv += ["--capture-like", capture]
    if routes is not None:
        argv += ["--runtime-config-from", str(routes)]
    return _run(capsys, *argv)


def test_a_client_attaches_as_the_owner_installs_and_is_checked_like_an_entry(tmp_path, capsys, root):
    first, _second = _hermes_pair(tmp_path, capsys, root)
    worker_before = (root / "runtime-config.json").read_bytes()
    client = (tmp_path / "TEST-claude-code-home").resolve()
    client.mkdir()

    code, result = _attach_client(capsys, root, client, routes=first / "scope-recall" / "runtime-config.json")
    assert (code, result["status"], result["new_scopes"]) == (0, "attached", 0), result
    assert result["worker_runtime_config_written"] is False
    assert (root / "runtime-config.json").read_bytes() == worker_before, "an unchanged worker is not restarted"
    assert read_attachment(client).host == "claude-code"
    config = load_shared_client(client, "claude-code")
    agent3_owner = next(
        row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private"
    )
    assert config.audience.capture_scope_id == agent3_owner["capture_scope_id"]
    assert config.audience.allowed_scope_ids == frozenset(agent3_owner["allowed_scope_ids"])
    entry = load_config(client / "scope-recall" / "runtime-config.json")
    assert entry.binding == config.to_binding() and entry.host_adapter == "claude-code"
    assert (entry.session_id, entry.owner_id) == ("claude-code-background", "claude-code-scope-recall")
    assert entry.auxiliary.ledger_path == client / "scope-recall" / "auxiliary-budget.sqlite3"
    assert str(entry.auxiliary.ledger_path) in result["ledgers_created"]

    plugin = (tmp_path / "TEST-claude" / "skills" / "scope-recall").resolve()
    options = dict(
        host="claude-code",
        target_plugin_dir=plugin,
        instance_root=client,
        project_root=None,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
    )
    installed = apply_install(plan_install(**options))
    assert installed.installation_id == config.installation_id
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert sorted(hooks) == ["SessionEnd", "Stop", "UserPromptSubmit"]
    command = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "--home " + client.as_posix() + " --host claude-code" in command
    assert chr(92) not in command, "a shell would read a backslash as an escape"
    server = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["scope-recall"]
    assert server["args"][-4:] == ["--home", client.as_posix(), "--host", "claude-code"]
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert (manifest["hooks"], manifest["mcpServers"]) == ("./hooks/hooks.json", "./.mcp.json")
    assert sorted(path.parent.name for path in (plugin / "skills").glob("*/SKILL.md")) == ["scope-recall-memory"]
    assert apply_install(plan_install(**options)).installation_id == config.installation_id, "an upgrade reinstalls"

    report = run_doctor(host="claude-code", instance_root=client, python_executable=Path(sys.executable))
    assert report.binding_ok and report.database_present
    assert report.shared_store == {"root": str(root), "entry_id": "claude-code", "entry_name": "Claude Code"}
    code, listing = _run(capsys, "entries", "--root", str(root))
    assert [(row["entry_id"], row["host"], row["pointer_present"]) for row in listing["entries"]][-1] == (
        "claude-code",
        "claude-code",
        True,
    )

    code, result = _run(capsys, "detach", "--instance-root", str(client))
    assert (code, result["status"]) == (0, "detached")
    assert not (client / "scope-recall").exists()


def _workbuddy_home(tmp_path):
    """A WorkBuddy home shaped as one in use is: keys of WorkBuddy's own and another tool's prompt hook in its
    settings, and one MCP server of WorkBuddy's.  Every value is made up."""
    home = (tmp_path / "TEST-profile" / ".workbuddy").resolve()
    home.mkdir(parents=True)
    settings = {
        "sandbox": {"enabled": True, "profile": "TEST"},
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [{"type": "command", "command": "TEST-other-tool", "timeout": 5}]}
            ]
        },
        "claw": {"TEST": [1, 2]},
        "enabledPlugins": {"TEST-plugin@TEST-market": True},
    }
    # The person's own MCP servers.  WorkBuddy's .mcp.json beside them is the app's record of its connector proxy,
    # which its agent is started with alone: no install touches it.
    mcp = {
        "mcpServers": {"TEST-other-server": {"command": "C:/TEST/other.exe", "args": ["--TEST"], "description": "TEST"}}
    }
    (home / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    (home / "mcp.json").write_text(json.dumps(mcp, indent=2), encoding="utf-8")
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "connector-proxy": {"type": "http", "url": "http://127.0.0.1:9/mcp", "description": "TEST proxy"}
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return home, settings, mcp


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_workbuddy_entry_installs_into_workbuddy_s_own_files_and_uninstalls_only_its_own(
    tmp_path, capsys, root, monkeypatch
):
    """WorkBuddy joins a shared store as the other clients do, the owner at this machine.  Its hooks and MCP server go
    into WorkBuddy's own settings.json and mcp.json, beside whatever else is there; a copy of each file is kept
    first, a second install changes nothing, an older hook of this entry is updated where it stands, and uninstall
    takes out this entry's entries and nothing else."""
    first, _second = _hermes_pair(tmp_path, capsys, root)
    entry = (tmp_path / "TEST-workbuddy-entry").resolve()
    entry.mkdir()
    code, result = _run(
        capsys,
        "attach",
        "--host",
        "workbuddy",
        "--instance-root",
        str(entry),
        "--root",
        str(root),
        "--entry",
        "workbuddy",
        "--display-name",
        "WorkBuddy",
        "--grants-like",
        "all",
        "--capture-like",
        "agent3",
        "--runtime-config-from",
        str(first / "scope-recall" / "runtime-config.json"),
    )
    assert (code, result["status"]) == (0, "attached"), result
    assert read_attachment(entry).host == "workbuddy"
    config = load_shared_client(entry, "workbuddy")
    runtime = load_config(entry / "scope-recall" / "runtime-config.json")
    assert runtime.binding == config.to_binding() and runtime.host_adapter == "workbuddy"
    assert (runtime.session_id, runtime.owner_id) == ("workbuddy-background", "workbuddy-scope-recall")

    workbuddy, settings, mcp = _workbuddy_home(tmp_path)
    before = {name: (workbuddy / name).read_bytes() for name in ("settings.json", "mcp.json")}
    proxy, present = (workbuddy / ".mcp.json").read_bytes(), {path.name for path in workbuddy.iterdir()}
    options = dict(
        host="workbuddy",
        target_plugin_dir=workbuddy,
        instance_root=entry,
        project_root=None,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
    )
    plan = plan_install(**options)
    assert plan.conflicts == [], plan.conflicts
    changes = [(change.action, Path(change.path).name) for change in plan.changes]
    assert ("merge", "settings.json") in changes and ("merge", "mcp.json") in changes
    assert ".mcp.json" not in {name for _action, name in changes}
    assert changes[-1] == ("restart", ".workbuddy") and "start it again" in plan.changes[-1].detail
    assert "approve the MCP server scope-recall" in plan.changes[-1].detail
    assert {path.name for path in workbuddy.iterdir()} == present, "a plan writes nothing"

    # A resident recall server runs the package it was started from: one of the installation an install replaces
    # (another venv, an older version) would hold the entry's lock against the new one's, so the install stops it.
    from scope_recall.adapters.clients import local_endpoint

    stopped = []
    monkeypatch.setattr(local_endpoint, "stop_residents", lambda home, host: stopped.append((home, host)) or [])
    installed = apply_install(plan)
    assert stopped == [(entry, "workbuddy")]
    assert installed.installation_id == config.installation_id
    assert sorted(Path(path).name for path in installed.files_merged) == ["mcp.json", "settings.json"]
    assert not any(name in Path(path).name for path in installed.files_written for name in before), (
        "WorkBuddy's files are never the receipt's"
    )
    copies = {Path(path).name: Path(path).read_bytes() for path in installed.backups if Path(path).name in before}
    assert copies == before, "each file is copied before it is changed"
    assert {path.name for path in workbuddy.iterdir()} == present, "nothing else is left in WorkBuddy's home"
    assert (workbuddy / ".mcp.json").read_bytes() == proxy, "WorkBuddy's own proxy record is not touched"

    written = _read(workbuddy / "settings.json")
    assert list(written) == list(settings)
    assert {key: value for key, value in written.items() if key != "hooks"} == {
        key: value for key, value in settings.items() if key != "hooks"
    }
    assert written["hooks"]["UserPromptSubmit"][0] == settings["hooks"]["UserPromptSubmit"][0], "another hook stays"
    ours = {event: groups[-1]["hooks"] for event, groups in written["hooks"].items()}
    assert {event: [(hook["type"], hook["timeout"]) for hook in hooks] for event, hooks in ours.items()} == {
        "UserPromptSubmit": [("command", 15)],
        "Stop": [("command", 10)],
        "SessionEnd": [("command", 10)],
    }
    command = ours["UserPromptSubmit"][0]["command"]
    assert all(hooks[0]["command"] == command for hooks in ours.values())
    assert command.startswith(f'"{Path(sys.executable).as_posix()}" -I -B -m scope_recall.adapters.codex.hook_entry ')
    assert command.endswith(f' --home "{entry.as_posix()}" --host workbuddy || exit 1')
    assert "\\" not in command and "~" not in command, "Git Bash reads a backslash as an escape and ~ as its own home"
    assert shlex.split(command)[0] == Path(sys.executable).as_posix()
    servers = _read(workbuddy / "mcp.json")["mcpServers"]
    assert list(servers) == ["TEST-other-server", "scope-recall"]
    assert servers["TEST-other-server"] == mcp["mcpServers"]["TEST-other-server"]
    assert (
        servers["scope-recall"]["type"] == "stdio"
        and servers["scope-recall"]["command"] == Path(sys.executable).as_posix()
    )
    assert servers["scope-recall"]["description"] == install_workbuddy.SERVER_DESCRIPTION
    assert servers["scope-recall"]["args"][-4:] == ["--home", entry.as_posix(), "--host", "workbuddy"]

    # Run again, as after an upgrade: WorkBuddy's files are not touched.
    stamps = {name: ((workbuddy / name).read_bytes(), (workbuddy / name).stat().st_mtime_ns) for name in before}
    plan = plan_install(**options)
    assert {("unchanged", "settings.json"), ("unchanged", "mcp.json")} <= {
        (change.action, Path(change.path).name) for change in plan.changes
    }
    again = apply_install(plan)
    assert again.files_merged == [] and not any(Path(path).name in before for path in again.backups)
    assert {name: ((workbuddy / name).read_bytes(), (workbuddy / name).stat().st_mtime_ns) for name in before} == stamps

    # The entry's hook from an older interpreter, with a hook another tool added after it in the same group: updated
    # where it stands, the other hook kept after it.
    edited = _read(workbuddy / "settings.json")
    old = edited["hooks"]["Stop"][0]["hooks"][0]
    old.update(
        command=old["command"].replace(Path(sys.executable).as_posix(), "C:/TEST-old-venv/python.exe"), timeout=3
    )
    edited["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "TEST-after"})
    (workbuddy / "settings.json").write_text(json.dumps(edited, indent=2), encoding="utf-8")
    assert [Path(path).name for path in apply_install(plan_install(**options)).files_merged] == ["settings.json"]
    assert _read(workbuddy / "settings.json")["hooks"]["Stop"] == [
        {
            "hooks": [
                {"type": "command", "command": command, "timeout": 10},
                {"type": "command", "command": "TEST-after"},
            ]
        }
    ]

    report = run_doctor(host="workbuddy", instance_root=entry, python_executable=Path(sys.executable))
    assert report.binding_ok and report.database_present
    assert report.shared_store == {"root": str(root), "entry_id": "workbuddy", "entry_name": "WorkBuddy"}

    removal = plan_uninstall(instance_root=entry)
    assert removal.conflicts == [] and removal.files_to_remove == []
    assert sorted(Path(path).name for path in removal.unmerged_files) == ["mcp.json", "settings.json"]
    held = {name: (workbuddy / name).read_bytes() for name in before}
    removed = apply_uninstall(removal)
    assert stopped == [(entry, "workbuddy")] * 4, "each install and the uninstall stop the entry's resident server"
    assert sorted(Path(path).name for path in removed.unmerged_files) == ["mcp.json", "settings.json"]
    assert {Path(path).name: Path(path).read_bytes() for path in removed.backups} == held
    assert _read(workbuddy / "settings.json") == {
        **settings,
        "hooks": {
            "UserPromptSubmit": settings["hooks"]["UserPromptSubmit"],
            "Stop": [{"hooks": [{"type": "command", "command": "TEST-after"}]}],
        },
    }
    assert _read(workbuddy / "mcp.json") == mcp and (workbuddy / ".mcp.json").read_bytes() == proxy
    assert plan_uninstall(instance_root=entry).unmerged_files == [], "nothing of this entry's is left"

    code, result = _run(capsys, "detach", "--instance-root", str(entry))
    assert (code, result["status"]) == (0, "detached")


def test_a_workbuddy_install_refuses_what_it_cannot_merge_and_writes_nothing(tmp_path, capsys, monkeypatch):
    entry = (tmp_path / "TEST-workbuddy-entry").resolve()
    entry.mkdir()
    workbuddy, settings, mcp = _workbuddy_home(tmp_path)
    options = dict(
        host="workbuddy",
        target_plugin_dir=workbuddy,
        instance_root=entry,
        project_root=None,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
    )
    plan = plan_install(**options)
    assert any("not attached to a shared store" in conflict for conflict in plan.conflicts), plan.conflicts
    with pytest.raises(InstallError):
        apply_install(plan)

    def conflicts(settings_value=None, mcp_value=None, *, settings_text=None):
        (workbuddy / "settings.json").write_text(
            settings_text if settings_text is not None else json.dumps(settings_value or settings), encoding="utf-8"
        )
        (workbuddy / "mcp.json").write_text(json.dumps(mcp_value or mcp), encoding="utf-8")
        before = {path.name: path.read_bytes() for path in workbuddy.iterdir()}
        found = plan_install(**options).conflicts
        assert {path.name: path.read_bytes() for path in workbuddy.iterdir()} == before
        return found

    # WorkBuddy would run both, and each would record the turn.
    another = (
        '"C:/TEST/python.exe" -I -B -m scope_recall.adapters.codex.hook_entry --home "C:/TEST-other" --host workbuddy'
    )
    remote = '"C:/TEST/python.exe" -I -B -m scope_recall.adapters.codex.remote_client --config "C:/TEST/client.json"'
    for command in (another, remote):
        value = json.loads(json.dumps(settings))
        value["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
        assert any("already runs another Scope Recall hook for Stop" in found for found in conflicts(value)), command
    taken = {"mcpServers": {**mcp["mcpServers"], "scope-recall": {"type": "http", "url": "http://127.0.0.1:9/mcp"}}}
    assert any("already has an MCP server named scope-recall" in found for found in conflicts(mcp_value=taken))
    commented = "// WorkBuddy reads comments\n" + json.dumps(settings)
    assert any("is not plain JSON" in found for found in conflicts(settings_text=commented))
    missing = dict(options, target_plugin_dir=tmp_path / "TEST-nowhere" / ".workbuddy")
    assert any("does not exist" in found for found in plan_install(**missing).conflicts)
    assert not (tmp_path / "TEST-nowhere").exists()

    # The command line finds WorkBuddy's home as WorkBuddy does; the other hosts still name their plugin directory.
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(workbuddy))
    argv = ["--instance-root", str(entry), "--agent-id", AGENT, "--python", sys.executable]
    assert cli.main(["plan-install", "--host", "workbuddy", *argv]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert (printed["host"], printed["target_plugin_dir"]) == ("workbuddy", str(workbuddy))
    with pytest.raises(SystemExit):
        cli.main(["plan-install", "--host", "claude-code", *argv])


def test_a_workbuddy_hook_command_is_quoted_with_forward_slashes_and_its_wait_covers_start_and_work(tmp_path):
    """WorkBuddy runs a hook through Git Bash on Windows, and blocks a prompt whose hook runs past its wait."""
    python = Path("C:/TEST venv/Scripts/python.exe")
    plan = InstallPlan(
        host="workbuddy",
        target_plugin_dir=tmp_path,
        instance_root=Path("D:/TEST homes/workbuddy"),
        project_root=None,
        agent_id=AGENT,
        python_executable=python,
        env_file=Path("D:/TEST homes/embedding.env"),
    )
    command = install_workbuddy.hook_command(plan)
    assert command.startswith('"C:/TEST venv/Scripts/python.exe" ')
    assert "\\" not in command and "~" not in command
    assert shlex.split(command) == [
        "C:/TEST venv/Scripts/python.exe",
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.codex.hook_entry",
        "--home",
        "D:/TEST homes/workbuddy",
        "--host",
        "workbuddy",
        "--env-file",
        "D:/TEST homes/embedding.env",
        "||",
        "exit",
        "1",
    ]
    # WorkBuddy blocks the prompt on a hook's exit 2, argparse's code when an older package does not know an option.
    assert command.endswith(" || exit 1")
    for unsafe in (
        "C:/TEST$HOME/python.exe",
        "C:/TEST`id`/python.exe",
        'C:/TEST"/python.exe',
        "C:/TEST/" + chr(0x5929),
    ):
        with pytest.raises(InstallError, match="Git Bash"):
            install_workbuddy.quoted(Path(unsafe), "interpreter")
    tilde = plan_install(
        host="workbuddy",
        target_plugin_dir=tmp_path,
        instance_root="~/TEST-workbuddy-entry",
        project_root=None,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
    )
    assert "~" not in install_workbuddy.hook_command(tilde), "a home given with ~ is written out"

    # WorkBuddy's timeout is in seconds; each wait covers the interpreter's start and the most the hook may work.
    most = instance_config._SECONDS_BOUNDS["hook_processing_seconds"][1]
    assert install_workbuddy.HOOK_WORK_SECONDS >= most
    assert sorted(install_workbuddy.HOOK_TIMEOUTS) == ["SessionEnd", "Stop", "UserPromptSubmit"]
    for event, seconds in install_workbuddy.HOOK_TIMEOUTS.items():
        assert seconds >= install_workbuddy.HOOK_WORK_SECONDS + install_workbuddy.START_SECONDS, event
    assert remote_client.HOOK_TIMEOUTS["workbuddy"] == install_workbuddy.HOOK_TIMEOUTS


def test_a_workbuddy_file_is_written_back_in_its_own_line_endings_and_byte_order_mark():
    """WorkBuddy's files are written as WorkBuddy writes JSON, in the line endings, final newline and byte order mark
    each had: a file saved by a Windows editor keeps its CRLF and its BOM."""
    value = {"keep": "TEST 值", "hooks": {}}
    windows = install_workbuddy.encode_config(value, b'\xef\xbb\xbf{\r\n  "keep": "TEST"\r\n}')
    assert windows.startswith(b"\xef\xbb\xbf") and windows.count(b"\n") == windows.count(b"\r\n") > 0
    assert not windows.endswith(b"\n"), "no final newline, as before"
    assert json.loads(windows.decode("utf-8-sig")) == value
    plain = install_workbuddy.encode_config(value, b'{\n  "keep": "TEST"\n}\n')
    assert not plain.startswith(b"\xef\xbb\xbf") and b"\r" not in plain and plain.endswith(b"}\n")
    assert install_workbuddy.encode_config(value, None).endswith(b"}\n"), "a new file ends with a newline"


def _weixin(home, user="TEST-wx-1"):
    """The owner's Weixin DM on the gateway of ``home``, as Hermes passes it."""
    return bind_hermes_identity(
        "TEST-wx-session",
        hermes_home=str(home),
        platform="weixin",
        user_id=user,
        chat_type="dm",
        chat_id=user,
        thread_id="",
        gateway_session_key=f"agent:main:weixin:dm:{user}",
        agent_identity=AGENT,
        agent_workspace="hermes",
        agent_context="primary",
    )


def test_an_attached_entry_s_grants_change_from_an_edited_copy_and_back(tmp_path, capsys, root):
    """An attached entry's grants change from an edited copy: the operator prints them, adds a gateway owner route
    and a read of another scope, sees the change, applies it with a backup and a receipt, and puts the old grants back
    the same way.  Rows stay exact; what the store holds keeps its scopes."""
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    manifest = root / "installation.json"
    original = json.loads(manifest.read_text(encoding="utf-8"))
    edit = tmp_path / "TEST-grants.json"
    code, shown = _run(capsys, "grants", "--instance-root", str(first), "--out", str(edit))
    assert (code, shown["status"], shown["entry_id"]) == (0, "shown", "agent3"), shown
    document = json.loads(edit.read_text(encoding="utf-8"))
    record = next(entry for entry in original["entries"] if entry["entry_id"] == "agent3")
    assert {key: document[key] for key in ("owner_principals", "audience_scopes", "audiences")} == {
        key: record[key] for key in ("owner_principals", "audience_scopes", "audiences")
    }
    code, again = _run(capsys, "grants", "--instance-root", str(first), "--out", str(edit))
    assert code == 2 and "exists" in again["error"], "an edit is never overwritten"
    elsewhere_out = tmp_path / "TEST-grants-unused.json"
    code, both = _run(
        capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--out", str(elsewhere_out)
    )
    assert code == 2 and "--out" in both["error"] and not elsewhere_out.exists(), "not silently ignored"
    assert _weixin(first).runtime_audience.capability_gaps, "the gateway DM binds no owner scope yet"

    owner = next(row for row in document["audiences"] if row["kind"] == "owner_private" and row["platform"] == "cli")
    elsewhere = next(entry for entry in original["entries"] if entry["entry_id"] == "agent2")["scope_ids"][0]
    assert elsewhere not in owner["allowed_scope_ids"]
    document["owner_principals"].append({"platform": "weixin", "user_id": "TEST-wx-1"})
    owner["allowed_scope_ids"].append(elsewhere)  # the owner reads what the other entry's chats hold, writes as it did
    dm = dict(owner, platform="weixin", user_id="TEST-wx-1", chat_type="dm", chat_id="TEST-wx-1", thread_id="")
    document["audiences"].append(dm)
    edit.write_text(json.dumps(document), encoding="utf-8")

    code, preview = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit))
    assert (code, preview["status"]) == (0, "preview"), preview
    added = {(row["platform"], row["user_id"], row["kind"]): row for row in preview["rows_added"]}
    assert set(added) == {("weixin", "TEST-wx-1", "owner_private"), ("cli", owner["user_id"], "owner_private")}
    assert elsewhere in added[("weixin", "TEST-wx-1", "owner_private")]["read"]
    assert elsewhere in added[("cli", owner["user_id"], "owner_private")]["read"]
    [removed] = preview["rows_removed"]
    assert (removed["platform"], elsewhere in removed["read"]) == ("cli", False), "the owner row as it was"
    assert preview["principals_added"] == ["weixin/TEST-wx-1"] and preview["entry_scopes_added"] == [elsewhere]
    assert preview["owner_rows_granting_nothing"] == [] and preview["entry_runtime_config_rebound"]
    bound = {change["route"].split("/")[0]: change for change in preview["routes_changed"]}
    assert set(bound) == {"cli", "weixin"}, "the routes that bind otherwise, as they bind"
    assert bound["weixin"]["before"] == {"read": [], "write": [], "capture": None}
    assert elsewhere in bound["weixin"]["after"]["read"] and bound["weixin"]["after"]["capture"] is not None
    assert elsewhere in bound["cli"]["after"]["read"] and elsewhere not in bound["cli"]["before"]["read"]
    assert json.loads(manifest.read_text(encoding="utf-8")) == original, "a preview writes nothing"

    code, applied = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--apply")
    assert (code, applied["status"]) == (0, "applied"), applied
    assert Path(applied["receipt"]).is_file() and Path(applied["rollback"]).is_file()
    assert applied["entry_runtime_config_rebound"]
    worker = root / "runtime-config.json"
    assert json.loads(worker.read_text(encoding="utf-8"))["binding"]["scope_ids"] == original["scope_ids"]
    dm_identity = _weixin(first)
    assert dm_identity.runtime_audience.includes_owner_private and not dm_identity.runtime_audience.capability_gaps
    assert elsewhere in dm_identity.runtime_audience.allowed_scope_ids
    assert elsewhere in _bind(first).runtime_audience.allowed_scope_ids, "the CLI reads it too"
    entry = load_config(first / "scope-recall" / "runtime-config.json")
    assert entry.binding == _bind(first).binding, "the entry's runtime config binds its new scopes"
    assert not _weixin(first, "TEST-wx-2").runtime_audience.includes_owner_private, "exact: another user is not it"
    assert read_shared_payload(root)["scope_ids"] == original["scope_ids"], "no scope moved"

    code, back = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", applied["rollback"], "--apply")
    assert (code, back["status"]) == (0, "applied"), back
    restored = next(entry for entry in read_shared_payload(root)["entries"] if entry["entry_id"] == "agent3")
    assert restored == record and _weixin(first).runtime_audience.capability_gaps
    code, same = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", applied["rollback"])
    assert (code, same["status"]) == (0, "unchanged"), same


def test_a_grants_apply_that_fails_puts_back_what_it_replaced_and_a_second_run_finishes(
    tmp_path, capsys, root, monkeypatch
):
    """A write that fails after the manifest changed would leave the entry bound to its old scopes, and running the
    command again would say unchanged.  The copies go back on a failure; and a run that finds the manifest done but the
    entry's runtime config not rebinds it."""
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    manifest, entry = root / "installation.json", first / "scope-recall" / "runtime-config.json"
    before_manifest, before_entry = manifest.read_bytes(), entry.read_bytes()
    elsewhere = read_shared_payload(root)["entries"][1]["scope_ids"][0]

    def read_more(document):
        for row in document["audiences"]:
            if row["kind"] == "owner_private":
                row["allowed_scope_ids"].append(elsewhere)

    real = shared.write_json

    def failing(path, value):
        if Path(path) == entry:
            raise OSError("TEST disk full")
        return real(path, value)

    monkeypatch.setattr(shared, "write_json", failing)
    code, failed = _granted(capsys, tmp_path, first, read_more)
    assert code == 2 and "disk full" in failed["error"]
    assert (manifest.read_bytes(), entry.read_bytes()) == (before_manifest, before_entry), "both put back"
    monkeypatch.setattr(shared, "write_json", real)

    code, applied = _granted(capsys, tmp_path, first, read_more)
    assert (code, applied["status"]) == (0, "applied")
    copy = Path(applied["receipt"]).with_suffix("") / f"entry-{entry.name}"
    entry.write_bytes(copy.read_bytes())  # as a run that died between the two writes left it
    document = tmp_path / "TEST-grants-agent3.json"
    code, again = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(document))
    assert (code, again["status"], again["entry_runtime_config_rebound"]) == (0, "preview", True), again
    code, again = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(document), "--apply")
    assert (code, again["status"], again["rollback"]) == (0, "applied", None), again
    assert load_config(entry).binding == _bind(first).binding


def test_a_grants_change_started_while_another_is_written_cannot_undo_it(tmp_path, capsys, root, monkeypatch):
    """Each change carries the whole manifest: written over one made meanwhile, it undid that one (a revoked route came
    back).  A second grants --apply started while the first writes its manifest waits for the store's lock and is
    refused; run again, it is planned from the manifest the first wrote, and both changes hold."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.3)
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    mine = read_shared_payload(root)["entries"][0]["audience_scopes"]["owner_private"]
    edits = {}
    for home in (first, second):
        code, shown = _run(capsys, "grants", "--instance-root", str(home))
        document = shown["grants"]
        if home == second:
            document["audiences"][0]["allowed_scope_ids"].append(mine)
        else:
            document["owner_principals"].append({"platform": "weixin", "user_id": "TEST-wx-1"})
        edits[home] = tmp_path / f"TEST-plan-{shown['entry_id']}.json"
        edits[home].write_text(json.dumps(document), encoding="utf-8")
    apply_second = ("grants", "--instance-root", str(second), "--grants-from", str(edits[second]), "--apply")
    other = _meanwhile(monkeypatch, capsys, shared, "write_shared_payload", *apply_second)
    code, applied = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edits[first]), "--apply")
    assert (code, applied["status"]) == (0, "applied"), applied
    assert other.get("code") == 2 and "is writing this store" in other["result"]["error"], other
    code, applied = _run(capsys, *apply_second)
    assert (code, applied["status"]) == (0, "applied"), applied
    entries = {entry["entry_id"]: entry for entry in read_shared_payload(root)["entries"]}
    assert {"platform": "weixin", "user_id": "TEST-wx-1"} in entries["agent3"]["owner_principals"]
    assert mine in entries["agent2"]["audiences"][0]["allowed_scope_ids"]


def test_two_rows_on_one_route_are_each_a_grant(tmp_path, capsys, root):
    """Two rows on one route are each a grant: both are kept, and taking away the one that reads more is a change,
    and written.  An owner row whose user is no owner principal grants nothing and is named, and the grants before a
    change put such a row back as it was."""
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    elsewhere = read_shared_payload(root)["entries"][1]["scope_ids"][0]

    def doubled(document):
        owner = next(row for row in document["audiences"] if row["kind"] == "owner_private")
        document["audiences"].append(dict(owner, allowed_scope_ids=[*owner["allowed_scope_ids"], elsewhere]))
        document["audiences"].append(
            dict(owner, kind="conversation", allowed_scope_ids=[elsewhere], writable_scope_ids=[], capture_scope_id="")
        )

    code, applied = _granted(capsys, tmp_path, first, doubled)
    assert (code, applied["status"]) == (0, "applied"), applied
    code, shown = _run(capsys, "grants", "--instance-root", str(first))
    document = shown["grants"]
    wider = next(
        row for row in document["audiences"] if elsewhere in row["allowed_scope_ids"] and row["kind"] == "owner_private"
    )
    document["audiences"].remove(wider)
    edit = tmp_path / "TEST-narrower.json"
    edit.write_text(json.dumps(document), encoding="utf-8")
    code, preview = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit))
    assert (code, preview["status"]) == (0, "preview"), preview
    assert len(preview["rows_removed"]) == 1 and preview["entry_scopes_removed"] == [], "the scope stays the entry's"
    code, narrowed = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--apply")
    assert (code, narrowed["status"]) == (0, "applied"), narrowed

    code, shown = _run(capsys, "grants", "--instance-root", str(first))
    document = shown["grants"]
    owner = next(row for row in document["audiences"] if row["kind"] == "owner_private")
    document["audiences"].append(
        dict(
            owner,
            platform="desktop",
            user_id="basic:TEST-guest",
            chat_type="private",
            chat_id="basic:TEST-guest",
            thread_id="main",
        )
    )
    edit.write_text(json.dumps(document), encoding="utf-8")
    code, inert = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--apply")
    assert (code, inert["status"]) == (0, "applied"), inert
    assert inert["owner_rows_granting_nothing"] == ["desktop/basic:TEST-guest/private/basic:TEST-guest/main//hermes"]
    document["owner_principals"].append({"platform": "desktop", "user_id": "basic:TEST-guest"})
    edit.write_text(json.dumps(document), encoding="utf-8")
    code, active = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--apply")
    assert (code, active["status"], active["owner_rows_granting_nothing"]) == (0, "applied", []), active
    code, back = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", active["rollback"], "--apply")
    assert (code, back["status"]) == (0, "applied"), back
    assert back["owner_rows_granting_nothing"] == ["desktop/basic:TEST-guest/private/basic:TEST-guest/main//hermes"]


def test_two_runs_of_one_command_in_one_second_keep_apart(tmp_path):
    from scope_recall.maintenance.shared_run import Run

    at = "2026-10-15T12:00:00Z"
    one, two = Run(tmp_path, "grants-TEST", at), Run(tmp_path, "grants-TEST", at)
    assert one.folder != two.folder and one.folder.is_dir() and two.folder.is_dir()
    receipt = Path(one.receipt({"status": "TEST"}))
    assert receipt == one.folder.with_name(one.folder.name + ".json") and one.folder.is_dir(), "kept, though empty"
    assert Run(tmp_path, "grants-TEST", at).folder not in (one.folder, two.folder)
    # import-entry's moments have fractions of a second: a receipt named by replacing a suffix would lose the fraction
    # and the command, and two runs in one second each find a name of their own.
    fine = "2026-10-15T12:00:00.123456+00:00"
    three, four = Run(tmp_path, "import-TEST", fine), Run(tmp_path, "import-TEST", fine)
    receipts = {Path(run.receipt({"status": "TEST"})).name for run in (three, four)}
    assert len(receipts) == 2 and all("import-TEST" in name for name in receipts), receipts


def test_a_grants_change_is_refused_whole_when_any_of_it_is_wrong(tmp_path, capsys, root):
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    manifest = root / "installation.json"
    before = manifest.read_bytes()
    code, shown = _run(capsys, "grants", "--instance-root", str(first))
    document = shown["grants"]
    owner = next(row for row in document["audiences"] if row["kind"] == "owner_private")
    edit = tmp_path / "TEST-grants.json"

    def refused(change, needle):
        candidate = json.loads(json.dumps(document))
        change(candidate)
        edit.write_text(json.dumps(candidate), encoding="utf-8")
        code, result = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--apply")
        assert code == 2 and needle in result["error"], result
        assert manifest.read_bytes() == before, "nothing is written"

    refused(lambda d: d.update(entry_id="agent2"), "another entry")
    refused(lambda d: d.update(extra=1), "holds schema")
    refused(lambda d: d["audiences"].append(dict(owner, platform="weixin", user_id="unknown")), "explicit exact")
    refused(lambda d: d["audiences"].append(dict(owner, writable_scope_ids=["TEST-not-read"])), "subset")
    other = json.loads(manifest.read_text(encoding="utf-8"))["entries"][1]["scope_ids"][0]
    only_other = dict(owner, allowed_scope_ids=[other], writable_scope_ids=[], capture_scope_id="")
    refused(lambda d: d.update(audiences=[only_other]), "no row of the entry reads")
    refused(lambda d: d.update(audiences=[only_other], audience_scopes={"owner_private": other}), "held by no entry")
    refused(
        lambda d: next(r for r in d["audiences"] if r["kind"] == "owner_private")["allowed_scope_ids"].append(
            "TEST-new-scope"
        ),
        "does not hold",
    )
    code, result = _run(capsys, "grants", "--instance-root", str(first), "--apply")
    assert code == 2 and "--grants-from" in result["error"]
    code, result = _run(capsys, "grants", "--instance-root", str(tmp_path / "TEST-not-attached"))
    assert code == 2 and "not attached" in result["error"]


def test_a_grants_preview_names_a_route_whose_captures_move_when_rows_change_order(tmp_path, capsys, root):
    """Of the rows a session matches, the first that captures into a scope it may write decides where its messages
    go: two owner rows on one route, swapped, move the owner's captures into another scope with every row, scope and
    principal list of the preview empty.  The preview names the route as it binds, before and after."""
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    payload = read_shared_payload(root)
    mine = payload["entries"][0]["audience_scopes"]["owner_private"]
    elsewhere = payload["entries"][1]["scope_ids"][0]

    def cli_owner_rows(document):
        return [row for row in document["audiences"] if row["kind"] == "owner_private" and row["platform"] == "cli"]

    def second_owner_row(document):
        [owner] = cli_owner_rows(document)
        document["audiences"].append(
            dict(owner, allowed_scope_ids=[elsewhere], writable_scope_ids=[elsewhere], capture_scope_id=elsewhere)
        )

    code, applied = _granted(capsys, tmp_path, first, second_owner_row)
    assert (code, applied["status"], applied["rows_reordered"]) == (0, "applied", False), applied
    [wider] = applied["routes_changed"]
    assert wider["route"].startswith("cli/") and wider["before"]["capture"] == wider["after"]["capture"] == mine
    assert set(wider["after"]["read"]) - set(wider["before"]["read"]) == {elsewhere}, wider
    assert _bind(first).runtime_audience.capture_scope_id == mine, "the first row still decides"
    code, shown = _run(capsys, "grants", "--instance-root", str(first))
    document = shown["grants"]
    one, two = (document["audiences"].index(row) for row in cli_owner_rows(document))
    document["audiences"][one], document["audiences"][two] = document["audiences"][two], document["audiences"][one]
    edit = tmp_path / "TEST-swapped.json"
    edit.write_text(json.dumps(document), encoding="utf-8")
    code, preview = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit))
    assert (code, preview["status"]) == (0, "preview"), preview
    assert preview["rows_added"] == preview["rows_removed"] == [] and preview["rows_reordered"], preview
    [moved] = preview["routes_changed"]
    assert moved["route"].startswith("cli/") and (moved["before"]["capture"], moved["after"]["capture"]) == (
        mine,
        elsewhere,
    ), moved
    assert (moved["before"]["read"], moved["before"]["write"]) == (moved["after"]["read"], moved["after"]["write"])
    code, swapped = _run(capsys, "grants", "--instance-root", str(first), "--grants-from", str(edit), "--apply")
    assert (code, swapped["status"]) == (0, "applied"), swapped
    assert _bind(first).runtime_audience.capture_scope_id == elsewhere, "the preview said what binding does"


def test_a_grants_apply_whose_receipt_fails_puts_back_what_it_replaced(tmp_path, capsys, root, monkeypatch):
    """The receipt is part of the change: a receipt that cannot be written (a full disk) puts back what the change
    replaced, rather than leave the grants changed with no word of them as they were."""
    from scope_recall.maintenance import shared_run

    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    manifest, entry = root / "installation.json", first / "scope-recall" / "runtime-config.json"
    before = (manifest.read_bytes(), entry.read_bytes())
    elsewhere = read_shared_payload(root)["entries"][1]["scope_ids"][0]

    def read_more(document):
        for row in document["audiences"]:
            if row["kind"] == "owner_private":
                row["allowed_scope_ids"].append(elsewhere)

    real = shared_run.Run.receipt

    def failing(self, body):
        if body.get("status") == "applied":
            raise OSError("TEST disk full")
        return real(self, body)

    monkeypatch.setattr(shared_run.Run, "receipt", failing)
    code, failed = _granted(capsys, tmp_path, first, read_more)
    assert code == 2 and "disk full" in failed["error"], failed
    assert (manifest.read_bytes(), entry.read_bytes()) == before, "both put back"
    monkeypatch.setattr(shared_run.Run, "receipt", real)
    code, applied = _granted(capsys, tmp_path, first, read_more)
    assert (code, applied["status"], applied["entry_runtime_config_rebound"]) == (0, "applied", True), applied
    assert Path(applied["receipt"]).is_file() and Path(applied["rollback"]).is_file()


def test_a_grants_apply_puts_back_past_a_reader_and_names_what_it_could_not(tmp_path, capsys, root, monkeypatch):
    """Putting the copies back is tried again as every write is, past a worker reading the manifest for a moment,
    which Windows refuses a replace for; a file it still could not put back is named with its copy."""
    import os

    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    manifest, entry = root / "installation.json", first / "scope-recall" / "runtime-config.json"
    before = (manifest.read_bytes(), entry.read_bytes())
    elsewhere = read_shared_payload(root)["entries"][1]["scope_ids"][0]

    def read_more(document):
        for row in document["audiences"]:
            if row["kind"] == "owner_private":
                row["allowed_scope_ids"].append(elsewhere)

    real_write, real_replace = shared.write_json, os.replace
    refusals = {"left": 3}

    def failing(path, value):
        if Path(path) == entry:
            raise OSError("TEST disk full")
        return real_write(path, value)

    def held(source, target):
        if Path(target) == manifest and Path(source).name.endswith(".restore") and refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError("TEST a reader holds the manifest")
        return real_replace(source, target)

    monkeypatch.setattr(shared, "write_json", failing)
    monkeypatch.setattr(os, "replace", held)
    code, failed = _granted(capsys, tmp_path, first, read_more)
    assert code == 2 and "disk full" in failed["error"] and refusals["left"] == 0, failed
    assert (manifest.read_bytes(), entry.read_bytes()) == before, "put back once the reader let go"

    refusals["left"] = 10_000
    code, failed = _granted(capsys, tmp_path, first, read_more)
    assert code == 2 and "disk full" in failed["error"] and "could not be undone" in failed["error"], failed
    assert f"store-installation.json -> {manifest}" in failed["error"], "the file left changed, with its copy"
    assert entry.read_bytes() == before[1], "the other file is still put back"


def _granted(capsys, tmp_path, home, change):
    code, shown = _run(capsys, "grants", "--instance-root", str(home))
    document = shown["grants"]
    change(document)
    edit = tmp_path / f"TEST-grants-{shown['entry_id']}.json"
    edit.write_text(json.dumps(document), encoding="utf-8")
    return _run(capsys, "grants", "--instance-root", str(home), "--grants-from", str(edit), "--apply")


def test_a_client_entry_s_grants_change_and_keep_its_own_route(tmp_path, capsys, root):
    """A client writes only where every owner row reads: with two Hermes entries whose chats keep apart, the one
    has to read the other's owner scope first, which is a grants change too.  Then the client's own row reads more."""
    first, second = _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-elsewhere")
    payload = read_shared_payload(root)
    mine = next(e for e in payload["entries"] if e["entry_id"] == "agent3")["audience_scopes"]["owner_private"]
    elsewhere = next(e for e in payload["entries"] if e["entry_id"] == "agent2")["audience_scopes"]["owner_private"]
    client = (tmp_path / "TEST-claude-code-home").resolve()
    code, result = _attach_client(capsys, root, client, like="agent3", capture="agent3")
    assert code == 2, "agent2's owner rows do not read agent3's scope"

    def read_mine(document):
        for row in document["audiences"]:
            if row["kind"] == "owner_private":
                row["allowed_scope_ids"].append(mine)

    code, applied = _granted(capsys, tmp_path, second, read_mine)
    assert (code, applied["status"]) == (0, "applied"), applied
    code, result = _attach_client(capsys, root, client, like="agent3", capture="agent3")
    assert code == 0, result
    assert elsewhere not in load_shared_client(client, "claude-code").audience.allowed_scope_ids
    code, shown = _run(capsys, "grants", "--instance-root", str(client))
    document = shown["grants"]
    document["audiences"][0]["allowed_scope_ids"].append(elsewhere)
    edit = tmp_path / "TEST-client-grants.json"
    edit.write_text(json.dumps(document), encoding="utf-8")
    code, applied = _run(capsys, "grants", "--instance-root", str(client), "--grants-from", str(edit), "--apply")
    assert (code, applied["status"]) == (0, "applied"), applied
    assert elsewhere in load_shared_client(client, "claude-code").audience.allowed_scope_ids
    for field, value in (("chat_id", "TEST-someone"), ("gateway_session_key", "TEST-key")):
        moved = json.loads(json.dumps(document))
        moved["audiences"][0][field] = value
        edit.write_text(json.dumps(moved), encoding="utf-8")
        code, refused = _run(capsys, "grants", "--instance-root", str(client), "--grants-from", str(edit))
        assert code == 2 and "its own route" in refused["error"], (field, refused)
    # A conversation row in the owner row's place reads and captures, and so does an owner row of another principal:
    # the client takes whoever types into it for the owner, so its route keeps an owner row of an owner principal.
    someone = {"platform": "weixin", "user_id": "TEST-wx-1"}
    for name, kind, principals in (
        ("conversation", "conversation", document["owner_principals"]),
        ("another owner", "owner_private", [someone]),
        ("both", "conversation", [someone]),
    ):
        demoted = json.loads(json.dumps(document))
        demoted["audiences"][0]["kind"] = kind
        demoted["owner_principals"] = principals
        edit.write_text(json.dumps(demoted), encoding="utf-8")
        code, refused = _run(capsys, "grants", "--instance-root", str(client), "--grants-from", str(edit))
        assert code == 2 and "its own route, of an owner principal" in refused["error"], (name, refused)
    beside = json.loads(json.dumps(document))
    beside["audiences"].append(
        dict(beside["audiences"][0], kind="conversation", writable_scope_ids=[], capture_scope_id="")
    )
    edit.write_text(json.dumps(beside), encoding="utf-8")
    code, preview = _run(capsys, "grants", "--instance-root", str(client), "--grants-from", str(edit))
    assert (code, preview["status"]) == (0, "preview"), "a conversation row beside the owner row is a grant"


def test_a_client_writes_only_where_every_owner_row_reads(tmp_path, capsys, root):
    _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-other-workspace")
    client = (tmp_path / "TEST-claude-code-home").resolve()
    client.mkdir()
    code, result = _attach_client(capsys, root, client)
    assert code == 2 and "not read by these owner rows: agent2:cli" in result["error"], result
    code, result = _attach_client(capsys, root, client, like="agent3,nobody")
    assert code == 2 and "nobody" in result["error"], result
    assert [entry["entry_id"] for entry in read_shared_payload(root)["entries"]] == ["agent3", "agent2"]
    assert read_attachment(client) is None
    with pytest.raises(InstallError):
        apply_install(
            plan_install(
                host="claude-code",
                target_plugin_dir=(tmp_path / "TEST-plugin" / "scope-recall"),
                instance_root=client,
                project_root=None,
                agent_id=AGENT,
                python_executable=Path(sys.executable),
            )
        )


def test_a_codex_home_with_its_own_store_still_in_place_is_refused(tmp_path, capsys, root):
    _hermes_pair(tmp_path, capsys, root)
    codex = (tmp_path / "TEST-codex-home").resolve()
    (codex / "data").mkdir(parents=True)
    (codex / "codex-installation.json").write_text("{}", encoding="utf-8")
    code, result = _attach_client(capsys, root, codex, host="codex")
    assert code == 2 and "still has its own store" in result["error"], result
    shutil.move(str(codex / "codex-installation.json"), str(tmp_path / "TEST-codex-aside.json"))
    code, result = _attach_client(capsys, root, codex, host="codex")
    assert (code, result["status"]) == (0, "attached"), result
    assert load_shared_client(codex, "codex").entry_id == "codex"


# -- a store whose first entry is a client --------------------------------------------------------------------------


def _client_homes(tmp_path, *hosts):
    homes = {host: (tmp_path / f"TEST-{host}-home").resolve() for host in hosts}
    for home in homes.values():
        home.mkdir()
    return homes


def _owner_rows(root, entry_id):
    record = next(entry for entry in read_shared_payload(root)["entries"] if entry["entry_id"] == entry_id)
    return [row for row in record["audiences"] if row["kind"] == "owner_private"]


def _written_routes(tmp_path):
    """A runtime config written by hand, as for a store no home with routes is attached to: its binding is made up."""
    nowhere = str((tmp_path / "TEST-nowhere").resolve())
    routes = {
        "binding": {
            "agent_id": AGENT,
            "installation_id": "TEST-unbound",
            "data_directory": nowhere,
            "scope_ids": ["TEST-scope"],
        },
        "session_id": "TEST-background",
        "allowed_scope_ids": ["TEST-scope"],
        "auxiliary": {
            "external_embedding": False,
            "external_consolidation": False,
            "embedding": {"credential_env": "TEST_EMBED_KEY"},
            "installation_dir": nowhere,
        },
    }
    path = tmp_path / "TEST-routes.json"
    path.write_text(json.dumps(routes), encoding="utf-8")
    return path


@pytest.mark.parametrize("host", CLIENT_HOSTS)
def test_each_client_attaches_first_to_a_new_store_and_another_client_names_it(tmp_path, capsys, root, host):
    """With no other entry attached, a client takes no grants like another's: it gets a fresh installation's owner
    grants and brings that owner scope into the store, and its routes give the shared worker its own.  A client of
    another host attached after it names it, and both bind as the owner, reading, writing and capturing in that one
    scope, their runtime configs bound to the store whatever the written one named."""
    later = CLIENT_HOSTS[(CLIENT_HOSTS.index(host) + 1) % len(CLIENT_HOSTS)]
    homes, routes = _client_homes(tmp_path, host, later), _written_routes(tmp_path)
    code, result = _attach_client(capsys, root, homes[host], host=host, like=None, capture=None, routes=routes)
    assert (code, result.get("new_scopes"), result.get("worker_runtime_config_written")) == (0, 1, True), result
    code, result = _attach_client(capsys, root, homes[later], host=later, like=host, capture=host, routes=routes)
    assert (code, result.get("new_scopes"), result.get("worker_runtime_config_written")) == (0, 0, False), result
    [scope] = read_shared_payload(root)["scope_ids"]
    assert load_config(root / "runtime-config.json").binding.scope_ids == {scope}
    for name, home in homes.items():
        config = load_shared_client(home, name)
        audience = config.audience
        assert (audience.allowed_scope_ids, audience.writable_scope_ids, audience.capture_scope_id) == (
            frozenset({scope}),
            frozenset({scope}),
            scope,
        ), name
        entry = load_config(home / "scope-recall" / "runtime-config.json")
        assert (entry.binding, entry.host_adapter) == (config.to_binding(), name)


def test_a_client_names_only_entries_attached_beside_it(tmp_path, capsys, root):
    """On a new store the first client names no entry to take grants like, and a client after it must name attached
    ones; the first client's attach run again repeats it.  A refused attach writes nothing."""
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like="all", capture="workbuddy")
    assert code == 2 and "no other entry is attached" in result["error"], result
    assert read_shared_payload(root)["entries"] == [] and read_attachment(homes["workbuddy"]) is None
    for new_scopes in (1, 0):
        code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
        assert (code, result.get("new_scopes")) == (0, new_scopes), result
    for like, capture, refused in (
        (None, None, "needs --grants-like and --capture-like"),
        ("all", None, "needs --grants-like and --capture-like"),
        ("workbuddy,nobody", "workbuddy", "not other entries attached to this store: nobody"),
        ("all", "dsh", "not other entries attached to this store: dsh"),
    ):
        code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like=like, capture=capture)
        assert code == 2 and refused in result["error"], (like, capture, result)
    assert [entry["entry_id"] for entry in read_shared_payload(root)["entries"]] == ["workbuddy"]
    assert read_attachment(homes["dsh"]) is None
    code, result = _run(capsys, "detach", "--instance-root", str(homes["workbuddy"]))
    assert (code, result["status"]) == (0, "detached"), result
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="workbuddy", capture="workbuddy")
    assert code == 2 and "no other entry is attached" in result["error"], "a detached entry is none to be like"


def test_a_hermes_home_attached_after_the_clients_holds_their_scope_or_is_granted_it(tmp_path, capsys, root):
    """An installation made with the default workspace holds the owner scope the store's first client brought: attached
    later, it brings no scope and its owner rows read, write and capture there.  One of another workspace keeps its
    owner scope apart, and a later client capturing where the clients do is refused, until grants let each side read
    the other's; then a client attached later reads both."""
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result
    clients = load_shared_client(homes["workbuddy"], "workbuddy").audience.capture_scope_id

    desk, _options = _installed(tmp_path, "desk")
    code, result = _attach(capsys, desk, root, _moved_aside(desk, _routes(desk)), "desk", "Desk")
    assert (code, result.get("new_scopes")) == (0, 0), result
    granted = {
        (tuple(row["allowed_scope_ids"]), tuple(row["writable_scope_ids"]), row["capture_scope_id"])
        for row in _owner_rows(root, "desk")
    }
    assert granted == {((clients,), (clients,), clients)}

    apart, _options = _installed(tmp_path, "apart", workspace="TEST-elsewhere")
    code, result = _attach(capsys, apart, root, _moved_aside(apart, _routes(apart)), "apart", "Apart")
    assert (code, result.get("new_scopes")) == (0, 1), result
    [own] = {row["capture_scope_id"] for row in _owner_rows(root, "apart")}
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="all", capture="workbuddy")
    assert code == 2 and "not read by these owner rows: apart:cli" in result["error"], result

    def reads(scope):
        def change(document):
            for row in document["audiences"]:
                if row["kind"] == "owner_private":
                    row["allowed_scope_ids"].append(scope)

        return change

    for home, scope in ((apart, clients), (homes["workbuddy"], own)):
        code, applied = _granted(capsys, tmp_path, home, reads(scope))
        assert (code, applied["status"]) == (0, "applied"), applied
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="all", capture="workbuddy")
    assert code == 0, result
    audience = load_shared_client(homes["dsh"], "dsh").audience
    assert (audience.allowed_scope_ids, audience.capture_scope_id) == (frozenset({clients, own}), clients)


def test_an_empty_hermes_home_a_store_began_with_detaches_and_its_clients_keep_their_grants(tmp_path, capsys, root):
    """A store begun with an empty Hermes home, so that a client had an entry to take grants like, lets that home go:
    the clients keep the grants they were given, and a client attached later names one of them."""
    empty = (tmp_path / "TEST-empty-hermes-home").resolve()
    empty.mkdir()
    argv = ["attach", "--host", "hermes", "--instance-root", str(empty), "--root", str(root)]
    code, result = _run(capsys, *argv, "--entry", "empty", "--display-name", "Empty")
    assert code == 0, result
    homes = _client_homes(tmp_path, "codex", "claude-code")
    code, result = _attach_client(capsys, root, homes["codex"], host="codex", like="empty", capture="empty")
    assert code == 0, result
    code, result = _run(capsys, "detach", "--instance-root", str(empty))
    assert (code, result["status"]) == (0, "detached"), result
    code, result = _attach_client(capsys, root, homes["claude-code"], host="claude-code", like="all", capture="codex")
    assert (code, result.get("new_scopes")) == (0, 0), result
    [scope] = read_shared_payload(root)["scope_ids"]
    for name, home in homes.items():
        audience = load_shared_client(home, name).audience
        assert (audience.allowed_scope_ids, audience.capture_scope_id) == (frozenset({scope}), scope), name


def test_a_client_captures_only_where_every_attached_owner_row_reads_a_client_s_included(tmp_path, capsys, root):
    """A later client's capture scope is refused when an attached client's owner row does not read it, though every
    Hermes owner row does, and so is an entry to capture like whose owner rows capture into two scopes."""
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result
    clients = load_shared_client(homes["workbuddy"], "workbuddy").audience.capture_scope_id
    apart, _options = _installed(tmp_path, "apart", workspace="TEST-elsewhere")
    code, result = _attach(capsys, apart, root, _moved_aside(apart, _routes(apart)), "apart", "Apart")
    assert code == 0, result
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="all", capture="apart")
    assert (code, result.get("error")) == (
        2,
        "apart's capture scope is not read by these owner rows: workbuddy:workbuddy; "
        "what the owner says in the client would not reach them",
    ), result

    def a_dm_capturing_where_the_clients_do(document):
        document["owner_principals"].append({"platform": "weixin", "user_id": "TEST-wx-1"})
        owner = next(row for row in document["audiences"] if row["kind"] == "owner_private")
        dm = dict(owner, platform="weixin", user_id="TEST-wx-1", chat_type="dm", chat_id="TEST-wx-1", thread_id="")
        dm.update(allowed_scope_ids=[clients], writable_scope_ids=[clients], capture_scope_id=clients)
        document["audiences"].append(dm)

    code, applied = _granted(capsys, tmp_path, apart, a_dm_capturing_where_the_clients_do)
    assert (code, applied["status"]) == (0, "applied"), applied
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="all", capture="apart")
    assert (code, result.get("error")) == (2, "apart's owner rows capture into 2 scopes; name an entry with one")


def _meanwhile(monkeypatch, capsys, module, name, *argv):
    """Start the operator command ``argv`` in a thread of its own the first time ``module.name`` is called, as one run
    at that moment would be, and let it run, or wait for the store's lock, until it ends.  What it answered is in the
    dict returned."""
    original, answered = getattr(module, name), {}

    def then_another(*args, **kwargs):
        monkeypatch.setattr(module, name, original)
        other = threading.Thread(target=lambda: answered.update(code=main(list(argv))))
        other.start()
        other.join(timeout=60)
        answered["result"] = json.loads(capsys.readouterr().out)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, then_another)
    return answered


def test_two_first_attaches_started_together_leave_one_entry_and_one_scope(tmp_path, capsys, root, monkeypatch):
    """Two clients attached to a new store at once: the one holding the store's lock reads, takes its grants and
    writes; the other, started just before the first writes its manifest, waits for the lock, is refused naming the
    command that holds it, and writes nothing.  The store holds one entry and one scope, and no home points at an
    entry the manifest does not hold."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.3)
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    dsh = ("attach", "--host", "dsh", "--instance-root", str(homes["dsh"]), "--root", str(root), "--entry", "dsh")
    other = _meanwhile(monkeypatch, capsys, shared_entries, "write_shared_payload", *dsh, "--display-name", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result
    assert other.get("code") == 2, other
    assert other["result"]["error"].startswith("attach --host workbuddy --entry workbuddy (process "), other
    assert other["result"]["error"].endswith("run attach --host dsh --entry dsh again once it has finished"), other
    payload = read_shared_payload(root)
    assert ([entry["entry_id"] for entry in payload["entries"]], len(payload["scope_ids"])) == (["workbuddy"], 1)
    assert read_attachment(homes["dsh"]) is None


def test_no_command_writes_between_an_attach_taking_its_grants_and_its_write(tmp_path, capsys, root, monkeypatch):
    """A Hermes entry attached while a client takes its grants would not read the scope the client captures into.  It
    waits for the store's lock and is refused, so the client's grants are written against the manifest they were taken
    from; attached after it, the Hermes entry stands as any entry attached later does."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.3)
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result
    apart, _options = _installed(tmp_path, "apart", workspace="TEST-elsewhere")
    grants_from = _moved_aside(apart, _routes(apart)) / "installation.json"
    hermes = ("attach", "--host", "hermes", "--instance-root", str(apart), "--root", str(root), "--entry", "apart")
    hermes += ("--display-name", "Apart", "--grants-from", str(grants_from))
    other = _meanwhile(monkeypatch, capsys, shared, "_client_grants", *hermes)
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="all", capture="workbuddy")
    assert code == 0, result
    assert other.get("code") == 2 and "is writing this store" in other["result"]["error"], other
    assert [entry["entry_id"] for entry in read_shared_payload(root)["entries"]] == ["workbuddy", "dsh"]
    code, result = _run(capsys, *hermes)
    assert code == 0, result


#: Another process holding the store's lock, as a long import does, until its stdin closes.
_HOLDER = """
import sys
from pathlib import Path
from scope_recall.maintenance.shared_run import store_lock
with store_lock(Path(sys.argv[1]), "import-entry --entry desk", "2026-10-17T12:00:00Z"):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_a_command_finding_the_store_held_by_another_process_waits_then_refuses_and_names_it(
    tmp_path, capsys, root, monkeypatch
):
    """Shared-store commands that write hold the store's lock from their first read to their last write.  One that
    finds another process holding it waits a bounded time, writes nothing, and refuses with the command that holds it;
    once that one has ended it goes through."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.5)
    homes = _client_homes(tmp_path, "workbuddy")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
        waited = time.monotonic() - started
    finally:
        holder.communicate(timeout=60)
    assert code == 2 and result["error"].startswith("import-entry --entry desk (process "), result
    assert "since 2026-10-17T12:00:00Z) is writing this store" in result["error"], result
    assert 0.5 <= waited < 10, waited
    assert read_shared_payload(root)["entries"] == [] and read_attachment(homes["workbuddy"]) is None
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result


@contextmanager
def _held_elsewhere(lock, place, command="attach --host TEST --entry TEST"):
    """``lock`` (``store_lock`` or ``home_lock``) of ``place`` held by another thread, as by another command named
    ``command``, until the block ends."""
    held, done = threading.Event(), threading.Event()

    def hold():
        with lock(place, command, "2026-10-17T12:00:00Z"):
            held.set()
            done.wait(60)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    try:
        yield
    finally:
        done.set()
        holder.join(30)


@pytest.mark.parametrize("command", ["detach", "adopt", "grants", "grants --apply", "import-entry"])
def test_each_command_that_writes_the_store_waits_for_its_lock_and_changes_nothing(
    tmp_path, capsys, root, monkeypatch, command
):
    """detach, adopt, grants and import-entry hold the store's lock as attach does: with another command holding it,
    each waits, refuses naming that command, and leaves the store and the home as they were."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.2)
    [home] = _client_homes(tmp_path, "workbuddy").values()
    code, result = _attach_client(capsys, root, home, host="workbuddy", like=None, capture=None)
    assert code == 0, result
    edit = tmp_path / "TEST-grants.json"
    code, result = _run(capsys, "grants", "--instance-root", str(home), "--out", str(edit))
    assert code == 0, result
    argv = {
        "detach": ("detach", "--instance-root", str(home)),
        "adopt": ("adopt", "--root", str(root)),
        "grants": ("grants", "--instance-root", str(home), "--grants-from", str(edit)),
        "grants --apply": ("grants", "--instance-root", str(home), "--grants-from", str(edit), "--apply"),
        "import-entry": ("import-entry", "--root", str(root), "--entry", "workbuddy", "--from", str(tmp_path)),
    }[command]
    manifest, pointer = (root / "installation.json").read_bytes(), read_attachment(home)
    with _held_elsewhere(shared_run.store_lock, root):
        code, result = _run(capsys, *argv)
    assert code == 2, result
    assert result["error"].startswith("attach --host TEST --entry TEST (process "), result
    assert ((root / "installation.json").read_bytes(), read_attachment(home)) == (manifest, pointer)


@pytest.mark.parametrize("command", ["attach", "detach", "grants --apply"])
def test_each_command_that_reads_or_changes_a_home_s_pointer_waits_for_the_home_s_lock(
    tmp_path, capsys, root, monkeypatch, command
):
    """attach, detach and grants hold the lock of the home whose pointer they read or change, taken before the
    store's: with another command holding it, each waits, refuses naming that command, and leaves the home's pointer
    and the store as they were."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.2)
    [home] = _client_homes(tmp_path, "workbuddy").values()
    code, result = _attach_client(capsys, root, home, host="workbuddy", like=None, capture=None)
    assert code == 0, result
    edit = tmp_path / "TEST-grants.json"
    code, result = _run(capsys, "grants", "--instance-root", str(home), "--out", str(edit))
    assert code == 0, result
    argv = {
        "attach": ("attach", "--host", "workbuddy", "--instance-root", str(home), "--root", str(root)),
        "detach": ("detach", "--instance-root", str(home)),
        "grants --apply": ("grants", "--instance-root", str(home), "--grants-from", str(edit), "--apply"),
    }[command]
    if command == "attach":
        argv += ("--entry", "workbuddy", "--display-name", "WorkBuddy")
    manifest, pointer = (root / "installation.json").read_bytes(), read_attachment(home)
    with _held_elsewhere(shared_run.home_lock, home, "detach --instance-root TEST"):
        code, result = _run(capsys, *argv)
    assert code == 2, result
    assert result["error"].startswith("detach --instance-root TEST (process "), result
    assert "is changing this home's attachment" in result["error"], result
    assert ((root / "installation.json").read_bytes(), read_attachment(home)) == (manifest, pointer)


def test_a_codex_home_detached_from_a_store_takes_a_store_of_its_own_as_an_empty_home_does(tmp_path, capsys, root):
    """A home keeps the lock of its attachment beside where its pointer was, also once detached; a Codex install of a
    store of its own does not take that lock for content of another installation."""
    [home] = _client_homes(tmp_path, "codex").values()
    code, result = _attach_client(capsys, root, home, host="codex", like=None, capture=None)
    assert code == 0, result
    code, result = _run(capsys, "detach", "--instance-root", str(home))
    assert code == 0, result
    assert [path.name for path in home.iterdir()] == [shared_run.HOME_LOCK_FILENAME]
    assert install_codex.foreign_instance_entries(home) == []


@pytest.mark.parametrize("command", ["detach", "grants --apply"])
def test_a_command_whose_home_points_at_another_store_by_the_time_it_holds_the_locks_is_refused(
    tmp_path, capsys, root, monkeypatch, command
):
    """detach and grants find the store from the home's pointer, then take that store's lock.  A pointer that names
    another store by the time both locks are held, written by something that takes neither, is refused: neither store
    is written and the pointer is left as it is, where detach would have removed the other store's pointer and grants
    would have written the other store's manifest under this one's lock."""
    [home] = _client_homes(tmp_path, "workbuddy").values()
    code, result = _attach_client(capsys, root, home, host="workbuddy", like=None, capture=None)
    assert code == 0, result
    edit = tmp_path / "TEST-grants.json"
    code, result = _run(capsys, "grants", "--instance-root", str(home), "--out", str(edit))
    assert code == 0, result
    document = json.loads(edit.read_text(encoding="utf-8"))
    _a_group_row(document)
    edit.write_text(json.dumps(document), encoding="utf-8")
    other = (tmp_path / "TEST-copied").resolve()
    shutil.copytree(root, other)
    code, result = _run(capsys, "adopt", "--root", str(other))
    assert code == 0, result
    argv = {
        "detach": ("detach", "--instance-root", str(home)),
        "grants --apply": ("grants", "--instance-root", str(home), "--grants-from", str(edit), "--apply"),
    }[command]
    lock, pointer = shared.store_lock, home / "scope-recall" / "attachment.json"

    def pointed_elsewhere_meanwhile(*args, **kwargs):
        monkeypatch.setattr(shared, "store_lock", lock)
        pointer.write_text(json.dumps(dict(json.loads(pointer.read_text(encoding="utf-8")), root=str(other))))
        return lock(*args, **kwargs)

    monkeypatch.setattr(shared, "store_lock", pointed_elsewhere_meanwhile)
    manifests = {store: (store / "installation.json").read_bytes() for store in (root, other)}
    code, result = _run(capsys, *argv)
    assert code == 2 and "pointer changed" in result["error"], result
    assert read_attachment(home).root == other
    assert {store: (store / "installation.json").read_bytes() for store in (root, other)} == manifests


def test_a_command_holding_the_store_under_a_long_name_is_named_by_one_that_waits(tmp_path, capsys, root, monkeypatch):
    """The note that names the holder is JSON within its bound whatever the command's words weigh: a command named by
    a home path of thousands of characters, not all of them ASCII, is named, shortened, to the command that waits."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.2)
    long = "grants --instance-root C:/TEST/" + "记忆-" * 700 + "home --apply"
    with _held_elsewhere(shared_run.store_lock, root, long):
        code, result = _run(capsys, "adopt", "--root", str(root))
    assert code == 2, result
    assert result["error"].startswith("grants --instance-root C:/TEST/记忆-"), result
    assert "home --apply (process " in result["error"] and len(result["error"]) < 400, result


@pytest.mark.parametrize("held", ["home", "store"])
def test_grants_shown_waits_for_the_home_and_its_store_and_writes_no_copy_meanwhile(
    tmp_path, capsys, root, monkeypatch, held
):
    """grants without --grants-from reads the home's pointer and the store's manifest, and writes --out, holding the
    home's lock and then the store's: with another command holding either, it waits, refuses naming that command, and
    writes no copy; once that one has ended it shows the grants."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.2)
    [home] = _client_homes(tmp_path, "workbuddy").values()
    code, result = _attach_client(capsys, root, home, host="workbuddy", like=None, capture=None)
    assert code == 0, result
    out = tmp_path / "TEST-grants.json"
    with _held_elsewhere(*((shared_run.home_lock, home) if held == "home" else (shared_run.store_lock, root))):
        code, result = _run(capsys, "grants", "--instance-root", str(home), "--out", str(out))
    assert code == 2 and result["error"].startswith("attach --host TEST --entry TEST (process "), result
    assert not out.exists()
    code, result = _run(capsys, "grants", "--instance-root", str(home), "--out", str(out))
    assert (code, result.get("status"), out.is_file()) == (0, "shown", True), result


def test_entries_reads_each_home_s_pointer_holding_that_home_s_lock(tmp_path, capsys, root, monkeypatch):
    """entries reads each home's pointer holding that home's lock, one home at a time: with another command holding a
    home's lock, it waits and refuses naming that command.  A home that is gone gets no lock file and is not made
    again; its pointer counts as absent."""
    monkeypatch.setattr(shared_run, "LOCK_WAIT_SECONDS", 0.2)
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="workbuddy", capture="workbuddy")
    assert code == 0, result
    with _held_elsewhere(shared_run.home_lock, homes["dsh"]):
        code, result = _run(capsys, "entries", "--root", str(root))
    assert code == 2 and result["error"].startswith("attach --host TEST --entry TEST (process "), result
    homes["dsh"].rename(homes["dsh"].with_name("TEST-dsh-home-moved"))
    code, listing = _run(capsys, "entries", "--root", str(root))
    assert code == 0, listing
    shown = [(row["entry_id"], row["pointer_present"]) for row in listing["entries"]]
    assert shown == [("workbuddy", True), ("dsh", False)] and not homes["dsh"].exists()


def test_a_holder_note_read_while_it_is_being_written_is_read_again_then_named(tmp_path, monkeypatch):
    """A reader that comes while the holder writes its note finds part of it; it reads again, and names the holder once
    the note is whole.  One that never reads as a note, after five reads, names no one."""
    lock = tmp_path / "TEST.lock"
    whole = json.dumps({"command": "adopt", "pid": 7, "since": "2026-10-17T12:00:00Z"}).encode("utf-8")
    lock.write_bytes(b"\0" + whole[:10])
    waits = []

    def written_meanwhile(seconds):
        waits.append(seconds)
        lock.write_bytes(b"\0" + whole)

    monkeypatch.setattr(shared_run.time, "sleep", written_meanwhile)
    assert (shared_run._holder(lock), len(waits)) == ("adopt (process 7, since 2026-10-17T12:00:00Z)", 1)
    lock.write_bytes(b"\0" + whole[:10])
    monkeypatch.setattr(shared_run.time, "sleep", waits.append)
    assert (shared_run._holder(lock), len(waits)) == ("", 6)


def test_a_holder_note_stays_json_within_1024_bytes_whatever_the_command_s_words(tmp_path):
    """Words of control characters, which JSON escapes to six bytes each, and of four-byte characters with half of a
    surrogate pair among them, and a long moment: the note after the lock's first byte is JSON within 1024 bytes,
    naming the command's start and end."""
    words = "\x01" * 3000 + "\U0001f600" * 3000 + "\ud800" + " --apply"
    with shared_run.store_lock(tmp_path, words, "T" * 500):
        with (tmp_path / shared_run.LOCK_FILENAME).open("rb") as handle:
            handle.seek(1)
            note = handle.read()
    assert len(note) <= 1024, len(note)
    parsed = json.loads(note.decode("utf-8"))
    assert parsed["command"].startswith("\x01") and parsed["command"].endswith("? --apply"), parsed["command"]
    assert parsed["since"] == "T" * 40


def _a_group_row(document):
    """A grants change: a group chat on the owner row's route, reading what it reads."""
    owner = next(row for row in document["audiences"] if row["kind"] == "owner_private")
    group = dict(owner, kind="conversation", chat_type="group", chat_id="TEST-group")
    document["audiences"].append(dict(group, writable_scope_ids=[], capture_scope_id=""))


def _kept(result):
    return any(note.startswith("grants_kept:") for note in result.get("notes", ()))


def test_a_home_attached_again_as_its_entry_keeps_the_grants_it_was_given_since(tmp_path, capsys, root):
    """An attach run again for a home already attached as its entry, a store's only client or a Hermes home, repeats
    the first: the entry keeps its grants, a route added with ``grants --apply`` included, and grant options given
    again are not used, which the result says.  So does one that points the home at the store copied elsewhere and
    adopted."""

    def attached_again(home, attach, *, options):
        code, result = attach()
        assert code == 0, result
        code, applied = _granted(capsys, tmp_path, home, _a_group_row)
        assert (code, applied["status"]) == (0, "applied"), applied
        edited = read_shared_payload(root)["entries"][-1]["audiences"]
        code, result = attach()
        assert code == 0, result
        assert read_shared_payload(root)["entries"][-1]["audiences"] == edited and len(edited) == 2
        assert _kept(result) == options, result

    [client] = _client_homes(tmp_path, "workbuddy").values()
    attached_again(
        client, lambda: _attach_client(capsys, root, client, host="workbuddy", like=None, capture=None), options=False
    )
    desk, _options = _installed(tmp_path, "desk")
    archive = _moved_aside(desk, _routes(desk))
    attached_again(desk, lambda: _attach(capsys, desk, root, archive, "desk", "Desk"), options=True)
    held = {entry["entry_id"]: entry["audiences"] for entry in read_shared_payload(root)["entries"]}
    moved = (tmp_path / "TEST-moved").resolve()
    shutil.copytree(root, moved)
    code, result = _run(capsys, "adopt", "--root", str(moved))
    assert code == 0, result
    code, result = _attach_client(capsys, moved, client, host="workbuddy", like="desk", capture="desk")
    assert code == 0 and _kept(result), result
    assert {entry["entry_id"]: entry["audiences"] for entry in read_shared_payload(moved)["entries"]} == held
    assert read_attachment(client).root == moved


def test_a_client_attached_again_beside_other_entries_needs_no_grant_options(tmp_path, capsys, root):
    """With another entry attached, a client's first attach names an entry to take grants like; attached again from
    its home it needs none, and keeps the grants it holds, an edited route included."""
    homes = _client_homes(tmp_path, "workbuddy", "dsh")
    code, result = _attach_client(capsys, root, homes["workbuddy"], host="workbuddy", like=None, capture=None)
    assert code == 0, result
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like="workbuddy", capture="workbuddy")
    assert code == 0, result
    code, applied = _granted(capsys, tmp_path, homes["dsh"], _a_group_row)
    assert (code, applied["status"]) == (0, "applied"), applied
    edited = read_shared_payload(root)["entries"][-1]["audiences"]
    code, result = _attach_client(capsys, root, homes["dsh"], host="dsh", like=None, capture=None)
    assert code == 0 and not _kept(result), result
    assert read_shared_payload(root)["entries"][-1]["audiences"] == edited and len(edited) == 2


def test_an_entry_searches_the_worker_s_vector_table_whatever_its_routes_named(tmp_path, capsys, root):
    """An entry whose routes came from a store that named its vector table otherwise (``source_embeddings``)
    searches the shared worker's table, the one the worker fills."""

    def with_table(home, table):
        routes = _routes(home)
        space = RuntimeInstanceConfig.from_mapping(routes)
        routes["vector"] = {
            "storage_dir": str(Path(routes["binding"]["data_directory"]) / "vectors" / space.embedding_space_id()),
            "table_name": table,
            "dimensions": space.embedding_space()["dimensions"],
        }
        return routes

    first, _options = _installed(tmp_path, "agent3")
    code, result = _attach(
        capsys, first, root, _moved_aside(first, with_table(first, "scope_recall")), "agent3", "天三"
    )
    assert code == 0, result
    second, _options = _installed(tmp_path, "agent1")
    code, result = _attach(
        capsys, second, root, _moved_aside(second, with_table(second, "source_embeddings")), "agent1", "天一"
    )
    assert code == 0, result
    worker = load_config(root / "runtime-config.json").vector
    entry = load_config(second / "scope-recall" / "runtime-config.json").vector
    assert entry.table_name == worker.table_name == "scope_recall"
    assert entry.storage_dir == worker.storage_dir


# -- dsh (DeepSeek Harness) -------------------------------------------------------------------------------------


def _dsh_home(tmp_path, text=None):
    """A dsh home with a patch file of the person's own (``text``), or none."""
    home = (tmp_path / "TEST-profile" / ".dsh").resolve()
    home.mkdir(parents=True)
    if text is not None:
        (home / "cordis.patch.yml").write_text(text, encoding="utf-8")
    return home


def _patch_ops(path):
    import yaml

    return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=install_dsh._Loader)


def _dsh_plan(home, entry, env_file=None):
    return InstallPlan(
        host="dsh",
        target_plugin_dir=home,
        instance_root=entry,
        project_root=None,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
        env_file=env_file,
    )


_DSH_OWN = (
    "# TEST the person's own rows\n"
    "- id: TEST-other\n"
    "  disabled: true\n"
    "- id: TEST-gated\n"
    "  disabled: !!js \"process.env.TEST_OFF === '1'\"\n"
)


def test_a_dsh_entry_installs_its_plugin_and_rows_and_uninstalls_only_its_own(tmp_path, capsys, root, monkeypatch):
    """dsh joins a shared store as the other clients do.  Its plugin file is the installer's own; its two rows (the
    plugin and the MCP server) go into dsh's home patch beside the person's own, with the session-log upload switched
    off; a second install changes nothing, and uninstall takes out the rows and the plugin and leaves the upload off."""
    first, _second = _hermes_pair(tmp_path, capsys, root)
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    entry.mkdir()
    code, result = _run(
        capsys,
        "attach",
        "--host",
        "dsh",
        "--instance-root",
        str(entry),
        "--root",
        str(root),
        "--entry",
        "dsh",
        "--display-name",
        "DeepSeek Harness",
        "--grants-like",
        "all",
        "--capture-like",
        "agent3",
        "--runtime-config-from",
        str(first / "scope-recall" / "runtime-config.json"),
    )
    assert (code, result["status"]) == (0, "attached"), result
    assert read_attachment(entry).host == "dsh"
    runtime = load_config(entry / "scope-recall" / "runtime-config.json")
    assert runtime.host_adapter == "dsh" and (runtime.session_id, runtime.owner_id) == (
        "dsh-background",
        "dsh-scope-recall",
    )
    from scope_recall.adapters.clients import local_endpoint

    assert local_endpoint.resident_minutes(entry, "dsh") == 120, (
        "dsh's hooks are processes of their own, as WorkBuddy's"
    )

    home = _dsh_home(tmp_path, _DSH_OWN)
    before = (home / "cordis.patch.yml").read_bytes()
    options = dict(
        host="dsh",
        target_plugin_dir=home,
        instance_root=entry,
        project_root=None,
        agent_id=AGENT,
        python_executable=Path(sys.executable),
    )
    plan = plan_install(**options)
    assert plan.conflicts == [], plan.conflicts
    changes = [(change.action, Path(change.path).name) for change in plan.changes]
    assert ("write", "index.mjs") in changes and ("merge", "cordis.patch.yml") in changes
    assert changes[-1][0] == "restart" and "--dump-config" in plan.changes[-1].detail

    stopped = []
    monkeypatch.setattr(local_endpoint, "stop_residents", lambda home_, host: stopped.append((home_, host)) or [])
    installed = apply_install(plan)
    assert stopped == [(entry, "dsh")]
    plugin = install_dsh.plugin_path(home)
    assert plugin.read_bytes() == install_dsh.plugin_source() and str(plugin) in installed.files_written
    assert [Path(path).name for path in installed.files_merged] == ["cordis.patch.yml"]
    assert [Path(path).read_bytes() for path in installed.backups if Path(path).name == "cordis.patch.yml"] == [before]

    ops = _patch_ops(home / "cordis.patch.yml")
    assert ops[:2] == [
        {"id": "TEST-other", "disabled": True},
        {"id": "TEST-gated", "disabled": ("!!js", "process.env.TEST_OFF === '1'")},
    ], "the person's rows stay"
    assert ops[2] == {"id": "session-log-deepseek", "config": {"enabled": False}}
    rows = ops[3]["insert"]
    assert [row["id"] for row in rows] == ["scope-recall", "mcp-scope-recall"]
    assert rows[0]["name"] == plugin.as_uri()
    assert rows[0]["config"] == {
        "python": Path(sys.executable).as_posix(),
        "home": entry.as_posix(),
        "version": install_dsh.PACKAGE_VERSION,
    }
    assert rows[1]["name"] == "@deepseek-ai/dsh-mcp-client"
    assert rows[1]["config"] == {
        "serverName": "scope-recall",
        "transport": "stdio",
        "command": Path(sys.executable).as_posix(),
        "args": [
            "-I",
            "-B",
            "-m",
            "scope_recall.adapters.codex.mcp_entry",
            "--home",
            entry.as_posix(),
            "--host",
            "dsh",
        ],
    }

    stamp = ((home / "cordis.patch.yml").read_bytes(), (home / "cordis.patch.yml").stat().st_mtime_ns)
    plan = plan_install(**options)
    assert ("unchanged", "cordis.patch.yml") in {(change.action, Path(change.path).name) for change in plan.changes}
    assert apply_install(plan).files_merged == []
    assert ((home / "cordis.patch.yml").read_bytes(), (home / "cordis.patch.yml").stat().st_mtime_ns) == stamp

    report = run_doctor(host="dsh", instance_root=entry, python_executable=Path(sys.executable))
    assert report.binding_ok and report.shared_store == {
        "root": str(root),
        "entry_id": "dsh",
        "entry_name": "DeepSeek Harness",
    }

    removal = plan_uninstall(instance_root=entry)
    assert removal.conflicts == [] and [Path(path).name for path in removal.files_to_remove] == ["index.mjs"]
    assert [Path(path).name for path in removal.unmerged_files] == ["cordis.patch.yml"]
    apply_uninstall(removal)
    assert not plugin.exists()
    assert _patch_ops(home / "cordis.patch.yml") == ops[:3], "the person's rows and the upload switched off stay"
    assert plan_uninstall(instance_root=entry).unmerged_files == []

    code, result = _run(capsys, "detach", "--instance-root", str(entry))
    assert (code, result["status"]) == (0, "detached")


@pytest.mark.parametrize(
    "text, refused",
    [
        ("[{id: TEST-flow, disabled: true}]\n", "not a block list"),
        ("  - id: TEST-indented\n    disabled: true\n", "not a block list"),
        ("TEST: a mapping\n", "does not hold a list"),
        ("- [\n", "is not YAML dsh can read"),
        ("- insert:\n    - id: scope-recall\n      name: TEST-another\n", "already has a row scope-recall"),
        (
            "- insert:\n    - id: TEST-mcp\n      name: '@deepseek-ai/dsh-mcp-client'\n"
            "      config:\n        serverName: scope-recall\n",
            "already has an MCP server named scope-recall",
        ),
        (
            "# SCOPE_RECALL_DSH_START (scope-recall 3.7.0 for C:/TEST/another-entry; "
            "apply-uninstall takes this block out)\n"
            "- insert:\n    - id: scope-recall\n      name: TEST\n# SCOPE_RECALL_DSH_END\n",
            "another Scope Recall entry",
        ),
    ],
)
def test_a_dsh_install_refuses_a_patch_it_cannot_edit_safely(tmp_path, text, refused):
    home = _dsh_home(tmp_path, text)
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    with pytest.raises(InstallError, match=refused):
        install_dsh.merged_file(_dsh_plan(home, entry), home / "cordis.patch.yml")


def test_a_dsh_patch_keeps_its_bytes_and_an_upload_switch_of_the_person_s_own(tmp_path):
    """The file comes back in its own line endings and byte order mark, and a person who already switched the upload off
    (here by disabling the row) gets no second switch; an empty or missing file becomes a list of the rows."""
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    home = _dsh_home(tmp_path)
    path = home / "cordis.patch.yml"
    path.write_bytes(b"\xef\xbb\xbf# TEST mine\r\n- id: session-log-deepseek\r\n  disabled: true\r\n")
    merged = install_dsh.merged_file(_dsh_plan(home, entry), path)
    assert merged.startswith(b"\xef\xbb\xbf# TEST mine\r\n") and b"\n" not in merged.replace(b"\r\n", b"")
    assert install_dsh.PRIVACY_START.encode() not in merged, "the person's own switch is enough"
    assert b"- id: scope-recall" in merged

    for text in (None, "", "# TEST only a comment\n", "[]\n"):
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
        written = install_dsh.merged_file(_dsh_plan(home, entry, env_file=Path(sys.executable)), path)
        path.write_bytes(written)
        ops = _patch_ops(path)
        assert [op.get("id") for op in ops[:1]] == ["session-log-deepseek"] and "[]" not in path.read_text("utf-8")
        assert ops[1]["insert"][0]["config"]["envFile"] == Path(sys.executable).as_posix()
        assert ops[1]["insert"][1]["config"]["args"][-2:] == ["--env-file", Path(sys.executable).as_posix()]
        assert install_dsh.merged_file(_dsh_plan(home, entry, env_file=Path(sys.executable)), path) is None
        stripped = install_dsh.unmerged_file(entry, path)
        assert _patch_ops_text(stripped) == [{"id": "session-log-deepseek", "config": {"enabled": False}}]

    path.write_text("# TEST\n" + "\n".join(install_dsh._rows(_dsh_plan(home, entry))) + "\n", encoding="utf-8")
    assert install_dsh.unmerged_file(entry, path).decode("utf-8").strip().splitlines()[-1] == "[]", (
        "a file left with comments alone fails dsh's boot"
    )
    assert install_dsh.unmerged_file((tmp_path / "TEST-another").resolve(), path) is None, "another entry's rows stay"


def test_a_dsh_reinstall_keeps_the_block_where_it_stands_and_an_operation_after_it(tmp_path):
    """dsh applies a patch's operations in order, each key replacing the row's own: an operation of the person's after
    the block (here switching the plugin off) stays after it through later installs, and is not another's row."""
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    home = _dsh_home(tmp_path, _DSH_OWN)
    path = home / "cordis.patch.yml"
    path.write_bytes(install_dsh.merged_file(_dsh_plan(home, entry), path))
    path.write_text(path.read_text(encoding="utf-8") + "- id: scope-recall\n  disabled: true\n", encoding="utf-8")
    assert install_dsh.merged_file(_dsh_plan(home, entry), path) is None, "nothing to change"
    changed = _patch_ops_text(install_dsh.merged_file(_dsh_plan(home, entry, env_file=Path(sys.executable)), path))
    at = next(index for index, operation in enumerate(changed) if "insert" in operation)
    assert changed[at]["insert"][0]["config"]["envFile"] == Path(sys.executable).as_posix(), "the block is rewritten"
    assert changed[at + 1 :] == [{"id": "scope-recall", "disabled": True}], (
        "and the person's operation is still after it"
    )
    assert changed[:2] == _patch_ops_text(_DSH_OWN.encode("utf-8")), "the person's rows before it stay before it"


def _upload_ops(data):
    return [operation for operation in _patch_ops_text(data) if operation.get("id") == "session-log-deepseek"]


def test_a_dsh_upload_counts_as_off_only_as_dsh_works_it_out(tmp_path):
    """dsh applies a patch's operations in order, a later ``config`` replacing the row's whole config (``enabled``
    defaulting to true): an upload switched off and then given another config is on, and gets the install's switch after
    it; one the person switches on again after the install's switch gets the switch again, last."""
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    home = _dsh_home(
        tmp_path,
        "- id: session-log-deepseek\n  config:\n    enabled: false\n"
        "- id: session-log-deepseek\n  config:\n    maxBytes: 4194304\n",
    )
    path = home / "cordis.patch.yml"
    merged = install_dsh.merged_file(_dsh_plan(home, entry), path)
    assert _upload_ops(merged) == [
        {"id": "session-log-deepseek", "config": {"enabled": False}},
        {"id": "session-log-deepseek", "config": {"maxBytes": 4194304}},
        {"id": "session-log-deepseek", "config": {"enabled": False}},
    ]
    assert install_dsh.upload_off(_patch_ops_text(merged)), "the install's switch comes after the person's"

    path.write_bytes(merged)
    path.write_text(
        path.read_text(encoding="utf-8") + "- id: session-log-deepseek\n  config:\n    enabled: true\n",
        encoding="utf-8",
    )
    again = install_dsh.merged_file(_dsh_plan(home, entry), path)
    assert [operation["config"] for operation in _upload_ops(again)][-2:] == [{"enabled": True}, {"enabled": False}]
    assert again.decode("utf-8").count(install_dsh.PRIVACY_START) == 1, "the switch moved, not copied"


def test_a_dsh_install_after_an_uninstall_goes_before_the_person_s_operation_on_its_row(tmp_path):
    """Uninstall leaves an operation of the person's on the plugin's row (``disabled: true``); the next install writes
    the block before it, so that it still switches the plugin off."""
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    home = _dsh_home(tmp_path, _DSH_OWN)
    path = home / "cordis.patch.yml"
    path.write_bytes(install_dsh.merged_file(_dsh_plan(home, entry), path))
    path.write_text(path.read_text(encoding="utf-8") + "- id: scope-recall\n  disabled: true\n", encoding="utf-8")
    path.write_bytes(install_dsh.unmerged_file(entry, path))
    operations = _patch_ops_text(install_dsh.merged_file(_dsh_plan(home, entry), path))
    at = next(index for index, operation in enumerate(operations) if "insert" in operation)
    assert operations[at + 1 :] == [{"id": "scope-recall", "disabled": True}]


def test_a_dsh_patch_keeps_every_other_line_byte_for_byte(tmp_path):
    """An indented ``[]`` is a value and stays; a quoted value holding U+2028 stays one line (dsh's YAML reads it
    there, where splitting at it would change the value)."""
    entry = (tmp_path / "TEST-dsh-entry").resolve()
    home = _dsh_home(tmp_path)
    path = home / "cordis.patch.yml"
    own = '- id: TEST-row\n  config:\n    args:\n      []\n    note: "TEST a' + chr(0x2028) + 'b"\n'
    path.write_bytes(own.encode("utf-8"))
    merged = install_dsh.merged_file(_dsh_plan(home, entry), path).decode("utf-8")
    assert merged.startswith(own)
    assert _patch_ops_text(merged.encode("utf-8"))[0]["config"]["args"] == []


def _patch_ops_text(data):
    import yaml

    return yaml.load(data.decode("utf-8-sig"), Loader=install_dsh._Loader)
