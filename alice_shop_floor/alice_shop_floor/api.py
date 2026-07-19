"""
ALICE Shop Floor - External API endpoints.
POST /api/method/alice_shop_floor.api.<function_name>
Auth: Frappe API key/secret.
"""

import frappe
from frappe import _


# ------------------------------------------------------------------
# PrintFactory webhook
# ------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def printfactory_webhook():
    """
    Called by PrintFactory Cloud when a print/cut job completes.
    Advances the matching tracker from Cutting -> Bundling automatically.

    Expected JSON payload:
      {"job_id": "PF-2026-00123", "status": "completed", ...}
    """
    import json

    payload = frappe.request.data
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    data = json.loads(payload)

    job_id = data.get("job_id")
    status = data.get("status")

    if not job_id:
        frappe.throw(_("Missing job_id in PrintFactory webhook payload"), frappe.ValidationError)

    if status != "completed":
        frappe.logger().warning(
            "PrintFactory job {} status: {}. No stage advance.".format(job_id, status)
        )
        return {"ok": True, "action": "none", "reason": "status={}".format(status)}

    trackers = frappe.get_list(
        "Production Stage Tracker",
        filters={"printfactory_job_id": job_id, "current_stage": "Cutting"},
        fields=["name", "work_order"],
        limit=1,
    )

    if not trackers:
        frappe.logger().warning(
            "No tracker in Cutting stage for PrintFactory job {}".format(job_id)
        )
        return {"ok": True, "action": "none", "reason": "no matching tracker in Cutting stage"}

    tracker_doc = frappe.get_doc("Production Stage Tracker", trackers[0]["name"])
    tracker_doc.advance_stage(
        trigger_source="PrintFactory Webhook",
        notes="Auto-advanced by PrintFactory job completion. Job ID: {}".format(job_id),
    )

    frappe.logger().info(
        "PrintFactory webhook: advanced WO {} from Cutting -> Bundling. Job: {}".format(
            trackers[0]["work_order"], job_id
        )
    )

    return {
        "ok": True,
        "action": "stage_advanced",
        "work_order": trackers[0]["work_order"],
        "from_stage": "Cutting",
        "to_stage": "Bundling",
    }


# ------------------------------------------------------------------
# Stage management
# ------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def advance_stage(tracker_name, notes=None):
    """Advance a tracker to the next stage. Called by ALICE on Replit."""
    doc = frappe.get_doc("Production Stage Tracker", tracker_name)
    doc.advance_stage(trigger_source="ALICE Auto", notes=notes)
    return {"ok": True, "new_stage": doc.current_stage}


@frappe.whitelist(allow_guest=False)
def supervisor_override(tracker_name, target_stage, reason):
    """Supervisor override: jump to any stage out of sequence. Requires reason."""
    if not reason:
        frappe.throw(_("Override reason is required"), frappe.ValidationError)

    doc = frappe.get_doc("Production Stage Tracker", tracker_name)
    doc.set_stage(
        stage=target_stage,
        trigger_source="Manual",
        is_supervisor_override=True,
        override_reason=reason,
    )
    return {"ok": True, "new_stage": doc.current_stage}


@frappe.whitelist(allow_guest=False)
def get_floor_status():
    """All active production orders and their current stage."""
    trackers = frappe.db.sql(
        """
        SELECT
            pst.name, pst.work_order, pst.current_stage, pst.stage_entered_at,
            pst.printfactory_job_id, pst.fabric_lot,
            wo.production_item, wo.qty, wo.expected_delivery_date
        FROM `tabProduction Stage Tracker` pst
        LEFT JOIN `tabWork Order` wo ON wo.name = pst.work_order
        WHERE pst.is_complete = 0
        ORDER BY wo.expected_delivery_date ASC
        """,
        as_dict=True,
    )
    return trackers


@frappe.whitelist(allow_guest=False)
def register_printfactory_job(tracker_name, job_id):
    """Link a PrintFactory Job ID to a tracker before the job completes."""
    frappe.db.set_value("Production Stage Tracker", tracker_name, "printfactory_job_id", job_id)
    frappe.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------
# Module 4 - Inline QC / Defect Tracker
# ------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def record_qc_result(
    tracker_name, qc_stage, result,
    checked_by=None, trigger_source="Manual",
    defects=None, photo_ref=None, notes=None,
):
    """
    Record a QC inspection for a single custom garment.
    Every garment is unique (POD) - one QC check per garment per stage.

    defects: list of {defect_type, severity, location, notes, photo_ref}
    result:  Pass / Fail / Rework Required
    """
    import json

    tracker = frappe.get_doc("Production Stage Tracker", tracker_name)

    if isinstance(defects, str):
        defects = json.loads(defects)

    doc = frappe.get_doc({
        "doctype": "Garment QC Check",
        "tracker": tracker_name,
        "work_order": tracker.work_order,
        "qc_stage": qc_stage,
        "result": result,
        "checked_by": checked_by or frappe.session.user,
        "trigger_source": trigger_source,
        "photo_ref": photo_ref or "",
        "notes": notes or "",
        "defects": [
            {
                "defect_type": d.get("defect_type"),
                "severity": d.get("severity", "Minor"),
                "location": d.get("location", ""),
                "photo_ref": d.get("photo_ref", ""),
                "notes": d.get("notes", ""),
            }
            for d in (defects or [])
        ],
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "qc_check": doc.name,
        "result": doc.result,
        "defect_score": doc.get_defect_score(),
        "critical_count": doc.get_critical_count(),
        "is_auto_fail": doc.is_auto_fail(),
        "defect_summary": doc.get_defect_summary(),
    }


@frappe.whitelist(allow_guest=False)
def get_qc_summary(work_order=None, qc_stage=None, checked_by=None, result=None, days=30):
    """QC analytics - filter by work order, stage, inspector, or result."""
    from frappe.utils import add_days, today

    filters = {}
    if work_order:
        filters["work_order"] = work_order
    if qc_stage:
        filters["qc_stage"] = qc_stage
    if checked_by:
        filters["checked_by"] = checked_by
    if result:
        filters["result"] = result
    filters["checked_at"] = [">=", add_days(today(), -int(days))]

    checks = frappe.get_list(
        "Garment QC Check",
        filters=filters,
        fields=[
            "name", "work_order", "tracker", "qc_stage", "result",
            "checked_by", "checked_at", "trigger_source",
        ],
        order_by="checked_at DESC",
        limit=500,
    )

    total = len(checks)
    passed = sum(1 for c in checks if c["result"] == "Pass")
    failed = sum(1 for c in checks if c["result"] == "Fail")
    rework = sum(1 for c in checks if c["result"] == "Rework Required")

    return {
        "checks": checks,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "rework_required": rework,
            "pass_rate": round((passed / total * 100), 1) if total else 0,
        },
    }


@frappe.whitelist(allow_guest=False)
def get_operator_quality_stats(checked_by, days=30):
    """Quality performance stats for one operator. Feeds Module 6."""
    from frappe.utils import add_days, today

    since = add_days(today(), -int(days))

    checks = frappe.db.sql(
        """
        SELECT qc.name, qc.work_order, qc.qc_stage, qc.result, qc.checked_at,
               COUNT(d.name) as defect_count
        FROM `tabGarment QC Check` qc
        LEFT JOIN `tabQC Defect` d ON d.parent = qc.name
        WHERE qc.checked_by = %(checked_by)s AND qc.checked_at >= %(since)s
        GROUP BY qc.name
        ORDER BY qc.checked_at DESC
        """,
        {"checked_by": checked_by, "since": since},
        as_dict=True,
    )

    defect_types = frappe.db.sql(
        """
        SELECT d.defect_type, d.severity, COUNT(*) as count
        FROM `tabQC Defect` d
        JOIN `tabGarment QC Check` qc ON qc.name = d.parent
        WHERE qc.checked_by = %(checked_by)s AND qc.checked_at >= %(since)s
        GROUP BY d.defect_type, d.severity
        ORDER BY count DESC
        """,
        {"checked_by": checked_by, "since": since},
        as_dict=True,
    )

    total = len(checks)
    passed = sum(1 for c in checks if c["result"] == "Pass")

    return {
        "operator": checked_by,
        "period_days": int(days),
        "total_inspections": total,
        "pass_rate": round((passed / total * 100), 1) if total else 0,
        "defect_breakdown": defect_types,
        "checks": checks,
    }


# ------------------------------------------------------------------
# Module 8 - Garment Passport QR
# ------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def seal_garment_passport(tracker_name):
    """
    Manually trigger passport sealing for a tracker at Pack stage.
    Normally called automatically by production_stage_tracker.set_stage()
    when stage reaches Pack. This endpoint allows ALICE to trigger it
    explicitly if needed (e.g. after crash recovery).
    """
    tracker = frappe.get_doc("Production Stage Tracker", tracker_name)

    if tracker.current_stage != "Pack":
        frappe.throw(
            _("Passport can only be sealed when tracker is at Pack stage. "
              "Current stage: {}".format(tracker.current_stage))
        )

    existing = frappe.db.exists("Garment Passport", {"work_order": tracker.work_order})
    if existing:
        passport = frappe.get_doc("Garment Passport", existing)
    else:
        passport = frappe.get_doc({
            "doctype": "Garment Passport",
            "work_order": tracker.work_order,
            "tracker": tracker_name,
            "fabric_lot": tracker.fabric_lot or "",
            "pattern_file_ref": tracker.pattern_file_ref or "",
            "printfactory_job_id": tracker.printfactory_job_id or "",
        })
        passport.insert(ignore_permissions=True)

    if passport.is_sealed:
        return {"ok": True, "already_sealed": True, "passport": passport.name,
                "passport_url": passport.passport_url}

    passport_url = passport.seal(sealed_by=frappe.session.user)
    return {"ok": True, "passport": passport.name, "passport_url": passport_url}


@frappe.whitelist(allow_guest=True)
def get_passport(passport_name=None, work_order=None):
    """
    Public endpoint: returns the full passport data for customer-facing display.
    Either passport_name or work_order must be provided.
    allow_guest=True so the hangtag QR scan works without authentication.
    """
    if passport_name:
        passport = frappe.get_doc("Garment Passport", passport_name)
    elif work_order:
        name = frappe.db.get_value("Garment Passport", {"work_order": work_order}, "name")
        if not name:
            frappe.throw(_("No passport found for Work Order {}".format(work_order)))
        passport = frappe.get_doc("Garment Passport", name)
    else:
        frappe.throw(_("passport_name or work_order required"))

    if not passport.is_sealed:
        frappe.throw(_("This passport has not been sealed yet."))

    return passport.get_public_data()


@frappe.whitelist(allow_guest=False)
def record_passport_operator(tracker_name, stage, operator, notes=None):
    """
    Add an operator touch record to the Garment Passport for this work order.
    Called by ALICE when a floor operator scans their job card at a station.
    Creates the passport doc if it does not exist yet (pre-Pack).
    """
    from frappe.utils import now_datetime

    tracker = frappe.get_doc("Production Stage Tracker", tracker_name)

    existing = frappe.db.exists("Garment Passport", {"work_order": tracker.work_order})
    if existing:
        passport = frappe.get_doc("Garment Passport", existing)
    else:
        passport = frappe.get_doc({
            "doctype": "Garment Passport",
            "work_order": tracker.work_order,
            "tracker": tracker_name,
            "fabric_lot": tracker.fabric_lot or "",
            "pattern_file_ref": tracker.pattern_file_ref or "",
            "printfactory_job_id": tracker.printfactory_job_id or "",
        })
        passport.insert(ignore_permissions=True)

    if passport.is_sealed:
        frappe.throw(_("Cannot add operator record to a sealed passport."))

    passport.append("operators", {
        "stage": stage,
        "operator": operator,
        "touched_at": now_datetime(),
        "operation_notes": notes or "",
    })
    passport.save(ignore_permissions=True)
    frappe.db.commit()

    return {"ok": True, "passport": passport.name}


@frappe.whitelist(allow_guest=False)
def get_unsealed_pack_trackers():
    """
    Return trackers that are at Pack or is_complete=1 but whose
    Garment Passport has not been sealed yet.
    Used by ALICE startup reconciliation.
    """
    trackers = frappe.db.sql(
        """
        SELECT pst.name, pst.work_order, pst.current_stage
        FROM `tabProduction Stage Tracker` pst
        WHERE (pst.current_stage = 'Pack' OR pst.is_complete = 1)
          AND NOT EXISTS (
              SELECT 1 FROM `tabGarment Passport` gp
              WHERE gp.work_order = pst.work_order
                AND gp.is_sealed = 1
          )
        ORDER BY pst.stage_entered_at ASC
        """,
        as_dict=True,
    )
    return trackers


# ======================================================================
# Module 1: Incentive Pay Engine — API endpoints
# ======================================================================

@frappe.whitelist()
def get_pay_period_preview(period_start=None, period_end=None):
    """
    Return a preview of incentive pay for the current (or specified) ISO week.
    No data is saved — supervisor can review before finalizing.
    """
    from alice_shop_floor.alice_shop_floor.incentive_pay_utils import (
        IncentivePayEngineERPNext,
    )
    from datetime import date

    if period_start and period_end:
        from datetime import datetime
        start = datetime.strptime(period_start, "%Y-%m-%d").date()
        end = datetime.strptime(period_end, "%Y-%m-%d").date()
        engine = IncentivePayEngineERPNext(start, end)
    else:
        today = date.today()
        from datetime import timedelta
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        engine = IncentivePayEngineERPNext(monday, sunday)

    return engine.get_period_preview()


@frappe.whitelist()
def calculate_pay_period(period_start, period_end):
    """
    Compute and save Operator Pay Period Summaries for the given window.
    Creates or updates unfinalized summaries — does NOT finalize them.
    Returns list of summary document names.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.incentive_pay_utils import (
        IncentivePayEngineERPNext,
    )
    from datetime import datetime

    start = datetime.strptime(period_start, "%Y-%m-%d").date()
    end = datetime.strptime(period_end, "%Y-%m-%d").date()
    engine = IncentivePayEngineERPNext(start, end)
    return engine.calculate_period()


@frappe.whitelist()
def finalize_pay_period(period_start, period_end):
    """
    Lock all Operator Pay Period Summaries for the given window.
    Cannot be undone — matches payroll commitment.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.incentive_pay_utils import (
        IncentivePayEngineERPNext,
    )
    from datetime import datetime

    start = datetime.strptime(period_start, "%Y-%m-%d").date()
    end = datetime.strptime(period_end, "%Y-%m-%d").date()
    engine = IncentivePayEngineERPNext(start, end)
    count = engine.finalize_period(closed_by=frappe.session.user)
    return {"finalized": count, "period_label": engine.period_label}


@frappe.whitelist()
def get_operator_pay_history(operator, limit=12):
    """
    Return the last N finalized pay period summaries for an operator.
    Used by the operator's personal dashboard tile.
    """
    return frappe.get_all(
        "Operator Pay Period Summary",
        filters={"operator": operator, "is_finalized": 1},
        fields=[
            "period_label", "pay_period_start", "pay_period_end",
            "total_pieces", "quality_score_pct",
            "base_pay", "quality_bonus", "speed_bonus",
            "defect_penalty", "total_pay",
        ],
        order_by="pay_period_start desc",
        limit_page_length=int(limit),
    )


@frappe.whitelist()
def get_incentive_leaderboard(period_label=None):
    """
    Return ranked operator pay totals for a pay period.
    Displayed on the shop floor supervisor dashboard.
    """
    if not period_label:
        from datetime import date, timedelta
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        iso = monday.isocalendar()
        period_label = "{}-W{:02d}".format(iso[0], iso[1])

    rows = frappe.get_all(
        "Operator Pay Period Summary",
        filters={"period_label": period_label},
        fields=[
            "operator", "total_pieces", "quality_score_pct",
            "total_pay", "is_finalized",
        ],
        order_by="total_pay desc",
    )
    return {"period": period_label, "leaderboard": rows}


# ======================================================================
# Module 2: Line Balancing AI — API endpoints
# ======================================================================

