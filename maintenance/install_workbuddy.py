"""WorkBuddy host: hooks merged into WorkBuddy's own settings.json and the MCP stdio server into its mcp.json, for a
WorkBuddy attached to a shared store.

WorkBuddy has no store of its own here: ``scope-recall attach --host workbuddy`` makes its home an entry first, and
this installer only tells WorkBuddy to run the hook client of ``adapters/clients`` for it.  WorkBuddy's agent reads
command hooks from ``hooks`` in ``settings.json`` in its own home (``~/.workbuddy``).  It reads no MCP server of its
own: the desktop app starts it with ``--strict-mcp-config`` and only its connector proxy, which serves the user's
servers listed in ``mcp.json`` there, each once the user has approved it in WorkBuddy.  (``.mcp.json`` beside it is
the app's own record of that proxy, and nothing reads another server from it.)  So the target of this install is that
home, which WorkBuddy shares: no file there is the installer's own.  The install adds or updates its own entries in
those two files, keeps every other key, hook and server, and copies a file into the install's backups before changing
it; uninstall takes out its own entries only.

On Windows WorkBuddy runs a hook command through Git Bash (``bash -c``; elsewhere through ``$SHELL -c``), so every
path in the command is a double-quoted forward-slash path.  A hook's ``timeout`` is in seconds, and a prompt hook that
runs past it blocks the prompt.  The remote client (``adapters/clients/remote_client.py``) merges its own hooks and
server into the same files with the functions here.
"""

from __future__ import annotations

import codecs
import copy
import json
import os
import shlex
from pathlib import Path
from typing import Any, Callable, Mapping

from scope_recall.adapters.clients.config import CodexConfigError, load_shared_client
from scope_recall.adapters.hermes.installation import attachment_path

from .install_common import RUNTIME_CONFIG_LIMIT, InstallError, InstallPlan, reject_symlink_chain, require_file

HOST = "workbuddy"
SETTINGS_FILENAME = "settings.json"
#: The user's own MCP servers (WorkBuddy's ``customMcpConfigPath``), not ``.mcp.json``.
MCP_FILENAME = "mcp.json"
SERVER_NAME = "scope-recall"
#: What WorkBuddy shows beside the server in its MCP settings, where it waits to be approved.
SERVER_DESCRIPTION = "Scope Recall: the memory this machine's agents share"
#: The most a hook's own work takes once its interpreter has started: the entry's ``hook_processing_seconds``, at most
#: 6 s (``runtime/instance.py``).  A Stop's record read is bounded inside it.
HOOK_WORK_SECONDS = 6
#: The least each wait leaves for Git Bash and the interpreter to start before that work begins; both take about 0.6 s
#: warm on the pilot machine, more on a cold start.
START_SECONDS = 4
#: The events recorded, and how many seconds WorkBuddy waits for each (60 unless a hook says otherwise).  WorkBuddy
#: blocks a prompt whose hook runs past its wait, so each wait covers the start and the work: the claude-code host's.
HOOK_TIMEOUTS = {"UserPromptSubmit": 15, "Stop": 10, "SessionEnd": 10}
#: What the plan says once, whatever changed: a running WorkBuddy may write settings.json itself, and it starts a new
#: MCP server only once the user approves it.
RESTART_NOTE = (
    "quit WorkBuddy before apply-install and start it again after; then approve the MCP server "
    "scope-recall in WorkBuddy's MCP settings, where it waits for approval"
)
#: What ends every hook command.  WorkBuddy blocks the prompt when its hook exits 2 (and a Stop hook's 2 asks the
#: model to go on), which is argparse's code when the package predates an option the command names, as after a
#: rollback: any failure is shown as 1 instead, which WorkBuddy reports and lets the prompt through.
FAIL_OPEN = " || exit 1"
_HOOK_MODULE = "scope_recall.adapters.codex.hook_entry"
_REMOTE_MODULE = "scope_recall.adapters.codex.remote_client"
_SERVER_MODULE = "scope_recall.adapters.codex.mcp_entry"
#: Characters a double-quoted word of a Git Bash command does not take literally.
_NOT_LITERAL = frozenset('"$`\\')


