from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reprojourney.agents.main_agent import (
    MainAgent,
    SessionState,
    SessionStore,
    apply_state_update,
    build_default_registry,
)
from reprojourney.tools.registry import ToolContext
from reprojourney.llm import OfflinePolicyChatModel, ScriptedChatModel, extract_facts, turn
from reprojourney.schemas.base_schemas import (
    Consent,
    Pregnancy,
    Profile,
    RiskStatus,
    SharedUserState,
    Symptom,
)

HIGH_CASE_MESSAGE = "孕周42周，阴道出血，病史子痫前期，帮我评估风险"
ALLOWED_TOOLS = {
    "read_user_state",
    "record_user_facts",
    "assess_risk",
    "audit_governance",
    "request_human_review",
    "persist_session",
}


def make_state(week=38.0, symptoms=None, consent=True) -> SharedUserState:
    return SharedUserState(
        user_id="u-test",
        profile=Profile(age=30, basic_profile="pregnant"),
        pregnancy=Pregnancy(gestational_week=week),
        symptoms=[Symptom(symptom=name, severity="high") for name in (symptoms or [])],
        consent=Consent(allow_record_storage=consent, allow_external_escalation=consent),
    )


def make_agent(**kwargs) -> MainAgent:
    """Offline model + no persistence unless the test asks for them."""

    kwargs.setdefault("model", OfflinePolicyChatModel())
    kwargs.setdefault("store", None)
    return MainAgent(**kwargs)


def make_context(agent: MainAgent, request_id: str = "req-test", message: str = "") -> ToolContext:
    return ToolContext(
        request_id=request_id,
        user_id=agent.user_id,
        state=agent.session.state,
        audit_events=agent.session.audit_events,
        store=agent.store,
        session=agent.session,
        original_user_msg=message,
    )


class ReActLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_01_loop_executes_tools_and_feeds_real_observations(self) -> None:
        model = ScriptedChatModel(
            script=[
                turn(
                    "思考：先把用户陈述的事实写入共享状态。",
                    record_user_facts={"gestational_week": 36, "symptoms": [{"symptom": "腹部坠胀"}]},
                ),
                turn("思考：调用确定性规则引擎评估风险。", assess_risk={"reason": "用户描述腹部坠胀"}),
                turn("最终回答：风险等级 MODERATE，建议 24 小时内产检。"),
            ]
        )
        agent = make_agent(model=model)

        result = await agent.run_turn("孕周36周，腹部坠胀")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(
            [entry["tool"] for entry in result.tool_calls], ["record_user_facts", "assess_risk"]
        )
        self.assertEqual([entry["step"] for entry in result.trace], [1, 2, 3])
        self.assertEqual(result.risk_level, "MODERATE")
        self.assertEqual(agent.session.state.pregnancy.gestational_week, 36.0)
        self.assertEqual([item.symptom for item in agent.session.state.symptoms], ["腹部坠胀"])

        # the model really received the tool observations (grounded reasoning)
        tool_messages = [message for message in model.calls[1] if message.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].name, "record_user_facts")
        self.assertIn("已写入共享状态", tool_messages[0].content)

        third_transcript = model.calls[2]
        third_tools = [message for message in third_transcript if message.role == "tool"]
        self.assertEqual([message.name for message in third_tools], ["record_user_facts", "assess_risk"])
        risk_observation = json.loads(third_tools[1].content)
        self.assertEqual(risk_observation["risk_level"], "MODERATE")
        self.assertTrue(risk_observation["ok"])

        # every executed action left an audit event
        self.assertGreaterEqual(len(agent.session.audit_events), len(result.tool_calls))

    async def test_02_unknown_tool_is_observed_and_loop_recovers(self) -> None:
        model = ScriptedChatModel(
            script=[
                turn("思考：先查一下天气。", weather_lookup={"city": "Taipei"}),
                turn("最终回答：抱歉，我只能处理孕产相关问题。"),
            ]
        )
        agent = make_agent(model=model)

        result = await agent.run_turn("今天天气如何")

        first_call = result.tool_calls[0]
        self.assertFalse(first_call["ok"])
        self.assertEqual(first_call["error"], "unknown_tool")
        self.assertIn("未知工具", first_call["observation"])
        self.assertEqual(result.status, "completed")
        self.assertIn("孕产", result.message)


class HumanInTheLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_03_high_risk_pauses_and_cannot_be_bypassed(self) -> None:
        agent = make_agent(user_id="u-high")

        result = await agent.run_turn(HIGH_CASE_MESSAGE)

        self.assertEqual(result.status, "needs_human_review")
        self.assertTrue(result.requires_human)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertIsNotNone(result.pending)
        self.assertEqual(result.pending["trigger_tool"], "assess_risk")
        self.assertIsNotNone(agent.session.pending_human)
        self.assertTrue(agent.session.state.risk_status.requires_human)

        actions = [event.action for event in agent.session.audit_events]
        self.assertIn("run_maternity_risk_evaluation", actions)
        self.assertNotIn("governance_audit_validation", actions)

        blocked = await agent.run_turn("那我先问点别的")
        self.assertEqual(blocked.status, "needs_human_review")
        self.assertIn("等待人工复核", blocked.message)

    async def test_04_human_approve_resumes_and_completes_flow(self) -> None:
        agent = make_agent(user_id="u-approve")
        paused = await agent.run_turn(HIGH_CASE_MESSAGE)
        self.assertEqual(paused.status, "needs_human_review")

        result = await agent.resume_turn(
            human_action="approve", reviewer="obgyn-01", note="已确认高危症状"
        )

        self.assertEqual(result.status, "completed")
        self.assertIn("人工复核结论", result.message)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertFalse(agent.session.state.risk_status.requires_human)
        self.assertIsNone(agent.session.pending_human)
        self.assertEqual(agent.session.state.follow_up["status"], "human_reviewed")

        actions = [event.action for event in agent.session.audit_events]
        self.assertIn("human_risk_review", actions)
        self.assertIn("governance_audit_validation", actions)

        review_event = next(
            event for event in agent.session.audit_events if event.action == "human_risk_review"
        )
        self.assertEqual(review_event.risk_level, "HIGH")
        self.assertFalse(review_event.human_required)

        governance_event = next(
            event
            for event in agent.session.audit_events
            if event.action == "governance_audit_validation"
        )
        self.assertIn("compliance=COMPLIANT", governance_event.decision)

    async def test_05_human_reject_and_override_paths(self) -> None:
        reject_agent = make_agent(user_id="u-reject")
        await reject_agent.run_turn(HIGH_CASE_MESSAGE)
        rejected = await reject_agent.resume_turn(
            human_action="reject", reviewer="nurse-02", note="人工判定为误报"
        )
        self.assertEqual(rejected.status, "completed")
        self.assertEqual(reject_agent.session.state.risk_status.level, "LOW")

        override_agent = make_agent(user_id="u-override")
        await override_agent.run_turn(HIGH_CASE_MESSAGE)
        overridden = await override_agent.resume_turn(
            human_action="override",
            reviewer="obgyn-03",
            revised_risk_level="MODERATE",
            note="复查后降级",
        )
        self.assertEqual(overridden.status, "completed")
        self.assertEqual(override_agent.session.state.risk_status.level, "MODERATE")
        self.assertEqual(override_agent.session.human_review["revised_risk_level"], "MODERATE")

    async def test_06_invalid_human_decisions_are_rejected(self) -> None:
        idle_agent = make_agent(user_id="u-idle")
        with self.assertRaises(RuntimeError):
            await idle_agent.resume_turn(human_action="approve", reviewer="x")

        agent = make_agent(user_id="u-invalid")
        await agent.run_turn(HIGH_CASE_MESSAGE)
        with self.assertRaises(ValueError):
            await agent.resume_turn(human_action="escalate_now", reviewer="x")
        with self.assertRaises(ValueError):
            await agent.resume_turn(human_action="override", reviewer="x")
        # a rejected decision must not change the tier
        self.assertEqual(agent.session.state.risk_status.level, "HIGH")
        self.assertIsNotNone(agent.session.pending_human)


class AgentSafetyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_07_max_steps_guard_stops_runaway_loops(self) -> None:
        model = ScriptedChatModel(
            script=[],
            fallback="（不应被使用）",
            fallback_tool_calls=[{"name": "read_user_state", "arguments": {}}],
        )
        agent = make_agent(model=model, max_steps=3)

        result = await agent.run_turn("评估风险")

        self.assertEqual(result.status, "completed")
        self.assertTrue(any(entry.get("note") == "max_steps_reached" for entry in result.trace))
        self.assertEqual(len(result.tool_calls), 3)
        self.assertIn("暂停", result.message)

    async def test_08_persist_session_is_consent_gated(self) -> None:
        registry = build_default_registry()

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = SessionStore(run_dir=tmp_dir)
            denied_agent = make_agent(user_id="u-nostorage", store=store)
            denied_agent.session.state.consent = Consent(allow_record_storage=False)

            denied = await registry.invoke("persist_session", {}, make_context(denied_agent))

            self.assertFalse(denied.ok)
            self.assertEqual(denied.error, "consent_denied")
            self.assertEqual(list(Path(tmp_dir).glob("*.json")), [])
            self.assertEqual(
                [event.action for event in denied.audit_events], ["persist_session_denied"]
            )

            allowed_agent = make_agent(user_id="u-storage", store=store)
            allowed = await registry.invoke("persist_session", {}, make_context(allowed_agent))

            self.assertTrue(allowed.ok)
            saved = list(Path(tmp_dir).glob("*.json"))
            self.assertEqual(len(saved), 1)
            payload = json.loads(saved[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["user_id"], "u-storage")
            self.assertNotIn("messages", payload)

    async def test_09_grounding_correction_overrides_model_risk_claims(self) -> None:
        agent = make_agent(user_id="u-grounding")
        await agent.run_turn(HIGH_CASE_MESSAGE)
        await agent.resume_turn(human_action="approve", reviewer="obgyn-01")

        # A model that tries to soften the tier must not be able to.
        agent.model = ScriptedChatModel(script=[turn("结论：风险等级 LOW，一切正常。")])
        result = await agent.run_turn("再确认一下")

        self.assertIn("确定性规则引擎结论为 HIGH", result.message)
        self.assertEqual(result.risk_level, "HIGH")
        self.assertIn(
            "risk_level_grounding_correction",
            [event.action for event in agent.session.audit_events],
        )

    async def test_10_offline_model_extracts_only_stated_facts(self) -> None:
        facts = extract_facts("孕周36周，腹部坠胀，报告说血压偏高，不要保存记录")

        self.assertEqual(facts["gestational_week"], 36.0)
        self.assertEqual([item["symptom"] for item in facts["symptoms"]], ["腹部坠胀"])
        self.assertEqual(
            facts["report_flags"], [{"report_type": "user_reported", "markers": {"bp_high": True}}]
        )
        self.assertEqual(facts["consent"], {"allow_record_storage": False})
        self.assertNotIn("medical_history", facts)

    async def test_11_state_merge_semantics(self) -> None:
        state = make_state(symptoms=["阴道出血"])

        merged = apply_state_update(
            state,
            {
                "symptoms": [{"symptom": "腹部坠胀", "severity": "moderate"}],
                "consent": {"allow_external_escalation": False},
                "pregnancy": {"gestational_week": 39.5},
                "unknown_field": "ignored",
            },
        )

        self.assertEqual([item.symptom for item in merged.symptoms], ["阴道出血", "腹部坠胀"])
        self.assertFalse(merged.consent.allow_external_escalation)
        self.assertTrue(merged.consent.allow_record_storage)
        self.assertEqual(merged.pregnancy.gestational_week, 39.5)
        self.assertFalse(hasattr(merged, "unknown_field"))

    async def test_12_registry_has_no_egress_capabilities(self) -> None:
        names = set(build_default_registry().names())

        self.assertEqual(names, ALLOWED_TOOLS)
        for name in names:
            for forbidden in ("http", "web", "email", "sms"):
                self.assertNotIn(forbidden, name)

    async def test_13_tools_do_not_mutate_state_without_merge(self) -> None:
        registry = build_default_registry()
        agent = make_agent(user_id="u-merge")
        context = make_context(agent)

        result = await registry.invoke(
            "record_user_facts", {"medical_history": ["子痫前期"]}, context
        )

        self.assertTrue(result.ok)
        self.assertEqual(context.state.medical_history, [])  # the tool only returns a patch
        self.assertIn("medical_history", result.state_update)
        agent.session.state = apply_state_update(agent.session.state, result.state_update)
        self.assertEqual(agent.session.state.medical_history, ["子痫前期"])


class SessionStateTests(unittest.TestCase):
    def test_14_session_snapshot_redacts_credentials(self) -> None:
        from reprojourney.agents.main_agent import redact

        payload = {
            "api_key": "sk-secret",
            "nested": {"password": "p@ss", "keep": "value"},
            "items": [{"token": "abc", "n": 1}],
        }

        redacted = redact(payload)

        self.assertEqual(redacted["api_key"], "***REDACTED***")
        self.assertEqual(redacted["nested"]["password"], "***REDACTED***")
        self.assertEqual(redacted["nested"]["keep"], "value")
        self.assertEqual(redacted["items"][0]["token"], "***REDACTED***")

    def test_15_session_state_round_trip_keeps_risk_level(self) -> None:
        session = SessionState.new("u-roundtrip")
        session.state.risk_status = RiskStatus(
            level="HIGH", reasons=["存在高危症状：阴道出血"], requires_human=True
        )

        self.assertEqual(session.risk_level, "HIGH")
        self.assertEqual(session.snapshot_dict()["risk_level"], "HIGH")


if __name__ == "__main__":
    unittest.main()


