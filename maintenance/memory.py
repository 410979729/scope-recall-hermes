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

from .restore import RestoreError, store_to_restore

FORMAT = "scope-recall-management/1"
_FILTERS = ("kind", "scope", "agent", "session", "since", "until")


def open_management(args):
    """Bind the explicit local installation without registering an agent entry."""
    binding = store_to_restore(
        host=args.host,
        instance_root=Path(args.instance_root).resolve() if args.instance_root else None,
        root=Path(args.root).resolve() if args.root else None,
    )
    if not (binding.data_directory / "memory.sqlite3").is_file():
        raise ContractError("SOURCE_MISSING", "database")
    core = MemoryCore(CoreConfig(binding))
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
        "mute",
        "unmute",
        "forget",
        "forget-session",
        "export",
    ):
        command = commands.add_parser(action)
        _common(command)
        if action in {"list", "search", "forget-session", "export"}:
            _filters(command)
        if action in {"list", "search", "history"}:
            command.add_argument("--limit", type=int, default=30)
        if action in {"list", "search"}:
            command.add_argument("--cursor")
        if action == "search":
            command.add_argument("query", help="literal substring, without a model or network request")
        if action in {"show", "history", "revise"}:
            command.add_argument("ref")
        if action == "show":
            command.add_argument("--revision", type=int)
        if action == "history":
            command.add_argument("--before", type=int)
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


def execute(api, args):
    """Translate one parsed command to the same methods used by the local UI."""
    if args.action in {"list", "search"}:
        filters = MemoryFilter(**{key: getattr(args, key) for key in _FILTERS}, query=getattr(args, "query", ""))
        return api.list(filters, limit=args.limit, cursor=args.cursor)
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


def _table(rows, columns):
    values = [[str(row.get(key, "")) for key in columns] for row in rows]
    widths = [min(72, max([len(key), *(len(row[i]) for row in values)])) for i, key in enumerate(columns)]
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
    _table(rows, ("kind", "ref", "revision", "content"))
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
    if "preview" in data:
        plan = data["preview"]
        _table(plan["affected"], ("kind", "ref", "revision", "scope_id"))
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
    elif "items" in data:
        _render_items(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv=None):
    """Run a local owner command; refused inputs or stale selections exit with code 2."""
    args = parser().parse_args(argv)
    try:
        payload = dict(format=FORMAT, action=args.action, status="ok", data=execute(open_management(args), args))
        code = 0
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
