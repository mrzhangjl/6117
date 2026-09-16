# 架构与安全设计说明（ReproJourney Agent Development Standard v1.0）

> 图表版总览见 [`FLOWCHART.md`](FLOWCHART.md)：组件数据流、单回合 ReAct 循环、HITL 状态机、
> 端到端时序、Orchestrator 批量路径、开发验证流水线，以及「节点 ↔ 代码」映射表。

本文说明 Agent 4 / Agent 5 / ReAct 主智能体的设计决策、契约与安全不变式，作为代码评审与
验收的依据。实现位置见各节标注的文件路径。

## 1. 分层视图

```text
                        ┌──────────────────────────────────────────┐
   用户消息 ───────────▶│  MainAgent（ReAct 循环 + 会话/HITL 存档） │
                        └───────┬───────────────────┬──────────────┘
                                │ tool_calls        │ state_update（唯一合并者）
                                ▼                   ▼
                        ┌───────────────┐   ┌──────────────────────┐
                        │ ToolRegistry  │   │ SharedUserState      │
                        │ （工具层）     │   │ （会话级共享状态）     │
                        └───┬───────┬───┘   └──────────────────────┘
                            │       │
              Agent 4 ◀─────┘       └─────▶ Agent 5
        （规则引擎分级 + HITL 触发）   （合规/隐私/HITL 记录校验）
                            │
                     BaseChatModel（offline / openai / anthropic / scripted）
```

- `reprojourney/agents/main_agent/`：真实 ReAct 循环、会话与 HITL。
- `reprojourney/agents/base.py`：`BaseAgent` —— 开发标准 §7 的统一入口
  `run(agent_input, state=None)`，并在返回前用 `enforce_output()` 校验 §4–§6。
- `reprojourney/tools/registry.py`：工具层（`ToolRegistry` + 6 个内置工具），与 `agents/` 平级，
  对应标准 §7/§8「Agent 与 Tool 必须分开」。
- `reprojourney/agents/risk/agent4_risk_assessment.py`：Agent 4。
- `reprojourney/agents/governance/agent5_governance_audit.py`：Agent 5。
- `reprojourney/schemas/risk_rules.py`：确定性规则引擎（唯一风险决策者）。
- `reprojourney/schemas/agent_contract.py`：开发标准 v1.0 的常量与校验函数（§2–§7 的唯一真源）。
- `reprojourney/llm/`：模型抽象层与四个实现。

## 2. 契约（schemas）

字段清单即《ReproJourney Agent Development Standard v1.0》的 §3 / §4 / §6（条款 ↔ 代码对照与偏差
登记见 `docs/AGENT_STANDARD.md`，机器断言见 `tests/test_standard_contract.py`）：

- `AgentInput`：`request_id / user_id / task / user_message / state / context / requested_by`。
- `AgentOutput`：`agent / status(success|needs_info|escalate|error) / summary / evidence /
  next_action / risk_level / requires_human / state_update / audit_events / error`。
  §4 的四条特别规则由 `agent_contract.check_output_contract()` 强制：无风险职责 ⇒ `UNKNOWN`；
  无更新 ⇒ `state_update={}`；不确定 ⇒ `needs_info`；HIGH ⇒ `escalate` + `requires_human`。
- `AuditEvent`：`timestamp / request_id / agent / action / data_accessed / tool_used / source /
  decision / risk_level / human_required`。
- `SharedUserState`：user_id / profile / pregnancy / medical_history / reports / symptoms / mood /
  appointments / consultation_history / risk_status / follow_up / consent（`extra="forbid"`）。

**状态写入规则**：Agent 只返回 `state_update` 补丁；由会话（`MainAgent`）或 `Orchestrator`
调用 `apply_state_update()` 合并（列表追加、字典字段级合并、未知键忽略）。工具自身绝不直接修改
共享状态（`tests/test_react_agent.py::test_13`）。

## 3. ReAct 循环

