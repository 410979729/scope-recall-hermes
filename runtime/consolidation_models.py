"""Consolidation requests: the chat and Responses routes' configuration, the messages and their guard, the answer's
text and usage, and the adapters that send them through the metered transport (``models``)."""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Any

from ..contracts import ContractError
from .model_budget import AuxiliaryBudgetLedger, BudgetPolicy
from .models import (
    MAX_CHAT_RESPONSE_BYTES,
    RESERVE_ENVELOPE_MARGIN,
    AuxiliaryModelError,
    HttpsTransport,
    HttpTransport,
    json_bytes,
    load_credential,
    metered_post,
    reject_secrets,
    seconds_left,
    validate_credential_env_name,
    validate_timeout_seconds,
)

#: Route kind naming the Responses-API consolidation dialect.  A stated kind is
#: required for it; the OpenAI-compatible chat route keeps accepting an absent
#: kind or ``"openai"``, so no existing installation changes meaning.
RESPONSES_KIND = "openai_responses"


#: DeepSeek's ``reasoning.effort`` vocabulary for ``/responses``.  ``minimal``,
#: ``medium`` and ``xhigh`` are also accepted by that endpoint and mapped there,
#: but a route that does not say what it sends is refused instead.
RESPONSES_EFFORTS = frozenset({"none", "low", "high", "max"})


#: Message roles a Responses ``input`` item can carry here.  ``tool`` has no item
#: shape in this adapter (``function_call``/``function_call_output`` are pairs,
#: not messages), so a tool message is refused rather than rewritten.
_RESPONSES_ROLES = frozenset({"system", "user", "assistant"})


_CHAT_ROLES = frozenset({"system", "user", "assistant", "tool"})


def _reject_secrets_outside_contents(build_body: Callable[[list[dict]], bytes], messages: list[dict]) -> None:
    """The body gate: everything a request carries besides its message contents.

    ``validate_chat_messages`` has already scanned every content as it was
    written.  Scanning the serialised body scanned each of them again through
    one more layer of escaping, where a line break inside a content reads
    ``\\\\n``: the scanner's break rule restored the break and left a backslash
    behind it, which an empty credential slot ("AppSecret:" and nothing after
    it) then took as its value: 124 of 124 candidate evaluations in one store
    passed the contents gate and were refused here, none holding a secret.
    The same builder is run on the same messages with their contents blanked,
    so the model, the route's fields and the message roles are still scanned,
    and each text is scanned once.
    """
    reject_secrets(build_body([dict(message, content="") for message in messages]).decode("utf-8"))


def validate_chat_messages(messages: object, *, roles: frozenset[str] = _CHAT_ROLES) -> None:
    """Exactly role and content per message, a role from the closed set, no secret-like text.

    ``roles`` is the closed role set of the dialect being spoken: the Responses
    route passes its own, because a role with no item shape in that dialect must
    be refused rather than reworded into one.
    """
    if not isinstance(messages, list) or not messages:
        raise AuxiliaryModelError("input_invalid")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in roles
            or type(message["content"]) is not str
        ):
            raise AuxiliaryModelError("input_invalid")
        reject_secrets(message["content"])


