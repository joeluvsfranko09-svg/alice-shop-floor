# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt
"""
decoration_engine.py
--------------------
ALICE Decoration Engine — per-method execution layer.

Sits above decoration_router.py (which decides WHERE to send a job) and
drives the actual Job Card lifecycle for each decoration method:

  DTG  — pretreat gate → machine driver → print job → cure → QC
  DTF  — film prep → machine driver → press → peel → QC
  EMB  — DST gate → machine driver → thread load → hoop → stitch → QC

Machine communication is fully abstracted via MachineDriverRegistry.
The engine never knows which physical machine it is talking to —
it only calls driver.send_job(params) and driver.get_job_status(id).

Supported machines (configured in Machine Config DocType):
  Epson SureColor G6070 35" DTF — EpsonDTFDriver     (Epson Edge Print 2 REST + hot folder)
  Epson SureColor DTG           — EpsonDTGDriver     (Epson Edge Print 2 REST + hot folder)
  Melco Summit 15-needle        — MelcoEmbDriver     (Melco OS REST + FTP fallback)
  Pneumatic heat press          — PneumaticPressDriver (non-networked; validates + logs)
  Generic hot folder            — HotFolderDriver    (file drop only)

DTF workflow (3 steps):
  1. start_dtf_job()   → EpsonDTFDriver.send_job()   (prints film on G6070)
  2. Dryer cure        → automatic, no driver
  3. start_press_job() → PneumaticPressDriver.send_job() (validates + logs press params)
  4. dtf_press_complete() → advances Job Card to QC, triggers PressInspectionLog

Also provides:
  - Scan-to-Print workflow (operator scans bin QR → gets recipe params)
  - Replacement order creation (triggered by DecorationDamageLog)
  - ERPNext Job Card hooks (on_submit, on_update)
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from alice_shop_floor.alice_shop_floor.decoration_utils import (
	DecoMethod,
	Workstation,
	get_decoration_queue_summary,
	is_dst_approved,
	log_decoration_failure,
)
from alice_shop_floor.alice_shop_floor.decoration_router import route_job_card
from alice_shop_floor.alice_shop_floor.machine_drivers.registry import MachineDriverRegistry


# ---------------------------------------------------------------------------
# Frappe doc event hooks (registered in hooks.py)
# ---------------------------------------------------------------------------

def on_job_card_submit(doc, method=None):
	"""
	Triggered when a Job Card is submitted.
	If not yet routed, auto-routes via DecorationRouter.
	"""
	if not doc.get("decoration_routed"):
		try:
			result = route_job_card(doc.name)
			if not result.get("ok"):
				frappe.logger().warning(
					f"[DecorationEngine] Auto-route failed for {doc.name}: {result}"
				)
		except Exception as e:
			frappe.logger().error(
				f"[DecorationEngine] Exception routing {doc.name}: {e}"
			)


def on_job_card_update(doc, method=None):
	"""
	Triggered on every Job Card save.
	Checks if an Embroidery job was just unblocked (DST approved).
	"""
	if doc.get("decoration_method") != DecoMethod.EMB:
		return
	if not doc.get("production_recipe"):
		return
	# If this Job Card has a digitizing entry now approved, update workstation
	if doc.get("decoration_routed") and is_dst_approved(doc.production_recipe):
		try:
			frappe.db.set_value("Job Card", doc.name, "workstation", Workstation.EMB)
		except Exception:
			pass


# ---------------------------------------------------------------------------
# Scan-to-Print workflow
# ---------------------------------------------------------------------------

def scan_to_print(job_card_name: str, operator_user: str = None) -> dict:
	"""
	Handles the QR scan event at a decoration station.

	When an operator scans the bin QR code, this function:
	  1. Validates the Job Card is routed and ready
	  2. For Embroidery: checks DST gate
	  3. Returns all machine parameters the operator/machine needs
	  4. Stamps last_scanned_at + last_scanned_by on the Job Card
	  5. Publishes realtime event to the ALICE OS DECORATION panel

	Returns:
	  {
	    "ok": True,
	    "job_card": "JC-00042",
	    "decoration_method": "DTF",
	    "recipe": "RECIPE-DTF-00001",
	    "params": { ... machine params ... },
	    "design_file": "/files/design_123.png",
	    "garment_passport": "GP-00042",
	  }
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	jc = frappe.get_doc("Job Card", job_card_name)
	operator = operator_user or frappe.session.user

	# Ensure the job has been routed
	if not jc.get("decoration_routed"):
		# Try routing now
		route_result = route_job_card(job_card_name)
		if not route_result.get("ok"):
			return {
				"ok": False,
				"error": "not_routed",
				"job_card": job_card_name,
				"detail": route_result,
			}
		jc.reload()

	method = jc.get("decoration_method")
	recipe_name = jc.get("production_recipe")

	if not recipe_name:
		return {
			"ok": False,
			"error": "no_recipe",
			"job_card": job_card_name,
		}

	# DST gate check for Embroidery
	if method == DecoMethod.EMB:
		if not is_dst_approved(recipe_name):
			return {
				"ok": False,
				"error": "dst_not_approved",
				"job_card": job_card_name,
				"decoration_method": method,
				"recipe": recipe_name,
				"message": "DST file not yet approved. Check Digitizing Queue.",
			}

	# Pull machine params from recipe
	recipe = frappe.get_doc("Production Recipe", recipe_name)
	params = recipe.get_machine_params()

	# Stamp scan event on Job Card
	frappe.db.set_value(
		"Job Card",
		job_card_name,
		{
			"last_scanned_at": now_datetime(),
			"last_scanned_by": operator,
		},
	)

	# Look up Garment Passport if available
	garment_passport = frappe.db.get_value(
		"Garment Passport",
		{"job_card": job_card_name},
		"name",
	)

	# Publish live update to ALICE OS dashboard
	frappe.publish_realtime(
		"scan_to_print",
		{
			"job_card": job_card_name,
			"decoration_method": method,
			"recipe": recipe_name,
			"operator": operator,
			"timestamp": str(now_datetime()),
		},
		room=frappe.local.site,
	)

	frappe.logger().info(
		f"[DecorationEngine] Scan-to-Print: {job_card_name} | {method} | "
		f"operator={operator} | recipe={recipe_name}"
	)

	return {
		"ok": True,
		"job_card": job_card_name,
		"decoration_method": method,
		"recipe": recipe_name,
		"params": params,
		"design_file": jc.get("design_file"),
		"design_placement": jc.get("design_placement"),
		"garment_passport": garment_passport,
		"workstation": jc.get("workstation"),
	}


# ---------------------------------------------------------------------------
# Per-method execution helpers
# ---------------------------------------------------------------------------

