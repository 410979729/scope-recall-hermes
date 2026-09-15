"""Public MemoryProvider signatures for offline host tests without importing Hermes."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class PublicMemoryProvider(ABC):
    """Mirror Hermes 0.21.0 MemoryProvider public surface used by this adapter slice."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None: ...

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        return None

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        return None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        return None

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        return ""

    def shutdown(self) -> None:
        return None

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]: ...