def _extract_chat_content(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise AuxiliaryModelError("unsupported_response_shape")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AuxiliaryModelError("unsupported_response_shape")
    if message.get("role") != "assistant":
        raise AuxiliaryModelError("unsupported_response_shape")
    # OpenAI-compatible gateways may attach reasoning/refusal/annotation
    # metadata to an assistant message.  It is not answer content and must be
    # ignored; a tool request is a different protocol and cannot be silently
    # treated as a text proposal.
    if message.get("tool_calls") or message.get("function_call"):
        raise AuxiliaryModelError("unsupported_response_shape")
    content = message.get("content")
    if type(content) is not str:
        raise AuxiliaryModelError("unsupported_response_shape")
    # "length" means the provider stopped at the output limit: the text is a
    # prefix, not an answer.  Otherwise it reaches the decoder as anonymous
    # invalid JSON and the one guided retry cannot tell the model what failed.
    if choice.get("finish_reason") == "length":
        raise ContractError("DERIVATION_INVALID", "model_output_truncated")
    return content


def _chat_usage(payload: Mapping[str, Any]) -> dict[str, int] | None:
    candidate = payload.get("usage")
    if isinstance(candidate, dict) and all(
        type(candidate.get(name)) is int and candidate[name] >= 0 for name in ("prompt_tokens", "completion_tokens")
    ):
        usage = {"prompt_tokens": candidate["prompt_tokens"], "completion_tokens": candidate["completion_tokens"]}
        cached = _cached_prompt_tokens(candidate)
        if cached is not None:
            usage["cached_prompt_tokens"] = cached
        unreported = _unreported_output_tokens(candidate)
        if unreported is not None:
            usage["unreported_output_tokens"] = unreported
        return usage
    return None


def _unreported_output_tokens(usage: Mapping[str, Any]) -> int | None:
    """Billed tokens ``total_tokens`` counts beyond the prompt and the completion.

    A thinking model can bill its reasoning without counting it in
    ``completion_tokens``: a Gemini 2.5 Flash route recorded a median of 325
    completion tokens a call while the provider's console showed roughly 8,000.
    OpenAI-style routes count reasoning inside ``completion_tokens`` and report
    a total equal to the sum, so nothing is counted twice.
    """
    total = usage.get("total_tokens")
    if type(total) is not int:
        return None
    extra = total - usage["prompt_tokens"] - usage["completion_tokens"]
    return extra if extra > 0 else None


def _cached_prompt_tokens(usage: Mapping[str, Any]) -> int | None:
    """Prompt tokens the provider says it served from its prefix cache.

    DeepSeek reports ``prompt_cache_hit_tokens``; OpenAI-compatible routes nest
    ``cached_tokens`` under ``prompt_tokens_details``.  Recorded for
    observation only: whether a prompt layout actually reuses its prefix is
    otherwise invisible from here.
    """
    details = usage.get("prompt_tokens_details")
    for value in (
        usage.get("prompt_cache_hit_tokens"),
        details.get("cached_tokens") if isinstance(details, dict) else None,
    ):
        if type(value) is int and 0 <= value <= usage["prompt_tokens"]:
            return value
    return None


def conservative_consolidation_input_reserve(body: bytes, configured: int) -> int:
    return max(configured, len(body) + RESERVE_ENVELOPE_MARGIN)


def _validate_consolidation_response_format(value: object) -> Mapping[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("response_format")
    if dict(value) != {"type": "json_object"}:
        raise ValueError("response_format")
    return MappingProxyType({"type": "json_object"})


def _validate_consolidation_reasoning_effort(value: object, *, opencode_go: bool = False) -> str | None:
    if value is None:
        return None
    allowed = {"low", "high", "max", "none"} if opencode_go else {"low", "high", "max"}
    if type(value) is not str or value not in allowed:
        raise ValueError("reasoning_effort")
    return value


_CONSOLIDATION_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")


_CONSOLIDATION_HEADER_RESERVED = {"authorization", "content-type", "user-agent", "host", "content-length"}


_OPENCODE_GO_SESSION_HEADER = "x-opencode-session"


_OPENCODE_GO_DEFAULT_SESSION = "scope-recall-auxiliary-consolidation"


def _validate_consolidation_headers(value: object) -> Mapping[str, str] | None:
    """Optional provider-specific static headers (e.g. routing session ids).

    Credential-bearing or transport-owned header names are rejected so this
    channel can never smuggle a second Authorization or override the explicit
    Content-Type/User-Agent set by the client.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("headers")
    if len(value) > 8:
        raise ValueError("headers")
    clean: dict[str, str] = {}
    for raw_name, raw_val in value.items():
        if type(raw_name) is not str or _CONSOLIDATION_HEADER_NAME_RE.fullmatch(raw_name) is None:
            raise ValueError("headers")
        if raw_name.casefold() in _CONSOLIDATION_HEADER_RESERVED:
            raise ValueError("headers")
        if type(raw_val) is not str:
            raise ValueError("headers")
        text = raw_val.strip()
        if not text or len(text) > 512 or "\r" in text or "\n" in text:
            raise ValueError("headers")
        clean[raw_name] = text
    return MappingProxyType(clean)


def _opencode_go_endpoint(endpoint: str) -> bool:
    if type(endpoint) is not str:
        return False
    parsed = urllib.parse.urlsplit(endpoint)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "opencode.ai"
        and (parsed.path == "/zen/go" or parsed.path.startswith("/zen/go/"))
    )


def _opencode_session_id() -> str:
    for name in ("SCOPE_RECALL_TEST_OPENCODE_SESSION", "SCOPE_RECALL_P11_TEST_CONTEXT"):
        value = os.environ.get(name)
        if type(value) is not str:
            continue
        text = value.strip()
        if not text or len(text) > 512 or "\r" in text or "\n" in text:
            continue
        if name == "SCOPE_RECALL_P11_TEST_CONTEXT":
            return "scope-recall-test-" + text
        return text
    return _OPENCODE_GO_DEFAULT_SESSION


def _consolidation_request_headers(route: "ConsolidationRouteConfig", key: str) -> dict[str, str]:
    headers = {
        **dict(route.headers or {}),
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "ScopeRecall-AuxiliaryConsolidation/1.1",
    }
    if _opencode_go_endpoint(route.endpoint) and not any(
        name.casefold() == _OPENCODE_GO_SESSION_HEADER for name in headers
    ):
        headers[_OPENCODE_GO_SESSION_HEADER] = _opencode_session_id()
    return headers


def model_output_reserve(policy: BudgetPolicy, model: str, requested_output: int) -> int:
    floor = policy.model_reserve_output.get(model, policy.default_reserve_output)
    return max(requested_output, floor)


@dataclass(frozen=True)
class ConsolidationRouteConfig:
    model: str
    endpoint: str
    credential_env: str
    output_limit_field: str
    max_output_tokens: int
    thinking: Mapping[str, str] | None = None
    response_format: Mapping[str, str] | None = None
    reasoning_effort: str | None = None
    stream: bool = False
    n: int = 1
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        _validate_route_target(self.model, self.endpoint, self.credential_env)
        if self.output_limit_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("output_limit_field")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 131_072:
            raise ValueError("max_output_tokens")
        if self.stream is not False:
            raise ValueError("stream")
        if type(self.n) is not int or self.n != 1:
            raise ValueError("n")
        if self.thinking is not None and not isinstance(self.thinking, Mapping):
            raise ValueError("thinking")
        object.__setattr__(self, "response_format", _validate_consolidation_response_format(self.response_format))
        object.__setattr__(
            self,
            "reasoning_effort",
            _validate_consolidation_reasoning_effort(
                self.reasoning_effort, opencode_go=_opencode_go_endpoint(self.endpoint)
            ),
        )
        object.__setattr__(self, "headers", _validate_consolidation_headers(self.headers))


def _validate_route_target(model: object, endpoint: object, credential_env: object) -> None:
    """A consolidation route names a model, an https endpoint and the environment variable holding its key."""
    if type(model) is not str or not model:
        raise ValueError("model")
    if type(endpoint) is not str or not endpoint.startswith("https://"):
        raise ValueError("endpoint")
    validate_credential_env_name(credential_env)


def _validate_responses_text_format(value: object) -> Mapping[str, str] | None:
    """Only JSON mode is implemented.

    ``{"type": "text"}`` is the endpoint's default and is sent by omitting the
    field; ``json_schema`` would need the schema it names to be validated here
    rather than silently forwarded, so it is refused until that exists.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping) or dict(value) != {"type": "json_object"}:
        raise ValueError("text_format")
    return MappingProxyType({"type": "json_object"})


@dataclass(frozen=True)
class ResponsesRouteConfig:
    """One non-streaming Responses-API consolidation route.

    Implemented for the documented DeepSeek ``POST https://api.deepseek.com/responses`` contract
    (``model: deepseek-flash``): that endpoint accepts a string or an item list in
    ``input``, inserts ``instructions`` as the first system message, reports
    ``status`` as ``completed``/``incomplete``/``failed``, and answers with an
    ``output`` array of ``reasoning`` and ``message`` items.  Nothing here claims
    streaming (``stream`` must be ``false``), OAuth, or another provider's
    compatibility -- a route is configuration, and this one says exactly what
    this adapter sends.
    """

    model: str
    endpoint: str
    credential_env: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    text_format: Mapping[str, str] | None = None
    stream: bool = False
    kind: str = RESPONSES_KIND

    def __post_init__(self) -> None:
        _validate_route_target(self.model, self.endpoint, self.credential_env)
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 131_072:
            raise ValueError("max_output_tokens")
        if self.reasoning_effort is not None and (
            type(self.reasoning_effort) is not str or self.reasoning_effort not in RESPONSES_EFFORTS
        ):
            raise ValueError("reasoning_effort")
        object.__setattr__(self, "text_format", _validate_responses_text_format(self.text_format))
        if self.stream is not False:
            raise ValueError("stream")
        if self.kind != RESPONSES_KIND:
            raise ValueError("kind")


class _ConsolidationAdapter:
    """What both consolidation dialects share: the route, the ledger and the transport, and how a request is sent."""

    def __init__(
        self,
        route: ConsolidationRouteConfig | ResponsesRouteConfig,
        *,
        ledger: AuxiliaryBudgetLedger,
        reserve_input: int,
        transport: HttpTransport | None = None,
    ) -> None:
        self._route = route
        self._ledger = ledger
        self._reserve_input = reserve_input
        self._transport = transport if transport is not None else HttpsTransport()

    def _send(self, body: bytes, deadline: float, *, headers, read_usage, read_result) -> str:
        """One proposal request, metered; refused when its time is up or the provider is holding calls.  ``headers``
        makes the request's headers from the key."""
        reserved_output = model_output_reserve(self._ledger.policy, self._route.model, self._route.max_output_tokens)
        if seconds_left(deadline) <= 0:
            raise AuxiliaryModelError("timeout")
        if self._ledger.provider_hold_until(self._route.model) is not None:
            raise AuxiliaryModelError("provider_hold")
        key = load_credential(self._route.credential_env)
        return metered_post(
            ledger=self._ledger,
            settle=self._ledger.finish,
            model=self._route.model,
            body=body,
            reserved_input=conservative_consolidation_input_reserve(body, self._reserve_input),
            reserved_output=reserved_output,
            deadline=deadline,
            transport=self._transport,
            endpoint=self._route.endpoint,
            headers=headers(key),
            # The consolidation answer cap, shared by both dialects.
            max_response_bytes=MAX_CHAT_RESPONSE_BYTES,
            read_usage=read_usage,
            read_result=read_result,
        )


class OpenAIConsolidationAdapter(_ConsolidationAdapter):
    _route: ConsolidationRouteConfig

    def _chat_body(self, messages: list[dict]) -> bytes:
        route = self._route
        body: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "stream": route.stream,
            "n": route.n,
            route.output_limit_field: route.max_output_tokens,
        }
        if route.thinking is not None:
            body["thinking"] = dict(route.thinking)
        if route.response_format is not None:
            body["response_format"] = dict(route.response_format)
        if route.reasoning_effort is not None:
            body["reasoning_effort"] = route.reasoning_effort
        return json_bytes(body)

    def propose(self, messages: list[dict], *, remaining_seconds: float) -> str:
        deadline = time.monotonic() + validate_timeout_seconds(remaining_seconds)
        validate_chat_messages(messages)
        body = self._chat_body(messages)
        _reject_secrets_outside_contents(self._chat_body, messages)
        return self._send(
            body,
            deadline,
            headers=partial(_consolidation_request_headers, self._route),
            read_usage=_chat_usage,
            read_result=_extract_chat_content,
        )


