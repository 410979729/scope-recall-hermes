"""Derive the wheel's module list and version stamps from the source tree.

The v3 wheel ships a deliberate subset of this repository: the 2.x modules
(``provider.py``, ``cli.py``, ``config_schema.py`` and friends) still live here
and must stay out.  That subset used to be a hand-copied list of 120 paths in
``packaging/v11-module-allowlist.json``, and the predictable thing happened --
six of the eight packaging failures on 2026-09-13 were new modules nobody had
remembered to add, found only because the packaging tier finally ran.

The subset is not arbitrary, though: it is exactly what the v3 entry points can
reach by import.  So it can be computed, and the committed allowlist becomes a
reviewed artifact that a test compares against the computation rather than a
list somebody retypes.  The safety property is unchanged -- a stray module still
cannot enter the wheel silently, because it can only enter by being imported
from an entry point, and the diff is shown to a human either way.

The same reasoning covers the version string, which lived in four files.
``_version.py`` is the source; everything else is stamped from it.

Not responsible for: building the wheel (``packaging_hooks/v11_build.py``) or
deciding what the entry points are -- that list is right here, in the open,
because it *is* the product's public surface.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = REPO_ROOT / "packaging" / "v11-module-allowlist.json"

#: Every way the outside world enters this package: the package itself, the
#: host adapters' registration surfaces, the console/worker entry points, and
#: the module-form CLIs.  Adding a genuinely new entry point is a deliberate
#: act and belongs in this list; adding a module that an existing entry point
#: imports needs no edit at all.
ENTRY_POINTS = (
    "__init__.py",
    "adapters/codex/hook_entry.py",
    "adapters/codex/mcp_entry.py",
    "adapters/hermes/register.py",
    "distribution/hermes/__init__.py",
    "maintenance/cli.py",
    "maintenance/migrate.py",
    "maintenance/upgrade_cli.py",
    "runtime/_http_worker.py",
    "runtime/_worker_bootstrap.py",
    "runtime/resume_entry.py",
    "runtime/worker_entry.py",
    "runtime/worker_watchdog.py",
)

#: Files that ship as data rather than as importable modules.  ``_lance_worker``
#: is the clearest case: it is launched as an isolated script and installs its
#: own package alias, so importing it as ``scope_recall._lance_worker`` would
#: defeat the isolation it exists to provide.
_DATA_ONLY = ("_lance_worker.py",)

#: Files whose version line is stamped from ``_version.py``.
PLUGIN_MANIFESTS = ("adapters/hermes/plugin.yaml", "distribution/hermes/plugin.yaml")

#: The Codex manifest carries the *semver* spelling instead, and was the one
#: nobody thought to update: it still read 3.1.0-rc.10 while the package was at
#: 3.1.0rc10.post17.  Stamping it from the same source is the whole point.
CODEX_MANIFEST = "distribution/codex/scope-recall/.codex-plugin/plugin.json"


def source_version(repo_root: Path | None = None) -> str:
    """The one true version, read as a literal so nothing has to be imported."""
    root = repo_root or REPO_ROOT
    tree = ast.parse((root / "_version.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            return node.value.value
    raise RuntimeError("_version.py does not define a literal __version__")


def _relative_paths(module: str, root: Path) -> list[str]:
    """The file or files that provide a dotted ``scope_recall.*`` name."""
    tail = module[len("scope_recall"):].lstrip(".")
    if not tail:
        return ["__init__.py"]
    base = tail.replace(".", "/")
    found = []
    if (root / f"{base}.py").is_file():
        found.append(f"{base}.py")
    if (root / base / "__init__.py").is_file():
        found.append(f"{base}/__init__.py")
    return found


def _imported_modules(relative: str, root: Path) -> set[str]:
    """Every ``scope_recall.*`` name one file imports, however deeply nested.

    Walks the whole tree rather than the module body because this code base
    imports inside functions on purpose -- to keep construction cheap and to
    break cycles -- and those imports are just as real at run time.
    """
    try:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    parts = relative[:-3].split("/")
    package = ["scope_recall"] + parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target.startswith("scope_recall"):
                found.add(target)
                # ``from x import y`` may name a submodule rather than a value.
                found.update(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("scope_recall"))
    return found


def reachable_modules(repo_root: Path | None = None) -> list[str]:
    """Sorted module paths the v3 entry points can reach, as the wheel needs them."""
    root = repo_root or REPO_ROOT
    seen: set[str] = set()
    queue = [entry for entry in ENTRY_POINTS if (root / entry).is_file()]
    while queue:
        relative = queue.pop()
        if relative in seen or not (root / relative).is_file():
            continue
        seen.add(relative)
        for module in _imported_modules(relative, root):
            queue.extend(path for path in _relative_paths(module, root) if path not in seen)
    # A module is only importable if every package above it is too, and a
    # package __init__ that imports nothing would otherwise never be reached.
    for relative in list(seen):
        parts = relative.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            init = "/".join(parts[:depth]) + "/__init__.py"
            if (root / init).is_file():
                seen.add(init)
    return sorted(seen - set(_DATA_ONLY))


def reachable_packages(modules: list[str]) -> list[str]:
    """The distribution package names implied by a module list."""
    packages = {"scope_recall"}
    for relative in modules:
        parts = relative.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            packages.add("scope_recall." + ".".join(parts[:depth]))
    return sorted(packages)


def expected_allowlist(repo_root: Path | None = None) -> dict:
    """The allowlist this source tree implies, with curated fields preserved.

    ``package_data`` and ``forbidden_wheel_substrings`` stay hand-maintained:
    they are judgements about what must ship and what must never ship, and
    neither is derivable from imports.
    """
    root = repo_root or REPO_ROOT
    current = json.loads((root / "packaging" / "v11-module-allowlist.json").read_text(encoding="utf-8"))
    modules = reachable_modules(root)
    return {
        "schema": current["schema"],
        "package_version": source_version(root),
        "packages": reachable_packages(modules),
        "python_modules": modules,
        "package_data": list(current["package_data"]),
        "forbidden_wheel_substrings": list(current["forbidden_wheel_substrings"]),
    }


def semver_version(version: str) -> str:
    """The semver spelling of a PEP 440 version, for manifests that demand one.

    Deliberately a second, independent statement of the rule that
    ``maintenance/install_common.py::_manifest_version`` applies, so that this build
    helper needs no runtime import.  ``tests/packaging/test_package_manifest.py``
    asserts the two agree; if they ever diverge, that test says so rather than a
    plugin manifest quietly carrying a version no host can parse.
    """
    if ".dev" in version:
        return version.replace(".dev", "-dev.", 1)
    import re

    return re.sub(r"(\d+\.\d+\.\d+)rc(\d+)", r"\1-rc.\2", version)


def stamped_manifest(text: str, version: str) -> str:
    """Return ``text`` with its ``version:`` line set to ``version``."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("version:"):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"version: {version}{ending}"
            return "".join(lines)
    raise RuntimeError("plugin manifest has no version line")


