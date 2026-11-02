"""Report documented host memory settings and file metadata without changing them."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import tomllib
from contextlib import closing
from pathlib import Path

import yaml
from scope_recall.core.recall_budget import estimate_tokens
from scope_recall.core.recall_restatement import normalized_statement

_CONFIG_LIMIT = 65536
RECOMMENDATION = "Keep project rules and host habits in host memory; keep preferences and decisions in Scope Recall."


def _configuration(path: Path, kind: str) -> dict | None:
    """Read a bounded settings file; an unreadable or invalid file cannot establish a switch."""
    try:
        with path.open("rb") as stream:
            data = stream.read(_CONFIG_LIMIT + 1)
        if len(data) > _CONFIG_LIMIT:
            return None
        text = data.decode("utf-8-sig")
        value = tomllib.loads(text) if kind == "toml" else yaml.safe_load(text) if kind == "yaml" else json.loads(text)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return None


def _file(path: Path) -> dict:
    """Report file size without reading or following a symbolic link."""
    try:
        if path.is_symlink():
            return {"path": str(path), "bytes": None, "state": "unknown"}
        stat = path.stat()
        return (
            {"path": str(path), "bytes": stat.st_size, "state": "present"}
            if path.is_file()
            else {"path": str(path), "bytes": None, "state": "unknown"}
        )
    except FileNotFoundError:
        return {"path": str(path), "bytes": None, "state": "missing"}
    except OSError:
        return {"path": str(path), "bytes": None, "state": "unknown"}


def _switches(config: dict | None, section: str, names: tuple[str, ...]) -> dict:
    part = config.get(section) if config else None
    part = part if isinstance(part, dict) else {}
    return {name: part.get(name) if type(part.get(name)) is bool else None for name in names}


def _project_root(project: Path) -> Path | None:
    """Resolve a repository's common checkout, or the given directory outside Git."""
    try:
        project = project.resolve()
        for folder in (project, *project.parents):
            marker = folder / ".git"
            if marker.is_dir():
                return folder
            if marker.is_file():
                with marker.open(encoding="utf-8") as stream:
                    text = stream.read(4097)
                if len(text) > 4096 or not text.startswith("gitdir: "):
                    return None
                gitdir = (folder / text[8:].strip()).resolve()
                common = gitdir / "commondir"
                if common.is_file():
                    with common.open(encoding="utf-8") as stream:
                        value = stream.read(4097).strip()
                else:
                    value = "../.."
                if len(value) > 4096:
                    return None
                root = (gitdir / value).resolve()
                return root.parent if root.name == ".git" else None
        return project
    except (OSError, UnicodeError, ValueError):
        return None


