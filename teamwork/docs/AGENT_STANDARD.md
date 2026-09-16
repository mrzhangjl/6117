# ReproJourney Agent Development Standard v1.0 —— 落地对照与偏差登记

本文档把《ReproJourney Agent Development Standard v1.0》的每一条"必须"映射到**当前仓库里可执行的位置**，
并如实登记尚未对齐的地方。它有两层作用：

1. **给人看**：新人读这一页就知道"标准说了什么、代码里在哪、怎么验证"；
2. **给机器查**：`reprojourney/schemas/agent_contract.py` 把 §2–§6 的字段与语义写成常量与校验函数，
   `tests/test_standard_contract.py` 逐条断言，`python docs/check_docs.py docs/AGENT_STANDARD.md`
   再核验本文档引用的文件与卫生问题。

> 一句话总结：**标准不是文档里的承诺，而是会被测试与文档守卫证伪的代码约束。**

## 1. 总原则（§1）

| 标准要求 | 落地位置 | 验证方式 |
| --- | --- | --- |
| 所有 Agent 是同一个 backend 的模块，不允许各自做独立 App | 全部代码在 `reprojourney/` 包内；无任何 Web 框架依赖 | `tests/test_standard_contract.py` 扫描 `reprojourney/**.py` 的 import 与秘钥/绝对路径 |
| 接收统一格式输入 | `AgentInput`（`reprojourney/schemas/base_schemas.py`） | `input_schema_problems()` 断言 7 个字段集合与 `extra="forbid"` |
| 读取同一个 Shared User State | `SharedUserState`；入口第二参数 `state` 会被校验并注入 | `state_schema_problems()` + `BaseAgent.prepare_input` |
| 返回统一格式输出 | `AgentOutput` | `output_schema_problems()` + `BaseAgent.enforce_output` |
| 不直接修改其他 Agent 的内部数据 | Agent 只返回 `state_update`；**唯一**写入者是编排器与会话 | `Orchestrator._merge_state_update`、`session.py::apply_state_update` |
| 不直接操作前端 | 无 UI 代码；CLI 与 Notebook 只是调用方 | 同第一行的扫描 |
| 不把 API key / 路径 / 模型名写死 | `reprojourney/config.py` 读环境变量与 `.env`；`DEFAULT_MODELS` 为集中默认值 | `check_docs.py` 的 hygiene 检查 + 契约测试 |
| 可以独立测试 | 每个 Agent 都是单参数/双参数入口的纯异步函数，无全局状态 | `tests/test_agent_contracts.py`、`tests/test_standard_contract.py` |
| 必须留下 audit event | 每次 `run` 至少一个 `AuditEvent`，由运行时强制 | `check_output_contract`（§6）+ `test_07` |

## 2. Shared User State（§2）

一级字段**只有这 12 个**，不得改名、不得新增：

```text
user_id · profile · pregnancy · medical_history · reports · symptoms
mood · appointments · consultation_history · risk_status · follow_up · consent
```

实现：`reprojourney/schemas/base_schemas.py::SharedUserState`（`extra="forbid"`，写错字段名直接报错）。
字段清单在代码里另有一份**同源声明**：`agent_contract.STATE_FIELDS`，两者必须集合相等。

最低要求子字段（`agent_contract.STATE_SUBFIELDS`，全部可空 —— 标准：*不知道的字段用 null，不要编造*）：

| 一级字段 | 子字段 | 模型 |
| --- | --- | --- |
| `profile` | `age`、`basic_profile` | `Profile` |
| `pregnancy` | `gestational_week`、`estimated_due_date` | `Pregnancy` |
| `reports` | `report_id`、`report_type`、`report_date`、`extracted_data`、`source_file` | `Report` |
| `symptoms` | `symptom`、`severity`、`timestamp` | `Symptom` |
| `consultation_history` | `query`、`response`、`evidence`、`timestamp` | `ConsultationRecord` |
| `risk_status` | `level`、`reasons`、`requires_human` | `RiskStatus` |
| `consent` | `allow_record_storage`、`allow_external_escalation`、`allow_family_notification` | `Consent` |

## 3. Standard Agent Input（§3）

