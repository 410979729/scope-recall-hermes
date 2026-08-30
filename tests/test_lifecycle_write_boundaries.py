"""Architecture guardrails for durable lifecycle and hard-delete writes."""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ALLOWED_METADATA_SQL = {
    ("memory_ops.py", "_sync_conflict_metadata"),
    ("memory_ops.py", "feedback_memory"),
    ("sql_store.py", "ensure_memory_columns"),
    ("sql_store.py", "update_row"),
    ("sql_store.py", "_sync_conflict_metadata_after_relation_delete"),
    ("lifecycle_service.py", "transition_memory_lifecycle"),
    ("journal.py", "_merge_metadata"),
    ("nightly_digest.py", "merge_candidate_metadata"),
    ("privacy_purge.py", "_redact_journal_sources"),
}
ALLOWED_LIFECYCLE_PLANNERS = {
    ("governance.py", "merge_metadata"),
    ("lifecycle_service.py", "transition_memory_lifecycle"),
    ("candidate_review.py", "transition_candidate_metadata"),
    ("scripts/benchmark.graph_relations.py", "item"),
    ("scripts/migrate.legacy_hygiene.py", "planned_updates"),
}
ALLOWED_HARD_DELETE_SQL = {("sql_store.py", "delete_rows")}
ALLOWED_DELETE_ROWS_CALLS = {
    ("lifecycle_service.py", "hard_delete_memories"),
    ("privacy_purge.py", "erase_privacy_purge"),
}


GENERATED_SOURCE_ROOTS = {
    ".execution",
    ".hermes-agent-src",
    ".venv",
    "build",
    "dist",
    "venv",
}


def _python_sources(root: Path = ROOT) -> list[Path]:
    sources: list[Path] = []
    for path in root.rglob("*.py"):
        relative_parts = path.relative_to(root).parts
        if (
            "tests" in relative_parts
            or "__pycache__" in relative_parts
            or ".git" in relative_parts
            or (relative_parts and relative_parts[0] in GENERATED_SOURCE_ROOTS)
        ):
            continue
        sources.append(path)
    return sorted(sources)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def test_python_sources_ignore_generated_copies(tmp_path: Path) -> None:
    source = tmp_path / "runtime.py"
    generated = tmp_path / "build" / "lib" / "scope_recall" / "runtime.py"
    evidence_copy = tmp_path / ".execution" / "evidence" / "runtime.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    generated.parent.mkdir(parents=True)
    generated.write_text("VALUE = 1\n", encoding="utf-8")
    evidence_copy.parent.mkdir(parents=True)
    evidence_copy.write_text("VALUE = 1\n", encoding="utf-8")

    assert _python_sources(tmp_path) == [source]


def _function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def _is_lifecycle_subscript(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "lifecycle"
    )


def test_lifecycle_and_hard_delete_writes_have_narrow_domain_boundaries() -> None:
    metadata_sql: set[tuple[str, str]] = set()
    lifecycle_assignments: set[tuple[str, str]] = set()
    hard_delete_sql: set[tuple[str, str]] = set()
    delete_rows_calls: set[tuple[str, str]] = set()

    for path in _python_sources():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        parents = _parents(tree)
        for node in ast.walk(tree):
            function = _function_name(node, parents)
            location = (relative, function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = " ".join(node.value.split()).lower()
                if "update memories" in normalized and "metadata" in normalized:
                    metadata_sql.add(location)
                if re.search(r"delete from memories\b", normalized) and "memories_fts" not in normalized:
                    hard_delete_sql.add(location)
            if isinstance(node, ast.Assign) and any(_is_lifecycle_subscript(target) for target in node.targets):
                lifecycle_assignments.add(location)
            if isinstance(node, ast.AnnAssign) and _is_lifecycle_subscript(node.target):
                lifecycle_assignments.add(location)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "delete_rows"
            ):
                delete_rows_calls.add(location)

    assert metadata_sql == ALLOWED_METADATA_SQL
    assert lifecycle_assignments == ALLOWED_LIFECYCLE_PLANNERS
    assert hard_delete_sql == ALLOWED_HARD_DELETE_SQL
    assert delete_rows_calls == ALLOWED_DELETE_ROWS_CALLS