def start_dtg_job(job_card_name: str, machine_config_name: str = None) -> dict:
	"""
	Initiates a DTG print job via the machine driver layer.

	If machine_config_name is given, uses that specific machine.
	Otherwise, uses the default active DTG machine from MachineConfig.

	Returns the driver result dict plus ALICE context.
	"""
	jc = frappe.get_doc("Job Card", job_card_name)
	if jc.get("decoration_method") != DecoMethod.DTG:
		frappe.throw(
			_(f"{job_card_name} is not a DTG job (method={jc.decoration_method})"),
			frappe.ValidationError,
		)

	design_file = jc.get("design_file")
	if not design_file:
		return {"ok": False, "error": "no_design_file", "job_card": job_card_name}

	recipe_name = jc.get("production_recipe")
	recipe_params = {}
	if recipe_name:
		try:
			recipe_params = frappe.get_doc("Production Recipe", recipe_name).get_machine_params()
		except Exception:
			pass

	try:
		driver = (
			MachineDriverRegistry.get_driver_by_name(machine_config_name)
			if machine_config_name
			else MachineDriverRegistry.get_default_driver(DecoMethod.DTG)
		)
	except RuntimeError as e:
		return {"ok": False, "error": "no_machine_configured",
		        "detail": str(e), "job_card": job_card_name}

	send_params = {
		"job_card":          job_card_name,
		"design_file":       design_file,
		"decoration_method": DecoMethod.DTG,
		"design_placement":  jc.get("design_placement") or "Full Front",
		"recipe_params":     recipe_params,
		"garment_color":     jc.get("garment_color") or "",
		"fabric_type":       jc.get("fabric_type") or "",
		"customer_name":     jc.get("customer_name") or "",
	}

	result = driver.send_job(send_params)
	_stamp_machine_job(job_card_name, result)

	frappe.logger().info(
		f"[DecorationEngine] DTG job sent: {job_card_name} | "
		f"driver={driver.DRIVER_TYPE} | ok={result.get('ok')}"
	)
	return {**result, "job_card": job_card_name, "decoration_method": DecoMethod.DTG}


def start_dtf_job(job_card_name: str, machine_config_name: str = None) -> dict:
	"""
	Initiates a DTF press job via the machine driver layer.

	If machine_config_name is given, uses that specific machine.
	Otherwise, uses the default active DTF machine from MachineConfig.
	"""
	jc = frappe.get_doc("Job Card", job_card_name)
	if jc.get("decoration_method") != DecoMethod.DTF:
		frappe.throw(
			_(f"{job_card_name} is not a DTF job (method={jc.decoration_method})"),
			frappe.ValidationError,
		)

	design_file = jc.get("design_file")
	if not design_file:
		return {"ok": False, "error": "no_design_file", "job_card": job_card_name}

	recipe_name = jc.get("production_recipe")
	recipe_params = {}
	if recipe_name:
		try:
			recipe_params = frappe.get_doc("Production Recipe", recipe_name).get_machine_params()
		except Exception:
			pass

	try:
		driver = (
			MachineDriverRegistry.get_driver_by_name(machine_config_name)
			if machine_config_name
			else MachineDriverRegistry.get_default_driver(DecoMethod.DTF)
		)
	except RuntimeError as e:
		return {"ok": False, "error": "no_machine_configured",
		        "detail": str(e), "job_card": job_card_name}

	send_params = {
		"job_card":          job_card_name,
		"design_file":       design_file,
		"decoration_method": DecoMethod.DTF,
		"design_placement":  jc.get("design_placement") or "Full Front",
		"recipe_params":     recipe_params,
		"customer_name":     jc.get("customer_name") or "",
	}

	result = driver.send_job(send_params)
	_stamp_machine_job(job_card_name, result)

	frappe.logger().info(
		f"[DecorationEngine] DTF job sent: {job_card_name} | "
		f"driver={driver.DRIVER_TYPE} | ok={result.get('ok')}"
	)
	return {**result, "job_card": job_card_name, "decoration_method": DecoMethod.DTF}


def start_emb_job(job_card_name: str, machine_config_name: str = None) -> dict:
	"""
	Initiates an embroidery job via the machine driver layer.
	Enforces DST gate — will not start if DST not approved.
	"""
	jc = frappe.get_doc("Job Card", job_card_name)
	if jc.get("decoration_method") != DecoMethod.EMB:
		frappe.throw(
			_(f"{job_card_name} is not an Embroidery job (method={jc.decoration_method})"),
			frappe.ValidationError,
		)

	recipe_name = jc.get("production_recipe")
	if not is_dst_approved(recipe_name):
		return {
			"ok":    False,
			"error": "dst_not_approved",
			"job_card": job_card_name,
			"recipe": recipe_name,
		}

	design_file = jc.get("emb_dst_file") or jc.get("design_file")
	if not design_file:
		return {"ok": False, "error": "no_dst_file", "job_card": job_card_name}

	recipe_params = {}
	if recipe_name:
		try:
			recipe_params = frappe.get_doc("Production Recipe", recipe_name).get_machine_params()
		except Exception:
			pass

	try:
		driver = (
			MachineDriverRegistry.get_driver_by_name(machine_config_name)
			if machine_config_name
			else MachineDriverRegistry.get_default_driver(DecoMethod.EMB)
		)
	except RuntimeError as e:
		return {"ok": False, "error": "no_machine_configured",
		        "detail": str(e), "job_card": job_card_name}

	send_params = {
		"job_card":           job_card_name,
		"design_file":        design_file,
		"decoration_method":  DecoMethod.EMB,
		"design_placement":   jc.get("design_placement") or "Full Front",
		"recipe_params":      recipe_params,
		"stitch_count":       jc.get("emb_stitch_count") or 0,
		"thread_colors":      _parse_thread_colors(jc.get("emb_thread_colors")),
		"dst_gate_approved":  True,   # already verified by is_dst_approved above
		"customer_name":      jc.get("customer_name") or "",
	}

	result = driver.send_job(send_params)
	_stamp_machine_job(job_card_name, result)

	frappe.logger().info(
		f"[DecorationEngine] Embroidery job sent: {job_card_name} | "
		f"driver={driver.DRIVER_TYPE} | ok={result.get('ok')}"
	)
	return {**result, "job_card": job_card_name, "decoration_method": DecoMethod.EMB}


