from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    age: Optional[int] = None
    basic_profile: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class Pregnancy(BaseModel):
    gestational_week: Optional[float] = None
    estimated_due_date: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class Report(BaseModel):
    report_id: Optional[str] = None
    report_type: Optional[str] = None
    report_date: Optional[str] = None
    extracted_data: Optional[Dict[str, Any]] = None
    source_file: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class Symptom(BaseModel):
    symptom: Optional[str] = None
    severity: Optional[str] = None
    timestamp: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class ConsultationRecord(BaseModel):
    query: Optional[str] = None
    response: Optional[str] = None
    evidence: List[str] = Field(default_factory=list)
    timestamp: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class RiskStatus(BaseModel):
    level: Literal["LOW", "MODERATE", "HIGH", "UNKNOWN"] = "UNKNOWN"
    reasons: List[str] = Field(default_factory=list)
    requires_human: bool = False
    model_config = ConfigDict(extra="forbid")


class Consent(BaseModel):
    allow_record_storage: Optional[bool] = None
    allow_external_escalation: Optional[bool] = None
    allow_family_notification: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class SharedUserState(BaseModel):
    user_id: str
    profile: Optional[Profile] = None
    pregnancy: Optional[Pregnancy] = None
    medical_history: List[str] = Field(default_factory=list)
    reports: List[Report] = Field(default_factory=list)
    symptoms: List[Symptom] = Field(default_factory=list)
    mood: Optional[str] = None
    appointments: List[Dict[str, Any]] = Field(default_factory=list)
    consultation_history: List[ConsultationRecord] = Field(default_factory=list)
    risk_status: Optional[RiskStatus] = None
    follow_up: Optional[Dict[str, Any]] = None
    consent: Optional[Consent] = None
    model_config = ConfigDict(extra="forbid")


class AuditEvent(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_id: str
    agent: str
    action: str
    data_accessed: List[str] = Field(default_factory=list)
    tool_used: Optional[str] = None
    source: List[str] = Field(default_factory=list)
    decision: str
    risk_level: Literal["LOW", "MODERATE", "HIGH", "UNKNOWN"] = "UNKNOWN"
    human_required: bool = False
    model_config = ConfigDict(extra="forbid")


class AgentInput(BaseModel):
    request_id: str
    user_id: str
    task: str
    user_message: Optional[str] = None
    state: SharedUserState
    context: Dict[str, Any] = Field(default_factory=dict)
    requested_by: Literal["user", "orchestrator"] | str = "user"
    model_config = ConfigDict(extra="forbid")


class AgentOutput(BaseModel):
    agent: str
    status: Literal["success", "needs_info", "escalate", "error"]
    summary: str
    evidence: List[str] = Field(default_factory=list)
    next_action: List[str] = Field(default_factory=list)
    risk_level: Literal["LOW", "MODERATE", "HIGH", "UNKNOWN"] = "UNKNOWN"
    requires_human: bool = False
    state_update: Dict[str, Any] = Field(default_factory=dict)
    audit_events: List[AuditEvent] = Field(default_factory=list)
    error: Optional[str] = None
    model_config = ConfigDict(extra="forbid")
