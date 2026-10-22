"""Two Hermes homes attached to one shared store.

Each home keeps its own grants (the audience rows its own installation had); the
store, the id and the memories are one.  What the owner tells one entry, another
recalls, marked with where it came in; a deletion through one is gone for all.
Sources are synthetic; nothing here is a person's memory.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scope_recall.adapters.hermes import HermesIdentityError, ScopeRecallHermesAdapter, bind_hermes_identity
from scope_recall.adapters.hermes.identity import switch_hermes_identity
from scope_recall.adapters.hermes.installation import build_installation_manifest, write_installation_manifest
from scope_recall.adapters.hermes.shared_entries import (
    attach_shared_entry,
    load_binding_for_home,
    new_shared_payload,
    read_shared_payload,
    write_shared_payload,
)
from scope_recall.core.claims import Qualification
from scope_recall.maintenance.shared_import import import_entry
from test_denial_values import NEGATED_STATES

NOW = "2026-09-22T20:00:00Z"
AGENT = "TEST-agent"
WORKSPACE = "TEST-workspace"
OWNER = "TEST-owner"


def _kwargs(home, **given):
    return {
        "hermes_home": str(home),
        "platform": "cli",
        "agent_context": "primary",
        "agent_identity": AGENT,
        "agent_workspace": WORKSPACE,
        "user_id": OWNER,
        "parent_session_id": "",
        **given,
    }


def _home(tmp_path, name, **options):
    """A home and the grants its own installation would have had."""
    home = tmp_path / f"TEST-{name}-home"
    home.mkdir()
    return build_installation_manifest(home, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE, **options)


@pytest.fixture
def root(tmp_path):
    """Create the synthetic shared store to which both entries attach."""
    store = tmp_path / "TEST-shared"
    write_shared_payload(store, new_shared_payload(store, agent_id=AGENT))
    return store


@pytest.fixture
def entries(tmp_path, root):
    agent3 = _home(tmp_path, "agent3")
    agent2 = _home(tmp_path, "agent2")
    attach_shared_entry(root, agent3, entry_id="agent3", display_name="天三", now=NOW)
    attach_shared_entry(root, agent2, entry_id="agent2", display_name="天二", now=NOW)
    return agent3.hermes_home, agent2.hermes_home


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
    return _query(
        root, "SELECT entry_id, session_id, source_event_key FROM source_events WHERE role='user' ORDER BY entry_id"
    )


def _items(injected):
    guidance, _newline, body = injected.partition("\n")
    return guidance, json.loads(body)["items"]


def test_an_attached_home_binds_the_shared_store_as_its_own_entry(root, entries):
    agent3, _agent2 = entries
    identity = bind_hermes_identity("TEST-session-1", **_kwargs(agent3))

    assert identity.binding.installation_kind == "shared"
    assert identity.binding.data_directory == root.resolve()
    assert identity.binding.installation_id == read_shared_payload(root)["installation_id"]
    assert identity.entry_id == identity.scope.entry_id == "agent3"
    assert identity.session_id == "TEST-session-1", "the host dispatches hooks by its own session id"
    context = identity.trusted_context()
    assert (context.session_id, context.entry_id) == ("agent3:TEST-session-1", "agent3")
    assert not (agent3 / "scope-recall" / "installation.json").exists()


def test_what_one_entry_is_told_another_recalls_marked_with_where_it_came_in(root, entries):
    agent3, agent2 = entries
    told, asked = _provider(agent2), _provider(agent3)
    try:
        _say(told, "TEST 青鸟计划的代号是 QX-17。")
        injected = asked.prefetch("青鸟计划的代号 QX-17 是什么")
    finally:
        told.shutdown()
        asked.shutdown()

    guidance, items = _items(injected)
    marked = [item for item in items if "QX-17" in item["content"]]
    assert marked and all(item["entries"] == [{"id": "agent2", "name": "天二"}] for item in marked)
    assert "You are 天三 (agent3)" in guidance


def test_an_entry_reading_only_its_own_memories_gets_no_entry_guidance(root, entries):
    agent3, _agent2 = entries
    provider = _provider(agent3)
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
    agent3, agent2 = entries
    for home in (agent3, agent2):
        provider = _provider(home, session="TEST-same-session")
        try:
            _say(provider, "TEST 两个入口听到同一句话。", session="TEST-same-session", turn="TEST-turn-1")
        finally:
            provider.shutdown()

    rows = _sources(root)
    assert [(entry, session) for entry, session, _key in rows] == [
        ("agent2", "agent2:TEST-same-session"),
        ("agent3", "agent3:TEST-same-session"),
    ]
    assert len({key for _entry, _session, key in rows}) == 2
    assert all(f":{entry}:TEST-same-session:" in key for entry, _session, key in rows)


@pytest.mark.parametrize("slow_write", [False, True], ids=["store", "slow_store"])
def test_a_deletion_through_one_entry_is_gone_for_every_entry(root, entries, slow_capture_store, slow_write):
    """Deletion blocks every entry's recall and purges the shared source, including after a slow capture."""
    agent3, agent2 = entries
    told, deleting = _provider(agent2), _provider(agent3)
    try:
        if slow_write:
            slow_capture_store()
        told.observe_pre_llm(
            session_id="TEST-session-1", turn_id="TEST-turn-1", user_message="TEST 我的储物柜密码是 4471。"
        )
        ref, revision = told._current_source_refs[-1].rsplit("@", 1)
        deleting.observe_pre_llm(session_id="TEST-session-1", turn_id="TEST-turn-2", user_message=f"忘记 {ref}。")
        receipt = json.loads(
            deleting.handle_tool_call(
                "forget",
                {
                    "protocol_version": "1.1",
                    "target_refs": [ref],
                    "mode": "delete",
                    "expected_revisions": {ref: int(revision)},
                },
            )
        )
        assert receipt["result"]["mode"] == "delete", receipt
        told.on_session_switch("TEST-session-2")
        injected = told.prefetch("储物柜密码 4471")
        # A shared scope id contains colons; the queued purge must still resolve its whole operation subject.
        purged = []

        class Purge:
            def purge_active(self, operation_id, *, receipt, remaining_seconds):
                purged.append(operation_id)
                return True

        drained = deleting._require_core().drain_worker(
            deleting._tool_context(mutation=True), purge=Purge(), max_items=1, remaining_seconds=10
        )
    finally:
        told.shutdown()
        deleting.shutdown()

    assert "4471" not in injected
    assert [(item.work_type, item.disposition) for item in drained.items] == [("purge", "completed")], drained
    operation = receipt["result"]["operation_id"]
    assert purged == [operation]
    layers = json.loads(
        _query(root, f"SELECT layers_json FROM deletion_operations WHERE operation_id='{operation}'")[0][0]
    )
    assert layers["sqlite_active"] == "removed" and layers["vector_active"] == "removed", layers
    assert _query(root, "SELECT count(*) FROM source_events WHERE content LIKE '%4471%'") == [(0,)]


