"""Tool layer (standard section 7/8): the deterministic capabilities Agents call.

Why this package exists
-----------------------
The standard asks for a ``tools/`` layer next to ``agents/`` so that an agent is
never "the OCR" or "the RAG": the agent decides *what* to do, the tool does the
deterministic work, and the result comes back as an observation plus audit
events.  Everything a tool may touch (data access, timeouts, audit) is defined
in :mod:`reprojourney.tools.registry`.

Import style
------------
Import the concrete module, ``from reprojourney.tools.registry import ToolRegistry``.
Names are re-exported lazily below so ``from reprojourney.tools import
ToolRegistry`` also works without creating an import cycle between
``reprojourney.tools`` and ``reprojourney.agents``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import (  # noqa: F401
        Tool,
        ToolContext,
        ToolRegistry,
        ToolResult,
        build_default_registry,
    )

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy re-export: keeps ``tools`` ↔ ``agents`` free of import cycles."""

    if name in __all__:
        from . import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
