"""Agent 5 —— 治理与审计智能体（``GovernanceAuditAgent``）。

职责：给整个流程当**合规门禁**。它不判临床风险（那是 Agent 4 + 确定性规则引擎的事），
它只回答一个问题：**这次流程留下的审计痕迹、用户授权与数据访问，合不合规？**

它检查的五类事情（每条发现都带稳定的 ``G-*`` 规则号，便于审计引用）：

1. 审计链路完整性 —— 有风险判定却没有任何留痕；
2. 审计事件字段完整性 —— 缺 ``request_id`` / ``agent`` / ``action`` / ``decision`` / ``timestamp`` 等；
3. 隐私 —— 审计内容里出现 password / api_key / secret / token 之类敏感串；
4. 最小权限 —— ``data_accessed`` 超出了 ``AGENT_DATA_SCOPE`` 给该 Agent 划定的数据域；
5. Consent 与 HITL —— 高风险却没拿到"外部升级授权"；``requires_human`` 却没有人工复核记录；
   人工复核结论没有写回共享状态等。

判定映射（``compliance_status``）：

* 只要有一条 ``critical`` —— ``NON_COMPLIANT`` + ``status="needs_info"`` + ``requires_human=True``；
* 只有 ``warning``        —— ``COMPLIANT_WITH_WARNINGS`` + ``status="success"``（仍算通过）；
* 没有任何发现            —— ``COMPLIANT`` + ``status="success"``。

特别说明：``AgentOutput.risk_level`` **恒为 ``"UNKNOWN"``**。标准 §4 规定"没有风险判断职责的
Agent 不得报告临床等级"，所以合规结论一律走 ``compliance_status`` + ``rule_ids``，
这样前端不可能把"审计告警"误读成"临床降级"。

文件内符号一览（输入 -> 输出）：

* ``GovernanceFinding``   —— 一条发现：``rule_id`` / ``severity`` / ``message`` / ``subject``，
  附带 ``is_critical``、``format()``、``to_dict()`` 三个派生输出；
* ``_as_event``           —— ``AuditEvent`` 或 ``dict`` -> ``AuditEvent``（解析失败返回 ``None``）；
* ``GovernanceAuditAgent`` —— 主类。7 个 ``_check_*`` 各只负责一类检查（便于单测与阅读），
  ``evaluate()`` 把它们汇总成结论，``run()`` / ``run_with_payload()`` 是标准入口。

三个入口怎么选::

    from reprojourney.agents.governance.agent5_governance_audit import GovernanceAuditAgent

    agent = GovernanceAuditAgent()
    payload = agent.evaluate(agent_input)                        # 同步纯函数 -> 完整 dict
    output = await agent.run(agent_input)                        # 只要标准 AgentOutput
    output, payload = await agent.run_with_payload(agent_input)  # 两者都要（工具层用的就是它）

``context`` 里必须放证据，否则等于"审计链路为空"：

* ``context["audit_events"]``  必需。``AuditEvent`` 对象或 dict 的列表。**顺序很重要**：
  Agent 4 的事件要先累积到这里再调用本 Agent；否则只要 ``state.risk_status`` 有值，
  就会因"审计链路为空"被判 critical。
* ``context["human_review"]``  可选。形如 ``{"action": "approve", "revised_risk_level": "HIGH"}``；
  人工已复核时传入，其结论若没写回 ``state.risk_status.level`` 同样会被判 critical。

谁在调用它：``reprojourney/agents/orchestrator/orchestrator.py``（``task="run_governance_audit"``）
与工具层 ``audit_governance``（``reprojourney/tools/registry.py``）；
也可以像 ``manual_test.py`` 那样单独调用做离线复核。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from reprojourney.agents.base import BaseAgent, StateLike
from reprojourney.schemas.base_schemas import AgentInput, AgentOutput, AuditEvent

AGENT_NAME = "governance_audit_agent"
#: 策略版本号：会原样出现在 payload 与审计事件的 decision 里，便于日后追溯"这次判罚依据哪版策略"。
POLICY_VERSION = "governance-audit/v1.0"

#: 发现只有两级：critical 让整体判 NON_COMPLIANT 并触发人工；warning 只降级为
#: COMPLIANT_WITH_WARNINGS（仍算通过）。两级之外不要自行发明。
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

#: 三种合规结论的字面量——对外契约的一部分，测试与上游都按这三个值判断，不要改写。
COMPLIANT = "COMPLIANT"
COMPLIANT_WITH_WARNINGS = "COMPLIANT_WITH_WARNINGS"
NON_COMPLIANT = "NON_COMPLIANT"

#: 风险审计动作白名单：审计链路里出现其中之一，才说明"风险判定确实留了痕"。
RISK_AUDIT_ACTIONS = {"run_maternity_risk_evaluation", "assess_risk"}
#: 人工复核动作白名单：HITL 检查据此判断"人确实参与过"。
HUMAN_REVIEW_ACTIONS = {"human_risk_review", "request_human_review"}

#: 一条审计事件必须非空具备的字段；缺任意一个 -> 字段不完整（critical）。
REQUIRED_AUDIT_FIELDS = ("request_id", "agent", "action", "decision", "timestamp")

#: 敏感串黑名单（小写匹配）。被扫描的字段是审计事件的
#: ``decision`` / ``action`` / ``source`` / ``data_accessed``。
SENSITIVE_TOKENS = ("password", "api_key", "apikey", "secret", "token", "身份证", "银行卡")

#: Least-privilege data domains. ``data_accessed`` outside this map is a finding.
#: 中文说明：最小权限表 —— 键是审计事件里的 ``agent`` 名，值是该 Agent 允许访问的数据域。
#: 校验逻辑见 ``_check_data_scope``：``data_accessed`` 里出现不在自己域内的字段即 critical。
#: 表里查不到的 ``agent`` 会跳过该项检查（未知 Agent 不误报）。
#: 维护提示：给某个 Agent 新增 ``data_accessed`` 字段时，必须同步这张表，否则审计会拦下来。
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
    """One policy violation or observation.

    一条"发现"，纯数据、无副作用。

    输入（构造参数）：
        ``rule_id``  —— 稳定规则号，如 ``G-AUD-01``；是审计引用与测试断言的主键。
        ``severity`` —— ``SEVERITY_CRITICAL`` 或 ``SEVERITY_WARNING``，决定整体合规结论。
        ``message``  —— 中文说明，解释"哪里不对"。
        ``subject``  —— 出问题的动作名或字段名（例：``audit_trail`` / 某个 ``event.action``），
                        便于在长审计链路里定位。

    输出（派生属性/方法）：
        ``is_critical`` -> ``bool``：是否 critical 级；
        ``format()``    -> ``str``：形如 ``[critical] G-AUD-01｜存在风险判定但审计链路为空：缺少留痕``，
                           直接进 ``AgentOutput.evidence`` 与 payload 的 ``issues``；
        ``to_dict()``   -> ``dict``：同样四个字段，便于 JSON 化交给外部系统。
    """

    rule_id: str
    severity: str
    message: str
    subject: str = ""

    @property
    def is_critical(self) -> bool:
        """输出 ``bool``：``severity`` 是否等于 ``SEVERITY_CRITICAL``。

        供 ``evaluate()`` 把发现分流成 critical / warning 两堆，决定整体合规结论。
        """

        return self.severity == SEVERITY_CRITICAL

    def format(self) -> str:
        """输出单行人类可读串：``[severity] rule_id｜message``（``rule_id`` 为空时省略规则号）。

        该字符串会进入 ``AgentOutput.evidence`` 与 payload 的 ``issues``；
        注意它**只含规则号与说明文本**，不含被标记数据域的原始值。
        """

        prefix = f"{self.rule_id}｜" if self.rule_id else ""
        return f"[{self.severity}] {prefix}{self.message}"

    def to_dict(self) -> Dict[str, Any]:
        """输出 ``dict``：``rule_id`` / ``severity`` / ``message`` / ``subject`` 四个字段。

        用于把发现结构化交给外部系统（JSON 落库、审计报表），与 ``format()`` 表达同一事实。
        """

        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
        }


def _as_event(event: Any) -> Optional[AuditEvent]:
    """Accept ``AuditEvent`` objects or plain dicts (e.g. from JSON payloads).

    输入：``event`` —— ``AuditEvent`` 对象、可解析的 ``dict``（例如从日志库/消息队列读回来的
    JSON），或任何其它类型。
    输出：``AuditEvent``；若入参是 ``dict`` 则经 ``AuditEvent.model_validate`` 严格校验
    （``extra="forbid"``），**解析失败或类型不对返回 ``None``**。
    调用方 ``evaluate()`` 会把 ``None`` 计入"无法解析的审计事件"，从而报出 critical 级发现——
    也就是说脏数据不会被静默忽略，而是变成一条可追责的合规问题。
    """

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

    ------------------------------------------------------------------
    中文导读：这个类干什么、怎么用
    ------------------------------------------------------------------

    职责
        读 ``state``（``risk_status`` / ``consent`` / ``consultation_history`` / ``follow_up``）
        与 ``context``（``audit_events`` / ``human_review``），依次跑 7 组 ``_check_*`` 检查，
        汇总成一个合规结论。它**不判临床等级**（``risk_duty = False``，``risk_level`` 恒为
        ``"UNKNOWN"``），因此它的输出可以安全地和 Agent 4 的结论并列展示。

    输入：``AgentInput``（标准 §3），逐个字段说明
        ``request_id``                 必填。用作审计事件的归属比对：链路里混入别的 request
                                       -> ``G-AUD-04``（warning）。
        ``state.risk_status``          强相关。"是否已做出风险判定"的判据：有它却没有审计事件，
                                       就是"判定没有留痕"（critical）。
        ``state.consent``              强相关。高风险 + 未授权外部升级 -> ``G-CON-01``（critical）；
                                       ``None`` 等价于没授权。
        ``state.consultation_history`` 有咨询记录却拿不到明确的存储授权 -> ``G-CON-03``（warning）。
        ``state.follow_up``            只影响 critical 时是否补写 ``follow_up``（已有则不动）。
        ``context["audit_events"]``    必需证据。``AuditEvent`` 对象或 dict 列表；
                                       **Agent 4 的事件必须先累积进来**。
        ``context["human_review"]``    可选。人工复核结论；与 ``state.risk_status.level``
                                       不一致时会报 critical（说明结论没落库）。
        ``user_message``               **不参与判定**——本 Agent 只看结构化证据，不看自然语言。

    输出
        ``evaluate()`` 返回 ``Dict[str, Any]``，键为：``compliance_status`` / ``policy_version`` /
        ``events_checked`` / ``critical_count`` / ``warning_count`` / ``issues`` / ``findings`` /
        ``rule_ids`` / ``status`` / ``risk_level`` / ``requires_human`` / ``summary`` /
        ``evidence`` / ``next_action`` / ``state_update`` / ``audit_event``。
        ``run()`` 与 ``run_with_payload()`` 则返回标准 ``AgentOutput``（可选地附带上述字典）。
        其中 ``risk_level`` 恒为 ``"UNKNOWN"``；``state_update`` 只在 critical 且
        ``state.follow_up`` 为空时出现，内容是一条 ``pending_governance_review`` 的治理跟进任务。

    调用姿势（三种入口见模块 docstring）::

        agent = GovernanceAuditAgent()
        payload = agent.evaluate(gov_input)          # 同步、纯函数，最适合写断言
        output = await agent.run(gov_input)          # 走标准 §4~§6 契约校验

        # 典型顺序：Agent 4 先跑 -> 事件累积 -> 写回 state -> 再审计
        gov_input = AgentInput(
            request_id=request_id,
            user_id=state.user_id,
            task="run_governance_audit",
            state=state,
            context={
                "audit_events": risk_output.audit_events,
                "human_review": {"action": "approve", "revised_risk_level": "HIGH"},
            },
            requested_by="orchestrator",
        )

    注意
        1. critical 时本 Agent 会**自己举手**（``requires_human=True`` +
           ``next_action=["request_human_review"]``），编排器据此把流程判为需要人工介入
           （见 ``Orchestrator._finalize_after_governance``）；不做这一步，合规门禁就只是装饰。
        2. 它写出的审计事件**只记规则号与计数**，不复制被检查事件的原始值——避免"审计记录
           泄露了它刚刚标记为敏感的数据"。
        3. 实例无状态，可跨请求复用；如果要用不同策略版本判定，用 ``policy_version`` 参数显式指定。
    """

    agent_name = AGENT_NAME
    task = "run_governance_audit"
    risk_duty = False

    def __init__(self, policy_version: str = POLICY_VERSION) -> None:
        """构造 Agent。

        输入：``policy_version`` —— 策略版本号；会回填进 payload 的 ``policy_version`` 字段，
        并写进自身审计事件的 ``decision``（形如 ``policy=governance-audit/v1.0``），
        方便日后回答"这条判罚依据的是哪一版治理策略"。
        输出：无（返回 ``None``）。除了这个字符串，实例不持有任何状态，可安全复用。
        """

        self.policy_version = policy_version

    # -- individual policy checks ------------------------------------------
    def _check_audit_trail(self, state, events: Sequence[AuditEvent]) -> List[GovernanceFinding]:
        """检查"风险判定有没有留下审计痕迹"。

        输入：``state``（只看 ``state.risk_status`` 是否存在）、``events``（已解析的审计事件列表）。
        输出：``List[GovernanceFinding]``，可能两条：
            * 有 ``risk_status`` 但事件列表为空 -> G-AUD-01（critical）；
            * 有 ``risk_status`` 但没有任何 ``RISK_AUDIT_ACTIONS`` 事件 -> G-GOV-01（critical）。
        """

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
        """逐条检查审计事件字段是否齐全。

        输入：``events`` —— 已解析的审计事件。
        输出：``List[GovernanceFinding]``，每条事件最多两条：
            * 缺 ``REQUIRED_AUDIT_FIELDS`` 中任一非空字段 -> G-AUD-02（critical）；
            * ``data_accessed`` 为空 -> G-AUD-03（warning，属"能查到但不够全"）。
        """

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
        """在审计事件文本里扫敏感串——防止"审计本身成为泄露渠道"。

        输入：``events``；被扫描的字段是 ``decision`` / ``action`` / ``source`` / ``data_accessed``，
        拼成一段小写文本后用 ``SENSITIVE_TOKENS`` 匹配。
        输出：命中即产生一条 G-PRV-01（critical）。
        注意：发现里只写"哪条事件有问题"，不回显命中的原文，避免二次泄露。
        """

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
        """最小权限校验：审计事件有没有读到"不该读"的数据域。

        输入：``events``；对每条事件取 ``AGENT_DATA_SCOPE[event.agent]`` 作为该 Agent 的允许集合。
        输出：``data_accessed`` 中出现超范围字段 -> G-PRV-02（critical）；
        事件的 ``agent`` 不在 ``AGENT_DATA_SCOPE`` 里则跳过该项（未知 Agent 不误报）。
        """

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
        """检查审计链路里有没有混进"别的请求"的事件。

        输入：``request_id``（本次请求 ID）、``events``。
        输出：存在 ``event.request_id != request_id`` 的事件 -> G-AUD-04（warning，只报数量，
        不逐条展开），提示链路可能被复用或串流；critical 与否由后续汇总决定。
        """

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
        """授权（Consent）校验。

        输入：``state.consent`` / ``state.risk_status`` / ``state.consultation_history``。
        第二个参数 ``_events`` 保留是为了与其它检查签名一致，本检查并不使用事件
        （授权是状态事实，不取决于链路内容）。
        输出：``List[GovernanceFinding]``，最多三条：
            * ``risk_status.level == "HIGH"`` 且 ``consent.allow_external_escalation is not True``
              （``consent`` 为 ``None`` 也算没授权）-> G-CON-01（critical）；
            * ``consent.allow_record_storage is False`` -> G-CON-02（warning，用户明确不允许存储）；
            * 已有 ``consultation_history`` 却拿不到明确的存储授权 -> G-CON-03（warning）。
        """

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
        """HITL（Human-in-the-loop）记录校验：人真的被拉进来了吗？结论落库了吗？

        输入：
            ``events``  —— 从中挑出 ``human_required=True`` 的判定事件与
                           ``HUMAN_REVIEW_ACTIONS`` 的人工复核事件；
            ``state``   —— ``state.risk_status.requires_human`` 是"系统认为自己需要人"的标记；
            ``context`` —— ``context["human_review"]`` 是人工复核的结论（若有）。
        输出：``List[GovernanceFinding]``，最多三条（第 1、2 条互斥，只会出现一条）：
            * 有 ``human_required`` 判定却没有人工复核事件 -> G-HITL-01（critical）；
            * 否则若 ``state.risk_status.requires_human`` 为真却没有人工复核事件 -> G-HITL-02（critical）；
            * ``human_review["revised_risk_level"]`` 与 ``state.risk_status.level`` 不一致
              -> G-HITL-03（critical，人工意见没写回共享状态，等于白复核）；
            * context 有人工复核信息但链路里没有对应审计事件 -> G-HITL-04（warning）。
        """

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
        """Pure, synchronous compliance evaluation -- no I/O, fully testable.

        输入：``agent_input`` —— 标准 §3 输入；本方法读
        ``request_id``、``state``、``context["audit_events"]``（``AuditEvent`` 或 dict）、
        ``context["human_review"]``。**不读** ``user_message``。
        输出：``Dict[str, Any]``——键与含义见类 docstring；上游最常用的四个是
        ``compliance_status``（三值结论）、``issues``（人读串列表）、``findings``（结构化列表）、
        ``rule_ids``（规则号列表）。每条检查只追加发现、从不删改 ``state``。
        特性：**同步纯函数**——同一输入必然同一输出、无 IO、无随机，因此可以直接写断言；
        ``run()`` 只是把它包上一层标准输出契约。
        边界：``state_update`` 只可能在"有 critical 且 ``state.follow_up`` 为空"时非空，
        内容是补一条治理跟进任务（``pending_governance_review`` / 1440 分钟 SLA）。
        """

        request_id = agent_input.request_id
        state = agent_input.state
        context = dict(agent_input.context or {})
        # 证据来源只有 context["audit_events"]：它可以是 AuditEvent 对象，也可以是外部系统
        # （日志库/消息队列）回传的 dict；解析不了的不丢弃，计入 unparsable 让流程显式报错。
        raw_events = context.get("audit_events") or []
        events = [event for event in (_as_event(item) for item in raw_events) if event is not None]
        unparsable = len(raw_events) - len(events)

        # 7 组检查依次执行，各管一类问题；顺序不影响结论，只影响 findings 的排列。
        findings: List[GovernanceFinding] = []
        findings.extend(self._check_audit_trail(state, events))
        findings.extend(self._check_event_completeness(events))
        findings.extend(self._check_sensitive_data(events))
        findings.extend(self._check_data_scope(events))
        findings.extend(self._check_consent(state, events))
        findings.extend(self._check_hitl(state, events, context))
        findings.extend(self._check_request_consistency(request_id, events))
        # 脏数据也是一条发现：不能因为"读不懂"就当作没问题（否则伪造的事件就能绕过审计）。
        if unparsable:
            findings.append(
                GovernanceFinding(
                    "G-AUD-05",
                    SEVERITY_CRITICAL,
                    f"存在 {unparsable} 条无法解析的审计事件",
                    subject="audit_events",
                )
            )

        # ---- 汇总判定：critical 决定一切 --------------------------------------
        critical = [finding for finding in findings if finding.is_critical]
        warnings = [finding for finding in findings if not finding.is_critical]

        if critical:
            # 有一条 critical 就不合规：改成 needs_info + requires_human，
            # 让编排器/工具层必须把流程交给人工（见类 docstring 的"注意 1"）。
            compliance_status = NON_COMPLIANT
            status = "needs_info"
            requires_human = True
        elif warnings:
            # 只有 warning：算通过，但结论带上告警，人工可按需查看。
            compliance_status = COMPLIANT_WITH_WARNINGS
            status = "success"
            requires_human = False
        else:
            # 无发现：链路完整、授权齐备、权限与 HITL 都合规。
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
        # 中文说明：只写"事由 + 计数 + 规则号"，绝不把被标记的原始值抄进来——
        # 否则审计记录本身就是一次泄露（例如把命中的敏感串回显出来）。
        decision_text = (
            f"compliance={compliance_status}; policy={self.policy_version}; "
            f"events_checked={len(events)}; critical={len(critical)}; warnings={len(warnings)}"
        )
        if findings:
            decision_text += "; rules=" + ",".join(
                f"{finding.rule_id}:{finding.severity}" for finding in findings
            )

        # 本 Agent 自己的审计事件：action 固定为 governance_audit_validation，
        # risk_level 恒 UNKNOWN，human_required 跟随 verdict（critical 时为 True）。
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

        # state_update 同样只是"增量补丁"：只在 critical 且当前没有跟进任务时补一条治理跟进，
        # 已有 follow_up 就不覆盖（人工或 Agent 4 可能已经排过期）。
        state_update: Dict[str, Any] = {}
        if critical and not state.follow_up:
            state_update["follow_up"] = {
                "status": "pending_governance_review",
                "owner": "governance_officer",
                "sla_minutes": 1440,
                "recommended_action": "人工复核合规问题并完成授权确认",
            }

        # issues：给人看的字符串列表；assurance：没有任何发现时的正面结论（证明"检查确实跑过"）。
        issues = [finding.format() for finding in findings]
        assurance = f"审计链路完整：{len(events)} 个审计事件通过校验"

        # 返回字典同时承载"给人看的"（summary / evidence）与"给机器用的"
        # （compliance_status / findings / rule_ids / state_update / audit_event）两部分；
        # run() / run_with_payload() 只是从中挑字段组装标准 AgentOutput。
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
        """Run the audit and return both the standard output and the raw payload.

        输入：同 ``run()``——``agent_input`` 必填，``state`` 可选（会校验 ``user_id`` 一致并覆盖
        ``agent_input.state``）。
        输出：二元组 ``(AgentOutput, dict)``：
            * ``AgentOutput`` —— 标准 §7 输出：``risk_level`` 恒 ``UNKNOWN``、
              ``audit_events`` 恰好 1 条、``requires_human`` 与 ``next_action`` 跟随 critical；
            * ``dict``        —— ``evaluate()`` 的完整结果，含 ``compliance_status`` /
              ``critical_count`` / ``issues`` / ``rule_ids`` 等。
        谁在用：工具层 ``audit_governance``（``reprojourney/tools/registry.py``）——
        它用 ``ok = payload["critical_count"] == 0`` 决定这次工具调用是否成功。
        """

        await asyncio.sleep(0)  # 让出一次事件循环；本 Agent 无 IO，仅为统一 async 形态
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
        """标准 §7 入口：只返回标准输出（详细 payload 由 ``run_with_payload`` 提供）。

        输入：
            ``agent_input`` —— 必填，:class:`AgentInput`（标准 §3；本 Agent 读的字段见类 docstring）。
            ``state``       —— 可选共享状态（``SharedUserState`` 或 dict）；传了会校验
                               ``user_id`` 与输入一致后覆盖 ``agent_input.state``。
        输出：``AgentOutput`` —— 已通过 ``BaseAgent.enforce_output`` 的标准 §4~§6 校验
        （``agent`` 必为 ``governance_audit_agent``；``risk_level`` 必须为 ``UNKNOWN``；
        审计事件 ``request_id`` 与输入对齐；``state_update`` 键必须是一级状态字段）。
        异常：契约不满足时抛 ``AgentContractError``（例如误把 ``risk_level`` 写成 ``LOW``）。
        副作用：无——不写共享状态、不落盘；``state_update`` 交给编排器/会话合并。
        """

        output, _ = await self.run_with_payload(agent_input, state)
        return output



