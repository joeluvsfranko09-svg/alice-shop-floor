"""
alice_shop_floor/teeriot_api.py
ALICE → TeeRiot capacity bridge.

Exposes real-time production load from ERPNext so TeeRiot's airline-style
pricing reflects actual shop floor reality instead of estimates.

Endpoint:
  GET /api/method/alice_shop_floor.teeriot_api.get_teeriot_capacity
  Auth: Frappe API key/secret (Authorization: token <key>:<secret>)

TeeRiot calls this every 5 minutes (cached client-side) to get:
  - Units committed from Work Orders per ISO week (next 4 weeks)
  - Current shift throughput rate (units/hr, live from Production Stage Tracker)
  - Weekly QC pass rate from Cut Inspection Results
  - Active work order count

Set up in Frappe:
  1. Create a User: teeriot-integration@athlettia.com (System User, read-only roles)
  2. Generate API key/secret for that user
  3. Add to Replit Secrets: ALICE_API_KEY, ALICE_API_SECRET
  4. Grant the user "Sales User" + "Stock User" roles (read access to Work Orders)
"""

import frappe
from datetime import datetime, timedelta, date


# ── ISO week helpers ────────────────────────────────────────────────────────

def _iso_week_key(d):
    """Return 'YYYY-Www' for a date."""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _week_date_range(week_key):
    """
    Return (monday, sunday) as date objects for a given 'YYYY-Www' key.
    """
    year, week_num = int(week_key[:4]), int(week_key[6:])
    # ISO: week 1 contains the first Thursday of the year
    jan_4 = date(year, 1, 4)
    week_start = jan_4 - timedelta(days=jan_4.weekday()) + timedelta(weeks=week_num - 1)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


# ── Main endpoint ───────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=False)
def get_teeriot_capacity():
    """
    Returns production capacity data for the next 4 ISO weeks.

    Response shape (matches what teeriot_capacity.js expects):
    {
      "ok": true,
      "source": "alice_erp",
      "generatedAt": "2026-07-15T10:30:00Z",
      "weeklyCapacity": 7500,
      "activeWorkOrders": 12,
      "qcPassRate": 0.97,
      "weeks": [
        {
          "weekKey":      "2026-W29",
          "weekStart":    "2026-07-14",
          "weekEnd":      "2026-07-20",
          "bookedFromErp": 840,
          "workOrders":   9,
          "inProcess":    3,
          "open":         6
        }, ...
      ],
      "currentShift": {
        "unitsCompleted":  145,
        "shiftStarted":    "2026-07-15T06:00:00",
        "throughputRate":  14.5
      }
    }
    """
    today = date.today()
    current_week_key = _iso_week_key(today)

    weeks_data = []
    total_active_wo = 0

    for offset in range(4):
        # Compute the ISO week key for this slot
        target_date = today + timedelta(weeks=offset)
        wk = _iso_week_key(target_date)
        wk_start, wk_end = _week_date_range(wk)

        # Query Work Orders whose planned production falls in this week.
        # We look at planned_start_date falling within the week window.
        # Status: Open or In Process = committed capacity.
        filters = [
            ["status", "in", ["Open", "In Process"]],
            ["planned_start_date", ">=", wk_start.strftime("%Y-%m-%d")],
            ["planned_start_date", "<=", wk_end.strftime("%Y-%m-%d")],
        ]

        work_orders = frappe.get_list(
            "Work Order",
            filters=filters,
            fields=["name", "qty", "produced_qty", "status"],
        )

        booked_units = sum(wo.get("qty", 0) or 0 for wo in work_orders)
        in_process   = sum(1 for wo in work_orders if wo.get("status") == "In Process")
        open_count   = sum(1 for wo in work_orders if wo.get("status") == "Open")
        total_active_wo += len(work_orders)

        weeks_data.append({
            "weekKey":       wk,
            "weekStart":     wk_start.strftime("%Y-%m-%d"),
            "weekEnd":       wk_end.strftime("%Y-%m-%d"),
            "bookedFromErp": booked_units,
            "workOrders":    len(work_orders),
            "inProcess":     in_process,
            "open":          open_count,
        })

    # ── Current shift throughput ──────────────────────────────────────────
    current_shift = _get_current_shift_metrics()

    # ── QC pass rate (last 7 days) ────────────────────────────────────────
    qc_pass_rate = _get_qc_pass_rate()

    return {
        "ok":               True,
        "source":           "alice_erp",
        "generatedAt":      datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weeklyCapacity":   7500,  # mirrors TeeRiot default — update both together
        "activeWorkOrders": total_active_wo,
        "qcPassRate":       qc_pass_rate,
        "weeks":            weeks_data,
        "currentShift":     current_shift,
    }


