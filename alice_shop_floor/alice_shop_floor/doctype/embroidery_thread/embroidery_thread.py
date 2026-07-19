# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class EmbroideryThread(Document):
	"""
	Child table row representing a single needle/thread assignment
	in a ProductionRecipe for embroidery decoration.

	Each row maps one needle position to one thread color.
	Madeira and Isacord color codes are the standard for Tajima TMEF-H1506.
	"""

	def validate(self):
		self._validate_needle_position()
		self._normalize_hex()

	def _validate_needle_position(self):
		if self.thread_position and (self.thread_position < 1 or self.thread_position > 15):
			frappe.throw(
				frappe._("Needle position must be between 1 and 15 (Tajima TMEF-H1506 is a 15-needle machine)."),
				frappe.ValidationError,
			)

	def _normalize_hex(self):
		"""Ensure hex value is stored in #RRGGBB format."""
		if self.thread_hex and not self.thread_hex.startswith("#"):
			self.thread_hex = f"#{self.thread_hex.lstrip('#')}"
		if self.thread_hex:
			self.thread_hex = self.thread_hex.upper()
