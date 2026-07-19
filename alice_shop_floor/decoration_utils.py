# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt
"""
decoration_utils.py
-------------------
Shared utilities for the ALICE Decoration Engine.

Covers three decoration methods:
  - DTG  : Epson SureColor DTG   (Epson Edge Print 2 REST + hot-folder fallback)
  - DTF  : Epson SureColor G6070 35" (print) + pneumatic heat press (transfer)
             Step 1 — EpsonDTFDriver   prints film at 1200 DPI on 35" roll
             Step 2 — dryer cures film (automatic, no driver)
             Step 3 — PneumaticPressDriver  validates + logs press params
  - EMB  : Melco Summit 15-needle (Melco OS REST + FTP fallback, DST primary)

Key consumers:
  - decoration_router.py   (routing score engine)
  - decoration_engine.py   (per-method execution)
  - api.py                 (scan-to-print, approve-digitizing endpoints)
  - tasks.py               (scheduled queue checks)
"""

from __future__ import annotations

import frappe
from frappe import _
from typing import Optional


# ---------------------------------------------------------------------------
# Decoration method constants
# ---------------------------------------------------------------------------

class DecoMethod:
	DTG = "DTG"
	DTF = "DTF"
	EMB = "Embroidery"
	ALL = [DTG, DTF, EMB]


# ---------------------------------------------------------------------------
# Workstation name constants
# ---------------------------------------------------------------------------

class Workstation:
	DTG       = "DTG Station"        # Epson SureColor DTG
	DTF_PRINT = "DTF Print Station"  # Epson SureColor G6070 — film print step
	DTF       = "DTF Heat Press"     # Pneumatic press — film-to-garment transfer step
	EMB       = "Embroidery Station" # Melco Summit 15-needle


# ---------------------------------------------------------------------------
# Fabric scoring weights for routing
# ---------------------------------------------------------------------------

# Score map: (decoration_method, fabric_type) → compatibility score 0-10
# 10 = perfect, 0 = do not use
FABRIC_SCORE_MAP: dict[tuple[str, str], int] = {
	# DTG — best on 100% cotton, poor on poly, no embellishments
	(DecoMethod.DTG, "100% Cotton"):       10,
	(DecoMethod.DTG, "Cotton Blend"):       8,
	(DecoMethod.DTG, "50/50 Blend"):        6,
	(DecoMethod.DTG, "100% Polyester"):     2,
	(DecoMethod.DTG, "Performance Fabric"): 1,
	(DecoMethod.DTG, "Fleece"):             7,
	(DecoMethod.DTG, "Tri-Blend"):          6,
	# DTF — works on nearly any fabric
	(DecoMethod.DTF, "100% Cotton"):        9,
	(DecoMethod.DTF, "Cotton Blend"):       9,
	(DecoMethod.DTF, "50/50 Blend"):        9,
	(DecoMethod.DTF, "100% Polyester"):     9,
	(DecoMethod.DTF, "Performance Fabric"): 9,
	(DecoMethod.DTF, "Fleece"):             8,
	(DecoMethod.DTF, "Tri-Blend"):          9,
	(DecoMethod.DTF, "Nylon"):              7,
	(DecoMethod.DTF, "Denim"):              7,
	# Embroidery — structural; best on wovens/fleece, poor on lightweight knits
	(DecoMethod.EMB, "100% Cotton"):        8,
	(DecoMethod.EMB, "Cotton Blend"):       8,
	(DecoMethod.EMB, "100% Polyester"):     7,
	(DecoMethod.EMB, "Fleece"):             9,
	(DecoMethod.EMB, "Performance Fabric"): 5,
	(DecoMethod.EMB, "Tri-Blend"):          6,
	(DecoMethod.EMB, "Denim"):             10,
}

# Design type routing preferences
DESIGN_TYPE_SCORE_MAP: dict[tuple[str, str], int] = {
	# DTG excels at photorealistic, full-color, complex art
	(DecoMethod.DTG, "Photorealistic"):    10,
	(DecoMethod.DTG, "Full Color"):        10,
	(DecoMethod.DTG, "Gradient"):          10,
	(DecoMethod.DTG, "Spot Color"):         7,
	(DecoMethod.DTG, "Logo"):               7,
	(DecoMethod.DTG, "Text Only"):          5,
	(DecoMethod.DTG, "Emblem"):             6,
	# DTF flexible on design type
	(DecoMethod.DTF, "Photorealistic"):     9,
	(DecoMethod.DTF, "Full Color"):         9,
	(DecoMethod.DTF, "Gradient"):           9,
	(DecoMethod.DTF, "Spot Color"):         9,
	(DecoMethod.DTF, "Logo"):               9,
	(DecoMethod.DTF, "Text Only"):          8,
	(DecoMethod.DTF, "Emblem"):             9,
	# Embroidery best for logos, emblems, text; cannot do photorealistic
	(DecoMethod.EMB, "Photorealistic"):     1,
	(DecoMethod.EMB, "Full Color"):         3,
	(DecoMethod.EMB, "Gradient"):           0,
	(DecoMethod.EMB, "Spot Color"):         8,
	(DecoMethod.EMB, "Logo"):              10,
	(DecoMethod.EMB, "Text Only"):         10,
	(DecoMethod.EMB, "Emblem"):            10,
}

