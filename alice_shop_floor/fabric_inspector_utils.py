"""
fabric_inspector_utils.py -- V1: Fabric Inspector (Cognex In-Sight 3900)
=========================================================================
Manages fabric inspection jobs through the Cognex In-Sight 3900 REST API.

Cognex In-Sight REST API endpoints used:
  POST /SISSW/v1/jobs/{job_name}/run     -- trigger a job
  GET  /SISSW/v1/results/latest          -- poll latest job result
  GET  /SISSW/v1/results/{job_id}        -- fetch specific result by ID

Pass/Fail gate: a Work Order's tracker cannot advance from
'Fabric Inspection' to 'Cutting' unless a Pass (or supervisor-overridden)
FabricInspectionResult exists for that fabric_lot.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime
import requests
from requests.auth import HTTPBasicAuth


class FabricInspectorEngine:
    """
    Frappe-side engine. Called from whitelisted API endpoints and scheduled task.
    Does NOT call Cognex directly -- that is alice_core's job via the
    fabric inspector vision client. This engine:
      1. Creates FabricInspectionResult docs (Pending state)
      2. Processes inbound Cognex result payloads (via webhook)
      3. Checks pass/fail gate for stage transitions
    """

    # ------------------------------------------------------------------
    # Trigger an inspection (creates Pending record; alice_core fires Cognex)
    # ------------------------------------------------------------------

    def trigger_inspection(self, work_order: str, fabric_lot: str,
                           triggered_by: str = None) -> dict:
        """
        Create a Pending FabricInspectionResult and return its name +
        the Cognex connection config so alice_core can fire the job.
        """
        # Prevent duplicate pending inspections for same lot
        existing = frappe.db.exists(
            "Fabric Inspection Result",
            {"fabric_lot": fabric_lot, "overall_result": "Pending"},
        )
        if existing:
            return {
                "status": "already_pending",
                "result_name": existing,
                "fabric_lot": fabric_lot,
            }

        doc = frappe.new_doc("Fabric Inspection Result")
        doc.fabric_lot   = fabric_lot
        doc.work_order   = work_order
        doc.overall_result = "Pending"
        doc.triggered_by = triggered_by or frappe.session.user
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        config = self._get_config()
        return {
            "status": "triggered",
            "result_name": doc.name,
            "fabric_lot": fabric_lot,
            "work_order": work_order,
            "cognex_host": config.cognex_host,
            "cognex_port": config.cognex_port,
            "cognex_job_name": config.cognex_job_name,
            "cognex_username": config.cognex_username,
            "cognex_password": config.get_password("cognex_password"),
            "poll_interval_seconds": config.poll_interval_seconds,
            "max_poll_attempts": config.max_poll_attempts,
        }

    # ------------------------------------------------------------------
    # Process an inbound Cognex result payload (webhook or polling response)
    # ------------------------------------------------------------------

    def process_cognex_result(self, result_name: str, cognex_payload: dict) -> dict:
        """
        Called when Cognex pushes or alice_core polls a completed result.
        Updates the FabricInspectionResult doc and fires gate checks.
        """
        doc = frappe.get_doc("Fabric Inspection Result", result_name)
        if doc.overall_result not in ("Pending", "Error"):
            return {"status": "already_processed", "result": doc.overall_result}

        doc.apply_cognex_result(cognex_payload)

        # Advance stage tracker if Pass
        if doc.overall_result == "Pass":
            self._notify_stage_clear(doc.work_order, doc.fabric_lot)

        return {
            "status": "processed",
            "result_name": result_name,
            "overall_result": doc.overall_result,
            "fabric_lot": doc.fabric_lot,
            "work_order": doc.work_order,
            "defects_minor": doc.defect_count_minor,
            "defects_major": doc.defect_count_major,
            "defects_critical": doc.defect_count_critical,
            "fail_reason": doc.fail_reason or "",
        }

    # ------------------------------------------------------------------
    # Gate check: can this Work Order advance from Fabric Inspection?
    # ------------------------------------------------------------------

    def check_fabric_pass_gate(self, work_order: str, fabric_lot: str) -> dict:
        """
        Returns whether the fabric_lot has a passing inspection.
        Called by production_stage_tracker before allowing Cutting transition.
        """
        pass_result = frappe.db.exists(
            "Fabric Inspection Result",
            {
                "fabric_lot": fabric_lot,
                "overall_result": "Pass",
            },
        )
        if pass_result:
            return {"gate": "open", "result_name": pass_result}

        pending = frappe.db.exists(
            "Fabric Inspection Result",
            {"fabric_lot": fabric_lot, "overall_result": "Pending"},
        )
        if pending:
            return {"gate": "pending", "result_name": pending,
                    "message": "Fabric inspection in progress."}

        fail_result = frappe.db.get_value(
            "Fabric Inspection Result",
            {"fabric_lot": fabric_lot, "overall_result": "Fail"},
            ["name", "fail_reason"],
            as_dict=True,
        )
        if fail_result:
            return {
                "gate": "failed",
                "result_name": fail_result.name,
                "message": "Fabric inspection failed: {}".format(fail_result.fail_reason),
            }

        return {"gate": "no_inspection",
                "message": "No fabric inspection record found for lot {}.".format(fabric_lot)}

    # ------------------------------------------------------------------
    # Poll pending inspections (called by scheduled task every 5 min)
    # ------------------------------------------------------------------

    def poll_pending_inspections(self) -> dict:
        """
        Find all Pending FabricInspectionResult docs that have a Cognex
        job ID but haven't resolved. Return their details so alice_core
        can poll Cognex and call the webhook endpoint with results.
        This is a Frappe-side helper; actual HTTP polling is in alice_core.
        """
        pending = frappe.get_all(
            "Fabric Inspection Result",
            filters={"overall_result": "Pending"},
            fields=["name", "fabric_lot", "work_order", "cognex_job_id", "creation"],
            order_by="creation asc",
        )
        config = self._get_config()
        return {
            "pending_count": len(pending),
            "pending": pending,
            "cognex_host": config.cognex_host,
            "cognex_port": config.cognex_port,
            "cognex_username": config.cognex_username,
            "cognex_password": config.get_password("cognex_password"),
        }

    # ------------------------------------------------------------------
    # Supervisor override pass
    # ------------------------------------------------------------------

    def force_pass(self, result_name: str, notes: str = None) -> dict:
        """Supervisor override: force a Fail/Error result to Pass."""
        doc = frappe.get_doc("Fabric Inspection Result", result_name)
        doc.supervisor_force_pass(notes=notes)
        self._notify_stage_clear(doc.work_order, doc.fabric_lot)
        return {"status": "overridden", "result_name": result_name,
                "overridden_by": doc.overridden_by}

    # ------------------------------------------------------------------
    # Inspection history for a fabric lot or work order
    # ------------------------------------------------------------------

    def get_history(self, fabric_lot: str = None, work_order: str = None,
                    limit: int = 20) -> list:
        filters = {}
        if fabric_lot:
            filters["fabric_lot"] = fabric_lot
        if work_order:
            filters["work_order"] = work_order
        return frappe.get_all(
            "Fabric Inspection Result",
            filters=filters,
            fields=[
                "name", "fabric_lot", "work_order", "overall_result",
                "inspected_at", "defect_count_minor", "defect_count_major",
                "defect_count_critical", "fail_reason", "supervisor_override",
            ],
            order_by="creation desc",
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_config(self):
        return frappe.get_single("Fabric Inspection Config")

    def _notify_stage_clear(self, work_order: str, fabric_lot: str):
        """Publish realtime event so the dashboard can unlock the Cutting button."""
        if not work_order:
            return
        frappe.publish_realtime(
            event="fabric_inspection_passed",
            message={
                "work_order": work_order,
                "fabric_lot": fabric_lot,
                "message": "Fabric inspection passed. Cutting stage unlocked.",
            },
            room="shop_floor",
        )


# ── module-level convenience wrappers ────────────────────────────────────────

def trigger_fabric_inspection(work_order: str, fabric_lot: str,
                               triggered_by: str = None) -> dict:
    return FabricInspectorEngine().trigger_inspection(work_order, fabric_lot, triggered_by)


def process_cognex_result(result_name: str, cognex_payload: dict) -> dict:
    return FabricInspectorEngine().process_cognex_result(result_name, cognex_payload)


def check_fabric_pass_gate(work_order: str, fabric_lot: str) -> dict:
    return FabricInspectorEngine().check_fabric_pass_gate(work_order, fabric_lot)


def poll_pending_inspections() -> dict:
    return FabricInspectorEngine().poll_pending_inspections()