@frappe.whitelist()
def get_floor_balance():
    """
    Live floor balance state — no snapshot persisted.
    Used by the shop floor dashboard for real-time polling.
    """
    from alice_shop_floor.alice_shop_floor.line_balancing_utils import LineBalancingEngine
    return LineBalancingEngine().get_current_balance()


@frappe.whitelist()
def run_line_balance_snapshot():
    """
    Force a snapshot now — persists LineBalanceSnapshot + recommendations.
    Returns the snapshot doc name.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.line_balancing_utils import LineBalancingEngine
    return LineBalancingEngine().run()


@frappe.whitelist()
def get_pending_recommendations():
    """Return all Pending rebalancing recommendations for the supervisor dashboard."""
    return frappe.get_all(
        "Rebalancing Recommendation",
        filters={"status": "Pending"},
        fields=[
            "name", "snapshot", "bottleneck_stage", "donor_stage",
            "suggested_operator", "confidence_score", "reason",
        ],
        order_by="creation desc",
    )


@frappe.whitelist()
def respond_to_recommendation(recommendation, status, notes=None):
    """
    Supervisor accepts or rejects a rebalancing recommendation.
    status must be 'Accepted' or 'Rejected'.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    doc = frappe.get_doc("Rebalancing Recommendation", recommendation)
    doc.respond(status=status, notes=notes)
    return {"status": doc.status, "name": doc.name}


# ===========================================================================
# Decoration Engine API
# POST /api/method/alice_shop_floor.alice_shop_floor.api.<function>
# ===========================================================================

@frappe.whitelist(allow_guest=False)
def decoration_scan_to_print(job_card_name: str) -> dict:
    """
    Scan-to-Print handler.
    Called when operator scans bin QR code at a decoration station.
    Returns all machine parameters needed to start the job.

    Mobile scan page: /alice-decoration-scan?jc=JC-00042
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import scan_to_print
    return scan_to_print(job_card_name)


@frappe.whitelist(allow_guest=False)
def decoration_route_job_card(job_card_name: str, force: int = 0) -> dict:
    """
    Manually trigger routing for a Job Card.
    Use when auto-routing on submit did not fire or needs override.
    """
    from alice_shop_floor.alice_shop_floor.decoration_router import route_job_card
    return route_job_card(job_card_name, force=bool(int(force)))


@frappe.whitelist(allow_guest=False)
def decoration_route_work_order(work_order_name: str, force: int = 0) -> dict:
    """Routes all Job Cards on a Work Order."""
    from alice_shop_floor.alice_shop_floor.decoration_router import route_from_work_order
    return route_from_work_order(work_order_name, force=bool(int(force)))


@frappe.whitelist(allow_guest=False)
def decoration_get_recommendation(
    fabric_type: str,
    design_type: str,
    garment_color: str = None,
    rush: int = 0,
) -> dict:
    """
    Dry-run routing recommendation — no DocType writes.
    Used by the DECORATION panel what-if calculator.
    """
    from alice_shop_floor.alice_shop_floor.decoration_router import get_routing_recommendation
    return get_routing_recommendation(fabric_type, design_type, garment_color, bool(int(rush)))


@frappe.whitelist(allow_guest=False)
def decoration_start_job(job_card_name: str) -> dict:
    """
    Starts a decoration job for the routed method (DTG/DTF/Embroidery).
    Validates all gates (DST approval for EMB, design file present).
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_start_decoration_job
    return api_start_decoration_job(job_card_name)


@frappe.whitelist(allow_guest=False)
def decoration_log_damage(
    job_card: str,
    damage_type: str,
    damage_severity: str,
    damage_description: str = "",
    damage_photo: str = None,
    root_cause_category: str = None,
    corrective_action: str = None,
) -> dict:
    """
    Logs a decoration damage event from the shop floor.
    Auto-triggers replacement blank order for Major/Total Loss damage.
    """
    from alice_shop_floor.alice_shop_floor.doctype.decoration_damage_log.decoration_damage_log import (
        log_decoration_damage,
    )
    return log_decoration_damage(
        job_card=job_card,
        damage_type=damage_type,
        damage_severity=damage_severity,
        damage_description=damage_description,
        damage_photo=damage_photo,
        root_cause_category=root_cause_category,
        corrective_action=corrective_action,
    )


@frappe.whitelist(allow_guest=False)
def digitizing_get_pending(priority: str = None) -> dict:
    """Returns all open DigitizingQueue entries (Submitted/Digitizing/Review)."""
    from alice_shop_floor.alice_shop_floor.doctype.digitizing_queue.digitizing_queue import (
        get_pending_digitizing,
    )
    return get_pending_digitizing(priority=priority)


@frappe.whitelist(allow_guest=False)
def digitizing_approve(queue_name: str, dst_file: str = None) -> dict:
    """Approves a DigitizingQueue entry (Review → Approved)."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.doctype.digitizing_queue.digitizing_queue import (
        approve_digitizing,
    )
    return approve_digitizing(queue_name, dst_file=dst_file)


@frappe.whitelist(allow_guest=False)
def digitizing_release(queue_name: str) -> dict:
    """Releases an Approved DigitizingQueue entry to the machine queue."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.doctype.digitizing_queue.digitizing_queue import (
        release_digitizing,
    )
    return release_digitizing(queue_name)


@frappe.whitelist(allow_guest=False)
def digitizing_reject(queue_name: str, reason: str) -> dict:
    """Rejects a DST submission and increments revision counter."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.doctype.digitizing_queue.digitizing_queue import (
        reject_digitizing,
    )
    return reject_digitizing(queue_name, reason=reason)


@frappe.whitelist(allow_guest=False)
def decoration_queue_summary() -> dict:
    """
    Live decoration queue counts for ALICE OS DECORATION panel.
    Returns DTG/DTF/EMB active jobs + pending digitizing entries.
    """
    from alice_shop_floor.alice_shop_floor.decoration_utils import get_decoration_queue_summary
    return get_decoration_queue_summary()


@frappe.whitelist(allow_guest=False)
def decoration_damage_summary(from_date: str = None, decoration_method: str = None) -> dict:
    """Damage stats summary — by severity, method, and replacement rate."""
    from alice_shop_floor.alice_shop_floor.doctype.decoration_damage_log.decoration_damage_log import (
        get_damage_summary,
    )
    return get_damage_summary(from_date=from_date, decoration_method=decoration_method)


@frappe.whitelist()
def get_operator_skill_scores(stage):
    """
    Return operator skill scores for a given stage.
    Used by the dashboard and alice_core to make informed move decisions.
    """
    from alice_shop_floor.alice_shop_floor.line_balancing_utils import LineBalancingEngine
    return LineBalancingEngine()._score_operators(stage)


@frappe.whitelist()
def get_recent_snapshots(limit=10):
    """Return the last N LineBalanceSnapshots for trend display."""
    return frappe.get_all(
        "Line Balance Snapshot",
        fields=[
            "name", "snapshot_at", "overall_status",
            "active_orders_total", "bottleneck_stage", "recommendation_count",
        ],
        order_by="snapshot_at desc",
        limit_page_length=int(limit),
    )


# ======================================================================
# Module 5: PrintFactory Job Configurator — API endpoints
# ======================================================================

@frappe.whitelist()
def get_orders_ready_for_batching():
    """
    Return WOs at Cutting stage with DXF files that have no batch assigned.
    Used by the dashboard to show what's available to batch.
    """
    from alice_shop_floor.alice_shop_floor.print_job_utils import PrintJobConfigurator
    return PrintJobConfigurator()._get_unbatched_cutting_orders()


@frappe.whitelist()
def auto_batch_cutting_orders():
    """
    ALICE auto-batches all ready cutting orders by fabric lot.
    Returns list of created batch doc names.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.print_job_utils import PrintJobConfigurator
    return PrintJobConfigurator().auto_batch_ready_orders()


@frappe.whitelist()
def submit_print_job_batch(batch_name):
    """
    Build the PrintFactory submission payload for a batch.
    Actual submission to PrintFactory is done by alice_core via REST.
    Returns payload dict.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.print_job_utils import PrintJobConfigurator
    return PrintJobConfigurator().submit_batch(batch_name)


@frappe.whitelist(allow_guest=False)
def printfactory_batch_webhook():
    """
    Receives PrintFactory job completion/failure webhooks for batches.
    PrintFactory POSTs JSON: {job_id, event, efficiency_pct, fabric_length_mm, error_message}
    """
    import json
    data = frappe.request.get_json() or {}
    job_id = data.get("job_id") or data.get("jobId")
    event = data.get("event") or data.get("status") or ""
    if not job_id:
        frappe.throw(frappe._("job_id is required in PrintFactory webhook payload."))

    from alice_shop_floor.alice_shop_floor.print_job_utils import PrintJobConfigurator
    PrintJobConfigurator().handle_webhook(
        job_id=job_id,
        event=event,
        efficiency_pct=data.get("nesting_efficiency_pct"),
        fabric_length_mm=data.get("fabric_length_mm"),
        error_message=data.get("error_message"),
    )
    return {"status": "ok", "job_id": job_id}


@frappe.whitelist()
def get_print_job_batches(status=None, limit=20):
    """Return recent PrintJobBatch records for dashboard display."""
    filters = {}
    if status:
        filters["status"] = status
    return frappe.get_all(
        "Print Job Batch",
        filters=filters,
        fields=[
            "name", "fabric_lot", "status", "printfactory_job_id",
            "order_count", "nesting_efficiency_pct",
            "submitted_at", "completed_at",
        ],
        order_by="creation desc",
        limit_page_length=int(limit),
    )


# ===========================================================================
# Design Studio → ERPNext Bridge
# POST /api/method/alice_shop_floor.alice_shop_floor.api.create_design_job_card
#
# Called by the ZAZFIT Design Studio Shopify App webhooks.orders-create.jsx
# whenever a customer places an order that includes a custom-designed garment.
#
# Flow:
#   Shopify Order → Design Studio webhook → THIS endpoint
#   → Work Order (created if not exists)
#   → Job Card (with all decoration fields)
#   → DecorationRouter (auto-assigns DTG / DTF / Embroidery + recipe)
#   → Returns job_card name back to Design Studio
# ===========================================================================

@frappe.whitelist(allow_guest=False)
def create_design_job_card():
    """
    ZAZFIT Design Studio → ERPNext bridge endpoint.

    Creates (or finds) a Work Order + Job Card for each custom-designed
    garment in a Shopify order. Auto-routes the decoration method via
    the DecorationRouter (DTG / DTF / Embroidery).

    Expected JSON body (from webhooks.orders-create.jsx):
    {
      "shop":             "zazfit.myshopify.com",
      "designId":         "clx_abc123",
      "orderId":          "gid://shopify/Order/12345",
      "lineItemId":       "gid://shopify/LineItem/99999",
      "orderNumber":      1042,
      "productId":        "gid://shopify/Product/7654321",
      "variantId":        "gid://shopify/ProductVariant/44444",
      "variantTitle":     "L / Black",
      "canvasJson":       "{...fabric.js canvas JSON...}",
      "decorationMethod": "DTF",
      "designPlacement":  "Full Front",
      "customerName":     "Alex Kim",
      "customerEmail":    "alex@example.com",
      "thumbnailUrl":     "https://cdn.shopify.com/..."
    }

    Returns:
    {
      "ok": true,
      "job_card": "JC-DECO-00042",
      "work_order": "WO-00099",
      "decoration_method": "DTF",
      "routed": true,
      "recipe": "RECIPE-DTF-00001"
    }
    """
    import json
    from frappe.utils import today, now_datetime

    # ── Parse payload ────────────────────────────────────────────────────────
    # Webhook sends JSON body; direct API calls may send form data
    raw = frappe.request.data
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        data = frappe.form_dict or {}

    # ── Extract fields ───────────────────────────────────────────────────────
    design_id        = data.get("designId")        or data.get("design_id")
    order_id         = data.get("orderId")         or data.get("order_id", "")
    line_item_id     = data.get("lineItemId")      or data.get("line_item_id", "")
    order_number     = str(data.get("orderNumber") or data.get("order_number") or "")
    product_id       = data.get("productId")       or data.get("product_id", "")
    variant_id       = data.get("variantId")       or data.get("variant_id", "")
    variant_title    = data.get("variantTitle")    or data.get("variant_title", "")
    canvas_json      = data.get("canvasJson")      or data.get("canvas_json", "")
    deco_method      = (
        data.get("decorationMethod") or data.get("decoration_method") or "DTF"
    )
    design_placement = (
        data.get("designPlacement") or data.get("design_placement") or "Full Front"
    )
    customer_name    = data.get("customerName")    or data.get("customer_name", "")
    customer_email   = data.get("customerEmail")   or data.get("customer_email", "")
    thumbnail_url    = data.get("thumbnailUrl")    or data.get("thumbnail_url", "")
    shop             = data.get("shop", "")

    if not design_id:
        frappe.throw(_("designId is required"), frappe.ValidationError)

    # Normalise Shopify GIDs (strip everything before the last "/")
    shopify_order_num  = _strip_gid(order_id)
    shopify_line_item  = _strip_gid(line_item_id)
    shopify_product    = _strip_gid(product_id)
    shopify_variant    = _strip_gid(variant_id)

    company = (
        frappe.defaults.get_defaults().get("company")
        or (frappe.get_all("Company", limit=1, pluck="name") or [""])[0]
    )

    # ── 1. Resolve ERPNext Item for this Shopify variant ─────────────────────
    item_code = _resolve_item_for_variant(shopify_product, shopify_variant, variant_title)

    # ── 2. Find or create Work Order ─────────────────────────────────────────
    existing_wo = frappe.db.get_value(
        "Work Order",
        {"shopify_order_id": shopify_order_num, "shopify_line_item_id": shopify_line_item},
        "name",
    ) if shopify_order_num else None

    if existing_wo:
        work_order_name = existing_wo
    else:
        wo = frappe.get_doc({
            "doctype": "Work Order",
            "production_item": item_code,
            "qty": 1,
            "company": company,
            "planned_start_date": today(),
            "expected_delivery_date": today(),
            "description": (
                f"Custom garment — Shopify #{order_number} | "
                f"Variant: {variant_title} | Customer: {customer_name}"
            ),
        })
        wo.insert(ignore_permissions=True)

        # Stamp Shopify reference IDs via custom fields
        frappe.db.set_value("Work Order", wo.name, {
            "shopify_order_id":    shopify_order_num,
            "shopify_line_item_id": shopify_line_item,
        })
        frappe.db.commit()
        work_order_name = wo.name

    # ── 3. Idempotency — check if Job Card already exists for this design ────
    existing_jc = frappe.db.get_value(
        "Job Card",
        {"shopify_design_id": design_id},
        "name",
    )
    if existing_jc:
        frappe.logger().info(
            f"[DesignBridge] Job Card already exists for design {design_id}: {existing_jc}"
        )
        return {
            "ok":           True,
            "job_card":     existing_jc,
            "work_order":   work_order_name,
            "already_exists": True,
        }

    # ── 4. Create Job Card ───────────────────────────────────────────────────
    jc = frappe.get_doc({
        "doctype": "Job Card",
        "work_order": work_order_name,
        "company":    company,
        "posting_date": today(),
    })
    jc.insert(ignore_permissions=True)

    # ── 5. Stamp all decoration + Shopify fields ─────────────────────────────
    update_fields = {
        "decoration_method":    deco_method,
        "design_placement":     design_placement,
        "design_file":          thumbnail_url,
        "shopify_design_id":    design_id,
        "shopify_order_id":     shopify_order_num,
        "shopify_line_item_id": shopify_line_item,
        "shopify_shop":         shop,
        "customer_name":        customer_name,
        "customer_email":       customer_email,
    }
    if canvas_json:
        update_fields["canvas_json"] = canvas_json

    frappe.db.set_value("Job Card", jc.name, update_fields)
    frappe.db.commit()

    # ── 6. Auto-route via DecorationRouter ───────────────────────────────────
    routing_result = {}
    try:
        from alice_shop_floor.alice_shop_floor.decoration_router import route_job_card
        routing_result = route_job_card(jc.name)
    except Exception as exc:
        frappe.logger().warning(
            f"[DesignBridge] DecorationRouter deferred for {jc.name}: {exc}"
        )

    frappe.logger().info(
        f"[DesignBridge] Created Job Card {jc.name} | "
        f"Design={design_id} | WO={work_order_name} | "
        f"method={deco_method} | routed={routing_result.get('ok', False)}"
    )

    return {
        "ok":               True,
        "job_card":         jc.name,
        "work_order":       work_order_name,
        "decoration_method": deco_method,
        "routed":           routing_result.get("ok", False),
        "recipe":           routing_result.get("recipe_name"),
        "winner":           routing_result.get("winner"),
    }


# ---------------------------------------------------------------------------
# Design bridge helpers
# ---------------------------------------------------------------------------

def _strip_gid(gid: str) -> str:
    """Extracts the numeric portion from a Shopify GID.
    'gid://shopify/Order/12345' → '12345'
    """
    if not gid:
        return ""
    return str(gid).split("/")[-1]


def _resolve_item_for_variant(product_id: str, variant_id: str, variant_title: str) -> str:
    """
    Resolves the ERPNext Item Code for a Shopify product variant.

    Priority:
      1. Item with matching shopify_variant_id custom field
      2. Item with matching shopify_product_id custom field
      3. Auto-create a generic blank garment item for this product
    """
    if variant_id:
        item_by_variant = frappe.db.get_value(
            "Item", {"shopify_variant_id": variant_id}, "item_code"
        )
        if item_by_variant:
            return item_by_variant

    if product_id:
        item_by_product = frappe.db.get_value(
            "Item", {"shopify_product_id": product_id}, "item_code"
        )
        if item_by_product:
            return item_by_product

    # Auto-create a placeholder Item so Work Order creation doesn't fail
    generic_code = f"BLANK-{product_id or 'SHOPIFY'}"
    if not frappe.db.exists("Item", generic_code):
        try:
            item = frappe.get_doc({
                "doctype":        "Item",
                "item_code":      generic_code,
                "item_name":      f"Shopify Blank Garment — {variant_title or product_id}",
                "item_group":     "Products",
                "stock_uom":      "Nos",
                "is_stock_item":  1,
                "description":    (
                    f"Auto-created placeholder for Shopify Product {product_id}. "
                    f"Link to actual item via shopify_product_id field."
                ),
            })
            item.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as exc:
            frappe.logger().warning(
                f"[DesignBridge] Could not create placeholder item {generic_code}: {exc}"
            )
            # Last resort — use the first available item in Products group
            fallback = frappe.get_all(
                "Item", filters={"item_group": "Products"}, limit=1, pluck="name"
            )
            return fallback[0] if fallback else "Blank Garment"

    return generic_code


@frappe.whitelist(allow_guest=False)
def get_design_job_status(design_id: str) -> dict:
    """
    Returns the current Job Card status for a Design Studio design ID.
    Called by the Design Studio to poll decoration progress.

    Returns decoration method, routing status, recipe, and workstation.
    """
    if not design_id:
        frappe.throw(_("design_id is required"), frappe.ValidationError)

    jc_name = frappe.db.get_value("Job Card", {"shopify_design_id": design_id}, "name")
    if not jc_name:
        return {"ok": False, "error": "not_found", "design_id": design_id}

    jc = frappe.get_doc("Job Card", jc_name)

    return {
        "ok":               True,
        "job_card":         jc.name,
        "decoration_method": jc.get("decoration_method"),
        "decoration_routed": bool(jc.get("decoration_routed")),
        "production_recipe": jc.get("production_recipe"),
        "design_placement":  jc.get("design_placement"),
        "workstation":       jc.get("workstation"),
        "status":            jc.get("status"),
        "shopify_design_id": design_id,
    }


@frappe.whitelist()
def mark_batch_submitted(batch_name, printfactory_job_id):
    """
    Called by alice_core after successfully submitting a batch to PrintFactory.
    Records the PrintFactory job ID and sets status to Submitted.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    batch = frappe.get_doc("Print Job Batch", batch_name)
    batch.on_submit_to_printfactory(printfactory_job_id)
    return {"status": "Submitted", "batch": batch_name, "job_id": printfactory_job_id}


