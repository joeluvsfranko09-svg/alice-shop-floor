"""
SanMar Live Stock Lookup
=========================
Checks SanMar inventory for a given SKU (or style+color+fit).

Cache strategy
──────────────
1.  Check SanMar Stock Cache for a non-expired record → return immediately.
2.  On cache miss, call the SanMar API → write result to cache → return.

TTL is configurable in SanMar Config (default 15 minutes).
The scheduled task (every 30 min) does a bulk refresh of all active SKUs.

Public API
──────────
  check_sku(sanmar_sku)          → StockResult
  check_style(style, color, fit) → list[StockResult]
  bulk_refresh_cache()           → called by scheduler
"""

import json
import frappe
from frappe.utils import now_datetime, add_to_date

from alice_shop_floor.alice_shop_floor.sanmar.client import (
    SanMarClient, SanMarAPIError, SanMarConfigMissing
)


class StockResult:
    """Simple value object for a single SKU stock check."""

    def __init__(self, sanmar_sku: str, total_qty: int, status: str,
                 warehouses: list, from_cache: bool = False):
        self.sanmar_sku  = sanmar_sku
        self.total_qty   = total_qty
        self.status      = status
        self.warehouses  = warehouses   # [{name, qty}]
        self.from_cache  = from_cache

    def is_available(self, needed_qty: int = 1) -> bool:
        return self.total_qty >= needed_qty

    def to_dict(self) -> dict:
        return {
            "sanmar_sku":  self.sanmar_sku,
            "total_qty":   self.total_qty,
            "status":      self.status,
            "warehouses":  self.warehouses,
            "from_cache":  self.from_cache,
            "available":   self.total_qty > 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public look-ups
# ─────────────────────────────────────────────────────────────────────────────

def check_sku(sanmar_sku: str, force_live: bool = False) -> StockResult:
    """
    Return stock for a single SanMar SKU.

    :param force_live: bypass cache and always call the API.
    """
    if not force_live:
        cached = _read_cache(sanmar_sku)
        if cached:
            return cached

    config = _get_config()
    style, color, fit = _parse_sku(sanmar_sku)
    client  = SanMarClient.from_config(config)

    rows = client.get_inventory(style, color_name=color, fit_code=fit)
    # Find the specific SKU in the response
    for row in rows:
        if row["sanmar_sku"] == sanmar_sku:
            result = StockResult(
                sanmar_sku = sanmar_sku,
                total_qty  = row["total_qty"],
                status     = row["status"],
                warehouses = row["warehouses"],
                from_cache = False,
            )
            _write_cache(result, config)
            return result

    # SKU not found in response — treat as out of stock
    result = StockResult(sanmar_sku, 0, "Out of Stock", [], False)
    _write_cache(result, config)
    return result


def check_style(style: str, color_name: str = None, fit_code: str = None,
                force_live: bool = False) -> list[StockResult]:
    """
    Return stock for a style (and optionally filtered to color/fit).
    Checks cache for each individual SKU; only calls API for cache misses.
    """
    config  = _get_config()
    client  = SanMarClient.from_config(config)
    rows    = client.get_inventory(style, color_name=color_name, fit_code=fit_code)

    results = []
    for row in rows:
        result = StockResult(
            sanmar_sku = row["sanmar_sku"],
            total_qty  = row["total_qty"],
            status     = row["status"],
            warehouses = row["warehouses"],
            from_cache = False,
        )
        _write_cache(result, config)
        results.append(result)

    return results


def bulk_refresh_cache():
    """
    Refresh the stock cache for all active SanMar Style Map entries.
    Called by the every_30_minutes scheduler task.
    """
    try:
        config = _get_config()
    except (SanMarConfigMissing, Exception):
        return

    if not config.stock_cache_enabled:
        return

    try:
        client = SanMarClient.from_config(config)
    except SanMarConfigMissing:
        return

    # Get distinct styles with active mappings
    styles = frappe.db.get_all(
        "SanMar Style Map",
        filters={"is_active": 1},
        fields=["sanmar_style"],
        distinct=True,
        pluck="sanmar_style",
    )

    refreshed = errors = 0
    seen = set()

    for style in styles:
        if style in seen:
            continue
        seen.add(style)
        try:
            rows = client.get_inventory(style)
            for row in rows:
                result = StockResult(
                    sanmar_sku = row["sanmar_sku"],
                    total_qty  = row["total_qty"],
                    status     = row["status"],
                    warehouses = row["warehouses"],
                    from_cache = False,
                )
                _write_cache(result, config)
                # Also update SanMar Style Map with latest stock
                if frappe.db.exists("SanMar Style Map", row["sanmar_sku"]):
                    frappe.db.set_value("SanMar Style Map", row["sanmar_sku"], {
                        "last_known_qty":  row["total_qty"],
                        "stock_status":    row["status"],
                        "stock_checked_at": now_datetime(),
                        "warehouse_breakdown": json.dumps(row["warehouses"]),
                    })
            refreshed += 1
        except SanMarAPIError as e:
            errors += 1
            frappe.log_error(f"SanMar stock cache refresh failed for {style}: {e}",
                             "SanMar Stock Cache")

    frappe.db.set_value("SanMar Config", None, "last_stock_cache", now_datetime())
    frappe.db.commit()
    frappe.logger().info(f"SanMar stock cache: {refreshed} styles refreshed, {errors} errors")


# ─────────────────────────────────────────────────────────────────────────────
# Cache read/write
# ─────────────────────────────────────────────────────────────────────────────

def _read_cache(sanmar_sku: str) -> StockResult | None:
    """Return a StockResult from cache if it's still fresh, else None."""
    now = now_datetime()
    row = frappe.db.get_value(
        "SanMar Stock Cache",
        sanmar_sku,
        ["total_qty", "stock_status", "warehouse_json", "expires_at"],
        as_dict=True,
    )
    if not row:
        return None
    if row.expires_at and row.expires_at < now:
        return None  # stale

    warehouses = []
    try:
        warehouses = json.loads(row.warehouse_json or "[]")
    except Exception:
        pass

    return StockResult(
        sanmar_sku = sanmar_sku,
        total_qty  = row.total_qty or 0,
        status     = row.stock_status or "Unknown",
        warehouses = warehouses,
        from_cache = True,
    )


def _write_cache(result: StockResult, config):
    ttl = int(config.stock_cache_ttl_minutes or 15)
    now = now_datetime()
    expires = add_to_date(now, minutes=ttl)

    data = {
        "sanmar_sku":    result.sanmar_sku,
        "sanmar_style":  _parse_sku(result.sanmar_sku)[0],
        "color_name":    _parse_sku(result.sanmar_sku)[1],
        "fit_code":      _parse_sku(result.sanmar_sku)[2],
        "total_qty":     result.total_qty,
        "stock_status":  result.status,
        "warehouse_json": json.dumps(result.warehouses),
        "cached_at":     now,
        "expires_at":    expires,
    }

    if frappe.db.exists("SanMar Stock Cache", result.sanmar_sku):
        frappe.db.set_value("SanMar Stock Cache", result.sanmar_sku, data)
    else:
        doc = frappe.new_doc("SanMar Stock Cache")
        doc.update(data)
        doc.insert(ignore_permissions=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sku(sku: str) -> tuple[str, str, str]:
    """Split STYLE-COLOR-FIT.  Handles styles with hyphens (e.g. PC61LS)."""
    parts = sku.rsplit("-", 2)
    style = parts[0] if len(parts) >= 1 else sku
    color = parts[1] if len(parts) >= 2 else ""
    fit   = parts[2] if len(parts) >= 3 else ""
    return style, color, fit


def _get_config():
    try:
        return frappe.get_single("SanMar Config")
    except Exception:
        raise SanMarConfigMissing("SanMar Config not found")
