/**
 * ALICE OS — Main Screen
 * ======================
 * Wall-mounted TV display for the ZAZFIT shop floor.
 *
 * Layout (full viewport, dark):
 *   ┌─────────────────────────────────────────────────────────┐
 *   │  HEADER: logo | shift stats | live clock               │
 *   ├──────────────┬──────────────────────┬───────────────────┤
 *   │  WO QUEUE    │   WIP PIPELINE       │  MACHINE STATUS   │
 *   │  (left col)  │   (center, bar viz)  │  (right col)      │
 *   ├──────────────┴──────────────────────┴───────────────────┤
 *   │  ALERT TICKER  |  OPERATOR PACE LEADERS                 │
 *   └─────────────────────────────────────────────────────────┘
 *
 * Data refresh: full pull every 30 s.
 * Realtime patches: bottleneck_alert, machine_offline,
 *   pace_alert, inspection_failed, stage_advanced.
 *
 * Brand compliant: no "size" — always "fit".
 */

frappe.pages["alice-os-screen"].on_page_load = function (wrapper) {

    // Kill default Frappe page chrome — we own the full viewport.
    $(wrapper).find(".page-head").hide();

    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "ALICE OS",
        single_column: true,
    });

    // Inject Chart.js for WIP pipeline bars
    if (!window.Chart) {
        var s = document.createElement("script");
        s.src = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js";
        s.onload = function () { AliceOS.init(wrapper); };
        document.head.appendChild(s);
    } else {
        AliceOS.init(wrapper);
    }
};

