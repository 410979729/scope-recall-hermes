"""Explicit benchmark ports: deterministic smoke or approved runtime adapters.

No route is discovered from a host installation, and no CLI model fallback is
available. Live construction requires both a config file and exact endpoint
allowlisting by the caller. This module makes no request during import.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path

from cache import RequestCache, canonical, digest, remap


def normalized_messages(messages: list[dict], refs: dict[str, str]) -> list[dict]:
    """Normalize refs inside structured message bodies while preserving source prose."""
    normalized = []
    for message in messages:
        content = message["content"]
        label, separator, suffix = content.partition("=")
        prefix, payload = (label + separator, suffix) if separator and label.isidentifier() else ("", content)
        try:
            value = json.loads(payload)
        except (ValueError, TypeError):
            normalized.append(dict(message))
        else:
            normalized.append(dict(message, content=prefix + canonical(remap(value, refs))))
    return normalized


def envelope(sources, proposals) -> dict:
    """Wrap model proposals in the core's existing result contract."""
    return {
        "protocol_version": "1.1",
        "source_refs": [f"{s.ref}@{s.revision}" for s in sources],
        "claim_proposals": proposals,
        "resume_proposals": [],
        "reference_proposals": [],
    }


class FakeModel:
    """Mechanical substring proposals for exercising contracts, never a quality model."""

    def propose(self, sources, **kwargs) -> str:
        """Return sparse, source-quoted test proposals without inference or I/O."""
        proposals = []
        for source in sources:
            # A sparse fake keeps public prose from manufacturing hundreds of
            # unrelated candidate revisions. Dedicated TEST sources exercise
            # every proposal; the public smoke samples the same port contract.
            key = source.event["source_event_key"]
            if not key.startswith("TEST-") and int(digest(key)[:8], 16) % 64:
                continue
            # Quote lengths obey the real result contract for long turns.
            quote = source.event["content"][:1024]
            words = quote.split()
            if len(words) < 4:
                continue
            proposals.append(
                {
                    "kind": "fact",
                    "subject": words[0],
                    "predicate": words[1],
                    "value_text": words[2],
                    "conditions": [],
                    "statement_kind": "assertion",
                    "valid_from": source.event["occurred_at"],
                    "valid_to": None,
                    "evidence_spans": [{"source_ref": source.ref, "source_revision": source.revision, "quote": quote}],
                }
            )
        return json.dumps(envelope(sources, proposals))

    def evaluate_candidate(self, candidate, sources, **kwargs) -> str:
        """Echo one supplied candidate for the core to validate and settle."""
        return json.dumps(envelope(sources, [candidate.payload]))


class FakeEmbedder:
    """Hash tokens into 3072 float coordinates; no semantic claims or network calls."""

    def embed_text(self, text, remaining_seconds=1.0):
        """Return a normalized hash vector; the deadline argument matches the port."""
        vector = [0.0] * 3072
        for token in re.findall(r"\w+", text.lower()):
            vector[int(digest(token)[:8], 16) % 3072] += 1
        length = math.sqrt(sum(value * value for value in vector)) or 1
        return tuple(value / length for value in vector)

    def embed_source(self, source, remaining_seconds=1.0):
        """Hash the stored source's content without reading other records."""
        return self.embed_text(source.event["content"], remaining_seconds)

    def embed_query(self, query, remaining_seconds=1.0):
        """Hash the query locally using the same synthetic geometry."""
        return self.embed_text(query, remaining_seconds)

    def embed_sources(self, sources, remaining_seconds=1.0):
        """Hash an ordered group of sources without I/O."""
        return tuple(self.embed_source(source, remaining_seconds) for source in sources)

    def embed_texts(self, texts, remaining_seconds=1.0):
        """Hash an ordered group of document texts without I/O."""
        return tuple(self.embed_text(text, remaining_seconds) for text in texts)

    def embed_queries(self, queries, remaining_seconds=1.0):
        """Hash an ordered group of queries without I/O."""
        return tuple(self.embed_query(query, remaining_seconds) for query in queries)


