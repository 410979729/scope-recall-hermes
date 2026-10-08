#!/usr/bin/env python3
"""Known-answer check for three scope-recall invariants, on a fresh store with synthetic data.

The invariants, as stated for 3.7.x on #179:

  A. A fact needs a human source and a worker pass before it is served as a fact.
  B. revise and forget need the person's own request, in the same session, naming the
     target's exact current version.
  C. After forget, nothing comes back from word search, from the vector companion or
     from recall, and that still holds after the companion is rebuilt.

How it runs:
  * Public MemoryCore calls, the shipped SQLite vector companion (sqlite-bruteforce),
    the shipped LanceEmbedPort / LanceVectorPort / LancePurgePort adapters, and the
    documented rebuild path (respace-embeddings into a new space, then worker passes).
  * No host, no network, no API key.  The only stand-ins are the two model seams:
    a stub consolidation model that proposes one fixed claim per known sentence, and a
    deterministic hashed bag-of-words embedder.  Both are in this file.
  * Every run starts from an empty temp directory and deletes it afterwards.

Usage, against an installed release (expected_answers.json is read from beside this file):
    python -m pip install "hermes-scope-recall==3.8.0"    # or: pip install . from a main checkout
    python recall_invariants_check.py                      # compares with expected_answers.json
    python recall_invariants_check.py --json observed.json

In the tree, test_recall_invariants_check.py beside this file runs the same check in the
storage tier (python scripts/check.py --tier storage) and fails on the first answer that
does not match.

Exit code 0 when every observed answer matches expected_answers.json, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import RecallPolicy
from scope_recall.runtime.lance_port import LanceEmbedPort, LancePurgePort, LanceVectorPort

try:  # 3.8.0 and later: a whole-store re-embed into a new space (respace-embeddings)
    from scope_recall.runtime.vector_upkeep import respace_if_due
except ImportError:  # 3.7.x has no whole-store re-embed (#200), so the rebuild checks are skipped there
    respace_if_due = None
from scope_recall.vector.store import build_vector_store

HERE = Path(__file__).resolve().parent
SCOPE = "repro-scope"
SPACE_1 = hashlib.sha256(b"repro hashed bag-of-words space 1").hexdigest()
SPACE_2 = hashlib.sha256(b"repro hashed bag-of-words space 2").hexdigest()
DIMS = 64
#: The hashed embedder is crude, so its admission threshold is lower than a real model's 0.653 / 0.70.
VECTOR_THRESHOLD = 0.30


# ---------------------------------------------------------------- deterministic model seams


def _vector(text: str) -> list[float]:
    """Hashed bag of words, L2 normalised.  Same text, same vector, on every machine."""
    out = [0.0] * DIMS
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
        out[h % DIMS] += 1.0 if (h >> 31) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in out))
    if norm == 0:
        out[0], norm = 1.0, 1.0
    return [v / norm for v in out]


class Embedder:
    def embed_source(self, source, remaining_seconds=1.0):
        return _vector(source.event.get("content") or "")

    def embed_text(self, text, remaining_seconds=1.0):
        return _vector(text)

    def embed_query(self, query, remaining_seconds=1.0):
        return _vector(query)


#: The stub "model": for each known sentence, the one claim a careful extractor would propose.
#: It proposes the same kind of claim for the assistant's sentence as for the person's.
KNOWN_SENTENCES = {
    "For project Orchard the staging database port is 6543.": ("Orchard", "staging database port", "6543"),
    "For project Orchard the release manager is Dana Whitfield.": ("Orchard", "release manager", "Dana Whitfield"),
    "For project Orchard the backup bucket is cold-archive-7.": ("Orchard", "backup bucket", "cold-archive-7"),
}


class StubConsolidation:
    def __init__(self):
        self.calls = 0

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
        self.calls += 1
        claims = []
        for s in sources:
            text = s.event.get("content") or ""
            if text in KNOWN_SENTENCES:
                subject, predicate, value = KNOWN_SENTENCES[text]
                claims.append(
                    dict(
                        kind="fact",
                        subject=subject,
                        predicate=predicate,
                        value_text=value,
                        conditions=[],
                        statement_kind="assertion",
                        valid_from=s.event["occurred_at"],
                        valid_to=None,
                        evidence_spans=[dict(source_ref=s.ref, source_revision=s.revision, quote=text)],
                    )
                )
        return json.dumps(
            dict(
                protocol_version="1.1",
                source_refs=[f"{s.ref}@{s.revision}" for s in sources],
                claim_proposals=claims,
                resume_proposals=[],
                reference_proposals=[],
            )
        )


# ---------------------------------------------------------------- harness


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.binding = InstanceBinding("repro-agent", "repro-install", root / "store", frozenset({SCOPE}))
        self.n = 0
        self.model = StubConsolidation()
        self.embedder = Embedder()
        self.store = None
        self.open(SPACE_1)
        self.core.initialize()

    def open(self, space: str):
        """(Re)open the companion for ``space`` and a MemoryCore wired to it, as a restarted host would."""
        if self.store is not None:
            self.store.close()
        self.space = space
        self.store = build_vector_store(
            "sqlite-bruteforce",
            storage_dir=self.root / "vectors" / space,
            table_name="repro_vectors",
            dimensions=DIMS,
            metric="cosine",
        )
        self.store.open()
        self.embed_port = LanceEmbedPort(
            self.store, self.embedder, agent_id="repro-agent", installation_id="repro-install", embedding_space=space
        )
        self.purge_port = LancePurgePort(
            self.store, embedding_spaces=(space,), agent_id="repro-agent", installation_id="repro-install"
        )
        vectors = LanceVectorPort(self.store, self.embedder, expected_embedding_space=space)
        self.core = MemoryCore(
            CoreConfig(self.binding),
            vectors=vectors,
            retrieval_policy=RecallPolicy(vector_threshold=VECTOR_THRESHOLD, embedding_space_id=space),
        )

    def ctx(self, session: str, origin: str = "human_direct") -> TrustedContext:
        return TrustedContext(self.binding, session, frozenset({SCOPE}), origin, project_id="orchard", branch_id="main")

    def say(self, session: str, text: str, origin: str = "human_direct", when: str = "2026-10-01T09:00:00Z"):
        self.n += 1
        role = {"human_direct": "user", "assistant_visible": "assistant", "tool_observation": "tool"}[origin]
        event = dict(
            protocol_version="1.1",
            source_event_key=f"repro/{self.n}",
            source_revision=1,
            origin=origin,
            role=role,
            content=text,
            occurred_at=when,
            recorded_at=when,
            time_precision="instant",
            capture_state="complete",
            evidence_refs=[],
        )
        receipt = self.core.record_event(self.ctx(session, origin), event, scope_id=SCOPE, remaining_seconds=10)
        return self.core.source(self.ctx(session), receipt.event_refs[0].ref, 1)

    def drain(self, ctx, rounds: int = 8) -> int:
        total = 0
        for _ in range(rounds):
            r = self.core.drain_worker(
                ctx,
                consolidation=self.model,
                embed=self.embed_port,
                purge=self.purge_port,
                max_items=32,
                remaining_seconds=20,
            )
            total += r.processed
            if r.processed == 0:
                break
        return total

    def rebuild(self, ctx) -> dict:
        """Wipe the companion, switch to a new space and re-embed the whole store from SQLite truth.

        This is the path docs/install.md gives for a rebuild (respace-embeddings --start --apply,
        then worker passes); respace_if_due is what each runtime worker pass calls.
        """
        self.store.close()
        self.store = None
        shutil.rmtree(self.root / "vectors")
        self.open(SPACE_2)
        report = self.core.respace_embeddings(ctx, space_id=SPACE_2, action="start", dry_run=False)
        pages = []
        for _ in range(40):
            pages.append(respace_if_due(self.core.storage, ctx, SPACE_2))
            processed = self.drain(ctx, rounds=1)
            run = self.core.respace_embeddings(ctx, space_id=SPACE_2)["run"]
            if run and run.get("completed") and processed == 0:
                break
        return dict(
            start=report.get("run"),
            pages=[p.get("outcome") if isinstance(p, dict) else p for p in pages],
            final=self.core.respace_embeddings(ctx, space_id=SPACE_2),
        )

    # reads ----------------------------------------------------------------
    def recall(self, ctx, query: str) -> dict:
        """An explicit lookup, as the recall tool makes it: no ambient background stands in for evidence."""
        self.n += 1
        request = dict(
            protocol_version="1.1",
            request_id=f"q{self.n}",
            query=query,
            mode="current",
            max_items=8,
            budget_tokens=4000,
        )
        return dict(self.core.recall_packet(ctx, request, deadline_seconds=10, background_without_evidence=False))

    def companion_dump(self) -> str:
        return json.dumps(self.store.list_records(), default=str)

    def vector_hits(self, query: str) -> list[dict]:
        records = self.store.list_records()
        partitions = sorted({r.get("scope_id") for r in records.values() if r.get("scope_id")})
        return self.store.search_scopes(_vector(query), scope_ids=partitions, limit=50) if partitions else []


def outcome(fn):
    try:
        return "accepted", fn()
    except ContractError as exc:
        return (f"{exc.code}:{exc.field}" if exc.field else exc.code), None


def active_claim_with(packet, value: str) -> bool:
    return any(
        i.get("kind") == "claim" and i.get("claim_state") == "active" and value in (i.get("content") or "")
        for i in packet.get("items", [])
    )


# ---------------------------------------------------------------- the run


def run() -> tuple[dict, dict]:
    obs: dict = {}
    diag: dict = {}
    root = Path(tempfile.mkdtemp(prefix="scope-recall-repro-"))
    try:
        h = Harness(root)
        A = h.ctx("session-A")

        # ---------------- A. a fact needs a human source and a worker pass
        port_src = h.say("session-A", "For project Orchard the staging database port is 6543.")
        manager_src = h.say("session-A", "For project Orchard the release manager is Dana Whitfield.")
        h.say("session-A", "For project Orchard the backup bucket is cold-archive-7.", origin="assistant_visible")
        h.say("session-A", "df -h /srv/orchard: 81% used, mount point is /dev/vdb1", origin="tool_observation")

        obs["A1_port_served_as_active_fact_before_worker_pass"] = active_claim_with(
            h.recall(A, "What is the Orchard staging database port?"), "6543"
        )
        obs["A2_port_words_findable_before_worker_pass"] = any(
            s.ref == port_src.ref for s in h.core.search_sources(A, "6543")
        )

        h.drain(A)
        obs["A3_port_served_as_active_fact_after_worker_pass"] = active_claim_with(
            h.recall(A, "What is the Orchard staging database port?"), "6543"
        )
        obs["A4_release_manager_served_as_active_fact_after_worker_pass"] = active_claim_with(
            h.recall(A, "Who is the Orchard release manager?"), "Dana Whitfield"
        )
        bucket = h.recall(A, "What is the Orchard backup bucket?")
        obs["A5_assistant_only_bucket_served_as_active_fact"] = active_claim_with(bucket, "cold-archive-7")
        diag["A5_bucket_items"] = [
            f"{i.get('kind')}:{i.get('claim_state') or '-'}:{i.get('qualification_reason') or '-'}"
            for i in bucket["items"]
            if "cold-archive-7" in (i.get("content") or "")
        ]
        disk = h.recall(A, "How full is /srv/orchard?")
        obs["A6_tool_output_served_as_fact"] = any(
            i.get("kind") == "claim" and "81%" in (i.get("content") or "") for i in disk["items"]
        )
        obs["A7_stub_model_was_called"] = h.model.calls > 0

        packet = h.recall(A, "What is the Orchard staging database port?")
        port_claim = next(
            (i for i in packet["items"] if i.get("kind") == "claim" and "6543" in (i.get("content") or "")), None
        )
        manager_claim = next(
            (i for i in h.recall(A, "Who is the Orchard release manager?")["items"] if i.get("kind") == "claim"), None
        )
        if port_claim is None:
            obs["SETUP_port_claim_visible"] = False
            return obs, diag
        ref, rev = port_claim["ref"], port_claim["revision"]
        diag["port_claim"] = dict(ref=ref, revision=rev)

        def revise_req(source, value, expected):
            return dict(
                protocol_version="1.1",
                target_ref=ref,
                expected_revision=expected,
                new_value=value,
                conditions=[],
                source_evidence_refs=[f"{source.ref}@{source.revision}"],
                valid_from=source.event["occurred_at"],
            )

        # ---------------- B. revise: the person's own request, same session, exact version
        T1 = "2026-10-02T09:00:00Z"
        obs["B1_revise_citing_only_the_original_statement"], _ = outcome(
            lambda: h.core.revise(A, revise_req(port_src, "6544", rev), remaining_seconds=10)
        )
        asst = h.say(
            "session-A", "Please correct Orchard staging database port: 6544.", origin="assistant_visible", when=T1
        )
        obs["B2_revise_citing_an_assistant_message"], _ = outcome(
            lambda: h.core.revise(A, revise_req(asst, "6544", rev), remaining_seconds=10)
        )
        other = h.say("session-B", "Please correct Orchard staging database port: 6544.", when=T1)
        obs["B3_revise_in_session_A_citing_a_request_made_in_session_B"], _ = outcome(
            lambda: h.core.revise(A, revise_req(other, "6544", rev), remaining_seconds=10)
        )
        mine = h.say("session-A", "Please correct Orchard staging database port: 6544.", when=T1)
        head = h.core.current_claim(A, ref).revision
        obs["B4_capture_alone_changed_the_fact"] = head != rev
        obs["B5_revise_with_own_request_but_wrong_version"], _ = outcome(
            lambda: h.core.revise(A, revise_req(mine, "6544", head + 1), remaining_seconds=10)
        )
        obs["B6_revise_with_own_request_and_exact_version"], _ = outcome(
            lambda: h.core.revise(A, revise_req(mine, "6544", head), remaining_seconds=10)
        )
        current = h.core.current_claim(A, ref)
        obs["B7_current_value_after_revise"] = current.payload["value_text"] if current else None
        obs["B8_history_values_after_revise"] = [v.payload["value_text"] for v in h.core.claim_history(A, ref)]
        rev2 = current.revision if current else None
        h.drain(A)

        # ---------------- B. forget: the person's own request, same session, exact version
        F = h.ctx("session-F")

        def forget_req(expected):
            return dict(protocol_version="1.1", mode="delete", target_refs=[ref], expected_revisions={ref: expected})

        obs["B9_forget_in_a_session_with_no_request"], _ = outcome(
            lambda: h.core.forget(F, forget_req(rev2), remaining_seconds=10)
        )
        negated = h.say(
            "session-F", "Never forget the Orchard staging database port 6544.", when="2026-10-03T09:00:00Z"
        )
        obs["B10_forget_after_negated_wording"], _ = outcome(
            lambda: h.core.forget(F, forget_req(rev2), remaining_seconds=10)
        )
        elsewhere = h.say(
            "session-A", "Please delete the Orchard staging database port 6544.", when="2026-10-03T09:01:00Z"
        )
        obs["B11_forget_in_session_F_when_the_request_was_made_in_session_A"], _ = outcome(
            lambda: h.core.forget(F, forget_req(rev2), remaining_seconds=10)
        )
        authorizing = h.say(
            "session-F", "Please delete the Orchard staging database port 6544.", when="2026-10-03T09:02:00Z"
        )
        obs["B12_forget_with_own_request_but_superseded_version"], _ = outcome(
            lambda: h.core.forget(F, forget_req(rev), remaining_seconds=10)
        )

        dump = h.companion_dump()
        obs["C0_companion_held_the_fact_before_forget"] = ref in dump and port_src.ref in dump

        status, receipt = outcome(lambda: h.core.forget(F, forget_req(rev2), remaining_seconds=10))
        obs["B13_forget_with_own_request_and_exact_version"] = status
        diag["forget_receipt"] = receipt
        h.drain(F)  # the worker pass that runs the purge against the companion

        # What must be gone: the claim, the sources it stands on, and the authorizing command.
        gone = {
            "claim": ref,
            "original_statement": port_src.ref,
            "correction_request": mine.ref,
            "authorizing_delete": authorizing.ref,
        }
        # What the deletion contract does not cover: other messages that merely repeat the value.
        bystanders = {
            "assistant_correction": asst.ref,
            "session_B_correction": other.ref,
            "negated_request": negated.ref,
            "session_A_delete_request": elsewhere.ref,
        }
        diag["refs_that_must_be_gone"] = gone
        diag["bystander_refs"] = bystanders
        control_refs = {manager_src.ref} | ({manager_claim["ref"]} if manager_claim else set())

        def after_forget(tag: str):
            found = set()
            for q in ("6543", "6544", "staging database port", "Orchard staging"):
                found |= {s.ref for s in h.core.search_sources(F, q, history=True)}
            obs[f"{tag}_word_search_returns_forgotten"] = sorted(k for k, v in gone.items() if v in found)
            obs[f"{tag}_word_search_bystanders_still_found"] = sorted(k for k, v in bystanders.items() if v in found)
            dump = h.companion_dump()
            obs[f"{tag}_companion_rows_for_forgotten"] = sorted(k for k, v in gone.items() if v in dump)
            hits = json.dumps(h.vector_hits("What is the Orchard staging database port 6543 6544?"), default=str)
            obs[f"{tag}_vector_search_returns_forgotten"] = sorted(k for k, v in gone.items() if v in hits)
            recalled = set()
            for q in (
                "What is the Orchard staging database port?",
                "Orchard staging database port 6543",
                "Orchard staging database port 6544",
            ):
                for item in h.recall(F, q)["items"]:
                    recalled.add(item.get("ref"))
                    recalled.update(r.split("@")[0] for r in item.get("evidence_refs") or [])
            obs[f"{tag}_recall_returns_forgotten"] = sorted(k for k, v in gone.items() if v in recalled)
            obs[f"{tag}_current_claim_exists"] = h.core.current_claim(F, ref) is not None
            obs[f"{tag}_history_versions"] = len(h.core.claim_history(F, ref))
            obs[f"{tag}_control_fact_recalled"] = active_claim_with(
                h.recall(F, "Who is the Orchard release manager?"), "Dana Whitfield"
            )
            control_hits = json.dumps(h.vector_hits("Orchard release manager Dana Whitfield"), default=str)
            obs[f"{tag}_control_fact_in_vector_search"] = any(r in control_hits for r in control_refs)

        after_forget("C1")
        if respace_if_due is None or not hasattr(h.core, "respace_embeddings"):
            diag["skipped"] = (
                "C2 and C3: this release has no whole-store re-embed (respace-embeddings arrived in 3.8.0)"
            )
            h.open(SPACE_1)
            after_forget("C4")
            h.store.close()
            return obs, diag
        diag["rebuild"] = h.rebuild(F)
        diag["rebuilt_companion_rows"] = len(h.store.list_records())
        obs["C2_rebuilt_companion_has_rows"] = len(h.store.list_records()) > 0
        after_forget("C3")
        h.open(SPACE_2)  # a fresh MemoryCore on the same files, as after a restart
        after_forget("C4")
        h.store.close()
        return obs, diag
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expected", default=str(HERE / "expected_answers.json"))
    ap.add_argument("--json", help="also write observed answers and diagnostics to this file")
    args = ap.parse_args()

    version = importlib.metadata.version("hermes-scope-recall")
    print(f"hermes-scope-recall {version} | python {platform.python_version()} | sqlite {sqlite3.sqlite_version}")
    observed, diag = run()

    checks = json.loads(Path(args.expected).read_text())["checks"] if Path(args.expected).exists() else {}
    failures = skipped = 0
    for key in sorted(set(observed) | set(checks), key=_order):
        got = observed.get(key, "<not run>")
        if key in checks and key not in observed and "skipped" in diag:
            skipped += 1
            print(f"skip {key}")
        elif key in checks:
            want = checks[key]["expected"]
            ok = got == want
            failures += not ok
            print(
                f"{'ok  ' if ok else 'FAIL'} {key}: {json.dumps(got)}"
                + ("" if ok else f"  (expected {json.dumps(want)})")
            )
        else:
            print(f"info {key}: {json.dumps(got, default=str)}")
    if "skipped" in diag:
        print(f"\nskipped {diag['skipped']}")
    print(f"\n{len(checks) - failures - skipped} of {len(checks) - skipped} expected answers matched")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                dict(
                    version=version,
                    python=platform.python_version(),
                    sqlite=sqlite3.sqlite_version,
                    observed=observed,
                    diagnostics=diag,
                ),
                indent=2,
                default=str,
            )
        )
    return 1 if failures else 0


def _order(key: str):
    m = re.match(r"([A-Z]+)(\d+)", key)
    return (m.group(1), int(m.group(2)), key) if m else (key, 0, key)


if __name__ == "__main__":
    sys.exit(main())
