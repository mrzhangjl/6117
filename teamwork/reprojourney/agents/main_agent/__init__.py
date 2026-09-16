"""ReAct main agent package.

Layout:
  * :mod:`prompts`     -- system prompt and HITL message templates;
  * :mod:`session`     -- session state, HITL checkpoint, state merge, persistence;
  * :mod:`agent`       -- the ReAct loop, HITL pause/resume and the CLI.

The audited tool layer lives one level up, next to the agents
(``reprojourney/tools/registry.py``), per standard section 7/8.
"""

from .agent import VALID_ACTIONS, VALID_LEVELS, MainAgent, TurnResult, run_cli_session
from .session import SessionState, SessionStore, apply_state_update, redact
from reprojourney.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult, build_default_registry

#: Backwards-compatible alias (the previous implementation was a single module
#: with the same public entry point).
ReActMainAgent = MainAgent

__all__ = [
    "MainAgent",
    "ReActMainAgent",
    "SessionState",
    "SessionStore",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "TurnResult",
    "VALID_ACTIONS",
    "VALID_LEVELS",
    "apply_state_update",
    "build_default_registry",
    "redact",
    "run_cli_session",
]
