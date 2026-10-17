#!/usr/bin/env python3
"""The recall benchmark: a synthetic shared store, its questions asked the way hosts ask, and the answers scored.

    python recall_bench.py                          # dev split at the CI size: summary, and regressions if any
    python recall_bench.py --split heldout          # the held-out split
    python recall_bench.py --scale 2 --json r.json  # the larger world; the full report as JSON
    python recall_bench.py --update                 # record the dev numbers in baseline.json

How it runs:
  * ``bench_world.py`` beside this file makes the world: owners, the entries they talk to, the scopes each entry
    reads, weeks of sessions, the claims a careful consolidation model proposes and the questions.  It is synthetic
    and the same on every machine.
  * Public MemoryCore calls on a fresh temp store, as in ``tests/known_answer/recall_invariants_check.py``: each
    message is captured at its own time on the world's clock with the principal its host attests, and a worker pass
    runs at the end of every day with a stub consolidation that proposes the world's claims, the shipped SQLite
    vector companion (sqlite-bruteforce) and a deterministic hashed embedder.  No host, no network, no model.
    Candidate evaluation, the worker's other model call, has no stub: its work stays pending.
  * Every question is asked twice, each time in a session of its own and on the same store: as the automatic packet
    a hook or a prefetch puts before a prompt, and as the explicit lookup a recall tool makes.  Questions are not
    captured, so none answers another.  A day a question names is read in the world's zone.
  * The store runs on a clock that ticks a microsecond each time it is read (``WorldClock``), so a recall's deadline
    counts the work it did and is never reached: every machine scores the same packets.  Latency is measured in a
    second pass on the real clock, and is reported, not gated.

An item answers a part of a question when it is that part's message, or a claim drawn from it that states what the
message says (``credits``): a claim that cites the message but names another value, denies it or quotes it answers
nothing, and an item held in a scope the asking entry does not read answers nothing and is a leak.  Per path and task
the report gives the share of each question's answer in the first 1, 3, 6 and 10 items, MRR, nDCG@10, how often a
replaced value ranked above the current one (and each other kind of hard negative above the answer), the share of
answerable questions answered and of the others met with ``unknown``, and over all questions their balanced accuracy,
items from a scope the asking entry does not read, packet size in the packet's own units, latency, and the channel
that found the first item that answered.  It also gives how many messages of each kind a claim made a fact, and how
many active claims stand on each kind.  ``test_recall_bench.py`` beside this file runs the dev split in the retrieval
tier.

The vector channel here hashes words: it finds what shares a question's words, not what means the same, so a
paraphrased question measures what the store finds without a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import types
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]
for _folder in (HERE, HERE.parent / "known_answer", HERE.parent / "contract"):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))
# The package is the checkout this file sits in, bound as tests/conftest.py binds it for the suite: what a run
# measures and records is never an installed copy, which may be older.  One bound already elsewhere -- imported
# before this file loaded -- is refused, not measured.
if "scope_recall" not in sys.modules:
    sys.modules["scope_recall"] = types.ModuleType("scope_recall")
    sys.modules["scope_recall"].__path__ = [str(CHECKOUT)]
_BOUND = [Path(entry).resolve() for entry in getattr(sys.modules["scope_recall"], "__path__", ())]
if _BOUND != [CHECKOUT]:
    raise ImportError(
        f"scope_recall is bound to {[str(entry) for entry in _BOUND]}, not this checkout ({CHECKOUT}): the bench"
        " measures and records only the code beside it"
    )

from bench_world import TASKS, Question, World, build_world, local_day  # noqa: E402
from recall_invariants_check import DIMS, VECTOR_THRESHOLD  # noqa: E402
from recall_probes import mean_reciprocal_rank  # noqa: E402
from scope_recall.contracts import InstanceBinding, TrustedContext, TrustedSourcePrincipal  # noqa: E402
from scope_recall.core import CoreConfig, MemoryCore  # noqa: E402
from scope_recall.core.events import lexical_terms  # noqa: E402
from scope_recall.core.recall_packet import envelope_tokens  # noqa: E402
from scope_recall.core.recall_policy import RecallPolicy  # noqa: E402
from scope_recall.core.retrieval import AUTOMATIC_PACKET_BUDGET_UNITS, CollectionQuery, optional_json  # noqa: E402
from scope_recall.core.retrieval_storage import scope_digest  # noqa: E402
from scope_recall.runtime.lance_port import LanceEmbedPort, LancePurgePort, LanceVectorPort  # noqa: E402
from scope_recall.vector.store import build_vector_store  # noqa: E402

BASELINE = HERE / "baseline.json"
SPACE = hashlib.sha256(b"recall bench hashed index terms").hexdigest()
AGENT, INSTALLATION = "bench-agent", "shared-install:recall-bench"
#: How each path asks: the automatic packet (``adapters/clients/prompt_recall.py``, Hermes' ``prefetch``), and the
#: recall tool's lookup with the budget the tool recommends and ten items, so that recall@10 means something.
PATHS = {
    "auto": ({"mode": "auto", "max_items": 6, "budget_tokens": AUTOMATIC_PACKET_BUDGET_UNITS}, {}),
    "explicit": (
        {"mode": "current", "max_items": 10, "budget_tokens": 4096},
        {"deadline_seconds": 5.0, "background_without_evidence": False},
    ),
}
CUTOFFS = (1, 3, 6, 10)
#: The metrics the baseline holds, where a block has them: ``answered`` where it has answerable questions,
#: ``abstained`` where it has unanswerable ones, balanced accuracy where it has both.  Size, latency, channels,
#: hard negatives other than the replaced value, and claim states are reported only.
GATED = (
    "recall@1",
    "recall@3",
    "recall@6",
    "recall@10",
    "mrr",
    "ndcg@10",
    "outdated_above_current",
    "answered",
    "abstained",
    "abstention_balanced_accuracy",
)
LOWER_IS_BETTER = frozenset({"outdated_above_current"})
#: How far a gated metric may move to the wrong side of its recorded value.  The world and the clock are fixed, so
#: the numbers repeat exactly; the margin is for ties another SQLite build may order differently.  One question of
#: the largest task moves its recall@k by 0.028, past the margin.
TOLERANCE = 0.02
#: Kinds of hard negative a careful extractor marks, or the store refuses by their source: none should become a
#: fact.  The baseline holds how many active claims stand on each kind (``facts``) as a ceiling, with no margin: the
#: store may refuse more of them, and a change that lets one more through fails.
NEVER_FACTS = ("hypothetical", "quoted", "negated", "assistant", "tool")
#: How long one page of the claim walk may take on the store's clock: ``MemoryCore.collection``'s deadline.  The
#: collection may stop a page at its deadline before it examines a claim and then names no next page, as the last
#: page does; a page without a next one that took this long fails the walk (``BenchStore._claim_versions``).
CLAIM_PAGE_SECONDS = 2.0


# ---------------------------------------------------------------- the model seams


def _vector(text: str) -> list[float]:
    """The store's own index terms (``lexical_terms``: words, identifiers, Chinese character pairs) hashed into
    ``DIMS`` signed buckets and L2-normalised.  ``recall_invariants_check``'s embedder hashes ASCII words only, so
    every Chinese-only text would land on one vector and match every other at cosine 1."""
    out = [0.0] * DIMS
    for term in lexical_terms(text):
        h = int.from_bytes(hashlib.sha256(term.encode("utf-8")).digest()[:4], "big")
        out[h % DIMS] += 1.0 if (h >> 31) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in out))
    if norm == 0:
        out[0], norm = 1.0, 1.0
    return [v / norm for v in out]


class TermEmbedder:
    def embed_source(self, source, remaining_seconds=1.0):
        return _vector(source.event.get("content") or "")

    def embed_text(self, text, remaining_seconds=1.0):
        return _vector(text)

    def embed_query(self, query, remaining_seconds=1.0):
        return _vector(query)


class ManifestConsolidation:
    """The stub model: for each sentence the world has claims for, those claims, quoting the whole sentence."""

    def __init__(self, claims) -> None:
        self.claims = claims

    def propose(self, sources, *, episode_ref=None, remaining_seconds=1.0, validation_feedback=None):
        proposals = [
            {
                "kind": claim.kind,
                "subject": claim.subject,
                "predicate": claim.predicate,
                "value_text": claim.value,
                "conditions": [],
                "statement_kind": claim.statement_kind,
                "valid_from": source.event["occurred_at"],
                "valid_to": None,
                "evidence_spans": [
                    {"source_ref": source.ref, "source_revision": source.revision, "quote": source.event["content"]}
                ],
            }
            for source in sources
            for claim in self.claims.get(source.event.get("content") or "", ())
        ]
        return json.dumps(
            {
                "protocol_version": "1.1",
                "source_refs": [f"{source.ref}@{source.revision}" for source in sources],
                "claim_proposals": proposals,
                "resume_proposals": [],
                "reference_proposals": [],
            }
        )


class WorldClock:
    """The world's time, moved by the store's builder, and a monotonic clock that ticks ``TICK`` each time it is read
    unless ``realtime`` is set.  A recall reads it a few hundred times, so none comes near its deadline of seconds on
    any machine, and a loop that waits on it still ends."""

    TICK = 1e-6

    def __init__(self, now: str) -> None:
        self.now = now
        self.realtime = False
        self._ticks = 0

    def utc_now(self) -> str:
        return self.now

    def monotonic(self) -> float:
        if self.realtime:
            return time.monotonic()
        self._ticks += 1
        return self._ticks * self.TICK


# ---------------------------------------------------------------- what answers


@dataclass(frozen=True)
class Frame:
    """What a claim states when it answers a part: its kind, its subject (any name it may be bound to), predicate and
    value."""

    kind: str
    subjects: frozenset[str]
    predicate: str
    value: str


def _same(left: object, right: object) -> bool:
    def norm(value: object) -> str:
        return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())

    return norm(left) == norm(right)


def credits(
    item: dict, gold: tuple[str, ...], key_of: Mapping[str, str], frames: Mapping[str, tuple[Frame, ...]]
) -> frozenset[str]:
    """The gold messages (parts of an answer) a delivered item gives: the message itself, or a claim drawn from it
    that asserts one of the ``frames`` that message states.  A claim citing the message with another value, a denial
    or a quotation of it gives none."""
    if item["kind"] == "event":
        return frozenset({key_of.get(item["ref"])} & set(gold))
    payload = optional_json(item["content"]) if item["kind"] in {"claim", "procedure"} else None
    if not isinstance(payload, dict) or payload.get("statement_kind") != "assertion":
        return frozenset()
    cited = {key_of.get(ref.rsplit("@", 1)[0]) for ref in item["evidence_refs"]}
    return frozenset(
        key
        for key in gold
        if key in cited
        and any(
            payload.get("kind") == frame.kind
            and any(_same(payload.get("subject"), subject) for subject in frame.subjects)
            and _same(payload.get("predicate"), frame.predicate)
            and _same(payload.get("value_text"), frame.value)
            for frame in frames.get(key, ())
        )
    )


def ndcg(credited: tuple[frozenset[str], ...], parts: int, cutoff: int = 10) -> float:
    """nDCG@cutoff over delivered items: an item's gain is the parts of the answer it is first to give, and the ideal
    ranking holds the items that gave any, most first, then one item for each part none gave."""
    given: set[str] = set()
    gains = []
    for keys in credited:
        gains.append(len(keys - given))
        given |= keys
    ideal = [*sorted((gain for gain in gains if gain), reverse=True), *[1] * (parts - len(given))]

    def dcg(values) -> float:
        return sum(value / math.log2(place + 1) for place, value in enumerate(values[:cutoff], 1))

    return dcg(gains) / dcg(ideal)


# ---------------------------------------------------------------- the store


@dataclass(frozen=True)
class Answer:
    """One recall: the gold messages each delivered item gives, every message it stands on, the scope it is held in
    itself, the channel that found it, and what it cost.  ``seen_before`` is whether the question's revoked scopes
    still showed in its packet a moment before."""

    packet: dict
    credited: tuple[frozenset[str], ...]
    carried: tuple[frozenset[str], ...]
    scopes: tuple[str | None, ...]
    channels: tuple[str | None, ...]
    tokens: int
    ms: float
    seen_before: bool | None = None


def message_roles(world: World) -> dict[str, str]:
    """Each message a question names: ``answer``, or the kind of hard negative it is first named as."""
    roles: dict[str, str] = {}
    for question in world.questions:
        roles.update((key, kind) for kind, key in question.negatives if key not in roles)
        roles.update((key, "answer") for key in question.gold)
    return roles


class BenchStore:
    """A world captured into a fresh shared store under ``root``, through public MemoryCore calls."""

    def __init__(self, world: World, root: Path, *, vectors: bool = True) -> None:
        self.world = world
        self.clock = WorldClock(world.messages[0].at)
        self.binding = InstanceBinding(AGENT, INSTALLATION, root / "store", world.scopes, False, "shared")
        self.companion = build_vector_store(
            "sqlite-bruteforce", storage_dir=root / "vectors", table_name="bench", dimensions=DIMS, metric="cosine"
        )
        self.companion.open()
        embedder = TermEmbedder()
        self.embed = LanceEmbedPort(
            self.companion, embedder, agent_id=AGENT, installation_id=INSTALLATION, embedding_space=SPACE
        )
        self.purge = LancePurgePort(
            self.companion, embedding_spaces=(SPACE,), agent_id=AGENT, installation_id=INSTALLATION
        )
        port = LanceVectorPort(self.companion, embedder, expected_embedding_space=SPACE, clock=self.clock.monotonic)
        self.core = MemoryCore(
            CoreConfig(self.binding),
            clock=self.clock,
            vectors=port if vectors else None,
            retrieval_policy=RecallPolicy(
                vector_threshold=VECTOR_THRESHOLD if vectors else None, embedding_space_id=SPACE
            ),
        )
        self.core.initialize()
        self.model = ManifestConsolidation(world.claims)
        self.worker = TrustedContext(self.binding, "bench-worker", world.scopes, "human_direct")
        self.entries = {entry.id: entry for entry in world.entries}
        self.key_of: dict[str, str] = {}
        self.scope_of = {message.key: message.scope for message in world.messages}
        self.text_of = {message.key: message.text for message in world.messages}
        self.frames = {message.key: self._frames(message) for message in world.messages}
        self.claim_scopes: dict[str, str | None] = {}
        with self.core.storage.write(self.worker) as tx:
            tx.registry.register_scopes(world.scopes)
            for entry in world.entries:
                tx.registry.register_entry(entry.id, entry.name, entry.host, now=self.clock.now)
        # The retrieval behind each packet, read for the channel that found each item.
        self.last_result = None
        search = self.core.recall_pipeline.search

        def witnessed(context):
            self.last_result = search(context)
            return self.last_result

        self.core.recall_pipeline.search = witnessed
        started = time.perf_counter()
        self._capture()
        self.build_seconds = time.perf_counter() - started

    def _frames(self, message) -> tuple[Frame, ...]:
        """What the claims asserted for a message state; a claim about the person ("user") is bound to its owner."""
        owner = self.entries[message.entry].owner
        return tuple(
            Frame(
                claim.kind,
                frozenset({claim.subject, f"principal:human:{owner}"} if claim.subject == "user" else {claim.subject}),
                claim.predicate,
                claim.value,
            )
            for claim in self.world.claims.get(message.text, ())
            if claim.statement_kind == "assertion"
        )

    def _context(self, entry_id: str, session: str, origin: str, reads: frozenset[str] | None = None) -> TrustedContext:
        """A context of ``entry_id`` as its host attests it: the owner and the entry's assistant are verified."""
        entry = self.entries[entry_id]
        principal = {
            "human_direct": TrustedSourcePrincipal("human", "verified", f"principal:human:{entry.owner}"),
            "assistant_visible": TrustedSourcePrincipal("assistant", "verified", f"principal:assistant:{entry.id}"),
        }.get(origin, TrustedSourcePrincipal("tool", "unresolved"))
        reads = entry.reads if reads is None else reads
        return TrustedContext(self.binding, session, reads, origin, source_principal=principal, entry_id=entry.id)

    def _capture(self) -> None:
        """Every message at its own time; worker passes after each day's last message, at the end of that day."""
        day = None
        for message in self.world.messages:
            today = local_day(message.at, self.world.zone)
            if day is not None and today != day:
                self._drain(day)
            day = today
            self.clock.now = message.at
            event = {
                "protocol_version": "1.1",
                "source_event_key": message.key,
                "source_revision": 1,
                "origin": message.origin,
                "role": {"human_direct": "user", "assistant_visible": "assistant"}.get(message.origin, "tool"),
                "content": message.text,
                "occurred_at": message.at,
                "recorded_at": message.at,
                "time_precision": "instant",
                "capture_state": "complete",
                "evidence_refs": [],
            }
            context = self._context(message.entry, message.session, message.origin)
            receipt = self.core.record_event(context, event, scope_id=message.scope, remaining_seconds=10)
            self.key_of.update((written.ref, message.key) for written in receipt.event_refs)
        if day is not None:
            self._drain(day)
        self.clock.now = self.world.now
        self._drain(None)

    def _drain(self, day) -> None:
        """Worker passes until the queue is idle, at the end of ``day`` (the world's now for ``None``)."""
        if day is not None:
            end = datetime.combine(day, datetime.min.time(), self.world.zone) + timedelta(hours=23, minutes=50)
            self.clock.now = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _round in range(50):
            receipt = self.core.drain_worker(
                self.worker,
                owner_id="bench-worker",
                consolidation=self.model,
                embed=self.embed,
                purge=self.purge,
                max_items=64,
                remaining_seconds=60,
            )
            if receipt.processed == 0:
                return
        raise RuntimeError(f"the worker did not run idle at the end of {day}")

    def ask(self, question: Question, path: str) -> Answer:
        request, call = PATHS[path]
        session = f"ask:{path}:{question.id}"
        seen_before = None
        if question.revoked:
            # The same entry and session a moment earlier, while it still read the revoked scopes: unless they showed
            # then, not showing after proves nothing.
            earlier = self._recall(question, request, call, session, question.reads | question.revoked)
            carried = (key for item in earlier["items"] for key in self._carried(item))
            seen_before = any(self.scope_of.get(key) in question.revoked for key in carried)
        started = time.perf_counter()
        packet = self._recall(question, request, call, session, question.reads)
        ms = (time.perf_counter() - started) * 1000
        found = {(c.kind, c.ref, c.revision): c.source for c in self.last_result.candidates} if self.last_result else {}
        items = packet["items"]
        # A procedure is a claim in the store; the packet names it apart.
        kinds = ("claim" if item["kind"] == "procedure" else item["kind"] for item in items)
        return Answer(
            packet,
            tuple(credits(item, question.gold, self.key_of, self.frames) for item in items),
            tuple(self._carried(item) for item in items),
            tuple(self._scope(item) for item in items),
            tuple(found.get((kind, item["ref"], item["revision"])) for kind, item in zip(kinds, items, strict=True)),
            envelope_tokens(packet),
            ms,
            seen_before,
        )

    def _recall(self, question: Question, request: dict, call: dict, session: str, reads: frozenset[str]) -> dict:
        context = self._context(question.entry, session, "human_direct", reads)
        body = {"protocol_version": "1.1", "request_id": session[:100], "query": question.text, **request}
        self.last_result = None
        return dict(self.core.recall_packet(context, body, zone=self.world.zone, **call))

    def _scope(self, item: dict) -> str | None:
        """The scope an item is held in itself: its message's, or its claim's as the store holds it (read once per
        claim, under the worker's context, which reads every scope).  None for any other kind, which no packet here
        holds: one would count as a leak until this learns where it is held."""
        if item["kind"] == "event":
            return self.scope_of.get(self.key_of.get(item["ref"], "?"))
        if item["kind"] not in {"claim", "procedure"}:
            return None
        if item["ref"] not in self.claim_scopes:
            versions = self.core.claim_history(self.worker, item["ref"])
            self.claim_scopes[item["ref"]] = versions[0].scope_id if versions else None
        return self.claim_scopes[item["ref"]]

    def _carried(self, item: dict) -> frozenset[str]:
        """The messages an item stands on (a message's evidence is itself).  A ref no message of the world was
        captured as reads as ``?``, which the leak count takes for another scope's."""
        return frozenset(self.key_of.get(ref.rsplit("@", 1)[0], "?") for ref in item["evidence_refs"])

    def qualification(self) -> dict[str, dict[str, int]]:
        """For each kind of message a question names (its answer, each kind of hard negative): how many of them a
        claim drawn from them made a fact (``active``), how many only had claims left unqualified, and how many had
        none.  Every claim version the store holds is read, whatever its slot."""
        roles = message_roles(self.world)
        states: dict[str, set[str]] = {}
        for obj in self._claim_versions():
            state = dict(obj.metadata).get("state")
            for ref in obj.evidence_refs:
                states.setdefault(self.key_of.get(ref.rsplit("@", 1)[0], "?"), set()).add(state)
        table: dict[str, Counter] = {}
        for key, role in roles.items():
            found = states.get(key, set())
            table.setdefault(role, Counter())[
                "active" if "active" in found else "unqualified" if found else "none"
            ] += 1
        return {role: dict(sorted(counts.items())) for role, counts in sorted(table.items())}

    def active_claims_on(self) -> dict[str, int]:
        """For each kind of message a question names: how many active claims cite one as evidence, each claim
        counted once however many of its versions do.  Every version the store holds is read, in every slot and with
        no cap; for the ``NEVER_FACTS`` kinds this is what the baseline caps."""
        roles = message_roles(self.world)
        claims: dict[str, set[str]] = {role: set() for role in roles.values()}
        for obj in self._claim_versions():
            if dict(obj.metadata).get("state") != "active":
                continue
            for ref in obj.evidence_refs:
                role = roles.get(self.key_of.get(ref.rsplit("@", 1)[0], "?"))
                if role is not None:
                    claims[role].add(obj.ref)
        return {role: len(refs) for role, refs in sorted(claims.items())}

    def _claim_versions(self):
        """Every version of every claim, paged through ``MemoryCore.collection`` in history mode under the worker's
        context, which reads every scope.  A page cut short at its deadline before it examined a claim names no next
        page, as the last page does, and its coverage cannot tell them apart (every page after the first is
        ``partial``); so a page without a next one that took the whole deadline on the store's clock fails the walk,
        since one that stops early counts short and lets more hard negatives through.  A slow page that names the
        next one is resumed from it."""
        query = CollectionQuery(
            "claim",
            page_size=100,
            memory_epoch=self.core.memory_epoch(self.worker),
            scope_digest=scope_digest(self.worker),
            mode="history",
        )
        cursor = None
        while True:
            started = self.core.clock.monotonic()
            page = self.core.collection(self.worker, query, cursor=cursor, deadline_seconds=CLAIM_PAGE_SECONDS)
            if page.next_cursor is None and self.core.clock.monotonic() - started >= CLAIM_PAGE_SECONDS:
                raise RuntimeError(
                    f"the claim walk's last page took its whole deadline ({CLAIM_PAGE_SECONDS} s on the store's"
                    " clock) and names no next page: the collection may have cut it short, and the counts with it"
                )
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def close(self) -> None:
        self.companion.close()


