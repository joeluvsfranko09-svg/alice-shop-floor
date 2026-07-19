# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt
"""
operator_quality_utils.py
--------------------------
Quality-of-work tracking for decoration station operators.

Each time an operator completes a decoration step (DTG, DTF press, or
embroidery) this module:

  1. Creates an OperatorQualityLog entry — immutable snapshot of
     defect count, rework flag, cycle time, and proficiency tier.

  2. Recomputes the operator's rolling quality score —
     defect rate over the last N jobs, weighted by recency.

  3. If the rolling defect rate exceeds the configured threshold,
     publishes a supervisor alert and optionally flags the operator
     for re-training review.

  4. Updates their SkillProfileHistory quality_score field so Module 6
     picks it up in the weekly skill recalculation.

Entry points
------------
  log_decoration_job_complete(...)   — called from api_dtg_print_complete,
                                       api_emb_job_complete, and
                                       api_dtf_press_complete hooks
  get_operator_quality_stats(...)    — read-only summary for dashboards
  get_quality_leaderboard(...)       — top operators by defect rate
  flag_operator_for_review(...)      — supervisor override
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime, getdate, add_days, today


# ---------------------------------------------------------------------------
# Thresholds (can be overridden in ALICE Settings)
# ---------------------------------------------------------------------------

DEFAULT_DEFECT_RATE_ALERT_THRESHOLD = 0.15   # 15% jobs with defects → alert
DEFAULT_ROLLING_WINDOW_JOBS         = 30     # last 30 jobs for rolling rate
TRAINEE_THRESHOLD_MULTIPLIER        = 2.0    # trainees get 2× the tolerance


# ---------------------------------------------------------------------------
# Primary entry point — called from decoration_engine on job completion
# ---------------------------------------------------------------------------

def log_decoration_job_complete(
	job_card_name:    str,
	decoration_method: str,
	employee:         str,
	machine_config:   str = None,
	defect_count:     int = 0,
	rework_flag:      bool = False,
	defect_notes:     str = "",
	defect_types:     str = "",
	started_at=None,
) -> str | None:
	"""
	Creates an OperatorQualityLog entry for a completed decoration job,
	then recomputes the operator's rolling quality stats.

	Args:
	  job_card_name:      ERPNext Job Card name
	  decoration_method:  "DTG" | "DTF" | "Embroidery"
	  employee:           Employee link (may be None for supervisor-override jobs)
	  machine_config:     MachineConfig link (optional)
	  defect_count:       defects observed on this garment at this station
	  rework_flag:        True if garment needed rework or replacement
	  defect_notes:       free-text description of defects
	  defect_types:       comma-separated defect type codes
	  started_at:         datetime when operator first scanned (for cycle time)

	Returns the OperatorQualityLog document name, or None on failure.
	"""
	if not job_card_name or not decoration_method:
		return None

	# Skip logging for anonymous/supervisor override runs
	if not employee or employee == "__unlisted__":
		frappe.logger().info(
			f"[QualityUtils] Skipping quality log for {job_card_name} — no operator"
		)
		return None

	# Cycle time
	cycle_time_sec = None
	completed_at = now_datetime()
	if started_at:
		try:
			from frappe.utils import time_diff_in_seconds
			cycle_time_sec = time_diff_in_seconds(completed_at, started_at)
		except Exception:
			pass

	# Snapshot proficiency at time of completion
	proficiency_at_time = _get_operator_proficiency(employee, decoration_method, machine_config)

	# Pull work_order + production_recipe from Job Card
	jc_fields = frappe.db.get_value(
		"Job Card",
		job_card_name,
		["work_order", "production_recipe"],
		as_dict=True,
	) or {}

	try:
		log = frappe.get_doc({
			"doctype":             "Operator Quality Log",
			"job_card":            job_card_name,
			"decoration_method":   decoration_method,
			"completed_at":        completed_at,
			"employee":            employee,
			"machine_config":      machine_config,
			"defect_count":        max(0, int(defect_count or 0)),
			"rework_flag":         int(bool(rework_flag)),
			"cycle_time_sec":      cycle_time_sec,
			"proficiency_at_time": proficiency_at_time,
			"defect_notes":        defect_notes or "",
			"defect_types":        defect_types or "",
			"work_order":          jc_fields.get("work_order") or "",
			"production_recipe":   jc_fields.get("production_recipe") or "",
		})
		log.insert(ignore_permissions=True)

		frappe.logger().info(
			f"[QualityUtils] OperatorQualityLog created: {log.name} | "
			f"{employee} | {decoration_method} | defects={defect_count} | "
			f"rework={rework_flag}"
		)

		# Asynchronously recompute rolling stats
		frappe.enqueue(
			"alice_shop_floor.alice_shop_floor.operator_quality_utils._recompute_rolling_stats",
			queue="short",
			employee=employee,
			decoration_method=decoration_method,
			quality_log_name=log.name,
			is_async=True,
		)

		return log.name

	except Exception as e:
		frappe.log_error(
			frappe.get_traceback(),
			f"OperatorQualityLog insert failed for {job_card_name}",
		)
		frappe.logger().error(f"[QualityUtils] Log insert error: {e}")
		return None


# ---------------------------------------------------------------------------
# Rolling stats recomputation (runs in background queue)
# ---------------------------------------------------------------------------

def _recompute_rolling_stats(
	employee:         str,
	decoration_method: str,
	quality_log_name:  str = None,
) -> None:
	"""
	Recomputes rolling defect rate for an operator + method combination.
	Called in the background after each OperatorQualityLog entry is created.

	Updates SkillProfileHistory (quality_score field) if it exists.
	Publishes a supervisor alert if defect rate exceeds threshold.
	"""
	threshold = _get_defect_threshold()
	window    = DEFAULT_ROLLING_WINDOW_JOBS

	stats = _compute_stats(employee, decoration_method, window)
	defect_rate    = stats["defect_rate"]
	rework_rate    = stats["rework_rate"]
	avg_cycle_time = stats["avg_cycle_time_sec"]
	job_count      = stats["job_count"]

	# Quality score: 100 = perfect, deduct 5 per % defect rate, floor at 0
	quality_score = max(0.0, round(100.0 - (defect_rate * 100 * 5), 1))

	frappe.logger().info(
		f"[QualityUtils] Rolling stats for {employee} / {decoration_method}: "
		f"jobs={job_count} | defect_rate={defect_rate:.1%} | "
		f"rework_rate={rework_rate:.1%} | quality_score={quality_score}"
	)

	# Update most recent SkillProfileHistory for this operator + method
	_update_skill_profile_quality(employee, decoration_method, quality_score, avg_cycle_time)

	# Alert if rate is unacceptable
	proficiency = _get_operator_proficiency(employee, decoration_method)
	effective_threshold = threshold * (
		TRAINEE_THRESHOLD_MULTIPLIER if proficiency == "Trainee" else 1.0
	)

	if defect_rate > effective_threshold and job_count >= 5:
		_publish_quality_alert(
			employee, decoration_method, defect_rate, rework_rate,
			job_count, quality_score, effective_threshold,
		)


def _compute_stats(
	employee:         str,
	decoration_method: str,
	window:           int,
) -> dict:
	"""Returns defect_rate, rework_rate, avg_cycle_time_sec, job_count over last N jobs."""
	rows = frappe.get_all(
		"Operator Quality Log",
		filters={
			"employee":          employee,
			"decoration_method": decoration_method,
		},
		fields=["defect_count", "rework_flag", "cycle_time_sec"],
		order_by="completed_at desc",
		limit=window,
	)

	if not rows:
		return {
			"defect_rate":        0.0,
			"rework_rate":        0.0,
			"avg_cycle_time_sec": None,
			"job_count":          0,
		}

	total       = len(rows)
	with_defect = sum(1 for r in rows if (r.defect_count or 0) > 0)
	reworked    = sum(1 for r in rows if r.rework_flag)
	cycles      = [r.cycle_time_sec for r in rows if r.cycle_time_sec]

	return {
		"defect_rate":        with_defect / total,
		"rework_rate":        reworked / total,
		"avg_cycle_time_sec": (sum(cycles) / len(cycles)) if cycles else None,
		"job_count":          total,
	}


def _update_skill_profile_quality(
	employee:         str,
	decoration_method: str,
	quality_score:    float,
	avg_cycle_time:   float = None,
) -> None:
	"""
	Writes quality_score back into the most recent SkillProfileHistory record
	for this operator, if the DocType has that field.
	"""
	try:
		profile = frappe.db.get_value(
			"Skill Profile History",
			{"employee": employee, "stage": decoration_method},
			"name",
			order_by="creation desc",
		)
		if profile:
			update: dict = {"quality_score": quality_score}
			if avg_cycle_time:
				update["avg_cycle_time_sec"] = avg_cycle_time
			frappe.db.set_value("Skill Profile History", profile, update)
	except Exception:
		pass   # field may not exist in older installs


def _publish_quality_alert(
	employee:          str,
	decoration_method: str,
	defect_rate:       float,
	rework_rate:       float,
	job_count:         int,
	quality_score:     float,
	threshold:         float,
) -> None:
	"""Publishes a realtime supervisor alert when defect rate is too high."""
	employee_name = frappe.db.get_value("Employee", employee, "employee_name") or employee
	frappe.publish_realtime(
		"operator_quality_alert",
		{
			"employee":          employee,
			"employee_name":     employee_name,
			"decoration_method": decoration_method,
			"defect_rate":       round(defect_rate * 100, 1),
			"rework_rate":       round(rework_rate * 100, 1),
			"job_count":         job_count,
			"quality_score":     quality_score,
			"threshold_pct":     round(threshold * 100, 1),
			"message": (
				f"⚠️ {employee_name} ({decoration_method}) defect rate "
				f"{defect_rate:.0%} over last {job_count} jobs — "
				f"exceeds {threshold:.0%} threshold."
			),
		},
		room=frappe.local.site,
	)
	frappe.logger().warning(
		f"[QualityUtils] ALERT: {employee_name} / {decoration_method} "
		f"defect rate {defect_rate:.1%} > threshold {threshold:.1%} "
		f"(last {job_count} jobs)"
	)


# ---------------------------------------------------------------------------
# Read-only helpers for dashboards
# ---------------------------------------------------------------------------

def get_operator_quality_stats(
	employee:         str,
	decoration_method: str = None,
	window:           int = 30,
) -> dict:
	"""
	Returns quality stats for an operator, optionally filtered by method.
	Used by the supervisor dashboard and the ALICE OS operator panel.
	"""
	if decoration_method:
		stats = _compute_stats(employee, decoration_method, window)
		return {
			"employee":          employee,
			"decoration_method": decoration_method,
			**stats,
			"quality_score": max(0.0, round(100.0 - (stats["defect_rate"] * 100 * 5), 1)),
		}

	# All methods
	from alice_shop_floor.alice_shop_floor.decoration_utils import DecoMethod
	result = {}
	for method in DecoMethod.ALL:
		stats = _compute_stats(employee, method, window)
		if stats["job_count"] > 0:
			result[method] = {
				**stats,
				"quality_score": max(0.0, round(100.0 - (stats["defect_rate"] * 100 * 5), 1)),
			}
	return {
		"employee": employee,
		"by_method": result,
		"window_jobs": window,
	}


def get_quality_leaderboard(
	decoration_method: str = None,
	window:           int = 30,
	limit:            int = 20,
) -> list:
	"""
	Returns top operators ranked by quality score (fewest defects).
	Used by ALICE OS operator efficiency panel.
	"""
	filters: dict = {}
	if decoration_method:
		filters["decoration_method"] = decoration_method

	# Get distinct employees who have recent logs
	recent_employees = frappe.db.sql("""
		SELECT DISTINCT employee, decoration_method
		FROM `tabOperator Quality Log`
		WHERE (%(method)s IS NULL OR decoration_method = %(method)s)
		ORDER BY completed_at DESC
	""", {"method": decoration_method}, as_dict=True)

	seen: set[tuple] = set()
	leaderboard = []

	for row in recent_employees:
		key = (row.employee, row.decoration_method)
		if key in seen:
			continue
		seen.add(key)

		stats = _compute_stats(row.employee, row.decoration_method, window)
		if stats["job_count"] == 0:
			continue

		quality_score = max(0.0, round(100.0 - (stats["defect_rate"] * 100 * 5), 1))
		employee_name = frappe.db.get_value("Employee", row.employee, "employee_name") or row.employee
		proficiency   = _get_operator_proficiency(row.employee, row.decoration_method)

		leaderboard.append({
			"employee":          row.employee,
			"employee_name":     employee_name,
			"decoration_method": row.decoration_method,
			"quality_score":     quality_score,
			"defect_rate_pct":   round(stats["defect_rate"] * 100, 1),
			"rework_rate_pct":   round(stats["rework_rate"] * 100, 1),
			"job_count":         stats["job_count"],
			"avg_cycle_time_sec": stats["avg_cycle_time_sec"],
			"proficiency":       proficiency,
		})

	# Sort best quality score first
	leaderboard.sort(key=lambda x: x["quality_score"], reverse=True)
	return leaderboard[:limit]


def flag_operator_for_review(
	employee:          str,
	decoration_method: str,
	reason:            str,
	flagged_by:        str = None,
) -> dict:
	"""
	Supervisor manually flags an operator for re-training review.
	Adds a note to their most recent SkillProfileHistory record and
	publishes a realtime event.
	"""
	flagged_by = flagged_by or frappe.session.user
	employee_name = frappe.db.get_value("Employee", employee, "employee_name") or employee

	# Append note to skill profile if it exists
	try:
		profile = frappe.db.get_value(
			"Skill Profile History",
			{"employee": employee, "stage": decoration_method},
			"name",
			order_by="creation desc",
		)
		if profile:
			existing_notes = frappe.db.get_value("Skill Profile History", profile, "notes") or ""
			new_note = (
				f"[{now_datetime().strftime('%Y-%m-%d')}] Flagged for re-training review "
				f"by {flagged_by}: {reason}"
			)
			frappe.db.set_value(
				"Skill Profile History", profile,
				"notes", f"{existing_notes}\n{new_note}".strip(),
			)
	except Exception:
		pass

	frappe.publish_realtime(
		"operator_flagged_for_review",
		{
			"employee":          employee,
			"employee_name":     employee_name,
			"decoration_method": decoration_method,
			"reason":            reason,
			"flagged_by":        flagged_by,
		},
		room=frappe.local.site,
	)

	frappe.logger().info(
		f"[QualityUtils] {employee_name} flagged for review by {flagged_by}: {reason}"
	)

	return {
		"ok":             True,
		"employee":       employee,
		"employee_name":  employee_name,
		"decoration_method": decoration_method,
	}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_operator_proficiency(
	employee:         str,
	decoration_method: str,
	machine_config:   str = None,
) -> str | None:
	"""
	Returns the operator's current proficiency level for a method.
	Returns highest tier when multiple certs exist.
	"""
	from frappe.utils import getdate, today as _today
	today_date = getdate(_today())
	proficiency_rank = {"Expert": 3, "Certified": 2, "Trainee": 1}

	filters: dict = {
		"employee":          employee,
		"decoration_method": decoration_method,
		"is_active":         1,
	}
	rows = frappe.get_all(
		"Machine Operator Certification",
		filters=filters,
		fields=["proficiency_level", "expires_on"],
	)

	valid = [
		r for r in rows
		if not r.expires_on or getdate(r.expires_on) >= today_date
	]
	if not valid:
		return None

	best = max(valid, key=lambda r: proficiency_rank.get(r.proficiency_level, 0))
	return best.proficiency_level


def _get_defect_threshold() -> float:
	"""
	Returns the configured defect rate alert threshold.
	Reads from ALICE Settings if available, else uses module default.
	"""
	try:
		return float(
			frappe.db.get_single_value("ALICE Settings", "quality_defect_alert_threshold")
			or DEFAULT_DEFECT_RATE_ALERT_THRESHOLD
		)
	except Exception:
		return DEFAULT_DEFECT_RATE_ALERT_THRESHOLD


# ---------------------------------------------------------------------------
# Whitelisted API endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist()
def api_get_operator_quality_stats(
	employee:         str,
	decoration_method: str = None,
	window:           int = 30,
) -> dict:
	"""Quality stats for a single operator. Used by ALICE OS operator panel."""
	return get_operator_quality_stats(employee, decoration_method, int(window))


@frappe.whitelist()
def api_get_quality_leaderboard(
	decoration_method: str = None,
	window:           int = 30,
	limit:            int = 20,
) -> list:
	"""Quality leaderboard for supervisor dashboard."""
	return get_quality_leaderboard(decoration_method, int(window), int(limit))


@frappe.whitelist()
def api_flag_operator_for_review(
	employee:          str,
	decoration_method: str,
	reason:            str,
) -> dict:
	"""Supervisor manual flag for re-training review."""
	return flag_operator_for_review(
		employee, decoration_method, reason, frappe.session.user
	)
