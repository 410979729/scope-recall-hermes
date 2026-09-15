from dataclasses import replace

import pytest

from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import RecallPolicy
from tests.contract.test_v11_recall_admission import _capture
from tests.v11_support import context, recall_request


class Clock:
    value = 100.0

    def utc_now(self):
        return "2026-09-06T12:00:00Z"

    def monotonic(self):
        return self.value


class TimeoutVector:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def search(self, context, *, limit, remaining_seconds):
        self.calls.append((context.deadline, remaining_seconds))
        self.clock.value += remaining_seconds
        raise TimeoutError("synthetic embedding request used its entire allowance")


@pytest.mark.parametrize("seconds", (0.1, 1.5, 4.0))
def test_timed_out_vector_preserves_authorized_lexical_packet(tmp_path, seconds):
    clock = Clock()
    vectors = TimeoutVector(clock)
    ctx = context(tmp_path / "TEST-vector-timeout")
    ctx = replace(ctx, binding=replace(ctx.binding, scope_ids=frozenset({"TEST-scope", "TEST-other"})))
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock, vectors=vectors,
                      retrieval_policy=RecallPolicy(vector_threshold=0.8))
    core.initialize()
    source = _capture(core, ctx, "TEST/vector-timeout/fact", "TEST测试灯塔的颜色是青绿色。")
    other_scope = replace(ctx, allowed_scope_ids=frozenset({"TEST-other"}))
    packet = core.recall_packet(ctx, recall_request(query="TEST测试灯塔是什么颜色", mode="auto"), deadline_seconds=seconds)
    assert source.ref in {item["ref"] for item in packet["items"]}
    assert "vector_unavailable" in packet["gaps"]
    assert "vector_error:TimeoutError" in packet["gaps"]
    assert vectors.calls and clock.value < 100 + seconds
    assert all(deadline <= 100 + seconds for deadline, _ in vectors.calls)
    assert core.status(ctx).sources == 1
    # A failed optional channel does not grant a wider source audience.
    excluded = core.recall_packet(other_scope, recall_request(query="TEST测试灯塔是什么颜色", mode="auto"), deadline_seconds=seconds)
    assert source.ref not in {item["ref"] for item in excluded["items"]}
