"""Conformance tests for the ReproJourney Agent Development Standard v1.0.

These cases do not test *clinical behaviour* (that is covered by
``test_agent_contracts.py`` / ``test_agents.py``) -- they test the **team
agreement**: the shared state, the unified input/output, the closed risk-level
vocabulary, the audit-event shape, the uniform entry point and the "an agent is
not an app" rules.  The standard lives in ``reprojourney/schemas/agent_contract.py``;
if somebody renames a field or invents a fifth risk level, it fails here first.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from pydantic import ValidationError

from reprojourney.agents.base import BaseAgent, parameter_names
from reprojourney.agents.governance.agent5_governance_audit import GovernanceAuditAgent
from reprojourney.agents.risk.agent4_risk_assessment import RiskAssessmentAgent
from reprojourney.schemas import agent_contract as contract
from reprojourney.schemas.base_schemas import (
    AgentInput,
    AgentOutput,
    AuditEvent,
    Consent,
    Pregnancy,
    Profile,
    RiskStatus,
    SharedUserState,
    Symptom,
)

ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = "req-standard"

#: A model name, path or credential in an agent source file is a standard violation.
SOURCE_PATTERNS = {
    # 用字符类拆开字面量，免得"检测绝对路径的正则"本身被守卫误判为绝对路径
    "绝对路径": re.compile(r"(?<![\w/])[A-Za-z]:[\\/]|/[Uu]sers/|/[Hh]ome/"),
    "硬编码秘钥": re.compile(r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"),
    "编造的风险等级词": re.compile(r"(?<![\w-])(amber|green|yellow|urgent|severe)(?![\w-])|maybe[-_]risk", re.I),
    "自建 Web/外联框架": re.compile(
        r"(?m)^\s*(?:import|from)\s+(fastapi|flask|django|uvicorn|starlette|requests|aiohttp|httpx|smtplib|twilio)\b"
    ),
}

#: Tools that would let an agent reach a real hospital / phone / inbox.
EXTERNAL_TOOLS = {
    "http_request",
    "web_search",
    "send_sms",
    "send_email",
    "send_notification",
    "webhook",
}


def make_state(**kwargs) -> SharedUserState:
    return SharedUserState(
        user_id=kwargs.pop("user_id", "u-standard"),
        profile=Profile(age=30, basic_profile="pregnant"),
        pregnancy=kwargs.pop("pregnancy", Pregnancy(gestational_week=38)),
        **kwargs,
    )


def make_input(
    state: SharedUserState, message: str = "评估风险", task: str = "run_risk_assessment", **context
) -> AgentInput:
    payload = {"audit_events": [], "original_user_msg": message}
    payload.update(context)
    return AgentInput(
        request_id=REQUEST_ID,
        user_id=state.user_id,
        task=task,
        user_message=message,
        state=state,
        context=payload,
        requested_by="orchestrator",
    )


def risk_event(level: str = "MODERATE", request_id: str = REQUEST_ID) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        agent="risk_assessment_agent",
        action="run_maternity_risk_evaluation",
        data_accessed=["symptoms"],
        source=["shared_user_state"],
        decision=f"risk={level}; rules=['R-SY-02']",
        risk_level=level,
        human_required=False,
    )


class StandardSchemaTests(unittest.TestCase):
    """Section 2 / 3 / 4 / 5 / 6: the shared vocabulary cannot drift."""

    def test_01_shared_user_state_matches_the_standard(self) -> None:
        self.assertEqual(contract.standard_problems(), [])
        self.assertEqual(tuple(SharedUserState.model_fields), contract.STATE_FIELDS)

        # "不得自行改名" is enforced, not merely documented.
        with self.assertRaises(ValidationError):
            SharedUserState(user_id="u-standard", risk_level="HIGH")
        with self.assertRaises(ValidationError):
            Profile(age=30, age_group="30-35")

    def test_02_agent_input_keeps_the_seven_standard_fields(self) -> None:
        self.assertEqual(tuple(AgentInput.model_fields), contract.INPUT_FIELDS)
        with self.assertRaises(ValidationError):
            AgentInput(
                request_id=REQUEST_ID,
                user_id="u-standard",
                task="t",
                state=make_state(),
                requested_by="orchestrator",
                extra_param="散乱参数",
            )

        for allowed in ("user", "orchestrator", "risk_assessment_agent"):
            self.assertIsNone(contract.requested_by_problem(allowed))
        self.assertIsNotNone(contract.requested_by_problem("some_random_service"))

    def test_03_risk_level_vocabulary_is_closed_and_synonym_free(self) -> None:
        self.assertEqual(contract.risk_level_schema_problems(), [])
        for invented in ("urgent", "green", "amber", "maybe-risk", "severe"):
            with self.subTest(level=invented):
                with self.assertRaises(ValidationError):
                    AgentOutput(agent="x", status="success", summary="s", risk_level=invented)

        offenders = []
        for path in sorted((ROOT / "reprojourney").rglob("*.py")):
            if path.name == "agent_contract.py":
                continue  # that module *declares* the forbidden list, by design
            match = SOURCE_PATTERNS["编造的风险等级词"].search(path.read_text(encoding="utf-8"))
            if match:
                offenders.append(f"{path.name}:{match.group(0)}")
        self.assertEqual(offenders, [], f"发现标准 §5 禁止的等级词：{offenders}")


def fingerprint(output: AgentOutput) -> tuple:
    """Comparable view of an output (audit timestamps are wall-clock, hence out)."""

    events = [event.model_dump(exclude={"timestamp"}) for event in output.audit_events]
    return output.model_dump(exclude={"audit_events"}), events


class StandardEntryPointTests(unittest.IsolatedAsyncioTestCase):
    """Sections 3 / 4 / 7: the uniform entry point and the uniform output."""

    def test_04_every_agent_declares_the_standard_entry_point(self) -> None:
        for agent_cls in (RiskAssessmentAgent, GovernanceAuditAgent):
            with self.subTest(agent=agent_cls.__name__):
                self.assertTrue(issubclass(agent_cls, BaseAgent))
                self.assertEqual(parameter_names(agent_cls), ("agent_input", "state"))
                spec = agent_cls.contract()
                self.assertEqual(spec["standard_version"], contract.STANDARD_VERSION)
                self.assertTrue(spec["entry_point"].startswith("run(agent_input, state"))

        self.assertTrue(RiskAssessmentAgent.risk_duty)
        self.assertFalse(GovernanceAuditAgent.risk_duty)

    async def test_05_both_call_styles_are_the_same_call(self) -> None:
        state = make_state(
            pregnancy=Pregnancy(gestational_week=42.5),
            symptoms=[Symptom(symptom="阴道出血", severity="high")],
            consent=Consent(allow_record_storage=True, allow_external_escalation=True),
        )
        agent = RiskAssessmentAgent()

        one_arg = await agent.run(make_input(state))
        two_arg = await agent.run(make_input(state), state)
        dict_arg = await agent.run(make_input(state), state.model_dump(mode="json"))

        self.assertEqual(fingerprint(one_arg), fingerprint(two_arg))
        self.assertEqual(fingerprint(one_arg), fingerprint(dict_arg))
        self.assertEqual(one_arg.risk_level, "HIGH")

        # Section 1: one shared state per user -- injecting somebody else's state
        # must fail instead of silently grading the wrong patient.
        with self.assertRaises(contract.AgentContractError):
            await agent.run(make_input(state), make_state(user_id="u-other"))

        # Section 3: requested_by is a closed vocabulary, not a free-text field.
        stranger = make_input(state).model_copy(update={"requested_by": "stranger"})
        with self.assertRaises(contract.AgentContractError):
            await agent.run(stranger)

    async def test_06_outputs_obey_section_4_special_rules(self) -> None:
        high_state = make_state(
            pregnancy=Pregnancy(gestational_week=42.5),
            symptoms=[Symptom(symptom="阴道出血", severity="high")],
        )
        clean_state = make_state(risk_status=RiskStatus(level="MODERATE", reasons=["轻微腰酸"]))

        produced = [
            (RiskAssessmentAgent, make_input(high_state)),
            (RiskAssessmentAgent, make_input(SharedUserState(user_id="u-standard"), "我该怎么办")),
            (
                GovernanceAuditAgent,
                make_input(clean_state, "审计", task="run_governance_audit", audit_events=[risk_event()]),
            ),
            (
                GovernanceAuditAgent,
                make_input(
                    make_state(risk_status=RiskStatus(level="HIGH", reasons=["阴道出血"], requires_human=True)),
                    "审计",
                    task="run_governance_audit",
                    audit_events=[],
                ),
            ),
        ]

        for agent_cls, agent_input in produced:
            with self.subTest(agent=agent_cls.__name__):
                output = await agent_cls().run(agent_input)
                self.assertEqual(
                    contract.check_output_contract(
                        output, risk_duty=agent_cls.risk_duty, request_id=agent_input.request_id
                    ),
                    [],
                )

        # Agent 5 never imitates a clinical tier, even when it blocks a flow.
        governance, gov_input = produced[3]
        blocked = await governance().run(gov_input)
        self.assertEqual(blocked.risk_level, "UNKNOWN")
        self.assertTrue(blocked.requires_human)
        self.assertEqual(blocked.status, "needs_info")

        # Negative control: the checker really does bite.
        lying = AgentOutput(
            agent=RiskAssessmentAgent.agent_name,
            status="success",
            summary="风险很高但我说没问题",
            risk_level="HIGH",
            requires_human=False,
            audit_events=[risk_event("HIGH")],
        )
        self.assertTrue(contract.check_output_contract(lying, risk_duty=True))

    async def test_07_audit_events_obey_section_6(self) -> None:
        high_state = make_state(symptoms=[Symptom(symptom="破水", severity="high")])
        gov_state = make_state(risk_status=RiskStatus(level="MODERATE", reasons=["轻微腰酸"]))

        outputs = [
            await RiskAssessmentAgent().run(make_input(high_state)),
            await GovernanceAuditAgent().run(
                make_input(gov_state, "审计", task="run_governance_audit", audit_events=[risk_event()])
            ),
        ]

        for output in outputs:
            for event in output.audit_events:
                with self.subTest(event=f"{event.agent}.{event.action}"):
                    self.assertEqual(tuple(type(event).model_fields), contract.AUDIT_EVENT_FIELDS)
                    self.assertIn(event.risk_level, contract.RISK_LEVELS)
                    self.assertEqual(event.request_id, REQUEST_ID)
                    self.assertEqual(contract.audit_event_problems(event), [])

        # Section 6: an audit record is not a log dump -- prompts, keys and
        # passwords must never be copied into it.
        leaky = AuditEvent(
            request_id=REQUEST_ID,
            agent="risk_assessment_agent",
            action="run_maternity_risk_evaluation",
            decision="prompt=用户原始长文; api_key=sk-live-000",
        )
        self.assertTrue(contract.audit_event_problems(leaky))

    def test_08_an_agent_is_not_an_app_and_declares_its_slot(self) -> None:
        from reprojourney.tools.registry import build_default_registry

        # Section 7: no own web server, no real hospitals, no hardcoded secrets
        # or Windows paths anywhere in the agent layer.
        offenders = []
        for path in sorted((ROOT / "reprojourney").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for label, pattern in SOURCE_PATTERNS.items():
                if label == "编造的风险等级词":
                    continue
                match = pattern.search(text)
                if match:
                    offenders.append(f"{path.name}: {label} -> {match.group(0)!r}")
        self.assertEqual(offenders, [], f"违反标准 §7 禁止事项：{offenders}")

        # Section 7/8: the tool layer is a whitelist -- nothing can reach out.
        self.assertEqual(set(build_default_registry().names()) & EXTERNAL_TOOLS, set())

        # Section 7: the standard's tree is matched name-for-name.
        self.assertTrue((ROOT / contract.TOOL_SLOT).is_dir(), f"{contract.TOOL_SLOT} 不存在")
        for slot, relative in contract.AGENT_SLOTS.items():
            with self.subTest(slot=slot):
                if slot in contract.PENDING_SLOTS:
                    self.assertFalse((ROOT / relative).exists(), f"{slot} 已实现，应移出 PENDING_SLOTS")
                    continue
                self.assertTrue((ROOT / relative).is_dir(), f"{slot} -> {relative} 不存在")
                self.assertEqual(Path(relative).name, slot, f"{slot} 的目录名必须与标准槽位逐字一致")

        # Section 7 environment rules are checked in the repository itself.
        self.assertTrue((ROOT / "requirements.txt").is_file())
        self.assertIn(".env", (ROOT / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
