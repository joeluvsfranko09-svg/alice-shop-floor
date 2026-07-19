# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


# Valid status transitions for the digitizing workflow
VALID_TRANSITIONS = {
	"Submitted":  ["Digitizing", "Cancelled"],
	"Digitizing": ["Review", "Cancelled"],
	"Review":     ["Approved", "Rejected", "Cancelled"],
	"Approved":   ["Released", "Cancelled"],
	"Released":   [],                         # terminal — can only cancel via admin
	"Rejected":   ["Submitted"],              # allow resubmit after revision
	"Cancelled":  [],
}

# Statuses that block a Job Card from starting
BLOCKING_STATUSES = {"Submitted", "Digitizing", "Review"}


class DigitizingQueue(Document):
	"""
	Tracks the art → DST digitizing workflow for embroidery Job Cards.

	Lifecycle:
	  Submitted → Digitizing → Review → Approved → Released
	                         ↘ Rejected → Submitted (re-submit after revision)

	A Job Card linked to an Embroidery ProductionRecipe CANNOT start
	until its DigitizingQueue entry reaches 'Approved' or 'Released'.

	The `approve_digitizing` API endpoint drives the Approved→Released
	transition and stamps the DST file back to the ProductionRecipe.
	"""

	# ------------------------------------------------------------------
	# Lifecycle hooks
	# ------------------------------------------------------------------

	def validate(self):
		self._validate_recipe_is_embroidery()
		self._validate_dst_required_for_approval()

	def before_insert(self):
		self.submitted_on = now_datetime()
		self.submitted_by = frappe.session.user
		self.revision_count = self.revision_count or 0

	def before_save(self):
		self._stamp_timestamps()

	def on_update(self):
		if self.status in ("Approved", "Released"):
			self._sync_dst_to_recipe()
		if self.status in BLOCKING_STATUSES:
			self._block_job_card()
		elif self.status in ("Approved", "Released"):
			self._unblock_job_card()

	# ------------------------------------------------------------------
	# Public workflow methods
	# ------------------------------------------------------------------

	def advance_status(self, new_status: str, reason: str = "") -> None:
		"""
		Moves the entry to `new_status` if the transition is valid.
		Raises ValidationError if the transition is not allowed.
		"""
		allowed = VALID_TRANSITIONS.get(self.status, [])
		if new_status not in allowed:
			frappe.throw(
				_("Cannot transition DigitizingQueue from '{0}' to '{1}'. "
				  "Allowed transitions: {2}").format(
					self.status, new_status, ", ".join(allowed) or "none"
				),
				frappe.ValidationError,
			)
		if new_status == "Rejected" and not reason:
			frappe.throw(
				_("A rejection reason is required when rejecting a digitizing entry."),
				frappe.ValidationError,
			)
		old_status = self.status
		self.status = new_status
		if new_status == "Rejected":
			self.rejection_reason = reason
			self.revision_count = (self.revision_count or 0) + 1
		self.save(ignore_permissions=True)
		frappe.logger().info(
			f"[DigitizingQueue] {self.name}: {old_status} → {new_status}"
		)

	def is_blocking(self) -> bool:
		"""Returns True if this entry is currently blocking its Job Card."""
		return self.status in BLOCKING_STATUSES

	# ------------------------------------------------------------------
	# Private helpers
	# ------------------------------------------------------------------

	def _validate_recipe_is_embroidery(self):
		if not self.production_recipe:
			return
		method = frappe.db.get_value("Production Recipe", self.production_recipe, "decoration_method")
		if method and method != "Embroidery":
			frappe.throw(
				_("DigitizingQueue can only be linked to Embroidery recipes. "
				  "'{0}' is a {1} recipe.").format(self.production_recipe, method),
				frappe.ValidationError,
			)

	def _validate_dst_required_for_approval(self):
		if self.status in ("Approved", "Released") and not self.dst_file:
			frappe.throw(
				_("A DST file must be attached before approving a DigitizingQueue entry."),
				frappe.ValidationError,
			)

	def _stamp_timestamps(self):
		now = now_datetime()
		user = frappe.session.user
		if self.status == "Digitizing" and not self.digitizing_started_on:
			self.digitizing_started_on = now
		elif self.status == "Review" and not self.review_sent_on:
			self.review_sent_on = now
		elif self.status == "Approved" and not self.dst_approved_on:
			self.dst_approved_on = now
			self.dst_approved_by = user
		elif self.status == "Released" and not self.released_on:
			self.released_on = now

	def _sync_dst_to_recipe(self):
		"""
		Writes the approved DST file back to the linked ProductionRecipe
		so the embroidery machine has the correct file at job start.
		"""
		if not self.dst_file or not self.production_recipe:
			return
		current = frappe.db.get_value("Production Recipe", self.production_recipe, "emb_dst_file")
		if current != self.dst_file:
			frappe.db.set_value(
				"Production Recipe",
				self.production_recipe,
				"emb_dst_file",
				self.dst_file,
			)
			frappe.logger().info(
				f"[DigitizingQueue] Synced DST file to recipe {self.production_recipe}"
			)

	def _block_job_card(self):
		"""Logs that this Job Card is blocked; actual gate check is in decoration_engine.py."""
		if not self.job_card:
			return
		frappe.logger().info(
			f"[DigitizingQueue] Job Card {self.job_card} BLOCKED — "
			f"DST not ready ({self.status})"
		)

	def _unblock_job_card(self):
		if not self.job_card:
			return
		frappe.logger().info(
			f"[DigitizingQueue] Job Card {self.job_card} UNBLOCKED — "
			f"DST approved ({self.status})"
		)


