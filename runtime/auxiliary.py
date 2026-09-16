"""Explicit auxiliary runtime composition for approved external routes only."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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


DEFAULT_LEDGER_NAME = "auxiliary-budget.sqlite3"


def _strict_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(name)
    return value


def _strict_nonneg_int(name: str, value: object, *, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError(name)
        return default
    if type(value) is bool or type(value) is not int:
        raise ValueError(name)
    if value < 0:
        raise ValueError(name)
    return value


def _strict_positive_int(name: str, value: object, *, default: int | None = None) -> int:
    parsed = _strict_nonneg_int(name, value, default=default)
    if parsed < 1:
        raise ValueError(name)
    return parsed


def _strict_decimal(name: str, value: object) -> Decimal:
    if type(value) is bool:
        raise ValueError(name)
    if isinstance(value, Decimal):
        amount = value
    elif type(value) is int:
        amount = Decimal(value)
    elif type(value) is str:
        if not value or value.strip() != value:
            raise ValueError(name)
        try:
            amount = Decimal(value)
        except Exception as exc:
            raise ValueError(name) from exc
    else:
        raise ValueError(name)
    if not amount.is_finite() or amount < 0:
        raise ValueError(name)
    return amount


def _absolute_path(value: object, *, name: str) -> Path:
    if value is None:
        raise ValueError(name)
    path = Path(str(value))
    if not path.is_absolute():
        raise ValueError(f"{name}_must_be_absolute")
    return path


def _optional_absolute_path(value: object, *, name: str) -> Path | None:
    if value is None:
        return None
    return _absolute_path(value, name=name)


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
        if not isinstance(value, Mapping):
            raise ValueError("config_mapping_required")
        external_embedding = _strict_bool("external_embedding_bool_required", value.get("external_embedding"))
        external_consolidation = _strict_bool("external_consolidation_bool_required", value.get("external_consolidation"))
        installation_dir = _optional_absolute_path(value.get("installation_dir"), name="installation_dir")
        ledger_raw = value.get("ledger_path")
        if ledger_raw is None and installation_dir is not None:
            ledger_path = installation_dir / DEFAULT_LEDGER_NAME
        elif ledger_raw is None:
            ledger_path = None
        else:
            ledger_path = _absolute_path(ledger_raw, name="ledger_path")
        budget = _budget_policy_from_mapping(value.get("budget"))
        embedding = _embedding_route_from_mapping(value.get("embedding"))
        consolidation = _consolidation_route_from_mapping(value.get("consolidation"))
        reserve_input = _strict_positive_int(
            "consolidation_reserve_input",
            value.get("consolidation_reserve_input", 32_768),
        )
        return AuxiliaryRuntimeConfig(
            external_embedding=external_embedding,
            external_consolidation=external_consolidation,
            installation_dir=installation_dir,
            ledger_path=ledger_path,
            budget=budget,
            embedding=embedding,
            consolidation=consolidation,
            consolidation_reserve_input=reserve_input,
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


def _budget_policy_from_mapping(raw: object) -> BudgetPolicy:
    if raw is None:
        return default_budget_policy()
    if not isinstance(raw, Mapping):
        raise ValueError("budget_mapping_required")
    batch_raw = raw.get("batch", "auxiliary")
    if type(batch_raw) is not str or not batch_raw:
        raise ValueError("batch")
    base = default_budget_policy(batch=batch_raw)
    pricing: dict[str, ModelPricing] = {}
    raw_pricing = raw.get("pricing")
    if raw_pricing is None:
        raise ValueError("pricing_required")
    if not isinstance(raw_pricing, Mapping):
        raise ValueError("pricing_mapping_required")
    for model, rates in raw_pricing.items():
        if type(model) is not str or not model:
            raise ValueError("pricing_model")
        if not isinstance(rates, Mapping):
            raise ValueError("pricing_rates")
        input_rate = rates.get("input_usd_per_million")
        output_rate = rates.get("output_usd_per_million")
        if input_rate is None or output_rate is None:
            raise ValueError("pricing_rate_required")
        pricing[model] = ModelPricing(
            input_usd_per_million=_strict_decimal("input_usd_per_million", input_rate),
            output_usd_per_million=_strict_decimal("output_usd_per_million", output_rate),
        )
    approved_raw = raw.get("approved_models")
    if not isinstance(approved_raw, (list, tuple)):
        raise ValueError("approved_models_required")
    approved: set[str] = set()
    for item in approved_raw:
        if type(item) is not str or not item:
            raise ValueError("approved_models")
        approved.add(item)
    for model in approved:
        if model not in pricing:
            raise ValueError("approved_model_pricing")
    model_caps: dict[str, tuple[int | None, int | None]] = {}
    raw_caps = raw.get("model_token_caps")
    if raw_caps is not None:
        if not isinstance(raw_caps, Mapping):
            raise ValueError("model_token_caps")
        for model, caps in raw_caps.items():
            if type(model) is not str or not model:
                raise ValueError("model_token_caps")
            if not isinstance(caps, Mapping):
                raise ValueError("model_token_caps")
            # Per-model totals are cumulative in the same way, so an explicit
            # null means uncapped for that model. A missing key stays an error,
            # as before — only a stated null opts out.
            sides: list[int | None] = []
            for name, key in (("model_token_caps_input", "input"),
                              ("model_token_caps_output", "output")):
                if key not in caps:
                    raise ValueError(name)
                side = caps[key]
                sides.append(None if side is None else _strict_nonneg_int(name, side))
            model_caps[model] = (sides[0], sides[1])
    reserve_output: dict[str, int] = {}
    raw_reserve_output = raw.get("model_reserve_output")
    if raw_reserve_output is not None:
        if not isinstance(raw_reserve_output, Mapping):
            raise ValueError("model_reserve_output")
        for model, tokens in raw_reserve_output.items():
            if type(model) is not str or not model:
                raise ValueError("model_reserve_output")
            reserve_output[model] = _strict_positive_int("model_reserve_output", tokens)
    # An explicit JSON null on a cumulative cap means "not capped"; an absent key
    # still inherits the base policy, which is 0 — no spend at all — so an
    # unconfigured instance stays fail-closed. See _OPTIONAL_CUMULATIVE_CAPS.
    def _optional_cap(name: str, value: object) -> int | None:
        return None if value is None else _strict_nonneg_int(name, value)

    return BudgetPolicy(
        batch=batch_raw,
        cap_micro_usd=_optional_cap("cap_micro_usd", raw.get("cap_micro_usd", base.cap_micro_usd)),
        total_input_cap=_optional_cap("total_input_cap", raw.get("total_input_cap", base.total_input_cap)),
        total_output_cap=_optional_cap("total_output_cap", raw.get("total_output_cap", base.total_output_cap)),
        total_call_cap=_optional_cap("total_call_cap", raw.get("total_call_cap", base.total_call_cap)),
        max_request_bytes=_strict_positive_int("max_request_bytes", raw.get("max_request_bytes", base.max_request_bytes)),
        default_reserve_input=_strict_positive_int(
            "default_reserve_input",
            raw.get("default_reserve_input", base.default_reserve_input),
        ),
        default_reserve_output=_strict_positive_int(
            "default_reserve_output",
            raw.get("default_reserve_output", base.default_reserve_output),
        ),
        model_reserve_output=MappingProxyType(reserve_output or dict(base.model_reserve_output)),
        model_token_caps=MappingProxyType(model_caps or dict(base.model_token_caps)),
        pricing=MappingProxyType(pricing),
        approved_models=frozenset(approved),
    )


def _required_text(name: str, value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError(name)
    return value


def _embedding_route_from_mapping(raw: object) -> EmbeddingRouteConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("embedding_mapping_required")
    # model/endpoint/dimensions/dialect are optional and move together: omit
    # them for the shipped Gemini space, or state all four to address another
    # provider. EmbeddingRouteConfig rejects a partial descriptor, because half
    # of one would silently pair a new model with the old dimensionality and the
    # space digest would not reveal the mix.
    stated = {key: raw.get(key) for key in ("model", "endpoint", "dimensions", "dialect")}
    if all(value is None for value in stated.values()):
        return EmbeddingRouteConfig(credential_env=_required_text("credential_env", raw.get("credential_env")))
    return EmbeddingRouteConfig(
        credential_env=_required_text("credential_env", raw.get("credential_env")),
        model=_required_text("embedding_model", stated["model"]),
        endpoint=_required_text("embedding_endpoint", stated["endpoint"]),
        dimensions=_strict_positive_int("embedding_dimensions", stated["dimensions"]),
        dialect=_required_text("embedding_dialect", stated["dialect"]),
    )


def _consolidation_route_from_mapping(raw: object) -> ConsolidationRouteConfig | CodexCliRouteConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("consolidation_mapping_required")
    if raw.get("kind") == "codex_cli":
        return CodexCliRouteConfig.from_mapping(raw)
    if raw.get("kind", "openai") != "openai":
        raise ValueError("consolidation_kind")
    thinking = raw.get("thinking")
    thinking_map = MappingProxyType(dict(thinking)) if isinstance(thinking, Mapping) else None
    response_format = raw.get("response_format")
    reasoning_effort = raw.get("reasoning_effort")
    return ConsolidationRouteConfig(
        model=_required_text("model", raw.get("model")),
        endpoint=_required_text("endpoint", raw.get("endpoint")),
        credential_env=_required_text("credential_env", raw.get("credential_env")),
        output_limit_field=_required_text("output_limit_field", raw.get("output_limit_field")),
        max_output_tokens=_strict_positive_int("max_output_tokens", raw.get("max_output_tokens")),
        thinking=thinking_map,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
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
        if type(raised) is int and not isinstance(raised, bool) and raised > reserve_input:
            reserve_input = raised
    if config.ledger_path is not None:
        policy = load_formal_p18_budget_policy() or config.budget
        ledger = AuxiliaryBudgetLedger(
            config.ledger_path,
            policy,
            covered_historical_breaches=covers,
        )
    embed_adapter = None
    if config.external_embedding is True:
        if config.embedding is None:
            gaps.append("external_embedding_unconfigured")
        elif ledger is None:
            gaps.append("auxiliary_budget_ledger_unconfigured")
        else:
            embed_adapter = GeminiEmbeddingAdapter(config.embedding, ledger=ledger, transport=transport)
    else:
        gaps.append("external_embedding_not_approved")
    consolidation_adapter = None
    if config.external_consolidation is True:
        if config.consolidation is None:
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
                config.consolidation,
                ledger=ledger,
                reserve_input=reserve_input,
                transport=transport,
            )
    else:
        gaps.append("external_consolidation_not_approved")
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
