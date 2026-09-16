"""Tool layer (standard section 7/8): deterministic capabilities the Agents call.

Design rules:
  * every tool is a small, auditable unit with a JSON schema and an
    :class:`AuditEvent` -- the standard's "each action is traceable" requirement;
  * tools return a ``state_update`` patch instead of mutating ``SharedUserState``
    (the session/orchestrator performs the merge);
  * tools never call an LLM, so their behaviour is deterministic and testable;
  * safety-relevant tools (risk, audit, human review, persistence) are built in;
    anything else can be attached through :class:`ToolRegistry`;
  * the layer sits next to ``agents/`` (not inside it), so an agent calls a tool
    instead of *being* the tool -- standard section 8 ("Agent 与 Tool 必须分开").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from reprojourney.llm.types import ToolSpec
from reprojourney.schemas.base_schemas import (
    AgentInput,
    AgentOutput,
    AuditEvent,
    SharedUserState,
)
from reprojourney.schemas.risk_rules import SYMPTOM_SEVERITY

# NOTE (layering): the two agents this layer drives are imported *inside* the
# handlers that need them, never at module level.  The agents import this
# module in turn (``MainAgent`` builds the registry), so a module-level import
# here would create a cycle -- exactly the boundary the standard's section 8
# draws: tools do not depend on agents, agents depend on tools.

MAX_LIST_ITEMS = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _short(value: Any, limit: int = 160) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class ToolResult:
    """Observation returned to the model plus machine-readable side effects."""

    tool: str
    ok: bool
    observation: str
    data: Dict[str, Any] = field(default_factory=dict)
    risk_level: str = "UNKNOWN"
    requires_human: bool = False
    state_update: Dict[str, Any] = field(default_factory=dict)
    audit_events: List[AuditEvent] = field(default_factory=list)
    error: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "observation": _short(self.observation, 600),
            "data": self.data,
            "risk_level": self.risk_level,
            "requires_human": self.requires_human,
            "state_update_fields": sorted(self.state_update.keys()),
            "error": self.error,
        }

    def to_message(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False)


@dataclass
class ToolContext:
    """Per-turn context handed to every tool."""

    request_id: str
    user_id: str
    state: SharedUserState
    audit_events: List[AuditEvent] = field(default_factory=list)
    sub_agent_outputs: List[AgentOutput] = field(default_factory=list)
    human_review: Optional[Dict[str, Any]] = None
    store: Any = None
    session: Any = None
    original_user_msg: Optional[str] = None

    def add_events(self, events: Sequence[AuditEvent]) -> None:
        self.audit_events.extend(events)

    def new_event(
        self,
        *,
        agent: str,
        action: str,
        decision: str,
        data_accessed: Sequence[str],
        tool_used: Optional[str] = None,
        source: Sequence[str] = ("main_agent",),
        risk_level: str = "UNKNOWN",
        human_required: bool = False,
    ) -> AuditEvent:
        return AuditEvent(
            request_id=self.request_id,
            agent=agent,
            action=action,
            data_accessed=list(data_accessed),
            tool_used=tool_used,
            source=list(source),
            decision=decision,
            risk_level=risk_level,
            human_required=human_required,
        )


ToolHandler = Callable[[Dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class Tool:
    """A single callable capability exposed to the model."""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


class ToolRegistry:
    """Ordered tool registry with a dispatch-by-name entry point."""

    def __init__(self, tools: Optional[Sequence[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def specs(self, exclude: Optional[Sequence[str]] = None) -> List[ToolSpec]:
        excluded = set(exclude or [])
        return [tool.spec for name, tool in self._tools.items() if name not in excluded]

    async def invoke(
        self, name: str, arguments: Optional[Dict[str, Any]], context: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                tool=name,
                ok=False,
                observation=f"未知工具：{name}。可用工具：{', '.join(self.names())}",
                error="unknown_tool",
            )
        try:
            return await tool.handler(_as_dict(arguments), context)
        except Exception as exc:  # a failing tool must never break the loop
            return ToolResult(
                tool=name,
                ok=False,
                observation=f"工具 {name} 执行失败：{exc}",
                error=f"tool_error: {type(exc).__name__}",
            )


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------


async def read_user_state(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Read the shared state (read-only, always audited)."""

    state = ctx.state
    summary: Dict[str, Any] = {
        "user_id": state.user_id,
        "age": state.profile.age if state.profile else None,
        "gestational_week": state.pregnancy.gestational_week if state.pregnancy else None,
        "symptoms": [item.symptom for item in state.symptoms],
        "medical_history": list(state.medical_history),
        "reports": [
            {"report_id": report.report_id, "markers": sorted((report.extracted_data or {}).keys())}
            for report in state.reports
        ],
        "mood": state.mood,
        "risk_status": state.risk_status.model_dump(mode="json") if state.risk_status else None,
        "follow_up": state.follow_up,
        "consent": state.consent.model_dump(mode="json") if state.consent else None,
        "consultations": len(state.consultation_history),
    }
    event = ctx.new_event(
        agent="session_state",
        action="read_user_state",
        decision="读取共享用户状态以支撑本回合决策",
        data_accessed=["shared_user_state"],
        tool_used="read_user_state",
        risk_level=state.risk_status.level if state.risk_status else "UNKNOWN",
    )
    ctx.add_events([event])
    return ToolResult(
        tool="read_user_state",
        ok=True,
        observation="当前共享状态：" + json.dumps(summary, ensure_ascii=False),
        data=summary,
        risk_level=state.risk_status.level if state.risk_status else "UNKNOWN",
        audit_events=[event],
    )