# ===========================================================================
# Module 6: Operator Efficiency & Skill AI
# ===========================================================================

@frappe.whitelist()
def get_operator_skill_profile(operator, stage):
    """Return the current Operator Skill Profile for one operator+stage pair."""
    from alice_shop_floor.alice_shop_floor.operator_skill_utils import get_skill_profile
    return get_skill_profile(operator, stage)


@frappe.whitelist()
def get_skill_leaderboard(stage=None, limit=20):
    """
    Return top operators by skill score.
    Optional: filter by stage.  Default limit 20.
    """
    from alice_shop_floor.alice_shop_floor.operator_skill_utils import get_skill_leaderboard
    return get_skill_leaderboard(stage=stage, limit=int(limit))


@frappe.whitelist()
def get_training_flags():
    """Return all Operator Skill Profiles where training_flag = 1."""
    from alice_shop_floor.alice_shop_floor.operator_skill_utils import get_training_flags
    return get_training_flags()


@frappe.whitelist()
def update_all_skill_profiles():
    """
    Trigger a full recalculation of all operator skill profiles.
    Restricted to Manufacturing Manager / System Manager.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.operator_skill_utils import update_all_skill_profiles
    return update_all_skill_profiles()


@frappe.whitelist()
def get_operator_performance_trend(operator, stage):
    """Return last 12 weeks of skill history plus current profile summary."""
    from alice_shop_floor.alice_shop_floor.operator_skill_utils import get_performance_trend
    return get_performance_trend(operator, stage)


# ===========================================================================
# V1: Fabric Inspector (Cognex In-Sight 3900)
# ===========================================================================

@frappe.whitelist()
def trigger_fabric_inspection(work_order, fabric_lot, triggered_by=None):
    """
    Create a Pending FabricInspectionResult and return Cognex connection
    config so alice_core can fire the job directly.
    """
    from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import trigger_fabric_inspection
    return trigger_fabric_inspection(work_order, fabric_lot, triggered_by)


@frappe.whitelist(allow_guest=False)
def cognex_fabric_webhook():
    """
    Receive Cognex result payload (called by alice_core after polling).
    Body: { result_name: str, payload: { job_id, defects, ... } }
    """
    import json
    data = frappe.request.get_json() or frappe.local.form_dict
    result_name    = data.get("result_name")
    cognex_payload = data.get("payload") or {}
    if not result_name:
        frappe.throw("result_name is required")
    from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import process_cognex_result
    return process_cognex_result(result_name, cognex_payload)


@frappe.whitelist()
def check_fabric_gate(work_order, fabric_lot):
    """Return whether fabric_lot has a passing inspection (gate open/pending/failed)."""
    from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import check_fabric_pass_gate
    return check_fabric_pass_gate(work_order, fabric_lot)


@frappe.whitelist()
def get_pending_fabric_inspections():
    """Return all Pending FabricInspectionResult records + Cognex config."""
    from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import poll_pending_inspections
    return poll_pending_inspections()


@frappe.whitelist()
def get_fabric_inspection_history(fabric_lot=None, work_order=None, limit=20):
    """Return inspection history for a fabric lot or work order."""
    from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import FabricInspectorEngine
    return FabricInspectorEngine().get_history(
        fabric_lot=fabric_lot, work_order=work_order, limit=int(limit)
    )


@frappe.whitelist()
def fabric_inspection_force_pass(result_name, notes=None):
    """Supervisor override: force a Failed/Errored inspection to Pass."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import FabricInspectorEngine
    return FabricInspectorEngine().force_pass(result_name, notes)


# ===========================================================================
# V2: Inline Stitch QC (Cognex In-Sight 3900) — EXCLUSIVE
# ===========================================================================

@frappe.whitelist()
def trigger_stitch_inspection(work_order, tracker=None, triggered_by=None):
    """Create a Pending StitchInspectionResult and return Cognex config."""
    from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import trigger_stitch_inspection
    return trigger_stitch_inspection(work_order, tracker, triggered_by)


@frappe.whitelist(allow_guest=False)
def cognex_stitch_webhook():
    """Receive Cognex stitch result payload from alice_core."""
    data = frappe.request.get_json() or frappe.local.form_dict
    result_name    = data.get("result_name")
    cognex_payload = data.get("payload") or {}
    if not result_name:
        frappe.throw("result_name is required")
    from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import process_cognex_stitch_result
    return process_cognex_stitch_result(result_name, cognex_payload)


@frappe.whitelist()
def check_stitch_gate(work_order):
    """Return whether work_order has a passing stitch inspection."""
    from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import check_stitch_pass_gate
    return check_stitch_pass_gate(work_order)


@frappe.whitelist()
def get_pending_stitch_inspections():
    """Return all Pending StitchInspectionResult records + Cognex config."""
    from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import poll_pending_stitch_inspections
    return poll_pending_stitch_inspections()


@frappe.whitelist()
def get_stitch_inspection_history(work_order=None, limit=20):
    """Return stitch inspection history for a work order."""
    from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import StitchInspectorEngine
    return StitchInspectorEngine().get_history(work_order=work_order, limit=int(limit))


@frappe.whitelist()
def stitch_inspection_force_pass(result_name, notes=None):
    """Supervisor override: force a Failed/Errored stitch inspection to Pass."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import StitchInspectorEngine
    return StitchInspectorEngine().force_pass(result_name, notes)


# ===========================================================================
# V3: Cut Accuracy Check (Cognex In-Sight 3900) — EXCLUSIVE
# ===========================================================================

@frappe.whitelist()
def trigger_cut_inspection(work_order, fabric_lot=None, triggered_by=None):
    """
    Create a Pending CutInspectionResult and return Cognex connection config
    so alice_core can fire the cut accuracy job directly.
    Gate enforced: Cutting -> Bundling requires a passing result.
    """
    from alice_shop_floor.alice_shop_floor.cut_inspector_utils import trigger_cut_inspection
    return trigger_cut_inspection(work_order, fabric_lot, triggered_by)


@frappe.whitelist(allow_guest=False)
def cognex_cut_webhook():
    """
    Receive Cognex cut accuracy result payload (called by alice_core after polling).
    Body: { result_name: str, payload: { job_id, panels_inspected, deviations, ... } }
    """
    data = frappe.request.get_json() or frappe.local.form_dict
    result_name    = data.get("result_name")
    cognex_payload = data.get("payload") or {}
    if not result_name:
        frappe.throw("result_name is required")
    from alice_shop_floor.alice_shop_floor.cut_inspector_utils import process_cognex_cut_result
    return process_cognex_cut_result(result_name, cognex_payload)


@frappe.whitelist()
def check_cut_gate(work_order):
    """Return whether work_order has a passing cut inspection (gate open/pending/failed)."""
    from alice_shop_floor.alice_shop_floor.cut_inspector_utils import check_cut_pass_gate
    return check_cut_pass_gate(work_order)


@frappe.whitelist()
def get_pending_cut_inspections():
    """Return all Pending CutInspectionResult records + Cognex config."""
    from alice_shop_floor.alice_shop_floor.cut_inspector_utils import poll_pending_cut_inspections
    return poll_pending_cut_inspections()


@frappe.whitelist()
def get_cut_inspection_history(work_order=None, fabric_lot=None, limit=20):
    """Return cut inspection history for a work order or fabric lot."""
    from alice_shop_floor.alice_shop_floor.cut_inspector_utils import CutInspectorEngine
    return CutInspectorEngine().get_history(
        work_order=work_order, fabric_lot=fabric_lot, limit=int(limit)
    )


@frappe.whitelist()
def cut_inspection_force_pass(result_name, notes=None):
    """Supervisor override: force a Failed/Errored cut inspection to Pass."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.cut_inspector_utils import CutInspectorEngine
    return CutInspectorEngine().force_pass(result_name, notes)


# ===========================================================================
# V4: Final Garment Inspector (Cognex In-Sight 3900) — EXCLUSIVE
# ===========================================================================

@frappe.whitelist()
def trigger_final_inspection(work_order, triggered_by=None):
    """
    Create a Pending FinalInspectionResult and return Cognex connection config.
    Gate enforced: Final QC -> Pack requires a passing result.
    """
    from alice_shop_floor.alice_shop_floor.final_inspector_utils import trigger_final_inspection
    return trigger_final_inspection(work_order, triggered_by)


@frappe.whitelist(allow_guest=False)
def cognex_final_webhook():
    """
    Receive Cognex final garment QC result payload from alice_core.
    Body: { result_name: str, payload: { job_id, defects, ... } }
    """
    data = frappe.request.get_json() or frappe.local.form_dict
    result_name    = data.get("result_name")
    cognex_payload = data.get("payload") or {}
    if not result_name:
        frappe.throw("result_name is required")
    from alice_shop_floor.alice_shop_floor.final_inspector_utils import process_cognex_final_result
    return process_cognex_final_result(result_name, cognex_payload)


@frappe.whitelist()
def check_final_gate(work_order):
    """Return whether work_order has a passing final garment inspection."""
    from alice_shop_floor.alice_shop_floor.final_inspector_utils import check_final_pass_gate
    return check_final_pass_gate(work_order)


@frappe.whitelist()
def get_pending_final_inspections():
    """Return all Pending FinalInspectionResult records + Cognex config."""
    from alice_shop_floor.alice_shop_floor.final_inspector_utils import poll_pending_final_inspections
    return poll_pending_final_inspections()


@frappe.whitelist()
def get_final_inspection_history(work_order=None, limit=20):
    """Return final inspection history for a work order."""
    from alice_shop_floor.alice_shop_floor.final_inspector_utils import FinalInspectorEngine
    return FinalInspectorEngine().get_history(work_order=work_order, limit=int(limit))


@frappe.whitelist()
def final_inspection_force_pass(result_name, notes=None):
    """Supervisor override: force a Failed/Errored final inspection to Pass."""
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.final_inspector_utils import FinalInspectorEngine
    return FinalInspectorEngine().force_pass(result_name, notes)


# ===========================================================================
# V5: Defect Intelligence Aggregator — cross-module analytics
# ===========================================================================

@frappe.whitelist()
def generate_defect_intelligence_report(window_days=7, window_label=None):
    """
    Generate a DefectIntelligenceReport aggregating V1-V4 inspection
    results over the given rolling window. Returns the report dict.
    """
    from alice_shop_floor.alice_shop_floor.defect_intelligence_utils import (
        generate_defect_intelligence_report,
    )
    return generate_defect_intelligence_report(
        window_days=int(window_days),
        window_label=window_label or None,
    )


@frappe.whitelist()
def get_latest_defect_intelligence_report():
    """Return the most recently generated DefectIntelligenceReport."""
    from alice_shop_floor.alice_shop_floor.defect_intelligence_utils import (
        get_latest_defect_intelligence_report,
    )
    return get_latest_defect_intelligence_report()


@frappe.whitelist()
def get_defect_trend(stage=None, days=30):
    """
    Return daily defect counts for the given stage (or all stages) over
    the last N days. Used by the shop floor dashboard trend charts.
    """
    from alice_shop_floor.alice_shop_floor.defect_intelligence_utils import get_defect_trend
    return get_defect_trend(stage=stage or None, days=int(days))


# ===========================================================================
# Module 7: Downtime Root-Cause AI
# ===========================================================================

@frappe.whitelist()
def log_downtime_event(stage, started_at, ended_at=None, work_order=None,
                       machine_id=None, operator=None, reported_cause=None,
                       cause_category=None):
    """Log a new downtime event. AI-classifies cause if no category provided."""
    from alice_shop_floor.alice_shop_floor.downtime_utils import log_downtime_event
    return log_downtime_event(
        stage, started_at, ended_at, work_order,
        machine_id, operator, reported_cause, cause_category
    )


