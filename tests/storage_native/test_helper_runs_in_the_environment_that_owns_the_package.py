"""The native helper must run in the environment that owns this package.

Hermes' plugin manager can boot the *base* interpreter and inject a venv's
site-packages into ``sys.path``/``PYTHONPATH`` instead of launching that venv
(no boot through the venv).  ``sys.executable`` and ``sys.prefix`` then name the
base installation while the dependencies live in the injected environment, so
the isolated ``-I`` worker -- which ignores ``PYTHONPATH`` -- could import
``scope_recall`` and died at ``import jsonschema`` inside ``contracts``.

These cases run a real base interpreter whose package belongs to a real external
environment laid out beside it, and assert on the child the production options
actually spawn.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import scope_recall

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the redirector-free helper launch is the Windows venv path; POSIX does not use __PYVENV_LAUNCHER__",
)

_ENV_ONLY_DEPENDENCY = "env_only_dependency"
_IGNORED_FROM_ENVIRONMENT = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__"}
_DRIVER = '''\
import json
import subprocess
import sys
import types

if len(sys.argv) > 1 and sys.argv[1]:
    package = types.ModuleType("scope_recall")
    package.__path__ = [sys.argv[1]]
    sys.modules["scope_recall"] = package

from scope_recall.vector.lance_native import python_subprocess_options

options = python_subprocess_options()
probe = (
    "import sys\\n"
    "print('PREFIX:' + sys.prefix)\\n"
    "import env_only_dependency\\n"
    "print('DEPENDENCY:' + env_only_dependency.__file__)\\n"
)
done = subprocess.run([sys.executable, "-I", "-B", "-c", probe], capture_output=True, text=True, **options)
print(json.dumps({
    "parent_executable": sys.executable,
    "parent_prefix": sys.prefix,
    "launcher": options.get("env", {}).get("__PYVENV_LAUNCHER__"),
    "executable": options.get("executable"),
    "returncode": done.returncode,
    "stdout": done.stdout,
    "stderr": done.stderr,
}))
'''


def _base_interpreter() -> Path:
    """The interpreter a Hermes install boots when it does not boot the venv."""
    candidates = (
        Path(sys.base_prefix) / "python.exe",
        Path(sys.base_prefix) / "Scripts" / "python.exe",
        Path(sys.base_prefix) / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip(f"no interpreter beside sys.base_prefix {sys.base_prefix!r}")


def _run_driver(work: Path, *, site_packages: Path | None = None, plugin_dir: Path | None = None) -> dict[str, Any]:
    """Run one real base interpreter on the production options and return what it saw."""
    driver = work / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _IGNORED_FROM_ENVIRONMENT
    }
    if site_packages is not None:
        environment["PYTHONPATH"] = str(site_packages)
    command = [str(_base_interpreter()), "-B", str(driver)]
    if plugin_dir is not None:
        command.append(str(plugin_dir))
    done = subprocess.run(
        command, cwd=str(work), env=environment,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert done.returncode == 0, f"driver failed:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def _package_root() -> Path:
    """The directory this package is imported from; the suite aliases the checkout."""
    return Path(next(iter(scope_recall.__path__))).resolve()


def _install_package(destination: Path) -> None:
    """An importable copy of this package: the modules these cases exercise.

    ``vector.lance_native`` reaches nothing but the standard library, so the
    copy stays small enough for the Windows path limit; the point is the
    environment this file lands in, not a full payload.
    """
    root = _package_root()
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "contracts.py", "_lance_worker.py"):
        shutil.copy2(root / name, destination / name)
    shutil.copytree(root / "vector", destination / "vector",
                    ignore=shutil.ignore_patterns("__pycache__"))


def _owning_environment(work: Path, *, interpreter_file: bool = True) -> Path:
    """An environment with this package and one dependency no other environment has."""
    root = work / "owning-environment"
    site_packages = root / "Lib" / "site-packages"
    dependency = site_packages / _ENV_ONLY_DEPENDENCY
    dependency.mkdir(parents=True, exist_ok=True)
    (dependency / "__init__.py").write_text("VALUE = 'environment-only'\n", encoding="utf-8")
    (root / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\ninclude-system-site-packages = false\n", encoding="utf-8",
    )
    _install_package(site_packages / "scope_recall")
    if interpreter_file:
        scripts = root / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_base_interpreter(), scripts / "python.exe")
    return root


def _child_prefix(outcome: dict[str, Any]) -> Path:
    for line in outcome["stdout"].splitlines():
        if line.startswith("PREFIX:"):
            return Path(line.removeprefix("PREFIX:"))
    raise AssertionError(f"the helper printed no prefix:\n{outcome}")


@pytest.fixture(scope="module")
def environment_work(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("native-environment")


@pytest.fixture(scope="module")
def owning_outcome(environment_work: Path) -> tuple[Path, dict[str, Any]]:
    """One base interpreter hosting a package that belongs to an external environment."""
    root = _owning_environment(environment_work)
    outcome = _run_driver(environment_work, site_packages=root / "Lib" / "site-packages")
    assert Path(outcome["parent_prefix"]).resolve() != root.resolve(), outcome
    assert Path(outcome["parent_executable"]).resolve() == _base_interpreter().resolve(), outcome
    return root, outcome


def test_the_helper_runs_in_the_environment_that_owns_the_package(owning_outcome) -> None:
    root, outcome = owning_outcome

    assert _child_prefix(outcome).resolve() == root.resolve(), (
        "the helper must run in the environment that owns scope_recall, not in the "
        f"interpreter hosting it:\n{outcome}"
    )


def test_a_dependency_only_the_package_environment_has_reaches_the_helper(owning_outcome) -> None:
    root, outcome = owning_outcome

    assert outcome["returncode"] == 0, f"the helper could not import its environment:\n{outcome}"
    dependency = [line.removeprefix("DEPENDENCY:") for line in outcome["stdout"].splitlines()
                  if line.startswith("DEPENDENCY:")]
    assert dependency, outcome
    assert Path(dependency[0]).resolve().is_relative_to((root / "Lib" / "site-packages").resolve()), outcome


def test_an_environment_without_its_interpreter_file_still_supplies_the_helper(environment_work: Path) -> None:
    """A relocated environment is recognised by its ``pyvenv.cfg``, the marker CPython itself reads.

    The launcher path only tells the child where to look; the child that is
    spawned is this interpreter, so a missing ``Scripts/python.exe`` must not
    fall back to an environment without the dependencies.
    """
    work = environment_work / "relocated"
    work.mkdir()
    root = _owning_environment(work, interpreter_file=False)
    assert not (root / "Scripts" / "python.exe").exists()

    outcome = _run_driver(work, site_packages=root / "Lib" / "site-packages")

    assert _child_prefix(outcome).resolve() == root.resolve(), outcome
    assert outcome["returncode"] == 0, outcome


def test_a_plugin_directory_outside_any_environment_keeps_the_current_interpreter(environment_work: Path) -> None:
    """A directory install belongs to no environment, so nothing about the launch changes."""
    work = environment_work / "plugin-directory"
    work.mkdir()
    plugin_dir = work / "plugins" / "scope-recall"
    _install_package(plugin_dir)
    assert not (plugin_dir / "pyvenv.cfg").exists()

    outcome = _run_driver(work, plugin_dir=plugin_dir)

    assert outcome["launcher"] == outcome["parent_executable"], outcome
    assert outcome["executable"] == outcome["parent_executable"], outcome
