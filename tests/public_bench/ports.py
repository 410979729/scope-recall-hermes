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
import urllib.request
from dataclasses import asdict, replace
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


class CachedEmbedder:
    """Keep query/document encodings in distinct cache namespaces."""

    def __init__(self, embedder, cache: RequestCache, config: dict) -> None:
        self.embedder, self.cache, self.config = embedder, cache, config

    def embed_source(self, source, remaining_seconds=1.0):
        """Cache the complete document source input and return its vector."""
        record = asdict(source)
        refs = {source.ref: f"source:{digest({key: value for key, value in record.items() if key != 'ref'})}"}
        request = {"source": record, "encoding": "document"}
        return tuple(
            self.cache.call(
                "document_embedding",
                request,
                self.config,
                refs,
                lambda: self.embedder.embed_source(source, remaining_seconds=remaining_seconds),
            )
        )

    def embed_text(self, text, remaining_seconds=1.0):
        """Cache a document encoding under its own operation namespace."""
        return tuple(
            self.cache.call(
                "document_embedding",
                {"text": text, "encoding": "document"},
                self.config,
                {},
                lambda: self.embedder.embed_text(text, remaining_seconds=remaining_seconds),
            )
        )

    def embed_query(self, query, remaining_seconds=1.0):
        """Cache query encoding separately from identical document text."""
        return tuple(
            self.cache.call(
                "query_embedding",
                {"text": query, "encoding": "query"},
                self.config,
                {},
                lambda: self.embedder.embed_query(query, remaining_seconds=remaining_seconds),
            )
        )


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
            "embedding": {"model": "hash", "dimensions": 3072},
        }
        if self.fake:
            model, embedder, self.chat = FakeModel(), FakeEmbedder(), None
        else:
            model, embedder = self._runtime(config, approved, ledger_root)
            self.chat = EvaluatorChat(config["chat"])
        self.model = CachedModel(model, cache, self.config)
        self.embedder = CachedEmbedder(embedder, cache, self.config["embedding"])

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
