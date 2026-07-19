# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt
"""
decoration_router.py
--------------------
DecorationRouter — scoring engine that maps a garment+design combination
to the correct decoration method (DTG | DTF | Embroidery) and then assigns
the appropriate ProductionRecipe and Workstation to a Job Card.

Scoring algorithm (see decoration_utils.score_decoration_method):
  40% fabric compatibility
  40% design type compatibility
  20% garment color adjustment (DTG dark-garment penalty)
  +1.0 DTF rush bonus when rush_flag=True

Entry points:
  route_job_card(job_card_name)      — main public function
  route_from_work_order(wo_name)     — routes all Job Cards on a WO
  get_routing_recommendation(...)    — dry-run scoring without saving
"""

from __future__ import annotations

import json
import frappe
from frappe import _
from frappe.utils import now_datetime

from alice_shop_floor.alice_shop_floor.decoration_utils import (
	DecoMethod,
	route_decoration,
	get_active_recipe,
	get_workstation_for_method,
	stamp_job_card,
)


# ---------------------------------------------------------------------------
# Main routing entry point
# ---------------------------------------------------------------------------

def route_job_card(job_card_name: str, force: bool = False) -> dict:
	"""
	Routes a single Job Card to DTG, DTF, or Embroidery.

	Steps:
	  1. Pull fabric_type, design_type, garment_color, rush flag from the
	     Job Card (and linked Work Order / BOM Item if needed)
	  2. Score all three methods via decoration_utils.route_decoration()
	  3. Find the best matching active ProductionRecipe for the winner
	  4. Write decoration_method, production_recipe, workstation,
	     decoration_routed, and decoration_router_scores to the Job Card
	  5. For Embroidery: check DigitizingQueue gate — block if DST not ready
	  6. Return routing result dict

	Args:
	  job_card_name: ERPNext Job Card name (e.g. "JC-00042")
	  force: if True, re-routes even if already routed

	Returns:
	  {
	    "ok": True,
	    "job_card": "JC-00042",
	    "winner": "DTF",
	    "scores": {"DTG": 7.2, "DTF": 8.9, "Embroidery": 3.0},
	    "recipe": "RECIPE-DTF-00001",
	    "workstation": "DTF Heat Press",
	    "dst_blocked": False,
	  }
	"""
	jc = frappe.get_doc("Job Card", job_card_name)

	# Skip if already routed (unless forced)
	if jc.get("decoration_routed") and not force:
		return {
			"ok": True,
			"skipped": True,
			"reason": "already_routed",
			"job_card": job_card_name,
			"decoration_method": jc.decoration_method,
			"production_recipe": jc.production_recipe,
		}

	# Extract routing inputs
	fabric_type, design_type, garment_color, rush = _extract_routing_inputs(jc)

	if not fabric_type or not design_type:
		frappe.logger().warning(
			f"[DecorationRouter] {job_card_name} — missing fabric_type or design_type, "
			f"cannot auto-route. fabric='{fabric_type}' design='{design_type}'"
		)
		return {
			"ok": False,
			"error": "missing_inputs",
			"job_card": job_card_name,
			"fabric_type": fabric_type,
			"design_type": design_type,
		}

	# Score all methods
	routing = route_decoration(fabric_type, design_type, garment_color, rush)
	winner = routing["winner"]
	scores = routing["scores"]

	# Find best ProductionRecipe for winner
	placement = jc.get("design_placement")
	item_code = jc.get("production_item")
	recipe_name = get_active_recipe(winner, placement, item_code)

	if not recipe_name:
		frappe.logger().warning(
			f"[DecorationRouter] {job_card_name} — no active ProductionRecipe "
			f"found for method={winner}, placement={placement}"
		)
		return {
			"ok": False,
			"error": "no_recipe_found",
			"job_card": job_card_name,
			"winner": winner,
			"scores": scores,
		}

	workstation = get_workstation_for_method(winner)

	# Check DST gate for embroidery
	dst_blocked = False
	if winner == DecoMethod.EMB:
		from alice_shop_floor.alice_shop_floor.decoration_utils import is_dst_approved
		dst_ready = is_dst_approved(recipe_name)
		if not dst_ready:
			dst_blocked = True
			frappe.logger().info(
				f"[DecorationRouter] {job_card_name} routed → Embroidery BUT blocked "
				f"(DST not approved for recipe {recipe_name})"
			)

	# Stamp everything onto the Job Card
	_stamp_routing_result(jc, winner, recipe_name, workstation, scores, dst_blocked)

	frappe.logger().info(
		f"[DecorationRouter] {job_card_name} → {winner} | recipe={recipe_name} | "
		f"scores={scores} | dst_blocked={dst_blocked}"
	)

	return {
		"ok": True,
		"job_card": job_card_name,
		"winner": winner,
		"scores": scores,
		"recipe": recipe_name,
		"workstation": workstation,
		"dst_blocked": dst_blocked,
		"fabric_type": fabric_type,
		"design_type": design_type,
		"garment_color": garment_color,
		"rush": rush,
	}


