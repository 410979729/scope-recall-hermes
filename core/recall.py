"""The one production retrieval pipeline used by automatic and tool recall."""

from __future__ import annotations

import inspect
import sqlite3
import time
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Protocol

from ..contracts import ContractError, utc_timestamp
from .background_context import background_candidates, current_task_candidate
from .claim_storage import parse_source_ref
from .claims import canonical_time
from .coverage import note_truncation
from .duplicate_collapse import DistinctContent, note_duplicates
from .events import lexical_terms
from .owner_view import OwnerView, owner_names, owner_view
from .recall_budget import estimate_tokens, event_admission_order
from .recall_needs import CHOICE_MARKERS, directed_followup_query, evidence_roots, mentions, unmet_needs
from .recall_policy import (
    RecallPolicy,
    asks_without_answering,
    identifiers_compatible,
    meaningful_query_terms,
    rrf_score,
    same_message,
)
from .recall_scope import asks_what_was_said, query_scope
from .retrieval import (
    CandidateRef,
    CollectionQuery,
    RetrievalResult,
    RetrievedObject,
    SearchContext,
    SearchLimits,
    effective_limits,
    optional_json,
)
from .retrieval_storage import CollectionPage, RetrievalStorage
from .vector_failure import vector_failure_label

#: Ceiling on the candidates hydrated after the deadline is already gone.
#:
#: Without a floor, an optional channel that overruns its allowance would empty
#: the packet: the lexical, exact and recent candidates are all in hand, and
#: the hydrate loop would abandon every one of them because the clock is gone.
#: The floor is the caller's own ``max_items`` rather than a fixed number,
#: because hydrating three when the packet holds eight just moves the emptiness
#: one stage later.  This ceiling bounds what a large ``max_items`` can cost
#: once the deadline is gone -- each one is a single bounded storage read, and
#: the overrun is still reported, so nothing is hidden.
MINIMUM_HYDRATION_CAP = 16
#: Claims offered per round.  A packet holds at most 30 items and a default one
#: six; a claim that answers competes for those slots on rank, so a deep pool
#: of claims would only spend hydration on statements that cannot be shown.
CLAIM_CANDIDATES = 16
#: Share of its fusion score context-only evidence keeps in live modes: a bare
#: question, or a reply restating what a recall tool returned in its turn.
#: Asked again, a question can return five earlier questions like it ahead of the
#: reply that answered one, and a report of a recall test can come back ahead of
#: the evidence it quoted.  At 0.6 a first-ranked one falls below a reply that
#: ranked in the mid-teens, and still ranks as context when nothing answers.
CONTEXT_ONLY_WEIGHT = 0.6
#: Meaningful terms a query needs before an older copy of it leads to what that copy was told
#: (``RetrievalPipeline._expand``).  A short command sent again asks nothing an old turn answered.  Chinese terms are
#: overlapping character pairs, so "继续执行" holds three and "按你说的做" four, and three would let both bring back
#: every old turn they opened; five is about six characters.  Of the owner's prompts over two weeks, 33 of the 115 with
#: fewer had an exact older copy; of the owner's real questions, 167 of 173 have five.
ECHO_TURN_MIN_TERMS = 5
#: SQLite virtual-machine steps between two looks at the recall's deadline while a candidate statement runs: one
#: every 10-60 ms of statement time.  Each look takes the GIL back from the statement, and beside a busy thread (the
#: MCP server's hook threads, a gateway's) waits for it: one every 10,000 steps made a 25 ms statement take 1.4 s.
_INTERRUPT_STEPS = 1_000_000
#: ``vector_owner`` is the vector channel's search for a question that names the owner, asked in their first person
#: (``core/owner_view.py``); it has slots of its own, so the question as asked keeps all of the channel's.
_CHANNELS = ("scoped", "exact", "lexical", "claim", "recent", "vector", "vector_owner")
#: Messages the scoped channel offers: a day of one entry, for a question that asks what was said then
#: (``recall_scope``).
SCOPED_CANDIDATES = 12
#: What a scoped candidate's fused score is multiplied by.  The question asked only what was said on its day: that
#: day's messages go before a candidate one channel found elsewhere by the question's words or the entry's name,
#: which on a shared store would fill the packet ("Claude Code" is in thousands of other messages).  Three puts a
#: first rank above any candidate two channels agree on at their first rank.
SCOPED_WEIGHT = 3.0
#: Vector admission reasons that are reported, and how; the rest are silent.
_VECTOR_REJECTION_GAPS = {
    "embedding_space_mismatch": "vector_old_or_mismatched_space",
    "vector_threshold_unconfigured": "vector_threshold_unconfigured",
}
_VECTOR_SILENT_REJECTIONS = frozenset({"vector_below_threshold", "vector_id_missing", "vector_score_invalid"})


def _takes_also(prefetch) -> bool:
    """Whether a vector port's ``prefetch_query`` asks for other readings of the question in the same request."""
    try:
        parameters = inspect.signature(prefetch).parameters
    except (TypeError, ValueError):
        return False
    return "also" in parameters or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())


def _asked_early(prefetched) -> tuple[str, ...]:
    """The questions an embedding asked for as the recall started holds: one, or several asked together."""
    if prefetched is None:
        return ()
    return tuple(getattr(prefetched, "queries", None) or (prefetched.query,))


class RetrievalClock(Protocol):
    def monotonic(self) -> float: ...


