"""Bounded Hermes MemoryProvider adapter over the host-independent core."""

from .identity import HermesIdentityError, bind_hermes_identity
from .installation import (
    build_archive_scope_id,
    install_hermes_archive_migration,
    install_hermes_scope_recall,
    is_archive_scope,
    load_installation_manifest,
)

__all__ = [
    "HermesIdentityError",
    "ScopeRecallHermesAdapter",
    "bind_hermes_identity",
    "build_archive_scope_id",
    "install_hermes_archive_migration",
    "install_hermes_scope_recall",
    "is_archive_scope",
    "load_installation_manifest",
    "register_adapter",
]


def __getattr__(name: str):
    # Install/identity must not import the host MemoryProvider.  Adapter
    # registration still resolves these names on first use.
    if name in {"ScopeRecallHermesAdapter", "register_adapter"}:
        from .provider import ScopeRecallHermesAdapter, register_adapter

        return ScopeRecallHermesAdapter if name == "ScopeRecallHermesAdapter" else register_adapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
