"""
LLM-powered semantic capture for scope-recall.

Extracts structured knowledge from user+assistant turns using a
configurable lightweight LLM — produces classified candidates instead of
dumping raw user messages into general.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

try:  # Support both package imports and direct manual test imports.
    from .http_utils import chat_completions_endpoint, redact_sensitive
except ImportError:  # pragma: no cover - exercised by manual script import style
    from http_utils import chat_completions_endpoint, redact_sensitive

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM_PROMPT = """You are a knowledge extraction engine for an AI assistant's memory system.
Analyze the conversation turn below and extract durable knowledge worth remembering.
Output JSON only — no explanations, no markdown outside the JSON.

For each extractable piece, produce:
{
  "action": "insert",
  "content": "Concise 1-3 sentence summary capturing the essential knowledge",
  "target": "user|memory|project|ops",
  "memory_type": "preference|factual|procedure|project|workflow|pitfall|decision|resource",
  "entities": ["named", "entities"],
  "tags": ["topic", "tags"]
}

If nothing is worth saving, return [].

RULES:
1. Extract BOTH the user's question/intent AND the assistant's approach/actions/results
2. If the assistant made a mistake and then fixed it → capture both the pitfall AND the fix as separate entries
3. If the assistant successfully completed a task → capture the workflow
4. If the user expressed a preference or correction → capture as user preference
5. If configuration, environment, or convention was discovered → capture as factual
6. NEVER include passwords, tokens, API keys, or secrets in content — redact them
7. Use the same language the user speaks (Chinese → Chinese, English → English)
8. Each content entry must be self-contained and understandable without context
9. If a design decision was made with explicit trade-offs → capture the WHY (e.g., "chose urllib over requests to avoid adding external dependency")
10. If the assistant rejected an alternative approach → capture the rationale as a separate decision entry
11. If the assistant references a prior session, bug, or earlier work → include that context so the memory chain is traceable
12. target meanings:
   - "user": user identity, preferences, habits, personal info
   - "memory": environment facts, conventions, tool quirks, lessons
   - "project": project milestones, version bumps, feature decisions
   - "ops": operations, debugging, deployment, server administration

Return ONLY a JSON array. Example:
[{"action":"insert","content":"User prefers directory names with dots instead of underscores for project folders.","target":"user","memory_type":"preference","entities":[],"tags":["naming","convention"]}]"""


_VALID_MEMORY_TYPES: frozenset[str] = frozenset({
    "preference", "factual", "procedure", "project", "episodic",
    "resource", "constraint", "workflow", "tool_trace", "summary",
    "pitfall", "decision",
})


def _truthy(value: Any) -> bool:
    """Interpret a config value as a boolean, handling JSON bools
    and string representations from Hermes config UI."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


@dataclass
class Candidate:
    content: str
    target: str
    memory_type: str = "factual"
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.8


def extract_capture_candidates(
    user_content: str,
    assistant_content: str,
    config: dict[str, Any],
) -> list[Candidate]:
    """Run LLM semantic extraction on a user+assistant turn.

    Returns a list of Candidate objects ready for enqueue_store.
    Returns an empty list if LLM extraction is disabled, not configured,
    or fails — callers should fall back to regex extraction.
    """
    llm_config = config.get("capture_llm")
    if not isinstance(llm_config, dict):
        return []
    if not _truthy(llm_config.get("enabled")):
        return []

    model = str(llm_config.get("model", "gpt-4o-mini"))
    base_url = str(llm_config.get("base_url", "https://api.openai.com")).rstrip("/")
    endpoint = str(llm_config.get("endpoint") or llm_config.get("chat_endpoint") or "").rstrip("/")
    append_v1 = _truthy(llm_config.get("append_v1", True)) if "append_v1" in llm_config else True
    timeout = float(llm_config.get("timeout", 15.0))
    max_tokens = int(llm_config.get("max_tokens_per_turn", 2000))

    api_key = _resolve_api_key(llm_config)
    if not api_key:
        logger.warning(
            "scope-recall capture_llm: no API key found (env or config), "
            "skipping LLM extraction"
        )
        return []

    # Truncate inputs to keep token cost bounded
    user_block = user_content[:2500] if user_content else "(empty)"
    assistant_block = assistant_content[:2500] if assistant_content else "(empty)"

    messages: list[dict[str, str]] = [
        {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"USER MESSAGE:\n{user_block}\n\nASSISTANT RESPONSE:\n{assistant_block}",
        },
    ]

    try:
        raw = _call_openai_compatible(base_url, api_key, model, messages, max_tokens, timeout, endpoint=endpoint, append_v1=append_v1)
    except Exception as exc:
        logger.warning(f"scope-recall capture_llm: API call failed: {redact_sensitive(exc)}")
        return []

    return _parse_response(raw)


