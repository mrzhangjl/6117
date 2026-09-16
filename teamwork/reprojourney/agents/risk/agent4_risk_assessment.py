from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from reprojourney.agents.base import BaseAgent, StateLike
from reprojourney.schemas.base_schemas import AgentInput, AgentOutput, AuditEvent, RiskStatus
from reprojourney.schemas.risk_rules import MaternityRiskRuleEngine

AGENT_NAME = "risk_assessment_agent"

#: Requests clearly outside the maternity-risk scope. The agent must not
#: pretend to assess them -- it refuses and explains why (safety negative case).
NON_CLINICAL_KEYWORDS = (
    "天气",
    "weather",
    "股票",
    "写诗",
    "笑话",
    "joke",
    "翻译",
    "translate",
    "彩票",
)

#: When an out-of-scope keyword appears, the request is still treated as clinical
#: if it explicitly mentions maternity-care subject matter.
CLINICAL_HINTS = (
    "孕",
    "产",
    "胎",
    "症状",
    "出血",
    "破水",
    "宫缩",
    "腹痛",
    "头晕",
    "水肿",
    "检查",
    "报告",
    "指标",
    "血压",
    "血糖",
    "风险",
    "评估",
    "建档",
)

#: Response targets per tier, surfaced to the orchestrator / HITL operator.
PRIORITY_BY_LEVEL = {
    "HIGH": "P0-立即处理",
    "MODERATE": "P1-24小时内跟进",
    "LOW": "P2-常规产检随访",
    "UNKNOWN": "P3-补充信息后再评估",
}

SLA_MINUTES_BY_LEVEL = {"HIGH": 15, "MODERATE": 1440, "LOW": 10080, "UNKNOWN": 1440}

HIGH_RISK_NEXT_STEPS = ["立即联系产科医生或前往急诊评估", "持续记录症状变化（时间、频率、出血量）"]


class RiskAssessmentAgent(BaseAgent):
    """Agent 4 -- deterministic maternity risk assessment with human escalation.

    Contract:
      * the tier comes **only** from :class:`MaternityRiskRuleEngine` (never an LLM);
      * ``risk_level == "HIGH"`` => ``status == "escalate"`` + ``requires_human``;
      * ``risk_level == "UNKNOWN"`` => ``status == "needs_info"`` with concrete gaps;
      * every run emits exactly one :class:`AuditEvent`;
      * ``state_update`` carries ``risk_status`` (plus a first-time ``follow_up``
        for HIGH) so the orchestrator stays the single writer of shared state;
      * standard section 7 entry point: ``run(agent_input, state=None)``
        (``RiskAssessmentAgent.risk_duty is True`` -- it owns the risk decision).
    """

    agent_name = AGENT_NAME
    task = "run_risk_assessment"
    risk_duty = True

    def __init__(self, rule_engine: Optional[MaternityRiskRuleEngine] = None) -> None:
        self.rule_engine = rule_engine or MaternityRiskRuleEngine()

    def _is_off_topic(self, agent_input: AgentInput) -> bool:
        """Off-topic = the *current* request is clearly outside maternity care.

        Historical state must not make an unrelated request "clinical": a stored
        gestational week does not turn "告诉我股票行情" into a risk assessment.
        """

        text = " ".join(
            str(part)
            for part in (agent_input.user_message, agent_input.context.get("original_user_msg"))
            if part
        ).lower()
        if not text or not any(keyword.lower() in text for keyword in NON_CLINICAL_KEYWORDS):
            return False
        return not any(hint in text for hint in CLINICAL_HINTS)

    def _evidence(self, result: Dict[str, Any], previous_level: Optional[str]) -> List[str]:
        rule_ids = result.get("rule_ids") or []
        evidence: List[str] = [f"命中规则：{', '.join(rule_ids)}"] if rule_ids else []
        evidence.extend(result["reasons"])
        if result.get("needs_info"):
            evidence.append("待补充信息：" + "；".join(result["needs_info"]))
        if previous_level and previous_level != result["risk_level"]:
            evidence.append(f"风险等级变化：{previous_level} -> {result['risk_level']}")
        return evidence

    async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput:
        await asyncio.sleep(0)
        # Section 7: accept the state either inside the input or as a second
        # argument; either way there is exactly one validated shared state.
        agent_input = self.prepare_input(agent_input, state)
        request_id = agent_input.request_id
        state = agent_input.state
        previous_level = state.risk_status.level if state.risk_status else None

        if self._is_off_topic(agent_input):
            decision = "请求与孕产风险评估无关，已拒绝给出风险结论"
            return self.enforce_output(
                AgentOutput(
                    agent=AGENT_NAME,
                    status="needs_info",
                    summary=decision + "。请提供孕周、症状或检查结果后再进行风险评估。",
                    evidence=[decision],
                    next_action=[],
                    risk_level="UNKNOWN",
                    requires_human=False,
                    state_update={},
                    audit_events=[
                        AuditEvent(
                            request_id=request_id,
                            agent=AGENT_NAME,
                            action="reject_non_clinical_request",
                            data_accessed=["user_message"],
                            tool_used=None,
                            source=["agent_input"],
                            decision=decision,
                            risk_level="UNKNOWN",
                            human_required=False,
                        )
                    ],
                    error=None,
                ),
                request_id,
            )

        result = self.rule_engine.evaluate(state)
        risk_level = result["risk_level"]
        reasons = result["reasons"]
        requires_human = result["requires_human"]

        status = "success"
        if risk_level == "HIGH" and requires_human:
            status = "escalate"
        elif risk_level == "UNKNOWN":
            status = "needs_info"

        if status == "escalate":
            next_action = ["request_human_review", "governance_audit_agent"]
        elif status == "needs_info":
            next_action = []
        else:
            next_action = ["governance_audit_agent"]

        audit_event = AuditEvent(
            request_id=request_id,
            agent=AGENT_NAME,
            action="run_maternity_risk_evaluation",
            data_accessed=["pregnancy", "symptoms", "reports", "medical_history", "follow_up"],
            tool_used="maternity_risk_rule_engine",
            source=["shared_user_state"],
            decision=f"risk={risk_level}; rules={result['rule_ids']}; reasons={reasons}",
            risk_level=risk_level,
            human_required=requires_human,
        )

        state_update: Dict[str, Any] = {
            "risk_status": RiskStatus(
                level=risk_level,
                reasons=reasons,
                requires_human=requires_human,
            ).model_dump(mode="json")
        }
        if status == "escalate" and not state.follow_up:
            state_update["follow_up"] = {
                "status": "pending_human",
                "owner": "human_clinician",
                "sla_minutes": SLA_MINUTES_BY_LEVEL["HIGH"],
                "recommended_action": HIGH_RISK_NEXT_STEPS[0],
            }

        summary = f"孕产风险评估完成：{risk_level}（{PRIORITY_BY_LEVEL[risk_level]}）。" + "；".join(reasons)

        return self.enforce_output(
            AgentOutput(
                agent=AGENT_NAME,
                status=status,
                summary=summary,
                evidence=self._evidence(result, previous_level),
                next_action=next_action,
                risk_level=risk_level,
                requires_human=requires_human,
                state_update=state_update,
                audit_events=[audit_event],
                error=None,
            ),
            request_id,
        )
