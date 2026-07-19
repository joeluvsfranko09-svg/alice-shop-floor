"""
SanMar Auto Purchase Order Creator
=====================================
When a Work Order is submitted and needs blank garments that aren't in stock,
this module can automatically create an ERPNext Purchase Order to SanMar
AND (if enabled) submit it directly to SanMar's PO API.

Trigger: Work Order on_submit → check_and_auto_po_for_work_order()
Manual:  api.sanmar_create_po_for_work_order(work_order_name)

Safety gates
─────────────
- SanMar Config.auto_po_enabled must be True (default False).
- PO is created in Draft state by default — requires human review before submit.
- If the blank is already in the ERPNext warehouse, no PO is created.
- Idempotent: won't create a second PO if one already exists for the same WO.
"""

import frappe
from frappe.utils import today, add_days

from alice_shop_floor.alice_shop_floor.sanmar.client import (
    SanMarClient, SanMarAPIError, SanMarConfigMissing
)
from alice_shop_floor.alice_shop_floor.sanmar.stock_lookup import check_sku


# ─────────────────────────────────────────────────────────────────────────────
# Work Order hook entry point
# ─────────────────────────────────────────────────────────────────────────────

def check_and_auto_po_for_work_order(doc, method=None):
    """
    Called from Work Order on_submit (when auto_po_enabled).
    Checks if the WO's blank item needs to be ordered from SanMar.
    Creates a draft PO if so.
    """
    try:
        config = frappe.get_single("SanMar Config")
    except Exception:
        return

    if not config.auto_po_enabled:
        return

    work_order_name = doc.name
    # Check if a PO already exists for this WO
    existing = frappe.db.exists("Purchase Order Item", {"sales_order": work_order_name})
    if existing:
        return

    result = create_po_for_work_order(work_order_name, config=config, submit_to_sanmar=False)
    if result.get("ok"):
        frappe.msgprint(
            f"SanMar Purchase Order {result['po_name']} created for blank garments. "
            "Review and submit when ready.",
            indicator="blue",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main PO creation logic
# ─────────────────────────────────────────────────────────────────────────────

def create_po_for_work_order(work_order_name: str, config=None,
                              submit_to_sanmar: bool = False) -> dict:
    """
    Inspect the Work Order's BOM for blank items mapped to SanMar.
    Create an ERPNext Purchase Order (Draft) if any are needed.
    Optionally submit the PO to SanMar's API.

    Returns {ok, po_name, lines_ordered, message}
    """
    if config is None:
        try:
            config = frappe.get_single("SanMar Config")
        except Exception:
            return {"ok": False, "po_name": "", "message": "SanMar Config not found"}

    wo = frappe.get_doc("Work Order", work_order_name)
    if not wo:
        return {"ok": False, "po_name": "", "message": "Work Order not found"}

    supplier = config.erpnext_supplier
    if not supplier:
        return {"ok": False, "po_name": "",
                "message": "SanMar Config: ERPNext Supplier (SanMar) is not set."}

    # Gather items to order
    lines_to_order = _get_lines_to_order(wo, config)
    if not lines_to_order:
        return {
            "ok": True, "po_name": "", "lines_ordered": 0,
            "message": "No SanMar blanks needed — all items in local stock or not mapped."
        }

    # Build ERPNext Purchase Order
    po = frappe.new_doc("Purchase Order")
    po.supplier          = supplier
    po.schedule_date     = add_days(today(), 5)   # expected delivery: 5 business days
    po.transaction_date  = today()
    po.buying_price_list = config.erpnext_price_list or "SanMar Purchase"
    po.currency          = "USD"

    # Link back to Work Order in custom field if available
    if hasattr(po, "custom_work_order"):
        po.custom_work_order = work_order_name

    for line in lines_to_order:
        po.append("items", {
            "item_code":      line["item_code"],
            "qty":            line["qty"],
            "schedule_date":  po.schedule_date,
            "warehouse":      config.default_warehouse or "",
            "description":    line.get("description", ""),
        })

    po.insert(ignore_permissions=True)
    frappe.db.commit()

    result = {
        "ok":           True,
        "po_name":      po.name,
        "lines_ordered": len(lines_to_order),
        "message":      f"Purchase Order {po.name} created in Draft — review before submitting.",
    }

    # Optionally push to SanMar API
    if submit_to_sanmar:
        sanmar_result = _submit_po_to_sanmar(po, lines_to_order, config)
        result["sanmar_response"] = sanmar_result
        if sanmar_result.get("ok"):
            frappe.db.set_value("Purchase Order", po.name,
                                "custom_sanmar_po_id", sanmar_result.get("sanmar_po_id", ""))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Determine what needs to be ordered
# ─────────────────────────────────────────────────────────────────────────────

def _get_lines_to_order(wo, config) -> list[dict]:
    """
    Walk the Work Order's required items and find any that:
      1. Are mapped in SanMar Style Map.
      2. Have insufficient ERPNext warehouse stock to cover WO qty.

    Returns list of {item_code, sanmar_sku, qty, description}.
    """
    lines = []
    warehouse = config.default_warehouse or wo.fg_warehouse or ""

    for row in (wo.required_items or []):
        item_code = row.item_code
        qty_required = row.required_qty or wo.qty or 1

        # Is this item mapped to SanMar?
        sanmar_sku = frappe.db.get_value(
            "SanMar Style Map",
            {"erpnext_item": item_code, "is_active": 1},
            "sanmar_sku",
        )
        if not sanmar_sku:
            continue

        # Check local warehouse stock
        local_qty = _get_local_qty(item_code, warehouse)
        needed    = max(0, qty_required - local_qty)

        if needed <= 0:
            continue  # enough in stock

        lines.append({
            "item_code":   item_code,
            "sanmar_sku":  sanmar_sku,
            "qty":         needed,
            "description": f"SanMar blank for WO {wo.name} — {sanmar_sku}",
        })

    return lines


def _get_local_qty(item_code: str, warehouse: str) -> float:
    """Return ERPNext actual qty for item at warehouse."""
    if not warehouse:
        return 0
    qty = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        "actual_qty",
    )
    return float(qty or 0)


