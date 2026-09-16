"""OpenAI (and OpenAI-compatible) chat model adapter with native tool calling."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from .types import BaseChatModel, ChatMessage, LLMResponse, ToolCall, ToolSpec


class OpenAIChatModel(BaseChatModel):
    """Thin adapter over ``openai.AsyncOpenAI``.

    The SDK is imported lazily so that the repository still imports cleanly
    when the optional dependency is not installed.
    """

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 800,
        timeout: float = 60.0,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "未安装 openai SDK：请执行 `pip install openai`，或设置 REPROJOURNEY_LLM_PROVIDER=offline"
            ) from exc

        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # ``api_key=None`` falls back to the OPENAI_API_KEY environment variable.
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    @staticmethod
    def _to_messages(messages: Sequence[ChatMessage]) -> List[Dict[str, Any]]:
        payload: List[Dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                payload.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                )
            elif message.role == "assistant":
                item: Dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ]
                payload.append(item)
            else:
                payload.append({"role": message.role, "content": message.content or ""})
        return payload

    @staticmethod
    def _to_tools(tools: Sequence[ToolSpec]) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters or {"type": "object", "properties": {}},
                },
            }
            for spec in tools
        ]

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> LLMResponse:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": self._to_messages(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            kwargs["tools"] = self._to_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        tool_calls: List[ToolCall] = []
        for item in message.tool_calls or []:
            raw_arguments = item.function.arguments or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except ValueError:
                arguments = {"_raw": raw_arguments}
            tool_calls.append(ToolCall(id=item.id, name=item.function.name, arguments=arguments))

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            model=self.model,
        )
