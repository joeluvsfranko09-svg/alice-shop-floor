"""
Incentive Pay Engine — Frappe-native implementation.
Runs inside ERPNext with direct DB access.
Called from api.py endpoints; alice_core calls those via REST.

See alice_core/modules/incentive_pay.py for the REST client counterpart.
"""

import frappe
from datetime import date, timedelta
from collections import defaultdict


STAGE_TO_QC_STAGE = {
    "Fabric Inspection": "Fabric Inspection",
    "Cutting":           "Post-Cutting",
    "Bundling":          None,
    "Sewing":            "Post-Sewing",
    "Final QC":          "Final QC",
    "Pack":              None,
}


class IncentivePayEngineERPNext:

    def __init__(self, period_start: date, period_end: date):
        self.period_start = period_start
        self.period_end = period_end
        self.period_label = self._make_label(period_start)

    @classmethod
    def for_week(cls, any_day: date) -> "IncentivePayEngineERPNext":
        monday = any_day - timedelta(days=any_day.weekday())
        sunday = monday + timedelta(days=6)
        return cls(monday, sunday)

    # ------------------------------------------------------------------

    def calculate_period(self) -> list:
        rules = self._load_rules()
        touches = self._fetch_operator_touches()
        qc_map = self._fetch_qc_results()
        defect_map = self._fetch_defect_counts()

        by_op_stage = defaultdict(lambda: defaultdict(list))
        for row in touches:
            by_op_stage[row.operator][row.stage].append(row)

        names = []
        for operator, stage_data in by_op_stage.items():
            doc = self._get_or_create_summary(operator)
            doc.stage_earnings = []
            for stage, touch_rows in stage_data.items():
                earn = self._compute_stage_earn(
                    operator, stage, touch_rows, qc_map, defect_map, rules.get(stage)
                )
                doc.append("stage_earnings", earn)
            doc.recalculate_totals()
            doc.save(ignore_permissions=True)
            frappe.db.commit()
            names.append(doc.name)
            frappe.logger().info(
                "ALICE: Incentive calculated — {} {} total=${:.2f}".format(
                    operator, self.period_label, doc.total_pay
                )
            )
        return names

    def finalize_period(self, closed_by=None) -> int:
        self.calculate_period()
        summaries = frappe.get_all(
            "Operator Pay Period Summary",
            filters={"period_label": self.period_label, "is_finalized": 0},
            pluck="name",
        )
        user = closed_by or frappe.session.user
        for name in summaries:
            frappe.get_doc("Operator Pay Period Summary", name).finalize(finalized_by=user)
        frappe.logger().info(
            "ALICE: Period {} finalized — {} summaries by {}.".format(
                self.period_label, len(summaries), user
            )
        )
        return len(summaries)

    def get_period_preview(self) -> list:
        rules = self._load_rules()
        touches = self._fetch_operator_touches()
        qc_map = self._fetch_qc_results()
        defect_map = self._fetch_defect_counts()

        by_op_stage = defaultdict(lambda: defaultdict(list))
        for row in touches:
            by_op_stage[row.operator][row.stage].append(row)

        previews = []
        for operator, stage_data in by_op_stage.items():
            base = quality = speed = penalty = pieces = 0
            for stage, touch_rows in stage_data.items():
                earn = self._compute_stage_earn(
                    operator, stage, touch_rows, qc_map, defect_map, rules.get(stage)
                )
                base += earn["base_pay"]
                quality += earn["quality_bonus"]
                speed += earn["speed_bonus"]
                penalty += earn["defect_penalty"]
                pieces += earn["pieces_touched"]
            previews.append({
                "operator": operator,
                "period": self.period_label,
                "pieces": pieces,
                "base_pay": round(base, 2),
                "quality_bonus": round(quality, 2),
                "speed_bonus": round(speed, 2),
                "defect_penalty": round(penalty, 2),
                "total_pay": round(base + quality + speed - penalty, 2),
            })
        previews.sort(key=lambda x: x["total_pay"], reverse=True)
        return previews

    # ------------------------------------------------------------------

    def _load_rules(self) -> dict:
        rules = {}
        for r in frappe.get_all("Incentive Pay Rule", filters={"is_active": 1}, pluck="name"):
            doc = frappe.get_doc("Incentive Pay Rule", r)
            rules[doc.stage] = doc
        return rules

    def _fetch_operator_touches(self) -> list:
        return frappe.db.sql(
            """
            SELECT por.operator, por.stage, por.touched_at, gp.work_order
            FROM `tabPassport Operator Record` por
            JOIN `tabGarment Passport` gp ON gp.name = por.parent
            WHERE gp.is_sealed = 1
              AND DATE(gp.sealed_at) BETWEEN %(start)s AND %(end)s
              AND por.operator IS NOT NULL
            ORDER BY por.operator, por.stage, por.touched_at
            """,
            {"start": self.period_start, "end": self.period_end},
            as_dict=True,
        )

    def _fetch_qc_results(self) -> dict:
        rows = frappe.db.sql(
            """
            SELECT qc.work_order, qc.qc_stage, qc.result
            FROM `tabGarment QC Check` qc
            JOIN `tabGarment Passport` gp ON gp.work_order = qc.work_order
            WHERE gp.is_sealed = 1
              AND DATE(gp.sealed_at) BETWEEN %(start)s AND %(end)s
            """,
            {"start": self.period_start, "end": self.period_end},
            as_dict=True,
        )
        result_map = defaultdict(dict)
        for r in rows:
            result_map[r.work_order][r.qc_stage] = r.result
        return result_map

    def _fetch_defect_counts(self) -> dict:
        rows = frappe.db.sql(
            """
            SELECT qc.work_order, qc.qc_stage, def.severity, COUNT(*) AS cnt
            FROM `tabQC Defect` def
            JOIN `tabGarment QC Check` qc ON qc.name = def.parent
            JOIN `tabGarment Passport` gp ON gp.work_order = qc.work_order
            WHERE gp.is_sealed = 1
              AND DATE(gp.sealed_at) BETWEEN %(start)s AND %(end)s
            GROUP BY qc.work_order, qc.qc_stage, def.severity
            """,
            {"start": self.period_start, "end": self.period_end},
            as_dict=True,
        )
        defect_map = defaultdict(lambda: {"Minor": 0, "Major": 0, "Critical": 0})
        for r in rows:
            defect_map[(r.work_order, r.qc_stage)][r.severity] += r.cnt
        return defect_map

    def _compute_stage_earn(self, operator, stage, touch_rows,
                             qc_map, defect_map, rule) -> dict:
        pieces = len(touch_rows)
        hours = self._estimate_hours(touch_rows)
        qc_stage = STAGE_TO_QC_STAGE.get(stage)

        qc_pass = qc_fail = minor = major = critical = 0
        for row in touch_rows:
            wo_qc = qc_map.get(row.work_order, {})
            if qc_stage and qc_stage in wo_qc:
                if wo_qc[qc_stage] == "Pass":
                    qc_pass += 1
                else:
                    qc_fail += 1
            if qc_stage:
                d = defect_map.get((row.work_order, qc_stage), {})
                minor += d.get("Minor", 0)
                major += d.get("Major", 0)
                critical += d.get("Critical", 0)

        avg_min = round((hours * 60) / pieces, 2) if pieces else 0

        if not rule:
            frappe.logger().warning(
                "ALICE IPE: No active pay rule for stage '{}' — {} earns $0 for {} pieces".format(
                    stage, operator, pieces
                )
            )
            return {
                "stage": stage, "pieces_touched": pieces,
                "avg_minutes_per_piece": avg_min,
                "qc_pass": qc_pass, "qc_fail": qc_fail,
                "defect_minor": minor, "defect_major": major,
                "defect_critical": critical,
                "base_pay": 0.0, "quality_bonus": 0.0,
                "speed_bonus": 0.0, "defect_penalty": 0.0, "stage_total": 0.0,
            }

        base_pay = pieces * (rule.base_rate_per_piece or 0)

        quality_bonus = 0.0
        total_qc = qc_pass + qc_fail
        if total_qc > 0:
            if (qc_pass / total_qc) * 100 >= (rule.quality_bonus_threshold_pct or 95):
                quality_bonus = pieces * (rule.quality_bonus_per_piece or 0)

        speed_bonus = 0.0
        if hours > 0 and rule.speed_bonus_threshold_pph:
            if (pieces / hours) >= rule.speed_bonus_threshold_pph:
                speed_bonus = pieces * (rule.speed_bonus_per_piece or 0)

        defect_penalty = (
            minor * (rule.defect_minor_penalty or 0)
            + major * (rule.defect_major_penalty or 0)
            + critical * (rule.defect_critical_penalty or 0)
        )
        defect_penalty = min(defect_penalty, base_pay)  # never exceeds base

        return {
            "stage": stage, "pieces_touched": pieces,
            "avg_minutes_per_piece": avg_min,
            "qc_pass": qc_pass, "qc_fail": qc_fail,
            "defect_minor": minor, "defect_major": major,
            "defect_critical": critical,
            "base_pay": round(base_pay, 2),
            "quality_bonus": round(quality_bonus, 2),
            "speed_bonus": round(speed_bonus, 2),
            "defect_penalty": round(defect_penalty, 2),
            "stage_total": round(
                base_pay + quality_bonus + speed_bonus - defect_penalty, 2
            ),
        }

    @staticmethod
    def _estimate_hours(touch_rows: list) -> float:
        if len(touch_rows) <= 1:
            return len(touch_rows) * 0.5
        times = sorted(r.touched_at for r in touch_rows if r.touched_at)
        if len(times) < 2:
            return len(touch_rows) * 0.5
        span = (times[-1] - times[0]).total_seconds() / 3600
        return max(span, len(touch_rows) * 0.25)

    def _get_or_create_summary(self, operator: str):
        existing = frappe.db.exists(
            "Operator Pay Period Summary",
            {"operator": operator, "period_label": self.period_label, "is_finalized": 0},
        )
        if existing:
            return frappe.get_doc("Operator Pay Period Summary", existing)
        doc = frappe.get_doc({
            "doctype": "Operator Pay Period Summary",
            "operator": operator,
            "period_label": self.period_label,
            "pay_period_start": str(self.period_start),
            "pay_period_end": str(self.period_end),
        })
        doc.insert(ignore_permissions=True)
        return doc

    @staticmethod
    def _make_label(d: date) -> str:
        return "{}-W{:02d}".format(d.isocalendar()[0], d.isocalendar()[1])
