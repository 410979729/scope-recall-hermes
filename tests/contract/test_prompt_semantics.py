"""Prompt composition and temporal-answer checks over packaged synthetic examples."""

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from scope_recall.core.language_resources import prompt_text

from tests.contract.test_model_request_roles import requests

ROOT = Path(__file__).resolve().parents[2]

FRAGMENT = (
    " 本批只展示原记录连续片段。不得把片段视为整条来源已处理，可提出本片段支持的 r"
    "esume/reference，由系统暂存并在全部片段处理后汇总。resume "
    "的 goal 可以逐字沿用 prior_fragment_goals 中先前片段"
    "提出的目标，其他新内容必须来自当前片段。不要声称整条来源已经处理完成。仅提取片段"
    "内有完整引文的 claim；边界可能截断句子，不得补猜省略的否定、条件或指代。"
)
REPAIR = (
    " The previous result failed validation. "
    "Repair the indicated schema/contract vio"
    "lation using only the authorized sources"
    "; return the complete JSON object. valid"
    "ation_error={feedback}"
)


def test_fragment_and_repair_preserve_the_complete_system_text(requests):
    base = requests["extraction"][0]["content"]
    feedback = json.dumps(
        {"code": "DERIVATION_INVALID", "field": "evidence_quote"}, sort_keys=True, separators=(",", ":")
    )
    assert requests["fragment"][0]["content"] == base + FRAGMENT
    assert requests["repair"][0]["content"] == base + REPAIR.format(feedback=feedback)
    assert requests["fragment_repair"][0]["content"] == base + FRAGMENT + REPAIR.format(feedback=feedback)


def test_support_and_merge_use_the_same_system_composition_in_evaluation(requests, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/language_eval"))
    evaluator = importlib.import_module("evaluate_prompts")
    for name in ("support", "merge"):
        example = json.loads((ROOT / f"prompts/{name}.examples.json").read_text(encoding="utf-8"))[0]
        assert evaluator.messages(name, example, None)[0]["content"] == requests[name][0]["content"]


@pytest.mark.parametrize("field", ["valid_from", "valid_to", "occurred_at"])
def test_prompt_evaluation_rejects_wrong_temporal_answers(monkeypatch, field):
    monkeypatch.syspath_prepend(str(ROOT / "tests/language_eval"))
    evaluator = importlib.import_module("evaluate_prompts")
    example = json.loads((ROOT / "prompts/consolidation.examples.json").read_text(encoding="utf-8"))[0]
    wanted = deepcopy(example["expected"])
    wanted["claim_proposals"][0][field] = "2026-09-08T12:00:00Z" if field == "valid_to" else "2026-09-06T12:00:00Z"
    assert evaluator.valid_result("consolidation", json.dumps(wanted), wanted, example["input"])
    for wrong in ("2026-09-07T12:00:00Z", None):
        actual = deepcopy(wanted)
        actual["claim_proposals"][0][field] = wrong
        assert not evaluator.valid_result("consolidation", json.dumps(actual), wanted, example["input"])
    if field == "occurred_at":
        actual = deepcopy(wanted)
        del actual["claim_proposals"][0][field]
        assert not evaluator.valid_result("consolidation", json.dumps(actual), wanted, example["input"])
