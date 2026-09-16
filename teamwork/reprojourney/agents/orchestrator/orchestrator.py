from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Literal, Optional, Tuple

from reprojourney.schemas.base_schemas import (
    AgentInput,
    AgentOutput,
    AuditEvent,
    RiskStatus,
    SharedUserState,
)
from reprojourney.schemas.risk_rules import MaternityRiskRuleEngine

from reprojourney.agents.governance.agent5_governance_audit import GovernanceAuditAgent
from reprojourney.agents.risk.agent4_risk_assessment import RiskAssessmentAgent

AGENT_NAME = "orchestrator"


class OrchestratorCheckpoint:
    def __init__(self) -> None:
        self.request_id: str = ""
        self.user_id: str = ""
        self.state: SharedUserState | None = None
        self.context: Dict[str, Any] = {}
        self.round: int = 0
        self.max_round: int = 5
        self.audit_events: list[AuditEvent] = []
        self.pending_next_agent: str = ""
        self.pending_risk_result: Optional[Dict[str, Any]] = None


class Orchestrator:
    def __init__(self) -> None:
        self.agent_map: Dict[str, Any] = {
            "risk_assessment_agent": RiskAssessmentAgent(),
            "governance_audit_agent": GovernanceAuditAgent(),
        }
        self.checkpoints: Dict[str, OrchestratorCheckpoint] = {}

    def _merge_state_update(self, base_state: SharedUserState, update: Dict[str, Any]) -> SharedUserState:
        state_dict = base_state.model_dump(mode="python")
        for key, value in update.items():
            if key not in state_dict:
                continue
            if isinstance(state_dict.get(key), list) and isinstance(value, list):
                state_dict[key].extend(value)
            else:
                state_dict[key] = value
        return SharedUserState.model_validate(state_dict)

    def _finalize_after_governance(
        self,
        checkpoint: OrchestratorCheckpoint,
        gov_out: AgentOutput,
        risk_fallback: str,
    ) -> Tuple[str, AgentOutput]:
        """Return the governance verdict, never a silent success.

        Agent 5 owns the compliance gate (G-* rules): a ``critical`` finding
        (e.g. a HIGH-risk case without external-escalation consent) comes back
        as ``requires_human`` on its own output. Returning ``completed`` in that
        case would make the gate decorative, so the orchestrator propagates it
        as ``needs_info`` + ``requires_human`` instead.

        The reported level is read from the checkpoint state, which already
        contains the human reviewer's decision (``override``/``reject``) when
        the flow was resumed.
        """
        risk_status = checkpoint.state.risk_status if checkpoint.state else None
        risk_level = risk_status.level if risk_status else risk_fallback

        if gov_out.requires_human:
            return "needs_info", AgentOutput(
                agent=AGENT_NAME,
                status="needs_info",
                summary=f"治理审计未通过，需人工介入：{gov_out.summary}",
                evidence=gov_out.evidence,
                next_action=["request_human_review"],
                risk_level=risk_level,
                requires_human=True,
                state_update={},
                audit_events=checkpoint.audit_events,
                error=None,
            )

        return "completed", AgentOutput(
            agent=AGENT_NAME,
            status="success",
            summary="风险评估与治理审计已完成",
            evidence=gov_out.evidence,
            next_action=[],
            risk_level=risk_level,
            requires_human=False,
            state_update={},
            audit_events=checkpoint.audit_events,
            error=None,
        )

    async def start_flow(self, agent_input: AgentInput, max_rounds: int = 5) -> Tuple[str, Optional[AgentOutput]]:
        request_id = agent_input.request_id
        checkpoint = OrchestratorCheckpoint()
        checkpoint.request_id = request_id
        checkpoint.user_id = agent_input.user_id
        checkpoint.state = deepcopy(agent_input.state)
        checkpoint.context = deepcopy(agent_input.context)
        checkpoint.round = 0
        checkpoint.max_round = max_rounds
        checkpoint.audit_events = []
        self.checkpoints[request_id] = checkpoint
        return await self._run_flow(request_id)

    async def resume_after_human(
        self,
        request_id: str,
        human_action: Literal["approve", "override", "reject"],
        human_reviewer: str,
        revised_risk_level: Optional[Literal["LOW", "MODERATE", "HIGH", "UNKNOWN"]] = None,
        human_decision_note: str = "",
    ) -> Tuple[str, Optional[AgentOutput]]:
        if request_id not in self.checkpoints:
            return "error", None

        checkpoint = self.checkpoints[request_id]
        old_risk = checkpoint.pending_risk_result.get("risk_level", "UNKNOWN") if checkpoint.pending_risk_result else "UNKNOWN"
        final_risk = revised_risk_level if human_action == "override" and revised_risk_level else old_risk

        if human_action == "reject":
            final_risk = "LOW"

        # Apply the human decision to shared state. The orchestrator is the only
        # writer of ``SharedUserState``, so the decision must land here -- Agent 5
        # verifies (G-HITL-03) that it really did.
        existing_reasons = list(checkpoint.state.risk_status.reasons) if checkpoint.state.risk_status else []
        review_reasons = [f"人工复核（{human_reviewer}）：{human_action}"]
        if human_decision_note:
            review_reasons.append(human_decision_note)
        checkpoint.state.risk_status = RiskStatus(
            level=final_risk,
            reasons=list(dict.fromkeys(existing_reasons + review_reasons)),
            requires_human=False,
        )

        human_audit = AuditEvent(
            request_id=request_id,
            agent=AGENT_NAME,
            action="human_risk_review",
            data_accessed=["risk_status"],
            tool_used=None,
            source=["risk_assessment_agent"],
            decision=(
                f"reviewer={human_reviewer}; action={human_action}; old_risk={old_risk}; "
                f"new_risk={final_risk}; note={human_decision_note}"
            ),
            risk_level=final_risk,
            human_required=False,
        )
        checkpoint.audit_events.append(human_audit)
        checkpoint.context["human_review"] = {
            "reviewer": human_reviewer,
            "action": human_action,
            "revised_risk_level": final_risk,
            "note": human_decision_note,
        }
        return await self._run_flow(request_id)

    async def _run_flow(self, request_id: str) -> Tuple[str, Optional[AgentOutput]]:
        checkpoint = self.checkpoints[request_id]

        while checkpoint.round < checkpoint.max_round:
            checkpoint.round += 1

            if checkpoint.pending_risk_result is not None and checkpoint.pending_next_agent == "governance_audit_agent":
                # Continue after human review to governance.
                checkpoint.pending_next_agent = ""
                gov_input = AgentInput(
                    request_id=request_id,
                    user_id=checkpoint.user_id,
                    task="run_governance_audit",
                    user_message=checkpoint.context.get("original_user_msg"),
                    state=checkpoint.state,
                    context={**checkpoint.context, "audit_events": checkpoint.audit_events},
                    requested_by="orchestrator",
                )
                gov_out = await self.agent_map["governance_audit_agent"].run(gov_input)
                checkpoint.audit_events.extend(gov_out.audit_events)
                checkpoint.state = self._merge_state_update(checkpoint.state, gov_out.state_update)
                # ``pending_risk_result`` holds the pre-review level; the helper
                # prefers ``state.risk_status``, i.e. the human decision.
                return self._finalize_after_governance(
                    checkpoint,
                    gov_out,
                    (checkpoint.pending_risk_result or {}).get("risk_level", "UNKNOWN"),
                )

            risk_input = AgentInput(
                request_id=request_id,
                user_id=checkpoint.user_id,
                task="run_risk_assessment",
                user_message=checkpoint.context.get("original_user_msg"),
                state=checkpoint.state,
                context=checkpoint.context,
                requested_by="orchestrator",
            )
            risk_out = await self.agent_map["risk_assessment_agent"].run(risk_input)
            checkpoint.audit_events.extend(risk_out.audit_events)
            checkpoint.state = self._merge_state_update(checkpoint.state, risk_out.state_update)
            checkpoint.pending_risk_result = {
                "risk_level": risk_out.risk_level,
                "requires_human": risk_out.requires_human,
            }

            if risk_out.requires_human:
                checkpoint.pending_next_agent = "governance_audit_agent"
                return "waiting_human", AgentOutput(
                    agent=AGENT_NAME,
                    status="escalate",
                    summary=f"高风险判定已触发人工复核：{risk_out.summary}",
                    evidence=risk_out.evidence,
                    next_action=["governance_audit_agent"],
                    risk_level=risk_out.risk_level,
                    requires_human=True,
                    state_update={},
                    audit_events=checkpoint.audit_events,
                    error=None,
                )

            gov_input = AgentInput(
                request_id=request_id,
                user_id=checkpoint.user_id,
                task="run_governance_audit",
                user_message=checkpoint.context.get("original_user_msg"),
                state=checkpoint.state,
                context={**checkpoint.context, "audit_events": checkpoint.audit_events},
                requested_by="orchestrator",
            )
            gov_out = await self.agent_map["governance_audit_agent"].run(gov_input)
            checkpoint.audit_events.extend(gov_out.audit_events)
            checkpoint.state = self._merge_state_update(checkpoint.state, gov_out.state_update)
            return self._finalize_after_governance(checkpoint, gov_out, risk_out.risk_level)

        return "error", AgentOutput(
            agent=AGENT_NAME,
            status="error",
            summary="流程超过最大轮次",
            evidence=[],
            next_action=[],
            risk_level="UNKNOWN",
            requires_human=False,
            state_update={},
            audit_events=checkpoint.audit_events,
            error="max_round_exceeded",
        )


__all__ = ["Orchestrator", "OrchestratorCheckpoint"]
