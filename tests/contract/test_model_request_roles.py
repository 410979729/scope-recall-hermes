"""Model request roles for production formatters and offline evaluation entry points."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.core.consolidate import candidate_evaluation_messages, consolidation_messages
from scope_recall.core.consolidation_chunks import ChunkedSource, ConsolidationChunk
from scope_recall.core.language_resources import prompt_text
from scope_recall.core.semantic_proposals import support_messages

from tests.contract.test_candidate_evidence_window import _candidate, _source, _span

ROOT = Path(__file__).resolve().parents[2]
FEEDBACK = {"code": "DERIVATION_INVALID", "field": "evidence_quote"}


def _assert_roles(messages):
    roles = [message["role"] for message in messages]
    assert roles[0] == "system"
    assert roles.count("system") == 1
    assert all(role == "user" for role in roles[1:])


@pytest.fixture
def requests():
    source = _source("TEST-build result ready")
    span = dict(_span(source, source.event["content"]), start=0, end=len(source.event["content"]))
    candidate = _candidate(source, [span])
    proposal = candidate.payload
    page = ChunkedSource(
        **source.__dict__,
        consolidation_window=ConsolidationChunk(0, len(source.event["content"]), len(source.event["content"]) + 20),
    )
    return {
        "extraction": consolidation_messages((source,)),
        "candidate": candidate_evaluation_messages(candidate, (source,)),
        "candidate_repair": candidate_evaluation_messages(candidate, (source,), validation_feedback=FEEDBACK),
        "fragment": consolidation_messages((page,)),
        "repair": consolidation_messages((source,), validation_feedback=FEEDBACK),
        "fragment_repair": consolidation_messages((page,), validation_feedback=FEEDBACK),
        "support": support_messages(proposal, (source,)),
        "merge": support_messages(proposal, (source,), ({"ref": candidate.ref, "revision": 1, "payload": proposal},)),
    }


def test_all_production_requests_have_one_leading_system_message(requests):
    for messages in requests.values():
        _assert_roles(messages)
    for name in ("candidate", "candidate_repair"):
        assert requests[name][0]["content"].startswith(prompt_text("candidate") + "\n\n")
        assert [message["role"] for message in requests[name]] == ["system", "user", "user"]


def test_prompt_evaluation_requests_and_ablations_have_one_leading_system_message(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/language_eval"))
    formatter = importlib.import_module("evaluate_prompts")
    for name in formatter.NAMES:
        examples = json.loads((ROOT / f"prompts/{name}.examples.json").read_text(encoding="utf-8"))
        for example in examples:
            for clause in (None, *formatter.clauses(name)):
                _assert_roles(formatter.messages(name, example, clause))


def test_extraction_evaluation_preserves_request_roles(requests, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/language_eval"))
    evaluator = importlib.import_module("evaluate")

    class Capture:
        def propose(self, messages, **kwargs):
            _assert_roles(messages)
            return "TEST-recorded-answer"

    for messages in requests.values():
        port = evaluator.PromptPort(Capture(), None)
        assert port.propose(messages) == "TEST-recorded-answer"


def test_synthetic_benchmark_answer_and_judge_have_one_leading_system_message(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "tests/language_eval"))
    benchmark = importlib.import_module("benchmark")
    public_run = importlib.import_module("run")
    captured = []

    def complete(self, messages, *, operation):
        _assert_roles(messages)
        captured.append((operation, [message["role"] for message in messages]))
        return "yes" if operation == "judge" else "blue"

    monkeypatch.setattr(benchmark.Ports, "complete", complete)
    ports = object.__new__(benchmark.EvaluationPorts)
    ports.fake = True
    ports.cache = SimpleNamespace(raise_if_missing=lambda: None)
    turn = SimpleNamespace(
        key="TEST-turn",
        session="TEST-session",
        role="user",
        content="I prefer blue.",
        at="2026-09-01T12:00:00Z",
        answer=True,
    )
    case = SimpleNamespace(
        id="TEST-roles",
        category="preference",
        question="Which color?",
        gold="blue",
        at=turn.at,
        sessions=(turn.session,),
        turns=(turn,),
    )
    result = public_run.run_case(
        case,
        root=tmp_path,
        ports=ports,
        prompts={"default": benchmark.JUDGE},
        method="full-context",
        retrieval_only=False,
    )
    assert result["status"] == "complete"
    assert captured == [("answer", ["system", "user"]), ("judge", ["system", "user"])]