```json
{
  "request_id": "string",
  "user_id": "string",
  "task": "string",
  "user_message": "string or null",
  "state": {},
  "context": {},
  "requested_by": "user | orchestrator | agent_name"
}
```

| 标准要求 | 落地位置 |
| --- | --- |
| 七个固定字段，不给 Agent 发散乱参数 | `AgentInput`，`extra="forbid"`（多传一个参数即 `ValidationError`） |
| `requested_by` 词表 | `agent_contract.KNOWN_AGENTS` + `requested_by_problem()`；`BaseAgent.prepare_input` 在入口处拒绝越界取值 |

> 说明：`AgentInput.requested_by` 的类型是 `Literal["user","orchestrator"] | str`，这样旧调用方不会被破坏；
> **真正的词表约束在入口处执行**（`prepare_input`），越界即 `AgentContractError`。

## 4. Standard Agent Output（§4）

```json
{
  "agent": "agent_name",
  "status": "success | needs_info | escalate | error",
  "summary": "short human-readable result",
  "evidence": [],
  "next_action": [],
  "risk_level": "LOW | MODERATE | HIGH | UNKNOWN",
  "requires_human": false,
  "state_update": {},
  "audit_events": [],
  "error": null
}
```

四条特别规则在 `agent_contract.check_output_contract()` 里逐条落地，并由 `BaseAgent.enforce_output()`
在每次 `run` 返回前调用（不合规即抛 `AgentContractError`，不会静默放过）：

| 标准特别规则 | 代码判定 | 当前状态 |
| --- | --- | --- |
| 没有风险判断职责的 Agent：`risk_level = "UNKNOWN"` | `risk_duty=False` ⇒ 必须 `UNKNOWN` | **v1.0 重构修复**：Agent 5 原先返回 `LOW`/`MODERATE`，现恒为 `UNKNOWN`，合规结论留在 `compliance_status` |
| 没有 state 更新：`state_update = {}` | `state_update` 的键必须 ⊆ §2 的 12 个一级字段 | Agent 4 只写 `risk_status`（HIGH 时再加 `follow_up`）；Agent 5 只在 critical 且无 `follow_up` 时写 `follow_up` |
| 不确定时禁止自己猜：`status = "needs_info"` | `risk_duty` 且 `risk_level=UNKNOWN` ⇒ `status ∈ {needs_info, error}` | Agent 4 的 `R-INFO-01`/拒绝路径均满足 |
| 高风险：`status = "escalate"` + `requires_human = true` | `risk_level=HIGH` ⇒ `requires_human` 必须为真；有风险职责时还必须 `escalate` | Agent 4 满足；Agent 5 无风险职责，用 `needs_info` + `requires_human=true` 表达治理拦截（见偏差登记 D） |

另外两条隐含一致性也被机器守住：

* `state_update["risk_status"]["level"]` 必须等于 `output.risk_level`（防止"状态与结论打架"）；
* 每条 `AuditEvent.risk_level` 必须等于 `output.risk_level`（前端与审计看到同一个等级）。

## 5. Risk Level 只有四种（§5）

`LOW` · `MODERATE` · `HIGH` · `UNKNOWN` —— `AgentOutput.risk_level`、`AuditEvent.risk_level`、
`RiskStatus.level` 三处都是同一个 `Literal` 闭集（`agent_contract.risk_level_schema_problems()` 断言三者一致）。

* 禁止词表：`agent_contract.FORBIDDEN_RISK_WORDS`（`green` / `amber` / `yellow` / `urgent` / `severe` / `maybe-risk`）；
  测试会扫描 `reprojourney/**/*.py`，一旦出现就失败。
* 唯一被允许的**另一条轴**是 Agent 5 的"合规严重度"：`critical` / `warning`
  （`agent5_governance_audit.py::SEVERITY_CRITICAL`）。它描述的是 policy finding 的严重程度，
  不是临床风险等级，且从不写入任何 `risk_level` 字段——所以不违反 §5。

## 6. Audit Event（§6）

```json
{
  "timestamp": "ISO-8601",
  "request_id": "string",
  "agent": "agent_name",
  "action": "string",
  "data_accessed": [],
  "tool_used": "string or null",
  "source": [],
  "decision": "string",
  "risk_level": "LOW | MODERATE | HIGH | UNKNOWN",
  "human_required": false
}
```