def _responses_usage(payload: Mapping[str, Any]) -> dict[str, int] | None:
    """The ledger's prompt/completion pair from a Responses ``usage`` block.

    ``output_tokens`` already counts the reasoning tokens the provider reports
    separately in ``output_tokens_details.reasoning_tokens`` (the same tokens
    ``max_output_tokens`` bounds), so reasoning is never billed a second time --
    the anomaly guard that charges output outside ``completion_tokens`` on the
    chat route does not apply here.  An absent or malformed block returns
    ``None`` and the caller keeps the reserved charge, exactly as before.
    """
    candidate = payload.get("usage")
    if not isinstance(candidate, dict):
        return None
    if any(type(candidate.get(name)) is not int or candidate[name] < 0 for name in ("input_tokens", "output_tokens")):
        return None
    usage = {"prompt_tokens": candidate["input_tokens"], "completion_tokens": candidate["output_tokens"]}
    details = candidate.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if type(cached) is int and 0 <= cached <= candidate["input_tokens"]:
        usage["cached_prompt_tokens"] = cached
    return usage


def _responses_output_text(payload: Mapping[str, Any]) -> str:
    """The completed assistant answer, and nothing else.

    Only a ``response`` whose own ``status`` is ``completed`` is an answer:
    ``incomplete`` is a prefix cut off at the output limit (the same named
    derivation failure the chat route raises for ``finish_reason: "length"``),
    and ``failed`` produced nothing usable.  Reasoning items and
    ``reasoning_text`` parts are never answer text, a refusal part is a refusal
    rather than an empty proposal, and a tool-call item is a different protocol
    that cannot be silently read as one.
    """
    status = payload.get("status")
    if status != "completed":
        if status == "incomplete":
            raise ContractError("DERIVATION_INVALID", "model_output_truncated")
        if status == "failed":
            raise AuxiliaryModelError("response_status_failed")
        raise AuxiliaryModelError("unsupported_response_shape")
    output = payload.get("output")
    if not isinstance(output, list):
        raise AuxiliaryModelError("unsupported_response_shape")
    answers: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise AuxiliaryModelError("unsupported_response_shape")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            raise AuxiliaryModelError("unsupported_response_shape")
        if item.get("role") != "assistant" or item.get("status") not in (None, "completed"):
            # A message that is not this route's answer, or one the response
            # marks unfinished while claiming to be complete, is a contradiction
            # rather than something to assemble an answer out of.
            raise AuxiliaryModelError("unsupported_response_shape")
        content = item.get("content")
        if not isinstance(content, list):
            raise AuxiliaryModelError("unsupported_response_shape")
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                raise AuxiliaryModelError("unsupported_response_shape")
            if part.get("type") == "refusal":
                raise AuxiliaryModelError("model_refused")
            if part.get("type") != "output_text" or type(part.get("text")) is not str:
                raise AuxiliaryModelError("unsupported_response_shape")
            parts.append(part["text"])
        answers.append("".join(parts))
    text = "\n\n".join(answers)
    if not text:
        raise AuxiliaryModelError("empty_output")
    return text


