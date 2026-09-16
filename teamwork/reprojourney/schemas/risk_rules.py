from __future__ import annotations

from typing import Any, Dict, List

from .base_schemas import SharedUserState

# ---------------------------------------------------------------------------
# Vocabulary shared by the rule engine, the fact-recording tool and the
# offline policy model. One source of truth keeps the "agent vocabulary" and
# the "rule vocabulary" from drifting apart.
# ---------------------------------------------------------------------------

HIGH_SYMPTOMS = {
    "阴道出血",
    "破水",
    "规律性宫缩",
    "剧烈腹痛",
    "头晕晕厥",
    "视物模糊",
}

MODERATE_SYMPTOMS = {"轻微腰酸", "假性宫缩", "轻度水肿", "轻微恶心"}

# Abdominal / pelvic pain needs assessment at any gestational week.
ABDOMINAL_PAIN_SYMPTOMS = {"腹痛", "腹部坠胀"}

PRETERM_SYMPTOMS = {"腹痛", "腹部坠胀", "规律宫缩"}

HIGH_HISTORY_ITEMS = {"子痫前期", "瘢痕子宫", "既往早产史", "妊娠期糖尿病"}

HIGH_REPORT_MARKERS = {
    "bp_high",
    "glucose_high",
    "proteinuria",
    "fetal_heart_abnormal",
    "amniotic_fluid_abnormal",
}

MODERATE_REPORT_MARKERS = {
    "hb_low",
    "platelet_low",
    "tsh_abnormal",
    "gbs_positive",
    "bmi_high",
}

# Phrase -> structured marker, used to turn free text into structured report
# facts deterministically (no LLM required).
REPORT_PHRASE_MARKERS: Dict[str, str] = {
    "血压高": "bp_high",
    "血压偏高": "bp_high",
    "血糖高": "glucose_high",
    "血糖偏高": "glucose_high",
    "蛋白尿": "proteinuria",
    "胎心异常": "fetal_heart_abnormal",
    "羊水异常": "amniotic_fluid_abnormal",
    "血红蛋白低": "hb_low",
    "贫血": "hb_low",
    "血小板低": "platelet_low",
    "甲状腺异常": "tsh_abnormal",
    "gbs阳性": "gbs_positive",
    "bmi高": "bmi_high",
}

SYMPTOM_SEVERITY: Dict[str, str] = {}
for _name in HIGH_SYMPTOMS:
    SYMPTOM_SEVERITY[_name] = "high"
for _name in MODERATE_SYMPTOMS | ABDOMINAL_PAIN_SYMPTOMS:
    SYMPTOM_SEVERITY[_name] = "moderate"

# Follow-up cadence: more than this many days overdue is a care-gap signal.
FOLLOW_UP_OVERDUE_DAYS = 14

_RULE_RANK = {"UNKNOWN": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3}


