"""Normalize local conversation exports without importing attachments or tool payloads."""

from __future__ import annotations

from pathlib import Path

from scope_recall.contracts import ContractError
from scope_recall.core.memory_import import ImportRecord

from .import_read import json_documents, timestamp


def _conversations(document):
    if not isinstance(document, list) or any(not isinstance(c, dict) for c in document):
        raise ContractError("INPUT_INVALID", "import_conversations")
    return document


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p["text"]
            for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        )
    if isinstance(content, dict):
        return "\n".join(p for p in content.get("parts", []) if isinstance(p, str))
    return ""


def read_chatgpt(path: Path):
    """Read every explicit user/assistant node in the export's conversation mapping."""
    for document in json_documents(path):
        for chat in _conversations(document):
            session = chat.get("id") or chat.get("conversation_id")
            if not session or not isinstance(chat.get("mapping"), dict):
                raise ContractError("INPUT_INVALID", "import_chatgpt_mapping")
            for node_key, node in chat["mapping"].items():
                message = node.get("message")
                if message is None:
                    continue
                role = message.get("author", {}).get("role")
                if role not in {"user", "assistant"}:
                    continue
                text = _text(message.get("content"))
                if not text.strip():
                    continue
                yield ImportRecord(
                    str(message.get("id") or node_key),
                    text,
                    role,
                    timestamp(message.get("create_time")),
                    chat.get("project_id"),
                    str(session),
                    "human_direct" if role == "user" else "assistant_visible",
                )


def read_claude(path: Path):
    """Read human/assistant messages, preferring structured text blocks over placeholder text."""
    for document in json_documents(path, prefixes=("conversations", "memories")):
        documents = [document] if isinstance(document, dict) else _conversations(document)
        for chat in documents:
            if "conversations_memory" in chat or "project_memories" in chat:
                yield from _claude_memories(chat)
                continue
            if not chat.get("uuid") or not isinstance(chat.get("chat_messages"), list):
                raise ContractError("INPUT_INVALID", "import_claude_messages")
            for message in chat["chat_messages"]:
                sender = message.get("sender")
                if sender not in {"human", "assistant"}:
                    continue
                text = _text(message.get("content")) or _text(message.get("text"))
                if not message.get("uuid"):
                    raise ContractError("INPUT_INVALID", "import_message_identity")
                if text.strip():
                    yield ImportRecord(
                        message["uuid"],
                        text,
                        "user" if sender == "human" else "assistant",
                        timestamp(message.get("created_at")),
                        chat.get("project_uuid"),
                        chat["uuid"],
                        "human_direct" if sender == "human" else "assistant_visible",
                    )


def _claude_memories(document):
    global_memory = document.get("conversations_memory", "")
    projects = document.get("project_memories", {})
    if not isinstance(global_memory, str) or not isinstance(projects, dict):
        raise ContractError("INPUT_INVALID", "import_claude_memories")
    for project, text in [(None, global_memory), *projects.items()]:
        if not isinstance(text, str) or project is not None and not isinstance(project, str):
            raise ContractError("INPUT_INVALID", "import_claude_memory")
        if text.strip():
            yield ImportRecord(
                "memory:" + (project or "global"),
                text,
                "assistant",
                project=project,
                session=document.get("account_uuid"),
                original_origin="assistant_visible",
            )
