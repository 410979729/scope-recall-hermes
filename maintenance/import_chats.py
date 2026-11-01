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


def _identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError("INPUT_INVALID", "import_message_identity")
    return value


def _chatgpt_text(content):
    if (
        not isinstance(content, dict)
        or not isinstance(content.get("content_type"), str)
        or content["content_type"] not in {"text", "code", "multimodal_text"}
    ):
        raise ContractError("INPUT_INVALID", "import_chatgpt_content")
    if content["content_type"] == "code":
        if not isinstance(content.get("text"), str):
            raise ContractError("INPUT_INVALID", "import_chatgpt_code")
        return content["text"]
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise ContractError("INPUT_INVALID", "import_chatgpt_parts")
    text = []
    for part in parts:
        if isinstance(part, str):
            text.append(part)
        elif (
            content["content_type"] == "multimodal_text"
            and isinstance(part, dict)
            and isinstance(part.get("content_type"), str)
            and part["content_type"] in {"image_asset_pointer", "audio_asset_pointer"}
            and isinstance(part.get("asset_pointer"), str)
            and bool(part["asset_pointer"])
        ):
            continue
        else:
            raise ContractError("INPUT_INVALID", "import_chatgpt_part")
    return "\n".join(text)


def _claude_text(message):
    if "content" not in message or message["content"] is None:
        if not isinstance(message.get("text"), str):
            raise ContractError("INPUT_INVALID", "import_claude_text")
        return message["text"]
    if not isinstance(message["content"], list):
        raise ContractError("INPUT_INVALID", "import_claude_content")
    text = []
    for block in message["content"]:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ContractError("INPUT_INVALID", "import_claude_block")
        if block["type"] == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block["type"] not in {"image", "document", "tool_use", "tool_result", "thinking", "redacted_thinking"}:
            raise ContractError("INPUT_INVALID", "import_claude_block")
    return "\n".join(text)


def read_chatgpt(path: Path):
    """Read every explicit user/assistant node in the export's conversation mapping."""
    for document in json_documents(path):
        for chat in _conversations(document):
            session = chat.get("id") or chat.get("conversation_id")
            session = _identity(session)
            if not isinstance(chat.get("mapping"), dict):
                raise ContractError("INPUT_INVALID", "import_chatgpt_mapping")
            for node_key, node in chat["mapping"].items():
                if not isinstance(node, dict) or "message" not in node:
                    raise ContractError("INPUT_INVALID", "import_chatgpt_node")
                message = node["message"]
                if message is None:
                    continue
                if not isinstance(message, dict) or not isinstance(message.get("author"), dict):
                    raise ContractError("INPUT_INVALID", "import_chatgpt_author")
                role = message["author"].get("role")
                if not isinstance(role, str) or role not in {"user", "assistant", "system", "tool"}:
                    raise ContractError("INPUT_INVALID", "import_chatgpt_role")
                if role not in {"user", "assistant"}:
                    continue
                text = _chatgpt_text(message.get("content"))
                if not text.strip():
                    continue
                yield ImportRecord(
                    _identity(message.get("id") or node_key),
                    text,
                    "user" if role == "user" else "assistant",
                    timestamp(message.get("create_time")),
                    chat.get("project_id"),
                    session,
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
            _identity(chat["uuid"])
            for message in chat["chat_messages"]:
                if not isinstance(message, dict):
                    raise ContractError("INPUT_INVALID", "import_claude_message")
                sender = message.get("sender")
                if not isinstance(sender, str) or sender not in {"human", "assistant"}:
                    raise ContractError("INPUT_INVALID", "import_claude_sender")
                text = _claude_text(message)
                _identity(message.get("uuid"))
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