def test_an_entry_attached_later_leaves_a_running_one_bound_as_it_was(tmp_path, root, entries):
    agent3, _agent2 = entries
    running = _provider(agent3)
    try:
        before = running._identity.binding
        agent4 = _home(tmp_path, "agent4", platform="telegram", conversation_key="TEST-group-9")
        attach_shared_entry(root, agent4, entry_id="agent4", display_name="天四", now=NOW)
        assert set(read_shared_payload(root)["scope_ids"]) > before.scope_ids

        running.on_session_switch("TEST-session-2")
        assert running._identity.binding == before
        _say(running, "TEST 新入口接入之后这里照常记。", session="TEST-session-2")
    finally:
        running.shutdown()
    assert ("agent3", "agent3:TEST-session-2") in {(entry, session) for entry, session, _key in _sources(root)}


def test_a_shared_entry_never_starts_a_worker(root, entries, monkeypatch):
    agent3, _agent2 = entries
    identity = bind_hermes_identity("TEST-session-1", **_kwargs(agent3))
    binding = identity.binding
    config = {
        "binding": {
            "agent_id": binding.agent_id,
            "installation_id": binding.installation_id,
            "data_directory": str(binding.data_directory),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
            "installation_kind": "shared",
        },
        "session_id": "TEST-session-1",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "owner_id": "TEST-entry",
        "auxiliary": {"external_embedding": False, "external_consolidation": False},
    }
    (agent3 / "scope-recall" / "runtime-config.json").write_text(json.dumps(config), encoding="utf-8")
    launch = Mock()
    monkeypatch.setattr("scope_recall.adapters.hermes.runtime_wiring.launch_worker", launch)

    provider = _provider(agent3)
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
    agent3, agent2 = entries
    current = bind_hermes_identity("TEST-session-1", **_kwargs(agent3))
    assert switch_hermes_identity(current, "TEST-session-2").entry_id == "agent3"

    stray = tmp_path / "TEST-stray-home"
    (stray / "scope-recall").mkdir(parents=True)
    (stray / "scope-recall" / "attachment.json").write_bytes((agent2 / "scope-recall" / "attachment.json").read_bytes())
    with pytest.raises(HermesIdentityError, match="another home"):
        load_binding_for_home(stray)


