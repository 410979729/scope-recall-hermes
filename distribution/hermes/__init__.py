"""Hermes host wrapper entry; delegates to the installed Scope Recall core adapter.

Keep the literal ``register_memory_provider`` string for Hermes user-plugin discovery.
"""

try:
    from scope_recall.adapters.hermes import register_adapter
except ImportError as exc:  # the core package is not in the Python environment this Hermes runs
    raise ImportError(
        "Scope Recall's core package (hermes-scope-recall) is not installed in the Python environment "
        "this Hermes runs; install the same release there (docs/install.md)"
    ) from exc


def register(ctx):
    return register_adapter(ctx)
