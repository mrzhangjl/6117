"""The ReproJourney Agent Development Standard v1.0, expressed as code.

A team agreement rots unless a machine can check it.  This module turns the
"every agent must ..." clauses of the standard into constants and pure
validator functions, shared by the agent base class, the test suite and the
documentation guard -- so there is exactly ONE definition of what a
ReproJourney agent is.

Section map:

  * section 2  shared user state first-level fields + required sub-fields;
  * section 3  unified agent input and the ``requested_by`` vocabulary;
  * section 4  unified agent output + the four special rules;
  * section 5  the closed risk-level vocabulary (four values, no synonyms);
  * section 6  audit-event fields and the "never log prompts/secrets" rule;
  * section 7  one uniform entry point per agent + the agent/tool slots.

Nothing here reads files, environment variables or the network: it is pure
data plus pure functions, importable from anywhere without side effects.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from reprojourney.schemas.base_schemas import (
    AgentInput,
    AgentOutput,
    AuditEvent,
    Consent,
    ConsultationRecord,
    Pregnancy,
    Profile,
    Report,
    RiskStatus,
    SharedUserState,
    Symptom,
)

STANDARD_VERSION = "1.0"

# --------------------------------------------------------------------------- section 2

#: First-level fields of the shared user state. Nobody may rename or add one:
#: ``SharedUserState`` is ``extra="forbid"``, so a typo is a hard error.
STATE_FIELDS: Tuple[str, ...] = (
    "user_id",
    "profile",
    "pregnancy",
    "medical_history",
    "reports",
    "symptoms",
    "mood",
    "appointments",
    "consultation_history",
    "risk_status",
    "follow_up",
    "consent",
)

#: Required sub-fields per section 2 ("unknown field -> null, never invent").
STATE_SUBFIELDS: Dict[str, Tuple[str, ...]] = {
    "profile": ("age", "basic_profile"),
    "pregnancy": ("gestational_week", "estimated_due_date"),
    "reports": ("report_id", "report_type", "report_date", "extracted_data", "source_file"),
    "symptoms": ("symptom", "severity", "timestamp"),
    "consultation_history": ("query", "response", "evidence", "timestamp"),
    "risk_status": ("level", "reasons", "requires_human"),
    "consent": ("allow_record_storage", "allow_external_escalation", "allow_family_notification"),
}

#: Model class behind every section 2 sub-field group.
STATE_MODELS = {
    "profile": Profile,
    "pregnancy": Pregnancy,
    "reports": Report,
    "symptoms": Symptom,
    "consultation_history": ConsultationRecord,
    "risk_status": RiskStatus,
    "consent": Consent,
}

# --------------------------------------------------------------------------- section 3

INPUT_FIELDS: Tuple[str, ...] = (
    "request_id",
    "user_id",
    "task",
    "user_message",
    "state",
    "context",
    "requested_by",
)

#: ``requested_by`` is "user | orchestrator | agent_name".
REQUESTED_BY_USER = "user"
REQUESTED_BY_ORCHESTRATOR = "orchestrator"

#: Callable identities allowed to invoke an agent (their ``AgentOutput.agent``).
KNOWN_AGENTS: Tuple[str, ...] = (
    "orchestrator",
    "main_agent",
    "risk_assessment_agent",
    "governance_audit_agent",
)

# --------------------------------------------------------------------------- sections 4 and 5

OUTPUT_FIELDS: Tuple[str, ...] = (
    "agent",
    "status",
    "summary",
    "evidence",
    "next_action",
    "risk_level",
    "requires_human",
    "state_update",
    "audit_events",
    "error",
)

AGENT_STATUSES: Tuple[str, ...] = ("success", "needs_info", "escalate", "error")

#: The only four risk levels the whole group may use.
RISK_LEVELS: Tuple[str, ...] = ("LOW", "MODERATE", "HIGH", "UNKNOWN")

#: Words the standard explicitly forbids as risk levels (they used to leak in
#: from ad-hoc UI colours / triage slang).  Agent 5's finding severity
#: (``critical`` / ``warning``) is a *different, orthogonal* axis -- compliance
#: severity, not clinical risk -- so it is deliberately not listed here.
FORBIDDEN_RISK_WORDS: Tuple[str, ...] = (
    "amber",
    "green",
    "yellow",
    "urgent",
    "severe",
    "maybe-risk",
    "maybe_risk",
)

# --------------------------------------------------------------------------- section 6

AUDIT_EVENT_FIELDS: Tuple[str, ...] = (
    "timestamp",
    "request_id",
    "agent",
    "action",
    "data_accessed",
    "tool_used",
    "source",
    "decision",
    "risk_level",
    "human_required",
)

#: Never allowed inside an audit ``decision`` (section 6: no prompts, keys or
#: passwords).  Kept in sync with Agent 5's own privacy rule ``G-PRV-01``.
AUDIT_FORBIDDEN_TOKENS: Tuple[str, ...] = ("password", "api_key", "apikey", "secret")

# --------------------------------------------------------------------------- section 7

#: Standard directory slots -> where this repository keeps them.
#: Names match the standard's tree one-to-one (see ``docs/AGENT_STANDARD.md``).
AGENT_SLOTS: Dict[str, str] = {
    "orchestrator": "reprojourney/agents/orchestrator",
    "health_record": "reprojourney/agents/health_record",
    "consultant": "reprojourney/agents/consultant",
    "risk": "reprojourney/agents/risk",
    "governance": "reprojourney/agents/governance",
}

#: The standard's tool layer (section 7/8): deterministic capabilities the
#: agents call. Kept next to ``agents/`` instead of inside an agent package.
TOOL_SLOT = "reprojourney/tools"

#: Slots the standard asks for but this repository has not built yet.  They are
#: declared here instead of being silently missing (see ``docs/AGENT_STANDARD.md``).
PENDING_SLOTS: Tuple[str, ...] = ("health_record", "consultant")

#: Imports that would turn an agent into its own app / reach the outside world
#: (section 7 forbids both: "no own web server", "no real hospital calls").
FORBIDDEN_WEB_FRAMEWORKS: Tuple[str, ...] = (
    "fastapi",
    "flask",
    "django",
    "uvicorn",
    "starlette",
    "bottle",
    "tornado",
    "aiohttp",
    "httpx",
    "requests",
    "urllib.request",
    "smtplib",
    "twilio",
)

#: The conceptual entry point of every agent (section 7).
ENTRY_POINT = "run(agent_input, state)"


class AgentContractError(ValueError):
    """Raised when an agent input or output breaks the standard."""


# --------------------------------------------------------------------------- schema shape


def _field_problems(label: str, model, expected: Tuple[str, ...], *, all_optional: bool) -> List[str]:
    """Compare a pydantic model's fields with the field list the standard names."""

    problems: List[str] = []
    actual = tuple(model.model_fields)
    for name in expected:
        if name not in actual:
            problems.append(f"{label} 缺少标准字段 {name!r}")
    for name in actual:
        if name not in expected:
            problems.append(f"{label} 多出标准之外的字段 {name!r}")
    if all_optional:
        for name in expected:
            field = model.model_fields.get(name)
            if field is not None and field.is_required():
                problems.append(f"{label}.{name} 必须可空/有默认值（标准：不知道的字段用 null）")
    if model.model_config.get("extra") != "forbid":
        problems.append(f"{label} 必须 extra='forbid'（禁止自行改名或新增字段）")
    return problems


