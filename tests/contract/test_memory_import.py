"""Synthetic source imports keep permission separate from evidence authority."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.memory_import import import_record
from scope_recall.maintenance.import_sources import ImportRecord, read_source
from scope_recall.maintenance.memory_import import apply_import, preview_import
from v11_support import context, source_event

SAMPLES = Path(__file__).parents[1] / "fixtures" / "memory_import"


@pytest.fixture
def store(tmp_path):
    ctx = context(tmp_path / "TEST-import")
    app = MemoryCore(CoreConfig(ctx.binding))
    app.initialize()
    return app, ctx


def records(kind):
    return read_source(kind, SAMPLES / kind / "memories.json" if kind == "mem0" else SAMPLES / kind)


@pytest.mark.parametrize("kind", ["claude-code", "codex", "hermes", "chatgpt", "claude", "mem0"])
def test_each_source_preserves_identity_time_and_requires_review(store, kind):
    app, ctx = store
    items = records(kind)
    assert items
    for item in items:
        receipt = import_record(app, ctx, item, source=kind, source_id="TEST-account", scope_id="TEST-scope")
        assert receipt.durability == "persisted"
        saved = app.source(replace(ctx, project_id=item.project), receipt.event_refs[0].ref, 1)
        assert saved.event["occurred_at"] == item.occurred_at
        assert saved.event["origin"] == "imported"
        assert saved.event["source_original_origin"] == item.original_origin
        assert saved.import_provenance_sha256
        if item.role != "user":
            from scope_recall.core.evidence_question import evidence_text, unanswerable_reason

            assert unanswerable_reason({"kind": "fact"}, [evidence_text(saved)]) == "no_authoritative_evidence"
    with app.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM claim_versions WHERE state='active'").fetchone()[0] == 0


def test_preview_and_cancel_never_open_or_change_database(store):
    app, _ctx = store
    before = app.storage.path.read_bytes()
    plan = preview_import("chatgpt", SAMPLES / "chatgpt", source_id="TEST-account")
    assert plan["count"] == 2
    assert plan["projects"] == {"TEST-project": 2}
    assert {i["original_origin"] for i in plan["items"]} == {"human_direct", "assistant_visible"}
    assert plan["time_range"]["since"] == "2026-01-01T12:00:00Z"
    assert app.storage.path.read_bytes() == before


def test_imported_user_words_are_searchable_and_replays_are_idempotent(store):
    app, ctx = store
    item = records("chatgpt")[0]
    first = import_record(app, ctx, item, source="chatgpt", source_id="TEST-account", scope_id="TEST-scope")
    replay = import_record(app, ctx, item, source="chatgpt", source_id="TEST-account", scope_id="TEST-scope")
    assert replay.disposition == "duplicate"
    assert replay.event_refs == first.event_refs or replay.event_refs[0].ref == first.event_refs[0].ref
    found = app.search_sources(replace(ctx, project_id=item.project), "saffron")
    assert first.event_refs[0].ref in {s.ref for s in found}
    changed = import_record(
        app,
        ctx,
        replace(item, content="TEST project prefers indigo"),
        source="chatgpt",
        source_id="TEST-account",
        scope_id="TEST-scope",
    )
    assert changed.event_refs[0].ref == first.event_refs[0].ref
    assert changed.event_refs[0].revision == 2


def test_stale_or_missing_confirmation_refuses_every_write(store):
    app, ctx = store
    plan = preview_import("chatgpt", SAMPLES / "chatgpt", source_id="TEST-account")
    for approval in (None, "wrong"):
        with pytest.raises(ContractError):
            apply_import(app, ctx, plan, preview_id=approval, scope_id="TEST-scope")
    with app.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_private_content_never_reaches_truth_or_indexes(store):
    app, ctx = store
    item = ImportRecord("TEST-private", "TEST public <private>hidden-canary</private> saffron", "assistant")
    receipt = import_record(app, ctx, item, source="mem0", source_id="TEST-account", scope_id="TEST-scope")
    assert "private_content_omitted" in receipt.gaps
    assert b"hidden-canary" not in app.storage.path.read_bytes()
    assert not app.search_sources(ctx, "hidden-canary")
    assert app.search_sources(ctx, "saffron")


def test_active_private_mode_refuses_undated_import(store):
    app, ctx = store
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    api = MemoryManagement(app, InstallationMaintenance(replace(ctx, actor_origin="host_generated")))
    rule = api.private_on(target="global", mode="read-only")
    receipt = import_record(
        app,
        ctx,
        ImportRecord("TEST-private-mode", "TEST saffron", "assistant"),
        source="mem0",
        source_id="TEST-account",
        scope_id="TEST-scope",
    )
    assert receipt.error_code == "PRIVATE_MODE"
    assert rule


def test_new_reader_is_added_without_changing_dispatch(tmp_path):
    sample = tmp_path / "TEST-session"
    sample.write_text("TEST saffron", encoding="utf-8")
    readers = {"session-history": lambda path: [ImportRecord("TEST-m1", path.read_text(), "user")]}
    assert read_source("session-history", sample, readers=readers)[0].key == "TEST-m1"


def test_inline_private_is_filtered_for_normal_capture_too(store):
    app, ctx = store
    receipt = app.record_event(
        ctx,
        source_event(content="TEST public <private>hidden-canary</private> saffron"),
        scope_id="TEST-scope",
    )
    assert "private_content_omitted" in receipt.gaps
    assert not app.search_sources(ctx, "hidden-canary")


def test_claude_mem_sqlite_is_read_only_and_never_authoritative(store, tmp_path):
    app, ctx = store
    path = tmp_path / "TEST-claude-mem.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript((SAMPLES / "claude-mem" / "schema.sql").read_text(encoding="utf-8"))
    before = path.read_bytes()
    items = read_source("claude-mem", path)
    assert len(items) == 2
    assert items[0].occurred_at == "2026-01-01T12:00:00Z"
    for item in items:
        receipt = import_record(app, ctx, item, source="claude-mem", source_id="TEST-account", scope_id="TEST-scope")
        saved = app.source(replace(ctx, project_id=item.project), receipt.event_refs[0].ref, 1)
        assert saved.event["source_original_origin"] == "assistant_visible"
        assert receipt.admission == ("admission_source_only:import_requires_confirmation",)
    assert path.read_bytes() == before


def test_upgrade_refusal_has_no_partial_write_and_same_preview_retries(store):
    from concurrent.futures import ThreadPoolExecutor

    from scope_recall.core.file_lock import advisory_file_lock
    from scope_recall.core.store_upgrade import UPGRADE_LOCK, UPGRADE_STATE

    app, ctx = store
    plan = preview_import("chatgpt", SAMPLES / "chatgpt", source_id="TEST-account")
    root = app.config.binding.data_directory
    (root / UPGRADE_STATE).write_text(json.dumps({"status": "running"}), encoding="utf-8")
    with advisory_file_lock(root / UPGRADE_LOCK), ThreadPoolExecutor(1) as pool:
        refused = pool.submit(
            apply_import, app, ctx, plan, preview_id=plan["preview_id"], scope_id="TEST-scope"
        ).result(5)
    assert refused["processed"] == 0
    assert refused["error"] == {"code": "SCHEMA_UNSUPPORTED", "field": "upgrade_in_progress", "at": 0}
    result = apply_import(app, ctx, plan, preview_id=plan["preview_id"], scope_id="TEST-scope")
    assert result["complete"] and result["counts"] == {"inserted": 2}
    assert apply_import(app, ctx, plan, preview_id=plan["preview_id"], scope_id="TEST-scope")["counts"] == {
        "duplicate": 2
    }


@pytest.mark.parametrize("kind", ["claude-code", "codex", "hermes", "chatgpt", "claude", "mem0"])
def test_summary_cannot_promote_a_fact_even_when_model_claims_it_can(store, kind):
    from scope_recall.core.evidence_question import evidence_text
    from test_claims import accept, draft

    app, ctx = store
    item = next(i for i in records(kind) if i.role == "assistant")
    receipt = import_record(app, ctx, item, source=kind, source_id="TEST-account", scope_id="TEST-scope")
    scoped = replace(ctx, project_id=item.project)
    saved = app.source(scoped, receipt.event_refs[0].ref, 1)
    result = accept(
        app,
        scoped,
        draft(
            saved,
            "saffron",
            subject="TEST project",
            predicate="prefers",
            kind="fact",
            statement_kind="assertion",
            quote_override=saved.event["content"],
        ),
    )
    assert result.items[0].state == "proposed"
    assert evidence_text(saved).origin == "assistant_visible"


def test_memory_import_command_previews_without_opening_store_and_requires_exact_confirmation(
    store, monkeypatch, capsys
):
    from scope_recall.maintenance import memory

    app, ctx = store
    args = [
        "import",
        "--source",
        "chatgpt",
        "--from",
        str(SAMPLES / "chatgpt"),
        "--source-id",
        "TEST-account",
        "--json",
    ]

    def forbidden(*args, **kwargs):
        raise AssertionError("preview cannot open a database")

    monkeypatch.setattr(memory, "open_management", forbidden)
    before = app.storage.path.read_bytes()
    assert memory.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    plan = output["data"]["import_preview"]
    assert plan["count"] == 2 and "_records" not in plan
    assert memory.main(args + ["--yes"]) == 2
    capsys.readouterr()
    assert app.storage.path.read_bytes() == before
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    monkeypatch.setattr(
        memory, "open_management", lambda *args, **kwargs: MemoryManagement(app, InstallationMaintenance(ctx))
    )
    assert memory.main(args + ["--yes", "--preview-id", plan["preview_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["result"]["counts"] == {"inserted": 2}


@pytest.mark.parametrize(
    "text",
    [
        "<private>hidden-canary</private>",
        "<private>hidden-canary",
        "<private>outer<private>hidden-canary</private>tail</private>",
    ],
)
def test_fully_private_import_has_no_source_or_work(store, text):
    app, ctx = store
    receipt = import_record(
        app,
        ctx,
        ImportRecord("TEST-private", text, "assistant"),
        source="mem0",
        source_id="TEST-account",
        scope_id="TEST-scope",
    )
    assert receipt.durability == "not_persisted"
    with app.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM source_events").fetchone()[0] == 0


def test_zip_export_and_project_time_selection_match_json(tmp_path):
    import zipfile

    path = tmp_path / "TEST-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(SAMPLES / "chatgpt" / "conversations.json", "export/conversations.json")
        archive.writestr("../../outside.txt", "TEST must never extract")
    assert read_source("chatgpt", path) == records("chatgpt")
    plan = preview_import(
        "chatgpt", path, source_id="TEST-account", project="TEST-project", since="2026-01-01T12:00:30Z"
    )
    assert plan["count"] == 1 and plan["items"][0]["role"] == "assistant"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_large_segmented_import_is_idempotent_and_changed_file_time_is_not_new_content(store):
    app, ctx = store
    item = ImportRecord("TEST-large", "TEST saffron " * 6000, "assistant", occurred_at="2026-01-01T12:00:00Z")
    first = import_record(app, ctx, item, source="mem0", source_id="TEST-account", scope_id="TEST-scope")
    again = import_record(
        app,
        ctx,
        replace(item, occurred_at="2026-01-02T12:00:00Z"),
        source="mem0",
        source_id="TEST-account",
        scope_id="TEST-scope",
    )
    assert len(first.event_refs) == 2
    assert again.disposition == "duplicate"


def test_import_progress_reports_counts_without_source_text_and_detects_interruption(store):
    from scope_recall.maintenance.import_progress import read_progress, run_import

    app, ctx = store
    plan = preview_import("chatgpt", SAMPLES / "chatgpt", source_id="TEST-account")
    result = run_import(app, ctx, plan, preview_id=plan["preview_id"], scope_id="TEST-scope")
    state = read_progress(app.config.binding.data_directory)
    assert result["complete"] and state["status"] == "complete"
    assert state["processed"] == state["total"] == 2
    assert "saffron" not in json.dumps(state)
    path = app.config.binding.data_directory / "memory-import.json"
    path.write_text(json.dumps({**state, "status": "running"}), encoding="utf-8")
    assert read_progress(app.config.binding.data_directory)["status"] == "interrupted"


def test_imported_user_original_is_returned_by_recall(store):
    from v11_support import recall_request

    app, ctx = store
    item = records("chatgpt")[0]
    receipt = import_record(app, ctx, item, source="chatgpt", source_id="TEST-account", scope_id="TEST-scope")
    packet = app.recall(replace(ctx, project_id=item.project), recall_request(query="saffron", mode="current"))
    assert receipt.event_refs[0].ref in {entry.ref for entry in packet.items}


def test_reimporting_already_sanitized_text_keeps_existing_version(store):
    app, ctx = store
    item = ImportRecord("TEST-private-replay", "TEST <private>hidden-canary</private> saffron", "assistant")
    first = import_record(app, ctx, item, source="mem0", source_id="TEST-account", scope_id="TEST-scope")
    again = import_record(
        app, ctx, replace(item, content="TEST  saffron"), source="mem0", source_id="TEST-account", scope_id="TEST-scope"
    )
    assert again.disposition == "duplicate"
    assert again.event_refs[0].revision == first.event_refs[0].revision


def test_destination_mapping_keeps_original_project_groups_visible():
    plan = preview_import(
        "claude-code", SAMPLES / "claude-code", source_id="TEST-account", destination_project="TEST-project"
    )
    assert plan["projects"] == {"(unassigned)": 2}
    assert all(item["project"] is None and item["write_project"] == "TEST-project" for item in plan["items"])


def test_unquoted_markdown_instant_is_preserved(tmp_path):
    path = tmp_path / "TEST-memory.md"
    path.write_text("---\nmodified: 2026-01-01T12:00:00Z\n---\nTEST saffron", encoding="utf-8")
    assert read_source("claude-code", path)[0].occurred_at == "2026-01-01T12:00:00Z"


def test_time_selection_orders_subsecond_instants_correctly(tmp_path):
    path = tmp_path / "TEST-mem0.json"
    path.write_text(
        json.dumps(
            {
                "results": [
                    {"id": "TEST-fraction", "memory": "TEST saffron", "created_at": "2026-01-01T12:00:00.100000Z"}
                ]
            }
        ),
        encoding="utf-8",
    )
    plan = preview_import("mem0", path, source_id="TEST-account", since="2026-01-01T12:00:00Z")
    assert plan["count"] == 1


def test_structured_mem0_export_keeps_unknown_origin(tmp_path):
    path = tmp_path / "TEST-structured.json"
    path.write_text(json.dumps({"favorite_color": "saffron"}), encoding="utf-8")
    item = read_source("mem0", path)[0]
    assert item.original_origin == "origin_unknown" and item.occurred_at is None


def test_import_receipt_explains_private_removal(store):
    app, ctx = store
    item = ImportRecord("TEST-private-result", "TEST <private>hidden-canary</private> saffron", "assistant")
    from scope_recall.core.memory_import import digest
    from scope_recall.maintenance.memory_import import _public_item

    public = dict(source="mem0", source_id="TEST-account", count=1, items=[_public_item(item)])
    plan = {**public, "preview_id": digest(public), "_records": (item,)}
    result = apply_import(app, ctx, plan, preview_id=plan["preview_id"], scope_id="TEST-scope")
    assert result["gaps"] == {"private_content_omitted": 1}


def test_shared_store_owner_import_uses_registered_management_entry(tmp_path, monkeypatch, capsys):
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance
    from scope_recall.maintenance import memory

    binding = replace(context(tmp_path / "TEST-shared-import").binding, installation_kind="shared")
    ctx = replace(context(binding.data_directory), binding=binding, actor_origin="host_generated")
    app = MemoryCore(CoreConfig(binding))
    app.initialize()
    api = MemoryManagement(app, InstallationMaintenance(ctx))
    monkeypatch.setattr(memory, "open_management", lambda *args, **kwargs: api)
    plan = preview_import("chatgpt", SAMPLES / "chatgpt", source_id="TEST-account")
    args = [
        "import",
        "--source",
        "chatgpt",
        "--from",
        str(SAMPLES / "chatgpt"),
        "--source-id",
        "TEST-account",
        "--yes",
        "--preview-id",
        plan["preview_id"],
        "--json",
    ]
    assert memory.main(args) == 0
    assert json.loads(capsys.readouterr().out)["data"]["result"]["counts"] == {"inserted": 2}
    with app.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT DISTINCT entry_id FROM source_events").fetchone()[0] == "local-management"


def test_malformed_frontmatter_is_a_content_free_contract_error(tmp_path):
    path = tmp_path / "TEST-malformed-memory.md"
    path.write_text('---\nmodified: "hidden-canary\n---\nTEST saffron', encoding="utf-8")
    with pytest.raises(ContractError) as failure:
        read_source("claude-code", path)
    assert "hidden-canary" not in str(failure.value)


def test_codex_generated_headers_preserve_each_thread_time_and_project():
    items = read_source("codex", SAMPLES / "codex_formats")
    assert len(items) == 3
    assert {item.occurred_at for item in items} == {"2026-01-01T12:00:00Z", "2026-01-02T13:00:00Z"}
    assert {item.project for item in items} == {"C:/TEST/project-a", "C:/TEST/project-b"}
    assert {item.session for item in items} == {"TEST-thread-a", "TEST-thread-b"}
    assert all(item.original_origin == "assistant_visible" for item in items)


def test_codex_summary_identity_survives_a_generated_filename_change(store, tmp_path):
    app, ctx = store
    original = SAMPLES / "codex_formats" / "rollout_summaries" / "TEST-summary-a.md"
    changed = tmp_path / "TEST-renamed-summary.md"
    changed.write_bytes(original.read_bytes())
    before, after = read_source("codex", original)[0], read_source("codex", changed)[0]
    assert before.key == after.key
    first = import_record(app, ctx, before, source="codex", source_id="TEST-account", scope_id="TEST-scope")
    replay = import_record(app, ctx, after, source="codex", source_id="TEST-account", scope_id="TEST-scope")
    assert replay.disposition == "duplicate" and first.event_refs[0].ref == replay.event_refs[0].ref


def test_private_spans_are_removed_before_splitting_generated_thread_sections(store, tmp_path):
    app, ctx = store
    path = tmp_path / "raw_memories.md"
    path.write_text(
        """# Raw Memories

