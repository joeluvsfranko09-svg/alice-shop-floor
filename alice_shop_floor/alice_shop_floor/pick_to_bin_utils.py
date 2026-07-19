"""
pick_to_bin_utils.py — Module 11: Pick-to-Bin Engine for Sewers
================================================================
Assigns Work Order bundles to sewing stations by matching TWO constraints:

  CONSTRAINT 1 — Machine match (BOM flow)
  ─────────────────────────────────────────
  Each Work Order has a BOM. The BOM has Operations. Each operation specifies
  a Workstation (machine type) — e.g. "Lockstitch", "Overlock 5-thread",
  "Flatlock", "Bartack", "Feed-off-the-arm".
  A station can only receive a WO if its machine_type (Workstation) matches
  the workstation required by the WO's sewing BOM operation.

  CONSTRAINT 2 — Operator skill (AI scoring)
  ─────────────────────────────────────────────
  Among all machine-compatible free stations, prefer the station whose
  default operator has the highest skill_score at the Sewing stage
  (from Operator Skill Profile, Module 6).

Assignment logic (per WO):
  1. Get required workstation from BOM Operations (sewing step)
  2. Filter free stations to machine-compatible ones
  3. Score compatible stations by operator skill_score DESC
  4. Assign to top scorer; tie-break = station_code alphabetical
  5. If NO compatible station is free → WO stays in queue (logged)

Priority ordering (Rush > High > Normal) and age (oldest bundle first
within the same priority tier) determine which WOs are processed first.

Auto-assignment runs every 30 minutes via scheduler.
Supervisors can also trigger manually or override via the API.
"""

import frappe
from frappe.utils import now_datetime, time_diff_in_seconds

PRIORITY_RANK = {"Rush": 0, "High": 1, "Normal": 2}
SEWING_OPERATION_KEYWORDS = [
    "sew", "stitch", "overlock", "lockstitch", "flatlock",
    "bartack", "feed-off", "coverstitch", "hem", "seam",
    "assembly", "join", "attach",
]


# ── Public entry points ───────────────────────────────────────────────────────

def auto_assign_bins() -> dict:
    """Scheduler entry point — sweep and assign all ready bundles."""
    return PickToBinEngine().run_auto_assign()


def get_floor_view(station=None, operator=None) -> list:
    """Active assignments for the sewing floor view page."""
    return PickToBinEngine().get_floor_view(station=station, operator=operator)


def get_queue(limit=50) -> list:
    """Bundles waiting for a compatible free station."""
    return PickToBinEngine().get_unassigned_queue(limit=int(limit))


def manually_assign(work_order, station, operator=None, priority="Normal") -> dict:
    """Supervisor override — assign a specific WO to a specific station."""
    return PickToBinEngine().manual_assign(
        work_order=work_order,
        station=station,
        operator=operator or "",
        priority=priority,
    )


def get_station_summary() -> list:
    """Per-station load summary for the supervisor dashboard."""
    return PickToBinEngine().get_station_summary()


# ── Engine ────────────────────────────────────────────────────────────────────

