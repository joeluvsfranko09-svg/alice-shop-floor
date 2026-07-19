"""
defect_intelligence_utils.py -- V5: Defect Intelligence Aggregator
====================================================================
Cross-module analytics: joins V1/V2/V3/V4 inspection results and
surfaces defect patterns by stage, operator, fabric lot, and time window.

Runs as a daily scheduled task; also callable on demand via API.
"""

import json
import frappe
from frappe.utils import now_datetime, add_days


# Source map: stage -> (result_doctype, defect_child_doctype, count_fields)
STAGE_SOURCES = {
    "Fabric Inspection": {
        "result_dt":  "Fabric Inspection Result",
        "minor_field":    "defect_count_minor",
        "major_field":    "defect_count_major",
        "critical_field": "defect_count_critical",
        "child_dt":       "Fabric Defect Map",
        "type_field":     "defect_type",
    },
    "Cutting": {
        "result_dt":      "Cut Inspection Result",
        "minor_field":    "deviation_count_minor",
        "major_field":    "deviation_count_major",
        "critical_field": "deviation_count_critical",
        "child_dt":       "Cut Deviation Map",
        "type_field":     "deviation_type",
    },
    "Sewing": {
        "result_dt":      "Stitch Inspection Result",
        "minor_field":    "defect_count_minor",
        "major_field":    "defect_count_major",
        "critical_field": "defect_count_critical",
        "child_dt":       "Stitch Defect Map",
        "type_field":     "defect_type",
    },
    "Final QC": {
        "result_dt":      "Final Inspection Result",
        "minor_field":    "defect_count_minor",
        "major_field":    "defect_count_major",
        "critical_field": "defect_count_critical",
        "child_dt":       "Garment Defect Map",
        "type_field":     "defect_type",
    },
}


