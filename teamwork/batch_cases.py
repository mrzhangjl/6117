import asyncio
import csv
from pathlib import Path

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


async def evaluate_case(case: dict) -> dict:
    state = SharedUserState(
        user_id=case.get("user_id", "case-user"),
        profile=Profile(age=case.get("age", 30), basic_profile=case.get("profile", "pregnant")),
        pregnancy=Pregnancy(
            gestational_week=case.get("gestational_week"),
            estimated_due_date=case.get("estimated_due_date"),
        ) if case.get("gestational_week") is not None else None,
        symptoms=[
            Symptom(symptom=s, severity=case.get("symptom_severity", "medium"), timestamp="2026-09-15T10:00:00")
            for s in case.get("symptoms", [])
        ],
        reports=[
            Report(
                report_id=rep.get("report_id", "report"),
                report_type=rep.get("report_type", "lab"),
                extracted_data=rep.get("extracted_data", {}),
                source_file=rep.get("source_file", "demo.txt"),
            )
            for rep in case.get("reports", [])
        ],
        medical_history=case.get("medical_history", []),
        consent=Consent(
            allow_record_storage=case.get("allow_record_storage", True),
            allow_external_escalation=case.get("allow_external_escalation", True),
            allow_family_notification=case.get("allow_family_notification", False),
        ),
    )

    agent_input = AgentInput(
        request_id=case.get("request_id", "req-default"),
        user_id=state.user_id,
        task="evaluate_maternity_risk",
        user_message=case.get("user_message", "评估风险"),
        state=state,
        context={"original_user_msg": case.get("user_message", "评估风险"), "audit_events": []},
        requested_by="user",
    )

    result = await RiskAssessmentAgent().run(agent_input)
    return {
        "case_name": case.get("case_name", "unnamed"),
        "risk_level": result.risk_level,
        "requires_human": result.requires_human,
        "status": result.status,
        "summary": result.summary,
        "evidence": " | ".join(result.evidence),
        "audit_count": len(result.audit_events),
    }


async def main():
    cases = [
        {
            "case_name": "case_01_high_risk_bleeding",
            "user_id": "u-01",
            "age": 30,
            "profile": "pregnant",
            "gestational_week": 42.8,
            "symptoms": ["阴道出血"],
            "symptom_severity": "high",
            "medical_history": ["子痫前期"],
            "allow_record_storage": True,
            "allow_external_escalation": True,
        },
        {
            "case_name": "case_02_preterm_pain",
            "user_id": "u-02",
            "age": 29,
            "profile": "pregnant",
            "gestational_week": 35,
            "symptoms": ["腹痛"],
            "symptom_severity": "medium",
        },
        {
            "case_name": "case_03_unknown_missing_info",
            "user_id": "u-03",
            "age": 31,
            "profile": "pregnant",
            "symptoms": [],
            "medical_history": [],
        },
        {
            "case_name": "case_04_moderate_history",
            "user_id": "u-04",
            "age": 33,
            "profile": "pregnant",
            "gestational_week": 39,
            "medical_history": ["瘢痕子宫"],
        },
        {
            "case_name": "case_05_high_report_abnormal",
            "user_id": "u-05",
            "age": 26,
            "profile": "pregnant",
            "gestational_week": 38,
            "reports": [{"report_id": "lab-a", "extracted_data": {"bp_high": True}}],
        },
        {
            "case_name": "case_06_mild_symptom",
            "user_id": "u-06",
            "age": 27,
            "profile": "pregnant",
            "gestational_week": 38,
            "symptoms": ["轻微腰酸"],
            "symptom_severity": "low",
        },
        {
            "case_name": "case_07_regular_contractions",
            "user_id": "u-07",
            "age": 35,
            "profile": "pregnant",
            "gestational_week": 34,
            "symptoms": ["规律性宫缩"],
            "symptom_severity": "high",
        },
        {
            "case_name": "case_08_visual_blur",
            "user_id": "u-08",
            "age": 32,
            "profile": "pregnant",
            "gestational_week": 38,
            "symptoms": ["视物模糊"],
            "symptom_severity": "high",
        },
        {
            "case_name": "case_09_no_threat_signals",
            "user_id": "u-09",
            "age": 28,
            "profile": "pregnant",
            "gestational_week": 27,
            "symptoms": ["轻微恶心"],
            "symptom_severity": "low",
        },
        {
            "case_name": "case_10_false_alarm",
            "user_id": "u-10",
            "age": 31,
            "profile": "pregnant",
            "gestational_week": 40,
            "symptoms": ["假性宫缩"],
            "symptom_severity": "low",
        },
    ]

    results = []
    for case in cases:
        result = await evaluate_case(case)
        results.append(result)

    output_path = Path("case_results.csv")
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["case_name", "risk_level", "requires_human", "status", "summary", "evidence", "audit_count"],
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"Saved CSV results to: {output_path}")
    for item in results:
        print(item)


if __name__ == "__main__":
    asyncio.run(main())
