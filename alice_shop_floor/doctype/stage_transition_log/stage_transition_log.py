"""
Stage Transition Log — Module 3: Cut-to-Pack Stage Tracker

Immutable audit log of every stage transition on a Production Stage Tracker.
Records who moved it, when, trigger source, and any supervisor override reason.

This doctype is read_only=1 and is never edited after creation.
All writes go through ProductionStageTracker._log_transition().
"""

import frappe
from frappe.model.document import Document


class StageTransitionLog(Document):

    def before_insert(self):
        """Validate log entry before writing."""
        if self.is_supervisor_override and not self.override_reason:
            frappe.throw(
                frappe._("Supervisor override entries must include an override reason.")
            )

    def after_insert(self):
        """Log to server console for observability."""
        frappe.logger().info(
            f"ALICE: Stage transition logged — "
            f"WO {self.work_order}: {self.from_stage} → {self.to_stage} "
            f"[{self.trigger_source}] by {self.transitioned_by}"
        )
