"""Stable migration CLI and orchestration.

Legacy conversion, catalog reads, host activation and indexing have separate
owners; this module is the public facade and the command line.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any, Callable

from .migration_records import (
    LEGACY_BASELINE as LEGACY_BASELINE,
    MigrationError as MigrationError,
    _stable as _stable,
)
from .legacy_catalog import build_legacy_catalog as build_legacy_catalog
from .migration_activation import (
    _load_installation_handoff as _load_installation_handoff,
    _accept_identical_archive_run,
    _archive_report_path,
    _require_hex64,
    _require_test_absolute_target,
    _write_complete_archive_receipt,
)
from .migration_index import queue_index_page as queue_index_page
from .legacy_conversion import migrate_legacy as migrate_legacy

_ARGUMENTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("--source", {"required": True, "help": "旧版离线 memory.sqlite3"}),
    ("--target", {"help": "新数据库目录；manifest 模式下由可信安装清单推导"}),
    ("--report", {"help": "迁移报告路径；已存在文件不会覆盖"}),
    ("--agent-id", {"default": "p15-migration-agent"}),
    ("--installation-id", {"default": "p15-migration-installation"}),
    ("--scope-id", {"action": "append", "dest": "scope_ids", "help": "重复指定以限制迁移 scope"}),
    ("--installation-manifest", {"help": "已正常安装的 Hermes home/installation.json 或 Codex installation.json"}),
    ("--host", {"choices": ("hermes", "codex"), "help": "manifest 所属宿主；省略时按清单路径识别"}),
    ("--scope-map", {"help": 'JSON 文件，例如 {"scope-a":"owner_private"}；值可为 manifest audience 名或已绑定 scope ID'}),
    ("--map-scope", {"action": "append", "default": [], "metavar": "SOURCE=AUDIENCE", "help": "重复指定 source scope 到 manifest audience 的映射"}),
    ("--single-scope-to", {"metavar": "AUDIENCE", "help": "仅当旧库只有一个 scope 时，将它明确映射到该 audience 名或已绑定 scope ID"}),
    ("--batch-key", {"default": "p15-fixed-batch-001"}),
    ("--catalog-only", {"action": "store_true", "help": "只输出 read-only 字典/计划"}),
    ("--archive-install-test", {"action": "store_true", "help": "安装 TEST 归档并执行转换"}),
    ("--source-hash", {"help": "确认源数据库的哈希"}),
    ("--catalog-hash", {"help": "确认 catalog 的哈希"}),
)

_Rule = tuple[Callable[[argparse.Namespace], object], str]
# Option combinations the parser rejects, checked in this order.
_OPTION_RULES: tuple[_Rule, ...] = (
    (lambda a: a.archive_install_test and a.catalog_only,
     "--catalog-only and --archive-install-test cannot be combined"),
    (lambda a: (a.archive_install_test or a.catalog_only)
     and (a.installation_manifest or a.host or a.scope_map or a.map_scope or a.single_scope_to or a.scope_ids),
     "archive/catalog modes cannot be combined with alternate manifest/map/single-scope/host/scope-selection options"),
    (lambda a: a.archive_install_test and not a.target,
     "--archive-install-test requires --target as hermes_home"),
    (lambda a: a.archive_install_test and not (a.source_hash and a.catalog_hash),
     "--archive-install-test requires --source-hash and --catalog-hash"),
    (lambda a: a.scope_map and a.map_scope,
     "--scope-map 与 --map-scope 不能同时使用"),
    (lambda a: a.single_scope_to and (a.scope_map or a.map_scope),
     "--single-scope-to 与 --scope-map/--map-scope 不能同时使用"),
)
# Checked after the scope map is parsed; ``mapping`` is None when none was given.
_MAPPING_RULES: tuple[tuple[Callable[[argparse.Namespace, dict | None], object], str], ...] = (
    (lambda a, mapping: mapping is not None and not a.installation_manifest,
     "--scope-map/--map-scope 需要 --installation-manifest"),
    (lambda a, mapping: a.installation_manifest and mapping is None and not a.single_scope_to,
     "manifest 模式必须显式提供 --scope-map 或 --map-scope"),
    (lambda a, mapping: a.single_scope_to and not a.installation_manifest,
     "--single-scope-to 需要 --installation-manifest"),
)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _scope_mapping(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, str] | None:
    """The explicit source->audience map from --scope-map or --map-scope."""
    if args.scope_map:
        try:
            payload = json.loads(Path(args.scope_map).expanduser().resolve().read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parser.error(f"scope map 无法读取: {exc}")
        if not isinstance(payload, dict) or any(type(key) is not str or type(value) is not str for key, value in payload.items()):
            parser.error("scope map 必须是 source scope 到 audience 名/ID 的 JSON 对象")
        return payload
    if not args.map_scope:
        return None
    mapping: dict[str, str] = {}
    for item in args.map_scope:
        source, separator, target = item.partition("=")
        if not separator or not target.strip():
            parser.error("--map-scope 格式必须为 SOURCE=AUDIENCE")
        target = target.strip()
        if source in mapping and mapping[source] != target:
            parser.error(f"--map-scope 重复 source 的目标不一致: {source}")
        mapping[source] = target
    return mapping


def _catalog_only(args: argparse.Namespace, mapping: dict[str, str] | None) -> int:
    catalog = build_legacy_catalog(args.source)
    _emit(catalog)
    return 0 if catalog["is_supported"] else 1


def _archive_install_test(args: argparse.Namespace, mapping: dict[str, str] | None) -> int:
    """Install a TEST archive bound to the exact source/catalog digests and convert."""
    source_hash = _require_hex64(args.source_hash, "--source-hash")
    catalog_hash = _require_hex64(args.catalog_hash, "--catalog-hash")
    target_path = _require_test_absolute_target(args.target)
    catalog = build_legacy_catalog(args.source)
    if catalog["source_sha256"] != source_hash or catalog["catalog_sha256"] != catalog_hash:
        raise MigrationError("source or catalog digest does not match the current files")
    report_path = _archive_report_path(target_path, args.report)
    receipt_path = report_path.with_suffix(".receipt")
    if _present(report_path) or _present(receipt_path):
        if not _present(report_path):
            raise MigrationError("preexisting receipt exists without matching report")
        if not _present(receipt_path):
            raise MigrationError("preexisting report exists without matching receipt")
        _accept_identical_archive_run(
            target_path=target_path, report_path=report_path, receipt_path=receipt_path,
            catalog=catalog, batch_key=args.batch_key,
        )
        _emit(json.loads(report_path.read_text(encoding="utf-8")))
        return 0
    from scope_recall.adapters.hermes.installation import install_hermes_archive_migration

    _binding, manifest, catalog = install_hermes_archive_migration(
        target_path, source_database=args.source, test_mode=True,
        expected_source_hash=source_hash, expected_catalog_hash=catalog_hash,
    )
    report = migrate_legacy(
        args.source,
        installation_manifest=manifest.data_directory / "installation.json",
        report_path=report_path,
        batch_key=args.batch_key,
        host="hermes",
    )
    if report["completion_status"] != "complete":
        _emit(report)
        return 3
    _write_complete_archive_receipt(
        target_path=target_path, report_path=report_path, receipt_path=receipt_path,
        catalog=catalog, batch_key=args.batch_key,
    )
    _emit(report)
    return 0


def _migrate(args: argparse.Namespace, mapping: dict[str, str] | None) -> int:
    report = migrate_legacy(
        args.source,
        args.target,
        agent_id=args.agent_id,
        installation_id=args.installation_id,
        scope_ids=args.scope_ids,
        batch_key=args.batch_key,
        report_path=args.report,
        installation_manifest=args.installation_manifest,
        host=args.host,
        source_scope_map=mapping,
        single_scope_to=args.single_scope_to,
    )
    _emit(report)
    return 0 if report["completion_status"] == "complete" else 3


_MODES = {"catalog": _catalog_only, "archive": _archive_install_test, "migrate": _migrate}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-recall-migrate")
    for flag, options in _ARGUMENTS:
        parser.add_argument(flag, **options)
    args = parser.parse_args(argv)
    for rejected, message in _OPTION_RULES:
        if rejected(args):
            parser.error(message)
    mapping = _scope_mapping(args, parser)
    for rejected_with_map, message in _MAPPING_RULES:
        if rejected_with_map(args, mapping):
            parser.error(message)
    mode = "catalog" if args.catalog_only else "archive" if args.archive_install_test else "migrate"
    return _MODES[mode](args, mapping)


__all__ = ["LEGACY_BASELINE", "MigrationError", "main", "migrate_legacy"]