def test_a_home_with_its_own_installation_and_a_pointer_binds_nothing(tmp_path, root, entries):
    agent3, _agent2 = entries
    write_installation_manifest(
        build_installation_manifest(agent3, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE)
    )
    with pytest.raises(HermesIdentityError, match="both its own installation and a shared store"):
        bind_hermes_identity("TEST-session-1", **_kwargs(agent3))


def test_attach_refuses_an_entry_id_from_another_home_and_a_home_twice(tmp_path, root, entries):
    other = _home(tmp_path, "other")
    with pytest.raises(HermesIdentityError, match="another home"):
        attach_shared_entry(root, other, entry_id="agent3", display_name="天三", now=NOW)
    agent3_home, _agent2 = entries
    again = build_installation_manifest(agent3_home, agent_id=AGENT, user_id=OWNER, agent_workspace=WORKSPACE)
    with pytest.raises(HermesIdentityError, match="another entry"):
        attach_shared_entry(root, again, entry_id="agent3-2", display_name="天三二", now=NOW)
    wrong_agent = build_installation_manifest(tmp_path / "TEST-wrong-agent", agent_id="TEST-other-agent", user_id=OWNER)
    with pytest.raises(HermesIdentityError, match="agent_id or test_mode"):
        attach_shared_entry(root, wrong_agent, entry_id="wrong", display_name="错", now=NOW)


# --- an entry brings the store it had before it attached ---------------------------------------


def _old_store(home, scope_id, *said, installation_id="hermes-install:TEST-legacy", session="TEST-old-session"):
    """The store a home had before it attached, moved aside.  Two of these share an installation id,
    as the legacy migration leaves stores, so one key gives both the same source id."""
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from v11_support import source_event

    binding = InstanceBinding(AGENT, installation_id, home / "scope-recall.local-TEST", frozenset({scope_id}), False)
    core = MemoryCore(CoreConfig(binding))
    core.initialize()
    context = TrustedContext(binding, session, frozenset({scope_id}), "human_direct")
    refs = [
        core.record_event(
            context,
            source_event(source_event_key=f"legacy:memories:{index}", content=text),
            scope_id=scope_id,
            remaining_seconds=10,
        ).event_refs[0]
        for index, text in enumerate(said)
    ]
    return core, context, refs, binding.data_directory


def _scope(home):
    return bind_hermes_identity("TEST-probe", **_kwargs(home)).local_scope_id


