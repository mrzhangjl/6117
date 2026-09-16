# ReproJourney 整体流程图

所有流程均**逐节点对应仓库中的真实实现**，末尾附「节点 ↔ 代码映射表」；
逐条核验过程与结论见「图 ↔ 代码一致性核验（逐节点审计记录）」一节。
开发标准（v1.0）的条款 ↔ 代码对照与偏差登记见 `docs/AGENT_STANDARD.md`。
Mermaid 图在 GitHub、VS Code（Markdown Preview Mermaid Support 扩展）或
<https://mermaid.live> 可直接渲染；文末另附纯 ASCII 版本，方便直接粘贴进报告/PPT。

---

## 图 0 · 一页总览（ASCII）

```text
                ┌──────────────────────── 用户 ────────────────────────┐
                │  "孕周42周，阴道出血"   /  approve  /  override  /  reject │
                └───────────────┬──────────────────────┬──────────────┘
                                │                      │ 人工复核结论
                     main.py →  ▼                      ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  MainAgent（真实 ReAct 循环 + 会话/HITL 存档）                  │
        │  ┌─ Thought ───────────▶ 模型输出 content + tool_calls         │
        │  ├─ Action  ───────────▶ ToolRegistry 按名分发（每次调用留审计）│
        │  ├─ Observation ──────▶ 真实结果回填 transcript（grounding）   │
        │  ├─ 状态合并 ─────────▶ 会话是 SharedUserState 唯一写者         │
        │  ├─ HITL 门禁 ────────▶ requires_human ⇒ 暂停 + checkpoint      │
        │  └─ Final ────────────▶ 接地校验（等级须等于规则引擎结论）      │
        └───────┬──────────────┬──────────────┬──────────────┬─────────┘
                │              │              │              │
        read/record      assess_risk    audit_governance   request_human_review
                │              │              │          persist_session
                ▼              ▼              ▼              ▼
        SharedUserState  ┌───────────┐  ┌──────────────┐  人工 / runs/ 脱敏快照
        （仅供读取）      │ Agent 4   │  │  Agent 5     │
                        │ + 规则引擎│  │ 治理与审计    │
                        │ 唯一分级  │  │ 审计完整性    │
                        └─────┬─────┘  │ 权限/Consent  │
                              │        │ HITL 记录一致性│
                     HIGH ⇒ requires_human └──────┬──────┘
                              └───────────────────┴──▶ AuditEvent 链路（可追溯）
```

> 两条**旁路入口**（不在上图主链路内，见 图 1）：`batch_cases.py` / `manual_test.py` 直接
> 调用 Agent 4（`manual_test.py` 另调 Agent 5），不经 ReAct 循环；`Orchestrator` 由
> `tests/test_agents.py` 驱动，只编排 Agent 4 → 人工 → Agent 5。
> “安全声明”不是 `agent.py` 追加的，而是提示词（`prompts.py` L37）与模型输出
> （`offline_chat.py::_compose_final_answer`）保证；`agent.py::_finalize` 仅在空文本时填兜底话术。

---

## 图 1 · 组件与数据流总览

