"""
Production Stage Tracker - Module 3: Cut-to-Pack Stage Tracker

Tracks a Work Order through ZAZFIT's 6-stage production flow:
  Fabric Inspection -> Cutting -> Bundling -> Sewing -> Final QC -> Pack

ZAZFIT is pure POD - every order is a unique custom garment.

Stage gates enforced (supervisor override bypasses all):
  Fabric Inspection -> Cutting  : requires passing FabricInspectionResult  (V1)
  Cutting           -> Bundling : requires passing CutInspectionResult      (V3)
  Sewing            -> Final QC : requires passing StitchInspectionResult   (V2)
  Final QC          -> Pack     : requires passing FinalInspectionResult    (V4)

On reaching Pack: Garment Passport is auto-created and sealed.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime


STAGE_ORDER = [
    "Fabric Inspection",
    "Cutting",
    "Bundling",
    "Sewing",
    "Final QC",
    "Pack",
]


class ProductionStageTracker(frappe.model.document.Document):

    def before_insert(self):
        self.current_stage = self.current_stage or "Fabric Inspection"
        self.stage_entered_at = now_datetime()

    def validate(self):
        if self.current_stage and self.current_stage not in STAGE_ORDER:
            frappe.throw(
                _("Invalid stage '{}'. Must be one of: {}".format(
                    self.current_stage, ", ".join(STAGE_ORDER)
                ))
            )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def advance_stage(self, trigger_source="Manual", notes=None,
                      is_supervisor_override=False, override_reason=None):
        next_stage = self.get_next_stage()
        if not next_stage:
            frappe.throw(
                _("Work Order {} is already at the final stage (Pack).".format(self.work_order))
            )
        self.set_stage(
            stage=next_stage,
            trigger_source=trigger_source,
            notes=notes,
            is_supervisor_override=is_supervisor_override,
            override_reason=override_reason,
        )

    def set_stage(self, stage, trigger_source="Manual", notes=None,
                  is_supervisor_override=False, override_reason=None):
        if is_supervisor_override and not override_reason:
            frappe.throw(_("Supervisor override requires a reason to be recorded."))

        previous_stage = self.current_stage

        if not is_supervisor_override:
            expected_next = self.get_next_stage()
            if stage != expected_next:
                frappe.throw(
                    _("Stage gate violation: cannot move from '{}' to '{}'. "
                      "Next required stage is '{}'. Use supervisor override to skip.".format(
                          previous_stage, stage, expected_next
                      ))
                )

        # V1: Fabric Inspection gate -- block Cutting until fabric lot passes
        if stage == "Cutting" and not is_supervisor_override:
            self._check_fabric_inspection_gate()

        # V3: Cut Accuracy gate -- block Bundling until cut panels pass
        if stage == "Bundling" and not is_supervisor_override:
            self._check_cut_inspection_gate()

        # V2: Stitch Inspection gate -- block Final QC until sewing passes
        if stage == "Final QC" and not is_supervisor_override:
            self._check_stitch_inspection_gate()

        # V4: Final Garment Inspector gate -- block Pack until full scan passes
        if stage == "Pack" and not is_supervisor_override:
            self._check_final_inspection_gate()

        self.current_stage = stage
        self.stage_entered_at = now_datetime()

        if stage == "Pack":
            self.is_complete = 1

        self.notes = notes if notes else self.notes
        self.save(ignore_permissions=True)

        if stage == "Pack":
            self._seal_garment_passport()

        self._log_transition(
            from_stage=previous_stage,
            to_stage=stage,
            trigger_source=trigger_source,
            is_supervisor_override=is_supervisor_override,
            override_reason=override_reason,
            notes=notes,
        )

        frappe.publish_realtime(
            event="stage_advanced",
            message={
                "tracker":        self.name,
                "work_order":     self.work_order,
                "from_stage":     previous_stage,
                "to_stage":       stage,
                "trigger_source": trigger_source,
            },
            room="shop_floor",
        )

    def get_next_stage(self):
        try:
            idx = STAGE_ORDER.index(self.current_stage)
        except ValueError:
            return STAGE_ORDER[0]
        if idx >= len(STAGE_ORDER) - 1:
            return None
        return STAGE_ORDER[idx + 1]

    def get_time_in_current_stage(self):
        if not self.stage_entered_at:
            return 0
        delta = now_datetime() - self.stage_entered_at
        return round(delta.total_seconds() / 3600, 2)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_fabric_inspection_gate(self):
        """V1: block Cutting until fabric lot has a passing inspection."""
        fabric_lot = self.fabric_lot or ""
        if not fabric_lot:
            frappe.throw(
                _("Cannot advance to Cutting: no fabric lot assigned to "
                  "Work Order {}.".format(self.work_order))
            )
        from alice_shop_floor.alice_shop_floor.fabric_inspector_utils import check_fabric_pass_gate
        gate = check_fabric_pass_gate(self.work_order, fabric_lot)
        status = gate.get("gate")
        if status == "open":
            return
        if status == "pending":
            frappe.throw(
                _("Fabric inspection for lot '{}' is still in progress. "
                  "Wait for the result or use supervisor override.".format(fabric_lot))
            )
        if status == "failed":
            frappe.throw(
                _("Fabric inspection for lot '{}' FAILED: {}. "
                  "Fix the fabric or use supervisor override.".format(
                      fabric_lot, gate.get("message", "")))
            )
        frappe.throw(
            _("No fabric inspection found for lot '{}'. "
              "Trigger an inspection before advancing to Cutting.".format(fabric_lot))
        )

    def _check_cut_inspection_gate(self):
        """V3: block Bundling until Work Order has a passing cut accuracy inspection."""
        from alice_shop_floor.alice_shop_floor.cut_inspector_utils import check_cut_pass_gate
        gate = check_cut_pass_gate(self.work_order)
        status = gate.get("gate")
        if status == "open":
            return
        if status == "pending":
            frappe.throw(
                _("Cut accuracy inspection for WO '{}' is still in progress. "
                  "Wait for the result or use supervisor override.".format(self.work_order))
            )
        if status == "failed":
            frappe.throw(
                _("Cut accuracy inspection for WO '{}' FAILED: {}. "
                  "Recut panels or use supervisor override.".format(
                      self.work_order, gate.get("message", "")))
            )
        frappe.throw(
            _("No cut accuracy inspection found for WO '{}'. "
              "Trigger an inspection before advancing to Bundling.".format(self.work_order))
        )

    def _check_stitch_inspection_gate(self):
        """V2: block Final QC until Work Order has a passing stitch inspection."""
        from alice_shop_floor.alice_shop_floor.stitch_inspector_utils import check_stitch_pass_gate
        gate = check_stitch_pass_gate(self.work_order)
        status = gate.get("gate")
        if status == "open":
            return
        if status == "pending":
            frappe.throw(
                _("Stitch inspection for WO '{}' is still in progress. "
                  "Wait for the result or use supervisor override.".format(self.work_order))
            )
        if status == "failed":
            frappe.throw(
                _("Stitch inspection for WO '{}' FAILED: {}. "
                  "Repair stitching or use supervisor override.".format(
                      self.work_order, gate.get("message", "")))
            )
        frappe.throw(
            _("No stitch inspection found for WO '{}'. "
              "Trigger an inspection before advancing to Final QC.".format(self.work_order))
        )

    def _check_final_inspection_gate(self):
        """V4: block Pack until Work Order has a passing final garment inspection."""
        from alice_shop_floor.alice_shop_floor.final_inspector_utils import check_final_pass_gate
        gate = check_final_pass_gate(self.work_order)
        status = gate.get("gate")
        if status == "open":
            return
        if status == "pending":
            frappe.throw(
                _("Final garment inspection for WO '{}' is still in progress. "
                  "Wait for the result or use supervisor override.".format(self.work_order))
            )
        if status == "failed":
            frappe.throw(
                _("Final garment inspection for WO '{}' FAILED: {}. "
                  "Address defects or use supervisor override.".format(
                      self.work_order, gate.get("message", "")))
            )
        frappe.throw(
            _("No final garment inspection found for WO '{}'. "
              "Trigger an inspection before advancing to Pack.".format(self.work_order))
        )

    def _seal_garment_passport(self):
        try:
            existing = frappe.db.exists("Garment Passport", {"work_order": self.work_order})
            if existing:
                passport = frappe.get_doc("Garment Passport", existing)
            else:
                passport = frappe.get_doc({
                    "doctype":             "Garment Passport",
                    "work_order":          self.work_order,
                    "tracker":             self.name,
                    "fabric_lot":          self.fabric_lot or "",
                    "pattern_file_ref":    self.pattern_file_ref or "",
                    "printfactory_job_id": self.printfactory_job_id or "",
                })
                passport.insert(ignore_permissions=True)
            if not passport.is_sealed:
                passport.seal(sealed_by=frappe.session.user)
        except Exception as e:
            frappe.logger().error(
                "ALICE: Failed to seal Garment Passport for WO {}: {}".format(
                    self.work_order, e
                )
            )

    def _log_transition(self, from_stage, to_stage, trigger_source,
                        is_supervisor_override, override_reason, notes):
        log = frappe.get_doc({
            "doctype":                "Stage Transition Log",
            "tracker":                self.name,
            "work_order":             self.work_order,
            "from_stage":             from_stage or "-",
            "to_stage":               to_stage,
            "trigger_source":         trigger_source,
            "transitioned_by":        frappe.session.user,
            "transitioned_at":        now_datetime(),
            "is_supervisor_override": 1 if is_supervisor_override else 0,
            "override_reason":        override_reason or "",
            "notes":                  notes or "",
        })
        log.insert(ignore_permissions=True)
        frappe.db.commit()


# ------------------------------------------------------------------
# Hook: auto-create tracker on Work Order submit
# ------------------------------------------------------------------

def create_tracker_for_work_order(doc, method=None):
    existing = frappe.db.exists("Production Stage Tracker", {"work_order": doc.name})
    if existing:
        return
    tracker = frappe.get_doc({
        "doctype":       "Production Stage Tracker",
        "work_order":    doc.name,
        "current_stage": "Fabric Inspection",
    })
    tracker.insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.logger().info(
        "ALICE: Created Production Stage Tracker for Work Order {}".format(doc.name)
    )