class ChannelBudget:
    """Candidate slots left per channel across follow-up rounds.

    A round may not spend what later rounds need: ``hold`` reserves one slot
    per remaining round in every channel so a directed follow-up always has
    room, even after rejected-by-dedup work consumed the first round.
    """

    def __init__(self, limits: SearchLimits) -> None:
        self.left = {
            "scoped": SCOPED_CANDIDATES,
            "exact": limits.candidate_pool,
            "lexical": limits.candidate_pool,
            "claim": min(CLAIM_CANDIDATES, limits.candidate_pool),
            "recent": limits.recent_items,
            "vector": limits.vector_limit,
            "vector_owner": limits.vector_limit,
        }
        self.total = sum(self.left.values())
        self.reserve = dict.fromkeys(_CHANNELS, 0)

    def hold(self, future_rounds: int) -> None:
        # A follow-up round never reads the day a question names (``RetrievalPipeline._collect_rounds``).
        self.reserve = {
            channel: 0 if channel == "scoped" else min(future_rounds, self.left[channel]) for channel in _CHANNELS
        }

    def allowance(self, channel: str, limit: int) -> int:
        free_total = self.total - sum(self.reserve.values())
        return min(limit, max(0, self.left[channel] - self.reserve[channel]), max(0, free_total))

    def spend(self, channel: str, count: int) -> None:
        self.left[channel] -= count
        self.total -= count


def _empty_result(context: SearchContext, gap: str) -> RetrievalResult:
    return RetrievalResult((), (), None, (gap,), "unknown", "unknown", 0, 0, request_id=context.request_id)


def _in_context(tx, obj: RetrievedObject, context: SearchContext) -> bool:
    """Whether everything ``obj`` stands on is this session's own and was said since ``context.in_context_since``,
    so that the asking host's model still holds it word for word.  Anything else is not: an item without evidence,
    one with any evidence of another session, from before that moment, or that cannot be read."""
    if not obj.evidence_refs or context.in_context_since is None:
        return False
    session = context.trusted_context.session_id
    for ref in obj.evidence_refs:
        try:
            source = tx.source(*parse_source_ref(ref))
            said = utc_timestamp(source.event.get("occurred_at")) if source is not None else None
        except ContractError:
            return False
        if source is None or source.session_id != session or said is None or said < context.in_context_since:
            return False
    return True


def _says_only_its_state(content: str) -> bool:
    """Whether an episode body is the placeholder a never-consolidated resume leaves."""
    payload = optional_json(content)
    return isinstance(payload, dict) and set(payload) <= {"state"}


def _statement_text(obj: RetrievedObject) -> str:
    """What a claim asserts, for term matching; other kinds match on content."""
    if obj.kind != "claim":
        return obj.content
    payload = optional_json(obj.content)
    if not isinstance(payload, dict):
        return obj.content
    return " ".join(str(payload.get(key, "")) for key in ("subject", "predicate", "value_text", "conditions"))


def _said_after(obj: RetrievedObject, moment: str) -> bool:
    """Whether ``obj`` was said after ``moment`` (``canonical_time``), as far as its time tells: an event's own, a
    claim's newest evidence's.  Without one it is taken as said before."""
    stamp = dict(obj.metadata).get("occurred_at")
    try:
        return (canonical_time(stamp) or "") > moment
    except ContractError:
        return False


#: See ``RetrievalPipeline.RELATION_WEIGHT``.
RELATION_WEIGHT = 0.5


@dataclass
class _Aside:
    """What hydration sets aside rather than delivers (``RetrievalPipeline._hydrate_admit``): older copies of the
    current message, which lead to their turn's replies, and what the asking model still holds, which leads on to
    what is related to it."""

    echoes: list[CandidateRef] = field(default_factory=list)
    held: list[CandidateRef] = field(default_factory=list)