# ---------------------------------------------------------------- scoring


@dataclass(frozen=True)
class Outcome:
    """What one question got on one path; positions are 1-based.  ``ms`` comes from the latency pass, when it ran."""

    ranks: tuple[int | None, ...]
    first: int | None
    channel: str | None
    ndcg: float | None
    negatives: tuple[tuple[str, int | None], ...]
    leaks: int
    answerability: str
    tokens: int
    items: tuple[str, ...]
    seen_before: bool | None
    ms: float | None = None

    @property
    def abstained(self) -> bool:
        return self.answerability == "unknown"


def outcome(question: Question, answer: Answer, scope_of: dict[str, str]) -> Outcome:
    """Where the answer's parts and each kind of hard negative first came, and what came from outside the reads: an
    item held in a scope the entry does not read, or standing on a message of one, is a leak and answers nothing."""
    inside = tuple(scope in question.reads for scope in answer.scopes)
    credited = tuple(keys if held else frozenset() for keys, held in zip(answer.credited, inside, strict=True))

    def first_where(test) -> int | None:
        return next((place for place in range(1, len(credited) + 1) if test(place - 1)), None)

    first = first_where(lambda index: credited[index])
    kinds = sorted({kind for kind, _key in question.negatives})
    labels = {key: kind for kind, key in question.negatives}

    def label(index: int) -> str:
        if credited[index]:
            return "gold"
        return "+".join(sorted({labels.get(key, "other") for key in answer.carried[index]})) or "other"

    return Outcome(
        ranks=tuple(first_where(lambda index, key=key: key in credited[index]) for key in question.gold),
        first=first,
        channel=answer.channels[first - 1] if first else None,
        ndcg=ndcg(credited, len(question.gold)) if question.gold else None,
        negatives=tuple(
            (kind, first_where(lambda index, kind=kind: any(labels.get(key) == kind for key in answer.carried[index])))
            for kind in kinds
        ),
        leaks=sum(
            not held or any(scope_of.get(key) not in question.reads for key in carried)
            for held, carried in zip(inside, answer.carried, strict=True)
        ),
        answerability=answer.packet["answerability"],
        tokens=answer.tokens,
        items=tuple(f"{label(index)}:{answer.channels[index]}" for index in range(len(credited))),
        seen_before=answer.seen_before,
    )


