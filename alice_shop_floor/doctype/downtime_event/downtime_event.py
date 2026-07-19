"""
Downtime Event - Module 7: Downtime Root-Cause AI

Auto-calculates duration on save. If cause_category is not set but
reported_cause text is present, the engine can classify it.
"""

import frappe
from frappe.utils import now_datetime, time_diff_in_seconds


class DowntimeEvent(frappe.model.document.Document):

    def before_save(self):
        self._calculate_duration()
        if not self.root_cause_group and self.cause_category:
            cat = frappe.db.get_value(
                "Downtime Cause Category", self.cause_category, "root_cause_group"
            )
            if cat:
                self.root_cause_group = cat

    def _calculate_duration(self):
        if self.started_at and self.ended_at:
            secs = time_diff_in_seconds(self.ended_at, self.started_at)
            self.duration_minutes = round(max(secs, 0) / 60, 2)