class PickToBinEngine:

    # ── Main assign sweep ─────────────────────────────────────────────────────

    def run_auto_assign(self) -> dict:
        ready_wos     = self._get_ready_work_orders()
        free_stations = self._get_free_stations()

        # Pre-build station → machine_type map to avoid repeated DB hits
        station_machine_map = {
            s["name"]: s["machine_type"] for s in free_stations
        }

        assigned, skipped = [], []

        for wo in ready_wos:
            req_machine = wo.get("required_machine_type") or ""

            # Filter to compatible stations
            if req_machine:
                compatible = [
                    s for s in free_stations
                    if s["machine_type"] == req_machine
                ]
            else:
                # No machine requirement in BOM → any free station is eligible
                compatible = list(free_stations)

            if not compatible:
                reason = (
                    f"No free station with machine '{req_machine}'"
                    if req_machine else "No free stations"
                )
                skipped.append({"work_order": wo["work_order"], "reason": reason})
                continue

            station = self._pick_best_station(compatible)
            if not station:
                skipped.append({"work_order": wo["work_order"], "reason": "Scoring failed"})
                continue

            doc = self._create_assignment(
                work_order=wo["work_order"],
                station=station["name"],
                operator=station.get("default_operator") or "",
                priority=wo.get("priority", "Normal"),
                method="Auto (AI)",
                required_machine_type=req_machine,
                station_machine_type=station.get("machine_type") or "",
                machine_match=(req_machine == "" or req_machine == station.get("machine_type")),
            )
            assigned.append({
                "work_order":           wo["work_order"],
                "station":              station["name"],
                "operator":             station.get("default_operator") or "",
                "machine_type":         station.get("machine_type") or "",
                "required_machine":     req_machine,
                "machine_match":        doc.machine_match,
                "operator_skill_score": station.get("skill_score", 0),
                "assignment":           doc.name,
            })
            # Remove station from pool so it isn't double-booked this sweep
            free_stations = [s for s in free_stations if s["name"] != station["name"]]

            frappe.publish_realtime("bin_assigned", {
                "work_order": wo["work_order"],
                "station":    station["name"],
                "operator":   station.get("default_operator") or "",
                "machine":    station.get("machine_type") or "",
                "assignment": doc.name,
            })

        return {
            "assigned":       assigned,
            "skipped":        skipped,
            "total_ready":    len(ready_wos),
            "assigned_count": len(assigned),
            "skipped_count":  len(skipped),
            "run_at":         str(now_datetime()),
        }

    # ── Manual supervisor override ────────────────────────────────────────────

    def manual_assign(self, work_order, station, operator="", priority="Normal") -> dict:
        # Warn if machine types don't match (but don't block — supervisor override)
        req_machine = self._get_required_machine(work_order)
        station_doc = frappe.get_doc("Sewing Station", station)
        station_machine = station_doc.machine_type or ""
        machine_match   = (req_machine == "" or req_machine == station_machine)

        if not machine_match:
            frappe.log_error(
                f"Manual bin assignment: WO {work_order} requires machine "
                f"'{req_machine}' but station {station} has '{station_machine}'. "
                f"Assigned anyway (supervisor override).",
                "Pick-to-Bin Machine Mismatch"
            )

        existing = frappe.db.exists(
            "Sewing Bin Assignment",
            {"work_order": work_order, "status": ["in", ["Queued", "Picked", "In Progress"]]},
        )
        if existing:
            doc = frappe.get_doc("Sewing Bin Assignment", existing)
            doc.station               = station
            doc.operator              = operator or doc.operator
            doc.priority              = priority
            doc.assignment_method     = "Manual"
            doc.required_machine_type = req_machine
            doc.station_machine_type  = station_machine
            doc.machine_match         = machine_match
            doc.save(ignore_permissions=True)
        else:
            doc = self._create_assignment(
                work_order=work_order,
                station=station,
                operator=operator,
                priority=priority,
                method="Manual",
                required_machine_type=req_machine,
                station_machine_type=station_machine,
                machine_match=machine_match,
            )

        frappe.publish_realtime("bin_assigned", {
            "work_order":    work_order,
            "station":       station,
            "operator":      operator,
            "machine":       station_machine,
            "machine_match": machine_match,
            "assignment":    doc.name,
        })

        return {
            "assignment":           doc.name,
            "work_order":           work_order,
            "station":              station,
            "operator":             operator,
            "priority":             priority,
            "method":               "Manual",
            "required_machine":     req_machine,
            "station_machine":      station_machine,
            "machine_match":        machine_match,
        }

    # ── Floor view ────────────────────────────────────────────────────────────

    def get_floor_view(self, station=None, operator=None) -> list:
        filters = {"status": ["in", ["Queued", "Picked", "In Progress"]]}
        if station:
            filters["station"] = station
        if operator:
            filters["operator"] = operator

        rows = frappe.get_all(
            "Sewing Bin Assignment",
            filters=filters,
            fields=[
                "name", "work_order", "production_item",
                "station", "operator", "status", "priority",
                "fabric_lot", "cut_inspection_status",
                "required_machine_type", "station_machine_type",
                "machine_match", "operator_skill_score",
                "assignment_method", "assigned_at", "picked_at",
            ],
            order_by="assigned_at asc",
        )
        now = now_datetime()
        for r in rows:
            r["elapsed_minutes"] = self._elapsed_min(r.get("assigned_at"), now)
            r["priority_rank"]   = PRIORITY_RANK.get(r.get("priority", "Normal"), 2)
            # Enrich with bundle shade status so the floor card can warn the sewer
            r["bundle_shade_status"] = self._get_bundle_shade_status(r["work_order"])

        rows.sort(key=lambda x: (x["priority_rank"], -x["elapsed_minutes"]))
        return rows

    def get_unassigned_queue(self, limit=50) -> list:
        ready = self._get_ready_work_orders(limit=limit)
        assigned_wos = set(
            r["work_order"] for r in frappe.get_all(
                "Sewing Bin Assignment",
                filters={"status": ["in", ["Queued", "Picked", "In Progress"]]},
                fields=["work_order"],
            )
        )
        return [wo for wo in ready if wo["work_order"] not in assigned_wos]

    def get_station_summary(self) -> list:
        stations = frappe.get_all(
            "Sewing Station",
            filters={"is_active": 1},
            fields=["name", "station_code", "station_name",
                    "machine_type", "machine_id", "default_operator"],
            order_by="station_code asc",
        )
        active = {
            r["station"]: r for r in frappe.get_all(
                "Sewing Bin Assignment",
                filters={"status": ["in", ["Queued", "Picked", "In Progress"]]},
                fields=["station", "work_order", "operator", "status",
                        "priority", "assigned_at", "production_item",
                        "required_machine_type", "machine_match"],
            )
        }
        now = now_datetime()
        result = []
        for st in stations:
            asgn = active.get(st["name"])
            result.append({
                "station":          st["name"],
                "station_code":     st["station_code"],
                "station_name":     st.get("station_name") or st["station_code"],
                "machine_type":     st.get("machine_type") or "",
                "machine_id":       st.get("machine_id") or "",
                "default_operator": st.get("default_operator") or "",
                "is_free":          asgn is None,
                "work_order":       asgn["work_order"]            if asgn else None,
                "production_item":  asgn["production_item"]       if asgn else None,
                "operator":         asgn["operator"]              if asgn else None,
                "assignment_status": asgn["status"]               if asgn else None,
                "priority":         asgn["priority"]              if asgn else None,
                "required_machine": asgn["required_machine_type"] if asgn else None,
                "machine_match":    asgn["machine_match"]         if asgn else None,
                "elapsed_minutes":  self._elapsed_min(
                    asgn.get("assigned_at"), now) if asgn else 0,
                # Bundle shade status — tells the sewer if nesting produced
                # multi-zone cuts and whether a supervisor has cleared any mismatch
                "bundle_shade_status": self._get_bundle_shade_status(
                    asgn["work_order"]) if asgn else None,
            })
        return result

    # ── BOM machine resolution ────────────────────────────────────────────────

    def _get_required_machine(self, work_order: str) -> str:
        """
        Read the ERPNext BOM Operations for this Work Order's BOM and
        find the sewing operation's workstation.

        ERPNext BOM Operations table: `tabBOM Operation`
          fields: parent (BOM name), operation, workstation, time_in_mins
        Work Order links to a BOM via the `bom_no` field.

        We look for the operation whose name contains a sewing keyword
        (e.g. "Sewing", "Stitch", "Overlock") and return its workstation.
        If multiple sewing operations exist, use the first one.
        If none found, return "" (any station is eligible).
        """
        try:
            bom_no = frappe.db.get_value("Work Order", work_order, "bom_no")
            if not bom_no:
                return ""

            ops = frappe.get_all(
                "BOM Operation",
                filters={"parent": bom_no},
                fields=["operation", "workstation"],
                order_by="idx asc",
            )

            for op in ops:
                op_name = (op.get("operation") or "").lower()
                if any(kw in op_name for kw in SEWING_OPERATION_KEYWORDS):
                    return op.get("workstation") or ""

            # Fallback: if no keyword match, take the last operation
            # (sewing is typically the last before finishing)
            if ops:
                return ops[-1].get("workstation") or ""

        except Exception as exc:
            frappe.log_error(
                f"pick_to_bin: could not resolve machine for WO {work_order}: {exc}",
                "Pick-to-Bin BOM Lookup"
            )
        return ""

    # ── Free station discovery ────────────────────────────────────────────────

    def _get_free_stations(self) -> list:
        """
        Active stations with no current active assignment.
        Enriched with operator skill score for scoring.
        """
        all_stations = frappe.get_all(
            "Sewing Station",
            filters={"is_active": 1},
            fields=["name", "station_code", "machine_type",
                    "machine_id", "default_operator"],
            order_by="station_code asc",
        )
        busy = set(
            r["station"] for r in frappe.get_all(
                "Sewing Bin Assignment",
                filters={"status": ["in", ["Queued", "Picked", "In Progress"]]},
                fields=["station"],
            )
        )
        free = [s for s in all_stations if s["name"] not in busy]

        # Attach skill scores
        for s in free:
            op = s.get("default_operator") or ""
            s["skill_score"] = float(
                frappe.db.get_value(
                    "Operator Skill Profile",
                    {"operator": op, "stage": "Sewing"},
                    "skill_score",
                ) or 0
            ) if op else 0.0

        return free

    def _pick_best_station(self, compatible_stations: list) -> dict | None:
        """
        From a pre-filtered compatible list, return the station whose operator
        has the highest Sewing skill score. Tie-break: station_code.
        """
        if not compatible_stations:
            return None
        return sorted(
            compatible_stations,
            key=lambda s: (-s.get("skill_score", 0), s.get("station_code", ""))
        )[0]

    # ── Ready WO discovery ────────────────────────────────────────────────────

    def _get_ready_work_orders(self, limit=100) -> list:
        """
        WOs at Bundling stage, V3 gate passed, no active bin assignment.
        Sorted Rush→High→Normal, then oldest-first within tier.
        """
        trackers = frappe.get_all(
            "Production Stage Tracker",
            filters={"current_stage": "Bundling", "is_complete": 0},
            fields=["work_order", "stage_entered_at", "fabric_lot"],
            order_by="stage_entered_at asc",
            limit=limit,
        )

        assigned_wos = set(
            r["work_order"] for r in frappe.get_all(
                "Sewing Bin Assignment",
                filters={"status": ["in", ["Queued", "Picked", "In Progress"]]},
                fields=["work_order"],
            )
        )

        ready = []
        for t in trackers:
            wo = t["work_order"]
            if wo in assigned_wos:
                continue

            # V3 gate must be clear
            try:
                from alice_shop_floor.alice_shop_floor.cut_inspector_utils import (
                    check_cut_pass_gate,
                )
                if check_cut_pass_gate(wo).get("gate") != "open":
                    continue
            except Exception:
                continue

            # Cut bundle must be complete (all nesting pieces recorded + shade OK)
            bundle_ok, bundle_reason = _bundle_ready_for_sewing(wo)
            if not bundle_ok:
                # Log at debug level — not an error, just waiting
                frappe.logger("pick_to_bin").debug(
                    "WO %s skipped: %s", wo, bundle_reason
                )
                continue

            required_machine = self._get_required_machine(wo)
            priority         = self._infer_priority(wo)

            ready.append({
                "work_order":           wo,
                "stage_entered_at":     t["stage_entered_at"],
                "fabric_lot":           t.get("fabric_lot") or "",
                "required_machine_type": required_machine,
                "priority":             priority,
            })

        ready.sort(key=lambda x: (
            PRIORITY_RANK.get(x["priority"], 2),
            x["stage_entered_at"] or "",
        ))
        return ready

    # ── Assignment creation ───────────────────────────────────────────────────

    def _create_assignment(self, work_order, station, operator, priority,
                            method, required_machine_type="",
                            station_machine_type="", machine_match=True):
        doc = frappe.get_doc({
            "doctype":                "Sewing Bin Assignment",
            "work_order":             work_order,
            "station":                station,
            "operator":               operator,
            "priority":               priority,
            "status":                 "Queued",
            "assignment_method":      method,
            "required_machine_type":  required_machine_type,
            "station_machine_type":   station_machine_type,
            "machine_match":          1 if machine_match else 0,
        })
        doc.insert(ignore_permissions=True)
        return doc

    # ── Helpers ───────────────────────────────────────────────────────────────


    def _get_bundle_shade_status(self, work_order: str) -> dict:
        """Return shade summary for the floor card display."""
        try:
            bundle_name = frappe.db.exists("Cut Bundle", {"work_order": work_order})
            if not bundle_name:
                return {"status": "No Bundle", "zones": 0, "cleared": False}
            b = frappe.db.get_value(
                "Cut Bundle", bundle_name,
                ["bundle_status", "shade_zones_count", "supervisor_cleared",
                 "total_pieces_cut", "total_pieces_expected"],
                as_dict=True,
            )
            return {
                "status":   b.bundle_status,
                "zones":    int(b.shade_zones_count or 0),
                "cleared":  bool(b.supervisor_cleared),
                "pieces_cut": int(b.total_pieces_cut or 0),
                "pieces_expected": int(b.total_pieces_expected or 0),
            }
        except Exception:
            return {"status": "Unknown", "zones": 0, "cleared": False}
    @staticmethod
    def _infer_priority(work_order: str) -> str:
        try:
            p = frappe.db.get_value("Work Order", work_order, "priority") or "Normal"
            return p if p in PRIORITY_RANK else "Normal"
        except Exception:
            return "Normal"

    @staticmethod
    def _elapsed_min(dt, now) -> float:
        if not dt:
            return 0.0
        try:
            return round(float(time_diff_in_seconds(now, dt)) / 60, 1)
        except Exception:
            return 0.0