* 字段与类型：`AuditEvent`（`audit_schema_problems()` 断言字段集合恰好是 `AUDIT_EVENT_FIELDS`）。
* "不记录完整 prompt / password / API key"：`audit_event_problems()` 检查 `decision` 为空或含
  `password` / `api_key` / `apikey` / `secret` 即判违规；Agent 5 的 `G-PRV-01` 从审计侧独立复核所有事件，
  且它自己的审计事件只写 `rule_id` 与计数，**不复制原始值**。
* `request_id` 一致性：`check_output_contract(..., request_id=...)` 要求本次运行产生的每条事件都带同一个 request_id。

## 7. 代码统一标准（§7）

### 7.1 统一入口

标准固定的是**概念签名** `run(agent_input, state) -> agent_output`，并允许最终由编排器锁定同步/异步形式。
本仓库锁定的形式（写在 `reprojourney/agents/base.py` 的模块 docstring 里）：

```python
async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput
```

两种调用**是同一个调用**：

```python
output = await RiskAssessmentAgent().run(agent_input)          # state 在 input 里
output = await RiskAssessmentAgent().run(agent_input, state)   # state 显式注入（标准写法）
```

`state` 接受 `SharedUserState` 或它的 `dict`（会先 `model_validate`），且必须与 `agent_input.user_id`
一致——否则抛 `AgentContractError`。这样"第二套 Shared State"在结构上就不可能存在。
`BaseAgent.contract()` 输出每个 Agent 的机器可读契约（agent / task / risk_duty / entry_point / 标准版本）。

### 7.2 环境与卫生

| 标准要求 | 现状 |
| --- | --- |
| 统一 `requirements.txt` | 存在；必需依赖只有 `pydantic`，真实 LLM/provider 依赖可选 |
| 所有 secret 放 `.env`，`.env` 不入库 | `.env.example` 提供模板；`.gitignore` 忽略 `.env` |
| 禁止绝对路径 | `check_docs.py` 的 hygiene 检查扫描源码；契约测试再扫一遍 `reprojourney/**/*.py` |
| 不允许 Agent 自建 Web server | 无 Web 框架依赖；契约测试禁止 `fastapi/flask/django/uvicorn/...` 等 import |
| 不调用真实医院 / 不发送真实通知 | 工具白名单里没有任何外联工具（`http_request` / `send_sms` / `web_email` 之类一律不存在） |

### 7.3 文件结构（已按标准 §7 对齐）

| 标准槽位 | 本仓库实际目录 | 状态 |
| --- | --- | --- |
| `agents/orchestrator/` | `reprojourney/agents/orchestrator/` | ✅ `Orchestrator.start_flow` |
| `agents/risk/` | `reprojourney/agents/risk/` | ✅ Agent 4（`agent4_risk_assessment.py`） |
| `agents/governance/` | `reprojourney/agents/governance/` | ✅ Agent 5（`agent5_governance_audit.py`） |
| `agents/health_record/` | — | ⛔ 未实现（`agent_contract.PENDING_SLOTS` 已声明） |
| `agents/consultant/` | — | ⛔ 未实现（`agent_contract.PENDING_SLOTS` 已声明） |
| `schemas/` | `reprojourney/schemas/` | ✅ 含 `agent_contract.py`、`risk_rules.py` |
| `tools/` | `reprojourney/tools/`（`registry.py`） | ✅ 与 `agents/` 平级（`agent_contract.TOOL_SLOT`） |
| `knowledge/` | — | ⛔ 未建（偏差 G） |
| `frontend/` | — | ⛔ 未建（偏差 G） |
| `tests/` | `tests/` | ✅ |
| （标准未列出的额外槽位） | `reprojourney/agents/main_agent/` + `reprojourney/agents/base.py` | ⚠️ 见偏差 E |

目录名与标准**逐字一致**由测试钉住：`tests/test_standard_contract.py::test_08` 断言
每个非 pending 槽位的路径末段等于槽位名，且 `reprojourney/tools/` 存在。

## 8. Agent 与 Tool 必须分开（§8）

