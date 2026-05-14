from __future__ import annotations

_ALIAS_MAP = {
    "answer": "reply",
    "answers": "reply",
    "command": "command",
    "commands": "command",
    "concise": "concise",
    "deploy": "deploy",
    "deployment": "deploy",
    "deployments": "deploy",
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
