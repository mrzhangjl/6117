"""Provider-agnostic chat model contract used by the ReAct loop.

The main agent talks to *any* model through :class:`BaseChatModel`. Two real
providers (OpenAI / Anthropic) and one deterministic offline policy model ship
with the project. Keeping this contract small is what makes the agent loop
identical in offline and online mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class ToolSpec:
    """Model-facing description of a callable tool (JSON-schema based)."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            arguments=dict(payload.get("arguments") or {}),
        )


@dataclass
class ChatMessage:
    """A provider-neutral conversation message.

    ``role`` is one of ``system`` / ``user`` / ``assistant`` / ``tool``.
    Assistant messages may carry ``tool_calls``; ``tool`` messages carry the
    matching ``tool_call_id`` plus the observation text.
    """

    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Optional[List[ToolCall]] = None) -> "ChatMessage":
        return cls(role="assistant", content=content, tool_calls=list(tool_calls or []))

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> "ChatMessage":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ChatMessage":
        return cls(
            role=str(payload.get("role") or "user"),
            content=str(payload.get("content") or ""),
            tool_calls=[ToolCall.from_dict(item) for item in payload.get("tool_calls") or []],
            tool_call_id=payload.get("tool_call_id"),
            name=payload.get("name"),
        )


@dataclass
class LLMResponse:
    """Normalised model response."""

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    model: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class BaseChatModel(ABC):
    """Minimal async chat-completion interface with optional tool calling."""

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> LLMResponse:
        """Return the next assistant turn for ``messages``."""
        raise NotImplementedError
