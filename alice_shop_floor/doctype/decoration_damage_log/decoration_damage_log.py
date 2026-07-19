# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


# Damage types that always require a replacement order
AUTO_REPLACE_TYPES = {
	"Scorched Fabric",
	"Needle Hole",
	"Total Loss",
}

# Severity → replacement decision mapping
SEVERITY_AUTO_REPLACE = {
	"Total Loss": True,
	"Major": True,
	"Minor": False,
}


class DecorationDamageLog(Document):
	"""
	Records every garment damaged during decoration (DTG, DTF, Embroidery).

	Key behaviors:
	  - Auto-determines whether a replacement blank order should be triggered
	  - Links to the replacement Purchase Order once created
	  - Feeds the decoration defect rate dashboard in ALICE OS
	  - Root cause data informs machine calibration and operator training

	Replacement order flow:
	  DecorationDamageLog created → replacement_triggered = 1
	  → decoration_utils._trigger_garment_replacement()
	  → decoration_engine.create_replacement_order() (background job)
	  → Purchase Order created, PO name written to replacement_po field
	"""

	# ------------------------------------------------------------------
	# Lifecycle hooks
	# ------------------------------------------------------------------

	def validate(self):
		self._set_decoration_method_from_job_card()
		self._auto_set_replacement_flag()
		self._auto_set_damage_detected_at()

	def after_insert(self):
		if self.replacement_triggered:
			self._enqueue_replacement_order()
		self._log_to_quality_alert()

	# ------------------------------------------------------------------
	# Private helpers
	# ------------------------------------------------------------------

	def _set_decoration_method_from_job_card(self):
		"""Pull decoration_method from the Job Card if not already set."""
		if self.job_card and not self.decoration_method:
			method = frappe.db.get_value("Job Card", self.job_card, "decoration_method")
			if method:
				self.decoration_method = method

	def _auto_set_replacement_flag(self):
		"""
		Auto-enable replacement_triggered for Total Loss, Major damage,
		or specific damage types that always need a replacement.
		"""
		if self.damage_type in AUTO_REPLACE_TYPES:
			self.replacement_triggered = 1
		elif self.damage_severity and SEVERITY_AUTO_REPLACE.get(self.damage_severity):
			self.replacement_triggered = 1
		if self.replacement_triggered and not self.replacement_status:
			self.replacement_status = "Pending"

	def _auto_set_damage_detected_at(self):
		if not self.damage_detected_at:
			self.damage_detected_at = now_datetime()

	def _enqueue_replacement_order(self):
		frappe.enqueue(
			"alice_shop_floor.alice_shop_floor.decoration_engine.create_replacement_order",
			queue="long",
			job_card_name=self.job_card,
			damage_log_name=self.name,
			is_async=True,
		)
		frappe.logger().info(
			f"[DecorationDamageLog] {self.name} — replacement order enqueued "
			f"(supplier: {self.replacement_supplier or 'auto-select'})"
		)

	def _log_to_quality_alert(self):
		"""
		Creates a Quality Alert (ERPNext Quality module) for Major/Total Loss
		so the QA manager is notified and can trigger corrective action.
		"""
		if self.damage_severity not in ("Major", "Total Loss"):
			return
		try:
			qa = frappe.get_doc({
				"doctype": "Quality Alert",
				"subject": f"Decoration Damage: {self.damage_type} [{self.damage_severity}] — {self.job_card}",
				"reference_type": "Decoration Damage Log",
				"reference_name": self.name,
			})
			qa.insert(ignore_permissions=True)
		except Exception:
			# Quality Alert module may not be installed — log and continue
			frappe.logger().warning(
				f"[DecorationDamageLog] Could not create Quality Alert for {self.name} "
				f"— Quality module may not be enabled."
			)

	# ------------------------------------------------------------------
	# Public methods
	# ------------------------------------------------------------------

	def mark_replacement_ordered(self, po_name: str, supplier: str = None) -> None:
		"""Called by decoration_engine.create_replacement_order() once PO is created."""
		self.replacement_po = po_name
		self.replacement_status = "Ordered"
		if supplier:
			self.replacement_supplier = supplier
		from frappe.utils import today
		self.replacement_ordered_on = today()
		self.save(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Whitelisted API endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist()
def log_decoration_damage(
	job_card: str,
	damage_type: str,
	damage_severity: str,
	damage_description: str = "",
	damage_photo: str = None,
	root_cause_category: str = None,
	corrective_action: str = None,
) -> dict:
	"""
	Creates a DecorationDamageLog entry from the shop floor scan workflow.
	Called when an operator flags a garment as damaged at the decoration station.
	"""
	for required, val in [("job_card", job_card), ("damage_type", damage_type), ("damage_severity", damage_severity)]:
		if not val:
			frappe.throw(_(f"{required} is required"), frappe.ValidationError)

	# Pull garment details from Job Card
	jc = frappe.get_doc("Job Card", job_card)

	doc = frappe.get_doc({
		"doctype": "Decoration Damage Log",
		"job_card": job_card,
		"work_order": jc.get("work_order"),
		"production_recipe": jc.get("production_recipe"),
		"decoration_method": jc.get("decoration_method"),
		"damage_type": damage_type,
		"damage_severity": damage_severity,
		"damage_description": damage_description,
		"damage_photo": damage_photo,
		"operator": frappe.session.user,
		"root_cause_category": root_cause_category,
		"corrective_action": corrective_action,
	})
	doc.insert(ignore_permissions=True)

	return {
		"ok": True,
		"name": doc.name,
		"replacement_triggered": bool(doc.replacement_triggered),
		"damage_severity": doc.damage_severity,
	}


@frappe.whitelist()
def get_damage_summary(from_date: str = None, decoration_method: str = None) -> dict:
	"""
	Returns damage stats for the ALICE OS dashboard.
	Optionally filtered by date range and/or decoration method.
	"""
	filters = {}
	if from_date:
		filters["creation"] = [">=", from_date]
	if decoration_method:
		filters["decoration_method"] = decoration_method

	logs = frappe.get_list(
		"Decoration Damage Log",
		filters=filters,
		fields=["damage_type", "damage_severity", "decoration_method", "replacement_triggered"],
	)

	total = len(logs)
	by_severity = {}
	by_method = {}
	replacements = 0
	for log in logs:
		by_severity[log.damage_severity] = by_severity.get(log.damage_severity, 0) + 1
		by_method[log.decoration_method or "Unknown"] = by_method.get(log.decoration_method or "Unknown", 0) + 1
		if log.replacement_triggered:
			replacements += 1

	return {
		"ok": True,
		"total": total,
		"by_severity": by_severity,
		"by_method": by_method,
		"replacement_orders_triggered": replacements,
	}
