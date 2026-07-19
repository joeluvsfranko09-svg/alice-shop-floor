"""
ALICE Shop Floor - Frappe Scheduled Tasks

hooks.py wires up:
  every_30_minutes: check_line_balance
  hourly:           escalate_stalled_orders

ZAZFIT is pure POD - every order is a unique custom garment.
Concurrent WOs in the same stage is normal. We watch throughput, not queue depth.
"""

import frappe
from frappe.utils import now_datetime, get_datetime

STALL_THRESHOLD_HOURS = 4
THROUGHPUT_WARN_MINUTES = 180


def check_line_balance():
    """Every 30 min: check throughput health across active custom orders."""
    trackers = frappe.db.sql(
        """
        SELECT current_stage, COUNT(*) as count,
               AVG(TIMESTAMPDIFF(MINUTE, stage_entered_at, NOW())) as avg_minutes,
               MAX(TIMESTAMPDIFF(MINUTE, stage_entered_at, NOW())) as max_minutes
        FROM `tabProduction Stage Tracker`
        WHERE is_complete = 0
          AND stage_entered_at IS NOT NULL
        GROUP BY current_stage
        ORDER BY avg_minutes DESC
        """,
        as_dict=True,
    )

    if not trackers:
        return

    slow_stages = [
        row for row in trackers
        if (row.get("avg_minutes") or 0) > THROUGHPUT_WARN_MINUTES
    ]

    if slow_stages:
        for row in slow_stages:
            avg_h = round((row.get("avg_minutes") or 0) / 60, 1)
            max_h = round((row.get("max_minutes") or 0) / 60, 1)
            frappe.logger().warning(
                "ALICE Throughput: Stage '{}' averaging {}h per order "
                "across {} active custom orders. Max in stage: {}h.".format(
                    row["current_stage"], avg_h, row["count"], max_h
                )
            )
        # TODO Module 2 - Line Balancing AI: trigger ALICE reallocation suggestion
    else:
        stage_summary = {row["current_stage"]: row["count"] for row in trackers}
        frappe.logger().info(
            "ALICE Line Balance: Floor throughput healthy. "
            "Active custom orders by stage: {}".format(stage_summary)
        )


def escalate_stalled_orders():
    """Hourly: flag orders stuck in a stage beyond STALL_THRESHOLD_HOURS."""
    from datetime import timedelta

    now = now_datetime()
    cutoff = now - timedelta(hours=STALL_THRESHOLD_HOURS)

    stalled = frappe.db.sql(
        """
        SELECT pst.name, pst.work_order, pst.current_stage, pst.stage_entered_at
        FROM `tabProduction Stage Tracker` pst
        WHERE pst.is_complete = 0
          AND pst.stage_entered_at IS NOT NULL
          AND pst.stage_entered_at <= %(cutoff)s
        ORDER BY pst.stage_entered_at ASC
        """,
        {"cutoff": cutoff},
        as_dict=True,
    )

    if not stalled:
        return

    for order in stalled:
        try:
            entered = get_datetime(order["stage_entered_at"])
            hours = round((now - entered).total_seconds() / 3600, 1)
        except Exception:
            hours = "?"

        frappe.logger().warning(
            "ALICE Stall Alert: Work Order {} has been in '{}' for {}h "
            "(tracker: {})".format(
                order["work_order"], order["current_stage"], hours, order["name"]
            )
        )
    # TODO Module 6 - Escalation Engine: push supervisor real-time alert


# ===========================================================================
# Decoration Engine scheduled tasks (registered in hooks.py)
# ===========================================================================

