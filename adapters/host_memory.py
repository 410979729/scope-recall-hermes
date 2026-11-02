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
from dataclasses import dataclass
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
        return _parse_configuration(data.decode("utf-8-sig"), kind)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return None


def _parse_configuration(text: str, kind: str) -> dict | None:
    """Parse one already bounded configuration without reopening its source."""
    try:
        value = tomllib.loads(text) if kind == "toml" else yaml.safe_load(text) if kind == "yaml" else json.loads(text)
        return value if isinstance(value, dict) else None
    except (ValueError, yaml.YAMLError):
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
    try:
        if not project.is_absolute() or not project.is_dir():
            return None
        project = project.resolve()
        root = _project_root(project)
        if root is None:
            return None
        paths = [
            home / "settings.json",
            project / ".claude" / "settings.json",
            project / ".claude" / "settings.local.json",
        ]
        if os.name != "nt" and root != project and (root / ".git").exists():
            owner = os.getuid()
            owned = all(
                path.stat().st_uid == owner for path in (root, root / ".git", root / ".claude") if path.exists()
            )
            user_home = env.get("HOME")
            if not user_home:
                return None
            if owned and root != Path(user_home).resolve():
                paths.append(root / ".claude" / "settings.local.json")
    except (OSError, ValueError):
        return None
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


def _claude_settings(texts: tuple[str | None, ...]) -> dict | None:
    """Merge the chosen settings snapshots; malformed sources make the result unknown."""
    merged = {}
    for text in texts:
        if text is None:
            continue
        config = _parse_configuration(text, "json")
        if config is None:
            return None
        permissions = config.get("permissions")
        prior = merged.get("permissions")
        merged.update(config)
        if isinstance(permissions, dict) and isinstance(prior, dict):
            merged["permissions"] = {**prior, **permissions}
            # This host restriction is enforced when any settings source enables it.
            if prior.get("blockReadsOutsideWorkingDirectories") is True:
                merged["permissions"]["blockReadsOutsideWorkingDirectories"] = True
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
    paths = _claude_paths(home, project, env) if host == "claude-code" else None
    if host == "claude-code":
        texts = _read_settings(paths) if paths is not None else None
        config = _claude_settings(texts) if texts is not None else None
    else:
        config_name = {"codex": ("config.toml", "toml"), "hermes": ("config.yaml", "yaml")}.get(host)
        config = _configuration(home / config_name[0], config_name[1]) if config_name else None
    return _host_state(host, home, project, env, config)


def _host_state(host: str, home: Path, project: Path, env, config: dict | None) -> dict:
    """Describe a host using the caller's already parsed settings snapshot."""
    state = {"status": "unknown", "switches": {}, "files": [], "recommendation": RECOMMENDATION}
    if host == "codex":
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
        state["switches"] = _switches(config, "memory", ("memory_enabled", "user_profile_enabled"))
        state["files"] = [_file(home / "memories" / name) for name in ("MEMORY.md", "USER.md")]
        state["status"] = "configured" if config is not None else "unknown"
    elif host == "claude-code":
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


@dataclass
class _MarkdownItem:
    """A list item's text and whether children carry its context."""

    indent: int
    parts: list[str]
    has_children: bool = False