def test_each_entry_brings_its_old_store_and_the_same_old_id_stays_two_memories(root, entries):
    from scope_recall.maintenance.shared_import import import_entry

    agent3, agent2 = entries
    *_, shu_old = _old_store(agent3, _scope(agent3), "TEST 天三旧库记着：仓库钥匙挂在北门 K-12。")
    *_, quan_old = _old_store(
        agent2, _scope(agent2), "TEST 天二旧库记着：备用电源放在西侧 W-7。", session="TEST-old-session-2"
    )
    for entry, old in (("agent3", shu_old), ("agent2", quan_old)):
        result = import_entry(root=root, entry_id=entry, source=old)
        assert (result["status"], result["counts"]["sources"]) == ("imported", 1), result

    rows = _query(root, "SELECT entry_id, session_id, source_event_key FROM source_events ORDER BY entry_id")
    assert rows == [
        ("agent2", "agent2:TEST-old-session-2", "import:agent2:legacy:memories:0"),
        ("agent3", "agent3:TEST-old-session", "import:agent3:legacy:memories:0"),
    ]
    asked = _provider(agent3)
    try:
        _guidance, items = _items(asked.prefetch("仓库钥匙 北门 K-12 挂在哪里"))
    finally:
        asked.shutdown()
    brought = [item for item in items if "K-12" in item["content"]]
    assert brought and all(item["entries"] == [{"id": "agent3", "name": "天三"}] for item in brought)
    again = import_entry(root=root, entry_id="agent3", source=shu_old)
    assert again["status"] == "already_imported"
    assert _query(root, "SELECT count(*) FROM source_events") == [(2,)]
    # The embedding each old store had queued for it is queued again, under the source's new id.
    assert _query(
        root,
        """SELECT count(*) FROM work_items w JOIN source_events e ON e.event_id=w.subject_ref
                           WHERE w.work_type='embed' AND w.state='pending'""",
    ) == [(2,)]


def test_an_id_two_old_stores_share_otherwise_refuses_the_second_and_writes_nothing(root, entries):
    from scope_recall.maintenance.shared import SharedStoreError
    from scope_recall.maintenance.shared_import import import_entry

    agent3, agent2 = entries
    *_, shu_old = _old_store(agent3, _scope(agent3), "TEST 同一场旧对话，天三这边。")
    *_, quan_old = _old_store(agent2, _scope(agent2), "TEST 同一场旧对话，天二这边。")
    import_entry(root=root, entry_id="agent3", source=shu_old)
    with pytest.raises(SharedStoreError, match="cannot be imported as it is"):
        import_entry(root=root, entry_id="agent2", source=quan_old)
    assert _query(root, "SELECT entry_id, count(*) FROM source_events GROUP BY entry_id") == [("agent3", 1)]


def test_import_keeps_owner_actions_and_missing_source_suppression(root, entries):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    agent3, _agent2 = entries
    scope = _scope(agent3)
    core, context, (source,), old = _old_store(agent3, scope, "TEST suppressed import source")
    owner = MemoryManagement(core, InstallationMaintenance(context))
    owner.apply(owner.preview([source.ref], mode="suppress"))
    missing = "event-" + "f" * 64
    with core.storage.write(context, remaining_seconds=10) as tx:
        tx._check(write=True).executemany(
            "INSERT INTO restored_suppression_blocks VALUES ('event',?,1,?,NULL,NULL)",
            [(source.ref, scope), (missing, scope)],
        )

    result = import_entry(root=root, entry_id="agent3", source=old)
    assert result["counts"]["management_actions"] == 1
    assert result["counts"]["restored_suppression_blocks"] == 2
    ((imported, suppressed),) = _query(root, "SELECT event_id,suppressed FROM source_events")
    assert imported != source.ref and suppressed == 1
    ((actor, targets),) = _query(root, "SELECT actor,targets_json FROM management_actions")
    assert actor == "local_user_management"
    assert imported in {target["ref"] for target in json.loads(targets)}
    assert source.ref not in targets
    blocks = _query(root, "SELECT object_ref,revision FROM restored_suppression_blocks")
    assert (imported, 1) in blocks
    assert all(ref not in {source.ref, missing} and revision == 1 for ref, revision in blocks)
    with sqlite3.connect(root / "memory.sqlite3") as db:
        db.execute("UPDATE source_events SET suppressed=0 WHERE event_id=?", (imported,))
        assert db.execute("SELECT suppressed FROM source_events").fetchone()[0] == 1


