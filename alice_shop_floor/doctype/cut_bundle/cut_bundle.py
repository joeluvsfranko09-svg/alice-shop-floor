"""
Cut Bundle — nesting-aware piece tracker.

After V3 cut inspection passes, the cutter records every piece here with
its fabric zone and shade reference. The system then:

  1. Counts cut vs expected pieces       → bundle_status = Incomplete / Complete
  2. Checks shade zone consistency       → Shade Warning if >1 zone, Mismatch if
                                           shade_ref values differ across pieces
  3. Blocks bin assignment until Complete (or supervisor clears a shade warning)

Shade flag logic:
  • All pieces same shade_ref            → OK
  • Pieces span multiple zones but same  → Warning (edge pieces may drift)
    shade_ref
  • Pieces have different shade_ref vals → Mismatch (supervisor must clear)
"""

import frappe
from frappe import _
from frappe.utils import now_datetime


class CutBundle(frappe.model.document.Document):

    def before_save(self):
        self._pull_item()
        self._count_pieces()
        self._evaluate_shade()
        self._set_status()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _pull_item(self):
        if self.work_order and not self.production_item:
            self.production_item = frappe.db.get_value(
                "Work Order", self.work_order, "production_item"
            ) or ""

    def _count_pieces(self):
        self.total_pieces_cut = len([
            p for p in (self.pieces or [])
            if p.cut_status == "Cut"
        ])

    def _evaluate_shade(self):
        cut_pieces = [p for p in (self.pieces or []) if p.cut_status == "Cut"]
        if not cut_pieces:
            self.shade_zones_count   = 0
            self.shade_mismatch_detail = ""
            return

        shade_refs  = [p.shade_ref or "" for p in cut_pieces]
        zones       = [p.fabric_zone or "" for p in cut_pieces]
        unique_refs  = set(shade_refs)
        unique_zones = set(zones)

        self.shade_zones_count = len(unique_zones)

        if len(unique_refs) <= 1:
            # All same shade ref — clear any previous flags
            for p in self.pieces:
                if p.cut_status == "Cut":
                    p.shade_flag = "OK"
            self.shade_mismatch_detail = ""
        else:
            # Multiple shade refs — flag the outliers
            from collections import Counter
            most_common_ref = Counter(shade_refs).most_common(1)[0][0]
            mismatch_pieces = []
            for p in self.pieces:
                if p.cut_status != "Cut":
                    continue
                if (p.shade_ref or "") != most_common_ref:
                    p.shade_flag = "Mismatch"
                    mismatch_pieces.append(
                        f"{p.piece_name} (zone {p.fabric_zone}, shade {p.shade_ref})"
                    )
                elif len(unique_zones) > 1:
                    p.shade_flag = "Warning"
                else:
                    p.shade_flag = "OK"
            self.shade_mismatch_detail = (
                "Pieces with differing shade ref: " + "; ".join(mismatch_pieces)
                if mismatch_pieces else ""
            )

    def _set_status(self):
        expected = int(self.total_pieces_expected or 0)
        cut      = int(self.total_pieces_cut or 0)
        mismatch_count = sum(
            1 for p in (self.pieces or [])
            if p.shade_flag == "Mismatch" and p.cut_status == "Cut"
        )
        warning_count = sum(
            1 for p in (self.pieces or [])
            if p.shade_flag == "Warning" and p.cut_status == "Cut"
        )

        if expected > 0 and cut < expected:
            self.bundle_status = "Incomplete"
        elif mismatch_count > 0 and not self.supervisor_cleared:
            self.bundle_status = "Shade Mismatch"
        elif warning_count > 0 and not self.supervisor_cleared:
            self.bundle_status = "Shade Warning"
        elif cut == 0:
            self.bundle_status = "Incomplete"
        else:
            self.bundle_status = "Complete"

        if self.supervisor_cleared and not self.cleared_by:
            self.cleared_by = frappe.session.user

    # ── Public API ────────────────────────────────────────────────────────────

    @frappe.whitelist()
    def supervisor_clear_shade(self, notes=""):
        """Manufacturing Manager clears a shade warning/mismatch."""
        frappe.only_for(["Manufacturing Manager", "System Manager"])
        self.supervisor_cleared = 1
        self.cleared_by         = frappe.session.user
        if notes:
            self.notes = (self.notes or "") + f"\nShade cleared by {frappe.session.user}: {notes}"
        self.save(ignore_permissions=True)
        frappe.publish_realtime("bundle_shade_cleared", {
            "work_order":  self.work_order,
            "bundle":      self.name,
            "cleared_by":  frappe.session.user,
        })
        return {"status": self.bundle_status, "cleared_by": self.cleared_by}

    def is_ready_for_sewing(self) -> bool:
        """
        Returns True if this bundle can be assigned to a sewing bin.
        Requires: Complete OR Shade Warning with supervisor_cleared.
        """
        if self.bundle_status == "Complete":
            return True
        if self.bundle_status in ("Shade Warning", "Shade Mismatch") and self.supervisor_cleared:
            return True
        return False
