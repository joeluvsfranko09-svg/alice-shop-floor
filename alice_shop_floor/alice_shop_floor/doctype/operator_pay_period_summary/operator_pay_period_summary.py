"""
Operator Pay Period Summary
Immutable once finalized — captures exactly what an operator earned for a pay period.
Populated by IncentivePayEngine.calculate_period() in alice_core.
Once finalized, no edits allowed (payroll is law).
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class OperatorPayPeriodSummary(Document):

    def validate(self):
        if self.is_finalized and self.has_value_changed("is_finalized") is False:
            # Already finalized — block any field edits except through finalize()
            if self._doc_before_save and self._doc_before_save.is_finalized:
                frappe.throw(
                    frappe._(
                        "Pay Period Summary for {} ({}) is finalized and cannot be edited.".format(
                            self.operator, self.period_label
                        )
                    )
                )

    def finalize(self, finalized_by=None):
        """Lock the record — called by IncentivePayEngine once the period closes."""
        if self.is_finalized:
            frappe.throw(
                frappe._("Already finalized on {}.".format(self.finalized_at))
            )
        self.is_finalized = 1
        self.finalized_at = now_datetime()
        self.finalized_by = finalized_by or frappe.session.user
        self.save(ignore_permissions=True)
        frappe.db.commit()

    def recalculate_totals(self):
        """Sum child table rows into the header pay fields."""
        base = quality = speed = penalty = pieces = qc_pass = qc_fail = 0
        for row in self.stage_earnings:
            base += row.base_pay or 0
            quality += row.quality_bonus or 0
            speed += row.speed_bonus or 0
            penalty += row.defect_penalty or 0
            pieces += row.pieces_touched or 0
            qc_pass += row.qc_pass or 0
            qc_fail += row.qc_fail or 0
            row.stage_total = (
                (row.base_pay or 0)
                + (row.quality_bonus or 0)
                + (row.speed_bonus or 0)
                - (row.defect_penalty or 0)
            )

        self.base_pay = base
        self.quality_bonus = quality
        self.speed_bonus = speed
        self.defect_penalty = penalty
        self.total_pay = base + quality + speed - penalty
        self.total_pieces = pieces
        self.total_qc_pass = qc_pass
        self.total_qc_fail = qc_fail
        total_qc = qc_pass + qc_fail
        self.quality_score_pct = round((qc_pass / total_qc) * 100, 1) if total_qc else 0.0
