"""Session state, HITL checkpoint and local persistence for the main agent.

State ownership rule (from the ReproJourney standard): **only the orchestrator
merges** ``state_update`` into ``SharedUserState``. In a chat session the main
agent *is* the orchestrator, so every state patch -- from tools or from
sub-agents such as Agent 4 -- goes through :func:`apply_state_update`. Tools
never mutate the shared state themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from reprojourney.schemas.base_schemas import AuditEvent, SharedUserState

#: Keys whose values must never be written to a local snapshot.
REDACT_KEYS = ("api_key", "apikey", "password", "secret", "token", "authorization", "auth")


def apply_state_update(state: SharedUserState, update: Dict[str, Any]) -> SharedUserState:
    """Merge a ``state_update`` patch into ``SharedUserState`` (orchestrator role).

    * unknown keys are ignored (agents may ship forward-compatible fields);
    * ``list`` + ``list`` appends (symptoms, reports, history ...);
    * ``dict`` + ``dict`` merges field-wise (pregnancy, consent, follow_up);
    * anything else replaces.
    """

    data = state.model_dump(mode="python")
    for key, value in update.items():
        if key not in data:
            continue
        current = data.get(key)
        if isinstance(current, list) and isinstance(value, list):
            current.extend(value)
        elif isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        else:
            data[key] = value
    return SharedUserState.model_validate(data)


def redact(payload: Any) -> Any:
    """Recursively redact credential-looking keys before persisting/printing."""

    if isinstance(payload, dict):
        redacted: Dict[str, Any] = {}
        for key, value in payload.items():
            if any(token in str(key).lower() for token in REDACT_KEYS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


@dataclass
class SessionState:
    """Everything the main agent carries between turns."""

    user_id: str
    state: SharedUserState
    audit_events: List[AuditEvent] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    consent_denied_writes: int = 0
    turn_index: int = 0
    active_risk: Optional[Dict[str, Any]] = None
    human_review: Optional[Dict[str, Any]] = None
    pending_human: Optional[Dict[str, Any]] = None

    @classmethod
    def new(cls, user_id: str = "demo-user") -> "SessionState":
        from reprojourney.schemas.base_schemas import Consent, Pregnancy, Profile

        return cls(
            user_id=user_id,
            state=SharedUserState(
                user_id=user_id,
                profile=Profile(age=30, basic_profile="pregnant patient"),
                pregnancy=Pregnancy(gestational_week=None),
                symptoms=[],
                medical_history=[],
                reports=[],
                consent=Consent(
                    allow_record_storage=True,
                    allow_external_escalation=True,
                    allow_family_notification=False,
                ),
            ),
        )

    @property
    def risk_level(self) -> str:
        return self.state.risk_status.level if self.state.risk_status else "UNKNOWN"

    def snapshot_dict(self) -> Dict[str, Any]:
        """Serialisable, redacted view of the session (no chat transcript).

        The pending-human payload is intentionally excluded: it contains the raw
        conversation, which should not be duplicated into a local file unless a
        dedicated consent flow covers it.
        """

        return redact(
            {
                "user_id": self.user_id,
                "turn_index": self.turn_index,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "state": self.state.model_dump(mode="json"),
                "risk_level": self.risk_level,
                "active_risk": self.active_risk,
                "human_review": self.human_review,
                "tool_calls": self.tool_calls,
                "audit_events": [event.model_dump(mode="json") for event in self.audit_events],
            }
        )


class SessionStore:
    """Minimal local JSON store for session snapshots (relative ``run_dir``)."""

    def __init__(self, run_dir: str = "runs", enabled: bool = True) -> None:
        self.run_dir = Path(run_dir)
        self.enabled = enabled

    def save(self, session: SessionState) -> Optional[Path]:
        if not self.enabled:
            return None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = self.run_dir / f"{stamp}-{session.user_id}-turn{session.turn_index}.json"
        path.write_text(
            json.dumps(session.snapshot_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def latest_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        if not self.run_dir.exists():
            return None
        candidates = sorted(self.run_dir.glob(f"*-{user_id}-turn*.json"))
        if not candidates:
            return None
        try:
            return json.loads(candidates[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