def run_decoration_routing_check():
	"""
	Every 5 minutes: finds submitted Job Cards that haven't been routed
	and triggers DecorationRouter on each.

	Covers the case where a Job Card was created/submitted before the
	decoration_engine.on_job_card_submit hook was available, or where
	the hook silently failed.
	"""
	unrouted = frappe.get_list(
		"Job Card",
		filters={
			"decoration_routed": 0,
			"status": ["in", ["Open", "Work In Progress"]],
		},
		fields=["name"],
		limit=50,
		order_by="creation asc",
	)

	if not unrouted:
		return

	frappe.logger().info(
		f"[DecorationEngine] Routing check: {len(unrouted)} unrouted Job Cards found"
	)

	from alice_shop_floor.alice_shop_floor.decoration_router import route_job_card

	success = 0
	failed = 0
	for jc in unrouted:
		try:
			result = route_job_card(jc.name)
			if result.get("ok") and not result.get("skipped"):
				success += 1
			elif not result.get("ok"):
				failed += 1
		except Exception as e:
			failed += 1
			frappe.logger().error(
				f"[DecorationEngine] Routing failed for {jc.name}: {e}"
			)

	frappe.logger().info(
		f"[DecorationEngine] Routing batch complete — "
		f"success={success}, failed={failed}, total={len(unrouted)}"
	)


def run_digitizing_queue_alerts():
	"""
	Every 30 minutes: checks DigitizingQueue for entries stuck in
	Submitted or Digitizing for more than 4 hours and fires supervisor alerts.
	"""
	from datetime import timedelta
	cutoff = now_datetime() - timedelta(hours=4)

	stuck = frappe.get_list(
		"Digitizing Queue",
		filters={
			"status": ["in", ["Submitted", "Digitizing"]],
			"submitted_on": ["<=", cutoff],
		},
		fields=["name", "status", "priority", "production_recipe", "job_card", "submitted_on"],
		order_by="submitted_on asc",
	)

	if not stuck:
		return

	for entry in stuck:
		try:
			entered = get_datetime(entry.submitted_on)
			hours = round((now_datetime() - entered).total_seconds() / 3600, 1)
		except Exception:
			hours = "?"

		frappe.logger().warning(
			f"[DigitizingQueue] STUCK: {entry.name} | status={entry.status} | "
			f"priority={entry.priority} | hours_waiting={hours} | "
			f"recipe={entry.production_recipe} | job_card={entry.job_card}"
		)

		# Publish realtime alert to ALICE OS DECORATION panel
		frappe.publish_realtime(
			"digitizing_queue_alert",
			{
				"name": entry.name,
				"status": entry.status,
				"hours_waiting": hours,
				"priority": entry.priority,
				"job_card": entry.job_card,
			},
			room=frappe.local.site,
		)


def run_decoration_damage_daily_summary():
	"""
	Daily: compiles decoration damage stats for the past 24 hours
	and logs a summary. Future: push to Slack / email digest.
	"""
	from frappe.utils import add_days
	yesterday = add_days(now_datetime(), -1)

	logs = frappe.get_list(
		"Decoration Damage Log",
		filters={"creation": [">=", yesterday]},
		fields=["damage_type", "damage_severity", "decoration_method", "replacement_triggered"],
	)

	if not logs:
		frappe.logger().info("[DecorationEngine] Daily damage summary: no damage events in past 24h")
		return

	by_severity = {}
	by_method = {}
	replacements = 0
	for log in logs:
		sev = log.damage_severity or "Unknown"
		meth = log.decoration_method or "Unknown"
		by_severity[sev] = by_severity.get(sev, 0) + 1
		by_method[meth] = by_method.get(meth, 0) + 1
		if log.replacement_triggered:
			replacements += 1

	frappe.logger().warning(
		f"[DecorationEngine] Daily damage summary: total={len(logs)} | "
		f"by_severity={by_severity} | by_method={by_method} | "
		f"replacement_orders={replacements}"
	)


# ======================================================================
# Module 1: Incentive Pay Engine — Scheduled Jobs
# ======================================================================

