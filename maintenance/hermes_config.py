"""Edit the provider scalar without serializing the user's YAML configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

from .install_common import InstallError


def provider_configuration(path: Path) -> bytes:
    """Return an exact byte patch; ambiguous mappings, aliases and flow syntax are refused."""
    original = path.read_bytes() if path.is_file() else b""
    text = original.decode("utf-8-sig")
    bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
    ending = "\r\n" if "\r\n" in text else "\n"
    node = yaml.compose(text)
    if node is None:
        return (
            original
            + (b"" if not original or text.endswith("\n") else ending.encode())
            + (f"memory:{ending}  provider: scope-recall{ending}").encode()
        )
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise InstallError("memory.provider requires a block configuration mapping")

    def member(mapping, name):
        matches = [(key, value) for key, value in mapping.value if key.value == name]
        if len(matches) > 1 or any(key.value == "<<" for key, _ in mapping.value):
            raise InstallError("memory.provider cannot be patched through duplicate or merged keys")
        return matches[0] if matches else None

    memory = member(node, "memory")
    if memory is None:
        addition = f"memory:{ending}  provider: scope-recall{ending}"
        end = next(
            (t.start_mark.index for t in yaml.scan(text) if isinstance(t, yaml.tokens.DocumentEndToken)), len(text)
        )
        prefix = "" if not text[:end] or text[:end].endswith("\n") else ending
        return bom + (text[:end] + prefix + addition + text[end:]).encode("utf-8")
    key, mapping = memory
    if not isinstance(mapping, yaml.MappingNode) or mapping.flow_style or mapping.start_mark.index < key.end_mark.index:
        raise InstallError("memory.provider requires an unaliased block memory mapping")
    if any(
        isinstance(t, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
        and key.end_mark.index <= t.start_mark.index < mapping.end_mark.index
        for t in yaml.scan(text)
    ):
        raise InstallError("memory.provider cannot be patched inside an anchor or alias")
    provider = member(mapping, "provider")
    if provider is None:
        # Start marks identify the first child line even when the last child ends at the next section.
        position = mapping.start_mark.index - mapping.start_mark.column
        addition = " " * mapping.start_mark.column + f"provider: scope-recall{ending}"
        return bom + (text[:position] + addition + text[position:]).encode("utf-8")
    key, value = provider
    if not isinstance(value, yaml.ScalarNode) or value.style not in (None, "'", '"'):
        raise InstallError("memory.provider requires a single-line scalar")
    if value.start_mark.line != key.start_mark.line or value.end_mark.line != key.start_mark.line:
        raise InstallError("memory.provider requires a single-line scalar")
    quote = value.style or ""
    if value.start_mark.index and text[value.start_mark.index - 1] == ":":
        updated = text[: value.start_mark.index] + " scope-recall" + text[value.end_mark.index :]
        return bom + updated.encode("utf-8")
    updated = text[: value.start_mark.index] + quote + "scope-recall" + quote + text[value.end_mark.index :]
    return bom + updated.encode("utf-8")
