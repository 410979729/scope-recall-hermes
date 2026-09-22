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

from scope_recall.adapters.hermes import HermesIdentityError, bind_hermes_identity
from scope_recall.adapters.hermes.installation import read_shared_payload
from scope_recall.maintenance.doctor import run_doctor
from scope_recall.maintenance.install import apply_install, plan_install
from scope_recall.maintenance.shared import main
from scope_recall.runtime.worker_entry import load_config

AGENT = "TEST-agent"


def _run(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _installed(tmp_path, name):
    """A Hermes home installed on its own, the way apply-install leaves one."""
    home = (tmp_path / f"TEST-{name}-home").resolve()
    plugin = (tmp_path / f"TEST-{name}-plugin" / "scope-recall").resolve()
    project = (tmp_path / f"TEST-{name}-project").resolve()
    plugin.mkdir(parents=True)
    project.mkdir()
    options = dict(host="hermes", target_plugin_dir=plugin, instance_root=home, project_root=project,
                   agent_id=AGENT, python_executable=Path(sys.executable))
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
        "auxiliary": {"external_embedding": False, "external_consolidation": False, "embedding": embedding},
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
    assert (code, result["status"]) == (0, "detached")
    assert not (home / "scope-recall").exists()
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