def calculate_weekly_pay():
    """
    Scheduled: every Sunday at 23:00 (cron: 0 23 * * 0).
    Calculates (but does NOT finalize) the current week's pay summaries.
    Supervisor reviews and calls finalize_pay_period() to lock.
    """
    from datetime import date, timedelta
    from alice_shop_floor.alice_shop_floor.incentive_pay_utils import (
        IncentivePayEngineERPNext,
    )

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    engine = IncentivePayEngineERPNext(monday, sunday)
    names = engine.calculate_period()
    frappe.logger().info(
        "ALICE: Weekly pay calculated — {} summaries for {}".format(
            len(names), engine.period_label
        )
    )


def recalculate_pay_daily():
    """
    Scheduled: daily at 18:00. Mid-week running tally so supervisors
    can see where operators stand before the period closes.
    Does not touch finalized records.
    """
    from datetime import date, timedelta
    from alice_shop_floor.alice_shop_floor.incentive_pay_utils import (
        IncentivePayEngineERPNext,
    )

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    engine = IncentivePayEngineERPNext(monday, sunday)
    engine.calculate_period()  # idempotent — updates unfinalized records only


# ======================================================================
# Module 2: Line Balancing AI — upgraded scheduler
# ======================================================================

def run_line_balance_snapshot():
    """
    Every 30 minutes: full snapshot + recommendations.
    Replaces the lightweight check_line_balance() stub above.
    Add this to hooks.py every_30_minutes to activate.
    """
    from alice_shop_floor.alice_shop_floor.line_balancing_utils import LineBalancingEngine
    LineBalancingEngine().run()


# ======================================================================
# Module 6: Operator Efficiency & Skill AI — weekly recalculation
# ======================================================================

def update_skill_profiles_weekly():
    """
    Weekly (Sunday 22:00): recalculate all operator skill profiles.
    Runs before calculate_weekly_pay (Sunday 23:00) so pay leaderboards
    reflect the freshest skill data.
    """
    from alice_shop_floor.alice_shop_floor.operator_skill_utils import update_all_skill_profiles
    result = update_all_skill_profiles()
    frappe.log_error(
        title="[Module 6] Skill profiles updated",
        message=str(result),
    ) if result.get("flagged_for_training") else None


# ======================================================================
# V1: Fabric Inspector — poll pending inspections every 5 minutes
# ======================================================================

def poll_fabric_inspections():
    """
    Every 5 minutes: collect any Pending FabricInspectionResult docs that
    have a Cognex job ID and try to resolve them.
    alice_core handles the actual HTTP poll; this task just flags
    unresolved inspections that are older than 10 minutes as Error so
    they don't block production indefinitely.
    """
    from frappe.utils import now_datetime, get_datetime
    from datetime import timedelta

    cutoff = now_datetime() - timedelta(minutes=10)
    stale = frappe.get_all(
        "Fabric Inspection Result",
        filters={
            "overall_result": "Pending",
            "creation": ["<", cutoff],
        },
        fields=["name", "fabric_lot", "work_order"],
    )
    for rec in stale:
        doc = frappe.get_doc("Fabric Inspection Result", rec["name"])
        doc.overall_result = "Error"
        doc.error_message  = "Inspection timed out — no Cognex result received within 10 minutes."
        doc.save(ignore_permissions=True)
        frappe.publish_realtime(
            event="fabric_inspection_timeout",
            message={
                "name":       rec["name"],
                "fabric_lot": rec["fabric_lot"],
                "work_order": rec["work_order"],
            },
            room="shop_floor_supervisors",
        )
    if stale:
        frappe.db.commit()


# ======================================================================
# V2: Inline Stitch QC -- timeout stale Pending inspections
# ======================================================================

def poll_stitch_inspections():
    """
    Every 5 minutes: auto-error stitch inspections stuck Pending > 10 min.
    Mirrors poll_fabric_inspections() — prevents Sewing stage from being
    permanently blocked by an unresponsive Cognex job.
    """
    from frappe.utils import now_datetime
    from datetime import timedelta

    cutoff = now_datetime() - timedelta(minutes=10)
    stale = frappe.get_all(
        "Stitch Inspection Result",
        filters={
            "overall_result": "Pending",
            "creation": ["<", cutoff],
        },
        fields=["name", "work_order"],
    )
    for rec in stale:
        doc = frappe.get_doc("Stitch Inspection Result", rec["name"])
        doc.overall_result = "Error"
        doc.error_message  = "Stitch inspection timed out — no Cognex result received within 10 minutes."
        doc.save(ignore_permissions=True)
        frappe.publish_realtime(
            event="stitch_inspection_timeout",
            message={"name": rec["name"], "work_order": rec["work_order"]},
            room="shop_floor_supervisors",
        )
    if stale:
        frappe.db.commit()