def _share(flags) -> float | None:
    flags = list(flags)
    return round(sum(flags) / len(flags), 4) if flags else None


def _percentile(values, share: float) -> float | None:
    """Nearest rank: the smallest value at least ``share`` of them do not exceed."""
    ordered = sorted(value for value in values if value is not None)
    return round(ordered[max(0, math.ceil(share * len(ordered)) - 1)], 1) if ordered else None


def _above(outcome: Outcome, kind: str) -> bool | None:
    """Whether a negative of ``kind`` came above the answer, or came without it; None when the question has none."""
    for name, rank in outcome.negatives:
        if name == kind:
            return rank is not None and (outcome.first is None or rank < outcome.first)
    return None


def metrics(pairs: list[tuple[Question, Outcome]]) -> dict:
    """One block of the report: a task's questions on one path, or all of them."""
    answerable = [o for q, o in pairs if q.gold]
    unanswerable = [o for q, o in pairs if not q.gold]
    block: dict = {"questions": len(pairs)}
    if answerable:
        for cutoff in CUTOFFS:
            parts = [sum(r is not None and r <= cutoff for r in o.ranks) / len(o.ranks) for o in answerable]
            block[f"recall@{cutoff}"] = round(sum(parts) / len(parts), 4)
        block["mrr"] = round(mean_reciprocal_rank([o.first for o in answerable]), 4)
        block["ndcg@10"] = round(sum(o.ndcg for o in answerable) / len(answerable), 4)
        block["answered"] = _share(not o.abstained for o in answerable)
        block["first_found_by"] = dict(sorted(Counter(o.channel or "none" for o in answerable).items()))
    if unanswerable:
        block["abstained"] = _share(o.abstained for o in unanswerable)
    if answerable and unanswerable:
        block["abstention_balanced_accuracy"] = round((block["answered"] + block["abstained"]) / 2, 4)
    kinds = sorted({name for _q, o in pairs for name, _rank in o.negatives})
    above = {kind: _share(flag for _q, o in pairs if (flag := _above(o, kind)) is not None) for kind in kinds}
    if "outdated" in above:
        block["outdated_above_current"] = above.pop("outdated")
    if above:
        block["negative_above_answer"] = above
    block["answerability"] = dict(sorted(Counter(o.answerability for _q, o in pairs).items()))
    block["leaks"] = sum(o.leaks for _q, o in pairs)
    block["packet_tokens_p50"] = _percentile((o.tokens for _q, o in pairs), 0.5)
    block["packet_tokens_p95"] = _percentile((o.tokens for _q, o in pairs), 0.95)
    block["latency_ms_p50"] = _percentile((o.ms for _q, o in pairs), 0.5)
    block["latency_ms_p95"] = _percentile((o.ms for _q, o in pairs), 0.95)
    return block


