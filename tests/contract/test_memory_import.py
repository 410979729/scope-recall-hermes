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
        "--root",
        str(ctx.binding.data_directory),
        "--scope",
        "TEST-scope",
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
        archive.writestr("attachments/outside.txt", "TEST must never extract")
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
    args = [
        "import",
        "--source",
        "chatgpt",
        "--from",
        str(SAMPLES / "chatgpt"),
        "--source-id",
        "TEST-account",
        "--root",
        str(binding.data_directory),
        "--scope",
        "TEST-scope",
        "--json",
    ]
    assert memory.main(args) == 0
    plan = json.loads(capsys.readouterr().out)["data"]["import_preview"]
    assert memory.main(args + ["--yes", "--preview-id", plan["preview_id"]]) == 0
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


@pytest.mark.parametrize("kind", ["chatgpt", "mem0"])
def test_production_mode_import_keeps_provenance_without_test_fields(tmp_path, kind):
    ctx = context(tmp_path / "TEST-production-import", test_mode=False)
    app = MemoryCore(CoreConfig(ctx.binding))
    app.initialize()
    item = records(kind)[0]
    from scope_recall.core.memory_import import prefetch_new_sources

    options = dict(source=kind, source_id="TEST-account", scope_id="TEST-scope")
    hints = prefetch_new_sources(app, ctx, (item,), **options)
    receipt = import_record(app, ctx, item, _new_sources=hints, **options)
    assert import_record(app, ctx, item, **options).disposition == "duplicate"
    assert receipt.durability == "persisted"
    saved = app.source(replace(ctx, project_id=item.project), receipt.event_refs[0].ref, 1)
    assert saved is not None and saved.import_provenance_sha256
    assert "dataset_id" not in saved.event
    if item.role != "user":
        assert receipt.admission == ("admission_source_only:import_requires_confirmation",)
    else:
        from v11_support import recall_request

        packet = app.recall(replace(ctx, project_id=item.project), recall_request(query="saffron", mode="current"))
        assert receipt.event_refs[0].ref in {entry.ref for entry in packet.items}


@pytest.mark.parametrize("field", ["root", "scope", "project_root"])
def test_approval_is_bound_to_normalized_write_target(tmp_path, monkeypatch, capsys, field):
    from scope_recall.maintenance import memory

    args = [
        "import",
        "--source",
        "chatgpt",
        "--from",
        str(SAMPLES / "chatgpt"),
        "--source-id",
        "TEST-account",
        "--root",
        str(tmp_path / "TEST-destination"),
        "--scope",
        "TEST-scope",
        "--project-root",
        str(tmp_path / "TEST-project"),
        "--json",
    ]
    monkeypatch.setattr(memory, "open_management", lambda *a, **kw: pytest.fail("target change cannot open store"))
    assert memory.main(args) == 0
    before = json.loads(capsys.readouterr().out)["data"]["import_preview"]["preview_id"]
    changed = list(args)
    flag = "--" + field.replace("_", "-")
    changed[changed.index(flag) + 1] += "-changed"
    assert memory.main(changed) == 0
    after = json.loads(capsys.readouterr().out)["data"]["import_preview"]["preview_id"]
    assert before != after
    assert memory.main(changed + ["--yes", "--preview-id", before]) == 2
    capsys.readouterr()


@pytest.mark.parametrize(
    "content",
    [
        {"content_type": "unknown", "parts": "hidden-canary"},
        {"content_type": "text", "parts": "hidden-canary"},
        {"content_type": "text", "parts": ["TEST", {"text": "hidden-canary"}]},
    ],
)
def test_chatgpt_unknown_text_shape_is_never_attested(tmp_path, content):
    document = json.loads((SAMPLES / "chatgpt" / "conversations.json").read_text())
    document[0]["mapping"]["m1"]["message"]["content"] = content
    path = tmp_path / "TEST-conversations.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError) as failure:
        read_source("chatgpt", path)
    assert "hidden-canary" not in str(failure.value)


