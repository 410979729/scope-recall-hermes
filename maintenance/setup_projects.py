"""Suggest repository copies without making a remote URL an authorization grant."""

from __future__ import annotations

import configparser
from pathlib import Path
from urllib.parse import urlsplit

from ..adapters.clients.config import CodexConfigError, load_codex_config
from .backup import atomic_write


def remote_identity(root: Path) -> str | None:
    """Read only a regular checkout's origin; ignore worktree pointers and credential-bearing URLs."""
    path = root / ".git" / "config"
    if not path.is_file() or path.stat().st_size > 65536:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
        url = parser.get('remote "origin"', "url", fallback="").strip()
    except (OSError, ValueError, configparser.Error):
        return None
    if "://" in url:
        parts = urlsplit(url)
        if (parts.username and parts.scheme != "ssh") or parts.password or parts.query or parts.fragment:
            return None
        identity = (parts.hostname or "").lower() + "/" + parts.path.strip("/")
    elif "@" in url and ":" in url:
        host, _, repository = url.rpartition(":")
        identity = host.partition("@")[2].lower() + "/" + repository.strip("/")
    else:
        return None
    return identity.removesuffix(".git").rstrip("/") or None


def project_suggestions(home: Path, project: Path) -> list[dict[str, str]]:
    """Only local Codex project mappings need this grant; shared entries bind identity independently of cwd."""
    path = home / "codex-installation.json"
    if not path.is_file():
        return []
    try:
        config = load_codex_config(path)
    except CodexConfigError:
        return []
    normalized = str(project.resolve())
    if any(Path(root) == project.resolve() for root in config.project_roots):
        return []
    remote = remote_identity(project)
    if remote is None:
        return []
    return [
        dict(
            directory=normalized, same_project_as=root, scope_id=scope, reason="matching origin; confirmation required"
        )
        for root, scope in config.project_roots.items()
        if remote_identity(Path(root)) == remote
    ]


def register_project_copy(home: Path, suggestion: dict[str, str]) -> None:
    """Register one explicitly confirmed directory, preserving all existing scope grants."""
    import json

    path = home / "codex-installation.json"
    config = load_codex_config(path)
    current = project_suggestions(home, Path(suggestion["directory"]))
    if suggestion not in current:
        raise ValueError("project suggestion changed; rerun setup")
    payload = json.loads(path.read_text(encoding="utf-8"))
    backup = home / ".scope-recall-backups" / "project-registration" / "codex-installation.json"
    if not backup.exists():
        atomic_write(backup, path.read_bytes())
    payload["project_roots"][suggestion["directory"]] = suggestion["scope_id"]
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    load_codex_config(config.config_path)
