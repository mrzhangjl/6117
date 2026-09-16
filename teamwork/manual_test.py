import asyncio
from uuid import uuid4

from reprojourney.agents.governance.agent5_governance_audit import GovernanceAuditAgent
from reprojourney.agents.risk.agent4_risk_assessment import RiskAssessmentAgent
from reprojourney.schemas.base_schemas import (
    AgentInput,
    Consent,
    Pregnancy,
    Profile,
    Report,
    SharedUserState,
    Symptom,
)


async def run_manual_case():
    # 修改这里来手动测试不同场景
    state = SharedUserState(
        user_id="manual-user-001",
        profile=Profile(age=30, basic_profile="pregnant"),
        pregnancy=Pregnancy(gestational_week=42.8, estimated_due_date="2026-10-01"),
        symptoms=[
            Symptom(symptom="阴道出血", severity="high", timestamp="2026-09-15T10:00:00"),
            Symptom(symptom="轻微恶心", severity="low", timestamp="2026-09-15T10:05:00"),
        ],
        reports=[
            Report(
                report_id="lab-001",
                report_type="blood_test",
                extracted_data={"bp_high": True, "proteinuria": False},
                source_file="demo_lab.txt",
            )
        ],
        medical_history=["子痫前期"],
        consent=Consent(
            allow_record_storage=True,
            allow_external_escalation=True,
            allow_family_notification=False,
        ),
    )

    request_id = str(uuid4())

    risk_input = AgentInput(
        request_id=request_id,
        user_id="manual-user-001",
        task="evaluate_maternity_risk",
        user_message="请评估当前孕期风险",
        state=state,
        context={"original_user_msg": "请评估当前孕期风险", "audit_events": []},
        requested_by="user",
    )

    risk_agent = RiskAssessmentAgent()
    risk_output = await risk_agent.run(risk_input)

    print("===== Risk Agent Output =====")
    print("status:", risk_output.status)
    print("risk_level:", risk_output.risk_level)
    print("requires_human:", risk_output.requires_human)
    print("summary:", risk_output.summary)
    print("evidence:", risk_output.evidence)
    print("state_update:", risk_output.state_update)
    print("audit_events:", [event.model_dump() for event in risk_output.audit_events])

    gov_input = AgentInput(
        request_id=request_id,
        user_id="manual-user-001",
        task="run_governance_audit",
        user_message="请检查审计与合规",
        state=state,
        context={"original_user_msg": "请检查审计与合规", "audit_events": risk_output.audit_events},
        requested_by="orchestrator",
    )

    gov_agent = GovernanceAuditAgent()
    gov_output = await gov_agent.run(gov_input)

    print("\n===== Governance Agent Output =====")
    print("status:", gov_output.status)
    print("risk_level:", gov_output.risk_level)
    print("summary:", gov_output.summary)
    print("evidence:", gov_output.evidence)
    print("audit_events:", [event.model_dump() for event in gov_output.audit_events])


if __name__ == "__main__":
    asyncio.run(run_manual_case())
