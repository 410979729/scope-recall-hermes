"""Installed-package capabilities and chat protocol conversion for paired evaluation."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import threading
from importlib.resources import files
from pathlib import Path


def digest(value):
    """Hash canonical evaluation metadata without reading credential values."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def capabilities():
    """Read the installed contract; unsupported fields never enter recorded replies."""
    schema = json.loads(files("scope_recall").joinpath("contracts/consolidation_result.schema.json").read_text("utf-8"))
    span = schema["properties"]["claim_proposals"]["items"]["properties"]["evidence_spans"]["items"]
    if "$ref" in span:
        reference = span["$ref"].removeprefix("#/").split("/")
        span = schema
        for key in reference:
            span = span[key]
    positions = "start" in span["properties"]
    semantic = importlib.util.find_spec("scope_recall.core.semantic_proposals") is not None
    from scope_recall.core.consolidate import ConsolidationWorkFence

    discards = "rejected_claims" in ConsolidationWorkFence.__dataclass_fields__
    return dict(
        proposal_discards="applicable" if discards else "not_applicable",
        evidence_positions="applicable" if positions else "not_applicable",
        model_merge="applicable" if semantic else "not_applicable",
        claim_merge="native_claim_publication",
        extraction="native_worker",
        candidate="native_worker",
    )


def package_identity():
    """Identify the installed subject rather than the checkout owning this harness."""
    import scope_recall

    directory = Path(scope_recall.__file__).resolve().parent
    names = sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)
    manifest = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in names}
    return dict(
        version=importlib.metadata.version("hermes-scope-recall"),
        location=str(directory),
        python_sha256=digest(manifest),
        capabilities=capabilities(),
    )


def one_system(messages):
    """Join leading system texts verbatim for transports requiring exactly one leading system message."""
    systems, rest = [], []
    for message in messages:
        if message["role"] == "system":
            if rest:
                raise ValueError("a system message follows user content")
            systems.append(message["content"])
        else:
            rest.append(dict(message))
    if not systems:
        raise ValueError("a leading system message is required")
    return [dict(role="system", content="\n\n".join(systems)), *rest]


def operation(messages):
    """Classify native requests for coverage and cost accounting without changing their contents."""
    if any(m["content"].startswith("candidate=") for m in messages if m["role"] == "user"):
        return "candidate"
    for message in messages:
        if message["role"] == "user" and message["content"].startswith("{"):
            body = json.loads(message["content"])
            if "proposal" in body:
                return "merge" if body.get("candidates") else "support"
    return "extraction"


class ChatPort:
    """Send one approved chat request without retries; plaintext routes never carry a credential."""

    def __init__(self, config, approved, *, transport=None):
        from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig
        from scope_recall.runtime.models import HttpsTransport

        self.route = ConsolidationRouteConfig(**config["chat"])
        if self.route.endpoint not in approved or not config.get("weights"):
            raise ValueError("an exact approved endpoint and frozen model weights are required")
        self.transport = transport or HttpsTransport(allow_insecure_endpoint=self.route.allow_insecure_endpoint)
        self.lock = threading.Lock()
        self.stopped = False
        self.requests = 0
        self.usage = dict(prompt_tokens=0, completion_tokens=0)

    def propose(self, messages, *, remaining_seconds):
        """Use the configured sampling fields and return the server's unmodified answer text."""
        from scope_recall.runtime.consolidation_models import validate_chat_messages
        from scope_recall.runtime.models import AuxiliaryModelError, load_credential

        messages = one_system(messages)
        validate_chat_messages(messages)
        with self.lock:
            if self.stopped:
                raise AuxiliaryModelError("model_unavailable")
        route = self.route
        headers = {"Content-Type": "application/json"}
        if route.endpoint.startswith("https:"):
            headers["Authorization"] = "Bearer " + load_credential(route.credential_env)
        body = dict(model=route.model, messages=one_system(messages))
        body[route.output_limit_field] = route.max_output_tokens
        for key in ("temperature", "thinking", "response_format"):
            value = getattr(route, key, None)
            if value is not None:
                body[key] = dict(value) if hasattr(value, "items") else value
        with self.lock:
            if self.stopped:
                raise AuxiliaryModelError("model_unavailable")
            self.requests += 1
        try:
            status, raw = self.transport.post(
                route.endpoint,
                body=json.dumps(body, ensure_ascii=False).encode(),
                headers=headers,
                timeout_seconds=remaining_seconds,
                max_response_bytes=16 * 1024 * 1024,
            )
        except (RuntimeError, OSError):
            with self.lock:
                self.stopped = True
            raise
        if status != 200:
            with self.lock:
                self.stopped = True
            raise AuxiliaryModelError("http_status", detail=str(status))
        try:
            result = json.loads(raw)
            choice = result["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            with self.lock:
                self.stopped = True
            raise AuxiliaryModelError("unsupported_response_shape") from exc
        if choice.get("finish_reason") not in (None, "stop"):
            raise AuxiliaryModelError("output_truncated")
        if not isinstance(text, str) or not text.strip():
            raise AuxiliaryModelError("empty_output")
        with self.lock:
            for key in self.usage:
                self.usage[key] += result.get("usage", {}).get(key, 0)
        return text

    def close(self):
        """Close only the transport owned by this evaluation."""
        self.transport.close()
