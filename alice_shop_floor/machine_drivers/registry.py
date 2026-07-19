# Copyright (c) 2026, Athlettia LLC
"""
registry.py
-----------
ALICE Machine Driver Registry

Factory that reads a MachineConfig document and returns the correct
BaseMachineDriver subclass instance.

Usage:
    from .machine_drivers.registry import MachineDriverRegistry

    driver = MachineDriverRegistry.get_driver(machine_config_doc)
    result = driver.send_job(params)

Adding a new driver:
    1. Create machine_drivers/your_driver.py with a class that extends BaseMachineDriver
    2. Import it here
    3. Add an entry to _DRIVER_MAP — the key is the driver_type value from MachineConfig
    4. If the driver_type can cover multiple decoration methods (e.g. EpsonEdgePrint
       serves both DTF and DTG), add a decoration_method sub-dispatch in get_driver().
"""

from __future__ import annotations

import frappe
from .base_driver import BaseMachineDriver
from .epson_dtf import EpsonDTFDriver
from .epson_dtg import EpsonDTGDriver
from .melco_emb import MelcoEmbDriver
from .pneumatic_press import PneumaticPressDriver


# ---------------------------------------------------------------------------
# Hot-folder fallback driver (generic — no machine-specific REST API)
# ---------------------------------------------------------------------------

