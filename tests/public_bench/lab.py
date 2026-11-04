"""Isolated public-data store using the shipped capture, worker and recall paths."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "recall_bench"))
# Clock helpers leave the caller's application package selection intact.
from bench_clock import WorldClock
from cache import digest
from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext, TrustedSourcePrincipal
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import EMBEDDING_SPACE, SPACE_ID, RecallPolicy
from scope_recall.core.work_storage import CAPACITY_REFUSALS
from scope_recall.runtime.vector_port import VectorEmbedPort, VectorPort, VectorPurgePort
from scope_recall.runtime.vector_upkeep import index_if_due
from scope_recall.vector.store import build_vector_store

#: Real seconds an idle settle round waits when the remaining work was last refused for capacity.  A provider's
#: rate limit counts real time, so moving only the business clock would ask again at once and be refused again.
THROTTLE_WAIT_SECONDS = 30.0


def benchmark_policy(config: dict) -> RecallPolicy:
    """Bind the product floor to the benchmark's request digest for identical built-in geometry.

    Benchmark space ids retain request settings, including the credential's
    environment-variable name. Product ids describe geometry and encoding.
    Other models need an explicit floor; synthetic ports supply their own.
    """
    route = config["embedding"]
    builtin = (
        all(route.get(key, EMBEDDING_SPACE[key]) == EMBEDDING_SPACE[key] for key in ("model", "dimensions", "endpoint"))
        and route.get("dialect", "gemini") == "gemini"
    )
    space = digest(route)
    policy = RecallPolicy(
        vector_threshold=config.get("vector_threshold"), embedding_space_id=SPACE_ID if builtin else space
    )
    return replace(policy, embedding_space_id=space)


def throttled(state: dict) -> bool:
    """Whether pending or leased work last ended in a provider's capacity refusal (rate limit or overload)."""
    return any(
        status in {"pending", "leased"} and not CAPACITY_REFUSALS.isdisjoint(str(error or "").split("|"))
        for _, status, error, _ in state["work"]
    )


