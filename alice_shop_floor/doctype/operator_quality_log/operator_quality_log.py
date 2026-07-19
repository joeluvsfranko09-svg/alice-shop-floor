# Copyright (c) 2026, Athlettia LLC and contributors
# For license information, please see license.txt
"""
OperatorQualityLog — immutable record of one decoration job's quality outcome.

One log entry is created automatically each time an operator completes a
decoration step (DTG print, DTF press, embroidery). It captures:

  - Which operator ran the job (employee)
  - Which machine they used (machine_config)
  - How many defects appeared (defect_count)
  - Whether the garment needed rework (rework_flag)
  - How long the step took (cycle_time_sec)
  - What their certification level was at the time (proficiency_at_time)

These logs roll up into Module 6 (Operator Efficiency & Skill AI) to:
  - Update the operator's rolling defect rate per method/machine
  - Adjust their quality score in SkillProfileHistory
  - Trigger supervisor alerts when defect rate exceeds threshold
  - Feed the ALICE OS quality dashboards

Created via operator_quality_utils.log_decoration_job_complete().
Never created manually — always auto-generated on job completion.
"""

from frappe.model.document import Document


class OperatorQualityLog(Document):
	"""Immutable quality log — no business logic in the document itself."""
	pass