def report(world: World, store: BenchStore, outcomes: dict[str, list[Outcome]], claims: dict) -> dict:
    """The report of one run.  ``claims`` is what the store's claims came to (``qualification`` and
    ``active_claims_on``), read before any latency pass, on the store's own clock."""
    paths = {}
    for path, results in outcomes.items():
        pairs = list(zip(world.questions, results, strict=True))
        paths[path] = {"all": metrics(pairs)} | {
            task: metrics([(q, o) for q, o in pairs if q.task == task]) for task in TASKS
        }
    status = store.core.status(store.worker)
    revoked = [o.seen_before for results in outcomes.values() for o in results if o.seen_before is not None]
    return {
        "split": world.split,
        "seed": world.seed,
        "scale": world.scale,
        "world": world.fingerprint(),
        "vectors": store.core.vectors is not None,
        "store": {
            "messages": len(world.messages),
            "questions": len(world.questions),
            "proposed_claims": sum(len(claims) for claims in world.claims.values()),
            "pending_work": status.pending_work,
            "failed_work": status.failed_work,
            "build_seconds": round(store.build_seconds, 1),
        },
        "qualification": claims["qualification"],
        "active_claims_on": claims["active_claims_on"],
        "leaks": sum(blocks["all"]["leaks"] for blocks in paths.values()),
        "revoked_seen_before": {"asked": len(revoked), "seen": sum(revoked)},
        "paths": paths,
        "questions": [
            {
                "id": q.id,
                "task": q.task,
                "form": q.form,
                "entry": q.entry,
                "text": q.text,
                **{path: _detail(results[index]) for path, results in outcomes.items()},
            }
            for index, q in enumerate(world.questions)
        ],
    }


