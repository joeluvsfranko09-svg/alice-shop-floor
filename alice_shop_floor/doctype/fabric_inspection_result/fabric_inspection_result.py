import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class FabricInspectionResult(Document):

    def validate(self):
        """Recalculate defect counts from child table rows."""
        self._recalculate_counts()

    def before_save(self):
        """If supervisor override is toggled on, record who did it."""
        if self.supervisor_override and not self.overridden_by:
            self.overridden_by = frappe.session.user
            self.overridden_at = now_datetime()

    def apply_cognex_result(self, cognex_payload: dict):
        """
        Populate this document from a raw Cognex In-Sight REST result payload.
        Expected keys: job_id, pass_fail, defects (list), fabric_width_mm,
                       fabric_length_mm, error (optional)
        """
        self.cognex_job_id     = cognex_payload.get("job_id", "")
        self.fabric_width_mm   = cognex_payload.get("fabric_width_mm") or 0
        self.fabric_length_mm  = cognex_payload.get("fabric_length_mm") or 0

        if cognex_payload.get("error"):
            self.overall_result = "Error"
            self.error_message  = cognex_payload["error"]
            self.save(ignore_permissions=True)
            frappe.db.commit()
            return

        # ── populate defect map ───────────────────────────────────────
        self.defect_map = []
        config = frappe.get_single("Fabric Inspection Config")
        min_area = float(config.min_defect_area_mm2 or 0)

        for idx, d in enumerate(cognex_payload.get("defects", []), start=1):
            area = float(d.get("area_mm2") or
                         (d.get("width_mm", 0) * d.get("height_mm", 0)))
            if area < min_area:
                continue
            self.append("defect_map", {
                "defect_index":    idx,
                "severity":        d.get("severity", "Minor").capitalize(),
                "defect_type":     d.get("defect_type", "Unknown"),
                "x_mm":            d.get("x_mm") or 0,
                "y_mm":            d.get("y_mm") or 0,
                "width_mm":        d.get("width_mm") or 0,
                "height_mm":       d.get("height_mm") or 0,
                "area_mm2":        area,
                "confidence_score": d.get("confidence") or 0,
                "image_ref":       d.get("image_ref") or "",
            })

        self._recalculate_counts()
        self.inspected_at = now_datetime()

        # ── pass / fail decision ──────────────────────────────────────
        result, reason = _evaluate_pass_fail(
            self.defect_count_minor,
            self.defect_count_major,
            self.defect_count_critical,
            config,
        )
        self.overall_result = result
        self.fail_reason    = reason

        self.save(ignore_permissions=True)
        frappe.db.commit()

        # Notify supervisors of failures
        if result == "Fail":
            frappe.publish_realtime(
                event="fabric_inspection_failed",
                message={
                    "name":        self.name,
                    "fabric_lot":  self.fabric_lot,
                    "work_order":  self.work_order,
                    "fail_reason": reason,
                    "critical":    self.defect_count_critical,
                    "major":       self.defect_count_major,
                },
                room="shop_floor_supervisors",
            )

    def supervisor_force_pass(self, notes=None):
        """Allow a Manufacturing Manager to override a Fail result."""
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

    # ------------------------------------------------------------------

    def _recalculate_counts(self):
        minor = major = critical = 0
        total_area = 0.0
        for row in self.defect_map:
            sev = (row.severity or "").lower()
            if sev == "minor":
                minor += 1
            elif sev == "major":
                major += 1
            elif sev == "critical":
                critical += 1
            total_area += float(row.area_mm2 or 0)
        self.defect_count_minor    = minor
        self.defect_count_major    = major
        self.defect_count_critical = critical
        self.total_defect_area_mm2 = round(total_area, 2)


def _evaluate_pass_fail(minor, major, critical, config):
    """Return (result_str, reason_str)."""
    if config.fail_on_any_critical and critical > 0:
        return "Fail", "Critical defect detected ({} found).".format(critical)
    max_major = int(config.max_major_defects or 2)
    if major > max_major:
        return "Fail", "Major defects ({}) exceed threshold ({}).".format(major, max_major)
    max_minor = int(config.max_minor_defects or 5)
    if minor > max_minor:
        return "Fail", "Minor defects ({}) exceed threshold ({}).".format(minor, max_minor)
    return "Pass", ""