# ─────────────────────────────────────────────────────────────────────────────
# Submit to SanMar API
# ─────────────────────────────────────────────────────────────────────────────

def _submit_po_to_sanmar(po, lines: list[dict], config) -> dict:
    """
    Push the Purchase Order to SanMar's PO API.
    Returns the SanMar confirmation dict.
    """
    try:
        client   = SanMarClient.from_config(config)
        ship_to  = _build_ship_to(config)

        sanmar_lines = [
            {"sanmar_sku": line["sanmar_sku"], "qty": int(line["qty"])}
            for line in lines
        ]

        result = client.submit_purchase_order({
            "po_number": po.name,
            "ship_to":   ship_to,
            "lines":     sanmar_lines,
        })
        return result

    except (SanMarAPIError, SanMarConfigMissing) as e:
        frappe.log_error(f"SanMar PO submission failed for {po.name}: {e}",
                         "SanMar PO Creator")
        return {"ok": False, "message": str(e)}


def _build_ship_to(config) -> dict:
    """
    Build the ship_to dict from the ERPNext company address.
    Falls back to empty strings if not configured.
    """
    address = {}
    try:
        company = frappe.get_single("Global Defaults").default_company
        addr_name = frappe.db.get_value(
            "Address",
            {"link_name": company, "is_primary_address": 1},
            "name",
        )
        if addr_name:
            a = frappe.get_doc("Address", addr_name)
            address = {
                "company":  company or "ZAZFIT",
                "address1": a.address_line1 or "",
                "city":     a.city or "",
                "state":    a.state or "",
                "zip":      a.pincode or "",
                "country":  a.country or "United States",
            }
    except Exception:
        pass

    return address or {
        "company": "ZAZFIT",
        "address1": "", "city": "", "state": "", "zip": "", "country": "US",
    }