@frappe.whitelist()
def resolve_downtime_event(event_name, resolution_notes, ended_at=None):
    """Mark a downtime event as resolved with notes and optional end time."""
    from alice_shop_floor.alice_shop_floor.downtime_utils import resolve_downtime_event
    return resolve_downtime_event(event_name, resolution_notes, ended_at)


@frappe.whitelist()
def classify_downtime_cause(reported_cause, cause_category=None):
    """AI-classify a downtime cause from free text. Returns group + confidence."""
    from alice_shop_floor.alice_shop_floor.downtime_utils import classify_downtime_cause
    return classify_downtime_cause(reported_cause, cause_category)


@frappe.whitelist()
def get_open_downtime_events(stage=None):
    """Return all unresolved downtime events (no ended_at), optionally filtered by stage."""
    from alice_shop_floor.alice_shop_floor.downtime_utils import DowntimeEngine
    return DowntimeEngine().get_open_events(stage=stage or None)


@frappe.whitelist()
def get_downtime_history(stage=None, days=7, limit=50):
    """Return recent downtime events for a stage or across all stages."""
    from alice_shop_floor.alice_shop_floor.downtime_utils import DowntimeEngine
    return DowntimeEngine().get_history(
        stage=stage or None, days=int(days), limit=int(limit)
    )


@frappe.whitelist()
def generate_downtime_report(window_days=7, window_label=None):
    """Generate a DowntimeIntelligenceReport for the given rolling window."""
    from alice_shop_floor.alice_shop_floor.downtime_utils import generate_downtime_report
    return generate_downtime_report(
        window_days=int(window_days), window_label=window_label or None
    )


# ===========================================================================
# Module 9: ESG / Sustainability Reporter
# ===========================================================================

@frappe.whitelist()
def log_esg_metrics(work_order, fabric_lot=None, fabric_used_gsm=None,
                    fabric_ordered_gsm=None, waste_grams=None,
                    water_litres=None, energy_kwh=None, notes=None):
    """Log ESG metrics for a Work Order. Creates or updates the ESGMetricLog."""
    from alice_shop_floor.alice_shop_floor.esg_utils import log_esg_metrics
    return log_esg_metrics(
        work_order, fabric_lot,
        float(fabric_used_gsm)    if fabric_used_gsm    else None,
        float(fabric_ordered_gsm) if fabric_ordered_gsm else None,
        float(waste_grams)        if waste_grams         else None,
        float(water_litres)       if water_litres        else None,
        float(energy_kwh)         if energy_kwh          else None,
        notes,
    )


@frappe.whitelist()
def generate_esg_report(window_days=7, period_label=None):
    """Generate an ESGSummaryReport for the given rolling window."""
    from alice_shop_floor.alice_shop_floor.esg_utils import generate_esg_report
    return generate_esg_report(
        window_days=int(window_days), period_label=period_label or None)


@frappe.whitelist()
def get_latest_esg_report():
    """Return the most recently generated ESGSummaryReport."""
    from alice_shop_floor.alice_shop_floor.esg_utils import get_latest_esg_report
    return get_latest_esg_report()


@frappe.whitelist()
def get_esg_metrics_for_wo(work_order):
    """Return ESG metric log for a specific Work Order."""
    from alice_shop_floor.alice_shop_floor.esg_utils import ESGEngine
    return ESGEngine().get_metrics_for_wo(work_order)


# ===========================================================================
# Module 10: Predictive WIP Bottleneck Detector
# ===========================================================================

@frappe.whitelist()
def get_current_wip():
    """Return live WIP count and congestion score for each stage."""
    from alice_shop_floor.alice_shop_floor.wip_bottleneck_utils import get_current_wip
    return get_current_wip()


@frappe.whitelist()
def run_wip_bottleneck_snapshot():
    """Trigger an immediate WIP snapshot + bottleneck check (normally run by scheduler)."""
    from alice_shop_floor.alice_shop_floor.wip_bottleneck_utils import run_wip_bottleneck_snapshot
    return run_wip_bottleneck_snapshot()


@frappe.whitelist()
def get_open_bottleneck_alerts():
    """Return all unresolved BottleneckAlert records."""
    from alice_shop_floor.alice_shop_floor.wip_bottleneck_utils import get_open_bottleneck_alerts
    return get_open_bottleneck_alerts()


@frappe.whitelist()
def resolve_bottleneck_alert(alert_name):
    """Mark a BottleneckAlert as resolved."""
    from alice_shop_floor.alice_shop_floor.wip_bottleneck_utils import WIPBottleneckEngine
    return WIPBottleneckEngine().resolve_alert(alert_name)


@frappe.whitelist()
def get_wip_snapshots(limit=48):
    """Return recent WIPSnapshot records for dashboard trend display."""
    from alice_shop_floor.alice_shop_floor.wip_bottleneck_utils import WIPBottleneckEngine
    return WIPBottleneckEngine().get_snapshots(limit=int(limit))


# ===========================================================================
# Shop Floor Supervisor Dashboard — Gate Status View
# ===========================================================================

@frappe.whitelist()
def get_active_wo_gates():
    """
    Return a list of all active (non-complete) ProductionStageTracker records,
    each annotated with the gate status for all four visual QC checkpoints:

      fabric_gate  — V1 FabricInspectionResult  (blocks Fabric Inspection → Cutting)
      cut_gate     — V3 CutInspectionResult      (blocks Cutting → Bundling)
      stitch_gate  — V2 StitchInspectionResult   (blocks Sewing → Final QC)
      final_gate   — V4 FinalInspectionResult    (blocks Final QC → Pack)

    Each gate value is one of:
      "open"         — inspection passed / gate clear
      "pending"      — inspection triggered, result not yet received
      "failed"       — inspection returned a failure
      "no_inspection"— no inspection record exists yet
      "N/A"          — gate not yet relevant for this WO's current stage

    Stage order: Fabric Inspection → Cutting → Bundling → Sewing → Final QC → Pack
    """
    import frappe
    from frappe.utils import now_datetime, time_diff_in_seconds

    STAGE_ORDER = [
        "Fabric Inspection",
        "Cutting",
        "Bundling",
        "Sewing",
        "Final QC",
        "Pack",
    ]

    # Stages at which each gate becomes relevant (i.e. the source stage)
    GATE_RELEVANT_FROM = {
        "fabric_gate":  "Fabric Inspection",  # V1 needed to leave Fabric Inspection
        "cut_gate":     "Cutting",             # V3 needed to leave Cutting
        "stitch_gate":  "Sewing",              # V2 needed to leave Sewing
        "final_gate":   "Final QC",            # V4 needed to leave Final QC
    }

    def _stage_index(stage):
        try:
            return STAGE_ORDER.index(stage)
        except ValueError:
            return -1

    def _elapsed_seconds(entered_at):
        if not entered_at:
            return 0
        try:
            return int(time_diff_in_seconds(now_datetime(), entered_at))
        except Exception:
            return 0

    def _get_fabric_gate(work_order, fabric_lot):
        if not fabric_lot:
            return "no_inspection"
        try:
            from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import check_fabric_pass_gate
            result = check_fabric_pass_gate(work_order, fabric_lot)
            return result.get("gate", "no_inspection")
        except Exception:
            return "error"

    def _get_cut_gate(work_order):
        try:
            from alice_shop_floor.alice_shop_floor.cut_inspector_utils import check_cut_pass_gate
            result = check_cut_pass_gate(work_order)
            return result.get("gate", "no_inspection")
        except Exception:
            return "error"

    def _get_stitch_gate(work_order):
        try:
            from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import check_stitch_pass_gate
            result = check_stitch_pass_gate(work_order)
            return result.get("gate", "no_inspection")
        except Exception:
            return "error"

    def _get_final_gate(work_order):
        try:
            from alice_shop_floor.alice_shop_floor.final_inspector_utils import check_final_pass_gate
            result = check_final_pass_gate(work_order)
            return result.get("gate", "no_inspection")
        except Exception:
            return "error"

    # Fetch all non-complete trackers, ordered by stage then entry time
    trackers = frappe.get_all(
        "Production Stage Tracker",
        filters={"is_complete": 0},
        fields=[
            "name",
            "work_order",
            "current_stage",
            "stage_entered_at",
            "fabric_lot",
        ],
        order_by="current_stage asc, stage_entered_at asc",
    )

    rows = []
    for t in trackers:
        wo         = t["work_order"]
        stage      = t["current_stage"] or "Fabric Inspection"
        stage_idx  = _stage_index(stage)
        fabric_lot = t.get("fabric_lot") or ""
        elapsed    = _elapsed_seconds(t.get("stage_entered_at"))

        # Build gate statuses — only compute the gate if the WO has reached
        # or passed the stage at which that gate becomes relevant.
        def gate_value(gate_key, fetch_fn):
            relevant_stage = GATE_RELEVANT_FROM[gate_key]
            relevant_idx   = _stage_index(relevant_stage)
            if stage_idx < relevant_idx:
                return "N/A"            # WO hasn't arrived at this gate yet
            return fetch_fn()

        fabric_gate = gate_value(
            "fabric_gate",
            lambda: _get_fabric_gate(wo, fabric_lot)
        )
        cut_gate    = gate_value(
            "cut_gate",
            lambda wo=wo: _get_cut_gate(wo)
        )
        stitch_gate = gate_value(
            "stitch_gate",
            lambda wo=wo: _get_stitch_gate(wo)
        )
        final_gate  = gate_value(
            "final_gate",
            lambda wo=wo: _get_final_gate(wo)
        )

        # Human-friendly elapsed time string  (e.g. "2h 14m")
        h, rem    = divmod(elapsed, 3600)
        m, _      = divmod(rem, 60)
        time_str  = "{}h {}m".format(h, m) if h else "{}m".format(m)

        rows.append({
            "name":         t["name"],
            "work_order":   wo,
            "current_stage": stage,
            "time_in_stage": time_str,
            "elapsed_seconds": elapsed,
            "fabric_gate":  fabric_gate,
            "cut_gate":     cut_gate,
            "stitch_gate":  stitch_gate,
            "final_gate":   final_gate,
        })

    return rows


# ===========================================================================
# Pattern Studio — Seamly2D Integration
# ===========================================================================

@frappe.whitelist()
def pattern_studio_health():
    """
    Check connectivity to the Seamly2D pattern export service.
    Returns service health JSON or an error dict.
    """
    try:
        from alice_core.pattern_studio import PatternStudioClient
        client = PatternStudioClient.from_frappe_config()
        return client.health()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@frappe.whitelist()
def export_pattern(work_order, fmt="dxf"):
    """
    Export the pattern file for a Work Order via the Seamly2D service.

    Returns the exported file as a Frappe File attachment URL.
    The exported file is saved to the Work Order's private files.

    Args:
        work_order  — Work Order name (e.g. "WO-00042")
        fmt         — Export format: dxf | pdf | svg | png (default: dxf)
    """
    from alice_core.pattern_studio import PatternStudioClient, PatternStudioError

    fmt = (fmt or "dxf").lower()
    if fmt not in ("dxf", "pdf", "svg", "png"):
        frappe.throw(
            "Invalid export format '{}'. Use dxf, pdf, svg or png.".format(fmt)
        )

    try:
        client   = PatternStudioClient.from_frappe_config()
        raw_data = client.export_for_work_order(work_order, fmt=fmt)
    except PatternStudioError as exc:
        frappe.throw(str(exc))

    # Save as a private Frappe file attached to the Work Order
    filename = "{}_pattern.{}".format(work_order.replace("/", "-"), fmt)
    file_doc = frappe.get_doc({
        "doctype":    "File",
        "file_name":  filename,
        "attached_to_doctype": "Work Order",
        "attached_to_name":    work_order,
        "is_private": 1,
        "content":    raw_data,
    })
    file_doc.save(ignore_permissions=True)

    frappe.publish_realtime(
        "pattern_export_complete",
        {
            "work_order": work_order,
            "format":     fmt,
            "file_url":   file_doc.file_url,
        },
        user=frappe.session.user,
    )

    return {
        "work_order": work_order,
        "format":     fmt,
        "file_url":   file_doc.file_url,
        "file_name":  filename,
        "size_bytes": len(raw_data),
    }


@frappe.whitelist()
def get_pattern_export_history(work_order, limit=10):
    """Return list of previously exported pattern files for a Work Order."""
    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Work Order",
            "attached_to_name":    work_order,
            "file_name":           ["like", "%_pattern.%"],
        },
        fields=["name", "file_name", "file_url", "file_size", "creation"],
        order_by="creation desc",
        limit=int(limit),
    )
    return files


# ===========================================================================
# Module 11: Pick-to-Bin — Sewing Station Assignment
# ===========================================================================

@frappe.whitelist()
def run_pick_to_bin():
    """
    Trigger an immediate pick-to-bin sweep.
    Assigns all ready bundles to compatible free stations using BOM machine
    matching + operator skill scoring. Normally runs on the scheduler every
    30 min but supervisors can trigger manually.
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import auto_assign_bins
    return auto_assign_bins()


@frappe.whitelist()
def get_sewing_floor_view(station=None, operator=None):
    """
    Return all active sewing bin assignments for the floor view.
    Optionally filter by station or operator.
    Each row includes: work_order, item, station, operator, status, priority,
    required_machine_type, station_machine_type, machine_match,
    operator_skill_score, elapsed_minutes.
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import get_floor_view
    return get_floor_view(station=station or None, operator=operator or None)


@frappe.whitelist()
def get_pick_to_bin_queue(limit=50):
    """
    Return bundles at Bundling stage with V3 gate passed,
    not yet assigned to a station. Shows required machine type
    so the supervisor can see if a compatible station is available.
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import get_queue
    return get_queue(limit=int(limit))


@frappe.whitelist()
def get_station_summary():
    """
    Per-station summary: machine type, current WO, operator, status,
    elapsed time, machine match flag.
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import get_station_summary
    return get_station_summary()