def start_press_job(job_card_name: str, machine_config_name: str = None) -> dict:
	"""
	Step 3 of the DTF workflow — pneumatic press dispatch.

	Validates press parameters from the ProductionRecipe against safe operating
	ranges, logs the dispatch event, and returns the validated press settings
	for the operator to configure on the physical press.

	The operator physically presses the button; this call records intent and
	validated parameters. Call dtf_press_complete() once transfer is done.

	machine_config_name: optional — if not given, finds the first active
	PneumaticPress machine. If there are multiple press stations, pass the
	specific machine name.

	Returns:
	  {
	    "ok": True,
	    "machine_job_id": "PRESS-JC-00042",
	    "press_params": {
	      "press_temp_f": 385.0, "dwell_time_sec": 12,
	      "pressure_psi": 50, "peel_type": "Hot", "pre_press_sec": 3
	    },
	    "note": "Press params validated — temp 385°F × 12s @ 50 PSI. ...",
	    "job_card": "JC-00042",
	  }
	"""
	jc = frappe.get_doc("Job Card", job_card_name)
	if jc.get("decoration_method") != DecoMethod.DTF:
		frappe.throw(
			_(f"{job_card_name} is not a DTF job (method={jc.decoration_method})"),
			frappe.ValidationError,
		)

	# Gather press params from ProductionRecipe (falls back to driver defaults)
	recipe_name = jc.get("production_recipe")
	recipe_params = {}
	if recipe_name:
		try:
			recipe_params = frappe.get_doc("Production Recipe", recipe_name).get_machine_params()
		except Exception:
			pass

	# Find the press machine driver
	try:
		if machine_config_name:
			driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
		else:
			# Look for any active PneumaticPress — no is_default needed (ZAZFIT has one press)
			press_name = frappe.db.get_value(
				"Machine Config",
				{"driver_type": "PneumaticPress", "is_active": 1},
				"name",
			)
			if not press_name:
				return {
					"ok":        False,
					"error":     "no_press_configured",
					"detail":    "No active PneumaticPress machine found in Machine Config.",
					"job_card":  job_card_name,
				}
			driver = MachineDriverRegistry.get_driver_by_name(press_name)
	except (RuntimeError, ValueError) as e:
		return {"ok": False, "error": "press_driver_error",
		        "detail": str(e), "job_card": job_card_name}

	send_params = {
		"job_card":           job_card_name,
		"recipe_params":      recipe_params,
		"design_placement":   jc.get("design_placement") or "",
		"garment_size":       jc.get("garment_size") or "",
		"fabric_type":        jc.get("fabric_type") or "",
	}

	result = driver.send_job(send_params)

	if result.get("ok"):
		# Advance workstation to DTF Heat Press and stamp press job ID
		press_job_id = result.get("machine_job_id", "")
		try:
			frappe.db.set_value("Job Card", job_card_name, {
				"workstation":       Workstation.DTF,
				"press_job_id":      press_job_id,
				"press_started_at":  now_datetime(),
			})
		except Exception:
			pass   # custom fields may not exist in older installs

		# Realtime update for floor dashboard
		frappe.publish_realtime(
			"dtf_press_dispatched",
			{
				"job_card":    job_card_name,
				"press_params": result.get("press_params", {}),
				"machine":     driver.name,
				"timestamp":   str(now_datetime()),
			},
			room=frappe.local.site,
		)

	frappe.logger().info(
		f"[DecorationEngine] DTF press dispatched: {job_card_name} | "
		f"driver={driver.DRIVER_TYPE} | ok={result.get('ok')}"
	)
	return {**result, "job_card": job_card_name, "decoration_method": DecoMethod.DTF}