def stale_version_files(repo_root: Path | None = None) -> dict[str, str]:
    """Files whose stamped version no longer matches ``_version.py``."""
    root = repo_root or REPO_ROOT
    version = source_version(root)
    stale: dict[str, str] = {}
    for relative in PLUGIN_MANIFESTS:
        text = (root / relative).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("version:"):
                found = line.split(":", 1)[1].strip()
                if found != version:
                    stale[relative] = found
                break
    listed = json.loads((root / "packaging" / "v11-module-allowlist.json").read_text(encoding="utf-8"))
    if listed.get("package_version") != version:
        stale["packaging/v11-module-allowlist.json"] = str(listed.get("package_version"))
    codex = json.loads((root / CODEX_MANIFEST).read_text(encoding="utf-8"))
    if codex.get("version") != semver_version(version):
        stale[CODEX_MANIFEST] = str(codex.get("version"))
    return stale


def write_all(repo_root: Path | None = None) -> list[str]:
    """Bring every generated file in line with the source.  Returns what changed."""
    root = repo_root or REPO_ROOT
    version = source_version(root)
    changed: list[str] = []
    for relative in PLUGIN_MANIFESTS:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        stamped = stamped_manifest(text, version)
        if stamped != text:
            path.write_text(stamped, encoding="utf-8", newline="\n")
            changed.append(relative)
    codex_path = root / CODEX_MANIFEST
    codex = json.loads(codex_path.read_text(encoding="utf-8"))
    if codex.get("version") != semver_version(version):
        codex["version"] = semver_version(version)
        codex_path.write_text(json.dumps(codex, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        changed.append(CODEX_MANIFEST)
    allowlist_path = root / "packaging" / "v11-module-allowlist.json"
    expected = expected_allowlist(root)
    current = json.loads(allowlist_path.read_text(encoding="utf-8"))
    if current != expected:
        allowlist_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        changed.append("packaging/v11-module-allowlist.json")
    return changed


__all__ = [
    "ALLOWLIST_PATH",
    "CODEX_MANIFEST",
    "ENTRY_POINTS",
    "PLUGIN_MANIFESTS",
    "REPO_ROOT",
    "expected_allowlist",
    "reachable_modules",
    "reachable_packages",
    "semver_version",
    "source_version",
    "stale_version_files",
    "stamped_manifest",
    "write_all",
]
