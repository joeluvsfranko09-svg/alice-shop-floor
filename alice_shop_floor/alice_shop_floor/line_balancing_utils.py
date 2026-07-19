"""
Module 2: Line Balancing AI — Frappe-native engine
ZAZFIT / Athlettia LLC

POD line balancing: we do NOT balance by queue depth.
In POD every WO is a unique custom garment — many orders at the same stage
simultaneously is completely normal. We balance by THROUGHPUT TIME:
how long orders are spending at each stage vs. the configured target.

Algorithm:
  1. Snapshot current floor: per-stage avg and max time in stage
  2. Compare to StageThroughputTarget — classify OK / Warning / Critical
  3. Identify bottleneck stage (worst variance over target)
  4. Score operators available to move (skill at bottleneck, available at donor stage)
  5. Persist LineBalanceSnapshot + RebalancingRecommendation(s)
  6. Publish realtime alert if Critical

Operator skill scoring uses historical data from:
  - PassportOperatorRecord (throughput speed)
  - GarmentQCCheck (quality at each stage)
  Combined into a skill_score 0-100 per (operator, stage) pair.
"""

import frappe
from frappe.utils import now_datetime
from collections import defaultdict


STAGE_ORDER = [
    "Fabric Inspection", "Cutting", "Bundling",
    "Sewing", "Final QC", "Pack",
]

# Default targets (minutes) used when no StageThroughputTarget is configured
DEFAULT_TARGETS = {
    "Fabric Inspection": 30,
    "Cutting":           45,
    "Bundling":          20,
    "Sewing":            90,
    "Final QC":          25,
    "Pack":              15,
}