def dtf_press_complete(
	job_card_name: str,
	operator_user: str = None,
	defect_count: int = 0,
	rework_flag: int = 0,
	defect_notes: str = "",
	defect_types: str = "",
) -> dict:
	"""
	Marks the DTF press transfer as complete — called by the operator
	at the press station after the garment has been pressed and peeled.

	1. Stamps press_completed_at on the Job Card
	2. Advances workstation to the next QC stage
	3. Creates a PressInspectionLog (triggers V6 Press QC Inspector)
	4. Fires OperatorQualityLog for rolling defect-rate tracking
	4. Publishes realtime event for the supervisor dashboard

	Returns: {"ok": True, "job_card": ..., "next_stage": "Press QC"}
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	operator = operator_user or frappe.session.user

	jc = frappe.get_doc("Job Card", job_card_name)
	if jc.get("decoration_method") != DecoMethod.DTF:
		return {
			"ok":      False,
			"error":   "not_dtf_job",
			"job_card": job_card_name,
		}

	# Stamp completion time
	try:
		frappe.db.set_value("Job Card", job_card_name, {
			"press_completed_at": now_datetime(),
			"press_completed_by": operator,
		})
	except Exception:
		pass

	# Create a PressInspectionLog to trigger V6 QC inspection
	try:
		press_log = frappe.get_doc({
			"doctype":        "Press Inspection Log",
			"job_card":       job_card_name,
			"inspection_type": "DTF Transfer",
			"status":         "Pending",
			"operator":       operator,
			"inspected_at":   now_datetime(),
		})
		press_log.insert(ignore_permissions=True)
		next_stage = "Press QC"
	except Exception as e:
		frappe.logger().warning(
			f"[DecorationEngine] Could not create PressInspectionLog for "
			f"{job_card_name}: {e}"
		)
		next_stage = "Complete"

	# Realtime event for supervisor dashboard and floor view
	frappe.publish_realtime(
		"dtf_press_complete",
		{
			"job_card":    job_card_name,
			"operator":    operator,
			"next_stage":  next_stage,
			"timestamp":   str(now_datetime()),
		},
		room=frappe.local.site,
	)

	# Quality log — record defects and drive rolling stats
	try:
		from alice_shop_floor.alice_shop_floor.operator_quality_utils import log_decoration_job_complete
		machine_config = frappe.db.get_value("Job Card", job_card_name, "machine_config_name")
		started_at = frappe.db.get_value("Job Card", job_card_name, "press_started_at")
		log_decoration_job_complete(
			job_card_name=job_card_name,
			decoration_method=DecoMethod.DTF,
			employee=operator,
			machine_config=machine_config,
			defect_count=int(defect_count or 0),
			rework_flag=bool(rework_flag),
			defect_notes=defect_notes or "",
			defect_types=defect_types or "",
			started_at=started_at,
		)
	except Exception:
		pass

	frappe.logger().info(
		f"[DecorationEngine] DTF press complete: {job_card_name} | "
		f"operator={operator} | next={next_stage}"
	)

	return {
		"ok":         True,
		"job_card":   job_card_name,
		"next_stage": next_stage,
		"operator":   operator,
	}


def _stamp_machine_job(job_card_name: str, driver_result: dict) -> None:
	"""Writes machine_job_id and send timestamp back onto the Job Card."""
	if not driver_result.get("ok"):
		return
	machine_job_id = driver_result.get("machine_job_id") or ""
	if machine_job_id:
		try:
			frappe.db.set_value("Job Card", job_card_name, {
				"machine_job_id":  machine_job_id,
				"machine_sent_at": now_datetime(),
			})
		except Exception:
			pass   # custom field may not exist yet in older installs


def _parse_thread_colors(raw) -> list:
	"""Parses emb_thread_colors — stored as comma-separated string or JSON list."""
	if not raw:
		return []
	if isinstance(raw, list):
		return raw
	import json as _json
	try:
		return _json.loads(raw)
	except Exception:
		return [c.strip() for c in str(raw).split(",") if c.strip()]


# ---------------------------------------------------------------------------
# Replacement order creation (background job)
# ---------------------------------------------------------------------------

def create_replacement_order(job_card_name: str, damage_log_name: str) -> None:
	"""
	Background job: creates a replacement blank garment Purchase Order.
	Supplier priority: SanMar → S&S Activewear → Alphabroader.

	Called via frappe.enqueue() from DecorationDamageLog.after_insert()
	and decoration_utils.log_decoration_failure().
	"""
	try:
		jc = frappe.get_doc("Job Card", job_card_name)
		damage_log = frappe.get_doc("Decoration Damage Log", damage_log_name)

		# Determine garment item to reorder
		item_code = damage_log.garment_item_code
		if not item_code and jc.get("work_order"):
			item_code = frappe.db.get_value("Work Order", jc.work_order, "production_item")

		if not item_code:
			frappe.logger().warning(
				f"[DecorationEngine] Cannot create replacement order for "
				f"{damage_log_name} — no item_code found"
			)
			return

		# Pick supplier (SanMar first, then fallback)
		supplier = _pick_replacement_supplier(item_code)

		# Create a minimal Purchase Order
		po = frappe.get_doc({
			"doctype": "Purchase Order",
			"supplier": supplier,
			"schedule_date": today(),
			"items": [
				{
					"item_code": item_code,
					"qty": 1,
					"schedule_date": today(),
					"description": (
						f"Replacement blank for decoration damage. "
						f"Ref: {damage_log_name} / {job_card_name}"
					),
				}
			],
		})
		po.insert(ignore_permissions=True)
		po.submit()

		# Update damage log with PO reference
		damage_log.mark_replacement_ordered(po.name, supplier)

		frappe.logger().info(
			f"[DecorationEngine] Replacement PO {po.name} created for "
			f"{damage_log_name} — supplier={supplier}, item={item_code}"
		)

	except Exception as e:
		frappe.log_error(
			frappe.get_traceback(),
			f"DecorationEngine.create_replacement_order failed for {damage_log_name}",
		)
		frappe.logger().error(
			f"[DecorationEngine] create_replacement_order exception: {e}"
		)


def _pick_replacement_supplier(item_code: str) -> str:
	"""
	Returns the preferred replacement supplier for a blank garment.
	Checks item's preferred supplier first, then falls back to SanMar.
	"""
	# Check if item has a preferred supplier set
	preferred = frappe.db.get_value("Item", item_code, "default_supplier")
	if preferred:
		return preferred

	# Default priority: SanMar > S&S Activewear > Alphabroader
	for supplier_name in ["SanMar", "S&S Activewear", "Alphabroader"]:
		exists = frappe.db.exists("Supplier", supplier_name)
		if exists:
			return supplier_name

	return "SanMar"  # ultimate fallback


# ---------------------------------------------------------------------------
# Whitelisted API endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist()
def api_scan_to_print(job_card_name: str) -> dict:
	"""Scan-to-Print QR scan handler. Called from mobile scan page."""
	return scan_to_print(job_card_name)


@frappe.whitelist()
def api_start_decoration_job(job_card_name: str) -> dict:
	"""
	Starts a decoration job for the correct method.
	Routes to start_dtg_job / start_dtf_job / start_emb_job based on
	the decoration_method field of the Job Card.
	"""
	method = frappe.db.get_value("Job Card", job_card_name, "decoration_method")
	dispatch = {
		DecoMethod.DTG: start_dtg_job,
		DecoMethod.DTF: start_dtf_job,
		DecoMethod.EMB: start_emb_job,
	}
	handler = dispatch.get(method)
	if not handler:
		frappe.throw(
			_(f"Unknown decoration method '{method}' on Job Card {job_card_name}"),
			frappe.ValidationError,
		)
	return handler(job_card_name)


@frappe.whitelist()
def api_dtf_scan_and_load(job_card_name: str) -> dict:
	"""
	DTF Print Station scan — called when operator scans Job Card at the G6070.

	Extends scan_to_print() with:
	  - DTF-only guard (rejects non-DTF jobs immediately)
	  - Live machine status (Online / Offline) from last ping
	  - design_file_url resolved to an absolute URL for the artwork preview
	  - machine_name so the page can display which printer it will send to

	Returns all data needed to render the print station card in one call.
	"""
	result = scan_to_print(job_card_name)
	if not result.get("ok"):
		return result

	if result.get("decoration_method") != DecoMethod.DTF:
		return {
			"ok":     False,
			"error":  "not_dtf_job",
			"detail": f"Job Card {job_card_name} is {result.get('decoration_method')} — use the correct station.",
			"job_card": job_card_name,
		}

	# Find the active Epson DTF machine name + last ping status
	machine_info = _get_dtf_machine_info()

	# Make design_file URL absolute (Frappe stores as /files/... relative path)
	design_file = result.get("design_file") or ""
	if design_file and design_file.startswith("/"):
		design_file_abs = frappe.utils.get_url(design_file)
	else:
		design_file_abs = design_file

	return {
		**result,
		"design_file_url": design_file_abs,
		"machine_name":    machine_info.get("name", ""),
		"machine_online":  machine_info.get("online", False),
		"machine_status":  machine_info.get("status", "Unknown"),
	}


@frappe.whitelist()
def api_dtf_start_print(job_card_name: str, machine_config_name: str = None) -> dict:
	"""
	Sends the DTF print job to the Epson G6070.
	Thin wrapper around start_dtf_job() for the print station page.
	"""
	return start_dtf_job(job_card_name, machine_config_name)


@frappe.whitelist()
def api_dtf_print_status(job_card_name: str) -> dict:
	"""
	Polls the DTF print job status via the Epson Edge Print 2 hot folder driver.

	Reads machine_job_id from the Job Card, finds the active DTF machine driver,
	and calls driver.get_job_status(). The EpsonDTFDriver checks hot folder file
	presence to determine state (file present = Queued, file absent = Complete).

	States returned: Queued | Printing | Complete | Error | Unknown

	Note: PRESS- prefixed job IDs belong to the pneumatic press step, not the
	print step — they are treated as complete from the print station's perspective.
	"""
	machine_job_id = frappe.db.get_value("Job Card", job_card_name, "machine_job_id") or ""
	if not machine_job_id:
		return {
			"ok":    False,
			"state": "NotSent",
			"error": "no_machine_job_id",
			"job_card": job_card_name,
		}

	# Press job IDs belong to the press step — print step already complete
	if machine_job_id.startswith("PRESS-"):
		return {
			"ok":    True,
			"state": "Complete",
			"detail": {"note": "Job already advanced to press step"},
			"job_card": job_card_name,
		}

	machine_info = _get_dtf_machine_info()
	machine_name = machine_info.get("name")
	if not machine_name:
		return {
			"ok":    False,
			"state": "Unknown",
			"error": "no_dtf_machine_configured",
			"job_card": job_card_name,
		}

	try:
		driver = MachineDriverRegistry.get_driver_by_name(machine_name)
		# For HF- jobs, driver checks hot folder file presence
		# For any other ID format, driver handles gracefully
		status = driver.get_job_status(machine_job_id)
		return {**status, "job_card": job_card_name, "machine_job_id": machine_job_id}
	except Exception as e:
		return {
			"ok":    False,
			"state": "Error",
			"error": str(e),
			"job_card": job_card_name,
		}


@frappe.whitelist()
def api_dtf_film_ready(job_card_name: str) -> dict:
	"""
	Operator confirms the printed DTF film has been cut and is ready for the dryer.
	Stamps dtf_film_printed_at on the Job Card so the dryer-to-press handoff
	is traceable.

	Called from the DTF Print Station "Film Ready — Send to Dryer" button.
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	operator = frappe.session.user

	try:
		frappe.db.set_value("Job Card", job_card_name, {
			"dtf_film_printed_at": now_datetime(),
			"dtf_film_ready_by":   operator,
		})
	except Exception:
		pass  # custom fields may not exist in older installs

	# Realtime event — supervisor dashboard shows "Film in Dryer"
	frappe.publish_realtime(
		"dtf_film_ready",
		{
			"job_card":  job_card_name,
			"operator":  operator,
			"timestamp": str(now_datetime()),
		},
		room=frappe.local.site,
	)

	frappe.logger().info(
		f"[DecorationEngine] DTF film ready: {job_card_name} | operator={operator}"
	)

	return {
		"ok":       True,
		"job_card": job_card_name,
		"next":     "Dryer → Press Station",
	}


