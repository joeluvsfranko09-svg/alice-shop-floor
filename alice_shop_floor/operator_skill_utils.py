"""
operator_skill_utils.py -- Module 6: Operator Efficiency & Skill AI
Computes per-(operator, stage) skill profiles using a 90-day rolling
window of completed garment data from PassportOperatorRecord,
GarmentQCCheck, and QCDefect.

Skill score: 60% quality + 40% speed  (0-100, higher = better)
Trend:       Compare last 3 ISO-week history entries
             Improving  -> score up >= 3 pts over the span
             Declining  -> score down >= 3 pts
             Stable     -> within +-3 pts
Training flag auto-set when:
             skill_score < TRAINING_THRESHOLD (default 60)  OR
             defect_rate_per_100 > DEFECT_SPIKE_THRESHOLD (default 10)
"""

import frappe
from frappe.utils import now_datetime
from datetime import datetime, timedelta, timezone

WINDOW_DAYS          = 90
MIN_PIECES           = 3
QUALITY_WEIGHT       = 0.60
SPEED_WEIGHT         = 0.40
TRAINING_THRESHOLD   = 60
DEFECT_SPIKE_PER_100 = 10.0


class OperatorSkillEngine:

    def __init__(self):
        self._target_cache = {}

    def update_all_profiles(self):
        window_start = _window_start()
        pairs = self._get_active_pairs(window_start)
        updated = flagged = skipped = 0
        for operator, stage in pairs:
            result = self._compute_and_save(operator, stage, window_start)
            if result == "flagged":
                updated += 1
                flagged += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1
        return {
            "pairs_found": len(pairs),
            "updated": updated,
            "flagged_for_training": flagged,
            "skipped_insufficient_data": skipped,
            "window_days": WINDOW_DAYS,
            "computed_at": str(now_datetime()),
        }

    def _compute_and_save(self, operator, stage, window_start):
        metrics = self._compute_metrics(operator, stage, window_start)
        if metrics["pieces"] < MIN_PIECES:
            return "skipped"
        skill_score = _clamp(_round(
            QUALITY_WEIGHT * metrics["quality_score"] +
            SPEED_WEIGHT   * metrics["speed_score"]
        ))
        training_flag = (
            skill_score < TRAINING_THRESHOLD or
            metrics["defect_rate_per_100"] > DEFECT_SPIKE_PER_100
        )
        existing_name = frappe.db.exists(
            "Operator Skill Profile", {"operator": operator, "stage": stage}
        )
        if existing_name:
            doc = frappe.get_doc("Operator Skill Profile", existing_name)
        else:
            doc = frappe.new_doc("Operator Skill Profile")
            doc.operator = operator
            doc.stage = stage
        trend = self._detect_trend(doc, skill_score)
        doc.skill_score          = skill_score
        doc.quality_score        = _round(metrics["quality_score"])
        doc.speed_score          = _round(metrics["speed_score"])
        doc.qc_pass_rate_pct     = _round(metrics["qc_pass_rate_pct"])
        doc.defect_rate_per_100  = _round(metrics["defect_rate_per_100"])
        doc.pieces_lifetime      = metrics["pieces_lifetime"]
        doc.periods_active       = metrics["periods_active"]
        doc.last_updated         = now_datetime()
        doc.trend                = trend
        if training_flag:
            doc.training_flag = 1
        _append_history_row(doc, skill_score, metrics)
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return "flagged" if training_flag else "updated"

    def _compute_metrics(self, operator, stage, window_start):
        ws = window_start.strftime("%Y-%m-%d %H:%M:%S")
        speed_rows = frappe.db.sql("""
            SELECT por.garment, por.minutes_in_stage, por.completed_at
            FROM `tabPassport Operator Record` por
            INNER JOIN `tabGarment Passport` gp ON gp.name = por.parent
            WHERE por.operator = %(operator)s
              AND por.stage = %(stage)s
              AND por.completed_at >= %(ws)s
              AND gp.is_sealed = 1
        """, {"operator": operator, "stage": stage, "ws": ws}, as_dict=True)
        pieces = len(speed_rows)
        if pieces == 0:
            return _empty_metrics()
        target_min = self._get_target_minutes(stage)
        speed_scores = []
        for row in speed_rows:
            if row.minutes_in_stage and row.minutes_in_stage > 0 and target_min:
                ratio = target_min / row.minutes_in_stage
                speed_scores.append(min(ratio * 100, 100))
            else:
                speed_scores.append(50.0)
        speed_score = sum(speed_scores) / len(speed_scores)
        qc_row = frappe.db.sql("""
            SELECT
                SUM(CASE WHEN qc.overall_result = 'Pass' THEN 1 ELSE 0 END) AS qc_pass,
                SUM(CASE WHEN qc.overall_result = 'Fail' THEN 1 ELSE 0 END) AS qc_fail
            FROM `tabGarment QC Check` qc
            INNER JOIN `tabPassport Operator Record` por
                ON por.garment = qc.garment
               AND por.operator = %(operator)s
               AND por.stage = %(stage)s
               AND por.completed_at >= %(ws)s
            INNER JOIN `tabGarment Passport` gp ON gp.name = por.parent AND gp.is_sealed = 1
            WHERE qc.checked_at >= %(ws)s
        """, {"operator": operator, "stage": stage, "ws": ws}, as_dict=True)
        qc_pass = int(qc_row[0].qc_pass or 0) if qc_row else 0
        qc_fail = int(qc_row[0].qc_fail or 0) if qc_row else 0
        total_qc = qc_pass + qc_fail
        qc_pass_rate = (qc_pass / total_qc * 100) if total_qc else 100.0
        quality_score = qc_pass_rate
        defect_row = frappe.db.sql("""
            SELECT COUNT(*) AS defect_count
            FROM `tabQC Defect` qd
            INNER JOIN `tabGarment QC Check` qc ON qc.name = qd.parent
            INNER JOIN `tabPassport Operator Record` por
                ON por.garment = qc.garment
               AND por.operator = %(operator)s
               AND por.stage = %(stage)s
               AND por.completed_at >= %(ws)s
            INNER JOIN `tabGarment Passport` gp ON gp.name = por.parent AND gp.is_sealed = 1
            WHERE qc.checked_at >= %(ws)s
        """, {"operator": operator, "stage": stage, "ws": ws}, as_dict=True)
        defect_count = int(defect_row[0].defect_count or 0) if defect_row else 0
        defect_rate_per_100 = (defect_count / pieces * 100) if pieces else 0.0
        lifetime_row = frappe.db.sql("""
            SELECT COUNT(*) AS cnt
            FROM `tabPassport Operator Record` por
            INNER JOIN `tabGarment Passport` gp ON gp.name = por.parent
            WHERE por.operator = %(operator)s AND por.stage = %(stage)s AND gp.is_sealed = 1
        """, {"operator": operator, "stage": stage}, as_dict=True)
        pieces_lifetime = int(lifetime_row[0].cnt or 0) if lifetime_row else pieces
        periods_row = frappe.db.sql("""
            SELECT COUNT(DISTINCT YEARWEEK(por.completed_at, 1)) AS cnt
            FROM `tabPassport Operator Record` por
            INNER JOIN `tabGarment Passport` gp ON gp.name = por.parent
            WHERE por.operator = %(operator)s AND por.stage = %(stage)s AND gp.is_sealed = 1
        """, {"operator": operator, "stage": stage}, as_dict=True)
        periods_active = int(periods_row[0].cnt or 0) if periods_row else 1
        return {
            "pieces": pieces,
            "speed_score": speed_score,
            "quality_score": quality_score,
            "qc_pass_rate_pct": qc_pass_rate,
            "defect_rate_per_100": defect_rate_per_100,
            "pieces_lifetime": pieces_lifetime,
            "periods_active": periods_active,
        }

    def _get_active_pairs(self, window_start):
        ws = window_start.strftime("%Y-%m-%d %H:%M:%S")
        rows = frappe.db.sql("""
            SELECT DISTINCT por.operator, por.stage
            FROM `tabPassport Operator Record` por
            INNER JOIN `tabGarment Passport` gp ON gp.name = por.parent
            WHERE por.completed_at >= %(ws)s
              AND gp.is_sealed = 1
              AND por.operator IS NOT NULL
              AND por.stage IS NOT NULL
        """, {"ws": ws}, as_dict=True)
        return [(r.operator, r.stage) for r in rows]

    def _get_target_minutes(self, stage):
        if stage not in self._target_cache:
            row = frappe.db.get_value("Stage Throughput Target", {"stage": stage}, "target_minutes")
            self._target_cache[stage] = float(row) if row else None
        return self._target_cache[stage]

    def _detect_trend(self, doc, current_score):
        history = getattr(doc, "history", [])
        if len(history) < 2:
            return "Stable"
        last_two = sorted(history, key=lambda r: r.period_label)[-2:]
        oldest_score = float(last_two[0].skill_score or 0)
        delta = current_score - oldest_score
        if delta >= 3:
            return "Improving"
        if delta <= -3:
            return "Declining"
        return "Stable"