# ======================================================================
# V3: Cut Accuracy Check -- timeout stale Pending inspections
# ======================================================================

def poll_cut_inspections():
    """
    Every 5 minutes: auto-error cut inspections stuck Pending > 10 min.
    Mirrors poll_fabric_inspections() and poll_stitch_inspections().
    Prevents the Cutting -> Bundling gate from being permanently blocked
    by an unresponsive Cognex cut accuracy job.
    """
    from frappe.utils import now_datetime
    from datetime import timedelta

    cutoff = now_datetime() - timedelta(minutes=10)
    stale = frappe.get_all(
        "Cut Inspection Result",
        filters={
            "overall_result": "Pending",
            "creation": ["<", cutoff],
        },
        fields=["name", "work_order", "fabric_lot"],
    )
    for rec in stale:
        doc = frappe.get_doc("Cut Inspection Result", rec["name"])
        doc.overall_result = "Error"
        doc.error_message  = "Cut inspection timed out — no Cognex result received within 10 minutes."
        doc.save(ignore_permissions=True)
        frappe.publish_realtime(
            event="cut_inspection_timeout",
            message={
                "name":       rec["name"],
                "work_order": rec["work_order"],
                "fabric_lot": rec.get("fabric_lot", ""),
            },
            room="shop_floor_supervisors",
        )
    if stale:
        frappe.db.commit()


# ======================================================================
# V4: Final Garment Inspector -- timeout stale Pending inspections
# ======================================================================

def poll_final_inspections():
    """
    Every 5 minutes: auto-error final inspections stuck Pending > 10 min.
    Mirrors V1/V2/V3 polling pattern.
    Prevents the Final QC -> Pack gate from being permanently blocked.
    """
    from frappe.utils import now_datetime
    from datetime import timedelta

    cutoff = now_datetime() - timedelta(minutes=10)
    stale = frappe.get_all(
        "Final Inspection Result",
        filters={
            "overall_result": "Pending",
            "creation": ["<", cutoff],
        },
        fields=["name", "work_order"],
    )
    for rec in stale:
        doc = frappe.get_doc("Final Inspection Result", rec["name"])
        doc.overall_result = "Error"
        doc.error_message  = "Final inspection timed out — no Cognex result received within 10 minutes."
        doc.save(ignore_permissions=True)
        frappe.publish_realtime(
            event="final_inspection_timeout",
            message={"name": rec["name"], "work_order": rec["work_order"]},
            room="shop_floor_supervisors",
        )
    if stale:
        frappe.db.commit()


# ======================================================================
# V5: Defect Intelligence -- daily roll-up
# ======================================================================

def generate_daily_defect_intelligence():
    """
    Daily: generate a 7-day rolling DefectIntelligenceReport.
    Runs after recalculate_pay_daily so fresh data is available.
    """
    from alice_shop_floor.alice_shop_floor.defect_intelligence_utils import (
        generate_defect_intelligence_report,
    )
    from frappe.utils import today
    label = "Daily {}".format(today())
    result = generate_defect_intelligence_report(window_days=7, window_label=label)
    frappe.logger().info(
        "ALICE V5: Defect Intelligence report generated — {} (pass rate: {}%)".format(
            result.get("name"), result.get("overall_pass_rate")
        )
    )
    # Surface critical defects to supervisors in real time
    if result.get("critical", 0) > 0:
        frappe.publish_realtime(
            event="defect_intelligence_critical_alert",
            message={
                "report":   result.get("name"),
                "critical": result.get("critical"),
                "summary":  result.get("ai_summary", "")[:300],
            },
            room="shop_floor_supervisors",
        )


