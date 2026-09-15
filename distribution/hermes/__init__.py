"""Hermes host wrapper entry; delegates to the installed Scope Recall core adapter.

Keep the literal ``register_memory_provider`` string for Hermes user-plugin discovery.
"""

from scope_recall.adapters.hermes import register_adapter


def register(ctx):
    return register_adapter(ctx)