async def record_user_facts(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Persist facts the user explicitly stated (deduplicated, validated)."""

    state = ctx.state
    patch: Dict[str, Any] = {}
    recorded: Dict[str, Any] = {}
    skipped: List[str] = []

    week = args.get("gestational_week")
    if week is not None:
        try:
            week_value = float(week)
        except (TypeError, ValueError):
            return ToolResult(
                tool="record_user_facts",
                ok=False,
                observation="gestational_week 必须是数字（例如 36 或 36.5）",
                error="invalid_argument",
            )
        if not 0 < week_value <= 45:
            return ToolResult(
                tool="record_user_facts",
                ok=False,
                observation=f"孕周 {week_value} 超出合理范围（0-45），请向用户确认",
                error="out_of_range",
            )
        patch["pregnancy"] = {"gestational_week": week_value}
        recorded["gestational_week"] = week_value

    symptom_patch: List[Dict[str, Any]] = []
    for item in _as_list(args.get("symptoms")):
        data = _as_dict(item) if isinstance(item, dict) else {"symptom": str(item)}
        name = str(data.get("symptom") or "").strip()
        if not name:
            continue
        if any((existing.symptom or "") == name for existing in state.symptoms):
            skipped.append(f"症状已记录：{name}")
            continue
        symptom_patch.append(
            {
                "symptom": name,
                "severity": str(data.get("severity") or SYMPTOM_SEVERITY.get(name) or "unknown"),
                "timestamp": _now(),
            }
        )
    if symptom_patch:
        patch["symptoms"] = symptom_patch[:MAX_LIST_ITEMS]
        recorded["symptoms"] = [item["symptom"] for item in symptom_patch]

    history_patch: List[str] = []
    for item in _as_list(args.get("medical_history")):
        name = str(item).strip()
        if not name:
            continue
        if name in state.medical_history:
            skipped.append(f"病史已记录：{name}")
            continue
        history_patch.append(name)
    if history_patch:
        patch["medical_history"] = history_patch[:MAX_LIST_ITEMS]
        recorded["medical_history"] = history_patch

    report_patch: List[Dict[str, Any]] = []
    for entry in _as_list(args.get("report_flags")):
        data = _as_dict(entry)
        markers = {str(key): value for key, value in _as_dict(data.get("markers")).items() if value is True}
        if not markers:
            continue
        if any((report.extracted_data or {}) == markers for report in state.reports):
            skipped.append(f"报告标记已记录：{sorted(markers)}")
            continue
        index = len(state.reports) + len(report_patch) + 1
        report_patch.append(
            {
                "report_id": f"user-report-{index}",
                "report_type": str(data.get("report_type") or "user_reported"),
                "report_date": str(data.get("report_date") or _now()[:10]),
                "extracted_data": markers,
                "source_file": "user_message",
            }
        )
    if report_patch:
        patch["reports"] = report_patch
        recorded["reports"] = [item["report_id"] for item in report_patch]

    consent_args = _as_dict(args.get("consent"))
    consent_patch = {
        field_name: consent_args[field_name]
        for field_name in ("allow_record_storage", "allow_external_escalation", "allow_family_notification")
        if isinstance(consent_args.get(field_name), bool)
    }
    if consent_patch:
        patch["consent"] = consent_patch
        recorded["consent"] = consent_patch

    mood = args.get("mood")
    if isinstance(mood, str) and mood.strip():
        patch["mood"] = mood.strip()
        recorded["mood"] = patch["mood"]

    appointments = [_as_dict(item) for item in _as_list(args.get("appointments")) if _as_dict(item)]
    if appointments:
        patch["appointments"] = appointments[:MAX_LIST_ITEMS]
        recorded["appointments"] = len(appointments)
        if not (state.consent and state.consent.allow_record_storage is True):
            skipped.append("预约信息需要记录存储授权，已标记待确认")

    follow_up = _as_dict(args.get("follow_up"))
    if follow_up:
        patch["follow_up"] = follow_up
        recorded["follow_up"] = sorted(follow_up.keys())

    notes = args.get("notes")
    if isinstance(notes, str) and notes.strip():
        note_text = notes.strip()[:500]
        if any(record.query == note_text for record in state.consultation_history):
            skipped.append("咨询记录重复，未重复写入")
        else:
            patch["consultation_history"] = [
                {
                    "query": note_text,
                    "response": None,
                    "evidence": [],
                    "timestamp": _now(),
                }
            ]
            recorded["consultation_history"] = 1

    current_level = state.risk_status.level if state.risk_status else "UNKNOWN"
    if not patch:
        return ToolResult(
            tool="record_user_facts",
            ok=False,
            observation=(
                "没有可写入的结构化事实。请提供孕周（gestational_week）、症状（symptoms）、"
                "病史（medical_history）、报告标记（report_flags）或授权（consent）。"
            ),
            error="empty_facts",
            risk_level=current_level,
        )

    event = ctx.new_event(
        agent="session_state",
        action="record_user_facts",
        decision=f"写入事实字段={sorted(recorded.keys())}; 跳过={len(skipped)}项",
        data_accessed=["shared_user_state", "user_message"],
        tool_used="record_user_facts",
        risk_level=current_level,
    )
    ctx.add_events([event])
    observation = "已写入共享状态：" + json.dumps(recorded, ensure_ascii=False)
    if skipped:
        observation += "；已跳过：" + "；".join(skipped)

    return ToolResult(
        tool="record_user_facts",
        ok=True,
        observation=observation,
        data={"recorded": recorded, "skipped": skipped},
        risk_level=current_level,
        state_update=patch,
        audit_events=[event],
    )


async def assess_risk(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Delegate the risk tier to Agent 4 (deterministic rule engine)."""

    from reprojourney.agents.risk.agent4_risk_assessment import RiskAssessmentAgent

    agent = RiskAssessmentAgent()
    agent_input = AgentInput(
        request_id=ctx.request_id,
        user_id=ctx.user_id,
        task="run_risk_assessment",
        user_message=ctx.original_user_msg or str(args.get("reason") or "") or None,
        state=ctx.state,
        context={
            "audit_events": list(ctx.audit_events),
            "original_user_msg": ctx.original_user_msg,
            "source": "main_agent_react_loop",
            "requested_tool": "assess_risk",
        },
        requested_by="orchestrator",
    )
    output = await agent.run(agent_input)
    ctx.sub_agent_outputs.append(output)
    ctx.add_events(output.audit_events)

    return ToolResult(
        tool="assess_risk",
        ok=output.status != "error",
        observation=output.summary,
        data={
            "agent": output.agent,
            "status": output.status,
            "risk_level": output.risk_level,
            "reasons": output.evidence,
            "next_action": output.next_action,
        },
        risk_level=output.risk_level,
        requires_human=output.requires_human,
        state_update=output.state_update,
        audit_events=output.audit_events,
        error=output.error,
    )


async def audit_governance(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Delegate the compliance gate to Agent 5 (audit + consent + privacy)."""

    from reprojourney.agents.governance.agent5_governance_audit import GovernanceAuditAgent

    agent = GovernanceAuditAgent()
    agent_input = AgentInput(
        request_id=ctx.request_id,
        user_id=ctx.user_id,
        task="run_governance_audit",
        user_message=ctx.original_user_msg or str(args.get("reason") or "") or None,
        state=ctx.state,
        context={
            "audit_events": list(ctx.audit_events),
            "human_review": ctx.human_review,
            "original_user_msg": ctx.original_user_msg,
            "source": "main_agent_react_loop",
        },
        requested_by="orchestrator",
    )
    output, payload = await agent.run_with_payload(agent_input)
    ctx.sub_agent_outputs.append(output)
    ctx.add_events(output.audit_events)

    return ToolResult(
        tool="audit_governance",
        ok=payload["critical_count"] == 0,
        observation=output.summary + "；" + "；".join(output.evidence),
        data={
            "compliance_status": payload["compliance_status"],
            "policy_version": payload["policy_version"],
            "events_checked": payload["events_checked"],
            "critical_count": payload["critical_count"],
            "warning_count": payload["warning_count"],
            "issues": payload["issues"],
            "rule_ids": payload["rule_ids"],
        },
        risk_level=output.risk_level,
        requires_human=output.requires_human,
        state_update=output.state_update,
        audit_events=output.audit_events,
    )


async def request_human_review(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Pause automation and hand the case to a human reviewer (HITL)."""

    question = str(args.get("question") or "请人工复核当前情况并给出处理意见。")
    reason = str(args.get("reason") or "智能体请求人工介入")
    current_level = ctx.state.risk_status.level if ctx.state.risk_status else "UNKNOWN"

    event = ctx.new_event(
        agent="main_agent",
        action="request_human_review",
        decision=f"human_review_requested: {reason}",
        data_accessed=["shared_user_state", "human_review"],
        tool_used="request_human_review",
        risk_level=current_level,
        human_required=True,
    )
    ctx.add_events([event])

    return ToolResult(
        tool="request_human_review",
        ok=True,
        observation=f"已创建人工复核任务并暂停自动流程。待人工回答：{question}",
        data={"question": question, "reason": reason, "risk_level": current_level},
        risk_level=current_level,
        requires_human=True,
        audit_events=[event],
    )


async def persist_session(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Write a redacted session snapshot -- consent-gated and audited."""

    if ctx.store is None or ctx.session is None:
        return ToolResult(
            tool="persist_session",
            ok=False,
            observation="当前会话未启用本地快照存储。",
            error="store_disabled",
        )

    consent = ctx.state.consent
    if consent is not None and consent.allow_record_storage is False:
        event = ctx.new_event(
            agent="local_filesystem",
            action="persist_session_denied",
            decision="用户未授权记录存储（consent.allow_record_storage=false），已拒绝落盘",
            data_accessed=["consent", "shared_user_state"],
            tool_used="persist_session",
            source=["main_agent", "local_filesystem"],
        )
        ctx.add_events([event])
        return ToolResult(
            tool="persist_session",
            ok=False,
            observation="用户未授权记录存储，已拒绝写入本地会话快照。",
            error="consent_denied",
            audit_events=[event],
        )

    path = ctx.store.save(ctx.session)
    if path is None:
        return ToolResult(
            tool="persist_session",
            ok=False,
            observation="本地快照存储已被配置关闭。",
            error="store_disabled",
        )

    relative_path = path.as_posix()
    event = ctx.new_event(
        agent="local_filesystem",
        action="persist_state_snapshot",
        decision=f"会话快照已写入相对路径 {relative_path}（凭证字段已脱敏）",
        data_accessed=["shared_user_state", "audit_events"],
        tool_used="persist_session",
        source=["main_agent", "local_filesystem"],
        risk_level=ctx.state.risk_status.level if ctx.state.risk_status else "UNKNOWN",
    )
    ctx.add_events([event])

    return ToolResult(
        tool="persist_session",
        ok=True,
        observation=f"会话快照已保存：{relative_path}（敏感字段已脱敏）",
        data={"path": relative_path},
        audit_events=[event],
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_EMPTY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_RECORD_FACTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "gestational_week": {"type": "number", "description": "孕周，例如 36 或 36.5"},
        "symptoms": {
            "type": "array",
            "description": "用户明确描述的症状，不要推断或补充",
            "items": {
                "type": "object",
                "properties": {
                    "symptom": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["symptom"],
            },
        },
        "medical_history": {"type": "array", "items": {"type": "string"}},
        "report_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "report_type": {"type": "string"},
                    "report_date": {"type": "string"},
                    "markers": {
                        "type": "object",
                        "description": "结构化异常标记，例如 {\"bp_high\": true, \"proteinuria\": true}",
                        "additionalProperties": {"type": "boolean"},
                    },
                },
                "required": ["markers"],
            },
        },
        "consent": {
            "type": "object",
            "properties": {
                "allow_record_storage": {"type": "boolean"},
                "allow_external_escalation": {"type": "boolean"},
                "allow_family_notification": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "mood": {"type": "string"},
        "appointments": {"type": "array", "items": {"type": "object"}},
        "follow_up": {"type": "object"},
        "notes": {"type": "string", "description": "咨询要点，写入 consultation_history"},
    },
}


def build_default_registry() -> ToolRegistry:
    """All built-in capabilities of the ReAct main agent."""

    return ToolRegistry(
        [
            Tool(
                name="read_user_state",
                description=(
                    "读取当前用户的共享状态（孕周、症状、病史、报告标记、风险等级、授权状态、随访）。"
                    "只读操作，不会修改任何数据。"
                ),
                parameters=_EMPTY_SCHEMA,
                handler=read_user_state,
            ),
            Tool(
                name="record_user_facts",
                description=(
                    "把用户明确陈述的事实（孕周、症状、病史、报告异常标记、授权、就诊安排、咨询要点）"
                    "结构化写入共享状态。禁止写入推断、猜测或模型自行补全的内容。"
                ),
                parameters=_RECORD_FACTS_SCHEMA,
                handler=record_user_facts,
            ),
            Tool(
                name="assess_risk",
                description=(
                    "调用确定性孕产风险规则引擎（Agent 4）得出风险等级。"
                    "风险等级只能来自该工具；返回 requires_human=true 时流程会自动暂停等待人工复核。"
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"reason": {"type": "string", "description": "本次评估的触发原因"}},
                },
                handler=assess_risk,
            ),
            Tool(
                name="audit_governance",
                description=(
                    "调用治理与审计智能体（Agent 5）校验审计链路完整性、Consent 授权、隐私合规与 HITL 记录。"
                    "风险结论产生后必须调用一次。"
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"reason": {"type": "string", "description": "本次审计的触发原因"}},
                },
                handler=audit_governance,
            ),
            Tool(
                name="request_human_review",
                description=(
                    "请求人工复核并暂停自动流程。适用于高风险结论、合规风险，或用户主动要求人工介入。"
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "reason": {"type": "string"},
                        "question": {"type": "string", "description": "希望人工确认或回答的具体问题"},
                    },
                },
                handler=request_human_review,
            ),
            Tool(
                name="persist_session",
                description=(
                    "把脱敏后的会话快照写入本地 runs/ 目录（相对路径）。仅在用户授权记录存储时允许执行。"
                ),
                parameters=_EMPTY_SCHEMA,
                handler=persist_session,
            ),
        ]
    )