@frappe.whitelist()
def manually_assign_bin(work_order, station, operator=None, priority="Normal"):
    """
    Supervisor manual override — assign a specific WO to a specific station.
    Logs a machine mismatch warning if the BOM workstation doesn't match
    but does NOT block the assignment.
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import manually_assign
    return manually_assign(
        work_order=work_order,
        station=station,
        operator=operator or None,
        priority=priority,
    )


@frappe.whitelist()
def bin_mark_picked(assignment_name):
    """Sewer physically picks the bin — Queued → Picked."""
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)
    return doc.mark_picked()


@frappe.whitelist()
def bin_mark_in_progress(assignment_name):
    """Sewer starts sewing — Picked → In Progress."""
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)
    return doc.mark_in_progress()


@frappe.whitelist()
def bin_mark_complete(assignment_name):
    """
    Sewer finishes — In Progress → Complete.
    Automatically advances the WO tracker from Bundling to Sewing.
    """
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)
    result = doc.mark_complete()

    # Auto-advance the stage tracker to Sewing now that the bin is complete
    try:
        tracker = frappe.db.get_value(
            "Production Stage Tracker",
            {"work_order": doc.work_order, "is_complete": 0},
            "name",
        )
        if tracker:
            tracker_doc = frappe.get_doc("Production Stage Tracker", tracker)
            if tracker_doc.current_stage == "Bundling":
                tracker_doc.advance_stage(
                    trigger_source="Pick-to-Bin",
                    notes=f"Bundle picked and sewn at station {doc.station} "
                          f"by operator {doc.operator or 'unknown'}",
                )
    except Exception as exc:
        frappe.log_error(
            f"bin_mark_complete: could not auto-advance tracker for WO "
            f"{doc.work_order}: {exc}",
            "Pick-to-Bin Stage Advance"
        )

    return result


@frappe.whitelist()
def bin_mark_returned(assignment_name, reason=""):
    """Return a bundle to queue — e.g. sewer found a problem before starting."""
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)
    return doc.mark_returned(reason=reason or "")


# ===========================================================================
# Cut Bundle — Nesting-Aware Piece Tracker
# ===========================================================================

@frappe.whitelist()
def create_cut_bundle(work_order, fabric_lot=None, total_pieces_expected=0):
    """
    Create a new Cut Bundle record for a Work Order.
    Called by the cutter after nesting is complete and cutting begins.
    """
    existing = frappe.db.exists("Cut Bundle", {"work_order": work_order})
    if existing:
        return frappe.get_doc("Cut Bundle", existing).as_dict()

    doc = frappe.get_doc({
        "doctype":               "Cut Bundle",
        "work_order":            work_order,
        "fabric_lot":            fabric_lot or "",
        "total_pieces_expected": int(total_pieces_expected or 0),
        "bundle_status":         "Incomplete",
    })
    doc.insert(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
def record_cut_piece(work_order, piece_name, fabric_zone, shade_ref="",
                     nest_x_mm=None, nest_y_mm=None, cut_status="Cut", notes=""):
    """
    Record a single cut piece on the bundle.
    Call once per piece as the cutter works through the nest.
    fabric_zone: position on the roll, e.g. 'A1', 'B3', 'Edge-Left'.
    shade_ref:   dye-lot shade code from the fabric roll inspection.
    """
    bundle_name = frappe.db.exists("Cut Bundle", {"work_order": work_order})
    if not bundle_name:
        frappe.throw(
            f"No Cut Bundle found for Work Order '{work_order}'. "
            "Create the bundle first via create_cut_bundle."
        )

    doc = frappe.get_doc("Cut Bundle", bundle_name)
    doc.append("pieces", {
        "piece_name":    piece_name,
        "fabric_zone":   fabric_zone,
        "shade_ref":     shade_ref or "",
        "nest_x_mm":     float(nest_x_mm) if nest_x_mm is not None else None,
        "nest_y_mm":     float(nest_y_mm) if nest_y_mm is not None else None,
        "cut_status":    cut_status,
        "notes":         notes or "",
    })
    doc.save(ignore_permissions=True)

    # Fire realtime so the floor UI can update piece count live
    frappe.publish_realtime("bundle_piece_added", {
        "work_order":    work_order,
        "bundle":        doc.name,
        "piece_name":    piece_name,
        "fabric_zone":   fabric_zone,
        "shade_ref":     shade_ref,
        "bundle_status": doc.bundle_status,
        "pieces_cut":    doc.total_pieces_cut,
        "pieces_expected": doc.total_pieces_expected,
    })

    return {
        "bundle":          doc.name,
        "bundle_status":   doc.bundle_status,
        "pieces_cut":      doc.total_pieces_cut,
        "pieces_expected": doc.total_pieces_expected,
        "shade_zones":     doc.shade_zones_count,
        "shade_mismatch":  doc.shade_mismatch_detail or "",
    }


@frappe.whitelist()
def get_cut_bundle(work_order):
    """Return the Cut Bundle for a Work Order, including all pieces."""
    bundle_name = frappe.db.exists("Cut Bundle", {"work_order": work_order})
    if not bundle_name:
        return None
    doc = frappe.get_doc("Cut Bundle", bundle_name)
    return doc.as_dict()


@frappe.whitelist()
def supervisor_clear_bundle_shade(work_order, notes=""):
    """
    Manufacturing Manager clears a shade warning or mismatch so the
    bundle can proceed to sewing.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    bundle_name = frappe.db.exists("Cut Bundle", {"work_order": work_order})
    if not bundle_name:
        frappe.throw(f"No Cut Bundle for Work Order '{work_order}'")
    doc = frappe.get_doc("Cut Bundle", bundle_name)
    return doc.supervisor_clear_shade(notes=notes or "")


@frappe.whitelist()
def get_bundle_readiness(work_order):
    """
    Check whether a bundle is ready for sewing bin assignment.
    Returns {ready: bool, reason: str, bundle_status: str, shade_zones: int}
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import (
        _bundle_ready_for_sewing,
    )
    ready, reason = _bundle_ready_for_sewing(work_order)

    bundle_name = frappe.db.exists("Cut Bundle", {"work_order": work_order})
    extras = {}
    if bundle_name:
        doc = frappe.get_doc("Cut Bundle", bundle_name)
        extras = {
            "bundle_status":     doc.bundle_status,
            "shade_zones":       doc.shade_zones_count,
            "pieces_cut":        doc.total_pieces_cut,
            "pieces_expected":   doc.total_pieces_expected,
            "shade_mismatch":    doc.shade_mismatch_detail or "",
            "supervisor_cleared": doc.supervisor_cleared,
        }

    return {"ready": ready, "reason": reason, **extras}


# ══════════════════════════════════════════════════════════════════════════════
#  BARCODE SCAN SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def scan_bin_action(barcode, operator=None):
    """
    Smart scan endpoint called by the mobile sewing-bin-scan page.

    Given a barcode value (= SewingBinAssignment.name), determine
    the correct next action from the current status and execute it.

    Status machine:
      Queued      → mark_picked      (sewer picks bin from rack)
      Picked      → mark_in_progress (sewer sits at machine)
      In Progress → mark_complete    (sewing done)
      Complete    → no-op, return info
      Returned    → no-op, return info

    Returns:
      {
        ok:           bool,
        assignment:   str,
        work_order:   str,
        production_item: str,
        station_code: str,
        operator:     str,
        prev_status:  str,
        new_status:   str,
        action_taken: str,   e.g. "picked" | "started" | "completed" | "none"
        message:      str,   human-readable result for the scan UI
        bundle_shade_status: dict,
      }
    """
    # ── Lookup ────────────────────────────────────────────────────────────────
    asgn_name = frappe.db.get_value(
        "Sewing Bin Assignment", {"bin_barcode": barcode}, "name"
    )
    if not asgn_name:
        # Fallback: treat barcode as the doc name directly
        asgn_name = frappe.db.exists("Sewing Bin Assignment", barcode)
    if not asgn_name:
        frappe.throw(f"No bin assignment found for barcode: {barcode}", frappe.DoesNotExistError)

    doc = frappe.get_doc("Sewing Bin Assignment", asgn_name)
    prev_status = doc.status

    # Optionally override operator if scanned from a specific terminal
    if operator and doc.status == "Queued" and not doc.operator:
        doc.operator = operator
        doc.save(ignore_permissions=True)

    action_taken = "none"
    message      = ""

    if doc.status == "Queued":
        doc.mark_picked()
        action_taken = "picked"
        message = f"✓ Picked — take bundle to station {_station_code(doc.station)}"

    elif doc.status == "Picked":
        doc.mark_in_progress()
        action_taken = "started"
        message = f"✓ Sewing started at station {_station_code(doc.station)}"

    elif doc.status == "In Progress":
        doc.mark_complete()
        action_taken = "completed"
        message = "✓ Bundle complete — Work Order advanced to Sewing stage"
        # Replicate auto-advance logic from bin_mark_complete endpoint
        try:
            tracker = frappe.db.get_value(
                "Production Stage Tracker",
                {"work_order": doc.work_order, "is_complete": 0},
                "name",
            )
            if tracker:
                tracker_doc = frappe.get_doc("Production Stage Tracker", tracker)
                if tracker_doc.current_stage == "Bundling":
                    tracker_doc.advance_stage(
                        trigger_source="Barcode Scan",
                        notes=f"Scanned complete at station {doc.station}",
                    )
        except Exception as exc:
            frappe.log_error(
                f"scan_bin_action: stage advance failed for {doc.work_order}: {exc}",
                "Scan Bin Stage Advance",
            )

    elif doc.status == "Complete":
        message = "This bundle is already complete."

    elif doc.status == "Returned":
        message = "This bundle was returned. Re-assign via the floor view."

    else:
        message = f"Status is '{doc.status}' — no action taken."

    # Shade status for the scan result card
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import PickToBinEngine
    shade = PickToBinEngine()._get_bundle_shade_status(doc.work_order)

    return {
        "ok":                  True,
        "assignment":          doc.name,
        "work_order":          doc.work_order,
        "production_item":     doc.production_item or "",
        "station_code":        _station_code(doc.station),
        "operator":            doc.operator or "",
        "prev_status":         prev_status,
        "new_status":          doc.status,
        "action_taken":        action_taken,
        "message":             message,
        "priority":            doc.priority,
        "machine_match":       doc.machine_match,
        "bundle_shade_status": shade,
    }


def _station_code(station_name):
    """Return the station_code for display (falls back to station name)."""
    if not station_name:
        return ""
    code = frappe.db.get_value("Sewing Station", station_name, "station_code")
    return code or station_name


@frappe.whitelist()
def get_bin_label(assignment_name):
    """
    Return printable HTML label for a SewingBinAssignment.

    The label includes:
      • QR code (assignment name = barcode value)
      • Work Order, Item, Priority
      • Station code + Machine type
      • Operator
      • Fabric lot
      • V3 cut gate status
      • Bundle shade status

    The returned HTML is self-contained (inline CSS, no external deps)
    so it can be opened in a print dialog directly.
    """
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)

    # Generate QR code as base64 PNG
    try:
        import qrcode, io, base64
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=2,
        )
        qr.add_data(doc.bin_barcode or doc.name)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        qr_html = f'<img src="data:image/png;base64,{qr_b64}" width="120" height="120" alt="QR">'
    except Exception:
        qr_html = f'<div style="font-size:9px;word-break:break-all;">{doc.name}</div>'

    # Shade badge
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import PickToBinEngine
    shade = PickToBinEngine()._get_bundle_shade_status(doc.work_order) or {}
    shade_status = shade.get("status", "")
    if shade_status == "Complete":
        shade_html = '<span style="color:#065f46;">✓ Bundle complete</span>'
    elif shade_status == "Shade Mismatch" and not shade.get("cleared"):
        shade_html = '<span style="color:#991b1b;">⚠ SHADE MISMATCH</span>'
    elif shade_status in ("Shade Mismatch", "Shade Warning"):
        shade_html = f'<span style="color:#92400e;">⚠ Multi-zone ({shade.get("zones","")} zones) — cleared</span>'
    elif shade_status == "Incomplete":
        shade_html = f'<span style="color:#6b7280;">{shade.get("pieces_cut","?")} / {shade.get("pieces_expected","?")} pieces cut</span>'
    else:
        shade_html = '<span style="color:#6b7280;">—</span>'

    priority_color = {"Rush": "#c00", "High": "#e07800"}.get(doc.priority or "", "#333")
    station_code   = _station_code(doc.station)
    machine        = frappe.db.get_value("Sewing Station", doc.station, "machine_type") if doc.station else ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bin Label — {doc.name}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 0; padding: 0; }}
  .label {{
    width: 3.5in; padding: 10px 12px;
    border: 2px solid #333; margin: 10px auto;
    page-break-after: always;
  }}
  .label-header {{
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 8px;
  }}
  .label-title  {{ font-size: 20px; font-weight: 800; color: #111; }}
  .label-priority {{ font-size: 14px; font-weight: 700; color: {priority_color}; }}
  .qr-block     {{ text-align: center; margin: 6px 0; }}
  .field-row    {{ display: flex; font-size: 11px; margin: 3px 0; }}
  .field-label  {{ width: 90px; color: #666; flex-shrink: 0; }}
  .field-value  {{ font-weight: 600; color: #111; }}
  .shade-row    {{ margin-top: 6px; font-size: 11px; }}
  .footer       {{ font-size: 9px; color: #aaa; margin-top: 8px; text-align: center; }}
  @media print {{ body {{ margin: 0; }} .label {{ border: 2px solid #000; }} }}
</style>
</head>
<body>
<div class="label">
  <div class="label-header">
    <div>
      <div class="label-title">{station_code or "BIN"}</div>
      <div class="label-priority">{doc.priority or "Normal"}</div>
    </div>
    <div class="qr-block">{qr_html}</div>
  </div>

  <div class="field-row">
    <span class="field-label">Work Order:</span>
    <span class="field-value">{doc.work_order or ""}</span>
  </div>
  <div class="field-row">
    <span class="field-label">Item:</span>
    <span class="field-value">{doc.production_item or ""}</span>
  </div>
  <div class="field-row">
    <span class="field-label">Machine:</span>
    <span class="field-value">{machine or "—"}</span>
  </div>
  <div class="field-row">
    <span class="field-label">Operator:</span>
    <span class="field-value">{doc.operator or "Unassigned"}</span>
  </div>
  <div class="field-row">
    <span class="field-label">Fabric Lot:</span>
    <span class="field-value">{doc.fabric_lot or "—"}</span>
  </div>
  <div class="field-row">
    <span class="field-label">V3 Gate:</span>
    <span class="field-value">{doc.cut_inspection_status or "—"}</span>
  </div>
  <div class="shade-row">
    <span style="color:#666;">Bundle: </span>{shade_html}
  </div>
  <div class="footer">{doc.name} · ALICE Shop Floor</div>
</div>
<script>window.onload = () => window.print();</script>
</body>
</html>"""

    # Mark label as printed
    frappe.db.set_value(
        "Sewing Bin Assignment", assignment_name,
        "label_printed", 1, update_modified=False,
    )

    return {"html": html, "assignment": assignment_name}


# ══════════════════════════════════════════════════════════════════════════════
#  SEWER THROUGHPUT PACING
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_sewing_pace_dashboard():
    """
    Return pace data for all operators active today.
    Sorted Critical → Behind → On Track → Ahead → No Target.

    Each entry:
      operator, shift, target_wos, wos_completed, wos_in_progress, wos_queued,
      avg_sew_min, remaining_shift_min, projected_total, projected_pct,
      pace_status, shift_start_str, shift_end_str
    """
    from alice_shop_floor.alice_shop_floor.pace_engine import get_floor_pace_summary
    return get_floor_pace_summary()


@frappe.whitelist()
def get_operator_pace(operator):
    """Return pace detail for a single operator (current shift)."""
    from alice_shop_floor.alice_shop_floor.pace_engine import get_operator_pace as _get
    return _get(operator)


