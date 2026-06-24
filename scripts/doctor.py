#!/usr/bin/env python3
"""Inspect scope-recall source metadata and runtime storage health.

The doctor is intentionally read-only. It treats SQLite as the truth layer and
any configured vector backend as an optional rebuildable companion, then emits a
compact JSON report that operators can use before running repair or release
checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

try:
    from scope_recall.capture_filters import contains_secret_like_text, redact_secret_like_text, sanitize_report_text
except Exception:  # pragma: no cover - keeps the standalone doctor script usable from source checkouts
    def contains_secret_like_text(text: Any) -> bool:
        value = "" if text is None else str(text)
        return bool(
            re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", value)
            or re.search(
                r"(?:api[_ \t-]?key|token|secret|password|passwd|credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)"
                r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
                value,
                flags=re.IGNORECASE,
            )
            or re.search(r"s" r"k-[A-Za-z0-9][A-Za-z0-9_-]{18,}", value)
            or re.search(r"g" r"h[pousr]_[A-Za-z0-9_]{20,}", value)
            or re.search(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=]{16,}", value, flags=re.IGNORECASE)
        )

    def redact_secret_like_text(text: Any) -> str:
        value = "" if text is None else str(text)
        value = re.sub(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            "[REDACTED_SECRET]",
            value,
        )
        value = re.sub(
            r"(?:api[_ \t-]?key|token|secret|password|passwd|credential(?:[_ \t-]?[a-z0-9_]+)?|private[_ \t-]?key)"
            r"(?:[ \t]*(?::|=|是)[ \t]*|[ \t]+is[ \t]+)[^\s]+",
            "[REDACTED_SECRET]",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"s" r"k-[A-Za-z0-9][A-Za-z0-9_-]{18,}", "[REDACTED_SECRET]", value)
        value = re.sub(r"g" r"h[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED_SECRET]", value)
        value = re.sub(r"bea" r"rer\s+[A-Za-z0-9._\-~+/=]{16,}", "[REDACTED_SECRET]", value, flags=re.IGNORECASE)
        return value

    def sanitize_report_text(text: Any) -> str:
        return redact_secret_like_text(text)

DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect scope-recall source/runtime health")
    parser.add_argument("--json", action="store_true", help="emit JSON output (default; accepted for operator convenience)")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT), help="scope-recall source checkout")
    parser.add_argument("--hermes-home", default="", help="Hermes home/profile path to inspect")
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def plugin_yaml_version(text: str) -> str:
    match = re.search(r"^version:\s*([^\s#]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profile_dotenv(hermes_home: Path) -> set[str]:
    env_path = hermes_home / ".env"
    loaded: set[str] = set()
    if not env_path.exists():
        return loaded
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or not value:
            continue
        loaded.add(key)
    return loaded


def load_runtime_config(source_root: Path, hermes_home: Path) -> dict[str, Any]:
    profile_env_keys = load_profile_dotenv(hermes_home)
    config: dict[str, Any] = {}
    for path in (source_root / "config.json", hermes_home / "scope-recall" / "config.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(read_text(path))
        except Exception:
            continue
        if isinstance(raw, dict):
            config = deep_merge(config, raw)
    if profile_env_keys:
        config["_profile_env_keys"] = sorted(profile_env_keys)
    return config


def coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def embedder_config_available(config: dict[str, Any], *, profile_env_keys: set[str] | None = None) -> bool:
    profile_env_keys = profile_env_keys or set()
    provider = str(config.get("provider") or "local-hash").strip().lower()
    if provider in {"local-hash", "local-debug"}:
        return True
    if provider in {"openai-compatible", "openai"}:
        if coerce_list(config.get("api_key")):
            return True
        return any(
            os.getenv(name, "").strip() or name in profile_env_keys
            for name in coerce_list(config.get("api_key_env") or "OPENAI_API_KEY")
        )
    if provider in {"sentence-transformers", "local-model", "local-embedding", "huggingface"}:
        try:
            import importlib.util

            return importlib.util.find_spec("sentence_transformers") is not None
        except Exception:
            return False
    return True


def expected_embedder_from_config(config: dict[str, Any]) -> dict[str, Any]:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    if vector_config.get("enabled") is False:
        return {}
    raw_primary = vector_config.get("embedder")
    raw_fallback = vector_config.get("fallback_embedder")
    primary: dict[str, Any] = raw_primary if isinstance(raw_primary, dict) else {}
    fallback: dict[str, Any] = raw_fallback if isinstance(raw_fallback, dict) else {}
    profile_env_keys = set(coerce_list(config.get("_profile_env_keys")))
    source = "embedder"
    selected: dict[str, Any] = dict(primary)
    if selected and not embedder_config_available(selected, profile_env_keys=profile_env_keys) and fallback and embedder_config_available(fallback, profile_env_keys=profile_env_keys):
        selected = dict(fallback)
        source = "fallback_embedder"
    if not selected:
        return {}
    return {
        "source": source,
        "provider": str(selected.get("provider") or ""),
        "model": str(selected.get("model") or ""),
        "dimensions": int(selected.get("dimensions") or 0),
    }


def vector_enabled_from_config(config: dict[str, Any]) -> bool:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    return vector_config.get("enabled") is not False


def vector_backend_from_config(config: dict[str, Any]) -> str:
    raw_vector = config.get("vector")
    vector_config: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    backend = str(vector_config.get("backend") or "lancedb").strip().lower()
    return "sqlite-bruteforce" if backend == "sqlite" else backend


def source_report(source_root: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    failures: list[str] = []
    recommendations: list[str] = []

    pyproject_path = source_root / "pyproject.toml"
    plugin_path = source_root / "plugin.yaml"
    readme_path = source_root / "README.md"
    changelog_path = source_root / "CHANGELOG.md"

    pyproject_version = ""
    plugin_version = ""
    readme_versions: list[str] = []
    changelog_has_version = False

    try:
        pyproject_version = tomllib.loads(read_text(pyproject_path))["project"]["version"]
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read pyproject version: {exc}")

    try:
        plugin_version = plugin_yaml_version(read_text(plugin_path))
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read plugin.yaml version: {exc}")

    try:
        readme_versions = re.findall(r"Version `([^`]+)`", read_text(readme_path))
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read README public version: {exc}")

    try:
        changelog_has_version = f"## [{pyproject_version}]" in read_text(changelog_path)
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read CHANGELOG version section: {exc}")

    if pyproject_version and plugin_version and pyproject_version != plugin_version:
        failures.append(f"pyproject/plugin version mismatch: {pyproject_version} != {plugin_version}")
    if pyproject_version and readme_versions != [pyproject_version]:
        failures.append(f"README public versions {readme_versions!r} do not match {pyproject_version}")
    if pyproject_version and not changelog_has_version:
        failures.append(f"CHANGELOG is missing ## [{pyproject_version}] section")

    if failures:
        recommendations.append("Align pyproject.toml, plugin.yaml, README.md, and CHANGELOG.md before release.")

    source = {
        "root": str(source_root),
        "pyproject_version": pyproject_version,
        "plugin_version": plugin_version,
        "readme_public_versions": readme_versions,
        "changelog_has_version": changelog_has_version,
    }
    check = {"ok": not failures, "failures": failures}
    return source, check, recommendations


def sqlite_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not db_path.exists():
        recommendations.append(
            "SQLite truth DB is missing; initialize scope-recall or restore memory.sqlite3 before running scripts/repair.vector_index.py."
        )
        sqlite_payload = {"path": str(db_path), "status": "missing", "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    try:
        conn = sqlite3.connect(db_path)
        try:
            tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            memory_count = 0
            if "memories" in tables:
                memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before rebuilding the vector companion.")
        sqlite_payload = {"path": str(db_path), "status": "error", "error": str(exc), "memory_count": 0, "tables": []}
        return sqlite_payload, {"ok": False, "failures": [f"SQLite truth DB error: {exc}"]}, recommendations

    sqlite_payload = {"path": str(db_path), "status": "ready", "memory_count": memory_count, "tables": tables}
    return sqlite_payload, {"ok": True, "failures": []}, recommendations


def memory_secret_report(hermes_home: Path, *, sample_limit: int = 10) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "active_secret_like_count": 0, "samples": []}, {"ok": True, "failures": []}, recommendations
    samples: list[dict[str, Any]] = []
    active_secret_like_count = 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" not in tables:
                return {"status": "schema_missing", "path": str(db_path), "active_secret_like_count": 0, "samples": []}, {"ok": True, "failures": []}, recommendations
            for row in conn.execute("SELECT id, scope_id, source, target, content, summary, updated_at, metadata FROM memories"):
                try:
                    metadata = json.loads(str(row["metadata"] or "{}"))
                except Exception:
                    metadata = {}
                if str(metadata.get("lifecycle") or "").strip().lower() == "archived":
                    continue
                content = str(row["content"] or "")
                if not contains_secret_like_text(content):
                    continue
                active_secret_like_count += 1
                if len(samples) < max(0, int(sample_limit)):
                    samples.append(
                        {
                            "id": str(row["id"]),
                            "scope_id": str(row["scope_id"] or ""),
                            "source": str(row["source"] or ""),
                            "target": str(row["target"] or ""),
                            "updated_at": str(row["updated_at"] or ""),
                            "preview": sanitize_report_text(content)[:220],
                        }
                    )
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting memory secret-scan status.")
        return {"status": "error", "path": str(db_path), "error": str(exc), "active_secret_like_count": 0, "samples": []}, {"ok": False, "failures": [f"memory secret scan error: {exc}"]}, recommendations

    payload = {"status": "ready", "path": str(db_path), "active_secret_like_count": active_secret_like_count, "samples": samples}
    if active_secret_like_count:
        recommendations.append("Active memory rows contain plaintext secret-like content; archive or hard-delete them and store only secret indexes/vault refs.")
    return payload, {"ok": active_secret_like_count == 0, "failures": [f"active plaintext secret-like memory rows: {active_secret_like_count}"] if active_secret_like_count else []}, recommendations


def journal_enabled_from_config(config: dict[str, Any]) -> bool:
    raw_journal = config.get("journal")
    journal_config: dict[str, Any] = raw_journal if isinstance(raw_journal, dict) else {}
    value = journal_config.get("enabled", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def journal_backlog_age_hours(oldest_created_at: str) -> float:
    if not oldest_created_at:
        return 0.0
    try:
        from datetime import datetime, timezone

        created = datetime.fromisoformat(str(oldest_created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def journal_report(hermes_home: Path, *, enabled: bool = True, journal_config: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    journal_config = journal_config or {}
    recommendations: list[str] = []
    storage_dir = hermes_home / "scope-recall"
    db_path = storage_dir / "memory.sqlite3"
    if not enabled:
        return {"enabled": False, "status": "disabled"}, {"ok": True, "failures": []}, recommendations
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, recommendations

    required_tables = {"journal_entries", "journal_digest_runs", "memory_journal_sources", "journal_rejections"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                recommendations.append("Initialize scope-recall with the current plugin or run journal digest once to create the journal/provenance schema.")
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "schema_missing",
                    "missing_tables": missing,
                }, {"ok": False, "failures": [f"journal tables missing: {missing}"]}, recommendations

            total_entries = int(conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])
            unprocessed_entries = int(
                conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id IS NULL OR processed_run_id = ''").fetchone()[0]
            )
            processed_entries = max(0, total_entries - unprocessed_entries)
            digest_runs = int(conn.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0])
            source_links = int(conn.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0])
            rejections = int(conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0])
            orphan_sources = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_journal_sources AS s
                    LEFT JOIN memories AS m ON m.id = s.memory_id
                    WHERE m.id IS NULL
                    """
                ).fetchone()[0]
            )
            oldest_unprocessed = conn.execute(
                """
                SELECT created_at FROM journal_entries
                WHERE processed_run_id IS NULL OR processed_run_id = ''
                ORDER BY created_at ASC LIMIT 1
                """
            ).fetchone()
            unprocessed_by_role = {
                str(row["role"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT role, COUNT(*) AS count
                    FROM journal_entries
                    WHERE processed_run_id IS NULL OR processed_run_id = ''
                    GROUP BY role
                    ORDER BY role
                    """
                )
            }
            contamination_counts: dict[str, dict[str, int]] = {}
            for marker in ("image_cache/img_", "[Image attached at:", "[inline image/", "/tmp/hermes", ".hermes/"):
                contamination_counts[marker] = {
                    "all": int(conn.execute("SELECT COUNT(*) FROM journal_entries WHERE content LIKE ?", (f"%{marker}%",)).fetchone()[0]),
                    "unprocessed": int(
                        conn.execute(
                            "SELECT COUNT(*) FROM journal_entries WHERE (processed_run_id IS NULL OR processed_run_id = '') AND content LIKE ?",
                            (f"%{marker}%",),
                        ).fetchone()[0]
                    ),
                    "tool_unprocessed": int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM journal_entries
                            WHERE (processed_run_id IS NULL OR processed_run_id = '') AND role = 'tool' AND content LIKE ?
                            """,
                            (f"%{marker}%",),
                        ).fetchone()[0]
                    ),
                }
            last_run = conn.execute(
                """
                SELECT id, started_at, finished_at, status, extractor, processed_entries, inserted, updated, skipped
                FROM journal_digest_runs
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            digest_status_counts = {
                str(row["status"] or "unknown"): int(row["count"])
                for row in conn.execute(
                    "SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS count FROM journal_digest_runs GROUP BY COALESCE(status, 'unknown') ORDER BY status"
                )
            }
            digest_extractor_counts = {
                str(row["extractor"] or "unknown"): {"runs": int(row["runs"]), "processed_entries": int(row["processed_entries"] or 0)}
                for row in conn.execute(
                    """
                    SELECT COALESCE(extractor, 'unknown') AS extractor, COUNT(*) AS runs, COALESCE(SUM(processed_entries), 0) AS processed_entries
                    FROM journal_digest_runs
                    GROUP BY COALESCE(extractor, 'unknown')
                    ORDER BY extractor
                    """
                )
            }
            recent_runs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, started_at, status, extractor, processed_entries, inserted, updated, skipped
                    FROM journal_digest_runs
                    ORDER BY started_at DESC
                    LIMIT 25
                    """
                )
            ]
            recent_status_counts: dict[str, int] = {}
            recent_extractor_counts: dict[str, int] = {}
            for row in recent_runs:
                recent_status_counts[str(row.get("status") or "unknown")] = recent_status_counts.get(str(row.get("status") or "unknown"), 0) + 1
                recent_extractor_counts[str(row.get("extractor") or "unknown")] = recent_extractor_counts.get(str(row.get("extractor") or "unknown"), 0) + 1
            retry_exhausted_rejections = int(
                conn.execute("SELECT COUNT(*) FROM journal_rejections WHERE reason LIKE 'retry-exhausted:%'").fetchone()[0]
            )
            dead_letter_rejections = int(
                conn.execute("SELECT COUNT(*) FROM journal_rejections WHERE reason LIKE 'dead-letter:%'").fetchone()[0]
            )
            retry_replay_candidates = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM journal_rejections AS r
                    JOIN journal_entries AS e ON e.id = r.journal_entry_id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE r.reason LIKE 'retry-exhausted:%'
                      AND COALESCE(e.processed_run_id, '') != ''
                      AND s.memory_id IS NULL
                    """
                ).fetchone()[0]
            )
            dead_letter_replay_candidates = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM journal_rejections AS r
                    JOIN journal_entries AS e ON e.id = r.journal_entry_id
                    LEFT JOIN memory_journal_sources AS s ON s.journal_entry_id = e.id
                    WHERE r.reason LIKE 'dead-letter:%'
                      AND COALESCE(e.processed_run_id, '') != ''
                      AND s.memory_id IS NULL
                    """
                ).fetchone()[0]
            )
            quarantine_runs = int(
                conn.execute("SELECT COUNT(*) FROM journal_digest_runs WHERE extractor = 'llm-quarantine'").fetchone()[0]
            )
            fallback_runs = int(
                conn.execute("SELECT COUNT(*) FROM journal_digest_runs WHERE extractor IN ('heuristic-fallback', 'llm-fallback') OR status = 'ok_with_fallback'").fetchone()[0]
            )
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting journal/provenance status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"journal health error: {exc}"]}, recommendations

    failures: list[str] = []
    warn_entries = max(0, coerce_int(journal_config.get("backlog_warn_entries"), 500))
    fail_entries = max(0, coerce_int(journal_config.get("backlog_fail_entries"), 3000))
    max_age_hours = max(0, coerce_int(journal_config.get("backlog_max_age_hours"), 72))
    max_entries_per_digest = max(1, coerce_int(journal_config.get("max_entries_per_digest"), 500))
    dynamic_threshold = max(0, coerce_int(journal_config.get("dynamic_backlog_threshold"), warn_entries or 500))
    ceiling = max(max_entries_per_digest, coerce_int(journal_config.get("max_entries_per_digest_ceiling"), max_entries_per_digest))
    if unprocessed_entries >= max(dynamic_threshold, 1):
        recommended_batch_size = min(ceiling, max(max_entries_per_digest, unprocessed_entries))
    else:
        recommended_batch_size = max_entries_per_digest
    estimated_runs_to_clear = 0 if unprocessed_entries == 0 else max(1, (unprocessed_entries + recommended_batch_size - 1) // recommended_batch_size)
    oldest_value = oldest_unprocessed["created_at"] if oldest_unprocessed else ""
    backlog_age = journal_backlog_age_hours(oldest_value)
    contaminated_unprocessed = sum(item["unprocessed"] for item in contamination_counts.values())
    contaminated_tool_unprocessed = sum(item["tool_unprocessed"] for item in contamination_counts.values())
    if orphan_sources:
        failures.append(f"memory_journal_sources contains {orphan_sources} orphan link(s)")
        recommendations.append("Run hygiene/repair or delete orphan memory_journal_sources before release.")
    if unprocessed_entries:
        recommendations.append("Run scripts/journal-digest.py to promote staged journal entries into durable memories.")
    if warn_entries and unprocessed_entries >= warn_entries:
        recommendations.append(
            f"Journal backlog has {unprocessed_entries} unprocessed entrie(s); increase/dynamically adjust max_entries_per_digest and verify digest throughput."
        )
    if fail_entries and unprocessed_entries > fail_entries:
        failures.append(f"journal backlog has {unprocessed_entries} unprocessed entrie(s), above fail threshold {fail_entries}")
    if max_age_hours and backlog_age > max_age_hours:
        failures.append(f"journal backlog oldest unprocessed entry is {backlog_age:.1f}h old, above threshold {max_age_hours}h")
    if contaminated_unprocessed:
        recommendations.append(
            f"Journal backlog contains {contaminated_unprocessed} unprocessed attachment/path marker hit(s); verify tool trace hygiene and sanitize_capture_text coverage."
        )
    if contaminated_tool_unprocessed:
        recommendations.append(
            f"Tool trace hygiene: {contaminated_tool_unprocessed} unprocessed tool trace marker hit(s) remain; run digest/cleanup after deploying sanitized ingestion."
        )
    digest_health_status = "ready"
    digest_health_reasons: list[str] = []
    recent_bad_runs = sum(recent_status_counts.get(status, 0) for status in ("error", "retry_scheduled", "dead_letter"))
    recent_fallback_runs = recent_status_counts.get("ok_with_fallback", 0) + recent_extractor_counts.get("heuristic-fallback", 0)
    recent_quarantine_runs = recent_extractor_counts.get("llm-quarantine", 0)
    if recent_bad_runs or recent_quarantine_runs:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_digest_failures_or_quarantine")
        recommendations.append("Journal digest recently failed or quarantined LLM batches; inspect retry/dead-letter health before relying on automated summaries.")
    if recent_fallback_runs:
        digest_health_status = "degraded"
        digest_health_reasons.append("recent_heuristic_fallback")
        recommendations.append("Journal digest recently used heuristic fallback; verify LLM extractor health and quality flags.")
    if quarantine_runs:
        digest_health_reasons.append("historical_llm_quarantine")
        recommendations.append(f"Journal digest has {quarantine_runs} historical llm-quarantine run(s); replay or classify them through retry/dead-letter tooling.")
    if retry_exhausted_rejections or dead_letter_rejections:
        digest_health_reasons.append("historical_retry_or_dead_letter_rejections")
        recommendations.append(
            f"Journal rejections include retry/dead-letter evidence (retry_exhausted={retry_exhausted_rejections}, dead_letter={dead_letter_rejections}); add replay/cleanup before declaring digest fully healthy."
        )
    if retry_replay_candidates:
        digest_health_reasons.append("retry_replay_queue_nonempty")
        recommendations.append(f"Journal recovery queue has {retry_replay_candidates} retry-exhausted entrie(s) eligible for replay; run scripts/journal.recovery.py dry-run/apply then journal-digest.")
    if dead_letter_replay_candidates:
        digest_health_reasons.append("dead_letter_replay_queue_nonempty")
        recommendations.append(f"Journal recovery queue has {dead_letter_replay_candidates} dead-letter entrie(s); only replay after fixing auth/quota/config root cause.")

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready" if not failures else "needs_repair",
        "tables": sorted(required_tables),
        "entries": {
            "total": total_entries,
            "processed": processed_entries,
            "unprocessed": unprocessed_entries,
            "oldest_unprocessed": oldest_value,
        },
        "backlog": {
            "unprocessed_by_role": dict(sorted(unprocessed_by_role.items())),
            "oldest_unprocessed_age_hours": round(backlog_age, 3),
            "contamination_counts": contamination_counts,
            "thresholds": {"warn_entries": warn_entries, "fail_entries": fail_entries, "max_age_hours": max_age_hours},
            "batch_policy": {
                "max_entries_per_digest": max_entries_per_digest,
                "dynamic_backlog_threshold": dynamic_threshold,
                "max_entries_per_digest_ceiling": ceiling,
                "recommended_batch_size": recommended_batch_size,
                "estimated_runs_to_clear": estimated_runs_to_clear,
            },
        },
        "digest_runs": digest_runs,
        "digest_health": {
            "status": digest_health_status,
            "reasons": digest_health_reasons,
            "status_counts": digest_status_counts,
            "extractor_counts": digest_extractor_counts,
            "recent_status_counts": recent_status_counts,
            "recent_extractor_counts": recent_extractor_counts,
            "fallback_runs": fallback_runs,
            "llm_quarantine_runs": quarantine_runs,
            "retry_exhausted_rejections": retry_exhausted_rejections,
            "dead_letter_rejections": dead_letter_rejections,
            "recovery_queue": {
                "retry_exhausted_candidates": retry_replay_candidates,
                "dead_letter_candidates": dead_letter_replay_candidates,
            },
            "recent_runs": recent_runs[:10],
        },
        "last_digest_run": dict(last_run) if last_run else {},
        "source_links": source_links,
        "rejections": rejections,
        "orphan_source_links": orphan_sources,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations


def lancedb_table_names(db: Any) -> list[str]:
    """Return table names across LanceDB list_tables API shapes."""
    list_tables = getattr(db, "list_tables", None)
    raw_tables: Any = list_tables() if callable(list_tables) else db.table_names()
    if isinstance(raw_tables, dict):
        raw_tables = raw_tables.get("tables", [])
    elif hasattr(raw_tables, "tables"):
        raw_tables = getattr(raw_tables, "tables")
    else:
        raw_items = list(raw_tables)
        if raw_items and all(isinstance(item, tuple) and len(item) == 2 for item in raw_items):
            mapped_items = dict(raw_items)
            if "tables" in mapped_items:
                raw_tables = mapped_items["tables"]
            else:
                raw_tables = raw_items
        else:
            raw_tables = raw_items
    return [str(name) for name in raw_tables]


def vector_dimensions(table: Any) -> int:
    try:
        vector_field = table.schema.field("vector")
        return int(getattr(vector_field.type, "list_size", 0) or 0)
    except Exception:
        return 0


def run_vector_search_smoke(table: Any, *, dimensions: int, row_count: int) -> str:
    if row_count <= 0:
        return "skipped_empty"
    if dimensions <= 0 or not hasattr(table, "search"):
        return "skipped_no_dimension"
    query = table.search([0.0] * dimensions)
    if hasattr(query, "limit"):
        query = query.limit(1)
    if hasattr(query, "to_list"):
        query.to_list()
    elif hasattr(query, "to_arrow"):
        query.to_arrow()
    return "ok"


def sqlite_indexable_memory_count(hermes_home: Path, *, index_general: bool = False) -> int:
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT target, metadata FROM memories").fetchall()
    finally:
        conn.close()
    count = 0
    for row in rows:
        if not index_general and str(row["target"] or "") == "general":
            continue
        try:
            metadata = json.loads(str(row["metadata"] or "{}"))
        except Exception:
            metadata = {}
        lifecycle = str(metadata.get("lifecycle") or "").strip().lower() if isinstance(metadata, dict) else ""
        if lifecycle in {"superseded", "obsolete", "rejected", "archived"}:
            continue
        count += 1
    return count


def apply_vector_truth_consistency(
    payload: dict[str, Any],
    *,
    hermes_home: Path,
    index_general: bool,
    recommendations: list[str],
) -> tuple[dict[str, Any], dict[str, Any], list[str]] | None:
    expected_rows = sqlite_indexable_memory_count(hermes_home, index_general=index_general)
    payload["expected_indexable_rows"] = expected_rows
    row_count = int(payload.get("row_count") or 0)
    if expected_rows > 0 and row_count <= 0:
        payload.update({"status": "needs_repair", "ready": False})
        message = "vector companion is empty while SQLite truth has indexable active memories"
        recommendations.append("Vector companion is empty but SQLite truth has active indexable rows; run scripts/repair.vector_index.py.")
        return payload, {"ok": False, "failures": [message]}, recommendations
    if expected_rows > 0 and row_count < expected_rows:
        payload["status"] = "degraded"
        recommendations.append("Vector companion has fewer rows than active SQLite truth; schedule scripts/repair.vector_index.py to rebuild the companion.")
    return None


def lancedb_vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    vector_dir = hermes_home / "scope-recall" / "lancedb"
    if not vector_dir.exists():
        recommendations.append("LanceDB companion directory is missing; run scripts/repair.vector_index.py after SQLite truth is ready.")
        payload = {"backend": "lancedb", "path": str(vector_dir), "status": "missing", "ready": False}
        return payload, {"ok": False, "failures": [f"LanceDB directory not found: {vector_dir}"]}, recommendations

    try:
        import lancedb  # type: ignore

        db = lancedb.connect(str(vector_dir))
        table_names = lancedb_table_names(db)
        if "memories" not in table_names:
            recommendations.append("LanceDB table 'memories' is missing; run scripts/repair.vector_index.py.")
            payload = {"backend": "lancedb", "path": str(vector_dir), "status": "needs_repair", "ready": False, "tables": table_names}
            return payload, {"ok": False, "failures": ["LanceDB table 'memories' is missing"]}, recommendations
        table = db.open_table("memories")
        row_count = int(table.count_rows())
        dimensions = vector_dimensions(table)
        search_smoke = run_vector_search_smoke(table, dimensions=dimensions, row_count=row_count)
        payload = {
            "backend": "lancedb",
            "path": str(vector_dir),
            "status": "ready",
            "ready": True,
            "tables": table_names,
            "row_count": row_count,
            "dimensions": dimensions,
            "search_smoke": search_smoke,
        }
        expected_dimensions = int((expected_embedder or {}).get("dimensions") or 0)
        if dimensions and expected_dimensions and dimensions != expected_dimensions:
            error = f"dimension mismatch: LanceDB table has {dimensions}, active/configured embedder expects {expected_dimensions}"
            recommendations.append("LanceDB companion dimensions do not match the active/configured embedder; run scripts/repair.vector_index.py to rebuild from SQLite truth.")
            payload.update(
                {
                    "status": "needs_repair",
                    "ready": False,
                    "error": error,
                    "expected_embedder": dict(expected_embedder or {}),
                }
            )
            return payload, {"ok": False, "failures": [error]}, recommendations
        consistency = apply_vector_truth_consistency(payload, hermes_home=hermes_home, index_general=index_general, recommendations=recommendations)
        if consistency is not None:
            return consistency
        return payload, {"ok": True, "failures": []}, recommendations
    except Exception as exc:
        recommendations.append("LanceDB companion is unreadable; run scripts/repair.vector_index.py to rebuild it from SQLite truth.")
        payload = {"backend": "lancedb", "path": str(vector_dir), "status": "needs_repair", "ready": False, "error": str(exc)}
        return payload, {"ok": False, "failures": [f"LanceDB error: {exc}"]}, recommendations


def sqlite_vector_search_smoke(conn: sqlite3.Connection, *, dimensions: int, row_count: int) -> str:
    if row_count <= 0:
        return "skipped_empty"
    row = conn.execute("SELECT vector_json FROM vector_records ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return "skipped_empty"
    vector = json.loads(str(row["vector_json"] or "[]"))
    if dimensions and len(vector) != dimensions:
        raise RuntimeError(f"stored vector has {len(vector)} dimensions, vector_meta expects {dimensions}")
    # Touch numeric values to catch malformed JSON without mutating the store.
    sum(float(item) * float(item) for item in vector)
    return "ok"


def sqlite_vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    vector_path = hermes_home / "scope-recall" / "vector.sqlite3"
    if not vector_path.exists():
        recommendations.append("sqlite-bruteforce companion DB is missing; run scripts/repair.vector_index.py after SQLite truth is ready.")
        payload = {"backend": "sqlite-bruteforce", "path": str(vector_path), "status": "missing", "ready": False}
        return payload, {"ok": False, "failures": [f"sqlite-bruteforce companion DB not found: {vector_path}"]}, recommendations

    try:
        conn = sqlite3.connect(f"file:{vector_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            required = {"vector_records", "vector_meta"}
            missing = sorted(required - set(tables))
            if missing:
                recommendations.append("sqlite-bruteforce companion schema is incomplete; run scripts/repair.vector_index.py.")
                payload = {"backend": "sqlite-bruteforce", "path": str(vector_path), "status": "needs_repair", "ready": False, "tables": tables}
                return payload, {"ok": False, "failures": [f"sqlite-bruteforce tables missing: {missing}"]}, recommendations
            row_count = int(conn.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])
            meta = {str(row["key"]): str(row["value"]) for row in conn.execute("SELECT key, value FROM vector_meta").fetchall()}
            dimensions = int(meta.get("dimensions") or 0)
            table_name = str(meta.get("table_name") or "")
            search_smoke = sqlite_vector_search_smoke(conn, dimensions=dimensions, row_count=row_count)
        finally:
            conn.close()

        payload = {
            "backend": "sqlite-bruteforce",
            "path": str(vector_path),
            "status": "ready",
            "ready": True,
            "tables": tables,
            "table": table_name,
            "row_count": row_count,
            "dimensions": dimensions,
            "search_smoke": search_smoke,
        }
        expected_dimensions = int((expected_embedder or {}).get("dimensions") or 0)
        if dimensions and expected_dimensions and dimensions != expected_dimensions:
            error = f"dimension mismatch: sqlite-bruteforce companion has {dimensions}, active/configured embedder expects {expected_dimensions}"
            recommendations.append("sqlite-bruteforce companion dimensions do not match the active/configured embedder; run scripts/repair.vector_index.py to rebuild from SQLite truth.")
            payload.update(
                {
                    "status": "needs_repair",
                    "ready": False,
                    "error": error,
                    "expected_embedder": dict(expected_embedder or {}),
                }
            )
            return payload, {"ok": False, "failures": [error]}, recommendations
        consistency = apply_vector_truth_consistency(payload, hermes_home=hermes_home, index_general=index_general, recommendations=recommendations)
        if consistency is not None:
            return consistency
        return payload, {"ok": True, "failures": []}, recommendations
    except Exception as exc:
        recommendations.append("sqlite-bruteforce companion is unreadable; run scripts/repair.vector_index.py to rebuild it from SQLite truth.")
        payload = {"backend": "sqlite-bruteforce", "path": str(vector_path), "status": "needs_repair", "ready": False, "error": str(exc)}
        return payload, {"ok": False, "failures": [f"sqlite-bruteforce error: {exc}"]}, recommendations


def vector_report(
    hermes_home: Path,
    *,
    expected_embedder: dict[str, Any] | None = None,
    backend: str = "lancedb",
    index_general: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    normalized = "sqlite-bruteforce" if str(backend or "lancedb").strip().lower() == "sqlite" else str(backend or "lancedb").strip().lower()
    if normalized == "sqlite-bruteforce":
        return sqlite_vector_report(hermes_home, expected_embedder=expected_embedder, index_general=index_general)
    if normalized == "lancedb":
        return lancedb_vector_report(hermes_home, expected_embedder=expected_embedder, index_general=index_general)
    payload = {"backend": normalized, "status": "unsupported", "ready": False}
    return payload, {"ok": False, "failures": [f"unsupported vector backend: {normalized}"]}, ["Set vector.backend to 'lancedb' or 'sqlite-bruteforce'."]


def experience_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    raw_experience = config.get("experience")
    experience_config: dict[str, Any] = raw_experience if isinstance(raw_experience, dict) else {}
    keys = (
        "enabled",
        "prefetch_enabled",
        "auto_promotion_enabled",
        "auto_promotion_limit_sessions",
        "auto_promote_low_risk",
        "promotion_min_entries",
        "promotion_min_tool_entries",
        "promotion_require_verification",
    )
    return {key: experience_config.get(key) for key in keys if key in experience_config}


def experience_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    required_tables = {
        "task_episodes",
        "procedural_playbooks",
        "procedural_playbooks_fts",
        "playbook_versions",
        "experience_runs",
        "reflection_events",
        "fact_freshness",
        "skill_anchors",
        "skill_conflicts",
    }
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, [
            "Initialize scope-recall with the current plugin to create Experience Kernel tables."
        ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                recommendations.append("Initialize scope-recall with the current plugin so ensure_schema() creates Experience Kernel tables.")
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "schema_missing",
                    "missing_tables": missing,
                }, {"ok": False, "failures": [f"experience tables missing: {missing}"]}, recommendations
            playbook_total = int(conn.execute("SELECT COUNT(*) FROM procedural_playbooks").fetchone()[0])
            playbook_by_status = {
                redact_secret_like_text(row["status"]): int(row["count"])
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM procedural_playbooks GROUP BY status")
            }
            run_total = int(conn.execute("SELECT COUNT(*) FROM experience_runs").fetchone()[0])
            run_by_outcome = {
                redact_secret_like_text(row["outcome"]): int(row["count"])
                for row in conn.execute("SELECT outcome, COUNT(*) AS count FROM experience_runs GROUP BY outcome")
            }
            stale_facts = int(conn.execute("SELECT COUNT(*) FROM fact_freshness WHERE status IN ('stale', 'needs_live_check')").fetchone()[0])
            promoted_missing_verified_at = int(
                conn.execute("SELECT COUNT(*) FROM procedural_playbooks WHERE status = 'promoted' AND COALESCE(last_verified_at, '') = ''").fetchone()[0]
            )
            duplicate_groups = [
                {
                    "task_class": redact_secret_like_text(str(row["task_class"] or "")),
                    "title": redact_secret_like_text(str(row["title"] or "")),
                    "count": int(row["count"]),
                    "statuses": redact_secret_like_text(str(row["statuses"] or "")),
                }
                for row in conn.execute(
                    """
                    SELECT task_class, title, COUNT(*) AS count, GROUP_CONCAT(status, ',') AS statuses
                    FROM procedural_playbooks
                    WHERE status NOT IN ('superseded', 'quarantined')
                    GROUP BY task_class, title
                    HAVING COUNT(*) > 1
                    ORDER BY count DESC, title ASC
                    LIMIT 10
                    """
                )
            ]
            misleading_runs = int(conn.execute("SELECT COUNT(*) FROM experience_runs WHERE outcome = 'misleading'").fetchone()[0])
            stale_runs = int(conn.execute("SELECT COUNT(*) FROM experience_runs WHERE outcome = 'stale'").fetchone()[0])
            unresolved_feedback = {
                str(row["outcome"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT r.outcome, COUNT(*) AS count
                    FROM experience_runs AS r
                    JOIN procedural_playbooks AS p ON p.id = r.playbook_id
                    WHERE r.outcome IN ('misleading', 'stale')
                      AND p.status NOT IN ('quarantined', 'superseded')
                    GROUP BY r.outcome
                    """
                ).fetchall()
            }
            unresolved_misleading_runs = int(unresolved_feedback.get("misleading", 0))
            unresolved_stale_runs = int(unresolved_feedback.get("stale", 0))
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting Experience Kernel status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"experience health error: {exc}"]}, recommendations

    needs_review_count = int(playbook_by_status.get("needs_review", 0))
    promoted_count = int(playbook_by_status.get("promoted", 0))
    quarantined_count = int(playbook_by_status.get("quarantined", 0))
    needs_review_ratio = (needs_review_count / playbook_total) if playbook_total else 0.0
    if needs_review_ratio >= 0.5 and playbook_total:
        recommendations.append(f"Experience promotion funnel is review-heavy ({needs_review_count}/{playbook_total} needs_review); tighten promotion scoring and dedupe candidates.")
    if duplicate_groups:
        recommendations.append(f"Experience playbooks contain {len(duplicate_groups)} duplicate title/task-class group(s); run dedupe/merge review before auto-promotion.")
    if promoted_missing_verified_at:
        recommendations.append(f"{promoted_missing_verified_at} promoted playbook(s) lack last_verified_at; require verification feedback before direct reuse.")
    if unresolved_misleading_runs or unresolved_stale_runs:
        recommendations.append(
            f"Experience feedback includes unresolved stale/misleading outcomes "
            f"(stale={unresolved_stale_runs}/{stale_runs}, misleading={unresolved_misleading_runs}/{misleading_runs}); "
            "quarantine or review affected playbooks."
        )

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": "ready",
        "tables": sorted(required_tables),
        "playbooks": {"total": playbook_total, "by_status": dict(sorted(playbook_by_status.items()))},
        "promotion_funnel": {
            "needs_review": needs_review_count,
            "promoted": promoted_count,
            "quarantined": quarantined_count,
            "needs_review_ratio": round(needs_review_ratio, 3),
            "duplicate_groups": duplicate_groups,
            "promoted_missing_last_verified_at": promoted_missing_verified_at,
            "feedback": {
                "stale": stale_runs,
                "misleading": misleading_runs,
                "unresolved_stale": unresolved_stale_runs,
                "unresolved_misleading": unresolved_misleading_runs,
            },
        },
        "runs": {"total": run_total, "by_outcome": dict(sorted(run_by_outcome.items()))},
        "stale_facts": stale_facts,
    }
    return payload, {"ok": True, "failures": []}, recommendations


def nightly_digest_report(hermes_home: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    recommendations: list[str] = []
    db_path = hermes_home / "scope-recall" / "memory.sqlite3"
    required_tables = {"nightly_digest_runs"}
    if not db_path.exists():
        return {"enabled": True, "status": "missing", "path": str(db_path)}, {"ok": False, "failures": [f"SQLite truth DB not found: {db_path}"]}, [
            "Initialize scope-recall or restore memory.sqlite3 before trusting nightly digest status."
        ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(required_tables - tables)
            if missing:
                return {
                    "enabled": True,
                    "path": str(db_path),
                    "status": "not_initialized",
                    "missing_tables": missing,
                }, {"ok": True, "failures": []}, ["Run scripts/nightly-digest.py once if this deployment uses nightly digest consolidation."]
            total_runs = int(conn.execute("SELECT COUNT(*) FROM nightly_digest_runs").fetchone()[0])
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, digest_date, started_at, finished_at, extractor, model, dry_run,
                           status, inserted, updated, skipped, deleted, error
                    FROM nightly_digest_runs
                    ORDER BY started_at DESC
                    LIMIT 10
                    """
                )
            ]
            by_status = {
                redact_secret_like_text(row["status"]): int(row["count"])
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM nightly_digest_runs GROUP BY status")
            }
        finally:
            conn.close()
    except Exception as exc:
        recommendations.append("Repair or restore the SQLite truth DB before trusting nightly digest status.")
        return {"enabled": True, "path": str(db_path), "status": "error", "error": str(exc)}, {"ok": False, "failures": [f"nightly digest health error: {exc}"]}, recommendations

    for row in rows:
        row["error"] = redact_secret_like_text(row.get("error") or "")

    latest = rows[0] if rows else {}
    latest_status = str(latest.get("status") or "")
    consecutive_errors = 0
    for row in rows:
        if str(row.get("status") or "") != "error":
            break
        consecutive_errors += 1

    recent_errors = [row for row in rows if str(row.get("status") or "") == "error"]
    recent_fallbacks = [row for row in rows if "fallback" in str(row.get("status") or "")]
    failures: list[str] = []
    if latest_status == "error":
        failures.append(f"latest nightly digest run failed: {latest.get('error') or latest.get('started_at')}")
    if consecutive_errors >= 3:
        failures.append(f"nightly digest has {consecutive_errors} consecutive error run(s)")
    if recent_fallbacks:
        recommendations.append("Nightly digest recently used fallback; inspect extractor/model timeout and provider health before relying on automated summaries.")
    if recent_errors and latest_status != "error":
        recommendations.append("Recent nightly digest errors exist but the latest run recovered; keep monitoring timeout/fallback trends.")

    status = "ready"
    if failures:
        status = "needs_attention"
    elif recent_fallbacks or recent_errors:
        status = "degraded"

    payload = {
        "enabled": True,
        "path": str(db_path),
        "status": status,
        "tables": sorted(required_tables),
        "runs": {"total": total_runs, "by_status": dict(sorted(by_status.items()))},
        "latest_run": latest,
        "recent_runs": rows,
        "consecutive_errors": consecutive_errors,
    }
    return payload, {"ok": not failures, "failures": failures}, recommendations


def disabled_vector_report() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    payload = {"enabled": False, "status": "disabled", "ready": False}
    return payload, {"ok": True, "failures": []}, []


def main() -> int:
    args = parse_args()
    source_root = Path(args.source_root).expanduser().resolve()
    source, source_check, recommendations = source_report(source_root)
    checks: dict[str, Any] = {"source_metadata": source_check}
    payload: dict[str, Any] = {"source": source, "checks": checks, "recommendations": recommendations, "runtime": {}}

    if args.hermes_home:
        hermes_home = Path(args.hermes_home).expanduser().resolve()
        runtime_config = load_runtime_config(source_root, hermes_home)
        expected_embedder = expected_embedder_from_config(runtime_config)
        sqlite_payload, sqlite_check, sqlite_recommendations = sqlite_report(hermes_home)
        secret_payload, secret_check, secret_recommendations = memory_secret_report(hermes_home)
        raw_journal = runtime_config.get("journal")
        journal_config = raw_journal if isinstance(raw_journal, dict) else {}
        journal_payload, journal_check, journal_recommendations = journal_report(
            hermes_home,
            enabled=journal_enabled_from_config(runtime_config),
            journal_config=journal_config,
        )
        experience_payload, experience_check, experience_recommendations = experience_report(hermes_home)
        experience_payload["config"] = experience_config_summary(runtime_config)
        nightly_payload, nightly_check, nightly_recommendations = nightly_digest_report(hermes_home)
        if vector_enabled_from_config(runtime_config):
            backend = vector_backend_from_config(runtime_config)
            raw_vector_config = runtime_config.get("vector")
            vector_config = raw_vector_config if isinstance(raw_vector_config, dict) else {}
            raw_index_general = vector_config.get("index_general", False)
            if isinstance(raw_index_general, str):
                index_general = raw_index_general.strip().lower() in {"1", "true", "yes", "on"}
            else:
                index_general = bool(raw_index_general)
            vector_payload, vector_check, vector_recommendations = vector_report(
                hermes_home,
                expected_embedder=expected_embedder,
                backend=backend,
                index_general=index_general,
            )
        else:
            backend = "disabled"
            vector_payload, vector_check, vector_recommendations = disabled_vector_report()
        vector_payload.setdefault("backend", backend)
        payload["runtime"] = {
            "hermes_home": str(hermes_home),
            "expected_embedder": expected_embedder,
            "vector_backend": backend,
            "sqlite": sqlite_payload,
            "memory_secret_scan": secret_payload,
            "journal": journal_payload,
            "experience": experience_payload,
            "nightly_digest": nightly_payload,
            "vector": vector_payload,
        }
        checks["sqlite_truth"] = sqlite_check
        checks["memory_secret_scan"] = secret_check
        checks["journal_provenance"] = journal_check
        checks["experience_kernel"] = experience_check
        checks["nightly_digest"] = nightly_check
        checks["vector_companion"] = vector_check
        recommendations.extend(sqlite_recommendations)
        recommendations.extend(secret_recommendations)
        recommendations.extend(journal_recommendations)
        recommendations.extend(experience_recommendations)
        recommendations.extend(nightly_recommendations)
        recommendations.extend(vector_recommendations)

    payload["ok"] = all(bool(check.get("ok")) for check in checks.values())
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
