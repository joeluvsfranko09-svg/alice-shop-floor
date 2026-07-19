"""
Final Inspection Result - V4: Final Garment Inspector
Gate: Final QC -> Pack requires a passing result.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime


class FinalInspectionResult(frappe.model.document.Document):

    def apply_cognex_result(self, cognex_payload: dict):
        """
        Receive normalised Cognex payload from FinalInspectorEngine and
        evaluate pass/fail against configured thresholds.
        """
        if cognex_payload.get("error"):
            self.overall_result = "Error"
            self.error_message  = cognex_payload["error"]
            self.save(ignore_permissions=True)
            return

        self.cognex_job_id = cognex_payload.get("job_id") or ""
        self.inspected_at  = now_datetime()

        # Populate defect map
        self.defect_map = []
        for idx, d in enumerate(cognex_payload.get("defects") or [], start=1):
            self.append("defect_map", {
                "defect_index":    idx,
                "defect_type":     d.get("defect_type") or "Other",
                "severity":        d.get("severity") or "Minor",
                "garment_zone":    d.get("garment_zone") or "Other",
                "x_mm":            d.get("x") or 0,
                "y_mm":            d.get("y") or 0,
                "width_mm":        d.get("width") or 0,
                "height_mm":       d.get("height") or 0,
                "confidence_score": d.get("confidence") or 0,
                "image_ref":       d.get("image_ref") or "",
            })

        self._recalculate_counts()
        self._evaluate_pass_fail()
        self.save(ignore_permissions=True)

        if self.overall_result == "Fail":
            frappe.publish_realtime(
                event="final_inspection_failed",
                message={
                    "result_name": self.name,
                    "work_order":  self.work_order,
                    "fail_reason": self.fail_reason,
                    "critical":    self.defect_count_critical,
                    "major":       self.defect_count_major,
                    "minor":       self.defect_count_minor,
                },
                room="shop_floor_supervisors",
            )

    def supervisor_force_pass(self, notes=None):
        frappe.only_for(["Manufacturing Manager", "System Manager"])
        self.overall_result     = "Pass"
        self.supervisor_override = 1
        self.overridden_by      = frappe.session.user
        self.overridden_at      = now_datetime()
        self.override_notes     = notes or ""
        self.save(ignore_permissions=True)

    def _recalculate_counts(self):
        minor = major = critical = 0
        for row in self.defect_map:
            sev = str(row.severity or "").lower()
            if sev == "critical":
                critical += 1
            elif sev == "major":
                major += 1
            else:
                minor += 1
        self.defect_count_minor    = minor
        self.defect_count_major    = major
        self.defect_count_critical = critical

    def _evaluate_pass_fail(self):
        try:
            config = frappe.get_single("Final Inspection Config")
        except Exception:
            self.overall_result = "Pass"
            return

        reasons = []
        if config.fail_on_any_critical and self.defect_count_critical > 0:
            reasons.append(
                f"{self.defect_count_critical} critical defect(s) found"
            )
        max_major = int(config.max_major_defects or 1)
        if self.defect_count_major > max_major:
            reasons.append(
                f"Major defects ({self.defect_count_major}) exceed limit ({max_major})"
            )
        max_minor = int(config.max_minor_defects or 3)
        if self.defect_count_minor > max_minor:
            reasons.append(
                f"Minor defects ({self.defect_count_minor}) exceed limit ({max_minor})"
            )

        if reasons:
            self.overall_result = "Fail"
            self.fail_reason    = "; ".join(reasons)
        else:
            self.overall_result = "Pass"
