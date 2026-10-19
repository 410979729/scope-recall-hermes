"""The owner export streams only visible records and writes a verifiable package."""

import gc
import hashlib
import json
import tracemalloc
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance import memory, memory_export
from test_claims import Clock, accept, app, capture, draft, initial
from test_episodes import artifact
from v11_support import context

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def owner(core, context):
    return MemoryManagement(core, InstallationMaintenance(replace(context, project_id=None, branch_id=None)))


def export(api, out, filters=None, *, with_attachments=False):
    with api.export_reader(filters or MemoryFilter()) as reader:
        return memory_export.write_export(reader, out, with_attachments=with_attachments)


def files_of(out):
    out = Path(out)
    if out.suffix.lower() == ".zip":
        with zipfile.ZipFile(out) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    return {path.relative_to(out).as_posix(): path.read_bytes() for path in out.rglob("*") if path.is_file()}


def records_of(files, name):
    text = files[name].decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def schema(name):
    return json.loads((CONTRACTS / f"memory_export_{name}.schema.json").read_text(encoding="utf-8"))


def validated(files, filename, kind):
    validator = Draft202012Validator(schema(kind), format_checker=FormatChecker())
    values = records_of(files, filename)
    for value in values:
        errors = list(validator.iter_errors(value))
        assert not errors, (filename, errors)
    return values


def manifest_of(files):
    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    errors = list(Draft202012Validator(schema("manifest"), format_checker=FormatChecker()).iter_errors(manifest))
    assert not errors, errors
    artifacts = {entry["path"]: entry for entry in manifest["artifacts"]}
    assert "manifest.json" not in artifacts
    for path, entry in artifacts.items():
        assert hashlib.sha256(files[path]).hexdigest() == entry["sha256"]
        assert len(files[path]) == entry["bytes"]
    return manifest


def two_facts(core, ctx):
    first, first_source = initial(core, ctx, value="蓝色", when="2026-09-01T12:00:00Z")
    source = capture(
        core,
        replace(ctx, session_id="TEST-other"),
        "TEST-project 尺寸 大。",
        when="2026-11-02T12:00:00Z",
    )
    second = accept(core, replace(ctx, session_id="TEST-other"), draft(source, "大", predicate="尺寸")).items[0]
    return (first, first_source), (second, source)


