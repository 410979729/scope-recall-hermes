"""Read-only import previews and explicitly approved, resumable capture passes."""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from scope_recall.contracts import ContractError
from scope_recall.core.capture_filters import redact_secret_text, strip_private_content
from scope_recall.core.memory_import import IMPORT_BATCH_SIZE, digest, import_record, prefetch_new_sources

from .import_read import timestamp
from .import_sources import read_source


def _public_item(item, destination_project=None):
    content, removed = strip_private_content(item.content)
    masked, _count = redact_secret_text(content)
    return dict(
        key=item.key,
        project=item.project,
        write_project=destination_project or item.project,
        session=item.session,
        occurred_at=item.occurred_at,
        role=item.role,
        origin="imported",
        original_origin=item.original_origin,
        review_required=item.original_origin != "human_direct",
        private_content_omitted=removed or item.private_content_omitted,
        content=masked,
    )


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None


def _selected(item, project, after, before):
    moment = _instant(item.occurred_at)
    return (
        (project is None or (item.project or "(unassigned)") == project)
        and (after is None or moment is not None and moment >= after)
        and (before is None or moment is not None and moment <= before)
    )


def preview_import(
    source: str,
    path: Path,
    *,
    source_id: str,
    project=None,
    since=None,
    until=None,
    destination_project=None,
    target=None,
):
    """Parse selected local evidence without opening, initializing or migrating a target store."""
    if not source_id.strip():
        raise ContractError("INPUT_INVALID", "import_source_id")
    after, before = _instant(timestamp(since)), _instant(timestamp(until))
    if after and before and after > before:
        raise ContractError("INPUT_INVALID", "import_time_range")
    skipped = Counter()
    loaded = read_source(source, path, skipped=skipped)
    records = tuple(item for item in loaded if _selected(item, project, after, before))
    items = [_public_item(item, destination_project) for item in records]
    times = sorted(
        (item.occurred_at for item in records if item.occurred_at),
        key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
    )
    plan: dict[str, Any] = dict(
        source=source,
        source_id=source_id,
        destination_project=destination_project,
        target=target,
        count=len(items),
        skipped=sum(skipped.values()),
        projects=dict(Counter(item.project or "(unassigned)" for item in records)),
        time_range=dict(
            since=times[0] if times else None,
            until=times[-1] if times else None,
            unknown=sum(item.occurred_at is None for item in records),
        ),
        items=items,
    )
    # Content is hashed only after private removal and masking. Approval binds
    # the whole selection, including its source identity and original actors.
    plan["preview_id"] = digest(plan)
    plan["_records"] = records
    return plan


def apply_import(core, context, plan, *, preview_id: str | None, scope_id: str, progress=None):
    """Apply exactly the approved preview, releasing the writer after each occurrence.

    A refused upgrade or busy writer ends the pass. Repeating the same command
    safely resumes through capture's version identities; no attestation enters
    the ordinary host retry inbox.
    """
    public = {k: v for k, v in plan.items() if k not in {"preview_id", "_records"}}
    if not preview_id or preview_id != plan["preview_id"] or digest(public) != preview_id:
        raise ContractError("VERSION_CONFLICT", "import_preview")
    if [_public_item(item, plan.get("destination_project")) for item in plan["_records"]] != plan["items"]:
        raise ContractError("VERSION_CONFLICT", "import_preview")
    target = plan.get("target")
    if target is not None:
        _check_target_directory(core, target)
    if target is not None and (
        target["scope"] != scope_id or target["project_root"] != _local_path(context.project_root)
    ):
        raise ContractError("VERSION_CONFLICT", "import_target")
    if plan.get("destination_project"):
        context = replace(context, project_id=plan["destination_project"])
    counts = Counter()
    gaps, refusals = Counter(), Counter()
    started = time.perf_counter()
    error = None
    new_sources = frozenset()
    for index, item in enumerate(plan["_records"]):
        try:
            if index % IMPORT_BATCH_SIZE == 0:
                new_sources = prefetch_new_sources(
                    core,
                    context,
                    plan["_records"][index : index + IMPORT_BATCH_SIZE],
                    source=plan["source"],
                    source_id=plan["source_id"],
                    scope_id=scope_id,
                )
            receipt = import_record(
                core,
                context,
                item,
                source=plan["source"],
                source_id=plan["source_id"],
                scope_id=scope_id,
                _new_sources=new_sources,
            )
        except ContractError as exc:
            error = dict(code=exc.code, field=exc.field, at=index)
            break
        counts[receipt.disposition] += 1
        gaps.update(receipt.gaps)
        if receipt.error_code:
            refusals[receipt.error_code] += 1
        if receipt.durability != "persisted" and receipt.error_code not in {
            "PRIVATE_MODE",
            "empty_source",
            "plaintext_secret_rejected",
        }:
            error = dict(code=receipt.error_code or "STORAGE_UNAVAILABLE", at=index)
            break
        if progress is not None:
            progress(dict(processed=index + 1, total=plan["count"], counts=dict(counts)))
        # Large imports voluntarily relinquish the interpreter and their writer
        # lease. Hooks keep their own capture budgets and never join this pass.
        time.sleep(0)
    return dict(
        counts=dict(counts),
        gaps=dict(gaps),
        refusals=dict(refusals),
        processed=sum(counts.values()),
        total=plan["count"],
        seconds=round(time.perf_counter() - started, 4),
        error=error,
        retryable=error is not None
        and error["code"] in {"STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED", "SCHEMA_UNSUPPORTED", "VERSION_CONFLICT"},
        complete=error is None,
    )