def default_home() -> Path:
    """WorkBuddy's home as WorkBuddy finds it: ``WORKBUDDY_CONFIG_DIR`` when set, else ``~/.workbuddy``."""
    configured = os.environ.get("WORKBUDDY_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".workbuddy"


def data_dir(instance_root: Path) -> Path:
    return attachment_path(instance_root).parent


def config_path(instance_root: Path) -> Path:
    return attachment_path(instance_root)


def instance_wrapper_files(instance_root: Path) -> tuple[Path, ...]:
    return ()


def home_plugin_dir(instance_root: Path) -> None:
    return None


def host_config_files(target_plugin_dir: Path) -> tuple[Path, ...]:
    """WorkBuddy's own files in its home (the install's target) that this install merges its entries into."""
    return (target_plugin_dir / SETTINGS_FILENAME, target_plugin_dir / MCP_FILENAME)


def validate_options(agent_workspace: str | None, env_file: Path | str | None) -> tuple[str, Path | None]:
    """WorkBuddy starts hooks and the MCP server with its own environment, so the installer may hand them a
    credential file, as the other clients' installers do."""
    if agent_workspace is not None and str(agent_workspace).strip():
        raise InstallError("agent_workspace is not used for WorkBuddy installation")
    if env_file is None or str(env_file).strip() == "":
        return "", None
    return "", require_file(Path(env_file), "env_file")


def validate_local_platforms(values: object) -> tuple[str, ...]:
    if values:
        raise InstallError("local_platform is only used for Hermes installation")
    return ()


def validate_owner_logins(values: object) -> tuple[str, ...]:
    if values:
        raise InstallError("owner_login is only used for Hermes installation")
    return ()


def unapproved_owner_logins(plan: InstallPlan) -> tuple[tuple[str, str], ...]:
    return ()


def unapproved_local_platforms(plan: InstallPlan) -> tuple[str, ...]:
    return ()


def approve_local_platforms(plan: InstallPlan) -> None:
    return None


def planned_files(plan: InstallPlan) -> dict[Path, str | bytes]:
    """No file is this installer's own: everything it writes is merged into WorkBuddy's files."""
    return {}


# -- the hook command and the server ----------------------------------------------------------------------------


def quoted(path: Path, what: str) -> str:
    """``path`` as one word of a Git Bash command: forward slashes, in double quotes.

    Refused when a character of it is not printable ASCII or is one a double-quoted word does not take literally:
    the command would then run something else, or nothing, and WorkBuddy would carry on without its hook.
    """
    text = path.as_posix()
    if any(char in _NOT_LITERAL or not " " <= char <= "~" for char in text):
        raise InstallError(
            f"WorkBuddy runs a hook through Git Bash: keep the {what} on a path of printable ASCII "
            f'without ", $, ` or \\ (not {text!r})'
        )
    return f'"{text}"'


def hook_command(plan: InstallPlan) -> str:
    command = (
        f"{quoted(plan.python_executable, 'interpreter')} -I -B -m {_HOOK_MODULE} "
        f"--home {quoted(plan.instance_root, 'home')} --host {HOST}"
    )
    if plan.env_file is not None:
        command += f" --env-file {quoted(plan.env_file, 'env file')}"
    return command + FAIL_OPEN


def _server(plan: InstallPlan) -> dict[str, Any]:
    args = ["-I", "-B", "-m", _SERVER_MODULE, "--home", plan.instance_root.as_posix(), "--host", HOST]
    if plan.env_file is not None:
        args += ["--env-file", plan.env_file.as_posix()]
    return {
        "type": "stdio",
        "command": plan.python_executable.as_posix(),
        "args": args,
        "description": SERVER_DESCRIPTION,
    }


def words(command: object) -> list[str]:
    """A hook command split as the shell splits it; nothing when it is no command this can read."""
    if type(command) is not str:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def option(parts: list[str], name: str) -> str | None:
    """The word after ``name``, when there is one."""
    try:
        return parts[parts.index(name) + 1]
    except (ValueError, IndexError):
        return None


def same_path(value: object, path: Path) -> bool:
    return type(value) is str and os.path.normcase(os.path.normpath(value)) == os.path.normcase(os.path.normpath(path))


def _this_entry(instance_root: Path) -> Callable[[list[str]], bool]:
    """Recognises the hook this install writes for ``instance_root``, whatever interpreter or env file it names."""
    return lambda parts: (
        _HOOK_MODULE in parts and option(parts, "--host") == HOST and same_path(option(parts, "--home"), instance_root)
    )


def _this_server(instance_root: Path) -> Callable[[object], bool]:
    def recognise(server: object) -> bool:
        args = server.get("args") if isinstance(server, dict) else None
        if not isinstance(args, list) or not all(type(arg) is str for arg in args):
            return False
        return (
            _SERVER_MODULE in args
            and option(args, "--host") == HOST
            and same_path(option(args, "--home"), instance_root)
        )

    return recognise


# -- merging into WorkBuddy's files -----------------------------------------------------------------------------


def read_config(path: Path) -> tuple[dict[str, Any], bytes | None]:
    """One of WorkBuddy's JSON files as an object, and the bytes it holds (None: there is no such file yet)."""
    reject_symlink_chain(path)
    if not path.exists():
        return {}, None
    if not path.is_file() or path.stat().st_size > RUNTIME_CONFIG_LIMIT:
        raise InstallError(f"{path} is not a file of at most {RUNTIME_CONFIG_LIMIT} bytes")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except (UnicodeError, ValueError) as exc:
        # WorkBuddy reads comments in these files; written back as JSON they would be lost.
        raise InstallError(
            f"{path} is not plain JSON: add this entry's hooks and server by hand, or take the comments out"
        ) from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path} does not hold a JSON object")
    return value, raw


