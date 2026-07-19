"""
downtime_utils.py -- Module 7: Downtime Root-Cause AI
======================================================
AI classification of downtime cause from free text + category,
recurring pattern detection, and corrective action recommendations.

No external LLM calls — classification is rule-based keyword matching
against the DowntimeCauseCategory master. Pattern detection uses SQL
aggregation over the last 30 days.
"""

import json
import frappe
from frappe import _
from frappe.utils import now_datetime, add_days


# ─── Keyword → root cause group mapping ─────────────────────────────────────
KEYWORD_MAP = {
    "Machine": [
        "machine", "breakdown", "motor", "belt", "jam", "needle", "thread tension",
        "bobbin", "overlock", "presser foot", "servo", "oil", "lubrication",
        "cutter blade", "printer", "heat press", "calibration", "sensor",
    ],
    "Operator": [
        "operator", "absent", "training", "skill", "error", "mistake",
        "injury", "bathroom", "break", "fatigue", "slow", "new hire",
    ],
    "Material": [
        "fabric", "material", "thread", "button", "zipper", "trim", "defect",
        "short supply", "stockout", "wrong material", "colour", "color", "dye lot",
        "shrinkage", "weight",
    ],
    "Process": [
        "process", "instruction", "spec", "pattern", "setup", "changeover",
        "workflow", "sequencing", "routing", "rework", "redo", "quality",
        "inspection", "approval",
    ],
    "External": [
        "power", "electricity", "outage", "blackout", "internet", "supplier",
        "delivery", "logistics", "customs", "weather", "flood",
    ],
}