class CachedModel:
    """Cache the complete consolidation/evaluation request and remap corpus refs."""

    def __init__(self, model, cache: RequestCache, config: dict) -> None:
        self.model, self.cache, self.config = model, cache, config

    def _call(self, operation, sources, candidate=None, **kwargs):
        records = [asdict(source) for source in sources]
        refs = {
            source.ref: f"source:{digest({key: value for key, value in record.items() if key != 'ref'})}"
            for source, record in zip(sources, records, strict=True)
        }
        episode = kwargs.get("episode_ref")
        if episode:
            refs[episode] = f"episode:{digest([refs[source.ref] for source in sources])}"
        request = {
            "sources": records,
            "options": {key: value for key, value in kwargs.items() if key != "remaining_seconds"},
        }
        if candidate is not None:
            refs[candidate.ref] = f"candidate:{digest(remap(candidate.payload, refs))}"
            request["candidate"] = asdict(candidate)
        from scope_recall.core.consolidate import candidate_evaluation_messages, consolidation_messages

        feedback = kwargs.get("validation_feedback")
        messages = (
            candidate_evaluation_messages(candidate, sources, validation_feedback=feedback)
            if candidate is not None
            else consolidation_messages(sources, episode_ref=episode, validation_feedback=feedback)
        )
        request["formatted_messages"] = normalized_messages(messages, refs)
        method = getattr(self.model, operation)

        def compute():
            text = method(candidate, sources, **kwargs) if candidate is not None else method(sources, **kwargs)
            return json.loads(text)

        return json.dumps(self.cache.call(operation, request, self.config, refs, compute))

    def propose(self, sources, **kwargs):
        """Replay or compute one complete, provenance-preserving proposal request."""
        return self._call("propose", sources, **kwargs)

    def evaluate_candidate(self, candidate, sources, **kwargs):
        """Replay or compute one candidate-specific request with local refs remapped."""
        return self._call("evaluate_candidate", sources, candidate, **kwargs)

    def judge(self, messages, **kwargs):
        """Cache source support and merge judgements through the configured product model."""
        body = json.loads(messages[-1]["content"])
        refs = {source["source_ref"]: "source:" + digest(source["content"]) for source in body["sources"]}
        for candidate in body["candidates"]:
            refs[candidate["ref"]] = "claim:" + digest(remap(candidate["payload"], refs))
        request = {"messages": normalized_messages(messages, refs)}

        def compute():
            method = getattr(self.model, "judge", None)
            if method is None:
                return dict(support="uncertain", action="new", target_ref=None, target_revision=None)
            return json.loads(method(messages, **kwargs))

        return json.dumps(self.cache.call("support", request, self.config, refs, compute))


