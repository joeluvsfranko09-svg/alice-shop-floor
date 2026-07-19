# Copyright (c) 2026, Athlettia LLC
"""
base_driver.py
--------------
Abstract base class for all ALICE decoration machine drivers.

Every driver (Epson, Melco, Kornit, STAHLS', Tajima, etc.) must
implement this interface. Business logic in decoration_engine.py
only calls methods on this base — it never knows which machine
it's talking to.

Driver contract:
  ping()           → {"ok": bool, "latency_ms": float}
  get_status()     → {"ok": bool, "state": str, "detail": dict}
  send_job(params) → {"ok": bool, "machine_job_id": str, ...}
  get_job_status(machine_job_id) → {"ok": bool, "state": str, ...}
  cancel_job(machine_job_id)     → {"ok": bool}
"""

from __future__ import annotations

import time
import json
import frappe
from abc import ABC, abstractmethod
from frappe.utils import now_datetime


class BaseMachineDriver(ABC):
    """
    Abstract base for all decoration machine drivers.

    Subclasses implement the abstract methods for their specific
    machine API / protocol.
    """

    # Override in each subclass
    DRIVER_TYPE: str = "Base"

    def __init__(self, machine_config):
        """
        machine_config: MachineConfig Frappe document.
        """
        self.cfg = machine_config
        self.name = machine_config.name
        self.host = machine_config.host
        self.port = machine_config.port
        self.use_https = bool(machine_config.use_https)
        self.timeout = int(machine_config.timeout_seconds or 30)
        self._base_url = self._build_base_url()

    def _build_base_url(self) -> str:
        scheme = "https" if self.use_https else "http"
        port_str = f":{self.port}" if self.port else ""
        return f"{scheme}://{self.host}{port_str}"

    def _get_headers(self) -> dict:
        """Returns auth headers based on machine_config.auth_type."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth_type = self.cfg.get("auth_type") or "None"

        if auth_type == "Basic":
            import base64
            creds = f"{self.cfg.username}:{self.cfg.get_password('password')}"
            token = base64.b64encode(creds.encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        elif auth_type in ("Bearer Token", "API Key"):
            key = self.cfg.get_password("api_key") or ""
            headers["Authorization"] = f"Bearer {key}"

        return headers

    # ------------------------------------------------------------------
    # Abstract methods — every driver must implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def ping(self) -> dict:
        """
        Check if machine is reachable.
        Returns: {"ok": bool, "latency_ms": float, "detail": str}
        """

    @abstractmethod
    def get_status(self) -> dict:
        """
        Get current machine state.
        Returns: {"ok": bool, "state": "Idle"|"Printing"|"Error"|"Offline", "detail": dict}
        """

    @abstractmethod
    def send_job(self, params: dict) -> dict:
        """
        Submit a decoration job to the machine.

        params dict always contains:
          - job_card:        ERPNext Job Card name
          - design_file:     URL or local path to the design file
          - decoration_method: "DTF"|"DTG"|"Embroidery"
          - design_placement: "Full Front"|"Left Chest"|etc.
          - recipe_params:   dict of machine-specific params from ProductionRecipe

        Returns: {"ok": bool, "machine_job_id": str, "detail": dict}
        """

    @abstractmethod
    def get_job_status(self, machine_job_id: str) -> dict:
        """
        Poll the status of a submitted job.
        Returns: {"ok": bool, "state": "Queued"|"Printing"|"Complete"|"Error", "detail": dict}
        """

    @abstractmethod
    def cancel_job(self, machine_job_id: str) -> dict:
        """
        Cancel a queued or running job.
        Returns: {"ok": bool}
        """

    # ------------------------------------------------------------------
    # Shared helpers — used by all subclasses
    # ------------------------------------------------------------------

    def _log_event(
        self,
        event_type: str,
        status: str,
        job_card: str = None,
        machine_job_id: str = None,
        request_payload: dict = None,
        response_payload: dict = None,
        error_code: str = None,
        error_message: str = None,
        response_time_ms: float = None,
    ) -> str:
        """
        Writes a MachineJobLog record.
        Returns the log doc name.
        """
        try:
            log = frappe.get_doc({
                "doctype":          "Machine Job Log",
                "machine_config":   self.name,
                "job_card":         job_card or "",
                "event_type":       event_type,
                "status":           status,
                "machine_job_id":   machine_job_id or "",
                "decoration_method": self.cfg.decoration_method or "",
                "sent_at":          now_datetime(),
                "response_time_ms": response_time_ms or 0.0,
                "request_payload":  json.dumps(request_payload or {}),
                "response_payload": json.dumps(response_payload or {}),
                "error_code":       error_code or "",
                "error_message":    error_message or "",
            })
            log.insert(ignore_permissions=True)
            frappe.db.commit()
            return log.name
        except Exception as e:
            frappe.logger().warning(f"[MachineDriver] Failed to write MachineJobLog: {e}")
            return ""

    def _timed_request(self, method: str, url: str, **kwargs) -> tuple:
        """
        Makes an HTTP request and returns (response, elapsed_ms).
        Returns (None, elapsed_ms) on connection error.
        """
        import requests

        headers = {**self._get_headers(), **kwargs.pop("headers", {})}
        start = time.monotonic()
        try:
            resp = requests.request(
                method, url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
            elapsed_ms = (time.monotonic() - start) * 1000
            return resp, elapsed_ms
        except requests.exceptions.Timeout:
            elapsed_ms = (time.monotonic() - start) * 1000
            return None, elapsed_ms
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            frappe.logger().error(f"[{self.DRIVER_TYPE}] Request error → {url}: {e}")
            return None, elapsed_ms

    def _bump_job_counter(self):
        """Increments total_jobs_sent on MachineConfig."""
        try:
            current = frappe.db.get_value("Machine Config", self.name, "total_jobs_sent") or 0
            frappe.db.set_value("Machine Config", self.name, {
                "total_jobs_sent": current + 1,
                "last_job_sent_at": now_datetime(),
            })
        except Exception:
            pass
