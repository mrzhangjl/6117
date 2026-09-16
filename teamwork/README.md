# ReproJourney 孕产风险与治理智能体（Agent 4 / Agent 5 / ReAct 主智能体）

> 文档索引：[整体流程图 `docs/FLOWCHART.md`](docs/FLOWCHART.md) ·
> [Agent 4 工作流程图（含示例输入走查） `docs/AGENT4_WORKFLOW.md`](docs/AGENT4_WORKFLOW.md) ·
> [Agent 5 工作流程图（含示例输入走查） `docs/AGENT5_WORKFLOW.md`](docs/AGENT5_WORKFLOW.md) ·
> [架构与安全设计 `docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
> [开发标准落地与偏差登记 `docs/AGENT_STANDARD.md`](docs/AGENT_STANDARD.md) ·
> [评估报告 `evaluation/report.md`](evaluation/report.md)

本项目按《ReproJourney Agent Development Standard v1.0》实现一个可运行、可审计、可评估的
孕产健康助手智能体系统：**标准里的字段与规则不是文档承诺，而是 `reprojourney/schemas/agent_contract.py`
里的常量 + `tests/test_standard_contract.py` 里的断言**（逐条对照见 `docs/AGENT_STANDARD.md`）：

| 组件 | 角色 | 关键约束 |
| --- | --- | --- |
| `MaternityRiskRuleEngine` | 确定性风险规则引擎（唯一决策者） | 规则带 `rule_id`，可复现、可作为审计证据 |
| **Agent 4** `RiskAssessmentAgent` | 风险评估 | 风险等级只来自规则引擎；HIGH ⇒ `escalate` + `requires_human` |
| **Agent 5** `GovernanceAuditAgent` | 治理与审计 | 审计完整性、Consent 授权、隐私数据域、HITL 记录校验；`risk_level` 恒为 `UNKNOWN`（§4），结论在 `compliance_status` |
| **Main Agent** `MainAgent` | 真实 ReAct 循环 + 会话/HITL | thought → action → observation；工具调用 + 人工复核暂停/恢复 |
| `Orchestrator` | 非交互式编排 | **唯一** 合并 `state_update` 到共享状态 |
| `BaseAgent` | 开发标准契约基类 | 统一入口 `run(agent_input, state=None)`；每次返回前校验 §4–§6 |
| `evaluation/` | 评估基座 | 分级准确率、高危漏报率、误报率、HITL 触发质量、审计完整性 |

## 1. 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # 仅需 pydantic，即可离线运行

python main.py                            # ReAct 主智能体交互式 CLI
```

无需任何 API key：默认使用内置的**确定性离线策略模型**
（`REPROJOURNEY_LLM_PROVIDER=offline`），它通过完全相同的消息协议驱动真实的 ReAct 循环、
工具层、HITL 门禁与审计链路。配置真实模型只需复制 `.env.example` 为 `.env` 并填入 key：

```powershell
Copy-Item .env.example .env
# .env:  REPROJOURNEY_LLM_PROVIDER=openai    + OPENAI_API_KEY=...
#        或 REPROJOURNEY_LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY=...
pip install "openai>=1.40"    # 或 anthropic>=0.34；可选 python-dotenv>=1.0
```

CLI 内可用命令：`/state`（共享状态）、`/audit`（审计链路）、`/tools`（工具清单）、`/quit`。

### 其它入口

```powershell
python batch_cases.py      # 10 个结构化病例的批量风险评估（输出 case_results.csv）
python manual_test.py      # Agent 4 / Agent 5 的最小可读示例
python evaluation/run_evaluation.py   # 评估基座：写 evaluation/report.md + results.json
# agent4_agent5_demo.ipynb            # 独立 demo：Jupyter 打开即可跑（Agent 4 / Agent 5 单机调用 + HITL）
```

## 2. 真实 ReAct 循环（不是关键词路由）

```text
用户消息
  │
  ├─ Thought      模型输出判断 + 结构化 tool_calls
  ├─ Action       ToolRegistry 按名分发（每个工具都会写审计事件）
  ├─ Observation  真实工具结果以 tool 消息回填，供下一步推理（grounding）
  ├─ 状态合并     只有会话/编排器把 state_update 合入 SharedUserState
  ├─ HITL         任一工具返回 requires_human=true ⇒ 立即暂停 + 存档 checkpoint
  └─ Final        输出结论 + 固定安全声明（并做接地校验）
```

关键机制：

- **消息协议**：`system / user / assistant(tool_calls) / tool(tool_call_id+observation)`，与
  OpenAI、Anthropic 原生 function calling 一一对应（`reprojourney/llm/`）。
- **HITL 暂停/恢复**：`run_turn()` 返回 `status="needs_human_review"` 并保存完整对话与
  checkpoint；`resume_turn(approve|override|reject)` 写入人工结论（含 `human_risk_review`
  审计事件）后**继续同一段 transcript**，而不是重新开始。
- **模型无关性**：`OfflinePolicyChatModel`（确定性）、`OpenAIChatModel`、`AnthropicChatModel`、
  `ScriptedChatModel`（测试/评估）实现同一个 `BaseChatModel` 接口。
- **接地校验**：若最终回答中的风险等级与规则引擎结论冲突，系统会追加强制更正并写入
  `risk_level_grounding_correction` 审计事件。
- **步数上限**：`REPROJOURNEY_MAX_REACT_STEPS`（默认 8），超限即安全停止并给出追问话术。

### 工具清单（`ToolRegistry` · `reprojourney/tools/registry.py`）

| 工具 | 作用 | 审计 |
| --- | --- | --- |
| `read_user_state` | 只读共享状态 | `session_state.read_user_state` |
| `record_user_facts` | 用户明确陈述的事实结构化入库（去重、范围校验） | `session_state.record_user_facts` |
| `assess_risk` | 调用 Agent 4（规则引擎） | 转发 Agent 4 的审计事件 |
| `audit_governance` | 调用 Agent 5 合规门禁 | 转发 Agent 5 的审计事件 |
| `request_human_review` | 创建人工复核任务并暂停 | `main_agent.request_human_review`（`human_required=true`） |
| `persist_session` | 脱敏快照落盘（需授权） | `local_filesystem.persist_state_snapshot` / `persist_session_denied` |

## 3. 风险规则目录（确定性、带 rule_id）

| rule_id | 触发条件 | 等级 |
| --- | --- | --- |
| `R-GA-01` | 孕周 > 42 | HIGH |
| `R-GA-02` | 孕周 < 37 且有早产相关症状 | MODERATE |
| `R-GA-03` | 孕周 < 37 无早产症状 | LOW |
| `R-GA-04` | 孕周 ≤ 0 或 > 45（数据异常） | UNKNOWN（需人工核对） |
| `R-SY-01` | 高危症状（阴道出血/破水/规律性宫缩/剧烈腹痛/头晕晕厥/视物模糊） | HIGH |
| `R-SY-02` | 需观察症状（轻微腰酸/假性宫缩/轻度水肿/轻微恶心） | MODERATE |
| `R-SY-03` | 腹痛 / 腹部坠胀 | MODERATE |
| `R-SY-04` | 无法识别的症状描述（注入防护：不参与分级） | UNKNOWN（需确认） |
| `R-RP-01` | 报告异常标记（bp_high / glucose_high / proteinuria / fetal_heart_abnormal / amniotic_fluid_abnormal） | HIGH |
| `R-RP-02` | 需关注报告指标（hb_low / platelet_low / tsh_abnormal / gbs_positive / bmi_high） | MODERATE |
| `R-RP-03` | 未识别的异常标记（保守默认） | MODERATE |
| `R-RP-04` | 报告缺少结构化数据 | UNKNOWN（不降级为 LOW） |
| `R-HX-01` | 高危病史（子痫前期/瘢痕子宫/既往早产史/妊娠期糖尿病） | MODERATE |
| `R-FU-01` | 随访超期 > 14 天 | MODERATE |
| `R-INFO-01` | 信息不足 / 存在无法解析的数据（兜底：不给出 LOW 结论） | UNKNOWN |
| 安全兜底 | `risk_level == HIGH` ⇒ `requires_human = true` | — |

## 4. 治理与审计（Agent 5）

`severity=critical` 的发现会导致 `NON_COMPLIANT` + `status="needs_info"` + `requires_human=true`；
仅 warning 时为 `COMPLIANT_WITH_WARNINGS`（仍算通过）。
编排器不会吞掉这个结论：`Orchestrator._finalize_after_governance` 会把 `requires_human` 原样
传导为 `needs_info` + `next_action=["request_human_review"]`，并回传人工复核后的风险等级。

| rule_id | 检查内容 |
| --- | --- |
| `G-AUD-01` | 有风险判定但审计链路为空 |
| `G-AUD-02` | 审计事件字段不完整（request_id/agent/action/decision/timestamp） |
| `G-AUD-03` | 审计事件缺少 `data_accessed`（warning） |
| `G-AUD-04` | 混入其它 request 的审计事件（warning） |
| `G-AUD-05` | 存在无法解析的审计事件 |
| `G-GOV-01` | 风险判定缺少对应的风险评估审计事件 |
| `G-PRV-01` | 审计事件出现敏感信息（password/api_key/secret/token/身份证/银行卡） |
| `G-PRV-02` | 访问了未授权数据域（最小权限表 `AGENT_DATA_SCOPE`） |
| `G-CON-01` | 高风险场景未取得外部升级授权 |
| `G-CON-02` | 用户未授权记录存储（warning） |
| `G-CON-03` | 已有咨询记录但缺少存储授权（warning） |
| `G-HITL-01/02` | `human_required` 的判定缺少人工复核记录 |
| `G-HITL-03` | 人工复核结论未写入共享状态 |
| `G-HITL-04` | 上下文有人工复核信息但缺少对应审计事件（warning） |

治理智能体自身的审计事件只记录 `rule_id` 与计数，**不复制原始值**，避免"审计记录泄露被审计数据"。

## 5. 测试与评估

```powershell
python -m pytest -q                     # 92 个测试：契约、ReAct、安全/负向、开发标准
python -m unittest discover -s tests -v # 同样的用例，标准库即可运行
python evaluation/run_evaluation.py     # 评估基座（退出码非 0 表示未达阈值）
python docs/check_docs.py               # 文档守卫：文档里的数字/名称/行号 ↔ 代码（非 pytest）
```

- `tests/test_agents.py` —— 原始端到端/编排用例（32 个：原 30 个保持兼容 + 2 个编排器回归）
- `tests/test_agent_contracts.py` —— Agent 4 / Agent 5 / 规则引擎契约与规则覆盖
- `tests/test_standard_contract.py` —— 开发标准 v1.0 契约（§2 状态字段、§3 输入、§4/§5 输出与等级、§6 审计事件、§7 入口与禁止事项）
- `tests/test_react_agent.py` —— ReAct 循环、工具调度、HITL 暂停/恢复、接地校验、状态合并
- `tests/test_safety.py` —— 注入抵抗、HITL 不可绕过、越权升级阻断、隐私与审计完整性

评估阈值（见 `evaluation/cases.py`）：分级准确率 ≥ 90%、**高危漏报率 = 0**、
审计完整性 = 100%、安全用例通过率 = 100%、端到端场景全部通过。

## 6. 目录结构

```text
teamwork/
├── main.py                     # CLI 入口
├── batch_cases.py              # 批量病例脚本
├── manual_test.py              # 最小可读示例
├── agent4_agent5_demo.ipynb    # 独立 demo：Agent 4 / Agent 5 单独运行 + ReAct/HITL 走一遍
├── .env.example                # 环境变量模板（真实 key 放 .env，已被 .gitignore 忽略）
├── docs/ARCHITECTURE.md        # 设计说明：循环、HITL 状态机、审计模型、安全策略
├── docs/FLOWCHART.md           # 整体流程图（Mermaid + ASCII，含代码映射表）
├── docs/AGENT_STANDARD.md      # 开发标准 v1.0 的落地对照 + 偏差登记
├── docs/check_flowchart.mjs    # 可选：用 Mermaid 官方解析器校验图语法（需 Node）
├── docs/check_docs.py          # 可选：文档断言守卫（数字 / 名称 / 行号 ↔ 代码，纯标准库）
├── evaluation/
│   ├── cases.py                # 18 个带标签用例 + 安全阈值
│   ├── run_evaluation.py       # 评估基座（生成 report.md / results.json）
│   └── report.md               # 最近一次评估报告（证据产物）
├── reprojourney/
│   ├── config.py               # 环境变量/`.env` 配置（无绝对路径、无内置密钥）
│   ├── llm/                    # 模型抽象层：types/factory/openai/anthropic/offline/scripted
│   ├── schemas/                # AgentInput/AgentOutput/AuditEvent/SharedUserState + 规则引擎 + 开发标准契约
│   │   ├── agent_contract.py   # 开发标准 v1.0 的常量与校验函数（§2–§7 唯一真源）
│   │   └── risk_rules.py       # 确定性风险规则引擎
│   ├── tools/                  # 工具层（标准 §7/§8）：registry.py = ToolRegistry + 6 个工具
│   └── agents/
│       ├── base.py             # BaseAgent：统一入口 run(agent_input, state=None) + 输出契约强制
│       ├── main_agent/         # ReAct 会话层（prompts / session / agent）
│       ├── orchestrator/       # 非交互式编排（唯一合并 state_update 的非会话路径）
│       ├── risk/               # Agent 4 —— 标准槽位 agents/risk
│       ├── governance/         # Agent 5 —— 标准槽位 agents/governance
│       ├── health_record/      # 待建：标准槽位 Agent 2（见 docs/AGENT_STANDARD.md 偏差 F）
│       └── consultant/         # 待建：标准槽位 Agent 3
└── tests/
```

## 7. 安全与合规要点

- **不做医学诊断**：所有回答都带固定声明"不构成医学诊断"；高风险一律转人工。
- **等级不可被诱导**：风险等级只由规则引擎产生；提示注入、自由文本症状、模型措辞都无法降低等级。
- **人工不可绕过**：HITL 在工具层触发（`requires_human`），模型无法"说服"系统跳过。
- **数据最小化**：审计事件只记录受限数据域；落盘快照默认脱敏，且必须 `allow_record_storage`。
- **无外联能力**：工具表内不存在任何网络/短信/邮件工具（`tests/test_safety.py` 断言）。
- **可离线复现**：评估与测试不需要外部 API，结果确定可复现。

