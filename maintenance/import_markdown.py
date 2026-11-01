"""Read selected Markdown memory files as unconfirmed assistant material."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import yaml
from scope_recall.contracts import ContractError
from scope_recall.core.capture_filters import strip_private_content
from scope_recall.core.memory_import import ImportRecord

from .import_read import read_bytes, timestamp


def _markdown(paths, root):
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.is_file():
            continue
        text, private_removed = strip_private_content(read_bytes(path).decode("utf-8-sig"))
        metadata = {}
        header = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
        if header:
            try:
                metadata = yaml.safe_load(header.group(1)) or {}
            except yaml.YAMLError:
                # Parser diagnostics can echo source text; keep malformed input
                # out of the command's error and log surfaces.
                raise ContractError("INPUT_INVALID", "import_frontmatter") from None
            if not isinstance(metadata, dict):
                raise ContractError("INPUT_INVALID", "import_frontmatter")
            text = text[header.end() :]
        occurred = timestamp(metadata.get("modified") or metadata.get("updated_at"))
        yield ImportRecord(
            str(path.relative_to(root)).replace("\\", "/"),
            text,
            "assistant",
            occurred_at=occurred,
            original_origin="assistant_visible",
            private_content_omitted=private_removed,
        )


def read_claude_code(path: Path):
    """Read a CLAUDE.md or an explicitly selected auto-memory directory."""
    paths = [path] if path.is_file() else list(path.glob("*.md"))
    if not paths:
        raise ContractError("INPUT_INVALID", "import_markdown")
    yield from _markdown(paths, path.parent if path.is_file() else path)


def _generated_header(text):
    metadata = {}
    lines = text.splitlines(keepends=True)
    index = 0
    for line in lines:
        match = re.fullmatch(
            r"(thread_id|updated_at|rollout_path|cwd|git_branch|rollout_summary_file): (.*?)\r?\n?", line
        )
        if match is None:
            break
        metadata[match.group(1)] = match.group(2)
        index += 1
    return metadata, "".join(lines[index:]).lstrip("\r\n")


def _codex_records(item):
    sections = list(re.finditer(r"^## Thread `([^`\r\n]+)`\r?$", item.content, re.M))
    if sections:
        for index, section in enumerate(sections):
            end = sections[index + 1].start() if index + 1 < len(sections) else len(item.content)
            metadata, content = _generated_header(item.content[section.end() : end].lstrip("\r\n"))
            yield replace(
                item,
                key="raw:" + section.group(1),
                session=section.group(1),
                content=content,
                project=metadata.get("cwd"),
                occurred_at=timestamp(metadata.get("updated_at")),
            )
    else:
        metadata, content = _generated_header(item.content)
        if metadata.get("thread_id"):
            item = replace(
                item,
                key="rollout:" + metadata["thread_id"],
                session=metadata["thread_id"],
                content=content,
                project=metadata.get("cwd"),
                occurred_at=timestamp(metadata.get("updated_at")),
            )
        yield item


def read_codex(path: Path):
    """Read generated memory Markdown, excluding raw session JSONL and executable skills."""
    if path.is_file():
        paths, root = [path], path.parent
    else:
        root = path
        paths = [path / name for name in ("MEMORY.md", "memory_summary.md", "raw_memories.md")]
        paths += list((path / "rollout_summaries").glob("*.md"))
    for item in _markdown(paths, root):
        yield from _codex_records(item)


def read_hermes(path: Path):
    """Read MEMORY.md and USER.md from a selected memory directory, never its host home."""
    paths = [path] if path.is_file() else [path / "MEMORY.md", path / "USER.md"]
    yield from _markdown(paths, path.parent if path.is_file() else path)