# ======================================================================
# Module 7: Downtime Root-Cause AI -- hourly intelligence snapshot
# ======================================================================

def run_downtime_intelligence():
    """
    Hourly: generate a 24-hour rolling DowntimeIntelligenceReport and
    push a realtime summary to the shop_floor room.
    """
    from alice_shop_floor.alice_shop_floor.downtime_utils import generate_downtime_report
    from frappe.utils import now_datetime
    label  = "Hourly {}".format(now_datetime().strftime("%Y-%m-%d %H:00"))
    result = generate_downtime_report(window_days=1, window_label=label)
    if result.get("total_events", 0) > 0:
        frappe.publish_realtime(
            event="downtime_intelligence_update",
            message={
                "report":           result.get("name"),
                "total_events":     result.get("total_events"),
                "total_minutes":    result.get("total_minutes_lost"),
                "top_root_cause":   result.get("top_root_cause"),
                "top_stage":        result.get("top_stage"),
                "recurring_events": result.get("recurring_events"),
            },
            room="shop_floor",
        )


# ======================================================================
# Module 9: ESG -- weekly sustainability report
# ======================================================================

def generate_weekly_esg_report():
    """
    Runs every Sunday at 21:00 (added to cron below via hooks.py).
    Generates a 7-day ESGSummaryReport and notifies Manufacturing Manager
    if compliance status is non-compliant.
    """
    from alice_shop_floor.alice_shop_floor.esg_utils import generate_esg_report
    from frappe.utils import now_datetime
    label  = "Week {}".format(now_datetime().strftime("%Y-W%W"))
    result = generate_esg_report(window_days=7, period_label=label)
    status = result.get("compliance_status", "")
    frappe.logger().info(
        "ALICE ESG: Weekly report generated — {} (status: {})".format(
            result.get("name"), status)
    )
    if status in ("Warning", "Non-Compliant"):
        frappe.publish_realtime(
            event="esg_compliance_alert",
            message={
                "report":    result.get("name"),
                "status":    status,
                "narrative": result.get("narrative", "")[:400],
            },
            room="shop_floor_supervisors",
        )


# ======================================================================
# Module 10: WIP Bottleneck Detector -- every 30-minute snapshot
# ======================================================================

def run_wip_bottleneck_check():
    """
    Every 30 minutes: snapshot WIP queue depth and detect bottlenecks.
    Fires realtime alert to shop_floor_supervisors if threshold exceeded.
    """
    from alice_shop_floor.alice_shop_floor.wip_bottleneck_utils import run_wip_bottleneck_snapshot
    result = run_wip_bottleneck_snapshot()
    if result.get("alert_fired"):
        frappe.logger().warning(
            "ALICE WIP: Bottleneck detected at '{}' — score {:.2f}".format(
                result.get("bottleneck_stage"), result.get("congestion_score", 0)
            )
        )


# ===========================================================================
# Module 11: Pick-to-Bin
# ===========================================================================

def run_pick_to_bin_auto_assign():
    """Every 30 min: assign ready bundles to compatible free sewing stations."""
    import frappe
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import auto_assign_bins
    try:
        result = auto_assign_bins()
        if result["assigned_count"] > 0:
            frappe.logger("pick_to_bin").info(
                "Pick-to-Bin auto-assign: %d assigned, %d skipped",
                result["assigned_count"], result["skipped_count"],
            )
    except Exception as exc:
        frappe.log_error(str(exc), "Pick-to-Bin Auto-Assign Error")


def run_pace_check():
    """Fire realtime pace_alert for Critical-paced sewers (called every 5 min)."""
    from alice_shop_floor.alice_shop_floor.pace_engine import run_pace_check as _check
    _check()


# ======================================================================
# V6: Press QC Inspector -- timeout stale Pending inspections
# ======================================================================