def encode_config(value: dict[str, Any], original: bytes | None) -> bytes:
    """``value`` as WorkBuddy writes its JSON (two spaces, keys in their order), in the original's line endings, with
    its final newline or none, and its byte order mark if it had one."""
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if original is None or original.rstrip(b" \t").endswith(b"\n"):
        text += "\n"
    if original is not None and b"\r\n" in original:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    return codecs.BOM_UTF8 + data if original is not None and original.startswith(codecs.BOM_UTF8) else data


def _is(entry: object, recognise: Callable[[list[str]], bool]) -> bool:
    return (
        isinstance(entry, dict) and entry.get("type", "command") == "command" and recognise(words(entry.get("command")))
    )


def _scope_recall(entry: object) -> bool:
    """A command hook that runs one of this package's hook clients, for any entry."""
    parts = words(entry.get("command")) if isinstance(entry, dict) else []
    return _HOOK_MODULE in parts or _REMOTE_MODULE in parts


def _groups(groups: list[Any], entry: dict[str, Any] | None, recognise: Callable[[list[str]], bool]) -> list[Any]:
    """One event's matcher groups with the hooks ``recognise`` names replaced: the first by ``entry``, where it stands,
    any other taken out.  A group left empty by that goes; ``entry`` with no place is appended in a group of its own."""
    placed = entry is None
    result = []
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list) or not any(_is(hook, recognise) for hook in hooks):
            result.append(group)
            continue
        kept = []
        for hook in hooks:
            if not _is(hook, recognise):
                kept.append(hook)
            elif not placed:
                kept.append(entry)
                placed = True
        if kept:
            result.append({**group, "hooks": kept})
    if not placed:
        result.append({"hooks": [entry]})
    return result


def _every_hook(hooks: dict[str, Any]):
    """Each hook of every event's matcher groups, with its event; whatever is not a group or a hook is passed over."""
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else ():
            entries = group.get("hooks") if isinstance(group, dict) else None
            for hook in entries if isinstance(entries, list) else ():
                if isinstance(hook, dict):
                    yield event, hook


def _event_hooks(settings: dict[str, Any]) -> dict[str, Any] | None:
    hooks = settings.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise InstallError(f"WorkBuddy's {SETTINGS_FILENAME} has hooks that are not an object: fix them by hand")
    return hooks


