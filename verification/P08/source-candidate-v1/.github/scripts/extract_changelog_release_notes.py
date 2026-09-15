#!/usr/bin/env python3
"""Extract one clean GitHub Release body from a versioned changelog section."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_changelog import extract_version_section  # noqa: E402


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the authoritative body for one release version."""

    return extract_version_section(changelog, version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract a clean GitHub Release body from CHANGELOG.md."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        notes = extract_release_notes(
            args.changelog.read_text(encoding="utf-8"),
            args.version,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"release-note extraction failed: {exc}") from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
