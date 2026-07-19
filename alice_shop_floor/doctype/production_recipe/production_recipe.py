# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import _


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DTG_REQUIRED = ["dtg_platen_size", "dtg_cure_temp", "dtg_cure_time"]
DTF_REQUIRED = ["dtf_press_temp", "dtf_dwell_time", "dtf_pressure", "dtf_peel_type"]
EMB_REQUIRED = ["emb_stitch_count", "emb_needle_count", "emb_stabilizer_type"]

DTF_TEMP_MIN = 340.0   # °F — lowest safe STAHLS' press temp
DTF_TEMP_MAX = 410.0   # °F — highest safe STAHLS' press temp
DTF_DWELL_MIN = 8      # seconds
DTF_DWELL_MAX = 30     # seconds

DTG_CURE_TEMP_MIN = 280.0
DTG_CURE_TEMP_MAX = 380.0

TAJIMA_MAX_NEEDLES = 15


class ProductionRecipe(Document):
	"""
	Stores all machine-specific parameters that ERPNext BOM cannot hold.

	One recipe covers ONE decoration method (DTG | DTF | Embroidery).
	A Job Card links to exactly one ProductionRecipe; a single Work Order
	can have multiple Job Cards with different recipes (e.g., front DTG +
	sleeve EMB).

	Key relationships:
	  Job Card   → production_recipe (Link)
	  BOM Item   → production_recipe (Link, custom field)
	  Digitizing Queue → production_recipe (Link, for EMB)
	"""

	# ------------------------------------------------------------------
	# Frappe lifecycle hooks
	# ------------------------------------------------------------------

	def validate(self):
		self._validate_method_fields()
		self._validate_dtf_params()
		self._validate_dtg_params()
		self._validate_emb_params()

	def before_save(self):
		self._auto_name_if_blank()

	def on_submit(self):
		frappe.logger().info(
			f"[ProductionRecipe] {self.name} submitted — method={self.decoration_method}"
		)

	# ------------------------------------------------------------------
	# Validation helpers
	# ------------------------------------------------------------------

	def _validate_method_fields(self):
		"""Check that required fields for the selected method are present."""
		method_map = {
			"DTG": DTG_REQUIRED,
			"DTF": DTF_REQUIRED,
			"Embroidery": EMB_REQUIRED,
		}
		required = method_map.get(self.decoration_method, [])
		missing = [f for f in required if not self.get(f)]
		if missing:
			labels = [self.meta.get_field(f).label for f in missing]
			frappe.throw(
				_("The following fields are required for {0}: {1}").format(
					self.decoration_method, ", ".join(labels)
				),
				frappe.MandatoryError,
			)

	def _validate_dtf_params(self):
		if self.decoration_method != "DTF":
			return
		if self.dtf_press_temp:
			if not (DTF_TEMP_MIN <= self.dtf_press_temp <= DTF_TEMP_MAX):
				frappe.throw(
					_("DTF press temp must be between {0}°F and {1}°F (got {2}°F). "
					  "STAHLS' standard is 385°F.").format(
						DTF_TEMP_MIN, DTF_TEMP_MAX, self.dtf_press_temp
					),
					frappe.ValidationError,
				)
		if self.dtf_dwell_time:
			if not (DTF_DWELL_MIN <= self.dtf_dwell_time <= DTF_DWELL_MAX):
				frappe.throw(
					_("DTF dwell time must be between {0}s and {1}s (got {2}s). "
					  "STAHLS' standard is 12 seconds.").format(
						DTF_DWELL_MIN, DTF_DWELL_MAX, self.dtf_dwell_time
					),
					frappe.ValidationError,
				)

	def _validate_dtg_params(self):
		if self.decoration_method != "DTG":
			return
		if self.dtg_cure_temp:
			if not (DTG_CURE_TEMP_MIN <= self.dtg_cure_temp <= DTG_CURE_TEMP_MAX):
				frappe.throw(
					_("DTG cure temp must be between {0}°F and {1}°F (got {2}°F).").format(
						DTG_CURE_TEMP_MIN, DTG_CURE_TEMP_MAX, self.dtg_cure_temp
					),
					frappe.ValidationError,
				)

	def _validate_emb_params(self):
		if self.decoration_method != "Embroidery":
			return
		if self.emb_needle_count and self.emb_needle_count > TAJIMA_MAX_NEEDLES:
			frappe.throw(
				_("Needle count cannot exceed {0} (Tajima TMEF-H1506 limit). Got {1}.").format(
					TAJIMA_MAX_NEEDLES, self.emb_needle_count
				),
				frappe.ValidationError,
			)
		if self.emb_thread_colors:
			positions = [row.thread_position for row in self.emb_thread_colors]
			if len(positions) != len(set(positions)):
				frappe.throw(
					_("Duplicate needle positions found in Thread Color Map. "
					  "Each needle position must be unique."),
					frappe.ValidationError,
				)

	def _auto_name_if_blank(self):
		if not self.recipe_name:
			method_abbr = {"DTG": "DTG", "DTF": "DTF", "Embroidery": "EMB"}.get(
				self.decoration_method, "DEC"
			)
			placement = self.design_placement or "GEN"
			self.recipe_name = f"{method_abbr} — {placement}"

	# ------------------------------------------------------------------
	# Public methods (callable from Job Card / DecorationRouter)
	# ------------------------------------------------------------------

	def get_machine_params(self) -> dict:
		"""
		Return a flat dict of machine parameters ready to push to
		the physical machine controller or to stamp on a Job Card.

		Used by decoration_engine.py before sending a job to the machine.
		"""
		base = {
			"recipe_name": self.recipe_name,
			"decoration_method": self.decoration_method,
			"design_placement": self.design_placement,
		}
		if self.decoration_method == "DTG":
			base.update({
				"platen_size": self.dtg_platen_size,
				"pretreat_required": bool(self.dtg_pretreat_required),
				"ink_profile": self.dtg_ink_profile,
				"resolution": self.dtg_resolution,
				"cure_temp_f": self.dtg_cure_temp,
				"cure_time_sec": self.dtg_cure_time,
			})
		elif self.decoration_method == "DTF":
			base.update({
				"film_width_in": self.dtf_film_width,
				"press_temp_f": self.dtf_press_temp,
				"dwell_time_sec": self.dtf_dwell_time,
				"pressure": self.dtf_pressure,
				"peel_type": self.dtf_peel_type,
				"film_type": self.dtf_film_type,
			})
		elif self.decoration_method == "Embroidery":
			base.update({
				"dst_file": self.emb_dst_file,
				"stitch_count": self.emb_stitch_count,
				"needle_count": self.emb_needle_count,
				"hoop_size": self.emb_hoop_size,
				"stabilizer_type": self.emb_stabilizer_type,
				"thread_map": [
					{
						"position": row.thread_position,
						"brand": row.thread_brand,
						"code": row.thread_color_code,
						"name": row.thread_color_name,
						"hex": row.thread_hex,
					}
					for row in (self.emb_thread_colors or [])
				],
			})
		return base

	def is_dst_ready(self) -> bool:
		"""
		For Embroidery recipes: returns True only if a DST file is attached
		AND the DigitizingQueue entry (if any) is in 'Approved' or 'Released' status.
		"""
		if self.decoration_method != "Embroidery":
			return True  # non-embroidery recipes are always ready
		if not self.emb_dst_file:
			return False
		# Check DigitizingQueue — if an open queue entry blocks this recipe, not ready
		blocking = frappe.get_list(
			"Digitizing Queue",
			filters={
				"production_recipe": self.name,
				"status": ["not in", ["Approved", "Released", "Cancelled"]],
			},
			limit=1,
		)
		return len(blocking) == 0


@frappe.whitelist()
def get_recipe_params(recipe_name: str) -> dict:
	"""
	API wrapper — returns machine params for a given ProductionRecipe.
	Called by the ALICE scan-to-print flow and decoration engine.
	"""
	if not recipe_name:
		frappe.throw(_("recipe_name is required"), frappe.ValidationError)
	doc = frappe.get_doc("Production Recipe", recipe_name)
	return {"ok": True, "params": doc.get_machine_params()}


@frappe.whitelist()
def list_active_recipes(decoration_method: str = None) -> dict:
	"""
	Returns all active ProductionRecipes, optionally filtered by method.
	Used by DecorationRouter to find eligible recipes for a job.
	"""
	filters = {"is_active": 1}
	if decoration_method:
		filters["decoration_method"] = decoration_method
	recipes = frappe.get_list(
		"Production Recipe",
		filters=filters,
		fields=["name", "recipe_name", "decoration_method", "design_placement", "item_code"],
		order_by="recipe_name asc",
	)
	return {"ok": True, "recipes": recipes}
