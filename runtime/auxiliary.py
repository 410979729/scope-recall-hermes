"""Explicit auxiliary runtime composition for approved external routes only."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..adapters.models import (
    ConsolidationRouteConfig,
    EmbeddingRouteConfig,
    GeminiEmbeddingAdapter,
    HttpTransport,
    OpenAIConsolidationAdapter,
)
from ..adapters.codex_cli import CodexCliConsolidationAdapter, CodexCliRouteConfig
from .subscription_budget import SubscriptionBudgetLedger
from .model_budget import (
    AuxiliaryBudgetLedger,
    BudgetPolicy,
    ModelPricing,
    default_budget_policy,
    load_hermes_attempt_authorization,
    read_auxiliary_budget_status,
)
from .validation import absolute_path, mapping, positive_int, strict_bool, text


DEFAULT_LEDGER_NAME = "auxiliary-budget.sqlite3"


@dataclass(frozen=True)
class AuxiliaryRuntimeConfig:
    external_embedding: bool
    external_consolidation: bool
    installation_dir: Path | None
    ledger_path: Path | None
    budget: BudgetPolicy
    embedding: EmbeddingRouteConfig | None
    consolidation: ConsolidationRouteConfig | CodexCliRouteConfig | None
    consolidation_reserve_input: int

    @staticmethod
    def from_mapping(value: Mapping[str, Any]) -> AuxiliaryRuntimeConfig:
        raw = mapping("config_mapping_required", value)
        installation_dir = raw.get("installation_dir")
        if installation_dir is not None:
            installation_dir = absolute_path("installation_dir", installation_dir)
        ledger_path = raw.get("ledger_path")
        if ledger_path is not None:
            ledger_path = absolute_path("ledger_path", ledger_path)
        elif installation_dir is not None:
            ledger_path = installation_dir / DEFAULT_LEDGER_NAME
        return AuxiliaryRuntimeConfig(
            external_embedding=strict_bool("external_embedding_bool_required", raw.get("external_embedding")),
            external_consolidation=strict_bool("external_consolidation_bool_required", raw.get("external_consolidation")),
            installation_dir=installation_dir,
            ledger_path=ledger_path,
            budget=_budget_policy_from_mapping(raw.get("budget")),
            embedding=_embedding_route_from_mapping(raw.get("embedding")),
            consolidation=_consolidation_route_from_mapping(raw.get("consolidation")),
            consolidation_reserve_input=positive_int(
                "consolidation_reserve_input", raw.get("consolidation_reserve_input", 32_768)
            ),
        )


@dataclass(frozen=True)
class AuxiliaryRuntime:
    source_embedding: GeminiEmbeddingAdapter | None
    query_embedding: GeminiEmbeddingAdapter | None
    consolidation: OpenAIConsolidationAdapter | CodexCliConsolidationAdapter | None
    capability_gaps: tuple[str, ...]
    ledger_path: Path | None

    def close(self) -> None:
        if self.query_embedding is not None:
            self.query_embedding.close()


def load_formal_p18_budget_policy() -> BudgetPolicy | None:
    """Load the hash-bound formal TEST budget; never copy or reset the ledger."""
    name = os.environ.get("SCOPE_RECALL_P18_BUDGET_CONFIG")
    expected = os.environ.get("SCOPE_RECALL_P18_BUDGET_SHA256")
    if name is None and expected is None:
        return None
    if not name or not expected:
        raise ValueError("formal P18 budget path and hash must be supplied together")
    path = Path(name)
    if not path.is_absolute() or "test" not in str(path).lower():
        raise ValueError("absolute formal P18 budget path required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected.lower():
        raise ValueError("formal P18 budget hash mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("batch") != "P18_EVALUATION":
        raise ValueError("formal TEST budget batch required")
    return _budget_policy_from_mapping(value)


def _pricing_from_mapping(raw: object) -> dict[str, ModelPricing]:
    if raw is None:
        raise ValueError("pricing_required")
    pricing: dict[str, ModelPricing] = {}
    for model, rates in mapping("pricing_mapping_required", raw).items():
        text("pricing_model", model)
        rates = mapping("pricing_rates", rates)
        if rates.get("input_usd_per_million") is None or rates.get("output_usd_per_million") is None:
            raise ValueError("pricing_rate_required")
        pricing[model] = ModelPricing(rates["input_usd_per_million"], rates["output_usd_per_million"])
    return pricing


def _model_caps_from_mapping(raw: object) -> dict[str, tuple[int | None, int | None]]:
    """Both sides must be stated; an explicit null on a side means uncapped."""
    caps: dict[str, tuple[int | None, int | None]] = {}
    for model, sides in mapping("model_token_caps", raw).items():
        text("model_token_caps", model)
        sides = mapping("model_token_caps", sides)
        for key in ("input", "output"):
            if key not in sides:
                raise ValueError(f"model_token_caps_{key}")
        caps[model] = (sides["input"], sides["output"])
    return caps


def _budget_policy_from_mapping(raw: object) -> BudgetPolicy:
    """Shape and presence are checked here; ``BudgetPolicy`` validates every number."""
    if raw is None:
        return default_budget_policy()
    raw = mapping("budget_mapping_required", raw)
    batch = text("batch", raw.get("batch", "auxiliary"))
    base = default_budget_policy(batch=batch)
    pricing = _pricing_from_mapping(raw.get("pricing"))
    approved_raw = raw.get("approved_models")
    if not isinstance(approved_raw, (list, tuple)):
        raise ValueError("approved_models_required")
    approved = frozenset(text("approved_models", item) for item in approved_raw)
    model_caps = _model_caps_from_mapping(raw["model_token_caps"]) if raw.get("model_token_caps") is not None else {}
    reserve_output = {}
    if raw.get("model_reserve_output") is not None:
        for model, tokens in mapping("model_reserve_output", raw["model_reserve_output"]).items():
            reserve_output[text("model_reserve_output", model)] = tokens
    # An absent cumulative cap inherits the base policy's 0 (no spend), so an
    # unconfigured instance stays fail-closed; an explicit null means uncapped.
    return BudgetPolicy(
        batch=batch,
        cap_micro_usd=raw.get("cap_micro_usd", base.cap_micro_usd),
        total_input_cap=raw.get("total_input_cap", base.total_input_cap),
        total_output_cap=raw.get("total_output_cap", base.total_output_cap),
        total_call_cap=raw.get("total_call_cap", base.total_call_cap),
        max_request_bytes=positive_int("max_request_bytes", raw.get("max_request_bytes", base.max_request_bytes)),
        default_reserve_input=positive_int(
            "default_reserve_input", raw.get("default_reserve_input", base.default_reserve_input)
        ),
        default_reserve_output=positive_int(
            "default_reserve_output", raw.get("default_reserve_output", base.default_reserve_output)
        ),
        model_reserve_output=MappingProxyType(reserve_output or dict(base.model_reserve_output)),
        model_token_caps=MappingProxyType(model_caps or dict(base.model_token_caps)),
        pricing=MappingProxyType(pricing),
        approved_models=approved,
    )


def _embedding_route_from_mapping(raw: object) -> EmbeddingRouteConfig | None:
    if raw is None:
        return None
    raw = mapping("embedding_mapping_required", raw)
    credential_env = text("credential_env", raw.get("credential_env"))
    # model/endpoint/dimensions/dialect move together: omit all four for the
    # shipped Gemini space, or state all four to address another provider.
    # EmbeddingRouteConfig rejects a partial descriptor.
    # The width's field name is a wire detail of the openai dialect, optional on its own.
    wire = {} if raw.get("dimensions_field") is None else {
        "dimensions_field": text("embedding_dimensions_field", raw.get("dimensions_field"))}
    if all(raw.get(key) is None for key in ("model", "endpoint", "dimensions", "dialect")):
        return EmbeddingRouteConfig(credential_env=credential_env, **wire)
    return EmbeddingRouteConfig(
        credential_env=credential_env,
        model=text("embedding_model", raw.get("model")),
        endpoint=text("embedding_endpoint", raw.get("endpoint")),
        dimensions=positive_int("embedding_dimensions", raw.get("dimensions")),
        dialect=text("embedding_dialect", raw.get("dialect")),
        **wire,
    )


def _consolidation_route_from_mapping(raw: object) -> ConsolidationRouteConfig | CodexCliRouteConfig | None:
    if raw is None:
        return None
    raw = mapping("consolidation_mapping_required", raw)
    kind = raw.get("kind", "openai")
    if kind == "codex_cli":
        return CodexCliRouteConfig.from_mapping(raw)
    if kind != "openai":
        raise ValueError("consolidation_kind")
    thinking = raw.get("thinking")
    return ConsolidationRouteConfig(
        model=text("model", raw.get("model")),
        endpoint=text("endpoint", raw.get("endpoint")),
        credential_env=text("credential_env", raw.get("credential_env")),
        output_limit_field=text("output_limit_field", raw.get("output_limit_field")),
        max_output_tokens=positive_int("max_output_tokens", raw.get("max_output_tokens")),
        thinking=MappingProxyType(dict(thinking)) if isinstance(thinking, Mapping) else None,
        response_format=raw.get("response_format"),
        reasoning_effort=raw.get("reasoning_effort"),
        stream=raw.get("stream", False),
        n=raw.get("n", 1),
        headers=raw.get("headers"),
    )


def build_auxiliary_runtime(
    config: AuxiliaryRuntimeConfig,
    *,
    transport: HttpTransport | None = None,
) -> AuxiliaryRuntime:
    gaps: list[str] = []
    ledger = None
    authorization = load_hermes_attempt_authorization()
    covers = tuple(authorization.get("covered_historical_breaches") or ()) if authorization else ()
    reserve_input = config.consolidation_reserve_input
    if authorization is not None:
        raised = authorization.get("reserved_input")
        if type(raised) is int and raised > reserve_input:
            reserve_input = raised
    if config.ledger_path is not None:
        policy = load_formal_p18_budget_policy() or config.budget
        ledger = AuxiliaryBudgetLedger(config.ledger_path, policy, covered_historical_breaches=covers)
    embed_adapter = None
    if config.external_embedding is not True:
        gaps.append("external_embedding_not_approved")
    elif config.embedding is None:
        gaps.append("external_embedding_unconfigured")
    elif ledger is None:
        gaps.append("auxiliary_budget_ledger_unconfigured")
    else:
        embed_adapter = GeminiEmbeddingAdapter(config.embedding, ledger=ledger, transport=transport)
    consolidation_adapter = None
    if config.external_consolidation is not True:
        gaps.append("external_consolidation_not_approved")
    elif config.consolidation is None:
        gaps.append("external_consolidation_unconfigured")
    elif ledger is None:
        gaps.append("auxiliary_budget_ledger_unconfigured")
    elif isinstance(config.consolidation, CodexCliRouteConfig):
        consolidation_adapter = CodexCliConsolidationAdapter(
            config.consolidation,
            ledger=SubscriptionBudgetLedger(ledger.path, config.consolidation.subscription_budget),
        )
    else:
        consolidation_adapter = OpenAIConsolidationAdapter(
            config.consolidation, ledger=ledger, reserve_input=reserve_input, transport=transport,
        )
    return AuxiliaryRuntime(
        source_embedding=embed_adapter,
        query_embedding=embed_adapter,
        consolidation=consolidation_adapter,
        capability_gaps=tuple(gaps),
        ledger_path=config.ledger_path,
    )


def auxiliary_runtime_status(config: AuxiliaryRuntimeConfig) -> dict:
    runtime = build_auxiliary_runtime(config)
    budget = (
        read_auxiliary_budget_status(config.ledger_path)
        if config.ledger_path is not None
        else {"ledger_exists": False, "requests": 0, "charge_micro_usd": 0, "meter_breach": False}
    )
    return {
        "external_embedding": config.external_embedding,
        "external_consolidation": config.external_consolidation,
        "capability_gaps": runtime.capability_gaps,
        "budget": budget,
        "subscription_budget": (
            runtime.consolidation.ledger.status()
            if isinstance(runtime.consolidation, CodexCliConsolidationAdapter) else None
        ),
    }
