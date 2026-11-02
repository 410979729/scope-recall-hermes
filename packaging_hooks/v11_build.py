"""Allowlisted build and installation commands for distributable package files."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from setuptools.command.build_py import build_py
from setuptools.command.install_lib import install_lib

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALLOWLIST_PATH = _REPO_ROOT / "packaging" / "v11-module-allowlist.json"


def _load_allowlist() -> dict:
    return json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))


def _source_version() -> str:
    tree = ast.parse((_REPO_ROOT / "_version.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError("_version.py does not define a literal __version__")


def _package_and_module(relative_py: str) -> tuple[str, str]:
    parts = relative_py[:-3].split("/")
    if len(parts) == 1:
        if parts[0] == "__init__":
            return "scope_recall", "__init__"
        return "scope_recall", parts[0]
    if parts[-1] == "__init__":
        return "scope_recall." + ".".join(parts[:-1]), "__init__"
    return "scope_recall." + ".".join(parts[:-1]), parts[-1]


class AllowlistedBuildPy(build_py):
    # ``egg_info`` asks build_py for source files before ``run`` initializes
    # command state, so the immutable allowlist must be available at class
    # construction time as well as during the actual build.
    allowlist: dict = _load_allowlist()

    def finalize_options(self) -> None:
        super().finalize_options()
        # setuptools caches these on the command during finalization. Updating
        # Distribution only in run() is too late for newly allowlisted skills
        # and operator documents; egg_info must see the same data contract.
        self.packages = list(self.allowlist["packages"])
        self.package_data = {"scope_recall": list(self.allowlist["package_data"])}
        self.distribution.packages = self.packages
        self.distribution.package_data = self.package_data

    def run(self) -> None:
        self.allowlist = _load_allowlist()
        expected = str(self.allowlist.get("package_version") or "")
        actual = _source_version()
        if expected != actual:
            raise RuntimeError(f"allowlist package_version {expected!r} disagrees with _version.py {actual!r}")
        self.distribution.packages = list(self.allowlist["packages"])
        self.distribution.package_data = {
            "scope_recall": list(self.allowlist["package_data"]),
        }
        super().run()
        for name in self.allowlist.get("startup_files", []):
            self.copy_file(str(_REPO_ROOT / "packaging" / name), str(Path(self.build_lib) / name))

    def find_package_modules(self, package, package_dir):
        allowed = set(self.allowlist["python_modules"])
        modules: list[tuple[str, str, str]] = []
        for relative in sorted(allowed):
            if not relative.endswith(".py"):
                continue
            pkg, module = _package_and_module(relative)
            if pkg != package:
                continue
            modules.append((package, module, relative))
        return modules


class AllowlistedInstallLib(install_lib):
    """Copy current package members even when a build directory contains retired output."""

    def get_exclusions(self) -> set[str]:
        """Omit unlisted files from installation while preserving the existing build directory."""
        excluded = set(super().get_exclusions())
        if self.build_dir is None or self.install_dir is None:
            return excluded
        allowlist = _load_allowlist()
        allowed = {
            "scope_recall/" + relative for field in ("python_modules", "package_data") for relative in allowlist[field]
        }
        allowed.update(allowlist.get("startup_files", []))
        root = Path(self.build_dir)
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_file() and relative.as_posix() not in allowed:
                # The copier preserves a dotted install directory; exclusions need the identical spelling.
                excluded.add(os.path.join(self.install_dir, *relative.parts))
        return excluded
