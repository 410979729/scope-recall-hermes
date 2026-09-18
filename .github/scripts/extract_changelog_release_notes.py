#!/usr/bin/env python3
"""Extract one clean GitHub Release body from a versioned changelog section.

The parser used to live in ``scripts/release_changelog.py``, which was removed
with the 2.x tooling in d96a159 while this file kept importing it: the release
workflow's last step has raised ``ModuleNotFoundError`` at import time ever
since, past the handler below, and nothing in any gate ran this script.  It has
one consumer, so it now carries its own parser, and ``tests/packaging`` runs it
against the real changelog.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<tail>[^\r\n]*)(?:\r?\n)?$")
_VERSION_HEADING_RE = re.compile(r"^ {0,3}##[ \t]+\[(?P<label>[^\]\r\n]+)\][^\r\n]*(?:\r?\n)?$")


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
            run, tail = fence.group("run"), fence.group("tail")
            if fence_char is None:
                fence_char, fence_length = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_length and not tail.strip():
                fence_char, fence_length = None, 0
            offset += len(line)
            continue

        if fence_char is None:
            heading = _VERSION_HEADING_RE.fullmatch(line)
            if heading is not None:
                headings.append(_Heading(label=heading.group("label").strip(), start=offset,
                                         body_start=offset + len(line)))
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
    matches = [(index, heading) for index, heading in enumerate(headings) if heading.label == version]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one changelog section for {version}, found {len(matches)}")

    index, heading = matches[0]
    end = headings[index + 1].start if index + 1 < len(headings) else len(changelog)
    body = changelog[heading.body_start:end].strip()
    if not body:
        raise ValueError(f"changelog section for {version} is empty")
    return body + "\n"


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the authoritative body for one release version."""

    return extract_version_section(changelog, version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract a clean GitHub Release body from CHANGELOG.md.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        notes = extract_release_notes(args.changelog.read_text(encoding="utf-8"), args.version)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"release-note extraction failed: {exc}") from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