def _detail(outcome: Outcome) -> dict:
    return {
        "first": outcome.first,
        "ranks": list(outcome.ranks),
        "answerability": outcome.answerability,
        "leaks": outcome.leaks,
        "items": list(outcome.items),
    }


def run(split: str = "dev", *, scale: int = 1, vectors: bool = True, latency: bool = True) -> dict:
    """Build the world's store in a temp directory, ask every question on both paths and report; with ``latency``,
    ask them all again on the real clock for their latency."""
    world = build_world(split, scale=scale)
    root = Path(tempfile.mkdtemp(prefix="scope-recall-bench-"))
    try:
        store = BenchStore(world, root, vectors=vectors)
        try:
            outcomes = {
                path: [outcome(q, store.ask(q, path), store.scope_of) for q in world.questions] for path in PATHS
            }
            # Counted on the store's own clock, as everything scored is: the latency pass leaves it on the real one.
            claims = {"qualification": store.qualification(), "active_claims_on": store.active_claims_on()}
            if latency:
                store.clock.realtime = True
                outcomes = {
                    path: [replace(o, ms=store.ask(q, path).ms) for q, o in zip(world.questions, results, strict=True)]
                    for path, results in outcomes.items()
                }
            return report(world, store, outcomes, claims)
        finally:
            store.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- the baseline


