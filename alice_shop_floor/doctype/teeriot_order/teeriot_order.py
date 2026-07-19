# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TeeRiotOrder(Document):
    """
    TeeRiot Order — records a confirmed, paid order received from TeeRiot.

    Each document represents one customer order:
      - One unique garment design (Cloudinary artwork URL)
      - One production week (ISO week key)
      - One quantity
      - One Square payment reference

    Status flow:
        Paid – Queued  → In Production → QC Passed → Shipped
        Any stage can go to Cancelled if the order is voided.
    """

    def validate(self):
        if self.quantity and int(self.quantity) < 1:
            frappe.throw(frappe._("Quantity must be at least 1"), frappe.ValidationError)

    def before_insert(self):
        """Log every new TeeRiot order for the ALICE audit trail."""
        frappe.logger().info(
            f"[TeeRiotOrder] New order queued: "
            f"session={self.session_id} | week={self.week_key} | "
            f"qty={self.quantity} | product={self.product_type} | "
            f"design={'✓' if self.design_url else '✗'}"
        )
