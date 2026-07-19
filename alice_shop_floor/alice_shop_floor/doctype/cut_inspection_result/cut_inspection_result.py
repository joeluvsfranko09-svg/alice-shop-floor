import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class CutInspectionResult(Document):

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

        self.deviation_map = []
        config = frappe.get_single("Cut Inspection Config")

        for idx, d in enumerate(cognex_payload.get("deviations", []), start=1):
            dev_mm = abs(float(d.get("deviation_mm") or
                               (float(d.get("measured_mm") or 0) - float(d.get("expected_mm") or 0))))
            self.append("deviation_map", {
                "deviation_index":   idx,
                "deviation_type":    d.get("deviation_type", "Other"),
                "severity":          _classify_severity(d, config),
                "panel_id":          d.get("panel_id") or "",
                "measured_mm":       d.get("measured_mm") or 0,
                "expected_mm":       d.get("expected_mm") or 0,
                "deviation_mm":      round(dev_mm, 3),
                "angle_deviation_deg": d.get("angle_deviation_deg") or 0,
                "confidence_score":  d.get("confidence") or 0,
                "image_ref":         d.get("image_ref") or "",
            })

        panels = cognex_payload.get("panels_inspected") or 0
        panels_passed = cognex_payload.get("panels_passed") or 0
        self.panels_inspected = panels
        self.panels_passed    = panels_passed
        self._recalculate_counts()
        self.inspected_at = now_datetime()

        result, reason = _evaluate_pass_fail(
            self.deviation_count_minor,
            self.deviation_count_major,
            self.deviation_count_critical,
            config,
        )
        self.overall_result = result
        self.fail_reason    = reason

        self.save(ignore_permissions=True)
        frappe.db.commit()

        if result == "Fail":
            frappe.publish_realtime(
                event="cut_inspection_failed",
                message={
                    "name":       self.name,
                    "work_order": self.work_order,
                    "fabric_lot": self.fabric_lot,
                    "fail_reason": reason,
                    "critical":   self.deviation_count_critical,
                    "major":      self.deviation_count_major,
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
        minor = major = critical = 0
        for row in self.deviation_map:
            sev = (row.severity or "").lower()
            if sev == "minor":
                minor += 1
            elif sev == "major":
                major += 1
            elif sev == "critical":
                critical += 1
        self.deviation_count_minor    = minor
        self.deviation_count_major    = major
        self.deviation_count_critical = critical


def _classify_severity(d: dict, config) -> str:
    """
    Auto-classify severity if not provided by Cognex.
    Uses the config tolerances: >3x tolerance = Critical, >1x = Major, else Minor.
    """
    if d.get("severity"):
        return str(d["severity"]).capitalize()
    dev_mm  = abs(float(d.get("deviation_mm") or 0))
    ang_deg = abs(float(d.get("angle_deviation_deg") or 0))
    len_tol = float(config.length_tolerance_mm or 3)
    ang_tol = float(config.angle_tolerance_degrees or 1.5)
    if dev_mm > len_tol * 3 or ang_deg > ang_tol * 3:
        return "Critical"
    if dev_mm > len_tol or ang_deg > ang_tol:
        return "Major"
    return "Minor"


def _evaluate_pass_fail(minor, major, critical, config):
    if config.fail_on_any_critical and critical > 0:
        return "Fail", "Critical cut deviation detected ({} found).".format(critical)
    max_major = int(config.max_major_deviations or 1)
    if major > max_major:
        return "Fail", "Major deviations ({}) exceed threshold ({}).".format(major, max_major)
    max_minor = int(config.max_minor_deviations or 3)
    if minor > max_minor:
        return "Fail", "Minor deviations ({}) exceed threshold ({}).".format(minor, max_minor)
    return "Pass", ""