// ─────────────────────────────────────────────────────────────────────────────
// Global styles injected once
// ─────────────────────────────────────────────────────────────────────────────
(function injectStyles() {
    if (document.getElementById("alice-os-styles")) return;
    var css = `
    /* ── Reset / fullscreen ─────────────────────────────────── */
    #alice-os-root {
        position: fixed; inset: 0;
        background: #0a0c10;
        color: #e8eaf0;
        font-family: "Inter", "Segoe UI", sans-serif;
        display: flex; flex-direction: column;
        overflow: hidden;
        z-index: 9999;
    }

    /* ── Header ─────────────────────────────────────────────── */
    #aos-header {
        display: flex; align-items: center;
        padding: 0 28px;
        height: 68px;
        background: #0d1117;
        border-bottom: 2px solid #1e2736;
        flex-shrink: 0;
    }
    #aos-logo {
        font-size: 22px; font-weight: 800; letter-spacing: 2px;
        color: #fff;
        margin-right: 12px;
    }
    #aos-logo span { color: #3b82f6; }
    #aos-shift-stats {
        display: flex; gap: 32px; margin-left: 28px;
    }
    .aos-stat {
        display: flex; flex-direction: column; align-items: center;
    }
    .aos-stat-val {
        font-size: 26px; font-weight: 700; line-height: 1;
        color: #f0f4ff;
    }
    .aos-stat-lbl {
        font-size: 10px; font-weight: 500; letter-spacing: 1.2px;
        color: #6b7a99; text-transform: uppercase; margin-top: 2px;
    }
    #aos-clock {
        margin-left: auto;
        font-size: 36px; font-weight: 300; letter-spacing: 3px;
        color: #a0aec0; font-variant-numeric: tabular-nums;
    }
    #aos-date {
        font-size: 12px; color: #6b7a99; letter-spacing: 1px; text-align: right;
    }

    /* ── Main body ──────────────────────────────────────────── */
    #aos-body {
        display: flex; flex: 1; min-height: 0;
        gap: 2px; background: #0a0c10;
    }

    /* ── Columns ────────────────────────────────────────────── */
    .aos-col {
        display: flex; flex-direction: column;
        background: #0d1117;
        overflow: hidden;
    }
    #aos-col-left  { width: 340px; flex-shrink: 0; border-right: 2px solid #1e2736; }
    #aos-col-mid   { flex: 1; border-right: 2px solid #1e2736; }
    #aos-col-right { width: 280px; flex-shrink: 0; }

    .aos-col-header {
        padding: 14px 18px 10px;
        font-size: 10px; font-weight: 700; letter-spacing: 2px;
        color: #3b82f6; text-transform: uppercase;
        border-bottom: 1px solid #1e2736;
        flex-shrink: 0;
    }

    /* ── WO Queue (left) ────────────────────────────────────── */
    #aos-wo-list {
        flex: 1; overflow-y: auto; padding: 8px 0;
    }
    #aos-wo-list::-webkit-scrollbar { width: 4px; }
    #aos-wo-list::-webkit-scrollbar-thumb { background: #1e2736; border-radius: 2px; }

    .aos-wo-row {
        display: flex; flex-direction: column;
        padding: 10px 18px;
        border-bottom: 1px solid #131820;
        cursor: default;
        transition: background .15s;
    }
    .aos-wo-row:hover { background: #111827; }
    .aos-wo-row.has-flag { border-left: 3px solid #ef4444; }
    .aos-wo-row.urgent   { border-left: 3px solid #f59e0b; }

    .aos-wo-top {
        display: flex; justify-content: space-between; align-items: center;
    }
    .aos-wo-name {
        font-size: 13px; font-weight: 700; color: #e2e8f0;
        font-variant-numeric: tabular-nums;
    }
    .aos-wo-badge {
        font-size: 9px; font-weight: 700; letter-spacing: .8px;
        padding: 2px 7px; border-radius: 3px;
        text-transform: uppercase;
    }
    .badge-sewing  { background: #1e3a5f; color: #60a5fa; }
    .badge-dtg     { background: #1e3050; color: #818cf8; }
    .badge-dtf     { background: #2d1b4e; color: #a78bfa; }
    .badge-emb     { background: #3b1f2b; color: #f472b6; }
    .badge-final   { background: #14302a; color: #34d399; }
    .badge-pack    { background: #2d2b0e; color: #fbbf24; }
    .badge-default { background: #1a2030; color: #94a3b8; }

    .aos-wo-mid {
        display: flex; justify-content: space-between;
        margin-top: 4px;
    }
    .aos-wo-customer {
        font-size: 11px; color: #94a3b8;
        max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .aos-wo-qty {
        font-size: 11px; color: #64748b;
    }
    .aos-wo-flags {
        display: flex; gap: 5px; margin-top: 5px; flex-wrap: wrap;
    }
    .aos-flag {
        font-size: 9px; padding: 1px 6px; border-radius: 2px;
        font-weight: 700; letter-spacing: .5px; text-transform: uppercase;
    }
    .flag-qc-fail  { background: #450a0a; color: #fca5a5; }
    .flag-stalled  { background: #431407; color: #fdba74; }
    .flag-no-deco  { background: #1c1c35; color: #a5b4fc; }

    /* ── WIP Pipeline (center) ──────────────────────────────── */
    #aos-pipeline-wrap {
        flex: 1; display: flex; flex-direction: column;
        padding: 18px 22px;
        min-height: 0;
    }
    #aos-pipeline-chart-wrap {
        flex: 1; min-height: 0; position: relative;
    }

    .aos-bottleneck-list {
        margin-top: 12px;
        display: flex; flex-wrap: wrap; gap: 8px;
        flex-shrink: 0;
    }
    .aos-bn-chip {
        font-size: 11px; font-weight: 700;
        padding: 5px 12px; border-radius: 4px;
        background: #450a0a; color: #fca5a5;
        border: 1px solid #7f1d1d;
        display: flex; align-items: center; gap: 6px;
    }
    .aos-bn-chip .bn-dot {
        width: 6px; height: 6px; border-radius: 50%;
        background: #ef4444; animation: aos-blink 1s infinite;
    }

    /* ── Machine Status (right) ─────────────────────────────── */
    #aos-machine-list {
        flex: 1; overflow-y: auto; padding: 10px 0;
    }
    .aos-machine-row {
        display: flex; align-items: center; gap: 12px;
        padding: 12px 18px;
        border-bottom: 1px solid #131820;
    }
    .aos-machine-icon { font-size: 22px; line-height: 1; }
    .aos-machine-info { flex: 1; min-width: 0; }
    .aos-machine-name {
        font-size: 13px; font-weight: 700; color: #e2e8f0;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .aos-machine-sub {
        font-size: 10px; color: #64748b; margin-top: 1px;
    }
    .aos-machine-dot {
        width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0;
    }
    .dot-online  { background: #22c55e; box-shadow: 0 0 8px #22c55e80; }
    .dot-idle    { background: #f59e0b; box-shadow: 0 0 6px #f59e0b60; }
    .dot-offline { background: #ef4444; animation: aos-blink 1.4s infinite; }
    .dot-unknown { background: #374151; }

    .aos-machine-jobs {
        font-size: 11px; font-weight: 700; color: #60a5fa;
        text-align: right; min-width: 28px;
    }

    /* ── Footer bar ─────────────────────────────────────────── */
    #aos-footer {
        height: 56px; flex-shrink: 0;
        background: #0d1117;
        border-top: 2px solid #1e2736;
        display: flex; align-items: center;
        overflow: hidden;
    }
    #aos-ticker-label {
        padding: 0 14px 0 18px;
        font-size: 10px; font-weight: 700; letter-spacing: 2px;
        color: #3b82f6; text-transform: uppercase; white-space: nowrap;
        border-right: 1px solid #1e2736;
    }
    #aos-ticker-track {
        flex: 1; overflow: hidden; position: relative; height: 56px;
    }
    #aos-ticker-inner {
        display: flex; align-items: center;
        white-space: nowrap;
        position: absolute; top: 50%; transform: translateY(-50%);
        animation: aos-scroll linear infinite;
    }
    .aos-tick-item {
        display: inline-flex; align-items: center; gap: 8px;
        padding: 0 32px;
        font-size: 13px; color: #cbd5e1;
    }
    .tick-alert { color: #fca5a5; }
    .tick-ok    { color: #86efac; }
    .tick-warn  { color: #fde68a; }

    #aos-pace-panel {
        width: 340px; flex-shrink: 0;
        border-left: 1px solid #1e2736;
        display: flex; align-items: center;
        padding: 0 14px; gap: 16px; overflow: hidden;
    }
    .aos-pace-label {
        font-size: 9px; font-weight: 700; letter-spacing: 2px;
        color: #6b7a99; text-transform: uppercase; white-space: nowrap;
    }
    .aos-pace-ops { display: flex; gap: 10px; overflow: hidden; }
    .aos-pace-op {
        display: flex; flex-direction: column; align-items: center;
        min-width: 52px;
    }
    .aos-pace-name {
        font-size: 9px; color: #64748b; text-align: center;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 52px;
    }
    .aos-pace-val {
        font-size: 16px; font-weight: 700; line-height: 1;
    }
    .pace-hot    { color: #ef4444; }
    .pace-ok     { color: #22c55e; }
    .pace-warn   { color: #f59e0b; }

    /* ── Animations ─────────────────────────────────────────── */
    @keyframes aos-blink {
        0%, 100% { opacity: 1; }
        50%       { opacity: .25; }
    }
    @keyframes aos-scroll {
        from { transform: translate(0, -50%); }
        to   { transform: translate(-50%, -50%); }
    }
    `;
    var el = document.createElement("style");
    el.id = "alice-os-styles";
    el.textContent = css;
    document.head.appendChild(el);
})();

