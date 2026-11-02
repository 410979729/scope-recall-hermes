"""Human-facing memory commands; all policy lives in the management API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scope_recall.contracts import ContractError, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.management import MemoryManagement
from scope_recall.core.management_query import MemoryFilter
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.storage import SQLiteStorage

from .restore import RestoreError, store_to_restore

FORMAT = "scope-recall-management/1"
_FILTERS = ("kind", "scope", "agent", "session", "since", "until")


def open_management(args, *, initialize: bool = True):
    """Bind the explicit local installation without registering an agent entry.

    ``args`` needs only the store selection (``root``, ``host``, ``instance_root``), which every caller's parser
    defines. ``initialize`` is false for a read-only listing, which must neither migrate nor write the store.
    """
    binding = store_to_restore(
        host=args.host,
        instance_root=Path(args.instance_root).resolve() if args.instance_root else None,
        root=Path(args.root).resolve() if args.root else None,
    )
    if not (binding.data_directory / "memory.sqlite3").is_file():
        raise ContractError("SOURCE_MISSING", "database")
    core = MemoryCore(CoreConfig(binding), storage=SQLiteStorage(binding, upgrade_on_open=initialize))
    if initialize:
        core.initialize()
    context = TrustedContext(binding, "local-management", binding.scope_ids, "host_generated")
    return MemoryManagement(core, InstallationMaintenance(context))


def _common(parser):
    parser.add_argument("--root", help="shared store root")
    parser.add_argument("--host", choices=("hermes", "codex", "claude-code", "workbuddy", "dsh"))
    parser.add_argument("--instance-root", help="host installation home for a store of its own")
    parser.add_argument("--json", action="store_true", help="emit the versioned JSON contract")


def _filters(parser):
    for name in _FILTERS:
        parser.add_argument("--" + name)


def _instruction_arguments(command, action):
    """Expose scope instruction fields only on management commands."""
    if action in {"instruction-get", "instruction-set"}:
        command.add_argument("--scope", required=True)
    if action == "instruction-set":
        command.add_argument("--text", required=True, help="instruction text, at most 2000 characters")


def parser():
    """Build command arguments without opening a database or calling a provider."""
    root = argparse.ArgumentParser(prog="scope-recall memory", description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    for action in (
        "list",
        "search",
        "show",
        "history",
        "revise",
        "importance",
        "mute",
        "unmute",
        "forget",
        "forget-session",
        "export",
        "secrets",
        "recalls",
        "import",
        "instruction-get",
        "instruction-set",
    ):
        command = commands.add_parser(action)
        _common(command)
        _instruction_arguments(command, action)
        if action == "recalls":
            command.add_argument("--session")
            command.add_argument("--request-id")
            command.add_argument("--before", type=int)
        if action == "import":
            from .memory_import import add_arguments

            add_arguments(command)
        if action in {"list", "search", "forget-session", "export"}:
            _filters(command)
        if action in {"list", "search", "history", "recalls"}:
            command.add_argument("--limit", type=int, default=30)
        if action in {"list", "search"}:
            command.add_argument("--cursor")
        if action == "search":
            command.add_argument("query", help="literal substring, without a model or network request")
        if action in {"show", "history", "revise", "importance"}:
            command.add_argument("ref")
        if action == "importance":
            command.add_argument("--expected-revision", type=int, required=True)
            command.add_argument("--score", type=int, choices=range(1, 6))
            pin = command.add_mutually_exclusive_group()
            pin.add_argument("--pin", dest="pinned", action="store_true", default=None)
            pin.add_argument("--unpin", dest="pinned", action="store_false")
        if action in {"show", "history"}:
            command.add_argument("--revision" if action == "show" else "--before", type=int)
        if action == "revise":
            command.add_argument("--expected-revision", type=int, required=True)
            value = command.add_mutually_exclusive_group(required=True)
            value.add_argument("--value")
            value.add_argument("--retract", action="store_true")
        if action in {"mute", "unmute", "forget", "forget-session"}:
            command.add_argument("--yes", action="store_true", help="apply the displayed dependency preview")
            command.add_argument("--preview-id", help="refuse if an earlier preview has changed")
        if action in {"mute", "unmute", "forget"}:
            command.add_argument("refs", nargs="+")
            command.add_argument("--expected-revision", type=int, help="expected head for a single selected object")
        if action == "export":
            command.add_argument("--out", required=True, help="new directory, or a new file ending in .zip")
            command.add_argument("--with-attachments", action="store_true", help="include retained attachments")
    return root


def _preview(api, args):
    if args.action == "forget-session":
        plan = api.forget_session(MemoryFilter(**{key: getattr(args, key) for key in _FILTERS}))
    else:
        if args.expected_revision is not None and len(args.refs) != 1:
            raise ContractError("INPUT_INVALID", "single_expected_revision")
        expected = {args.refs[0]: args.expected_revision} if args.expected_revision is not None else None
        mode = {"forget": "delete", "mute": "suppress", "unmute": "unmute"}[args.action]
        plan = api.preview(args.refs, mode=mode, expected=expected)
    if args.preview_id is not None and args.preview_id != plan["preview_id"]:
        raise ContractError("VERSION_CONFLICT", "preview")
    if not args.yes:
        return dict(preview=plan, applied=False)
    return dict(preview=plan, applied=True, result=api.apply(plan))


def _inventory(api, args):
    """Translate bounded inventory reads, including history and diagnostic metadata."""
    if args.action == "recalls":
        return api.recalls(session=args.session, request_id=args.request_id, before=args.before, limit=args.limit)
    if args.action == "secrets":
        return api.secrets()
    filters = MemoryFilter(**{key: getattr(args, key) for key in _FILTERS}, query=getattr(args, "query", ""))
    return api.list(filters, limit=args.limit, cursor=args.cursor)


def execute(api, args):
    """Translate one parsed command to the same methods used by the local UI."""
    if args.action in {"instruction-get", "instruction-set"}:
        return (
            {"instructions": api.memory_instructions(args.scope)}
            if args.action == "instruction-get"
            else api.set_memory_instruction(args.scope, args.text)
        )
    if args.action in {"recalls", "secrets", "list", "search"}:
        return _inventory(api, args)
    if args.action == "importance":
        return api.importance(
            args.ref, expected_revision=args.expected_revision, importance=args.score, pinned=args.pinned
        )
    if args.action == "show":
        return api.show(args.ref, revision=args.revision)
    if args.action == "history":
        return api.history(args.ref, before=args.before, limit=args.limit)
    if args.action == "revise":
        return api.revise(
            args.ref, expected_revision=args.expected_revision, value=None if args.retract else args.value
        )
    if args.action == "export":
        from .memory_export import write_export

        filters = MemoryFilter(**{key: getattr(args, key) for key in _FILTERS})
        with api.export_reader(filters) as reader:
            return write_export(reader, args.out, with_attachments=args.with_attachments)
    return _preview(api, args)


def table(rows, columns, *, max_width: int | None = 72):
    """Print a plain-text table, optionally retaining every metadata character."""
    values = [[str(row.get(key, "")) for key in columns] for row in rows]
    widths = [max([len(key), *(len(row[i]) for row in values)]) for i, key in enumerate(columns)]
    if max_width is not None:
        widths = [min(max_width, width) for width in widths]
    print("  ".join(key.upper().ljust(widths[i]) for i, key in enumerate(columns)))
    for row in values:
        print("  ".join(value.replace("\n", " ")[: widths[i]].ljust(widths[i]) for i, value in enumerate(row)))


def _render_scan(data):
    """Distinguish an exhausted search from a bounded call with work remaining."""
    if "examined" not in data:
        return
    count = data["examined"]
    if not data["items"]:
        if data["scan_complete"]:
            print(f"None of the {count} objects examined matched. Scan complete.")
        else:
            print(f"No object matched among the {count} examined; more remain.")
    else:
        print(f"Examined {count} objects. " + ("Scan complete." if data["scan_complete"] else "More remain."))
    if data["stop_reason"] == "time_budget":
        print("Scan stopped early at the time budget.")
    elif data["stop_reason"] == "limit":
        print("Scan stopped at the match limit.")
    if data.get("next_cursor"):
        print("Repeat the same command with --cursor " + data["next_cursor"])


def _render_items(data):
    """Render inventory and history rows with their respective continuation."""
    rows = [
        dict(
            kind=item.get("kind", ""),
            ref=item.get("ref", ""),
            revision=item.get("revision", ""),
            content=item.get("content", json.dumps(item.get("value", {}), ensure_ascii=False)),
        )
        for item in data["items"]
    ]
    table(rows, ("kind", "ref", "revision", "content"))
    _render_scan(data)
    if data.get("next_before") is not None:
        print("Next history page: --before " + str(data["next_before"]))


def render(payload, *, as_json):
    """Render readable output by default, keeping JSON output independent of locale."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if payload["status"] == "error":
        print(f"{payload['code']}: {payload.get('field', '')}")
        return
    data = payload["data"]
    if "import_preview" in data:
        plan = data["import_preview"]
        table(
            plan["items"],
            ("key", "project", "write_project", "occurred_at", "role", "original_origin", "review_required"),
        )
        print(
            f"Source: {plan['source']}; records: {plan['count']}; skipped: {plan['skipped']}"
            f"; time: {plan['time_range']}"
        )
        print(f"Projects: {plan['projects']}; preview: {plan['preview_id']}")
        print("Target: " + json.dumps(plan["target"], ensure_ascii=False))
        if not data["applied"]:
            print("Review this selection, then repeat with --yes --preview-id " + plan["preview_id"])
            print("Import permission does not confirm the content or identify its author as the local owner.")
        else:
            print(json.dumps(data["result"], ensure_ascii=False, indent=2))
    elif "preview" in data:
        plan = data["preview"]
        table(plan["affected"], ("kind", "ref", "revision", "scope_id"))
        entries = sorted({s["entry_id"] for s in plan["sources"]})
        print(f"Sources: {len(plan['sources'])}; entries: {', '.join(entries)}; preview: {plan['preview_id']}")
        if not data["applied"]:
            print("Review this selection, then repeat with --yes and --preview-id to require the same preview.")
        else:
            print(json.dumps(data["result"], ensure_ascii=False, indent=2))
    elif "counts" in data:
        counts = data["counts"]
        print(
            f"Exported {counts['sources']} sources, {counts['facts']} facts, "
            f"{counts['fact_history']} historical versions, {counts['episodes']} episodes and "
            f"{counts['attachments']} attachments to {data['out']}."
        )
    elif "rows_read" in data:
        table(
            data["items"], ("ref", "revision", "kind", "origin", "entry", "session", "time", "patterns"), max_width=None
        )
        print(
            f"Read {data['rows_read']} rows; scanned {data['candidates_scanned']} candidates; "
            f"found {len(data['items'])} secret-bearing versions."
        )
    elif "items" in data:
        _render_items(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv=None):
    """Run a local owner command; refused inputs or stale selections exit with code 2."""
    args = parser().parse_args(argv)
    data: dict
    try:
        if args.action == "import":
            from .import_progress import run_import
            from .memory_import import execute_import

            data = execute_import(args, open_store=open_management, resolve_store=store_to_restore, run=run_import)
        else:
            api = open_management(args, initialize=args.action not in {"secrets", "recalls"})
            data = execute(api, args)
        payload = dict(format=FORMAT, action=args.action, status="ok", data=data)
        code = 2 if data.get("result", {}).get("error") else 0
    except ContractError as exc:
        payload = dict(format=FORMAT, action=args.action, status="error", code=exc.code, field=exc.field)
        code = 2
    except (RestoreError, OSError, ValueError) as exc:
        payload = dict(
            format=FORMAT,
            action=args.action,
            status="error",
            code=getattr(exc, "code", "CONFIG_INVALID"),
            field=getattr(exc, "hint", "store"),
        )
        code = 2
    render(payload, as_json=args.json)
    return code
