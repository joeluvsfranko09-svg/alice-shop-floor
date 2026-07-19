"""
Rebalancing Recommendation
ALICE line balancing suggestion: move an operator from a fast stage to a bottleneck.
Supervisor accepts or rejects via the shop floor dashboard.
Expires automatically on the next snapshot cycle if not acted on.
"""
import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class RebalancingRecommendation(Document):

    def respond(self, status, notes=None):
        """Accept or reject this recommendation. Called from supervisor dashboard."""
        if self.status != "Pending":
            frappe.throw(
                frappe._("Recommendation is already {} — cannot respond again.".format(
                    self.status
                ))
            )
        if status not in ("Accepted", "Rejected"):
            frappe.throw(frappe._("Status must be Accepted or Rejected."))

        self.status = status
        self.responded_by = frappe.session.user
        self.responded_at = now_datetime()
        self.response_notes = notes or ""
        self.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.publish_realtime(
            event="rebalancing_response",
            message={
                "recommendation": self.name,
                "bottleneck_stage": self.bottleneck_stage,
                "suggested_operator": self.suggested_operator,
                "status": status,
                "responded_by": frappe.session.user,
            },
            room="shop_floor_supervisors",
        )
