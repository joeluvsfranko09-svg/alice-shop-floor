# Copyright (c) 2026, Athlettia LLC
"""
melco_emb.py
------------
ALICE Machine Driver — Melco Summit Embroidery (single-head, 15-needle)

Primary machine: Melco Summit (15-needle, single head)
Management software: Melco SUMMIT Manager (Windows desktop app)

IMPORTANT — Melco SUMMIT Manager has no public REST API.
Integration is via FTP file push to the machine's job queue directory:
  1. ALICE pushes the .DST embroidery file + sidecar .json to the
     machine's FTP directory (SUMMIT Manager monitors this directory).
  2. SUMMIT Manager picks up the file and queues it on the machine.
  3. ALICE polls status by checking whether the job file is still
     present on the FTP server (present = Queued, absent = Complete).

One-time FTP setup on SUMMIT Manager:
  • Enable FTP server in SUMMIT Manager → Network Settings
  • Set FTP port (default 21), user/pass as configured in MachineConfig
  • Configure the job queue directory (melco_ftp_dir on MachineConfig)

Melco Summit key specs:
  - 15 needles, single head, auto color change
  - Max embroidery field: 17.7" × 11.8" (450 × 300 mm)
  - Supported formats: .DST (primary), .JEF (secondary)
  - Speed: up to 1,000 SPM

EMB-specific params delivered to Job Card by this driver:
  thread_colors      (list of Madeira/Robison-Anton thread codes)
  stitch_count       (total stitches in design)
  hoop_size          (e.g. "12x8" — Summit max 17.7x11.8")
  underlay_type      (ZigZag / Edge Walk / Center Walk / None)
  density            (Pull Comp % — 100 = no adjustment)
  dst_file           (resolved local path to .DST file)
"""

from __future__ import annotations

import io
import json
import os
import ftplib
import time
import frappe
from .base_driver import BaseMachineDriver
from .epson_dtf import _resolve_frappe_file


