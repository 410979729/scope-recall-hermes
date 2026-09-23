"""A provider that refused the calls just before is left alone until its hold ends.

Through a monthly spend cap Google refused every embedding for seven hours while
gamma, alpha and beta kept asking about 5,000 times a day: each pass stood
the refused work type down, and the next pass asked again at once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

import pytest

from scope_recall.adapters.models import AuxiliaryModelError
from scope_recall.runtime.auxiliary import build_auxiliary_runtime
from scope_recall.runtime.model_budget import (
    PROVIDER_HOLD_FIRST_SECONDS,
    PROVIDER_HOLD_LONGEST_SECONDS,
    REQUESTS_TABLE,
    provider_hold_until,
    provider_holds,
)
from tests.contract.test_finite_supervisor import NOW, fixture, queue
from tests.contract.test_runtime_auxiliary import FakeTransport, _runtime_config, _vector
from tests.contract.test_v11_claims import app, capture  # noqa: F401  (fixture)
from tests.contract.test_v11_worker import worker_app  # noqa: F401  (fixture)

REFUSED = "http_429_usage_unknown_reserved_charge_retained"


def _ledger(tmp_path, rows):
    """``rows`` are ``(model, status, seconds_ago)``, oldest first."""
    path = tmp_path / "auxiliary-budget.sqlite3"
    now = time.time()
    with sqlite3.connect(path) as conn:
        conn.execute(REQUESTS_TABLE)
        for model, status, ago in rows:
            conn.execute("INSERT INTO requests(model,status,started_ns) VALUES (?,?,?)",
                         (model, status, int((now - ago) * 1_000_000_000)))
    return path, now


def test_one_refusal_holds_the_model_a_minute(tmp_path):
    path, now = _ledger(tmp_path, [("m", "http_200", 100), ("m", REFUSED, 10)])
    assert provider_hold_until(path, "m", now=now) == pytest.approx(now - 10 + PROVIDER_HOLD_FIRST_SECONDS, abs=.01)


def test_each_refusal_in_a_row_doubles_the_hold_up_to_half_an_hour(tmp_path):
    three = tmp_path / "three"
    three.mkdir()
    path, now = _ledger(three, [("m", REFUSED, 30), ("m", REFUSED, 20), ("m", REFUSED, 10)])
    assert provider_hold_until(path, "m", now=now) == pytest.approx(now - 10 + 4 * PROVIDER_HOLD_FIRST_SECONDS, abs=.01)
    twenty = tmp_path / "twenty"
    twenty.mkdir()
    path, now = _ledger(twenty, [("m", REFUSED, 60 - n) for n in range(20)])
    assert provider_hold_until(path, "m", now=now) == pytest.approx(now - 41 + PROVIDER_HOLD_LONGEST_SECONDS, abs=.01)


def test_one_wave_of_concurrent_refusals_is_one_refusal(tmp_path):
    """A group's requests go out together, so all of them refused is one refusal, not several in a row.

    Counted one by one, a single wave of four refused requests held a pilot's embeddings for
    eight minutes (sixteen with the refusal before it) where one refusal earns one.
    """
    one = tmp_path / "one"
    one.mkdir()
    path, now = _ledger(one, [("m", REFUSED, 10.3), ("m", REFUSED, 10.2), ("m", REFUSED, 10.1), ("m", REFUSED, 10.0)])
    assert provider_hold_until(path, "m", now=now) == pytest.approx(now - 10 + PROVIDER_HOLD_FIRST_SECONDS, abs=.01)
    two = tmp_path / "two"
    two.mkdir()
    path, now = _ledger(two, [("m", REFUSED, 90.2), ("m", REFUSED, 90.1), ("m", REFUSED, 10.1), ("m", REFUSED, 10.0)])
    assert provider_hold_until(path, "m", now=now) == pytest.approx(now - 10 + 2 * PROVIDER_HOLD_FIRST_SECONDS, abs=.01)


def test_an_answer_ends_the_hold(tmp_path):
    path, now = _ledger(tmp_path, [("m", REFUSED, 30), ("m", REFUSED, 20), ("m", "http_200", 10)])
    assert provider_hold_until(path, "m", now=now) is None


def test_calls_that_got_no_answer_neither_extend_nor_end_a_hold(tmp_path):
    path, now = _ledger(tmp_path, [("m", REFUSED, 20), ("m", "network_error_usage_unknown_reserved_charge_retained", 15),
                                   ("m", "reserved_before_network", 5)])
    assert provider_hold_until(path, "m", now=now) == pytest.approx(now - 20 + PROVIDER_HOLD_FIRST_SECONDS, abs=.01)


def test_an_account_refusal_holds_too(tmp_path):
    path, now = _ledger(tmp_path, [("m", "http_402:unknown_error_usage_unknown_reserved_charge_retained", 5)])
    assert provider_hold_until(path, "m", now=now) is not None


def test_an_invalid_request_is_an_answer_not_a_refusal(tmp_path):
    path, now = _ledger(tmp_path, [("m", REFUSED, 20), ("m", "http_400_usage_unknown", 5)])
    assert provider_hold_until(path, "m", now=now) is None


def test_a_hold_that_has_run_out_is_no_hold(tmp_path):
    path, now = _ledger(tmp_path, [("m", REFUSED, 120)])
    assert provider_hold_until(path, "m", now=now) is None


def test_a_missing_ledger_or_model_holds_nothing(tmp_path):
    assert provider_hold_until(None, "m") is None
    assert provider_hold_until(tmp_path / "absent.sqlite3", "m") is None
    path, _now = _ledger(tmp_path, [("m", REFUSED, 1)])
    assert provider_hold_until(path, None) is None


def test_only_the_refusing_route_is_held(tmp_path, monkeypatch):
    config, ledger, _budget = _runtime_config(tmp_path)
    now = time.time()
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO requests(model,status,started_ns) VALUES (?,?,?)",
                     ("gemini-embedding-2", REFUSED, int((now - 5) * 1_000_000_000)))
        conn.execute("INSERT INTO requests(model,status,started_ns) VALUES (?,?,?)",
                     ("deepseek-v4-flash", "http_200", int((now - 4) * 1_000_000_000)))
    holds = provider_holds(config, now=now)
    assert set(holds) == {"embed"} and holds["embed"][0] == "gemini-embedding-2"


def test_a_held_model_is_not_asked_again(tmp_path, monkeypatch):
    config, _ledger_path, _budget = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (429, b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'))
    runtime = build_auxiliary_runtime(config, transport=transport)
    with pytest.raises(AuxiliaryModelError) as first:
        runtime.query_embedding.embed_query("hello", remaining_seconds=2.0)
    assert first.value.error_type == "http_status" and transport.calls == 1
    with pytest.raises(AuxiliaryModelError) as held:
        runtime.query_embedding.embed_query("hello again", remaining_seconds=2.0)
    assert held.value.error_type == "provider_hold" and transport.calls == 1, "nothing was sent"


def test_a_healthy_model_is_asked_as_before(tmp_path, monkeypatch):
    config, _ledger_path, _budget = _runtime_config(tmp_path)
    monkeypatch.setenv("SCOPE_RECALL_TEST_EMBED_KEY", "test-key")
    transport = FakeTransport(lambda **kwargs: (200, json.dumps(
        {"embeddings": [{"values": _vector()}], "usageMetadata": {"promptTokenCount": 3}}).encode()))
    runtime = build_auxiliary_runtime(config, transport=transport)
    for text in ("one", "two", "three"):
        runtime.query_embedding.embed_query(text, remaining_seconds=2.0)
    assert transport.calls == 3


def test_a_drain_does_not_claim_held_work(worker_app):
    from scope_recall.core.worker import WorkerConfig, drain_worker

    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 向量等待服务商恢复。")
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()

    class CountingEmbed:
        calls = 0

        def prepare_source(self, source, *, remaining_seconds=1.0):
            CountingEmbed.calls += 1
            return {"ref": source.ref, "revision": source.revision}

        def publish_source(self, prepared, *, source, lease_token, lease_owner, lease_guard, remaining_seconds=1.0):
            pass

    receipt = drain_worker(core.storage, clock, ctx, embed=CountingEmbed(), remaining_seconds=5,
                           config=WorkerConfig("TEST-held", held_work_types=frozenset({"embed"})))
    assert receipt.processed == 0 and CountingEmbed.calls == 0
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT state,attempt FROM work_items WHERE work_type='embed'").fetchone() == ("pending", 0)
    with pytest.raises(ValueError, match="held_work_types"):
        WorkerConfig("TEST-held", held_work_types=frozenset({"purge"}))


def test_the_planner_sleeps_held_work_until_its_hold_ends(tmp_path, monkeypatch):
    core, cfg, _path = fixture(tmp_path)
    queue(core, cfg, kind="rebuild_projection", due=NOW)
    ends = NOW + timedelta(minutes=8)
    monkeypatch.setattr("scope_recall.runtime.model_budget.provider_holds",
                        lambda auxiliary, *, now=None: {"rebuild_projection": ("TEST-model", ends.timestamp())})
    from scope_recall.runtime.scheduling import next_wake

    plan = next_wake(cfg, now=NOW)
    assert plan.due_at == ends.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    assert plan.reason == "capability_cooldown"
    monkeypatch.setattr("scope_recall.runtime.model_budget.provider_holds", lambda auxiliary, *, now=None: {})
    assert next_wake(cfg, now=NOW).due_at == "2026-09-12T00:00:00Z"