def test_datasets_match_schema_and_manifest_and_keep_source_text(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    said = "TEST-export original line\n  with indentation and 蓝色。"
    extra = capture(core, context, said, when="2026-09-02T12:00:00Z")
    api = owner(core, context)
    api.revise(item.ref, expected_revision=1, value="绿色")
    out = tmp_path / "TEST-export"
    result = export(api, out)
    files = files_of(out)
    manifest = manifest_of(files)
    sources = validated(files, "sources.jsonl", "source")
    facts = validated(files, "facts.jsonl", "fact")
    history = validated(files, "fact_history.jsonl", "fact_history")
    episodes = validated(files, "episodes.jsonl", "episode")
    assert manifest["counts"] == {
        "sources": len(sources),
        "facts": len(facts),
        "fact_history": len(history),
        "episodes": len(episodes),
        "attachments": 0,
    }
    stored = core.source(context, extra.ref, 1).event["content"]
    assert next(row for row in sources if row["ref"] == extra.ref)["content"] == stored
    assert "evidence_spans" not in facts[0]["payload"]
    assert facts[0]["value"] == "绿色"
    assert history[0]["value"] == "蓝色"
    assert result["package_format"] == "scope-recall-export/1"
    session_text = b"".join(data for path, data in files.items() if path.startswith("readable/sessions/"))
    assert stored.encode("utf-8") in session_text


def test_fact_history_and_readable_facts_carry_their_provenance(app, tmp_path):
    core, context = app
    item, source = initial(core, context)
    api = owner(core, context)
    api.revise(item.ref, expected_revision=1, value="银色")
    out = tmp_path / "TEST-history"
    export(api, out)
    files = files_of(out)
    history = records_of(files, "fact_history.jsonl")
    assert len(history) == 1 and history[0]["revision"] == 1
    assert {entry["ref"] for entry in history[0]["sources"]} == {source.ref}
    text = files["readable/facts.md"].decode("utf-8")
    assert "Non-current version" in text and "银色" in text and source.ref in text


def test_deleted_memory_is_absent_from_every_export_file(app, tmp_path):
    core, context = app
    marker = "TEST-EXPORT-FORGOTTEN-紫色"
    item, _source = initial(core, context, value=marker)
    api = owner(core, context)
    api.apply(api.preview([item.ref]))
    out = tmp_path / "TEST-after-delete"
    export(api, out)
    files = files_of(out)
    assert all(marker.encode("utf-8") not in data for data in files.values())
    assert records_of(files, "sources.jsonl") == []
    assert records_of(files, "facts.jsonl") == []
    assert records_of(files, "fact_history.jsonl") == []
    assert records_of(files, "episodes.jsonl") == []


def test_muted_records_are_visible_to_the_owner_and_marked(app, tmp_path):
    core, context = app
    item, _source = initial(core, context)
    api = owner(core, context)
    api.apply(api.preview([item.ref], mode="suppress"))
    out = tmp_path / "TEST-muted"
    export(api, out)
    files = files_of(out)
    assert records_of(files, "sources.jsonl")[0]["suppressed"] is True
    assert records_of(files, "facts.jsonl")[0]["suppressed"] is True
    assert "蓝色" in files["readable/facts.md"].decode("utf-8")


def test_session_and_time_filters_keep_whole_facts_together(app, tmp_path):
    core, context = app
    (first, first_source), (second, second_source) = two_facts(core, context)
    api = owner(core, context)
    session_dir = tmp_path / "TEST-session"
    export(api, session_dir, MemoryFilter(session="TEST-session"))
    session_files = files_of(session_dir)
    assert {row["ref"] for row in records_of(session_files, "sources.jsonl")} == {first_source.ref}
    assert {row["ref"] for row in records_of(session_files, "facts.jsonl")} == {first.ref}
    assert all(second.ref not in data.decode("utf-8", "ignore") for data in session_files.values())
    late_dir = tmp_path / "TEST-late"
    export(api, late_dir, MemoryFilter(since="2026-10-01T00:00:00Z"))
    late_files = files_of(late_dir)
    assert {row["ref"] for row in records_of(late_files, "sources.jsonl")} == {second_source.ref}
    assert {row["ref"] for row in records_of(late_files, "facts.jsonl")} == {second.ref}


def test_kind_agent_and_scope_filters_and_invalid_kind(app, tmp_path):
    core, context = app
    (first, first_source), (second, second_source) = two_facts(core, context)
    api = owner(core, context)
    events_dir = tmp_path / "TEST-events"
    export(api, events_dir, MemoryFilter(kind="event"))
    events = files_of(events_dir)
    assert {row["ref"] for row in records_of(events, "sources.jsonl")} == {first_source.ref, second_source.ref}
    assert records_of(events, "facts.jsonl") == [] and records_of(events, "episodes.jsonl") == []
    claims_dir = tmp_path / "TEST-claims-export"
    export(api, claims_dir, MemoryFilter(kind="claim"))
    claims = files_of(claims_dir)
    assert {row["ref"] for row in records_of(claims, "facts.jsonl")} == {first.ref, second.ref}
    assert records_of(claims, "episodes.jsonl") == []
    nobody = tmp_path / "TEST-agent-none"
    export(api, nobody, MemoryFilter(agent="agent2"))
    assert records_of(files_of(nobody), "sources.jsonl") == []
    other_scope = tmp_path / "TEST-scope-none"
    export(api, other_scope, MemoryFilter(scope="TEST-other-scope"))
    assert records_of(files_of(other_scope), "sources.jsonl") == []
    with pytest.raises(ContractError) as error:
        export(api, tmp_path / "TEST-invalid-kind", MemoryFilter(kind="artifact"))
    assert error.value.code == "INPUT_INVALID" and not (tmp_path / "TEST-invalid-kind").exists()


def test_folder_and_zip_are_equivalent_with_accurate_hashes(app, tmp_path):
    core, context = app
    initial(core, context)
    artifact(core, context, tmp_path)
    api = owner(core, context)
    folder = tmp_path / "TEST-equivalent"
    archive = tmp_path / "TEST-equivalent.zip"
    export(api, folder, with_attachments=True)
    export(api, archive, with_attachments=True)
    folder_files = files_of(folder)
    zip_files = files_of(archive)
    assert folder_files == zip_files
    manifest = manifest_of(folder_files)
    assert manifest["with_attachments"] is True
    assert manifest["attachments"][0]["path"].startswith("attachments/")
    attachment = manifest["attachments"][0]
    assert hashlib.sha256(zip_files[attachment["path"]]).hexdigest() == attachment["sha256"]


def test_attachment_option_reads_only_the_retained_artifact(app, tmp_path):
    core, context = app
    _item, _source, original = artifact(core, context, tmp_path)
    api = owner(core, context)
    without = tmp_path / "TEST-no-attachments"
    export(api, without)
    without_files = files_of(without)
    assert records_of(without_files, "facts.jsonl") == []
    assert not any(path.startswith("attachments/") for path in without_files)
    assert manifest_of(without_files)["counts"]["attachments"] == 0
    with_attachments = tmp_path / "TEST-attachments"
    export(api, with_attachments, with_attachments=True)
    with_files = files_of(with_attachments)
    manifest = manifest_of(with_files)
    assert manifest["counts"]["attachments"] == 1
    entry = manifest["attachments"][0]
    assert with_files[entry["path"]] == original.read_bytes()
    assert entry["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()


def test_an_existing_output_is_never_overwritten(app, tmp_path):
    core, context = app
    initial(core, context)
    api = owner(core, context)
    directory = tmp_path / "TEST-existing-dir"
    directory.mkdir()
    (directory / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ContractError) as error:
        export(api, directory)
    assert error.value.code == "OUTPUT_EXISTS"
    assert (directory / "keep.txt").read_text(encoding="utf-8") == "keep"
    archive = tmp_path / "TEST-existing.zip"
    archive.write_bytes(b"keep")
    with pytest.raises(ContractError) as error:
        export(api, archive)
    assert error.value.code == "OUTPUT_EXISTS"
    assert archive.read_bytes() == b"keep"


def test_deletion_during_export_refuses_publication(app):
    core, context = app
    item, _source = initial(core, context)
    api = owner(core, context)
    with api.export_reader(MemoryFilter()) as reader:
        api.apply(api.preview([item.ref]))
        with pytest.raises(ContractError) as error:
            reader.verify()
    assert error.value.code == "VERSION_CONFLICT"


@pytest.mark.parametrize("suffix", ["", ".zip"])
def test_cancelled_export_removes_its_partial_bodies(app, tmp_path, monkeypatch, suffix):
    core, context = app
    item, _source = initial(core, context)
    api = owner(core, context)
    original = memory_export._write_index

    def delete_before_publication(*args):
        original(*args)
        api.apply(api.preview([item.ref]))

    monkeypatch.setattr(memory_export, "_write_index", delete_before_publication)
    out = tmp_path / ("TEST-cancelled" + suffix)
    with pytest.raises(ContractError) as error:
        export(api, out)
    assert error.value.code == "VERSION_CONFLICT"
    assert not out.exists()


def measured_export(core, context, out):
    gc.collect()
    tracemalloc.start()
    try:
        export(owner(core, context), out)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def sized_store(tmp_path, name, count):
    ctx = replace(context(tmp_path / name), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    for index in range(count):
        capture(
            core,
            ctx,
            f"TEST-export message {index:04d} " + "x" * 2048,
            key=f"TEST-{name}/{index}",
            when="2026-09-01T12:00:00Z",
        )
    return core, ctx


def test_export_memory_does_not_grow_linearly_with_store_size(tmp_path):
    small, small_context = sized_store(tmp_path, "TEST-small", 100)
    large, large_context = sized_store(tmp_path, "TEST-large", 1000)
    small_peak = measured_export(small, small_context, tmp_path / "TEST-small-export")
    large_peak = measured_export(large, large_context, tmp_path / "TEST-large-export")
    small_counts = json.loads((tmp_path / "TEST-small-export" / "manifest.json").read_text(encoding="utf-8"))
    large_counts = json.loads((tmp_path / "TEST-large-export" / "manifest.json").read_text(encoding="utf-8"))
    assert small_counts["counts"]["sources"] == 100 and large_counts["counts"]["sources"] == 1000
    print(
        json.dumps(
            {
                "small_records": 100,
                "large_records": 1000,
                "small_peak_bytes": small_peak,
                "large_peak_bytes": large_peak,
            }
        )
    )
    assert large_peak <= small_peak * 3


def test_cli_export_returns_the_management_envelope(app, tmp_path, monkeypatch, capsys):
    core, context = app
    initial(core, context)
    api = owner(core, context)
    monkeypatch.setattr(memory, "open_management", lambda args: api)
    out = tmp_path / "TEST-cli-export"
    code = memory.main(["export", "--out", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "ok" and payload["action"] == "export"
    management_schema = json.loads((CONTRACTS / "memory_management.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(management_schema).iter_errors(payload))
    assert payload["data"]["counts"]["sources"] == 1
    assert (out / "manifest.json").is_file()


def test_late_history_does_not_replace_the_exported_current_fact(app, tmp_path):
    core, ctx = app
    current, _ = initial(core, ctx, value="银色", when="2026-09-03T12:00:00Z")
    old = capture(
        core, ctx, "TEST-project 配色 蓝色。", origin="imported", source_original_origin="human_direct", attested=True
    )
    inserted = accept(core, ctx, draft(old)).items[0]
    assert inserted.revision == 2 and core.current_claim(ctx, current.ref).revision == 1
    out = tmp_path / "TEST-head-export"
    export(owner(core, ctx), out)
    files = files_of(out)
    assert [(row["revision"], row["value"]) for row in records_of(files, "facts.jsonl")] == [(1, "银色")]
    assert [(row["revision"], row["value"]) for row in records_of(files, "fact_history.jsonl")] == [(2, "蓝色")]
    assert "Current — revision 1" in files["readable/facts.md"].decode("utf-8")


def test_long_messages_keep_segments_and_readable_order(app, tmp_path):
    core, ctx = app
    text = "TEST-START-" + "x" * 65536 + "TEST-END"
    capture(core, ctx, text, key="TEST-segmented-export")
    out = tmp_path / "TEST-segments-export"
    export(owner(core, ctx), out)
    files = files_of(out)
    sources = validated(files, "sources.jsonl", "source")
    assert len(sources) == 2
    assert {row["segment"]["group_key"] for row in sources} == {"TEST-segmented-export"}
    assert "".join(row["content"] for row in sorted(sources, key=lambda row: row["segment"]["index"])) == text
    for path, data in files.items():
        if path.startswith(("readable/sessions/", "readable/months/")):
            assert data.index(b"TEST-START-") < data.index(b"TEST-END")


def test_zip_footer_failure_still_removes_partial_bodies(tmp_path, monkeypatch):
    out = tmp_path / "TEST-footer-failure.zip"
    package = memory_export._create_package(out)
    with package.open("sources.jsonl") as stream:
        stream.write(b'{"content":"TEST-private-body"}\n')
    original = package.archive.close

    def fail_close():
        raise OSError("TEST disk full while writing footer")

    monkeypatch.setattr(package.archive, "close", fail_close)
    try:
        with pytest.raises(OSError, match="TEST disk full"):
            package.abort()
        assert not out.exists()
    finally:
        # Prevent ZipFile's finalizer retrying a deliberately broken writer.
        package.archive.fp = None
        monkeypatch.setattr(package.archive, "close", original)


def test_unknown_segment_total_keeps_its_gap_and_matches_schema(app, tmp_path):
    core, ctx = app
    capture(
        core,
        ctx,
        "TEST incomplete message",
        key="TEST-unknown/0",
        capture_state="partial",
        segment={"group_key": "TEST-unknown", "index": 0, "total": None, "truncated": False},
    )
    out = tmp_path / "TEST-unknown-total-export"
    export(owner(core, ctx), out)
    files = files_of(out)
    source = validated(files, "sources.jsonl", "source")[0]
    assert source["segment"]["total"] is None
    assert "source_segments_incomplete" in source["capture_gaps"]
    assert any(b"part 1/unknown" in data for name, data in files.items() if name.startswith("readable/sessions/"))


def history_store(tmp_path, name, count):
    """Seed a large synthetic history without timing unrelated mutation work."""
    core, ctx = sized_store(tmp_path, name, 0)
    core.test_sequence = iter(range(1, 3))
    item, _ = initial(core, ctx, value="x" * 2048)
    with core.storage.write(ctx) as tx:
        conn = tx._check()
        conn.execute(
            """WITH RECURSIVE numbers(n) AS (VALUES(2) UNION ALL SELECT n+1 FROM numbers WHERE n<?)
            INSERT INTO claim_versions
            SELECT v.claim_id,n,v.payload_json,v.state,v.basis,v.qualification_reason,
                   v.valid_from,v.valid_to,v.recorded_from,v.recorded_to,NULL,v.conflict_revisions_json
            FROM numbers CROSS JOIN claim_versions v WHERE v.claim_id=? AND v.revision=1""",
            (count, item.ref),
        )
        conn.execute(
            """INSERT INTO evidence_links
            SELECT e.object_kind,e.object_ref,v.revision,e.source_ref,e.source_revision,e.relation,e.quote,e.location
            FROM evidence_links e JOIN claim_versions v ON v.claim_id=e.object_ref
            WHERE e.object_ref=? AND e.object_revision=1 AND v.revision>1""",
            (item.ref,),
        )
    return core, ctx


def test_single_fact_history_has_bounded_export_memory(tmp_path):
    small, small_ctx = history_store(tmp_path, "TEST-history-small", 100)
    large, large_ctx = history_store(tmp_path, "TEST-history-large", 1000)
    small_peak = measured_export(small, small_ctx, tmp_path / "TEST-history-small-export")
    large_peak = measured_export(large, large_ctx, tmp_path / "TEST-history-large-export")
    for name, expected in (("small", 99), ("large", 999)):
        manifest = json.loads((tmp_path / f"TEST-history-{name}-export" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["counts"]["fact_history"] == expected
    print(
        json.dumps(
            {
                "small_versions": 100,
                "large_versions": 1000,
                "small_peak_bytes": small_peak,
                "large_peak_bytes": large_peak,
            }
        )
    )
    assert large_peak <= small_peak * 3