@pytest.mark.parametrize(
    "kind,bad", [("chatgpt", "author"), ("chatgpt", "message"), ("claude", "message"), ("claude", "content")]
)
def test_chat_exports_reject_malformed_structures_without_source_text(tmp_path, kind, bad):
    document = json.loads((SAMPLES / kind / "conversations.json").read_text())
    if kind == "chatgpt":
        message = document[0]["mapping"]["m1"]["message"]
        if bad == "author":
            message["author"] = "hidden-canary"
        else:
            document[0]["mapping"]["m1"]["message"] = ["hidden-canary"]
    elif bad == "message":
        document[0]["chat_messages"][0] = ["hidden-canary"]
    else:
        document[0]["chat_messages"][0]["content"] = [{"type": "text", "text": ["hidden-canary"]}]
    path = tmp_path / "TEST-conversations.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError) as failure:
        read_source(kind, path)
    assert "hidden-canary" not in str(failure.value)


@pytest.mark.parametrize(
    "names",
    [
        ["conversations.json", "conversations.json"],
        ["../conversations.json"],
        ["export/../conversations.json"],
        ["/conversations.json"],
        ["export\\conversations.json"],
        ["export//conversations.json"],
    ],
)
def test_export_archive_rejects_duplicate_or_ambiguous_members(tmp_path, names):
    import zipfile

    path = tmp_path / "TEST-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, (SAMPLES / "chatgpt" / "conversations.json").read_bytes())
    if any("\\" in name for name in names):
        # The Windows ZIP writer canonicalizes separators; change both name
        # headers to exercise a noncanonical archive received from elsewhere.
        path.write_bytes(path.read_bytes().replace(b"export/conversations.json", b"export\\conversations.json"))
    with pytest.raises(ContractError) as failure:
        read_source("chatgpt", path)
    assert failure.value.field == "import_archive"


def test_broken_archive_never_escapes_cli_or_echoes_member_name(tmp_path, monkeypatch, capsys):
    from scope_recall.maintenance import memory

    path = tmp_path / "TEST-broken.zip"
    path.write_bytes(b"TEST not an archive <private>hidden-canary")
    monkeypatch.setattr(memory, "open_management", lambda *a, **kw: pytest.fail("bad archive cannot open store"))
    assert (
        memory.main(["import", "--source", "chatgpt", "--from", str(path), "--source-id", "TEST-account", "--json"])
        == 2
    )
    output = capsys.readouterr()
    assert "hidden-canary" not in output.out + output.err
    assert json.loads(output.out)["field"] == "import_archive"


@pytest.mark.parametrize("field,value", [("metadata", ["hidden-canary"]), ("id", {"hidden-canary": 1})])
def test_mem0_malformed_metadata_is_a_content_free_contract_error(tmp_path, field, value):
    document = {"results": [{"id": "TEST-memory", "memory": "TEST saffron", "metadata": {}}]}
    document["results"][0][field] = value
    path = tmp_path / "TEST-mem0.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError) as failure:
        read_source("mem0", path)
    assert "hidden-canary" not in str(failure.value)