# ── Cut bundle completeness helper ────────────────────────────────────────────

def _bundle_ready_for_sewing(work_order: str) -> tuple[bool, str]:
    """
    Returns (is_ready, reason_string).

    Ready conditions:
      • CutBundle exists AND bundle_status = 'Complete'
      • OR bundle_status in ('Shade Warning', 'Shade Mismatch') AND supervisor_cleared = 1
      • If NO CutBundle exists yet → not ready (cutter hasn't recorded pieces)

    This gates the pick-to-bin assignment: a bundle with pieces from different
    fabric zones must be explicitly cleared by a supervisor before sewers receive it.
    """
    bundle_name = frappe.db.exists("Cut Bundle", {"work_order": work_order})
    if not bundle_name:
        return False, "No Cut Bundle recorded — cutter must log pieces first"

    bundle = frappe.get_doc("Cut Bundle", bundle_name)

    expected = int(bundle.total_pieces_expected or 0)
    cut      = int(bundle.total_pieces_cut or 0)

    if bundle.bundle_status == "Incomplete":
        return False, f"Bundle incomplete — {cut}/{expected} pieces cut"

    if bundle.bundle_status == "Rejected":
        return False, "Bundle has been rejected"

    if bundle.bundle_status in ("Shade Mismatch", "Shade Warning") \
            and not bundle.supervisor_cleared:
        detail = bundle.shade_mismatch_detail or "Shade variation detected"
        return False, f"Shade issue must be cleared by supervisor: {detail}"

    return True, "ok"