@frappe.whitelist()
def set_shift_target(operator, target_wos, shift=None, shift_date=None,
                     warn_pct=80, critical_pct=60):
    """
    Create or update a ShiftProductionTarget for an operator.
    Can be called from the floor view supervisor panel or a script.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    from alice_shop_floor.alice_shop_floor.pace_engine import upsert_shift_target
    name = upsert_shift_target(
        operator=operator,
        target_wos=int(target_wos),
        shift=shift or None,
        shift_date=shift_date or None,
        warn_pct=float(warn_pct),
        critical_pct=float(critical_pct),
    )
    return {"name": name, "ok": True}


@frappe.whitelist()
def get_rebalance_suggestions():
    """
    Return WO reassignment suggestions for Critical-paced sewers.

    Each suggestion:
      work_order, assignment, from_operator, from_station,
      to_operator, to_station, reason
    """
    from alice_shop_floor.alice_shop_floor.pace_engine import get_rebalance_suggestions as _get
    return _get()


@frappe.whitelist()
def apply_rebalance(assignment, to_station, supervisor_note=""):
    """
    Execute a rebalance suggestion — move assignment to a different station/operator.
    Sets assignment_method = Manual and updates station + operator.
    """
    frappe.only_for(["Manufacturing Manager", "System Manager"])
    doc = frappe.get_doc("Sewing Bin Assignment", assignment)
    if doc.status not in ("Queued",):
        frappe.throw("Can only rebalance a Queued assignment.")

    station_doc = frappe.get_doc("Sewing Station", to_station)
    doc.station             = to_station
    doc.operator            = station_doc.default_operator or doc.operator
    doc.assignment_method   = "Manual"
    doc.notes               = ((doc.notes or "") +
                                f"\nRebalanced by supervisor. {supervisor_note}").strip()
    doc.save(ignore_permissions=True)
    frappe.publish_realtime("bin_assigned", {
        "assignment": doc.name,
        "work_order": doc.work_order,
        "station":    doc.station,
        "operator":   doc.operator,
        "note":       "Rebalanced for pace",
    })
    return {"ok": True, "assignment": doc.name, "new_station": to_station}


# ═══════════════════════════════════════════════════════════════════════════════
# SEWING INSTRUCTIONS — multi-language API
# ═══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_bin_instructions(assignment_name: str, language_code: str = "en") -> dict:
    """
    Return the SewingInstructionSet (steps + photo) for the WO attached to
    this bin assignment, served in the operator's preferred language.

    Returns:
    {
      set_name, item, work_order, garment_photo, notes,
      steps: [{sequence, piece_type, stitch_type, machine_setting,
               step_photo, instruction_text}],
      language_used,
    }
    Falls back to English if requested language has no translation.
    """
    from alice_shop_floor.alice_shop_floor.translator import (
        get_step_text, get_operator_language,
    )

    asgn = frappe.db.get_value(
        "Sewing Bin Assignment", assignment_name,
        ["work_order", "operator"], as_dict=True,
    )
    if not asgn:
        return {}

    # Resolve language: explicit param > operator profile > en
    lang = language_code
    if lang == "en" and asgn.get("operator"):
        lang = get_operator_language(asgn["operator"])

    wo = asgn["work_order"]

    # Look for WO-level override first, then Item template
    set_name = frappe.db.get_value(
        "Sewing Instruction Set",
        {"work_order": wo, "is_wo_override": 1},
        "name",
    )
    if not set_name:
        item = frappe.db.get_value("Work Order", wo, "production_item")
        set_name = frappe.db.get_value(
            "Sewing Instruction Set",
            {"item": item},
            "name",
        )
    if not set_name:
        return {"work_order": wo, "steps": [], "language_used": lang,
                "garment_photo": None, "notes": ""}

    set_doc = frappe.get_doc("Sewing Instruction Set", set_name)

    # Translate notes if needed
    notes = set_doc.notes or ""
    if notes and lang != "en":
        from alice_shop_floor.alice_shop_floor.translator import translate_text
        notes = translate_text(notes, lang)

    steps = []
    for step in set_doc.steps or []:
        step_dict = step.as_dict()
        steps.append({
            "sequence":       step.sequence,
            "piece_type":     step.piece_type or "",
            "stitch_type":    step.stitch_type or "",
            "machine_setting": step.machine_setting or "",
            "step_photo":     step.step_photo or "",
            "instruction_text": get_step_text(step_dict, lang),
        })

    return {
        "set_name":     set_doc.name,
        "item":         set_doc.item or "",
        "work_order":   set_doc.work_order or wo,
        "garment_photo": set_doc.garment_photo or "",
        "notes":        notes,
        "steps":        steps,
        "language_used": lang,
    }


@frappe.whitelist()
def trigger_retranslate(set_name: str) -> dict:
    """Force re-translation of all steps in a SewingInstructionSet."""
    frappe.enqueue(
        "alice_shop_floor.alice_shop_floor.translator.translate_instruction_set_all",
        queue="short",
        timeout=120,
        set_name=set_name,
    )
    return {"queued": True, "set_name": set_name}


@frappe.whitelist()
def get_operator_language_code(operator: str = None) -> str:
    """Return the preferred_language for the current or given operator."""
    from alice_shop_floor.alice_shop_floor.translator import get_operator_language
    user = operator or frappe.session.user
    return get_operator_language(user)


@frappe.whitelist()
def get_active_languages() -> list:
    """Return active language list from ALICE Settings."""
    from alice_shop_floor.alice_shop_floor.doctype.alice_settings.alice_settings import (
        get_active_languages as _get,
    )
    return _get()


# ============================================================================
# Task #44 — Picker Kitting System
# ============================================================================

@frappe.whitelist()
def get_pick_assignment(assignment_name: str) -> dict:
    """
    Return full pick assignment data for the picker tablet.
    Includes bin details, work order context, and annotated pick list
    with rack/slot from the linked Piece Storage Location.
    """
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)

    # Resolve location details for each pick row
    rows = []
    for r in doc.pick_list or []:
        loc_label = ""
        if r.storage_location:
            loc_label = frappe.db.get_value(
                "Piece Storage Location", r.storage_location, "location_label"
            ) or f"{r.rack or ''}-{r.slot or ''}"
        rows.append({
            "idx": r.idx,
            "piece_type": r.piece_type,
            "storage_location": r.storage_location,
            "rack": r.rack or "",
            "slot": r.slot or 0,
            "location_label": loc_label,
            "qty_required": r.qty_required,
            "qty_picked": r.qty_picked or 0,
            "status": r.status,
            "picked_at": str(r.picked_at) if r.picked_at else None,
        })

    return {
        "name": doc.name,
        "work_order": doc.work_order,
        "production_item": doc.production_item,
        "station": doc.station,
        "operator": doc.operator,
        "status": doc.status,
        "kit_status": doc.kit_status or "Not Started",
        "priority": doc.priority,
        "bin_barcode": doc.bin_barcode,
        "pick_list": rows,
        "total": len(rows),
        "picked": sum(1 for r in rows if r["status"] == "Picked"),
        "short": sum(1 for r in rows if r["status"] == "Short"),
    }


@frappe.whitelist()
def confirm_piece_pick(assignment_name: str, idx: int, qty_picked: float = None) -> dict:
    """
    Confirm a single piece as picked.
    idx: the child-table idx (1-based) of the Bin Pick List Item row.
    qty_picked: defaults to qty_required for that row.
    Returns updated kit_status and whether all pieces are now done.
    """
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)

    # Default qty to required
    if qty_picked is None:
        row = next((r for r in doc.pick_list if r.idx == int(idx)), None)
        if row:
            qty_picked = row.qty_required

    result = doc.confirm_piece(int(idx), float(qty_picked or 1), frappe.session.user)
    return result


@frappe.whitelist()
def mark_piece_short(assignment_name: str, idx: int) -> dict:
    """Mark a pick-list piece as Short (couldn't be found on rack)."""
    doc = frappe.get_doc("Sewing Bin Assignment", assignment_name)
    doc.mark_piece_short(int(idx), frappe.session.user)
    doc.reload()
    return {
        "kit_status": doc.kit_status,
        "status": doc.status,
        "message": "Piece marked as Short. Supervisor alert fired.",
    }


@frappe.whitelist()
def get_queued_assignments_for_picker() -> list:
    """
    Return all assignments in Queued or Kitting status, ordered by priority then assigned_at.
    Used on the picker tablet home screen to show what needs kitting.
    """
    priority_order = {"Rush": 0, "High": 1, "Normal": 2}

    rows = frappe.get_all(
        "Sewing Bin Assignment",
        filters={"status": ["in", ["Queued", "Kitting"]]},
        fields=[
            "name", "work_order", "production_item", "station",
            "operator", "status", "kit_status", "priority",
            "bin_barcode", "assigned_at",
        ],
        order_by="assigned_at asc",
        limit=100,
    )

    # Enrich with pick progress
    for row in rows:
        pick_rows = frappe.get_all(
            "Bin Pick List Item",
            filters={"parent": row["name"]},
            fields=["status"],
        )
        row["total_pieces"] = len(pick_rows)
        row["picked_pieces"] = sum(1 for r in pick_rows if r["status"] == "Picked")
        row["short_pieces"] = sum(1 for r in pick_rows if r["status"] == "Short")

    # Sort: Rush → High → Normal, then assigned_at
    rows.sort(key=lambda r: (priority_order.get(r["priority"], 2), r["assigned_at"] or ""))
    return rows


@frappe.whitelist()
def find_storage_locations_for_item(item_code: str) -> list:
    """
    Return active Piece Storage Locations that hold this item and have qty > 0.
    Used when a supervisor is building a pick list for a new assignment.
    """
    return frappe.get_all(
        "Piece Storage Location",
        filters={
            "item": item_code,
            "is_active": 1,
            "qty_available": [">", 0],
        },
        fields=["name", "rack", "slot", "location_label", "dye_lot", "qty_available"],
        order_by="rack asc, slot asc",
    )


# ============================================================================
# Task #45 — Roll-to-Trace Solid Cut Entry
# ============================================================================

@frappe.whitelist()
def create_solid_cut_log(work_order: str, fabric_item: str = None, rolls: list = None) -> dict:
    """
    Create a new Solid Fabric Cut Log for a Work Order.
    rolls: list of dicts [{roll_id, dye_lot, yardage_used, piece_types_cut, roll_notes}]
    Returns the created log name and bridge detection result.
    """
    import json
    if isinstance(rolls, str):
        rolls = json.loads(rolls)

    doc = frappe.new_doc("Solid Fabric Cut Log")
    doc.work_order    = work_order
    doc.fabric_item   = fabric_item or None
    doc.cut_date      = frappe.utils.today()
    doc.cut_by        = frappe.session.user
    doc.status        = "Draft"

    for r in (rolls or []):
        doc.append("rolls", {
            "roll_id":          r.get("roll_id", ""),
            "dye_lot":          r.get("dye_lot", ""),
            "yardage_used":     r.get("yardage_used") or 0,
            "piece_types_cut":  r.get("piece_types_cut", ""),
            "roll_notes":       r.get("roll_notes", ""),
        })

    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "name": doc.name,
        "status": doc.status,
        "dye_lot_bridge_detected": doc.dye_lot_bridge_detected,
        "total_rolls_used": doc.total_rolls_used,
    }


@frappe.whitelist()
def add_roll_to_cut_log(log_name: str, roll_id: str, dye_lot: str,
                         yardage_used: float = 0, piece_types_cut: str = "",
                         roll_notes: str = "") -> dict:
    """Append a new roll row to an existing Solid Fabric Cut Log."""
    doc = frappe.get_doc("Solid Fabric Cut Log", log_name)
    if doc.status == "Confirmed":
        frappe.throw("Cannot add rolls to a Confirmed cut log.")
    doc.append("rolls", {
        "roll_id": roll_id,
        "dye_lot": dye_lot,
        "yardage_used": yardage_used,
        "piece_types_cut": piece_types_cut,
        "roll_notes": roll_notes,
    })
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "name": doc.name,
        "status": doc.status,
        "dye_lot_bridge_detected": doc.dye_lot_bridge_detected,
        "total_rolls_used": doc.total_rolls_used,
        "rolls": [
            {"roll_id": r.roll_id, "dye_lot": r.dye_lot,
             "yardage_used": r.yardage_used, "piece_types_cut": r.piece_types_cut}
            for r in doc.rolls
        ],
    }


@frappe.whitelist()
def confirm_cut_log(log_name: str) -> dict:
    """Confirm a Solid Fabric Cut Log (non-bridge path)."""
    doc = frappe.get_doc("Solid Fabric Cut Log", log_name)
    doc.confirm()
    frappe.db.commit()
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def override_bridge_confirm(log_name: str, supervisor_notes: str) -> dict:
    """Supervisor override — confirm despite dye-lot bridge."""
    doc = frappe.get_doc("Solid Fabric Cut Log", log_name)
    doc.override_bridge_confirm(supervisor_notes)
    frappe.db.commit()
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def get_cut_log(log_name: str) -> dict:
    """Return full cut log data for the solid cut entry tablet."""
    doc = frappe.get_doc("Solid Fabric Cut Log", log_name)
    return {
        "name": doc.name,
        "work_order": doc.work_order,
        "production_item": doc.production_item,
        "fabric_item": doc.fabric_item,
        "cut_date": str(doc.cut_date) if doc.cut_date else None,
        "cut_by": doc.cut_by,
        "status": doc.status,
        "dye_lot_bridge_detected": doc.dye_lot_bridge_detected,
        "total_rolls_used": doc.total_rolls_used,
        "rolls": [
            {
                "idx": r.idx,
                "roll_id": r.roll_id,
                "dye_lot": r.dye_lot,
                "yardage_used": r.yardage_used or 0,
                "piece_types_cut": r.piece_types_cut or "",
                "roll_notes": r.roll_notes or "",
            }
            for r in doc.rolls
        ],
        "notes": doc.notes or "",
        "supervisor_notes": doc.supervisor_notes or "",
    }


@frappe.whitelist()
def get_open_cut_logs_for_wo(work_order: str) -> list:
    """Return Draft and Bridge Alert cut logs for a Work Order."""
    return frappe.get_all(
        "Solid Fabric Cut Log",
        filters={"work_order": work_order, "status": ["in", ["Draft", "Bridge Alert"]]},
        fields=["name", "status", "cut_date", "total_rolls_used", "dye_lot_bridge_detected"],
        order_by="cut_date desc",
    )


# ============================================================
# V6: Press QC Inspector
# ============================================================

@frappe.whitelist()
def trigger_press_inspection(
    work_order: str,
    press_temperature_c: float = 0.0,
    press_pressure_bar: float = 0.0,
    dwell_time_sec: float = 0.0,
) -> dict:
    """
    Trigger a Press QC inspection for a Work Order.
    Creates a PressInspectionLog (Pending) and enqueues the Cognex poll.
    Returns the new log name.
    """
    from alice_shop_floor.alice_shop_floor.press_inspector import trigger_press_inspection as _trigger
    log_name = _trigger(
        work_order=work_order,
        press_temperature_c=float(press_temperature_c),
        press_pressure_bar=float(press_pressure_bar),
        dwell_time_sec=float(dwell_time_sec),
    )
    return {"name": log_name}


@frappe.whitelist()
def get_press_inspection_log(log_name: str) -> dict:
    """Return Press Inspection Log data for the shop floor tablet."""
    doc = frappe.get_doc("Press Inspection Log", log_name)
    return {
        "name": doc.name,
        "work_order": doc.work_order,
        "production_item": getattr(doc, "production_item", ""),
        "overall_result": doc.overall_result,
        "confidence_score": doc.confidence_score or 0,
        "press_temperature_c": doc.press_temperature_c or 0,
        "press_pressure_bar": doc.press_pressure_bar or 0,
        "dwell_time_sec": doc.dwell_time_sec or 0,
        "defect_count_scorch": doc.defect_count_scorch or 0,
        "defect_count_shine": doc.defect_count_shine or 0,
        "defect_count_crease": doc.defect_count_crease or 0,
        "defect_count_burn": doc.defect_count_burn or 0,
        "image_capture": doc.image_capture or "",
        "error_message": getattr(doc, "error_message", "") or "",
        "supervisor_override": doc.supervisor_override or 0,
        "supervisor_notes": doc.supervisor_notes or "",
        "defect_map": [
            {
                "defect_type": d.defect_type,
                "severity": d.severity,
                "zone": d.zone or "",
                "confidence_score": d.confidence_score or 0,
                "x_mm": d.x_mm or 0,
                "y_mm": d.y_mm or 0,
                "image_ref": d.image_ref or "",
            }
            for d in (doc.defect_map or [])
        ],
    }


@frappe.whitelist()
def get_press_inspection_logs_for_wo(work_order: str) -> list:
    """Return recent Press Inspection Logs for a Work Order (last 20)."""
    return frappe.get_all(
        "Press Inspection Log",
        filters={"work_order": work_order},
        fields=[
            "name", "overall_result", "confidence_score",
            "defect_count_scorch", "defect_count_shine",
            "defect_count_crease", "defect_count_burn",
            "supervisor_override", "creation",
        ],
        order_by="creation desc",
        limit=20,
    )


@frappe.whitelist()
def supervisor_override_press(log_name: str, supervisor_notes: str) -> dict:
    """Supervisor override — accept a failed Press Inspection."""
    from alice_shop_floor.alice_shop_floor.press_inspector import (
        supervisor_override_press as _override,
    )
    _override(log_name=log_name, notes=supervisor_notes)
    frappe.db.commit()
    return {"name": log_name, "status": "overridden"}


# ===========================================================================
# Machine Driver Layer — Equipment Communication API
# ===========================================================================

@frappe.whitelist(allow_guest=False)
def machine_ping(machine_config_name: str) -> dict:
    """
    Ping a machine and return latency + online status.
    Also updates last_ping_at and last_ping_status on MachineConfig.

    Args:
        machine_config_name — MachineConfig document name

    Returns:
        {"ok": bool, "latency_ms": float, "driver": str}
    """
    from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry

    driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
    result = driver.ping()

    # Update ping fields on MachineConfig
    from frappe.utils import now_datetime
    frappe.db.set_value("Machine Config", machine_config_name, {
        "last_ping_at":     now_datetime(),
        "last_ping_status": "Online" if result.get("ok") else "Offline",
    })
    frappe.db.commit()

    return {**result, "machine_config": machine_config_name}


