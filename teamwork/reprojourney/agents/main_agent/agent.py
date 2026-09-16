"""ReAct main agent: real thought -> action -> observation loop with HITL.

Compared to a keyword router, this implementation:
  * builds a real message transcript (system / user / assistant / tool) and
    iterates until the model stops calling tools or ``max_steps`` is reached;
  * executes every tool call against a registry and feeds the *actual*
    observation back so the next thought is grounded;
  * pauses the loop on any ``requires_human`` tool result, exposes a checkpoint,
    and resumes the *same* transcript after the human decision;
  * keeps the risky assertions in code: the risk tier always comes from Agent 4,
    compliance always passes through Agent 5, and every action is audited.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from reprojourney.config import Settings
from reprojourney.llm import ChatMessage, build_chat_model
from reprojourney.schemas.base_schemas import AuditEvent, RiskStatus, SharedUserState

from .prompts import HUMAN_NOTE_PREFIX, HUMAN_REVIEW_TEMPLATE, SYSTEM_PROMPT, build_tools_hint
from .session import SessionState, SessionStore, apply_state_update, redact
from reprojourney.tools.registry import ToolContext, ToolRegistry, ToolResult, build_default_registry

VALID_ACTIONS = {"approve", "override", "reject"}
VALID_LEVELS = {"LOW", "MODERATE", "HIGH", "UNKNOWN"}
_LEVEL_TOKEN = re.compile(r"\b(LOW|MODERATE|HIGH|UNKNOWN)\b")

_UNSET = object()


@dataclass
class TurnResult:
    """Outcome of one agent turn (or one resumed turn)."""

    request_id: str
    status: str  # completed | needs_human_review | error
    message: str
    risk_level: str
    requires_human: bool
    state: SharedUserState
    audit_events: List[AuditEvent] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    pending: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "message": self.message,
            "risk_level": self.risk_level,
            "requires_human": self.requires_human,
            "state": self.state.model_dump(mode="json"),
            "audit_events": [event.model_dump(mode="json") for event in self.audit_events],
            "trace": self.trace,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


class MainAgent:
    """Session-level ReAct orchestrator for a single user."""

    def __init__(
        self,
        user_id: str = "demo-user",
        *,
        model: Any = None,
        settings: Optional[Settings] = None,
        tools: Optional[ToolRegistry] = None,
        store: Any = _UNSET,
        session: Optional[SessionState] = None,
        max_steps: Optional[int] = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.model = model or build_chat_model(self.settings)
        self.tools = tools or build_default_registry()
        if store is _UNSET:
            self.store = SessionStore(self.settings.run_dir, self.settings.persist_sessions)
        else:
            self.store = store
        self.session = session or SessionState.new(user_id)
        self.user_id = self.session.user_id
        self.max_steps = max_steps or self.settings.max_react_steps
        self.transcript: List[ChatMessage] = []

    # -- context / prompt assembly -----------------------------------------
    def _new_context(self, request_id: str, original_user_msg: Optional[str] = None) -> ToolContext:
        return ToolContext(
            request_id=request_id,
            user_id=self.user_id,
            state=self.session.state,
            audit_events=self.session.audit_events,
            human_review=self.session.human_review,
            store=self.store,
            session=self.session,
            original_user_msg=original_user_msg,
        )

    def _session_context_block(self) -> str:
        consent = self.session.state.consent
        block = {
            "user_id": self.user_id,
            "turn_index": self.session.turn_index,
            "current_risk_level": self.session.risk_level,
            "ga_week": self.session.state.pregnancy.gestational_week if self.session.state.pregnancy else None,
            "consent": consent.model_dump(mode="json") if consent else None,
            "pending_human_review": self.session.pending_human is not None,
        }
        return "当前会话上下文：" + json.dumps(block, ensure_ascii=False)

    def _build_messages(self, user_message: str) -> List[ChatMessage]:
        messages = [
            ChatMessage.system(SYSTEM_PROMPT),
            ChatMessage.system(build_tools_hint(self.tools.names())),
            ChatMessage.system(self._session_context_block()),
        ]
        messages.extend(self.transcript[-6:])
        messages.append(ChatMessage.user(user_message))
        return messages

    # -- public API --------------------------------------------------------
    async def run_turn(self, user_message: str, request_id: Optional[str] = None) -> TurnResult:
        """Run one full ReAct turn for ``user_message``."""

        if self.session.pending_human is not None:
            pending = self.session.pending_human
            return TurnResult(
                request_id=pending.get("request_id", request_id or "unknown"),
                status="needs_human_review",
                message=(
                    "当前仍有任务在等待人工复核，请先完成复核："
                    f"{pending.get('question', '')}"
                ),
                risk_level=pending.get("risk_level", "UNKNOWN"),
                requires_human=True,
                state=self.session.state,
                audit_events=list(self.session.audit_events),
                tool_calls=list(self.session.tool_calls),
                pending={key: value for key, value in pending.items() if key != "messages"},
            )

        request_id = request_id or f"req-{uuid4().hex[:12]}"
        self.session.turn_index += 1
        context = self._new_context(request_id, user_message)
        messages = self._build_messages(user_message)
        self.transcript.append(ChatMessage.user(user_message))
        return await self._run_loop(messages, context, request_id)

    async def _run_loop(
        self, messages: List[ChatMessage], context: ToolContext, request_id: str
    ) -> TurnResult:
        """The actual ReAct loop: think -> act -> observe, until an answer or HITL."""

        trace: List[Dict[str, Any]] = []
        turn_tool_calls: List[Dict[str, Any]] = []
        final_text = ""
        error: Optional[str] = None

        for step in range(1, self.max_steps + 1):
            try:
                response = await self.model.complete(messages, tools=self.tools.specs())
            except Exception as exc:  # provider outage must not lose the session
                error = f"模型调用失败：{type(exc).__name__}: {exc}"
                trace.append({"step": step, "error": error})
                return self._finalize(
                    request_id,
                    "抱歉，推理服务暂时不可用。你的信息已安全记录在本会话中，请稍后再试或联系人工。",
                    context,
                    trace,
                    turn_tool_calls,
                    error,
                )

            step_record: Dict[str, Any] = {
                "step": step,
                "thought": response.content,
                "tool_calls": [],
                "observations": [],
            }
            trace.append(step_record)
            messages.append(ChatMessage.assistant(response.content, response.tool_calls))

            if not response.has_tool_calls:
                final_text = response.content
                break

            pause_result: Optional[ToolResult] = None
            pending_calls = response.tool_calls

            for index, call in enumerate(pending_calls):
                arguments = dict(call.arguments or {})
                result = await self.tools.invoke(call.name, arguments, context)
                context.state = self.session.state
                messages.append(ChatMessage.tool_result(call.id, call.name, result.to_message()))

                entry = {
                    "step": step,
                    "tool": call.name,
                    "arguments": redact(arguments),
                    "ok": result.ok,
                    "observation": result.observation,
                    "error": result.error,
                    "risk_level": result.risk_level,
                    "requires_human": result.requires_human,
                }
                turn_tool_calls.append(entry)
                self.session.tool_calls.append(entry)
                step_record["tool_calls"].append(
                    {"tool": call.name, "arguments": redact(arguments), "ok": result.ok}
                )
                step_record["observations"].append(
                    {"tool": call.name, "observation": result.observation}
                )

                # Orchestrator role: the session is the only writer of shared state.
                if result.state_update:
                    self.session.state = apply_state_update(self.session.state, result.state_update)
                    context.state = self.session.state

                if result.requires_human:
                    pause_result = result
                    for skipped in pending_calls[index + 1 :]:
                        messages.append(
                            ChatMessage.tool_result(
                                skipped.id,
                                skipped.name,
                                json.dumps(
                                    {
                                        "tool": skipped.name,
                                        "ok": False,
                                        "skipped": True,
                                        "observation": "流程已暂停等待人工复核，该工具未执行",
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        )
                    break

            if pause_result is not None:
                return self._pause(
                    messages, context, request_id, pause_result, trace, turn_tool_calls
                )
        else:
            trace.append({"step": self.max_steps, "note": "max_steps_reached"})
            final_text = (
                "我已多次尝试后仍无法确定下一步，为避免误判先暂停自动处理。"
                "请补充孕周、症状持续时间或最近检查结果，或要求人工介入。"
            )

        return self._finalize(request_id, final_text, context, trace, turn_tool_calls, error)

    # -- HITL --------------------------------------------------------------
    def _pause(
        self,
        messages: List[ChatMessage],
        context: ToolContext,
        request_id: str,
        pause_result: ToolResult,
        trace: List[Dict[str, Any]],
        turn_tool_calls: List[Dict[str, Any]],
    ) -> TurnResult:
        """Freeze the loop, store a checkpoint and surface the question."""

        payload = pause_result.data or {}
        question = str(payload.get("question") or "请人工复核并给出处理意见。")
        reason = str(payload.get("reason") or f"{pause_result.tool} 触发人工复核")

        checkpoint = {
            "request_id": request_id,
            "reason": reason,
            "question": question,
            "risk_level": pause_result.risk_level,
            "trigger_tool": pause_result.tool,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "original_user_msg": context.original_user_msg,
            "messages": [message.to_dict() for message in messages],
        }
        self.session.pending_human = checkpoint
        self.session.active_risk = {
            "status": "pending_human",
            "risk_level": pause_result.risk_level,
            "trigger_tool": pause_result.tool,
            "reason": reason,
        }

        message = (
            f"已暂停自动流程，等待人工复核。触发工具：{pause_result.tool}；"
            f"当前风险等级：{pause_result.risk_level}。\n待人工确认：{question}"
        )
        return TurnResult(
            request_id=request_id,
            status="needs_human_review",
            message=message,
            risk_level=pause_result.risk_level,
            requires_human=True,
            state=self.session.state,
            audit_events=list(context.audit_events),
            trace=trace,
            tool_calls=turn_tool_calls,
            pending={key: value for key, value in checkpoint.items() if key != "messages"},
        )

    async def resume_turn(
        self,
        human_action: str,
        reviewer: str = "human_operator",
        revised_risk_level: Optional[str] = None,
        note: str = "",
    ) -> TurnResult:
        """Apply the human decision and continue the paused transcript."""

        checkpoint = self.session.pending_human
        if checkpoint is None:
            raise RuntimeError("当前没有等待人工复核的任务。")

        action = (human_action or "").strip().lower()
        if action not in VALID_ACTIONS:
            raise ValueError(f"human_action 必须是 {sorted(VALID_ACTIONS)} 之一，收到：{human_action}")

        old_level = checkpoint.get("risk_level", "UNKNOWN")
        if action == "override":
            level = (revised_risk_level or "").strip().upper()
            if level not in VALID_LEVELS:
                raise ValueError(
                    "override 时必须提供有效的 revised_risk_level（LOW/MODERATE/HIGH/UNKNOWN）"
                )
            final_level = level
        elif action == "reject":
            final_level = "LOW"
        else:
            final_level = old_level

        # Orchestrator role: the human decision must land in shared state.
        existing_reasons = list(self.session.state.risk_status.reasons) if self.session.state.risk_status else []
        review_reasons = [f"人工复核（{reviewer}）：{action}"]
        if note:
            review_reasons.append(note)
        self.session.state.risk_status = RiskStatus(
            level=final_level,
            reasons=list(dict.fromkeys(existing_reasons + review_reasons)),
            requires_human=False,
        )
        self.session.human_review = {
            "reviewer": reviewer,
            "action": action,
            "old_risk_level": old_level,
            "revised_risk_level": final_level,
            "note": note,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.session.active_risk = {
            "status": "reviewed",
            "risk_level": final_level,
            "trigger_tool": checkpoint.get("trigger_tool"),
            "reviewer": reviewer,
            "action": action,
        }

        audit_event = AuditEvent(
            request_id=checkpoint.get("request_id", "unknown"),
            agent="main_agent",
            action="human_risk_review",
            data_accessed=["risk_status"],
            tool_used=None,
            source=["main_agent", "human"],
            decision=(
                f"reviewer={reviewer}; action={action}; old_risk={old_level}; "
                f"new_risk={final_level}; note={note}"
            ),
            risk_level=final_level,
            human_required=False,
        )
        self.session.audit_events.append(audit_event)

        request_id = checkpoint.get("request_id", "unknown")
        messages = [ChatMessage.from_dict(item) for item in checkpoint.get("messages", [])]
        messages.append(
            ChatMessage.user(
                HUMAN_REVIEW_TEMPLATE.format(
                    prefix=HUMAN_NOTE_PREFIX,
                    reviewer=reviewer,
                    action=action,
                    old_level=old_level,
                    final_level=final_level,
                    note=note or "无",
                )
            )
        )

        self.session.pending_human = None
        context = self._new_context(request_id, checkpoint.get("original_user_msg"))

        # Keep the follow-up record consistent with the human decision.
        follow_up = dict(self.session.state.follow_up or {})
        if follow_up:
            follow_up["status"] = "human_reviewed"
            follow_up["reviewer"] = reviewer
            follow_up["human_action"] = action
            self.session.state = apply_state_update(self.session.state, {"follow_up": follow_up})

        result = await self._run_loop(messages, context, request_id)

        if result.status == "completed":
            result.message = (
                f"【人工复核结论】{action}：{old_level} -> {final_level}（reviewer={reviewer}）\n"
                + result.message
            )
        return result

    # -- finishing ---------------------------------------------------------
    def _grounding_check(self, text: str) -> Optional[str]:
        """Neutralise risk tiers in the answer that contradict the rule engine."""

        risk_status = self.session.state.risk_status
        if risk_status is None:
            return None
        mentioned = {match.group(1) for match in _LEVEL_TOKEN.finditer(text)}
        if mentioned and risk_status.level not in mentioned:
            return (
                f"（确定性规则引擎结论为 {risk_status.level}，"
                "正文中出现的其他风险等级不作为结论。）"
            )
        return None

    def _finalize(
        self,
        request_id: str,
        final_text: str,
        context: ToolContext,
        trace: List[Dict[str, Any]],
        turn_tool_calls: List[Dict[str, Any]],
        error: Optional[str],
    ) -> TurnResult:
        text = (final_text or "").strip() or "我已记录你的信息，请补充孕周、症状或最近检查结果。"
        correction = self._grounding_check(text)
        if correction:
            text = text + "\n" + correction
            context.add_events(
                [
                    AuditEvent(
                        request_id=request_id,
                        agent="main_agent",
                        action="risk_level_grounding_correction",
                        data_accessed=["risk_status"],
                        tool_used=None,
                        source=["main_agent"],
                        decision=correction,
                        risk_level=self.session.risk_level,
                        human_required=False,
                    )
                ]
            )
        self.transcript.append(ChatMessage.assistant(text))

        return TurnResult(
            request_id=request_id,
            status="error" if error else "completed",
            message=text,
            risk_level=self.session.risk_level,
            requires_human=False,
            state=self.session.state,
            audit_events=list(self.session.audit_events),
            trace=trace,
            tool_calls=turn_tool_calls,
            error=error,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_trace(result: TurnResult) -> None:
    if not result.tool_calls:
        print("  （本回合未调用工具）")
        return
    print("  ReAct 轨迹：")
    for entry in result.tool_calls:
        flag = "OK " if entry["ok"] else "ERR"
        print(
            f"    - step{entry['step']} [{flag}] {entry['tool']}"
            f" risk={entry['risk_level']} human={entry['requires_human']}"
        )
        print(f"      observation: {entry['observation'][:160]}")


async def run_cli_session(user_id: str = "demo-user") -> None:
    """Interactive CLI demo of the ReAct loop, HITL and audit trail."""

    agent = MainAgent(user_id=user_id)

    print("ReproJourney 主智能体已启动（真实 ReAct 循环 + HITL 人工复核 + 审计留痕）。")
    print(f"模型：{getattr(agent.model, 'name', 'unknown')} | {agent.settings.describe()}")
    print("命令：/state 查看共享状态 | /audit 查看审计链路 | /tools 查看工具 | /quit 退出")
    print("示例：孕周36周，最近血压偏高，有点头晕；或：请人工帮忙复核一下")

    while True:
        try:
            raw = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAgent: 会话结束。")
            break

        if not raw:
            continue
        if raw.lower() in {"quit", "exit", "退出", "/quit"}:
            print("Agent: 会话结束。")
            break
        if raw == "/state":
            print(json.dumps(agent.session.state.model_dump(mode="json"), ensure_ascii=False, indent=2))
            continue
        if raw == "/audit":
            if not agent.session.audit_events:
                print("（暂无审计事件）")
            for event in agent.session.audit_events:
                print(
                    f"- {event.agent}.{event.action} | risk={event.risk_level} "
                    f"| human={event.human_required} | {event.decision[:120]}"
                )
            continue
        if raw == "/tools":
            print(", ".join(agent.tools.names()))
            continue

        result = await agent.run_turn(raw)
        print(f"\nAgent> {result.message}")
        _print_trace(result)

        human_rounds = 0
        while result.status == "needs_human_review":
            human_rounds += 1
            if human_rounds > 3:
                print("Agent: 人工复核轮次过多，已停止本回合自动流程。")
                break
            decision = input("人工复核 [approve / override / reject] > ").strip().lower()
            if decision not in {"approve", "override", "reject"}:
                print("请输入 approve / override / reject")
                continue
            revised = None
            if decision == "override":
                revised = input("新的风险等级 [LOW / MODERATE / HIGH / UNKNOWN] > ").strip()
            note = input("复核备注（可留空）> ").strip()
            result = await agent.resume_turn(
                human_action=decision,
                reviewer="human_operator",
                revised_risk_level=revised,
                note=note,
            )
            print(f"\nAgent> {result.message}")
            _print_trace(result)


__all__ = ["MainAgent", "TurnResult", "run_cli_session", "VALID_ACTIONS", "VALID_LEVELS"]



