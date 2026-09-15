"""Parse versioned changelog sections outside Markdown code fences."""

from __future__ import annotations

import re
from dataclasses import dataclass


SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<tail>[^\r\n]*)(?:\r?\n)?$")
_VERSION_HEADING_RE = re.compile(
    r"^ {0,3}##[ \t]+\[(?P<label>[^\]\r\n]+)\][^\r\n]*(?:\r?\n)?$"
)


@dataclass(frozen=True)
class _Heading:
    """One bracketed level-two heading outside a fenced code block."""

    label: str
    start: int
    body_start: int


def _version_headings(changelog: str) -> list[_Heading]:
    """Return bracketed level-two headings that carry Markdown authority."""
    headings: list[_Heading] = []
    offset = 0
    fence_char: str | None = None
    fence_length = 0

    for line in changelog.splitlines(keepends=True):
        fence = _FENCE_RE.fullmatch(line)
        if fence is not None:
            run = fence.group("run")
            tail = fence.group("tail")
            if fence_char is None:
                fence_char = run[0]
                fence_length = len(run)
            elif run[0] == fence_char and len(run) >= fence_length and not tail.strip():
                fence_char = None
                fence_length = 0
            offset += len(line)
            continue

        if fence_char is None:
            heading = _VERSION_HEADING_RE.fullmatch(line)
            if heading is not None:
                headings.append(
                    _Heading(
                        label=heading.group("label").strip(),
                        start=offset,
                        body_start=offset + len(line),
                    )
                )
        offset += len(line)

    return headings


def extract_version_section(changelog: str, version: str) -> str:
    """Return one non-empty version-section body with a trailing newline.

    Only real top-level ``## [label]`` headings outside fenced code blocks can
    delimit a release. Missing, duplicate, or empty target sections fail
    closed so release tooling cannot publish ambiguous or example content.
    """
    if not SEMVER_RE.fullmatch(version):
        raise ValueError("version must use major.minor.patch syntax")

    headings = _version_headings(changelog)
    matches = [
        (index, heading)
        for index, heading in enumerate(headings)
        if heading.label == version
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one changelog section for {version}, found {len(matches)}"
        )

    index, heading = matches[0]
    end = headings[index + 1].start if index + 1 < len(headings) else len(changelog)
    body = changelog[heading.body_start:end].strip()
    if not body:
        raise ValueError(f"changelog section for {version} is empty")
    return body + "\n"
