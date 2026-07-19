"""
Garment QC Check — Module 4: Inline QC Tracker

Records the quality inspection result for a single custom garment
at a specific production stage.

ZAZFIT is pure Print-on-Demand — every garment is unique. Traditional
DHU (Defects per Hundred Units) batch sampling does not apply.
Instead, every garment gets its own QC check at each checkpoint:

  Fabric Inspection  → before cutting (Visual Module 1)
  Post-Cutting       → after cut panels off the table (Visual Module 3)
  Post-Sewing        → after critical sewing operations (Visual Module 2)
  Final QC           → complete garment before Pack (Visual Module 4)

Results feed:
  - Operator quality scoring (Module 6 — Operator Efficiency AI)
  - Pattern defect trend detection (flags .val files that produce repeat defects)
  - Garment Passport QR (Module 8 — all QC results sealed into the hangtag)
  - Defect Intelligence aggregation (Visual Module 5)
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

# Severity scores for weighted defect scoring
SEVERITY_WEIGHT = {"Minor": 1, "Major": 2, "Critical": 3}

# Auto-fail thresholds — any single Critical defect, or total score >= this
CRITICAL_AUTO_FAIL_THRESHOLD = 1   # number of Critical defects
SCORE_AUTO_FAIL_THRESHOLD = 5      # total weighted defect score


class GarmentQcCheck(Document):

    def before_insert(self):
        self.checked_at = self.checked_at or now_datetime()
        self._carry_pattern_ref()

    def after_insert(self):
        self._publish_result()
        frappe.logger().info(
            f"ALICE QC: {self.result} — WO {self.work_order} at {self.qc_stage} "
            f"[{self.trigger_source}] by {self.checked_by}. "
            f"Defects: {len(self.defects or [])}"
        )

    # ------------------------------------------------------------------
    # Public methods — called by api.py and alice_core
    # ------------------------------------------------------------------

    def get_defect_score(self):
        """
        Weighted defect score for this check.
        Minor=1, Major=2, Critical=3.
        Used by ALICE to decide pass/fail and rework priority.
        """
        return sum(SEVERITY_WEIGHT.get(d.severity, 1) for d in (self.defects or []))

    def get_critical_count(self):
        """Number of Critical-severity defects in this check."""
        return sum(1 for d in (self.defects or []) if d.severity == "Critical")

    def is_auto_fail(self):
        """
        Returns True if defect profile triggers an automatic fail:
          - Any Critical defect, OR
          - Total weighted score >= SCORE_AUTO_FAIL_THRESHOLD
        """
        return (
            self.get_critical_count() >= CRITICAL_AUTO_FAIL_THRESHOLD
            or self.get_defect_score() >= SCORE_AUTO_FAIL_THRESHOLD
        )

    def get_defect_summary(self):
        """
        Returns a dict summarising defects by type and severity.
        Used by ALICE for pattern/operator trend analysis.
        """
        summary = {}
        for d in (self.defects or []):
            key = d.defect_type
            if key not in summary:
                summary[key] = {"Minor": 0, "Major": 0, "Critical": 0, "total": 0}
            summary[key][d.severity] += 1
            summary[key]["total"] += 1
        return summary

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _carry_pattern_ref(self):
        """Pull the pattern_file_ref from the linked tracker for traceability."""
        if self.tracker and not self.pattern_ref:
            try:
                self.pattern_ref = frappe.db.get_value(
                    "Production Stage Tracker", self.tracker, "pattern_file_ref"
                )
            except Exception:
                pass

    def _publish_result(self):
        """Push real-time event to the shop floor dashboard."""
        event_data = {
            "qc_check": self.name,
            "work_order": self.work_order,
            "tracker": self.tracker,
            "qc_stage": self.qc_stage,
            "result": self.result,
            "defect_score": self.get_defect_score(),
            "critical_count": self.get_critical_count(),
            "trigger_source": self.trigger_source,
            "checked_by": self.checked_by,
        }

        frappe.publish_realtime(
            event="qc_result",
            message=event_data,
            room="shop_floor",
        )

        # Escalate failures immediately to the supervisor room
        if self.result in ("Fail", "Rework Required"):
            frappe.publish_realtime(
                event="qc_failure",
                message=event_data,
                room="shop_floor_supervisors",
            )
