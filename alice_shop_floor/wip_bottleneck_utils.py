"""
wip_bottleneck_utils.py -- Module 10: Predictive WIP Bottleneck Detector
=========================================================================
Every 30 minutes: snapshot live WIP queue depth at each stage, score
congestion against StageThoughputTarget, detect bottlenecks, and fire
realtime alerts with AI root-cause and rebalancing recommendations.

Congestion score = wip_count / target_wip_count (ratio).
Bottleneck threshold: congestion_score >= 1.5 (50% over target).
"""

import frappe
from frappe.utils import now_datetime


STAGE_ORDER = [
    "Fabric Inspection",
    "Cutting",
    "Bundling",
    "Sewing",
    "Final QC",
    "Pack",
]

BOTTLENECK_THRESHOLD = 1.5   # 50% above target WIP triggers alert
DEFAULT_TARGET_WIP   = 5     # fallback if no StageThoughputTarget configured


class WIPBottleneckEngine:

    def run_snapshot(self) -> dict:
        """
        Query live WIP counts per stage, score each, detect bottlenecks,
        save WIPSnapshot, fire BottleneckAlert docs and realtime events.
        Returns snapshot summary.
        """
        now = now_datetime()
        counts = self._get_wip_counts()
        targets = self._get_targets()

        stage_data = {}
        for stage in STAGE_ORDER:
            wip    = counts.get(stage, 0)
            target = targets.get(stage, DEFAULT_TARGET_WIP)
            score  = round(wip / target, 3) if target > 0 else 0
            stage_data[stage] = {"wip": wip, "target": target, "score": score}

        # Bottleneck: stage with highest score above threshold
        bottleneck_stage = None
        max_score = 0.0
        for stage, data in stage_data.items():
            if data["score"] > max_score:
                max_score = data["score"]
                bottleneck_stage = stage

        alert_fired = (bottleneck_stage is not None and max_score >= BOTTLENECK_THRESHOLD)

        # Save WIPSnapshot
        snap = frappe.new_doc("WIP Snapshot")
        snap.snapshot_at             = now
        snap.fabric_inspection_count = counts.get("Fabric Inspection", 0)
        snap.cutting_count           = counts.get("Cutting", 0)
        snap.bundling_count          = counts.get("Bundling", 0)
        snap.sewing_count            = counts.get("Sewing", 0)
        snap.final_qc_count          = counts.get("Final QC", 0)
        snap.pack_count              = counts.get("Pack", 0)
        snap.bottleneck_stage        = bottleneck_stage or ""
        snap.congestion_score        = max_score
        snap.alert_fired             = 1 if alert_fired else 0
        snap.insert(ignore_permissions=True)

        if alert_fired:
            self._fire_bottleneck_alert(bottleneck_stage, stage_data, max_score, snap.name)

        frappe.db.commit()

        return {
            "snapshot": snap.name,
            "snapshot_at": str(now),
            "stage_data": stage_data,
            "bottleneck_stage": bottleneck_stage,
            "congestion_score": max_score,
            "alert_fired": alert_fired,
        }

    def get_current_wip(self) -> dict:
        """Return current WIP counts per stage (no snapshot saved)."""
        counts  = self._get_wip_counts()
        targets = self._get_targets()
        result  = {}
        for stage in STAGE_ORDER:
            wip    = counts.get(stage, 0)
            target = targets.get(stage, DEFAULT_TARGET_WIP)
            result[stage] = {
                "wip":    wip,
                "target": target,
                "score":  round(wip / target, 3) if target > 0 else 0,
            }
        return result

    def get_open_alerts(self) -> list:
        return frappe.get_all(
            "Bottleneck Alert",
            filters={"is_resolved": 0},
            fields=["name", "stage", "detected_at", "wip_count", "target_wip",
                    "congestion_score", "root_cause", "recommendation"],
            order_by="detected_at desc",
        )

    def resolve_alert(self, alert_name: str) -> dict:
        doc = frappe.get_doc("Bottleneck Alert", alert_name)
        doc.is_resolved = 1
        doc.resolved_at = now_datetime()
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {"name": alert_name, "resolved_at": str(doc.resolved_at)}

    def get_snapshots(self, limit: int = 48) -> list:
        return frappe.get_all(
            "WIP Snapshot",
            fields=["name", "snapshot_at", "fabric_inspection_count",
                    "cutting_count", "bundling_count", "sewing_count",
                    "final_qc_count", "pack_count", "bottleneck_stage",
                    "congestion_score", "alert_fired"],
            order_by="snapshot_at desc",
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_wip_counts(self) -> dict:
        """Count active (non-complete) Production Stage Trackers per stage."""
        rows = frappe.db.sql(
            """
            SELECT current_stage, COUNT(*) AS cnt
            FROM `tabProduction Stage Tracker`
            WHERE is_complete = 0 OR is_complete IS NULL
            GROUP BY current_stage
            """,
            as_dict=True,
        )
        return {r.current_stage: int(r.cnt) for r in rows}

    def _get_targets(self) -> dict:
        """Load target WIP per stage from StageThoughputTarget."""
        rows = frappe.get_all(
            "Stage Throughput Target",
            fields=["stage", "target_units_per_hour"],
        )
        # target_wip ≈ target_units_per_hour / 2  (30-min cadence)
        result = {}
        for r in rows:
            if r.stage and r.target_units_per_hour:
                result[r.stage] = max(1, int(float(r.target_units_per_hour) / 2))
        return result

    def _fire_bottleneck_alert(self, stage: str, stage_data: dict,
                                score: float, snapshot_name: str):
        wip_count  = stage_data[stage]["wip"]
        target_wip = stage_data[stage]["target"]
        root_cause = self._infer_root_cause(stage, stage_data)
        recommendation = self._build_recommendation(stage, root_cause, wip_count, target_wip)

        # Avoid duplicate open alerts for same stage
        existing = frappe.db.exists("Bottleneck Alert",
                                    {"stage": stage, "is_resolved": 0})
        if existing:
            # Update congestion score on existing alert
            frappe.db.set_value("Bottleneck Alert", existing, {
                "congestion_score": score,
                "wip_count":        wip_count,
                "root_cause":       root_cause,
                "recommendation":   recommendation,
            })
            alert_name = existing
        else:
            alert = frappe.new_doc("Bottleneck Alert")
            alert.stage           = stage
            alert.detected_at     = now_datetime()
            alert.wip_count       = wip_count
            alert.target_wip      = target_wip
            alert.congestion_score = score
            alert.root_cause      = root_cause
            alert.recommendation  = recommendation
            alert.snapshot        = snapshot_name
            alert.insert(ignore_permissions=True)
            alert_name = alert.name

        frappe.publish_realtime(
            event="bottleneck_alert",
            message={
                "alert_name":       alert_name,
                "stage":            stage,
                "wip_count":        wip_count,
                "target_wip":       target_wip,
                "congestion_score": score,
                "root_cause":       root_cause,
                "recommendation":   recommendation,
            },
            room="shop_floor_supervisors",
        )

    def _infer_root_cause(self, stage: str, stage_data: dict) -> str:
        """
        Simple heuristic:
        - Next stage score also high → Downstream Block
        - Previous stage score also high → Upstream Surge
        - Check for recent open downtime events at this stage → Machine Downtime
        - Default → Capacity Gap
        """
        idx = STAGE_ORDER.index(stage)

        # Check downstream block
        if idx < len(STAGE_ORDER) - 1:
            next_stage = STAGE_ORDER[idx + 1]
            if stage_data.get(next_stage, {}).get("score", 0) >= BOTTLENECK_THRESHOLD:
                return "Downstream Block"

        # Check upstream surge
        if idx > 0:
            prev_stage = STAGE_ORDER[idx - 1]
            if stage_data.get(prev_stage, {}).get("score", 0) >= BOTTLENECK_THRESHOLD:
                return "Upstream Surge"

        # Check for open downtime events at this stage
        open_downtime = frappe.db.count(
            "Downtime Event",
            filters={"stage": stage, "ended_at": ("is", "not set")},
        )
        if open_downtime > 0:
            return "Machine Downtime"

        return "Capacity Gap"

    @staticmethod
    def _build_recommendation(stage: str, root_cause: str,
                               wip: int, target: int) -> str:
        excess = wip - target
        recs = {
            "Capacity Gap":     (
                f"Add capacity at {stage}: cross-train operators, add a shift, "
                f"or move {excess} WO(s) to overflow. Review throughput targets."
            ),
            "Downstream Block": (
                f"Downstream stage is saturated — hold intake to {stage} "
                f"until downstream clears. Do not push more WOs forward."
            ),
            "Upstream Surge":   (
                f"Upstream stage is over-producing. Pace upstream or "
                f"temporarily increase {stage} capacity by {excess} unit(s)."
            ),
            "Operator Shortage":(
                f"Assign additional operators to {stage}. "
                f"Check attendance and cross-training availability."
            ),
            "Machine Downtime": (
                f"Active downtime at {stage} causing WIP buildup. "
                f"Escalate to maintenance. Consider diverting {excess} WO(s)."
            ),
            "Unknown": (
                f"Investigate {stage} immediately — {wip} WOs vs target {target}. "
                "Log root cause in Downtime module."
            ),
        }
        return recs.get(root_cause, recs["Unknown"])


# Module-level wrappers
def run_wip_bottleneck_snapshot():
    return WIPBottleneckEngine().run_snapshot()

def get_current_wip():
    return WIPBottleneckEngine().get_current_wip()

def get_open_bottleneck_alerts():
    return WIPBottleneckEngine().get_open_alerts()
