"""Generate the configuration reference from the definitions the runtime actually reads."""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scope_recall.contracts import InstanceBinding
from scope_recall.maintenance.setup import parser as setup_parser
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.runtime.codex_cli import CodexCliRouteConfig
from scope_recall.runtime.consolidation_models import ConsolidationRouteConfig, ResponsesRouteConfig
from scope_recall.runtime.embedding_models import EmbeddingRouteConfig
from scope_recall.runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig, configuration_value
from scope_recall.runtime.model_budget import BudgetPolicy, ModelPricing, ModelTokenCapInput
from scope_recall.runtime.subscription_budget import SubscriptionBudgetPolicy

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "configuration.md"


@dataclasses.dataclass(frozen=True)
class InputDefinition:
    """One runtime reader and a valid minimal example; defaults come from executing that reader."""

    definition: type
    reader: Callable[[dict], Any]
    minimum: dict


def _auxiliary(**parts):
    return AuxiliaryRuntimeConfig.from_mapping(dict(external_embedding=False, external_consolidation=False, **parts))


def input_definitions() -> tuple[InputDefinition, ...]:
    """Cover nested input types without duplicating accepted fields, defaults or parser rules."""
    binding = dict(
        agent_id="example", installation_id="example", data_directory=str(ROOT / "example-store"), scope_ids=["example"]
    )
    runtime = dict(binding=binding, session_id="example", allowed_scope_ids=["example"])
    route = dict(
        model="example",
        endpoint="https://example.test/chat",
        credential_env="EXAMPLE_CREDENTIAL",
        max_output_tokens=4096,
    )
    return (
        InputDefinition(RuntimeInstanceConfig, RuntimeInstanceConfig.from_mapping, runtime),
        InputDefinition(
            InstanceBinding, lambda raw: RuntimeInstanceConfig.from_mapping(dict(runtime, binding=raw)).binding, binding
        ),
        InputDefinition(
            VectorRuntimeConfig,
            VectorRuntimeConfig.from_mapping,
            dict(storage_dir=str(ROOT / "example-store"), table_name="example", dimensions=3072),
        ),
        InputDefinition(
            AuxiliaryRuntimeConfig,
            AuxiliaryRuntimeConfig.from_mapping,
            dict(external_embedding=False, external_consolidation=False),
        ),
        InputDefinition(BudgetPolicy, lambda raw: _auxiliary(budget=raw).budget, dict(approved_models=[], pricing={})),
        InputDefinition(
            ModelPricing,
            lambda raw: _auxiliary(budget=dict(approved_models=["example"], pricing=dict(example=raw))).budget.pricing[
                "example"
            ],
            dict(input_usd_per_million="0", output_usd_per_million="0"),
        ),
        InputDefinition(
            EmbeddingRouteConfig,
            lambda raw: _auxiliary(embedding=raw).embedding,
            dict(credential_env="EXAMPLE_CREDENTIAL"),
        ),
        InputDefinition(
            ConsolidationRouteConfig,
            lambda raw: _auxiliary(consolidation=raw).consolidation,
            dict(route, output_limit_field="max_tokens"),
        ),
        InputDefinition(
            ResponsesRouteConfig,
            lambda raw: _auxiliary(consolidation=raw).consolidation,
            dict(route, kind="openai_responses"),
        ),
        InputDefinition(
            CodexCliRouteConfig,
            CodexCliRouteConfig.from_mapping,
            dict(executable=str(ROOT / "example-cli"), executable_sha256="0" * 64),
        ),
        InputDefinition(SubscriptionBudgetPolicy, SubscriptionBudgetPolicy.from_mapping, {}),
    )


def input_default(contract: InputDefinition, name: str) -> str:
    """A field is required only if its actual reader rejects its omission."""
    sample = copy.deepcopy(contract.minimum)
    sample.pop(name, None)
    try:
        value = configuration_value(contract.reader(sample))[name]
    except (ValueError, TypeError, KeyError):
        return "required"
    return json.dumps(value, ensure_ascii=False).replace("|", "&#124;")


def render() -> str:
    """Render the accepted nested JSON types and reader defaults, plus setup's own parser options."""
    lines = [
        "# Configuration",
        "",
        "This reference is generated from runtime input definitions and their readers.",
        "Regenerate with `python scripts/generate_configuration_docs.py --write`; `--check` detects drift.",
        "",
        "Setup preserves existing routes and grants. New installations have no model routes and zero model budget.",
        "Credentials are environment variable references. Never put their values in a route file.",
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
    for contract in input_definitions():
        definition = contract.definition
        lines.extend(
            ["", f"## {definition.__name__}", "", "| Field | JSON input type | Default when omitted |", "|---|---|---|"]
        )
        for item in dataclasses.fields(definition):
            anchor = ""
            if item.name not in anchored:
                anchor = f'<a id="{item.name}"></a>'
                anchored.add(item.name)
            annotation = str(item.type).replace("|", "&#124;")
            if item.name == "model_token_caps":
                annotation = "object: model name → " + json.dumps(
                    {key: None for key in sorted(ModelTokenCapInput.__required_keys__)}
                )
            lines.append(f"| {anchor}`{item.name}` | `{annotation}` | `{input_default(contract, item.name)}` |")
    lines.extend(
        [
            "",
            "Nested objects use the definitions above: binding → InstanceBinding; auxiliary → AuxiliaryRuntimeConfig;",
            "budget → BudgetPolicy; pricing values → ModelPricing; subscription_budget → SubscriptionBudgetPolicy.",
            "In model_token_caps each model maps to an object with input and output integers or null; "
            "both keys are stated.",
            "Embedding descriptor fields may be omitted together to select the built-in space.",
        ]
    )
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