def add_arguments(parser):
    """Attach import-specific flags to the shared memory command's store selection."""
    from .import_sources import SOURCE_READERS

    parser.add_argument("--source", choices=tuple(SOURCE_READERS), required=True)
    parser.add_argument("--from", dest="source_path", type=Path, required=True)
    parser.add_argument("--source-id", required=True, help="stable account or source namespace, reused on every import")
    parser.add_argument("--scope", help="destination scope; required when the store has several scopes")
    parser.add_argument(
        "--project-id", help="destination project for the selected records; original project labels remain visible"
    )
    parser.add_argument("--project", help="select one original project label")
    parser.add_argument("--project-root", help="explicit local project directory for private-mode matching")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--preview-id", help="exact preview approved by the owner")


def _local_path(value):
    return os.path.normcase(str(Path(value).resolve())) if value is not None else None


def _target(args, *, resolve_store=None):
    """Resolve an explicit store's configuration without opening its database."""
    root = _local_path(args.root)
    instance_root = _local_path(args.instance_root)
    directory = root
    if root is None and args.host and instance_root:
        if resolve_store is None:
            raise ContractError("INPUT_INVALID", "import_target")
        binding = resolve_store(host=args.host, instance_root=Path(instance_root), root=None)
        directory = _local_path(binding.data_directory)
    return dict(
        root=root,
        host=args.host,
        instance_root=instance_root,
        data_directory=directory,
        scope=args.scope,
        project_root=_local_path(args.project_root),
    )


def _check_target_directory(core, target):
    """Catch configuration redirects before initialization or entry registration."""
    if target["data_directory"] != _local_path(core.config.binding.data_directory):
        raise ContractError("VERSION_CONFLICT", "import_target")


def execute_import(args, *, open_store, resolve_store=None, run=apply_import):
    """Preview before binding any destination; only explicit approval opens a writable store."""
    from scope_recall.core.store_upgrade import upgrade_in_progress

    target = _target(args, resolve_store=resolve_store)
    plan = preview_import(
        args.source,
        args.source_path,
        source_id=args.source_id,
        project=args.project,
        since=args.since,
        until=args.until,
        destination_project=args.project_id,
        target=target,
    )
    public = {k: v for k, v in plan.items() if k != "_records"}
    if not args.yes:
        return dict(import_preview=public, applied=False)
    if not args.preview_id or args.preview_id != plan["preview_id"]:
        raise ContractError("VERSION_CONFLICT", "import_preview")
    if not target["scope"] or not (target["root"] or target["host"] and target["instance_root"]):
        raise ContractError("INPUT_INVALID", "import_target")
    args = type(args)(
        **{**vars(args), **{key: target[key] for key in ("root", "host", "instance_root", "scope", "project_root")}}
    )
    # Inspect the coordinator before writable initialization, which would wait
    # for a running upgrade. Reads and preview require no such wait.
    api = open_store(args, initialize=False)
    core = api.core
    _check_target_directory(core, target)
    if upgrade_in_progress(core.config.binding.data_directory):
        raise ContractError("SCHEMA_UNSUPPORTED", "upgrade_in_progress")
    core.storage.initialize(remaining_seconds=core.config.write_timeout_seconds)
    context = api._context()
    if context.binding.installation_kind == "shared":
        # Owner imports use the same explicit entry as local corrections. Source
        # principals still belong to the originating account, not this entry.
        with core.storage.write(context) as tx:
            tx.registry.register_entry("local-management", "Local management", "management", now=core.clock.utc_now())
        context = replace(context, entry_id="local-management")
    if args.project_root:
        context = replace(context, project_root=str(Path(args.project_root).resolve()))
    scope = args.scope
    if scope is None and len(context.allowed_scope_ids) == 1:
        scope = next(iter(context.allowed_scope_ids))
    if scope not in context.allowed_scope_ids:
        raise ContractError("ACCESS_DENIED", "import_scope")
    result = run(core, context, plan, preview_id=args.preview_id, scope_id=scope)
    return dict(import_preview=public, applied=True, result=result)