class CachedEmbedder:
    """Cache individual encodings while preserving the wrapped batch capabilities."""

    _BATCH_METHODS = {"embed_sources": "embed_source", "embed_texts": "embed_text", "embed_queries": "embed_query"}

    def __init__(self, embedder, cache: RequestCache, config: dict) -> None:
        self.embedder, self.cache, self.config = embedder, cache, config

    def __getattr__(self, name):
        """Advertise a batch method only when the wrapped adapter implements it."""
        if name in self._BATCH_METHODS and callable(getattr(self.embedder, name, None)):
            return partial(self._call, name)
        raise AttributeError(name)

    @staticmethod
    def _request(method, item):
        """Describe the single input identically for single and batch requests."""
        if method == "embed_source":
            record = asdict(item)
            refs = {item.ref: f"source:{digest({key: value for key, value in record.items() if key != 'ref'})}"}
            return {"source": record, "encoding": "document"}, refs
        return {"text": item, "encoding": "query" if method == "embed_query" else "document"}, {}

    def _call(self, method, items, remaining_seconds=1.0):
        """Send only cache misses to the selected adapter method, preserving order."""
        items = list(items)
        single = self._BATCH_METHODS.get(method, method)
        operation = "query_embedding" if single == "embed_query" else "document_embedding"

        def compute(missing):
            invoke = getattr(self.embedder, method)
            selected = [items[index] for index in missing]
            if method in self._BATCH_METHODS:
                return invoke(selected, remaining_seconds=remaining_seconds)
            return [invoke(selected[0], remaining_seconds=remaining_seconds)]

        return tuple(
            tuple(vector)
            for vector in self.cache.call_many(
                operation, [self._request(single, item) for item in items], self.config, compute
            )
        )

    def embed_source(self, source, remaining_seconds=1.0):
        """Cache the complete document source input and return its vector."""
        return self._call("embed_source", [source], remaining_seconds)[0]

    def embed_text(self, text, remaining_seconds=1.0):
        """Cache a document encoding under its own operation namespace."""
        return self._call("embed_text", [text], remaining_seconds)[0]

    def embed_query(self, query, remaining_seconds=1.0):
        """Cache query encoding separately from identical document text."""
        return self._call("embed_query", [query], remaining_seconds)[0]


# A full-context prompt of about 115,000 tokens takes about a minute on one GPU, and two that share a GPU take
# about twice as long, so answer and judge requests get ten minutes.
ANSWER_SECONDS = 600


def route_pricing(config: dict, models: frozenset[str]) -> dict:
    """Prices from the route file for every route model; a missing price is refused instead of counted as free."""
    from scope_recall.runtime.model_budget import ModelPricing

    table = config.get("pricing") or {}
    missing = sorted(models - table.keys())
    if missing:
        raise ValueError("route_pricing_missing:" + ",".join(missing))
    return {
        model: ModelPricing(table[model]["input_usd_per_million"], table[model]["output_usd_per_million"])
        for model in models
    }


