"""Explicit local source readers normalized to capture-ready evidence records."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from scope_recall.contracts import ContractError
from scope_recall.core.memory_import import ImportRecord

from .import_chats import read_chatgpt, read_claude
from .import_markdown import read_claude_code, read_codex, read_hermes
from .import_summaries import read_claude_mem, read_mem0

SourceReader = Callable[[Path], Iterable[ImportRecord]]
SOURCE_READERS: dict[str, SourceReader] = {
    "claude-code": read_claude_code,
    "codex": read_codex,
    "hermes": read_hermes,
    "chatgpt": read_chatgpt,
    "claude": read_claude,
    "claude-mem": read_claude_mem,
    "mem0": read_mem0,
}


def read_source(source: str, path: Path, *, readers=None) -> tuple[ImportRecord, ...]:
    """Read only the explicitly selected path; registering a reader needs no dispatch changes."""
    reader = (SOURCE_READERS if readers is None else readers).get(source)
    if reader is None:
        raise ContractError("INPUT_INVALID", "import_source")
    return tuple(reader(Path(path)))