def test_a_rehearsal_writes_nothing_and_another_homes_store_is_refused(root, entries):
    from scope_recall.maintenance.shared import SharedStoreError
    from scope_recall.maintenance.shared_import import import_entry

    agent3, _agent2 = entries
    *_, old = _old_store(agent3, _scope(agent3), "TEST 只在排练里导入的一句话。")
    assert import_entry(root=root, entry_id="agent3", source=old, dry_run=True)["status"] == "rehearsed"
    assert _query(root, "SELECT count(*) FROM source_events") == [(0,)]
    with pytest.raises(SharedStoreError, match="not of this entry's home"):
        import_entry(root=root, entry_id="agent2", source=old)


def test_what_an_old_store_forgot_stays_forgotten(root, entries):
    from scope_recall.core.delete_storage import group_digest
    from scope_recall.maintenance.shared_import import import_entry
    from v11_support import source_event

    agent3, _agent2 = entries
    scope = _scope(agent3)
    core, context, (secret,), old = _old_store(agent3, scope, "TEST 旧库里的保险柜密码是 5520。")
    for index in range(1, 9):  # the old store ran for a while: its epoch is well past the new store's
        core.record_event(
            context,
            source_event(source_event_key=f"legacy:memories:{index}", content=f"TEST 旧库里的第 {index} 句话。"),
            scope_id=scope,
            remaining_seconds=10,
        )
    core.record_event(
        context,
        source_event(source_event_key="legacy:memories:9", content=f"删除 {secret.ref}"),
        scope_id=scope,
        remaining_seconds=10,
    )
    core.forget(
        context,
        {
            "protocol_version": "1.1",
            "target_refs": [secret.ref],
            "mode": "delete",
            "expected_revisions": {secret.ref: secret.revision},
        },
        remaining_seconds=10,
    )

    result = import_entry(root=root, entry_id="agent3", source=old)
    assert result["counts"]["group_blocks_without_sources"] == 0
    # Recorded at the import, not at the old store's own epoch: one above this store's read as a
    # deletion after every read in its scopes, emptying recall and failing derived work there.
    [(epoch,)] = _query(root, "SELECT memory_epoch FROM instance_meta")
    assert _query(root, "SELECT DISTINCT memory_epoch FROM deletion_operations") == [(epoch,)]
    # A deletion takes the message that asked for it along; both groups stay blocked under the store's id.
    store = SimpleNamespace(installation_id=read_shared_payload(root)["installation_id"])
    assert set(_query(root, "SELECT group_sha256 FROM source_group_blocks")) == {
        (group_digest(store, scope, None, None, f"import:agent3:legacy:memories:{index}"),) for index in (0, 9)
    }
    asked = _provider(agent3)
    try:
        injected = asked.prefetch("保险柜密码 5520")
    finally:
        asked.shutdown()
    assert "5520" not in injected


