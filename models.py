"""Core dataclasses and normalization helpers shared by provider, recall, migration, and vector code.

Keep these models small and serializable so they can cross tool, CLI, and test boundaries safely."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class RecallItem:
    id: str
    content: str
    summary: str
    source: str
    target: str
    score: float
    updated_at: str
    metadata: dict[str, Any] | None = None


@dataclass
class RuntimeScope:
    platform: str = "cli"
    user_id: str = ""
    chat_id: str = ""
    thread_id: str = ""
    gateway_session_key: str = ""
    agent_identity: str = ""
    agent_workspace: str = ""
    agent_context: str = "primary"


@dataclass
class ImportedMemoryRow:
    id: str
    scope_id: str
    platform: str
    user_id: str
    chat_id: str
    thread_id: str
    gateway_session_key: str
    agent_identity: str
    agent_workspace: str
    session_id: str
    source: str
    target: str
    content: str
    summary: str
    created_at: str
    updated_at: str
    import_metadata: str
    import_fingerprint: str


@dataclass
class VectorIndexRecord:
    id: str
    scope_id: str
    source: str
    target: str
    content: str
    summary: str
    updated_at: str

    def to_payload(self, vector: list[float]) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_id": self.scope_id,
            "source": self.source,
            "target": self.target,
            "content": self.content,
            "summary": self.summary,
            "updated_at": self.updated_at,
            "vector": vector,
        }


def recall_scope_mode(target: str, source: str = "") -> str:
    """Classify a memory row as shared or local.

    User/project/ops/memory facts are durable by default and should be reusable
    across windows/chats for the same user + agent profile. Raw turn transcripts
    and one-off general captures remain local to the chat/thread/session scope.
    """

    normalized_target = str(target or "memory").strip().lower()
    normalized_source = str(source or "").strip().lower()
    if normalized_target in {"user", "memory", "project", "ops"}:
        return "shared"
    if normalized_source == "tool-store" and normalized_target != "general":
        return "shared"
    return "local"


def json_dumps_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_import_timestamp(raw_ts: Any) -> str:
    if raw_ts not in (None, ""):
        try:
            return datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def normalize_import_fingerprint_timestamp(raw_ts: Any) -> str:
    """Return stable timestamp material for import fingerprints.

    ``normalize_import_timestamp`` may use the current import time for row
    timestamps when legacy data is malformed. Fingerprints must not do that, or
    rerunning an import would create a new id for the same source row.
    """

    if raw_ts not in (None, ""):
        try:
            return datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc).isoformat()
        except Exception:
            return f"invalid:{str(raw_ts).strip()}"
    return "missing"


def build_import_fingerprint(*, raw_scope: str, category: str, text: str, timestamp: str, metadata_text: str, source_id: str = "") -> str:
    material = "\n".join([source_id.strip(), raw_scope.strip(), category.strip(), text.strip(), timestamp.strip(), metadata_text.strip()])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()