def _claude_paths(home: Path, project: Path, env) -> tuple[Path, ...] | None:
    """Known settings in precedence order; an unreadable policy directory is unknown."""
    root = _project_root(project) or project
    paths = [home / "settings.json", root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    if os.name == "nt":
        program_files = env.get("ProgramFiles") or env.get("PROGRAMFILES")
        policy = Path(program_files) / "ClaudeCode" if program_files else None
    elif sys.platform == "darwin":
        policy = Path("/Library/Application Support/ClaudeCode")
    else:
        policy = Path("/etc/claude-code")
    if policy is not None:
        paths.append(policy / "managed-settings.json")
        try:
            with os.scandir(policy / "managed-settings.d") as listing:
                names = []
                for entry in listing:
                    if not entry.name.startswith(".") and entry.name.endswith(".json"):
                        names.append(entry.name)
                        if len(names) > 64:
                            return None
            paths.extend(policy / "managed-settings.d" / name for name in sorted(names))
        except FileNotFoundError:
            pass
        except OSError:
            return None
    return tuple(paths)


def _claude_settings(paths: tuple[Path, ...]) -> dict | None:
    """Merge existing readable files; an inaccessible or malformed source is unknown."""
    merged = {}
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        config = _configuration(path, "json")
        if config is None:
            return None
        permissions = config.get("permissions")
        prior = merged.get("permissions")
        merged.update(config)
        if isinstance(permissions, dict) and isinstance(prior, dict):
            merged["permissions"] = {**prior, **permissions}
    return merged


def _claude_directory(home: Path, project: Path, config: dict, env) -> Path | None:
    """Resolve an explicit directory or the documented repository-scoped default."""
    directory = config.get("autoMemoryDirectory")
    if isinstance(directory, str):
        if directory.startswith("~/"):
            base = env.get("USERPROFILE") or env.get("HOME")
            directory = str(Path(base) / directory[2:]) if base else str(home.parent / directory[2:])
        candidate = Path(directory)
        return candidate if candidate.is_absolute() else None
    if directory is not None:
        return None
    name = env.get("CLAUDE_CODE_PROJECT_DIR_NAME") if env.get("CLAUDE_CONFIG_DIR") else None
    root = _project_root(project)
    if root is None:
        return None
    name = name or re.sub(r"[^a-zA-Z0-9]", "-", str(root))
    # The host has multiple hash implementations for paths over 200 characters.
    if len(name) > 200 or not name or name in {".", ".."} or Path(name).name != name:
        return None
    return home / "projects" / name / "memory"


def inspect_host_memory(host: str, home: Path, *, project: Path, environment=None) -> dict:
    """Describe known configured switches and files, leaving session-effective state unknown.

    ``home`` is the explicitly selected host root. Missing or unsupported settings
    remain unknown because host policy and session options can override files.
    """
    env = os.environ if environment is None else environment
    state = {"status": "unknown", "switches": {}, "files": [], "recommendation": RECOMMENDATION}
    if host == "codex":
        config = _configuration(home / "config.toml", "toml")
        state["switches"] = _switches(config, "memories", ("generate_memories", "use_memories"))
        features = config.get("features") if config else None
        flags = (
            [features[name] for name in ("memories", "memory_tool") if name in features]
            if isinstance(features, dict)
            else []
        )
        # Conflicting or malformed aliases cannot establish an effective feature state.
        state["feature_switch"] = (
            flags[0] if flags and all(type(flag) is bool and flag == flags[0] for flag in flags) else None
        )
        settings = config.get("memories", {}) if config else {}
        if not isinstance(settings, dict):
            return state
        version = settings.get("version")
        if version not in (None, "v1", "v2"):
            return state
        if version is None:
            version = "v1" if (home / "memories").is_dir() or settings.get("dual_write") is False else "v2"
        root = home / ("memories_v2" if version == "v2" else "memories")
        state["files"] = [_file(root / "memory_summary.md")]
        state["status"] = "configured" if config is not None else "unknown"
    elif host == "hermes":
        config = _configuration(home / "config.yaml", "yaml")
        state["switches"] = _switches(config, "memory", ("memory_enabled", "user_profile_enabled"))
        state["files"] = [_file(home / "memories" / name) for name in ("MEMORY.md", "USER.md")]
        state["status"] = "configured" if config is not None else "unknown"
    elif host == "claude-code":
        paths = _claude_paths(home, project, env)
        config = _claude_settings(paths) if paths is not None else None
        enabled = config.get("autoMemoryEnabled") if config else None
        permissions = config.get("permissions") if config else None
        if (
            isinstance(permissions, dict)
            and permissions.get("blockReadsOutsideWorkingDirectories") is True
            and config is not None
            and config.get("autoMemoryDirectory")
        ):
            enabled = None
        if env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1":
            enabled = False
        state["switches"] = {"autoMemoryEnabled": enabled if type(enabled) is bool else None}
        directory = _claude_directory(home, project, config, env) if config is not None else None
        if directory is not None:
            state["files"] = [_file(directory / "MEMORY.md")]
        state["status"] = "configured" if config else "unknown"
    return state


def memory_entries(text: str) -> tuple[str, ...]:
    """Split visible Markdown into whole list items and standalone prose entries."""
    entries = []
    paragraph = []
    in_fence = False
    list_item = False

    def flush() -> None:
        if paragraph:
            entries.append(normalized_statement(" ".join(paragraph)))
            paragraph.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            in_fence = not in_fence
            list_item = False
            continue
        if in_fence:
            continue
        item = re.match(r"^\s*(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if item:
            flush()
            list_item = True
            paragraph.append(item.group(1))
        elif not stripped or stripped.startswith("#"):
            flush()
            list_item = False
        else:
            if paragraph and not list_item and paragraph[-1].endswith((".", "。", "!", "！", "?", "？")):
                flush()
            paragraph.append(stripped)
    flush()
    return tuple(entry for entry in entries if entry)


_MEMORY_LIMIT = 65536
_MEMORY_READ_SECONDS = 0.2
_MEMORY_READ_LOCK = threading.Lock()


def _token_prefix(text: str, limit: int) -> str:
    """Keep the longest prefix within the packet estimator's token budget."""
    if estimate_tokens(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _host_text(path: Path, started: float) -> str | None:
    """Read one unchanged regular file that predates the session, within the byte ceiling."""
    try:
        if path.is_symlink():
            return None
        before = path.stat()
        if not path.is_file() or before.st_size > _MEMORY_LIMIT or before.st_mtime_ns >= int(started * 1_000_000_000):
            return None
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(_MEMORY_LIMIT + 1)
            after = os.fstat(stream.fileno())
        current = path.stat()

        def fingerprint(st):
            return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

        if len(data) > _MEMORY_LIMIT or any(fingerprint(st) != fingerprint(before) for st in (opened, after, current)):
            return None
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError, OverflowError, ValueError):
        return None


def _settings_snapshot(paths: tuple[Path, ...], started: float) -> tuple[str | None, ...] | None:
    """Read each available setting file before and after discovery, rejecting late or unreadable files."""
    snapshot = []
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            snapshot.append(None)
            continue
        except OSError:
            return None
        text = _host_text(path, started)
        if text is None:
            return None
        snapshot.append(text)
    return tuple(snapshot)


def _read_host(host: str, home: Path, project: Path, started: float, environment) -> tuple[str, ...]:
    config_name = {"hermes": "config.yaml", "codex": "config.toml", "claude-code": "settings.json"}.get(host)
    if config_name is None:
        return ()
    env = os.environ if environment is None else environment
    paths = _claude_paths(home, project, env) if host == "claude-code" else (home / config_name,)
    if paths is None:
        return ()
    before = _settings_snapshot(paths, started)
    if before is None:
        return ()
    state = inspect_host_memory(host, home, project=project, environment=environment)
    if host == "claude-code" and _claude_paths(home, project, env) != paths:
        return ()
    if _settings_snapshot(paths, started) != before:
        return ()
    switches = state["switches"]
    if (host == "codex" and (state.get("feature_switch") is not True or switches.get("use_memories") is not True)) or (
        host == "claude-code" and switches.get("autoMemoryEnabled") is not True
    ):
        return ()
    selected = (
        [
            row
            for row, key in zip(state["files"], ("memory_enabled", "user_profile_enabled"), strict=True)
            if switches.get(key) is True
        ]
        if host == "hermes"
        else state["files"]
    )
    entries = []
    for row in selected:
        text = _host_text(Path(row["path"]), started)
        if text is None:
            return ()
        full_text = text
        if host == "claude-code":
            text = b"\n".join(text.encode("utf-8").split(b"\n")[:200])[:25000].decode("utf-8", errors="ignore")
        elif host == "codex":
            text = _token_prefix(text, 2500)
        visible = memory_entries(text.replace("§", "\n") if host == "hermes" else text)
        # A limit can cut off a continuation or condition, so its last entry is not known to be complete.
        entries.extend(visible[:-1] if text != full_text else visible)
    return tuple(entries)


def _hermes_session_started(home: Path, session_id: str) -> float | None:
    """Read the selected profile's session-start timestamp without writing its store."""
    path = home / "state.db"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.01)) as db:
            row = db.execute("SELECT started_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        value = row[0] if row else None
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            else None
        )
    except (OSError, sqlite3.Error, ValueError):
        return None