class HotFolderDriver(BaseMachineDriver):
    """
    Generic hot-folder driver for machines not supported by a dedicated REST driver.
    Copies the design file to a watched directory; operator manually runs the job.
    Suitable for: RIP software hot folders, legacy DTF/UV printers, etc.
    """

    DRIVER_TYPE = "HotFolder"

    def __init__(self, machine_config):
        super().__init__(machine_config)
        self._folder = self.cfg.get("hot_folder_path") or ""

    def ping(self) -> dict:
        import os
        ok = bool(self._folder) and os.path.isdir(self._folder)
        return {"ok": ok, "latency_ms": 0.0,
                "detail": "folder_accessible" if ok else "folder_not_found",
                "driver": self.DRIVER_TYPE}

    def get_status(self) -> dict:
        import os
        ok = bool(self._folder) and os.path.isdir(self._folder)
        return {"ok": ok, "state": "Idle" if ok else "Offline",
                "detail": {}, "driver": self.DRIVER_TYPE}

    def send_job(self, params: dict) -> dict:
        import os, json, shutil
        from .epson_dtf import _resolve_frappe_file

        job_card    = params.get("job_card", "")
        design_file = params.get("design_file", "")
        recipe      = params.get("recipe_params", {})

        if not self._folder or not os.path.isdir(self._folder):
            return {"ok": False, "error": "hot_folder_not_accessible", "driver": self.DRIVER_TYPE}

        local_path = _resolve_frappe_file(design_file)
        if not local_path or not os.path.exists(local_path):
            return {"ok": False, "error": "design_file_not_found", "driver": self.DRIVER_TYPE}

        try:
            ext       = os.path.splitext(local_path)[1]
            filename  = f"ZAZFIT-{job_card}{ext}"
            dest_path = os.path.join(self._folder, filename)
            shutil.copy2(local_path, dest_path)

            sidecar_path = os.path.join(self._folder, f"ZAZFIT-{job_card}.json")
            with open(sidecar_path, "w") as f:
                json.dump({"job_card": job_card, "recipe_params": recipe}, f)

            self._log_event("FilePushed", "Success", job_card=job_card,
                            request_payload={"dest": dest_path})
            self._bump_job_counter()
            return {
                "ok":            True,
                "method":        "hot_folder",
                "machine_job_id": f"HF-{job_card}",
                "dest_file":     dest_path,
                "driver":        self.DRIVER_TYPE,
            }
        except Exception as e:
            frappe.logger().error(f"[HotFolderDriver] Error: {e}")
            return {"ok": False, "error": str(e), "driver": self.DRIVER_TYPE}

    def get_job_status(self, machine_job_id: str) -> dict:
        # Hot-folder jobs have no programmatic status
        return {"ok": True, "state": "Queued",
                "detail": {"note": "Hot folder — status tracked manually"},
                "driver": self.DRIVER_TYPE}

    def cancel_job(self, machine_job_id: str) -> dict:
        # Cannot cancel via API; operator must remove file from folder
        return {"ok": False, "error": "cancel_not_supported_for_hot_folder",
                "driver": self.DRIVER_TYPE}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class MachineDriverRegistry:
    """
    Factory: reads MachineConfig.driver_type (and optionally
    MachineConfig.decoration_method) and returns the right driver instance.

    EpsonEdgePrint is shared between DTF and DTG. The registry uses
    decoration_method to dispatch to the correct subclass.
    """

    # driver_type → driver class (or dict keyed by decoration_method)
    _DRIVER_MAP: dict = {
        # Epson SureColor G6070 DTF (35") + SureColor DTG
        # Both use Epson Edge Print 2; decoration_method selects the subclass
        "EpsonEdgePrint": {
            "DTF":       EpsonDTFDriver,
            "DTG":       EpsonDTGDriver,
            "_default":  EpsonDTFDriver,
        },
        # Melco Summit (15-needle single head, DST/JEF via FTP + Melco OS REST)
        "MelcoOS":         MelcoEmbDriver,
        # Pneumatic heat press (DTF transfer step — manual, params-validated)
        "PneumaticPress":  PneumaticPressDriver,
        # Generic hot folder — file drop for any unsupported RIP/machine
        "HotFolder":       HotFolderDriver,
        # Future drivers — add here as they are built:
        # "Kornit":        KornitDriver,
        # "Tajima":        TajimaDriver,
        # "TCPSocket":     TCPSocketDriver,
        # "Custom":        CustomDriver,
    }

    @classmethod
    def get_driver(cls, machine_config) -> BaseMachineDriver:
        """
        Returns an instantiated driver for the given MachineConfig document.

        Raises ValueError if the driver_type is not registered.
        """
        driver_type       = (machine_config.driver_type or "").strip()
        decoration_method = (machine_config.decoration_method or "").strip()

        if not driver_type:
            raise ValueError(
                f"MachineConfig '{machine_config.name}' has no driver_type set."
            )

        entry = cls._DRIVER_MAP.get(driver_type)
        if entry is None:
            raise ValueError(
                f"No driver registered for driver_type='{driver_type}'. "
                f"Registered types: {list(cls._DRIVER_MAP.keys())}"
            )

        # If entry is a dict, select by decoration_method
        if isinstance(entry, dict):
            driver_class = entry.get(decoration_method) or entry.get("_default")
            if driver_class is None:
                raise ValueError(
                    f"driver_type='{driver_type}' does not support "
                    f"decoration_method='{decoration_method}'"
                )
        else:
            driver_class = entry

        return driver_class(machine_config)

    @classmethod
    def get_driver_by_name(cls, machine_config_name: str) -> BaseMachineDriver:
        """
        Convenience: loads a MachineConfig by name and returns its driver.
        """
        mc = frappe.get_doc("Machine Config", machine_config_name)
        if not mc.is_active:
            raise RuntimeError(
                f"MachineConfig '{machine_config_name}' is inactive."
            )
        return cls.get_driver(mc)

    @classmethod
    def get_default_driver(cls, decoration_method: str) -> BaseMachineDriver:
        """
        Returns the driver for the default active machine for the given
        decoration method.

        Raises RuntimeError if no default machine is configured.
        """
        mc_name = frappe.db.get_value(
            "Machine Config",
            {"decoration_method": decoration_method, "is_default": 1, "is_active": 1},
            "name",
        )
        if not mc_name:
            raise RuntimeError(
                f"No default active machine configured for decoration_method='{decoration_method}'. "
                f"Please set a default in Machine Config."
            )
        return cls.get_driver_by_name(mc_name)

    @classmethod
    def registered_types(cls) -> list[str]:
        """Returns all registered driver_type keys."""
        return list(cls._DRIVER_MAP.keys())
