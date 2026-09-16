"""Deterministic offline policy model.

Why it exists: the project must runnable with *zero* API keys (CI, grading,
demo), while still exercising the real ReAct machinery -- multi-step tool
calling, observations, HITL pause/resume and the governance gate.

It is **not** a mock of the agent: it is a real :class:`BaseChatModel`
implementation that decides the next tool call from the transcript. Because it
is deterministic it is also the fixture used by the agent-loop unit tests.

Safety: it never invents risk tiers. It only (a) extracts facts the user
explicitly stated, (b) routes them through the deterministic tools, and
(c) summarises whatever the tools returned.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from reprojourney.schemas.risk_rules import (
    HIGH_HISTORY_ITEMS,
    REPORT_PHRASE_MARKERS,
    SYMPTOM_SEVERITY,
)

from .types import BaseChatModel, ChatMessage, LLMResponse, ToolCall, ToolSpec

HUMAN_NOTE_PREFIX = "[HUMAN_REVIEW]"

FACT_TOOL = "record_user_facts"
READ_TOOL = "read_user_state"
RISK_TOOL = "assess_risk"
GOV_TOOL = "audit_governance"
HUMAN_TOOL = "request_human_review"

_WEEK_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:周|週|weeks?|wks?|w)\b", re.IGNORECASE),
    re.compile(r"gestational[\s_]*week[^0-9]{0,6}(\d+(?:\.\d+)?)", re.IGNORECASE),
)

_CONSENT_RULES: Tuple[Tuple[Tuple[str, ...], str, bool], ...] = (
    (("不授权外部升级", "不允许外部升级", "不要升级", "拒绝外部升级", "不要外部升级"), "allow_external_escalation", False),
    (("允许外部升级", "授权外部升级", "可以外部升级", "同意外部升级"), "allow_external_escalation", True),
    (("不要通知家人", "不要通知家属", "不通知家属", "不要告诉家人"), "allow_family_notification", False),
    (("可以通知家人", "可以通知家属", "通知家属", "通知家人"), "allow_family_notification", True),
    (("不要保存", "不要记录", "不保存记录", "不要留痕"), "allow_record_storage", False),
    (("可以保存", "保存记录", "允许记录", "可以留痕"), "allow_record_storage", True),
)

_GOVERNANCE_KEYWORDS = ("审计", "治理", "合规", "留痕", "audit", "compliance")
_HUMAN_REVIEW_KEYWORDS = ("人工", "医生复核", "护士复核", "找人确认", "人工审核", "请医生")
#: Negations must win over the keyword match ("不要再提人工复核" is *not* a request).
_HUMAN_REVIEW_NEGATIONS = (
    "不要人工",
    "不用人工",
    "不需要人工",
    "不要提人工",
    "别提人工",
    "不要转人工",
    "不用转人工",
)
#: Generic negation prefixes; a keyword within 6 characters counts as negated.
_NEGATION_PREFIXES = ("不要", "不用", "不需要", "不必", "拒绝", "别")
_CLINICAL_KEYWORDS = (
    "孕",
    "风险",
    "评估",
    "症状",
    "出血",
    "破水",
    "宫缩",
    "腹痛",
    "头晕",
    "水肿",
    "报告",
    "检查",
    "指标",
    "血压",
    "血糖",
    "蛋白尿",
)


def _wants_human_review(text: str) -> bool:
    """True only when the user actually asks for human involvement.

    Chinese negations are frequent and can interleave words
    ("不要**再**提人工复核"), so an explicit list alone is not enough: any
    negation within a few characters before the keyword also counts as a no.
    """

    if not any(keyword in text for keyword in _HUMAN_REVIEW_KEYWORDS):
        return False
    if any(negation in text for negation in _HUMAN_REVIEW_NEGATIONS):
        return False
    for negation in _NEGATION_PREFIXES:
        start = text.find(negation)
        if start == -1:
            continue
        for keyword in ("人工", "医生", "护士"):
            position = text.find(keyword, start)
            if position != -1 and position - start <= 6:
                return False
    return True


def _find_phrases(text: str, phrases: Sequence[str]) -> List[str]:
    lowered = text.lower()
    found: List[str] = []
    for phrase in phrases:
        if phrase.lower() in lowered and phrase not in found:
            found.append(phrase)
    return found


def _extract_week(text: str) -> Optional[float]:
    for pattern in _WEEK_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def extract_facts(text: str) -> Dict[str, Any]:
    """Extract only facts the user explicitly stated (no inference)."""

    facts: Dict[str, Any] = {}

    week = _extract_week(text)
    if week is not None:
        facts["gestational_week"] = week

    symptom_names = _find_phrases(text, sorted(SYMPTOM_SEVERITY, key=len, reverse=True))
    if symptom_names:
        facts["symptoms"] = [
            {"symptom": name, "severity": SYMPTOM_SEVERITY[name]} for name in symptom_names
        ]

    history = _find_phrases(text, sorted(HIGH_HISTORY_ITEMS, key=len, reverse=True))
    if history:
        facts["medical_history"] = history

    markers = {
        marker: True
        for phrase, marker in sorted(REPORT_PHRASE_MARKERS.items(), key=lambda item: len(item[0]), reverse=True)
        if phrase.lower() in text.lower()
    }
    if markers:
        facts["report_flags"] = [{"report_type": "user_reported", "markers": markers}]

    consent: Dict[str, bool] = {}
    for phrases, field, value in _CONSENT_RULES:
        if field in consent:
            continue
        if any(phrase in text for phrase in phrases):
            consent[field] = value
    if consent:
        facts["consent"] = consent

    return facts


def _turn_view(messages: Sequence[ChatMessage]) -> Tuple[str, List[str], Dict[str, Dict[str, Any]]]:
    """Return (last real user text, tools already called, last observation per tool)."""

    last_user_index = -1
    for index, message in enumerate(messages):
        if message.role == "user" and not message.content.startswith(HUMAN_NOTE_PREFIX):
            last_user_index = index

    user_text = messages[last_user_index].content if last_user_index >= 0 else ""

    called: List[str] = []
    observations: Dict[str, Dict[str, Any]] = {}
    for message in messages[last_user_index + 1 :]:
        if message.role == "assistant":
            called.extend(call.name for call in message.tool_calls)
        elif message.role == "tool" and message.name:
            try:
                observations[message.name] = json.loads(message.content)
            except (TypeError, ValueError):
                observations[message.name] = {"observation": message.content}
    return user_text, called, observations


class OfflinePolicyChatModel(BaseChatModel):
    """Deterministic planner used when no LLM credentials are configured."""

    name = "offline-policy"

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> LLMResponse:
        available = {spec.name for spec in (tools or [])}
        user_text, called, observations = _turn_view(messages)
        facts = extract_facts(user_text)

        def call(tool: str, arguments: Dict[str, Any], thought: str) -> LLMResponse:
            return LLMResponse(
                content=thought,
                tool_calls=[
                    ToolCall(id=f"call-{len(called) + 1}-{tool}", name=tool, arguments=arguments)
                ],
                finish_reason="tool_calls",
                model=self.name,
            )

        # 1) Persist explicitly stated facts before reasoning about them.
        if facts and FACT_TOOL in available and FACT_TOOL not in called:
            return call(
                FACT_TOOL,
                facts,
                "思考：用户提出了明确的孕期事实，我先把它结构化写入共享状态，避免后续推断。",
            )

        # 2) Explicit request for a human reviewer (negations win).
        wants_human = _wants_human_review(user_text)
        if wants_human and HUMAN_TOOL in available and HUMAN_TOOL not in called:
            return call(
                HUMAN_TOOL,
                {
                    "reason": "用户明确希望由人工复核",
                    "question": "请人工复核用户请求并确认后续处理方式。",
                },
                "思考：用户希望人工介入，我调用人工复核工具并暂停自动流程。",
            )

        clinical = bool(facts) or any(keyword in user_text for keyword in _CLINICAL_KEYWORDS)

        # 3) Risk tier must come from the deterministic engine.
        if clinical and RISK_TOOL in available and RISK_TOOL not in called:
            return call(
                RISK_TOOL,
                {"reason": "用户请求/描述涉及孕产风险信号"},
                "思考：这属于孕产风险场景，风险等级必须由确定性规则引擎给出，我调用风险评估工具。",
            )

        # 4) Every risk decision is followed by the governance check.
        if RISK_TOOL in called and GOV_TOOL in available and GOV_TOOL not in called:
            return call(
                GOV_TOOL,
                {"reason": "风险评估后执行强制治理审计"},
                "思考：风险结论已经产生，我调用治理与审计工具校验留痕、授权与隐私合规。",
            )

        # 5) Standalone governance/audit request.
        if (
            any(keyword in user_text.lower() for keyword in _GOVERNANCE_KEYWORDS)
            and GOV_TOOL in available
            and GOV_TOOL not in called
        ):
            return call(
                GOV_TOOL,
                {"reason": "用户请求审计与合规检查"},
                "思考：用户要求审计/合规检查，我调用治理与审计工具读取审计链路。",
            )

        return LLMResponse(
            content=_compose_final_answer(observations, facts),
            tool_calls=[],
            finish_reason="stop",
            model=self.name,
        )


def _compose_final_answer(observations: Dict[str, Dict[str, Any]], facts: Dict[str, Any]) -> str:
    """Localised summary grounded strictly in tool observations."""

    risk_payload = observations.get(RISK_TOOL) or {}
    risk_data = risk_payload.get("data") or {}
    gov_payload = observations.get(GOV_TOOL) or {}
    gov_data = gov_payload.get("data") or {}

    lines: List[str] = []
    level = risk_data.get("risk_level")
    if level:
        lines.append(f"风险评估结论：{level}。")
        reasons = risk_data.get("reasons") or []
        if reasons:
            lines.append("主要依据：" + "；".join(str(reason) for reason in reasons) + "。")
        if level == "HIGH":
            lines.append("该结论已进入人工复核流程，请以医护人员的判断为准。")
        elif level == "UNKNOWN":
            lines.append("目前结构化信息不足，请补充孕周、症状出现时间与最近检查结果。")
        else:
            lines.append("建议按产检计划继续随访，出现新症状请及时告知。")
    elif facts:
        lines.append("我已记录你提供的信息，可继续补充孕周、症状或检查结果以便评估。")
    else:
        lines.append(
            "我可以帮你做孕产风险初筛、记录孕期信息，并检查数据与流程合规情况。"
            "请告诉我孕周、症状或最近的检查结果。"
        )

    if gov_data:
        issues = gov_data.get("issues") or []
        status = gov_data.get("compliance_status", "UNKNOWN")
        if issues:
            lines.append(f"治理审计：{status}，发现 {len(issues)} 项需处理：" + "；".join(str(item) for item in issues))
        else:
            lines.append(f"治理审计：{status}，未发现合规问题。")

    lines.append("说明：以上为流程性风险提示，不构成医学诊断；出现出血、破水、剧烈腹痛等情况请立即就医。")
    return "\n".join(lines)