class LineBalancingEngine:

    def __init__(self):
        self.targets = self._load_targets()

    # ------------------------------------------------------------------
    # Main entry: take a snapshot and generate recommendations
    # ------------------------------------------------------------------

    def run(self) -> str:
        """
        Take a floor snapshot, compute balance, generate recommendations.
        Returns the LineBalanceSnapshot doc name.
        """
        readings = self._compute_stage_readings()
        overall, bottleneck = self._classify_overall(readings)

        snapshot = frappe.get_doc({
            "doctype": "Line Balance Snapshot",
            "snapshot_at": now_datetime(),
            "overall_status": overall,
            "active_orders_total": sum(r["active_orders"] for r in readings.values()),
            "bottleneck_stage": bottleneck or "",
        })
        for stage in STAGE_ORDER:
            if stage not in readings:
                snapshot.append("stage_readings", {
                    "stage": stage,
                    "active_orders": 0,
                    "avg_minutes_in_stage": 0,
                    "target_minutes": self.targets.get(stage, {}).get("target", 0),
                    "variance_pct": 0,
                    "status": "No Data",
                })
            else:
                r = readings[stage]
                snapshot.append("stage_readings", {
                    "stage": stage,
                    "active_orders": r["active_orders"],
                    "avg_minutes_in_stage": round(r["avg_minutes"], 1),
                    "target_minutes": r["target"],
                    "variance_pct": round(r["variance_pct"], 1),
                    "status": r["status"],
                })

        snapshot.insert(ignore_permissions=True)

        rec_count = 0
        if bottleneck:
            rec_count = self._generate_recommendations(snapshot.name, bottleneck, readings)

        snapshot.recommendation_count = rec_count
        snapshot.save(ignore_permissions=True)
        frappe.db.commit()

        if overall in ("Warning", "Critical"):
            frappe.publish_realtime(
                event="line_balance_alert",
                message={
                    "snapshot": snapshot.name,
                    "overall_status": overall,
                    "bottleneck_stage": bottleneck,
                    "recommendation_count": rec_count,
                },
                room="shop_floor_supervisors",
            )
            frappe.logger().warning(
                "ALICE LBA: Floor status {} — bottleneck at '{}'. "
                "{} recommendation(s) generated.".format(
                    overall, bottleneck, rec_count
                )
            )
        else:
            frappe.logger().info(
                "ALICE LBA: Floor balanced — {} active orders across all stages.".format(
                    snapshot.active_orders_total
                )
            )

        return snapshot.name

    def get_current_balance(self) -> dict:
        """
        Return live floor state without persisting a snapshot.
        Used by the dashboard for real-time polling.
        """
        readings = self._compute_stage_readings()
        overall, bottleneck = self._classify_overall(readings)
        return {
            "overall_status": overall,
            "bottleneck_stage": bottleneck,
            "active_orders_total": sum(r["active_orders"] for r in readings.values()),
            "stages": [
                {
                    "stage": stage,
                    "active_orders": readings.get(stage, {}).get("active_orders", 0),
                    "avg_minutes": round(readings.get(stage, {}).get("avg_minutes", 0), 1),
                    "target_minutes": self.targets.get(stage, {}).get("target", DEFAULT_TARGETS.get(stage, 0)),
                    "variance_pct": round(readings.get(stage, {}).get("variance_pct", 0), 1),
                    "status": readings.get(stage, {}).get("status", "No Data"),
                }
                for stage in STAGE_ORDER
            ],
        }

    # ------------------------------------------------------------------
    # Stage reading computation
    # ------------------------------------------------------------------

    def _compute_stage_readings(self) -> dict:
        """
        Query active trackers, compute per-stage throughput.
        Returns {stage: {active_orders, avg_minutes, target, variance_pct, status}}
        """
        rows = frappe.db.sql(
            """
            SELECT
                current_stage,
                COUNT(*) AS active_orders,
                AVG(TIMESTAMPDIFF(MINUTE, stage_entered_at, NOW())) AS avg_minutes,
                MAX(TIMESTAMPDIFF(MINUTE, stage_entered_at, NOW())) AS max_minutes
            FROM `tabProduction Stage Tracker`
            WHERE is_complete = 0
              AND stage_entered_at IS NOT NULL
            GROUP BY current_stage
            """,
            as_dict=True,
        )

        readings = {}
        for row in rows:
            stage = row.current_stage
            cfg = self.targets.get(stage, {})
            target = cfg.get("target") or DEFAULT_TARGETS.get(stage, 60)
            warn_pct = cfg.get("warn_pct", 50)
            crit_pct = cfg.get("crit_pct", 100)
            avg = float(row.avg_minutes or 0)
            variance_pct = ((avg - target) / target * 100) if target else 0

            if avg <= 0 or row.active_orders == 0:
                status = "No Data"
            elif variance_pct >= crit_pct:
                status = "Critical"
            elif variance_pct >= warn_pct:
                status = "Warning"
            else:
                status = "OK"

            readings[stage] = {
                "active_orders": int(row.active_orders),
                "avg_minutes": avg,
                "max_minutes": float(row.max_minutes or 0),
                "target": target,
                "warn_pct": warn_pct,
                "crit_pct": crit_pct,
                "variance_pct": variance_pct,
                "status": status,
            }

        return readings

    def _classify_overall(self, readings: dict):
        """Return (overall_status, bottleneck_stage)."""
        if not readings:
            return "Balanced", None

        worst_status = "Balanced"
        worst_variance = -999
        bottleneck = None

        for stage, r in readings.items():
            if r["status"] == "Critical" and worst_status != "Critical":
                worst_status = "Critical"
            elif r["status"] == "Warning" and worst_status == "Balanced":
                worst_status = "Warning"

            if r["variance_pct"] > worst_variance and r["active_orders"] > 0:
                worst_variance = r["variance_pct"]
                bottleneck = stage

        if worst_status == "Balanced":
            bottleneck = None

        return worst_status, bottleneck

    # ------------------------------------------------------------------
    # Recommendation generation
    # ------------------------------------------------------------------

    def _generate_recommendations(self, snapshot_name: str,
                                   bottleneck: str, readings: dict) -> int:
        """
        Find the best operator to move to the bottleneck stage.
        Scores candidates by: skill at bottleneck × availability at donor stage.
        Returns number of recommendations created.
        """
        # Expire pending recommendations from previous snapshots
        frappe.db.sql(
            """
            UPDATE `tabRebalancing Recommendation`
            SET status = 'Expired'
            WHERE status = 'Pending'
              AND snapshot != %(snap)s
            """,
            {"snap": snapshot_name},
        )

        # Find donor stages — OK stages with active operators
        donor_candidates = [
            stage for stage, r in readings.items()
            if stage != bottleneck and r["status"] == "OK" and r["active_orders"] > 0
        ]
        if not donor_candidates:
            return 0

        operator_skills = self._score_operators(bottleneck)
        if not operator_skills:
            # No skill data yet — make a generic recommendation without operator suggestion
            rec = frappe.get_doc({
                "doctype": "Rebalancing Recommendation",
                "snapshot": snapshot_name,
                "bottleneck_stage": bottleneck,
                "donor_stage": donor_candidates[0],
                "suggested_operator": None,
                "confidence_score": 0,
                "status": "Pending",
                "reason": (
                    "Stage '{}' is running {:.0f}% over target throughput time. "
                    "Consider moving an available operator from '{}'. "
                    "No historical skill data yet to suggest a specific operator.".format(
                        bottleneck,
                        readings[bottleneck]["variance_pct"],
                        donor_candidates[0],
                    )
                ),
            })
            rec.insert(ignore_permissions=True)
            frappe.db.commit()
            return 1

        # Pick the best-skilled operator for the bottleneck who is currently at a donor stage
        current_assignments = self._get_current_operator_assignments()
        best = None
        best_score = -1
        best_donor = None

        for op, skill_score in sorted(operator_skills.items(), key=lambda x: -x[1]):
            assigned_stage = current_assignments.get(op)
            if assigned_stage in donor_candidates:
                if skill_score > best_score:
                    best = op
                    best_score = skill_score
                    best_donor = assigned_stage
                    break

        if not best:
            # No operator found at a donor stage — still recommend moving someone
            best = next(iter(operator_skills))
            best_score = operator_skills[best]
            best_donor = donor_candidates[0]

        variance = readings[bottleneck]["variance_pct"]
        confidence = min(round(best_score, 0), 100)

        reason = (
            "Stage '{}' is {:.0f}% over target throughput time ({:.0f} min avg vs {:.0f} min target). "
            "Operator {} has a skill score of {:.0f}/100 at this stage based on historical throughput "
            "and QC pass rate. Current assignment: '{}'. "
            "Moving them should reduce the bottleneck.".format(
                bottleneck,
                variance,
                readings[bottleneck]["avg_minutes"],
                readings[bottleneck]["target"],
                best,
                best_score,
                best_donor or "unknown",
            )
        )

        rec = frappe.get_doc({
            "doctype": "Rebalancing Recommendation",
            "snapshot": snapshot_name,
            "bottleneck_stage": bottleneck,
            "donor_stage": best_donor or "",
            "suggested_operator": best,
            "confidence_score": confidence,
            "status": "Pending",
            "reason": reason,
        })
        rec.insert(ignore_permissions=True)
        frappe.db.commit()
        return 1

    # ------------------------------------------------------------------
    # Operator skill scoring
    # ------------------------------------------------------------------

    def _score_operators(self, stage: str) -> dict:
        """
        Score each operator's skill at a given stage.
        Score = 0.6 × quality_score + 0.4 × speed_score, scaled 0-100.

        Quality score: QC pass rate for garments they worked on at this stage.
        Speed score: 1 / avg_minutes_per_piece relative to the stage target.
        """
        qc_stage_map = {
            "Fabric Inspection": "Fabric Inspection",
            "Cutting": "Post-Cutting",
            "Bundling": None,
            "Sewing": "Post-Sewing",
            "Final QC": "Final QC",
            "Pack": None,
        }
        qc_stage = qc_stage_map.get(stage)
        target_minutes = self.targets.get(stage, {}).get("target") or DEFAULT_TARGETS.get(stage, 60)

        # Pull recent operator touches at this stage (last 90 days)
        touches = frappe.db.sql(
            """
            SELECT por.operator,
                   COUNT(*) AS pieces,
                   AVG(TIMESTAMPDIFF(MINUTE,
                       (SELECT MIN(por2.touched_at)
                        FROM `tabPassport Operator Record` por2
                        WHERE por2.parent = por.parent
                          AND por2.stage = por.stage),
                       por.touched_at
                   )) AS avg_min_per_piece
            FROM `tabPassport Operator Record` por
            JOIN `tabGarment Passport` gp ON gp.name = por.parent
            WHERE por.stage = %(stage)s
              AND gp.is_sealed = 1
              AND gp.sealed_at >= DATE_SUB(NOW(), INTERVAL 90 DAY)
              AND por.operator IS NOT NULL
            GROUP BY por.operator
            HAVING pieces >= 3
            """,
            {"stage": stage},
            as_dict=True,
        )

        if not touches:
            return {}

        # Pull QC pass rates for each operator at this stage
        qc_scores = {}
        if qc_stage:
            qc_rows = frappe.db.sql(
                """
                SELECT por.operator,
                       SUM(CASE WHEN qc.result = 'Pass' THEN 1 ELSE 0 END) AS passes,
                       COUNT(*) AS total
                FROM `tabPassport Operator Record` por
                JOIN `tabGarment Passport` gp ON gp.name = por.parent
                JOIN `tabGarment QC Check` qc ON qc.work_order = gp.work_order
                WHERE por.stage = %(stage)s
                  AND qc.qc_stage = %(qc_stage)s
                  AND gp.is_sealed = 1
                  AND gp.sealed_at >= DATE_SUB(NOW(), INTERVAL 90 DAY)
                  AND por.operator IS NOT NULL
                GROUP BY por.operator
                """,
                {"stage": stage, "qc_stage": qc_stage},
                as_dict=True,
            )
            for row in qc_rows:
                qc_scores[row.operator] = (
                    (row.passes / row.total * 100) if row.total else 50
                )

        scores = {}
        for row in touches:
            op = row.operator
            avg_min = float(row.avg_min_per_piece or target_minutes)

            # Speed score: faster than target = higher score, capped at 100
            speed_score = min((target_minutes / max(avg_min, 1)) * 100, 100)
            # Quality score: default 50 if no QC data for this stage
            quality_score = qc_scores.get(op, 50)

            scores[op] = round(0.6 * quality_score + 0.4 * speed_score, 1)

        return dict(sorted(scores.items(), key=lambda x: -x[1]))

    def _get_current_operator_assignments(self) -> dict:
        """
        Return {operator: current_stage} for all operators active in the last 2 hours.
        Uses PassportOperatorRecord to infer where each operator is currently working.
        """
        rows = frappe.db.sql(
            """
            SELECT operator, stage, MAX(touched_at) AS last_touch
            FROM `tabPassport Operator Record`
            WHERE touched_at >= DATE_SUB(NOW(), INTERVAL 2 HOUR)
              AND operator IS NOT NULL
            GROUP BY operator
            ORDER BY last_touch DESC
            """,
            as_dict=True,
        )
        assignments = {}
        for row in rows:
            if row.operator not in assignments:
                assignments[row.operator] = row.stage
        return assignments

    # ------------------------------------------------------------------
    # Configuration loader
    # ------------------------------------------------------------------

    def _load_targets(self) -> dict:
        targets = {}
        for r in frappe.get_all(
            "Stage Throughput Target",
            fields=["stage", "target_minutes", "warning_threshold_pct", "critical_threshold_pct"],
        ):
            targets[r.stage] = {
                "target": r.target_minutes,
                "warn_pct": r.warning_threshold_pct or 50,
                "crit_pct": r.critical_threshold_pct or 100,
            }
        return targets