```text
messages = [system(prompt) + system(tool hint) + system(session context) + transcript[-6:] + user]
for step in 1..max_steps:                     # REPROJOURNEY_MAX_REACT_STEPS，默认 8
    response = model.complete(messages, tools=registry.specs())
    messages.append(assistant(response.content, response.tool_calls))
    if not response.tool_calls:               # 无工具调用 ⇒ 收敛为最终回答
        break
    for call in response.tool_calls:          # 顺序执行，禁止并行写同一状态
        result = registry.invoke(call.name, call.arguments, ctx)
        messages.append(tool(tool_call_id, name, observation=result.payload()))
        if result.state_update: state = apply_state_update(state, result.state_update)
        if result.requires_human:             # 安全门：立即暂停
           回答剩余 tool_call（标记 skipped）以保证 transcript 合法
           return PAUSE(checkpoint)
else:
    达到步数上限 ⇒ 安全停止 + 追问话术（不猜测结论）
```

设计要点：

1. **观察结果必须真实回填**：模型下一步只能基于工具返回的 payload 推理（grounding）。
   单测 `test_01` 断言模型在第三轮确实收到了前两轮的 observation。
2. **未知工具不会中断流程**：`ToolRegistry.invoke` 返回 `ok=false, error="unknown_tool"`，
   模型可自行纠正（`test_02`）。
3. **工具异常被隔离**：任何异常都被转换为 `ok=false` 的观察结果，循环继续（生产环境不会
   因一个工具崩溃而丢失整个会话）。
4. **模型故障降级**：`BaseChatModel.complete` 抛错时返回 `status="error"` 的安全话术，
   且**不写入任何风险等级**（`tests/test_safety.py::test_10`）。

## 4. HITL 状态机

| 当前状态 | 触发 | 下一状态 | 副作用 |
| --- | --- | --- | --- |
| `idle` | `run_turn(msg)` | `running` | 追加用户消息到 transcript |
| `running` | 工具返回 `requires_human=true` | `waiting_human` | 保存 checkpoint（含完整 messages），`session.pending_human` 置位 |
| `waiting_human` | 再次 `run_turn` | `waiting_human` | 回执"仍在等待人工复核"，不执行新任务 |
| `waiting_human` | `resume_turn(approve)` | `running` | 写入 `human_risk_review` 审计事件，等级保持 |
| `waiting_human` | `resume_turn(override, level)` | `running` | 等级改为人工指定值，`requires_human=false` |
| `waiting_human` | `resume_turn(reject)` | `running` | 等级改为 `LOW` 并记录"误报"备注 |
| `running` | 模型给出最终回答 | `completed` | 接地校验 + 追加安全声明 |
| `running` | 模型故障 / 超步数 | `error` / `completed` | 安全话术，不产生虚构结论 |

- 非法动作（未知 action、override 缺等级）抛 `ValueError`；无待复核任务时抛 `RuntimeError`
  （`tests/test_react_agent.py::test_06`）。
- 恢复时**沿用原 transcript**（含被跳过工具的占位结果），保证模型能看到完整上下文。
- 人工结论会同时写入 `SharedUserState.risk_status` 与 `session.human_review`，Agent 5 的
  `G-HITL-03` 会验证两者一致——如果哪一层漏写，治理审计会直接报 critical。

## 5. 审计模型

- 每个工具调用至少产生一个 `AuditEvent`；评估基座断言"每次执行的审计完整性 = 100%"。
- 事件类型：`read_user_state`、`record_user_facts`、`run_maternity_risk_evaluation`、
  `governance_audit_validation`、`request_human_review`、`human_risk_review`、
  `persist_state_snapshot` / `persist_session_denied`、`reject_non_clinical_request`、
  `risk_level_grounding_correction`。
- **最小权限数据域**：`AGENT_DATA_SCOPE` 为每个 `agent` 声明可访问的数据域，
  Agent 5 的 `G-PRV-02` 会把越界访问判为 critical（含真实案例：工具曾因访问
  `user_message` 未登记而被拦下，随后补齐权限表）。
- **不回写原始值**：Agent 5 自身的审计记录只包含 `rule_id` 与计数，防止审计日志二次泄露
  （`test_26`）。
- **落盘脱敏**：`redact()` 递归替换 `api_key/password/secret/token/authorization` 等键；
  落盘前检查 `consent.allow_record_storage`，未授权则拒绝并写 `persist_session_denied`。

## 6. 安全不变式与测试映射

