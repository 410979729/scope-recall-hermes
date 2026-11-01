"""Installed-subject interface smoke and common acceptance checks with no network access."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import compare
from evaluate import Clock, PromptPort, RecordingPort, run_case, summarize
from scope_recall.contracts import TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.candidate_lifecycle import CandidateSnapshot
from scope_recall.core.claims import Qualification
from scope_recall.core.consolidate import candidate_evaluation_messages
from subject import ChatPort, capabilities, digest, one_system, package_identity
from v11_support import context, source_event


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Keep every synthetic store inside the caller's explicit test boundary."""
        cls.directory = Path(tempfile.mkdtemp(prefix="TEST-language-", dir=os.environ["R2_EVAL_TEST_ROOT"]))

    def test_candidate_round_trip_uses_the_installed_native_interface(self):
        clock = Clock()
        ctx = replace(
            context(self.directory / "candidate"),
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"),
        )
        core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
        core.initialize()
        event = source_event(content="I prefer blue.")
        saved = core.record_event(ctx, event, scope_id="TEST-scope", remaining_seconds=20)
        source = core.source(ctx, saved.event_refs[0].ref, 1)
        proposal = dict(
            kind="preference",
            subject="principal:TEST-owner",
            predicate="color",
            value_text="blue",
            conditions=[],
            statement_kind="assertion",
            valid_from=None,
            valid_to=None,
            evidence_spans=[dict(source_ref=source.ref, source_revision=1, quote=event["content"])],
        )
        with core.storage.write(ctx, remaining_seconds=20) as tx:
            candidate = tx.claims.append(
                "TEST-scope",
                proposal,
                Qualification("proposed", "inferred_suggestion", "synthetic"),
                recorded_at=clock.utc_now(),
            )
            tx.candidates.register(candidate.ref, 1, observed_at=clock.utc_now())
            tx._check(write=True).execute(
                "UPDATE work_items SET state='done' WHERE work_type IN ('consolidate','embed')"
            )
        recording = dict(
            kind="preference",
            subject="user",
            predicate="color",
            value_text="blue",
            conditions=[],
            statement_kind="assertion",
            support="supported",
        )
        port = PromptPort(RecordingPort(recording), None)
        from scope_recall.core.worker import WorkerConfig, drain_worker
        from scope_recall.core.worker_consolidation import build_consolidation_model

        drain_worker(
            core.storage,
            clock,
            ctx,
            config=WorkerConfig("TEST-candidate", max_items=1),
            consolidation=build_consolidation_model(port),
            remaining_seconds=20,
        )
        self.assertEqual(port.calls["candidate"], 1)
        self.assertEqual(core.current_claim(ctx, candidate.ref).state, "active")
        self.assertTrue(all(bool(s) for s in port.prompts.values()))

    def test_native_claim_merge_keeps_an_identical_replay_in_one_slot(self):
        clock = Clock()
        ctx = replace(
            context(self.directory / "merge"),
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:TEST-owner"),
        )
        core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
        core.initialize()
        event = source_event(content="I prefer blue.")
        saved = core.record_event(ctx, event, scope_id="TEST-scope", remaining_seconds=20)
        source = core.source(ctx, saved.event_refs[0].ref, 1)
        port = RecordingPort(
            dict(
                kind="preference",
                subject="user",
                predicate="color",
                value_text="blue",
                conditions=[],
                statement_kind="assertion",
                support="supported",
            )
        )
        from scope_recall.core.consolidate import consolidation_messages

        value = json.loads(port.propose(consolidation_messages((source,))))
        first = core.records.accept_claim_proposals(ctx, value, scope_id="TEST-scope", remaining_seconds=20)
        second = core.records.accept_claim_proposals(ctx, value, scope_id="TEST-scope", remaining_seconds=20)
        self.assertEqual(first.items[0].state, "active")
        self.assertEqual(second.items[0].disposition, "duplicate")
        self.assertEqual(first.items[0].ref, second.items[0].ref)
        self.assertEqual(second.items[0].revision, 1)

    def test_recording_matches_only_the_installed_evidence_contract(self):
        source = dict(source_ref="TEST-source", source_revision=1, content="I prefer blue.", occurred_at=None)
        body = dict(sources=[source], source_refs=["TEST-source@1"])
        port = RecordingPort(
            dict(
                kind="preference",
                subject="user",
                predicate="color",
                value_text="blue",
                conditions=[],
                statement_kind="assertion",
                support="supported",
            )
        )
        result = json.loads(
            port.propose([dict(role="system", content="TEST"), dict(role="user", content=json.dumps(body))])
        )
        span = result["claim_proposals"][0]["evidence_spans"][0]
        self.assertEqual("start" in span, capabilities()["evidence_positions"] == "applicable")
        self.assertIn("site-packages", package_identity()["location"])

    def test_protocol_conversion_preserves_text_and_rejects_late_system_messages(self):
        messages = [
            dict(role="system", content="first"),
            dict(role="system", content="second"),
            dict(role="user", content="body"),
        ]
        self.assertEqual(one_system(messages), [dict(role="system", content="first\n\nsecond"), messages[-1]])
        with self.assertRaises(ValueError):
            one_system([messages[0], messages[-1], messages[1]])

    def test_unkeyed_chat_transport_and_http_stop(self):
        calls = []

        class Transport:
            def post(self, url, **kwargs):
                calls.append(kwargs)
                return 200, json.dumps(
                    dict(choices=[dict(finish_reason="stop", message=dict(content="TEST"))])
                ).encode()

            def close(self):
                pass

        route = dict(
            weights="TEST-fixed",
            chat=dict(
                model="TEST-model",
                endpoint="http://127.0.0.1:12345/v1/chat/completions",
                credential_env="TEST_MISSING_KEY",
                output_limit_field="max_tokens",
                max_output_tokens=128,
            ),
        )
        port = ChatPort(route, [route["chat"]["endpoint"]], transport=Transport())
        self.assertEqual(port.propose([dict(role="system", content="TEST")], remaining_seconds=1), "TEST")
        self.assertNotIn("Authorization", calls[0]["headers"])
        self.assertEqual(json.loads(calls[0]["body"])["max_tokens"], 128)

        class Failure(Transport):
            def post(self, *args, **kwargs):
                return 400, b"TEST"

        port = ChatPort(route, [route["chat"]["endpoint"]], transport=Failure())
        with self.assertRaises(RuntimeError):
            port.propose([dict(role="system", content="TEST")], remaining_seconds=1)
        self.assertTrue(port.stopped)

        class Malformed(Transport):
            def post(self, *args, **kwargs):
                return 200, b"not JSON"

        port = ChatPort(route, [route["chat"]["endpoint"]], transport=Malformed())
        with self.assertRaises(RuntimeError):
            port.propose([dict(role="system", content="TEST")], remaining_seconds=1)
        self.assertTrue(port.stopped)

    def test_comparison_checks_all_four_gates_and_comparability(self):
        rows = [
            dict(
                id="positive",
                gold_active=True,
                active=True,
                promoted_within_1200=True,
                validation_failed=False,
                candidate_count=0,
                candidates_with_reasons=0,
                states=[dict(state="active", reason="supported", at="2026-09-06T12:00:10Z", current=True)],
                delay_seconds=10.0,
            ),
            dict(
                id="negative",
                gold_active=False,
                active=False,
                promoted_within_1200=False,
                validation_failed=False,
                candidate_count=1,
                candidates_with_reasons=1,
                states=[dict(state="proposed", reason="unsupported", at="2026-09-06T12:00:10Z", current=True)],
                delay_seconds=None,
            ),
        ]
        for row in rows:
            row.update(
                discarded_proposals=0,
                receipts=[],
                model_calls={},
                prompt_sha256={},
                operational_errors=[],
                requests_with_clause_removed=0,
            )
        report = dict(
            schema="TEST",
            mode="fake",
            corpus_sha256="same",
            case_ids_sha256=digest([row["id"] for row in rows]),
            model_config_sha256="same",
            protocol_sha256="same",
            evaluator_sha256="same",
            complete=True,
            subject={"version": "TEST"},
            prompt_sha256={"extraction": ["TEST"]},
            variants=[dict(clause=None, rows=rows, metrics=summarize(rows))],
        )
        old = copy.deepcopy(report)
        old["variants"][0]["rows"][0]["validation_failed"] = True
        old["variants"][0]["rows"][0]["receipts"] = [
            dict(type="consolidate", disposition="retry", error="INPUT_INVALID")
        ]
        old["variants"][0]["metrics"] = summarize(old["variants"][0]["rows"])
        self.assertTrue(compare(old, report)["accepted"])
        for key in ("corpus_sha256", "case_ids_sha256", "model_config_sha256", "protocol_sha256", "evaluator_sha256"):
            changed = copy.deepcopy(report)
            changed[key] = "different"
            with self.assertRaises(ValueError):
                compare(old, changed)
        changed = copy.deepcopy(report)
        changed["complete"] = False
        with self.assertRaises(ValueError):
            compare(old, changed)
        changed = copy.deepcopy(report)
        changed["variants"][0]["metrics"]["candidate_reason_coverage"] = 0
        with self.assertRaises(ValueError):
            compare(old, changed)
        changed = copy.deepcopy(report)
        changed["variants"][0]["rows"][1]["states"][0]["reason"] = ""
        changed["variants"][0]["rows"][1]["candidates_with_reasons"] = 0
        changed["variants"][0]["metrics"] = summarize(changed["variants"][0]["rows"])
        self.assertFalse(compare(old, changed)["acceptance"]["all_candidates_have_reasons"])
        self.assertFalse(compare(report, report)["acceptance"]["validation_improves"])


if __name__ == "__main__":

    def offline(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("network denied in interface smoke")

    sys.addaudithook(offline)
    unittest.main()