```mermaid
flowchart TB
    U["使用者<br/>孕产妇 / 家属 / 医护人员 / 开发者"] --> CLI["main.py → run_cli_session()<br/>交互式 CLI；/state /audit /tools"]
    U --> BATCH["batch_cases.py / manual_test.py<br/>直接调用 Agent 4（manual_test 另调 Agent 5）<br/>旁路 ReAct 循环与编排器"]
    U --> ORCH["Orchestrator<br/>非交互式编排：只调 Agent 4/5，无模型<br/>调用方：tests/test_agents.py"]

    CLI --> MA["MainAgent<br/>ReAct 循环 + 会话 + HITL"]
    MA --> MODEL["BaseChatModel<br/>offline / openai / anthropic / scripted"]
    MA --> REG["ToolRegistry<br/>工具白名单（无网络/短信/邮件）"]
    ORCH --> A4
    ORCH --> A5

    REG --> T_READ["read_user_state"]
    REG --> T_REC["record_user_facts"]
    REG --> T_RISK["assess_risk"]
    REG --> T_GOV["audit_governance"]
    REG --> T_HITL["request_human_review"]
    REG --> T_SAVE["persist_session"]

    T_RISK --> A4["Agent 4 · RiskAssessmentAgent"]
    T_GOV --> A5["Agent 5 · GovernanceAuditAgent"]
    T_READ --> STATE["SharedUserState<br/>唯一写者：会话 / 编排器"]
    T_REC --> STATE
    T_SAVE --> FS["runs/ 脱敏快照<br/>需 allow_record_storage"]
    T_HITL --> HUMAN["人工复核<br/>approve / override / reject"]

    A4 --> RULES["MaternityRiskRuleEngine<br/>risk_rules.py · 唯一风险分级来源"]
    RULES --> GATE{"risk_level == HIGH ?"}
    GATE -->|"是（安全兜底）"| ESC["requires_human = true<br/>status = escalate"]
    GATE -->|"否"| OK["requires_human = false"]

    ESC --> HUMAN
    HUMAN --> MA
    A5 --> VERDICT["Agent 5 输出<br/>COMPLIANT / COMPLIANT_WITH_WARNINGS / NON_COMPLIANT"]
    VERDICT -->|"critical 发现"| NEEDS["status=needs_info<br/>requires_human=true"]
    VERDICT -->|"无 critical"| PASS["status=success"]
    NEEDS -.->|"编排器原样传导"| NI["_finalize_after_governance<br/>needs_info + requires_human=true"]
    PASS -.-> DONE2["completed + 完整 audit_events"]

    MA --> AUD["AuditEvent 链路<br/>每次工具调用 ≥ 1 事件（本地工具自建；<br/>assess_risk / audit_governance 用 ctx.add_events<br/>汇入子智能体的事件）"]
    A4 --> AUD
    A5 --> AUD
    A5 -.->|"校验：完整性 / 权限 / consent / HITL"| AUD

    MA --> OUT["TurnResult<br/>message · risk_level · status<br/>audit_events · trace · pending"]
    OUT --> CLI
    OUT --> STATE

    classDef safe fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef risk fill:#ffebee,stroke:#c62828,color:#b71c1c;
    class RULES,GATE,ESC risk;
    class A5,PASS,VERDICT safe;
```

---

## 图 2 · 单回合 ReAct 循环（含 HITL 暂停分支）

对应 `reprojourney/agents/main_agent/agent.py::MainAgent._run_loop`。