def with_hooks(
    settings: dict[str, Any], command: str, timeouts: Mapping[str, int], recognise: Callable[[list[str]], bool]
) -> dict[str, Any]:
    """``settings`` with one command hook running ``command`` for each event of ``timeouts``.

    A hook ``recognise`` names is this install's: updated where it stands, any further copy taken out, and taken out
    of any other event.  Every other key, hook and group stays as it is.  Another Scope Recall hook (another entry's,
    or a remote client's) is refused: WorkBuddy would run both, and each would record the turn.
    """
    hooks = _event_hooks(settings) or {}
    for event, hook in _every_hook(hooks):
        if _scope_recall(hook) and not _is(hook, recognise):
            raise InstallError(
                f"WorkBuddy's {SETTINGS_FILENAME} already runs another Scope Recall hook for {event} "
                f"({hook.get('command')}): uninstall it first"
            )
    merged: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            if event in timeouts:
                raise InstallError(
                    f"WorkBuddy's {SETTINGS_FILENAME} has {event} hooks that are not a list: fix them by hand"
                )
            merged[event] = groups
            continue
        entry = {"type": "command", "command": command, "timeout": timeouts[event]} if event in timeouts else None
        kept = _groups(groups, entry, recognise)
        if kept or not groups:
            merged[event] = kept
    for event, timeout in timeouts.items():
        if event not in merged:
            merged[event] = [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
    return copy.deepcopy({**settings, "hooks": merged})


def without_hooks(settings: dict[str, Any], recognise: Callable[[list[str]], bool]) -> dict[str, Any]:
    """``settings`` with the hooks ``recognise`` names taken out, and any group, event or hooks object that left empty;
    everything else as it is."""
    hooks = _event_hooks(settings)
    if not hooks:
        return settings
    kept_events = {}
    for event, groups in hooks.items():
        kept = _groups(groups, None, recognise) if isinstance(groups, list) else groups
        if kept or not groups:
            kept_events[event] = kept
    result = copy.deepcopy(settings)
    if kept_events:
        result["hooks"] = kept_events
    else:
        del result["hooks"]
    return result


def _servers(config: dict[str, Any]) -> dict[str, Any] | None:
    servers = config.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise InstallError(f"WorkBuddy's {MCP_FILENAME} has mcpServers that are not an object: fix them by hand")
    return servers


def with_server(config: dict[str, Any], server: dict[str, Any], recognise: Callable[[object], bool]) -> dict[str, Any]:
    """``config`` with ``server`` as the MCP server ``scope-recall``, every other server and key as it is.  A server of
    that name ``recognise`` does not take for this install's is refused."""
    servers = _servers(config) or {}
    present = servers.get(SERVER_NAME)
    if present is not None and not recognise(present):
        raise InstallError(
            f"WorkBuddy's {MCP_FILENAME} already has an MCP server named {SERVER_NAME} that is not "
            "this entry's: take it out first"
        )
    return {**copy.deepcopy(config), "mcpServers": {**copy.deepcopy(servers), SERVER_NAME: server}}


def without_server(config: dict[str, Any], recognise: Callable[[object], bool]) -> dict[str, Any]:
    servers = _servers(config)
    if not servers or SERVER_NAME not in servers or not recognise(servers[SERVER_NAME]):
        return config
    result = copy.deepcopy(config)
    del result["mcpServers"][SERVER_NAME]
    return result


def merged_file(plan: InstallPlan, path: Path) -> bytes | None:
    """``path`` with this entry's hooks or MCP server merged in, or None when it holds them as they would be written."""
    value, raw = read_config(path)
    if path.name == SETTINGS_FILENAME:
        merged = with_hooks(value, hook_command(plan), HOOK_TIMEOUTS, _this_entry(plan.instance_root))
    else:
        merged = with_server(value, _server(plan), _this_server(plan.instance_root))
    return None if raw is not None and merged == value else encode_config(merged, raw)


def unmerged_file(instance_root: Path, path: Path) -> bytes | None:
    """``path`` without the hooks or MCP server installed for ``instance_root``, or None when it holds none of them."""
    value, raw = read_config(path)
    if raw is None:
        return None
    if path.name == SETTINGS_FILENAME:
        stripped = without_hooks(value, _this_entry(instance_root))
    else:
        stripped = without_server(value, _this_server(instance_root))
    return None if stripped == value else encode_config(stripped, raw)


# -- the entry --------------------------------------------------------------------------------------------------


def foreign_instance_entries(instance_root: Path) -> list[str]:
    """A home this installer is asked to create: WorkBuddy's is only ever an attached one."""
    return [f"{instance_root} is not attached to a shared store; run scope-recall attach --host workbuddy first"]


def initialize_instance(plan: InstallPlan) -> str:
    raise InstallError("WorkBuddy joins a shared store: attach its home first (scope-recall attach --host workbuddy)")


def _bound(instance_root: Path):
    try:
        return load_shared_client(instance_root, HOST)
    except CodexConfigError as exc:
        raise InstallError(f"existing WorkBuddy binding is unusable: {exc}") from exc


def installation_id(instance_root: Path) -> str:
    return _bound(instance_root).installation_id


def validate_reuse(plan: InstallPlan) -> None:
    config = _bound(plan.instance_root)
    if config.agent_id != plan.agent_id:
        raise InstallError("existing WorkBuddy entry agent_id mismatch: the store's is " + config.agent_id)
    if config.test_mode != plan.test_mode:
        raise InstallError(
            f"existing WorkBuddy entry test_mode mismatch: stored={config.test_mode}, requested={plan.test_mode}"
        )


def purge_identity(instance_root: Path) -> tuple[Path, str, str, Path]:
    raise InstallError("an entry of a shared store is never purged from its home; detach it instead")