@frappe.whitelist(allow_guest=False)
def machine_get_status(machine_config_name: str) -> dict:
    """
    Return the current operational state of a machine.

    Returns:
        {"ok": bool, "state": "Idle"|"Printing"|"Error"|"Offline", "detail": dict}
    """
    from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry

    driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
    return {**driver.get_status(), "machine_config": machine_config_name}


@frappe.whitelist(allow_guest=False)
def machine_send_job(job_card_name: str, machine_config_name: str = None) -> dict:
    """
    Send a decoration job to a machine.
    Delegates to decoration_engine.start_dtg/dtf/emb_job based on
    the Job Card's decoration_method.

    Args:
        job_card_name       — ERPNext Job Card name
        machine_config_name — override; if omitted uses default machine for method

    Returns:
        {"ok": bool, "machine_job_id": str, "method": "rest_api"|"hot_folder"|"ftp", ...}
    """
    method = frappe.db.get_value("Job Card", job_card_name, "decoration_method")
    if not method:
        frappe.throw(_("Job Card has no decoration_method set"), frappe.ValidationError)

    from alice_shop_floor.alice_shop_floor.decoration_engine import (
        start_dtg_job, start_dtf_job, start_emb_job,
    )
    from alice_shop_floor.alice_shop_floor.decoration_utils import DecoMethod

    dispatch = {
        DecoMethod.DTG: start_dtg_job,
        DecoMethod.DTF: start_dtf_job,
        DecoMethod.EMB: start_emb_job,
    }
    handler = dispatch.get(method)
    if not handler:
        frappe.throw(
            _(f"Unknown decoration method '{method}' on Job Card {job_card_name}"),
            frappe.ValidationError,
        )
    return handler(job_card_name, machine_config_name=machine_config_name)


@frappe.whitelist(allow_guest=False)
def machine_get_job_status(machine_config_name: str, machine_job_id: str) -> dict:
    """
    Poll the status of a submitted job on a machine.

    Args:
        machine_config_name — MachineConfig document name
        machine_job_id      — machine-native job ID returned by machine_send_job

    Returns:
        {"ok": bool, "state": "Queued"|"Printing"|"Complete"|"Error", "detail": dict}
    """
    from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry

    driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
    return {**driver.get_job_status(machine_job_id), "machine_config": machine_config_name}


@frappe.whitelist(allow_guest=False)
def machine_cancel_job(machine_config_name: str, machine_job_id: str) -> dict:
    """
    Cancel a queued or in-progress job on a machine.

    Returns:
        {"ok": bool, "machine_job_id": str}
    """
    from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry

    driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
    return {**driver.cancel_job(machine_job_id), "machine_config": machine_config_name}


@frappe.whitelist(allow_guest=False)
def machine_list(decoration_method: str = None) -> list:
    """
    Return all active MachineConfig records, optionally filtered by decoration_method.
    Includes last ping status and driver type.

    Args:
        decoration_method — "DTF" | "DTG" | "Embroidery" (optional filter)

    Returns:
        List of machine dicts with name, decoration_method, driver_type,
        is_default, last_ping_status, last_ping_at, total_jobs_sent.
    """
    filters = {"is_active": 1}
    if decoration_method:
        filters["decoration_method"] = decoration_method

    machines = frappe.get_all(
        "Machine Config",
        filters=filters,
        fields=[
            "name", "machine_name", "decoration_method", "driver_type",
            "is_default", "host", "last_ping_status", "last_ping_at",
            "last_job_sent_at", "total_jobs_sent",
        ],
        order_by="decoration_method asc, is_default desc, machine_name asc",
    )
    return machines


@frappe.whitelist(allow_guest=False)
def machine_ping_all() -> dict:
    """
    Ping all active machines and return a summary.
    Called by the scheduled task (every 5 min) and manually from the
    Machine Config list view action button.

    Returns:
        {"pinged": int, "online": int, "offline": int, "results": [...]}
    """
    from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry
    from frappe.utils import now_datetime

    machines = frappe.get_all(
        "Machine Config",
        filters={"is_active": 1},
        fields=["name", "decoration_method", "driver_type"],
    )

    results   = []
    online    = 0
    offline   = 0

    for mc_ref in machines:
        try:
            driver = MachineDriverRegistry.get_driver_by_name(mc_ref["name"])
            ping   = driver.ping()
            ok     = bool(ping.get("ok"))

            frappe.db.set_value("Machine Config", mc_ref["name"], {
                "last_ping_at":     now_datetime(),
                "last_ping_status": "Online" if ok else "Offline",
            })

            if ok:
                online += 1
            else:
                offline += 1

            results.append({
                "machine": mc_ref["name"],
                "ok":      ok,
                "latency_ms": ping.get("latency_ms"),
                "driver":  ping.get("driver"),
            })
        except Exception as e:
            offline += 1
            results.append({
                "machine": mc_ref["name"],
                "ok":      False,
                "error":   str(e),
            })
            frappe.logger().error(f"[machine_ping_all] {mc_ref['name']}: {e}")

    frappe.db.commit()

    return {
        "pinged":  len(machines),
        "online":  online,
        "offline": offline,
        "results": results,
    }


# ---------------------------------------------------------------------------
# DTG Print Station endpoints  (Task #66)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def dtg_scan_and_load(job_card_name: str) -> dict:
    """DTG Print Station scan - loads Job Card with DTG-specific params.
    Returns platen size, pretreat flag, ink profile, cure settings,
    garment color, Epson hot-folder machine list, and certified operators.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtg_scan_and_load
    return api_dtg_scan_and_load(job_card_name)


@frappe.whitelist(allow_guest=False)
def dtg_start_print(job_card_name: str, machine_config_name: str = None,
                    operator_employee: str = None) -> dict:
    """Sends the DTG print file to the selected Epson F2270/F3070 via the hot folder.
    Returns {"ok": bool, "machine_job_id": "HF-JC-xxxxx", "method": "hot_folder"}
    operator_employee: Employee link — stamped on the Job Card for quality tracking.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtg_start_print
    return api_dtg_start_print(job_card_name, machine_config_name, operator_employee)


@frappe.whitelist(allow_guest=False)
def dtg_print_status(job_card_name: str) -> dict:
    """Polls DTG print status via hot folder file presence.
    States: NotSent | Queued | Complete | Error
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtg_print_status
    return api_dtg_print_status(job_card_name)


@frappe.whitelist(allow_guest=False)
def dtg_print_complete(
    job_card_name: str,
    operator_employee: str = None,
    defect_count: int = 0,
    rework_flag: int = 0,
    defect_notes: str = "",
    defect_types: str = "",
) -> dict:
    """Operator confirms garment printed and sent to cure tunnel.
    Stamps dtg_complete_at and operator, fires OperatorQualityLog.
    defect_count / rework_flag / defect_notes / defect_types: quality outcome fields.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtg_print_complete
    return api_dtg_print_complete(
        job_card_name, operator_employee,
        defect_count=int(defect_count or 0),
        rework_flag=bool(rework_flag),
        defect_notes=defect_notes or "",
        defect_types=defect_types or "",
    )


@frappe.whitelist(allow_guest=False)
def dtg_pretreat_confirmed(job_card_name: str) -> dict:
    """Operator confirms pretreatment applied for a dark garment before printing.
    Must be called before dtg_start_print when pretreat_required=True.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtg_pretreat_confirmed
    return api_dtg_pretreat_confirmed(job_card_name)


# ---------------------------------------------------------------------------
# Embroidery Station endpoints  (Task #67)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def emb_scan_and_load(job_card_name: str) -> dict:
    """Embroidery Station scan - loads Job Card with EMB-specific params.
    Returns DST gate status (Pending/Approved/Released), thread color sequence
    with needle assignments, stitch count, hoop size, available_machines list,
    and certified_operators list.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_emb_scan_and_load
    return api_emb_scan_and_load(job_card_name)


@frappe.whitelist(allow_guest=False)
def emb_start_job(job_card_name: str, machine_config_name: str = None,
                  operator_employee: str = None) -> dict:
    """Sends embroidery DST file to the selected Melco Summit head via FTP.
    Enforces DST gate - rejects if DigitizingQueue is not Approved or Released.
    Returns {"ok": bool, "machine_job_id": "FTP-JC-xxxxx", "method": "ftp"}
    operator_employee: Employee link — stamped on the Job Card for quality tracking.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_emb_start_job
    return api_emb_start_job(job_card_name, machine_config_name, operator_employee)


@frappe.whitelist(allow_guest=False)
def emb_job_status(job_card_name: str) -> dict:
    """Polls embroidery job status via FTP file presence on the Melco.
    States: NotSent | Queued | Complete | Error
    File present on FTP = Queued. File absent = Complete (SUMMIT Manager consumed it).
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_emb_job_status
    return api_emb_job_status(job_card_name)


@frappe.whitelist(allow_guest=False)
def emb_job_complete(
    job_card_name: str,
    operator_employee: str = None,
    defect_count: int = 0,
    rework_flag: int = 0,
    defect_notes: str = "",
    defect_types: str = "",
) -> dict:
    """Operator confirms embroidery done, garment unhooped and visually inspected.
    Stamps emb_complete_at and operator, fires OperatorQualityLog for rolling defect tracking.
    defect_count / rework_flag / defect_notes / defect_types: quality outcome fields.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_emb_job_complete
    return api_emb_job_complete(
        job_card_name, operator_employee,
        defect_count=int(defect_count or 0),
        rework_flag=bool(rework_flag),
        defect_notes=defect_notes or "",
        defect_types=defect_types or "",
    )


# ---------------------------------------------------------------------------
# DTF Print Station endpoints  (Task #65)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def dtf_scan_and_load(job_card_name: str) -> dict:
    """DTF Print Station scan - loads Job Card with DTF-specific params.
    Returns film width, resolution, color mode, white ink, peel type,
    design file URL, and Epson G6070 hot-folder machine status.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtf_scan_and_load
    return api_dtf_scan_and_load(job_card_name)


@frappe.whitelist(allow_guest=False)
def dtf_start_print(job_card_name: str, machine_config_name: str = None) -> dict:
    """Sends the DTF print file to the Epson G6070 via the hot folder.
    Returns {"ok": bool, "machine_job_id": "HF-JC-xxxxx", "method": "hot_folder"}
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtf_start_print
    return api_dtf_start_print(job_card_name, machine_config_name)




@frappe.whitelist(allow_guest=False)
def dtf_print_status(job_card_name: str) -> dict:
    """Polls DTF print status via hot folder file presence on the G6070.
    States: NotSent | Queued | Complete | Error
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtf_print_status
    return api_dtf_print_status(job_card_name)


@frappe.whitelist(allow_guest=False)
def dtf_film_ready(job_card_name: str) -> dict:
    """Operator confirms DTF film is printed and sent to the dryer.
    Stamps dtf_film_ready_at and operator, advances Job Card to press queue.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtf_film_ready
    return api_dtf_film_ready(job_card_name)


@frappe.whitelist(allow_guest=False)
def dtf_press_scan_and_load(job_card_name: str) -> dict:
    """DTF Press Station scan — loads Job Card with validated press params.
    Returns press_temp_f, dwell_time_sec, pressure_psi, peel_type, pre_press_sec,
    plus available PneumaticPress machines and DTF-certified operators.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtf_press_scan_and_load
    return api_dtf_press_scan_and_load(job_card_name)


@frappe.whitelist(allow_guest=False)
def dtf_press_complete(
    job_card_name: str,
    operator_employee: str = None,
    defect_count: int = 0,
    rework_flag: int = 0,
    defect_notes: str = "",
    defect_types: str = "",
) -> dict:
    """Operator confirms DTF press transfer complete — garment pressed and peeled.
    Advances Job Card to Press QC, triggers PressInspectionLog (V6), and fires
    OperatorQualityLog for rolling defect-rate tracking.
    operator_employee: Employee link — stamped on Job Card.
    defect_count / rework_flag / defect_notes / defect_types: quality outcome fields.
    """
    from alice_shop_floor.alice_shop_floor.decoration_engine import api_dtf_press_complete
    return api_dtf_press_complete(
        job_card_name,
        operator_employee=operator_employee,
        defect_count=int(defect_count or 0),
        rework_flag=bool(rework_flag),
        defect_notes=defect_notes or "",
        defect_types=defect_types or "",
    )


# ---------------------------------------------------------------------------
# Operator Quality — stats, leaderboard, supervisor flag  (Task #76)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def get_operator_quality_stats_v2(
    employee: str,
    decoration_method: str = None,
    window: int = 30,
) -> dict:
    """Returns rolling quality stats for a single operator.
    Includes defect_rate, rework_rate, avg_cycle_time_sec, quality_score, job_count.
    decoration_method: optional filter (DTG / DTF / Embroidery). All methods if omitted.
    window: number of recent jobs to include (default 30).
    """
    from alice_shop_floor.alice_shop_floor.operator_quality_utils import get_operator_quality_stats
    return get_operator_quality_stats(employee, decoration_method, window)


@frappe.whitelist(allow_guest=False)
def get_quality_leaderboard(
    decoration_method: str = None,
    window: int = 30,
    limit: int = 20,
) -> list:
    """Returns quality leaderboard — operators ranked by quality_score desc.
    Each entry: employee, employee_name, quality_score, defect_rate, rework_rate,
                avg_cycle_time_sec, job_count, decoration_method.
    decoration_method: optional filter. All methods if omitted.
    window: rolling window in jobs (default 30). limit: max rows (default 20).
    """
    from alice_shop_floor.alice_shop_floor.operator_quality_utils import get_quality_leaderboard
    return get_quality_leaderboard(decoration_method, window, limit)


@frappe.whitelist(allow_guest=False)
def flag_operator_for_review(
    employee: str,
    decoration_method: str,
    reason: str,
) -> dict:
    """Supervisor flags an operator for quality review.
    Appends a note to SkillProfileHistory and publishes operator_flagged_for_review
    realtime event to the supervisor dashboard.
    Requires Shop Floor Supervisor role.
    """
    from alice_shop_floor.alice_shop_floor.operator_quality_utils import flag_operator_for_review as _flag
    return _flag(employee, decoration_method, reason, flagged_by=frappe.session.user)


# ===========================================================================
# Task #77: Work Order → ProductionRecipe → Decoration Job Card scheduling
# ===========================================================================

@frappe.whitelist(allow_guest=False)
def create_decoration_job_cards(work_order_name: str) -> dict:
    """
    Manually trigger decoration Job Card creation for a submitted Work Order.

    Idempotent — safe to call multiple times; returns the existing Job Card
    if one was already created.

    work_order_name: ERPNext Work Order name (e.g. "WO-00042").

    Returns:
    {
      "ok": true,
      "already_existed": false,
      "job_card": "JC-00099",
      "work_order": "WO-00042",
      "decoration_method": "DTF"
    }
    """
    from alice_shop_floor.alice_shop_floor.work_order_scheduler import (
        create_decoration_job_cards as _create,
    )
    return _create(work_order_name)


@frappe.whitelist(allow_guest=False)
def get_work_order_deco_status(work_order_name: str) -> dict:
    """
    Return the decoration scheduling status for a Work Order.

    Returns:
    {
      "ok": true,
      "work_order": "WO-00042",
      "production_recipe": "RECIPE-DTF-00001",
      "decoration_method": "DTF",
      "deco_jc_created": true,
      "deco_job_card": "JC-00099",
      "job_card_status": "Open",
      "decoration_routed": true
    }
    """
    if not work_order_name:
        frappe.throw(_("work_order_name is required"), frappe.ValidationError)

    wo_fields = frappe.db.get_value(
        "Work Order",
        work_order_name,
        ["production_recipe", "decoration_method", "deco_jc_created", "deco_job_card"],
        as_dict=True,
    )
    if not wo_fields:
        frappe.throw(
            _(f"Work Order {work_order_name} not found"),
            frappe.DoesNotExistError,
        )

    jc_status = None
    jc_routed = False
    if wo_fields.get("deco_job_card"):
        jc_row = frappe.db.get_value(
            "Job Card",
            wo_fields["deco_job_card"],
            ["status", "decoration_routed"],
            as_dict=True,
        )
        if jc_row:
            jc_status = jc_row.status
            jc_routed = bool(jc_row.decoration_routed)

    return {
        "ok":                True,
        "work_order":        work_order_name,
        "production_recipe": wo_fields.get("production_recipe") or "",
        "decoration_method": wo_fields.get("decoration_method") or "",
        "deco_jc_created":   bool(wo_fields.get("deco_jc_created")),
        "deco_job_card":     wo_fields.get("deco_job_card") or "",
        "job_card_status":   jc_status or "",
        "decoration_routed": jc_routed,
    }


