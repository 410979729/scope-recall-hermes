"""Strict, semantics-preserving Hermes YAML edits for installer activation.

The installer only changes ``memory.provider``. Unsupported YAML constructs fail
closed instead of being normalized or silently discarded.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken, DocumentStartToken


class InstallerYamlError(ValueError):
    """Raised when Hermes YAML cannot be updated without semantic loss."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys at every depth."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise InstallerYamlError("YAML mapping key is not hashable") from exc
        if duplicate:
            raise InstallerYamlError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)
_TOP_LEVEL_MEMORY_RE = re.compile(
    r"^(?P<key>memory|'memory'|\"memory\")\s*:(?P<rest>.*)$"
)
_PROVIDER_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<key>provider|'provider'|\"provider\")\s*:"
    r"(?P<value>[^#]*?)(?P<comment>[ \t]+#.*)?$"
)


def _reject_unsupported_tokens(text: str) -> None:
    try:
        tokens = list(yaml.scan(text))
    except yaml.YAMLError as exc:
        raise InstallerYamlError(f"malformed YAML: {exc}") from exc
    for token in tokens:
        if isinstance(token, (AnchorToken, AliasToken)):
            raise InstallerYamlError(
                "YAML anchors and aliases are unsupported for atomic provider edits"
            )
        if isinstance(token, DirectiveToken):
            raise InstallerYamlError("YAML directives are unsupported")
        if isinstance(token, DocumentStartToken):
            raise InstallerYamlError("explicit or multi-document YAML is unsupported")


def load_unique_yaml_mapping(text: str) -> dict[str, Any]:
    """Load one mapping document while rejecting ambiguous YAML."""

    if "\x00" in text:
        raise InstallerYamlError("YAML contains a NUL byte")
    _reject_unsupported_tokens(text)
    try:
        documents = list(yaml.load_all(text, Loader=_UniqueKeySafeLoader))
    except (yaml.YAMLError, InstallerYamlError) as exc:
        if isinstance(exc, InstallerYamlError):
            raise
        raise InstallerYamlError(f"malformed YAML: {exc}") from exc
    if len(documents) > 1:
        raise InstallerYamlError("multi-document YAML is unsupported")
    document: Any = documents[0] if documents else None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise InstallerYamlError("Hermes config root must be a YAML mapping")
    return document


def _split_flow_comment(value: str) -> tuple[str, str]:
    quote = ""
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if quote == "'":
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    continue
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth = max(0, depth - 1)
        elif character == "#" and depth == 0 and (
            index == 0 or value[index - 1].isspace()
        ):
            return value[:index].rstrip(), value[index:]
    return value.rstrip(), ""


def _memory_block_end(lines: list[str], start: int) -> int:
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line.startswith((" ", "\t")):
            index += 1
            continue
        break
    return index


def _rewrite_block_memory(
    lines: list[str],
    *,
    index: int,
    match: re.Match[str],
) -> tuple[list[str], int]:
    end = _memory_block_end(lines, index + 1)
    block = list(lines[index + 1 : end])
    child_indents = [
        len(line) - len(line.lstrip(" \t"))
        for line in block
        if line.strip() and not line.lstrip().startswith("#")
    ]
    direct_indent = min(child_indents) if child_indents else 2
    provider_found = False
    for block_index, line in enumerate(block):
        provider_match = _PROVIDER_RE.match(line)
        if provider_match is None:
            continue
        if len(provider_match.group("indent")) != direct_indent:
            continue
        comment = provider_match.group("comment") or ""
        block[block_index] = (
            f"{provider_match.group('indent')}{provider_match.group('key')}: "
            f"scope-recall{comment}"
        )
        provider_found = True
        break
    if not provider_found:
        block.insert(0, f"{' ' * direct_indent}provider: scope-recall")
    return [lines[index], *block], end


def set_memory_provider_yaml_text(text: str) -> tuple[str, bool]:
    """Set ``memory.provider`` and prove every other YAML value is unchanged."""

    before_mapping = load_unique_yaml_mapping(text)
    memory_before = before_mapping.get("memory")
    if memory_before is not None and not isinstance(memory_before, Mapping):
        raise InstallerYamlError("top-level memory must be a YAML mapping")

    lines = text.splitlines()
    output: list[str] = []
    found_memory = False
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _TOP_LEVEL_MEMORY_RE.match(line)
        if match is None:
            output.append(line)
            index += 1
            continue
        if found_memory:
            raise InstallerYamlError("duplicate top-level memory mapping")
        found_memory = True
        rest = match.group("rest")
        stripped = rest.strip()
        if not stripped or stripped.startswith("#"):
            rewritten, index = _rewrite_block_memory(lines, index=index, match=match)
            output.extend(rewritten)
            continue

        flow_value, comment = _split_flow_comment(rest)
        if not flow_value.lstrip().startswith("{"):
            raise InstallerYamlError(
                "top-level memory must use block or inline mapping syntax"
            )
        memory_mapping = dict(memory_before or {})
        memory_mapping["provider"] = "scope-recall"
        rendered = yaml.safe_dump(
            memory_mapping,
            allow_unicode=True,
            default_flow_style=True,
            sort_keys=False,
            width=1_000_000,
        ).strip()
        suffix = f" {comment}" if comment else ""
        output.append(f"{match.group('key')}: {rendered}{suffix}")
        index += 1

    if not found_memory:
        if output and output[-1].strip():
            output.append("")
        output.extend(["memory:", "  provider: scope-recall"])

    after = "\n".join(output).rstrip() + "\n"
    after_mapping = load_unique_yaml_mapping(after)
    expected = copy.deepcopy(before_mapping)
    expected_memory = dict(memory_before or {})
    expected_memory["provider"] = "scope-recall"
    expected["memory"] = expected_memory
    if after_mapping != expected:
        raise InstallerYamlError(
            "provider edit changed YAML semantics outside memory.provider"
        )
    normalized_before = text.rstrip() + "\n" if text else text
    return after, after != normalized_before


def memory_provider_from_yaml(text: str) -> str:
    mapping = load_unique_yaml_mapping(text)
    memory = mapping.get("memory")
    if not isinstance(memory, Mapping):
        return ""
    return str(memory.get("provider") or "").strip()


def atomic_replace_text(
    path: Path,
    text: str,
    *,
    expected_before: str | None = None,
) -> Path:
    """Atomically replace a regular file or a symlink target with fsync barriers."""

    destination = path.resolve(strict=False) if path.is_symlink() else path
    if destination.exists() and not destination.is_file():
        raise InstallerYamlError(f"config target is not a regular file: {destination}")
    if expected_before is not None:
        current = (
            destination.read_text(encoding="utf-8", errors="strict")
            if destination.is_file()
            else ""
        )
        if current != expected_before:
            raise InstallerYamlError("config changed concurrently before atomic replace")

    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = destination.stat().st_mode & 0o777 if destination.is_file() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.scope-recall.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(destination.parent, directory_flags)
        except OSError:
            # The replacement is already visible and cannot be rolled back
            # atomically here. Windows and some filesystems do not support
            # opening/fsyncing directories, so reporting failure would violate
            # the API's stable-state contract (failure means original bytes).
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                # Same post-replace durability limitation as directory open.
                # The caller must observe success because the new bytes are live.
                pass
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return destination
