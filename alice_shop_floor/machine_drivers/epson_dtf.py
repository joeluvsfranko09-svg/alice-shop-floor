# Copyright (c) 2026, Athlettia LLC
"""
epson_dtf.py
------------
ALICE Machine Driver — Epson SureColor G6070 DTF (35" wide format)

Primary machine: Epson SureColor SC-G6070 (35" printable width)
RIP software:    Epson Edge Print 2 (Windows desktop RIP)

IMPORTANT — Epson Edge Print 2 has no local REST API.
Integration is via hot folder only:
  1. ALICE copies the design file into a configured hot folder directory.
  2. Edge Print monitors the hot folder and, on detecting a new file,
     applies the pre-configured Quick Set and sends the job to the G6070.
  3. Edge Print moves the file out of the hot folder once queued.
  4. ALICE polls job completion by checking whether the design file is
     still present in the hot folder (present = Queued, absent = Complete).

Hot folder setup (one-time, in Epson Edge Print UI):
  • File → Hot Folder → Register → pick directory → assign Quick Set → Auto Print ON
  • Quick Set should include: DTF film media, 1200 DPI, CMYK+W, auto-cut off

DTF 3-step workflow at ZAZFIT:
  Step 1 — Epson G6070 prints design onto DTF film (this driver)
  Step 2 — Film cures in the dryer (automatic, no driver)
  Step 3 — Operator transfers film to garment via pneumatic press
            → tracked by PneumaticPressDriver (press_params on Job Card)

DTF-specific press params attached to Job Card by this driver:
  press_temp_f       (default 385°F — standard DTF transfer temp)
  dwell_time_sec     (default 12 sec)
  pressure           (default Medium — pneumatic auto-adjusts to ~40 PSI)
  peel_type          (default Hot Peel)
  film_width_inches  (35" — G6070 max usable print width)
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import frappe
from .base_driver import BaseMachineDriver


class EpsonDTFDriver(BaseMachineDriver):
    """
    Driver for Epson SureColor G6070 DTF printer (35" wide format).

    Integration: hot folder only (Epson Edge Print 2 has no REST API).
    MachineConfig field used: epson_hot_folder_path (path to watched folder).

    Status lifecycle:
      send_job()        → copies file to hot folder → returns HF-{job_card}
      get_job_status()  → file present = Queued, file absent = Complete
      cancel_job()      → deletes files from hot folder before Edge Print picks up

    This driver handles the PRINT step only.  The PRESS step is handled by
    PneumaticPressDriver via start_press_job() in decoration_engine.py.
    """

    DRIVER_TYPE = "EpsonEdgePrint"

    # Default DTF parameters for the G6070 (overridden by ProductionRecipe)
    DTF_DEFAULTS = {
        "press_temp_f":      385.0,   # Transfer temp — standard DTF hot peel
        "dwell_time_sec":    12,      # Press dwell for pneumatic press
        "pressure":          "Medium", # Pneumatic press auto-adjusts; Medium ≈ 40 PSI
        "peel_type":         "Hot",   # Hot peel on G6070 film
        "film_width_inches": 35,      # G6070 max usable print width
        "color_mode":        "CMYK+W",
        "white_ink":         True,    # G6070 has white ink channel for dark garments
        "resolution_dpi":    1200,
    }

    def __init__(self, machine_config):
        super().__init__(machine_config)
        self._hot_folder  = (machine_config.get("epson_hot_folder_path") or "").strip()
        self._printer_id  = machine_config.get("epson_printer_id") or ""
        # _base_url kept in base class but not used — no HTTP API on Edge Print

    # ------------------------------------------------------------------
    # BaseMachineDriver interface implementation
    # ------------------------------------------------------------------

    def ping(self) -> dict:
        """
        Ping = verify hot folder directory is accessible and writable.
        Called every 5 minutes by tasks.ping_all_machines().
        """
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
            test_path = os.path.join(self._hot_folder, ".alice_ping")
            try:
                with open(test_path, "w") as f:
                    f.write("ping")
                os.remove(test_path)
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
                else "Hot folder exists but is not writable"
            ),
        )
        return {
            "ok":        ok,
            "latency_ms": round(elapsed_ms, 2),
            "detail":    "hot_folder_ok" if ok else (
                "hot_folder_missing" if not accessible else "hot_folder_not_writable"
            ),
            "driver":    self.DRIVER_TYPE,
        }

    def get_status(self) -> dict:
        """
        Status = hot folder accessibility + pending job count.
        Pending = ZAZFIT-*.json sidecar files still present (one per queued job).
        """
        if not self._hot_folder:
            return {"ok": False, "state": "Offline",
                    "detail": {"error": "hot_folder_not_configured"}}

        if not os.path.isdir(self._hot_folder):
            return {"ok": False, "state": "Offline",
                    "detail": {"error": "hot_folder_directory_missing",
                               "path": self._hot_folder}}

        try:
            all_files = os.listdir(self._hot_folder)
            # Count ZAZFIT design files (non-.json) waiting to be consumed
            pending = [f for f in all_files
                       if f.startswith("ZAZFIT-") and not f.endswith(".json")]
            state = "Printing" if pending else "Idle"
            return {
                "ok":    True,
                "state": state,
                "detail": {
                    "pending_jobs": len(pending),
                    "hot_folder":   self._hot_folder,
                },
                "driver": self.DRIVER_TYPE,
            }
        except OSError as e:
            return {"ok": False, "state": "Error",
                    "detail": {"error": str(e)}}

    def send_job(self, params: dict) -> dict:
        """
        Submits a DTF print job by copying the design file into the hot folder.

        Epson Edge Print 2 monitors the hot folder and auto-prints any new file
        using the pre-configured Quick Set for that folder.

        params expected keys:
          job_card, design_file, recipe_params (dict with DTF press params),
          design_placement, customer_name
        """
        job_card    = params.get("job_card", "")
        design_file = params.get("design_file", "")
        recipe      = params.get("recipe_params", {})
        placement   = params.get("design_placement", "Full Front")

        # Merge recipe params with DTF defaults
        press_params = {**self.DTF_DEFAULTS, **recipe}

        if not self._hot_folder:
            self._log_event(
                "JobSent", "Failure", job_card=job_card,
                error_code="no_hot_folder",
                error_message="epson_hot_folder_path not configured",
            )
            return {
                "ok":    False,
                "error": "hot_folder_not_configured",
                "driver": self.DRIVER_TYPE,
            }

        if not os.path.isdir(self._hot_folder):
            self._log_event(
                "JobSent", "Failure", job_card=job_card,
                error_code="hot_folder_missing",
                error_message=f"Hot folder directory not found: {self._hot_folder}",
            )
            return {
                "ok":    False,
                "error": "hot_folder_directory_missing",
                "detail": self._hot_folder,
                "driver": self.DRIVER_TYPE,
            }

        # Resolve the design file URL to a local filesystem path
        local_path = _resolve_frappe_file(design_file)
        if not local_path or not os.path.exists(local_path):
            self._log_event(
                "JobSent", "Failure", job_card=job_card,
                error_code="file_not_found",
                error_message=f"Design file not accessible: {design_file}",
            )
            return {
                "ok":    False,
                "error": "design_file_not_found",
                "detail": design_file,
                "driver": self.DRIVER_TYPE,
            }

        ext      = os.path.splitext(local_path)[1] or ".png"
        filename = f"ZAZFIT-{job_card}{ext}"
        dest     = os.path.join(self._hot_folder, filename)
        sidecar  = os.path.join(self._hot_folder, f"ZAZFIT-{job_card}.json")

        try:
            shutil.copy2(local_path, dest)

            # Write sidecar with press params — not read by Edge Print, used
            # by ALICE press station and for audit trail
            with open(sidecar, "w") as f:
                json.dump({
                    "job_card":     job_card,
                    "placement":    placement,
                    "press_params": press_params,
                    "film_width":   press_params["film_width_inches"],
                    "color_mode":   press_params.get("color_mode", "CMYK+W"),
                    "white_ink":    press_params.get("white_ink", True),
                    "resolution":   press_params.get("resolution_dpi", 1200),
                }, f)

            machine_job_id = f"HF-{job_card}"
            self._log_event(
                "JobSent", "Success",
                job_card=job_card,
                machine_job_id=machine_job_id,
                request_payload={
                    "hot_folder":   self._hot_folder,
                    "dest_file":    dest,
                    "press_params": press_params,
                },
            )
            self._bump_job_counter()
            return {
                "ok":             True,
                "machine_job_id": machine_job_id,
                "method":         "hot_folder",
                "dest_file":      dest,
                "press_params":   press_params,
                "driver":         self.DRIVER_TYPE,
            }

        except Exception as e:
            frappe.logger().error(f"[EpsonDTFDriver] Hot folder write error: {e}")
            self._log_event(
                "JobSent", "Failure", job_card=job_card,
                error_code="hot_folder_write_error",
                error_message=str(e),
            )
            return {
                "ok":    False,
                "error": "hot_folder_write_error",
                "detail": str(e),
                "driver": self.DRIVER_TYPE,
            }

    def get_job_status(self, machine_job_id: str) -> dict:
        """
        Poll job status by checking hot folder file presence.

        Edge Print consumes (moves/deletes) the design file from the hot folder
        once it queues the job internally.

          Design file present in hot folder → state = "Queued"  (waiting for Edge Print)
          Design file absent from hot folder → state = "Complete" (Edge Print consumed it)

        Note: "Complete" here means Edge Print has taken ownership and is printing or
        has printed. ALICE then waits for the operator to confirm "Film Ready".
        """
        job_card = (
            machine_job_id[3:]   # strip "HF-"
            if machine_job_id.startswith("HF-")
            else machine_job_id
        )

        if not self._hot_folder or not os.path.isdir(self._hot_folder):
            # Can't check — assume complete (Edge Print has it)
            return {
                "ok":    True,
                "state": "Complete",
                "detail": {"note": "hot_folder_inaccessible_assumed_complete"},
                "driver": self.DRIVER_TYPE,
            }

        try:
            all_files   = os.listdir(self._hot_folder)
            job_files   = [f for f in all_files
                           if f.startswith(f"ZAZFIT-{job_card}") and not f.endswith(".json")]
            in_folder   = len(job_files) > 0

            state  = "Queued" if in_folder else "Complete"
            self._log_event(
                "StatusPoll", "Success",
                machine_job_id=machine_job_id,
                response_payload={"in_hot_folder": in_folder, "files_found": job_files},
            )
            return {
                "ok":    True,
                "state": state,
                "detail": {
                    "in_hot_folder": in_folder,
                    "files":         job_files,
                },
                "driver": self.DRIVER_TYPE,
            }
        except OSError as e:
            return {
                "ok":    False,
                "state": "Unknown",
                "detail": {"error": str(e)},
            }

    def cancel_job(self, machine_job_id: str) -> dict:
        """
        Cancel by removing the design file from the hot folder before Edge Print
        picks it up. Once Edge Print has consumed the file, cancellation is no
        longer possible through this driver.
        """
        job_card = (
            machine_job_id[3:]
            if machine_job_id.startswith("HF-")
            else machine_job_id
        )

        if not self._hot_folder:
            return {
                "ok":    False,
                "error": "hot_folder_not_configured",
                "driver": self.DRIVER_TYPE,
            }

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
        self._log_event(
            "CancelJob", "Success" if ok else "Failure",
            machine_job_id=machine_job_id,
            response_payload={"files_removed": removed, "errors": errors},
        )
        return {
            "ok":             ok,
            "machine_job_id": machine_job_id,
            "method":         "hot_folder_delete",
            "files_removed":  removed,
            "driver":         self.DRIVER_TYPE,
        }


# ---------------------------------------------------------------------------
# Shared utilities (imported by epson_dtg.py and melco_emb.py)
# ---------------------------------------------------------------------------

def _safe_json(resp) -> dict:
    """Safely parse JSON from an HTTP response (used by drivers that DO have REST)."""
    try:
        return resp.json()
    except Exception:
        return {}


def _resolve_frappe_file(url: str) -> str:
    """
    Converts a Frappe /files/ or /private/files/ URL to an absolute local path.
    Returns the URL unchanged if it's already a filesystem path or unrecognised.
    """
    if not url:
        return ""
    if url.startswith("/files/"):
        return frappe.get_site_path("public", url.lstrip("/"))
    if url.startswith("/private/files/"):
        return frappe.get_site_path(url.lstrip("/"))
    return url  # Already an absolute path or external URL
