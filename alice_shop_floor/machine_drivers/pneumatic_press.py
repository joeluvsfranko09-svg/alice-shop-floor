# Copyright (c) 2026, Athlettia LLC
"""
pneumatic_press.py
------------------
ALICE Machine Driver — Pneumatic Heat Press (DTF Transfer Station)

The pneumatic press is not networked — it has no REST API.
This driver tracks the press operation as a manual step in the DTF
workflow and validates press parameters against safe operating ranges.

DTF workflow at ZAZFIT:
  Step 1 — Epson G6070 prints DTF film  (EpsonDTFDriver)
  Step 2 — Film cures through dryer     (automatic, no driver)
  Step 3 — Operator loads garment + film onto press, presses button
            → THIS DRIVER — validates params, logs the press event,
              advances the Job Card to the next stage

Press parameters (set on ProductionRecipe, validated here):
  press_temp_f      Platen surface temp (safe range: 300°F – 420°F)
  dwell_time_sec    Platen close time   (safe range: 8 – 20 sec)
  pressure_psi      Pneumatic pressure  (safe range: 30 – 80 PSI)
  peel_type         Hot / Cold / Warm
  pre_press_sec     Optional pre-press to remove moisture (0 = skip)

MachineConfig override fields (applied before ProductionRecipe overrides):
  press_default_temp_f      Override driver default temp (still validated against safe range)
  press_default_dwell_sec   Override driver default dwell
  press_default_psi         Override driver default PSI

Because there's no network connection:
  ping()           → checks if a MachineJobLog was written recently
                     (i.e. the press is being actively used)
  get_status()     → always returns Idle (we can't poll a dumb press)
  send_job()       → validates params, writes a PressJobLog record,
                     returns a synthetic job ID "PRESS-{job_card}"
  get_job_status() → returns Complete immediately (operator confirms
                     the transfer physically; no async state)
  cancel_job()     → no-op with explanation
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime
from .base_driver import BaseMachineDriver
from .epson_dtf import _safe_json


class PneumaticPressDriver(BaseMachineDriver):
    """
    Driver for a pneumatic heat press station (DTF transfer).

    The press has no network API. This driver:
      - Validates press parameters against safe operating ranges
      - Logs every press event to MachineJobLog for traceability
      - Advances the Job Card status so the floor view reflects
        that the transfer step is in progress / complete
    """

    DRIVER_TYPE = "PneumaticPress"

    # Standard DTF transfer parameters for ZAZFIT (override via ProductionRecipe)
    PRESS_DEFAULTS = {
        "press_temp_f":   385.0,   # °F — standard hot-peel DTF
        "dwell_time_sec": 12,      # seconds under pressure
        "pressure_psi":   50,      # PSI — typical pneumatic auto-press setting
        "peel_type":      "Hot",   # Hot / Warm / Cold
        "pre_press_sec":  3,       # short pre-press to flash moisture out of garment
    }

    # Safe operating envelope — warn / block if outside these
    SAFE_TEMP_MIN_F   = 300.0
    SAFE_TEMP_MAX_F   = 420.0
    SAFE_DWELL_MIN    = 8
    SAFE_DWELL_MAX    = 20
    SAFE_PSI_MIN      = 30
    SAFE_PSI_MAX      = 80

    # How many minutes of inactivity before ping() considers the press "idle/offline"
    ACTIVE_WINDOW_MIN = 30

    def __init__(self, machine_config):
        super().__init__(machine_config)
        # Pneumatic press has no base URL — override to empty
        self._base_url = ""

        # Allow per-machine overrides from MachineConfig fields
        # (these only apply if the operator has filled in the Pneumatic Press Settings section)
        cfg_temp  = machine_config.get("press_default_temp_f")
        cfg_dwell = machine_config.get("press_default_dwell_sec")
        cfg_psi   = machine_config.get("press_default_psi")

        if cfg_temp:
            self.PRESS_DEFAULTS = {**self.PRESS_DEFAULTS, "press_temp_f": float(cfg_temp)}
        if cfg_dwell:
            self.PRESS_DEFAULTS = {**self.PRESS_DEFAULTS, "dwell_time_sec": int(cfg_dwell)}
        if cfg_psi:
            self.PRESS_DEFAULTS = {**self.PRESS_DEFAULTS, "pressure_psi": int(cfg_psi)}

    # ------------------------------------------------------------------
    # BaseMachineDriver interface
    # ------------------------------------------------------------------

    def ping(self) -> dict:
        """
        Checks whether the press has had any logged activity in the last
        ACTIVE_WINDOW_MIN minutes.  Returns ok=True if active, ok=False if idle.
        """
        from frappe.utils import get_datetime
        from datetime import timedelta

        cutoff = now_datetime() - timedelta(minutes=self.ACTIVE_WINDOW_MIN)
        recent = frappe.db.exists(
            "Machine Job Log",
            {
                "machine_config": self.name,
                "status":         "Success",
                "sent_at":        [">=", cutoff],
            },
        )
        active = bool(recent)
        self._log_event(
            "Ping", "Success",
            response_payload={"active_last_30min": active},
        )
        return {
            "ok":       True,         # driver itself is always reachable
            "active":   active,
            "latency_ms": 0.0,
            "detail":   f"Press {'active' if active else 'idle'} in last {self.ACTIVE_WINDOW_MIN} min",
            "driver":   self.DRIVER_TYPE,
        }

    def get_status(self) -> dict:
        """
        Pneumatic press has no programmatic status.
        Returns Idle — actual state is visible to the operator on the floor.
        """
        return {
            "ok":     True,
            "state":  "Idle",
            "detail": {"note": "Pneumatic press — status tracked manually by operator"},
            "driver": self.DRIVER_TYPE,
        }

    def send_job(self, params: dict) -> dict:
        """
        Validates press parameters for safety, logs the press event,
        and returns a synthetic job ID.

        This does NOT physically trigger the press — the operator does that.
        This call records that a press job has been dispatched and the
        parameters are safe to use.

        params expected keys:
          job_card, recipe_params (dict with press params),
          design_placement, garment_size, fabric_type
        """
        job_card = params.get("job_card", "")
        recipe   = params.get("recipe_params", {})

        press = {**self.PRESS_DEFAULTS, **recipe}

        # ── Safety validation ────────────────────────────────────────────────
        violations = self._check_safety(press)
        if violations:
            for v in violations:
                frappe.logger().error(
                    f"[PneumaticPressDriver] SAFETY VIOLATION on {job_card}: {v}"
                )
            self._log_event(
                "JobSent", "Failure",
                job_card=job_card,
                request_payload=press,
                error_code="safety_violation",
                error_message=" | ".join(violations),
            )
            return {
                "ok":        False,
                "error":     "safety_violation",
                "violations": violations,
                "job_card":  job_card,
                "driver":    self.DRIVER_TYPE,
            }

        # ── Log the press dispatch ───────────────────────────────────────────
        machine_job_id = f"PRESS-{job_card}"

        self._log_event(
            "JobSent", "Success",
            job_card=job_card,
            machine_job_id=machine_job_id,
            request_payload={
                "press_params":      press,
                "design_placement":  params.get("design_placement", ""),
                "garment_size":      params.get("garment_size", ""),
                "fabric_type":       params.get("fabric_type", ""),
            },
        )
        self._bump_job_counter()

        # Stamp press params back onto the Job Card for operator reference
        try:
            import json
            frappe.db.set_value("Job Card", job_card, {
                "press_params_json": json.dumps(press),
                "press_dispatched_at": now_datetime(),
            })
        except Exception:
            pass  # field may not exist in older installs

        return {
            "ok":            True,
            "machine_job_id": machine_job_id,
            "method":        "manual",
            "press_params":  press,
            "note":          (
                f"Press params validated — temp {press['press_temp_f']}°F × "
                f"{press['dwell_time_sec']}s @ {press['pressure_psi']} PSI. "
                f"Operator completes the transfer physically."
            ),
            "driver":        self.DRIVER_TYPE,
        }

    def get_job_status(self, machine_job_id: str) -> dict:
        """
        Pneumatic press operations are synchronous from the operator's perspective.
        If they called send_job(), they followed it immediately with the press.
        Returns Complete — actual confirmation happens via the Job Card scan event.
        """
        return {
            "ok":     True,
            "state":  "Complete",
            "detail": {
                "note":     "Press transfer is a synchronous manual operation.",
                "confirm":  "Operator confirms by scanning the Job Card after transfer.",
            },
            "driver": self.DRIVER_TYPE,
        }

    def cancel_job(self, machine_job_id: str) -> dict:
        """
        Press operations cannot be cancelled programmatically.
        Operator physically stops the press cycle if needed.
        """
        return {
            "ok":     False,
            "error":  "cancel_not_supported",
            "detail": "Pneumatic press is manually operated — cancel physically at the machine.",
            "driver": self.DRIVER_TYPE,
        }

    # ------------------------------------------------------------------
    # Safety validation
    # ------------------------------------------------------------------

    def _check_safety(self, press: dict) -> list[str]:
        """
        Validates press parameters against safe operating ranges.
        Returns a list of violation strings (empty = all safe).
        """
        violations = []

        temp = press.get("press_temp_f", 0)
        if not (self.SAFE_TEMP_MIN_F <= temp <= self.SAFE_TEMP_MAX_F):
            violations.append(
                f"press_temp_f {temp}°F is outside safe range "
                f"{self.SAFE_TEMP_MIN_F}–{self.SAFE_TEMP_MAX_F}°F"
            )

        dwell = press.get("dwell_time_sec", 0)
        if not (self.SAFE_DWELL_MIN <= dwell <= self.SAFE_DWELL_MAX):
            violations.append(
                f"dwell_time_sec {dwell}s is outside safe range "
                f"{self.SAFE_DWELL_MIN}–{self.SAFE_DWELL_MAX}s"
            )

        psi = press.get("pressure_psi", 0)
        if not (self.SAFE_PSI_MIN <= psi <= self.SAFE_PSI_MAX):
            violations.append(
                f"pressure_psi {psi} is outside safe range "
                f"{self.SAFE_PSI_MIN}–{self.SAFE_PSI_MAX} PSI"
            )

        return violations
