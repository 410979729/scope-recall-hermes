"""Explicit local source readers normalized to capture-ready evidence records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from scope_recall.contracts import ContractError
from scope_recall.core.memory_import import ImportRecord

from .import_chats import read_chatgpt, read_claude
from .import_markdown import read_claude_code, read_codex, read_hermes
from .import_read import SkippedImport
from .import_summaries import read_claude_mem, read_mem0

SourceReader = Callable[[Path], Iterable[ImportRecord | SkippedImport]]
SOURCE_READERS: dict[str, SourceReader] = {
    "claude-code": read_claude_code,
    "codex": read_codex,
    "hermes": read_hermes,
    "chatgpt": read_chatgpt,
    "claude": read_claude,
    "claude-mem": read_claude_mem,
    "mem0": read_mem0,
}


def read_source(source: str, path: Path, *, readers=None, skipped=None) -> tuple[ImportRecord, ...]:
    """Return records from the selected path, optionally counting validated text-free occurrences.

    Registered readers yield records or SkippedImport markers; those markers
    affect only preview counts and never reach capture or storage.
    """
    reader = (SOURCE_READERS if readers is None else readers).get(source)
    if reader is None:
        raise ContractError("INPUT_INVALID", "import_source")
    try:
        records = []
        for item in reader(Path(path)):
            if isinstance(item, SkippedImport):
                if skipped is not None:
                    skipped["no_text"] = skipped.get("no_text", 0) + 1
            else:
                records.append(item)
        return tuple(records)
    except ContractError:
        raise
    except (json.JSONDecodeError, UnicodeError, sqlite3.Error, OSError, OverflowError, ValueError):
        raise ContractError("INPUT_INVALID", "import_source_format") from None