class MaternityRiskRuleEngine:
    """Deterministic maternity risk engine (authoritative decision maker).

    The LLM may explain or summarise the finding, but it must never decide the
    tier. Every branch carries a stable ``rule_id`` so decisions are
    reproducible and quotable as audit evidence.
    """

    # -- rule catalogue ----------------------------------------------------
    R_OVERDUE_PREGNANCY = "R-GA-01"  # 过期妊娠
    R_PRETERM_WITH_SIGNS = "R-GA-02"  # 早产征象
    R_PRETERM_NO_SIGNS = "R-GA-03"  # 早产期无征象
    R_GA_DATA_QUALITY = "R-GA-04"  # 孕周数据异常
    R_INFO_MISSING = "R-INFO-01"  # 信息不足
    R_SYMPTOM_HIGH = "R-SY-01"
    R_SYMPTOM_MODERATE = "R-SY-02"
    R_SYMPTOM_ABDOMINAL_PAIN = "R-SY-03"
    R_SYMPTOM_UNRECOGNISED = "R-SY-04"
    R_REPORT_HIGH = "R-RP-01"
    R_REPORT_MODERATE = "R-RP-02"
    R_REPORT_UNKNOWN_MARKER = "R-RP-03"
    R_REPORT_NO_DATA = "R-RP-04"
    R_HISTORY_HIGH = "R-HX-01"
    R_FOLLOW_UP_OVERDUE = "R-FU-01"

    HIGH_SYMPTOMS = HIGH_SYMPTOMS
    MODERATE_SYMPTOMS = MODERATE_SYMPTOMS
    ABDOMINAL_PAIN_SYMPTOMS = ABDOMINAL_PAIN_SYMPTOMS
    PRETERM_SYMPTOMS = PRETERM_SYMPTOMS
    HIGH_HISTORY_ITEMS = HIGH_HISTORY_ITEMS
    HIGH_REPORT_MARKERS = HIGH_REPORT_MARKERS
    MODERATE_REPORT_MARKERS = MODERATE_REPORT_MARKERS
    REPORT_PHRASE_MARKERS = REPORT_PHRASE_MARKERS
    SYMPTOM_SEVERITY = SYMPTOM_SEVERITY

    def evaluate(self, state: SharedUserState) -> Dict[str, Any]:
        risk_level = "UNKNOWN"
        reasons: List[str] = []
        rule_ids: List[str] = []
        needs_info: List[str] = []
        data_gaps: List[str] = []

        def escalate(level: str, rule_id: str, reason: str) -> None:
            nonlocal risk_level
            if _RULE_RANK[level] > _RULE_RANK[risk_level]:
                risk_level = level
            reasons.append(reason)
            rule_ids.append(rule_id)

        self._evaluate_pregnancy(state, escalate, needs_info, data_gaps)
        self._evaluate_symptoms(state, escalate, needs_info)
        self._evaluate_reports(state, escalate, needs_info, data_gaps)
        self._evaluate_history(state, escalate)
        self._evaluate_follow_up(state, escalate)

        # Safety rule: a data-quality gap must not be reported as a confident
        # LOW. Rule ids/evidence stay untouched, only the tier is capped.
        if data_gaps and _RULE_RANK[risk_level] <= _RULE_RANK["LOW"]:
            risk_level = "UNKNOWN"
            reasons.append("存在无法解析的数据，风险等级不作 LOW 结论")
            rule_ids.append(self.R_INFO_MISSING)

        # Deterministic safety rule: HIGH always requires a human reviewer.
        requires_human = risk_level == "HIGH"

        if not reasons:
            risk_level = "UNKNOWN"
            reasons.append("缺少足够的结构化信息，无法做确定性风险判断")
            rule_ids.append(self.R_INFO_MISSING)

        return {
            "risk_level": risk_level,
            "reasons": list(dict.fromkeys(reasons)),
            "rule_ids": list(dict.fromkeys(rule_ids)),
            "requires_human": requires_human,
            "needs_info": list(dict.fromkeys(needs_info)),
            "data_gaps": list(dict.fromkeys(data_gaps)),
        }

    # -- rule groups -------------------------------------------------------
    def _evaluate_pregnancy(
        self, state: SharedUserState, escalate, needs_info: List[str], data_gaps: List[str]
    ) -> None:
        pregnancy = state.pregnancy
        if not pregnancy or pregnancy.gestational_week is None:
            needs_info.append("缺少孕周信息")
            return

        gestational_week = pregnancy.gestational_week
        if gestational_week <= 0 or gestational_week > 45:
            escalate(
                "UNKNOWN",
                self.R_GA_DATA_QUALITY,
                f"孕周数据异常：{gestational_week}，未参与分级",
            )
            needs_info.append(f"孕周数据超出合理范围（{gestational_week}），需人工核对")
            data_gaps.append(f"孕周数据异常：{gestational_week}")
            return

        if gestational_week > 42:
            escalate("HIGH", self.R_OVERDUE_PREGNANCY, "过期妊娠：孕周大于42周")
            return

        if gestational_week < 37:
            preterm_signs = {(symptom.symptom or "") for symptom in state.symptoms} & self.PRETERM_SYMPTOMS
            if preterm_signs:
                escalate("MODERATE", self.R_PRETERM_WITH_SIGNS, "孕周小于37周，且伴随早产相关症状")
            else:
                escalate("LOW", self.R_PRETERM_NO_SIGNS, "孕周小于37周，但未出现早产相关症状")

    def _evaluate_symptoms(self, state: SharedUserState, escalate, needs_info: List[str]) -> None:
        for symptom in state.symptoms or []:
            name = symptom.symptom or ""
            if not name:
                needs_info.append("存在缺少名称的症状记录")
                continue
            if name in self.HIGH_SYMPTOMS:
                escalate("HIGH", self.R_SYMPTOM_HIGH, f"存在高危症状：{name}")
            elif name in self.ABDOMINAL_PAIN_SYMPTOMS:
                escalate("MODERATE", self.R_SYMPTOM_ABDOMINAL_PAIN, f"存在需评估的腹痛症状：{name}")
            elif name in self.MODERATE_SYMPTOMS:
                escalate("MODERATE", self.R_SYMPTOM_MODERATE, f"存在需要观察的症状：{name}")
            else:
                # Unrecognised free text never drives the tier (prompt-injection
                # safe) but is surfaced for human confirmation.
                escalate(
                    "UNKNOWN",
                    self.R_SYMPTOM_UNRECOGNISED,
                    f"无法识别的症状描述（未参与分级）：{name}",
                )
                needs_info.append(f"无法识别的症状描述，需人工确认：{name}")

    def _evaluate_reports(
        self, state: SharedUserState, escalate, needs_info: List[str], data_gaps: List[str]
    ) -> None:
        for report in state.reports or []:
            extracted = report.extracted_data
            if not extracted:
                escalate(
                    "UNKNOWN",
                    self.R_REPORT_NO_DATA,
                    f"报告缺失结构化数据（未参与分级）：{report.report_id or '(未知编号)'}",
                )
                needs_info.append(f"报告缺少结构化解析结果：{report.report_id or '(未知编号)'}")
                data_gaps.append(f"报告缺少结构化解析结果：{report.report_id or '(未知编号)'}")
                continue
            for marker, value in extracted.items():
                if value is not True:
                    continue
                if marker in self.HIGH_REPORT_MARKERS:
                    escalate("HIGH", self.R_REPORT_HIGH, f"报告异常：{marker}")
                elif marker in self.MODERATE_REPORT_MARKERS:
                    escalate("MODERATE", self.R_REPORT_MODERATE, f"报告需关注指标：{marker}")
                else:
                    # Conservative default for unrecognised abnormal markers.
                    escalate(
                        "MODERATE",
                        self.R_REPORT_UNKNOWN_MARKER,
                        f"存在未识别的报告异常标记，需人工确认：{marker}",
                    )

    def _evaluate_history(self, state: SharedUserState, escalate) -> None:
        for item in state.medical_history or []:
            if item in self.HIGH_HISTORY_ITEMS:
                escalate("MODERATE", self.R_HISTORY_HIGH, f"存在高危病史：{item}")

    def _evaluate_follow_up(self, state: SharedUserState, escalate) -> None:
        follow_up = state.follow_up or {}
        overdue_days = follow_up.get("overdue_days") or follow_up.get("follow_up_overdue_days")
        if isinstance(overdue_days, (int, float)) and overdue_days > FOLLOW_UP_OVERDUE_DAYS:
            escalate("MODERATE", self.R_FOLLOW_UP_OVERDUE, f"产检随访超期：{int(overdue_days)}天")