def _literal_values(annotation) -> Tuple[str, ...]:
    return tuple(getattr(annotation, "__args__", ()))


def state_schema_problems() -> List[str]:
    """Section 2: shape of ``SharedUserState`` and each sub-model."""

    problems = _field_problems("SharedUserState", SharedUserState, STATE_FIELDS, all_optional=False)
    if not SharedUserState.model_fields["user_id"].is_required():
        problems.append("SharedUserState.user_id 必须是必填字段")
    for group, expected in STATE_SUBFIELDS.items():
        problems.extend(_field_problems(group, STATE_MODELS[group], expected, all_optional=True))
    return problems


def input_schema_problems() -> List[str]:
    """Section 3: shape of ``AgentInput``."""

    return _field_problems("AgentInput", AgentInput, INPUT_FIELDS, all_optional=False)


def output_schema_problems() -> List[str]:
    """Section 4: shape of ``AgentOutput``."""

    problems = _field_problems("AgentOutput", AgentOutput, OUTPUT_FIELDS, all_optional=False)
    statuses = _literal_values(AgentOutput.model_fields["status"].annotation)
    if set(statuses) != set(AGENT_STATUSES):
        problems.append(f"AgentOutput.status 应恰为 {list(AGENT_STATUSES)}，实际 {list(statuses)}")
    return problems


def risk_level_schema_problems() -> List[str]:
    """Section 5: every risk-level field is the same closed four-value set."""

    problems: List[str] = []
    for label, annotation in (
        ("AgentOutput.risk_level", AgentOutput.model_fields["risk_level"].annotation),
        ("AuditEvent.risk_level", AuditEvent.model_fields["risk_level"].annotation),
        ("RiskStatus.level", RiskStatus.model_fields["level"].annotation),
    ):
        values = _literal_values(annotation)
        if set(values) != set(RISK_LEVELS):
            problems.append(f"{label} 应恰为 {list(RISK_LEVELS)}，实际 {list(values)}")
    return problems


def audit_schema_problems() -> List[str]:
    """Section 6: shape of ``AuditEvent``."""

    return _field_problems("AuditEvent", AuditEvent, AUDIT_EVENT_FIELDS, all_optional=False)


def standard_problems() -> List[str]:
    """All schema-shape checks above, in one call (used by the test suite)."""

    return (
        state_schema_problems()
        + input_schema_problems()
        + output_schema_problems()
        + risk_level_schema_problems()
        + audit_schema_problems()
    )


# --------------------------------------------------------------------------- semantic rules


