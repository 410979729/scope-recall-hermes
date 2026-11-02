"""Read-only host discovery from explicit environment roots; never scan other users."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOST_DIRECTORIES = {
    "claude-code": ("CLAUDE_CONFIG_DIR", ".claude"),
    "codex": ("CODEX_HOME", ".codex"),
    "hermes": ("HERMES_HOME", ".hermes"),
    "workbuddy": ("CODEBUDDY_CONFIG_DIR", ".workbuddy"),
    "dsh": ("DSH_HOME", ".dsh"),
}


@dataclass(frozen=True)
class HostLocation:
    """A discovered host home and its local interpreter, or the setup interpreter."""

    host: str
    home: Path
    python: Path
    python_source: str


def user_home(environment: Mapping[str, str], *, platform: str = sys.platform) -> Path:
    """Resolve only the supplied user's home; missing roots require an explicit choice."""
    names = ("USERPROFILE", "HOME") if platform == "win32" else ("HOME", "USERPROFILE")
    value = next((environment[name] for name in names if environment.get(name)), None)
    if value is None or not Path(value).is_absolute():
        raise ValueError("an absolute HOME or USERPROFILE is required")
    return Path(value)


def host_locations(environment: Mapping[str, str], *, platform: str = sys.platform) -> dict[str, Path]:
    """Return defaults and explicit overrides without reading the filesystem or expanding tilde."""
    home = user_home(environment, platform=platform)
    locations = {host: Path(environment.get(key) or home / folder) for host, (key, folder) in HOST_DIRECTORIES.items()}
    if any(not path.is_absolute() for path in locations.values()):
        raise ValueError("host home overrides must be absolute")
    return locations


def interpreter(home: Path, *, platform: str = sys.platform) -> tuple[Path, str]:
    """Prefer a host-local venv without executing anything discovered in its configuration."""
    suffix = Path("Scripts/python.exe") if platform == "win32" else Path("bin/python")
    for directory in ("venv", ".venv", "hermes-agent/venv"):
        candidate = home / directory / suffix
        if candidate.is_file():
            # Preserve venv symlinks: resolving bin/python would select the base environment.
            return candidate, "host-venv"
    return Path(sys.executable), "setup-interpreter"


def discover_hosts(environment: Mapping[str, str] | None = None) -> list[HostLocation]:
    """Probe only the selected user's default roots and explicit host overrides."""
    env = os.environ if environment is None else environment
    return [HostLocation(host, home, *interpreter(home)) for host, home in host_locations(env).items() if home.is_dir()]