def update_all_skill_profiles():
    return OperatorSkillEngine().update_all_profiles()


def get_skill_profile(operator, stage):
    name = frappe.db.exists("Operator Skill Profile", {"operator": operator, "stage": stage})
    if not name:
        return {}
    return frappe.get_doc("Operator Skill Profile", name).as_dict()


def get_skill_leaderboard(stage=None, limit=20):
    filters = {}
    if stage:
        filters["stage"] = stage
    return frappe.get_all(
        "Operator Skill Profile",
        filters=filters,
        fields=["operator", "stage", "skill_score", "trend",
                "qc_pass_rate_pct", "defect_rate_per_100", "pieces_lifetime", "training_flag"],
        order_by="skill_score desc",
        limit=limit,
    )


def get_training_flags():
    return frappe.get_all(
        "Operator Skill Profile",
        filters={"training_flag": 1},
        fields=["operator", "stage", "skill_score", "trend",
                "qc_pass_rate_pct", "defect_rate_per_100", "training_notes", "last_updated"],
        order_by="skill_score asc",
    )


def get_performance_trend(operator, stage):
    name = frappe.db.exists("Operator Skill Profile", {"operator": operator, "stage": stage})
    if not name:
        return {"operator": operator, "stage": stage, "history": []}
    doc = frappe.get_doc("Operator Skill Profile", name)
    history = sorted(
        [h.as_dict() for h in doc.history],
        key=lambda r: r.get("period_label", ""),
    )[-12:]
    return {
        "operator": operator,
        "stage": stage,
        "skill_score": doc.skill_score,
        "trend": doc.trend,
        "quality_score": doc.quality_score,
        "speed_score": doc.speed_score,
        "qc_pass_rate_pct": doc.qc_pass_rate_pct,
        "defect_rate_per_100": doc.defect_rate_per_100,
        "training_flag": doc.training_flag,
        "last_updated": str(doc.last_updated),
        "history": history,
    }