| 标准要求 | 落地位置 |
| --- | --- |
| Agent 2 = Health Record Agent → 调 OCR Tool → 验证 → 更新 record（**不是** OCR 本身） | 未实现；`agent_contract.PENDING_SLOTS` 已登记，待建时按此形状做 |
| Agent 3 = Consultant Agent → 理解问题 → 调 Retrieval Tool → 用 evidence 回答（**不是** RAG 本身） | 同上 |
| Agent 4：不要让 LLM 单独决定危险程度 | `RiskAssessmentAgent` 的等级**只**来自 `MaternityRiskRuleEngine`（确定性、带 `rule_id`），LLM 只负责解释与措辞；`AuditEvent.tool_used="maternity_risk_rule_engine"` |
| Agent → 决定 workflow / escalate | Agent 4 返回 `next_action`（`request_human_review` / `governance_audit_agent`），由编排器与会话决定流程 |
| 不要让 Agent 4 直接宣称诊断 | 回答层固定声明"不构成医学诊断"，高风险一律转人工（`README.md` 第 7 节） |
| 工具与 Agent 分离 | 工具层在 `reprojourney/tools/registry.py`（与 `agents/` 平级）：只做 dispatch + 审计；子 Agent 以工具形式被调用（`assess_risk` / `audit_governance`）。**依赖方向单向**：Agent 可以 import 工具层，工具层不得在模块级 import Agent（否则成环），因此 `assess_risk` / `audit_governance` 在函数内部惰性 import Agent 4/5 |

## 9. 偏差登记（Deviation register）

诚实登记比"看起来全对"更有用。下表是当前**已知未完全对齐**的地方，以及为什么、怎么办。

| # | 偏差 | 影响 | 处理建议 |
| --- | --- | --- | --- |
| A | **已修复**：目录名原为 `risk_agent/`、`governance_audit/`，标准写的是 `risk/`、`governance/` | 与标准树不一致 | v1.0 方案 2 已重命名为 `reprojourney/agents/{risk,governance}/`，并同步更新 `docs/check_docs.py` 的符号锚点、`README.md` / `docs/FLOWCHART.md` / `docs/ARCHITECTURE.md` 与全部 import |
| B | **已修复**：Agent 5 原先用 `LOW` / `MODERATE` 表示合规结论 | 前端可能把审计告警误读成临床降级 | v1.0 重构中改为恒 `UNKNOWN`，结论走 `compliance_status`；同步修正 2 条旧断言 |
| C | `AgentInput.requested_by` 类型仍是 `Literal["user","orchestrator"] \| str` | 类型层面不封闭 | 词表在 `BaseAgent.prepare_input` 处以 `requested_by_problem()` 强制；若要类型层也封闭，可改成 `Literal["user","orchestrator","risk_assessment_agent","governance_audit_agent","main_agent"]`（需同步测试） |
| D | Agent 5 遇 critical 时返回 `status="needs_info"` + `requires_human=true`，而非 `escalate` | 与 §4 的"高风险 ⇒ escalate"字面不同 | 合理：§4 该条针对**有风险判断职责**的 Agent，而 Agent 5 的风险等级恒为 `UNKNOWN`；治理拦截用 `requires_human` + `next_action=["request_human_review"]` 表达，编排器已据此传导（`Orchestrator._finalize_after_governance`） |
| E | `main_agent/` 是标准树未命名的槽位 | 标准只列 5 个槽位 | 它是标准 §1 的"ReAct 会话层"实现，入口是 `run_turn()` / `resume_turn()`（不是无状态 `run`）；标准入口 `run(agent_input, state)` 由 Agent 4/5 实现，编排层用 `Orchestrator.start_flow()` |
| F | Agent 2（Health Record）/ Agent 3（Consultant）尚未实现 | 标准 §8 的例子无法验证 | 已在 `agent_contract.PENDING_SLOTS` 与 §7.3 表中显式声明为"未实现"，避免被误当成已完成 |
| G | **部分修复**：顶层 `tools/` 已建（`reprojourney/tools/registry.py`）；`knowledge/`、`frontend/` 仍未建 | 标准 §7 的目录建议 | `knowledge/`（Agent 3 的检索语料）与 Agent 2/3 一起建；`frontend/` 本仓库刻意不做（交付形态是 CLI + notebook，标准 §1「不直接操作前端」） |