def test_a_legacy_source_id_is_renamed_wherever_the_old_store_names_it(root, entries):
    """Sources a 2.x migration named event-legacy-<32 hex> are renamed in every table that names them, not in
    columns only, or their queued embeddings name no source and the worker drops every one."""
    from scope_recall.maintenance.shared_import import _Names, import_entry

    agent3, _agent2 = entries
    _core, _context, (said,), old = _old_store(agent3, _scope(agent3), "TEST 一条从 2.x 迁移来的旧记录。")
    legacy = "event-legacy-" + "0123456789abcdef" * 2
    with closing(sqlite3.connect(old / "memory.sqlite3")) as db:
        # The store as a 2.x migration left it: the legacy id in every column that names the source.
        db.execute("PRAGMA foreign_keys=OFF")
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for column in [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[2] == "TEXT"]:
                db.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (legacy, said.ref))
        db.execute(
            "UPDATE source_events SET extra_json=? WHERE event_id=?",
            (json.dumps({"TEST_cites": [f"{legacy}@1", "event-driven"]}), legacy),
        )
        db.commit()

    import_entry(root=root, entry_id="agent3", source=old)
    new = _Names("agent3").event(legacy)
    assert _query(root, f"SELECT count(*) FROM work_items WHERE work_type='embed' AND subject_ref='{new}'") == [(1,)]
    assert _query(root, "SELECT count(*) FROM work_items WHERE subject_ref LIKE 'event-legacy-%'") == [(0,)]
    ((extra,),) = _query(root, f"SELECT extra_json FROM source_events WHERE event_id='{new}'")
    assert json.loads(extra)["TEST_cites"] == [f"{new}@1", "event-driven"], "an id-shaped word that is no id stays"


def test_tool_outputs_retention_would_expire_at_once_are_not_queued_for_embedding(root, entries):
    """A withheld output's summary and a repeated tool output are sources only; an import does not embed them.

    Stores from earlier releases embedded both, and their embed history would queue them again, each
    embedded and then deleted by retention within the hour.  They are recorded as expired under
    retention's own reason instead.
    """
    from dataclasses import replace

    from scope_recall.maintenance.shared_import import _Names, import_entry
    from v11_support import source_event

    agent3, _agent2 = entries
    scope = _scope(agent3)
    core, context, (said,), old = _old_store(agent3, scope, "TEST 一句用户说过的话。")
    tool = replace(context, actor_origin="tool_observation")
    outputs = (
        "Tool execution summary: TEST-list-files (exit 0) — output omitted",
        "TEST 工具输出：目录里有三个文件。",
        "TEST 工具输出：目录里有三个文件。",
    )
    made = [
        core.record_event(
            tool,
            source_event(
                source_event_key=f"legacy:tools:{index}", origin="tool_observation", role="tool", content=text
            ),
            scope_id=scope,
            remaining_seconds=10,
        ).event_refs[0]
        for index, text in enumerate(outputs)
    ]
    summary, first, repeat = made
    errored = core.record_event(
        tool,
        source_event(
            source_event_key="legacy:tools:errored",
            origin="tool_observation",
            role="tool",
            content="Tool execution summary (terminal): tool=terminal; output_chars=88; exit_code=1; "
            "error=TEST-deploy 配置文件不可写; output_preview=omitted",
        ),
        scope_id=scope,
        remaining_seconds=10,
    ).event_refs[0]
    with closing(sqlite3.connect(old / "memory.sqlite3")) as db:
        # The history an earlier release left: every source embedded, whatever it was.
        for ref in (said, *made):
            db.execute(
                """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,state,available_at)
                          VALUES ('embed',?,?,?,'done','2026-09-01T00:00:00Z')
                          ON CONFLICT(work_type,subject_ref,subject_revision) DO UPDATE SET state='done'""",
                (ref.ref, ref.revision, scope),
            )
        # And indexed every source by its words, the withheld output's summary too.
        db.execute("INSERT OR IGNORE INTO lexical_terms(term) VALUES ('tool'),('summary'),('omitted')")
        for ref in (summary.ref, errored.ref):
            db.execute(
                """INSERT OR IGNORE INTO lexical_postings(term_id,source_id) SELECT t.term_id,e.source_id
                          FROM lexical_terms t JOIN source_events e ON e.event_id=?
                          WHERE t.term IN ('tool','summary','omitted')""",
                (ref,),
            )
        db.commit()

    result = import_entry(root=root, entry_id="agent3", source=old)
    assert result["counts"]["embeddings_retention_would_expire"] == 2, result["counts"]
    assert result["counts"]["withheld_outputs"] == 2, result["counts"]
    held = (
        "SELECT count(*) FROM lexical_postings p JOIN source_events e ON e.source_id=p.source_id WHERE e.event_id='{}'"
    )
    names = _Names("agent3", frozenset({said.ref, errored.ref, *(ref.ref for ref in made)}))
    queued = {
        row[0] for row in _query(root, "SELECT subject_ref FROM work_items WHERE work_type='embed' AND state='pending'")
    }
    assert queued == {names.event(said.ref), names.event(first.ref)}, queued
    assert set(_query(root, "SELECT source_ref, reason FROM expired_vectors")) == {
        (names.event(summary.ref), "omitted"),
        (names.event(repeat.ref), "repeat"),
    }
    assert _query(root, "SELECT count(*) FROM source_events") == [(5,)], (
        "every source is imported; only embedding is skipped"
    )
    assert _query(root, held.format(names.event(summary.ref))) == [(0,)], "the summary's postings stay behind"
    assert _query(root, held.format(names.event(first.ref))) != [(0,)], "a tool output keeps its words"
    kept = {
        row[0]
        for row in _query(
            root,
            f"""SELECT t.term FROM lexical_postings p JOIN lexical_terms t
        ON t.term_id=p.term_id JOIN source_events e ON e.source_id=p.source_id
        WHERE e.event_id='{names.event(errored.ref)}'""",
        )
    }
    assert kept and "tool" not in kept and "test-deploy" in kept, "a placeholder keeps its error text alone"


