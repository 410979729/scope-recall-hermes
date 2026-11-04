"""Read the maintained state tables and construct isolated synthetic queue stores."""

from __future__ import annotations

import itertools
import re
from dataclasses import replace
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace

from hypothesis import settings
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.recall_policy import EMBEDDING_SPACE
from scope_recall.runtime.instance_config import RuntimeInstanceConfig, VectorRuntimeConfig

from tests.contract.test_claims import Clock
from tests.v11_support import context

QUEUE_SETTINGS = settings(max_examples=12, stateful_step_count=30, deadline=None, database=None, derandomize=True)


@cache
def state_table(heading):
    """Read the allowed successor states from one maintainer-facing Markdown table."""
    text = (Path(__file__).resolve().parents[1] / "docs/background-work.md").read_text(encoding="utf-8")
    body = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return {
        cells[0].strip(): {state.strip() for state in cells[1].split(",")}
        for line in body.splitlines()
        if line.startswith("| ") and (cells := line.strip("| ").split("|"))[0].strip() != "State"
    }


def check_transitions(table, before, after):
    """Every observed transition must be an edge in the maintainer's state table."""
    for key in before.keys() & after.keys():
        assert after[key] in table[before[key]], (key, before[key], after[key])


def schema_states(conn, table, column):
    """Read a SQLite column's persisted CHECK state set without changing the database."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    check = re.search(rf"CHECK\({column} IN\s*\(([^)]+)\)\)", sql)
    assert check, sql
    return set(re.findall(r"'([^']+)'", check[1]))


def advance(core, seconds):
    """Advance one synthetic core's UTC clock by the supplied seconds."""
    now = datetime.fromisoformat(core.clock.utc_now().replace("Z", "+00:00")) + timedelta(seconds=seconds)
    core.clock.now = now.isoformat().replace("+00:00", "Z")


def planning_config(context, *, capable):
    """Enable synthetic routes for wake planning without constructing a transport or opening a budget ledger."""
    binding = context.binding
    config = RuntimeInstanceConfig.from_mapping(
        {
            "binding": {
                "agent_id": binding.agent_id,
                "installation_id": binding.installation_id,
                "data_directory": str(binding.data_directory),
                "scope_ids": sorted(binding.scope_ids),
                "test_mode": True,
            },
            "session_id": context.session_id,
            "allowed_scope_ids": sorted(context.allowed_scope_ids),
            "actor_origin": context.actor_origin,
            "project_id": context.project_id,
            "branch_id": context.branch_id,
            "auxiliary": {"external_embedding": False, "external_consolidation": False},
        }
    )
    auxiliary = replace(
        config.auxiliary,
        external_embedding=capable,
        embedding=SimpleNamespace(space=lambda: dict(EMBEDDING_SPACE), model="TEST-planner"),
        external_consolidation=capable,
        consolidation=object(),
        ledger_path=binding.data_directory / "TEST-planner-budget.sqlite3",
    )
    return replace(
        config,
        auxiliary=auxiliary,
        vector=VectorRuntimeConfig(
            backend="sqlite-vec",
            storage_dir=binding.data_directory / "TEST-planner-vectors",
            table_name="TEST-planner",
            dimensions=EMBEDDING_SPACE["dimensions"],
            test_injection_override=True,
        ),
    )


def store_factory(root):
    """Create a factory that initializes independent synthetic stores beneath the owned test root."""
    sequence = itertools.count()

    def create():
        ctx = context(root / f"TEST-queue-{next(sequence)}")
        core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
        core.initialize()
        core.test_sequence = itertools.count()
        return core, ctx

    return create