def _get_dtf_machine_info() -> dict:
	"""
	Returns the name and last-known online status of the active DTF printer.
	Used by the print station page and status poll.
	"""
	row = frappe.db.get_value(
		"Machine Config",
		{
			"driver_type":        "EpsonEdgePrint",
			"decoration_method":  DecoMethod.DTF,
			"is_active":          1,
		},
		["name", "last_ping_status"],
		as_dict=True,
	)
	if not row:
		return {"name": None, "online": False, "status": "Not configured"}
	return {
		"name":   row.name,
		"online": row.last_ping_status == "Online",
		"status": row.last_ping_status or "Unknown",
	}


@frappe.whitelist()
def api_start_press_job(job_card_name: str, machine_config_name: str = None) -> dict:
	"""
	DTF step 3 — dispatch to pneumatic press.
	Called from the DTF Press Station tablet page when the operator
	has the cured film ready and is standing at the press.
	"""
	return start_press_job(job_card_name, machine_config_name)


@frappe.whitelist()
def api_dtf_press_complete(
	job_card_name: str,
	operator_employee: str = None,
	defect_count: int = 0,
	rework_flag: int = 0,
	defect_notes: str = "",
	defect_types: str = "",
) -> dict:
	"""
	DTF step 3 completion — operator has pressed and peeled the transfer.
	Advances Job Card to Press QC stage and triggers PressInspectionLog.
	Also fires OperatorQualityLog for rolling defect-rate tracking.
	Called from the DTF Press Station tablet page "Transfer Complete" button.
	"""
	return dtf_press_complete(
		job_card_name,
		operator_user=operator_employee,
		defect_count=int(defect_count or 0),
		rework_flag=bool(rework_flag),
		defect_notes=defect_notes or "",
		defect_types=defect_types or "",
	)


# ---------------------------------------------------------------------------
# Multi-machine helpers — shared across all decoration methods
# ---------------------------------------------------------------------------

def _get_deco_machines(decoration_method: str) -> list:
	"""
	Returns all active MachineConfig records for the given decoration method,
	enriched with live online/busy status.

	Each dict has:
	  name          — MachineConfig document name
	  machine_id    — human label (epson_printer_id | melco_machine_id | machine_name)
	  online        — bool, True if last_ping_status == "Online"
	  status        — last_ping_status string
	  busy          — bool, True if any Job Card is currently WIP on this machine
	  current_job   — job_card name if busy
	  hoop_size     — melco_hoop_size if EMB, else None
	  driver_type   — driver class name
	"""
	rows = frappe.get_all(
		"Machine Config",
		filters={"decoration_method": decoration_method, "is_active": 1},
		fields=[
			"name", "machine_name", "driver_type",
			"last_ping_status", "epson_printer_id",
			"melco_machine_id", "melco_hoop_size",
		],
		order_by="machine_name asc",
	)

	# Build busy map: which machines have a WIP Job Card right now?
	busy_map: dict[str, str] = {}
	wip_jcs = frappe.get_all(
		"Job Card",
		filters={
			"decoration_method": decoration_method,
			"status": ["in", ["Open", "Work In Progress"]],
			"machine_config_name": ["is", "set"],
		},
		fields=["machine_config_name", "name"],
	) if rows else []
	for jc in wip_jcs:
		busy_map[jc.machine_config_name] = jc.name

	result = []
	for r in rows:
		machine_id = r.epson_printer_id or r.melco_machine_id or r.machine_name or r.name
		current_job = busy_map.get(r.name)
		result.append({
			"name":        r.name,
			"machine_id":  machine_id,
			"online":      r.last_ping_status == "Online",
			"status":      r.last_ping_status or "Unknown",
			"busy":        bool(current_job),
			"current_job": current_job or "",
			"hoop_size":   r.melco_hoop_size or None,
			"driver_type": r.driver_type or "",
		})
	return result