def requested_by_problem(value: str) -> Optional[str]:
    """Section 3: ``requested_by`` is user, orchestrator or an agent name."""

    if value in {REQUESTED_BY_USER, REQUESTED_BY_ORCHESTRATOR} or value in KNOWN_AGENTS:
        return None
    return f"requested_by={value!r} 非法（只允许 user / orchestrator / agent_name）"


def audit_event_problems(event: AuditEvent) -> List[str]:
    """Section 6: one audit event must be complete and leak-free."""

    problems: List[str] = []
    if not event.decision.strip():
        problems.append(f"{event.agent}.{event.action}: 审计 decision 不能为空")
    if not event.action.strip():
        problems.append(f"{event.agent}: 审计 action 不能为空")
    if event.risk_level not in RISK_LEVELS:
        problems.append(f"{event.agent}.{event.action}: 审计 risk_level={event.risk_level!r} 不在四值闭集内")
    lowered = event.decision.lower()
    for token in AUDIT_FORBIDDEN_TOKENS:
        if token in lowered:
            problems.append(f"{event.agent}.{event.action}: 审计 decision 疑似记录了敏感信息（{token}）")
    return problems


def check_output_contract(
    output: AgentOutput,
    *,
    risk_duty: bool,
    state_fields: Tuple[str, ...] = STATE_FIELDS,
    request_id: Optional[str] = None,
) -> List[str]:
    """Sections 4/5/6 checked against one ``AgentOutput``; ``[]`` means compliant.

    ``risk_duty`` is the only legitimate reason for an agent to report a
    clinical tier: Agent 4 owns the deterministic rule engine, Agent 5 owns
    compliance.  An agent without that duty must answer ``UNKNOWN``.

    ``request_id`` is optional: when given, every audit event must carry it
    (section 6, "one request -- one trail").
    """

    problems: List[str] = []
    label = output.agent or "<unnamed agent>"

    if not output.agent.strip():
        problems.append("output.agent 不能为空")
    if output.status not in AGENT_STATUSES:
        problems.append(f"{label}: status={output.status!r} 不在 {list(AGENT_STATUSES)} 内")
    if output.risk_level not in RISK_LEVELS:
        problems.append(f"{label}: risk_level={output.risk_level!r} 不在 {list(RISK_LEVELS)} 内")
    if not output.summary.strip():
        problems.append(f"{label}: summary 必须给出人类可读结论")

    # Section 4 special rules.
    if not risk_duty and output.risk_level != "UNKNOWN":
        problems.append(
            f"{label}: 没有风险判断职责的 Agent 必须 risk_level='UNKNOWN'，实际 {output.risk_level!r}"
        )
    if output.risk_level == "HIGH":
        if not output.requires_human:
            problems.append(f"{label}: HIGH 必须 requires_human=true")
        if risk_duty and output.status != "escalate":
            problems.append(f"{label}: HIGH 必须 status='escalate'，实际 {output.status!r}")
    if output.status == "escalate" and not output.requires_human:
        problems.append(f"{label}: status='escalate' 必须 requires_human=true")
    if output.status == "error" and not output.error:
        problems.append(f"{label}: status='error' 必须填 error 说明")
    if output.status != "error" and output.error:
        problems.append(f"{label}: 非 error 状态不应携带 error 文本")
    if risk_duty and output.risk_level == "UNKNOWN" and output.status not in {"needs_info", "error"}:
        problems.append(f"{label}: 不确定时禁止猜，status 应为 'needs_info'，实际 {output.status!r}")

    unknown_keys = sorted(set(output.state_update) - set(state_fields))
    if unknown_keys:
        problems.append(f"{label}: state_update 出现非一级字段 {unknown_keys}（section 2 不允许新字段）")
    shipped = output.state_update.get("risk_status")
    if isinstance(shipped, dict) and shipped.get("level") != output.risk_level:
        problems.append(
            f"{label}: state_update['risk_status']['level']={shipped.get('level')!r} 与 "
            f"output.risk_level={output.risk_level!r} 不一致"
        )

    if not output.audit_events:
        problems.append(f"{label}: 每次运行都必须留下审计事件（section 6）")
    for event in output.audit_events:
        problems.extend(audit_event_problems(event))
        if event.agent != output.agent:
            problems.append(f"{label}: 审计事件的 agent={event.agent!r} 与 output.agent 不一致")
        if request_id is not None and event.request_id != request_id:
            problems.append(f"{label}: 审计事件的 request_id={event.request_id!r} 与本次请求 {request_id!r} 不一致")
        if event.risk_level != output.risk_level:
            problems.append(
                f"{label}: 审计事件 risk_level={event.risk_level!r} 与 "
                f"output.risk_level={output.risk_level!r} 不一致"
            )
    return problems


def assert_output_contract(
    output: AgentOutput, *, risk_duty: bool, request_id: Optional[str] = None
) -> AgentOutput:
    """Raise ``AgentContractError`` unless the output satisfies sections 4-6."""

    problems = check_output_contract(output, risk_duty=risk_duty, request_id=request_id)
    if problems:
        raise AgentContractError("AgentOutput 违反开发标准：" + "；".join(problems))
    return output
