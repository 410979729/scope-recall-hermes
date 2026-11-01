"""Normalize local conversation exports without importing attachments or tool payloads."""

from __future__ import annotations

from pathlib import Path

from scope_recall.contracts import ContractError
from scope_recall.core.memory_import import ImportRecord

from .import_read import SkippedImport, json_documents, timestamp


def _conversations(document):
    if not isinstance(document, list) or any(not isinstance(c, dict) for c in document):
        raise ContractError("INPUT_INVALID", "import_conversations")
    return document


def _identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError("INPUT_INVALID", "import_message_identity")
    return value


def _text_field(content, name):
    value = content.get(name)
    if not isinstance(value, str):
        raise ContractError("INPUT_INVALID", "import_chatgpt_text")
    return value


def _asset_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError("INPUT_INVALID", "import_chatgpt_asset")


def _realtime_asset(content):
    audio = content.get("audio_asset_pointer")
    frames = content.get("frames_asset_pointers", [])
    video = content.get("video_container_asset_pointer")
    if not isinstance(frames, list) or not (audio or frames or video):
        raise ContractError("INPUT_INVALID", "import_chatgpt_asset")
    if audio is not None:
        if not isinstance(audio, dict) or audio.get("content_type") != "audio_asset_pointer":
            raise ContractError("INPUT_INVALID", "import_chatgpt_asset")
        _asset_id(audio.get("asset_pointer"))
    if video is not None:
        _asset_id(video)
    for frame in frames:
        _asset_id(frame)


def _chatgpt_part(part):
    if isinstance(part, str):
        return part, False
    if not isinstance(part, dict):
        raise ContractError("INPUT_INVALID", "import_chatgpt_part")
    kind = part.get("content_type")
    if kind == "audio_transcription":
        # Machine transcription is material, never the user's typed wording.
        return _text_field(part, "text"), True
    if kind in ("audio_asset_pointer", "image_asset_pointer"):
        _asset_id(part.get("asset_pointer"))
    elif kind == "real_time_user_audio_video_asset_pointer":
        _realtime_asset(part)
    else:
        raise ContractError("INPUT_INVALID", "import_chatgpt_part")
    return "", False


def _chatgpt_text(content):
    if not isinstance(content, dict) or not isinstance(content.get("content_type"), str):
        raise ContractError("INPUT_INVALID", "import_chatgpt_content")
    kind = content["content_type"]
    if kind == "user_editable_context":
        # Profile/instructions are supplied context, not statements in this
        # conversation. Keep them unconfirmed even when the author is user.
        fields = [content[name] for name in ("user_profile", "user_instructions") if name in content]
        if not fields or any(not isinstance(field, str) for field in fields):
            raise ContractError("INPUT_INVALID", "import_chatgpt_context")
        return "\n".join(fields), True
    if kind == "code":
        return _text_field(content, "text"), False
    if kind not in {"text", "multimodal_text"}:
        return _chatgpt_part(content)
    parts = content.get("parts")
    if not isinstance(parts, list) or kind == "text" and any(not isinstance(part, str) for part in parts):
        raise ContractError("INPUT_INVALID", "import_chatgpt_parts")
    values = [_chatgpt_part(part) for part in parts]
    # A mixed message containing a transcription is conservatively material
    # as a whole; no machine-generated span acquires human authority.
    return "\n".join(text for text, _material in values if text), any(material for _text, material in values)


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
                key = _identity(message.get("id") or node_key)
                text, material = _chatgpt_text(message.get("content"))
                if not text.strip():
                    yield SkippedImport()
                    continue
                yield ImportRecord(
                    key,
                    text,
                    "user" if role == "user" else "assistant",
                    timestamp(message.get("create_time")),
                    chat.get("project_id"),
                    session,
                    "origin_unknown" if material else "human_direct" if role == "user" else "assistant_visible",
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