def _get_certified_operators(
	decoration_method: str,
	machine_config_name: str = None,
) -> list:
	"""
	Returns active, non-expired operator certifications for a decoration method.
	Merges method-level certs and machine-specific certs, deduplicates by employee
	(higher proficiency wins), sorts Expert → Certified → Trainee.

	Each dict has:
	  employee        — Employee link
	  employee_name   — full name
	  proficiency     — "Expert" | "Certified" | "Trainee"
	  machine_config  — specific machine or None (method-level cert)
	"""
	from frappe.utils import getdate, today

	today_date = getdate(today())
	proficiency_rank = {"Expert": 3, "Certified": 2, "Trainee": 1}

	# Pull all relevant certs: method-level + machine-specific
	filters = {
		"decoration_method": decoration_method,
		"is_active": 1,
	}
	rows = frappe.get_all(
		"Machine Operator Certification",
		filters=filters,
		fields=["employee", "employee_name", "proficiency_level", "machine_config", "expires_on"],
	)

	# Filter expired
	valid = [
		r for r in rows
		if not r.expires_on or getdate(r.expires_on) >= today_date
	]

	# Scope to this machine if specified (keep method-level certs too)
	if machine_config_name:
		valid = [
			r for r in valid
			if not r.machine_config or r.machine_config == machine_config_name
		]

	# Deduplicate by employee — keep highest proficiency
	best: dict[str, dict] = {}
	for r in valid:
		emp = r.employee
		rank = proficiency_rank.get(r.proficiency_level, 0)
		if emp not in best or rank > proficiency_rank.get(best[emp]["proficiency"], 0):
			best[emp] = {
				"employee":       r.employee,
				"employee_name":  r.employee_name or r.employee,
				"proficiency":    r.proficiency_level,
				"machine_config": r.machine_config or None,
			}

	# Sort Expert first
	sorted_ops = sorted(
		best.values(),
		key=lambda x: proficiency_rank.get(x["proficiency"], 0),
		reverse=True,
	)
	return sorted_ops


def _first_machine_shims(machines: list) -> dict:
	"""
	Backward-compat helper: returns single machine_name / machine_online /
	machine_status fields from the first idle+online machine in the list,
	or falls back to the first machine if none are idle.

	Used by scan_and_load responses so older code that reads a single
	machine_name field still works.
	"""
	if not machines:
		return {"machine_name": "", "machine_online": False, "machine_status": "Not configured"}

	idle_online = [m for m in machines if m.get("online") and not m.get("busy")]
	pick = idle_online[0] if idle_online else machines[0]
	return {
		"machine_name":   pick["name"],
		"machine_online": pick["online"],
		"machine_status": pick["status"],
	}


# ---------------------------------------------------------------------------
# DTG station — scan_and_load + start + status + complete + pretreat
# ---------------------------------------------------------------------------

def api_dtg_scan_and_load(job_card_name: str) -> dict:
	"""
	DTG Print Station scan — called when operator scans a Job Card QR.

	Extends scan_to_print() with:
	  - DTG-only guard
	  - DTG recipe params (platen_size, pretreat_required, ink_profile,
	    cure_temp_f, cure_time_sec)
	  - Garment color + dark-garment flag
	  - available_machines list (all active DTG machines with online/busy)
	  - certified_operators list (operators certified for DTG)

	Returns everything the dtg_print_station.js page needs in one call.
	"""
	result = scan_to_print(job_card_name)
	if not result.get("ok"):
		return result

	if result.get("decoration_method") != DecoMethod.DTG:
		return {
			"ok":     False,
			"error":  "not_dtg_job",
			"detail": (
				f"Job Card {job_card_name} is {result.get('decoration_method')} "
				f"— use the correct station."
			),
			"job_card": job_card_name,
		}

	params = result.get("params", {})
	jc = frappe.get_cached_doc("Job Card", job_card_name)
	garment_color = jc.get("garment_color") or ""

	# Ink profile: dark garments need CMYK+W
	dark_colors = {"black", "navy", "dark grey", "dark gray", "maroon", "forest green", "dark red"}
	is_dark = garment_color.lower() in dark_colors if garment_color else False
	ink_profile = params.get("dtg_ink_profile") or ("dark_garment" if is_dark else "light_garment")

	# Design file URL (absolute)
	design_file = result.get("design_file") or ""
	if design_file and design_file.startswith("/"):
		design_file = frappe.utils.get_url(design_file)

	machines      = _get_deco_machines(DecoMethod.DTG)
	operators     = _get_certified_operators(DecoMethod.DTG)
	shims         = _first_machine_shims(machines)

	return {
		**result,
		# DTG-specific params
		"platen_size":        params.get("dtg_platen_size") or params.get("platen_size") or "L",
		"pretreat_required":  bool(params.get("dtg_pretreat_required") or is_dark),
		"ink_profile":        ink_profile,
		"cure_temp_f":        params.get("dtg_cure_temp") or params.get("cure_temp_f") or 320,
		"cure_time_sec":      params.get("dtg_cure_time") or params.get("cure_time_sec") or 90,
		# Garment context
		"garment_color":      garment_color,
		"design_file_url":    design_file,
		# Machine + operator selection data
		"available_machines": machines,
		"certified_operators": operators,
		# Backward-compat single-machine shims
		**shims,
	}


def api_dtg_start_print(
	job_card_name: str,
	machine_config_name: str = None,
	operator_employee: str = None,
) -> dict:
	"""
	Sends the DTG print file to the selected Epson F2270/F3070 machine.
	Stamps operator_employee on the Job Card before delegating to start_dtg_job().
	"""
	if operator_employee:
		try:
			frappe.db.set_value("Job Card", job_card_name, {
				"decoration_operator":  operator_employee,
				"machine_config_name":  machine_config_name or "",
				"machine_sent_at":      now_datetime(),
			})
		except Exception:
			pass
	return start_dtg_job(job_card_name, machine_config_name)


def api_dtg_print_status(job_card_name: str) -> dict:
	"""
	Polls DTG print status via hot folder file presence on the Epson F2270/F3070.
	States: NotSent | Queued | Complete | Error | Unknown
	"""
	machine_job_id = frappe.db.get_value("Job Card", job_card_name, "machine_job_id") or ""
	if not machine_job_id:
		return {
			"ok":    False,
			"state": "NotSent",
			"error": "no_machine_job_id",
			"job_card": job_card_name,
		}

	# Get the machine this job was sent to
	machine_config_name = frappe.db.get_value("Job Card", job_card_name, "machine_config_name") or ""
	if not machine_config_name:
		# Fallback: first active DTG machine
		machine_config_name = frappe.db.get_value(
			"Machine Config",
			{"decoration_method": DecoMethod.DTG, "is_active": 1},
			"name",
		) or ""
	if not machine_config_name:
		return {
			"ok":    False,
			"state": "Unknown",
			"error": "no_dtg_machine_configured",
			"job_card": job_card_name,
		}

	try:
		driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
		status = driver.get_job_status(machine_job_id)
		return {**status, "job_card": job_card_name, "machine_job_id": machine_job_id}
	except Exception as e:
		return {
			"ok":    False,
			"state": "Error",
			"error": str(e),
			"job_card": job_card_name,
		}


