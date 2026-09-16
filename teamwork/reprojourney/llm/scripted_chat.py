"""Scripted chat model -- deterministic provider for tests and evaluation.

The ReAct loop must be testable without network access and without depending on
a real model's wording. ``ScriptedChatModel`` replays a fixed script of
assistant turns (thoughts plus tool calls) and records every transcript it
receives, so tests can assert *what the agent observed* rather than what a
model happened to say.

It is a real :class:`BaseChatModel`, so the loop, tools, HITL gate and audit
trail are exercised exactly as in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .types import BaseChatModel, ChatMessage, LLMResponse, ToolCall, ToolSpec


@dataclass
class ScriptedTurn:
    """One canned assistant turn."""

    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    def to_response(self, index: int, model: str) -> LLMResponse:
        calls = [
            ToolCall(
                id=str(item.get("id") or f"script-{index}-{position}"),
                name=str(item.get("name") or ""),
                arguments=dict(item.get("arguments") or {}),
            )
            for position, item in enumerate(self.tool_calls)
        ]
        return LLMResponse(
            content=self.content,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            model=model,
        )


class ScriptedChatModel(BaseChatModel):
    """Replay a script; after it is exhausted, return ``fallback``."""

    name = "scripted"

    def __init__(
        self,
        script: Optional[Sequence[ScriptedTurn]] = None,
        fallback: str = "（脚本已执行完毕）",
        fallback_tool_calls: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        self.script: List[ScriptedTurn] = list(script or [])
        self.fallback = fallback
        self.fallback_tool_calls = list(fallback_tool_calls or [])
        self.calls: List[List[ChatMessage]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        index = len(self.calls) - 1
        if index < len(self.script):
            return self.script[index].to_response(index, self.name)
        return ScriptedTurn(content=self.fallback, tool_calls=list(self.fallback_tool_calls)).to_response(
            index, self.name
        )

    # -- assertions helpers -------------------------------------------------
    @property
    def last_transcript(self) -> List[ChatMessage]:
        return self.calls[-1] if self.calls else []

    def tool_messages(self, call_index: int = -1) -> List[ChatMessage]:
        transcript = self.calls[call_index] if self.calls else []
        return [message for message in transcript if message.role == "tool"]

    def assistant_messages(self, call_index: int = -1) -> List[ChatMessage]:
        transcript = self.calls[call_index] if self.calls else []
        return [message for message in transcript if message.role == "assistant"]


def turn(content: str = "", **tool_calls: Any) -> ScriptedTurn:
    """Convenience builder: ``turn("thought", assess_risk={"reason": "x"})``."""

    return ScriptedTurn(
        content=content,
        tool_calls=[{"name": name, "arguments": arguments or {}} for name, arguments in tool_calls.items()],
    )
