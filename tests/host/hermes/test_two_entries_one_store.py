"""Two Hermes homes attached to one shared store.

Each home keeps its own grants (the audience rows its own installation had); the
store, the id and the memories are one.  What the owner tells one entry, another
recalls, marked with where it came in; a deletion through one is gone for all.
Sources are synthetic; nothing here is a person's memory.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scope_recall.adapters.hermes import HermesIdentityError, ScopeRecallHermesAdapter, bind_hermes_identity
from scope_recall.adapters.hermes.identity import switch_hermes_identity
from scope_recall.adapters.hermes.installation import (
    attach_shared_entry,
    build_installation_manifest,
    load_binding_for_home,
    new_shared_payload,
    read_shared_payload,
    write_installation_manifest,
    write_shared_payload,
)

NOW = "2026-09-22T20:00:00Z"
AGENT = "TEST-agent"
WORKSPACE = "TEST-workspace"
OWNER = "TEST-owner"


def _kwargs(home, **given):
    return {"hermes_home": str(home), "platform": "cli", "agent_context": "primary", "agent_identity": AGENT,
            "agent_workspace": WORKSPACE, "user_id": OWNER, "parent_session_id": "", **given}


def _home(tmp_path, name, **options):
    """A home and the grants its own installation would have had."""
    home = tmp_path / f"TEST-{name}-home"
    home.mkdir()
    return build_installation_manifest(home, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE, **options)


@pytest.fixture
def root(tmp_path):
    store = tmp_path / "TEST-shared"
    write_shared_payload(store, new_shared_payload(store, agent_id=AGENT))
    return store


@pytest.fixture
def entries(tmp_path, root):
    tianshu = _home(tmp_path, "tianshu")
    tianquan = _home(tmp_path, "tianquan")
    attach_shared_entry(root, tianshu, entry_id="tianshu", display_name="天枢", now=NOW)
    attach_shared_entry(root, tianquan, entry_id="tianquan", display_name="天权", now=NOW)
    return tianshu.hermes_home, tianquan.hermes_home


def _provider(home, session="TEST-session-1"):
    provider = ScopeRecallHermesAdapter()
    provider.initialize(session, **_kwargs(home))
    return provider


def _say(provider, text, *, session="TEST-session-1", turn="TEST-turn-1"):
    provider.on_turn_start(1, text, turn_id=turn, session_id=session)
    provider.observe_pre_llm(session_id=session, turn_id=turn, user_message=text)
    provider.sync_turn(text, "好的。", session_id=session)


def _query(root, sql):
    with closing(sqlite3.connect(root / "memory.sqlite3")) as connection:
        return connection.execute(sql).fetchall()


def _sources(root):
    return _query(root, "SELECT entry_id, session_id, source_event_key FROM source_events WHERE role='user' ORDER BY entry_id")


def _items(injected):
    guidance, _newline, body = injected.partition("\n")
    return guidance, json.loads(body)["items"]


def test_an_attached_home_binds_the_shared_store_as_its_own_entry(root, entries):
    tianshu, _tianquan = entries
    identity = bind_hermes_identity("TEST-session-1", **_kwargs(tianshu))

    assert identity.binding.installation_kind == "shared"
    assert identity.binding.data_directory == root.resolve()
    assert identity.binding.installation_id == read_shared_payload(root)["installation_id"]
    assert identity.entry_id == identity.scope.entry_id == "tianshu"
    assert identity.session_id == "TEST-session-1", "the host dispatches hooks by its own session id"
    context = identity.trusted_context()
    assert (context.session_id, context.entry_id) == ("tianshu:TEST-session-1", "tianshu")
    assert not (tianshu / "scope-recall" / "installation.json").exists()


def test_what_one_entry_is_told_another_recalls_marked_with_where_it_came_in(root, entries):
    tianshu, tianquan = entries
    told, asked = _provider(tianquan), _provider(tianshu)
    try:
        _say(told, "TEST 青鸟计划的代号是 QX-17。")
        injected = asked.prefetch("青鸟计划的代号 QX-17 是什么")
    finally:
        told.shutdown()
        asked.shutdown()

    guidance, items = _items(injected)
    marked = [item for item in items if "QX-17" in item["content"]]
    assert marked and all(item["entries"] == [{"id": "tianquan", "name": "天权"}] for item in marked)
    assert "You are 天枢 (tianshu)" in guidance


def test_an_entry_reading_only_its_own_memories_gets_no_entry_guidance(root, entries):
    tianshu, _tianquan = entries
    provider = _provider(tianshu)
    try:
        _say(provider, "TEST 白鹭计划的代号是 BL-3。")
        provider.on_session_switch("TEST-session-2")
        injected = provider.prefetch("白鹭计划的代号 BL-3 是什么")
    finally:
        provider.shutdown()

    guidance, items = _items(injected)
    assert any("BL-3" in item["content"] for item in items)
    assert "You are" not in guidance


def test_the_same_host_session_and_turn_on_two_entries_are_two_sources(root, entries):
    tianshu, tianquan = entries
    for home in (tianshu, tianquan):
        provider = _provider(home, session="TEST-same-session")
        try:
            _say(provider, "TEST 两个入口听到同一句话。", session="TEST-same-session", turn="TEST-turn-1")
        finally:
            provider.shutdown()

    rows = _sources(root)
    assert [(entry, session) for entry, session, _key in rows] == [
        ("tianquan", "tianquan:TEST-same-session"), ("tianshu", "tianshu:TEST-same-session")]
    assert len({key for _entry, _session, key in rows}) == 2
    assert all(f":{entry}:TEST-same-session:" in key for entry, _session, key in rows)


def test_a_deletion_through_one_entry_is_gone_for_every_entry(root, entries):
    tianshu, tianquan = entries
    told, deleting = _provider(tianquan), _provider(tianshu)
    try:
        told.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 我的储物柜密码是 4471。")
        ref, revision = told._current_source_refs[-1].rsplit("@", 1)
        deleting.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-2", user_message=f"忘记 {ref}。")
        receipt = json.loads(deleting.handle_tool_call("forget", {
            "protocol_version": "1.1", "target_refs": [ref], "mode": "delete",
            "expected_revisions": {ref: int(revision)},
        }))
        assert receipt["result"]["mode"] == "delete", receipt
        told.on_session_switch("TEST-session-2")
        injected = told.prefetch("储物柜密码 4471")
    finally:
        told.shutdown()
        deleting.shutdown()

    assert "4471" not in injected
    # The text itself is erased by the shared store's worker, which the deletion queued.
    assert _query(root, "SELECT count(*) FROM work_items WHERE work_type='purge'") != [(0,)]


def test_an_entry_attached_later_leaves_a_running_one_bound_as_it_was(tmp_path, root, entries):
    tianshu, _tianquan = entries
    running = _provider(tianshu)
    try:
        before = running._identity.binding
        tianxuan = _home(tmp_path, "tianxuan", platform="telegram", conversation_key="TEST-group-9")
        attach_shared_entry(root, tianxuan, entry_id="tianxuan", display_name="天璇", now=NOW)
        assert set(read_shared_payload(root)["scope_ids"]) > before.scope_ids

        running.on_session_switch("TEST-session-2")
        assert running._identity.binding == before
        _say(running, "TEST 新入口接入之后这里照常记。", session="TEST-session-2")
    finally:
        running.shutdown()
    assert ("tianshu", "tianshu:TEST-session-2") in {(entry, session) for entry, session, _key in _sources(root)}


def test_a_shared_entry_never_starts_a_worker(root, entries, monkeypatch):
    tianshu, _tianquan = entries
    identity = bind_hermes_identity("TEST-session-1", **_kwargs(tianshu))
    binding = identity.binding
    config = {
        "binding": {"agent_id": binding.agent_id, "installation_id": binding.installation_id,
                    "data_directory": str(binding.data_directory), "scope_ids": sorted(binding.scope_ids),
                    "test_mode": binding.test_mode, "installation_kind": "shared"},
        "session_id": "TEST-session-1", "allowed_scope_ids": sorted(binding.scope_ids), "owner_id": "TEST-entry",
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    (tianshu / "scope-recall" / "runtime-config.json").write_text(json.dumps(config), encoding="utf-8")
    launch = Mock()
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)

    provider = _provider(tianshu)
    try:
        assert provider._host_runtime.configured, "the entry's own runtime config, beside its pointer"
        _say(provider, "TEST 这句话由主库的 worker 整理。")
        provider.on_session_end([])
        provider.on_pre_compress([])
        assert provider.diagnostics.capability_gaps == ()
    finally:
        provider.shutdown()
    launch.assert_not_called()


def test_a_session_switch_keeps_the_entry_and_a_pointer_binds_only_its_own_home(tmp_path, root, entries):
    tianshu, tianquan = entries
    current = bind_hermes_identity("TEST-session-1", **_kwargs(tianshu))
    assert switch_hermes_identity(current, "TEST-session-2").entry_id == "tianshu"

    stray = tmp_path / "TEST-stray-home"
    (stray / "scope-recall").mkdir(parents=True)
    (stray / "scope-recall" / "attachment.json").write_bytes((tianquan / "scope-recall" / "attachment.json").read_bytes())
    with pytest.raises(HermesIdentityError, match="another home"):
        load_binding_for_home(stray)


def test_a_home_with_its_own_installation_and_a_pointer_binds_nothing(tmp_path, root, entries):
    tianshu, _tianquan = entries
    write_installation_manifest(build_installation_manifest(tianshu, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE))
    with pytest.raises(HermesIdentityError, match="both its own installation and a shared store"):
        bind_hermes_identity("TEST-session-1", **_kwargs(tianshu))


def test_attach_refuses_an_entry_id_from_another_home_and_a_home_twice(tmp_path, root, entries):
    other = _home(tmp_path, "other")
    with pytest.raises(HermesIdentityError, match="another home"):
        attach_shared_entry(root, other, entry_id="tianshu", display_name="天枢", now=NOW)
    tianshu_home, _tianquan = entries
    again = build_installation_manifest(tianshu_home, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE)
    with pytest.raises(HermesIdentityError, match="another entry"):
        attach_shared_entry(root, again, entry_id="tianshu-2", display_name="天枢二", now=NOW)
    wrong_agent = build_installation_manifest(tmp_path / "TEST-wrong-agent", agent_id="TEST-other-agent", user_id=OWNER)
    with pytest.raises(HermesIdentityError, match="agent_id or test_mode"):
        attach_shared_entry(root, wrong_agent, entry_id="wrong", display_name="错", now=NOW)


# --- an entry brings the store it had before it attached ---------------------------------------

def _old_store(home, scope_id, *said, installation_id="hermes-install:TEST-legacy", session="TEST-old-session"):
    """The store a home had before it attached, moved aside.  Two of these share an installation id,
    as the legacy migration left the pilot's stores, so one key gives both the same source id."""
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from v11_support import source_event

    binding = InstanceBinding(AGENT, installation_id, home / "scope-recall.local-TEST", frozenset({scope_id}), False)
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    context = TrustedContext(binding, session, frozenset({scope_id}), "human_direct")
    refs = [core.record_event(context, source_event(source_event_key=f"legacy:memories:{index}", content=text),
                              scope_id=scope_id, remaining_seconds=10).event_refs[0] for index, text in enumerate(said)]
    return core, context, refs, binding.data_directory


