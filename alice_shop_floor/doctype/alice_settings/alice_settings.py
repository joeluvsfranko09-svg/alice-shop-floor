import frappe
from frappe.model.document import Document


class ALICESettings(Document):
    pass


def get_settings() -> "ALICESettings":
    return frappe.get_single("ALICE Settings")


def get_api_key() -> str:
    s = get_settings()
    return s.get_password("anthropic_api_key") or ""


def get_model() -> str:
    s = get_settings()
    return s.anthropic_model or "claude-haiku-4-5-20251001"


def get_active_languages() -> list[dict]:
    """Return list of {code, name} for all active languages."""
    s = get_settings()
    return [
        {"code": row.language_code, "name": row.language_name}
        for row in (s.supported_languages or [])
        if row.is_active
    ]