def gated(report: dict) -> dict:
    """The numbers the baseline holds, per path and block."""
    return {
        path: {
            name: {metric: block[metric] for metric in GATED if block.get(metric) is not None}
            for name, block in blocks.items()
        }
        for path, blocks in report["paths"].items()
    }


def facts(report: dict) -> dict[str, int]:
    """How many active claims stand on hard negatives of each ``NEVER_FACTS`` kind."""
    return {kind: report["active_claims_on"].get(kind, 0) for kind in NEVER_FACTS}


def regressions(report: dict, baseline: dict) -> list[str]:
    """Each gated metric further than the tolerance on the wrong side of its recorded value, and each kind of hard
    negative more active claims stand on than recorded."""
    if report["world"] != baseline["world"]:
        return [f"the world is not the one {BASELINE.name} was measured on: the templates or the generator changed"]
    problems = [
        f"active claims standing on {kind} messages: {count} (baseline {baseline['facts'].get(kind, 0)})"
        for kind, count in facts(report).items()
        if count > baseline["facts"].get(kind, 0)
    ]
    now = gated(report)
    for path, blocks in baseline["metrics"].items():
        for name, recorded in blocks.items():
            for metric, value in recorded.items():
                current = now.get(path, {}).get(name, {}).get(metric)
                worse = None if current is None else (current - value if metric in LOWER_IS_BETTER else value - current)
                if worse is None or worse > baseline["tolerance"]:
                    problems.append(f"{path} {name} {metric}: {current} (baseline {value})")
    return problems