# ===========================================================================
# Task #78: Pattern sizing — SizeStream / FitModel / resolve_vit / export
# ===========================================================================

@frappe.whitelist(allow_guest=False)
def get_size_streams() -> list:
    """
    Return all active SizeStream records with their size rows.

    Used by the Work Order form to populate the size_stream picker
    and the size_code dropdown dynamically.

    Returns list of:
    {
      "name": "ASTM D5585 Women",
      "standard": "ASTM D5585",
      "unit": "cm",
      "sizes": [
        {"size_code": "XS", "size_label": "Extra Small", "bust_cm": 84.0, ...},
        ...
      ]
    }
    """
    streams = frappe.get_all(
        "Size Stream",
        filters={"is_active": 1},
        fields=["name", "stream_name", "standard", "unit"],
        order_by="stream_name asc",
    )
    result = []
    for s in streams:
        rows = frappe.get_all(
            "Size Stream Row",
            filters={"parent": s["name"], "parenttype": "Size Stream"},
            fields=[
                "size_code", "size_label",
                "bust_cm", "waist_cm", "hip_cm",
                "inseam_cm", "rise_cm", "thigh_cm",
                "shoulder_cm", "sleeve_cm",
                "neck_cm", "chest_cm",
                "back_length_cm", "front_length_cm",
            ],
            order_by="idx asc",
        )
        result.append({
            "name":     s["name"],
            "standard": s["standard"] or "",
            "unit":     s["unit"] or "cm",
            "sizes":    rows,
        })
    return result


@frappe.whitelist(allow_guest=False)
def get_fit_models() -> list:
    """
    Return all active FitModel records.

    Returns list of:
    {
      "name": "House Model A",
      "reference_size_code": "M",
      "bust_cm": 92.0,
      "waist_cm": 72.0,
      ...
    }
    """
    return frappe.get_all(
        "Fit Model",
        filters={"is_active": 1},
        fields=[
            "name", "model_name", "reference_size_code",
            "bust_cm", "waist_cm", "hip_cm",
            "inseam_cm", "rise_cm", "thigh_cm",
            "shoulder_cm", "sleeve_cm",
            "neck_cm", "chest_cm",
            "back_length_cm", "front_length_cm",
            "notes",
        ],
        order_by="model_name asc",
    )


@frappe.whitelist(allow_guest=False)
def resolve_vit_preview(work_order_name: str) -> dict:
    """
    Dry-run resolve_vit() and return the .vit XML as a string for debugging.

    Useful during sizing setup to verify the correct measurements are being
    picked up before a real Seamly2D export.

    Returns:
    {
      "ok": true,
      "work_order": "WO-00042",
      "pattern_mode": "MTM",
      "vit_xml": "<?xml version=\"1.0\" ...>"
    }
    """
    from alice_core.pattern_studio import resolve_vit, PatternStudioError
    try:
        vit_bytes = resolve_vit(work_order_name)
        mode = frappe.db.get_value("Work Order", work_order_name, "pattern_mode") or "MTM"
        return {
            "ok":           True,
            "work_order":   work_order_name,
            "pattern_mode": mode,
            "vit_xml":      vit_bytes.decode("utf-8"),
        }
    except PatternStudioError as exc:
        return {
            "ok":    False,
            "error": str(exc),
            "work_order": work_order_name,
        }


@frappe.whitelist(allow_guest=False)
def export_pattern(
    work_order_name: str,
    fmt: str = "dxf",
) -> dict:
    """
    Full pattern export pipeline for a Work Order.

    Resolves .vit from the WO's pattern_mode (MTM / Graded / FitModel),
    then exports via the Seamly2D VPS service.

    fmt: dxf | pdf | svg | png  (default dxf)

    Returns:
    {
      "ok": true,
      "work_order": "WO-00042",
      "pattern_mode": "Graded",
      "size_code": "M",
      "fmt": "dxf",
      "file_url": "/private/files/WO-00042_pattern.dxf"
    }
    """
    from alice_core.pattern_studio import (
        export_pattern_for_wo_v2,
        PatternStudioError,
    )
    import os, tempfile

    if fmt not in ("dxf", "pdf", "svg", "png"):
        frappe.throw(
            _(f"Unsupported format '{fmt}'. Use dxf, pdf, svg, or png."),
            frappe.ValidationError,
        )

    try:
        dxf_bytes = export_pattern_for_wo_v2(work_order_name, fmt=fmt)
    except PatternStudioError as exc:
        return {"ok": False, "error": str(exc), "work_order": work_order_name}

    # Save to private files
    filename = f"{work_order_name}_pattern.{fmt}"
    file_doc = frappe.get_doc({
        "doctype":   "File",
        "file_name": filename,
        "content":   dxf_bytes,
        "is_private": 1,
        "attached_to_doctype": "Work Order",
        "attached_to_name":    work_order_name,
    })
    file_doc.insert(ignore_permissions=True)

    mode     = frappe.db.get_value("Work Order", work_order_name, "pattern_mode") or "MTM"
    size_code = frappe.db.get_value("Work Order", work_order_name, "size_code") or ""

    return {
        "ok":           True,
        "work_order":   work_order_name,
        "pattern_mode": mode,
        "size_code":    size_code,
        "fmt":          fmt,
        "file_url":     file_doc.file_url,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Task #79 — ALICE OS Screen: single aggregated payload for wall-mounted TV
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=False)
def get_os_screen_data():
    """
    Returns a single JSON payload for the ALICE OS wall display:
      - work_orders      : list of active WOs with stage, flags, customer
      - pipeline         : unit count per stage
      - bottlenecks      : stages with open bottleneck alerts
      - machines         : all Machine Configs with live status
      - alerts           : recent alert log entries for the ticker
      - pace_leaders     : top-5 operators by pace % this shift
      - header stats     : totals derived from the above
    """

    # ── Active Work Orders ────────────────────────────────────────────────────
    wo_records = frappe.db.sql("""
        SELECT
            wo.name,
            wo.customer_name,
            wo.qty,
            wo.status,
            pst.current_stage,
            pst.has_qc_flag,
            pst.is_stalled,
            CASE WHEN jc.name IS NOT NULL AND jc.decoration_routed = 0
                 THEN 1 ELSE 0 END AS no_deco_routed,
            CASE WHEN di.name IS NOT NULL THEN 1 ELSE 0 END AS has_defect
        FROM `tabWork Order` wo
        LEFT JOIN `tabProduction Stage Tracker` pst ON pst.work_order = wo.name
        LEFT JOIN `tabJob Card` jc
            ON jc.work_order = wo.name
           AND jc.status NOT IN ('Completed','Cancelled')
           AND jc.decoration_routed = 0
        LEFT JOIN `tabOperator Quality Log` di
            ON di.work_order = wo.name
           AND di.defect_severity IN ('Critical','Major')
           AND di.creation >= DATE_SUB(NOW(), INTERVAL 8 HOUR)
        WHERE wo.status IN ('In Process','Not Started')
        GROUP BY wo.name
        ORDER BY pst.is_stalled DESC, wo.creation ASC
        LIMIT 60
    """, as_dict=True)

    work_orders = []
    for w in wo_records:
        work_orders.append({
            "name":          w.name,
            "customer_name": w.customer_name or "",
            "qty":           w.qty or 0,
            "current_stage": w.current_stage or "—",
            "has_qc_flag":   bool(w.has_qc_flag),
            "is_stalled":    bool(w.is_stalled),
            "no_deco_routed": bool(w.no_deco_routed),
            "has_defect":    bool(w.has_defect),
        })

    # ── WIP pipeline — units per stage ───────────────────────────────────────
    pipeline_rows = frappe.db.sql("""
        SELECT current_stage AS stage, COUNT(*) AS count
        FROM `tabProduction Stage Tracker`
        WHERE current_stage IS NOT NULL AND current_stage != ''
          AND status NOT IN ('Completed','Cancelled')
        GROUP BY current_stage
    """, as_dict=True)
    pipeline = [{"stage": r.stage, "count": r.count} for r in pipeline_rows]

    # ── Open bottleneck alerts ────────────────────────────────────────────────
    bn_rows = frappe.db.sql("""
        SELECT stage, COUNT(*) AS count
        FROM `tabBottleneck Alert`
        WHERE status = 'Open'
        GROUP BY stage
        ORDER BY count DESC
        LIMIT 8
    """, as_dict=True)
    bottlenecks = [{"stage": r.stage, "count": r.count} for r in bn_rows]

    # ── Machine configs + last ping status ───────────────────────────────────
    machine_rows = frappe.db.get_all(
        "Machine Config",
        filters={"is_active": 1},
        fields=["name", "machine_type", "last_ping_status", "last_ping_at",
                "current_operator"],
        order_by="machine_type asc",
    )

    machines = []
    for m in machine_rows:
        # Count active Job Cards assigned to this machine
        active_jobs = frappe.db.count("Job Card", {
            "workstation": m.name,
            "status": ["in", ["Work In Progress", "Open"]],
        })
        status = m.last_ping_status or "Unknown"
        machines.append({
            "name":         m.name,
            "machine_type": m.machine_type or "",
            "status":       status,
            "operator":     m.current_operator or "",
            "active_jobs":  active_jobs,
        })

    # ── Recent alerts for ticker (last 4 hours) ───────────────────────────────
    alert_rows = frappe.db.sql("""
        SELECT message, alert_type, creation
        FROM `tabALICE Alert Log`
        WHERE creation >= DATE_SUB(NOW(), INTERVAL 4 HOUR)
        ORDER BY creation DESC
        LIMIT 30
    """, as_dict=True)

    alert_type_cls = {
        "Critical":    "tick-alert",
        "Warning":     "tick-warn",
        "Info":        "",
    }
    alerts = [{
        "text": r.message or "",
        "cls":  alert_type_cls.get(r.alert_type, ""),
    } for r in alert_rows] if alert_rows else []

    # ── Pace leaders (top 5 by pace % this shift) ────────────────────────────
    # Pull from Sewing Operator Log for the last 8 hours
    pace_rows = frappe.db.sql("""
        SELECT
            sol.operator AS operator_name,
            ROUND(
                100.0 * SUM(sol.pieces_completed)
                / NULLIF(SUM(sol.pieces_target), 0)
            , 0) AS pace_pct
        FROM `tabSewing Operator Log` sol
        WHERE sol.log_date >= DATE_SUB(NOW(), INTERVAL 8 HOUR)
          AND sol.pieces_target > 0
        GROUP BY sol.operator
        ORDER BY pace_pct DESC
        LIMIT 5
    """, as_dict=True)
    pace_leaders = [{"operator_name": r.operator_name, "pace_pct": int(r.pace_pct or 0)}
                    for r in pace_rows]

    # ── Header totals ─────────────────────────────────────────────────────────
    total_wo         = len(work_orders)
    total_units_in_wip = sum(w["qty"] for w in work_orders)
    bottleneck_count = len(bottlenecks)
    alert_count      = len([a for a in alerts if a["cls"] in ("tick-alert", "tick-warn")])
    machines_online  = sum(1 for m in machines if m["status"] == "Online")

    return {
        "work_orders":       work_orders,
        "pipeline":          pipeline,
        "bottlenecks":       bottlenecks,
        "machines":          machines,
        "alerts":            alerts,
        "pace_leaders":      pace_leaders,
        "total_wo":          total_wo,
        "total_units_in_wip": total_units_in_wip,
        "bottleneck_count":  bottleneck_count,
        "alert_count":       alert_count,
        "machines_online":   machines_online,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SanMar Integration API
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def sanmar_test_connection():
    """Test SanMar API credentials. Called from SanMar Config form."""
    from alice_shop_floor.alice_shop_floor.sanmar.client import SanMarClient, SanMarConfigMissing
    try:
        config = frappe.get_single("SanMar Config")
        client = SanMarClient.from_config(config)
        ok, msg = client.ping()
        return {"ok": ok, "message": msg}
    except SanMarConfigMissing as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"Unexpected error: {e}"}


@frappe.whitelist()
def sanmar_check_stock(sanmar_sku: str, force_live: bool = False):
    """
    Live stock check for a single SanMar SKU.
    Used by the WO form and picking UI before confirming a blank is available.
    """
    from alice_shop_floor.alice_shop_floor.sanmar.stock_lookup import check_sku
    from alice_shop_floor.alice_shop_floor.sanmar.client import SanMarConfigMissing, SanMarAPIError
    try:
        result = check_sku(sanmar_sku, force_live=bool(force_live))
        return result.to_dict()
    except SanMarConfigMissing as e:
        return {"ok": False, "message": str(e), "total_qty": 0, "status": "Unknown"}
    except SanMarAPIError as e:
        return {"ok": False, "message": str(e), "total_qty": 0, "status": "Unknown"}


@frappe.whitelist()
def sanmar_check_style(style_number: str, color_name: str = None, fit_code: str = None):
    """
    Return inventory for a style (optionally filtered).
    Used by the catalog browser and WO blank picker.
    """
    from alice_shop_floor.alice_shop_floor.sanmar.stock_lookup import check_style
    from alice_shop_floor.alice_shop_floor.sanmar.client import SanMarConfigMissing, SanMarAPIError
    try:
        results = check_style(style_number, color_name=color_name, fit_code=fit_code)
        return [r.to_dict() for r in results]
    except (SanMarConfigMissing, SanMarAPIError) as e:
        return {"ok": False, "message": str(e)}


@frappe.whitelist()
def sanmar_sync_catalog():
    """Manually trigger a SanMar catalog sync (runs in background)."""
    frappe.enqueue(
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_catalog_sync",
        queue="long",
        timeout=600,
        job_name="sanmar_catalog_sync_manual",
        is_async=True,
    )
    return {"ok": True, "message": "Catalog sync queued — check SanMar Config for status."}


@frappe.whitelist()
def sanmar_sync_pricing():
    """Manually trigger a SanMar pricing sync (runs in background)."""
    frappe.enqueue(
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_pricing_sync",
        queue="long",
        timeout=300,
        job_name="sanmar_pricing_sync_manual",
        is_async=True,
    )
    return {"ok": True, "message": "Pricing sync queued."}


@frappe.whitelist()
def sanmar_create_po_for_work_order(work_order_name: str,
                                    submit_to_sanmar: bool = False):
    """
    Manually create a SanMar PO for a Work Order.
    submit_to_sanmar=True will push directly to SanMar's PO API.
    """
    from alice_shop_floor.alice_shop_floor.sanmar.po_creator import create_po_for_work_order
    return create_po_for_work_order(
        work_order_name,
        submit_to_sanmar=bool(submit_to_sanmar),
    )


@frappe.whitelist()
def sanmar_get_style_map(style_number: str = None, is_active: bool = True):
    """
    Return SanMar Style Map entries, optionally filtered by style.
    Used by the WO blank picker and catalog browser UI.
    """
    filters = {"is_active": 1} if is_active else {}
    if style_number:
        filters["sanmar_style"] = style_number

    rows = frappe.db.get_all(
        "SanMar Style Map",
        filters=filters,
        fields=[
            "sanmar_sku", "sanmar_style", "color_name", "color_code",
            "fit_code", "brand_name", "product_name", "erpnext_item",
            "net_price", "stock_status", "last_known_qty",
        ],
        order_by="sanmar_style asc, color_name asc, fit_code asc",
    )
    return rows
