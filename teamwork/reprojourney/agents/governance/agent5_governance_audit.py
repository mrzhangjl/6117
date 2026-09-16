from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from reprojourney.agents.base import BaseAgent, StateLike
from reprojourney.schemas.base_schemas import AgentInput, AgentOutput, AuditEvent

AGENT_NAME = "governance_audit_agent"
POLICY_VERSION = "governance-audit/v1.0"

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

COMPLIANT = "COMPLIANT"
COMPLIANT_WITH_WARNINGS = "COMPLIANT_WITH_WARNINGS"
NON_COMPLIANT = "NON_COMPLIANT"

RISK_AUDIT_ACTIONS = {"run_maternity_risk_evaluation", "assess_risk"}
HUMAN_REVIEW_ACTIONS = {"human_risk_review", "request_human_review"}

REQUIRED_AUDIT_FIELDS = ("request_id", "agent", "action", "decision", "timestamp")

SENSITIVE_TOKENS = ("password", "api_key", "apikey", "secret", "token", "身份证", "银行卡")

#: Least-privilege data domains. ``data_accessed`` outside this map is a finding.
AGENT_DATA_SCOPE: Dict[str, set] = {
    "risk_assessment_agent": {
        "pregnancy",
        "symptoms",
        "reports",
        "medical_history",
        "follow_up",
        "shared_user_state",
    },
    "governance_audit_agent": {"risk_status", "consent", "audit_events", "shared_user_state"},
    "orchestrator": {"risk_status", "audit_events", "human_review", "shared_user_state"},
    "main_agent": {
        "risk_status",
        "audit_events",
        "human_review",
        "shared_user_state",
        "user_message",
    },
    "session_state": {"shared_user_state", "user_message"},
    "local_filesystem": {"shared_user_state", "audit_events", "consent"},
}


@dataclass
class GovernanceFinding:
    """One policy violation or observation."""

    rule_id: str
    severity: str
    message: str
    subject: str = ""

    @property
    def is_critical(self) -> bool:
        return self.severity == SEVERITY_CRITICAL

    def format(self) -> str:
        prefix = f"{self.rule_id}｜" if self.rule_id else ""
        return f"[{self.severity}] {prefix}{self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
        }


def _as_event(event: Any) -> Optional[AuditEvent]:
    """Accept ``AuditEvent`` objects or plain dicts (e.g. from JSON payloads)."""

    if isinstance(event, AuditEvent):
        return event
    if isinstance(event, dict):
        try:
            return AuditEvent.model_validate(event)
        except Exception:
            return None
    return None


