"""
Stage Throughput Target
One record per stage. Enforces uniqueness so only one target exists per stage.
"""
import frappe
from frappe.model.document import Document


class StageThroughputTarget(Document):

    def validate(self):
        duplicate = frappe.db.exists(
            "Stage Throughput Target",
            {"stage": self.stage, "name": ["!=", self.name]},
        )
        if duplicate:
            frappe.throw(
                frappe._(
                    "A throughput target for stage '{}' already exists ({}).".format(
                        self.stage, duplicate
                    )
                )
            )