def api_dtg_print_complete(
	job_card_name: str,
	operator_employee: str = None,
	defect_count: int = 0,
	rework_flag: int = 0,
	defect_notes: str = "",
	defect_types: str = "",
) -> dict:
	"""
	Operator confirms garment printed and sent to cure tunnel.
	Stamps dtg_complete_at and operator, fires OperatorQualityLog for
	rolling defect-rate tracking.
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	operator = operator_employee or frappe.session.user
	machine_config = frappe.db.get_value("Job Card", job_card_name, "machine_config_name")
	started_at = frappe.db.get_value("Job Card", job_card_name, "machine_sent_at")

	try:
		frappe.db.set_value("Job Card", job_card_name, {
			"dtg_complete_at":     now_datetime(),
			"decoration_operator": operator,
		})
	except Exception:
		pass

	# Quality log
	try:
		from alice_shop_floor.alice_shop_floor.operator_quality_utils import log_decoration_job_complete
		log_decoration_job_complete(
			job_card_name=job_card_name,
			decoration_method=DecoMethod.DTG,
			employee=operator,
			machine_config=machine_config,
			defect_count=int(defect_count or 0),
			rework_flag=bool(rework_flag),
			defect_notes=defect_notes or "",
			defect_types=defect_types or "",
			started_at=started_at,
		)
	except Exception:
		pass

	frappe.publish_realtime(
		"dtg_print_complete",
		{
			"job_card":  job_card_name,
			"operator":  operator,
			"timestamp": str(now_datetime()),
		},
		room=frappe.local.site,
	)
	frappe.logger().info(
		f"[DecorationEngine] DTG complete: {job_card_name} | operator={operator}"
	)
	return {
		"ok":       True,
		"job_card": job_card_name,
		"next":     "QC",
		"operator": operator,
	}


def api_dtg_pretreat_confirmed(job_card_name: str) -> dict:
	"""
	Operator confirms pretreatment applied for a dark garment before printing.
	Stamps dtg_pretreat_confirmed_at on the Job Card.
	Called from the DTG Print Station pretreat modal "Confirmed" button.
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	try:
		frappe.db.set_value("Job Card", job_card_name, {
			"dtg_pretreat_confirmed_at": now_datetime(),
			"dtg_pretreat_confirmed_by": frappe.session.user,
		})
	except Exception:
		pass

	frappe.logger().info(
		f"[DecorationEngine] DTG pretreat confirmed: {job_card_name} | "
		f"operator={frappe.session.user}"
	)

	return {"ok": True, "job_card": job_card_name}


# ---------------------------------------------------------------------------
# Embroidery station — scan_and_load + start + status + complete
# ---------------------------------------------------------------------------

def api_emb_scan_and_load(job_card_name: str) -> dict:
	"""
	Embroidery Station scan — called when operator scans a Job Card QR.

	Returns:
	  - DST gate status (dst_status, dst_approved)
	  - Thread color sequence (thread_colors list with color_name, needle_pos, thread_code)
	  - Stitch count, hoop size, stabilizer type, speed_spm, underlay
	  - available_machines list (Melco heads with online/busy/hoop_size/compatible flag)
	  - certified_operators list
	"""
	result = scan_to_print(job_card_name)

	# EMB returns ok=False if DST not approved — that's expected; we still render the page
	emb_error = result.get("error")
	is_dst_blocked = emb_error == "dst_not_approved"

	if not result.get("ok") and not is_dst_blocked:
		return result

	if result.get("decoration_method") and result["decoration_method"] != DecoMethod.EMB:
		return {
			"ok":     False,
			"error":  "not_emb_job",
			"detail": (
				f"Job Card {job_card_name} is {result.get('decoration_method')} "
				f"— use the correct station."
			),
			"job_card": job_card_name,
		}

	jc = frappe.get_cached_doc("Job Card", job_card_name)
	recipe_name = jc.get("production_recipe") or result.get("recipe")
	params = result.get("params", {})

	# DST gate status from DigitizingQueue
	dst_approved = False
	dst_status   = "Not Required"
	if recipe_name:
		dq = frappe.db.get_value(
			"Digitizing Queue",
			{"production_recipe": recipe_name},
			["status", "name"],
			as_dict=True,
		)
		if dq:
			dst_status   = dq.status
			dst_approved = dq.status in ("Approved", "Released")
		else:
			dst_approved = True   # no queue entry = no gate
			dst_status   = "Approved"

	# Thread colors from recipe
	thread_colors = []
	if recipe_name:
		try:
			recipe = frappe.get_doc("Production Recipe", recipe_name)
			for tc in (recipe.get("emb_thread_colors") or []):
				thread_colors.append({
					"color_name":   tc.get("color_name") or tc.get("thread_color") or "",
					"needle_pos":   tc.get("needle_position") or tc.get("needle_pos") or "",
					"thread_code":  tc.get("thread_code") or "",
				})
		except Exception:
			pass

	# Machine list — include hoop compatibility check
	required_hoop = jc.get("emb_hoop_size") or params.get("emb_hoop_size") or ""
	machines      = _get_deco_machines(DecoMethod.EMB)
	for m in machines:
		installed = m.get("hoop_size") or ""
		m["compatible"] = (not required_hoop) or (installed == required_hoop) or (not installed)

	operators = _get_certified_operators(DecoMethod.EMB)
	shims     = _first_machine_shims(machines)

	return {
		"ok":           True,
		"job_card":     job_card_name,
		"decoration_method": DecoMethod.EMB,
		"recipe":       recipe_name,
		# DST gate
		"dst_status":   dst_status,
		"dst_approved": dst_approved,
		# Stitch params
		"stitch_count":      params.get("emb_stitch_count") or jc.get("emb_stitch_count") or 0,
		"hoop_size":         required_hoop,
		"stabilizer_type":   params.get("emb_stabilizer_type") or "",
		"needle_count":      params.get("emb_needle_count") or 0,
		"thread_count":      len(thread_colors),
		"thread_colors":     thread_colors,
		# Design
		"design_placement":  jc.get("design_placement") or "Full Front",
		"garment_passport":  result.get("garment_passport"),
		# Machine + operator selection
		"available_machines":  machines,
		"certified_operators": operators,
		# Backward-compat shims
		**shims,
	}


def api_emb_start_job(
	job_card_name: str,
	machine_config_name: str = None,
	operator_employee: str = None,
) -> dict:
	"""
	Sends embroidery DST file to the selected Melco Summit head via FTP.
	Stamps operator_employee and machine before delegating to start_emb_job().
	Enforces DST gate — rejects if DigitizingQueue entry not Approved/Released.
	"""
	if operator_employee:
		try:
			frappe.db.set_value("Job Card", job_card_name, {
				"decoration_operator":  operator_employee,
				"machine_config_name":  machine_config_name or "",
			})
		except Exception:
			pass
	return start_emb_job(job_card_name, machine_config_name)


