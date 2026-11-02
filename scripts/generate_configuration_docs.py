"""Generate the configuration reference from the definitions the runtime actually reads."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from scope_recall.maintenance.setup import parser as setup_parser
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.runtime.codex_cli import CodexCliRouteConfig
from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig, ResponsesRouteConfig
from scope_recall.runtime.embedding_models import EmbeddingRouteConfig
from scope_recall.runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig
from scope_recall.runtime.model_budget import BudgetPolicy, default_budget_policy

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "configuration.md"
DEFINITIONS = (
    RuntimeInstanceConfig,
    VectorRuntimeConfig,
    AuxiliaryRuntimeConfig,
    BudgetPolicy,
    EmbeddingRouteConfig,
    ConsolidationRouteConfig,
    ResponsesRouteConfig,
    CodexCliRouteConfig,
)


def _default(item, definition):
    if definition is BudgetPolicy:
        value = getattr(default_budget_policy(), item.name)
    elif item.default is not dataclasses.MISSING:
        value = item.default
    elif item.default_factory is not dataclasses.MISSING:
        value = item.default_factory()
    else:
        return "required"
    if isinstance(value, (set, frozenset)):
        value = sorted(value)
    if isinstance(value, dict) or value is None or isinstance(value, (bool, str, int, float, list)):
        return json.dumps(value, ensure_ascii=False).replace("|", "&#124;")
    return str(value).replace("|", "&#124;")


def render() -> str:
    """Render defaults from dataclasses and setup options from the CLI parser, without a second schema."""
    lines = [
        "# Configuration",
        "",
        "This reference is generated from runtime definitions and the setup parser.",
        "Regenerate it with `python scripts/generate_configuration_docs.py --write`; `--check` detects drift.",
        "",
        "Setup preserves existing routes and grants. A new installation has no model routes and zero model budget.",
        "Model credentials are environment variable names. Never put credential values in a route file.",
        "",
        "## Setup options",
        "",
        "| Option | Meaning |",
        "|---|---|",
    ]
    for action in setup_parser()._actions:
        if action.dest != "help":
            lines.append(f"| `{' / '.join(action.option_strings)}` | {action.help} |")
    anchored = set()
    for definition in DEFINITIONS:
        lines.extend(["", f"## {definition.__name__}", "", "| Field | Type | Default |", "|---|---|---|"])
        for item in dataclasses.fields(definition):
            anchor = ""
            if item.name not in anchored:
                anchor = f'<a id="{item.name}"></a>'
                anchored.add(item.name)
            annotation = str(item.type).replace("|", "&#124;")
            lines.append(f"| {anchor}`{item.name}` | `{annotation}` | `{_default(item, definition)}` |")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write only the reference, or compare it byte for byte without changing anything."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.write:
        TARGET.write_text(expected, encoding="utf-8")
        return 0
    return 0 if TARGET.read_text(encoding="utf-8") == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