DEFAULT_SCORE = 5  # fallback if combo not in map


# ---------------------------------------------------------------------------
# Routing score engine
# ---------------------------------------------------------------------------

def score_decoration_method(
	method: str,
	fabric_type: str,
	design_type: str,
	garment_color: Optional[str] = None,
	rush: bool = False,
) -> float:
	"""
	Returns a composite score 0.0–10.0 for using `method` on a given
	fabric_type + design_type combination.

	Scoring weights:
	  40% fabric compatibility
	  40% design type compatibility
	  20% garment color bonus/penalty
	  + rush flag can boost DTF (fastest setup) by 1.0

	Used by DecorationRouter to rank DTG vs DTF vs Embroidery for a job.
	"""
	fabric_score = FABRIC_SCORE_MAP.get((method, fabric_type), DEFAULT_SCORE)
	design_score = DESIGN_TYPE_SCORE_MAP.get((method, design_type), DEFAULT_SCORE)

	# Color bonus: DTG needs pretreatment on dark/colored garments (small penalty)
	color_bonus = 0.0
	if garment_color and method == DecoMethod.DTG:
		dark_colors = {"black", "navy", "dark grey", "dark gray", "maroon", "forest green", "dark red"}
		if garment_color.lower() in dark_colors:
			color_bonus = -1.5  # pretreat adds time and cost
		else:
			color_bonus = 0.5

	composite = (fabric_score * 0.40) + (design_score * 0.40) + color_bonus
	if rush and method == DecoMethod.DTF:
		composite += 1.0  # DTF has fastest physical setup; favor it in rush

	return round(min(max(composite, 0.0), 10.0), 2)


def route_decoration(
	fabric_type: str,
	design_type: str,
	garment_color: Optional[str] = None,
	rush: bool = False,
) -> dict:
	"""
	Scores all three methods and returns the winner + full score breakdown.

	Returns:
	  {
	    "winner": "DTF",
	    "scores": {"DTG": 6.1, "DTF": 8.4, "Embroidery": 3.0},
	    "fabric_type": ...,
	    "design_type": ...,
	    "rush": ...
	  }
	"""
	scores = {
		m: score_decoration_method(m, fabric_type, design_type, garment_color, rush)
		for m in DecoMethod.ALL
	}
	winner = max(scores, key=scores.__getitem__)
	return {
		"winner": winner,
		"scores": scores,
		"fabric_type": fabric_type,
		"design_type": design_type,
		"garment_color": garment_color,
		"rush": rush,
	}


# ---------------------------------------------------------------------------
# Recipe lookup helpers
# ---------------------------------------------------------------------------

def get_recipe_for_job_card(job_card_name: str) -> Optional[str]:
	"""
	Returns the production_recipe linked on a Job Card, or None.
	Custom field 'production_recipe' added in Task #53.
	"""
	return frappe.db.get_value("Job Card", job_card_name, "production_recipe")


def get_active_recipe(
	decoration_method: str,
	design_placement: Optional[str] = None,
	item_code: Optional[str] = None,
) -> Optional[str]:
	"""
	Finds the best matching active ProductionRecipe for the given method.

	Priority order:
	  1. Exact item_code + method + placement match
	  2. Method + placement match (no item)
	  3. Method only match
	  4. None
	"""
	filters: dict = {"is_active": 1, "decoration_method": decoration_method}

	if item_code and design_placement:
		filters["item_code"] = item_code
		filters["design_placement"] = design_placement
		result = frappe.get_list("Production Recipe", filters=filters, fields=["name"], limit=1)
		if result:
			return result[0].name

	if design_placement:
		filters = {"is_active": 1, "decoration_method": decoration_method, "design_placement": design_placement}
		result = frappe.get_list("Production Recipe", filters=filters, fields=["name"], limit=1)
		if result:
			return result[0].name

	filters = {"is_active": 1, "decoration_method": decoration_method}
	result = frappe.get_list("Production Recipe", filters=filters, fields=["name"], limit=1)
	if result:
		return result[0].name

	return None


# ---------------------------------------------------------------------------
# Workstation helpers
# ---------------------------------------------------------------------------

