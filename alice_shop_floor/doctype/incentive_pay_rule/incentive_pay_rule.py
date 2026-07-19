"""
Incentive Pay Rule
Defines per-stage pay rates, quality bonuses, speed bonuses, and defect penalties.
One active rule per stage at a time; enforced in validate().
"""

import frappe
from frappe.model.document import Document


class IncentivePayRule(Document):

    def validate(self):
        if self.is_active:
            duplicate = frappe.db.exists(
                "Incentive Pay Rule",
                {
                    "stage": self.stage,
                    "is_active": 1,
                    "name": ["!=", self.name],
                }
            )
            if duplicate:
                frappe.throw(
                    frappe._(
                        "An active Incentive Pay Rule for stage '{}' already exists ({}). "
                        "Deactivate it before creating a new one.".format(self.stage, duplicate)
                    )
                )
