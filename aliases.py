"""Alias normalization helpers for compatibility with older Scope Recall tool and provider names.

Keep this layer small: callers should canonicalize once at the boundary, then use stable tool/provider identifiers internally."""

from __future__ import annotations

_ALIAS_MAP = {
    "answer": "reply",
    "answers": "reply",
    "brief": "concise",
    "command": "command",
    "commands": "command",
    "concise": "concise",
    "deploy": "deploy",
    "deployment": "deploy",
    "deployments": "deploy",
    "direct": "concise",
    "gateway": "gateway",
    "like": "prefer",
    "likes": "prefer",
    "prefer": "prefer",
    "preference": "prefer",
    "preferences": "prefer",
    "prefers": "prefer",
    "prod": "prod",
    "production": "prod",
    "release": "deploy",
    "releases": "deploy",
    "reply": "reply",
    "replies": "reply",
    "response": "reply",
    "responses": "reply",
    "short": "concise",
    "restart": "restart",
    "restarts": "restart",
    "rollout": "deploy",
    "rollouts": "deploy",
    "ship": "deploy",
    "shipping": "deploy",
    "style": "style",
    "tone": "style",
    "use": "use",
    "uses": "use",
    "warm": "warm",
}


def canonicalize_alias(token: str) -> str:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return ""
    return _ALIAS_MAP.get(normalized, normalized)


__all__ = ["canonicalize_alias"]