def get_workstation_for_method(method: str) -> str:
	"""
	Maps a decoration method to its initial ERPNext Workstation name.

	For DTF, the Job Card starts at the print station (Epson G6070).
	It advances to DTF Heat Press (pneumatic press) when start_press_job()
	is called from the DTF Press Station tablet page.
	"""
	return {
		DecoMethod.DTG: Workstation.DTG,
		DecoMethod.DTF: Workstation.DTF_PRINT,   # initial: Epson G6070 print step
		DecoMethod.EMB: Workstation.EMB,
	}.get(method, Workstation.DTG)


# ---------------------------------------------------------------------------
# Job Card stamping helpers
# ---------------------------------------------------------------------------

def stamp_job_card(job_card_name: str, decoration_method: str, recipe_name: str) -> None:
	"""
	Writes decoration_method + production_recipe onto a Job Card.
	Safe to call multiple times (idempotent if values unchanged).
	"""
	jc = frappe.get_doc("Job Card", job_card_name)
	updated = False
	if jc.get("decoration_method") != decoration_method:
		jc.decoration_method = decoration_method
		updated = True
	if jc.get("production_recipe") != recipe_name:
		jc.production_recipe = recipe_name
		updated = True
	if updated:
		jc.flags.ignore_validate_update_after_submit = True
		jc.save(ignore_permissions=True)
		frappe.logger().info(
			f"[decoration_utils] Stamped Job Card {job_card_name} "
			f"→ {decoration_method} / {recipe_name}"
		)


# ---------------------------------------------------------------------------
# DST readiness check
# ---------------------------------------------------------------------------

def is_dst_approved(production_recipe_name: str) -> bool:
	"""
	Returns True if the embroidery DST file for a recipe is approved.
	Checks DigitizingQueue for any blocking entry on this recipe.
	"""
	blocking = frappe.get_list(
		"Digitizing Queue",
		filters={
			"production_recipe": production_recipe_name,
			"status": ["not in", ["Approved", "Released", "Cancelled"]],
		},
		limit=1,
	)
	return len(blocking) == 0


# ---------------------------------------------------------------------------
# Decoration damage / replacement helpers
# ---------------------------------------------------------------------------

def log_decoration_failure(
	job_card_name: str,
	damage_type: str,
	notes: str = "",
	trigger_replacement: bool = True,
) -> str:
	"""
	Creates a DecorationDamageLog entry for a failed decoration job.
	Optionally triggers a blank garment replacement order via SanMar/S&S/Alphabroader.

	Returns the name of the created DecorationDamageLog document.
	"""
	doc = frappe.get_doc({
		"doctype": "Decoration Damage Log",
		"job_card": job_card_name,
		"damage_type": damage_type,
		"notes": notes,
		"replacement_triggered": int(trigger_replacement),
	})
	doc.insert(ignore_permissions=True)
	frappe.logger().info(
		f"[decoration_utils] DecorationDamageLog created: {doc.name} "
		f"for Job Card {job_card_name} — {damage_type}"
	)
	if trigger_replacement:
		_trigger_garment_replacement(job_card_name, doc.name)
	return doc.name


def _trigger_garment_replacement(job_card_name: str, damage_log_name: str) -> None:
	"""
	Enqueues a background job to create a replacement blank garment PO
	through the preferred supplier (SanMar → S&S → Alphabroader priority).
	"""
	frappe.enqueue(
		"alice_shop_floor.alice_shop_floor.decoration_engine.create_replacement_order",
		queue="long",
		job_card_name=job_card_name,
		damage_log_name=damage_log_name,
		is_async=True,
	)


# ---------------------------------------------------------------------------
# Queue status summary (for ALICE OS dashboard)
# ---------------------------------------------------------------------------

def get_decoration_queue_summary() -> dict:
	"""
	Returns live queue counts for the DECORATION panel in ALICE OS.
	Called by the dashboard API endpoint.
	"""
	def count(method: str, statuses: list[str]) -> int:
		return frappe.db.count(
			"Job Card",
			filters={
				"decoration_method": method,
				"status": ["in", statuses],
			},
		)

	dtg_active = count(DecoMethod.DTG, ["Open", "Work In Progress"])
	dtf_active = count(DecoMethod.DTF, ["Open", "Work In Progress"])
	emb_active = count(DecoMethod.EMB, ["Open", "Work In Progress"])

	pending_digitizing = frappe.db.count(
		"Digitizing Queue",
		filters={"status": ["in", ["Submitted", "Digitizing", "Review"]]},
	)

	return {
		"dtg_jobs": dtg_active,
		"dtf_jobs": dtf_active,
		"emb_jobs": emb_active,
		"digitizing_queue": pending_digitizing,
		"total_active": dtg_active + dtf_active + emb_active,
	}
