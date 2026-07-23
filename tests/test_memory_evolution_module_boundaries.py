"""Static architecture guards for the fact-evolution implementation boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_DOC = ROOT / "docs" / "fact-evolution-architecture.md"
PLANNED_MODULES = {
    "digest_pollution.py",
    "fact_identity.py",
    "fact_actions.py",
    "evolution_policy.py",
    "temporal_facts.py",
    "temporal_query.py",
    "fact_repository.py",
    "fact_executor.py",
    "fact_evolution.py",
    "fact_tooling.py",
    "reflection.py",
    "reflection_llm.py",
    "reflection_tooling.py",
}
LOW_LEVEL_MODULES = {
    "digest_pollution.py",
    "fact_identity.py",
    "fact_actions.py",
    "evolution_policy.py",
    "temporal_facts.py",
    "temporal_query.py",
    "fact_repository.py",
}
RUNTIME_FACADES = {
    "provider",
    "tooling",
    "fact_tooling",
    "journal",
    "nightly_digest",
    "reflection",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.lstrip(".").split(".")[0])
    return imports


def test_architecture_document_declares_every_planned_module():
    text = ARCHITECTURE_DOC.read_text(encoding="utf-8")

    assert {name for name in PLANNED_MODULES if f"`{name}`" not in text} == set()
    assert "NOOP / ADD / ENRICH / SUPERSEDE / RETRACT / REVIEW" in text or all(
        f"`{action}`" in text
        for action in ("noop", "add", "enrich", "supersede", "retract", "review")
    )


def test_low_level_fact_modules_never_import_runtime_facades():
    violations: dict[str, list[str]] = {}
    for name in LOW_LEVEL_MODULES:
        path = ROOT / name
        if not path.exists():
            continue
        forbidden = sorted(_imports(path) & RUNTIME_FACADES)
        if forbidden:
            violations[name] = forbidden

    assert violations == {}


def test_reflection_never_imports_mutation_executor():
    violations = {
        name: sorted(_imports(ROOT / name) & {"fact_executor"})
        for name in ("reflection.py", "reflection_llm.py")
        if (ROOT / name).exists()
    }

    assert all(not imports for imports in violations.values())


def test_fact_action_and_identity_modules_do_not_import_sqlite():
    violations = {
        name: sorted(_imports(ROOT / name) & {"sqlite3"})
        for name in ("fact_identity.py", "fact_actions.py", "evolution_policy.py")
        if (ROOT / name).exists()
    }

    assert all(not imports for imports in violations.values())
