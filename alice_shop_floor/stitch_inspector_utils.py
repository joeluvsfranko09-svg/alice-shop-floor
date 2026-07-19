"""
stitch_inspector_utils.py -- V2: Inline Stitch QC (Cognex In-Sight 3900)
=========================================================================
Manages inline stitch quality inspection jobs.
Gate: Work Order cannot advance from Sewing to Final QC without a Pass.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime


class StitchInspectorEngine:

    def trigger_inspection(self, work_order: str, tracker: str = None,
                           triggered_by: str = None) -> dict:
        existing = frappe.db.exists(
            "Stitch Inspection Result",
            {"work_order": work_order, "overall_result": "Pending"},
        )
        if existing:
            return {"status": "already_pending", "result_name": existing, "work_order": work_order}

        doc = frappe.new_doc("Stitch Inspection Result")
        doc.work_order   = work_order
        doc.tracker      = tracker or ""
        doc.overall_result = "Pending"
        doc.triggered_by = triggered_by or frappe.session.user
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        config = self._get_config()
        return {
            "status":               "triggered",
            "result_name":          doc.name,
            "work_order":           work_order,
            "cognex_host":          config.cognex_host,
            "cognex_port":          config.cognex_port,
            "cognex_job_name":      config.cognex_job_name,
            "cognex_username":      config.cognex_username,
            "cognex_password":      config.get_password("cognex_password"),
            "poll_interval_seconds": config.poll_interval_seconds,
            "max_poll_attempts":    config.max_poll_attempts,
        }

    def process_cognex_result(self, result_name: str, cognex_payload: dict) -> dict:
        doc = frappe.get_doc("Stitch Inspection Result", result_name)
        if doc.overall_result not in ("Pending", "Error"):
            return {"status": "already_processed", "result": doc.overall_result}

        doc.apply_cognex_result(cognex_payload)

        if doc.overall_result == "Pass":
            frappe.publish_realtime(
                event="stitch_inspection_passed",
                message={"work_order": doc.work_order, "result_name": result_name},
                room="shop_floor",
            )

        return {
            "status":          "processed",
            "result_name":     result_name,
            "overall_result":  doc.overall_result,
            "work_order":      doc.work_order,
            "skipped":         doc.skipped_stitches,
            "broken":          doc.broken_stitches,
            "loose":           doc.loose_stitches,
            "critical":        doc.critical_defect_count,
            "fail_reason":     doc.fail_reason or "",
        }

    def check_stitch_pass_gate(self, work_order: str) -> dict:
        pass_result = frappe.db.exists(
            "Stitch Inspection Result",
            {"work_order": work_order, "overall_result": "Pass"},
        )
        if pass_result:
            return {"gate": "open", "result_name": pass_result}

        pending = frappe.db.exists(
            "Stitch Inspection Result",
            {"work_order": work_order, "overall_result": "Pending"},
        )
        if pending:
            return {"gate": "pending", "result_name": pending,
                    "message": "Stitch inspection in progress."}

        fail_result = frappe.db.get_value(
            "Stitch Inspection Result",
            {"work_order": work_order, "overall_result": "Fail"},
            ["name", "fail_reason"],
            as_dict=True,
        )
        if fail_result:
            return {
                "gate":        "failed",
                "result_name": fail_result.name,
                "message":     "Stitch inspection failed: {}".format(fail_result.fail_reason),
            }

        return {"gate": "no_inspection",
                "message": "No stitch inspection found for WO {}.".format(work_order)}

    def poll_pending_inspections(self) -> dict:
        pending = frappe.get_all(
            "Stitch Inspection Result",
            filters={"overall_result": "Pending"},
            fields=["name", "work_order", "cognex_job_id", "creation"],
            order_by="creation asc",
        )
        config = self._get_config()
        return {
            "pending_count":  len(pending),
            "pending":        pending,
            "cognex_host":    config.cognex_host,
            "cognex_port":    config.cognex_port,
            "cognex_username": config.cognex_username,
            "cognex_password": config.get_password("cognex_password"),
        }

    def force_pass(self, result_name: str, notes: str = None) -> dict:
        doc = frappe.get_doc("Stitch Inspection Result", result_name)
        doc.supervisor_force_pass(notes=notes)
        frappe.publish_realtime(
            event="stitch_inspection_passed",
            message={"work_order": doc.work_order, "result_name": result_name,
                     "override": True},
            room="shop_floor",
        )
        return {"status": "overridden", "result_name": result_name,
                "overridden_by": doc.overridden_by}

    def get_history(self, work_order: str = None, limit: int = 20) -> list:
        filters = {}
        if work_order:
            filters["work_order"] = work_order
        return frappe.get_all(
            "Stitch Inspection Result",
            filters=filters,
            fields=["name", "work_order", "overall_result", "inspected_at",
                    "skipped_stitches", "broken_stitches", "loose_stitches",
                    "critical_defect_count", "fail_reason", "supervisor_override"],
            order_by="creation desc",
            limit=limit,
        )

    def _get_config(self):
        return frappe.get_single("Stitch Inspection Config")


def trigger_stitch_inspection(work_order, tracker=None, triggered_by=None):
    return StitchInspectorEngine().trigger_inspection(work_order, tracker, triggered_by)

def process_cognex_stitch_result(result_name, cognex_payload):
    return StitchInspectorEngine().process_cognex_result(result_name, cognex_payload)

def check_stitch_pass_gate(work_order):
    return StitchInspectorEngine().check_stitch_pass_gate(work_order)

def poll_pending_stitch_inspections():
    return StitchInspectorEngine().poll_pending_inspections()