def route_from_work_order(work_order_name: str, force: bool = False) -> dict:
	"""
	Routes all Job Cards associated with a Work Order.
	Returns a summary of results for each Job Card.
	"""
	job_cards = frappe.get_list(
		"Job Card",
		filters={"work_order": work_order_name},
		fields=["name"],
		order_by="creation asc",
	)

	results = []
	for jc in job_cards:
		result = route_job_card(jc.name, force=force)
		results.append(result)

	success = sum(1 for r in results if r.get("ok"))
	failed = len(results) - success

	return {
		"ok": True,
		"work_order": work_order_name,
		"total": len(results),
		"success": success,
		"failed": failed,
		"results": results,
	}


def get_routing_recommendation(
	fabric_type: str,
	design_type: str,
	garment_color: str = None,
	rush: bool = False,
) -> dict:
	"""
	Dry-run routing — returns scores and winner without touching any DocType.
	Used by the ALICE OS DECORATION panel for what-if analysis.
	"""
	routing = route_decoration(fabric_type, design_type, garment_color, rush)
	return {
		"ok": True,
		"winner": routing["winner"],
		"scores": routing["scores"],
		"fabric_type": fabric_type,
		"design_type": design_type,
		"garment_color": garment_color,
		"rush": rush,
	}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _extract_routing_inputs(jc) -> tuple[str | None, str | None, str | None, bool]:
	"""
	Extracts fabric_type, design_type, garment_color, rush from a Job Card.

	Lookup priority for fabric_type:
	  1. jc.fabric_type (custom field if set)
	  2. Work Order → Item → Item's fabric_type custom field
	  3. None (will block routing)

	Lookup priority for design_type:
	  1. jc.design_type (custom field if set)
	  2. Work Order → custom attribute 'Design Type'
	  3. None
	"""
	fabric_type = jc.get("fabric_type")
	design_type = jc.get("design_type")
	garment_color = jc.get("garment_color")
	rush = bool(jc.get("rush_flag") or jc.get("is_rush"))

	# Fallback: pull from Item via Work Order
	if not fabric_type and jc.get("work_order"):
		wo = frappe.get_cached_doc("Work Order", jc.work_order)
		if wo.production_item:
			fabric_type = frappe.db.get_value(
				"Item", wo.production_item, "fabric_type"
			)
		if not design_type:
			design_type = wo.get("design_type") or frappe.db.get_value(
				"Work Order", jc.work_order, "design_type"
			)
		if not garment_color:
			garment_color = wo.get("garment_color")
		if not rush:
			rush = bool(wo.get("rush_flag") or wo.get("is_rush"))

	return fabric_type, design_type, garment_color, rush


def _stamp_routing_result(
	jc,
	winner: str,
	recipe_name: str,
	workstation: str,
	scores: dict,
	dst_blocked: bool,
) -> None:
	"""Writes all routing results to the Job Card in one save."""
	jc.decoration_method = winner
	jc.production_recipe = recipe_name
	jc.decoration_routed = 1
	jc.decoration_router_scores = json.dumps(scores, indent=2)

	# Stamp machine params from recipe onto Job Card fields
	recipe = frappe.get_doc("Production Recipe", recipe_name)
	params = recipe.get_machine_params()

	if winner == DecoMethod.DTG:
		jc.dtg_platen_size = params.get("platen_size")
		jc.dtg_pretreat_required = int(params.get("pretreat_required", False))
		jc.dtg_cure_temp = params.get("cure_temp_f")
		jc.dtg_cure_time = params.get("cure_time_sec")
	elif winner == DecoMethod.DTF:
		jc.dtf_press_temp = params.get("press_temp_f")
		jc.dtf_dwell_time = params.get("dwell_time_sec")
		jc.dtf_pressure = params.get("pressure")
		jc.dtf_peel_type = params.get("peel_type")
	elif winner == DecoMethod.EMB:
		jc.emb_dst_file = params.get("dst_file")
		jc.emb_stitch_count = params.get("stitch_count")
		jc.emb_hoop_size = params.get("hoop_size")
		jc.emb_stabilizer_type = params.get("stabilizer_type")

	# Update workstation on the Job Card's operation
	if workstation and not dst_blocked:
		try:
			frappe.db.set_value("Job Card", jc.name, "workstation", workstation)
		except Exception:
			pass  # workstation field may be read-only depending on status

	jc.flags.ignore_validate_update_after_submit = True
	jc.save(ignore_permissions=True)

	frappe.publish_realtime(
		"decoration_routed",
		{
			"job_card": jc.name,
			"decoration_method": winner,
			"recipe": recipe_name,
			"dst_blocked": dst_blocked,
		},
		room=frappe.local.site,
	)


# ---------------------------------------------------------------------------
# Whitelisted API wrappers
# ---------------------------------------------------------------------------

@frappe.whitelist()
def api_route_job_card(job_card_name: str, force: int = 0) -> dict:
	"""API endpoint for manual re-routing from the Job Card form."""
	return route_job_card(job_card_name, force=bool(force))


@frappe.whitelist()
def api_route_work_order(work_order_name: str, force: int = 0) -> dict:
	"""API endpoint to bulk-route all Job Cards on a Work Order."""
	return route_from_work_order(work_order_name, force=bool(force))


@frappe.whitelist()
def api_get_recommendation(
	fabric_type: str,
	design_type: str,
	garment_color: str = None,
	rush: int = 0,
) -> dict:
	"""Dry-run recommendation — no DocType writes."""
	return get_routing_recommendation(fabric_type, design_type, garment_color, bool(rush))
