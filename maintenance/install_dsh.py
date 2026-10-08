"""dsh (DeepSeek Harness) host: a native plugin and the MCP server, as rows of dsh's home patch, for a dsh attached to a
shared store.

dsh has no store of its own here: ``scope-recall attach --host dsh`` makes its home an entry first, and this installer
tells dsh to run, for that entry, the plugin ``distribution/dsh/scope-recall/index.mjs`` (recall before a turn's first
step and capture of each turn, through the hook client of ``adapters/clients``) and the MCP stdio server (the explicit
tools).  dsh's hooks cannot capture (no turn, no reply, a compressed session log), so the plugin is the way.

dsh composes every profile from patch layers; the home layer ``$DSH_HOME/cordis.patch.yml`` (``~/.dsh`` by default)
reaches them all and outranks the settings a profile's UI writes.  The target of this install is that home, which dsh
shares:
- the plugin file is the installer's own, at ``<home>/scope-recall/dsh-plugin/index.mjs`` (beside the patch that names
  it, in the receipt, removed by uninstall);
- the two rows go into ``cordis.patch.yml`` between markers, as one ``insert`` operation; every other line of the file
  is kept, and the file is copied into the install's backups before it changes;
- dsh uploads its session log to its model API by default, recalled memories with it.  The install writes that upload
  off (``session-log-deepseek``, ``enabled: false``) between markers of their own, which uninstall leaves in place:
  switching it on again would upload everything recorded while it was off.

The file is read with PyYAML (a dependency already) to refuse what this cannot edit safely: a file that is not a list,
a list not written as a block at column 0, rows of these ids inserted by something else, or another MCP server named
``scope-recall``.  dsh applies the operations in order, each key replacing the row's own.  So an operation that names
one of this install's rows without inserting it (``- id: scope-recall`` with ``disabled: true``) is the person's and
stays after the block, which a re-install writes where it stood and a fresh install before that operation; and the
upload counts as off only as the operations leave it, the install's own switch going after every other.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from scope_recall._version import __version__ as PACKAGE_VERSION

from . import install_client
from .install_client import (  # noqa: F401 -- install.py calls these on every host's module
    approve_local_platforms,
    config_path,
    data_dir,
    home_plugin_dir,
    instance_wrapper_files,
    purge_identity,
    unapproved_local_platforms,
    unapproved_owner_logins,
    validate_local_platforms,
    validate_owner_logins,
)
from .install_common import InstallError, InstallPlan, like_original, read_host_file

HOST = "dsh"
PATCH_FILENAME = "cordis.patch.yml"
PLUGIN_ROW = "scope-recall"
MCP_ROW = "mcp-scope-recall"
MCP_SERVER_NAME = "scope-recall"
MCP_CLIENT = "@deepseek-ai/dsh-mcp-client"
PRIVACY_ROW = "session-log-deepseek"
START = "# SCOPE_RECALL_DSH_START"
END = "# SCOPE_RECALL_DSH_END"
PRIVACY_START = "# SCOPE_RECALL_DSH_PRIVACY_START"
PRIVACY_END = "# SCOPE_RECALL_DSH_PRIVACY_END"
RESTART_NOTE = (
    "quit every running dsh (web, headless, tui, Desktop) before apply-install and start it again after; "
    "check with dsh --profile headless --dump-config that the rows scope-recall and mcp-scope-recall are "
    "there and session-log-deepseek has enabled: false"
)
_SERVER_MODULE = "scope_recall.adapters.codex.mcp_entry"
_MARKER = re.compile(re.escape(START) + r" \(scope-recall \S+ for (.+); apply-uninstall takes this block out\)$")
# dsh starts the plugin's hooks and the MCP server with an environment of its own (it scrubs the ambient one for an MCP
# server), so the install may hand them a credential file (``validate_options``).
_ENTRY = install_client.AttachedEntry(HOST, "dsh")
validate_options = _ENTRY.validate_options
foreign_instance_entries = _ENTRY.foreign_instance_entries
initialize_instance = _ENTRY.initialize_instance
installation_id = _ENTRY.installation_id
validate_reuse = _ENTRY.validate_reuse


def default_home() -> Path:
    """dsh's home as dsh finds it: ``DSH_HOME`` when set (a blank value is ignored), else ``~/.dsh``."""
    configured = os.environ.get("DSH_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".dsh"


def host_config_files(target_plugin_dir: Path) -> tuple[Path, ...]:
    """dsh's home patch, which this install adds its rows to."""
    return (target_plugin_dir / PATCH_FILENAME,)


def plugin_path(target_plugin_dir: Path) -> Path:
    return target_plugin_dir / "scope-recall" / "dsh-plugin" / "index.mjs"


def plugin_source() -> bytes:
    """The plugin as packaged (``distribution/dsh/scope-recall/index.mjs``)."""
    from scope_recall import distribution

    return (Path(distribution.__file__).resolve().parent / "dsh" / "scope-recall" / "index.mjs").read_bytes()


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    """The plugin file, the installer's own; the patch file is merged (``merged_file``)."""
    return {plugin_path(plan.target_plugin_dir): plugin_source()}


# -- the rows ---------------------------------------------------------------------------------------------------


def _scalar(value: str) -> str:
    """``value`` as a double-quoted YAML scalar (JSON's quoting is YAML's)."""
    return json.dumps(value, ensure_ascii=False)


def _rows(plan: InstallPlan) -> list[str]:
    """This entry's block: the plugin row and the MCP server row, as one ``insert``."""
    python = plan.python_executable.as_posix()
    home = plan.instance_root.as_posix()
    args = ["-I", "-B", "-m", _SERVER_MODULE, "--home", home, "--host", HOST]
    if plan.env_file is not None:
        args += ["--env-file", plan.env_file.as_posix()]
    lines = [
        f"{START} (scope-recall {PACKAGE_VERSION} for {home}; apply-uninstall takes this block out)",
        "- insert:",
        f"    - id: {PLUGIN_ROW}",
        f"      name: {_scalar(plugin_path(plan.target_plugin_dir).as_uri())}",
        "      config:",
        f"        python: {_scalar(python)}",
        f"        home: {_scalar(home)}",
        f"        version: {_scalar(PACKAGE_VERSION)}",
    ]
    if plan.env_file is not None:
        lines.append(f"        envFile: {_scalar(plan.env_file.as_posix())}")
    lines += [
        f"    - id: {MCP_ROW}",
        f"      name: {_scalar(MCP_CLIENT)}",
        "      config:",
        f"        serverName: {MCP_SERVER_NAME}",
        "        transport: stdio",
        f"        command: {_scalar(python)}",
        "        args: [" + ", ".join(_scalar(arg) for arg in args) + "]",
        END,
    ]
    return lines


def _privacy_rows() -> list[str]:
    return [
        f"{PRIVACY_START} (written by scope-recall: dsh would upload its session log, recalled memories with it, to "
        "its model API; uninstall leaves this in place)",
        f"- id: {PRIVACY_ROW}",
        "  config:",
        "    enabled: false",
        PRIVACY_END,
    ]


# -- reading and writing the patch ------------------------------------------------------------------------------


class _Loader(yaml.SafeLoader):
    """PyYAML's safe loader that reads dsh's ``!!js`` expressions as text instead of refusing the file."""


_Loader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: ("!!js", loader.construct_scalar(node)))