class EvaluatorChat:
    """Answer and judge requests the evaluator makes itself, straight to the configured route.

    They are not product operations, so the product's request checks do not apply: a public question's history
    is sent as published. Capture, consolidation and recall still run through the shipped product. As the
    product does, the key travels only over HTTPS.
    """

    def __init__(self, route: dict):
        self.route = route

    def complete(self, messages: list[dict]) -> str:
        body = {
            "model": self.route["model"],
            "messages": messages,
            self.route["output_limit_field"]: self.route["max_output_tokens"],
        }
        if self.route.get("temperature") is not None:
            body["temperature"] = self.route["temperature"]
        if self.route.get("thinking") is not None:
            body["thinking"] = self.route["thinking"]
        request = urllib.request.Request(
            self.route["endpoint"], data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        if self.route["endpoint"].startswith("https://"):
            request.add_header("Authorization", "Bearer " + os.environ[self.route["credential_env"]])
        with urllib.request.urlopen(request, timeout=ANSWER_SECONDS) as response:
            payload = json.loads(response.read())
        return payload["choices"][0]["message"]["content"] or ""


#: The embedding API refuses a burst of a few thousand texts within seconds with HTTP 429, so the evaluator keeps
#: every sixty-second window of a run, across all its question threads, under this many texts.
EMBED_TEXTS_PER_MINUTE = 2000
#: A refused request is asked again after the wait the provider names, capped here, at most this many times.
EMBED_RETRY_WAIT_CAP_SECONDS = 120.0
EMBED_RETRIES = 3
#: Without a named wait, a refused request is asked once more after this many seconds.
EMBED_UNHINTED_WAIT_SECONDS = 60.0
#: A request that times out or loses its connection is asked once more after this many seconds.
EMBED_TRANSPORT_WAIT_SECONDS = 10.0


class TextWindow:
    """Admit texts so that no sixty-second window holds more than ``limit`` of them; callers wait their turn."""

    def __init__(self, limit: int, *, clock=time.monotonic, sleep=time.sleep) -> None:
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.sent: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()

    def admit(self, count: int) -> None:
        with self.lock:
            while True:
                now = self.clock()
                while self.sent and now - self.sent[0][0] >= 60:
                    self.sent.popleft()
                if not self.sent or sum(n for _, n in self.sent) + count <= self.limit:
                    self.sent.append((now, count))
                    return
                self.sleep(60 - (now - self.sent[0][0]))


_EVALUATOR_WINDOW = TextWindow(EMBED_TEXTS_PER_MINUTE)


def retry_hint(error: urllib.error.HTTPError) -> float | None:
    """The capped wait a rate-limit reply names, from its RetryInfo detail or Retry-After header, or None."""
    hinted = error.headers.get("Retry-After") if error.headers else None
    try:
        details = json.loads(error.read() or b"{}").get("error", {}).get("details", [])
    except ValueError:
        details = []
    for detail in details:
        if str(detail.get("@type", "")).endswith("RetryInfo") and detail.get("retryDelay"):
            hinted = str(detail["retryDelay"]).removesuffix("s")
    try:
        return None if hinted is None else min(max(float(hinted), 0.0), EMBED_RETRY_WAIT_CAP_SECONDS)
    except ValueError:
        return None


class EvaluatorEmbedding:
    """Embed baseline histories directly, using the product's Gemini wire encoding.

    Public corpus text bypasses product request checks and budget accounting.
    Credentials travel only in the request header and never enter cache inputs.
    Requests share one per-minute text window per process, and an HTTP 429 is
    asked again only as the provider's reply allows.
    """

    def __init__(self, route: dict, *, window: TextWindow | None = None, sleep=time.sleep):
        from scope_recall.runtime.embedding_models import EmbeddingRouteConfig

        self.route = EmbeddingRouteConfig(**route)
        self.window = window or _EVALUATOR_WINDOW
        self.sleep = sleep

    def _post(self, request: urllib.request.Request, count: int, timeout: float) -> dict:
        """One chunk's response, waiting for the window, for each named retry wait and once after a stall."""
        retries = stalls = 0
        while True:
            self.window.admit(count)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                if error.code != 429:
                    raise
                hinted = retry_hint(error)
                if retries >= (EMBED_RETRIES if hinted is not None else 1):
                    raise
                retries += 1
                self.sleep(EMBED_UNHINTED_WAIT_SECONDS if hinted is None else hinted)
            except (TimeoutError, urllib.error.URLError):
                if stalls:
                    raise
                stalls += 1
                self.sleep(EMBED_TRANSPORT_WAIT_SECONDS)

    def _embed(self, texts, *, kind, remaining_seconds):
        from scope_recall.core.recall_policy import encode_embedding_text
        from scope_recall.runtime.embedding_models import MAX_EMBED_BATCH, _embedding_vectors, build_gemini_embed_body

        if self.route.wire_dialect() != "gemini":
            raise ValueError("evaluator_embedding_requires_gemini")
        space = self.route.space()
        encoded = [encode_embedding_text(text, kind=kind) for text in texts]
        vectors = []
        for start in range(0, len(encoded), MAX_EMBED_BATCH):
            chunk = encoded[start : start + MAX_EMBED_BATCH]
            request = urllib.request.Request(
                space["endpoint"],
                data=build_gemini_embed_body(chunk, model=space["model"], dimensions=space["dimensions"]),
                headers={"Content-Type": "application/json", "x-goog-api-key": os.environ[self.route.credential_env]},
                method="POST",
            )
            payload = self._post(request, len(chunk), remaining_seconds)
            vectors.extend(
                _embedding_vectors(payload, dialect="gemini", dimensions=space["dimensions"], count=len(chunk))
            )
        return tuple(vectors)

    def embed_texts(self, texts, remaining_seconds=30):
        """Encode documents in ordered provider-sized requests, without product screening."""
        return self._embed(texts, kind="document", remaining_seconds=remaining_seconds)

    def embed_queries(self, queries, remaining_seconds=30):
        """Encode queries separately from documents using the same configured space."""
        return self._embed(queries, kind="query", remaining_seconds=remaining_seconds)

    def embed_text(self, text, remaining_seconds=30):
        """Embed one document through the same wire path as a document group."""
        return self.embed_texts([text], remaining_seconds)[0]

    def embed_query(self, query, remaining_seconds=30):
        """Embed one query through the same wire path as a query group."""
        return self.embed_queries([query], remaining_seconds)[0]


class Ports:
    """One run's ports and recorded model settings; fake mode is entirely local."""

    def __init__(
        self, cache: RequestCache, config: dict | None = None, *, approved=(), ledger_root: Path | None = None
    ):
        self.cache = cache
        self.fake = config is None
        self.config = config or {
            "mode": "fake",
            "weights": "deterministic-substrings/3-sparse",
            "temperature": 0,
            "thinking": "disabled",
            "vector_threshold": 0.7,
            "embedding": {"model": "hash", "dimensions": 3072},
        }
        if self.fake:
            model, embedder, self.chat = FakeModel(), FakeEmbedder(), None
        else:
            model, embedder = self._runtime(config, approved, ledger_root)
            self.chat = EvaluatorChat(config["chat"])
        self.model = CachedModel(model, cache, self.config)
        self.embedder = CachedEmbedder(embedder, cache, self.config["embedding"])
        evaluator = embedder if self.fake else EvaluatorEmbedding(config["embedding"])
        self.evaluator_embedder = CachedEmbedder(evaluator, cache, self.config["embedding"])

    @staticmethod
    def _runtime(config: dict, approved, root: Path):
        """Build shipped adapters only for explicitly supplied and approved routes."""
        from scope_recall.core.worker_consolidation import build_consolidation_model
        from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig, OpenAIConsolidationAdapter
        from scope_recall.runtime.embedding_models import EmbeddingRouteConfig, GeminiEmbeddingAdapter
        from scope_recall.runtime.model_budget import (
            AuxiliaryBudgetLedger,
            default_budget_policy,
            initialize_auxiliary_budget_ledger,
        )

        routes = [config["chat"], config["embedding"]]
        if any(route["endpoint"] not in approved for route in routes):
            raise ValueError("endpoint_not_explicitly_approved")
        if not config.get("weights") or "temperature" not in config["chat"] or "thinking" not in config["chat"]:
            raise ValueError("freeze weights, temperature and thinking before running")
        models = frozenset(route["model"] for route in routes)
        policy = replace(
            default_budget_policy(batch="public-benchmark"),
            cap_micro_usd=None,
            total_input_cap=None,
            total_output_cap=None,
            total_call_cap=None,
            approved_models=models,
            pricing=route_pricing(config, models),
            max_request_bytes=16 * 1024 * 1024,
        )
        # The product only writes to a ledger an installer created; a run creates its own beside its results.
        ledger_path = (root / "model-ledger.sqlite3").resolve()
        initialize_auxiliary_budget_ledger(ledger_path, policy)
        ledger = AuxiliaryBudgetLedger(ledger_path, policy)
        chat = OpenAIConsolidationAdapter(
            ConsolidationRouteConfig(**config["chat"]), ledger=ledger, reserve_input=1_000_000
        )
        embed = GeminiEmbeddingAdapter(EmbeddingRouteConfig(**config["embedding"]), ledger=ledger)
        return build_consolidation_model(chat), embed

    def complete(self, messages: list[dict], *, operation: str) -> str:
        """Answer or judge using the same configured chat model; no gold enters answers."""

        def compute():
            if self.fake:
                return "no" if operation == "judge" else "No information available."
            return self.chat.complete(messages)

        return str(self.cache.call(operation, {"messages": messages}, self.config, {}, compute))