```mermaid
flowchart TD
    START(["run_turn(user_message)"]) --> PEND{"session.pending_human<br/>是否已有待复核任务？"}
    PEND -->|"是"| BUSY["返回 needs_human_review<br/>回执「请先完成复核」<br/>不执行新任务"]
    PEND -->|"否"| INIT["turn_index += 1<br/>组装 messages：<br/>system(提示词) + system(工具表) + system(会话上下文)<br/>+ transcript[-6:] + user(本次消息)"]

    INIT --> STEP["step = 1"]
    STEP --> CALL["await model.complete(messages, tools)"]
    CALL --> ERR{"模型调用异常？"}
    ERR -->|"是"| SAFE["安全话术：推理服务不可用<br/>不编造任何风险等级<br/>status = error"]
    ERR -->|"否"| HAS{"response.tool_calls<br/>是否为空？"}

    HAS -->|"空"| FINAL["final_text = 模型回答<br/>（安全声明由提示词 + 模型输出保证）"]
    HAS -->|"非空"| EXEC["按顺序执行 tool_calls<br/>（串行：禁止并行写同一状态）"]

    EXEC --> APPEND["agent.py L184-191<br/>trace.append(step_record)<br/>messages += assistant(content, tool_calls)"]
    APPEND --> INVOKE["ToolRegistry.invoke(name, args, ctx)<br/>① 未知工具 → ok=false（模型可自愈）<br/>② 工具抛错 → 转成 ok=false 观察结果<br/>③ context.state 每次调用后同步为会话状态"]
    INVOKE --> REC["agent.py L206-223<br/>turn_tool_calls / session.tool_calls / step_record 记账<br/>（redact(arguments) · risk_level · requires_human）"]
    REC --> MERGE["agent.py L225-228<br/>若 result.state_update：<br/>仅会话合并进 SharedUserState"]
    MERGE --> HUMANQ{"result.requires_human ?"}

    HUMANQ -->|"是"| SKIP["为剩余未执行 tool_call<br/>补 skipped 占位结果<br/>（保证 transcript 合法）"]
    SKIP --> PAUSE["_pause()：<br/>存 checkpoint（含完整 messages）<br/>pending_human / active_risk=pending_human<br/>status = needs_human_review"]

    HUMANQ -->|"否"| OBS["observation 以 tool 消息回填<br/>→ 下一步推理基于真实结果"]
    OBS --> MAX{"step < max_steps ?"}
    MAX -->|"是"| STEP2["step += 1"]
    STEP2 --> CALL
    MAX -->|"否（超限）"| CAP["安全话术：<br/>「请补充孕周/症状/检查结果<br/>或要求人工介入」<br/>不猜测结论"]
    CAP --> FINAL

    FINAL --> GROUND{"接地校验：<br/>回答中的等级与规则引擎结论冲突？"}
    GROUND -->|"冲突"| FIX["追加强制更正<br/>+ 审计 risk_level_grounding_correction"]
    GROUND -->|"一致"| KEEP["保留原回答"]
    FIX --> DONE
    KEEP --> DONE
    SAFE --> DONE
    DONE(["TurnResult（completed / error）<br/>message · risk_level · audit_events · trace"])

    PAUSE --> HITL(["等待人工复核<br/>→ 见 图 3 / 图 4"])

    classDef pause fill:#fff8e1,stroke:#f9a825,color:#7f6000;
    classDef guard fill:#ffebee,stroke:#c62828,color:#b71c1c;
    class PAUSE,HITL,SKIP pause;
    class GROUND,FIX,SAFE,CAP guard;
```

---

## 图 3 · HITL 状态机

对应 `run_turn` / `_pause` / `resume_turn`，以及 `session.py` 中的 `SessionState`。

```mermaid
stateDiagram-v2
    [*] --> idle
    idle --> running : run_turn(消息)
    running --> running : 模型/工具往返（step++）
    running --> completed : 模型不再调用工具 → 接地校验后返回（安全声明由提示词与模型输出保证）
    running --> error : 模型调用失败（安全话术，不产出风险等级）
    running --> waiting_human : 工具返回 requires_human=true → 存 checkpoint

    waiting_human --> waiting_human : 再次 run_turn（回执：请先完成复核）
    waiting_human --> running : resume_turn(approve)，等级保持原值
    waiting_human --> running : resume_turn(override, level)，等级改为人工指定值
    waiting_human --> running : resume_turn(reject)，等级改为 LOW 并记「误报」备注

    note right of waiting_human
        resume_turn 副作用（缺一不可）：
        · 审计事件 main_agent.human_risk_review
        · state.risk_status = {level, reasons 追加人工意见, requires_human=false}
        · session.human_review = {reviewer, action, old/new level, note}
        · follow_up.status = human_reviewed
        · 沿用同一段 transcript 继续循环（非重新开始）
        非法动作 → ValueError；无待复核任务 → RuntimeError
    end note

    completed --> idle : 下一次 run_turn
    error --> idle : 下一次 run_turn
    completed --> [*]
```

---