| 不变式 | 实现位置 | 验证用例 |
| --- | --- | --- |
| 风险等级只来自规则引擎 | `assess_risk` 工具 + `_grounding_check` | `test_react_agent::test_09`、`evaluation EV-15/16` |
| HIGH ⇒ `requires_human` | `MaternityRiskRuleEngine.evaluate` 末尾兜底 | `test_agent_contracts::test_12` |
| 提示注入不能降级 | 未识别症状不参与分级（`R-SY-04`） | `test_safety::test_01/03` |
| 模型不能跳过人工 | HITL 由工具结果触发，与模型意愿无关 | `test_safety::test_02` |
| 越权外部升级被阻断 | Agent 5 `G-CON-01` ⇒ 再次暂停 | `test_safety::test_05` |
| 未授权不留存 | `persist_session` 授权门禁 + 脱敏 | `test_react_agent::test_08` |
| 审计可追溯且完整 | 每工具必审计 + Agent 5 字段校验 | `test_react_agent::test_01`、评估 `audit_completeness` |
| 无外联（无网络/短信/邮件工具） | `ToolRegistry` 白名单 | `test_react_agent::test_12` |
| 用户可见回答含安全声明 | `OfflinePolicyChatModel` / 系统提示词 | `test_safety::test_09` |
| 数据质量缺口不伪装成 LOW | 规则引擎数据质量兜底 | `evaluation EV-14`、`test_agent_contracts::test_09` |

## 7. 模型适配层与降级策略

`reprojourney/config.py` 从环境变量（可选 `.env`）读取配置，优先级：
`REPROJOURNEY_LLM_PROVIDER` → 依据可用 key 自动探测 → `offline`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `REPROJOURNEY_LLM_PROVIDER` | 自动探测 | `openai` / `anthropic` / `offline` |
| `REPROJOURNEY_LLM_MODEL` | `gpt-4o-mini` / `claude-3-5-sonnet-latest` | 模型名 |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 空 | 仅从环境读取，代码中无密钥 |
| `REPROJOURNEY_MAX_REACT_STEPS` | 8 | ReAct 步数上限 |
| `REPROJOURNEY_TOOL_TIMEOUT` | 20 | 工具超时预算（秒） |
| `REPROJOURNEY_RUN_DIR` | `runs` | 会话快照目录（相对路径） |
| `REPROJOURNEY_PERSIST_SESSIONS` | true | 是否允许落盘（仍需用户 consent） |
| `REPROJOURNEY_GOVERNANCE_GATE` | true | 治理门禁开关 |

降级原则：SDK 缺失、key 无效、base_url 不可达等任何初始化异常都会被捕获并回退到
确定性离线策略模型，同时向 stderr 打印原因——**宁可行为可预测，也不要静默失败**。

## 8. 评估方法

`evaluation/run_evaluation.py` 使用 18 个带标签用例（含 4 个安全/负向）与 3 个端到端场景：

| 指标 | 定义 | 阈值 |
| --- | --- | --- |
| 风险分级准确率 | 预测等级 == 标签等级 | ≥ 90% |
| 高危漏报率 | 标签 HIGH 但预测非 HIGH 的比例 | **= 0**（安全关键） |
| 高危误报率 | 标签非 HIGH 但预测 HIGH 的比例 | 参考值 |
| HITL 精确率/召回率 | 以"是否需要人工复核"为标签 | 参考值 |
| 审计完整性 | 每次执行都产生字段完整的审计事件 | 100% |
| 安全用例通过率 | 注入/越界等不变式全部成立 | 100% |
| 端到端场景 | HIGH 暂停→批准→合规、中危直通、注入抵抗 | 全部通过 |

结果写入 `evaluation/report.md`（人读）与 `evaluation/results.json`（机读），
任一阈值不达标时进程返回非 0，可直接用于 CI。

## 9. 已知限制与后续工作

1. **离线策略模型的意图识别基于规则**：它支持否定词（"不要人工"）、抽取式事实识别与
   工具路由，但不具备真实 LLM 的语言泛化能力；正式部署请配置真实 provider。
2. **会话持久化仅覆盖快照**：待人工复核的完整 transcript 只保留在内存中（避免把原始对话
   额外写入磁盘）；如需跨进程恢复，应增加加密存储与更细的授权流程。
3. **规则库为示例规模**：`risk_rules.py` 的词典与阈值需由临床团队评审后扩充，
   并补充对应的回归用例与 `rule_id` 版本记录。
4. **多用户/多会话**：当前 `MainAgent` 实例对应一个用户；规模化需引入会话注册表与并发控制。
5. **可观测性**：审计事件已结构化，可直接接入集中式日志/追踪系统（如 OpenTelemetry）。

