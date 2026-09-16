"""Chat model factory.

Selection order:
  1. ``REPROJOURNEY_LLM_PROVIDER`` (``openai`` / ``anthropic`` / ``offline``)
  2. auto-detect from available credentials
  3. deterministic offline policy model

Any provider initialisation error (missing SDK, missing key, bad base URL)
degrades to the offline model instead of crashing, so agent behaviour stays
observable rather than fatal.
"""

from __future__ import annotations

import sys
from typing import Optional

from reprojourney.config import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OFFLINE,
    PROVIDER_OPENAI,
    Settings,
)

from .anthropic_chat import AnthropicChatModel
from .offline_chat import OfflinePolicyChatModel
from .openai_chat import OpenAIChatModel
from .types import BaseChatModel


def build_chat_model(settings: Optional[Settings] = None) -> BaseChatModel:
    """Return the configured chat model, falling back to the offline policy."""

    settings = settings or Settings.from_env()

    if settings.provider == PROVIDER_OFFLINE:
        return OfflinePolicyChatModel()

    try:
        if settings.provider == PROVIDER_OPENAI:
            return OpenAIChatModel(
                model=settings.model,
                api_key=settings.api_key,
                base_url=settings.base_url,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
                timeout=settings.request_timeout,
            )
        if settings.provider == PROVIDER_ANTHROPIC:
            return AnthropicChatModel(
                model=settings.model,
                api_key=settings.api_key,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
                timeout=settings.request_timeout,
            )
    except Exception as exc:  # pragma: no cover - environment dependent
        print(
            f"[ReproJourney] {settings.provider} 模型初始化失败：{exc}。"
            "已回退到离线确定性策略模型（行为、HITL 与审计链路保持一致）。",
            file=sys.stderr,
        )
        return OfflinePolicyChatModel()

    print(
        f"[ReproJourney] 未知的 provider={settings.provider}，已回退到离线确定性策略模型。",
        file=sys.stderr,
    )
    return OfflinePolicyChatModel()