## Thread `TEST-thread-a`
updated_at: 2026-01-01T12:00:00Z
cwd: C:/TEST/project-a

TEST public saffron. <private>hidden-canary
## Thread `TEST-hidden-thread`
updated_at: 2026-01-02T13:00:00Z
cwd: C:/TEST/hidden-project

hidden-canary</private>
## Thread `TEST-thread-b`
updated_at: 2026-01-03T13:00:00Z
cwd: C:/TEST/project-b

TEST public indigo.
""",
        encoding="utf-8",
    )
    items = read_source("codex", path)
    assert len(items) == 2
    assert "TEST-hidden-thread" not in json.dumps([item.key for item in items])
    for item in items:
        assert "hidden-canary" not in item.content
        receipt = import_record(app, ctx, item, source="codex", source_id="TEST-account", scope_id="TEST-scope")
        assert receipt.durability == "persisted" and "private_content_omitted" in receipt.gaps


def test_claude_memory_exports_are_assistant_material_with_original_project_keys(store):
    app, ctx = store
    items = read_source("claude", SAMPLES / "claude_export" / "memories.json")
    assert len(items) == 2
    assert {item.project for item in items} == {None, "TEST-project-a"}
    assert all(item.original_origin == "assistant_visible" and item.occurred_at is None for item in items)
    for item in items:
        receipt = import_record(app, ctx, item, source="claude", source_id="TEST-account", scope_id="TEST-scope")
        assert receipt.admission == ("admission_source_only:import_requires_confirmation",)
    plan = preview_import("claude", SAMPLES / "claude_export" / "memories.json", source_id="TEST-account")
    assert "hidden-canary" not in json.dumps({k: v for k, v in plan.items() if k != "_records"})


def test_claude_zip_reads_memories_and_chats_without_guessing_chat_projects(tmp_path):
    import zipfile

    path = tmp_path / "TEST-claude-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("conversations.json", "memories.json"):
            archive.write(SAMPLES / "claude_export" / name, "export/" + name)
    items = read_source("claude", path)
    assert len(items) == 4
    assert all(item.project is None for item in items if item.role == "user")
    assert any(item.project == "TEST-project-a" for item in items)


def test_authenticated_destination_project_is_not_replaced_by_source_metadata(store):
    app, ctx = store
    destination = replace(ctx, project_id="TEST-destination")
    item = ImportRecord("TEST-mapped", "TEST saffron", "assistant", project="TEST-original")
    receipt = import_record(app, destination, item, source="mem0", source_id="TEST-account", scope_id="TEST-scope")
    saved = app.source(destination, receipt.event_refs[0].ref, 1)
    assert saved is not None and saved.project_id == "TEST-destination"


def test_import_without_a_timezone_cannot_use_the_local_machine_clock():
    from scope_recall.maintenance.import_read import timestamp

    with pytest.raises(ContractError):
        timestamp("2026-01-01T12:00:00")
