# Copyright (c) 2026, Athlettia LLC
"""
epson_dtg.py
------------
ALICE Machine Driver — Epson SureColor DTG (SC-F3070 / F2270)

Primary machine: Epson SureColor F2270 (sold through Melco as DTG unit)
RIP software:    Epson Edge Print 2 (Windows desktop RIP)

IMPORTANT — Epson Edge Print 2 has no local REST API.
Integration is via hot folder, identical to EpsonDTFDriver.
See epson_dtf.py for full hot folder documentation.

DTG-specific params delivered to Job Card by this driver:
  platen_size        (XS/S/M/L/XL — physical platen installed on F2270)
  pretreat_required  (bool — dark garments need pretreatment before print)
  ink_profile        ("dark_garment" | "light_garment")
  cure_temp_f        (default 320°F — tunnel dryer or heat press cure)
  cure_time_sec      (default 90 sec)
  underbase_passes   (white ink passes for dark garments, default 0)
  resolution_dpi     (600 or 1200)
  color_mode         ("CMYK+W" for dark, "CMYK" for light)
"""

from __future__ import annotations

import json
import os
import shutil
import time
import frappe
from .base_driver import BaseMachineDriver
from .epson_dtf import _safe_json, _resolve_frappe_file


class EpsonDTGDriver(BaseMachineDriver):
    """
    Driver for Epson SureColor DTG machines (F2270 / F3070).

    Integration: hot folder only (Epson Edge Print 2 has no REST API).
    MachineConfig field used: epson_hot_folder_path.

    Shares the same hot folder status lifecycle as EpsonDTFDriver:
      HF-{job_card} file present in folder → Queued
      HF-{job_card} file absent from folder → Complete
    """

    DRIVER_TYPE = "EpsonEdgePrint"

    DTG_DEFAULTS = {
        "platen_size":       "L",
        "pretreat_required": False,
        "ink_profile":       "light_garment",
        "cure_temp_f":       320.0,
        "cure_time_sec":     90,
        "underbase_passes":  0,
        "resolution_dpi":    1200,
        "color_mode":        "CMYK+W",
    }

    # Dark garment colors that require pretreatment
    _DARK_COLORS = {
        "black", "navy", "dark green", "dark grey", "dark gray",
        "charcoal", "maroon", "dark red", "forest green",
    }

    def __init__(self, machine_config):
        super().__init__(machine_config)
        self._hot_folder = (machine_config.get("epson_hot_folder_path") or "").strip()
        self._printer_id = machine_config.get("epson_printer_id") or ""

    # ------------------------------------------------------------------
    # BaseMachineDriver interface
    # ------------------------------------------------------------------

    def ping(self) -> dict:
        """Ping = verify hot folder is accessible and writable."""
        if not self._hot_folder:
            self._log_event(
                "Ping", "Failure",
                error_message="epson_hot_folder_path is not configured on MachineConfig",
            )
            return {
                "ok": False, "latency_ms": 0,
                "detail": "hot_folder_not_configured", "driver": self.DRIVER_TYPE,
            }

        t0 = time.monotonic()
        accessible = os.path.isdir(self._hot_folder)
        writable   = False
        if accessible:
            test = os.path.join(self._hot_folder, ".alice_ping")
            try:
                with open(test, "w") as f:
                    f.write("ping")
                os.remove(test)
                writable = True
            except OSError:
                pass
        elapsed_ms = (time.monotonic() - t0) * 1000
        ok = accessible and writable

        self._log_event(
            "Ping", "Success" if ok else "Failure",
            response_time_ms=elapsed_ms,
            error_message="" if ok else (
                "Hot folder directory not found" if not accessible
                else "Hot folder not writable"
            ),
        )
        return {
            "ok":         ok,
            "latency_ms": round(elapsed_ms, 2),
            "detail":     "hot_folder_ok" if ok else (
                "hot_folder_missing" if not accessible else "hot_folder_not_writable"
            ),
            "driver": self.DRIVER_TYPE,
        }

    def get_status(self) -> dict:
        """Status = hot folder accessibility + pending job count."""
        if not self._hot_folder:
            return {"ok": False, "state": "Offline",
                    "detail": {"error": "hot_folder_not_configured"}}
        if not os.path.isdir(self._hot_folder):
            return {"ok": False, "state": "Offline",
                    "detail": {"error": "hot_folder_missing", "path": self._hot_folder}}
        try:
            pending = [f for f in os.listdir(self._hot_folder)
                       if f.startswith("ZAZFIT-") and not f.endswith(".json")]
            return {
                "ok":    True,
                "state": "Printing" if pending else "Idle",
                "detail": {"pending_jobs": len(pending), "hot_folder": self._hot_folder},
                "driver": self.DRIVER_TYPE,
            }
        except OSError as e:
            return {"ok": False, "state": "Error", "detail": {"error": str(e)}}

    def send_job(self, params: dict) -> dict:
        """
        Submits a DTG print job via hot folder.
        Auto-applies pretreat and white underbase for dark garments.
        """
        job_card    = params.get("job_card", "")
        design_file = params.get("design_file", "")
        recipe      = params.get("recipe_params", {})
        placement   = params.get("design_placement", "Full Front")
        garment_color = params.get("garment_color", "").lower()

        dtg_params = {**self.DTG_DEFAULTS, **recipe}

        # Auto-set dark garment profile if not overridden by recipe
        if "ink_profile" not in recipe:
            if any(c in garment_color for c in self._DARK_COLORS):
                dtg_params["ink_profile"]       = "dark_garment"
                dtg_params["pretreat_required"] = True
                dtg_params["underbase_passes"]  = max(
                    dtg_params.get("underbase_passes", 0), 2
                )

        if not self._hot_folder:
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="no_hot_folder",
                            error_message="epson_hot_folder_path not configured")
            return {"ok": False, "error": "hot_folder_not_configured", "driver": self.DRIVER_TYPE}

        if not os.path.isdir(self._hot_folder):
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="hot_folder_missing",
                            error_message=f"Hot folder not found: {self._hot_folder}")
            return {"ok": False, "error": "hot_folder_directory_missing",
                    "detail": self._hot_folder, "driver": self.DRIVER_TYPE}

        local_path = _resolve_frappe_file(design_file)
        if not local_path or not os.path.exists(local_path):
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="file_not_found",
                            error_message=f"Design file not accessible: {design_file}")
            return {"ok": False, "error": "design_file_not_found",
                    "detail": design_file, "driver": self.DRIVER_TYPE}

        ext      = os.path.splitext(local_path)[1] or ".png"
        filename = f"ZAZFIT-{job_card}{ext}"
        dest     = os.path.join(self._hot_folder, filename)
        sidecar  = os.path.join(self._hot_folder, f"ZAZFIT-{job_card}.json")

        try:
            shutil.copy2(local_path, dest)
            with open(sidecar, "w") as f:
                json.dump({
                    "job_card":   job_card,
                    "placement":  placement,
                    "dtg_params": dtg_params,
                }, f)

            machine_job_id = f"HF-{job_card}"
            self._log_event(
                "JobSent", "Success",
                job_card=job_card,
                machine_job_id=machine_job_id,
                request_payload={
                    "hot_folder":  self._hot_folder,
                    "dest_file":   dest,
                    "dtg_params":  dtg_params,
                },
            )
            self._bump_job_counter()
            return {
                "ok":             True,
                "machine_job_id": machine_job_id,
                "method":         "hot_folder",
                "dest_file":      dest,
                "dtg_params":     dtg_params,
                "driver":         self.DRIVER_TYPE,
            }
        except Exception as e:
            frappe.logger().error(f"[EpsonDTGDriver] Hot folder write error: {e}")
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="hot_folder_write_error", error_message=str(e))
            return {"ok": False, "error": "hot_folder_write_error",
                    "detail": str(e), "driver": self.DRIVER_TYPE}

    def get_job_status(self, machine_job_id: str) -> dict:
        """
        Poll status via hot folder file presence.
        File in folder → Queued. File gone → Complete (Edge Print consumed it).
        """
        job_card = (
            machine_job_id[3:]
            if machine_job_id.startswith("HF-")
            else machine_job_id
        )

        if not self._hot_folder or not os.path.isdir(self._hot_folder):
            return {"ok": True, "state": "Complete",
                    "detail": {"note": "hot_folder_inaccessible_assumed_complete"},
                    "driver": self.DRIVER_TYPE}

        try:
            job_files = [f for f in os.listdir(self._hot_folder)
                         if f.startswith(f"ZAZFIT-{job_card}") and not f.endswith(".json")]
            state = "Queued" if job_files else "Complete"
            self._log_event("StatusPoll", "Success", machine_job_id=machine_job_id,
                            response_payload={"in_hot_folder": bool(job_files),
                                              "files": job_files})
            return {
                "ok":    True,
                "state": state,
                "detail": {"in_hot_folder": bool(job_files), "files": job_files},
                "driver": self.DRIVER_TYPE,
            }
        except OSError as e:
            return {"ok": False, "state": "Unknown", "detail": {"error": str(e)}}

    def cancel_job(self, machine_job_id: str) -> dict:
        """Cancel by removing files from hot folder before Edge Print picks them up."""
        job_card = (
            machine_job_id[3:]
            if machine_job_id.startswith("HF-")
            else machine_job_id
        )

        if not self._hot_folder:
            return {"ok": False, "error": "hot_folder_not_configured", "driver": self.DRIVER_TYPE}

        removed = 0
        errors  = []
        try:
            for fname in os.listdir(self._hot_folder):
                if fname.startswith(f"ZAZFIT-{job_card}"):
                    try:
                        os.remove(os.path.join(self._hot_folder, fname))
                        removed += 1
                    except OSError as e:
                        errors.append(str(e))
        except OSError as e:
            errors.append(str(e))

        ok = removed > 0
        self._log_event("CancelJob", "Success" if ok else "Failure",
                        machine_job_id=machine_job_id,
                        response_payload={"files_removed": removed, "errors": errors})
        return {
            "ok":             ok,
            "machine_job_id": machine_job_id,
            "method":         "hot_folder_delete",
            "files_removed":  removed,
            "driver":         self.DRIVER_TYPE,
        }
