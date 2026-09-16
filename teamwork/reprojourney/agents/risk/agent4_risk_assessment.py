"""Agent 4 —— 孕产风险评估智能体（``RiskAssessmentAgent``）。

这个文件只做一件事：**把结构化共享状态换成确定性的风险等级**。

读代码前请先记住 4 条设计约束（标准 §4/§5/§7）：

1. 风险等级**只来自** ``MaternityRiskRuleEngine``（``reprojourney/schemas/risk_rules.py``），
   不由 LLM 决定；每条结论都带稳定 ``rule_id``，因此可复现、可当审计证据引用。
2. 本 Agent **不修改** ``SharedUserState``：它只把要更新的字段放进 ``AgentOutput.state_update``，
   由编排器或会话合并（``Orchestrator._merge_state_update`` / ``apply_state_update``）。
3. 只要 ``risk_level == "HIGH"``，就必须 ``status == "escalate"`` 且 ``requires_human is True``。
4. 每次调用**恰好产出 1 条** ``AuditEvent``（越界拒绝时是 ``reject_non_clinical_request``）。

文件内符号一览（输入 -> 输出）：

* ``NON_CLINICAL_KEYWORDS`` / ``CLINICAL_HINTS`` —— 越界话题词表 / 临床线索词表，供
  ``_is_off_topic`` 使用；
* ``PRIORITY_BY_LEVEL`` / ``SLA_MINUTES_BY_LEVEL`` / ``HIGH_RISK_NEXT_STEPS`` ——
  等级 -> 处置文案 / 响应时限 / 建议动作，只影响 ``summary`` 与 ``state_update``；
* ``RiskAssessmentAgent`` —— 本 Agent 主类（继承 ``BaseAgent``）；
* ``RiskAssessmentAgent._is_off_topic`` —— ``AgentInput`` -> ``bool``；
* ``RiskAssessmentAgent._evidence`` —— 规则引擎结果 + 历史等级 -> ``List[str]``；
* ``RiskAssessmentAgent.run`` —— ``AgentInput`` -> ``AgentOutput``，标准 §7 唯一入口。

最小可运行示例::

    from reprojourney.schemas.base_schemas import AgentInput, Pregnancy, SharedUserState
    from reprojourney.agents.risk.agent4_risk_assessment import RiskAssessmentAgent

    message = "孕周 42 周，阴道出血"
    state = SharedUserState(user_id="u-1", pregnancy=Pregnancy(gestational_week=42))
    agent_input = AgentInput(
        request_id="req-1",
        user_id="u-1",
        task="run_risk_assessment",
        user_message=message,                       # 仅参与"越界话题"判断，不参与分级
        state=state,                                # 唯一分级依据
        context={"audit_events": [], "original_user_msg": message},
        requested_by="orchestrator",
    )

    output = await RiskAssessmentAgent().run(agent_input)
    output.risk_level        # "HIGH" / "MODERATE" / "LOW" / "UNKNOWN"
    output.status            # "escalate" / "success" / "needs_info"
    output.state_update      # {"risk_status": {...}}；HIGH 且原本无 follow_up 时再加 "follow_up"
    output.audit_events[0]   # 本次评估的唯一审计事件

谁会调用它：

* ``reprojourney/agents/orchestrator/orchestrator.py``（非交互式编排，``task="run_risk_assessment"``）；
* ``reprojourney/tools/registry.py`` 的 ``assess_risk`` 工具（ReAct 主智能体经工具层调用）；
* ``batch_cases.py`` / ``manual_test.py`` / ``agent4_agent5_demo.ipynb``（离线批量与示例）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from reprojourney.agents.base import BaseAgent, StateLike
from reprojourney.schemas.base_schemas import AgentInput, AgentOutput, AuditEvent, RiskStatus
from reprojourney.schemas.risk_rules import MaternityRiskRuleEngine

AGENT_NAME = "risk_assessment_agent"

#: Requests clearly outside the maternity-risk scope. The agent must not
#: pretend to assess them -- it refuses and explains why (safety negative case).
#: 中文说明：与孕产风险**无关**的话题词表（天气 / 股票 / 写诗 ...）。命中它、且文本里
#: 没有任何临床线索时，``run()`` 走拒绝分支：``status="needs_info"`` +
#: ``risk_level="UNKNOWN"`` + ``state_update={}``（绝不污染共享状态），
#: 审计动作固定为 ``reject_non_clinical_request``。
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
#: 中文说明：临床线索词表。命中 ``NON_CLINICAL_KEYWORDS`` 之后的第二道关——文本里只要出现
#: "孕 / 产 / 胎 / 出血 / 血压 ..." 任一线索，就说明本次请求确实在谈孕产，于是照常走规则引擎。
#: 这样"报告里提到股票群里的偏方"之类的混写不会被误判成越界。
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
#: 中文说明：等级 -> 处置优先级文案，只用来拼 ``summary`` 给人看（也会被工具层/编排器原样透传）；
#: 等级本身已经由规则引擎定好，这里不改判、不降级。
PRIORITY_BY_LEVEL = {
    "HIGH": "P0-立即处理",
    "MODERATE": "P1-24小时内跟进",
    "LOW": "P2-常规产检随访",
    "UNKNOWN": "P3-补充信息后再评估",
}

#: 等级 -> 响应时限（分钟）。只有 HIGH 会真正写进
#: ``state_update["follow_up"]["sla_minutes"]`` 交给人工排期；其余等级仅作为运维参考。
SLA_MINUTES_BY_LEVEL = {"HIGH": 15, "MODERATE": 1440, "LOW": 10080, "UNKNOWN": 1440}

#: HIGH 场景给人工复核者的下一步建议；``HIGH_RISK_NEXT_STEPS[0]`` 会被写进
#: ``state_update["follow_up"]["recommended_action"]``，是"高风险必须转人工"的落地话术。
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

    ------------------------------------------------------------------
    中文导读：这个类干什么、怎么用
    ------------------------------------------------------------------

    职责
        读共享状态里的结构化字段（孕周 / 症状 / 报告 / 病史 / 随访），调用确定性规则引擎，
        得出四值风险等级 ``LOW | MODERATE | HIGH | UNKNOWN``；需要人介入时把流程升级为 HITL。
        它是全系统**唯一**有权决定临床风险等级的 Agent（``risk_duty = True``）。

    输入：``AgentInput``（标准 §3），逐个字段说明谁被读、谁没被读
        ``request_id``   必填。写进审计事件；**不参与分级**。
        ``user_id``      必填。只用于与注入的 ``state.user_id`` 对齐（不一致直接抛契约错）。
        ``task``         必填，习惯写 ``run_risk_assessment``；本 Agent 不按它分支。
        ``state``        必填，**唯一分级依据**：``pregnancy.gestational_week``、
                         ``symptoms[].symptom``、``reports[].extracted_data``、
                         ``medical_history[]``、``follow_up``。
        ``user_message`` 可选。只参与"是不是非孕产话题"的判断，**不参与分级**；
                         写在文字里的"孕周 42 周"不会改变等级，必须结构化进 ``state``。
        ``context["original_user_msg"]`` 可选。作用同 ``user_message``（二者给一个就够）。
        ``context["audit_events"]``      **本 Agent 不读**（它是 Agent 5 的必需输入）。
        ``requested_by`` 建议 ``"user"`` / ``"orchestrator"``（词表在 ``BaseAgent`` 强制）。

    输入的第二形态
        ``run(agent_input, state=None)`` 的第二个参数是可选的共享状态；
        传了就会 ``model_validate`` 并校验 ``user_id``，然后覆盖 ``agent_input.state``，
        两种写法完全等价（见 ``BaseAgent.prepare_input``）。

    输出：``AgentOutput``（标准 §4~§6），三条互斥路径
        1. **越界拒绝**（``_is_off_topic`` 为真）：
           ``status="needs_info"``、``risk_level="UNKNOWN"``、``requires_human=False``、
           ``next_action=[]``、``state_update={}``、审计动作 ``reject_non_clinical_request``。
        2. **正常评估**：
           ``status``：``HIGH`` -> ``escalate``；``UNKNOWN`` -> ``needs_info``；其余 -> ``success``；
           ``evidence``：首项 ``命中规则：R-XX-01, ...``，其后是原因、``待补充信息：...``、
           以及等级真的变化时的 ``风险等级变化：旧 -> 新``；
           ``next_action``：``escalate`` -> ``["request_human_review", "governance_audit_agent"]``，
           ``needs_info`` -> ``[]``，其余 -> ``["governance_audit_agent"]``；
           ``state_update["risk_status"]`` = ``{"level", "reasons", "requires_human"}``。
        3. **首次 HIGH 且默认无跟进任务**：额外补
           ``state_update["follow_up"]`` = ``{"status": "pending_human",
           "owner": "human_clinician", "sla_minutes": 15, "recommended_action": ...}``。

    常用调用姿势::

        agent = RiskAssessmentAgent()                      # 默认规则引擎（确定性、无 IO）
        out = await agent.run(agent_input)                 # state 放在 input 里
        out = await agent.run(agent_input, shared_state)   # state 作为第二个参数

        RiskAssessmentAgent.contract()                     # 机器可读契约（agent/task/risk_duty/入口）

    注意：本类不做 IO、不调 LLM、不写共享状态、不自己落盘；它交出的 ``state_update``
    只有编排器或会话可以合并进 ``SharedUserState``。
    """

    agent_name = AGENT_NAME
    task = "run_risk_assessment"
    risk_duty = True

    def __init__(self, rule_engine: Optional[MaternityRiskRuleEngine] = None) -> None:
        """构造 Agent（唯一依赖是规则引擎）。

        输入：``rule_engine`` —— 可选的 :class:`MaternityRiskRuleEngine` 实例，便于测试注入
        自定义规则；省略时新建默认引擎（确定性、无状态、无 IO）。
        输出：无（返回 ``None``）。实例只持有这一个引用，**不保存任何用户状态**，
        因此同一个实例可以跨请求、跨用户复用。
        """

        self.rule_engine = rule_engine or MaternityRiskRuleEngine()

    def _is_off_topic(self, agent_input: AgentInput) -> bool:
        """Off-topic = the *current* request is clearly outside maternity care.

        Historical state must not make an unrelated request "clinical": a stored
        gestational week does not turn "告诉我股票行情" into a risk assessment.

        输入：``agent_input`` —— 只读**当前请求文本**两处：``agent_input.user_message`` 与
        ``agent_input.context["original_user_msg"]``（两者拼接后转小写）。历史 ``state`` 刻意不参与，
        否则库里存的孕周会把一句"告诉我股票行情"变成一次风险评估。
        输出：``bool`` —— ``True`` 表示"命中 ``NON_CLINICAL_KEYWORDS`` 且没有任何 ``CLINICAL_HINTS``"，
        ``run()`` 据此走拒绝分支；``False`` 表示照常交给规则引擎。
        判定顺序：文本为空 -> False；没命中越界词 -> False；命中越界词但有临床线索 -> False。
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
        """把规则引擎结果整理成"人和审计都看得懂"的证据列表。

        输入：
            ``result``         —— ``MaternityRiskRuleEngine.evaluate()`` 的返回字典
                                  （键：``risk_level`` / ``reasons`` / ``rule_ids`` /
                                  ``requires_human`` / ``needs_info`` / ``data_gaps``）；
            ``previous_level`` —— 本次评估前的 ``state.risk_status.level``，没有历史则 ``None``。
        输出：``List[str]``，按顺序拼接：
            1. ``命中规则：R-XX-01, R-SY-01``（有 ``rule_ids`` 时才有，是审计引用的关键）；
            2. 规则给出的逐条 ``reasons``（例：``存在高危症状：阴道出血``）；
            3. ``待补充信息：...``（``needs_info`` 非空时）；
            4. ``风险等级变化：LOW -> HIGH``（等级确实发生变化时）。
        该列表会原样进入 ``AgentOutput.evidence``，因此措辞要保持"可被规则号引用 + 不含隐私原文"。
        """

        rule_ids = result.get("rule_ids") or []
        evidence: List[str] = [f"命中规则：{', '.join(rule_ids)}"] if rule_ids else []
        evidence.extend(result["reasons"])
        if result.get("needs_info"):
            evidence.append("待补充信息：" + "；".join(result["needs_info"]))
        if previous_level and previous_level != result["risk_level"]:
            evidence.append(f"风险等级变化：{previous_level} -> {result['risk_level']}")
        return evidence

    async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput:
        """标准 §7 唯一入口：评估一次孕产风险，返回标准输出。

        输入：
            ``agent_input`` —— 必填，:class:`AgentInput`（标准 §3，字段含义见类 docstring）。
            ``state``       —— 可选，``SharedUserState`` 或其 ``dict``。传了会先 ``model_validate``
                               并校验 ``user_id`` 与 ``agent_input.user_id`` 一致，再覆盖
                               ``agent_input.state``；两种调用姿势语义完全相同。
        输出：``AgentOutput`` —— 已通过 ``BaseAgent.enforce_output`` 的标准 §4~§6 校验
        （``agent`` 必为 ``risk_assessment_agent``；``audit_events`` 恰好 1 条且 ``request_id`` 对齐；
        HIGH 必然 ``escalate`` + ``requires_human``；``state_update`` 的键必须是共享状态一级字段）。
        异常：入参不是 ``AgentInput``、``requested_by`` 不在词表、注入 state 与 ``user_id`` 不一致，
        或输出违反契约时抛 ``AgentContractError``——设计上"宁可炸"也不静默降级。
        副作用：无。既不写共享状态也不落盘；请把 ``state_update`` 交给编排器/会话去合并。
        """

        await asyncio.sleep(0)  # 让出一次事件循环；本 Agent 无 IO，只是为了统一 async 形态
        # Section 7: accept the state either inside the input or as a second
        # argument; either way there is exactly one validated shared state.
        # 中文说明：把"state 在 input 里"和"state 作为第二参数"两种写法归一，
        # 顺带做契约校验（requested_by 词表 + user_id 一致）。
        agent_input = self.prepare_input(agent_input, state)
        request_id = agent_input.request_id
        state = agent_input.state
        # 记录历史等级，只用于 evidence 里的"风险等级变化"提示，不影响本次判定。
        previous_level = state.risk_status.level if state.risk_status else None

        # 路径 1：越界拒绝。只看当前请求文本，不看历史 state。
        if self._is_off_topic(agent_input):
            decision = "请求与孕产风险评估无关，已拒绝给出风险结论"
            # 关键安全点：state_update 保持为空 —— 越界请求绝不能污染共享状态；
            # 同时留一条可审计的拒绝记录，说明"系统为什么没有给结论"。
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

        # 路径 2 / 3：正常评估。风险等级**只能**来自确定性规则引擎（标准：LLM 不得定级）。
        # evaluate() 返回 {"risk_level", "reasons", "rule_ids", "requires_human",
        # "needs_info", "data_gaps"}，其中 requires_human 已由引擎保证"HIGH => True"。
        result = self.rule_engine.evaluate(state)
        risk_level = result["risk_level"]
        reasons = result["reasons"]
        requires_human = result["requires_human"]

        # 标准 §4/§5 的 status 映射：HIGH -> escalate；UNKNOWN -> needs_info；其余 -> success。
        status = "success"
        if risk_level == "HIGH" and requires_human:
            status = "escalate"
        elif risk_level == "UNKNOWN":
            status = "needs_info"

        # next_action 是给编排器/会话看的"下一步该找谁"：
        # escalate -> 先人工复核再走治理审计；needs_info -> 停下来补信息；其余 -> 直接治理审计。
        if status == "escalate":
            next_action = ["request_human_review", "governance_audit_agent"]
        elif status == "needs_info":
            next_action = []
        else:
            next_action = ["governance_audit_agent"]

        # 本次评估的唯一审计事件：只记规则号与结论，不记用户原文/隐私内容。
        # data_accessed 会受 Agent 5 的最小权限表（AGENT_DATA_SCOPE）校验，多写一个字段就报错。
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

        # state_update 是"增量补丁"，不是新状态对象：编排器/会话会用 _merge_state_update 合并。
        # 用 model_dump(mode="json") 是为了让补丁能直接序列化（落盘/JSON 传输都不报错）。
        state_update: Dict[str, Any] = {
            "risk_status": RiskStatus(
                level=risk_level,
                reasons=reasons,
                requires_human=requires_human,
            ).model_dump(mode="json")
        }
        # 首次进入 HIGH 且当前没有跟进任务时，顺手建一条"等人处理"的随访任务（含 15 分钟 SLA）；
        # 若已有 follow_up 则不动它，避免覆盖人工或其它 Agent 写过的排期。
        if status == "escalate" and not state.follow_up:
            state_update["follow_up"] = {
                "status": "pending_human",
                "owner": "human_clinician",
                "sla_minutes": SLA_MINUTES_BY_LEVEL["HIGH"],
                "recommended_action": HIGH_RISK_NEXT_STEPS[0],
            }

        # 人类可读摘要：等级 + 处置优先级 + 具体原因（原因里同样只有规则话术，没有原始隐私文本）。
        summary = f"孕产风险评估完成：{risk_level}（{PRIORITY_BY_LEVEL[risk_level]}）。" + "；".join(reasons)

        # 统一出口：enforce_output() 会按标准 §4~§6 校验 agent 名、审计事件 request_id、
        # HIGH/UNKNOWN 的 status 映射、state_update 的键范围等；不合规直接抛 AgentContractError。
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
