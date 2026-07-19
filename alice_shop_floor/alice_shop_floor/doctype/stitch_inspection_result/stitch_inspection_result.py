import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DEFECT_TYPE_BUCKET = {
    "Skipped Stitch": "skipped",
    "Broken Stitch":  "broken",
    "Loose Stitch":   "loose",
    "Uneven Tension": "loose",
    "Pucker":         "loose",
    "Other":          "loose",
}


class StitchInspectionResult(Document):

    def validate(self):
        self._recalculate_counts()

    def before_save(self):
        if self.supervisor_override and not self.overridden_by:
            self.overridden_by = frappe.session.user
            self.overridden_at = now_datetime()

    def apply_cognex_result(self, cognex_payload: dict):
        self.cognex_job_id = cognex_payload.get("job_id", "")
        if cognex_payload.get("error"):
            self.overall_result = "Error"
            self.error_message  = cognex_payload["error"]
            self.save(ignore_permissions=True)
            frappe.db.commit()
            return

        self.defect_map = []
        config = frappe.get_single("Stitch Inspection Config")

        for idx, d in enumerate(cognex_payload.get("defects", []), start=1):
            self.append("defect_map", {
                "defect_index":         idx,
                "defect_type":          d.get("defect_type", "Other"),
                "severity":             d.get("severity", "Minor").capitalize(),
                "seam_location":        d.get("seam_location") or "",
                "x_mm":                 d.get("x_mm") or 0,
                "y_mm":                 d.get("y_mm") or 0,
                "stitch_count_affected": d.get("stitch_count_affected") or 1,
                "confidence_score":     d.get("confidence") or 0,
                "image_ref":            d.get("image_ref") or "",
            })

        self._recalculate_counts()
        self.inspected_at = now_datetime()

        result, reason = _evaluate_pass_fail(
            self.skipped_stitches,
            self.broken_stitches,
            self.loose_stitches,
            self.critical_defect_count,
            config,
        )
        self.overall_result = result
        self.fail_reason    = reason

        self.save(ignore_permissions=True)
        frappe.db.commit()

        if result == "Fail":
            frappe.publish_realtime(
                event="stitch_inspection_failed",
                message={
                    "name":        self.name,
                    "work_order":  self.work_order,
                    "fail_reason": reason,
                    "critical":    self.critical_defect_count,
                    "skipped":     self.skipped_stitches,
                    "broken":      self.broken_stitches,
                },
                room="shop_floor_supervisors",
            )

    def supervisor_force_pass(self, notes=None):
        frappe.only_for(["Manufacturing Manager", "System Manager"])
        if self.overall_result not in ("Fail", "Error"):
            frappe.throw(_("Only Failed or Errored inspections can be overridden."))
        self.supervisor_override = 1
        self.override_notes      = notes or ""
        self.overridden_by       = frappe.session.user
        self.overridden_at       = now_datetime()
        self.overall_result      = "Pass"
        self.save(ignore_permissions=True)
        frappe.db.commit()

    def _recalculate_counts(self):
        skipped = broken = loose = critical = total = 0
        for row in self.defect_map:
            bucket = DEFECT_TYPE_BUCKET.get(row.defect_type or "Other", "loose")
            if bucket == "skipped":
                skipped += 1
            elif bucket == "broken":
                broken += 1
            else:
                loose += 1
            if (row.severity or "").lower() == "critical":
                critical += 1
            total += 1
        self.skipped_stitches    = skipped
        self.broken_stitches     = broken
        self.loose_stitches      = loose
        self.critical_defect_count = critical
        self.total_defect_count  = total


def _evaluate_pass_fail(skipped, broken, loose, critical, config):
    if config.fail_on_any_critical and critical > 0:
        return "Fail", "Critical stitch defect detected ({} found).".format(critical)
    max_skipped = int(config.max_skipped_stitches or 0)
    if skipped > max_skipped:
        return "Fail", "Skipped stitches ({}) exceed threshold ({}).".format(skipped, max_skipped)
    max_broken = int(config.max_broken_stitches or 0)
    if broken > max_broken:
        return "Fail", "Broken stitches ({}) exceed threshold ({}).".format(broken, max_broken)
    max_loose = int(config.max_loose_stitches or 3)
    if loose > max_loose:
        return "Fail", "Loose/uneven stitches ({}) exceed threshold ({}).".format(loose, max_loose)
    return "Pass", ""
