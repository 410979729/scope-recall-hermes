"""Bounded P08 native Lance + SQLite integration probe.

Run this with ``.execution/TEST-P08-native/Scripts/python.exe``.  The probe
uses a marked two-dimensional synthetic embedding and never calls an API.
Each run writes a small JSON receipt beneath ``TEST-P08-native-e2e``; the
caller should capture stdout and stderr separately as the command evidence.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
E2E_ROOT = ROOT / ".execution" / "TEST-P08-native-e2e"


class SyntheticQueryEmbedding:
    synthetic = True

    def embed_query(self, text: str, *, remaining_seconds: float):
        if not text or remaining_seconds <= 0:
            raise RuntimeError("synthetic query embedding called without budget")
        return (1.0, 0.0)


class ProbeClock:
    def utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def monotonic(self) -> float:
        return time.monotonic()


class SearchCallRecorder:
    def __init__(self, store) -> None:
        self._store = store
        self.calls: list[dict[str, object]] = []

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict]:
        self.calls.append({"scope_id": scope_id, "limit": limit, "vector": list(vector)})
        return self._store.search(vector, scope_id=scope_id, limit=limit)


def _event(key: str, revision: int, content: str, when: str) -> dict:
    return {
        "protocol_version": "1.1",
        "source_event_key": key,
        "source_revision": revision,
        "origin": "human_direct",
        "role": "user",
        "content": content,
        "occurred_at": when,
        "recorded_at": when,
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
        "dataset_id": "SYNTHETIC_TEST_ONLY",
    }


def _record(
    source,
    *,
    vector_id: str,
    space: str,
    project: str | None = "TEST-project",
    branch: str | None = "TEST-main",
    embedding: tuple[float, ...] = (1.0, 0.0),
):
    from scope_recall.adapters.lance import LanceVectorRecord

    return LanceVectorRecord(
        object_kind="event",
        object_ref=source.ref,
        object_revision=source.revision,
        vector_id=vector_id,
        embedding_space=space,
        embedding=embedding,
        scope_id=source.scope_id,
        agent_id="TEST-agent",
        installation_id="TEST-installation",
        project_id=project,
        branch_id=branch,
    )


def _expected_partitions(context, *, logical_scope_id: str, embedding_space: str) -> set[str]:
    from scope_recall.adapters.lance import physical_partition_scope_id

    combos = (
        (context.project_id, context.branch_id),
        (context.project_id, None),
        (None, context.branch_id),
        (None, None),
    )
    return {
        physical_partition_scope_id(
            agent_id=context.binding.agent_id,
            installation_id=context.binding.installation_id,
            embedding_space=embedding_space,
            logical_scope_id=logical_scope_id,
            project_id=project_id,
            branch_id=branch_id,
        )
        for project_id, branch_id in combos
    }


def main() -> int:
    from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorPort, physical_partition_scope_id
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID
    from scope_recall.core.retrieval import SearchContext
    from scope_recall.lance_process_store import ProcessLanceVectorStore

    run_dir = E2E_ROOT / f"run-{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    sqlite_dir = run_dir / "sqlite"
    lance_dir = run_dir / "lance"
    binding = InstanceBinding("TEST-agent", "TEST-installation", sqlite_dir, frozenset({"TEST-scope"}), True)
    context = TrustedContext(
        binding,
        "TEST-session",
        frozenset({"TEST-scope"}),
        "human_direct",
        project_id="TEST-project",
        branch_id="TEST-main",
    )
    clock = ProbeClock()
    core = MemoryCore(CoreConfig(binding), clock=clock)
    core.initialize()
    first = core.record_event(
        context,
        _event("TEST-P08/native", 1, "海上晨雾项目采用暮光方案。", "2026-09-01T12:00:00Z"),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    old = core.source(context, first.event_refs[0].ref, 1)
    second = core.record_event(
        context,
        _event("TEST-P08/native", 2, "海上晨雾项目采用晨星方案。", "2026-09-05T12:00:00Z"),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    current = core.source(context, second.event_refs[0].ref, 2)
    assert old is not None and current is not None

    store = ProcessLanceVectorStore(lance_dir, table_name="TEST_vectors", dimensions=2)
    store.open()
    try:
        writer = LanceIndexWriter(store)
        writer.upsert_records(
            (
                _record(old, vector_id="v-stale", space=SPACE_ID),
                _record(current, vector_id="v-good", space=SPACE_ID),
                _record(current, vector_id="v-old-space", space="wrong-space"),
                _record(current, vector_id="v-cross-project", space=SPACE_ID, project="OTHER-project"),
            )
        )
        recorder = SearchCallRecorder(store)
        port = LanceVectorPort(recorder, SyntheticQueryEmbedding())
        search_context = SearchContext.from_request(
            {
                "protocol_version": "1.1",
                "request_id": "TEST-native",
                "query": "完全没有共同词的语义问题",
                "mode": "current",
                "max_items": 6,
                "budget_tokens": 1200,
            },
            context,
            now=clock.utc_now(),
            deadline=clock.monotonic() + 5,
        )
        candidates = port.search(search_context, limit=10, remaining_seconds=5)
        print("NATIVE_CANDIDATES", [asdict(candidate) for candidate in candidates], file=sys.stderr)
        expected_partitions = _expected_partitions(context, logical_scope_id="TEST-scope", embedding_space=SPACE_ID)
        assert {call["scope_id"] for call in recorder.calls} == expected_partitions
        assert {candidate.vector_id for candidate in candidates} == {"v-stale", "v-good"}
        assert all(not hasattr(candidate, "content") for candidate in candidates)
        core.recall_pipeline.vector_port = port
        core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.8)
        before = core.status(context)
        result = core.recall(
            context,
            {
                "protocol_version": "1.1",
                "request_id": "TEST-native-recall",
                "query": "完全没有共同词的语义问题",
                "mode": "current",
                "max_items": 6,
                "budget_tokens": 1200,
            },
            deadline_seconds=5,
        )
        after = core.status(context)
        assert [item.content for item in result.items] == [current.event["content"]]
        assert result.items[0].revision == 2
        assert "vector_old_or_mismatched_space" not in result.gaps
        assert after.memory_epoch == before.memory_epoch
        assert after.pending_work == before.pending_work

        store.delete_by_ids(["v-stale", "v-good", "v-old-space", "v-cross-project"])
        starvation_write = core.record_event(
            context,
            _event("TEST-P08/starvation", 1, "松林团队采用霞光协议。", "2026-09-05T12:00:00Z"),
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        starvation_source = core.source(context, starvation_write.event_refs[0].ref, 1)
        assert starvation_source is not None
        writer.upsert_records(
            (
                _record(starvation_source, vector_id="v-star-cross-project", space=SPACE_ID, project="OTHER-project"),
                _record(starvation_source, vector_id="v-star-old-space", space="wrong-space"),
                _record(starvation_source, vector_id="v-star-valid", space=SPACE_ID, embedding=(0.8, 0.6)),
            )
        )
        raw_logical_hits = store.search([1.0, 0.0], scope_id="TEST-scope", limit=2)
        starvation_recorder = SearchCallRecorder(store)
        starvation_port = LanceVectorPort(starvation_recorder, SyntheticQueryEmbedding())
        starvation_context = replace(
            search_context,
            request_id="TEST-native-starvation",
            query="完全没有共同词的语义问题",
            limits=replace(search_context.limits, vector_limit=2, relation_hops=0, relation_objects=0),
            deadline=clock.monotonic() + 5,
        )
        core.recall_pipeline.vector_port = starvation_port
        core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.5)
        starvation_candidates = starvation_port.search(starvation_context, limit=2, remaining_seconds=5)
        starvation_result = core.recall_pipeline.search(starvation_context)
        valid_partition = physical_partition_scope_id(
            agent_id="TEST-agent",
            installation_id="TEST-installation",
            embedding_space=SPACE_ID,
            logical_scope_id="TEST-scope",
            project_id="TEST-project",
            branch_id="TEST-main",
        )
        assert raw_logical_hits == []
        assert valid_partition in {call["scope_id"] for call in starvation_recorder.calls}
        assert {candidate.vector_id for candidate in starvation_candidates} == {"v-star-valid"}
        assert [candidate.vector_id for candidate in starvation_result.candidates] == ["v-star-valid"]
        starvation_diagnostic = {
            "status": "STARVATION_FIXED",
            "requested_limit": 2,
            "raw_logical_scope_ids": [],
            "physical_search_calls": starvation_recorder.calls,
            "valid_lower_score_id": "v-star-valid",
            "adapter_candidates": [candidate.vector_id for candidate in starvation_candidates],
            "pipeline_items": [candidate.vector_id for candidate in starvation_result.candidates],
        }
        receipt = {
            "status": "PASS",
            "synthetic_embedding": True,
            "python": sys.version,
            "lancedb": __import__("importlib.metadata", fromlist=["version"]).version("lancedb"),
            "pyarrow": __import__("importlib.metadata", fromlist=["version"]).version("pyarrow"),
            "run_dir": str(run_dir),
            "candidate_vector_ids": [candidate.vector_id for candidate in candidates],
            "physical_partitions": sorted(expected_partitions),
            "hydrated": [{"ref": item.ref, "revision": item.revision, "content": item.content} for item in result.items],
            "gaps": list(result.gaps),
            "candidate_starvation": starvation_diagnostic,
            "native_elapsed_seconds": round(time.perf_counter() - _START, 6),
        }
    finally:
        store.close()
    receipt_path = run_dir / "probe-result.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


_START = time.perf_counter()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        failure = {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}
        E2E_ROOT.mkdir(parents=True, exist_ok=True)
        (E2E_ROOT / "last-failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        raise
