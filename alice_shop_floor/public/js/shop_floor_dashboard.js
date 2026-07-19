/**
 * ALICE Shop Floor Supervisor Dashboard
 * ======================================
 * Live view of:
 *   - WIP queue depth per stage (bar chart + congestion scores)
 *   - Active bottleneck alerts
 *   - Open downtime events
 *   - V1-V4 inspection gate status for active Work Orders
 *   - Skill leaderboard (top 10 operators)
 *
 * Realtime events via frappe.realtime:
 *   stage_advanced, bottleneck_alert, downtime_recurring_alert,
 *   fabric/stitch/cut/final inspection_failed/_timeout,
 *   defect_intelligence_critical_alert, esg_compliance_alert
 *
 * Refresh cadence: full data refresh every 60 seconds + realtime patches.
 */

frappe.pages["shop-floor-dashboard"].on_page_load = function (wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "ALICE Shop Floor",
        single_column: true,
    });

    // ── Inject Chart.js from CDN ──────────────────────────────────────────────
    if (!window.Chart) {
        var script = document.createElement("script");
        script.src = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js";
        script.onload = function () { alice_dashboard.init(page, wrapper); };
        document.head.appendChild(script);
    } else {
        alice_dashboard.init(page, wrapper);
    }
};

var alice_dashboard = {

    chart: null,
    refresh_timer: null,
    STAGES: [
        "Fabric Inspection", "Cutting", "Bundling",
        "Sewing", "DTF Print", "DTF Press",
        "DTG Print", "Embroidery", "Final QC", "Pack"
    ],
    STAGE_COLORS: {
        "Fabric Inspection": "#4e79a7",
        "Cutting":           "#f28e2b",
        "Bundling":          "#e15759",
        "Sewing":            "#76b7b2",
        "DTF Print":         "#9b59b6",
        "DTF Press":         "#8e44ad",
        "DTG Print":         "#2980b9",
        "Embroidery":        "#c0392b",
        "Final QC":          "#59a14f",
        "Pack":              "#edc948",
    },

    // Decoration station page routes + display names
    DECO_STATIONS: [
        { page: "dtf-print-station",  label: "DTF Print",    icon: "🖨",  machine_method: "DTF" },
        { page: "dtf-press-station",  label: "DTF Press",    icon: "🔥",  machine_method: "DTF" },
        { page: "dtg-print-station",  label: "DTG Print",    icon: "👕",  machine_method: "DTG" },
        { page: "emb-station",        label: "Embroidery",   icon: "🪡",  machine_method: "Embroidery" },
    ],
    BOTTLENECK_COLOR: "#e15759",

    // ── Initialise layout ────────────────────────────────────────────────────
    init: function (page, wrapper) {
        var me = this;
        me.page    = page;
        me.wrapper = wrapper;

        // Toolbar buttons
        page.add_button(__("Refresh Now"), function () { me.refresh_all(); }, "btn-default");
        page.add_button(__("Log Downtime"), function () {
            frappe.new_doc("Downtime Event");
        }, "btn-warning");

        // Build layout
        var $body = $(wrapper).find(".page-content");
        $body.html(me._layout_html());

        // Bind realtime events
        me._bind_realtime();

        // Initial load + auto-refresh every 60s
        me.refresh_all();
        me.refresh_timer = setInterval(function () { me.refresh_all(); }, 60000);
    },

    // ── Full data refresh ────────────────────────────────────────────────────
    refresh_all: function () {
        var me = this;
        me._set_status("Refreshing…");
        Promise.all([
            me._fetch("get_current_wip"),
            me._fetch("get_open_bottleneck_alerts"),
            me._fetch("get_open_downtime_events"),
            me._fetch("get_skill_leaderboard", {limit: 10}),
            me._fetch("get_active_wo_gates"),
            me._fetch("machine_list"),
        ]).then(function (results) {
            me._render_wip_chart(results[0].message || {});
            me._render_bottleneck_alerts(results[1].message || []);
            me._render_downtime_events(results[2].message || []);
            me._render_skill_leaderboard(results[3].message || []);
            me._render_wo_gates(results[4].message || []);
            me._render_deco_stations(results[5].message || []);
            me._set_status("Last updated: " + frappe.datetime.now_time());
        }).catch(function (err) {
            me._set_status("Error loading data — retrying next cycle.");
            console.error("[ALICE Dashboard]", err);
        });
    },

    // ── WIP chart ────────────────────────────────────────────────────────────
    _render_wip_chart: function (wip_data) {
        var me    = this;
        var ctx   = document.getElementById("alice-wip-chart");
        if (!ctx) return;

        var labels = me.STAGES;
        var counts  = labels.map(function (s) {
            return (wip_data[s] || {}).wip || 0;
        });
        var targets = labels.map(function (s) {
            return (wip_data[s] || {}).target || 5;
        });
        var scores  = labels.map(function (s) {
            return (wip_data[s] || {}).score || 0;
        });
        var bgColors = labels.map(function (s, i) {
            return scores[i] >= 1.5 ? me.BOTTLENECK_COLOR : me.STAGE_COLORS[s] || "#aaa";
        });

        if (me.chart) {
            me.chart.data.datasets[0].data   = counts;
            me.chart.data.datasets[0].backgroundColor = bgColors;
            me.chart.data.datasets[1].data   = targets;
            me.chart.update();
        } else {
            me.chart = new Chart(ctx, {
                type: "bar",
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: "WIP Count",
                            data: counts,
                            backgroundColor: bgColors,
                        },
                        {
                            label: "Target WIP",
                            data: targets,
                            type: "line",
                            borderColor: "#888",
                            borderDash: [6, 3],
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: false,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    plugins: {
                        legend: { position: "bottom" },
                        title:  { display: true, text: "Live WIP Queue Depth by Stage" },
                    },
                    scales: {
                        y: { beginAtZero: true, title: { display: true, text: "Work Orders" } },
                    },
                },
            });
        }

        // Congestion score badges
        var $badges = $("#alice-congestion-badges").empty();
        labels.forEach(function (s, i) {
            var sc  = scores[i].toFixed(2);
            var cls = scores[i] >= 1.5 ? "red" : scores[i] >= 1.0 ? "orange" : "green";
            $badges.append(
                '<span class="indicator-pill ' + cls + '" style="margin:2px 4px">' +
                s.split(" ")[0] + ": " + sc + "×</span>"
            );
        });
    },

    // ── Bottleneck alerts ────────────────────────────────────────────────────
    _render_bottleneck_alerts: function (alerts) {
        var $el = $("#alice-bottleneck-alerts").empty();
        if (!alerts.length) {
            $el.html('<div class="text-muted small">No active bottleneck alerts.</div>');
            return;
        }
        alerts.forEach(function (a) {
            $el.append(
                '<div class="alice-alert-row">' +
                '<span class="indicator-pill red">' + a.stage + '</span> ' +
                '<strong>' + a.wip_count + ' WOs</strong> vs target ' + a.target_wip +
                ' &nbsp;|&nbsp; Score: ' + (a.congestion_score || 0).toFixed(2) +
                ' &nbsp;|&nbsp; <em>' + (a.root_cause || "Unknown") + '</em>' +
                '<br><small class="text-muted">' + (a.recommendation || "") + '</small>' +
                '<a class="btn btn-xs btn-default" style="float:right;margin-top:4px" ' +
                'data-alert="' + a.name + '" onclick="alice_dashboard._resolve_alert(this)">Resolve</a>' +
                '</div><hr style="margin:6px 0">'
            );
        });
    },

    _resolve_alert: function (el) {
        var name = $(el).data("alert");
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.resolve_bottleneck_alert",
            args: { alert_name: name },
            callback: function () { alice_dashboard.refresh_all(); },
        });
    },

    // ── Open downtime events ─────────────────────────────────────────────────
    _render_downtime_events: function (events) {
        var $el = $("#alice-downtime-events").empty();
        if (!events.length) {
            $el.html('<div class="text-muted small">No open downtime events.</div>');
            return;
        }
        events.forEach(function (e) {
            var dur = e.duration_minutes ? e.duration_minutes.toFixed(0) + " min" : "ongoing";
            $el.append(
                '<div class="alice-alert-row">' +
                '<span class="indicator-pill orange">' + e.stage + '</span> ' +
                (e.machine_id ? '<strong>' + e.machine_id + '</strong> &nbsp;|&nbsp;' : '') +
                (e.root_cause_group || "Unclassified") +
                ' &nbsp;|&nbsp; ' + dur +
                (e.work_order ? ' &nbsp;|&nbsp; WO: ' + e.work_order : '') +
                '<a class="btn btn-xs btn-default" style="float:right;margin-top:2px" ' +
                'href="/app/downtime-event/' + e.name + '">View</a>' +
                '</div><hr style="margin:6px 0">'
            );
        });
    },

    // ── Skill leaderboard ────────────────────────────────────────────────────
    _render_skill_leaderboard: function (profiles) {
        var $el = $("#alice-skill-leaderboard").empty();
        if (!profiles.length) {
            $el.html('<div class="text-muted small">No skill profiles yet.</div>');
            return;
        }
        var html = '<table class="table table-sm table-bordered" style="font-size:12px">' +
            '<thead><tr><th>#</th><th>Operator</th><th>Stage</th>' +
            '<th>Score</th><th>Trend</th><th>Training?</th></tr></thead><tbody>';
        profiles.slice(0, 10).forEach(function (p, i) {
            var trend_icon = p.trend === "Improving" ? "↑" :
                             p.trend === "Declining"  ? "↓" : "→";
            var trend_cls  = p.trend === "Improving" ? "green" :
                             p.trend === "Declining"  ? "red" : "grey";
            html += '<tr>' +
                '<td>' + (i + 1) + '</td>' +
                '<td>' + (p.operator || "") + '</td>' +
                '<td>' + (p.stage || "") + '</td>' +
                '<td><strong>' + (p.skill_score || 0).toFixed(1) + '</strong></td>' +
                '<td style="color:' + trend_cls + '">' + trend_icon + ' ' + (p.trend || "") + '</td>' +
                '<td>' + (p.needs_training ? '<span class="indicator-pill red">Yes</span>' : '') + '</td>' +
                '</tr>';
        });
        html += '</tbody></table>';
        $el.html(html);
    },

    // ── Active WO gate status ────────────────────────────────────────────────
    _render_wo_gates: function (wos) {
        var $el = $("#alice-wo-gates").empty();
        if (!wos.length) {
            $el.html('<div class="text-muted small">No active Work Orders on the floor.</div>');
            return;
        }
        var html = '<table class="table table-sm table-bordered" style="font-size:12px">' +
            '<thead><tr><th>Work Order</th><th>Stage</th>' +
            '<th>Time in Stage</th><th>V1 Fabric</th><th>V3 Cut</th>' +
            '<th>V2 Stitch</th><th>V4 Final</th></tr></thead><tbody>';
        wos.forEach(function (w) {
            var gate_cell = function (g) {
                if (!g || g === "N/A") return "<td>—</td>";
                var cls = g === "open" ? "green" : g === "pending" ? "orange" : "red";
                return '<td><span class="indicator-pill ' + cls + '">' + g + '</span></td>';
            };
            html += '<tr>' +
                '<td><a href="/app/production-stage-tracker/' + w.name + '">' + w.work_order + '</a></td>' +
                '<td>' + (w.current_stage || "") + '</td>' +
                '<td>' + (w.time_in_stage || 0).toFixed(1) + 'h</td>' +
                gate_cell(w.fabric_gate) +
                gate_cell(w.cut_gate) +
                gate_cell(w.stitch_gate) +
                gate_cell(w.final_gate) +
                '</tr>';
        });
        html += '</tbody></table>';
        $el.html(html);
    },

    // ── Decoration station quick-access panel ───────────────────────────────
    _render_deco_stations: function (machine_list) {
        var me  = this;
        var $el = $("#alice-deco-stations").empty();

        // Build a lookup: machine_method -> array of machine records
        var by_method = {};
        (machine_list || []).forEach(function (m) {
            var meth = (m.decoration_method || "").trim();
            if (!by_method[meth]) by_method[meth] = [];
            by_method[meth].push(m);
        });

        me.DECO_STATIONS.forEach(function (station) {
            var machines = by_method[station.machine_method] || [];

            // Build small online/offline dots for each machine
            var dots_html = machines.map(function (m) {
                // A machine is "online" if last_seen within the last 10 minutes
                var online = false;
                if (m.last_seen) {
                    var diff = (new Date() - new Date(m.last_seen)) / 60000; // minutes
                    online = diff < 10;
                }
                var dot_color = online ? "#27ae60" : "#e74c3c";
                var dot_title = (m.machine_id || m.name) + ": " + (online ? "Online" : "Offline");
                return '<span title="' + dot_title + '" style="display:inline-block;width:9px;height:9px;' +
                       'border-radius:50%;background:' + dot_color + ';margin-right:3px"></span>';
            }).join("");

            // All offline if no machine records
            if (!machines.length) {
                dots_html = '<span title="No machine configured" style="display:inline-block;width:9px;' +
                            'height:9px;border-radius:50%;background:#aaa;margin-right:3px"></span>';
            }

            var btn_html =
                '<button class="btn btn-sm btn-default alice-deco-btn" ' +
                'onclick="frappe.set_route(\'page\', \'' + station.page + '\')" ' +
                'style="display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:6px">' +
                '<span style="font-size:16px">' + station.icon + '</span>' +
                '<span style="font-weight:600;font-size:13px">' + station.label + '</span>' +
                '<span style="display:flex;align-items:center;gap:2px;margin-left:4px">' + dots_html + '</span>' +
                '</button>';

            $el.append(btn_html);
        });
    },

    // ── Realtime event bindings ──────────────────────────────────────────────
    _bind_realtime: function () {
        var me = this;

        var refresh_events = [
            "stage_advanced",
            "bottleneck_alert",
            "downtime_recurring_alert",
            "fabric_inspection_failed",
            "fabric_inspection_timeout",
            "stitch_inspection_failed",
            "stitch_inspection_timeout",
            "cut_inspection_timeout",
            "final_inspection_failed",
            "final_inspection_timeout",
            "defect_intelligence_critical_alert",
            "esg_compliance_alert",
            "downtime_intelligence_update",
            // Decoration machine completion events
            "dtf_film_ready",
            "dtf_press_dispatched",
            "dtg_print_complete",
            "emb_job_complete",
        ];

        refresh_events.forEach(function (evt) {
            frappe.realtime.on(evt, function (data) {
                me._show_toast(evt, data);
                // Debounce: refresh 2s after last event
                clearTimeout(me._rt_refresh_timer);
                me._rt_refresh_timer = setTimeout(function () {
                    me.refresh_all();
                }, 2000);
            });
        });
    },

    _show_toast: function (event_type, data) {
        var msg_map = {
            "stage_advanced":                 "WO " + (data.work_order || "") + " → " + (data.to_stage || ""),
            "bottleneck_alert":               "⚠ Bottleneck at " + (data.stage || ""),
            "downtime_recurring_alert":       "⚠ Recurring downtime: " + (data.cause_category || ""),
            "fabric_inspection_failed":       "Fabric inspection FAILED — WO " + (data.work_order || ""),
            "fabric_inspection_timeout":      "Fabric inspection timed out",
            "stitch_inspection_failed":       "Stitch QC FAILED — WO " + (data.work_order || ""),
            "stitch_inspection_timeout":      "Stitch inspection timed out",
            "cut_inspection_timeout":         "Cut inspection timed out",
            "final_inspection_failed":        "Final QC FAILED — WO " + (data.work_order || ""),
            "final_inspection_timeout":       "Final inspection timed out",
            "defect_intelligence_critical_alert": "⚠ Critical defects detected — see report",
            "esg_compliance_alert":           "⚠ ESG " + (data.status || "alert"),
            "downtime_intelligence_update":   "Downtime update: " + (data.total_events || 0) + " events",
            // Decoration machine events
            "dtf_film_ready":       "DTF film ready — Job Card " + (data.job_card || ""),
            "dtf_press_dispatched": "DTF press complete — WO " + (data.work_order || ""),
            "dtg_print_complete":   "DTG print complete — Job Card " + (data.job_card || ""),
            "emb_job_complete":     "Embroidery complete — Job Card " + (data.job_card || ""),
        };
        var indicator = event_type.includes("failed") || event_type.includes("bottleneck") ||
                        event_type.includes("critical") ? "red" :
                        event_type.includes("timeout") || event_type.includes("esg") ? "orange" : "blue";
        frappe.show_alert({
            message: msg_map[event_type] || event_type,
            indicator: indicator,
        }, 6);
    },

    // ── HTML layout ──────────────────────────────────────────────────────────
    _layout_html: function () {
        return [
            '<div style="padding:12px">',

            // Status bar
            '<div id="alice-status-bar" class="text-muted small" style="margin-bottom:8px">Loading…</div>',

            // Row 0: Decoration Stations quick-access
            '<div class="row" style="margin-bottom:12px">',
            '  <div class="col-md-12">',
            '    <div class="alice-card" style="padding:10px 14px">',
            '      <h5 style="margin-bottom:8px">Decoration Stations</h5>',
            '      <div id="alice-deco-stations" style="display:flex;flex-wrap:wrap;gap:8px"></div>',
            '    </div>',
            '  </div>',
            '</div>',

            // Row 1: WIP chart + congestion badges
            '<div class="row">',
            '  <div class="col-md-9">',
            '    <div class="alice-card">',
            '      <h5>WIP Queue</h5>',
            '      <canvas id="alice-wip-chart" height="90"></canvas>',
            '    </div>',
            '  </div>',
            '  <div class="col-md-3">',
            '    <div class="alice-card">',
            '      <h5>Congestion</h5>',
            '      <div id="alice-congestion-badges"></div>',
            '      <hr>',
            '      <h5 style="margin-top:8px">Bottleneck Alerts</h5>',
            '      <div id="alice-bottleneck-alerts"></div>',
            '    </div>',
            '  </div>',
            '</div>',

            // Row 2: open downtime + WO gate status
            '<div class="row" style="margin-top:12px">',
            '  <div class="col-md-4">',
            '    <div class="alice-card">',
            '      <h5>Open Downtime Events</h5>',
            '      <div id="alice-downtime-events"></div>',
            '    </div>',
            '  </div>',
            '  <div class="col-md-8">',
            '    <div class="alice-card">',
            '      <h5>Active WO Gate Status</h5>',
            '      <div id="alice-wo-gates" style="max-height:220px;overflow-y:auto"></div>',
            '    </div>',
            '  </div>',
            '</div>',

            // Row 3: skill leaderboard
            '<div class="row" style="margin-top:12px">',
            '  <div class="col-md-12">',
            '    <div class="alice-card">',
            '      <h5>Operator Skill Leaderboard</h5>',
            '      <div id="alice-skill-leaderboard"></div>',
            '    </div>',
            '  </div>',
            '</div>',

            '</div>',  // end padding div

            // Inline styles
            '<style>',
            '.alice-card { background:#fff; border:1px solid #d1d8dd; border-radius:6px;',
            '              padding:12px; margin-bottom:4px; }',
            '.alice-alert-row { padding:4px 0; }',
            '</style>',
        ].join("\n");
    },

    // ── Helpers ──────────────────────────────────────────────────────────────
    _fetch: function (method, args) {
        return new Promise(function (resolve, reject) {
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api." + method,
                args: args || {},
                callback: resolve,
                error: reject,
            });
        });
    },

    _set_status: function (msg) {
        $("#alice-status-bar").text(msg);
    },
};