def poll_press_inspections():
    """
    Every 5 minutes: auto-error Press QC logs stuck Pending > 10 min.
    Mirrors V1/V2/V3/V4 polling pattern.
    Prevents the Press -> Pack gate from being permanently blocked.
    """
    from frappe.utils import now_datetime
    from datetime import timedelta

    cutoff = now_datetime() - timedelta(minutes=10)
    stale = frappe.get_all(
        "Press Inspection Log",
        filters={
            "overall_result": "Pending",
            "creation": ["<", cutoff],
        },
        fields=["name", "work_order"],
    )
    for rec in stale:
        doc = frappe.get_doc("Press Inspection Log", rec["name"])
        doc.overall_result = "Error"
        doc.error_message  = (
            "Press inspection timed out — no Cognex result received within 10 minutes."
        )
        doc.save(ignore_permissions=True)
        frappe.publish_realtime(
            event="press_inspection_timeout",
            message={"name": rec["name"], "work_order": rec["work_order"]},
            room="shop_floor_supervisors",
        )
    if stale:
        frappe.db.commit()


# ===========================================================================
# Machine Driver Layer — scheduled tasks
# ===========================================================================

def ping_all_machines():
    """
    Every 5 minutes: ping every active machine, update last_ping_status.
    Fires a realtime alert to shop_floor_supervisors for each machine
    that just went offline (was Online last ping, now unreachable).
    """
    from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry

    machines = frappe.get_all(
        "Machine Config",
        filters={"is_active": 1},
        fields=["name", "machine_name", "decoration_method", "driver_type", "last_ping_status"],
    )

    if not machines:
        return

    online      = 0
    offline     = 0
    went_offline = []

    for mc_ref in machines:
        prev_status = mc_ref.get("last_ping_status") or "Unknown"
        try:
            driver = MachineDriverRegistry.get_driver_by_name(mc_ref["name"])
            ping   = driver.ping()
            ok     = bool(ping.get("ok"))
            new_status = "Online" if ok else "Offline"

            frappe.db.set_value("Machine Config", mc_ref["name"], {
                "last_ping_at":     now_datetime(),
                "last_ping_status": new_status,
            })

            if ok:
                online += 1
            else:
                offline += 1
                # Alert only if machine just went offline this sweep
                if prev_status == "Online":
                    went_offline.append({
                        "machine": mc_ref["name"],
                        "name":    mc_ref.get("machine_name") or mc_ref["name"],
                        "method":  mc_ref.get("decoration_method"),
                        "latency": ping.get("latency_ms"),
                    })
        except Exception as e:
            offline += 1
            frappe.logger().error(f"[ping_all_machines] {mc_ref['name']}: {e}")

    frappe.db.commit()

    frappe.logger().info(
        f"[MachineDriverLayer] Ping sweep — "
        f"online={online}, offline={offline}, total={len(machines)}"
    )

    for m in went_offline:
        frappe.publish_realtime(
            event="machine_offline_alert",
            message=m,
            room="shop_floor_supervisors",
        )
        frappe.logger().warning(
            f"[MachineDriverLayer] MACHINE WENT OFFLINE: {m['name']} "
            f"({m['method']}) — last ping failed"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SanMar Integration Tasks
# ─────────────────────────────────────────────────────────────────────────────

def run_sanmar_catalog_sync():
    """Daily: pull SanMar style/color/fit catalog into ERPNext Items + Style Map."""
    from alice_shop_floor.alice_shop_floor.sanmar import catalog_sync
    catalog_sync.run()


def run_sanmar_pricing_sync():
    """Daily: sync SanMar net pricing into ERPNext Item Price list."""
    from alice_shop_floor.alice_shop_floor.sanmar import pricing_sync
    pricing_sync.run()


def run_sanmar_stock_cache():
    """Every 30 min: refresh live SanMar inventory cache for all active SKUs."""
    from alice_shop_floor.alice_shop_floor.sanmar import stock_lookup
    stock_lookup.bulk_refresh_cache()
