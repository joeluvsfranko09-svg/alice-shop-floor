"""
Solid Fabric Cut Log
====================
Records roll-to-trace data for solid (non-printed) fabrics.

On save:
 1. Recounts total_rolls_used from the rolls child table.
 2. Detects dye-lot bridge (2+ distinct dye lots across rolls).
 3. If bridge detected → sets status = 'Bridge Alert' and fires
    a realtime event so the supervisor dashboard highlights it.
 4. On Confirm → stamps production_item from Work Order and
    propagates fabric_lot to any open Sewing Bin Assignments
    for this Work Order.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import today


class SolidFabricCutLog(Document):

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def validate(self):
        self._sync_production_item()
        self._recalculate_roll_stats()

    def on_update(self):
        if self.dye_lot_bridge_detected and self.status != "Bridge Alert":
            # Auto-escalate
            frappe.db.set_value(
                "Solid Fabric Cut Log", self.name, "status", "Bridge Alert",
                update_modified=False
            )
            self._fire_bridge_alert()

    def on_submit(self):
        """When confirmed, propagate fabric_lot to open Sewing Bin Assignments."""
        self._propagate_fabric_lot_to_bins()

    # ── Core logic ───────────────────────────────────────────────────────────

    def _sync_production_item(self) -> None:
        if self.work_order and not self.production_item:
            self.production_item = frappe.db.get_value(
                "Work Order", self.work_order, "production_item"
            ) or ""

    def _recalculate_roll_stats(self) -> None:
        """Count rolls and detect dye-lot bridge."""
        rows = self.rolls or []
        self.total_rolls_used = len(rows)

        dye_lots = set(r.dye_lot for r in rows if r.dye_lot)
        bridge = len(dye_lots) > 1
        self.dye_lot_bridge_detected = 1 if bridge else 0

        if bridge and self.status == "Draft":
            self.status = "Bridge Alert"

    def _fire_bridge_alert(self) -> None:
        dye_lots = list(set(r.dye_lot for r in (self.rolls or []) if r.dye_lot))
        frappe.publish_realtime(
            "dye_lot_bridge_alert",
            {
                "log": self.name,
                "work_order": self.work_order,
                "production_item": self.production_item,
                "dye_lots": dye_lots,
                "message": (
                    f"DYE-LOT BRIDGE: Work Order {self.work_order} cut from "
                    f"{len(dye_lots)} dye lots — {', '.join(dye_lots)}"
                ),
            },
            after_commit=True,
        )

    def _propagate_fabric_lot_to_bins(self) -> None:
        """
        Stamp the primary dye lot onto all Sewing Bin Assignments for this WO.
        Takes the dye lot from the first roll (most yards used if tied).
        """
        rows = sorted(
            self.rolls or [],
            key=lambda r: -(r.yardage_used or 0)
        )
        primary_lot = rows[0].dye_lot if rows else ""
        if not primary_lot:
            return

        bins = frappe.get_all(
            "Sewing Bin Assignment",
            filters={"work_order": self.work_order, "status": ["in", ["Queued", "Kitting", "Kit Ready"]]},
            pluck="name",
        )
        for b in bins:
            frappe.db.set_value(
                "Sewing Bin Assignment", b, "fabric_lot", primary_lot,
                update_modified=False
            )

    # ── Public helpers ───────────────────────────────────────────────────────

    def confirm(self) -> None:
        """Confirm the cut log — changes status to Confirmed and propagates lot."""
        if self.status == "Bridge Alert":
            frappe.throw(
                "Cannot confirm — dye-lot bridge detected. "
                "Resolve with supervisor before confirming."
            )
        self.status = "Confirmed"
        self.save(ignore_permissions=True)
        self._propagate_fabric_lot_to_bins()

    def override_bridge_confirm(self, supervisor_notes: str) -> None:
        """Supervisor override: confirm despite bridge alert."""
        self.supervisor_notes = supervisor_notes
        self.status = "Confirmed"
        self.save(ignore_permissions=True)
        self._propagate_fabric_lot_to_bins()