## 图 4 · 端到端时序（高危 → 人工复核 → 合规）

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant CLI as main.py（CLI）
    participant MA as MainAgent（ReAct）
    participant M as ChatModel
    participant REG as ToolRegistry
    participant A4 as Agent 4 + 规则引擎
    participant A5 as Agent 5
    actor H as 人工复核员
    participant ST as SharedUserState / AuditEvent

    U->>CLI: "孕周42周，阴道出血"
    CLI->>MA: run_turn(消息)
    MA->>MA: 组装 messages（system + 工具表 + 上下文 + 历史）
    loop step 1..max_steps（直到无 tool_calls 或 HITL）
        MA->>M: complete(messages, tools)
        M-->>MA: assistant.content + tool_calls
        MA->>REG: invoke(tool, args)
        REG->>ST: 审计事件 + state_update（回给会话合并）
        REG-->>MA: observation（真实结果，回填 transcript）
    end
    Note over REG,A4: 示例：离线策略模型的首轮决策顺序<br/>record_user_facts → assess_risk → audit_governance
    REG->>A4: AgentInput(task=run_risk_assessment)
    A4->>A4: MaternityRiskRuleEngine.evaluate()
    A4-->>REG: HIGH + requires_human=true + rules[R-SY-01,R-HX-01]
    REG-->>MA: requires_human ⇒ 暂停（跳过剩余工具）
    MA->>ST: 保存 checkpoint（pending_human / active_risk）
    MA-->>CLI: status=needs_human_review + 问题 + ReAct 轨迹
    CLI-->>U: 「已暂停自动流程，等待人工复核」

    U->>H: 人工介入
    H->>CLI: approve / override / reject（+备注）
    CLI->>MA: resume_turn(...)
    MA->>ST: ① human_risk_review 审计事件 ② risk_status 更新 ③ follow_up=human_reviewed
    MA->>M: 继续同一 transcript（注入人工结论消息）
    M-->>MA: tool_calls = [audit_governance]（同轮下一个 step）
    MA->>REG: audit_governance
    REG->>A5: AgentInput(task=run_governance_audit, audit_events=...)
    A5->>ST: 校验审计完整性 / 权限 / consent / HITL 一致性
    A5-->>REG: COMPLIANT（critical ⇒ NON_COMPLIANT + requires_human=true）
    REG-->>MA: observation（真实校验结论，回填 transcript）
    MA->>M: complete(messages, tools)（已调用过的工具不再提供）
    M-->>MA: 最终回答（含结论 + 人工意见 + 安全声明）
    MA->>MA: 接地校验（等级必须等于规则引擎结论）
    MA-->>CLI: TurnResult（completed, audit_events, trace）
    CLI-->>U: 显示结论与 /audit 审计链路
```

---

## 图 5 · Orchestrator 批量路径（无模型，确定性编排）

对应 `reprojourney/agents/orchestrator/orchestrator.py`。

```mermaid
flowchart TD
    IN["AgentInput<br/>request_id / user_id / state / context"] --> CP["start_flow(max_rounds=5)<br/>deepcopy 状态 + 建 checkpoint"]
    CP --> R4["Agent 4 · RiskAssessmentAgent.run()"]
    R4 --> M1["编排器合并 state_update<br/>（唯一写者）"]
    M1 --> HU{"requires_human ?"}

    HU -->|"是"| WAIT["返回 waiting_human<br/>pending_next_agent = governance_audit_agent<br/>checkpoint 保留待人工结论"]
    WAIT --> RES["resume_after_human(approve / override / reject, reviewer, note)"]
    RES -->|"checkpoint 不存在"| ERR2["返回 error"]
    RES --> APPLY["写回 risk_status（reasons 追加人工意见；override ⇒ 人工指定值，reject ⇒ LOW）<br/>+ human_risk_review 审计事件 + context.human_review<br/>（pending_risk_result / pending_next_agent 暂停时已存，恢复时沿用）"]
    APPLY --> GOV
    HU -->|"否"| GOV

    GOV["Agent 5 · GovernanceAuditAgent.run()<br/>输入含累计 audit_events"] --> M2["编排器合并 state_update"]
    M2 --> VERD{"gov_out.requires_human ?<br/>（_finalize_after_governance）"}
    VERD -->|"是（critical ⇒ NON_COMPLIANT）"| NI["返回 needs_info<br/>requires_human=true<br/>next_action=[request_human_review]"]
    VERD -->|"否"| DONE["返回 completed · status=success<br/>+ 完整 audit_events"]
    NI --> LVL["risk_level 取 checkpoint.state.risk_status<br/>即人工复核后的等级（override/reject 生效）"]
    DONE --> LVL
    VERD -->|"while 循环自然退出（round ≥ max_rounds）"| ERR["返回 error：max_round_exceeded"]

    classDef stop fill:#ffebee,stroke:#c62828,color:#b71c1c;
    class WAIT,NI,ERR,ERR2 stop;
