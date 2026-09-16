"""Runtime configuration for ReproJourney agents.

Rules honoured here:
  * Secrets never live in code. Keys are read from environment variables,
    optionally hydrated from a ``.env`` file at the project root (git-ignored).
  * No absolute paths. ``run_dir`` is always a *relative* directory name; the
    process working directory decides where it resolves to.
  * When no provider credentials are available the system degrades to the
    deterministic offline policy model, so the agent loop always runs
    (offline mode exercises exactly the same ReAct loop, tools, HITL gate and
    governance gate as the online mode).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OFFLINE = "offline"

SUPPORTED_PROVIDERS = (PROVIDER_OPENAI, PROVIDER_ANTHROPIC, PROVIDER_OFFLINE)

DEFAULT_MODELS = {
    PROVIDER_OPENAI: "gpt-4o-mini",
    PROVIDER_ANTHROPIC: "claude-3-5-sonnet-latest",
    PROVIDER_OFFLINE: "offline-policy-v1",
}


def _load_env_file() -> None:
    """Hydrate ``os.environ`` from a ``.env`` file found in the project tree.

    ``python-dotenv`` searches upwards from this file, so a ``.env`` placed at
    the repository root is picked up without hard-coding any path. The function
    is a no-op when the optional dependency is missing or no file exists.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:  # optional dependency
        return
    load_dotenv(override=False)


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings (already sanitised / defaulted)."""

    provider: str = PROVIDER_OFFLINE
    model: str = DEFAULT_MODELS[PROVIDER_OFFLINE]
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 800
    request_timeout: float = 60.0
    max_retries: int = 1
    retry_backoff: float = 0.8
    max_react_steps: int = 8
    tool_timeout: float = 20.0
    run_dir: str = "runs"
    persist_sessions: bool = True
    governance_gate: bool = True

    @property
    def is_offline(self) -> bool:
        """True when the deterministic policy model is used instead of a real LLM."""

        return self.provider == PROVIDER_OFFLINE

    def describe(self) -> str:
        key_state = "configured" if self.api_key else "not configured"
        return (
            f"provider={self.provider}, model={self.model}, api_key={key_state}, "
            f"max_react_steps={self.max_react_steps}, run_dir={self.run_dir}"
        )

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables / ``.env`` file."""

        _load_env_file()

        openai_key = _env("OPENAI_API_KEY")
        anthropic_key = _env("ANTHROPIC_API_KEY")

        provider = (_env("REPROJOURNEY_LLM_PROVIDER") or "").lower()
        if provider not in SUPPORTED_PROVIDERS:
            if openai_key:
                provider = PROVIDER_OPENAI
            elif anthropic_key:
                provider = PROVIDER_ANTHROPIC
            else:
                provider = PROVIDER_OFFLINE

        if provider == PROVIDER_OPENAI:
            api_key = openai_key
        elif provider == PROVIDER_ANTHROPIC:
            api_key = anthropic_key
        else:
            api_key = None

        model = _env("REPROJOURNEY_LLM_MODEL") or DEFAULT_MODELS[provider]
        base_url = _env("REPROJOURNEY_LLM_BASE_URL") or _env("OPENAI_BASE_URL")

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=_env_float("REPROJOURNEY_LLM_TEMPERATURE", 0.2),
            max_tokens=_env_int("REPROJOURNEY_LLM_MAX_TOKENS", 800),
            request_timeout=_env_float("REPROJOURNEY_LLM_TIMEOUT", 60.0),
            max_retries=_env_int("REPROJOURNEY_LLM_MAX_RETRIES", 1),
            retry_backoff=_env_float("REPROJOURNEY_LLM_BACKOFF", 0.8),
            max_react_steps=_env_int("REPROJOURNEY_MAX_REACT_STEPS", 8),
            tool_timeout=_env_float("REPROJOURNEY_TOOL_TIMEOUT", 20.0),
            run_dir=_env("REPROJOURNEY_RUN_DIR") or "runs",
            persist_sessions=_env_bool("REPROJOURNEY_PERSIST_SESSIONS", True),
            governance_gate=_env_bool("REPROJOURNEY_GOVERNANCE_GATE", True),
        )
