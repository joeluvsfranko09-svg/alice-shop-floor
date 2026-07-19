"""
Print Job Batch
Groups multiple Work Orders into a single PrintFactory cut job.
Mixed-design nesting: each WO is a unique custom garment (POD).
PrintFactory handles the nesting geometry; we track the efficiency result.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class PrintJobBatch(Document):

    def validate(self):
        self.order_count = len(self.work_orders)
        if self.status == "Draft" and self.order_count == 0:
            frappe.throw(frappe._("Add at least one Work Order before saving a Print Job Batch."))

    def on_submit_to_printfactory(self, job_id: str):
        """Called by print_job_utils after a successful PrintFactory submission."""
        self.printfactory_job_id = job_id
        self.status = "Submitted"
        self.submitted_at = now_datetime()
        for row in self.work_orders:
            row.cut_status = "Submitted"
        self.save(ignore_permissions=True)
        frappe.db.commit()

    def on_printfactory_complete(self, efficiency_pct: float = None,
                                  fabric_length_mm: float = None):
        """Called by the webhook handler when PrintFactory reports job completion."""
        self.status = "Completed"
        self.completed_at = now_datetime()
        if efficiency_pct is not None:
            self.nesting_efficiency_pct = efficiency_pct
        if fabric_length_mm is not None:
            self.fabric_length_mm = fabric_length_mm
        for row in self.work_orders:
            row.cut_status = "Cut"
        self.save(ignore_permissions=True)
        frappe.db.commit()

        # Alert if nesting efficiency is below the configured threshold
        try:
            cfg = frappe.get_single("Print Job Config")
            warn_threshold = cfg.warn_below_efficiency_pct or 75
            if efficiency_pct and efficiency_pct < warn_threshold:
                frappe.publish_realtime(
                    event="nesting_efficiency_warning",
                    message={
                        "batch": self.name,
                        "fabric_lot": self.fabric_lot,
                        "efficiency_pct": efficiency_pct,
                        "threshold": warn_threshold,
                    },
                    room="shop_floor_supervisors",
                )
                frappe.logger().warning(
                    "ALICE PJC: Nesting efficiency {:.1f}% is below {:.1f}% threshold "
                    "for batch {} (fabric lot {}).".format(
                        efficiency_pct, warn_threshold, self.name, self.fabric_lot
                    )
                )
        except Exception:
            pass

    def on_printfactory_failed(self, error_message: str = ""):
        """Called when PrintFactory reports failure."""
        self.status = "Failed"
        self.error_message = error_message
        for row in self.work_orders:
            row.cut_status = "Failed"
        self.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.publish_realtime(
            event="print_job_failed",
            message={"batch": self.name, "fabric_lot": self.fabric_lot,
                     "error": error_message},
            room="shop_floor_supervisors",
        )
