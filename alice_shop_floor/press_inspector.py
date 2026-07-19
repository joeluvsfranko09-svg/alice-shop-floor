"""
V6: Press QC Inspector
======================
Cognex camera integration for the press machine stage.

Architecture mirrors V1-V4 (Fabric, Stitch, Cut, Final) inspectors.

Camera placement: one Cognex In-Sight camera mounted above the pressing
board captures each garment piece after it leaves the press. The camera
sends a structured JSON payload containing defect detections:
    scorch, burn_mark, shine, crease_miss, pressure_uneven, fabric_distortion.

Flow:
    trigger_press_inspection(work_order)
      ↓  creates PressInspectionLog (status=Pending)
      ↓  polls Cognex via HTTP (EasyJobs REST API)
      ↓  applies result → evaluates against PressInspectionConfig thresholds
      ↓  Pass / Fail → realtime event → Production Stage gate

Gate:  Press result must be Pass (or supervisor-overridden) before the
        Production Stage Tracker advances the garment to Final QC.
"""

import frappe
import requests
from frappe.utils import now_datetime


# ── Config helpers ────────────────────────────────────────────────────────────

def _get_config() -> "PressInspectionConfig":
    return frappe.get_single("Press Inspection Config")


def _config_enabled() -> bool:
    try:
        return bool(_get_config().enabled)
    except Exception:
        return False


# ── Trigger ───────────────────────────────────────────────────────────────────

def trigger_press_inspection(work_order: str,
                              press_temperature_c: float = None,
                              press_pressure_bar: float = None,
                              dwell_time_sec: float = None) -> str:
    """
    Create a PressInspectionLog in Pending state and dispatch the
    Cognex poll job to the background queue.
    Returns the log name.
    """
    doc = frappe.new_doc("Press Inspection Log")
    doc.work_order          = work_order
    doc.production_item     = frappe.db.get_value("Work Order", work_order, "production_item") or ""
    doc.overall_result      = "Pending"
    doc.triggered_by        = frappe.session.user
    doc.press_temperature_c = press_temperature_c
    doc.press_pressure_bar  = press_pressure_bar
    doc.dwell_time_sec      = dwell_time_sec
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    # Queue the Cognex poll in the background
    frappe.enqueue(
        "alice_shop_floor.alice_shop_floor.press_inspector.poll_and_apply",
        queue="short",
        timeout=300,
        log_name=doc.name,
    )
    return doc.name


# ── Cognex polling ────────────────────────────────────────────────────────────

def poll_and_apply(log_name: str) -> None:
    """
    Background worker: poll Cognex EasyJobs REST API and apply the result.
    Mirrors the V1-V4 poll_and_apply pattern exactly.
    """
    doc = frappe.get_doc("Press Inspection Log", log_name)
    if doc.overall_result != "Pending":
        return  # already resolved

    config = _get_config()
    host   = config.cognex_host or ""
    port   = config.cognex_port or 80
    job    = config.cognex_job_name or "press_qc"
    user   = config.cognex_username or "admin"
    pw     = frappe.utils.password.get_decrypted_password(
        "Press Inspection Config", "Press Inspection Config", "cognex_password"
    ) or ""

    max_attempts = int(config.max_poll_attempts or 20)
    interval_sec = int(config.poll_interval_seconds or 30)
    threshold    = float(config.confidence_threshold or 0.80)

    import time
    url = f"http://{host}:{port}/api/jobs/{job}/result"

    for attempt in range(max_attempts):
        try:
            resp = requests.get(
                url,
                auth=(user, pw),
                timeout=10,
                params={"min_confidence": threshold},
            )
            resp.raise_for_status()
            payload = resp.json()

            if payload.get("status") == "ready":
                _apply_cognex_result(doc, payload, threshold)
                return

        except requests.RequestException as exc:
            frappe.log_error(str(exc), f"PressInspector poll attempt {attempt+1}/{max_attempts}")

        time.sleep(interval_sec)

    # Timed out
    _apply_timeout(doc)


