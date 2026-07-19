"""
Sewing Bin Assignment
=====================
Manages assignment of a Work Order bundle to a sewing station.
Includes picker kitting logic: tracks piece-by-piece pick progress
and gates the bin to 'Kit Ready' only when all pieces are confirmed.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SewingBinAssignment(Document):

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def validate(self):
        """Sync production_item from the linked Work Order."""
        if self.work_order and not self.production_item:
            self.production_item = frappe.db.get_value(
                "Work Order", self.work_order, "production_item"
            ) or ""

    def on_update(self):
        """After any save, re-evaluate kit completeness."""
        self._evaluate_kit_status()

    # ── Kit gate logic ───────────────────────────────────────────────────────

    def _evaluate_kit_status(self) -> None:
        """
        If the pick_list is populated:
          - all Picked → kit_status = Kit Ready, status = Kit Ready
          - any Pending  → kit_status = In Progress, status = Kitting
          - empty list   → no change
        Saves without triggering on_update again (ignore_permissions, no hooks).
        """
        rows = self.pick_list or []
        if not rows:
            return

        statuses = [r.status for r in rows]
        all_picked = all(s == "Picked" for s in statuses)
        any_picked = any(s == "Picked" for s in statuses)

        if all_picked:
            if self.kit_status != "Kit Ready":
                frappe.db.set_value(
                    "Sewing Bin Assignment", self.name,
                    {
                        "kit_status": "Kit Ready",
                        "status": "Kit Ready",
                        "kitted_at": now_datetime(),
                        "kitted_by": frappe.session.user,
                    },
                    update_modified=False,
                )
                frappe.publish_realtime(
                    "bin_kit_ready",
                    {"assignment": self.name, "work_order": self.work_order},
                    after_commit=True,
                )
        elif any_picked:
            if self.kit_status == "Not Started":
                frappe.db.set_value(
                    "Sewing Bin Assignment", self.name,
                    {"kit_status": "In Progress", "status": "Kitting"},
                    update_modified=False,
                )

    # ── Public helpers ───────────────────────────────────────────────────────

    def confirm_piece(self, idx: int, qty_picked: float, user: str) -> dict:
        """
        Mark a single pick-list row as Picked.
        idx is 1-based (matches child table idx field).
        Returns: {"kit_status": ..., "all_done": bool}
        """
        row = next((r for r in self.pick_list if r.idx == idx), None)
        if not row:
            frappe.throw(f"Pick list row idx={idx} not found in {self.name}")

        if row.status == "Picked":
            frappe.throw(f"Piece '{row.piece_type}' already picked.")

        row.status = "Picked"
        row.qty_picked = qty_picked
        row.picked_by = user
        row.picked_at = now_datetime()

        # Deduct from storage location qty
        if row.storage_location:
            try:
                loc = frappe.get_doc("Piece Storage Location", row.storage_location)
                loc.adjust_qty(-qty_picked)
            except Exception:
                pass  # non-fatal

        self.save(ignore_permissions=True)
        self.reload()

        all_done = all(r.status == "Picked" for r in self.pick_list)
        return {"kit_status": self.kit_status, "all_done": all_done}

    def mark_piece_short(self, idx: int, user: str) -> None:
        """Mark a pick-list row as Short (couldn't find pieces)."""
        row = next((r for r in self.pick_list if r.idx == idx), None)
        if not row:
            frappe.throw(f"Pick list row idx={idx} not found in {self.name}")
        row.status = "Short"
        row.picked_by = user
        row.picked_at = now_datetime()
        self.save(ignore_permissions=True)
