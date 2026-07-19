# Copyright (c) 2026, Athlettia LLC
# For license information, please see license.txt
"""
MachineConfig — physical decoration machine configuration.

One record per machine on the shop floor. Holds all connection
details needed by the machine driver layer to communicate.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class MachineConfig(Document):

    def validate(self):
        self._validate_default_uniqueness()
        self._validate_network_config()

    def _validate_default_uniqueness(self):
        """Only one machine can be the default per decoration method."""
        if not self.is_default:
            return
        existing = frappe.db.get_value(
            "Machine Config",
            {
                "decoration_method": self.decoration_method,
                "is_default": 1,
                "name": ["!=", self.name],
            },
            "name",
        )
        if existing:
            frappe.throw(
                _(
                    "{} is already the default {} machine. "
                    "Unset it first before making {} the default."
                ).format(existing, self.decoration_method, self.machine_name)
            )

    def _validate_network_config(self):
        # EpsonEdgePrint uses a hot folder (filesystem path — no network host needed).
        # PneumaticPress is non-networked (validates params + logs events only).
        # All other driver types require host to establish a network connection.
        file_based_drivers = ("EpsonEdgePrint", "PneumaticPress")

        if self.driver_type not in file_based_drivers and not self.host:
            frappe.throw(_("Host / IP Address is required"), frappe.ValidationError)

        if self.driver_type == "EpsonEdgePrint" and not self.epson_hot_folder_path:
            frappe.throw(
                _("Hot Folder Path is required for Epson Edge Print driver"),
                frappe.ValidationError,
            )

        if self.timeout_seconds and int(self.timeout_seconds) < 5:
            frappe.throw(_("Timeout must be at least 5 seconds"), frappe.ValidationError)

    def ping(self) -> dict:
        """
        Test connectivity to the machine.
        Updates last_ping_at and last_ping_status.
        Returns {"ok": bool, "latency_ms": float, "detail": str}
        """
        from alice_shop_floor.alice_shop_floor.machine_drivers.registry import (
            MachineDriverRegistry,
        )
        result = MachineDriverRegistry.get_driver(self).ping()
        frappe.db.set_value(
            "Machine Config",
            self.name,
            {
                "last_ping_at":     frappe.utils.now_datetime(),
                "last_ping_status": "Online" if result.get("ok") else "Offline",
            },
        )
        return result

    def get_status(self) -> dict:
        """
        Returns the machine's current operational status.
        Idle / Printing / Error / Offline
        """
        from alice_shop_floor.alice_shop_floor.machine_drivers.registry import (
            MachineDriverRegistry,
        )
        return MachineDriverRegistry.get_driver(self).get_status()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def get_default_machine(decoration_method: str) -> "MachineConfig | None":
    """Returns the default MachineConfig for a given decoration method."""
    name = frappe.db.get_value(
        "Machine Config",
        {"decoration_method": decoration_method, "is_default": 1, "is_active": 1},
        "name",
    )
    if name:
        return frappe.get_doc("Machine Config", name)
    # Fall back to any active machine for this method
    name = frappe.db.get_value(
        "Machine Config",
        {"decoration_method": decoration_method, "is_active": 1},
        "name",
    )
    return frappe.get_doc("Machine Config", name) if name else None


def list_active_machines(decoration_method: str = None) -> list:
    """Lists all active machines, optionally filtered by decoration method."""
    filters = {"is_active": 1}
    if decoration_method:
        filters["decoration_method"] = decoration_method
    return frappe.get_all(
        "Machine Config",
        filters=filters,
        fields=[
            "name", "machine_name", "decoration_method", "driver_type",
            "host", "is_default", "last_ping_status", "last_job_sent_at",
            "total_jobs_sent",
        ],
        order_by="decoration_method asc, machine_name asc",
    )
