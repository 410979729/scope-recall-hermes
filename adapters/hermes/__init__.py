"""Bounded Hermes MemoryProvider adapter over the host-independent core."""

from .gating import is_trivial_prompt
from .identity import HermesIdentityError, bind_hermes_identity
from .installation import (
    build_archive_scope_id,
    install_hermes_scope_recall,
    is_archive_scope,
    load_installation_manifest,
)

__all__ = [
    "HermesIdentityError",
    "ScopeRecallHermesAdapter",
    "bind_hermes_identity",
    "build_archive_scope_id",
    "install_hermes_scope_recall",
    "is_archive_scope",
    "is_trivial_prompt",
    "load_installation_manifest",
    "register_adapter",
]


def __getattr__(name: str):
    # Install/identity must not import the host MemoryProvider.  Adapter
    # registration still resolves these names on first use.
    if name == "ScopeRecallHermesAdapter":
        from .provider import ScopeRecallHermesAdapter

        return ScopeRecallHermesAdapter
    if name == "register_adapter":
        from .register import register

        return register
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