# ---------------------------------------------------------------------------
# Whitelisted API methods
# ---------------------------------------------------------------------------

@frappe.whitelist()
def approve_digitizing(queue_name: str, dst_file: str = None) -> dict:
	"""
	Approves a DigitizingQueue entry and optionally attaches the DST file.
	Moves status: Review → Approved.

	Called by the decoration supervisor from the ALICE OS or Job Card form.
	"""
	if not queue_name:
		frappe.throw(_("queue_name is required"), frappe.ValidationError)
	doc = frappe.get_doc("Digitizing Queue", queue_name)
	if dst_file:
		doc.dst_file = dst_file
	doc.advance_status("Approved")
	return {
		"ok": True,
		"name": doc.name,
		"status": doc.status,
		"production_recipe": doc.production_recipe,
	}


@frappe.whitelist()
def release_digitizing(queue_name: str) -> dict:
	"""
	Releases an Approved DigitizingQueue entry to the machine queue.
	Moves status: Approved → Released.
	"""
	if not queue_name:
		frappe.throw(_("queue_name is required"), frappe.ValidationError)
	doc = frappe.get_doc("Digitizing Queue", queue_name)
	doc.advance_status("Released")
	return {
		"ok": True,
		"name": doc.name,
		"status": doc.status,
	}


@frappe.whitelist()
def reject_digitizing(queue_name: str, reason: str) -> dict:
	"""Rejects a DST submission and increments revision_count."""
	if not queue_name:
		frappe.throw(_("queue_name is required"), frappe.ValidationError)
	if not reason:
		frappe.throw(_("reason is required for rejection"), frappe.ValidationError)
	doc = frappe.get_doc("Digitizing Queue", queue_name)
	doc.advance_status("Rejected", reason=reason)
	return {
		"ok": True,
		"name": doc.name,
		"status": doc.status,
		"revision_count": doc.revision_count,
	}


@frappe.whitelist()
def get_pending_digitizing(priority: str = None) -> dict:
	"""
	Returns all non-terminal DigitizingQueue entries, optionally filtered
	by priority. Used by the ALICE OS DECORATION panel.
	"""
	filters = {"status": ["in", list(BLOCKING_STATUSES)]}
	if priority:
		filters["priority"] = priority
	items = frappe.get_list(
		"Digitizing Queue",
		filters=filters,
		fields=[
			"name", "status", "priority", "production_recipe",
			"job_card", "work_order", "submitted_on", "revision_count"
		],
		order_by="priority asc, submitted_on asc",
	)
	return {"ok": True, "count": len(items), "items": items}
