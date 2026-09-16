"""Stable migration CLI and orchestration.

Legacy conversion, catalog reads, host activation and indexing have separate
owners. Existing public imports (and historical helper imports) remain valid.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any

from .migration_records import (
    MigrationError as MigrationError,
    _write_report as _write_report,
    _materialize_explicit_scope_selection as _materialize_explicit_scope_selection,
    _blocked_prewrite_report as _blocked_prewrite_report,
    _canon as _canon,
    _digest as _digest,
    _stable as _stable,
    _safe as _safe,
    _safe_text as _safe_text,
    _json as _json,
    _tables as _tables,
    _columns as _columns,
    _rows as _rows,
    _time as _time,
    _recorded as _recorded,
    LEGACY_BASELINE as LEGACY_BASELINE,
    REPORT_FORMAT as REPORT_FORMAT,
)
from .legacy_catalog import (
    _classify_table_disposition as _classify_table_disposition,
    _offline_source_path as _offline_source_path,
    build_legacy_catalog as build_legacy_catalog,
)
from .migration_activation import (
    _load_installation_handoff as _load_installation_handoff,
    _resolve_scope_mapping as _resolve_scope_mapping,
    _existing_target_scopes as _existing_target_scopes,
    _require_hex64 as _require_hex64,
    _require_test_absolute_target as _require_test_absolute_target,
    _archive_report_path as _archive_report_path,
    _file_sha256 as _file_sha256,
    _storage_file_digests as _storage_file_digests,
    _memory_wal_path as _memory_wal_path,
    _nonempty_wal_size as _nonempty_wal_size,
    _refuse_nonempty_wal as _refuse_nonempty_wal,
    _checkpoint_existing_memory_db as _checkpoint_existing_memory_db,
    _archive_receipt_payload as _archive_receipt_payload,
    _write_exclusive_text as _write_exclusive_text,
    _accept_identical_archive_run as _accept_identical_archive_run,
    _write_complete_archive_receipt as _write_complete_archive_receipt,
)
# Preserve the public facade without moving index ownership or job locking.
from .migration_index import queue_index_page as queue_index_page
from .legacy_conversion import (
    _procedure_claim_id as _procedure_claim_id,
    _role_origin as _role_origin,
    _metadata as _metadata,
    _scope as _scope,
    _map_scope_rows as _map_scope_rows,
    _source as _source,
    _resolve as _resolve,
    _evidence_items as _evidence_items,
    _json_list as _json_list,
    _safe_list as _safe_list,
    _claim_payload as _claim_payload,
    _claim_basis as _claim_basis,
    _procedure_slot_collisions as _procedure_slot_collisions,
    _insert_version as _insert_version,
    migrate_legacy as migrate_legacy,
)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-recall-migrate")
    parser.add_argument("--source", required=True, help="旧版离线 memory.sqlite3")
    parser.add_argument(
        "--target", help="新数据库目录；manifest 模式下由可信安装清单推导"
    )
    parser.add_argument("--report", help="迁移报告路径；已存在文件不会覆盖")
    parser.add_argument("--agent-id", default="p15-migration-agent")
    parser.add_argument("--installation-id", default="p15-migration-installation")
    parser.add_argument(
        "--scope-id", action="append", dest="scope_ids", help="重复指定以限制迁移 scope"
    )
    parser.add_argument(
        "--installation-manifest",
        help="已正常安装的 Hermes home/installation.json 或 Codex installation.json",
    )
    parser.add_argument(
        "--host",
        choices=("hermes", "codex"),
        help="manifest 所属宿主；省略时按清单路径识别",
    )
    parser.add_argument(
        "--scope-map",
        help='JSON 文件，例如 {"scope-a":"owner_private"}；值可为 manifest audience 名或已绑定 scope ID',
    )
    parser.add_argument(
        "--map-scope",
        action="append",
        default=[],
        metavar="SOURCE=AUDIENCE",
        help="重复指定 source scope 到 manifest audience 的映射",
    )
    parser.add_argument(
        "--single-scope-to",
        metavar="AUDIENCE",
        help="仅当旧库只有一个 scope 时，将它明确映射到该 audience 名或已绑定 scope ID",
    )
    parser.add_argument("--batch-key", default="p15-fixed-batch-001")
    parser.add_argument("--catalog-only", action="store_true", help="只输出 read-only 字典/计划")
    parser.add_argument("--archive-install-test", action="store_true", help="安装 TEST 归档并执行转换")
    parser.add_argument("--source-hash", help="确认源数据库的哈希")
    parser.add_argument("--catalog-hash", help="确认 catalog 的哈希")
    args = parser.parse_args(argv)

    if args.archive_install_test and args.catalog_only:
        parser.error("--catalog-only and --archive-install-test cannot be combined")
    if args.archive_install_test or args.catalog_only:
        if args.installation_manifest or args.host or args.scope_map or args.map_scope or args.single_scope_to or args.scope_ids:
            parser.error("archive/catalog modes cannot be combined with alternate manifest/map/single-scope/host/scope-selection options")

    if args.catalog_only:
        catalog = build_legacy_catalog(args.source)
        print(json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if catalog["is_supported"] else 1

    if args.archive_install_test:
        if not args.target:
            parser.error("--archive-install-test requires --target as hermes_home")
        if not args.source_hash or not args.catalog_hash:
            parser.error("--archive-install-test requires --source-hash and --catalog-hash")
        source_hash = _require_hex64(args.source_hash, "--source-hash")
        catalog_hash = _require_hex64(args.catalog_hash, "--catalog-hash")
        target_path = _require_test_absolute_target(args.target)
        catalog = build_legacy_catalog(args.source)
        if (
            catalog["source_sha256"] != source_hash
            or catalog["catalog_sha256"] != catalog_hash
        ):
            raise MigrationError(
                "source or catalog digest does not match the current files"
            )
        report_path = _archive_report_path(target_path, args.report)
        receipt_path = report_path.with_suffix(".receipt")
        report_exists = report_path.exists() or report_path.is_symlink()
        receipt_exists = receipt_path.exists() or receipt_path.is_symlink()
        if report_exists or receipt_exists:
            if not report_exists:
                raise MigrationError("preexisting receipt exists without matching report")
            if not receipt_exists:
                raise MigrationError("preexisting report exists without matching receipt")
            _accept_identical_archive_run(
                target_path=target_path,
                report_path=report_path,
                receipt_path=receipt_path,
                catalog=catalog,
                batch_key=args.batch_key,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        from scope_recall.adapters.hermes.installation import (
            install_hermes_archive_migration,
        )
        _binding, manifest, catalog = install_hermes_archive_migration(
            target_path,
            source_database=args.source,
            test_mode=True,
            expected_source_hash=source_hash,
            expected_catalog_hash=catalog_hash,
        )
        report = migrate_legacy(
            args.source,
            installation_manifest=manifest.data_directory / "installation.json",
            report_path=report_path,
            batch_key=args.batch_key,
            host="hermes",
        )
        if report["completion_status"] != "complete":
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            return 3
        _write_complete_archive_receipt(
            target_path=target_path,
            report_path=report_path,
            receipt_path=receipt_path,
            catalog=catalog,
            batch_key=args.batch_key,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.scope_map and args.map_scope:
        parser.error("--scope-map 与 --map-scope 不能同时使用")
    if args.single_scope_to and (args.scope_map or args.map_scope):
        parser.error("--single-scope-to 与 --scope-map/--map-scope 不能同时使用")
    mapping: dict[str, str] | None = None
    if args.scope_map:
        try:
            payload = json.loads(
                Path(args.scope_map).expanduser().resolve().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parser.error(f"scope map 无法读取: {exc}")
        if not isinstance(payload, dict) or any(
            type(key) is not str or type(value) is not str
            for key, value in payload.items()
        ):
            parser.error("scope map 必须是 source scope 到 audience 名/ID 的 JSON 对象")
        mapping = payload
    elif args.map_scope:
        mapping = {}
        for item in args.map_scope:
            source, separator, target = item.partition("=")
            if not separator or not target.strip():
                parser.error("--map-scope 格式必须为 SOURCE=AUDIENCE")
            target = target.strip()
            if source in mapping and mapping[source] != target:
                parser.error(f"--map-scope 重复 source 的目标不一致: {source}")
            mapping[source] = target
    if mapping is not None and not args.installation_manifest:
        parser.error("--scope-map/--map-scope 需要 --installation-manifest")
    if args.installation_manifest and mapping is None and not args.single_scope_to:
        parser.error("manifest 模式必须显式提供 --scope-map 或 --map-scope")
    if args.single_scope_to and not args.installation_manifest:
        parser.error("--single-scope-to 需要 --installation-manifest")
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
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["completion_status"] == "complete" else 3


__all__ = ["LEGACY_BASELINE", "MigrationError", "main", "migrate_legacy"]