class _MarkdownEntries:
    """Keep headings, parent list items and paragraph continuations with their entries."""

    def __init__(self) -> None:
        self.entries: list[str] = []
        self.paragraph: list[str] = []
        self.headings: list[tuple[int, str]] = []
        self.items: list[_MarkdownItem] = []
        self.in_fence = False
        self.blank_after_item = False
        self.characters = 0

    def emit(self, parts: list[str]) -> None:
        # Repeated ancestor context must not amplify a bounded file into an unbounded snapshot.
        self.characters += sum(map(len, parts)) + max(0, len(parts) - 1)
        if self.characters > _MEMORY_LIMIT:
            raise ValueError("host context too large")
        value = normalized_statement(" ".join(parts))
        if value:
            self.entries.append(value)

    def flush_paragraph(self) -> None:
        if self.paragraph:
            self.emit([*(heading[1] for heading in self.headings), *self.paragraph])
            self.paragraph.clear()

    def close_item(self) -> None:
        item = self.items.pop()
        if not item.has_children:
            self.emit(
                [*(heading[1] for heading in self.headings), *(" ".join(p.parts) for p in self.items), *item.parts]
            )

    def close_items(self) -> None:
        while self.items:
            self.close_item()

    def heading(self, match: re.Match[str]) -> None:
        self.flush_paragraph()
        self.close_items()
        level = len(match.group(1))
        while self.headings and self.headings[-1][0] >= level:
            self.headings.pop()
        self.headings.append((level, match.group(2)))

    def item(self, match: re.Match[str]) -> None:
        self.flush_paragraph()
        indent = len(match.group(1).expandtabs(4))
        while self.items and self.items[-1].indent >= indent:
            self.close_item()
        if self.items:
            self.items[-1].has_children = True
        self.items.append(_MarkdownItem(indent, [match.group(2)]))
        self.blank_after_item = False

    def line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            self.flush_paragraph()
            self.close_items()
            self.in_fence = not self.in_fence
            return
        if self.in_fence:
            return
        heading = re.match(r"^\s*(#{1,6})\s+(.+)$", line)
        if heading:
            self.heading(heading)
            return
        item = re.match(r"^(\s*)(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if item:
            self.item(item)
        elif not stripped:
            self.flush_paragraph()
            self.blank_after_item = bool(self.items)
        elif self.items:
            if self.blank_after_item and len(line) == len(line.lstrip()):
                self.close_items()
                self.paragraph.append(stripped)
            else:
                self.items[-1].parts.append(stripped)
            self.blank_after_item = False
        else:
            self.paragraph.append(stripped)


def memory_entries(text: str) -> tuple[str, ...]:
    """Split visible Markdown without dropping conditions in surrounding context."""
    parser = _MarkdownEntries()
    for line in text.splitlines():
        parser.line(line)
    parser.flush_paragraph()
    parser.close_items()
    return tuple(parser.entries)


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


def _setting_fingerprint(path: Path) -> tuple[int, ...] | None:
    """Identify a setting file without reopening its content."""
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def _settings_snapshot(paths: tuple[Path, ...], started: float):
    """Read each available setting once, rejecting late, changing or unreadable files."""
    snapshot = []
    fingerprints = []
    for path in paths:
        try:
            before = _setting_fingerprint(path)
            if before is None:
                snapshot.append(None)
                fingerprints.append(None)
                continue
            text = _host_text(path, started)
            if text is None or _setting_fingerprint(path) != before:
                return None
        except OSError:
            return None
        snapshot.append(text)
        fingerprints.append(before)
    return tuple(snapshot), tuple(fingerprints)


def _read_settings(paths: tuple[Path, ...]) -> tuple[str | None, ...] | None:
    """Read settings for doctor without claiming they describe a session snapshot."""
    snapshot = []
    for path in paths:
        try:
            with path.open("rb") as stream:
                data = stream.read(_CONFIG_LIMIT + 1)
            if len(data) > _CONFIG_LIMIT:
                return None
            snapshot.append(data.decode("utf-8-sig"))
        except FileNotFoundError:
            snapshot.append(None)
        except (OSError, UnicodeError):
            return None
    return tuple(snapshot)


def _read_host(host: str, home: Path, project: Path, started: float, environment) -> frozenset[str]:
    config_name = {"hermes": "config.yaml", "codex": "config.toml", "claude-code": "settings.json"}.get(host)
    if config_name is None:
        return frozenset()
    env = os.environ if environment is None else environment
    paths = _claude_paths(home, project, env) if host == "claude-code" else (home / config_name,)
    if paths is None:
        return frozenset()
    captured = _settings_snapshot(paths, started)
    if captured is None:
        return frozenset()
    snapshot, fingerprints = captured
    kind = {"claude-code": "json", "codex": "toml", "hermes": "yaml"}[host]
    config = (
        _claude_settings(snapshot)
        if host == "claude-code"
        else (_parse_configuration(snapshot[0], kind) if snapshot[0] is not None else None)
    )
    state = _host_state(host, home, project, env, config)
    switches = state["switches"]
    if (host == "codex" and (state.get("feature_switch") is not True or switches.get("use_memories") is not True)) or (
        host == "claude-code" and switches.get("autoMemoryEnabled") is not True
    ):
        return frozenset()
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
    total_bytes = 0
    for row in selected:
        text = _host_text(Path(row["path"]), started)
        if text is None:
            return frozenset()
        # UTF-8 BOMs are stripped while decoding; reserve their maximum size per file.
        total_bytes += len(text.encode("utf-8")) + 3
        if total_bytes > _MEMORY_LIMIT:
            return frozenset()
        full_text = text
        if host == "claude-code":
            text = b"\n".join(text.encode("utf-8").split(b"\n")[:200])[:25000].decode("utf-8", errors="ignore")
        elif host == "codex":
            text = _token_prefix(text, 2500)
        visible = memory_entries(text.replace("§", "\n") if host == "hermes" else text)
        # A limit can cut off a continuation or condition, so its last entry is not known to be complete.
        entries.extend(visible[:-1] if text != full_text else visible)
    if (host == "claude-code" and _claude_paths(home, project, env) != paths) or tuple(
        _setting_fingerprint(path) for path in paths
    ) != fingerprints:
        return frozenset()
    return frozenset(entries)


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
) -> frozenset[str]:
    """Read only known enabled session-old memory without extending the caller's deadline."""
    if session_started is not None and (
        type(session_started) not in (int, float) or not math.isfinite(session_started)
    ):
        return frozenset()
    if (
        session_started is None
        and not callable(start_reader)
        and not (host == "hermes" and type(session_id) is str and session_id)
    ):
        return frozenset()
    remaining = min(_MEMORY_READ_SECONDS, deadline - time.monotonic())
    if remaining <= 0 or not _MEMORY_READ_LOCK.acquire(blocking=False):
        return frozenset()
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
        except Exception:  # noqa: BLE001 - host memory is optional and must not prevent recall
            pass
        finally:
            _MEMORY_READ_LOCK.release()
            finished.set()

    try:
        threading.Thread(target=read, name="host-memory-read", daemon=True).start()
    except RuntimeError:
        _MEMORY_READ_LOCK.release()
        return frozenset()
    return result[0] if finished.wait(remaining) and result else frozenset()
