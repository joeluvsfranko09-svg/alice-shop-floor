app_name = "alice_shop_floor"
app_title = "Alice Shop Floor"
app_publisher = "Athlettia LLC"
app_description = "ALICE Shop Floor Layer -- AI-powered MES modules for ZAZFIT"
app_email = "frankoy@athlettia.com"
app_license = "MIT"

required_apps = ["frappe", "erpnext"]

after_install = "alice_shop_floor.alice_shop_floor.setup.after_install"

doc_events = {
    "Work Order": {
        "on_submit": [
            "alice_shop_floor.alice_shop_floor.doctype.production_stage_tracker.production_stage_tracker.create_tracker_for_work_order",
            "alice_shop_floor.alice_shop_floor.work_order_scheduler.create_decoration_job_cards_for_work_order",
            "alice_shop_floor.alice_shop_floor.sanmar.po_creator.check_and_auto_po_for_work_order",
        ],
    },
    "Job Card": {
        "on_submit": "alice_shop_floor.alice_shop_floor.decoration_engine.on_job_card_submit",
        "on_update":  "alice_shop_floor.alice_shop_floor.decoration_engine.on_job_card_update",
    },
}

scheduler_events = {
    "every_5_minutes": [
        "alice_shop_floor.alice_shop_floor.tasks.run_decoration_routing_check",
        "alice_shop_floor.alice_shop_floor.tasks.run_pace_check",
        "alice_shop_floor.alice_shop_floor.tasks.poll_fabric_inspections",
        "alice_shop_floor.alice_shop_floor.tasks.poll_stitch_inspections",
        "alice_shop_floor.alice_shop_floor.tasks.poll_cut_inspections",
        "alice_shop_floor.alice_shop_floor.tasks.poll_final_inspections",
        "alice_shop_floor.alice_shop_floor.tasks.poll_press_inspections",
        "alice_shop_floor.alice_shop_floor.tasks.ping_all_machines",
    ],
    "every_30_minutes": [
        "alice_shop_floor.alice_shop_floor.tasks.run_digitizing_queue_alerts",
        "alice_shop_floor.alice_shop_floor.tasks.run_pick_to_bin_auto_assign",
        "alice_shop_floor.alice_shop_floor.tasks.run_line_balance_snapshot",
        "alice_shop_floor.alice_shop_floor.tasks.run_wip_bottleneck_check",
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_stock_cache",
    ],
    "hourly": [
        "alice_shop_floor.alice_shop_floor.tasks.escalate_stalled_orders",
        "alice_shop_floor.alice_shop_floor.tasks.run_downtime_intelligence",
    ],
    "daily": [
        "alice_shop_floor.alice_shop_floor.tasks.run_decoration_damage_daily_summary",
        "alice_shop_floor.alice_shop_floor.tasks.recalculate_pay_daily",
        "alice_shop_floor.alice_shop_floor.tasks.generate_daily_defect_intelligence",
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_catalog_sync",
        "alice_shop_floor.alice_shop_floor.tasks.run_sanmar_pricing_sync",
    ],
}

page_js = {
    "shop-floor-dashboard": "public/js/shop_floor_dashboard.js",
    "sewing-floor-view":    "public/js/sewing_floor_view.js",
    "sewing-bin-scan":      "public/js/sewing_bin_scan.js",
    "piece-picker":         "public/js/piece_picker.js",
    "solid-cut-entry":      "public/js/solid_cut_entry.js",
    "dtf-press-station":    "public/js/dtf_press_station.js",
    "dtf-print-station":    "public/js/dtf_print_station.js",
    "dtg-print-station":    "public/js/dtg_print_station.js",
    "emb-station":          "public/js/emb_station.js",
    "alice-os-screen":      "public/js/alice_os_screen.js",
}
