"""
SanMar Pricing Sync
====================
Pulls SanMar net pricing into the ERPNext Item Price list.

Price list name = SanMar Config → erpnext_price_list  (default: "SanMar Purchase")
Currency = USD.

Called by:
  - Scheduled task  : tasks.run_sanmar_pricing_sync()  (daily)
  - Manual trigger  : api.sanmar_sync_pricing()
"""

import frappe
from frappe.utils import now_datetime

from alice_shop_floor.alice_shop_floor.sanmar.client import (
    SanMarClient, SanMarAPIError, SanMarConfigMissing
)


def run():
    """Main entry — called by scheduled task and manual trigger."""
    try:
        config = frappe.get_single("SanMar Config")
    except Exception:
        frappe.log_error("SanMar Config not found — skipping pricing sync", "SanMar Pricing Sync")
        return

    if not config.pricing_sync_enabled:
        return

    try:
        client     = SanMarClient.from_config(config)
        price_list = config.erpnext_price_list or "SanMar Purchase"
        _ensure_price_list(price_list)

        # Get distinct styles from Style Map
        styles = frappe.db.get_all(
            "SanMar Style Map",
            filters={"is_active": 1},
            fields=["sanmar_style"],
            distinct=True,
            pluck="sanmar_style",
        )

        synced = errors = 0
        seen_styles = set()

        for style in styles:
            if style in seen_styles:
                continue
            seen_styles.add(style)
            try:
                prices = client.get_pricing(style)
                for p in prices:
                    _upsert_item_price(p, price_list)
                    # Write back to Style Map
                    if frappe.db.exists("SanMar Style Map", p["sanmar_sku"]):
                        frappe.db.set_value("SanMar Style Map", p["sanmar_sku"], {
                            "net_price":      p["net_price"],
                            "case_price":     p["case_price"],
                            "price_synced_at": now_datetime(),
                        })
                synced += 1
            except SanMarAPIError as e:
                errors += 1
                frappe.log_error(f"SanMar pricing sync failed for {style}: {e}",
                                 "SanMar Pricing Sync")

        frappe.db.set_value("SanMar Config", None, "last_pricing_sync", now_datetime())
        frappe.db.commit()

        msg = f"Pricing sync: {synced} styles synced, {errors} errors"
        frappe.logger().info("SanMar Pricing Sync: " + msg)

    except (SanMarConfigMissing, SanMarAPIError) as e:
        frappe.log_error(str(e), "SanMar Pricing Sync")


def _ensure_price_list(name: str):
    if not frappe.db.exists("Price List", name):
        pl = frappe.new_doc("Price List")
        pl.price_list_name = name
        pl.currency        = "USD"
        pl.buying          = 1
        pl.selling         = 0
        pl.enabled         = 1
        pl.insert(ignore_permissions=True)


def _upsert_item_price(price: dict, price_list: str):
    """Create or update an Item Price record for this SKU."""
    sku       = price["sanmar_sku"]
    net_price = price.get("net_price", 0) or 0

    if not sku or net_price <= 0:
        return

    # Check if Item exists in ERPNext (may not be synced yet)
    if not frappe.db.exists("Item", sku):
        return

    existing = frappe.db.get_value(
        "Item Price",
        {"item_code": sku, "price_list": price_list},
        "name",
    )

    if existing:
        frappe.db.set_value("Item Price", existing, "price_list_rate", net_price)
    else:
        ip = frappe.new_doc("Item Price")
        ip.item_code       = sku
        ip.price_list      = price_list
        ip.price_list_rate = net_price
        ip.currency        = "USD"
        ip.buying          = 1
        ip.insert(ignore_permissions=True)