def _scope(home):
    return bind_hermes_identity("TEST-probe", **_kwargs(home)).local_scope_id


def test_each_entry_brings_its_old_store_and_the_same_old_id_stays_two_memories(root, entries):
    from scope_recall.maintenance.shared_import import import_entry

    tianshu, tianquan = entries
    *_, shu_old = _old_store(tianshu, _scope(tianshu), "TEST 天枢旧库记着：仓库钥匙挂在北门 K-12。")
    *_, quan_old = _old_store(tianquan, _scope(tianquan), "TEST 天权旧库记着：备用电源放在西侧 W-7。",
                              session="TEST-old-session-2")
    for entry, old in (("tianshu", shu_old), ("tianquan", quan_old)):
        result = import_entry(root=root, entry_id=entry, source=old)
        assert (result["status"], result["counts"]["sources"]) == ("imported", 1), result

    rows = _query(root, "SELECT entry_id, session_id, source_event_key FROM source_events ORDER BY entry_id")
    assert rows == [("tianquan", "tianquan:TEST-old-session-2", "import:tianquan:legacy:memories:0"),
                    ("tianshu", "tianshu:TEST-old-session", "import:tianshu:legacy:memories:0")]
    asked = _provider(tianshu)
    try:
        _guidance, items = _items(asked.prefetch("仓库钥匙 北门 K-12 挂在哪里"))
    finally:
        asked.shutdown()
    brought = [item for item in items if "K-12" in item["content"]]
    assert brought and all(item["entries"] == [{"id": "tianshu", "name": "天枢"}] for item in brought)
    again = import_entry(root=root, entry_id="tianshu", source=shu_old)
    assert again["status"] == "already_imported"
    assert _query(root, "SELECT count(*) FROM source_events") == [(2,)]
    # The embedding each old store had queued for it is queued again, under the source's new id.
    assert _query(root, """SELECT count(*) FROM work_items w JOIN source_events e ON e.event_id=w.subject_ref
                           WHERE w.work_type='embed' AND w.state='pending'""") == [(2,)]


