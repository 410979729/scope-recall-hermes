"""A withheld tool output's placeholder is kept but found by nothing (#206).

The capture filter leaves "Tool execution summary (terminal): tool=terminal; output_chars=377; exit_code=0;
output_preview=omitted" in place of an output it withheld.  Every word in it is the envelope's own.  Indexed, the
placeholders an imported store holds (212,773 of the shared store's 311,051 sources) pushed "tool", "status",
"patch", "true" and "0" past the common-term ceiling, and the lexical channel dropped those words from every
question that held them: 289 of the 1,772 messages the owner had sent.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import lexical_index
from scope_recall.core.events import lexical_terms, query_terms
from scope_recall.core.retrieval_storage import _discriminating_terms

from test_v11_recall_admission import _app, _capture, _item_ref, recall


def _placeholder(index: int) -> str:
    return (f"Tool execution summary (terminal): tool=terminal; output_chars={300 + index}; exit_code=0; "
            "status=true; patch=applied; output_preview=omitted")


def _postings(core, ref: str) -> int:
    with closing(sqlite3.connect(core.storage.path)) as conn:
        return conn.execute("""SELECT count(*) FROM lexical_postings p JOIN source_events e ON e.source_id=p.source_id
                               WHERE e.event_id=?""", (ref,)).fetchone()[0]


def _withheld(core, ctx, count: int) -> list:
    """Placeholders as an imported store holds them: indexed, as an earlier release, the 1109 upgrade or an import
    indexed them."""
    made = [_capture(core, ctx, f"TEST-withheld/{index}", _placeholder(index), origin="tool_observation")
            for index in range(count)]
    with closing(sqlite3.connect(core.storage.path)) as conn, conn:
        for source in made:
            (source_id,), = conn.execute("SELECT source_id FROM source_events WHERE event_id=?", (source.ref,))
            lexical_index.index_terms(conn, source_id, lexical_terms(source.event["content"]))
    return made


def test_a_placeholder_is_kept_and_not_indexed(tmp_path):
    core, ctx, _vectors = _app(tmp_path)
    placeholder = _capture(core, ctx, "TEST-withheld/0", _placeholder(0), origin="tool_observation")
    output = _capture(core, ctx, "TEST-output/0", "TEST 工具输出：patch 已应用，status 正常。", origin="tool_observation")
    assert core.source(ctx, placeholder.ref, 1) is not None, "the source stays"
    assert _postings(core, placeholder.ref) == 0
    assert _postings(core, output.ref) > 0, "a tool output keeps its words"
    with core.storage.read(ctx) as tx:
        assert tx.source_projection_status(placeholder.ref, 1)[0] == "ready", "no terms is all it should hold"
        assert tx.source_projection_status(output.ref, 1)[0] == "ready"


def test_indexing_a_placeholder_again_drops_what_an_older_release_gave_it(tmp_path):
    core, ctx, _vectors = _app(tmp_path)
    placeholder, = _withheld(core, ctx, 1)
    assert _postings(core, placeholder.ref) > 0
    with core.storage.write(ctx, remaining_seconds=10) as tx:
        tx.index_source(placeholder.ref, 1)
    assert _postings(core, placeholder.ref) == 0


def test_a_legacy_conversion_does_not_index_a_placeholder(tmp_path):
    from collections import Counter
    from types import SimpleNamespace

    from scope_recall.maintenance.legacy_conversion import _project_lexical_terms

    core, ctx, _vectors = _app(tmp_path)
    placeholder = _capture(core, ctx, "TEST-withheld/0", _placeholder(0), origin="tool_observation")
    output = _capture(core, ctx, "TEST-output/0", "TEST 工具输出：patch 已应用。", origin="tool_observation")
    cv = SimpleNamespace(inserted=Counter(), sources=[
        {"event_id": source.ref, "role": "tool", "content": source.event["content"]} for source in (placeholder, output)])
    with closing(sqlite3.connect(core.storage.path)) as conn, conn:
        conn.execute("DELETE FROM lexical_postings")
        _project_lexical_terms(cv, conn)
    assert _postings(core, placeholder.ref) == 0 and _postings(core, output.ref) > 0
    assert cv.inserted["lexical_projection"] == len(lexical_terms(output.event["content"]))


def test_unindexing_goes_a_bounded_page_at_a_time_and_previews_first(tmp_path):
    core, ctx, _vectors = _app(tmp_path)
    placeholders = _withheld(core, ctx, 5)
    output = _capture(core, ctx, "TEST-output/0", "TEST 工具输出：patch 已应用。", origin="tool_observation")
    said = _capture(core, ctx, "TEST-said/0", "Tool execution summary 是什么意思？output omitted 又是什么？")
    # SQL's LIKE ignores case and takes this one too; it is no placeholder and keeps its words.
    near = _capture(core, ctx, "TEST-output/1", "tool execution summary of TEST-build: output omitted by its runner",
                    origin="tool_observation")
    assert _postings(core, near.ref) > 0
    held = sum(_postings(core, source.ref) for source in placeholders)

    preview = core.unindex_withheld_outputs(ctx, limit=10)
    assert (preview["dry_run"], preview["sources"], preview["postings"], preview["more"]) == (True, 5, held, False)
    assert sum(_postings(core, source.ref) for source in placeholders) == held, "a preview changes nothing"

    first = core.unindex_withheld_outputs(ctx, limit=2, dry_run=False)
    assert (first["dry_run"], first["sources"], first["more"]) == (False, 2, True)
    rest = core.unindex_withheld_outputs(ctx, after_id=first["next_after_id"], limit=10, dry_run=False)
    assert (rest["sources"], rest["more"]) == (3, False)
    assert first["postings"] + rest["postings"] == held
    assert all(_postings(core, source.ref) == 0 for source in placeholders)
    assert all(_postings(core, source.ref) > 0 for source in (output, said, near)), "only the placeholders lose words"
    again = core.unindex_withheld_outputs(ctx, limit=10, dry_run=False)
    assert (again["sources"], again["postings"], again["more"]) == (0, 0, False), "a second run finds nothing"
    for bad in ({"limit": 0}, {"limit": lexical_index.WITHHELD_PAGE_MAX + 1}, {"after_id": -1}):
        with pytest.raises(ContractError):
            core.unindex_withheld_outputs(ctx, **bad)


def test_unindexing_stays_inside_the_context_s_scopes(tmp_path):
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from test_v11_recall_admission import FixedClock
    from v11_support import source_event

    scopes = frozenset({"TEST-scope", "TEST-other"})
    binding = InstanceBinding("TEST-agent", "TEST-installation", tmp_path / "TEST-two", scopes, True)
    ctx = TrustedContext(binding, "TEST-session", scopes, "human_direct")
    core = MemoryCore(CoreConfig(binding), clock=FixedClock())
    core.initialize()
    tool = replace(ctx, actor_origin="tool_observation")
    refs = {}
    for scope in sorted(scopes):
        event = source_event(source_event_key=f"TEST-withheld/{scope}", origin="tool_observation", role="tool",
                             content=_placeholder(0))
        refs[scope] = core.record_event(tool, event, scope_id=scope, remaining_seconds=10).event_refs[0].ref
    with closing(sqlite3.connect(core.storage.path)) as conn, conn:
        for ref in refs.values():
            (source_id,), = conn.execute("SELECT source_id FROM source_events WHERE event_id=?", (ref,))
            lexical_index.index_terms(conn, source_id, lexical_terms(_placeholder(0)))
    page = core.unindex_withheld_outputs(replace(ctx, allowed_scope_ids=frozenset({"TEST-scope"})), limit=10,
                                         dry_run=False)
    assert page["sources"] == 1
    assert _postings(core, refs["TEST-scope"]) == 0 and _postings(core, refs["TEST-other"]) > 0


@pytest.mark.parametrize("until_done", [False, True])
def test_the_command_goes_on_page_after_page_only_when_asked(monkeypatch, capsys, until_done):
    import json
    from types import SimpleNamespace

    from scope_recall.maintenance import cli

    pages = iter([{"sources": 2, "postings": 20, "next_after_id": 7, "more": True},
                  {"sources": 1, "postings": 9, "next_after_id": 9, "more": False}])
    asked = []

    class Core:
        def unindex_withheld_outputs(self, context, **kwargs):
            asked.append(kwargs)
            return {"dry_run": kwargs["dry_run"], **next(pages)}

    config = SimpleNamespace(context=lambda: "TEST-context", request_seconds=5.0)
    monkeypatch.setattr(cli, "_run_core", lambda args, call, **_: cli._emit(call(Core(), config)) or 0)
    flags = ["--until-done"] if until_done else []
    assert cli.main(["unindex-withheld-outputs", "--config", "TEST-config.json", "--limit", "2", *flags, "--apply"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    if until_done:
        assert receipt == {"dry_run": False, "pages": 2, "sources": 3, "postings": 29, "next_after_id": 9, "more": False}
        assert [ask["after_id"] for ask in asked] == [0, 7]
    else:
        assert receipt == {"dry_run": False, "pages": 1, "sources": 2, "postings": 20, "next_after_id": 7, "more": True}
    assert all(ask["limit"] == 2 and ask["dry_run"] is False for ask in asked)


def test_the_envelope_s_words_reach_questions_again(tmp_path):
    """Eighty indexed placeholders among a hundred and eleven sources put "patch" past the ceiling (64 here), and a
    question naming it searched only its other words, which thirty other records share."""
    core, ctx, _vectors = _app(tmp_path)
    _withheld(core, ctx, 80)
    for index in range(30):
        _capture(core, ctx, f"TEST-log/{index}", f"TEST 部署记录第{index}条：一切正常。")
    wanted = _capture(core, ctx, "TEST-log/patch", "TEST 部署记录：patch 回退到上一版。")
    terms = query_terms("部署记录 patch")
    with core.storage.read(ctx) as tx:
        assert "patch" not in _discriminating_terms(tx, terms)
    core.unindex_withheld_outputs(ctx, limit=100, dry_run=False)
    with core.storage.read(ctx) as tx:
        assert "patch" in _discriminating_terms(tx, terms)
    reader = replace(ctx, session_id="TEST-reader")
    refs = [_item_ref(item) for item in recall(core, reader, query="部署记录 patch", mode="current").items]
    assert refs and refs[0] == wanted.ref, refs