class MelcoEmbDriver(BaseMachineDriver):
    """
    Driver for Melco Summit embroidery machine (15-needle).

    Integration: FTP file push only (SUMMIT Manager has no REST API).

    Status lifecycle:
      send_job()       → FTP push .DST + sidecar .json → returns FTP-{job_card}
      get_job_status() → FTP NLST check: file present = Queued, absent = Complete
      cancel_job()     → FTP delete before SUMMIT Manager picks it up

    DST Gate: DigitizingQueue record must be in Approved/Released status
    before send_job() is permitted (enforced in this driver).
    """

    DRIVER_TYPE = "MelcoOS"

    # Default EMB parameters for Melco Summit (overridden by ProductionRecipe)
    EMB_DEFAULTS = {
        "hoop_size":     "12x8",     # Summit standard hoop (within 17.7x11.8" max)
        "underlay_type": "ZigZag",
        "density":       100,        # Pull Comp % — 100 = no adjustment
        "speed_spm":     850,        # SPM (Summit max = 1,000)
        "trims_enabled": True,
        "color_changes": True,       # Summit 15 needles — auto color change
        "repeat_count":  1,
        "format":        "DST",      # Melco Summit primary format
        "needles":       15,
    }

    # Melco Summit physical limits
    MAX_HOOP_WIDTH_IN  = 17.7
    MAX_HOOP_HEIGHT_IN = 11.8
    MAX_SPEED_SPM      = 1000

    def __init__(self, machine_config):
        super().__init__(machine_config)
        self._ftp_dir  = (machine_config.get("melco_ftp_dir") or "").strip()
        self._formats  = [
            f.strip().upper()
            for f in (machine_config.get("melco_supported_formats") or "DST").split(",")
        ]
        # FTP credentials resolved at connection time
        self._ftp_user = machine_config.get("username") or "anonymous"
        self._ftp_pass = (
            machine_config.get_password("password")
            if machine_config.get("auth_type") == "Basic"
            else ""
        )
        self._ftp_port = int(self.port) if self.port else 21

    # ------------------------------------------------------------------
    # BaseMachineDriver interface
    # ------------------------------------------------------------------

    def ping(self) -> dict:
        """Ping = FTP connect + login test."""
        if not self.host:
            return {
                "ok": False, "latency_ms": 0,
                "detail": "host_not_configured", "driver": self.DRIVER_TYPE,
            }

        t0 = time.monotonic()
        try:
            with ftplib.FTP() as ftp:
                ftp.connect(self.host, self._ftp_port, timeout=10)
                ftp.login(self._ftp_user, self._ftp_pass)
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._log_event("Ping", "Success", response_time_ms=elapsed_ms)
            return {
                "ok":         True,
                "latency_ms": round(elapsed_ms, 2),
                "detail":     "ftp_ok",
                "driver":     self.DRIVER_TYPE,
            }
        except ftplib.all_errors as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._log_event("Ping", "Failure", response_time_ms=elapsed_ms,
                            error_message=str(e))
            return {
                "ok":         False,
                "latency_ms": round(elapsed_ms, 2),
                "detail":     str(e),
                "driver":     self.DRIVER_TYPE,
            }

    def get_status(self) -> dict:
        """Status = FTP connect + list job queue directory."""
        if not self.host:
            return {"ok": False, "state": "Offline",
                    "detail": {"error": "host_not_configured"}}
        try:
            with ftplib.FTP() as ftp:
                ftp.connect(self.host, self._ftp_port, timeout=self.timeout)
                ftp.login(self._ftp_user, self._ftp_pass)
                if self._ftp_dir:
                    ftp.cwd(self._ftp_dir)
                files   = ftp.nlst()
                pending = [f for f in files
                           if f.startswith("ZAZFIT-") and not f.endswith(".json")]
            state = "Printing" if pending else "Idle"
            return {
                "ok":    True,
                "state": state,
                "detail": {"pending_jobs": len(pending), "ftp_dir": self._ftp_dir},
                "driver": self.DRIVER_TYPE,
            }
        except ftplib.all_errors as e:
            return {"ok": False, "state": "Offline",
                    "detail": {"error": str(e)}}

    def send_job(self, params: dict) -> dict:
        """
        Submits an embroidery job via FTP push to SUMMIT Manager's queue directory.

        Enforces the DST gate: DigitizingQueue record must be Approved/Released.

        params expected keys:
          job_card, design_file, recipe_params (dict), design_placement,
          stitch_count, thread_colors (list), dst_gate_approved (bool)
        """
        job_card    = params.get("job_card", "")
        design_file = params.get("design_file", "")
        recipe      = params.get("recipe_params", {})

        # ── DST gate — DigitizingQueue must be approved ──────────────────
        if not params.get("dst_gate_approved", False):
            frappe.logger().warning(
                f"[MelcoEmbDriver] Job {job_card} blocked — DST gate not approved"
            )
            return {
                "ok":    False,
                "error": "dst_gate_not_approved",
                "detail": "DigitizingQueue record must be Approved or Released before embroidery",
                "driver": self.DRIVER_TYPE,
            }

        emb_params = {**self.EMB_DEFAULTS, **recipe}

        # Enforce Melco Summit hardware speed limit
        if emb_params.get("speed_spm", 0) > self.MAX_SPEED_SPM:
            frappe.logger().warning(
                f"[MelcoEmbDriver] Capping speed {emb_params['speed_spm']} → "
                f"{self.MAX_SPEED_SPM} SPM (Summit max)"
            )
            emb_params["speed_spm"] = self.MAX_SPEED_SPM

        # Resolve design file to a local filesystem path
        local_path = _resolve_frappe_file(design_file)
        if not local_path or not os.path.exists(local_path):
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="file_not_found",
                            error_message=f"Design file not accessible: {design_file}")
            return {"ok": False, "error": "design_file_not_found",
                    "driver": self.DRIVER_TYPE}

        # Validate file format
        ext = os.path.splitext(local_path)[1].upper().lstrip(".")
        if ext not in self._formats:
            return {
                "ok":    False,
                "error": "unsupported_format",
                "detail": f"Machine accepts {self._formats}, got .{ext}",
                "driver": self.DRIVER_TYPE,
            }

        if not self.host:
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="no_host", error_message="host not configured")
            return {"ok": False, "error": "ftp_host_not_configured", "driver": self.DRIVER_TYPE}

        ftp_result = self._send_via_ftp(job_card, local_path, emb_params, params)
        if ftp_result.get("ok"):
            self._log_event(
                "FilePushed", "Success",
                job_card=job_card,
                machine_job_id=ftp_result.get("machine_job_id"),
                request_payload={"ftp_dir": self._ftp_dir, "file": design_file},
                response_payload=ftp_result,
            )
            self._bump_job_counter()

        return ftp_result

    def get_job_status(self, machine_job_id: str) -> dict:
        """
        Poll status via FTP NLST.
        Job file on server → Queued. File absent → Complete (SUMMIT Manager consumed it).
        """
        if not machine_job_id.startswith("FTP-"):
            return {"ok": False, "state": "Unknown",
                    "detail": {"error": "non_ftp_job_id"}}

        job_card = machine_job_id[4:]  # strip "FTP-"

        if not self.host:
            return {"ok": True, "state": "Complete",
                    "detail": {"note": "no_host_assumed_complete"}, "driver": self.DRIVER_TYPE}

        try:
            with ftplib.FTP() as ftp:
                ftp.connect(self.host, self._ftp_port, timeout=self.timeout)
                ftp.login(self._ftp_user, self._ftp_pass)
                if self._ftp_dir:
                    ftp.cwd(self._ftp_dir)
                files     = ftp.nlst()
                job_files = [f for f in files
                             if f.startswith(f"ZAZFIT-{job_card}") and not f.endswith(".json")]

            state = "Queued" if job_files else "Complete"
            self._log_event(
                "StatusPoll", "Success",
                machine_job_id=machine_job_id,
                response_payload={"on_ftp": bool(job_files), "files": job_files},
            )
            return {
                "ok":    True,
                "state": state,
                "detail": {"on_ftp": bool(job_files), "files": job_files},
                "driver": self.DRIVER_TYPE,
            }
        except ftplib.all_errors as e:
            frappe.logger().warning(f"[MelcoEmbDriver] FTP status check failed: {e}")
            # Connection failure — conservatively assume still queued
            return {
                "ok":    True,
                "state": "Queued",
                "detail": {"note": "ftp_check_failed", "error": str(e)},
                "driver": self.DRIVER_TYPE,
            }

    def cancel_job(self, machine_job_id: str) -> dict:
        """Cancel by deleting the FTP file before SUMMIT Manager picks it up."""
        if not machine_job_id.startswith("FTP-"):
            return {"ok": False, "error": "non_ftp_job_id", "driver": self.DRIVER_TYPE}

        job_card = machine_job_id[4:]
        return self._remove_ftp_job(job_card, machine_job_id)

    # ------------------------------------------------------------------
    # FTP helpers
    # ------------------------------------------------------------------

    def _send_via_ftp(self, job_card: str, local_path: str,
                       emb_params: dict, original_params: dict) -> dict:
        """
        Pushes DST file + sidecar JSON to FTP queue directory.
        Returns driver result dict.
        """
        try:
            ext      = os.path.splitext(local_path)[1] or ".DST"
            filename = f"ZAZFIT-{job_card}{ext}"
            sidecar  = f"ZAZFIT-{job_card}.json"

            sidecar_data = json.dumps({
                "job_card":      job_card,
                "emb_params":    emb_params,
                "thread_colors": original_params.get("thread_colors", []),
                "stitch_count":  original_params.get("stitch_count", 0),
                "placement":     original_params.get("design_placement", ""),
            }).encode()

            with ftplib.FTP() as ftp:
                ftp.connect(self.host, self._ftp_port, timeout=self.timeout)
                ftp.login(self._ftp_user, self._ftp_pass)
                if self._ftp_dir:
                    ftp.cwd(self._ftp_dir)

                with open(local_path, "rb") as f:
                    ftp.storbinary(f"STOR {filename}", f)

                ftp.storbinary(f"STOR {sidecar}", io.BytesIO(sidecar_data))

            machine_job_id = f"FTP-{job_card}"
            return {
                "ok":             True,
                "machine_job_id": machine_job_id,
                "method":         "ftp",
                "ftp_path":       f"{self._ftp_dir}/{filename}".lstrip("/"),
                "emb_params":     emb_params,
                "driver":         self.DRIVER_TYPE,
            }
        except ftplib.all_errors as e:
            frappe.logger().error(f"[MelcoEmbDriver] FTP push error: {e}")
            self._log_event("JobSent", "Failure", job_card=job_card,
                            error_code="ftp_error", error_message=str(e))
            return {"ok": False, "error": "ftp_error", "detail": str(e),
                    "driver": self.DRIVER_TYPE}
        except Exception as e:
            frappe.logger().error(f"[MelcoEmbDriver] Unexpected error: {e}")
            return {"ok": False, "error": str(e), "driver": self.DRIVER_TYPE}

    def _remove_ftp_job(self, job_card: str, machine_job_id: str) -> dict:
        """Attempts to delete the FTP-queued design file + sidecar for a job."""
        if not self.host:
            return {"ok": False, "error": "ftp_host_not_configured", "driver": self.DRIVER_TYPE}

        removed = 0
        errors  = []
        try:
            with ftplib.FTP() as ftp:
                ftp.connect(self.host, self._ftp_port, timeout=self.timeout)
                ftp.login(self._ftp_user, self._ftp_pass)
                if self._ftp_dir:
                    ftp.cwd(self._ftp_dir)

                files     = ftp.nlst()
                to_delete = [f for f in files if f.startswith(f"ZAZFIT-{job_card}")]
                for fname in to_delete:
                    try:
                        ftp.delete(fname)
                        removed += 1
                    except ftplib.error_perm as e:
                        errors.append(str(e))

        except ftplib.all_errors as e:
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
            "method":         "ftp_delete",
            "files_removed":  removed,
            "driver":         self.DRIVER_TYPE,
        }
