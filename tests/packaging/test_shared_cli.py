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
import sys
from pathlib import Path

import pytest
from scope_recall.adapters.clients import remote_client
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.hermes import HermesIdentityError, bind_hermes_identity
from scope_recall.adapters.hermes.installation import read_attachment, read_shared_payload
from scope_recall.maintenance import cli, install_dsh, install_workbuddy
from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.install import apply_install, apply_uninstall, plan_install, plan_uninstall
from scope_recall.maintenance.install_common import InstallError, InstallPlan
from scope_recall.maintenance.shared import main
from scope_recall.runtime import instance as runtime_instance
from scope_recall.runtime.instance import RuntimeInstanceConfig
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
    home, options = _installed(tmp_path, "tianshu")
    own = json.loads((home / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    archive = _moved_aside(home, _routes(home))

    code, result = _attach(capsys, home, root, archive, "tianshu", "天枢")
    assert (code, result["status"], result["entry_id"]) == (0, "attached", "tianshu")
    assert result["new_scopes"] == result["store_scopes"] == len(own["scope_ids"])
    assert Path(result["receipt"]).is_file()

    identity = _bind(home)
    assert identity.entry_id == "tianshu" and identity.binding.installation_kind == "shared"
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
    assert report.shared_store == {"root": str(root), "entry_id": "tianshu", "entry_name": "天枢"}

    code, listing = _run(capsys, "entries", "--root", str(root))
    assert code == 0 and listing["store"] == "ok"
    assert [(row["entry_id"], row["pointer_present"]) for row in listing["entries"]] == [("tianshu", True)]


def test_attach_refuses_a_home_whose_own_store_is_still_in_place(tmp_path, capsys, root):
    home, _options = _installed(tmp_path, "tianshu")
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
        "tianshu",
        "--display-name",
        "天枢",
    )
    assert code == 2 and "still has its own store" in result["error"]
    assert read_shared_payload(root)["entries"] == []


def test_a_second_entry_must_use_the_worker_s_embedding_model(tmp_path, capsys, root):
    first, _options = _installed(tmp_path, "tianshu")
    _attach(capsys, first, root, _moved_aside(first, _routes(first)), "tianshu", "天枢")
    second, _options = _installed(tmp_path, "tianquan")
    archive = _moved_aside(second, _routes(second, model="TEST-other-embedding"))

    code, result = _attach(capsys, second, root, archive, "tianquan", "天权")
    assert code == 2 and result["error"].startswith("embedding_space_differs")
    assert [entry["entry_id"] for entry in read_shared_payload(root)["entries"]] == ["tianshu"]
    assert not (second / "scope-recall" / "attachment.json").exists()


def test_a_later_entry_widens_the_worker_and_keeps_its_routes(tmp_path, capsys, root):
    first, _options = _installed(tmp_path, "tianshu")
    _attach(capsys, first, root, _moved_aside(first, _routes(first)), "tianshu", "天枢")
    before = json.loads((root / "runtime-config.json").read_text(encoding="utf-8"))
    second, _options = _installed(tmp_path, "tianquan")
    code, result = _attach(capsys, second, root, _moved_aside(second, _routes(second)), "tianquan", "天权")
    assert code == 0
    after = json.loads((root / "runtime-config.json").read_text(encoding="utf-8"))
    assert set(after["binding"]["scope_ids"]) == set(read_shared_payload(root)["scope_ids"])
    assert after["auxiliary"] == before["auxiliary"] and after["session_id"] == before["session_id"]


def test_detach_leaves_the_memories_and_the_record(tmp_path, capsys, root):
    home, _options = _installed(tmp_path, "tianshu")
    _attach(capsys, home, root, _moved_aside(home, _routes(home)), "tianshu", "天枢")

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
    assert record["entry_id"] == "tianshu" and record["detached_at"]
    code, listing = _run(capsys, "entries", "--root", str(root))
    assert listing["entries"][0]["pointer_present"] is False and listing["entries"][0]["first_seen"]


def test_a_copied_store_opens_only_after_adopt_and_takes_its_entries_from_new_homes(tmp_path, capsys, root):
    home, _options = _installed(tmp_path, "tianshu")
    _attach(capsys, home, root, _moved_aside(home, _routes(home)), "tianshu", "天枢")
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
    new_home, _options = _installed(tmp_path, "tianshu-new")
    code, result = _attach(capsys, new_home, copy, _moved_aside(new_home, _routes(new_home)), "tianshu", "天枢")
    assert code == 0 and _bind(new_home).binding.data_directory == copy
    assert _bind(home).binding.data_directory == root, "the original is untouched"


def test_init_refuses_a_directory_in_use_or_inside_an_agent_home(tmp_path, capsys):
    used = tmp_path / "TEST-used"
    used.mkdir()
    (used / "something").write_text("TEST", encoding="utf-8")
    code, result = _run(capsys, "init-shared", "--root", str(used))
    assert code == 2 and "new or empty" in result["error"]
    home, _options = _installed(tmp_path, "tianshu")
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
    """The worker's config lists every scope of the store twice, about 120 bytes each.  The pilot's 221 scopes
    made 58 KB; one more instance passed the 64 KB these commands read, and the next attach refused the store."""
    homes = []
    for name in ("tianshu", "tianji", "yuheng"):
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
    """Two Hermes entries of the store, tianshu's routes the worker's."""
    first, _options = _installed(tmp_path, "tianshu")
    _attach(capsys, first, root, _moved_aside(first, _routes(first)), "tianshu", "天枢")
    second, _options = _installed(tmp_path, "tianquan", workspace=second_workspace)
    _attach(capsys, second, root, _moved_aside(second, _routes(second)), "tianquan", "天权")
    return first, second


def _attach_client(capsys, root, home, *, host="claude-code", like="all", capture="tianshu", routes=None):
    argv = [
        "attach",
        "--host",
        host,
        "--instance-root",
        str(home),
        "--root",
        str(root),
        "--entry",
        host,
        "--display-name",
        "Claude Code" if host == "claude-code" else "Codex",
        "--grants-like",
        like,
        "--capture-like",
        capture,
    ]
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
    tianshu_owner = next(
        row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private"
    )
    assert config.audience.capture_scope_id == tianshu_owner["capture_scope_id"]
    assert config.audience.allowed_scope_ids == frozenset(tianshu_owner["allowed_scope_ids"])
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
    """A WorkBuddy home shaped like the pilot's: keys of WorkBuddy's own and another tool's prompt hook in its
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
        "tianshu",
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
    # (another venv, an older version) held the entry's lock against the new one's (review 2 of 3.6.0rc1).
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
    most = runtime_instance._SECONDS_BOUNDS["hook_processing_seconds"][1]
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


def test_a_client_writes_only_where_every_owner_row_reads(tmp_path, capsys, root):
    _hermes_pair(tmp_path, capsys, root, second_workspace="TEST-other-workspace")
    client = (tmp_path / "TEST-claude-code-home").resolve()
    client.mkdir()
    code, result = _attach_client(capsys, root, client)
    assert code == 2 and "not read by these owner rows: tianquan:cli" in result["error"], result
    code, result = _attach_client(capsys, root, client, like="tianshu,nobody")
    assert code == 2 and "nobody" in result["error"], result
    assert [entry["entry_id"] for entry in read_shared_payload(root)["entries"]] == ["tianshu", "tianquan"]
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


def test_an_entry_searches_the_worker_s_vector_table_whatever_its_routes_named(tmp_path, capsys, root):
    """tianji's routes came from its own 3.1 store, which named its table source_embeddings; its entry then
    searched a table the shared worker never fills, and recall lost its vector half (2026-09-24)."""

    def with_table(home, table):
        routes = _routes(home)
        space = RuntimeInstanceConfig.from_mapping(routes)
        routes["vector"] = {
            "storage_dir": str(Path(routes["binding"]["data_directory"]) / "vectors" / space.embedding_space_id()),
            "table_name": table,
            "dimensions": space.embedding_space()["dimensions"],
        }
        return routes

    first, _options = _installed(tmp_path, "tianshu")
    code, result = _attach(
        capsys, first, root, _moved_aside(first, with_table(first, "scope_recall")), "tianshu", "天枢"
    )
    assert code == 0, result
    second, _options = _installed(tmp_path, "tianji")
    code, result = _attach(
        capsys, second, root, _moved_aside(second, with_table(second, "source_embeddings")), "tianji", "天姬"
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
        "tianshu",
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
            "- insert:\n    - id: TEST-mcp\n      name: '@deepseek-ai/dsh-mcp-client'\n      config:\n        serverName: scope-recall\n",
            "already has an MCP server named scope-recall",
        ),
        (
            "# SCOPE_RECALL_DSH_START (scope-recall 3.7.0 for C:/TEST/another-entry; apply-uninstall takes this block out)\n"
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
