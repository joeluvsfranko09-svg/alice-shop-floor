"""
Operator Skill Profile
One record per (operator, stage) pair.
Updated weekly by OperatorSkillEngine. Read-only except training_flag and training_notes.
"""
import frappe
from frappe.model.document import Document


class OperatorSkillProfile(Document):

    def validate(self):
        duplicate = frappe.db.exists(
            "Operator Skill Profile",
            {"operator": self.operator, "stage": self.stage, "name": ["!=", self.name]},
        )
        if duplicate:
            frappe.throw(
                frappe._(
                    "A skill profile for {} at stage '{}' already exists ({}).".format(
                        self.operator, self.stage, duplicate
                    )
                )
            )
