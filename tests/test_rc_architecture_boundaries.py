"""Release-candidate architecture boundary gates.

These checks intentionally use the AST.  SQLite, locks, and compatibility
reflection remain valid inside infrastructure and the outer Hermes adapter;
the gates apply to Domain/Application code and the Core-facing port module.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "provider.py"
COMPOSITION = ROOT / "_internal" / "runtime" / "composition.py"
PORTS = ROOT / "_internal" / "runtime" / "ports.py"

LEAF_DIRS = (
    ROOT / "_internal" / "application",
    ROOT / "_internal" / "recall",
    ROOT / "_internal" / "memory",
)

CORE_RUNTIME_FILES = (
    ROOT / "_internal" / "runtime" / "composition.py",
    ROOT / "_internal" / "runtime" / "bootstrap.py",
    ROOT / "_internal" / "runtime" / "capture_service.py",
    ROOT / "_internal" / "runtime" / "vector_service.py",
)

GENERATED_SOURCE_ROOTS = {
    ".execution",
    ".git",
    ".hermes-agent-src",
    ".venv",
    "build",
    "dist",
    "venv",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _python_files(directory: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(directory.rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def _is_generated_source(path: Path, *, root: Path = ROOT) -> bool:
    relative = path.relative_to(root)
    return bool(relative.parts and relative.parts[0] in GENERATED_SOURCE_ROOTS)


def test_generated_source_filter_covers_build_and_evidence(tmp_path: Path) -> None:
    assert _is_generated_source(
        tmp_path / "build" / "lib" / "provider.py",
        root=tmp_path,
    )
    assert _is_generated_source(
        tmp_path / ".execution" / "evidence" / "provider.py",
        root=tmp_path,
    )
    assert not _is_generated_source(tmp_path / "provider.py", root=tmp_path)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _annotation_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        item.id if isinstance(item, ast.Name) else item.attr
        for item in ast.walk(node)
        if isinstance(item, (ast.Name, ast.Attribute))
    }


def _function_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.arg, ...]:
    return tuple(node.args.posonlyargs + node.args.args + node.args.kwonlyargs)


def test_provider_is_the_only_hermes_composition_root() -> None:
    provider_tree = _tree(PROVIDER)
    provider_imports = _imported_modules(provider_tree)
    assert "agent.memory_provider" in provider_imports

    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if path == PROVIDER or "tests" in path.parts or _is_generated_source(path):
            continue
        imports = _imported_modules(_tree(path))
        if any(
            "hermes_cli" in module
            or "hermes.memory" in module
            or module == "agent.memory_provider"
            for module in imports
        ):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []

    composition_calls = [
        node
        for node in ast.walk(provider_tree)
        if isinstance(node, ast.Call) and _call_name(node) == "assemble_runtime"
    ]
    assert composition_calls, "Provider must remain the runtime composition root"


def test_application_and_domain_do_not_import_provider() -> None:
    forbidden_roots = {"provider", "sqlite3", "threading", "hermes_cli"}
    offenders: list[str] = []
    for directory in LEAF_DIRS:
        for path in _python_files(directory):
            imports = _imported_modules(_tree(path))
            if any(
                module.split(".")[0] in forbidden_roots
                or module.endswith(".provider")
                or ".provider." in module
                for module in imports
            ):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_core_ports_do_not_expose_sqlite_connection() -> None:
    tree = _tree(PORTS)
    assert "sqlite3" not in _imported_modules(tree)
    exposed = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "Connection" in _annotation_names(node.returns)
    ]
    assert exposed == []


def test_core_ports_do_not_expose_lock_types() -> None:
    tree = _tree(PORTS)
    forbidden = {"Lock", "RLock", "QueryLock"}
    exposed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        annotations = _annotation_names(node.returns)
        for arg in _function_args(node):
            annotations.update(_annotation_names(arg.annotation))
        if annotations & forbidden:
            exposed.append(node.name)
    assert exposed == []


def test_core_ports_do_not_use_generic_host_or_unbounded_any() -> None:
    tree = _tree(PORTS)
    assert "Any" not in _imported_modules(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.args.vararg is not None or node.args.kwarg is not None:
            offenders.append(node.name)
            continue
        for arg in _function_args(node):
            if arg.arg in {"host", "adapter", "provider", "obj"}:
                offenders.append(node.name)
            if "Any" in _annotation_names(arg.annotation):
                offenders.append(node.name)
        if "Any" in _annotation_names(node.returns):
            offenders.append(node.name)
    assert sorted(set(offenders)) == []


def test_assemble_runtime_accepts_typed_dependencies_not_provider() -> None:
    tree = _tree(COMPOSITION)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "assemble_runtime"
    )
    first = function.args.args[0]
    assert first.arg in {"dependencies", "deps"}
    assert "RuntimeDependencies" in _annotation_names(first.annotation)

    provider_tree = _tree(PROVIDER)
    for call in (
        node
        for node in ast.walk(provider_tree)
        if isinstance(node, ast.Call) and _call_name(node) == "assemble_runtime"
    ):
        assert call.args
        assert not isinstance(call.args[0], ast.Name) or call.args[0].id != "self"


def test_core_runtime_does_not_resolve_provider_through_sys_modules() -> None:
    offenders: list[str] = []
    for path in CORE_RUNTIME_FILES:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "modules":
                if isinstance(node.value, ast.Name) and node.value.id == "sys":
                    offenders.append(path.relative_to(ROOT).as_posix())
            if isinstance(node, ast.Attribute) and node.attr == "__module__":
                offenders.append(path.relative_to(ROOT).as_posix())
            if isinstance(node, ast.Constant) and node.value == "scope_recall.provider":
                offenders.append(path.relative_to(ROOT).as_posix())
    assert sorted(set(offenders)) == []


def test_runtime_hooks_are_constructor_injected() -> None:
    for relative in (
        "_internal/runtime/bootstrap.py",
        "_internal/runtime/capture_service.py",
        "_internal/runtime/vector_service.py",
    ):
        path = ROOT / relative
        tree = _tree(path)
        initializers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ]
        assert initializers, f"{relative} must expose an injected runtime object"
        assert any(
            any(arg.arg in {"hooks", "dependencies", "gateway", "operations"}
                for arg in _function_args(initializer))
            for initializer in initializers
        ), f"{relative} must receive hooks/capabilities explicitly"