def test_an_imported_store_holds_no_denial_as_a_value(root, entries):
    """The store an entry brings in took a denial for its slot's value as readily as any other value.  The import
    settles each as restore-displaced-values does, in its own transaction: "不是 us-east-2", all TEST-project's region
    held, leaves it no value, "不是 Jenkins 了" ends the Jenkins it denies, and the person's own 不吃辣 stays their
    value.  Every version stays, and the receipt counts what moved."""
    from dataclasses import replace

    from scope_recall.contracts import TrustedSourcePrincipal
    from scope_recall.core.claims import Qualification
    from scope_recall.maintenance.shared_import import import_entry
    from v11_support import source_event

    agent3, _agent2 = entries
    scope = _scope(agent3)
    core, context, (region, jenkins, ended), old = _old_store(
        agent3,
        scope,
        "TEST-project deploy region 不是 us-east-2。",
        "TEST-project CI service 是 Jenkins。",
        "TEST-project CI service 不是 Jenkins 了。",
    )
    owner = replace(context, source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"))
    said = source_event(source_event_key="legacy:memories:diet", content="我不吃辣。")
    diet = core.record_event(owner, said, scope_id=scope, remaining_seconds=10).event_refs[0]

    def proposal(source, predicate, value, kind="fact", subject="TEST-project"):
        event = core.source(context, source.ref, source.revision).event
        quoted = [dict(source_ref=source.ref, source_revision=source.revision, quote=event["content"])]
        frame = dict(kind=kind, subject=subject, predicate=predicate, value_text=value, conditions=[])
        times = dict(statement_kind="assertion", valid_from=event["occurred_at"], valid_to=None)
        return {**frame, **times, "evidence_spans": quoted}

    def accepted(ctx, claim):
        refs = [f"{span['source_ref']}@{span['source_revision']}" for span in claim["evidence_spans"]]
        body = dict(protocol_version="1.1", source_refs=refs, claim_proposals=[claim], resume_proposals=[])
        return core.records.accept_claim_proposals(
            ctx, {**body, "reference_proposals": []}, scope_id=scope, remaining_seconds=10
        ).items[0]

    assert accepted(context, proposal(jenkins, "CI service", "Jenkins")).state == "active"
    assert accepted(owner, proposal(diet, "饮食", "不吃辣", kind="preference", subject="user")).state == "active"
    # As a release before the denial rule wrote them: each denial its slot's current value.
    with core.storage.write(context) as tx:
        verdict = Qualification("active", "direct_report", "explicit_scoped_source")
        tx.claims.append(
            scope, proposal(region, "deploy region", "不是 us-east-2"), verdict, recorded_at=core.clock.utc_now()
        )
        head = next(
            v
            for v in tx.claims.slot(scope, proposal(jenkins, "CI service", "Jenkins"))
            if v.revision == v.current_revision
        )
        tx.claims.append(
            scope,
            proposal(ended, "CI service", "不是 Jenkins"),
            verdict,
            recorded_at=core.clock.utc_now(),
            previous=head,
        )

    result = import_entry(root=root, entry_id="agent3", source=old)
    assert (result["status"], result["counts"]["denials_settled"]) == ("imported", 2), result
    heads = dict(
        _query(
            root,
            "SELECT c.predicate, v.state FROM claims c JOIN claim_versions v"
            " ON v.claim_id=c.claim_id AND v.revision=c.current_revision",
        )
    )
    assert heads == {"deploy region": "retracted", "CI service": "retracted", "饮食": "active"}
    versions = _query(
        root,
        "SELECT c.predicate, v.state, json_extract(v.payload_json, '$.value_text') FROM claims c"
        " JOIN claim_versions v USING(claim_id) WHERE c.predicate='CI service' ORDER BY v.revision",
    )
    assert versions == [
        ("CI service", "active", "Jenkins"),
        ("CI service", "active", "不是 Jenkins"),
        ("CI service", "retracted", "不是 Jenkins"),
    ]


def test_import_preserves_every_tabulated_description_and_settles_only_denials(root, entries):
    """An imported store retains every description and its history; repeating the import changes no claim."""
    agent3, _ = entries
    scope = _scope(agent3)
    states = (*NEGATED_STATES["zh"], *NEGATED_STATES["en"])
    values = (*states, "不是 CircleCI", "not Jenkins")
    texts = [f"TEST-project state {i} 是 {value}。" for i, value in enumerate(values)]
    core, context, sources, old = _old_store(agent3, scope, *texts)
    with core.storage.write(context) as tx:
        for i, (value, source) in enumerate(zip(values, sources, strict=True)):
            event = core.source(context, source.ref, source.revision).event
            payload = dict(
                kind="fact",
                subject="TEST-project",
                predicate=f"state {i}",
                value_text=value,
                conditions=[],
                statement_kind="assertion",
                valid_from=event["occurred_at"],
                valid_to=None,
                evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision, quote=texts[i])],
            )
            tx.claims.append(
                scope,
                payload,
                Qualification("active", "direct_report", "explicit_scoped_source"),
                recorded_at=core.clock.utc_now(),
            )
    report = import_entry(root=root, entry_id="agent3", source=old, dry_run=True)
    assert report["status"] == "rehearsed" and report["counts"]["denials_settled"] == 2
    assert _query(root, "SELECT count(*) FROM claims") == [(0,)]
    report = import_entry(root=root, entry_id="agent3", source=old)
    assert report["status"] == "imported" and report["counts"]["denials_settled"] == 2
    query = (
        "SELECT c.predicate,v.state,json_extract(v.payload_json,'$.value_text') FROM claims c"
        " JOIN claim_versions v USING(claim_id) ORDER BY c.predicate,v.revision"
    )
    versions = _query(root, query)
    for i, value in enumerate(states):
        assert [row for row in versions if row[0] == f"state {i}"] == [(f"state {i}", "active", value)]
    for i, value in enumerate(values[len(states) :], len(states)):
        assert [row for row in versions if row[0] == f"state {i}"] == [
            (f"state {i}", "active", value),
            (f"state {i}", "retracted", value),
        ]
    assert import_entry(root=root, entry_id="agent3", source=old)["status"] == "already_imported"
    assert _query(root, query) == versions