class DefectIntelligenceEngine:

    def generate_report(self, window_days: int = 7,
                        window_label: str = None) -> dict:
        """
        Aggregate all inspection results across V1-V4 for the given
        rolling window. Creates + saves a DefectIntelligenceReport document.
        Returns the report dict.
        """
        now    = now_datetime()
        since  = add_days(now, -window_days)
        label  = window_label or "Last {}d {}".format(
            window_days, now.strftime("%Y-%m-%d"))

        stage_rows  = []
        grand_minor = grand_major = grand_critical = 0
        grand_insp  = grand_pass = 0
        defect_type_counts: dict = {}
        fabric_lot_fails:   dict = {}

        for stage, src in STAGE_SOURCES.items():
            row = self._aggregate_stage(stage, src, since, now,
                                        defect_type_counts, fabric_lot_fails)
            stage_rows.append(row)
            grand_insp     += row["total_inspected"]
            grand_pass     += row["total_passed"]
            grand_minor    += row["defects_minor"]
            grand_major    += row["defects_major"]
            grand_critical += row["defects_critical"]

        total_defects   = grand_minor + grand_major + grand_critical
        overall_pass    = round(grand_pass / grand_insp * 100, 1) if grand_insp else 0

        top_defect_types = sorted(
            defect_type_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        top_fabric_lots = sorted(
            fabric_lot_fails.items(), key=lambda x: x[1], reverse=True)[:5]
        top_operators   = self._get_operator_attention_list(since, now)

        ai_summary = self._build_ai_summary(
            stage_rows, top_defect_types, top_fabric_lots, top_operators,
            overall_pass, grand_critical, window_days
        )

        # Persist to Frappe
        report = frappe.new_doc("Defect Intelligence Report")
        report.window_label          = label
        report.window_start          = since
        report.window_end            = now
        report.generated_at          = now
        report.total_wos_inspected   = grand_insp
        report.overall_pass_rate_pct = overall_pass
        report.total_defects         = total_defects
        report.defects_minor         = grand_minor
        report.defects_major         = grand_major
        report.defects_critical      = grand_critical
        report.top_defect_types      = json.dumps(dict(top_defect_types))
        report.top_fabric_lots       = json.dumps(dict(top_fabric_lots))
        report.top_operators         = json.dumps(top_operators)
        report.ai_summary            = ai_summary

        for row in stage_rows:
            report.append("stage_summaries", row)

        report.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "name":              report.name,
            "window_label":      label,
            "overall_pass_rate": overall_pass,
            "total_defects":     total_defects,
            "critical":          grand_critical,
            "top_defect_types":  dict(top_defect_types),
            "top_fabric_lots":   dict(top_fabric_lots),
            "top_operators":     top_operators,
            "ai_summary":        ai_summary,
        }

    def get_latest_report(self) -> dict:
        latest = frappe.db.get_value(
            "Defect Intelligence Report", {}, "name",
            order_by="generated_at desc"
        )
        if not latest:
            return {}
        doc = frappe.get_doc("Defect Intelligence Report", latest)
        return doc.as_dict()

    def get_defect_trend(self, stage: str = None, days: int = 30) -> list:
        """
        Return daily defect counts (minor/major/critical) for the given
        stage over the last N days. Used for dashboard trend charts.
        """
        src_list = ([STAGE_SOURCES[stage]] if stage and stage in STAGE_SOURCES
                    else list(STAGE_SOURCES.values()))
        rows = []
        for src in src_list:
            result_dt = src["result_dt"]
            results = frappe.db.sql(
                """
                SELECT
                    DATE(creation)        AS day,
                    SUM({mf})             AS minor,
                    SUM({mjf})            AS major,
                    SUM({cf})             AS critical,
                    COUNT(*)              AS inspected,
                    SUM(CASE WHEN overall_result='Pass' THEN 1 ELSE 0 END) AS passed
                FROM `tab{dt}`
                WHERE overall_result IN ('Pass','Fail')
                  AND creation >= %(since)s
                GROUP BY DATE(creation)
                ORDER BY day ASC
                """.format(
                    dt=result_dt,
                    mf=src["minor_field"],
                    mjf=src["major_field"],
                    cf=src["critical_field"],
                ),
                {"since": add_days(now_datetime(), -days)},
                as_dict=True,
            )
            for r in results:
                r["source"] = result_dt
            rows.extend(results)
        return rows

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _aggregate_stage(self, stage: str, src: dict,
                         since, until,
                         defect_type_counts: dict,
                         fabric_lot_fails: dict) -> dict:
        result_dt = src["result_dt"]
        mf  = src["minor_field"]
        mjf = src["major_field"]
        cf  = src["critical_field"]

        results = frappe.db.sql(
            """
            SELECT name, overall_result, {mf}, {mjf}, {cf},
                   COALESCE(fabric_lot, '') AS fabric_lot
            FROM `tab{dt}`
            WHERE overall_result IN ('Pass','Fail')
              AND creation BETWEEN %(since)s AND %(until)s
            """.format(dt=result_dt, mf=mf, mjf=mjf, cf=cf),
            {"since": since, "until": until},
            as_dict=True,
        )

        total_insp = len(results)
        total_pass = sum(1 for r in results if r.overall_result == "Pass")
        total_fail = total_insp - total_pass
        minor = sum(int(r[mf] or 0) for r in results)
        major = sum(int(r[mjf] or 0) for r in results)
        crit  = sum(int(r[cf] or 0) for r in results)

        # Fabric lot fail tracking
        for r in results:
            if r.overall_result == "Fail" and r.fabric_lot:
                fabric_lot_fails[r.fabric_lot] = (
                    fabric_lot_fails.get(r.fabric_lot, 0) + 1)

        # Top defect types from child table
        top_type = ""
        if src.get("child_dt") and src.get("type_field"):
            type_counts = frappe.db.sql(
                """
                SELECT c.{tf} AS dtype, COUNT(*) AS cnt
                FROM `tab{child}` c
                JOIN `tab{result}` r ON c.parent = r.name
                WHERE r.overall_result IN ('Pass','Fail')
                  AND r.creation BETWEEN %(since)s AND %(until)s
                GROUP BY c.{tf}
                ORDER BY cnt DESC
                LIMIT 5
                """.format(
                    child=src["child_dt"],
                    result=result_dt,
                    tf=src["type_field"],
                ),
                {"since": since, "until": until},
                as_dict=True,
            )
            for tc in type_counts:
                dtype = tc.dtype or "Unknown"
                defect_type_counts[dtype] = (
                    defect_type_counts.get(dtype, 0) + int(tc.cnt))
            if type_counts:
                top_type = type_counts[0].dtype or ""

        pass_rate = round(total_pass / total_insp * 100, 1) if total_insp else 0

        return {
            "stage":           stage,
            "source_doctype":  result_dt,
            "total_inspected": total_insp,
            "total_passed":    total_pass,
            "total_failed":    total_fail,
            "pass_rate_pct":   pass_rate,
            "defects_minor":   minor,
            "defects_major":   major,
            "defects_critical": crit,
            "top_defect_type": top_type,
        }

    def _get_operator_attention_list(self, since, until) -> list:
        """
        Operators appearing in GarmentQCCheck (Module 4) with high defect
        rates in the window — uses existing QC infrastructure.
        """
        try:
            rows = frappe.db.sql(
                """
                SELECT
                    por.operator                   AS operator,
                    COUNT(DISTINCT g.name)          AS garments,
                    SUM(qc.total_defects)           AS total_defects,
                    AVG(qc.total_defects)           AS avg_defects,
                    SUM(CASE WHEN qc.qc_status='Failed' THEN 1 ELSE 0 END) AS fails
                FROM `tabPassport Operator Record` por
                JOIN `tabGarment Passport`         g   ON por.parent = g.name
                JOIN `tabGarment QC Check`         qc  ON qc.work_order = g.work_order
                WHERE g.creation BETWEEN %(since)s AND %(until)s
                GROUP BY por.operator
                HAVING total_defects > 0
                ORDER BY avg_defects DESC
                LIMIT 10
                """,
                {"since": since, "until": until},
                as_dict=True,
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    @staticmethod
    def _build_ai_summary(stage_rows, top_defect_types, top_fabric_lots,
                          top_operators, overall_pass, grand_critical,
                          window_days) -> str:
        lines = [
            f"Defect Intelligence Summary — Last {window_days} days",
            f"Overall pass rate: {overall_pass}%",
            "",
        ]

        # Stage breakdown
        for row in stage_rows:
            pr = row["pass_rate_pct"]
            flag = " ⚠" if pr < 80 else ""
            lines.append(
                f"  {row['stage']}: {row['total_inspected']} inspected, "
                f"{pr}% pass{flag}"
            )
            if row["top_defect_type"]:
                lines.append(f"    Top defect: {row['top_defect_type']}")

        if grand_critical > 0:
            lines.append(f"\n⚠ {grand_critical} CRITICAL defect(s) recorded — immediate review required.")

        if top_defect_types:
            lines.append("\nMost frequent defect types:")
            for dtype, cnt in top_defect_types[:5]:
                lines.append(f"  {dtype}: {cnt}")

        if top_fabric_lots:
            lines.append("\nFabric lots with highest failure count:")
            for lot, cnt in top_fabric_lots[:3]:
                lines.append(f"  {lot}: {cnt} fail(s)")

        if top_operators:
            lines.append("\nOperators with highest avg defects per garment:")
            for op in top_operators[:3]:
                lines.append(
                    f"  {op.get('operator')}: avg {round(float(op.get('avg_defects') or 0), 1)}"
                    f" defects, {op.get('fails')} fail(s)"
                )

        return "\n".join(lines)


# Module-level wrappers
def generate_defect_intelligence_report(window_days=7, window_label=None):
    return DefectIntelligenceEngine().generate_report(window_days, window_label)

def get_latest_defect_intelligence_report():
    return DefectIntelligenceEngine().get_latest_report()

def get_defect_trend(stage=None, days=30):
    return DefectIntelligenceEngine().get_defect_trend(stage, days)
