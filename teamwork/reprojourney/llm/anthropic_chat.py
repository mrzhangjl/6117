"""Anthropic Claude chat model adapter with native tool use blocks."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .types import BaseChatModel, ChatMessage, LLMResponse, ToolCall, ToolSpec


class AnthropicChatModel(BaseChatModel):
    """Thin adapter over ``anthropic.AsyncAnthropic`` (Messages API)."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 800,
        timeout: float = 60.0,
    ) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "未安装 anthropic SDK：请执行 `pip install anthropic`，或设置 REPROJOURNEY_LLM_PROVIDER=offline"
            ) from exc

        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # ``api_key=None`` falls back to the ANTHROPIC_API_KEY environment variable.
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)

    def _to_messages(self, messages: Sequence[ChatMessage]) -> List[Dict[str, Any]]:
        """Convert neutral messages into Anthropic content blocks.

        Consecutive same-role turns are merged because the Messages API expects
        alternating user/assistant turns.
        """

        converted: List[Dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue  # handled through the top-level ``system`` parameter

            if message.role == "tool":
                role = "user"
                blocks: List[Dict[str, Any]] = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                ]
            elif message.role == "assistant":
                role = "assistant"
                blocks = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments or {}}
                    )
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
            else:
                role = "user"
                blocks = [{"type": "text", "text": message.content or ""}]

            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})
        return converted

    @staticmethod
    def _to_tools(tools: Sequence[ToolSpec]) -> List[Dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters or {"type": "object", "properties": {}},
            }
            for spec in tools
        ]

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> LLMResponse:
        system_prompt = "\n\n".join(
            message.content for message in messages if message.role == "system" and message.content
        )

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": self._to_messages(messages),
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._to_tools(tools)

        response = await self._client.messages.create(**kwargs)

        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in response.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=dict(getattr(block, "input", None) or {}),
                    )
                )

        return LLMResponse(
            content="\n".join(part for part in text_parts if part),
            tool_calls=tool_calls,
            finish_reason=getattr(response, "stop_reason", None),
            model=self.model,
        )