def test_an_id_two_old_stores_share_otherwise_refuses_the_second_and_writes_nothing(root, entries):
    from scope_recall.maintenance.shared import SharedStoreError
    from scope_recall.maintenance.shared_import import import_entry

    tianshu, tianquan = entries
    *_, shu_old = _old_store(tianshu, _scope(tianshu), "TEST 同一场旧对话，天枢这边。")
    *_, quan_old = _old_store(tianquan, _scope(tianquan), "TEST 同一场旧对话，天权这边。")
    import_entry(root=root, entry_id="tianshu", source=shu_old)
    with pytest.raises(SharedStoreError, match="cannot be imported as it is"):
        import_entry(root=root, entry_id="tianquan", source=quan_old)
    assert _query(root, "SELECT entry_id, count(*) FROM source_events GROUP BY entry_id") == [("tianshu", 1)]


def test_a_rehearsal_writes_nothing_and_another_homes_store_is_refused(root, entries):
    from scope_recall.maintenance.shared import SharedStoreError
    from scope_recall.maintenance.shared_import import import_entry

    tianshu, _tianquan = entries
    *_, old = _old_store(tianshu, _scope(tianshu), "TEST 只在排练里导入的一句话。")
    assert import_entry(root=root, entry_id="tianshu", source=old, dry_run=True)["status"] == "rehearsed"
    assert _query(root, "SELECT count(*) FROM source_events") == [(0,)]
    with pytest.raises(SharedStoreError, match="not of this entry's home"):
        import_entry(root=root, entry_id="tianquan", source=old)


