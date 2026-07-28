"""Fail-closed bootstrap for provably fresh vector companion generations.

This module owns setup-time backend/embedder selection. It never replaces an
existing generation: once an embedding space is active, normal runtime startup
must reopen that exact manifest or require an explicit migration.
"""

from __future__ import annotations

import shutil
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
from .vector_mutation_guard import vector_mutation_guard
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


def _sqlite_companion_paths(storage_dir: Path) -> tuple[Path, ...]:
    """Return the SQLite main file and sidecars that share one ownership boundary."""

    base = storage_dir / "vector.sqlite3"
    return (
        base,
        Path(f"{base}-wal"),
        Path(f"{base}-shm"),
        Path(f"{base}-journal"),
    )


def vector_companion_presence(backend: str, storage_dir: Path) -> bool | None:
    """Return local physical presence, or ``None`` for remote/unknown stores."""

    normalized = normalize_vector_backend(backend)
    if normalized == "lancedb":
        path = storage_dir / "lancedb"
        return path.exists() or path.is_symlink()
    if normalized == "sqlite-bruteforce":
        return any(
            path.exists() or path.is_symlink()
            for path in _sqlite_companion_paths(storage_dir)
        )
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
    probe_readiness: bool = True,
) -> _CandidateProbe:
    """Inspect one companion and optionally verify its embedder readiness.

    ``probe_readiness=False`` is the state-discovery phase: it may open existing
    storage read-only but must not load or download the configured model.
    """

    normalized_backend = normalize_vector_backend(backend) if backend else ""
    configured = bool(normalized_backend and embedder_config)
    if not normalized_backend:
        return _CandidateProbe(
            label, "", False, None, _zero_counts(), False, False, True,
            f"{label}_backend_not_configured",
        )
    presence = vector_companion_presence(normalized_backend, storage_dir)
    if not embedder_config:
        return _CandidateProbe(
            label,
            normalized_backend,
            False,
            None,
            _zero_counts(),
            False,
            presence is not False,
            presence is False,
            f"{label}_embedder_not_configured",
        )

    try:
        embedder = build_embedder(embedder_config)
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
            f"{label}_embedder_identity_failed:{detail}",
        )

    embedder_available = False
    embedder_reason = f"{label}_embedder_unavailable"
    try:
        embedder_available = bool(embedder.is_available())
    except Exception as exc:
        detail = sanitize_report_text(str(exc))[:180]
        embedder_reason = f"{label}_embedder_probe_failed:{detail}"
    else:
        if embedder_available:
            embedder_reason = "available"
            readiness_probe = getattr(embedder, "probe_readiness", None)
            if probe_readiness and callable(readiness_probe):
                try:
                    readiness_probe()
                except Exception as exc:
                    detail = sanitize_report_text(str(exc))[:180]
                    embedder_available = False
                    embedder_reason = (
                        f"{label}_embedder_readiness_failed:{detail}"
                    )
                else:
                    try:
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
                            f"{label}_embedder_identity_failed:{detail}",
                        )
        else:
            embedder_reason = f"{label}_embedder_unavailable"
    try:
        store = build_vector_store(
            normalized_backend,
            storage_dir=storage_dir,
            table_name=table_name,
            dimensions=int(identity.dimensions),
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
                    "missing" if embedder_available else embedder_reason,
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
            if presence is True:
                return _CandidateProbe(
                    label,
                    normalized_backend,
                    configured,
                    identity,
                    _zero_counts(),
                    False,
                    True,
                    False,
                    f"{label}_existing_companion_table_missing",
                )
            return _CandidateProbe(
                label,
                normalized_backend,
                configured,
                identity,
                _zero_counts(),
                embedder_available,
                False,
                True,
                "missing" if embedder_available else embedder_reason,
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
            "available" if embedder_available else embedder_reason,
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


def _compensate_new_empty_companion(
    probe: _CandidateProbe,
    *,
    storage_dir: Path,
    counts: dict[str, int],
) -> bool:
    """Remove only a local empty companion proven to be created by this attempt."""

    if probe.existing or int(counts.get("physical_rows") or 0) != 0:
        return True
    backend = normalize_vector_backend(probe.backend)
    if backend == "lancedb":
        paths = [storage_dir / "lancedb"]
    elif backend == "sqlite-bruteforce":
        paths = list(_sqlite_companion_paths(storage_dir))
    else:
        return False
    try:
        for path in paths:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
    except OSError:
        return False
    return not any(path.exists() or path.is_symlink() for path in paths)


def _truth_row_count(truth_conn: Any) -> int:
    row = truth_conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    return int(row[0] if row else 0)


def bootstrap_fresh_vector_companion(
    storage_dir: Path,
    runtime_config: dict[str, Any],
    *,
    truth_conn: Any,
) -> dict[str, Any]:
    """Serialize and run one fail-closed fresh-generation bootstrap."""

    with vector_mutation_guard(storage_dir=storage_dir):
        return _bootstrap_fresh_vector_companion_guarded(
            storage_dir,
            runtime_config,
            truth_conn=truth_conn,
        )


def _bootstrap_fresh_vector_companion_guarded(
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
            probe_readiness=False,
        ),
        _probe_candidate(
            label="fallback",
            backend=fallback_backend,
            embedder_config=fallback_embedder,
            storage_dir=storage_dir,
            vector_config=vector_config,
            table_name=table_name,
            metric=metric,
            probe_readiness=False,
        ),
    ]
    configured = [probe for probe in probes if probe.configured]
    uninspectable = [probe for probe in probes if probe.existing and not probe.inspected]
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
    # Model readiness may download or load large artifacts. Delay it until all
    # state is proven fresh, then stop after the first fully usable candidate.
    # The read-only probes above still inspect every configured companion so an
    # unused fallback cannot hide manifestless rows.
    attempted = {probe.label: probe for probe in probes}
    selected: _CandidateProbe | None = None
    candidate_inputs = {
        "primary": (primary_backend, primary_embedder),
        "fallback": (fallback_backend, fallback_embedder),
    }
    for label in ("primary", "fallback"):
        static_probe = attempted[label]
        if not (
            static_probe.configured
            and static_probe.usable
            and static_probe.inspected
            and static_probe.identity is not None
        ):
            continue
        backend, embedder_config = candidate_inputs[label]
        readiness_probe = _probe_candidate(
            label=label,
            backend=backend,
            embedder_config=embedder_config,
            storage_dir=storage_dir,
            vector_config=vector_config,
            table_name=table_name,
            metric=metric,
            probe_readiness=True,
        )
        attempted[label] = readiness_probe
        if (
            readiness_probe.usable
            and readiness_probe.inspected
            and readiness_probe.identity is not None
        ):
            selected = readiness_probe
            break
    if selected is None:
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": ";".join(
                f"{label}={attempted[label].reason}"
                for label in ("primary", "fallback")
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
        else (
            f"{attempted['primary'].reason};"
            "fallback_backend_and_embedder_available"
        )
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
        cleaned = _compensate_new_empty_companion(
            selected,
            storage_dir=storage_dir,
            counts=counts,
        )
        return {
            "status": "unavailable",
            "selection": "none",
            "reason": (
                "truth_became_nonempty_during_fresh_bootstrap"
                if cleaned
                else "truth_became_nonempty_during_fresh_bootstrap;companion_cleanup_failed"
            ),
        }

    identity = selected.identity
    assert identity is not None
    try:
        manifest = bootstrap_fresh_generation(
            truth_conn,
            identity=identity,
            storage_path=".",
            metadata=metadata,
        )
    except Exception:
        try:
            authoritative = current_generation(truth_conn)
        except Exception:
            raise RuntimeError(
                "fresh vector manifest publish failed and manifest state is unreadable; "
                "empty companion retained for explicit recovery"
            ) from None
        cleaned = authoritative is not None or _compensate_new_empty_companion(
            selected,
            storage_dir=storage_dir,
            counts=counts,
        )
        if not cleaned:
            raise RuntimeError(
                "fresh vector manifest publish failed and empty companion cleanup failed; recovery required"
            ) from None
        raise
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
