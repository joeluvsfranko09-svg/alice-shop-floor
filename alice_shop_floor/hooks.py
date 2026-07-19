app_name = "alice_shop_floor"
app_title = "Alice Shop Floor"
app_publisher = "Athlettia LLC"
app_description = "ALICE Shop Floor Layer -- AI-powered MES modules for ZAZFIT"
app_email = "frankoy@athlettia.com"
app_license = "MIT"

required_apps = ["frappe", "erpnext"]

# ------------------------------------------------------------------
# Install hook
# ------------------------------------------------------------------

after_install = "alice_shop_floor.alice_shop_floor.setup.after_install"

# ------------------------------------------------------------------
# Document hooks
# ------------------------------------------------------------------

doc_events = {
    "Work Order": {
        "on_submit": [
            # Module 3: Create cut-to-pack stage tracker
            "alice_shop_floor.alice_shop_floor.doctype.production_stage_tracker.production_stage_tracker.create_tracker_for_work_order",
            # Task #77: Auto-create decoration Job Card from ProductionRecipe
            "alice_shop_floor.alice_shop_floor.work_order_scheduler.create_decoration_job_cards_for_work_order",
            # SanMar: auto-create Purchase Order for blanks if auto_po_enabled
            "alice_shop_floor.alice_shop_floor.sanmar.po_creator.check_and_auto_po_for_work_order",
        ],
        "on_update": [
            # TeeRiot: notify capacity bridge when WO qty or status changes
            # (allows future cache-bust webhook; no-op today — 5-min cache handles it)
            "alice_shop_floor.alice_shop_floor.teeriot_api.on_work_order_update",
        ],
        "on_cancel": [
            # TeeRiot: cancelled WO frees capacity — same hook, no-op today
            "alice_shop_floor.alice_shop_floor.teeriot_api.on_work_order_update",
        ],
    },
    "Job Card": {
        # Auto-route decoration method when a Job Card is created or submitted
        "on_submit": "alice_shop_floor.alice_shop_floor.decoration_engine.on_job_card_submit",
        "on_update":  "alice_shop_floor.alice_shop_floor.decoration_engine.on_job_card_update",
    },
}

# ------------------------------------------------------------------
# Scheduled tasks
# ------------------------------------------------------------------

scheduler_events = {
    "every_5_minutes": [
        # Decoration Engine -- poll Job Cards awaiting routing
        "alice_shop_floor.alice_shop_floor.tasks.run_decoration_routing_check",
        # Pace engine -- fire realtime pace_alert for Critical sewers
        "alice_shop_floor.alice_shop_floor.tasks.run_pace_check",
        # V1: Fabric Inspector -- timeout stale Pending inspections
        "alice_shop_floor.alice_shop_floor.tasks.poll_fabric_inspections",
        # V2: Inline Stitch QC -- timeout stale Pending inspections
        "alice_shop_floor.alice_shop_floor.tasks.poll_stitch_inspections",
        # V3: Cut Accuracy Check -- timeout stale Pending inspections
        "alice_shop_floor.alice_shop_floor.tasks.poll_cut_inspections",
        # V4: Final Garment Inspector -- timeout stale Pending inspections
        "alice_shop_floor.alice_shop_floor.tasks.poll_final_inspections",
        # V6: Press QC Inspector -- timeout stale Pending inspections
        "alice_shop_floor.alice_shop_floor.tasks.poll_press_inspections",
        # Machine Driver Layer — ping all active machines, alert on offline
        "alice_shop_floor.alice_shop_floor.tasks.ping_all_machines",
    ],
    "every_30_minutes": [
        # Decoration Engine — alert on DigitizingQueue entries stuck >4h
        "alice_shop_floor.alice_shop_floor.tasks.run_digitizing_queue_alerts",
        "alice_shop_floor.alice_shop_floor.tasks.run_pick_to_bin_auto_assign",
        # Module 2: Full line balance snapshot + recommendations
        "alice_shop_floor.alice_shop_floor.tasks.run_line_balance_snapshot",
        # Module 10: WIP Bottleneck Detector -- live queue depth snapshot
        "alice_shop_floor.alice_shop_floor.tasks.run_wip_bottleneck_check",
        # SanMar: refresh live stock cache
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_stock_cache",
    ],
    "hourly": [
        # Escalate orders stuck in a stage for too long
        "alice_shop_floor.alice_shop_floor.tasks.escalate_stalled_orders",
        # Module 7: Downtime Root-Cause AI -- hourly intelligence snapshot
        "alice_shop_floor.alice_shop_floor.tasks.run_downtime_intelligence",
    ],
    "daily": [
        # Decoration Engine — 24h damage summary log
        "alice_shop_floor.alice_shop_floor.tasks.run_decoration_damage_daily_summary",
        # Module 1: Mid-week running pay tally -- updates unfinalized summaries
        "alice_shop_floor.alice_shop_floor.tasks.recalculate_pay_daily",
        # V5: Defect Intelligence -- 7-day rolling report
        "alice_shop_floor.alice_shop_floor.tasks.generate_daily_defect_intelligence",
        # SanMar: sync catalog (styles + items)
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_catalog_sync",
        # SanMar: sync pricing into ERPNext price list
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_pricing_sync",
    ],
    "cron": {
        # Module 1: Full week pay calculation every Sunday at 23:00
        "0 23 * * 0": [
            "alice_shop_floor.alice_shop_floor.tasks.calculate_weekly_pay",
        ],
        # Module 6: Skill profiles recalculated every Sunday at 22:00
        "0 22 * * 0": [
            "alice_shop_floor.alice_shop_floor.tasks.update_skill_profiles_weekly",
        ],
        # Module 9: ESG weekly report every Sunday at 21:00
        "0 21 * * 0": [
            "alice_shop_floor.alice_shop_floor.tasks.generate_weekly_esg_report",
        ],
    },
}

