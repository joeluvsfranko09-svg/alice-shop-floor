# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt
"""
Work Order Scheduler
====================
Hook: fires on Work Order on_submit.

For every ZAZFIT Work Order that carries a Production Recipe, this module
auto-creates one decoration Job Card and enqueues the DecorationRouter to
assign DTG / DTF / Embroidery workstation + machine.

Flow
----
  Work Order submitted
      └─ create_decoration_job_cards_for_work_order(doc, method)
            ├─ guard: skip if no production_recipe
            ├─ guard: idempotency (deco_jc_created flag + DB check)
            ├─ _build_job_card(wo, recipe) → dict
            ├─ frappe.get_doc(jc_dict).insert()
            ├─ stamp Work Order: deco_jc_created=1, deco_job_card=jc.name,
            │                    decoration_method=recipe.decoration_method
            └─ enqueue decoration_engine.route_job_card (queue="short")

Public API (callable from api.py stub)
---------------------------------------
  create_decoration_job_cards(work_order_name) → dict
      Manual trigger — safe to call on already-submitted Work Orders.
      Returns { ok, job_card, work_order, decoration_method, already_existed }.
"""

import frappe
from frappe import _
from frappe.utils import today, now_datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WORKSTATION_MAP = {
    "DTG":        "DTG Station",
    "DTF":        "DTF Print Station",
    "Embroidery": "Embroidery Station",
}

# Fields propagated from Work Order → Job Card when present
_WO_PROPAGATE_FIELDS = [
    "shopify_order_id",
    "shopify_line_item_id",
    "customer_name",
    "customer_email",
    "canvas_json",
]


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def create_decoration_job_cards_for_work_order(doc, method=None):
    """
    Called by hooks.py on Work Order on_submit.

    Silently returns for Work Orders without a production_recipe — those
    are standard manufacturing WOs that don't go through the decoration
    pipeline.
    """
    recipe_name = doc.get("production_recipe")
    if not recipe_name:
        return  # Non-decorated WO — not our concern

    if doc.get("deco_jc_created"):
        frappe.logger().info(
            f"[WOScheduler] WO {doc.name} already has decoration JC — skipping."
        )
        return

    try:
        result = _create_jc_for_wo(doc.name, recipe_name)
        if result.get("ok"):
            frappe.logger().info(
                f"[WOScheduler] Created {result['job_card']} for WO {doc.name} "
                f"(method={result['decoration_method']})"
            )
    except Exception as exc:
        # Never block Work Order submission — log and continue
        frappe.log_error(
            message=frappe.get_traceback(),
            title=f"WOScheduler: failed to create JC for {doc.name}",
        )


# ---------------------------------------------------------------------------
# Public callable (also used by api.py stub)
# ---------------------------------------------------------------------------

