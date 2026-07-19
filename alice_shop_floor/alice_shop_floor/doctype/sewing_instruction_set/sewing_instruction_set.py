"""
Sewing Instruction Set
======================
Stores step-by-step sewing instructions for a garment item (template)
or a specific Work Order (override).

On save: if auto-translate is enabled in ALICE Settings, triggers
background translation of all steps into configured languages.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SewingInstructionSet(Document):

    def validate(self):
        if not self.item and not self.work_order:
            frappe.throw("Either Item or Work Order must be set.")
        # Re-number steps in sequence order
        for i, step in enumerate(self.steps or [], start=1):
            step.sequence = i

    def after_save(self):
        """Trigger async translation after save."""
        try:
            from alice_shop_floor.alice_shop_floor.doctype.alice_settings.alice_settings import (
                get_settings,
            )
            if get_settings().translation_auto_on_save:
                frappe.enqueue(
                    "alice_shop_floor.alice_shop_floor.translator.translate_instruction_set_all",
                    queue="short",
                    timeout=120,
                    set_name=self.name,
                )
        except Exception as exc:
            frappe.log_error(str(exc), "SewingInstructionSet.after_save")

    def mark_translated(self, languages: list[str]) -> None:
        self.translated_languages = ", ".join(sorted(set(languages)))
        self.last_translated      = now_datetime()
        self.save(ignore_permissions=True)
