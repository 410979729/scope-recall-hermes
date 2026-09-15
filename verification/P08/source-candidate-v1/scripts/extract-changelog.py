#!/usr/bin/env python3
"""Extract a version section from CHANGELOG.md for release notes."""
import re
import sys


def extract(version: str, changelog_path: str, output_path: str) -> None:
    with open(changelog_path) as f:
        text = f.read()

    # Match "## [VERSION]" heading through the next "## [...]" heading or EOF
    pattern = rf"(## \[{re.escape(version)}\].*?)(?=\n## \[|\Z)"
    match = re.search(pattern, text, re.DOTALL)

    if not match:
        print(
            f"::error::No CHANGELOG section found for version {version}",
            file=sys.stderr,
        )
        sys.exit(1)

    entry = match.group(1).strip()

    with open(output_path, "w") as f:
        f.write(entry + "\n")

    print(f"Extracted {len(entry)} chars for version {version}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <version> <changelog.md> <output.md>",
            file=sys.stderr,
        )
        sys.exit(1)
    extract(sys.argv[1], sys.argv[2], sys.argv[3])
