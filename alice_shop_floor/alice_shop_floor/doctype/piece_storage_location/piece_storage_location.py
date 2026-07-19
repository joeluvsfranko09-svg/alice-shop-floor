"""
Piece Storage Location
======================
Tracks where cut fabric pieces live on the rack system.
location_label is auto-set to "{rack}-{slot}" for quick display
on the picker tablet virtual light-assist screen.
"""

import frappe
from frappe.model.document import Document


class PieceStorageLocation(Document):

    def validate(self):
        if self.rack:
            self.rack = self.rack.upper().strip()
        self.location_label = f"{self.rack}-{self.slot}" if self.rack and self.slot else ""

    def adjust_qty(self, delta: float) -> None:
        """Increment or decrement qty_available and save."""
        self.qty_available = max(0.0, (self.qty_available or 0) + delta)
        self.save(ignore_permissions=True)