def test_what_an_old_store_forgot_stays_forgotten(root, entries):
    from scope_recall.core.delete_storage import group_digest
    from scope_recall.maintenance.shared_import import import_entry
    from v11_support import source_event

    tianshu, _tianquan = entries
    scope = _scope(tianshu)
    core, context, (secret,), old = _old_store(tianshu, scope, "TEST 旧库里的保险柜密码是 5520。")
    core.record_event(context, source_event(source_event_key="legacy:memories:9", content=f"删除 {secret.ref}"),
                      scope_id=scope, remaining_seconds=10)
    core.forget(context, {"protocol_version": "1.1", "target_refs": [secret.ref], "mode": "delete",
                          "expected_revisions": {secret.ref: secret.revision}}, remaining_seconds=10)

    result = import_entry(root=root, entry_id="tianshu", source=old)
    assert result["counts"]["group_blocks_without_sources"] == 0
    # A deletion takes the message that asked for it along; both groups stay blocked under the store's id.
    store = SimpleNamespace(installation_id=read_shared_payload(root)["installation_id"])
    assert set(_query(root, "SELECT group_sha256 FROM source_group_blocks")) == {
        (group_digest(store, scope, None, None, f"import:tianshu:legacy:memories:{index}"),) for index in (0, 9)}
    asked = _provider(tianshu)
    try:
        injected = asked.prefetch("保险柜密码 5520")
    finally:
        asked.shutdown()
    assert "5520" not in injected


def test_a_legacy_source_id_is_renamed_wherever_the_old_store_names_it(root, entries):
    """2.x migrations named sources event-legacy-<32 hex>; 3.2.0rc3 renamed them in columns only,
    so their queued embeddings named no source and the worker dropped every one."""
    from scope_recall.maintenance.shared_import import _Names, import_entry

    tianshu, _tianquan = entries
    _core, _context, (said,), old = _old_store(tianshu, _scope(tianshu), "TEST 一条从 2.x 迁移来的旧记录。")
    legacy = "event-legacy-" + "0123456789abcdef" * 2
    with closing(sqlite3.connect(old / "memory.sqlite3")) as db:
        # The store as a 2.x migration left it: the legacy id in every column that names the source.
        db.execute("PRAGMA foreign_keys=OFF")
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for column in [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[2] == "TEXT"]:
                db.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (legacy, said.ref))
        db.execute("UPDATE source_events SET extra_json=? WHERE event_id=?",
                   (json.dumps({"TEST_cites": [f"{legacy}@1", "event-driven"]}), legacy))
        db.commit()

    import_entry(root=root, entry_id="tianshu", source=old)
    new = _Names("tianshu").event(legacy)
    assert _query(root, f"SELECT count(*) FROM work_items WHERE work_type='embed' AND subject_ref='{new}'") == [(1,)]
    assert _query(root, "SELECT count(*) FROM work_items WHERE subject_ref LIKE 'event-legacy-%'") == [(0,)]
    (extra,), = _query(root, f"SELECT extra_json FROM source_events WHERE event_id='{new}'")
    assert json.loads(extra)["TEST_cites"] == [f"{new}@1", "event-driven"], "an id-shaped word that is no id stays"
