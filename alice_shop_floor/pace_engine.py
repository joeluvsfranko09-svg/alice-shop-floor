"""
ALICE Pace Engine
=================
Tracks whether sewers are on-pace to hit their shift WO targets.

Core concepts
─────────────
• Shift window  — configurable per-shift time window (default: Morning 06:00–14:00,
                  Afternoon 14:00–22:00, Night 22:00–06:00)
• Pace status   — On Track | Behind | Critical | Ahead | No Target
• Projection    — completed_wos + (remaining_shift_minutes / avg_sew_minutes_per_wo)
• Rebalance     — if a sewer is Critical, suggest moving their next-queued bin to a
                  station whose operator is Ahead or On Track with available capacity

Public API (called by api.py)
─────────────────────────────
  get_floor_pace_summary()      → list of per-operator pace dicts
  get_operator_pace(operator)   → single operator detail
  upsert_shift_target(operator, target_wos, shift=None, shift_date=None,
                      warn_pct=80, critical_pct=60)
  get_rebalance_suggestions()   → list of {assignment, from_station, to_station, reason}
  run_pace_check()              → fires realtime pace_alert for Critical operators;
                                  called by scheduler every 5 min
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional

import frappe
from frappe.utils import now_datetime, get_datetime, today

# ── Shift window defaults ─────────────────────────────────────────────────────
# Can be overridden per-target; these are fallback values.
DEFAULT_SHIFT_WINDOWS = {
    "Morning":   (time(6,  0), time(14, 0)),
    "Afternoon": (time(14, 0), time(22, 0)),
    "Night":     (time(22, 0), time(6,  0)),   # spans midnight
}

PACE_AHEAD    = "Ahead"
PACE_ON_TRACK = "On Track"
PACE_BEHIND   = "Behind"
PACE_CRITICAL = "Critical"
PACE_NO_DATA  = "No Target"


# ── Shift helpers ─────────────────────────────────────────────────────────────

def current_shift(at: Optional[datetime] = None) -> str:
    """Return 'Morning' | 'Afternoon' | 'Night' for a given datetime."""
    at = at or now_datetime()
    t  = at.time()
    if time(6, 0) <= t < time(14, 0):
        return "Morning"
    if time(14, 0) <= t < time(22, 0):
        return "Afternoon"
    return "Night"


def shift_window(shift: str, shift_date: Optional[str] = None,
                 target_doc=None) -> tuple[datetime, datetime]:
    """
    Return (start_dt, end_dt) for a given shift on shift_date.
    Respects overrides on the target doc (shift_start / shift_end fields).
    """
    date_str  = shift_date or today()
    base_date = get_datetime(date_str).date()

    # Check for target doc overrides
    t_start = t_end = None
    if target_doc:
        if target_doc.shift_start:
            t_start = (datetime.strptime(str(target_doc.shift_start), "%H:%M:%S")
                       .replace(tzinfo=None).time())
        if target_doc.shift_end:
            t_end = (datetime.strptime(str(target_doc.shift_end), "%H:%M:%S")
                     .replace(tzinfo=None).time())

    defaults        = DEFAULT_SHIFT_WINDOWS.get(shift, (time(6, 0), time(14, 0)))
    t_start         = t_start or defaults[0]
    t_end           = t_end   or defaults[1]

    start_dt = datetime.combine(base_date, t_start)
    end_dt   = datetime.combine(base_date, t_end)

    # Night shift spans midnight
    if t_end <= t_start:
        end_dt += timedelta(days=1)

    return start_dt, end_dt


# ── Target lookup ─────────────────────────────────────────────────────────────

def _get_target(operator: str, shift: str, shift_date: str) -> Optional[object]:
    """
    Return the ShiftProductionTarget doc for this operator/shift/date, or None.
    Falls back to a global default (operator == None) if no individual target set.
    """
    name = frappe.db.get_value(
        "Shift Production Target",
        {"operator": operator, "shift": shift, "shift_date": shift_date},
        "name",
    )
    if not name:
        # Fallback: any target for today's shift without a specific operator
        # (not currently used but keeps the engine flexible)
        return None
    return frappe.get_doc("Shift Production Target", name)


# ── Core pace computation ─────────────────────────────────────────────────────

def _completed_assignments(operator: str, start_dt: datetime, end_dt: datetime) -> list:
    """Return all Complete SewingBinAssignments for operator within the shift window."""
    return frappe.get_all(
        "Sewing Bin Assignment",
        filters={
            "operator":    operator,
            "status":      "Complete",
            "completed_at": ["between", [start_dt, end_dt]],
        },
        fields=["name", "work_order", "picked_at", "completed_at"],
    )


def _in_progress_assignments(operator: str) -> list:
    return frappe.get_all(
        "Sewing Bin Assignment",
        filters={"operator": operator, "status": ["in", ["Picked", "In Progress"]]},
        fields=["name", "work_order", "picked_at", "station"],
    )


def _queued_assignments(operator: str) -> list:
    return frappe.get_all(
        "Sewing Bin Assignment",
        filters={"operator": operator, "status": "Queued"},
        fields=["name", "work_order", "station", "priority"],
        order_by="priority desc, assigned_at asc",
    )


def _avg_sew_minutes(completed: list) -> float:
    """
    Average (completed_at − picked_at) in minutes across completed assignments.
    Falls back to 30 min if no data.
    """
    if not completed:
        return 30.0
    durations = []
    for row in completed:
        if row.picked_at and row.completed_at:
            try:
                picked    = get_datetime(str(row.picked_at))
                completed = get_datetime(str(row.completed_at))
                mins      = (completed - picked).total_seconds() / 60.0
                if 1 <= mins <= 480:   # sanity filter: 1 min – 8 hours
                    durations.append(mins)
            except Exception:
                pass
    return round(sum(durations) / len(durations), 1) if durations else 30.0


def compute_operator_pace(operator: str,
                          shift: Optional[str]      = None,
                          shift_date: Optional[str] = None) -> dict:
    """
    Return a pace dict for one operator:
    {
      operator, shift, shift_date,
      target_wos, warn_pct, critical_pct,
      wos_completed, wos_in_progress, wos_queued,
      avg_sew_min, remaining_shift_min,
      projected_total, projected_pct,
      pace_status,                         # Ahead | On Track | Behind | Critical | No Target
      shift_start_str, shift_end_str,
    }
    """
    shift      = shift      or current_shift()
    shift_date = shift_date or today()
    now        = now_datetime()

    target_doc   = _get_target(operator, shift, shift_date)
    start_dt, end_dt = shift_window(shift, shift_date, target_doc)

    completed    = _completed_assignments(operator, start_dt, end_dt)
    in_progress  = _in_progress_assignments(operator)
    queued       = _queued_assignments(operator)

    wos_done     = len(completed)
    avg_sew      = _avg_sew_minutes(completed)

    # Remaining shift time (clamped to 0 if shift has ended)
    remaining_min = max(0.0, (end_dt - now).total_seconds() / 60.0)

    # Projected total = done + current WO (in-progress, partial credit 0.5) + future
    in_prog_credit = len(in_progress) * 0.5     # partial — not yet done
    projected = wos_done + in_prog_credit
    if avg_sew > 0 and remaining_min > 0:
        projected += (remaining_min / avg_sew)
    projected = round(projected, 1)

    if not target_doc:
        pace_status   = PACE_NO_DATA
        target_wos    = 0
        warn_pct      = 80.0
        critical_pct  = 60.0
        projected_pct = 0.0
    else:
        target_wos    = target_doc.target_wos or 1
        warn_pct      = float(target_doc.warn_pct     or 80)
        critical_pct  = float(target_doc.critical_pct or 60)
        projected_pct = round((projected / target_wos) * 100, 1) if target_wos else 0.0

        if projected_pct >= 100:
            pace_status = PACE_AHEAD
        elif projected_pct >= warn_pct:
            pace_status = PACE_ON_TRACK
        elif projected_pct >= critical_pct:
            pace_status = PACE_BEHIND
        else:
            pace_status = PACE_CRITICAL

    return {
        "operator":          operator,
        "shift":             shift,
        "shift_date":        shift_date,
        "target_wos":        target_wos,
        "warn_pct":          warn_pct,
        "critical_pct":      critical_pct,
        "wos_completed":     wos_done,
        "wos_in_progress":   len(in_progress),
        "wos_queued":        len(queued),
        "avg_sew_min":       avg_sew,
        "remaining_shift_min": round(remaining_min, 0),
        "projected_total":   projected,
        "projected_pct":     projected_pct,
        "pace_status":       pace_status,
        "shift_start_str":   start_dt.strftime("%H:%M"),
        "shift_end_str":     end_dt.strftime("%H:%M"),
    }


# ── Floor summary ─────────────────────────────────────────────────────────────

def get_floor_pace_summary() -> list:
    """
    Pace data for all operators with an active bin assignment today.
    Returns list of pace dicts, sorted Critical → Behind → On Track → Ahead → No Target.
    """
    SORT_KEY = {PACE_CRITICAL: 0, PACE_BEHIND: 1,
                PACE_ON_TRACK: 2, PACE_AHEAD: 3, PACE_NO_DATA: 4}

    # Operators with any active or today-completed assignment
    active_ops = frappe.db.sql("""
        SELECT DISTINCT operator
        FROM `tabSewing Bin Assignment`
        WHERE operator IS NOT NULL AND operator != ''
          AND (
            status IN ('Queued','Picked','In Progress')
            OR (status = 'Complete' AND DATE(completed_at) = CURDATE())
          )
    """, as_dict=False)

    operators = [row[0] for row in active_ops]
    if not operators:
        return []

    shift      = current_shift()
    shift_date = today()

    results = [compute_operator_pace(op, shift, shift_date) for op in operators]
    results.sort(key=lambda r: (SORT_KEY.get(r["pace_status"], 5), r["operator"]))
    return results


# ── Operator detail ───────────────────────────────────────────────────────────

def get_operator_pace(operator: str) -> dict:
    """Compute pace for a single operator (current shift)."""
    return compute_operator_pace(operator)


# ── Target upsert ─────────────────────────────────────────────────────────────

def upsert_shift_target(operator: str, target_wos: int,
                        shift: Optional[str]      = None,
                        shift_date: Optional[str] = None,
                        warn_pct: float           = 80.0,
                        critical_pct: float       = 60.0) -> str:
    """Create or update a ShiftProductionTarget. Returns the doc name."""
    shift      = shift      or current_shift()
    shift_date = shift_date or today()

    existing = frappe.db.get_value(
        "Shift Production Target",
        {"operator": operator, "shift": shift, "shift_date": shift_date},
        "name",
    )
    if existing:
        doc = frappe.get_doc("Shift Production Target", existing)
        doc.target_wos    = target_wos
        doc.warn_pct      = warn_pct
        doc.critical_pct  = critical_pct
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.get_doc({
        "doctype":      "Shift Production Target",
        "operator":     operator,
        "shift":        shift,
        "shift_date":   shift_date,
        "target_wos":   target_wos,
        "warn_pct":     warn_pct,
        "critical_pct": critical_pct,
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


# ── Rebalance suggestions ─────────────────────────────────────────────────────

def get_rebalance_suggestions() -> list:
    """
    For each Critical sewer with queued bins, check if a Behind/On-Track/Ahead
    sewer at a compatible station is available to absorb one of their upcoming WOs.

    Returns list of:
    {
      work_order, assignment, from_operator, from_station,
      to_operator, to_station, reason
    }
    """
    from alice_shop_floor.alice_shop_floor.pick_to_bin_utils import PickToBinEngine

    pace_summary = get_floor_pace_summary()
    critical_ops = {r["operator"] for r in pace_summary if r["pace_status"] == PACE_CRITICAL}
    healthy_ops  = {r["operator"] for r in pace_summary
                    if r["pace_status"] in (PACE_ON_TRACK, PACE_AHEAD)
                    and r["wos_queued"] == 0}     # healthy AND free in queue

    if not critical_ops or not healthy_ops:
        return []

    suggestions = []
    engine = PickToBinEngine()

    for op in critical_ops:
        queued = _queued_assignments(op)
        if not queued:
            continue
        # Take the next-up queued assignment for this sewer
        asgn = queued[0]
        required_machine = engine._get_required_machine(asgn["work_order"])

        # Find healthy operators with a compatible free station
        for h_op in healthy_ops:
            h_station = frappe.db.get_value(
                "Sewing Station",
                {"default_operator": h_op, "is_active": 1},
                "name",
            )
            if not h_station:
                continue
            h_machine = frappe.db.get_value("Sewing Station", h_station, "machine_type")
            if required_machine and h_machine != required_machine:
                continue

            # Check this healthy station has no active assignment
            busy = frappe.db.exists(
                "Sewing Bin Assignment",
                {"station": h_station, "status": ["in", ["Queued","Picked","In Progress"]]}
            )
            if busy:
                continue

            # Get station codes for display
            from_station_code = frappe.db.get_value(
                "Sewing Station", asgn.get("station"), "station_code"
            ) if asgn.get("station") else ""
            to_station_code   = frappe.db.get_value(
                "Sewing Station", h_station, "station_code") or h_station

            suggestions.append({
                "work_order":   asgn["work_order"],
                "assignment":   asgn["name"],
                "from_operator": op,
                "from_station":  from_station_code or asgn.get("station", ""),
                "to_operator":   h_op,
                "to_station":    to_station_code,
                "reason": (f"{op} is Critical paced — {h_op} is free and "
                           f"machine-compatible"),
            })
            break   # one suggestion per critical sewer is enough

    return suggestions


# ── Scheduler task ────────────────────────────────────────────────────────────

def run_pace_check() -> None:
    """
    Called every 5 minutes by the scheduler.
    Fires realtime 'pace_alert' for Critical operators so the floor view
    can show an alert badge without waiting for a manual refresh.
    """
    try:
        summary = get_floor_pace_summary()
        critical = [r for r in summary if r["pace_status"] == PACE_CRITICAL]
        if critical:
            frappe.publish_realtime(
                "pace_alert",
                {
                    "critical_count": len(critical),
                    "operators": [
                        {"operator": r["operator"],
                         "wos_completed": r["wos_completed"],
                         "target_wos":    r["target_wos"],
                         "projected_pct": r["projected_pct"]}
                        for r in critical
                    ],
                },
            )
    except Exception as exc:
        frappe.log_error(str(exc), "Pace Check Error")
