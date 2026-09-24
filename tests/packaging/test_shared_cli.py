"""The shared store's operator commands, run the way an operator runs them.

A Hermes home is installed on its own, its store moved aside, and the home
attached to a shared store with the grants and routes that store had; then it
is checked, reinstalled over, detached, and the store copied and adopted.
Nothing here opens a real instance or a person's memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from scope_recall.adapters.codex.config import load_shared_client
from scope_recall.adapters.hermes import HermesIdentityError, bind_hermes_identity
from scope_recall.adapters.hermes.installation import read_attachment, read_shared_payload
from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.install import apply_install, plan_install
from scope_recall.maintenance.install_common import InstallError
from scope_recall.maintenance.shared import main
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
    options = dict(host="hermes", target_plugin_dir=plugin, instance_root=home, project_root=project,
                   agent_id=AGENT, python_executable=Path(sys.executable), agent_workspace=workspace)
    apply_install(plan_install(**options))
    return home, options


def _routes(home, *, model=None):
    """A runtime config with its own model routes, bound to the home's own store."""
    manifest = json.loads((home / "scope-recall" / "installation.json").read_text(encoding="utf-8"))
    embedding = {"credential_env": "TEST_EMBED_KEY"}
    if model is not None:
        embedding.update(model=model, endpoint="https://example.test/v1/embeddings", dimensions=64, dialect="openai")
    return {
        "binding": {"agent_id": manifest["agent_id"], "installation_id": manifest["installation_id"],
                    "data_directory": manifest["data_directory"], "scope_ids": manifest["scope_ids"],
                    "test_mode": manifest["test_mode"]},
        "session_id": "TEST-background",
        "allowed_scope_ids": manifest["scope_ids"],
        "owner_id": "TEST-worker",
        "auxiliary": {"external_embedding": False, "external_consolidation": False, "embedding": embedding,
                      "installation_dir": manifest["data_directory"]},
    }


def _moved_aside(home, routes):
    (home / "scope-recall" / "runtime-config.json").write_text(json.dumps(routes), encoding="utf-8")
    archive = home / "scope-recall.local-TEST"
    (home / "scope-recall").rename(archive)
    return archive


def _attach(capsys, home, root, archive, entry, name):
    return _run(capsys, "attach", "--host", "hermes", "--instance-root", str(home), "--root", str(root),
                "--entry", entry, "--display-name", name,
                "--grants-from", str(archive / "installation.json"),
                "--runtime-config-from", str(archive / "runtime-config.json"))


def _bind(home):
    return bind_hermes_identity("TEST-session", hermes_home=str(home), platform="cli", agent_identity=AGENT,
                                agent_workspace="hermes", user_id="local", agent_context="primary")


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
    assert sorted(result["ledgers_created"]) == sorted(str(path) for path in (entry.auxiliary.ledger_path,
                                                                                  worker.auxiliary.ledger_path))
    for ledger in (entry.auxiliary.ledger_path, worker.auxiliary.ledger_path):
        assert read_auxiliary_budget_status(ledger) == {"ledger_exists": True, "requests": 0, "charge_micro_usd": 0,
                                                         "meter_breach": False}

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
    code, result = _run(capsys, "attach", "--host", "hermes", "--instance-root", str(home), "--root", str(root),
                        "--entry", "tianshu", "--display-name", "天枢")
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
    assert any(Path(kept).name == "entry-auxiliary-budget.sqlite3" for kept in json.loads(
        Path(result["receipt"]).read_text(encoding="utf-8"))["backups"]), "the entry's spend record is kept"
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
        manifest["audiences"].append(dict(manifest["audiences"][0], kind="conversation", chat_type="group",
                                          chat_id=f"TEST-group-{index:03d}", allowed_scope_ids=[scope],
                                          writable_scope_ids=[scope], capture_scope_id=scope))
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
    assert len(load_config(root / "runtime-config.json").binding.scope_ids) == len(read_shared_payload(root)["scope_ids"])
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
    argv = ["attach", "--host", host, "--instance-root", str(home), "--root", str(root), "--entry", host,
            "--display-name", "Claude Code" if host == "claude-code" else "Codex", "--grants-like", like,
            "--capture-like", capture]
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
    tianshu_owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"]
                         if row["kind"] == "owner_private")
    assert config.audience.capture_scope_id == tianshu_owner["capture_scope_id"]
    assert config.audience.allowed_scope_ids == frozenset(tianshu_owner["allowed_scope_ids"])
    entry = load_config(client / "scope-recall" / "runtime-config.json")
    assert entry.binding == config.to_binding() and entry.host_adapter == "claude-code"
    assert (entry.session_id, entry.owner_id) == ("claude-code-background", "claude-code-scope-recall")
    assert entry.auxiliary.ledger_path == client / "scope-recall" / "auxiliary-budget.sqlite3"
    assert str(entry.auxiliary.ledger_path) in result["ledgers_created"]

    plugin = (tmp_path / "TEST-claude" / "skills" / "scope-recall").resolve()
    options = dict(host="claude-code", target_plugin_dir=plugin, instance_root=client, project_root=None,
                   agent_id=AGENT, python_executable=Path(sys.executable))
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
        "claude-code", "claude-code", True)

    code, result = _run(capsys, "detach", "--instance-root", str(client))
    assert (code, result["status"]) == (0, "detached")
    assert not (client / "scope-recall").exists()


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
        apply_install(plan_install(host="claude-code", target_plugin_dir=(tmp_path / "TEST-plugin" / "scope-recall"),
                                   instance_root=client, project_root=None, agent_id=AGENT,
                                   python_executable=Path(sys.executable)))


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
        routes["vector"] = {"storage_dir": str(Path(routes["binding"]["data_directory"]) / "vectors"
                                               / space.embedding_space_id()),
                            "table_name": table, "dimensions": space.embedding_space()["dimensions"]}
        return routes

    first, _options = _installed(tmp_path, "tianshu")
    code, result = _attach(capsys, first, root, _moved_aside(first, with_table(first, "scope_recall")), "tianshu", "天枢")
    assert code == 0, result
    second, _options = _installed(tmp_path, "tianji")
    code, result = _attach(capsys, second, root, _moved_aside(second, with_table(second, "source_embeddings")),
                           "tianji", "天姬")
    assert code == 0, result
    worker = load_config(root / "runtime-config.json").vector
    entry = load_config(second / "scope-recall" / "runtime-config.json").vector
    assert entry.table_name == worker.table_name == "scope_recall"
    assert entry.storage_dir == worker.storage_dir