def read_host_memory(
    host: str,
    home: Path,
    *,
    project: Path,
    session_started,
    deadline: float,
    environment=None,
    session_id=None,
    start_reader=None,
) -> tuple[str, ...]:
    """Read only known enabled session-old memory without extending the caller's deadline."""
    if session_started is not None and (
        type(session_started) not in (int, float) or not math.isfinite(session_started)
    ):
        return ()
    if (
        session_started is None
        and not callable(start_reader)
        and not (host == "hermes" and type(session_id) is str and session_id)
    ):
        return ()
    remaining = min(_MEMORY_READ_SECONDS, deadline - time.monotonic())
    if remaining <= 0 or not _MEMORY_READ_LOCK.acquire(blocking=False):
        return ()
    result = []
    finished = threading.Event()

    def read() -> None:
        try:
            started = session_started
            if started is None and callable(start_reader):
                started = start_reader()
            elif started is None and isinstance(session_id, str):
                started = _hermes_session_started(home, session_id)
            if isinstance(started, (int, float)) and not isinstance(started, bool) and math.isfinite(started):
                result.append(_read_host(host, home, project, started, environment))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        finally:
            _MEMORY_READ_LOCK.release()
            finished.set()

    try:
        threading.Thread(target=read, name="host-memory-read", daemon=True).start()
    except RuntimeError:
        _MEMORY_READ_LOCK.release()
        return ()
    return result[0] if finished.wait(remaining) and result else ()