# ── Shift metrics helper ────────────────────────────────────────────────────

def _get_current_shift_metrics():
    """
    Reads today's Production Stage Tracker records to estimate
    current shift throughput. Returns units completed and rate (units/hr).

    Shift schedule:
      Shift 1:  6:00 AM – 4:00 PM
      Shift 2:  4:00 PM – 2:00 AM
      Shift 3:  2:00 AM – 12:00 PM (next day)
    Sunday: first shift starts 4:00 PM.
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # Determine shift start based on current hour
    hour = now.hour
    if hour < 2:
        # Late night: we're in shift 3 which started previous day at 2am
        shift_start = datetime(now.year, now.month, now.day) - timedelta(hours=(24 - 2))
    elif hour < 6:
        # Early morning: shift 3 (2am–12pm)
        shift_start = datetime(now.year, now.month, now.day, 2, 0, 0)
    elif hour < 16:
        # Day shift: shift 1 (6am–4pm)
        shift_start = datetime(now.year, now.month, now.day, 6, 0, 0)
    else:
        # Evening: shift 2 (4pm–2am)
        shift_start = datetime(now.year, now.month, now.day, 16, 0, 0)

    shift_started_str = shift_start.strftime("%Y-%m-%d %H:%M:%S")

    # Count Production Stage Tracker docs that moved to "Completed" or
    # advanced past "Sewing" since shift start — each represents one jersey done.
    try:
        completed_this_shift = frappe.db.count(
            "Production Stage Tracker",
            filters={
                "current_stage": ["in", ["QC Final", "Completed"]],
                "modified": [">=", shift_started_str],
            },
        )
    except Exception:
        completed_this_shift = 0

    # Hours elapsed in shift (minimum 0.5 to avoid division by zero on shift start)
    hours_elapsed = max(0.5, (now - shift_start).total_seconds() / 3600)
    throughput_rate = round(completed_this_shift / hours_elapsed, 1)

    return {
        "unitsCompleted": completed_this_shift,
        "shiftStarted":   shift_started_str,
        "throughputRate": throughput_rate,
    }


# ── QC pass rate helper ─────────────────────────────────────────────────────

def _get_qc_pass_rate():
    """
    Returns QC pass rate from Cut Inspection Results over the last 7 days.
    Falls back to 0.97 (our historical baseline) if no data.
    """
    try:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        total = frappe.db.count(
            "Cut Inspection Result",
            filters={"creation": [">=", week_ago]},
        )

        if not total:
            return 0.97  # baseline

        passed = frappe.db.count(
            "Cut Inspection Result",
            filters={
                "creation":        [">=", week_ago],
                "inspection_status": "Pass",
            },
        )

        return round(passed / total, 4)

    except Exception:
        return 0.97  # always return something usable


# ── Utility: clear TeeRiot capacity cache on Work Order save ───────────────

# ---------------------------------------------------------------------------
# TeeRiot → ALICE: Receive a paid order from TeeRiot after Square payment
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def receive_teeriot_order(
    session_id: str,
    week_key: str,
    quantity,
    product_type: str = "jersey",
    garment_type: str = "jersey",
    design_url: str = "",
    square_payment_id: str = "",
    customer_name: str = "",
    customer_email: str = "",
    design_placement: str = "Full Front",
    notes: str = "",
) -> dict:
    """
    Receives a confirmed, paid TeeRiot order.

    Called by teeriot_alice_handoff.js on Replit after Square payment succeeds.
    Creates a TeeRiot Order document in ERPNext so the shop floor has:
      - The Cloudinary artwork URL (design_url)
      - The production week (week_key)
      - The quantity and product type
      - The Square payment reference

    Auth: Frappe token — teeriot-integration@athlettia.com must have
          Manufacturing User + Sales User roles.

    Returns:
        {
            "ok": True,
            "order_id": "TRO-0001",
            "message": "Order received and queued for production week 2026-W32"
        }
    """
    try:
        qty = int(quantity)
    except (ValueError, TypeError):
        frappe.throw(frappe._("quantity must be an integer"), frappe.ValidationError)

    if not session_id:
        frappe.throw(frappe._("session_id is required"), frappe.ValidationError)
    if not week_key:
        frappe.throw(frappe._("week_key is required"), frappe.ValidationError)

    # Derive planned production start date from ISO week key (e.g. "2026-W32")
    try:
        planned_date = _week_key_to_date(week_key)
    except Exception:
        planned_date = frappe.utils.today()

    # Check for duplicate — idempotent on (session_id, square_payment_id)
    existing = None
    if square_payment_id:
        existing = frappe.db.get_value(
            "TeeRiot Order",
            {"square_payment_id": square_payment_id},
            "name",
        )
    if not existing and session_id:
        existing = frappe.db.get_value(
            "TeeRiot Order",
            {"session_id": session_id, "status": ["!=", "Cancelled"]},
            "name",
        )

    if existing:
        frappe.logger().info(
            f"[TeeRiotAPI] Duplicate order ignored — already exists: {existing}"
        )
        return {
            "ok":       True,
            "order_id": existing,
            "message":  f"Order already recorded: {existing}",
            "duplicate": True,
        }

    # Create the TeeRiot Order document
    order_doc = frappe.get_doc({
        "doctype":          "TeeRiot Order",
        "session_id":       session_id,
        "week_key":         week_key,
        "quantity":         qty,
        "product_type":     product_type,
        "garment_type":     garment_type,
        "design_url":       design_url,
        "design_placement": design_placement or "Full Front",
        "customer_name":    customer_name or "",
        "customer_email":   customer_email or "",
        "square_payment_id": square_payment_id or "",
        "status":           "Paid – Queued",
        "planned_start_date": planned_date,
        "notes":            notes or "",
    })
    order_doc.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.logger().info(
        f"[TeeRiotAPI] Order received: {order_doc.name} | "
        f"week={week_key} | qty={qty} | product={product_type} | "
        f"design={'✓' if design_url else '✗'} | payment={square_payment_id or 'N/A'}"
    )

    # Publish realtime event so the ALICE dashboard can show the new order
    frappe.publish_realtime(
        "teeriot_order_received",
        {
            "order_id":     order_doc.name,
            "week_key":     week_key,
            "quantity":     qty,
            "product_type": product_type,
            "customer":     customer_name or "Unknown",
            "has_design":   bool(design_url),
        },
        room=frappe.local.site,
    )

    return {
        "ok":       True,
        "order_id": order_doc.name,
        "message":  f"Order received and queued for production {week_key}",
    }


def _week_key_to_date(week_key: str) -> str:
    """
    Converts an ISO week key like "2026-W32" to the Monday date of that week.
    Returns today's date as fallback if parsing fails.
    """
    import datetime
    try:
        # Python's %G-W%V%u: ISO year + week + weekday (1=Monday)
        dt = datetime.datetime.strptime(f"{week_key}-1", "%G-W%V-%u")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return frappe.utils.today()


def on_work_order_update(doc, method=None):
    """
    Hook this to Work Order's on_update so TeeRiot sees changes immediately.
    Add to hooks.py:
        doc_events = {
            "Work Order": {
                "on_update": "alice_shop_floor.teeriot_api.on_work_order_update"
            }
        }
    The 5-min cache in teeriot_capacity.js will pick up changes within one cycle.
    No action needed here unless you want to push a webhook proactively.
    """
    # Future: push a webhook to Replit to bust the 5-min cache immediately
    # For now, the cache handles it.
    pass