def record_baseline(report: dict) -> None:
    payload = {
        "about": "The dev split at scale 1, measured by recall_bench.py; recall_bench.py --update rewrites it.",
        "split": report["split"],
        "scale": report["scale"],
        "world": report["world"],
        "tolerance": TOLERANCE,
        "facts": facts(report),
        "metrics": gated(report),
    }
    # Replaced whole: a reader or a crash never sees half a baseline, and a hard link to the old one keeps the old one.
    handle = tempfile.NamedTemporaryFile(dir=BASELINE.parent, prefix=".baseline-", suffix=".json", delete=False)
    try:
        with handle:
            handle.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        os.replace(handle.name, BASELINE)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def is_baseline(path: Path, baseline: Path = BASELINE) -> bool:
    """Whether ``path`` names the baseline file: by its resolved name, or, where both exist, as the same file under
    another name (a hard link)."""
    if path.resolve() == baseline.resolve():
        return True
    return path.exists() and baseline.exists() and os.path.samefile(path, baseline)


# ---------------------------------------------------------------- the summary


def summary(report: dict) -> str:
    store, revoked = report["store"], report["revoked_seen_before"]
    lines = [
        f"recall bench: {report['split']} split, scale {report['scale']},"
        f" vectors {'on' if report['vectors'] else 'off'}"
        f" | {store['messages']} messages, {store['questions']} questions, built in {store['build_seconds']} s",
        f"cross-scope items: {report['leaks']} | revoked scopes seen before revocation: {revoked['seen']} of"
        f" {revoked['asked']}",
        "messages whose claims became facts: "
        + ", ".join(
            f"{role} {counts.get('active', 0)}/{sum(counts.values())}"
            for role, counts in report["qualification"].items()
        ),
        "active claims standing on them: " + ", ".join(f"{k} {n}" for k, n in report["active_claims_on"].items()),
    ]
    columns = ("recall@1", "recall@3", "recall@6", "recall@10", "mrr", "ndcg@10")
    for path, blocks in report["paths"].items():
        lines += [
            "",
            f"{path:<17}{'r@1':>6}{'r@3':>6}{'r@6':>6}{'r@10':>6}{'mrr':>6}{'ndcg':>6}{'old>new':>8}{'answ':>6}"
            f"{'abst':>6}{'leaks':>6}{'tokens p50/95':>15}{'ms p50/95':>12}",
        ]
        for name, block in blocks.items():
            lines.append(
                f"{name:<17}{''.join(f'{_cell(block.get(metric)):>6}' for metric in columns)}"
                f"{_cell(block.get('outdated_above_current')):>8}"
                f"{_cell(block.get('answered')):>6}{_cell(block.get('abstained')):>6}"
                f"{block['leaks']:>6}{_count(block['packet_tokens_p50']):>9}/{_count(block['packet_tokens_p95']):<5}"
                f"{_count(block['latency_ms_p50']):>7}/{_count(block['latency_ms_p95']):<4}"
            )
        everything = blocks["all"]
        lines.append(f"abstention balanced accuracy: {_cell(everything.get('abstention_balanced_accuracy'))}")
        lines.append("first answer found by: " + _counts(everything.get("first_found_by", {})))
        lines.append("negative above the answer: " + _counts(everything.get("negative_above_answer", {})))
    return "\n".join(lines)


