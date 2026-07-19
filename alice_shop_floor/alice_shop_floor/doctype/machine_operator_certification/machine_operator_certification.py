# Copyright (c) 2024, Athlettia LLC and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import today


class MachineOperatorCertification(Document):
    """
    Tracks operator certification for each decoration machine / method.

    A certification can be method-level (no machine_config → certified for all
    machines of that method) or machine-specific (machine_config set → certified
    only for that exact unit).

    Proficiency levels:
      Trainee   — may only run under supervision; station shows warning
      Certified — operates independently; no warning
      Expert    — certified + can train others + overrides safety banners silently
    """

    def validate(self):
        self._validate_employee_active()
        self._validate_expiry()

    def _validate_employee_active(self):
        status = frappe.db.get_value("Employee", self.employee, "status")
        if status and status != "Active":
            frappe.msgprint(
                f"Employee {self.employee} has status '{status}'. "
                "Certification saved but the operator will not appear in active lists.",
                indicator="orange",
                alert=True,
            )

    def _validate_expiry(self):
        if self.expires_on and self.expires_on < today():
            frappe.msgprint(
                f"Certification for {self.employee_name} expired on {self.expires_on}.",
                indicator="red",
                alert=True,
            )
            # Auto-deactivate expired certs
            self.is_active = 0