class ResponsesConsolidationAdapter(_ConsolidationAdapter):
    """One non-streaming Responses route on the shared consolidation boundary.

    Reservation, transport, deadline, response cap, settlement and the raw
    answer handed to the existing proposal validator are the chat route's; only
    the request dialect and the answer extraction differ.
    """

    _route: ResponsesRouteConfig

    def _responses_body(self, messages: list[dict]) -> bytes:
        """Carry every message in ``input`` without moving system messages.

        Each message becomes one item whose text part is typed for its role.
        Using ``instructions`` would move interleaved system messages to the
        beginning, so this adapter deliberately keeps them in ``input``.
        ``store`` is false and the caller supplies all context explicitly.
        """
        route = self._route
        items: list[dict[str, Any]] = []
        for message in messages:
            role, content = message["role"], message["content"]
            items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [
                        {
                            "type": "output_text" if role == "assistant" else "input_text",
                            "text": content,
                        }
                    ],
                }
            )
        body: dict[str, Any] = {
            "model": route.model,
            "max_output_tokens": route.max_output_tokens,
            "stream": route.stream,
            "store": False,
        }
        body["input"] = items
        if route.reasoning_effort is not None:
            body["reasoning"] = {"effort": route.reasoning_effort}
        if route.text_format is not None:
            body["text"] = {"format": dict(route.text_format)}
        return json_bytes(body)

    def propose(self, messages: list[dict], *, remaining_seconds: float) -> str:
        deadline = time.monotonic() + validate_timeout_seconds(remaining_seconds)
        validate_chat_messages(messages, roles=_RESPONSES_ROLES)
        body = self._responses_body(messages)
        _reject_secrets_outside_contents(self._responses_body, messages)
        return self._send(
            body,
            deadline,
            headers=lambda key: {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "ScopeRecall-AuxiliaryConsolidation/1.1",
            },
            read_usage=_responses_usage,
            read_result=_responses_output_text,
        )