def test_export_archive_is_read_from_disk_once(tmp_path, monkeypatch):
    import io
    import zipfile

    path = tmp_path / "TEST-once.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(SAMPLES / "chatgpt" / "conversations.json", "export/conversations.json")
    original, opened = io.open, []

    def observe(file, *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file) == path:
            opened.append(file)
        return original(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", observe)
    assert len(read_source("chatgpt", path)) == 2
    assert len(opened) == 1


def test_crc_error_is_a_fixed_archive_error(tmp_path):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("conversations.json", (SAMPLES / "chatgpt" / "conversations.json").read_bytes())
    payload = buffer.getvalue().replace(b"saffron", b"indigo!", 1)
    path = tmp_path / "TEST-corrupt.zip"
    path.write_bytes(payload)
    with pytest.raises(ContractError) as failure:
        read_source("chatgpt", path)
    assert failure.value.field == "import_archive"


@pytest.mark.parametrize(
    "content,expected",
    [
        ({"content_type": "code", "text": "TEST literal code"}, "TEST literal code"),
        (
            {
                "content_type": "multimodal_text",
                "parts": ["TEST caption", {"content_type": "audio_asset_pointer", "asset_pointer": "TEST-audio"}],
            },
            "TEST caption",
        ),
    ],
)
def test_chatgpt_known_code_and_asset_shapes_preserve_only_original_text(tmp_path, content, expected):
    document = json.loads((SAMPLES / "chatgpt" / "conversations.json").read_text())
    document[0]["mapping"]["m1"]["message"]["content"] = content
    path = tmp_path / "TEST-known.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert read_source("chatgpt", path)[0].content == expected


@pytest.mark.parametrize(
    "existing,revision,disposition", [("TEST previous", 2, "inserted"), ("TEST incoming", 1, "duplicate")]
)
def test_prefetched_absence_rechecks_a_concurrent_source_write(store, existing, revision, disposition):
    from scope_recall.core.memory_import import prefetch_new_sources

    app, ctx = store
    item = ImportRecord("TEST-concurrent", "TEST incoming", "assistant")
    options = dict(source="mem0", source_id="TEST-account", scope_id="TEST-scope")
    hint = prefetch_new_sources(app, ctx, (item,), **options)
    before = import_record(app, ctx, replace(item, content=existing), **options)
    receipt = import_record(app, ctx, item, _new_sources=hint, **options)
    assert receipt.disposition == disposition and receipt.event_refs[0].revision == revision
    assert app.source(ctx, before.event_refs[0].ref, 1).event["content"] == existing
    assert app.source(ctx, receipt.event_refs[0].ref, revision).event["content"] == item.content


def test_bulk_import_prefetch_reduces_reads_without_combining_write_leases(store, tmp_path, monkeypatch):
    from contextlib import contextmanager

    app, ctx = store
    path = tmp_path / "TEST-many-memories.json"
    path.write_text(
        json.dumps({"results": [{"id": f"TEST-{i}", "memory": f"TEST remember {i}"} for i in range(64)]}),
        encoding="utf-8",
    )
    plan = preview_import("mem0", path, source_id="TEST-account")
    counts = {"read": 0, "write": 0}
    for name in counts:
        original = getattr(type(app.storage), name)

        @contextmanager
        def observe(*args, _original=original, _name=name, **kwargs):
            counts[_name] += 1
            with _original(*args, **kwargs) as tx:
                yield tx

        monkeypatch.setattr(type(app.storage), name, observe)
    result = apply_import(app, ctx, plan, preview_id=plan["preview_id"], scope_id="TEST-scope")
    assert result["complete"] and result["counts"] == {"inserted": 64}
    assert counts["read"] <= 4 and counts["write"] >= 64


@pytest.mark.parametrize("source_kind", ["imported", "native"])
def test_segmented_memory_material_keeps_its_group_admission(store, source_kind):
    app, ctx = store
    text = "TEST remember " * 6000
    if source_kind == "imported":
        receipt = import_record(
            app,
            ctx,
            ImportRecord("TEST-large-material", text, "assistant"),
            source="mem0",
            source_id="TEST-account",
            scope_id="TEST-scope",
        )
    else:
        shown = replace(ctx, actor_origin="assistant_visible")
        event = source_event(
            source_event_key="native-memory:TEST-large-material",
            origin="assistant_visible",
            role="assistant",
            content=text,
        )
        receipt = app.record_event(shown, event, scope_id="TEST-scope")
    assert receipt.durability == "persisted"
    assert set(receipt.admission) == {"admission_source_only:import_requires_confirmation"}
    assert any(app.source(ctx, ref.ref, ref.revision).event.get("segment") for ref in receipt.event_refs)