class GovernanceAuditAgent(BaseAgent):
    """Agent 5 -- governance & audit gate for the maternity assistant.

    Responsibilities (per the ReproJourney standard):
      * audit-trail completeness: every risk decision must be traceable;
      * consent / authorisation checks before any record storage or escalation;
      * privacy compliance: no secrets or out-of-scope data domains in audit;
      * HITL enforcement: a ``human_required`` decision must have a review record;
      * a single compliance verdict consumed by the orchestrator.

    Verdicts:
      ``critical`` findings  -> ``NON_COMPLIANT``, ``status="needs_info"``,
      ``requires_human=True``;
      only ``warning`` findings -> ``COMPLIANT_WITH_WARNINGS`` (``success``);
      nothing -> ``COMPLIANT`` (``success``).

    ``risk_level`` is always ``"UNKNOWN"``: standard section 4 says an agent
    without a risk-judgement duty must not report a clinical tier, and Agent 5's
    axis is *compliance*, not clinical risk.  The verdict travels in
    ``compliance_status`` + ``G-*`` rule ids, and the deterministic tier stays
    owned by Agent 4.  Entry point: ``run(agent_input, state=None)``.
    """

    agent_name = AGENT_NAME
    task = "run_governance_audit"
    risk_duty = False

    def __init__(self, policy_version: str = POLICY_VERSION) -> None:
        self.policy_version = policy_version

    # -- individual policy checks ------------------------------------------
    def _check_audit_trail(self, state, events: Sequence[AuditEvent]) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        if state.risk_status is not None and not events:
            findings.append(
                GovernanceFinding(
                    "G-AUD-01",
                    SEVERITY_CRITICAL,
                    "存在风险判定但审计链路为空：缺少留痕",
                    subject="audit_trail",
                )
            )
        if state.risk_status is not None and not any(
            event.action in RISK_AUDIT_ACTIONS for event in events
        ):
            findings.append(
                GovernanceFinding(
                    "G-GOV-01",
                    SEVERITY_CRITICAL,
                    "风险判定缺少对应的风险评估审计事件",
                    subject="risk_status",
                )
            )
        return findings

    def _check_event_completeness(self, events: Sequence[AuditEvent]) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        for event in events:
            missing = [field for field in REQUIRED_AUDIT_FIELDS if not getattr(event, field, None)]
            if missing:
                findings.append(
                    GovernanceFinding(
                        "G-AUD-02",
                        SEVERITY_CRITICAL,
                        f"审计事件字段不完整（缺少 {', '.join(missing)}）：{event.action}",
                        subject=event.action,
                    )
                )
            if not event.data_accessed:
                findings.append(
                    GovernanceFinding(
                        "G-AUD-03",
                        SEVERITY_WARNING,
                        f"审计事件缺少 data_accessed：{event.action}",
                        subject=event.action,
                    )
                )
        return findings

    def _check_sensitive_data(self, events: Sequence[AuditEvent]) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        for event in events:
            haystack = " ".join(
                [
                    str(event.decision),
                    str(event.action),
                    " ".join(str(source) for source in event.source),
                    " ".join(str(item) for item in event.data_accessed),
                ]
            ).lower()
            if any(token in haystack for token in SENSITIVE_TOKENS):
                findings.append(
                    GovernanceFinding(
                        "G-PRV-01",
                        SEVERITY_CRITICAL,
                        f"审计事件存在敏感信息泄露风险：{event.action}",
                        subject=event.action,
                    )
                )
        return findings

    def _check_data_scope(self, events: Sequence[AuditEvent]) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        for event in events:
            scope = AGENT_DATA_SCOPE.get(event.agent)
            if scope is None:
                continue
            unauthorised = [item for item in event.data_accessed if item not in scope]
            if unauthorised:
                findings.append(
                    GovernanceFinding(
                        "G-PRV-02",
                        SEVERITY_CRITICAL,
                        f"审计事件访问了未授权数据域：{', '.join(str(item) for item in unauthorised)}",
                        subject=event.action,
                    )
                )
        return findings

    def _check_request_consistency(
        self, request_id: str, events: Sequence[AuditEvent]
    ) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        mismatched = [event for event in events if event.request_id and event.request_id != request_id]
        if mismatched:
            findings.append(
                GovernanceFinding(
                    "G-AUD-04",
                    SEVERITY_WARNING,
                    f"存在 {len(mismatched)} 条来自其他请求的审计事件，已单独标注",
                    subject="request_id",
                )
            )
        return findings

    def _check_consent(self, state, _events: Sequence[AuditEvent]) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        consent = state.consent
        risk_status = state.risk_status

        if risk_status is not None and risk_status.level == "HIGH":
            if consent is None or consent.allow_external_escalation is not True:
                findings.append(
                    GovernanceFinding(
                        "G-CON-01",
                        SEVERITY_CRITICAL,
                        "高风险场景未取得外部升级授权",
                        subject="consent.allow_external_escalation",
                    )
                )

        if consent is not None and consent.allow_record_storage is False:
            findings.append(
                GovernanceFinding(
                    "G-CON-02",
                    SEVERITY_WARNING,
                    "用户未授权记录存储",
                    subject="consent.allow_record_storage",
                )
            )

        if state.consultation_history and (
            consent is None or consent.allow_record_storage is not True
        ):
            findings.append(
                GovernanceFinding(
                    "G-CON-03",
                    SEVERITY_WARNING,
                    "已存在咨询记录，但缺少明确的记录存储授权",
                    subject="consultation_history",
                )
            )

        return findings

    def _check_hitl(self, state, events: Sequence[AuditEvent], context: Dict[str, Any]) -> List[GovernanceFinding]:
        findings: List[GovernanceFinding] = []
        review_events = [event for event in events if event.action in HUMAN_REVIEW_ACTIONS]
        pending_events = [event for event in events if event.human_required]

        if pending_events and not review_events:
            findings.append(
                GovernanceFinding(
                    "G-HITL-01",
                    SEVERITY_CRITICAL,
                    f"存在 human_required 的判定但缺少人工复核记录：{pending_events[0].action}",
                    subject=pending_events[0].action,
                )
            )
        elif (
            state.risk_status is not None
            and state.risk_status.requires_human
            and not review_events
        ):
            findings.append(
                GovernanceFinding(
                    "G-HITL-02",
                    SEVERITY_CRITICAL,
                    "共享状态标记需要人工复核，但审计链路中没有人工复核记录",
                    subject="risk_status.requires_human",
                )
            )

        human_review = (context or {}).get("human_review") or {}
        revised_level = human_review.get("revised_risk_level")
        if revised_level and state.risk_status is not None and state.risk_status.level != revised_level:
            findings.append(
                GovernanceFinding(
                    "G-HITL-03",
                    SEVERITY_CRITICAL,
                    f"人工复核结论未写入共享状态：{revised_level} != {state.risk_status.level}",
                    subject="human_review.revised_risk_level",
                )
            )
        if human_review and not review_events:
            findings.append(
                GovernanceFinding(
                    "G-HITL-04",
                    SEVERITY_WARNING,
                    "上下文包含人工复核信息，但缺少对应的人工复核审计事件",
                    subject="human_review",
                )
            )
        return findings

    # -- verdict -----------------------------------------------------------
    def evaluate(self, agent_input: AgentInput) -> Dict[str, Any]:
        """Pure, synchronous compliance evaluation -- no I/O, fully testable."""

        request_id = agent_input.request_id
        state = agent_input.state
        context = dict(agent_input.context or {})
        raw_events = context.get("audit_events") or []
        events = [event for event in (_as_event(item) for item in raw_events) if event is not None]
        unparsable = len(raw_events) - len(events)

        findings: List[GovernanceFinding] = []
        findings.extend(self._check_audit_trail(state, events))
        findings.extend(self._check_event_completeness(events))
        findings.extend(self._check_sensitive_data(events))
        findings.extend(self._check_data_scope(events))
        findings.extend(self._check_consent(state, events))
        findings.extend(self._check_hitl(state, events, context))
        findings.extend(self._check_request_consistency(request_id, events))
        if unparsable:
            findings.append(
                GovernanceFinding(
                    "G-AUD-05",
                    SEVERITY_CRITICAL,
                    f"存在 {unparsable} 条无法解析的审计事件",
                    subject="audit_events",
                )
            )

        critical = [finding for finding in findings if finding.is_critical]
        warnings = [finding for finding in findings if not finding.is_critical]

        if critical:
            compliance_status = NON_COMPLIANT
            status = "needs_info"
            requires_human = True
        elif warnings:
            compliance_status = COMPLIANT_WITH_WARNINGS
            status = "success"
            requires_human = False
        else:
            compliance_status = COMPLIANT
            status = "success"
            requires_human = False

        # Standard section 4, first special rule: Agent 5 has no risk-judgement
        # duty (that is Agent 4's, backed by the deterministic rule engine), so it
        # never reports a clinical tier -- it must answer UNKNOWN.  The compliance
        # verdict lives in ``compliance_status`` / ``G-*`` rule ids instead, so
        # nothing is lost, and a consumer can no longer mistake an audit warning
        # for a clinical downgrade.
        risk_level = "UNKNOWN"

        # The audit record keeps rule ids / counts only: no raw values, so the
        # governance event can never leak the very data it just flagged.
        decision_text = (
            f"compliance={compliance_status}; policy={self.policy_version}; "
            f"events_checked={len(events)}; critical={len(critical)}; warnings={len(warnings)}"
        )
        if findings:
            decision_text += "; rules=" + ",".join(
                f"{finding.rule_id}:{finding.severity}" for finding in findings
            )

        audit_event = AuditEvent(
            request_id=request_id,
            agent=AGENT_NAME,
            action="governance_audit_validation",
            data_accessed=["risk_status", "consent", "audit_events"],
            tool_used=None,
            source=["shared_user_state", "orchestrator"],
            decision=decision_text,
            risk_level=risk_level,
            human_required=requires_human,
        )

        state_update: Dict[str, Any] = {}
        if critical and not state.follow_up:
            state_update["follow_up"] = {
                "status": "pending_governance_review",
                "owner": "governance_officer",
                "sla_minutes": 1440,
                "recommended_action": "人工复核合规问题并完成授权确认",
            }

        issues = [finding.format() for finding in findings]
        assurance = f"审计链路完整：{len(events)} 个审计事件通过校验"

        return {
            "compliance_status": compliance_status,
            "policy_version": self.policy_version,
            "events_checked": len(events),
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "issues": issues,
            "findings": [finding.to_dict() for finding in findings],
            "rule_ids": [finding.rule_id for finding in findings],
            "status": status,
            "risk_level": risk_level,
            "requires_human": requires_human,
            "summary": (
                f"治理审计完成：{compliance_status}"
                f"（critical={len(critical)}, warnings={len(warnings)}）。"
            ),
            "evidence": issues or [assurance],
            "next_action": ["request_human_review"] if requires_human else [],
            "state_update": state_update,
            "audit_event": audit_event,
        }

    async def run_with_payload(
        self, agent_input: AgentInput, state: StateLike = None
    ) -> Tuple[AgentOutput, Dict[str, Any]]:
        """Run the audit and return both the standard output and the raw payload."""

        await asyncio.sleep(0)
        agent_input = self.prepare_input(agent_input, state)
        payload = self.evaluate(agent_input)
        output = AgentOutput(
            agent=AGENT_NAME,
            status=payload["status"],
            summary=payload["summary"],
            evidence=payload["evidence"],
            next_action=payload["next_action"],
            risk_level=payload["risk_level"],
            requires_human=payload["requires_human"],
            state_update=payload["state_update"],
            audit_events=[payload["audit_event"]],
            error=None,
        )
        return self.enforce_output(output, agent_input.request_id), payload

    async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput:
        output, _ = await self.run_with_payload(agent_input, state)
        return output