def read_patch(path: Path) -> tuple[str, bytes | None]:
    """The patch file's text (without a byte order mark) and its bytes; ("", None) when there is none yet."""
    raw = read_host_file(path)
    if raw is None:
        return "", None
    try:
        return raw.decode("utf-8-sig"), raw
    except UnicodeError as exc:
        raise InstallError(f"{path} is not UTF-8 text") from exc


def _lines(text: str) -> list[str]:
    """``text`` split at its line feeds alone, each line without its carriage return (``str.splitlines`` also splits at
    characters a quoted YAML value may hold, such as U+2028); a final line feed ends the last line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _parse(text: str, path: Path) -> list[Any]:
    try:
        value = yaml.load(text, Loader=_Loader) if text.strip() else []
    except yaml.YAMLError as exc:
        raise InstallError(f"{path} is not YAML dsh can read; fix it by hand first ({exc.__class__.__name__})") from exc
    if value is None:
        return []
    if not isinstance(value, list):
        raise InstallError(f"{path} does not hold a list of patch operations; fix it by hand first")
    return value


#: Where this entry's block stood, in the lines ``merged_file`` works on.
_PLACE = object()


def _without_block(
    lines: list, start: str, end: str, *, home: Path | None = None, mark: bool = False
) -> tuple[list, bool]:
    """``lines`` without the marked block (only this entry's when ``home`` is given), with ``_PLACE`` where it stood
    when ``mark``, and whether one was there."""
    result: list = []
    skipping = found = False
    for line in lines:
        if line is _PLACE:
            result.append(line)
            continue
        if not skipping and line.startswith(start) and (home is None or _names_home(line, home)):
            skipping = found = True
            if mark:
                result.append(_PLACE)
            continue
        if skipping:
            if line.startswith(end):
                skipping = False
            continue
        result.append(line)
    if skipping:
        raise InstallError(f"a {start} block has no {end} line; fix the patch file by hand")
    return result, found


def _names_home(marker: str, home: Path) -> bool:
    """Whether a block's start line (``_rows``) names ``home``, a path that may hold spaces or semicolons."""
    found = _MARKER.match(marker)
    return found is not None and (
        os.path.normcase(os.path.normpath(found.group(1))) == os.path.normcase(os.path.normpath(home.as_posix()))
    )


def _inserted(operations: list[Any]) -> list[dict[str, Any]]:
    """The rows the operations insert."""
    return [
        row
        for operation in operations
        if isinstance(operation, dict) and isinstance(operation.get("insert"), list)
        for row in operation["insert"]
        if isinstance(row, dict)
    ]


def _ids(operations: list[Any]) -> list[str]:
    """Every row id the operations insert."""
    return [str(row["id"]) for row in _inserted(operations) if "id" in row]


def _server_names(operations: list[Any]) -> list[str]:
    """The MCP server names the operations give: an inserted row's, or an operation's that replaces another row's config
    (one that replaces this install's MCP row's config keeps that row's server)."""
    rows = _inserted(operations) + [
        operation
        for operation in operations
        if isinstance(operation, dict) and "insert" not in operation and operation.get("id") != MCP_ROW
    ]
    return [
        row["config"]["serverName"]
        for row in rows
        if isinstance(row.get("config"), dict) and isinstance(row["config"].get("serverName"), str)
    ]


def upload_off(operations: list[Any]) -> bool:
    """Whether the patch leaves dsh's session-log upload switched off, as dsh applies it: its operations in order, each
    key replacing the row's own, so the last ``disabled`` and the last ``config`` decide (a ``config`` without
    ``enabled: false`` switches the upload on again, ``enabled`` defaulting to true)."""
    disabled: Any = None
    enabled: Any = None
    for operation in operations:
        rows = (
            operation["insert"]
            if isinstance(operation, dict) and isinstance(operation.get("insert"), list)
            else [operation]
        )
        for row in rows:
            if not isinstance(row, dict) or row.get("id") != PRIVACY_ROW:
                continue
            if "disabled" in row:
                disabled = row["disabled"]
            if "config" in row:
                config = row["config"]
                enabled = config.get("enabled", True) if isinstance(config, dict) else True
    return disabled is True or enabled is False


def _first_naming(lines: list[str]) -> int | None:
    """The line of the first operation that names one of this install's rows without inserting it (``- id:
    scope-recall`` with ``disabled: true``): a block written afresh goes before it, so that it still applies."""
    try:
        node = yaml.compose("\n".join(lines), Loader=_Loader)
    except yaml.YAMLError:
        return None
    for item in node.value if isinstance(node, yaml.SequenceNode) else ():
        if not isinstance(item, yaml.MappingNode):
            continue
        keys = {key.value: value for key, value in item.value if isinstance(key, yaml.ScalarNode)}
        target = keys.get("id")
        if "insert" not in keys and isinstance(target, yaml.ScalarNode) and target.value in (PLUGIN_ROW, MCP_ROW):
            return item.start_mark.line
    return None


def _block_style(lines: list[str], path: Path) -> None:
    """Refuse a list this cannot append to: a flow list (``[a, b]``) or one indented from column 0."""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _empty_list(line):
            continue
        if not line.startswith("- "):
            raise InstallError(
                f"{path} is not a block list at column 0; add this entry's rows by hand "
                f"(see docs/install.md, section 13)"
            )
        return


def _encode(lines: list[str], original: bytes | None) -> bytes:
    return like_original("\n".join(lines).rstrip("\n") + "\n", original)


def _empty_list(line: str) -> bool:
    """Whether ``line`` is the file's own empty list, ``[]`` at column 0 (an indented one is a value)."""
    return line.rstrip() == "[]"


def _composed(lines: list[str]) -> list[str]:
    """``lines`` as a list dsh boots from: an empty list's ``[]`` dropped when rows follow, kept (or added) when
    nothing else is left, since a file of comments alone fails dsh's boot."""
    content = [line for line in lines if line.strip() and not line.strip().startswith("#")]
    kept = [line for line in lines if not _empty_list(line)]
    return kept if any(not _empty_list(line) for line in content) else [*kept, "[]"]


def merged_file(plan: InstallPlan, path: Path) -> bytes | None:
    """The patch file with this entry's rows (and the upload switched off, when nothing switches it off yet), or None
    when it already holds them as they would be written."""
    text, raw = read_patch(path)
    marked, _found = _without_block(_lines(text), START, END, home=plan.instance_root, mark=True)
    rest = [line for line in marked if line is not _PLACE]
    others = _parse("\n".join(rest), path)
    if any(line.startswith(START) for line in rest):
        raise InstallError(f"{path} already holds the rows of another Scope Recall entry: uninstall it first")
    for row in (PLUGIN_ROW, MCP_ROW):
        if row in _ids(others):
            raise InstallError(f"{path} already has a row {row} that is not this entry's: take it out first")
    if MCP_SERVER_NAME in _server_names(others):
        raise InstallError(f"{path} already has an MCP server named {MCP_SERVER_NAME}: take it out first")
    _block_style(rest, path)
    rows = _rows(plan)
    privacy: list[str] = []
    if not upload_off(others):
        # Last, after every operation that names the row: dsh applies them in order.
        marked, _ = _without_block(marked, PRIVACY_START, PRIVACY_END)
        privacy = _privacy_rows()
    if _PLACE in marked:
        # Where the block stood, so that an operation of the person's after it stays after it.
        at = marked.index(_PLACE)
        marked = [line for line in marked if line is not _PLACE]
        merged = [*marked[:at], *rows, *marked[at:], *privacy]
    elif (naming := _first_naming(marked)) is not None:
        merged = [*marked[:naming], *rows, *marked[naming:], *privacy]
    else:
        merged = [*marked, *privacy, *rows]
    merged = _composed(merged)
    composed = _parse("\n".join(merged), path)
    if PLUGIN_ROW not in _ids(composed) or not upload_off(composed):
        raise InstallError(f"{path}: the rows written would not read back; add them by hand")
    data = _encode(merged, raw)
    return None if raw is not None and data == raw else data


def unmerged_file(instance_root: Path, path: Path) -> bytes | None:
    """The patch file without this entry's rows, or None when it holds none.  The upload stays off."""
    text, raw = read_patch(path)
    if raw is None:
        return None
    rest, found = _without_block(_lines(text), START, END, home=instance_root)
    if not found:
        return None
    return _encode(_composed(rest), raw)