def _apply_cognex_result(doc, payload: dict, threshold: float) -> None:
    """Evaluate Cognex payload against configured thresholds."""
    doc.cognex_job_id  = payload.get("job_id") or ""
    doc.inspected_at   = now_datetime()

    # Confidence
    scores = [d.get("confidence", 0) for d in (payload.get("defects") or [])]
    doc.confidence_score = sum(scores) / len(scores) if scores else 1.0

    # Populate defect map (only detections above threshold)
    doc.defect_map = []
    for d in (payload.get("defects") or []):
        if float(d.get("confidence", 0)) < threshold:
            continue
        doc.append("defect_map", {
            "defect_type":      d.get("defect_type") or "Other",
            "severity":         d.get("severity") or "Minor",
            "zone":             d.get("zone") or "",
            "confidence_score": d.get("confidence") or 0,
            "x_mm":             d.get("x") or 0,
            "y_mm":             d.get("y") or 0,
            "image_ref":        d.get("image_ref") or "",
        })

    # Count by type
    doc.defect_count_scorch  = sum(1 for r in doc.defect_map if r.defect_type == "Scorch")
    doc.defect_count_shine   = sum(1 for r in doc.defect_map if r.defect_type == "Shine")
    doc.defect_count_crease  = sum(1 for r in doc.defect_map if r.defect_type == "Crease Miss")
    doc.defect_count_burn    = sum(1 for r in doc.defect_map if r.defect_type == "Burn Mark")

    # Evaluate pass/fail
    config = _get_config()
    reasons = []
    if doc.defect_count_scorch > int(config.max_scorch_allowed or 0):
        reasons.append(f"{doc.defect_count_scorch} scorch mark(s) (max {config.max_scorch_allowed})")
    if doc.defect_count_shine > int(config.max_shine_allowed or 2):
        reasons.append(f"{doc.defect_count_shine} shine mark(s) (max {config.max_shine_allowed})")
    if doc.defect_count_crease > int(config.max_crease_miss_allowed or 1):
        reasons.append(f"{doc.defect_count_crease} crease miss(es) (max {config.max_crease_miss_allowed})")
    if config.fail_on_any_burn and doc.defect_count_burn > 0:
        reasons.append(f"{doc.defect_count_burn} burn mark(s) — zero tolerance")

    doc.overall_result = "Fail" if reasons else "Pass"
    doc.fail_reason    = "; ".join(reasons) if reasons else ""
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    _fire_realtime(doc)


def _apply_timeout(doc) -> None:
    doc.overall_result = "Error"
    doc.error_message  = "Press inspection timed out — no Cognex result received."
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.publish_realtime(
        "press_inspection_timeout",
        {"name": doc.name, "work_order": doc.work_order},
        room="shop_floor_supervisors",
    )


def _fire_realtime(doc) -> None:
    if doc.overall_result == "Pass":
        frappe.publish_realtime(
            "press_inspection_passed",
            {"name": doc.name, "work_order": doc.work_order},
            after_commit=True,
        )
    else:
        frappe.publish_realtime(
            "press_inspection_failed",
            {
                "name":        doc.name,
                "work_order":  doc.work_order,
                "fail_reason": doc.fail_reason,
            },
            room="shop_floor_supervisors",
            after_commit=True,
        )


# ── Supervisor override ───────────────────────────────────────────────────────

def supervisor_override_press(log_name: str, notes: str) -> None:
    """Allow a supervisor to override a failed press inspection."""
    doc = frappe.get_doc("Press Inspection Log", log_name)
    if doc.overall_result not in ("Fail", "Error"):
        frappe.throw("Only Failed or Error inspections can be overridden.")
    doc.supervisor_override = 1
    doc.overridden_by       = frappe.session.user
    doc.overridden_at       = now_datetime()
    doc.override_notes      = notes
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.publish_realtime(
        "press_inspection_overridden",
        {"name": log_name, "work_order": doc.work_order, "overridden_by": frappe.session.user},
        room="shop_floor_supervisors",
    )
