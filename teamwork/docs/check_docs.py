#!/usr/bin/env python
"""可选工具：校验文档里"可被代码证伪"的断言（与 ``docs/check_flowchart.mjs`` 并列）。

``README.md``、``docs/FLOWCHART.md`` 与 ``docs/AGENT_STANDARD.md`` 中的测试数、工具名、
rule_id、评估阈值、行号引用、符号名，本质上都是对代码的**承诺**。改了代码却忘改文档时，
本脚本以退出码 1 报出来；它守的是文档，不属于 pytest 套件（因此不会改变测试总数）。

用法（仓库根目录）::

    python docs/check_docs.py                  # 校验 README.md + docs/FLOWCHART.md + docs/AGENT_STANDARD.md
    python docs/check_docs.py CONTRIBUTING.md  # 再附加一份文档（仅参与通用检查）

退出码：0 全部一致；1 有断言与代码不符；2 用法 / 文件错误。

设计约束：
  * 只用标准库，不 import 项目代码（``evaluation.cases`` 之类的值直接解析源码文本），
    这样脚本在依赖未装、代码有语法错误时同样可用；
  * 数字一律从代码侧算出，再与文档里的字面量比对，绝不把数字写死在脚本里。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS: Tuple[str, ...] = ("README.md", "docs/FLOWCHART.md", "docs/AGENT_STANDARD.md")

# 运行时产物 / 被 .gitignore 忽略的文件：文档提到但当前不存在不算文档错误
ALLOW_MISSING: Set[str] = {
    ".env",
    "case_results.csv",
    "evaluation/report.md",
    "evaluation/results.json",
}

# 扫描卫生问题时跳过的目录
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules", ".idea"}

# 高危秘钥形态：长度足够才判定，避免把 "sk-secret" 之类的假 key 当泄漏
SECRET_PATTERNS: Tuple[str, ...] = (
    r"sk-[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"ghp_[A-Za-z0-9]{30,}",
    r"xox[baprs]-[0-9A-Za-z-]{10,}",
    r"AIza[0-9A-Za-z_-]{30,}",
)

# 文档引用行号背后的语义断言：代码重排后行号会漂，这里把"语义"也钉住
LINE_ANCHORS: Tuple[Tuple[str, int, int, Tuple[str, ...]], ...] = (
    ("reprojourney/agents/main_agent/agent.py", 184, 191, ("trace.append", "ChatMessage.assistant")),
    ("reprojourney/agents/main_agent/agent.py", 193, 193, ("has_tool_calls",)),
    ("reprojourney/agents/main_agent/agent.py", 200, 223, ("invoke(", "redact(arguments)")),
    ("reprojourney/agents/main_agent/agent.py", 203, 203, ("context.state = self.session.state",)),
    ("reprojourney/agents/main_agent/agent.py", 225, 228, ("apply_state_update",)),
    ("reprojourney/agents/main_agent/agent.py", 230, 247, ("skipped",)),
    ("reprojourney/schemas/risk_rules.py", 140, 140, ('requires_human = risk_level == "HIGH"',)),
    ("reprojourney/llm/offline_chat.py", 215, 263, ("FACT_TOOL", "GOV_TOOL")),
)

# 「节点 ↔ 代码映射表」里的符号承诺
SYMBOL_ANCHORS: Tuple[Tuple[str, str], ...] = (
    ("reprojourney/agents/main_agent/agent.py", "def run_turn"),
    ("reprojourney/agents/main_agent/agent.py", "def _build_messages"),
    ("reprojourney/agents/main_agent/agent.py", "def _run_loop"),
    ("reprojourney/agents/main_agent/agent.py", "def _pause"),
    ("reprojourney/agents/main_agent/agent.py", "def resume_turn"),
    ("reprojourney/agents/main_agent/agent.py", "def _grounding_check"),
    ("reprojourney/agents/main_agent/agent.py", "def _finalize"),
    ("reprojourney/agents/main_agent/agent.py", "def run_cli_session"),
    ("reprojourney/tools/registry.py", "def build_default_registry"),
    ("reprojourney/tools/registry.py", "class ToolRegistry"),
    ("reprojourney/tools/registry.py", "async def invoke"),
    ("reprojourney/agents/main_agent/prompts.py", "def build_tools_hint"),
    ("reprojourney/agents/main_agent/session.py", "class SessionState"),
    ("reprojourney/agents/main_agent/session.py", "def apply_state_update"),
    ("reprojourney/agents/main_agent/session.py", "def redact"),
    ("reprojourney/schemas/risk_rules.py", "class MaternityRiskRuleEngine"),
    ("reprojourney/agents/orchestrator/orchestrator.py", "def _merge_state_update"),
    ("reprojourney/agents/orchestrator/orchestrator.py", "def _finalize_after_governance"),
    ("reprojourney/llm/offline_chat.py", "def _compose_final_answer"),
    # 开发标准（docs/AGENT_STANDARD.md）：条款 -> 代码的承诺
    ("reprojourney/schemas/agent_contract.py", "STATE_FIELDS: Tuple[str, ...] = ("),
    ("reprojourney/schemas/agent_contract.py", "STATE_SUBFIELDS: Dict[str, Tuple[str, ...]] = {"),
    ("reprojourney/schemas/agent_contract.py", "AGENT_SLOTS: Dict[str, str] = {"),
    ("reprojourney/schemas/agent_contract.py", 'TOOL_SLOT = "reprojourney/tools"'),
    ("reprojourney/schemas/agent_contract.py", "def standard_problems"),
    ("reprojourney/schemas/agent_contract.py", "def check_output_contract"),
    ("reprojourney/schemas/agent_contract.py", "def requested_by_problem"),
    ("reprojourney/schemas/agent_contract.py", "def audit_event_problems"),
    ("reprojourney/agents/base.py", "class BaseAgent"),
    ("reprojourney/agents/base.py", "def prepare_input"),
    ("reprojourney/agents/base.py", "def enforce_output"),
    (
        "reprojourney/agents/risk/agent4_risk_assessment.py",
        "async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput:",
    ),
    (
        "reprojourney/agents/governance/agent5_governance_audit.py",
        "async def run(self, agent_input: AgentInput, state: StateLike = None) -> AgentOutput:",
    ),
)


def _excluded(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _read(rel: str) -> str:
    """读取仓库内文件；缺失时抛出 SystemExit（由 main 兜住并返回退出码 2）。"""
    path = ROOT / rel
    if not path.is_file():
        raise SystemExit(f"找不到文件：{path}")
    return path.read_text(encoding="utf-8")


def _source(name: str) -> Optional[Path]:
    """按文件名定位仓库内唯一源码文件（文档里常只写 ``agent.py``）。同名多个则返回 None。"""
    hits = [p for p in ROOT.rglob(name) if p.is_file() and not _excluded(p)]
    return hits[0] if len(hits) == 1 else None


def _grab(text: str, pattern: str) -> Optional[int]:
    """取第一个捕获组并转成 int；没匹配到返回 None。"""
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def _pct(value: float) -> str:
    """0.9 → '90'；文档里的阈值一律以百分比书写。"""
    return f"{int(round(value * 100))}"


class Report:
    """按检查项收集失败原因，最后统一打印，避免"报一半就退出"。"""

    def __init__(self) -> None:
        self.by_check: Dict[str, List[str]] = {}
        self.notes: List[str] = []

    def fail(self, check: str, detail: str) -> None:
        self.by_check.setdefault(check, []).append(detail)

    def note(self, detail: str) -> None:
        self.notes.append(detail)

    def expect(
        self,
        check: str,
        label: str,
        value: object,
        reference: object,
        names: Tuple[str, str] = ("文档", "代码"),
    ) -> None:
        """断言两侧不相等即记一次失败；names 决定报错时两个值的标签。"""
        if value != reference:
            self.fail(check, f"{label}：{names[0]}={value!r}，{names[1]}={reference!r}")

    def expect_set(self, check: str, label: str, doc_ids: Set[str], code_ids: Set[str]) -> None:
        missing = sorted(code_ids - doc_ids)
        extra = sorted(doc_ids - code_ids)
        if missing:
            self.fail(check, f"{label}：文档缺少 {missing}")
        if extra:
            self.fail(check, f"{label}：文档多出（代码里没有）{extra}")

    def finish(self, check: str, detail: str = "") -> None:
        problems = self.by_check.get(check, [])
        suffix = f"  ({detail})" if detail else ""
        if not problems:
            print(f"{check}: OK{suffix}")
            return
        print(f"{check}: FAIL{suffix}")
        for problem in problems:
            print(f"    - {problem}")

    @property
    def failed(self) -> List[str]:
        return [name for name, problems in self.by_check.items() if problems]


# --------------------------------------------------------------------------- 检查：测试数

def check_test_counts(docs: Dict[str, str], report: Report) -> None:
    check = "test_counts"
    per_file = {
        path.name: len(re.findall(r"(?m)^\s*(?:async )?def test_", path.read_text(encoding="utf-8")))
        for path in sorted((ROOT / "tests").glob("test_*.py"))
    }
    total = sum(per_file.values())
    agents = per_file.get("test_agents.py", 0)
    readme, flowchart = docs["README.md"], docs["docs/FLOWCHART.md"]

    report.expect(check, "README「N 个测试」", _grab(readme, r"# (\d+) 个测试"), total)
    report.expect(check, "FLOWCHART 图 6「N 用例」", _grab(flowchart, r"(\d+) 用例："), total)
    report.expect(check, "FLOWCHART 核验表「（N 通过）」", _grab(flowchart, r"（(\d+) 通过）"), total)

    breakdown = re.search(
        r"test_agents\.py[^\n]*?（(\d+) 个：原 (\d+) 个保持兼容 \+ (\d+) 个编排器回归）", readme
    )
    regression: Optional[int] = None
    if breakdown is None:
        report.fail(check, "README 里找不到 test_agents.py 的用例拆分说明（N 个：原 M 个… + K 个…）")
    else:
        declared, kept, added = (int(group) for group in breakdown.groups())
        regression = added
        report.expect(check, "README test_agents.py 用例数", declared, agents)
        report.expect(check, "README 拆分之和 = 该文件用例数", kept + added, declared, ("和", "声明"))

    delta = re.search(r"用例总数 (\d+) → (\d+)", flowchart)
    if delta is None:
        report.fail(check, "FLOWCHART 核验表里找不到「用例总数 A → B」")
    else:
        before, after = (int(group) for group in delta.groups())
        report.expect(check, "FLOWCHART 核验表用例总数（后者）", after, total)
        # 增量必须能对上：优先用"新增 N 个…用例"的显式声明（可覆盖不相邻的多次增长），
        # 没有声明时退回旧约定——增量恰好等于 test_agents.py 的编排器回归用例数。
        declared = _grab(flowchart, r"新增 (\d+) 个[^\n。]*用例")
        if declared is not None:
            report.expect(
                check,
                "FLOWCHART 声明的增量 vs 用例总数之差",
                before + declared,
                after,
                ("声明", "实际"),
            )
        elif regression is not None:
            report.expect(check, "两份文档各自声称的新增用例数", regression, total - before)

    numbers = [
        int(number)
        for number in re.findall(
            r"(?m)^\s*(?:async )?def test_(\d+)",
            (ROOT / "tests/test_agents.py").read_text(encoding="utf-8"),
        )
    ]
    if numbers != list(range(1, agents + 1)):
        report.fail(check, f"test_agents.py 的用例编号不连续或重复：{numbers}")

    report.finish(check, f"共 {total} 个（" + ", ".join(f"{n}={c}" for n, c in sorted(per_file.items())) + "）")


# --------------------------------------------------------------------------- 检查：工具白名单

def _code_tool_names() -> Set[str]:
    body = _read("reprojourney/tools/registry.py").split("def build_default_registry", 1)[1]
    return set(re.findall(r'name="([a-z_]+)"', body.split("\ndef ", 1)[0]))


def check_tool_whitelist(docs: Dict[str, str], report: Report) -> None:
    check = "tool_whitelist"
    code = _code_tool_names()
    readme, flowchart = docs["README.md"], docs["docs/FLOWCHART.md"]

    report.expect_set(
        check,
        "FLOWCHART 图 1 的 REG 节点",
        set(re.findall(r'REG --> \w+\["([a-z_]+)"\]', flowchart)),
        code,
    )

    section = readme.split("### 工具清单", 1)[-1].split("## 3.", 1)[0]
    report.expect_set(check, "README 工具清单表", set(re.findall(r"(?m)^\|\s*`([a-z_]+)`\s*\|", section)), code)

    report.expect(
        check,
        "FLOWCHART 核验表「N 个工具名」",
        _grab(flowchart, r"build_default_registry`（(\d+) 个工具名）"),
        len(code),
    )

    external = code & {"http_request", "web_search", "send_sms", "send_email", "send_notification", "webhook"}
    if external:
        report.fail(check, f"工具白名单出现外联工具（README 第 7 节承诺不存在）：{sorted(external)}")

    report.finish(check, f"{len(code)} 个：" + ", ".join(sorted(code)))


# --------------------------------------------------------------------------- 检查：rule_id 目录

def _expand_id(cell: str) -> Set[str]:
    """``G-HITL-01/02`` → ``{G-HITL-01, G-HITL-02}``。"""
    if "/" not in cell:
        return {cell}
    base, _, rest = cell.rpartition("/")
    stem = base.rpartition("-")[0]
    return {base} | {f"{stem}-{number}" for number in rest.split("/")}


def _doc_rule_ids(readme: str, section_start: str, section_end: str, prefix: str) -> Set[str]:
    section = readme.split(section_start, 1)[-1].split(section_end, 1)[0]
    ids: Set[str] = set()
    for cell in re.findall(rf"(?m)^\|\s*`({prefix}-[A-Z]+-\d+(?:/\d+)*)`", section):
        ids |= _expand_id(cell)
    return ids


def check_risk_rule_ids(docs: Dict[str, str], report: Report) -> None:
    check = "risk_rule_ids"
    code = set(re.findall(r"R-[A-Z]+-\d+", _read("reprojourney/schemas/risk_rules.py")))
    doc = _doc_rule_ids(docs["README.md"], "## 3. 风险规则目录", "## 4.", "R")
    report.expect_set(check, "README 风险规则表 vs risk_rules.py", doc, code)
    report.finish(check, f"{len(code)} 条：" + ", ".join(sorted(code)))


def check_governance_rule_ids(docs: Dict[str, str], report: Report) -> None:
    check = "governance_rule_ids"
    code = set(
        re.findall(r"G-[A-Z]+-\d+", _read("reprojourney/agents/governance/agent5_governance_audit.py"))
    )
    doc = _doc_rule_ids(docs["README.md"], "## 4. 治理与审计", "## 5.", "G")
    report.expect_set(check, "README 治理规则表 vs Agent 5", doc, code)

    stray = set(re.findall(r"G-[A-Z]+-\d+", docs["docs/FLOWCHART.md"])) - code
    if stray:
        report.fail(check, f"FLOWCHART 引用了代码里不存在的治理 rule_id：{sorted(stray)}")

    report.finish(check, f"{len(code)} 条：" + ", ".join(sorted(code)))


# --------------------------------------------------------------------------- 检查：阈值与语料规模

def _thresholds() -> Dict[str, float]:
    block = _read("evaluation/cases.py").split("SAFETY_THRESHOLDS", 1)[1].split("}", 1)[0]
    return {key: float(value) for key, value in re.findall(r'"(\w+)":\s*([\d.]+)', block)}


def check_thresholds(docs: Dict[str, str], report: Report) -> None:
    check = "thresholds"
    values = _thresholds()
    combined = docs["README.md"] + "\n" + docs["docs/FLOWCHART.md"]

    variants: Dict[str, Tuple[str, Tuple[str, ...]]] = {
        "tier_accuracy": ("分级准确率", ("≥ {v}%", "≥{v}%")),
        "high_risk_miss_rate": ("高危漏报率", ("漏报率 = {v}", "漏报率={v}", "漏报={v}")),
        "audit_completeness": ("审计完整性", ("审计完整性 = {v}%", "审计完整性={v}%")),
        "safety_pass_rate": ("安全用例通过率", ("安全用例通过率 = {v}%", "安全={v}%")),
    }
    for key, (label, templates) in variants.items():
        if key not in values:
            report.fail(check, f"evaluation/cases.py::SAFETY_THRESHOLDS 里找不到 {key}")
            continue
        wanted = [template.format(v=_pct(values[key])) for template in templates]
        if not any(text in combined for text in wanted):
            report.fail(check, f"{label}（代码={values[key]}）在文档里找不到：{' / '.join(wanted)}")

    corpus = len(re.findall(r'"case_id"', _read("evaluation/cases.py")))
    report.expect(check, "README「N 个带标签用例」", _grab(docs["README.md"], r"(\d+) 个带标签用例"), corpus)
    report.expect(check, "FLOWCHART 图 6「N 标签用例」", _grab(docs["docs/FLOWCHART.md"], r"(\d+) 标签用例"), corpus)

    batch = len(re.findall(r'"case_name"\s*:\s*"', _read("batch_cases.py")))
    report.expect(
        check, "README「N 个结构化病例」vs batch_cases.py", _grab(docs["README.md"], r"(\d+) 个结构化病例"), batch
    )

    report.finish(
        check,
        "阈值 "
        + ", ".join(f"{key}={value}" for key, value in sorted(values.items()))
        + f"；语料 {corpus} 例 / 批量 {batch} 例",
    )


# --------------------------------------------------------------------------- 检查：默认常量与等级集合

def check_constants(docs: Dict[str, str], report: Report) -> None:
    check = "constants"
    readme, flowchart = docs["README.md"], docs["docs/FLOWCHART.md"]
    config = _read("reprojourney/config.py")

    dataclass_default = _grab(config, r"max_react_steps: int = (\d+)")
    report.expect(
        check,
        "config.py 的 dataclass 默认值与 env 默认值",
        _grab(config, r'_env_int\("REPROJOURNEY_MAX_REACT_STEPS", (\d+)\)'),
        dataclass_default,
        ("env", "dataclass"),
    )
    report.expect(
        check,
        "README「REPROJOURNEY_MAX_REACT_STEPS（默认 N）」",
        _grab(readme, r"`REPROJOURNEY_MAX_REACT_STEPS`（默认 (\d+)）"),
        dataclass_default,
    )

    max_rounds = _grab(_read("reprojourney/agents/orchestrator/orchestrator.py"), r"max_rounds: int = (\d+)")
    written = {int(number) for number in re.findall(r"max_rounds\s*=\s*(\d+)", readme + flowchart)}
    for value in written:
        report.expect(check, "文档里写出的 max_rounds", value, max_rounds)
    if not written:
        report.note(f"文档提到 max_rounds 但未写出数值（代码默认 {max_rounds}），跳过逐值比对")

    rank = re.search(r"_RULE_RANK\s*=\s*\{([^}]*)\}", _read("reprojourney/schemas/risk_rules.py"))
    if rank is None:
        report.note("risk_rules.py 里找不到 _RULE_RANK，跳过风险等级集合校验")
    else:
        levels = set(re.findall(r'"(\w+)":\s*\d', rank.group(1)))
        # 只比对全大写等级词：severity 用的是小写 medium/high，不会误判
        cited = set(re.findall(r"\b(HIGH|MODERATE|MEDIUM|LOW|UNKNOWN|NORMAL|SEVERE)\b", readme + flowchart))
        stray = cited - levels
        if stray:
            report.fail(check, f"文档写出了代码里不存在的风险等级：{sorted(stray)}（代码：{sorted(levels)}）")

    report.finish(check, f"max_react_steps={dataclass_default}, max_rounds={max_rounds}")


# --------------------------------------------------------------------------- 检查：图的清点

def check_diagrams(docs: Dict[str, str], report: Report) -> None:
    check = "diagrams"
    flowchart = docs["docs/FLOWCHART.md"]
    blocks = re.findall(r"```mermaid\r?\n([\s\S]*?)```", flowchart)

    kinds = {"flowchart": 0, "stateDiagram": 0, "sequence": 0}
    for block in blocks:
        first = next((line.strip() for line in block.splitlines() if line.strip()), "")
        if first.startswith("flowchart"):
            kinds["flowchart"] += 1
        elif first.startswith("stateDiagram"):
            kinds["stateDiagram"] += 1
        elif first.startswith("sequenceDiagram"):
            kinds["sequence"] += 1
        else:
            report.fail(check, f"无法识别的图类型：{first[:40]!r}")

    report.expect(
        check, "FLOWCHART「本文档当前 N 张图」", _grab(flowchart, r"本文档当前 (\d+) 张图"), len(blocks)
    )

    claim = re.search(r"`flowchart-v2` × (\d+)、`stateDiagram` × (\d+)、`sequence` × (\d+)", flowchart)
    if claim is None:
        report.fail(check, "FLOWCHART 里找不到图类型数量声明（`flowchart-v2` × N、…）")
    else:
        report.expect(check, "flowchart 图数量", int(claim.group(1)), kinds["flowchart"])
        report.expect(check, "stateDiagram 图数量", int(claim.group(2)), kinds["stateDiagram"])
        report.expect(check, "sequence 图数量", int(claim.group(3)), kinds["sequence"])

    parts = re.split(r"(?m)^## 图 (\d+)[^\n]*$", flowchart)
    titled = [int(number) for number, body in zip(parts[1::2], parts[2::2]) if "```mermaid" in body]
    if len(titled) != len(blocks):
        report.fail(check, f"有 {len(blocks)} 个 mermaid 代码块，但只有 {len(titled)} 个「## 图 N」章节含图")
    if titled != sorted(titled):
        report.fail(check, f"「## 图 N」章节顺序不对：{titled}")

    report.finish(check, f"{len(blocks)} 张；" + ", ".join(f"{k}×{v}" for k, v in kinds.items()))


# --------------------------------------------------------------------------- 检查：行号引用

def check_line_references(docs: Dict[str, str], report: Report) -> None:
    check = "line_references"
    checked = 0
    for rel, text in docs.items():
        for lineno, line in enumerate(text.splitlines(), 1):
            names = list(re.finditer(r"([\w./]+\.py)", line))
            for ref in re.finditer(r"\bL(\d+)(?:-(\d+))?", line):
                before = [match for match in names if match.end() <= ref.start()]
                if not before:
                    report.note(f"{rel}:{lineno} 的 {ref.group(0)} 前没写文件名，跳过")
                    continue
                name = before[-1].group(1)
                path = ROOT / name if "/" in name else _source(name)
                if path is None or not path.is_file():
                    report.fail(check, f"{rel}:{lineno} 引用的文件不存在或同名不唯一：{name}")
                    continue
                total = len(path.read_text(encoding="utf-8").splitlines())
                start = int(ref.group(1))
                end = int(ref.group(2) or ref.group(1))
                if start < 1 or end < start:
                    report.fail(check, f"{rel}:{lineno} 行号区间非法：{ref.group(0)}")
                    continue
                if end > total:
                    report.fail(check, f"{rel}:{lineno} 引用 {name} {ref.group(0)}，但该文件只有 {total} 行")
                    continue
                checked += 1
    report.finish(check, f"核验 {checked} 处行号引用")


# --------------------------------------------------------------------------- 检查：语义锚点

def check_anchors(docs: Dict[str, str], report: Report) -> None:
    check = "anchors"
    for rel, start, end, needles in LINE_ANCHORS:
        window = "\n".join(_read(rel).splitlines()[start - 1 : end])
        for needle in needles:
            if needle not in window:
                report.fail(
                    check,
                    f"文档引用 {rel} L{start}-{end} 应当包含 {needle!r}，实际没有（代码移动了？请同步文档）",
                )
    for rel, symbol in SYMBOL_ANCHORS:
        if symbol not in _read(rel):
            report.fail(check, f"映射表承诺的 {rel}::{symbol} 不存在")
    report.finish(check, f"{len(LINE_ANCHORS)} 处行号语义 + {len(SYMBOL_ANCHORS)} 个符号")


# --------------------------------------------------------------------------- 检查：文档引用的文件

def check_referenced_files(docs: Dict[str, str], report: Report) -> None:
    check = "referenced_files"
    pattern = re.compile(r"[\w.][\w./]*\.(?:py|mjs|md|json|csv|txt|example)\b")
    seen: Set[str] = set()
    for rel, text in docs.items():
        for token in pattern.findall(text):
            if token in seen or token in ALLOW_MISSING:
                continue
            seen.add(token)
            if (ROOT / token).is_file() or ("/" not in token and _source(token) is not None):
                continue
            report.fail(check, f"文档引用的文件不存在：{token}（出现在 {rel}）")

    cell = re.search(r"reprojourney/llm/`?（([^）]+)）", docs["docs/FLOWCHART.md"])
    if cell is None:
        report.note("FLOWCHART 映射表里找不到 reprojourney/llm/ 的模块清单，跳过")
    else:
        for module in (part.strip().strip("`") for part in cell.group(1).split("/")):
            if module and not (ROOT / "reprojourney" / "llm" / f"{module}.py").is_file():
                report.fail(check, f"映射表列出的模块 reprojourney/llm/{module}.py 不存在")

    report.finish(check, f"核验 {len(seen)} 个路径引用")


# --------------------------------------------------------------------------- 检查：卫生（绝对路径 / 秘钥）

def check_hygiene(docs: Dict[str, str], report: Report) -> None:
    check = "hygiene"
    targets: Dict[str, str] = dict(docs)
    for generated in ("*.py", "reprojourney/**/*.py", "tests/*.py", "evaluation/*.py"):
        for path in ROOT.glob(generated):
            if path.is_file() and not _excluded(path):
                targets[str(path.relative_to(ROOT)).replace("\\", "/")] = path.read_text(encoding="utf-8")

    for rel, text in targets.items():
        # (?<!\w) 避免把 https:// 里的 p:/ 当成 C:\ 之类的盘符
        for match in re.finditer(r"(?<![\w/])[A-Za-z]:[\\/]|/Users/|/home/", text):
            report.fail(check, f"{rel} 出现绝对路径：{text[match.start():match.start() + 40]!r}")
        for secret in SECRET_PATTERNS:
            for match in re.finditer(secret, text):
                report.fail(check, f"{rel} 疑似硬编码秘钥：{match.group(0)[:8]}…")

    report.finish(check, f"扫描 {len(targets)} 个文件")


CHECKS: Tuple[Callable[[Dict[str, str], Report], None], ...] = (
    check_test_counts,
    check_tool_whitelist,
    check_risk_rule_ids,
    check_governance_rule_ids,
    check_thresholds,
    check_constants,
    check_diagrams,
    check_line_references,
    check_anchors,
    check_referenced_files,
    check_hygiene,
)


def main(argv: Sequence[str]) -> int:
    extra = [name for name in argv[1:] if name not in DEFAULT_DOCS]
    targets = list(DEFAULT_DOCS) + extra
    try:
        docs = {rel: _read(rel) for rel in targets}
    except SystemExit as exc:
        print(exc)
        return 2

    report = Report()
    for check in CHECKS:
        check(docs, report)

    if report.notes:
        print("\n提示（未参与判定）：")
        for note in report.notes:
            print(f"  - {note}")

    failed = report.failed
    print(f"\nchecked {len(CHECKS)} aspect(s) across {len(docs)} doc(s): failures={len(failed)}")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    # 中文输出在 Windows 终端可能是 GBK，显式切到 UTF-8，避免 UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