def _cell(value) -> str:
    return "-" if value is None else f"{value:.2f}"


def _count(value) -> str:
    return "-" if value is None else f"{value:.0f}"


def _counts(values: dict) -> str:
    return ", ".join(f"{name} {value}" for name, value in values.items()) or "-"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "heldout"), default="dev")
    parser.add_argument("--scale", type=int, default=1, help="1 is the CI size; 2 doubles the days and the questions")
    parser.add_argument("--no-vectors", action="store_true", help="recall without the vector channel")
    parser.add_argument("--no-latency", action="store_true", help="skip the second pass that measures latency")
    parser.add_argument("--json", help="also write the full report, every question included, to this file")
    parser.add_argument("--update", action="store_true", help="record this run in baseline.json (dev, scale 1)")
    parser.add_argument("--allow-worse", action="store_true", help="with --update: record numbers below the baseline")
    args = parser.parse_args()
    measured_as_baseline = (args.split, args.scale, args.no_vectors) == ("dev", 1, False)
    if args.update and not measured_as_baseline:
        parser.error("the baseline is the dev split at scale 1, with vectors")
    if args.json and is_baseline(Path(args.json)):
        parser.error(f"--json would overwrite {BASELINE.name}; --update is what records it")
    result = run(args.split, scale=args.scale, vectors=not args.no_vectors, latency=not args.no_latency)
    print(summary(result))
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    problems = regressions(result, baseline) if baseline is not None and measured_as_baseline else []
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.update:
        return update_baseline(result, baseline, problems, allow_worse=args.allow_worse)
    if problems:
        print("\nbelow the baseline:\n  " + "\n  ".join(problems))
    return 1 if problems or result["leaks"] else 0


def update_baseline(result: dict, baseline: dict | None, problems: list[str], *, allow_worse: bool) -> int:
    """Record ``result`` as the baseline, unless it has a leak or, on the same world, falls below it unasked."""
    if result["leaks"]:
        # Never recorded, whatever else is asked: a baseline is only taken of a store that keeps its scopes apart.
        print(f"\nnot recorded: {result['leaks']} items came from a scope the asking entry does not read")
        return 1
    # A changed world has nothing to be worse than; on the same world, recording a drop is asked for in words.
    if problems and baseline is not None and result["world"] == baseline["world"] and not allow_worse:
        print("\nnot recorded, below the baseline (--allow-worse records it):\n  " + "\n  ".join(problems))
        return 1
    record_baseline(result)
    print(f"\nrecorded in {BASELINE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