class DowntimeEngine:

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------

    def log_event(self, stage: str, started_at, ended_at=None,
                  work_order: str = None, machine_id: str = None,
                  operator: str = None, reported_cause: str = None,
                  cause_category: str = None) -> dict:
        doc = frappe.new_doc("Downtime Event")
        doc.stage          = stage
        doc.work_order     = work_order or ""
        doc.machine_id     = machine_id or ""
        doc.operator       = operator or ""
        doc.started_at     = started_at
        doc.ended_at       = ended_at
        doc.reported_cause = reported_cause or ""
        doc.cause_category = cause_category or ""

        # AI classify if no category provided
        if not cause_category and reported_cause:
            classification = self._classify_cause(reported_cause)
            doc.cause_category   = classification["category"] or ""
            doc.root_cause_group = classification["group"]
            doc.ai_classified    = 1
            doc.ai_confidence    = classification["confidence"]
            doc.recommended_action = classification["recommended_action"]
        elif cause_category:
            doc.root_cause_group = frappe.db.get_value(
                "Downtime Cause Category", cause_category, "root_cause_group") or "Unknown"

        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        # Check for recurrence and update flag
        self._check_and_flag_recurring(doc)

        return {
            "name":              doc.name,
            "stage":             doc.stage,
            "duration_minutes":  doc.duration_minutes,
            "root_cause_group":  doc.root_cause_group,
            "cause_category":    doc.cause_category,
            "ai_classified":     bool(doc.ai_classified),
            "recommended_action": doc.recommended_action,
            "is_recurring":      bool(doc.is_recurring),
        }

    def resolve_event(self, event_name: str, resolution_notes: str,
                      ended_at=None) -> dict:
        doc = frappe.get_doc("Downtime Event", event_name)
        doc.resolution_notes = resolution_notes
        doc.resolved_by      = frappe.session.user
        if ended_at:
            doc.ended_at = ended_at
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return {
            "name":             doc.name,
            "duration_minutes": doc.duration_minutes,
            "resolved_by":      doc.resolved_by,
        }

    # ------------------------------------------------------------------
    # AI classification
    # ------------------------------------------------------------------

    def classify_cause(self, reported_cause: str,
                       cause_category: str = None) -> dict:
        """Public entry point for on-demand classification."""
        if cause_category:
            cat_doc = frappe.db.get_value(
                "Downtime Cause Category", cause_category,
                ["root_cause_group", "recommended_action"], as_dict=True)
            if cat_doc:
                return {
                    "group":              cat_doc.root_cause_group or "Unknown",
                    "category":           cause_category,
                    "confidence":         1.0,
                    "recommended_action": cat_doc.recommended_action or "",
                }
        return self._classify_cause(reported_cause or "")

    def _classify_cause(self, text: str) -> dict:
        text_lower = text.lower()
        scores: dict = {group: 0 for group in KEYWORD_MAP}

        for group, keywords in KEYWORD_MAP.items():
            for kw in keywords:
                if kw in text_lower:
                    scores[group] += 1

        best_group = max(scores, key=lambda g: scores[g])
        best_score = scores[best_group]

        if best_score == 0:
            best_group = "Unknown"
            confidence = 0.0
        else:
            total = sum(scores.values())
            confidence = round(best_score / total, 2) if total else 0.0

        # Try to match to a specific category in master
        matched_category = self._match_category(best_group, text_lower)
        recommended = self._get_recommendation(matched_category, best_group)

        return {
            "group":              best_group,
            "category":           matched_category,
            "confidence":         confidence,
            "recommended_action": recommended,
        }

    def _match_category(self, group: str, text_lower: str) -> str:
        """Find best-matching DowntimeCauseCategory for the given group."""
        cats = frappe.get_all(
            "Downtime Cause Category",
            filters={"root_cause_group": group, "is_active": 1},
            fields=["name", "category_name"],
        )
        for cat in cats:
            if cat.category_name.lower() in text_lower:
                return cat.name
        return cats[0].name if cats else ""

    def _get_recommendation(self, category: str, group: str) -> str:
        if category:
            rec = frappe.db.get_value(
                "Downtime Cause Category", category, "recommended_action")
            if rec:
                return rec
        defaults = {
            "Machine":   "Schedule preventive maintenance. Log fault in machine log.",
            "Operator":  "Review with operator lead. Check training records.",
            "Material":  "Flag fabric/material lot. Raise issue with procurement.",
            "Process":   "Review SOPs for this stage. Update work instructions.",
            "External":  "Log incident. Check supplier/utility status.",
            "Unknown":   "Investigate further. Assign cause category.",
        }
        return defaults.get(group, "Investigate and assign root cause.")

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def _check_and_flag_recurring(self, doc):
        """
        Count how many events with the same cause_category or root_cause_group
        occurred in the last 30 days for the same stage/machine.
        Flag is_recurring if count >= 3.
        """
        if not doc.cause_category and not doc.root_cause_group:
            return

        filters = {
            "stage":    doc.stage,
            "creation": [">=", add_days(now_datetime(), -30)],
            "name":     ["!=", doc.name],
        }
        if doc.cause_category:
            filters["cause_category"] = doc.cause_category
        elif doc.root_cause_group:
            filters["root_cause_group"] = doc.root_cause_group

        count = frappe.db.count("Downtime Event", filters=filters)
        if count >= 2:  # 2 prior + current = 3 total
            frappe.db.set_value("Downtime Event", doc.name, {
                "is_recurring":    1,
                "recurrence_count": count + 1,
            })
            # Alert supervisors
            frappe.publish_realtime(
                event="downtime_recurring_alert",
                message={
                    "event_name":    doc.name,
                    "stage":         doc.stage,
                    "cause_category": doc.cause_category or doc.root_cause_group,
                    "recurrence_count": count + 1,
                    "machine_id":    doc.machine_id,
                },
                room="shop_floor_supervisors",
            )

    # ------------------------------------------------------------------
    # Intelligence report
    # ------------------------------------------------------------------

    def generate_report(self, window_days: int = 7,
                        window_label: str = None) -> dict:
        now   = now_datetime()
        since = add_days(now, -window_days)
        label = window_label or "Last {}d {}".format(
            window_days, now.strftime("%Y-%m-%d"))

        events = frappe.db.sql(
            """
            SELECT stage, root_cause_group, cause_category, machine_id,
                   duration_minutes, is_recurring
            FROM `tabDowntime Event`
            WHERE started_at >= %(since)s
              AND started_at <= %(now)s
            """,
            {"since": since, "now": now},
            as_dict=True,
        )

        if not events:
            return self._empty_report(label, since, now)

        total_events = len(events)
        total_mins   = sum(float(e.duration_minutes or 0) for e in events)
        avg_mins     = round(total_mins / total_events, 2) if total_events else 0
        recurring    = sum(1 for e in events if e.is_recurring)

        # By stage
        by_stage: dict = {}
        for e in events:
            s = e.stage or "Unknown"
            by_stage.setdefault(s, {"events": 0, "minutes": 0})
            by_stage[s]["events"]  += 1
            by_stage[s]["minutes"] += float(e.duration_minutes or 0)

        # By root cause group
        by_group: dict = {}
        for e in events:
            g = e.root_cause_group or "Unknown"
            by_group.setdefault(g, {"events": 0, "minutes": 0})
            by_group[g]["events"]  += 1
            by_group[g]["minutes"] += float(e.duration_minutes or 0)

        # Top machines
        by_machine: dict = {}
        for e in events:
            m = e.machine_id or "Unknown"
            by_machine.setdefault(m, {"events": 0, "minutes": 0})
            by_machine[m]["events"]  += 1
            by_machine[m]["minutes"] += float(e.duration_minutes or 0)
        top_machines = dict(sorted(
            by_machine.items(), key=lambda x: x[1]["events"], reverse=True)[:5])

        # Recurring causes
        recurring_causes: dict = {}
        for e in events:
            if e.is_recurring:
                key = e.cause_category or e.root_cause_group or "Unknown"
                recurring_causes[key] = recurring_causes.get(key, 0) + 1
        top_recurring = dict(sorted(
            recurring_causes.items(), key=lambda x: x[1], reverse=True)[:5])

        top_group = max(by_group, key=lambda g: by_group[g]["minutes"],
                        default="Unknown")
        top_stage = max(by_stage, key=lambda s: by_stage[s]["minutes"],
                        default="Unknown")

        recommendations = self._build_recommendations(
            by_group, top_recurring, avg_mins, recurring)

        # Persist
        rpt = frappe.new_doc("Downtime Intelligence Report")
        rpt.window_label          = label
        rpt.window_start          = since
        rpt.window_end            = now
        rpt.generated_at          = now
        rpt.total_events          = total_events
        rpt.total_minutes_lost    = round(total_mins, 2)
        rpt.avg_duration_minutes  = avg_mins
        rpt.top_root_cause_group  = top_group
        rpt.top_stage             = top_stage
        rpt.recurring_events      = recurring
        rpt.by_stage_json         = json.dumps(by_stage)
        rpt.by_root_cause_json    = json.dumps(by_group)
        rpt.top_machines_json     = json.dumps(top_machines)
        rpt.recurring_causes_json = json.dumps(top_recurring)
        rpt.recommendations       = recommendations
        rpt.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "name":               rpt.name,
            "window_label":       label,
            "total_events":       total_events,
            "total_minutes_lost": round(total_mins, 2),
            "avg_duration":       avg_mins,
            "top_root_cause":     top_group,
            "top_stage":          top_stage,
            "recurring_events":   recurring,
            "by_stage":           by_stage,
            "by_root_cause":      by_group,
            "top_machines":       top_machines,
            "recurring_causes":   top_recurring,
            "recommendations":    recommendations,
        }

    def get_open_events(self, stage: str = None) -> list:
        filters = {"ended_at": ("is", "not set")}
        if stage:
            filters["stage"] = stage
        return frappe.get_all(
            "Downtime Event",
            filters=filters,
            fields=["name", "stage", "work_order", "machine_id", "operator",
                    "started_at", "root_cause_group", "cause_category",
                    "reported_cause", "duration_minutes"],
            order_by="started_at asc",
        )

    def get_history(self, stage: str = None, days: int = 7,
                    limit: int = 50) -> list:
        filters = {"started_at": [">=", add_days(now_datetime(), -days)]}
        if stage:
            filters["stage"] = stage
        return frappe.get_all(
            "Downtime Event",
            filters=filters,
            fields=["name", "stage", "work_order", "machine_id", "operator",
                    "started_at", "ended_at", "duration_minutes",
                    "root_cause_group", "cause_category", "is_recurring",
                    "recurrence_count", "recommended_action"],
            order_by="started_at desc",
            limit=limit,
        )

    @staticmethod
    def _build_recommendations(by_group: dict, recurring: dict,
                                avg_mins: float, recurring_count: int) -> str:
        lines = ["Downtime AI Recommendations", ""]
        top_g = sorted(by_group.items(),
                       key=lambda x: x[1]["minutes"], reverse=True)
        for group, data in top_g[:3]:
            mins  = round(data["minutes"], 1)
            count = data["events"]
            lines.append(f"• {group} ({count} events, {mins} min lost):")
            defaults = {
                "Machine":   "  → Schedule PM for highest-frequency machines. "
                             "Review lubrication and blade wear schedules.",
                "Operator":  "  → Review operator assignments for affected stages. "
                             "Schedule targeted skill refreshers.",
                "Material":  "  → Audit incoming fabric/trim QC process. "
                             "Flag problematic lots with procurement.",
                "Process":   "  → Review stage SOPs and changeover procedures. "
                             "Consider kaizen event for top affected stage.",
                "External":  "  → Assess backup power/supplier contingency plans.",
                "Unknown":   "  → Enforce cause category capture on all events.",
            }
            lines.append(defaults.get(group, "  → Investigate and take corrective action."))

        if recurring_count >= 3:
            lines.append(
                f"\n⚠ {recurring_count} recurring downtime events detected. "
                "Root causes are not being eliminated — escalate to maintenance lead."
            )
            for cause, cnt in list(recurring.items())[:3]:
                lines.append(f"  Recurring: {cause} ({cnt}x)")

        if avg_mins > 30:
            lines.append(
                f"\n⚠ Average downtime duration {avg_mins} min is high. "
                "Review first-response and escalation procedures."
            )
        return "\n".join(lines)

    @staticmethod
    def _empty_report(label, since, now) -> dict:
        return {
            "window_label":    label,
            "total_events":    0,
            "total_minutes_lost": 0,
            "message": "No downtime events recorded in this window.",
        }


# Module-level wrappers
def log_downtime_event(stage, started_at, ended_at=None, work_order=None,
                       machine_id=None, operator=None, reported_cause=None,
                       cause_category=None):
    return DowntimeEngine().log_event(
        stage, started_at, ended_at, work_order, machine_id,
        operator, reported_cause, cause_category)

def resolve_downtime_event(event_name, resolution_notes, ended_at=None):
    return DowntimeEngine().resolve_event(event_name, resolution_notes, ended_at)

def classify_downtime_cause(reported_cause, cause_category=None):
    return DowntimeEngine().classify_cause(reported_cause, cause_category)

def generate_downtime_report(window_days=7, window_label=None):
    return DowntimeEngine().generate_report(window_days, window_label)
