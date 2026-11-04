"""Choose a usable hook interpreter and plan installation into a fresh host venv."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .._version import __version__
from .doctor import probe_python_package
from .install_common import InstallError


def _source() -> str:
    """Use the installed development checkout locally; published releases use their exact package version."""
    if ".dev" in __version__:
        # A source checkout can expose egg-info before the installed dist-info;
        # its metadata then has no direct_url, and the dev build is not on PyPI.
        checkout = Path(__file__).resolve().parent.parent
        if (checkout / "pyproject.toml").is_file():
            return str(checkout)
        raw = distribution("hermes-scope-recall").read_text("direct_url.json")
        if raw:
            url = urlsplit(json.loads(raw).get("url", ""))
            if url.scheme == "file" and not url.netloc:
                path = unquote(url.path)
                if os.name == "nt" and path.startswith("/"):
                    path = path[1:]
                if Path(path).is_dir():
                    return path
    return "hermes-scope-recall==" + __version__


def choose_python(host: str, candidate: Path, *, explicit: bool) -> tuple[Path, list[str], str | None]:
    """Keep host-local Python when usable; only a fresh Hermes venv needs package installation."""
    if candidate == Path(sys.executable):
        return candidate, [], None
    probe = probe_python_package(candidate)
    if probe.get("version") == __version__:
        return candidate, [], None
    if not explicit and host != "hermes":
        return Path(sys.executable), [], None
    if probe.get("version"):
        return candidate, [], "the host environment has another release; coordinate its live package upgrade first"
    venv = candidate.parent.parent
    uv = shutil.which("uv")
    if not (venv / "pyvenv.cfg").is_file() or not uv:
        return (
            candidate,
            [],
            "install the package in the selected venv or run setup from its Python; uv is needed for a pip-less venv",
        )
    return candidate, [uv, "pip", "install", "--python", str(candidate), _source()], None


def install_package(command: list[str], python: Path) -> None:
    """Run only the approved venv package step, with bounded output and no credential-bearing error text."""
    if command:
        result = subprocess.run(command, capture_output=True, timeout=300)
        if result.returncode or probe_python_package(python).get("version") != __version__:
            raise InstallError("host venv package installation failed; preserve the environment and rerun setup")
