"""
esg_utils.py -- Module 9: ESG / Sustainability Reporter
=========================================================
Track per-garment material waste, water, energy, and carbon footprint.
Generate weekly/monthly summary reports with target compliance scoring.
"""

import frappe
from frappe.utils import now_datetime, add_days


class ESGEngine:

    def log_metrics(self, work_order: str, fabric_lot: str = None,
                    fabric_used_gsm: float = None, fabric_ordered_gsm: float = None,
                    waste_grams: float = None, water_litres: float = None,
                    energy_kwh: float = None, notes: str = None) -> dict:
        """Create or update an ESGMetricLog for a Work Order."""
        existing = frappe.db.get_value(
            "ESG Metric Log", {"work_order": work_order}, "name")
        if existing:
            doc = frappe.get_doc("ESG Metric Log", existing)
        else:
            doc = frappe.new_doc("ESG Metric Log")
            doc.work_order = work_order
            doc.fabric_lot = fabric_lot or ""

        if fabric_used_gsm    is not None: doc.fabric_used_gsm     = fabric_used_gsm
        if fabric_ordered_gsm is not None: doc.fabric_ordered_gsm  = fabric_ordered_gsm
        if waste_grams        is not None: doc.waste_grams          = waste_grams
        if water_litres       is not None: doc.water_litres         = water_litres
        if energy_kwh         is not None: doc.energy_kwh           = energy_kwh
        if notes              is not None: doc.notes                = notes

        if existing:
            doc.save(ignore_permissions=True)
        else:
            doc.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "name":                  doc.name,
            "work_order":            doc.work_order,
            "waste_pct":             doc.waste_pct,
            "carbon_kg":             doc.carbon_kg,
            "exceeds_waste_target":  bool(doc.exceeds_waste_target),
            "exceeds_water_target":  bool(doc.exceeds_water_target),
            "exceeds_energy_target": bool(doc.exceeds_energy_target),
        }

    def generate_report(self, window_days: int = 7,
                        period_label: str = None) -> dict:
        now   = now_datetime()
        since = add_days(now, -window_days)
        label = period_label or "Week of {}".format(now.strftime("%Y-%W"))

        logs = frappe.db.sql(
            """
            SELECT waste_grams, waste_pct, water_litres, energy_kwh, carbon_kg,
                   exceeds_waste_target, exceeds_water_target, exceeds_energy_target
            FROM `tabESG Metric Log`
            WHERE logged_at >= %(since)s AND logged_at <= %(now)s
            """,
            {"since": since, "now": now},
            as_dict=True,
        )

        if not logs:
            return {"period_label": label, "garments_logged": 0,
                    "message": "No ESG data recorded in this window."}

        n = len(logs)
        total_waste_g  = sum(float(r.waste_grams  or 0) for r in logs)
        total_water    = sum(float(r.water_litres or 0) for r in logs)
        total_kwh      = sum(float(r.energy_kwh   or 0) for r in logs)
        total_co2      = sum(float(r.carbon_kg    or 0) for r in logs)
        avg_waste_pct  = round(sum(float(r.waste_pct or 0) for r in logs) / n, 2)
        exc_waste  = sum(1 for r in logs if r.exceeds_waste_target)
        exc_water  = sum(1 for r in logs if r.exceeds_water_target)
        exc_energy = sum(1 for r in logs if r.exceeds_energy_target)

        # Compliance status
        exc_total = exc_waste + exc_water + exc_energy
        if exc_total == 0:
            compliance = "Compliant"
        elif exc_total <= n * 0.1:  # up to 10% of garments exceed any target
            compliance = "Warning"
        else:
            compliance = "Non-Compliant"

        narrative = self._build_narrative(
            n, avg_waste_pct, total_water, total_kwh, total_co2,
            exc_waste, exc_water, exc_energy, compliance, window_days
        )

        rpt = frappe.new_doc("ESG Summary Report")
        rpt.period_label          = label
        rpt.period_start          = since
        rpt.period_end            = now
        rpt.generated_at          = now
        rpt.garments_logged       = n
        rpt.total_waste_kg        = round(total_waste_g / 1000, 4)
        rpt.avg_waste_pct         = avg_waste_pct
        rpt.total_water_litres    = round(total_water, 2)
        rpt.avg_water_per_garment = round(total_water / n, 2) if n else 0
        rpt.total_energy_kwh      = round(total_kwh, 2)
        rpt.avg_kwh_per_garment   = round(total_kwh / n, 2) if n else 0
        rpt.total_co2_kg          = round(total_co2, 4)
        rpt.avg_co2_per_garment   = round(total_co2 / n, 4) if n else 0
        rpt.wos_exceeding_waste   = exc_waste
        rpt.wos_exceeding_water   = exc_water
        rpt.wos_exceeding_energy  = exc_energy
        rpt.compliance_status     = compliance
        rpt.narrative             = narrative
        rpt.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "name":              rpt.name,
            "period_label":      label,
            "garments_logged":   n,
            "avg_waste_pct":     avg_waste_pct,
            "total_co2_kg":      round(total_co2, 4),
            "avg_co2_per_garment": round(total_co2 / n, 4) if n else 0,
            "compliance_status": compliance,
            "narrative":         narrative,
        }

    def get_latest_report(self) -> dict:
        latest = frappe.db.get_value(
            "ESG Summary Report", {}, "name", order_by="generated_at desc")
        if not latest:
            return {}
        return frappe.get_doc("ESG Summary Report", latest).as_dict()

    def get_metrics_for_wo(self, work_order: str) -> dict:
        name = frappe.db.get_value("ESG Metric Log", {"work_order": work_order}, "name")
        if not name:
            return {}
        return frappe.get_doc("ESG Metric Log", name).as_dict()

    @staticmethod
    def _build_narrative(n, avg_waste_pct, total_water, total_kwh, total_co2,
                         exc_waste, exc_water, exc_energy, compliance,
                         window_days) -> str:
        lines = [
            f"ESG Summary — Last {window_days} days",
            f"Status: {compliance}",
            f"{n} garments logged.",
            f"Avg fabric waste: {avg_waste_pct}%",
            f"Total water: {round(total_water, 1)} L  |  "
            f"Total energy: {round(total_kwh, 2)} kWh  |  "
            f"Total CO₂: {round(total_co2, 2)} kg",
            "",
        ]
        if exc_waste > 0:
            lines.append(
                f"⚠ {exc_waste} WO(s) exceeded fabric waste target. "
                "Review cutting patterns and operator technique.")
        if exc_water > 0:
            lines.append(
                f"⚠ {exc_water} WO(s) exceeded water usage target. "
                "Check print/wash process.")
        if exc_energy > 0:
            lines.append(
                f"⚠ {exc_energy} WO(s) exceeded energy target. "
                "Audit machine idle times and heat press settings.")
        if compliance == "Compliant":
            lines.append("All metrics within target. No action required.")
        return "\n".join(lines)


# Module-level wrappers
def log_esg_metrics(work_order, fabric_lot=None, fabric_used_gsm=None,
                    fabric_ordered_gsm=None, waste_grams=None,
                    water_litres=None, energy_kwh=None, notes=None):
    return ESGEngine().log_metrics(
        work_order, fabric_lot, fabric_used_gsm, fabric_ordered_gsm,
        waste_grams, water_litres, energy_kwh, notes)

def generate_esg_report(window_days=7, period_label=None):
    return ESGEngine().generate_report(window_days, period_label)

def get_latest_esg_report():
    return ESGEngine().get_latest_report()