class RetrievalPipeline:
    """Collect, hydrate, admit, and rank candidates in one read-only pass."""

    def __init__(
        self,
        storage,
        *,
        vector_port=None,
        policy: RecallPolicy | None = None,
        clock: RetrievalClock | None = None,
        storage_reader: RetrievalStorage | None = None,
    ):
        self.storage = storage
        self.vector_port = vector_port
        self.policy = policy if policy is not None else RecallPolicy(vector_threshold=None)
        self.clock = clock if clock is not None else time
        self.storage_reader = storage_reader if storage_reader is not None else RetrievalStorage(clock=self.clock)
        if hasattr(self.storage_reader, "clock"):
            self.storage_reader.clock = self.clock

    def _remaining(self, context: SearchContext) -> float:
        return context.deadline - self.clock.monotonic()

    # -- candidate channels ---------------------------------------------------

    def _prefetch_query(self, context: SearchContext):
        """Ask the vector port for the query's embedding before the SQLite channels run, if it can take one early.

        Asked for after them, the embedding would have three quarters of what they left (runtime/instance.py
        ``_QueryEmbedding``).  A port that cannot start one early, or fails to, embeds in its search instead.  A
        question that names the owner is asked for in their first person as well, in the same request where the port
        takes several (``also``): one after the other, the second would wait for the first.
        """
        if self.vector_port is None or context.limits.vector_limit == 0:
            return None
        prefetch = getattr(self.vector_port, "prefetch_query", None)
        if not callable(prefetch):
            return None
        view = self._owner_view(context)
        try:
            if view is not None and _takes_also(prefetch):
                return prefetch(context, also=(view.first,))
            return prefetch(context)
        except Exception:
            return None

    def _owner_view(self, context: SearchContext) -> OwnerView | None:
        """The question as the store's owner would ask it, when it names them and asks something besides
        (``core/owner_view.py``); a question about the name alone ("Joy 是谁？") is read as asked."""
        names = owner_names(self.policy.owner_names, context.trusted_context.source_principal)
        view = owner_view(context.query, names) if names else None
        return view if view is not None and meaningful_query_terms(view.bare) else None

    def _admission_query(self, context: SearchContext) -> str:
        """The question coverage reads: without the owner's name, which their own words never carry, so that it is no
        word an answer in them is short of."""
        view = self._owner_view(context)
        return view.bare if view is not None else context.query

    def _lexically_admitted(self, candidate: CandidateRef, context: SearchContext) -> bool:
        """Admitted by the question as asked, which a reply naming the owner beside its other words still meets, or
        without the owner's name: their own words never carry it, and in a short question it would be one of the two
        words a candidate has to match.  The name alone admits nothing either way."""
        if self.policy.lexical_admission(candidate, context.query, exact=False)[0]:
            return True
        view = self._owner_view(context)
        return view is not None and self.policy.lexical_admission(candidate, view.bare, exact=False)[0]

    def _vector_candidates(
        self,
        context: SearchContext,
        gaps: list[str],
        *,
        budget: ChannelBudget | None = None,
        prefetched=None,
        channel: str = "vector",
    ) -> tuple[CandidateRef, ...]:
        if self.vector_port is None or context.limits.vector_limit == 0:
            gaps.append("vector_unavailable")
            return ()
        limit = context.limits.vector_limit
        if budget is not None:
            limit = budget.allowance(channel, limit)
        if limit <= 0:
            return ()
        remaining = self._remaining(context)
        if remaining <= 0:
            gaps.append("deadline_exceeded_vector")
            return ()
        # Keep part of the caller's deadline for SQLite hydration and the
        # packet's final authority checks if the optional vector call times out.
        vector_context = replace(context, deadline=context.deadline - remaining * 0.25)
        # The embedding asked for when the recall started is this round's only if the round kept its query.
        early = {"prefetched": prefetched} if context.query in _asked_early(prefetched) else {}
        try:
            raw = tuple(
                islice(
                    iter(
                        self.vector_port.search(
                            vector_context, limit=limit, remaining_seconds=remaining * 0.75, **early
                        )
                        or ()
                    ),
                    limit,
                )
            )
        except Exception as exc:
            # The class alone does not say what went wrong; see core/vector_failure.py.
            gaps.append("vector_unavailable")
            gaps.append(f"vector_error:{vector_failure_label(exc)}")
            return ()
        if budget is not None:
            budget.spend(channel, len(raw))
        admitted = []
        for rank, candidate in enumerate(raw, 1):
            if candidate.rank != rank:
                candidate = replace(candidate, rank=rank)
            accepted, reason = self.policy.vector_admission(candidate)
            if accepted:
                admitted.append(candidate)
                continue
            gap = _VECTOR_REJECTION_GAPS.get(reason)
            if gap is None and reason not in _VECTOR_SILENT_REJECTIONS:
                gap = f"vector_rejected:{reason}"
            if gap is not None:
                gaps.append(gap)
        return tuple(admitted)

    def _admit(self, candidate: CandidateRef, context: SearchContext) -> bool:
        if candidate.kind == "event" and f"{candidate.ref}@{candidate.revision}" in context.current_source_refs:
            return False
        if candidate.source == "vector":
            return self.policy.vector_admission(candidate)[0]
        if candidate.source in {"exact_ref", "relation", "claim_lexical", "scoped"}:
            # A claim candidate already passed its own statement rule in storage; a scoped one is a message of
            # the day and entry the question names.
            return True
        return self._lexically_admitted(candidate, context)

    def _fuse_candidates(
        self, admitted: list[CandidateRef], seen: set[tuple[str, str, int]] | None
    ) -> tuple[CandidateRef, ...]:
        by_key: dict[tuple[str, str, int], list[CandidateRef]] = {}
        for candidate in admitted:
            if seen is not None and candidate.key in seen:
                continue
            by_key.setdefault(candidate.key, []).append(candidate)
        seeds: list[CandidateRef] = []
        for _key, signals in sorted(by_key.items()):
            representative = min(signals, key=lambda item: (item.rank, item.source))
            fusion = rrf_score((item.rank for item in signals), k=self.policy.rrf_k)
            scoped = [item for item in signals if item.source == "scoped"]
            if scoped:
                # A message of the day the question named stays one, whichever channel ranked it higher; a ref the
                # caller named keeps its place first.
                fusion *= SCOPED_WEIGHT
                if representative.source != "exact_ref":
                    representative = scoped[0]
            seeds.append(replace(representative, fusion_score=fusion))
        return tuple(seeds)

    def _collect(
        self,
        tx,
        context: SearchContext,
        gaps: list[str],
        *,
        seen: set[tuple[str, str, int]],
        budget: ChannelBudget,
        prefetched=None,
    ) -> tuple[CandidateRef, ...]:
        """One round of exact, lexical, recent and vector candidates, fused by identity."""
        if self._remaining(context) <= 0:
            gaps.append("deadline_exceeded_collect")
            return ()
        raw: list[CandidateRef] = []

        def admitted() -> tuple[CandidateRef, ...]:
            # Deadline exits still pass every collected candidate through the
            # same current-source and channel qualification gate.
            return self._fuse_candidates([candidate for candidate in raw if self._admit(candidate, context)], seen)

        channels = (
            # First, so a slow statement after it cannot cost the one channel that reads what the question names.
            (
                "scoped",
                getattr(self.storage_reader, "scoped", None) if context.scope is not None else None,
                SCOPED_CANDIDATES,
            ),
            ("exact", self.storage_reader.exact, context.limits.candidate_pool),
            ("lexical", self.storage_reader.lexical, context.limits.candidate_pool),
            # A reader predating the claim channel simply offers no claims.
            ("claim", getattr(self.storage_reader, "claims", None), CLAIM_CANDIDATES),
            ("recent", self.storage_reader.recent, context.limits.recent_items),
        )
        # A statement cannot see the recall's deadline: with no planner statistics the lexical one ran 18-21 s for a
        # long Telegram message, and the recall came back empty long after its deadline.  Past the
        # deadline a channel's statement is interrupted: that channel gives nothing, the gap says so, and what the
        # channels before it found still answers.  Only here: interrupted while hydrating, a recall would lose what
        # it had already found.
        check = getattr(tx, "_check", None)
        conn = check() if callable(check) else None
        interruptible = callable(getattr(conn, "set_progress_handler", None))
        if interruptible:
            conn.set_progress_handler(lambda: 1 if self._remaining(context) <= 0 else 0, _INTERRUPT_STEPS)
        try:
            for channel, loader, limit in channels:
                if loader is None:
                    continue
                if self._remaining(context) <= 0:
                    gaps.append("deadline_exceeded_collect")
                    return admitted()
                allowance = budget.allowance(channel, limit)
                if allowance <= 0:
                    continue
                values = tuple(loader(tx, context, limit=allowance))
                budget.spend(channel, len(values))
                raw.extend(values)
        except ContractError:
            raise
        except sqlite3.OperationalError as exc:
            # Only the interrupt is the deadline; a locked store past it is still reported as the error it is.
            if interruptible and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                gaps.append("deadline_exceeded_collect")
                return admitted()
            gaps.append(f"sqlite_candidate_error:{type(exc).__name__}")
        except Exception as exc:
            gaps.append(f"sqlite_candidate_error:{type(exc).__name__}")
        finally:
            if interruptible:
                conn.set_progress_handler(None, 0)
        if self._remaining(context) <= 0:
            gaps.append("deadline_exceeded_collect")
            return admitted()
        raw.extend(self._vector_candidates(context, gaps, budget=budget, prefetched=prefetched))
        view = self._owner_view(context)
        searchable = self.vector_port is not None and context.limits.vector_limit > 0
        if view is not None and searchable and self._remaining(context) > 0:
            # The question as the owner would ask it, beside the question as asked: their own words answer it and never
            # carry their name, while the question as asked finds what names them.  One candidate the two searches
            # agree on ranks by both.
            owned = replace(context, query=view.first)
            raw.extend(
                self._vector_candidates(owned, gaps, budget=budget, prefetched=prefetched, channel="vector_owner")
            )
        return admitted()

    # -- hydration and admission ----------------------------------------------

    def _hydrate_admit(
        self,
        tx,
        candidate: CandidateRef,
        context: SearchContext,
        *,
        original_query: str | None = None,
        aside: _Aside | None = None,
    ) -> RetrievedObject | None:
        obj = self.storage_reader.hydrate(tx, candidate, context)
        if obj is None:
            return None
        # The current message already supplies this text. Older copies add no
        # information to automatic context and can crowd out its actual evidence.
        # What such a copy was told in its turn still answers it: the copy is set
        # aside in ``aside.echoes`` to lead to its turn's replies, never delivered.
        if context.mode == "auto" and obj.kind == "event":
            query = context.query if original_query is None else original_query
            if same_message(obj.content, query):
                if aside is not None:
                    aside.echoes.append(candidate)
                return None
        # A message of the day a question named was chosen by its day and entry: the entry's name ("pc2") is not an
        # identifier it has to repeat.  Every other candidate still has to.
        asked = context.scope.rest if context.scope is not None and candidate.source == "scoped" else context.query
        if candidate.source != "exact_ref" and not identifiers_compatible(asked, obj.content):
            return None
        # An episode whose resume was never consolidated carries nothing but its
        # own state: `{"state": "unknown"}` fills a packet slot and answers
        # nothing.  Every plain chat turn opens one.  Named directly it is still
        # delivered, because then the caller asked for that object.
        if candidate.source != "exact_ref" and obj.kind == "episode" and _says_only_its_state(obj.content):
            return None
        if context.mode == "auto" and context.in_context_since is not None and _in_context(tx, obj, context):
            # The asking model still holds everything it stands on, word for word: delivered, it would be a second copy
            # in that model's context, and take a slot from what the model does not have.  Set aside in ``aside``, it
            # still leads to what is related to it, as it would if delivered: left out of that, a session's own messages
            # would stop leading to the claims and procedures drawn from it before its last compaction.
            if aside is not None:
                aside.held.append(candidate)
            return None
        return obj

    def _hydrate_all(
        self,
        tx,
        hydrated: list[tuple[CandidateRef, RetrievedObject]],
        candidates,
        context: SearchContext,
        gaps: list[str],
        *,
        floor: int,
        original_query: str | None = None,
        aside: _Aside | None = None,
    ) -> None:
        """Hydrate in rank order; once the deadline is gone, only up to ``floor`` items."""
        known = {candidate.key for candidate, _obj in hydrated}
        candidates = tuple(candidates)
        prefetch = getattr(self.storage_reader, "prefetch", None)
        if prefetch is not None and self._remaining(context) > 0:
            # What hydration reads, loaded together (``RetrievalStorage.prefetch``).
            prefetch(tx, tuple(candidate for candidate in candidates if candidate.key not in known), context)
        for candidate in candidates:
            if self._remaining(context) <= 0 and len(hydrated) >= floor:
                gaps.append("deadline_exceeded_hydrate")
                break
            if candidate.key in known:
                continue
            obj = self._hydrate_admit(tx, candidate, context, original_query=original_query, aside=aside)
            if obj is not None:
                hydrated.append((candidate, obj))
                known.add(candidate.key)

    #: What an object reached by relation may score against the hit it was reached from.
    #:
    #: Scored by its place in its own seed's list, the first object related to the
    #: thirteenth hit would score 1/62, above every direct hit but the first.  Measured,
    #: a query reached about sixteen related objects, and scored that way they all ranked
    #: with the best hits: over one store's real questions the reply that answered each was in
    #: the top five for 20 of 30, against 28 with this weight, while fact recall,
    #: no-match, supersession and question recall were the same either way.  Held to the
    #: smaller of its own score and its seed's, times this, it is 28 (0.8 gives the
    #: same; letting a related object continue its seed's rank gives 27).  The price: on
    #: a second store, a related object scored by rank answers one question of 25 that
    #: this weight loses (19 against 18).  At 0.5 the best it can score is 1/124 and the
    #: 48th hit of one channel scores 1/108, so with the default pool a related object
    #: sorts after every direct hit, and among themselves they keep the order of the
    #: hits that led to them.  A turn's replies are not weighed: they are what the
    #: question was told, and are read before the hops.
    RELATION_WEIGHT = RELATION_WEIGHT

    def _expand(
        self,
        tx,
        context: SearchContext,
        seeds: tuple[CandidateRef, ...],
        gaps: list[str],
        *,
        echoes: tuple[CandidateRef, ...] = (),
    ) -> tuple[CandidateRef, ...]:
        """Bounded relation hops out of the seeds; every inspected object counts.

        ``echoes`` are older copies of the current message (``_hydrate_admit``): each leads to the replies of its turn
        like a seed, and to nothing else.  What the latest copy was told is raised once hydrated (``_raise_echo_turn``).
        """
        limits = context.limits
        if limits.relation_hops == 0 or limits.relation_objects == 0:
            return seeds
        all_candidates = list(seeds)
        seen = {candidate.key for candidate in (*seeds, *echoes)}
        frontier = list(seeds)
        inspected = 0

        def stopped(gap: str) -> tuple[CandidateRef, ...]:
            gaps.append(gap)
            return tuple(all_candidates)

        # The replies a recalled message received come before any other
        # relation, for every seed: spent seed by seed they would go to
        # whichever seeds ranked first, and a lower-ranked question would
        # never reach its answer.  Half the bound is the most they may take,
        # because a query that recalls many messages would otherwise leave
        # nothing for the claims and episodes an answer is just as often
        # reached through.  A question asked again word for word is refused
        # as an echo of the current message, and it is the only way to what
        # it was told: over the owner's real questions asked again on a
        # shared store, 42 of the 46 answers never reached were behind such a
        # copy.
        turn_replies = getattr(self.storage_reader, "turn_replies", None)
        if callable(turn_replies):
            turn_bound = max(1, limits.relation_objects // 2)
            for seed in sorted((*frontier, *echoes), key=lambda item: (-item.fusion_score, item.key)):
                if inspected >= turn_bound:
                    break
                # Checked per seed as well: a seed whose turn has no reply still costs its two look-ups.
                if self._remaining(context) <= 0:
                    return stopped("deadline_exceeded_relation")
                for candidate in turn_replies(tx, seed):
                    if inspected >= turn_bound:
                        break
                    if self._remaining(context) <= 0:
                        return stopped("deadline_exceeded_relation")
                    inspected += 1
                    if candidate.key in seen:
                        continue
                    seen.add(candidate.key)
                    if f"{candidate.ref}@{candidate.revision}" in context.current_source_refs:
                        continue
                    if self._hydrate_admit(tx, candidate, context) is None:
                        continue
                    all_candidates.append(
                        replace(candidate, fusion_score=rrf_score((candidate.rank + 1,), k=self.policy.rrf_k))
                    )

        for _hop in range(limits.relation_hops):
            next_frontier: list[CandidateRef] = []
            # The shared object bound is spent best seed first.  In key order it
            # would go to whichever refs sort lowest, often a weak match's claims.
            for seed in sorted(frontier, key=lambda item: (-item.fusion_score, item.key)):
                if inspected >= limits.relation_objects:
                    return stopped("relation_bound_reached")
                if self._remaining(context) <= 0:
                    return stopped("deadline_exceeded_relation")
                for candidate in self.storage_reader.related(tx, seed, limit=limits.relation_objects - inspected):
                    inspected += 1
                    bound_reached = inspected >= limits.relation_objects
                    if self._remaining(context) <= 0:
                        return stopped("deadline_exceeded_relation")
                    if candidate.key in seen:
                        if bound_reached:
                            return stopped("relation_bound_reached")
                        continue
                    seen.add(candidate.key)
                    if (
                        candidate.kind == "event"
                        and f"{candidate.ref}@{candidate.revision}" in context.current_source_refs
                    ):
                        continue
                    aside = _Aside()
                    admitted = self._hydrate_admit(tx, candidate, context, aside=aside) is not None
                    if admitted or aside.held:
                        own = rrf_score((candidate.rank + 1,), k=self.policy.rrf_k)
                        # A seed built by hand in a test may carry no fusion score; a real one always does.
                        bound = min(own, seed.fusion_score) if seed.fusion_score > 0 else own
                        candidate = replace(candidate, fusion_score=bound * self.RELATION_WEIGHT)
                        if admitted:
                            all_candidates.append(candidate)
                        # One the asking model holds is not delivered, and leads on as it did (``_hydrate_admit``).
                        next_frontier.append(candidate)
                    if bound_reached:
                        return stopped("relation_bound_reached")
            frontier = next_frontier
            if not frontier:
                break
        return tuple(all_candidates)

    # -- ranking and budget ---------------------------------------------------

    def _rank_hydrated(
        self, hydrated: list[tuple[CandidateRef, RetrievedObject]], context: SearchContext | None = None
    ) -> list[tuple[CandidateRef, RetrievedObject]]:
        context_only: set[tuple[str, str, int]] = set()
        if context is not None and context.mode in {"auto", "current"}:
            # A bare question, and a reply restating what a recall tool returned
            # in its turn, are context, not evidence.  The weight goes on the
            # candidate itself: budget admission re-sorts on fusion score, and a
            # short question would otherwise win its place back there.
            context_only = {
                candidate.key
                for candidate, obj in hydrated
                if obj.kind == "event"
                and candidate.source != "exact_ref"
                and (asks_without_answering(obj.content) or dict(obj.metadata).get("recall_echo") == "true")
            }
            hydrated = [
                (replace(candidate, fusion_score=candidate.fusion_score * CONTEXT_ONLY_WEIGHT), obj)
                if candidate.key in context_only
                else (candidate, obj)
                for candidate, obj in hydrated
            ]
        ranked = sorted(
            hydrated,
            key=lambda pair: (
                -pair[0].fusion_score,
                -(pair[0].vector_score if pair[0].vector_score is not None else -1.0),
                pair[0].kind,
                pair[0].ref,
                pair[0].revision,
            ),
        )
        # Only candidates that already passed hydration, scope, time and source
        # admission reach this point; scores never create permission or facts.
        # Explicit historical lookups keep their original ordering.
        if context is None or context.mode in {"history", "as_of"}:
            return ranked
        query_terms = set(meaningful_query_terms(self._admission_query(context)))
        if not query_terms:
            return ranked
        matched = {
            candidate.key: query_terms.intersection(lexical_terms(_statement_text(obj))) for candidate, obj in ranked
        }
        selected: list[tuple[CandidateRef, RetrievedObject]] = []
        covered: set[str] = set()
        selected_roots: set[str] = set()

        def score(pair):
            candidate, obj = pair
            hits = matched[candidate.key]
            roots = evidence_roots(obj)
            # A small, bounded diversity bonus favours another part of the
            # question over repeats from one source. RRF remains the base.
            relevance = len(hits) / len(query_terms)
            additional = len(hits - covered) / len(query_terms)
            repeated = bool(roots and roots <= selected_roots and not (hits - covered))
            # An earlier question or a restating reply covers the query's words
            # by construction, so its coverage bonus is weighted like its fusion.
            weight = CONTEXT_ONLY_WEIGHT if candidate.key in context_only else 1.0
            return (
                candidate.source == "exact_ref",
                candidate.fusion_score + weight * (0.008 * relevance + 0.008 * additional) - 0.006 * repeated,
            )

        while ranked:
            if self._remaining(context) <= 0:
                selected.extend(ranked)
                break
            pair = ranked.pop(max(range(len(ranked)), key=lambda index: score(ranked[index])))
            selected.append(pair)
            covered.update(matched[pair[0].key])
            selected_roots.update(evidence_roots(pair[1]))
        return selected

    def _apply_budget(
        self, ranked: list[tuple[CandidateRef, RetrievedObject]], limits: SearchLimits
    ) -> list[tuple[CandidateRef, RetrievedObject]]:
        kept: list[tuple[CandidateRef, RetrievedObject]] = []
        oversized: list[tuple[CandidateRef, RetrievedObject]] = []
        total_tokens = 0
        for candidate, item in event_admission_order(ranked, limits):
            if len(kept) >= limits.max_items:
                break
            tokens = estimate_tokens(getattr(item, "content", ""))
            if tokens > limits.budget_tokens:
                # Never charge an undeliverable item against all later hits.
                oversized.append((candidate, item))
                continue
            if kept and total_tokens + tokens > limits.budget_tokens:
                # One large candidate must not starve smaller useful evidence
                # later in the ranking. The compiler budgets the whole packet.
                continue
            total_tokens += tokens
            kept.append((candidate, item))
        # An item that alone exceeds the whole budget cannot fit in what is
        # left of it, so it is worth returning only when there is nothing else
        # to show: the expandable hint is a fallback, not a suffix.
        return (kept or oversized)[: limits.max_items]

    # -- the search itself ----------------------------------------------------

    def search(self, context: SearchContext) -> RetrievalResult:
        if not isinstance(context, SearchContext):
            raise ContractError("INPUT_INVALID", "search_context")
        limits = effective_limits(context)
        working = replace(context, limits=limits) if limits != context.limits else context
        if self._remaining(working) <= 0:
            return _empty_result(working, "deadline_exceeded")
        gaps: list[str] = []
        prefetched = self._prefetch_query(working)
        try:
            with self.storage.read(
                working.trusted_context, remaining_seconds=max(self._remaining(working), 0.001)
            ) as tx:
                epoch = self.storage_reader.epoch(tx)
                working = self._scoped(tx, working, gaps)
                aside = _Aside()
                hydrated, seed_count = self._collect_rounds(tx, working, gaps, prefetched=prefetched, aside=aside)
                if len(meaningful_query_terms(working.query)) < ECHO_TURN_MIN_TERMS:
                    aside.echoes.clear()
                self._hydrate_related(tx, working, hydrated, gaps, echoes=tuple(aside.echoes), held=tuple(aside.held))
                ranked, query_items = self._select(tx, working, hydrated, gaps)
                return self._result(working, epoch, ranked, query_items, gaps, seed_count, len(hydrated))
        except ContractError as exc:
            if exc.code == "DEADLINE_EXCEEDED":
                return _empty_result(working, "deadline_exceeded")
            return _empty_result(working, f"sqlite_unavailable:{exc.code}")
        except Exception as exc:
            return _empty_result(working, f"sqlite_unavailable:{type(exc).__name__}")

    @staticmethod
    def _scoped(tx, working: SearchContext, gaps: list[str]) -> SearchContext:
        """The context with the days and entries its question names (``recall_scope``) when it names a day and asks
        only what was said or done then; any other question has no scope.

        Reading the scope never fails a recall: a question it cannot read is recalled as if it named no day, and the
        gap says so.  A prompt grown past the search's length by normalisation would empty the whole recall.
        """
        scope = None
        try:
            if query_scope(working.query, now=working.now, zone=working.zone, entries={}) is not None:
                entries = getattr(getattr(tx, "registry", None), "entries", None)
                names = (
                    {entry_id: str(value.get("name") or entry_id) for entry_id, value in entries().items()}
                    if callable(entries)
                    else {}
                )
                scope = query_scope(working.query, now=working.now, zone=working.zone, entries=names)
                if scope is not None and not asks_what_was_said(scope.rest):
                    scope = None
        except Exception as exc:
            gaps.append(f"scope_unreadable:{type(exc).__name__}")
            scope = None
        return working if working.scope == scope else replace(working, scope=scope)

    def _collect_rounds(
        self,
        tx,
        working: SearchContext,
        gaps: list[str],
        *,
        prefetched=None,
        aside: _Aside | None = None,
    ) -> tuple[list[tuple[CandidateRef, RetrievedObject]], int]:
        """The first round plus at most one directed follow-up for an open need; what hydration sets aside on the
        way goes to ``aside``."""
        hydrated: list[tuple[CandidateRef, RetrievedObject]] = []
        seen: set[tuple[str, str, int]] = set()
        seed_count = 0
        # Enough to fill the packet the caller asked for, and no more.
        floor = min(working.limits.max_items, MINIMUM_HYDRATION_CAP)
        max_rounds = min(2, 1 + working.limits.followups)
        budget = ChannelBudget(working.limits)
        follow_query: str | None = None
        for round_index in range(max_rounds):
            if self._remaining(working) <= 0:
                gaps.append("deadline_exceeded_followup" if round_index else "deadline_exceeded")
                break
            # The day a question names is read once: a follow-up asks for something else.
            round_context = replace(working, query=follow_query, scope=None) if follow_query else working
            budget.hold(max_rounds - round_index - 1)
            seeds = self._collect(tx, round_context, gaps, seen=seen, budget=budget, prefetched=prefetched)
            seed_count += len(seeds)
            seen.update(candidate.key for candidate in seeds)
            self._hydrate_all(
                tx,
                hydrated,
                seeds,
                round_context,
                gaps,
                floor=floor,
                original_query=working.query,
                aside=aside,
            )
            if round_index + 1 >= max_rounds:
                break
            items = tuple(obj for _candidate, obj in hydrated)
            needs = unmet_needs(working.query, items)
            if not needs:
                break
            if self._remaining(working) <= 0:
                gaps.append("deadline_exceeded_followup")
                break
            follow_query = directed_followup_query(working, needs, items)
            if follow_query is None:
                break
            if "resume_state" in needs:
                task = current_task_candidate(tx, working, self.storage_reader, self.clock)
                if task is not None and task[0].key not in {candidate.key for candidate, _obj in hydrated}:
                    # This task answers an explicit resume request.
                    hydrated.append((replace(task[0], source="relation", lexical_score=1.0), task[1]))
        return hydrated, seed_count

    def _hydrate_related(
        self,
        tx,
        working: SearchContext,
        hydrated: list[tuple[CandidateRef, RetrievedObject]],
        gaps: list[str],
        *,
        echoes: tuple[CandidateRef, ...] = (),
        held: tuple[CandidateRef, ...] = (),
    ) -> None:
        # What the asking model holds is a seed as well, and never delivered (``_hydrate_admit``).
        seeds = (*(candidate for candidate, _obj in hydrated), *held)
        known = {candidate.key for candidate in (*seeds, *echoes)}
        expanded = [
            candidate
            for candidate in self._expand(tx, working, seeds, gaps, echoes=echoes)
            if candidate.key not in known
        ]
        floor = min(working.limits.max_items, MINIMUM_HYDRATION_CAP)
        self._hydrate_all(tx, hydrated, expanded, working, gaps, floor=floor)
        self._raise_echo_turn(tx, working, hydrated, echoes, gaps)

    def _raise_echo_turn(
        self,
        tx,
        working: SearchContext,
        hydrated: list[tuple[CandidateRef, RetrievedObject]],
        echoes: tuple[CandidateRef, ...],
        gaps: list[str],
    ) -> None:
        """What the same question was told the last time it was asked goes before the best candidate of its time.

        Reached as any seed's turn is, at a first rank's fixed score, it would fall below every candidate two channels
        agree on: with vectors on, one person's questions asked again lost about 25 of 124 answers in a threshold sweep.
        Only the latest copy's turn is raised, so an answer that changed since is not raised beside the one that
        replaced it.  Of that turn at most half the packet is: the replies a channel ranked highest, which say what was
        asked, then the turn's last reply, which answers it, when the turn was read to its end.  An agent's turn opens
        with what it is about to do, and raising every reply would let an old turn's first messages fill the packet, as
        raising every reply a channel found would let its narration of the subject do; the last reply read of a turn cut
        by the window is narration too.  Raising only the best found reply lost 6 of the owner's 173 questions asked
        again with vectors on, where more replies are found and the answer is not always the best of them.  They go
        above the best candidate said up to that turn, the turn's other replies included, and not above what was said
        after it and ranks higher still, which may say the answer changed.  Kept below every such candidate outranking
        the best of the turn's time, the answer fell out of the packet for 5 of the owner's 173 questions asked again: a
        store keeps growing, and something said since matches most questions.
        """
        latest_turn = getattr(self.storage_reader, "latest_turn", None)
        if not echoes or not callable(latest_turn):
            return
        if self._remaining(working) <= 0:
            gaps.append("deadline_exceeded_relation")
            return
        read = latest_turn(tx, echoes, now=working.now)
        if read is None:
            return
        opened, replies, ended = read
        at = {candidate.key: index for index, (candidate, _obj) in enumerate(hydrated)}
        found = [reply for reply in replies if reply.key in at and hydrated[at[reply.key]][0].source != "relation"]
        # At most half the packet: the replies a channel ranked highest, then the last reply of a whole turn.
        room = max(1, working.limits.max_items // 2)
        final = replies[-1] if ended else None
        by_score = [
            reply
            for reply in sorted(found, key=lambda reply: -hydrated[at[reply.key]][0].fusion_score)
            if final is None or reply.key != final.key
        ]
        raised = [*by_score[: room - (final is not None)], *([final] if final is not None else [])]
        # The turn's other replies are of its time, and stay below what is raised of it.
        of_turn, lifted = {reply.key for reply in replies}, {reply.key for reply in raised}
        others = [
            (candidate.fusion_score, candidate.key not in of_turn and _said_after(obj, opened))
            for candidate, obj in hydrated
            if candidate.key not in lifted
        ]
        best = max((score for score, after in others if not after), default=0.0)
        for index, reply in enumerate(raised):
            score = best + rrf_score((index + 2,), k=self.policy.rrf_k)
            if reply.key in at:
                candidate, obj = hydrated[at[reply.key]]
                if candidate.fusion_score < score:
                    hydrated[at[reply.key]] = (replace(candidate, fusion_score=score), obj)
            elif (
                f"{reply.ref}@{reply.revision}" not in working.current_source_refs
                and (obj := self._hydrate_admit(tx, reply, working)) is not None
            ):
                hydrated.append((replace(reply, fusion_score=score), obj))

    def _select(
        self, tx, working: SearchContext, hydrated: list[tuple[CandidateRef, RetrievedObject]], gaps: list[str]
    ):
        """Rank, fold duplicate bodies, fit the item budget, then add background."""
        # Distinct *content*, not distinct rows: candidates are already unique
        # by (kind, ref, revision), but a corpus where a legacy import
        # re-delivered the same bodies under fresh identities would let one
        # document hold four of six slots.  One filter spans both passes so
        # background cannot repeat the evidence either.
        distinct = DistinctContent()
        ranked = self._apply_budget(distinct.filtered(self._rank_hydrated(hydrated, working)), working.limits)
        query_items = tuple(obj for _candidate, obj in ranked)
        if working.mode == "auto" and self._remaining(working) > 0:
            if ranked and all(estimate_tokens(obj.content) > working.limits.budget_tokens for _c, obj in ranked):
                # Nothing that fits answered (as when the only hit that fits is a message the model still holds): one
                # hit too large for any packet stays, as the hint that it can be expanded, and the background takes
                # the other slots.  Six such hits would take all of them, the compiler would refuse each, and the
                # packet would come back empty.
                ranked = ranked[:1]
            background = background_candidates(
                tx, working, self.storage_reader, self.clock, gaps, query_evidence=bool(query_items)
            )
            present = {candidate.key for candidate, _obj in ranked}
            # Query evidence comes first; background shares the final byte
            # budget and can only consume remaining packet slots.  What the
            # asking model still holds is not background either (``_in_context``).
            ranked.extend(
                pair
                for pair in background
                if pair[0].key not in present
                and distinct.admits(pair[1])
                and not (working.in_context_since is not None and _in_context(tx, pair[1], working))
            )
            note_truncation(gaps, "packet_slots", considered=working.limits.max_items, available=len(ranked))
            ranked = ranked[: working.limits.max_items]
        note_duplicates(gaps, distinct.collapsed)
        return ranked, query_items

    @staticmethod
    def _result(
        working: SearchContext,
        epoch: int,
        ranked,
        query_items: tuple[RetrievedObject, ...],
        gaps: list[str],
        seed_count: int,
        hydrated_count: int,
    ) -> RetrievalResult:
        items = tuple(obj for _candidate, obj in ranked)
        needs = unmet_needs(working.query, query_items)
        if items and not query_items:
            needs = (*needs, "query_evidence_missing")
        if not query_items:
            answerability = "unknown"
        elif needs:
            answerability = "partial"
        elif len(query_items) > 1 and mentions(working.query, CHOICE_MARKERS):
            answerability = "ambiguous"
        else:
            answerability = "supported"
        return RetrievalResult(
            tuple(candidate for candidate, _obj in ranked),
            items,
            epoch,
            tuple(dict.fromkeys(gaps)),
            "partial" if gaps else "unknown",
            answerability,
            seed_count,
            hydrated_count,
            request_id=working.request_id,
            unmet_needs=needs,
        )

    def collection(self, context: SearchContext, query: CollectionQuery, cursor=None) -> CollectionPage:
        if not isinstance(context, SearchContext) or not isinstance(query, CollectionQuery):
            raise ContractError("INPUT_INVALID", "collection_request")
        remaining = self._remaining(context)
        if remaining <= 0:
            raise ContractError("DEADLINE_EXCEEDED", "collection")
        with self.storage.read(context.trusted_context, remaining_seconds=remaining) as tx:
            return self.storage_reader.collection(tx, context, query, cursor)


def recall(
    context: SearchContext, *, storage, vector_port=None, policy: RecallPolicy | None = None, clock=None
) -> RetrievalResult:
    """Small functional entry point for adapters that do not retain a service."""
    return RetrievalPipeline(storage, vector_port=vector_port, policy=policy, clock=clock).search(context)
