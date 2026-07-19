"""
SanMar Catalog Sync
====================
Pulls SanMar style/color/fit data into ERPNext Items and SanMar Style Map.

Called by:
  - Scheduled task  : tasks.run_sanmar_catalog_sync()  (daily)
  - Manual trigger  : api.sanmar_sync_catalog()
  - CLI             : bench execute alice_shop_floor.alice_shop_floor.sanmar.catalog_sync.run

Flow
────
1.  Load SanMar Config — get style filter list (or "all").
2.  For each style:
    a.  Call client.get_product(style) → product dict with colors/fits.
    b.  For each color × fit combination:
        i.   Upsert SanMar Style Map (create or update fields).
        ii.  If no ERPNext Item linked yet, create one (Item Code = sanmar_sku).
3.  Write back last_catalog_sync + status to SanMar Config.
"""

import frappe
from frappe.utils import now_datetime

from alice_shop_floor.alice_shop_floor.sanmar.client import (
    SanMarClient, SanMarAPIError, SanMarConfigMissing
)


# ─────────────────────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────────────────────

def run():
    """Main entry — called by scheduled task and manual trigger."""
    try:
        config = frappe.get_single("SanMar Config")
    except Exception:
        frappe.log_error("SanMar Config not found — skipping catalog sync", "SanMar Catalog Sync")
        return

    if not config.catalog_sync_enabled:
        return

    try:
        client  = SanMarClient.from_config(config)
        styles  = _get_style_list(config)
        created = updated = errors = 0

        for style in styles:
            try:
                product = client.get_product(style)
                if not product:
                    continue
                c, u = _upsert_style(product, config)
                created += c
                updated += u
            except SanMarAPIError as e:
                errors += 1
                frappe.log_error(f"SanMar catalog sync failed for style {style}: {e}",
                                 "SanMar Catalog Sync")

        msg = f"Synced {len(styles)} styles — {created} created, {updated} updated, {errors} errors"
        _write_status(config, "OK", msg)
        frappe.logger().info("SanMar Catalog Sync: " + msg)

    except (SanMarConfigMissing, SanMarAPIError) as e:
        _write_status(config, "Error", str(e))
        frappe.log_error(str(e), "SanMar Catalog Sync")


# ─────────────────────────────────────────────────────────────────────────────
# Internal
# ─────────────────────────────────────────────────────────────────────────────

def _get_style_list(config) -> list[str]:
    """Return list of style numbers to sync, or an empty list meaning 'all'."""
    raw = (config.sync_style_list or "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    # No filter set — return a sensible default set of ZAZFIT-relevant blanks.
    # Expand this list as the SanMar relationship matures.
    return [
        "PC61",   # Port & Company Essential Tee
        "PC54",   # Port & Company Core Cotton Tee
        "DT6000", # District Very Important Tee
        "G500",   # Gildan Heavy Cotton Tee
        "ST650",  # Sport-Tek Competitor Tee
        "LT202",  # Port Authority Ladies Tee
        "PC61LS", # Port & Company Essential Long-Sleeve
        "PC78H",  # Port & Company Core Fleece Hooded Sweatshirt
        "PC850",  # Port & Company Fan Favorite Tee
        "DT5000", # District The Concert Tee
    ]


def _upsert_style(product: dict, config) -> tuple[int, int]:
    """
    Create or update SanMar Style Map + ERPNext Item for every color×fit in
    `product`.  Returns (created_count, updated_count).
    """
    created = updated = 0
    style       = product["style"]
    brand       = product.get("brand", "")
    prod_name   = product.get("product_name", "")

    for entry in product.get("colors", []):
        sku        = entry.get("sanmar_sku") or f"{style}-{entry['color_name']}-{entry['fit_code']}"
        color_name = entry.get("color_name", "")
        color_code = entry.get("color_code", "")
        fit_code   = entry.get("fit_code", "")
        fit_label  = entry.get("fit_label", fit_code)

        if not sku:
            continue

        existing = frappe.db.exists("SanMar Style Map", sku)

        if existing:
            frappe.db.set_value("SanMar Style Map", sku, {
                "sanmar_style": style,
                "color_name":   color_name,
                "color_code":   color_code,
                "fit_code":     fit_code,
                "brand_name":   brand,
                "product_name": prod_name,
                "is_active":    1,
            })
            updated += 1
        else:
            doc = frappe.new_doc("SanMar Style Map")
            doc.sanmar_sku   = sku
            doc.sanmar_style = style
            doc.color_name   = color_name
            doc.color_code   = color_code
            doc.fit_code     = fit_code
            doc.brand_name   = brand
            doc.product_name = prod_name
            doc.is_active    = 1
            # Auto-create or link ERPNext Item
            doc.erpnext_item = _ensure_erpnext_item(sku, style, brand, prod_name,
                                                    color_name, fit_code, config)
            doc.insert(ignore_permissions=True)
            created += 1

    frappe.db.commit()
    return created, updated


def _ensure_erpnext_item(sku: str, style: str, brand: str, prod_name: str,
                          color_name: str, fit_code: str, config) -> str:
    """
    Return the ERPNext Item name for this SKU, creating it if absent.
    Item Code = sanmar_sku.  Item Name = "{brand} {prod_name} — {color} Fit {fit_code}".
    ZAZFIT brand bible: 'fit' not 'size'.
    """
    if frappe.db.exists("Item", sku):
        return sku

    item_name = f"{brand} {prod_name} — {color_name} / Fit {fit_code}".strip(" —/")
    item_group = config.item_group or "Blank Apparel"

    # Ensure item group exists
    if not frappe.db.exists("Item Group", item_group):
        ig = frappe.new_doc("Item Group")
        ig.item_group_name  = item_group
        ig.parent_item_group = "All Item Groups"
        ig.insert(ignore_permissions=True)

    item = frappe.new_doc("Item")
    item.item_code      = sku
    item.item_name      = item_name
    item.item_group     = item_group
    item.description    = f"SanMar {style} — {color_name} / Fit {fit_code}. Brand: {brand}."
    item.is_purchase_item = 1
    item.is_sales_item    = 0
    item.is_stock_item    = 1
    item.stock_uom        = "Nos"

    # Custom fields the main app adds to Item
    item.custom_sanmar_sku   = sku
    item.custom_sanmar_style = style
    item.custom_color_name   = color_name
    item.custom_fit_code     = fit_code

    item.insert(ignore_permissions=True)
    return item.item_code


def _write_status(config, status: str, message: str):
    frappe.db.set_value("SanMar Config", None, {
        "last_catalog_sync":    now_datetime(),
        "catalog_sync_status":  status,
        "catalog_sync_message": message[:500],
    })
    frappe.db.commit()