```

---

## 图 6 · 开发与验证流水线（规则/代码变更的准入流程）

```mermaid
flowchart LR
    C["修改规则 / 智能体 / 工具"] --> T1["python -m pytest -q<br/>92 用例：契约 · ReAct · 安全 · 标准"]
    T1 --> OK1{"全部通过？"}
    OK1 -->|"否"| FIX["按失败用例定位（含 rule_id）"]
    OK1 -->|"是"| EV["python evaluation/run_evaluation.py<br/>18 标签用例 + 3 端到端场景"]
    EV --> OK2{"阈值全达标？<br/>准确率≥90% · 高危漏报=0<br/>审计完整性=100% · 安全=100%"}
    OK2 -->|"否"| FIX
    OK2 -->|"是"| ART["证据产物：evaluation/report.md<br/>+ results.json（退出码 0，可接 CI）"]
    FIX --> C
    ART --> DONE(["可提交 / 演示"])

    classDef bad fill:#ffebee,stroke:#c62828,color:#b71c1c;
    classDef good fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    class FIX bad;
    class ART,DONE good;
```

---

## 图表校验与预览

- **预览**：GitHub 直接渲染 Mermaid；VS Code 安装 “Markdown Preview Mermaid Support” 扩展后
  按 `Ctrl+Shift+V`；也可把代码块粘贴到 <https://mermaid.live>。
- **校验（可选）**：`docs/check_flowchart.mjs` 用 Mermaid 官方解析器逐图 `parse`，
  有任何语法错误即退出码 1。本文档当前 6 张图均已通过校验
  （`flowchart-v2` × 4、`stateDiagram` × 1、`sequence` × 1）：

  ```powershell
  npm i mermaid@11 jsdom@24                    # 任意目录安装一次（仓库本身不依赖 Node）
  $env:MERMAID_DEPS = "<含 node_modules 的目录>"
  node docs/check_flowchart.mjs docs/FLOWCHART.md
  ```

- **文档断言守卫（可选）**：`docs/check_docs.py` 用纯标准库，把本文件与 `README.md` 里
  “可被代码证伪”的断言逐条对照源码 —— 测试总数与拆分、工具白名单、`R-*` / `G-*` rule_id 目录、
  评估阈值与语料规模、`max_react_steps` / `max_rounds` 默认值、图的数量与类型，
  以及本文档引用的行号与符号是否仍落在代码里；与代码不符即退出码 1：

  ```powershell
  python docs/check_docs.py                    # 无需安装任何依赖
  ```

---

## 图 ↔ 代码一致性核验（逐节点审计记录）

每张图都不是凭记忆画的，而是对着代码逐节点核对。核验方式与偏差如下：

| 图 | 核验方式 | 结论 |
| --- | --- | --- |
| 图 0 / 图 1 | 逐节点 `grep` 到具体文件/函数：`main.py`、`registry.py::build_default_registry`（6 个工具名）、`risk_rules.py::evaluate`（`requires_human = risk_level == "HIGH"`，L140）、Agent 4/5 的 `status` 取值 | 修正 3 处：入口归属（batch/manual 直连 Agent 4/5，不走编排器）、审计事件来源、Agent 5 结论到编排器的传导 |
| 图 2 | 对照 `agent.py::_run_loop` 行号：L184-191（trace/assistant 落 transcript）、L193（无 tool_calls 收敛）、L200-223（逐调用 `invoke` + 记账）、L203（`context.state` 同步）、L225-228（会话合并）、L230-247（HITL 暂停 + skipped 占位） | 修正 1 处：`assistant(tool_calls)` 入 transcript 属于循环，不属于 `ToolRegistry.invoke` |
| 图 3 | 对照 `_pause` / `resume_turn` / `session.py::SessionState` 的 5 项副作用 | 修正 1 处：安全声明不是 `agent.py` 追加的 |
| 图 4 | 对照 `offline_chat.py` L215-263 的决策顺序（facts → human → risk → governance → final） | 修正 2 处：`assess_risk` 标为示例序列；恢复后先 `audit_governance` 再出最终回答 |
| 图 5 | 对照 `orchestrator.py` 全文，并用真实场景跑通（approve / override / reject、以及 critical 场景） | 修正 2 处图错误，并**修掉 2 个真实代码缺陷**（见下） |
| 图 6 | 实跑 `python -m pytest -q`（92 通过）与 `python -m evaluation.run_evaluation`（verdict PASS）；阈值取自 `evaluation/cases.py::SAFETY_THRESHOLDS` | 一致 |

### 审计中发现并修复的两个代码缺陷（`orchestrator.py`）

1. **治理闸门不传导**：Agent 5 返回 `NON_COMPLIANT`（critical，例如高危场景未取得外部升级授权）时，
   编排器原先一律返回 `completed` + `status=success` + `requires_human=false`，闸门形同虚设。
   现由 `Orchestrator._finalize_after_governance` 传导为 `needs_info` + `requires_human=true`
   + `next_action=["request_human_review"]`。
2. **返回陈旧风险等级**：恢复流程原用 `pending_risk_result`（人工复核**之前**的等级）作为输出等级，
   导致 `override → MODERATE`、`reject → LOW` 在 `AgentOutput` 上仍显示 `HIGH`。
   现改取 `checkpoint.state.risk_status.level`，即人工结论。

两处均补了回归用例：`tests/test_agents.py::test_31_governance_failure_is_not_reported_as_success`、
`test_32_returned_risk_level_follows_human_decision`（用例总数 84 → 92，新增 8 个开发标准契约用例）。

---

## 节点 ↔ 代码映射表

| 图中节点 | 代码位置 |
| --- | --- |
| `run_turn` / `_build_messages` / `_run_loop` | `reprojourney/agents/main_agent/agent.py` |
| `_pause` / checkpoint / `pending_human` | `agent.py::_pause`、`session.py::SessionState` |
| `resume_turn` / 人工结论副作用 | `agent.py::resume_turn` |
| 接地校验 | `agent.py::_grounding_check`、`_finalize` |
| 工具分发 / 审计 / 状态合并 | `reprojourney/tools/registry.py::ToolRegistry` |
| 状态合并规则 | `session.py::apply_state_update`、`orchestrator.py::_merge_state_update` |
| 脱敏与落盘门禁 | `session.py::redact`、`registry.py` 的 `persist_session` |
| 工具清单与提示词 | `prompts.py::build_tools_hint`、`registry.py::build_default_registry` |
| 风险分级（唯一来源） | `reprojourney/schemas/risk_rules.py::MaternityRiskRuleEngine` |
| 开发标准契约（§2–§7） | `reprojourney/schemas/agent_contract.py`（字段闭集、四值等级、输出/审计校验） |
| 统一入口与输出强制 | `reprojourney/agents/base.py::BaseAgent`（`prepare_input` / `enforce_output`） |
| Agent 4 | `reprojourney/agents/risk/agent4_risk_assessment.py` |
| Agent 5 | `reprojourney/agents/governance/agent5_governance_audit.py` |
| 模型抽象层 | `reprojourney/llm/`（`types` / `factory` / `openai_chat` / `anthropic_chat` / `offline_chat` / `scripted_chat`） |
| 批量编排 | `reprojourney/agents/orchestrator/orchestrator.py` |
| 治理结论传导 / 恢复后的等级 | `orchestrator.py::_finalize_after_governance` |
| 工具审计事件来源 | `registry.py`：`read_user_state` / `record_user_facts` / `request_human_review` / `persist_session` 自建事件；`assess_risk` / `audit_governance` 用 `ctx.add_events(子智能体事件)` 汇入 |
| 回复中的安全声明 | `prompts.py` 系统提示要求 + `offline_chat.py::_compose_final_answer`；`agent.py::_finalize` 仅在空文本时填兜底话术 |
| 旁路入口 | `batch_cases.py`、`manual_test.py`（直连 Agent 4 / Agent 5） |
| CLI | `main.py` → `agent.py::run_cli_session` |
| 测试与评估 | `tests/`、`evaluation/cases.py`、`evaluation/run_evaluation.py` |

> 生成/更新时间：见 `evaluation/report.md` 中的运行记录；如需重新出图，只需在支持 Mermaid 的
> 编辑器中打开本文件，无需安装任何依赖。