// ─────────────────────────────────────────────────────────────────────────────
// AliceOS controller
// ─────────────────────────────────────────────────────────────────────────────
var AliceOS = {

    REFRESH_MS: 30000,
    _timer: null,
    _pipeline_chart: null,
    _last_data: null,

    STAGES: [
        "Fabric Inspect", "Cutting", "Bundling", "Sewing",
        "DTF Print", "DTF Press", "DTG Print", "Embroidery",
        "Final QC", "Pack"
    ],

    STAGE_COLORS: [
        "#3b82f6", "#f59e0b", "#ef4444", "#22c55e",
        "#a78bfa", "#8b5cf6", "#60a5fa", "#ec4899",
        "#34d399", "#fbbf24"
    ],

    MACHINE_ICONS: {
        "DTG":        "👕",
        "DTF":        "🖨",
        "Embroidery": "🪡",
        "default":    "⚙️",
    },

    STAGE_BADGE: {
        "Sewing":     "badge-sewing",
        "DTG Print":  "badge-dtg",
        "DTF Print":  "badge-dtf",
        "DTF Press":  "badge-dtf",
        "Embroidery": "badge-emb",
        "Final QC":   "badge-final",
        "Pack":       "badge-pack",
    },

    // ── Bootstrap ────────────────────────────────────────────────────────────
    init: function (wrapper) {
        var me = this;
        me.wrapper = wrapper;
        me._build_skeleton();
        me._bind_realtime();
        me._start_clock();
        me.refresh();
        me._timer = setInterval(function () { me.refresh(); }, me.REFRESH_MS);
    },

    // ── DOM skeleton ─────────────────────────────────────────────────────────
    _build_skeleton: function () {
        var root = document.createElement("div");
        root.id = "alice-os-root";
        root.innerHTML = `
        <!-- HEADER -->
        <div id="aos-header">
            <div id="aos-logo">ALICE<span>OS</span></div>
            <div style="width:1px;height:34px;background:#1e2736;margin:0 20px;"></div>
            <div id="aos-shift-stats">
                <div class="aos-stat">
                    <div class="aos-stat-val" id="aos-stat-wo">—</div>
                    <div class="aos-stat-lbl">Active WOs</div>
                </div>
                <div class="aos-stat">
                    <div class="aos-stat-val" id="aos-stat-units">—</div>
                    <div class="aos-stat-lbl">Units in WIP</div>
                </div>
                <div class="aos-stat">
                    <div class="aos-stat-val" id="aos-stat-bottlenecks" style="color:#ef4444;">—</div>
                    <div class="aos-stat-lbl">Bottlenecks</div>
                </div>
                <div class="aos-stat">
                    <div class="aos-stat-val" id="aos-stat-alerts" style="color:#f59e0b;">—</div>
                    <div class="aos-stat-lbl">Alerts</div>
                </div>
                <div class="aos-stat">
                    <div class="aos-stat-val" id="aos-stat-machines" style="color:#22c55e;">—</div>
                    <div class="aos-stat-lbl">Machines Online</div>
                </div>
            </div>
            <div style="margin-left:auto;text-align:right;">
                <div id="aos-clock">—</div>
                <div id="aos-date" style="font-size:11px;color:#6b7a99;"></div>
            </div>
        </div>

        <!-- MAIN BODY -->
        <div id="aos-body">

            <!-- LEFT: Active Work Orders -->
            <div class="aos-col" id="aos-col-left">
                <div class="aos-col-header">Active Work Orders</div>
                <div id="aos-wo-list"><div style="padding:24px;color:#374151;font-size:13px;">Loading…</div></div>
            </div>

            <!-- CENTER: WIP Pipeline -->
            <div class="aos-col" id="aos-col-mid">
                <div class="aos-col-header">WIP Pipeline — Units per Stage</div>
                <div id="aos-pipeline-wrap">
                    <div id="aos-pipeline-chart-wrap">
                        <canvas id="aos-pipeline-canvas"></canvas>
                    </div>
                    <div class="aos-bottleneck-list" id="aos-bottleneck-chips"></div>
                </div>
            </div>

            <!-- RIGHT: Machine Status -->
            <div class="aos-col" id="aos-col-right">
                <div class="aos-col-header">Machine Status</div>
                <div id="aos-machine-list"><div style="padding:24px;color:#374151;font-size:13px;">Loading…</div></div>
            </div>

        </div>

        <!-- FOOTER: alert ticker + pace leaders -->
        <div id="aos-footer">
            <div id="aos-ticker-label">⚡ Live Alerts</div>
            <div id="aos-ticker-track">
                <div id="aos-ticker-inner"></div>
            </div>
            <div id="aos-pace-panel">
                <div class="aos-pace-label">Pace Leaders</div>
                <div class="aos-pace-ops" id="aos-pace-ops"></div>
            </div>
        </div>
        `;
        this.wrapper.appendChild(root);
        this._root = root;
    },

    // ── Clock ────────────────────────────────────────────────────────────────
    _start_clock: function () {
        var DAYS  = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
        var MONTHS= ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
        function tick() {
            var now = new Date();
            var hh  = String(now.getHours()).padStart(2,"0");
            var mm  = String(now.getMinutes()).padStart(2,"0");
            var ss  = String(now.getSeconds()).padStart(2,"0");
            var el  = document.getElementById("aos-clock");
            var de  = document.getElementById("aos-date");
            if (el) el.textContent = hh + ":" + mm + ":" + ss;
            if (de) de.textContent = DAYS[now.getDay()] + ", " + MONTHS[now.getMonth()] + " " + now.getDate();
        }
        tick();
        setInterval(tick, 1000);
    },

    // ── Data refresh ─────────────────────────────────────────────────────────
    refresh: function () {
        var me = this;
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_os_screen_data",
            callback: function (r) {
                if (!r || !r.message) return;
                me._last_data = r.message;
                me._render(r.message);
            },
            error: function () {
                me._push_alert_tick("⚠ Data refresh failed", "tick-alert");
            }
        });
    },

    // ── Master render ─────────────────────────────────────────────────────────
    _render: function (d) {
        this._render_header_stats(d);
        this._render_wo_list(d.work_orders || []);
        this._render_pipeline(d.pipeline || []);
        this._render_bottleneck_chips(d.bottlenecks || []);
        this._render_machines(d.machines || []);
        this._render_ticker(d.alerts || []);
        this._render_pace(d.pace_leaders || []);
    },

    // ── Header stats ─────────────────────────────────────────────────────────
    _render_header_stats: function (d) {
        var set = function (id, val) {
            var el = document.getElementById(id);
            if (el) el.textContent = (val === null || val === undefined) ? "—" : val;
        };
        set("aos-stat-wo",          d.total_wo);
        set("aos-stat-units",       d.total_units_in_wip);
        set("aos-stat-bottlenecks", d.bottleneck_count);
        set("aos-stat-alerts",      d.alert_count);
        set("aos-stat-machines",    d.machines_online);
    },

    // ── Work Order list ───────────────────────────────────────────────────────
    _render_wo_list: function (wos) {
        var el = document.getElementById("aos-wo-list");
        if (!wos.length) {
            el.innerHTML = '<div style="padding:32px 18px;color:#374151;font-size:13px;">No active Work Orders</div>';
            return;
        }
        var me = this;
        var html = wos.map(function (wo) {
            var badge_cls = me.STAGE_BADGE[wo.current_stage] || "badge-default";
            var row_cls   = "";
            if (wo.has_qc_flag || wo.has_defect) row_cls = "has-flag";
            else if (wo.is_stalled)               row_cls = "urgent";

            var flags = [];
            if (wo.has_qc_flag || wo.has_defect) flags.push('<span class="aos-flag flag-qc-fail">QC Flag</span>');
            if (wo.is_stalled)    flags.push('<span class="aos-flag flag-stalled">Stalled</span>');
            if (wo.no_deco_routed) flags.push('<span class="aos-flag flag-no-deco">Awaiting Route</span>');
            var flags_html = flags.length ? '<div class="aos-wo-flags">' + flags.join("") + '</div>' : "";

            var stage_label = wo.current_stage || "—";
            var cust  = wo.customer_name ? frappe.utils.escape_html(wo.customer_name) : "—";
            var qty   = wo.qty ? wo.qty + " pc" : "";
            var name  = frappe.utils.escape_html(wo.name);

            return `
            <div class="aos-wo-row ${row_cls}" title="${name}">
                <div class="aos-wo-top">
                    <div class="aos-wo-name">${name}</div>
                    <div class="aos-wo-badge ${badge_cls}">${frappe.utils.escape_html(stage_label)}</div>
                </div>
                <div class="aos-wo-mid">
                    <div class="aos-wo-customer">${cust}</div>
                    <div class="aos-wo-qty">${qty}</div>
                </div>
                ${flags_html}
            </div>`;
        }).join("");
        el.innerHTML = html;
    },

    // ── WIP Pipeline chart ───────────────────────────────────────────────────
    _render_pipeline: function (pipeline) {
        var me = this;
        var labels = [], vals = [], colors = [];
        var stage_map = {};
        (pipeline || []).forEach(function (row) { stage_map[row.stage] = row.count; });

        me.STAGES.forEach(function (s, i) {
            labels.push(s);
            vals.push(stage_map[s] || 0);
            colors.push(me.STAGE_COLORS[i] || "#3b82f6");
        });

        var canvas = document.getElementById("aos-pipeline-canvas");
        if (!canvas) return;
        var ctx = canvas.getContext("2d");

        if (me._pipeline_chart) {
            me._pipeline_chart.data.datasets[0].data   = vals;
            me._pipeline_chart.data.datasets[0].backgroundColor = colors;
            me._pipeline_chart.update("none");
            return;
        }

        me._pipeline_chart = new Chart(ctx, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [{
                    label: "Units",
                    data: vals,
                    backgroundColor: colors,
                    borderRadius: 4,
                    borderSkipped: false,
                    maxBarThickness: 48,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 400 },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: "#1e2736",
                        titleColor: "#94a3b8",
                        bodyColor: "#f0f4ff",
                        callbacks: {
                            label: function (ctx) { return " " + ctx.parsed.y + " units"; }
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: "#94a3b8", font: { size: 11, weight: "600" },
                            maxRotation: 30,
                        },
                        grid: { color: "#1e2736" }
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            color: "#64748b", font: { size: 11 },
                            precision: 0,
                        },
                        grid: { color: "#1a2030" }
                    }
                }
            }
        });
    },

    // ── Bottleneck chips below chart ──────────────────────────────────────────
    _render_bottleneck_chips: function (bns) {
        var el = document.getElementById("aos-bottleneck-chips");
        if (!bns.length) { el.innerHTML = ""; return; }
        el.innerHTML = bns.map(function (bn) {
            return `<div class="aos-bn-chip">
                <div class="bn-dot"></div>
                ${frappe.utils.escape_html(bn.stage)} — ${bn.count} units stuck
            </div>`;
        }).join("");
    },

    // ── Machine status ────────────────────────────────────────────────────────
    _render_machines: function (machines) {
        var me = this;
        var el = document.getElementById("aos-machine-list");
        if (!machines.length) {
            el.innerHTML = '<div style="padding:24px 18px;color:#374151;font-size:13px;">No machines configured</div>';
            return;
        }
        el.innerHTML = machines.map(function (m) {
            var icon = me.MACHINE_ICONS[m.machine_type] || me.MACHINE_ICONS["default"];
            var dot_cls = {
                "Online":  "dot-online",
                "Idle":    "dot-idle",
                "Offline": "dot-offline",
            }[m.status] || "dot-unknown";
            var jobs_html = m.active_jobs > 0
                ? `<div class="aos-machine-jobs">${m.active_jobs}</div>`
                : "";
            return `
            <div class="aos-machine-row">
                <div class="aos-machine-icon">${icon}</div>
                <div class="aos-machine-info">
                    <div class="aos-machine-name">${frappe.utils.escape_html(m.name)}</div>
                    <div class="aos-machine-sub">${frappe.utils.escape_html(m.status)}${m.operator ? " · " + frappe.utils.escape_html(m.operator) : ""}</div>
                </div>
                ${jobs_html}
                <div class="aos-machine-dot ${dot_cls}"></div>
            </div>`;
        }).join("");
    },

    // ── Alert ticker ──────────────────────────────────────────────────────────
    _render_ticker: function (alerts) {
        var me = this;
        me._ticker_items = alerts.slice();
        if (!me._ticker_items.length) {
            me._ticker_items = [{ text: "All systems nominal — shop floor running", cls: "tick-ok" }];
        }
        me._rebuild_ticker();
    },

    _rebuild_ticker: function () {
        var items = this._ticker_items;
        // Duplicate for seamless looping
        var html = items.concat(items).map(function (a) {
            return `<span class="aos-tick-item ${a.cls || ""}">${a.icon || ""}${frappe.utils.escape_html(a.text)}<span style="margin-left:20px;color:#1e2736;">|</span></span>`;
        }).join("");
        var inner = document.getElementById("aos-ticker-inner");
        if (!inner) return;
        inner.innerHTML = html;
        // Set animation duration proportional to content length
        var dur = Math.max(18, items.length * 6) + "s";
        inner.style.animationDuration = dur;
    },

    _push_alert_tick: function (text, cls) {
        var me = this;
        if (!me._ticker_items) me._ticker_items = [];
        me._ticker_items.unshift({ text: text, cls: cls || "tick-alert" });
        if (me._ticker_items.length > 20) me._ticker_items.pop();
        me._rebuild_ticker();
    },

    // ── Pace leaders ──────────────────────────────────────────────────────────
    _render_pace: function (leaders) {
        var el = document.getElementById("aos-pace-ops");
        if (!leaders || !leaders.length) { el.innerHTML = ""; return; }
        el.innerHTML = leaders.slice(0, 5).map(function (op) {
            var pace_cls = op.pace_pct >= 100 ? "pace-hot" : op.pace_pct >= 80 ? "pace-warn" : "pace-ok";
            var first = (op.operator_name || "?").split(" ")[0];
            return `
            <div class="aos-pace-op">
                <div class="aos-pace-val ${pace_cls}">${op.pace_pct || 0}<span style="font-size:10px">%</span></div>
                <div class="aos-pace-name">${frappe.utils.escape_html(first)}</div>
            </div>`;
        }).join("");
    },

    // ── Realtime socket subscriptions ─────────────────────────────────────────
    _bind_realtime: function () {
        var me = this;

        frappe.realtime.on("bottleneck_alert", function (data) {
            me._push_alert_tick("⛔ Bottleneck: " + (data.stage || "?") + " — " + (data.count || "?") + " units", "tick-alert");
            me.refresh();
        });

        frappe.realtime.on("machine_offline", function (data) {
            me._push_alert_tick("🔴 Machine offline: " + (data.machine || "?"), "tick-alert");
            me.refresh();
        });

        frappe.realtime.on("pace_alert", function (data) {
            var msg = "⚡ Pace alert: " + (data.operator_name || "?") + " — " + (data.message || "check pace");
            me._push_alert_tick(msg, "tick-warn");
        });

        frappe.realtime.on("stage_advanced", function (data) {
            // Lightweight patch — re-render WO card if visible; full refresh on next cycle
            if (data.work_order) {
                me._push_alert_tick("✅ " + data.work_order + " → " + (data.new_stage || "next stage"), "tick-ok");
            }
        });

        // QC failures from any inspection module
        ["fabric", "stitch", "cut", "final", "press"].forEach(function (mod) {
            frappe.realtime.on(mod + "_inspection_failed", function (data) {
                me._push_alert_tick(
                    "❌ " + (mod.charAt(0).toUpperCase() + mod.slice(1)) + " QC fail: " + (data.work_order || "WO?"),
                    "tick-alert"
                );
                me.refresh();
            });
        });

        frappe.realtime.on("defect_intelligence_critical_alert", function (data) {
            me._push_alert_tick("🔥 Critical defect pattern detected — " + (data.message || "review defect log"), "tick-alert");
        });

        frappe.realtime.on("esg_compliance_alert", function (data) {
            me._push_alert_tick("🌱 ESG alert: " + (data.message || "check ESG targets"), "tick-warn");
        });
    },

};
