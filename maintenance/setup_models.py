"""Interactive route choices collect references and limits, never credential values."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ..runtime.auxiliary import AuxiliaryRuntimeConfig
from ..runtime.instance_config import VectorRuntimeConfig
from .setup_configuration import validate_models
from .setup_workload import recommended_totals, recommended_workload


def _ask(label: str, default: str = "") -> str:
    value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def configure_models() -> dict:
    """Collect explicit model and budget choices before the final setup confirmation."""
    embedding = dict(
        model=_ask("Embedding model"),
        endpoint=_ask("Embedding endpoint (no key in URL)"),
        dimensions=int(_ask("Embedding dimensions", "3072")),
        dialect=_ask("Embedding dialect", "openai"),
        credential_env=_ask("Embedding credential environment variable", "SCOPE_RECALL_EMBED_KEY"),
    )
    extraction = dict(
        model=_ask("Extraction model"),
        endpoint=_ask("Extraction endpoint (no key in URL)"),
        credential_env=_ask("Extraction credential environment variable", "SCOPE_RECALL_CHAT_KEY"),
        output_limit_field=_ask("Output limit field", "max_tokens"),
        max_output_tokens=4096,
    )
    pricing = {}
    for model in (embedding["model"], extraction["model"]):
        pricing[model] = dict(
            input_usd_per_million=_ask(f"{model} input price per million tokens (USD)", "0"),
            output_usd_per_million=_ask(f"{model} output price per million tokens (USD)", "0"),
        )
    totals = recommended_totals()
    print("Planning scenarios per 100 turns:", recommended_workload())
    print("Embedding, entity decisions and backlog depend on workload. The limits below are your explicit caps.")
    budget = dict(
        batch="memory",
        approved_models=list(pricing),
        pricing=pricing,
        cap_micro_usd=int(Decimal(_ask("Total spending cap (USD)", "0")) * 1000000),
        total_call_cap=int(_ask("Total call cap", str(totals["total_call_cap"]))),
        total_input_cap=int(_ask("Total input token cap", str(totals["total_input_cap"]))),
        total_output_cap=int(_ask("Total output token cap", str(totals["total_output_cap"]))),
        period=_ask("Budget period (lifetime or monthly)", "monthly"),
        time_zone=_ask("Budget time zone", "UTC"),
    )
    auxiliary = dict(
        external_embedding=True,
        external_consolidation=True,
        budget=budget,
        embedding=embedding,
        consolidation=extraction,
    )
    AuxiliaryRuntimeConfig.from_mapping(auxiliary)
    vector = dict(
        backend="sqlite-bruteforce",
        storage_dir=str(Path.cwd()),
        table_name="memory",
        dimensions=embedding["dimensions"],
    )
    VectorRuntimeConfig.from_mapping(vector)
    return validate_models(
        dict(
            auxiliary=auxiliary,
            vector=vector,
            vector_threshold=float(_ask("Calibrated vector similarity threshold", "0.70")),
        )
    )
