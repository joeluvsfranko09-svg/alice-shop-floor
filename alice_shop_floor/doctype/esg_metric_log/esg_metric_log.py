"""ESG Metric Log — auto-calculates waste%, CO2, and flags on save."""

import frappe
from frappe.utils import now_datetime


class ESGMetricLog(frappe.model.document.Document):

    def before_save(self):
        self.logged_at = self.logged_at or now_datetime()
        self._calc_waste()
        self._calc_carbon()
        self._set_flags()

    def _calc_waste(self):
        used    = float(self.fabric_used_gsm or 0)
        ordered = float(self.fabric_ordered_gsm or 0)
        if ordered > 0 and used > 0:
            waste = max(ordered - used, 0)
            self.waste_grams = round(waste, 2)
            self.waste_pct   = round(waste / ordered * 100, 2)
        elif self.waste_grams and ordered > 0:
            self.waste_pct = round(float(self.waste_grams) / ordered * 100, 2)

    def _calc_carbon(self):
        kwh = float(self.energy_kwh or 0)
        if kwh > 0:
            try:
                factor = float(
                    frappe.db.get_single_value(
                        "ESG Target Config", "kwh_to_co2_factor") or 0.233
                )
            except Exception:
                factor = 0.233
            self.carbon_kg = round(kwh * factor, 4)

    def _set_flags(self):
        try:
            cfg = frappe.get_single("ESG Target Config")
        except Exception:
            return
        self.exceeds_waste_target  = (
            1 if float(self.waste_pct or 0) > float(cfg.max_waste_pct or 5) else 0)
        self.exceeds_water_target  = (
            1 if float(self.water_litres or 0) > float(cfg.max_water_litres_per_garment or 50) else 0)
        self.exceeds_energy_target = (
            1 if float(self.energy_kwh or 0) > float(cfg.max_kwh_per_garment or 2) else 0)
