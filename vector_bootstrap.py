"""Fail-closed bootstrap for provably fresh vector companion generations.

This module owns setup-time backend/embedder selection. It never replaces an
existing generation: once an embedding space is active, normal runtime startup
must reopen that exact manifest or require an explicit migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture_filters import sanitize_report_text
from .embedders import BaseEmbedder, build_embedder
from .gating import config_bool
from .vector_generation import (
    GenerationIdentity,
    bootstrap_fresh_generation,
    current_generation,
)
from .vector_store import build_vector_store, normalize_vector_backend


def _generation_identity(
    *,
    backend: str,
    embedder: BaseEmbedder,
    embedder_config: dict[str, Any],
    table_name: str,
    metric: str,
) -> GenerationIdentity:
    """Build the immutable identity for one selected setup candidate."""

    return GenerationIdentity(
        backend=normalize_vector_backend(backend),
        provider=str(getattr(embedder, "provider", "") or "unknown"),
        model=str(getattr(embedder, "model", "") or "unknown"),
        dimensions=int(getattr(embedder, "dimensions", 0) or 0),
        metric=metric,
        prompt_profile=str(embedder_config.get("prompt_profile") or "default-v1"),
        document_prefix=str(embedder_config.get("document_prefix") or ""),
        query_prefix=str(embedder_config.get("query_prefix") or ""),
        request_dimensions=bool(embedder_config.get("request_dimensions", False)),
        table_name=table_name,
    )


def _zero_counts() -> dict[str, int]:
    return {"physical_rows": 0, "unique_ids": 0, "duplicate_rows": 0}


@dataclass(frozen=True)
class _CandidateProbe:
    """Read-only evidence for one configured backend/embedder pair."""

    label: str
    backend: str
    configured: bool
    identity: GenerationIdentity | None
    counts: dict[str, int]
    usable: bool
    existing: bool
    inspected: bool
    reason: str

    @property
    def physical_rows(self) -> int:
        return int(self.counts.get("physical_rows") or 0)


def vector_companion_presence(backend: str, storage_dir: Path) -> bool | None:
    """Return local physical presence, or ``None`` for remote/unknown stores."""

    normalized = normalize_vector_backend(backend)
    if normalized == "lancedb":
        path = storage_dir / "lancedb"
        return path.exists() or path.is_symlink()
    if normalized == "sqlite-bruteforce":
        path = storage_dir / "vector.sqlite3"
        return path.exists() or path.is_symlink()
    return None


def _probe_candidate(
    *,
    label: str,
    backend: str,
    embedder_config: dict[str, Any],
    storage_dir: Path,
    vector_config: dict[str, Any],
    table_name: str,
    metric: str,
) -> _CandidateProbe:
    """Inspect an existing companion without creating physical state."""

    normalized_backend = normalize_vector_backend(backend) if backend else ""
    configured = bool(normalized_backend and embedder_config)
    if not normalized_backend:
        return _CandidateProbe(
            label, "", False, None, _zero_counts(), False, False, True,
            f"{label}_backend_not_configured",
        )
    if not embedder_config:
        return _CandidateProbe(
            label, normalized_backend, False, None, _zero_counts(), False, False,
            True, f"{label}_embedder_not_configured",
        )

    presence = vector_companion_presence(normalized_backend, storage_dir)
    try:
        embedder = build_embedder(embedder_config)
        identity = _generation_identity(
            backend=normalized_backend,
            embedder=embedder,
            embedder_config=embedder_config,
            table_name=table_name,
            metric=metric,
        )
    except Exception as exc:
        detail = sanitize_report_text(str(exc))[:180]
        return _CandidateProbe(
            label,
            normalized_backend,
            configured,
            None,
            _zero_counts(),
            False,
            presence is not False,
            presence is False,
            f"{label}_embedder_build_failed:{detail}",
        )

    try:
        embedder_available = bool(embedder.is_available())
    except Exception as exc:
        detail = sanitize_report_text(str(exc))[:180]
        return _CandidateProbe(
            label,
            normalized_backend,
            configured,
            identity,
            _zero_counts(),
            False,
            presence is not False,
            presence is False,
            f"{label}_embedder_probe_failed:{detail}",
        )
    try:
        store = build_vector_store(
            normalized_backend,
            storage_dir=storage_dir,
            table_name=table_name,
            dimensions=int(embedder.dimensions),
            metric=metric,
            config=vector_config,
        )
    except Exception as exc:
        detail = sanitize_report_text(str(exc))[:180]
        return _CandidateProbe(
            label,
            normalized_backend,
            configured,
            identity,
            _zero_counts(),
            False,
            presence is not False,
            presence is False,
            f"{label}_backend_build_failed:{detail}",
        )

    try:
        backend_available = bool(store.is_available())
        if not backend_available:
            return _CandidateProbe(
                label,
                normalized_backend,
                configured,
                identity,
                _zero_counts(),
                False,
                presence is not False,
                presence is False,
                f"{label}_backend_unavailable",
            )
        open_existing = getattr(store, "open_existing", None)
        if not callable(open_existing):
            if presence is False:
                return _CandidateProbe(
                    label,
                    normalized_backend,
                    configured,
                    identity,
                    _zero_counts(),
                    embedder_available,
                    False,
                    True,
                    "missing",
                )
            return _CandidateProbe(
                label,
                normalized_backend,
                configured,
                identity,
                _zero_counts(),
                False,
                True,
                False,
                f"{label}_backend_has_no_readonly_probe",
            )
        try:
            open_existing()
        except FileNotFoundError:
            return _CandidateProbe(
                label,
                normalized_backend,
                configured,
                identity,
                _zero_counts(),
                embedder_available,
                False,
                True,
                "missing",
            )
        raw_counts = store.audit_counts()
        counts = {
            "physical_rows": int(raw_counts.get("physical_rows") or 0),
            "unique_ids": int(raw_counts.get("unique_ids") or 0),
            "duplicate_rows": int(raw_counts.get("duplicate_rows") or 0),
        }
        return _CandidateProbe(
            label,
            normalized_backend,
            configured,
            identity,
            counts,
            embedder_available,
            True,
            True,
            "available" if embedder_available else f"{label}_embedder_unavailable",
        )
    except Exception as exc:
        detail = sanitize_report_text(str(exc))[:180]
        return _CandidateProbe(
            label,
            normalized_backend,
            configured,
            identity,
            _zero_counts(),
            False,
            presence is not False,
            presence is False,
            f"{label}_backend_inspection_failed:{detail}",
        )
    finally:
        try:
            store.close()
        except Exception:
            pass


def _create_selected_empty_candidate(
    probe: _CandidateProbe,
    *,
    storage_dir: Path,
    vector_config: dict[str, Any],
    table_name: str,
    metric: str,
) -> tuple[dict[str, int], str]:
    """Open/create the one selected fresh store and prove that it is empty."""

    if probe.identity is None:
        return {}, f"{probe.label}_identity_unavailable"
    store: Any | None = None
    try:
        store = build_vector_store(
            probe.backend,
            storage_dir=storage_dir,
            table_name=table_name,
            dimensions=int(probe.identity.dimensions),
            metric=metric,
            config=vector_config,
        )
        if not store.is_available():
            return {}, f"{probe.label}_backend_unavailable"
        store.open()
        raw_counts = store.audit_counts()
        counts = {
            "physical_rows": int(raw_counts.get("physical_rows") or 0),
            "unique_ids": int(raw_counts.get("unique_ids") or 0),
            "duplicate_rows": int(raw_counts.get("duplicate_rows") or 0),
        }
        if counts["physical_rows"]:
            return counts, f"{probe.label}_became_nonempty_during_bootstrap"
        return counts, "empty_store_created"
    except Exception as exc:
        detail = sanitize_report_text(str(exc))[:180]
        return {}, f"{probe.label}_backend_open_failed:{detail}"
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def _truth_row_count(truth_conn: Any) -> int:
    row = truth_conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    return int(row[0] if row else 0)


def bootstrap_fresh_vector_companion(
    storage_dir: Path,
    runtime_config: dict[str, Any],
    *,
    truth_conn: Any,
) -> dict[str, Any]:
    """Register a generation only when every source of state is provably fresh.

    Existing manifests remain authoritative. Without one, every configured
    companion is probed read-only before selection. Any non-empty manifestless
    companion fails closed because physical dimensions and configured model names
    cannot prove which embedding space produced its vectors. Primary-first
    creation is allowed only when both truth and all companions are proven empty.
    """

    existing = current_generation(truth_conn)
    if existing is not None:
        return {
            "status": "existing",
            "selection": "existing",
            "backend": str(existing.get("backend") or ""),
            "generation_id": str(existing.get("generation_id") or ""),
            "reason": "active_generation_is_authoritative",
        }

    manifest_count = int(
        truth_conn.execute("SELECT COUNT(*) FROM vector_generations").fetchone()[0]
    )
    if manifest_count:
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": "generation_manifests_exist_without_current_pointer",
            "manifest_count": manifest_count,
            "explicit_migration_required": True,
        }

    vector_config = dict((runtime_config or {}).get("vector") or {})
    if not config_bool(vector_config, "enabled", False):
        return {
            "status": "disabled",
            "selection": "none",
            "reason": "vector_disabled",
        }

    retrieval_config = dict((runtime_config or {}).get("retrieval") or {})
    table_name = str(vector_config.get("table_name") or "memories")
    metric = str(retrieval_config.get("metric") or "cosine")
    primary_backend = str(vector_config.get("backend") or "lancedb").strip()
    fallback_backend = str(vector_config.get("fallback_backend") or "").strip()
    primary_embedder = dict(vector_config.get("embedder") or {})
    fallback_embedder = dict(vector_config.get("fallback_embedder") or {})

    probes = [
        _probe_candidate(
            label="primary",
            backend=primary_backend,
            embedder_config=primary_embedder,
            storage_dir=storage_dir,
            vector_config=vector_config,
            table_name=table_name,
            metric=metric,
        ),
        _probe_candidate(
            label="fallback",
            backend=fallback_backend,
            embedder_config=fallback_embedder,
            storage_dir=storage_dir,
            vector_config=vector_config,
            table_name=table_name,
            metric=metric,
        ),
    ]
    configured = [probe for probe in probes if probe.configured]
    uninspectable = [
        probe for probe in configured if probe.existing and not probe.inspected
    ]
    if uninspectable:
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": "legacy_companion_uninspectable:"
            + ";".join(f"{probe.label}={probe.reason}" for probe in uninspectable),
        }

    nonempty = [probe for probe in configured if probe.physical_rows > 0]
    if nonempty:
        truth_rows = _truth_row_count(truth_conn)
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": (
                "manifest_missing_nonempty_legacy_companion;"
                f"companions={','.join(probe.label for probe in nonempty)};"
                f"truth_rows={truth_rows};explicit_migration_required"
            ),
        }

    truth_rows = _truth_row_count(truth_conn)
    if truth_rows:
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": (
                "manifest_missing_truth_nonempty_companions_empty;"
                f"truth_rows={truth_rows};explicit_migration_required"
            ),
        }
    selected = next(
        (
            probe
            for label in ("primary", "fallback")
            for probe in configured
            if probe.label == label
            and probe.usable
            and probe.inspected
            and probe.identity is not None
        ),
        None,
    )
    if selected is None:
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": ";".join(
                f"{probe.label}={probe.reason}" for probe in probes
            ),
        }
    counts, create_reason = _create_selected_empty_candidate(
        selected,
        storage_dir=storage_dir,
        vector_config=vector_config,
        table_name=table_name,
        metric=metric,
    )
    if create_reason != "empty_store_created":
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": create_reason,
        }
    selection = selected.label
    selection_reason = (
        "primary_backend_and_embedder_available"
        if selected.label == "primary"
        else f"{probes[0].reason};fallback_backend_and_embedder_available"
    )
    metadata: dict[str, Any] = {
        "provenance": "fresh-setup-bootstrap",
        "selection": selection,
        "selection_reason": selection_reason,
    }

    # Recheck after physical probing. A concurrent setup may have committed the
    # authoritative generation while this process was outside a truth write.
    existing = current_generation(truth_conn)
    if existing is not None:
        return {
            "status": "existing",
            "selection": "existing",
            "backend": str(existing.get("backend") or ""),
            "generation_id": str(existing.get("generation_id") or ""),
            "reason": "concurrent_generation_became_authoritative",
        }
    if _truth_row_count(truth_conn) != 0:
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": "truth_became_nonempty_during_fresh_bootstrap",
        }

    identity = selected.identity
    assert identity is not None
    manifest = bootstrap_fresh_generation(
        truth_conn,
        identity=identity,
        storage_path=".",
        metadata=metadata,
    )
    return {
        "status": "ready",
        "selection": selection,
        "backend": identity.backend,
        "provider": identity.provider,
        "model": identity.model,
        "dimensions": identity.dimensions,
        "generation_id": str(manifest.get("generation_id") or ""),
        "row_count": int(counts.get("physical_rows") or 0),
        "reason": selection_reason,
    }