def _window_start():
    return datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)


def _append_history_row(doc, skill_score, metrics):
    period_label = _iso_week_label()
    for row in doc.history:
        if row.period_label == period_label:
            row.skill_score         = skill_score
            row.quality_score       = _round(metrics["quality_score"])
            row.speed_score         = _round(metrics["speed_score"])
            row.pieces_in_period    = metrics["pieces"]
            row.qc_pass_rate_pct    = _round(metrics["qc_pass_rate_pct"])
            row.defect_rate_per_100 = _round(metrics["defect_rate_per_100"])
            return
    doc.append("history", {
        "period_label":        period_label,
        "skill_score":         skill_score,
        "quality_score":       _round(metrics["quality_score"]),
        "speed_score":         _round(metrics["speed_score"]),
        "pieces_in_period":    metrics["pieces"],
        "qc_pass_rate_pct":    _round(metrics["qc_pass_rate_pct"]),
        "defect_rate_per_100": _round(metrics["defect_rate_per_100"]),
    })


def _iso_week_label():
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return "{}-W{:02d}".format(iso[0], iso[1])


def _round(v):
    return round(float(v), 2)


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _empty_metrics():
    return {
        "pieces": 0,
        "speed_score": 0.0,
        "quality_score": 0.0,
        "qc_pass_rate_pct": 0.0,
        "defect_rate_per_100": 0.0,
        "pieces_lifetime": 0,
        "periods_active": 0,
    }