# ------------------------------------------------------------------
# Website / desk pages
# ------------------------------------------------------------------

page_js = {
    "shop-floor-dashboard": "public/js/shop_floor_dashboard.js",
    "sewing-floor-view":    "public/js/sewing_floor_view.js",
    "sewing-bin-scan":      "public/js/sewing_bin_scan.js",
    "piece-picker":         "public/js/piece_picker.js",
    "solid-cut-entry":      "public/js/solid_cut_entry.js",
    "dtf-press-station":    "public/js/dtf_press_station.js",   # Task #64
    "dtf-print-station":    "public/js/dtf_print_station.js",   # Task #65
    "dtg-print-station":    "public/js/dtg_print_station.js",   # Task #66
    "emb-station":          "public/js/emb_station.js",          # Task #67
    "alice-os-screen":    "public/js/alice_os_screen.js",    # Task #79
}

# ------------------------------------------------------------------
# Fixtures (export these doctypes with the app)
# ------------------------------------------------------------------

fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [["dt", "in", ["Work Order", "Employee", "Job Card", "Item"]]]
    },
    {
        "doctype": "Workstation",
        "filters": [["name", "in", ["DTG Station", "DTF Print Station", "DTF Heat Press", "Embroidery Station"]]]
    },
    {
        "doctype": "Quality Inspection Template",
        "filters": [["name", "in", ["DTG Quality Check", "DTF Quality Check", "Embroidery Quality Check"]]]
    },
    {
        "doctype": "Incentive Pay Rule",
        "filters": [["is_active", "=", 1]]
    },
    {
        "doctype": "Fabric Inspection Config",
        "filters": []
    },
    {
        "doctype": "Stitch Inspection Config",
        "filters": []
    },
    {
        "doctype": "Cut Inspection Config",
        "filters": []
    },
    {
        "doctype": "Final Inspection Config",
        "filters": []
    },
    {
        "doctype": "Press Inspection Config",
        "filters": []
    },
    {
        "doctype": "ESG Target Config",
        "filters": []
    },
    {
        "doctype": "Stage Throughput Target",
        "filters": []
    },
    {
        "doctype": "Downtime Cause Category",
        "filters": [["is_active", "=", 1]]
    },
    {
        "doctype": "ALICE Settings",
        "filters": []
    },
    {
        "doctype": "Piece Storage Location",
        "filters": [["is_active", "=", 1]]
    },
    {
        "doctype": "Sewing Bin Assignment",
        "filters": []
    },
    {
        "doctype": "Solid Fabric Cut Log",
        "filters": [["status", "in", ["Confirmed", "Bridge Alert"]]]
    },
    {
        "doctype": "Machine Config",
        "filters": [["is_active", "=", 1]]
    },
    {
        "doctype": "Machine Operator Certification",
        "filters": [["is_active", "=", 1]]
    },
    {
        "doctype": "Operator Quality Log",
        "filters": [["creation", ">=", "2026-01-01"]]
    },
    # Task #78: Pattern sizing
    {
        "doctype": "Size Stream",
        "filters": [["is_active", "=", 1]]
    },
    {
        "doctype": "Fit Model",
        "filters": [["is_active", "=", 1]]
    },
    # SanMar integration
    {
        "doctype": "SanMar Config",
        "filters": []
    },
    {
        "doctype": "SanMar Style Map",
        "filters": [["is_active", "=", 1]]
    },
]