### 方案 2 已执行：旧 → 新 import 对照

| 旧（v1.0 前） | 新（当前） |
| --- | --- |
| `reprojourney.agents.risk_agent.agent4_risk_assessment` | `reprojourney.agents.risk.agent4_risk_assessment` |
| `reprojourney.agents.governance_audit.agent5_governance_audit` | `reprojourney.agents.governance.agent5_governance_audit` |
| `reprojourney.agents.main_agent.tools` | `reprojourney.tools.registry` |

（工具模块本身也从 `main_agent/` 目录移到了 `reprojourney/tools/registry.py`。）

`AGENT_NAME` / `task` / `action` 等**取值没有变**（`"governance_audit_agent"`、`"run_governance_audit"`、
`"governance_audit_validation"` 仍是客户端与审计看到的名字），改的只是模块所在目录。

## 10. 如何验证

```powershell
python -m pytest -q                                   # 契约 / ReAct / 安全 / 开发标准
python docs/check_docs.py docs/AGENT_STANDARD.md      # 本文档 + README + FLOWCHART 的文档守卫
```

第一行里的"开发标准"这组断言在 `tests/test_standard_contract.py`：

| 用例 | 覆盖条款 |
| --- | --- |
| `StandardSchemaTests::test_01` | §2 一级字段与子字段、`extra="forbid"` |
| `StandardSchemaTests::test_02` | §3 七个字段、散乱参数被拒、`requested_by` 词表 |
| `StandardSchemaTests::test_03` | §5 四值闭集、禁止等级词在源码中不存在 |
| `StandardEntryPointTests::test_04` | §7 统一入口与 `risk_duty` 声明 |
| `StandardEntryPointTests::test_05` | §7 两种调用等价、跨用户 state 被拒 |
| `StandardEntryPointTests::test_06` | §4 四条特别规则（含反向对照） |
| `StandardEntryPointTests::test_07` | §6 审计事件字段、request_id 一致性、不记录敏感信息 |
| `StandardEntryPointTests::test_08` | §7 禁止事项（无 Web server、无外联工具、无绝对路径/密钥）+ §2 槽位声明 |

## 11. 变更记录

* **v1.0 重构（本次）**
  * 新增 `reprojourney/schemas/agent_contract.py`：把 §2–§6 变成常量与校验函数（唯一真源）。
  * 新增 `reprojourney/agents/base.py`：`BaseAgent` 提供 §7 统一入口、state 归一化与输出契约强制。
  * Agent 4 / Agent 5 改为继承 `BaseAgent`，入口变为 `run(agent_input, state=None)`
    （旧调用 `run(agent_input)` 完全兼容）。
  * **修复 §4 违规**：Agent 5 的 `risk_level` 由 `LOW`/`MODERATE` 改为恒 `UNKNOWN`；
    两处旧断言（`tests/test_agents.py::test_22`、`tests/test_agent_contracts.py::test_13`）同步更新。
  * 新增 `tests/test_standard_contract.py`（8 条标准契约用例）。
* **v1.0 方案 2（目录完全对齐）**
  * `reprojourney/agents/risk_agent/` → `reprojourney/agents/risk/`
  * `reprojourney/agents/governance_audit/` → `reprojourney/agents/governance/`
  * 工具模块从 `reprojourney/agents/main_agent/` 移到 `reprojourney/tools/registry.py`
    （工具层与 `agents/` 平级；`reprojourney/tools/__init__.py` 用 PEP 562 惰性再导出，避免与
    `reprojourney.agents` 形成 import 环）
  * `agent_contract.AGENT_SLOTS` 改为与标准逐字一致，并新增 `agent_contract.TOOL_SLOT`
  * `tests/test_standard_contract.py::test_08` 增加"目录名逐字一致 + `reprojourney/tools/` 存在"断言
  * 同步：`docs/check_docs.py` 的符号锚点与工具白名单扫描路径、`README.md` 目录树、
    `docs/FLOWCHART.md` 映射表、`docs/ARCHITECTURE.md` 模块清单、notebook 的 import 与说明文字
