"""Trusted runtime composition for the bounded background worker.

Construction opens nothing: SQLite is opened only by ``status``/``recall``/
``drain`` and a vector companion only when an operation explicitly asks.  No
model or host supplied request field is accepted as identity.

The one construction side effect is this process's running-code record
(``runtime/running_code.py``): construction is the moment a process has
demonstrably loaded the package and bound itself to this instance, the write
is one small file that can neither block nor fail the caller, and it acquires
no lock and opens no store.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Callable, Mapping, Sequence

from ..contracts import ContractError, TrustedContext
from ..core.capture_filters import redact_private_paths
from ..core.composition import CoreConfig, MemoryCore
from ..core.deadline import RequestDeadline, using_request_deadline
from ..core.index_rebuild import IMPORT_EMBED_QUEUE_CEILING
from ..core.retrieval import SearchContext
from ..core.storage import SQLiteStorage
from ..vector.lance_native import helper_start_failure
from ..vector.process_store import NativeVectorPathError
from ..vector.store import build_vector_store
from .auxiliary import build_auxiliary_runtime
from .backup_upkeep import mirror_if_due
from .codex_cli import CodexCliConsolidationAdapter
from .embedding_retry import embed_with_one_retry
from .instance_config import RuntimeInstanceConfig, VectorRuntimeConfig
from .lance_port import LanceEmbedPort, LancePurgePort, LanceVectorPort, search_partitions
from .model_budget import provider_holds
from .models import AuxiliaryModelError
from .running_code import record_running_code
from .validation import (
    strict_float,
    utc_now,
)
from .vector_retention import expire_if_due
from .vector_upkeep import backfill_if_due, compact_if_due, index_if_due, respace_if_due
from .worker_launch import failure_reason

#: Share of a recall's window its query embedding may take when it is asked for as the recall starts.  The rest is the
#: vector search's own: an embedding that took all of it would leave the search nothing.
QUERY_EMBEDDING_SHARE = 0.7


class _QueryEmbedding:
    """A recall's query embeddings, asked for when it starts instead of after the SQLite channels.

    Query embeddings start alongside SQLite retrieval so connection setup shares the full recall window rather
    than only the time left after the exact, lexical, claim and recent channels.

    ``queries`` is the question and the other ways recall reads it (``core/owner_view.py``), embedded in one request:
    one round trip on one pooled query connection, which leaves the other connections to recalls running alongside.
    """

    def __init__(self, queries: tuple[str, ...], deadline: float) -> None:
        self.queries = queries
        self.query = queries[0]
        self.deadline = deadline
        self._done = threading.Event()
        self._vectors: tuple[Any, ...] = ()
        self._error: BaseException | None = None

    def run(self, embed: Callable[[tuple[str, ...], float], Sequence[Any]]) -> None:
        try:
            deadline = RequestDeadline.from_absolute(self.deadline, now=time.monotonic())
            with using_request_deadline(deadline):
                budget = deadline.remaining()
                if budget <= 0:
                    raise TimeoutError("query embedding stage deadline exhausted")
                # A connection that failed in milliseconds costs the whole
                # semantic channel otherwise; a rejected request is not retried.
                vectors = tuple(
                    embed_with_one_retry(
                        lambda seconds: embed(self.queries, seconds),
                        budget_seconds=budget,
                        remaining=deadline.remaining,
                    )
                )
                if len(vectors) != len(self.queries):
                    raise AuxiliaryModelError("unsupported_response_shape")
                self._vectors = vectors
        except BaseException as exc:  # the recall that waits for it reports it
            self._error = exc
        finally:
            self._done.set()

    def result(self, deadline: float, query: str | None = None) -> Any:
        """The vector of ``query`` (the question's own by default); the embedding's own failure; a timeout if it is
        still out at ``deadline``."""
        if not self._done.wait(max(0.0, deadline - time.monotonic())):
            raise AuxiliaryModelError("timeout")
        if self._error is not None:
            raise self._error
        return self._vectors[self.queries.index(self.query if query is None else query)]


def _taken(prefetched: Any, deadline: float, query: str) -> Any:
    """The vector asked for early for ``query``; one asked for the question alone answers ``result(deadline)``."""
    return prefetched.result(deadline) if query == prefetched.query else prefetched.result(deadline, query)


class _LazyVectorPort:
    """Request-local vector facade for direct Core and Runtime calls.

    Construction never opens Lance.  The first real SearchContext opens only
    an existing companion under that request's absolute deadline, then keeps
    the trusted identity and scope supplied by that context all the way to
    ``LanceVectorPort``.  A disabled query embedding route returns no vector
    candidates; it never exposes the raw native store as a Core port.
    """

    def __init__(self, instance: "RuntimeInstance") -> None:
        self._instance = instance

    def _embedding_adapter(self) -> LanceVectorPort | None:
        embedding = getattr(self._instance.auxiliary, "query_embedding", None)
        if embedding is None:
            return None
        return LanceVectorPort(None, embedding, expected_embedding_space=self._instance.config.embedding_space_id())

    def _query_embedder(self) -> Callable[[str, float], Any] | None:
        adapter = self._embedding_adapter()
        return None if adapter is None else adapter._embed_query

    def _checked_query(self, context, route, texts, seconds):
        """Check the current conversation at each actual query embedding request."""
        from ..core.private import using_outbound_check

        with using_outbound_check(lambda: self._instance.core.private_mode(context.trusted_context) == "full"):
            return route(texts, seconds)

    def prefetch_query(self, context: SearchContext, *, also: tuple[str, ...] = ()) -> _QueryEmbedding | None:
        """Ask for the query's embedding now, beside the SQLite channels; ``search`` takes it from there.  ``also``
        are other ways the recall reads the question, embedded in the same request where the route can send several;
        where it cannot, each is embedded in its own search."""
        if not isinstance(context, SearchContext) or self._instance.config.vector is None:
            return None
        adapter = self._embedding_adapter()
        if adapter is None:
            return None
        now = time.monotonic()
        window = context.deadline - now
        if window <= 0.0:
            return None
        queries = tuple(dict.fromkeys((context.query, *also)))
        many = adapter._queries_embedder() if len(queries) > 1 else None
        if many is None:
            queries = queries[:1]
            one = adapter._embed_query
            many = lambda texts, seconds: (one(texts[0], seconds),)  # noqa: E731
        route = many

        def checked(texts, seconds):
            return self._checked_query(context, route, texts, seconds)

        pending = _QueryEmbedding(queries, now + window * QUERY_EMBEDDING_SHARE)
        threading.Thread(target=pending.run, args=(checked,), name="scope-recall-query-embedding", daemon=True).start()
        return pending

    def search(
        self, context: SearchContext, *, limit: int, remaining_seconds: float, prefetched: _QueryEmbedding | None = None
    ):
        if not isinstance(context, SearchContext):
            raise TypeError("context must be SearchContext")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if type(remaining_seconds) not in (int, float) or not math.isfinite(float(remaining_seconds)):
            raise ValueError("remaining_seconds must be finite")
        if prefetched is not None and context.query not in getattr(prefetched, "queries", (prefetched.query,)):
            prefetched = None
        now = time.monotonic()
        remaining = min(float(remaining_seconds), context.deadline - now)
        if remaining <= 0.0:
            return ()
        effective_deadline = min(context.deadline, now + float(remaining_seconds))
        context = replace(context, deadline=effective_deadline)
        deadline = RequestDeadline.from_absolute(effective_deadline, now=now)
        prepared: list[tuple[str, Any]] = []
        failed: list[Exception] = []

        def embed_while_opening() -> bool | None:
            # The embedding's failure is this recall's to report.  Raised inside the open, it would close the
            # helper as well, and the next prompt would open the table cold again; reported as False, the open is
            # taken if it has come and parked for the next request if not, so a fast failure does not wait for the
            # table either.
            try:
                if prefetched is not None:
                    prepared.append((context.query, _taken(prefetched, effective_deadline, context.query)))
                    return True
                embed = self._query_embedder()
                if embed is None:
                    return None
                embedding_remaining = deadline.remaining()
                if embedding_remaining <= 0:
                    raise TimeoutError("query embedding stage deadline exhausted")

                prepared.append(
                    (
                        context.query,
                        embed_with_one_retry(
                            lambda seconds: self._checked_query(context, embed, context.query, seconds),
                            budget_seconds=embedding_remaining,
                            remaining=deadline.remaining,
                        ),
                    )
                )
                return True
            except Exception as exc:
                failed.append(exc)
                return False

        with using_request_deadline(deadline):
            port = self._instance._ensure_vector_port(
                allow_create=False, deadline=effective_deadline, during_open=embed_while_opening
            )
            if port is None:
                return ()
            if failed:
                raise failed[0]
            if deadline.remaining() <= 0.0:
                # The helper is still opening the table: its answer waits for the next request, which a hook's
                # process never makes.  Returned as nothing, this recall would look as if the search had found nothing
                # and report no gap, and a client would lose its vector search this way unseen.
                raise TimeoutError("native vector helper open deadline exhausted before the search")
            if not prepared and prefetched is not None:
                prepared.append((context.query, _taken(prefetched, effective_deadline, context.query)))
            remaining = min(remaining, deadline.remaining())
            if remaining <= 0.0:
                # The embedding came back with no time left to search: the search would return nothing and say
                # nothing, and the recall would look as if it had searched by meaning.
                raise TimeoutError("native vector helper request deadline exhausted before the search")
            if prepared:
                return port.search(context, limit=limit, remaining_seconds=remaining, _prepared_query=prepared[0])
            return port.search(context, limit=limit, remaining_seconds=remaining)


def _open_store(resource: Any, *, allow_create: bool, deadline: float | None, during_open) -> None:
    """Open a companion store; ``during_open`` overlaps an existing-index open when the store can."""
    opener = getattr(resource, "open" if allow_create else "open_existing", None)
    overlap = getattr(resource, "open_existing_with_work", None)
    if not allow_create and during_open is not None and callable(overlap):
        opener = partial(overlap, during_open)
    if not callable(opener):
        return
    if deadline is None:
        opener()
        return
    with using_request_deadline(RequestDeadline.from_absolute(deadline, now=time.monotonic())):
        opener()


def _close_quietly(resource: Any) -> None:
    closer = getattr(resource, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _helper_start_failure(deadline: float) -> str | None:
    """One line saying why the vector helper could not start, from its start-up run once more; or ``None``.

    The helper's stderr is discarded, so a helper that dies at an import (as on Hermes Desktop, at ``import
    jsonschema``) would leave every pass reporting ``vector_unavailable:RuntimeError`` and nothing else.  The run is
    sent no request, so it holds no memory
    text; the line is the last of its traceback, chosen as the watchdog chooses a worker's, paths redacted.  It
    takes at most a quarter of what is left of the pass.
    """
    timeout = (deadline - time.monotonic()) / 4
    if timeout < 1.0:
        return None

    stderr = helper_start_failure(timeout)
    line = failure_reason(stderr) if stderr else None
    return redact_private_paths(line)[:200] if line else None


@dataclass
class RuntimeInstance:
    """Own runtime ports and release their resources when the instance closes."""

    config: RuntimeInstanceConfig
    core: MemoryCore
    auxiliary: Any
    _vector_factory: Callable[[VectorRuntimeConfig], Any] | None = None
    _vector_port: Any = None
    _vector_store: Any = None
    _default_embed: Any = None
    _default_purge: Any = None
    _ingress_authorizer: Callable[[object], frozenset[str]] | None = None
    _owned_resources: list[Any] = field(default_factory=list)
    _vector_openers: dict[int, int] = field(default_factory=dict, init=False, repr=False, compare=False)
    _closed: bool = False
    _state_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)
    background_gaps: tuple[str, ...] = ()
    #: Why this drain's vector helper could not start, one line, or ``None`` (``_open_vector_for_drain``).
    vector_helper_error: str | None = None
    ingress_receipts: tuple[Any, ...] = ()
    #: Receipt of the vector compaction this drain ran, or ``None``.
    vector_compaction: dict | None = None
    #: Receipt of the look this drain took at the nearest-neighbour index, or ``None``.
    vector_index: dict | None = None
    #: Receipt of the vector retention pass this drain ran, or ``None``.
    vector_retention: dict | None = None
    #: Receipt of the page of an import's embeddings this drain queued (``backfill_if_due``), or ``None``.
    embed_backfill: dict | None = None
    #: Receipt of the page of a re-embed run this drain reopened (``respace_if_due``), or ``None`` without a run.
    embed_respace: dict | None = None
    #: Snapshot or mirror outcome from this drain, independent of model budgets.
    backup_upkeep: dict | None = None
    #: Work types this drain left alone, each with the held model and when its
    #: hold ends (runtime/model_budget.py ``provider_holds``).
    provider_holds: dict = field(default_factory=dict)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("runtime_instance_closed")

    def _ensure_vector_port(
        self, *, allow_create: bool = False, deadline: float | None = None, during_open=None
    ) -> Any:
        """Open the companion store and compose the Core ports over it.

        Query paths pass ``allow_create=False`` and may only open an existing
        index; explicit background maintenance is the sole path allowed to
        create one.  Injected VectorPort values stay untouched for
        deterministic tests and alternate backends.
        """
        with self._state_lock:
            self._ensure_open()
            resource = self._vector_store
            factory, config = self._vector_factory, self.config.vector
        new_resource = resource is None
        if new_resource:
            if config is None or factory is None:
                return None
            resource = factory(config)
        registered = False
        try:
            if new_resource:
                with self._state_lock:
                    self._ensure_open()
                    if not any(owned is resource for owned in self._owned_resources):
                        self._owned_resources.append(resource)
                    identity = id(resource)
                    self._vector_openers[identity] = self._vector_openers.get(identity, 0) + 1
                    registered = True
            # Registration and publication share the shutdown lock; native I/O
            # stays outside it so one slow open cannot serialize recalls.
            if new_resource or getattr(resource, "requires_reopen", False):
                _open_store(resource, allow_create=allow_create, deadline=deadline, during_open=during_open)
            with self._state_lock:
                self._ensure_open()
                if registered:
                    self._finish_vector_open(resource)
                    registered = False
                redundant = (
                    new_resource
                    and self._vector_store is not None
                    and self._vector_store is not resource
                    and id(resource) not in self._vector_openers
                )
                if redundant:
                    # Only the first completed open owns the published ports.
                    # Detach the redundant helper before closing it outside the lock.
                    self._owned_resources[:] = [owned for owned in self._owned_resources if owned is not resource]
                elif new_resource and self._vector_store is None:
                    self._compose_ports(resource)
                port = self._vector_port
        except BaseException:
            with self._state_lock:
                if registered:
                    self._finish_vector_open(resource)
                # A factory may lend the same object to overlapping opens. A
                # failed call must not close a published or still-opening peer.
                shared = self._vector_store is resource or id(resource) in self._vector_openers
                close_resource = self._closed or (new_resource and not shared)
                if close_resource:
                    self._owned_resources[:] = [owned for owned in self._owned_resources if owned is not resource]
            # An open may finish after shutdown's first close, creating a late
            # handle. Release it again before propagating the original error.
            if close_resource:
                _close_quietly(resource)
            raise
        if redundant:
            _close_quietly(resource)
        return port

    def _finish_vector_open(self, resource: Any) -> None:
        """Release one open's identity claim while the caller holds the state lock."""
        identity = id(resource)
        remaining = self._vector_openers[identity] - 1
        if remaining:
            self._vector_openers[identity] = remaining
        else:
            del self._vector_openers[identity]

    def warm_vector_store(self, seconds: float) -> bool:
        """Open the existing companion store and search it once, off any prompt's time.

        Made at its first prompt, a recall handler kept across prompts (adapters/clients/local_endpoint.KeptRecaller)
        would start the helper, open the table and read the index inside that prompt's recall, and that recall would
        answer by words alone (``helper_request_deadline``) after every start of its server, which for Claude Code is
        every session.  Warmed when the server starts, the first prompt finds them ready.  It writes nothing.
        """
        self._ensure_open()
        if self.config.vector is None or self._vector_factory is None:
            return False
        now = time.monotonic()
        deadline = now + float(seconds)
        with using_request_deadline(RequestDeadline.from_absolute(deadline, now=now)):
            if self._ensure_vector_port(allow_create=False, deadline=deadline) is None:
                return False
            search_scopes = getattr(self._vector_store, "search_scopes", None)
            trusted = self.config.context()
            if not callable(search_scopes) or not trusted.allowed_scope_ids:
                return True

            # Every partition a recall of this runtime filters on (``lance_port._partition_hits``).  The first
            # search of an index reads all of it into the helper's cache whatever the filter; after that a search
            # touches only the codes of the rows its filter keeps.  Filtered on the first scope's partition, which
            # held no rows for any entry of a shared store, the search touched 26 MB of the 306 MB the next recall
            # paged back in once the helper's memory was trimmed.  What it finds is not looked at.
            search_scopes(
                [1.0] + [0.0] * (self.config.vector.dimensions - 1),
                scope_ids=list(search_partitions(trusted, self.config.embedding_space_id())),
                limit=1,
            )
        return True

    def warm_query_embedding(self, seconds: float) -> bool:
        """Ask the query embedding route for one vector, off any prompt's time: a server's start, for its embedding
        worker and its connection.

        Warm the query embedding worker and connection outside the prompt's time budget using fixed text.
        The returned vector is discarded and no memory content is stored, but the embedding request still reserves
        and settles usage in the auxiliary budget ledger."""
        self._ensure_open()
        if self.config.vector is None:
            return False
        embed = _LazyVectorPort(self)._query_embedder()
        if embed is None:
            return False
        now = time.monotonic()
        deadline = RequestDeadline.from_absolute(now + float(seconds), now=now)
        with using_request_deadline(deadline):
            from ..core.private import outbound_blocked, using_outbound_check

            if outbound_blocked(self.core.storage, self.core.clock, self.config.context()):
                return False

            def checked(budget):
                with using_outbound_check(
                    lambda: outbound_blocked(self.core.storage, self.core.clock, self.config.context())
                ):
                    return embed("scope recall warm-up", budget)

            embed_with_one_retry(
                checked,
                budget_seconds=deadline.remaining(),
                remaining=deadline.remaining,
            )
        return True

    def _compose_ports(self, resource: Any) -> None:
        """Core consumes ports that turn trusted SearchContext values into
        partitioned native queries; the raw store is never exposed as one.
        Purge is a local native operation and stays available even when both
        embedding routes are disabled."""
        binding = self.config.binding
        space_id = self.config.embedding_space_id()
        port = None
        query_embedding = getattr(self.auxiliary, "query_embedding", None)
        if query_embedding is not None:
            port = LanceVectorPort(resource, query_embedding, expected_embedding_space=space_id)
            source_embedding = getattr(self.auxiliary, "source_embedding", None)
            if source_embedding is not None:
                self._default_embed = LanceEmbedPort(
                    resource,
                    source_embedding,
                    agent_id=binding.agent_id,
                    installation_id=binding.installation_id,
                    embedding_space=space_id,
                )
        self._default_purge = LancePurgePort(
            resource,
            embedding_spaces=(space_id,),
            agent_id=binding.agent_id,
            installation_id=binding.installation_id,
        )
        self._vector_store = resource
        self._vector_port = port

    def status(self, *, include_admission: bool = True, include_queue_age: bool = True) -> Any:
        self._ensure_open()
        return self.core.status(
            self.config.context(), include_admission=include_admission, include_queue_age=include_queue_age
        )

    def memory_epoch(self) -> int:
        """Read the authority fence without counting the backlog.

        Calling ``status()`` also aggregates work and source counts and, when
        admission diagnostics are enabled, scans source metadata within the
        caller's authorized context.  Reading the epoch avoids those diagnostics
        while checking the same binding and fetching the singleton metadata row.
        """
        self._ensure_open()
        return self.core.memory_epoch(self.config.context())

    def recall(self, request: Mapping[str, Any], *, current_source_refs: tuple[str, ...] = ()) -> Any:
        return self.core.recall(
            self.config.context(),
            dict(request),
            current_source_refs=current_source_refs,
            deadline_seconds=self.config.request_seconds,
        )

    def drain(
        self,
        *,
        embed: Any = None,
        purge: Any = None,
        consolidation: Any = None,
        max_items: int | None = None,
        purge_only: bool = False,
        remaining_seconds: float | None = None,
    ) -> Any:
        self._ensure_open()
        budget = self.config.drain_seconds if remaining_seconds is None else remaining_seconds
        strict_float("remaining_seconds", budget, minimum=0.001, maximum=self.config.drain_seconds)
        deadline = time.monotonic() + budget
        self.backup_upkeep = mirror_if_due(self.core.storage, self.config, available_seconds=budget)
        ingress_gaps = self._replay_ingress(budget)
        vector_gaps = self._open_vector_for_drain(deadline, budget)
        self.background_gaps = (*ingress_gaps, *vector_gaps)
        # Upkeep comes before the queue, not after it: on a busy instance the
        # budget is gone by the time the queue drains, so upkeep at the end is
        # upkeep that only ever runs when it is not needed.
        self.vector_retention = expire_if_due(
            self._vector_store,
            self.config,
            self.core.storage,
            self.config.context(),
            available_seconds=max(0.0, deadline - time.monotonic()),
        )
        expired = (self.vector_retention or {}).get("expired", 0)
        self.vector_compaction = compact_if_due(
            self._vector_store,
            self.config.vector,
            available_seconds=max(0.0, deadline - time.monotonic()),
            reason=f"vectors_expired:{expired}" if expired else None,
        )
        # A store this pass could not open says nothing about its index; a look now would be recorded as a
        # failure and put off the build for hours.
        self.vector_index = (
            None
            if vector_gaps
            else index_if_due(
                self._vector_store,
                self.config.vector,
                available_seconds=max(0.0, deadline - time.monotonic()),
            )
        )
        if (self.backup_upkeep or {}).get("outcome") == "failed":
            self.background_gaps = (
                *self.background_gaps,
                f"deletion_mirror_failed:{(self.backup_upkeep or {}).get('error', 'unknown')}",
            )
        from ..core.worker import WorkerConfig, drain_worker  # only a process that drains loads the worker

        model, candidate = self._consolidation_ports(consolidation)
        self.provider_holds = {} if purge_only else provider_holds(self.config.auxiliary)
        # An import's history in a person's roles gets the embedding its source store never queued, a page a pass
        # and only while the queue is shallow (``core.index_rebuild.queue_import_embeddings``).  Embeddings are
        # claimed before candidate evaluations, so while one is ready that this pass would claim, no more than half
        # a pass of them is kept waiting: evaluations share every pass, and the backfill still moves when they
        # cannot be done.  One without an evaluator, under a provider hold, or in a pass that only purges does not
        # count.
        evaluations = (
            frozenset()
            if purge_only or candidate is None
            else frozenset({"evaluate_candidate"}) - frozenset(self.provider_holds)
        )
        page = self.config.max_items if max_items is None else max_items
        embeds = not vector_gaps and (embed if embed is not None else self._default_embed) is not None
        self.embed_backfill = (
            None
            if not embeds
            else backfill_if_due(
                self.core.storage,
                self.config.context(),
                self.config.vector,
                yield_to=evaluations,
                yield_ceiling=min(IMPORT_EMBED_QUEUE_CEILING, max(1, page // 2)),
            )
        )
        # An operator's re-embed run (``respace-embeddings``) goes on behind the import backfill, under the same
        # queue ceiling.  A run into another space, or a page that failed, is said where the doctor reads it.
        self.embed_respace = (
            None
            if not embeds or purge_only
            else respace_if_due(
                self.core.storage,
                self.config.context(),
                self.config.embedding_space_id(),
                yield_to=evaluations,
                yield_ceiling=min(IMPORT_EMBED_QUEUE_CEILING, max(1, page // 2)),
            )
        )
        outcome = (self.embed_respace or {}).get("outcome")
        if outcome == "failed":
            self.background_gaps = (*self.background_gaps, f"embedding_respace_failed:{self.embed_respace['error']}")
        elif outcome == "space_mismatch":
            self.background_gaps = (*self.background_gaps, "embedding_respace_space_mismatch")
        limit = self.config.request_seconds
        effective_embed = embed if embed is not None else self._default_embed
        effective_purge = purge if purge is not None else self._default_purge
        return drain_worker(
            self.core.storage,
            self.core.clock,
            self.config.context(),
            config=WorkerConfig(
                owner_id=self.config.owner_id,
                max_items=self.config.max_items if max_items is None else max_items,
                lease_seconds=self.config.lease_seconds,
                auto_retry_cooldown_seconds=self.config.auto_retry_cooldown_seconds,
                max_auto_recoveries=self.config.max_auto_recoveries,
                purge_only=purge_only,
                admission_policy=self.core.config.admission_policy,
                # The bound the model and embedding ports below are clamped to.
                request_seconds=limit,
                model_request_seconds=self.config.model_request_seconds,
                held_work_types=frozenset(self.provider_holds),
            ),
            remaining_seconds=max(0.001, deadline - time.monotonic()),
            consolidation=model,
            candidate=candidate,
            embed=_BoundedEmbed(effective_embed, limit) if effective_embed is not None else None,
            purge=_BoundedPurge(effective_purge, limit) if effective_purge is not None else None,
        )

    def _replay_ingress(self, budget: float) -> tuple[str, ...]:
        """Persist captured inbox payloads.  Ingress never spends the model-work budget."""
        # On first use: the worker and the wake start without the inbox's replay.
        from ..core.capture_inbox import INGRESS_PENDING_GAP, replay_inbox, resolve_conflicted_ingress

        self.ingress_receipts = ()
        try:
            context = self.config.context()
            if self._ingress_authorizer is None:
                return self._unauthorized_ingress_gaps(context, budget)
            options = dict(
                authorize=self._ingress_authorizer,
                admission_policy=self.core.config.admission_policy,
                remaining_seconds=min(2.0, budget / 4),
            )
        except (ContractError, OSError, RuntimeError, ValueError):
            return (INGRESS_PENDING_GAP,)
        # A key-collided capture is invisible to the first replay, which only
        # retries failures that might clear by themselves; without the second
        # it stays in the inbox forever, captured but never stored.  Each runs
        # whatever the other met: one that raised would skip the other.
        pending = False
        for replay in (replay_inbox, resolve_conflicted_ingress):
            try:
                self.ingress_receipts += tuple(replay(self.core.storage, self.core.clock, context, **options))
            except (ContractError, OSError, RuntimeError, ValueError, sqlite3.Error):
                pending = True
        if pending or any(INGRESS_PENDING_GAP in receipt.gaps for receipt in self.ingress_receipts):
            return (INGRESS_PENDING_GAP,)
        return ()

    def _unauthorized_ingress_gaps(self, context: TrustedContext, budget: float) -> tuple[str, ...]:
        """Without an authorizer, pending ingress can only be reported, not replayed."""
        scopes = tuple(sorted(context.allowed_scope_ids))
        with self.core.storage.read(context, remaining_seconds=min(1.0, budget / 8)) as tx:
            pending = (
                tx._check()
                .execute(
                    f"""SELECT 1 FROM capture_inbox
                WHERE scope_id IN ({",".join("?" for _ in scopes)})
                  AND project_id IS ? AND branch_id IS ? LIMIT 1""",
                    (*scopes, context.project_id, context.branch_id),
                )
                .fetchone()
            )
        if pending is None:
            return ()
        return ("capture_gap:durable_ingress_authorizer_unconfigured", "capture_gap:durable_ingress_pending")

    def _open_vector_for_drain(self, deadline: float, budget: float) -> tuple[str, ...]:
        """Open (or create) the index for this pass; a failure is a gap, never a stopped drain.

        Optional indexing failure cannot suppress a healthy independent
        consolidation route.  Purge stays pending unless acknowledged.
        """
        self.vector_helper_error = None
        vector_deadline = min(deadline, time.monotonic() + min(self.config.request_seconds, budget / 4))
        try:
            self._ensure_vector_port(allow_create=True, deadline=vector_deadline)
        except Exception as exc:
            from ..core.vector_failure import vector_failure_label  # only when the vector store fails to open

            if isinstance(exc, NativeVectorPathError):
                return (NativeVectorPathError.code,)
            label = vector_failure_label(exc)
            # The open here is a fresh helper's first request (a failed store is closed, and reopening starts
            # another), so ``worker_failed`` means the helper ended before its first answer.
            if label == "RuntimeError:worker_failed":
                self.vector_helper_error = _helper_start_failure(deadline)
            return (f"vector_unavailable:{label}",)
        return ()

    def _consolidation_ports(self, consolidation: Any) -> tuple[Any, Any]:
        """The pass's bounded consolidation model and, when it offers one, candidate evaluator."""
        model = consolidation
        if model is None:
            from ..core.worker import build_consolidation_model  # only a process that drains loads the worker

            port = self.core.consolidation
            # One CLI allowance is shared by consolidation and candidate
            # evaluation within this pass; no new scheduler, lease or queue.
            if isinstance(port, CodexCliConsolidationAdapter):
                port = port.for_pass()
            model = build_consolidation_model(port)
        limit = self.config.request_seconds
        if self.config.model_request_seconds is not None:
            limit = self.config.model_request_seconds
        candidate = _BoundedCandidate(model, limit) if callable(getattr(model, "evaluate_candidate", None)) else None
        return (_BoundedConsolidation(model, limit) if model is not None else None), candidate

    def retry_failed(
        self,
        work_ids,
        *,
        operation_id: str,
        expected_memory_epoch: int | None = None,
        operator_context: TrustedContext | None = None,
    ) -> tuple[Any, ...]:
        """Request a bounded, explicitly named maintenance retry.

        The storage transaction performs all identity/scope/source/epoch
        checks.  This method only exposes that controlled mutation to the
        worker entry point; it does not accept host/model identity or SQL.
        The caller may follow it with one normal bounded ``drain``.
        """
        self._ensure_open()
        maintenance_context = self.config.context() if operator_context is None else operator_context
        if maintenance_context.binding != self.config.binding:
            raise ContractError("IDENTITY_UNBOUND")
        if not maintenance_context.allowed_scope_ids <= self.config.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED")
        with self.core.storage.write(maintenance_context, remaining_seconds=self.config.request_seconds) as tx:
            return tx.work.operator_retry_failed(
                work_ids,
                now=utc_now(),
                operation_id=operation_id,
                expected_memory_epoch=expected_memory_epoch,
                max_items=min(8, self.config.max_items),
            )

    def close(self) -> None:
        """Detach owned resources atomically, then close them without holding the state lock."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            resources, self._owned_resources = self._owned_resources, []
            self._vector_store = None
            self._vector_port = None
            self._default_embed = None
            self._default_purge = None
        seen: set[int] = set()
        for resource in reversed(resources):
            if id(resource) not in seen:
                seen.add(id(resource))
                _close_quietly(resource)


def default_vector_factory(config: VectorRuntimeConfig) -> Any:
    """Build an existing-companion store without opening or creating it."""
    return build_vector_store(
        config.backend,
        storage_dir=config.storage_dir,
        table_name=config.table_name,
        dimensions=config.dimensions,
        metric=config.metric,
    )


class _Bounded:
    """Forward ``methods`` to a port with ``remaining_seconds`` clamped to ``limit``.

    The durable worker owns the drain budget while every model or native
    boundary receives the stricter per-call budget; keeping the clamp at the
    runtime boundary stops a future worker port from turning the 45 second
    contract into 120.  Methods resolve through ``__getattr__`` so a port that
    lacks one (claims on an embed port) still looks like it lacks it: the
    worker probes for capabilities, and a plain method would always answer.
    """

    methods: tuple[str, ...] = ()

    def __init__(self, inner: Any, limit: float) -> None:
        self._inner = inner
        self._limit = limit

    def __getattr__(self, name: str):
        if name not in self.methods:
            raise AttributeError(name)
        target = getattr(self._inner, name, None)
        if target is None:
            raise AttributeError(name)
        limit = self._limit

        def bounded(*args, remaining_seconds=1.0, **kwargs):
            return target(*args, remaining_seconds=min(float(remaining_seconds), limit), **kwargs)

        return bounded


class _BoundedConsolidation(_Bounded):
    methods = ("propose",)


class _BoundedCandidate(_Bounded):
    methods = ("evaluate_candidate",)


class _BoundedEmbed(_Bounded):
    # prepare_sources and publish_sources are the group forms of prepare_source
    # and publish_source, and each has to be listed here or the worker probes
    # for it, does not find it through this wrapper, and falls back to one
    # document per request and one commit per vector as if the capability did
    # not exist, silently.
    methods = (
        "prepare_source",
        "prepare_sources",
        "publish_source",
        "publish_sources",
        "prepare_claim",
        "prepare_claims",
        "publish_claim",
        "publish_claims",
    )


class _BoundedPurge(_Bounded):
    methods = ("purge_active",)


def build_runtime_instance(
    config: RuntimeInstanceConfig,
    *,
    vector_factory: Callable[[VectorRuntimeConfig], Any] | None = None,
    vectors: Any = None,
    consolidation: Any = None,
) -> RuntimeInstance:
    if not isinstance(config, RuntimeInstanceConfig):
        raise TypeError("config must be RuntimeInstanceConfig")
    auxiliary = build_auxiliary_runtime(config.auxiliary) if config.auxiliary is not None else None
    core = MemoryCore(
        CoreConfig(config.binding, auto_recall_seconds=config.auto_recall_seconds),
        storage=SQLiteStorage(config.binding),
        vectors=vectors,
        consolidation=consolidation if consolidation is not None else getattr(auxiliary, "consolidation", None),
        retrieval_policy=config.recall_policy(),
    )
    ingress_authorizer = None
    # A shared store's inbox holds its Hermes entries' captures, whichever
    # process replays them; each is checked against the entry that made it.
    if config.host_adapter == "hermes" or config.binding.installation_kind == "shared":
        # The worker checks a replayed capture against the host's current installation map, which is the host
        # adapters' identity code: the one import from runtime up into adapters, kept to the call (it would add
        # about twenty modules to every worker start).
        from ..adapters.hermes.authorization import build_ingress_authorizer

        ingress_authorizer = build_ingress_authorizer(config.binding)
    elif config.host_adapter == "codex":
        from ..adapters.clients.authorization import build_ingress_authorizer

        ingress_authorizer = build_ingress_authorizer(config.binding)
    instance = RuntimeInstance(
        config=config,
        core=core,
        auxiliary=auxiliary,
        _vector_factory=vector_factory or (default_vector_factory if config.vector is not None else None),
        _vector_port=vectors,
        _vector_store=vectors,
        _ingress_authorizer=ingress_authorizer,
        _owned_resources=[resource for resource in (vectors, auxiliary) if resource is not None],
    )
    if vectors is None and config.vector is not None:
        facade = _LazyVectorPort(instance)
        core.vectors = facade
        core.recall_pipeline.vector_port = facade
    record_running_code(
        config.binding.data_directory,
        host_adapter=config.host_adapter,
        installation_id=config.binding.installation_id,
    )
    return instance


__all__ = [
    "RuntimeInstanceConfig",
    "RuntimeInstance",
    "VectorRuntimeConfig",
    "build_runtime_instance",
    "default_vector_factory",
]