class Lab:
    """One question's independent store; construction never opens a host config."""

    def __init__(
        self, root: Path, ports, *, now: str, backend="sqlite", scopes=None, existing=False, installation_kind="local"
    ) -> None:
        if existing and not (root / "truth" / "memory.sqlite3").is_file():
            raise ValueError("existing synthetic store is missing")
        root.mkdir(parents=True, exist_ok=existing)
        self.clock, self.ports = WorldClock(now), ports
        self.clock.realtime = True
        self.binding = InstanceBinding(
            "public-bench",
            "shared-install:" + digest("TEST-public-bench-install")[:32]
            if installation_kind == "shared"
            else "public-bench-install",
            root / "truth",
            scopes or frozenset({"public"}),
            installation_kind=installation_kind,
        )
        self.context = TrustedContext(
            self.binding,
            "ingest",
            self.binding.scope_ids,
            "human_direct",
            source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:bench"),
        )
        space = digest(ports.config["embedding"])
        self.companion = build_vector_store(
            backend, storage_dir=root / "vectors", table_name="public_bench", dimensions=3072, metric="cosine"
        )
        self.companion.open()
        # Request settings keep their complete identity in the space digest; the cache needs only model geometry.
        geometry = {
            "model": ports.config["embedding"].get("model", ""),
            "dimensions": ports.config["embedding"].get("dimensions", 3072),
        }
        self.companion.register_space(space, geometry)
        self.embed = VectorEmbedPort(
            self.companion,
            ports.embedder,
            agent_id=self.binding.agent_id,
            installation_id=self.binding.installation_id,
            embedding_space=space,
        )
        self.purge = VectorPurgePort(
            self.companion,
            embedding_spaces=(space,),
            agent_id=self.binding.agent_id,
            installation_id=self.binding.installation_id,
        )
        vectors = VectorPort(self.companion, ports.embedder, expected_embedding_space=space, clock=self.clock.monotonic)
        self.core = MemoryCore(
            CoreConfig(self.binding),
            clock=self.clock,
            vectors=vectors,
            retrieval_policy=benchmark_policy(ports.config),
        )
        try:
            self.core.initialize()
        except ContractError:
            self.companion.close()
            raise
        self.keys: dict[str, str] = {}
        self.sessions: dict[str, str] = {}

    @property
    def vector_threshold(self) -> float | None:
        """The effective admission floor used by this store's recall pipeline."""
        return self.core.recall_pipeline.policy.vector_threshold

    def capture(self, turn):
        """Persist a source at its corpus time; gold labels are never sent to core."""
        self.clock.now = turn.at
        origin = {"user": "human_direct", "assistant": "assistant_visible", "document": "external_document"}[turn.role]
        principal = (
            TrustedSourcePrincipal("human", "verified", "principal:human:bench")
            if turn.role == "user"
            else TrustedSourcePrincipal("assistant", "verified", "principal:assistant:bench")
            if turn.role == "assistant"
            else TrustedSourcePrincipal("tool", "unresolved")
        )
        context = replace(self.context, session_id=turn.session, actor_origin=origin, source_principal=principal)
        value = {
            "protocol_version": "1.1",
            "source_event_key": turn.key,
            "source_revision": 1,
            "origin": origin,
            "role": turn.role,
            "content": turn.content,
            "occurred_at": turn.at,
            "recorded_at": turn.at,
            "time_precision": "instant",
            "capture_state": "complete",
            "evidence_refs": [],
        }
        receipt = self.core.record_event(context, value, scope_id="public", remaining_seconds=20)
        for ref in receipt.event_refs:
            self.keys[ref.ref], self.sessions[ref.ref] = turn.key, turn.session
        return receipt

    def state(self) -> dict:
        """Read synthetic queue states directly, including failures and future work."""
        with sqlite3.connect(self.core.storage.path) as connection:
            work = connection.execute(
                "SELECT work_type,state,last_error_code,count(*) FROM work_items GROUP BY 1,2,3"
            ).fetchall()
            candidates = connection.execute(
                "SELECT processing_state,reason,count(*) FROM candidate_lifecycle GROUP BY 1,2"
            ).fetchall()
            pending = connection.execute(
                "SELECT min(available_at) FROM work_items WHERE state IN ('pending','leased')"
            ).fetchone()[0]
        phases = {}
        capabilities = {
            "consolidate": callable(getattr(self.ports.model, "propose", None)),
            "embed": self.embed is not None,
            "evaluate_candidate": callable(getattr(self.ports.model, "evaluate_candidate", None)),
        }
        for kind, capable in capabilities.items():
            rows = [(state, count) for task, state, _, count in work if task == kind]
            waiting = any(state in {"pending", "leased"} for state, _ in rows)
            phases[kind] = (
                "capability_unavailable"
                if waiting and not capable
                else "pending"
                if waiting
                else "failed"
                if any(state == "failed" for state, _ in rows)
                else "complete"
                if rows
                else "not_required"
            )
        return {"work": work, "candidates": candidates, "next_at": pending, "phases": phases}

    def maintain_vectors(self, *, available_seconds=8.0):
        """Run the shipped bounded cache upkeep without invoking an embedding port."""
        config = SimpleNamespace(storage_dir=self.companion.db_path.parent)
        return index_if_due(self.companion, config, available_seconds=available_seconds)

    def settle(self, *, max_rounds=256, wall_seconds=900) -> dict:
        """Drain to verified terminal states, advancing only the injected business clock.

        Work done/obsolete and candidate resolved/archived/waiting_evidence are
        terminal for this evidence set. Failed work is reported as failed,
        blocked candidates as unavailable. Pending/leased work or unsettled
        candidates never becomes complete merely because a pass processed zero.
        Wall and round caps fail explicitly, and real network deadlines remain
        real-time deadlines inside the runtime adapters. A pass whose window
        runs out inside the drain is followed by the next pass, as the shipped
        worker supervisor does; the item it held is taken again after its lease.
        """
        deadline, advances = time.monotonic() + wall_seconds, 0
        for round_index in range(max_rounds):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "wall_limit", "rounds": round_index, **self.state()}
            try:
                receipt = self.core.drain_worker(
                    self.context,
                    owner_id="public-worker",
                    consolidation=self.ports.model,
                    embed=self.embed,
                    purge=self.purge,
                    max_items=256,
                    remaining_seconds=min(120, remaining),
                )
            except ContractError as exc:
                if exc.code != "DEADLINE_EXCEEDED":
                    raise
                receipt = None
            self.ports.cache.raise_if_missing()
            if (left := deadline - time.monotonic()) > 0:
                self.maintain_vectors(available_seconds=min(8.0, left))
            state = self.state()
            if time.monotonic() >= deadline:
                return {"status": "wall_limit", "rounds": round_index + 1, **state}
            if "capability_unavailable" in state["phases"].values():
                return {"status": "unavailable", "rounds": round_index + 1, **state}
            pending = sum(count for _, status, _, count in state["work"] if status in {"pending", "leased"})
            unsettled = sum(count for status, _, count in state["candidates"] if status == "pending_evaluation")
            if not pending and not unsettled:
                failed = sum(count for _, status, _, count in state["work"] if status == "failed")
                blocked = sum(count for status, _, count in state["candidates"] if status == "blocked")
                return {
                    "status": "unavailable" if blocked else "failed" if failed else "complete",
                    "rounds": round_index + 1,
                    "business_advances": advances,
                    **state,
                }
            if receipt is None or receipt.processed:
                continue
            waiting = throttled(state)
            if waiting:
                time.sleep(min(THROTTLE_WAIT_SECONDS, max(0.0, deadline - time.monotonic())))
            current = datetime.fromisoformat(self.clock.now.replace("Z", "+00:00"))
            next_at = datetime.fromisoformat(state["next_at"].replace("Z", "+00:00")) if state["next_at"] else current
            self.clock.now = max(current + timedelta(seconds=901), next_at + timedelta(seconds=1)).isoformat()
            # Waiting out a provider's refusal is bounded by the wall clock, not by the business-clock advances.
            advances += 0 if waiting else 1
            if advances > 16:
                return {"status": "business_time_limit", "rounds": round_index + 1, **state}
        return {"status": "round_limit", "rounds": max_rounds, **self.state()}

    def recall(self, case, *, mode="auto") -> tuple[dict, float]:
        """Ask at the corpus question date through the real automatic packet entry."""
        self.clock.now = case.at
        context = replace(self.context, session_id=f"ask:{case.id}:{mode}")
        body = {
            "protocol_version": "1.1",
            "request_id": digest([case.id, mode])[:24],
            "query": case.question,
            "mode": mode,
            "max_items": 6 if mode == "auto" else 10,
            "budget_tokens": 4096,
        }
        started = time.perf_counter()
        packet = dict(self.core.recall_packet(context, body, zone=UTC, deadline_seconds=5))
        self.ports.cache.raise_if_missing()
        return packet, (time.perf_counter() - started) * 1000

    def retrieved_sources(self, packet: dict) -> tuple[set[str], set[str]]:
        """Resolve returned evidence refs back to corpus turns and sessions."""
        refs = {ref.rsplit("@", 1)[0] for item in packet["items"] for ref in item["evidence_refs"]}
        return {self.keys[ref] for ref in refs if ref in self.keys}, {
            self.sessions[ref] for ref in refs if ref in self.sessions
        }

    def close(self) -> None:
        """Release the synthetic vector companion; no host process was started."""
        self.companion.close()