def api_emb_job_status(job_card_name: str) -> dict:
	"""
	Polls embroidery job status via FTP file presence on the Melco Summit.
	States: NotSent | Queued | Complete | Error
	File present on FTP = Queued. File absent = Complete (SUMMIT Manager consumed it).
	"""
	machine_job_id = frappe.db.get_value("Job Card", job_card_name, "machine_job_id") or ""
	if not machine_job_id:
		return {
			"ok":    False,
			"state": "NotSent",
			"error": "no_machine_job_id",
			"job_card": job_card_name,
		}

	machine_config_name = frappe.db.get_value("Job Card", job_card_name, "machine_config_name") or ""
	if not machine_config_name:
		machine_config_name = frappe.db.get_value(
			"Machine Config",
			{"decoration_method": DecoMethod.EMB, "is_active": 1},
			"name",
		) or ""
	if not machine_config_name:
		return {
			"ok":    False,
			"state": "Unknown",
			"error": "no_emb_machine_configured",
			"job_card": job_card_name,
		}

	try:
		driver = MachineDriverRegistry.get_driver_by_name(machine_config_name)
		status = driver.get_job_status(machine_job_id)
		return {**status, "job_card": job_card_name, "machine_job_id": machine_job_id}
	except Exception as e:
		return {
			"ok":    False,
			"state": "Error",
			"error": str(e),
			"job_card": job_card_name,
		}




def _get_deco_machines_by_driver(driver_type: str) -> list:
	"""
	Like _get_deco_machines but filters by driver_type instead of decoration_method.
	Used for PneumaticPress machines which share decoration_method=DTF with the
	Epson G6070 film printer but are physically separate units.
	"""
	rows = frappe.get_all(
		"Machine Config",
		filters={"driver_type": driver_type, "is_active": 1},
		fields=[
			"name", "machine_name", "driver_type", "last_ping_status",
			"press_station_label", "epson_printer_id", "melco_machine_id",
		],
	)

	busy_map: dict[str, str] = {}
	wip_jcs = frappe.get_all(
		"Job Card",
		filters={
			"status": ["in", ["Open", "Work In Progress"]],
			"machine_config_name": ["is", "set"],
		},
		fields=["machine_config_name", "name"],
	)
	for jc in wip_jcs:
		busy_map[jc.machine_config_name] = jc.name

	machines = []
	for row in rows:
		machine_id = (
			row.press_station_label
			or row.epson_printer_id
			or row.melco_machine_id
			or row.name
		)
		busy_job = busy_map.get(row.name)
		machines.append({
			"name":        row.name,
			"machine_id":  machine_id,
			"online":      True,   # pneumatic presses are non-networked; always "ready"
			"status":      row.last_ping_status or "Ready",
			"busy":        bool(busy_job),
			"current_job": busy_job,
			"hoop_size":   None,
			"driver_type": row.driver_type or "",
		})
	return machines


def api_emb_job_complete(
	job_card_name: str,
	operator_employee: str = None,
	defect_count: int = 0,
	rework_flag: int = 0,
	defect_notes: str = "",
	defect_types: str = "",
) -> dict:
	"""
	Operator confirms embroidery done — garment is unhooped and visually inspected.
	Stamps emb_complete_at, fires OperatorQualityLog, advances Job Card.
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	operator = operator_employee or frappe.session.user
	machine_config = frappe.db.get_value("Job Card", job_card_name, "machine_config_name")
	started_at = frappe.db.get_value("Job Card", job_card_name, "machine_sent_at")

	try:
		frappe.db.set_value("Job Card", job_card_name, {
			"emb_complete_at":     now_datetime(),
			"decoration_operator": operator,
		})
	except Exception:
		pass

	# Quality log
	try:
		from alice_shop_floor.alice_shop_floor.operator_quality_utils import log_decoration_job_complete
		log_decoration_job_complete(
			job_card_name=job_card_name,
			decoration_method=DecoMethod.EMB,
			employee=operator,
			machine_config=machine_config,
			defect_count=int(defect_count or 0),
			rework_flag=bool(rework_flag),
			defect_notes=defect_notes or "",
			defect_types=defect_types or "",
			started_at=started_at,
		)
	except Exception:
		pass

	frappe.publish_realtime(
		"emb_job_complete",
		{
			"job_card":  job_card_name,
			"operator":  operator,
			"timestamp": str(now_datetime()),
		},
		room=frappe.local.site,
	)
	frappe.logger().info(
		f"[DecorationEngine] EMB complete: {job_card_name} | operator={operator}"
	)
	return {
		"ok":       True,
		"job_card": job_card_name,
		"next":     "QC",
	}


def api_dtf_press_scan_and_load(job_card_name: str) -> dict:
	"""
	DTF Press Station scan — operator scans a Job Card QR at the press.

	Returns everything dtf_press_station.js needs:
	  - press_params (press_temp_f, dwell_time_sec, pressure_psi, peel_type, pre_press_sec)
	  - available_machines: all active PneumaticPress machines with busy state
	  - certified_operators: DTF-certified operators
	  - garment / job_card context fields
	"""
	if not job_card_name:
		frappe.throw(_("job_card_name is required"), frappe.ValidationError)

	jc = frappe.get_doc("Job Card", job_card_name)

	if jc.get("decoration_method") != DecoMethod.DTF:
		return {
			"ok":     False,
			"error":  "not_dtf_job",
			"detail": (
				f"Job Card {job_card_name} is {jc.get('decoration_method')} "
				f"\u2014 use the DTF Print Station instead."
			),
			"job_card": job_card_name,
		}

	# Pull press params from ProductionRecipe (safe defaults if not set)
	recipe_name = jc.get("production_recipe") or ""
	recipe_params: dict = {}
	if recipe_name:
		try:
			recipe_params = frappe.get_doc("Production Recipe", recipe_name).get_machine_params()
		except Exception:
			pass

	press_params = {
		"press_temp_f":   recipe_params.get("press_temp_f")  or 385,
		"dwell_time_sec": recipe_params.get("dwell_time_sec") or 12,
		"pressure_psi":   recipe_params.get("pressure_psi")  or 50,
		"peel_type":      recipe_params.get("peel_type")      or "Hot",
		"pre_press_sec":  recipe_params.get("pre_press_sec")  or 3,
	}

	press_machines = _get_deco_machines_by_driver("PneumaticPress")
	certified_operators = _get_certified_operators(DecoMethod.DTF)
	shims = _first_machine_shims(press_machines)

	try:
		frappe.db.set_value("Job Card", job_card_name, {"last_scanned_at": now_datetime()})
	except Exception:
		pass

	return {
		"ok":                  True,
		"job_card":            job_card_name,
		"decoration_method":   DecoMethod.DTF,
		"garment_color":       jc.get("garment_color") or "",
		"garment_size":        jc.get("garment_size") or "",
		"design_placement":    jc.get("design_placement") or "",
		"press_params":        press_params,
		"available_machines":  press_machines,
		"certified_operators": certified_operators,
		**shims,
	}
