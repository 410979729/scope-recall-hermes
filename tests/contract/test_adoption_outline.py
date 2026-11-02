"""Bounded outline locators cannot lend omitted or shortened text to evidence."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.adoption import adoption_messages, decode_selection, fragments
from scope_recall.core.worker_adoption import prepare_adoptions
from scope_recall.core.worker_consolidation import build_consolidation_model

from tests.contract.test_adoption import assistant, selected
from tests.contract.test_adoption_revision import Port, reply
from tests.contract.test_claims import app, capture


def owner(text="采用方案二"):
    return SimpleNamespace(event={"content": text})


def test_moderately_long_reply_always_uses_outline():
    content = "方案一：覆盖。\n" + "a" * 800 + "\n\n方案二：先备份。\n" + "b" * 800
    body = json.loads(adoption_messages(owner(), fragments(content))[-1]["content"])
    assert body["outline"] is True
    assert body["fragments"][1]["text"] == "方案二：先备份。"
    assert body["fragments"][1]["full_length"] > 800
    assert "b" * 100 not in json.dumps(body)


def test_locator_material_has_hard_limit_and_reports_ordered_omissions():
    parts = fragments("\n\n".join(f"方案{i}：" + "说明" * 25 for i in range(1, 61)))
    body = json.loads(adoption_messages(owner("采用方案60"), parts)[-1]["content"])
    material = {key: body[key] for key in ("outline", "fragments", "omitted_fragments")}
    assert len(json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 1200
    assert body["omitted_fragments"] > 0
    assert [p["id"] for p in body["fragments"]] == list(range(1, len(body["fragments"]) + 1))
    assert len(body["fragments"]) + body["omitted_fragments"] == len(parts)


def test_unresolved_outline_is_a_processing_error():
    with pytest.raises(ContractError, match="adoption_locator_incomplete"):
        decode_selection('{"unresolved":true}', fragments("方案二：先备份。"))


def test_outline_expands_selected_original_for_decision_and_support(app):
    core, ctx = app
    content = "方案一：覆盖。\n" + "a" * 800 + "\n\n方案二：先备份。\n" + "b" * 800 + "TAIL"
    plan = assistant(core, ctx, content)
    confirmation = capture(core, ctx, "采用方案二")
    calls = []
    base = reply()

    def judge(messages, **kwargs):
        body = json.loads(messages[-1]["content"])
        calls.append(body)
        if body.get("task") == "locate_adoption":
            assert "TAIL" not in json.dumps(body)
        return base(messages, **kwargs)

    item = prepare_adoptions(core.storage, ctx, (confirmation,), judge, clock=core.clock, remaining_seconds=10)[0]
    assert calls[1]["adopted_fragments"][0]["text"].endswith("TAIL")
    assert "TAIL" in json.dumps(calls[2])
    span = item.proposal["evidence_spans"][1]
    assert span["quote"] == plan.event["content"][span["start"] : span["end"]]
    assert span["quote"].endswith("TAIL")


@pytest.mark.parametrize("unresolved", [False, True])
def test_omitted_named_option_is_work_error_without_review_or_decision(app, unresolved):
    core, ctx = app
    text = "\n\n".join(f"方案{i}：只备份配置。" for i in range(1, 61))
    assistant(core, ctx, text)
    confirmation = capture(core, ctx, "采用方案60")

    class OmittedPort(Port):
        def propose(self, messages, **kwargs):
            body = json.loads(messages[-1]["content"])
            if "task" not in body and "proposal" not in body:
                return json.dumps(
                    dict(
                        protocol_version="1.1",
                        source_refs=body["source_refs"],
                        claim_proposals=[],
                        resume_proposals=[],
                        reference_proposals=[],
                    )
                )
            if body.get("task") == "locate_adoption":
                assert body["outline"] is True and body["omitted_fragments"] > 0
                assert 60 not in {part["id"] for part in body["fragments"]}
                return json.dumps(
                    dict(unresolved=True)
                    if unresolved
                    else selected() | dict(fragment_ids=[60], reference_label="方案60"),
                    ensure_ascii=False,
                )
            return super().propose(messages, **kwargs)

    core.drain_worker(
        ctx, consolidation=build_consolidation_model(OmittedPort(confirmation)), max_items=12, remaining_seconds=20
    )
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("SELECT count(*) FROM adoption_relations").fetchone()[0] == 0
        assert tx._check().execute("SELECT count(*) FROM claims").fetchone()[0] == 0
        assert (
            tx._check()
            .execute("SELECT w.state FROM work_items w JOIN adoption_processing a ON a.work_id=w.work_id")
            .fetchone()[0]
            == "pending"
        )
        assert tx._check().execute("SELECT 1 FROM work_error_details WHERE stage='adoption_locate'").fetchone()
        assert (
            json.loads(tx._check().execute("SELECT payload_json FROM adoption_processing").fetchone()[0])["stage"]
            == "locate"
        )


def test_short_reply_keeps_exact_full_fragments():
    parts = fragments("方案一：覆盖。\n\n方案二：先备份。")
    body = json.loads(adoption_messages(owner(), parts)[-1]["content"])
    assert body["outline"] is False
    assert body["fragments"] == list(parts)
    assert body["omitted_fragments"] == 0


def test_outline_first_line_is_bounded_in_codepoints():
    text = "方案二：" + "说明" * 200 + "\n" + "x" * 1200
    body = json.loads(adoption_messages(owner(), fragments(text))[-1]["content"])
    entry = body["fragments"][0]
    assert body["outline"] is True
    assert entry["text"] == text.partition("\n")[0][:120]
    assert entry["full_length"] == len(text)


def test_long_corpus_retains_original_confirmations_and_expected_selections():
    root = Path(__file__).resolve().parents[2]
    short = json.loads((root / "fixtures/adoption/cases.json").read_text("utf-8"))
    long = json.loads((root / "fixtures/adoption/cases-long.json").read_text("utf-8"))
    assert len(short) == len(long) == 24
    for original, expanded in zip(short, long, strict=True):
        assert {k: v for k, v in original.items() if k != "assistant"} == {
            k: v for k, v in expanded.items() if k != "assistant"
        }
        parts = fragments(expanded["assistant"])
        assert len(parts) == 2 and all(1200 < len(p["text"]) < 4096 for p in parts)
        body = json.loads(adoption_messages(owner(expanded["confirmation"]), parts)[-1]["content"])
        assert body["outline"] is True and body["omitted_fragments"] == 0