def create_decoration_job_cards(work_order_name: str) -> dict:
    """
    Manual trigger — idempotent.

    Usable from the api.py stub, bench console, or Frappe scheduled tasks.
    Returns a result dict so the caller can inspect what happened.
    """
    if not work_order_name:
        frappe.throw(_("work_order_name is required"), frappe.ValidationError)

    wo = frappe.get_doc("Work Order", work_order_name)
    recipe_name = wo.get("production_recipe")

    if not recipe_name:
        return {
            "ok":    False,
            "error": "no_recipe",
            "detail": (
                f"Work Order {work_order_name} has no Production Recipe linked. "
                "Set the production_recipe field and try again."
            ),
            "work_order": work_order_name,
        }

    # Idempotency: if JC already recorded on WO, return it
    if wo.get("deco_jc_created") and wo.get("deco_job_card"):
        return {
            "ok":               True,
            "already_existed":  True,
            "job_card":         wo.deco_job_card,
            "work_order":       work_order_name,
            "decoration_method": wo.get("decoration_method") or "",
        }

    # Secondary idempotency: DB check in case flag wasn't written
    existing_jc = frappe.db.get_value(
        "Job Card",
        {"work_order": work_order_name, "production_recipe": recipe_name},
        "name",
    )
    if existing_jc:
        # Repair the flag if it was lost
        frappe.db.set_value("Work Order", work_order_name, {
            "deco_jc_created": 1,
            "deco_job_card":   existing_jc,
        })
        frappe.db.commit()
        return {
            "ok":               True,
            "already_existed":  True,
            "job_card":         existing_jc,
            "work_order":       work_order_name,
            "decoration_method": frappe.db.get_value("Job Card", existing_jc, "decoration_method") or "",
        }

    return _create_jc_for_wo(work_order_name, recipe_name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _create_jc_for_wo(work_order_name: str, recipe_name: str) -> dict:
    """
    Core creation logic. Assumes idempotency guards have already passed.
    """
    recipe = frappe.get_doc("Production Recipe", recipe_name)

    if not recipe.is_active:
        frappe.log_error(
            f"Production Recipe {recipe_name} is inactive — "
            f"cannot create Job Card for WO {work_order_name}",
            "WOScheduler: inactive recipe",
        )
        return {
            "ok":    False,
            "error": "recipe_inactive",
            "detail": f"Production Recipe {recipe_name} is marked inactive.",
            "work_order": work_order_name,
        }

    decoration_method = recipe.decoration_method or ""
    workstation = _WORKSTATION_MAP.get(decoration_method, "DTG Station")

    company = (
        frappe.db.get_value("Work Order", work_order_name, "company")
        or (frappe.get_all("Company", limit=1, pluck="name") or [""])[0]
    )

    # ── Create the bare Job Card ─────────────────────────────────────────────
    jc = frappe.get_doc({
        "doctype":      "Job Card",
        "work_order":   work_order_name,
        "company":      company,
        "posting_date": today(),
        "workstation":  workstation,
    })
    jc.insert(ignore_permissions=True)

    # ── Stamp all decoration fields ──────────────────────────────────────────
    stamp = _build_stamp_fields(recipe, decoration_method, work_order_name)
    frappe.db.set_value("Job Card", jc.name, stamp)

    # ── Propagate WO fields to JC ────────────────────────────────────────────
    wo_vals = frappe.db.get_value(
        "Work Order", work_order_name, _WO_PROPAGATE_FIELDS, as_dict=True
    ) or {}
    propagate = {k: v for k, v in wo_vals.items() if v}
    if propagate:
        frappe.db.set_value("Job Card", jc.name, propagate)

    frappe.db.commit()

    # ── Mark Work Order as scheduled ─────────────────────────────────────────
    frappe.db.set_value("Work Order", work_order_name, {
        "deco_jc_created":   1,
        "deco_job_card":     jc.name,
        "decoration_method": decoration_method,
    })
    frappe.db.commit()

    # ── Enqueue decoration routing (non-blocking) ────────────────────────────
    try:
        frappe.enqueue(
            "alice_shop_floor.alice_shop_floor.decoration_engine.route_job_card",
            queue="short",
            timeout=120,
            job_card_name=jc.name,
        )
    except Exception:
        # Routing failure must not break WO submission
        frappe.logger().warning(
            f"[WOScheduler] Could not enqueue router for {jc.name} — "
            "will be picked up by the 5-minute scheduler poll."
        )

    return {
        "ok":               True,
        "already_existed":  False,
        "job_card":         jc.name,
        "work_order":       work_order_name,
        "decoration_method": decoration_method,
    }


def _build_stamp_fields(recipe, decoration_method: str, work_order_name: str) -> dict:
    """
    Build the dict of custom fields to stamp onto the new Job Card.
    Always stamps core fields; stamps method-specific params section.
    """
    stamp = {
        "decoration_method": decoration_method,
        "production_recipe": recipe.name,
        "design_placement":  recipe.design_placement or "",
        "decoration_routed": 0,
    }

    if decoration_method == "DTG":
        stamp.update({
            "dtg_platen_size":       recipe.dtg_platen_size or "",
            "dtg_pretreat_required": int(recipe.dtg_pretreat_required or 0),
            "dtg_cure_temp":         float(recipe.dtg_cure_temp or 0),
            "dtg_cure_time":         int(recipe.dtg_cure_time or 0),
        })

    elif decoration_method == "DTF":
        stamp.update({
            "dtf_press_temp":  float(recipe.dtf_press_temp or 0),
            "dtf_dwell_time":  int(recipe.dtf_dwell_time or 0),
            "dtf_pressure":    recipe.dtf_pressure or "",
            "dtf_peel_type":   recipe.dtf_peel_type or "",
        })

    elif decoration_method == "Embroidery":
        stamp.update({
            "emb_dst_file":        recipe.emb_dst_file or "",
            "emb_stitch_count":    int(recipe.emb_stitch_count or 0),
            "emb_hoop_size":       recipe.emb_hoop_size or "",
            "emb_stabilizer_type": recipe.emb_stabilizer_type or "",
        })

    return stamp