# ── helpers ──────────────────────────────────────────────────────────


def _resolve_api_key(llm_config: dict[str, Any]) -> str:
    """Resolve API key from env vars or direct config value."""
    env_names = llm_config.get("api_key_env")
    if isinstance(env_names, str):
        env_names = [env_names]
    if not env_names:
        env_names = ["SCOPE_RECALL_CAPTURE_LLM_API_KEY", "OPENAI_API_KEY"]

    for name in env_names:
        value = os.environ.get(str(name))
        if value:
            return value

    direct = llm_config.get("api_key")
    if direct and str(direct).strip():
        return str(direct).strip()

    return ""


def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float,
    *,
    endpoint: str = "",
    append_v1: bool = True,
) -> str:
    """Call an OpenAI-compatible chat completions endpoint."""
    url = chat_completions_endpoint(base_url, endpoint=endpoint, append_v1=append_v1)
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"LLM HTTP {exc.code} at {url}: {body}") from exc

    choices = result.get("choices")
    if not choices:
        raise ValueError("LLM response contained no choices")
    content = choices[0].get("message", {}).get("content", "")
    return str(content)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def _log_parse_failure(reason: str, raw: str) -> None:
    logger.warning("scope-recall capture_llm: %s (raw_len=%d)", reason, len(raw or ""))


def _repair_truncated(text: str) -> str:
    """Attempt to repair JSON truncated by max_tokens cutoff.

    Heuristic: count unmatched opening brackets/braces/quotes,
    then append matching closing characters.
    Returns repaired string or empty string if unfixable.
    """
    if not text or len(text) < 3:
        return ""

    # Remove trailing incomplete fragments (e.g., '{"key": "val')
    # by finding the last valid structural character
    stripped = text.rstrip()
    if not stripped:
        return ""

    # Stack-based repair for brackets and braces
    stack: list[str] = []
    in_string = False
    escape_next = False
    for ch in stripped:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("[", "{"):
            stack.append(ch)
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()

    # If we're still inside a string, close it
    suffix = ""
    if in_string:
        suffix += '"'

    # Close any remaining open brackets/braces in reverse order
    for opener in reversed(stack):
        if opener == "[":
            suffix += "]"
        elif opener == "{":
            suffix += "}"

    if not suffix:
        return ""  # nothing to repair — likely not a truncation issue

    repaired = stripped + suffix
    return repaired


def _parse_response(raw: str) -> list[Candidate]:
    """Parse LLM response into Candidate list, gracefully handling
    markdown fences and partial JSON."""
    raw = raw.strip()
    if not raw:
        return []

    # Try extracting JSON from markdown code fences
    fenced = _JSON_FENCE_RE.findall(raw)
    if fenced:
        raw = "\n".join(fenced)

    # Try direct parse
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find a JSON array anywhere in the output
        match = _JSON_ARRAY_RE.search(raw)
        if match:
            json_text = match.group()
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                # Try to repair truncated JSON (max_tokens cutoff)
                repaired = _repair_truncated(json_text)
                if repaired:
                    try:
                        data = json.loads(repaired)
                    except json.JSONDecodeError:
                        _log_parse_failure("JSON parse failed even after repair", raw)
                        return []
                else:
                    _log_parse_failure("JSON parse failed", raw)
                    return []
        else:
            # No JSON array found — try repairing raw text as last resort
            repaired = _repair_truncated(raw)
            if repaired:
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError:
                    _log_parse_failure("no JSON array found", raw)
                    return []
            else:
                _log_parse_failure("no JSON array found", raw)
                return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    candidates: list[Candidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = item.get("action", "")
        if action == "skip":
            continue
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue

        target = str(item.get("target", "memory")).strip().lower()
        if target not in ("user", "memory", "project", "ops"):
            target = "general"

        memory_type = str(item.get("memory_type", "factual")).strip().lower()
        if memory_type not in _VALID_MEMORY_TYPES:
            memory_type = "factual"

        entities: list[str] = []
        raw_entities = item.get("entities")
        if isinstance(raw_entities, list):
            entities = [str(e) for e in raw_entities if str(e).strip()]

        tags: list[str] = []
        raw_tags = item.get("tags")
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags if str(t).strip()]

        candidates.append(
            Candidate(
                content=content,
                target=target,
                memory_type=memory_type,
                entities=entities,
                tags=tags,
            )
        )

    return candidates
