"""
ALICE Shop Floor — Install / Setup Hooks
=========================================
Called once after `bench install-app alice_shop_floor`.

Creates:
  • ALICE Settings with default EN + ES languages
  • preferred_language Custom Field on Employee
"""

import frappe


ALICE_DEFAULT_LANGUAGES = [
    {"language_code": "en", "language_name": "English",  "is_active": 1},
    {"language_code": "es", "language_name": "Español",  "is_active": 1},
    {"language_code": "ht", "language_name": "Kreyòl",   "is_active": 0},
    {"language_code": "pt", "language_name": "Português", "is_active": 0},
    {"language_code": "fr", "language_name": "Français",  "is_active": 0},
    {"language_code": "vi", "language_name": "Tiếng Việt", "is_active": 0},
    {"language_code": "zh", "language_name": "中文",       "is_active": 0},
]


def after_install() -> None:
    _seed_alice_settings()
    _add_employee_language_field()
    frappe.db.commit()


# ── ALICE Settings seed ───────────────────────────────────────────────────────

def _seed_alice_settings() -> None:
    try:
        doc = frappe.get_single("ALICE Settings")
        if not doc.supported_languages:
            for row in ALICE_DEFAULT_LANGUAGES:
                doc.append("supported_languages", row)
            doc.translation_auto_on_save = 1
            doc.anthropic_model          = "claude-haiku-4-5-20251001"
            doc.press_confidence_threshold = 0.80
            doc.save(ignore_permissions=True)
            frappe.logger().info("ALICE Settings seeded with default languages.")
    except Exception as exc:
        frappe.log_error(str(exc), "ALICE Setup: seed_alice_settings")


# ── Employee preferred_language custom field ──────────────────────────────────

def _add_employee_language_field() -> None:
    """
    Add preferred_language Select field to Employee DocType if not already there.
    Options match ALICE Language Config codes.
    """
    if frappe.db.exists("Custom Field", {"dt": "Employee",
                                         "fieldname": "preferred_language"}):
        return   # already exists

    try:
        cf = frappe.get_doc({
            "doctype":     "Custom Field",
            "dt":          "Employee",
            "module":      "Alice Shop Floor",
            "label":       "Preferred Language",
            "fieldname":   "preferred_language",
            "fieldtype":   "Select",
            "options":     "\nen\nes\nht\npt\nfr\nvi\nzh",
            "default":     "en",
            "insert_after": "employee_name",
            "description": (
                "Language used on sewing tablet and picker tablet. "
                "ALICE will serve instructions in this language."
            ),
            "in_list_view": 0,
            "in_standard_filter": 1,
        })
        cf.insert(ignore_permissions=True)
        frappe.logger().info("Employee.preferred_language custom field created.")
    except Exception as exc:
        frappe.log_error(str(exc), "ALICE Setup: add_employee_language_field")
