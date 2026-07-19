/**
 * ALICE — Sewing Floor View
 * =========================
 * Tablet-optimised page for sewers and supervisors.
 *
 * Cards:  FREE (grey) | QUEUED (yellow) | PICKED (blue) | IN PROGRESS (green)
 *
 * Each active card shows machine badge, shade-zone badge, sewer action buttons,
 * and a 🏷️ Print Label button.
 *
 * Supervisor toolbar:
 *   [Assign Now] [View Queue] [📱 Scan Bin] [🏷️ Print Labels] [📊 Pace]
 *
 * Each active card shows a pace badge: 🟢 Ahead | 🟢 On Track | 🟡 Behind | 🔴 Critical
 * Clicking the pace badge opens a supervisor dialog with targets + rebalance suggestions.
 *
 * Auto-refreshes every 30 s. Subscribes to realtime bin_* and pace_alert events.
 */

frappe.pages["sewing-floor-view"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent:    wrapper,
        title:     "Sewing Floor",
        single_column: true,
    });

    $("<style>").text(`
        .sf-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 16px; padding: 16px 0;
        }
        .sf-card {
            border-radius:10px; padding:18px 16px 14px;
            box-shadow:0 2px 8px rgba(0,0,0,.10);
            border:2px solid transparent; transition:box-shadow .15s; position:relative;
        }
        .sf-card:hover { box-shadow:0 4px 14px rgba(0,0,0,.16); }
        .sf-card.free   { background:#f5f5f5; border-color:#ddd; }
        .sf-card.queued { background:#fffbea; border-color:#f9c74f; }
        .sf-card.picked { background:#eff6ff; border-color:#4895ef; }
        .sf-card.inprog { background:#e8faf0; border-color:#38b000; }
        .sf-station-code { font-size:22px; font-weight:700; margin-bottom:2px; }
        .sf-machine-badge {
            display:inline-block; font-size:11px; font-weight:600;
            padding:2px 8px; border-radius:99px; margin-bottom:8px;
            background:rgba(0,0,0,.08); color:#444;
        }
        .sf-machine-mismatch { background:#ffe0e0 !important; color:#c00 !important; }
        .sf-wo   { font-size:13px; font-weight:600; color:#333; }
        .sf-item { font-size:12px; color:#666; margin-bottom:4px;
                   white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .sf-op   { font-size:12px; color:#555; }
        .sf-meta { font-size:11px; color:#888; margin-top:6px; }
        .sf-priority-rush { color:#c00; font-weight:700; }
        .sf-priority-high { color:#e07800; font-weight:600; }
        .sf-elapsed       { font-size:11px; color:#999; }
        .sf-actions { margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; }
        .sf-btn {
            flex:1; min-width:80px; padding:7px 4px; border:none;
            border-radius:6px; font-size:13px; font-weight:600;
            cursor:pointer; transition:opacity .15s;
        }
        .sf-btn:hover { opacity:.85; }
        .sf-btn-pick   { background:#4895ef; color:#fff; }
        .sf-btn-start  { background:#38b000; color:#fff; }
        .sf-btn-done   { background:#2d6a4f; color:#fff; font-size:14px; }
        .sf-btn-return { background:#e5e5e5; color:#555; }
        .sf-btn-print  {
            flex:0 0 42px; width:42px; padding:7px 4px; border:none;
            border-radius:6px; font-size:16px; cursor:pointer;
            background:#f0f0f0; color:#555; transition:opacity .15s;
        }
        .sf-btn-print:hover { opacity:.8; }
        .sf-free-label { color:#aaa; font-size:13px; margin-top:8px; }
        .sf-queue-table td, .sf-queue-table th { padding:8px 10px; font-size:13px; }
        .sf-match-yes { color:#2d6a4f; font-weight:600; }
        .sf-match-no  { color:#c00;    font-weight:600; }

        /* Pace badges */
        .sf-pace-badge {
            display:inline-block; font-size:11px; font-weight:700;
            padding:2px 8px; border-radius:99px; margin-bottom:4px;
            cursor:pointer; margin-left:4px;
        }
        .sf-pace-badge:hover { opacity:.82; }
        .pace-ahead    { background:#d1fae5; color:#065f46; }
        .pace-ontrack  { background:#dcfce7; color:#166534; }
        .pace-behind   { background:#fef3c7; color:#92400e; }
        .pace-critical { background:#fee2e2; color:#991b1b; animation:sf-pulse 1.5s infinite; }
        .pace-notarget { background:#f3f4f6; color:#6b7280; }
        @keyframes sf-pulse {
            0%,100% { opacity:1; }
            50%      { opacity:.6; }
        }
        .sf-progress-bar {
            height:4px; border-radius:2px; background:#e5e7eb;
            margin: 3px 0 6px; overflow:hidden;
        }
        .sf-progress-fill {
            height:100%; border-radius:2px; transition:width .4s;
        }
        .fill-ahead    { background:#22c55e; }
        .fill-ontrack  { background:#4ade80; }
        .fill-behind   { background:#fbbf24; }
        .fill-critical { background:#ef4444; }
        .fill-notarget { background:#d1d5db; }

        /* Shade badges */
        .sf-shade-badge {
            display:inline-block; font-size:11px; font-weight:600;
            padding:2px 8px; border-radius:99px; margin-bottom:6px; margin-left:4px;
        }
        .sf-shade-ok               { background:#d1fae5; color:#065f46; }
        .sf-shade-warning          { background:#fef3c7; color:#92400e; }
        .sf-shade-mismatch         { background:#fee2e2; color:#991b1b; }
        .sf-shade-mismatch-cleared { background:#fef3c7; color:#92400e; }
        .sf-shade-incomplete       { background:#f3f4f6; color:#6b7280; }
        .sf-shade-none             { background:#fee2e2; color:#991b1b; }
    `).appendTo("head");

    // Toolbar
    page.add_button("Assign Now",      () => sf.trigger_assign(),  { icon:"fa fa-magic", btn_class:"btn-primary" });
    page.add_button("View Queue",      () => sf.show_queue(),      { icon:"fa fa-list" });
    page.add_button("📱 Scan Bin",     () => frappe.set_route("sewing-bin-scan"), {});
    page.add_button("🏷️ Print Labels", () => sf.print_all_labels(), {});
    page.add_button("📊 Pace", () => sf.show_pace_dashboard(), {});

    const $body = $(wrapper).find(".layout-main-section");
    $body.html(`<div id="sf-container">
        <div id="sf-status-bar" style="font-size:12px;color:#999;margin-bottom:4px;"></div>
        <div id="sf-grid" class="sf-grid"></div>
    </div>`);

    // ─────────────────────────────────────────────────────────────────────────
    const sf = {
        _timer:    null,
        _pace_map: {},    // operator → pace dict, refreshed with each cycle

        init() {
            this.refresh();
            this._timer = setInterval(() => this.refresh(), 30000);
            this._bind_realtime();
        },

        refresh() {
            // Fetch station summary and pace summary in parallel
            let stations_done = false, pace_done = false;
            let stations_data = null;

            const try_render = () => {
                if (stations_done && pace_done && stations_data)
                    this._render(stations_data);
            };

            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.get_station_summary",
                callback: (r) => {
                    stations_data = r.message || [];
                    stations_done = true;
                    $("#sf-status-bar").text("Last updated: " + frappe.datetime.now_time());
                    try_render();
                },
            });

            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.get_sewing_pace_dashboard",
                callback: (r) => {
                    this._pace_map = {};
                    (r.message || []).forEach(p => { this._pace_map[p.operator] = p; });
                    pace_done = true;
                    try_render();
                },
            });
        },

        _render(stations) {
            const $grid = $("#sf-grid").empty();
            stations.forEach(st => $grid.append(this._make_card(st)));
        },

        _make_card(st) {
            const is_free  = st.is_free;
            const status   = (st.assignment_status || "").toLowerCase().replace(/\s/g, "");
            const css_cls  = is_free ? "free"
                : status === "queued"     ? "queued"
                : status === "picked"     ? "picked"
                : status === "inprogress" ? "inprog" : "queued";

            const mismatch       = !is_free && st.machine_match === 0;
            const machine_label  = st.machine_type || "No machine set";
            const machine_cls    = mismatch ? "sf-machine-badge sf-machine-mismatch" : "sf-machine-badge";

            let pri_html = "";
            if (!is_free && st.priority === "Rush") pri_html = `<span class="sf-priority-rush">🔴 RUSH</span> `;
            else if (!is_free && st.priority === "High") pri_html = `<span class="sf-priority-high">🟠 HIGH</span> `;

            const elapsed      = is_free ? "" : `<span class="sf-elapsed">${st.elapsed_minutes}m in bin</span>`;
            const shade_badge  = is_free ? "" : this._shade_badge(st.bundle_shade_status);
            const pace_badge   = is_free ? "" : this._pace_badge(st.operator);

            const wo_block = is_free
                ? `<div class="sf-free-label">— Station Free —</div>`
                : `<div class="sf-wo">${pri_html}${st.work_order || ""}</div>
                   <div class="sf-item">${st.production_item || ""}</div>
                   <div class="sf-op">Operator: <strong>${st.operator || "Unassigned"}</strong></div>
                   ${pace_badge}
                   ${shade_badge}
                   <div class="sf-meta">${elapsed}</div>`;

            const actions = is_free ? "" : this._make_actions(st);

            return $(`
                <div class="sf-card ${css_cls}" data-station="${st.station}">
                    <div class="sf-station-code">${st.station_code}</div>
                    <span class="${machine_cls}">${machine_label}</span>
                    ${mismatch ? '<span class="sf-machine-badge sf-machine-mismatch">⚠ Machine mismatch</span>' : ""}
                    ${wo_block}
                    ${actions}
                </div>
            `);
        },

        _make_actions(st) {
            const s = st.assignment_status || "";
            let b = "";
            if (s === "Queued")
                b += `<button class="sf-btn sf-btn-pick"
                        onclick="sewing_floor._action('pick','${st.station}')">PICK</button>`;
            if (s === "Picked")
                b += `<button class="sf-btn sf-btn-start"
                        onclick="sewing_floor._action('start','${st.station}')">START SEWING</button>`;
            if (s === "In Progress")
                b += `<button class="sf-btn sf-btn-done"
                        onclick="sewing_floor._action('done','${st.station}')">✓ DONE</button>`;
            if (s !== "")
                b += `<button class="sf-btn sf-btn-return"
                        onclick="sewing_floor._action('return','${st.station}')">Return</button>`;
            if (s !== "")
                b += `<button class="sf-btn sf-btn-print"
                        onclick="sewing_floor._print_label_for_station('${st.station}')">🏷️</button>`;
            return `<div class="sf-actions">${b}</div>`;
        },

        /**
         * _shade_badge — renders nesting/shade status below operator line.
         * shade_status: { status, zones, cleared, pieces_cut, pieces_expected }
         */
        _shade_badge(shade_status) {
            if (!shade_status)
                return `<span class="sf-shade-badge sf-shade-none">⛔ No bundle</span>`;
            const { status, zones, cleared, pieces_cut, pieces_expected } = shade_status;
            if (status === "Complete")
                return `<span class="sf-shade-badge sf-shade-ok">✓ Bundle complete</span>`;
            if (status === "Shade Warning") {
                const z = zones > 1 ? ` · ${zones} zones` : "";
                return `<span class="sf-shade-badge sf-shade-warning">⚠ Shade variation${z} — cleared</span>`;
            }
            if (status === "Shade Mismatch") {
                if (cleared)
                    return `<span class="sf-shade-badge sf-shade-mismatch-cleared">⚠ Shade mismatch — supervisor cleared</span>`;
                return `<span class="sf-shade-badge sf-shade-mismatch">🔴 Shade mismatch — awaiting supervisor</span>`;
            }
            if (status === "Incomplete") {
                const cut = pieces_cut      !== undefined ? pieces_cut      : "?";
                const exp = pieces_expected !== undefined ? pieces_expected : "?";
                return `<span class="sf-shade-badge sf-shade-incomplete">⚪ ${cut}/${exp} pieces cut</span>`;
            }
            return `<span class="sf-shade-badge sf-shade-incomplete">${status || "No bundle"}</span>`;
        },

        _action(action, station) {
            frappe.call({
                method: "frappe.client.get_list",
                args: {
                    doctype: "Sewing Bin Assignment",
                    filters: { station, status: ["in", ["Queued","Picked","In Progress"]] },
                    fields:  ["name"],
                    limit:   1,
                },
                callback: (r) => {
                    if (!r.message || !r.message.length) {
                        frappe.msgprint("No active assignment found for this station.");
                        return;
                    }
                    this._call_action(action, r.message[0].name);
                },
            });
        },

        _call_action(action, assignment_name) {
            const M = {
                pick:   "alice_shop_floor.alice_shop_floor.api.bin_mark_picked",
                start:  "alice_shop_floor.alice_shop_floor.api.bin_mark_in_progress",
                done:   "alice_shop_floor.alice_shop_floor.api.bin_mark_complete",
                return: "alice_shop_floor.alice_shop_floor.api.bin_mark_returned",
            };

            if (action === "return") {
                frappe.prompt(
                    [{label:"Reason for return", fieldtype:"Data", fieldname:"reason"}],
                    (v) => frappe.call({
                        method: M.return,
                        args:   { assignment_name, reason: v.reason || "" },
                        callback: () => this.refresh(),
                    }),
                    "Return bundle to queue", "Return"
                );
                return;
            }

            if (action === "done") {
                frappe.confirm(
                    "Mark this bundle as <strong>complete</strong>? " +
                    "This will advance the Work Order to the Sewing stage.",
                    () => frappe.call({
                        method: M.done,
                        args:   { assignment_name },
                        callback: () => {
                            frappe.show_alert({message:"Bundle complete — WO advanced to Sewing ✓", indicator:"green"});
                            this.refresh();
                        },
                    })
                );
                return;
            }

            frappe.call({
                method: M[action],
                args:   { assignment_name },
                callback: () => this.refresh(),
            });
        },

        trigger_assign() {
            frappe.show_alert({message:"Running pick-to-bin assignment…", indicator:"blue"});
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.run_pick_to_bin",
                callback: (r) => {
                    const res = r.message || {};
                    frappe.show_alert({
                        message: `Assigned ${res.assigned_count||0} bundles, ${res.skipped_count||0} waiting for machine.`,
                        indicator: "green",
                    });
                    this.refresh();
                },
            });
        },

        show_queue() {
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.get_pick_to_bin_queue",
                args: { limit: 100 },
                callback: (r) => {
                    const rows = r.message || [];
                    if (!rows.length) {
                        frappe.msgprint("No bundles in queue — all assigned or none ready.");
                        return;
                    }
                    const tbody = rows.map(row => `
                        <tr>
                            <td>${row.work_order}</td>
                            <td>${row.production_item||""}</td>
                            <td>${row.required_machine_type||"<em>Any</em>"}</td>
                            <td>${row.priority||"Normal"}</td>
                            <td>${row.fabric_lot||""}</td>
                        </tr>`).join("");
                    frappe.msgprint({
                        title: `Sewing Queue — ${rows.length} bundle(s) waiting`,
                        message: `<table class="table sf-queue-table">
                            <thead><tr>
                                <th>Work Order</th><th>Item</th>
                                <th>Required Machine</th><th>Priority</th><th>Fabric Lot</th>
                            </tr></thead>
                            <tbody>${tbody}</tbody>
                        </table>`,
                        wide: true,
                    });
                },
            });
        },

        _print_label_for_station(station) {
            frappe.call({
                method: "frappe.client.get_list",
                args: {
                    doctype: "Sewing Bin Assignment",
                    filters: { station, status: ["in", ["Queued","Picked","In Progress"]] },
                    fields:  ["name"],
                    limit:   1,
                },
                callback: (r) => {
                    if (!r.message || !r.message.length) {
                        frappe.msgprint("No active assignment at this station.");
                        return;
                    }
                    this._open_label(r.message[0].name);
                },
            });
        },

        _open_label(assignment_name) {
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.get_bin_label",
                args:   { assignment_name },
                callback: (r) => {
                    if (!r.message || !r.message.html) return;
                    const win = window.open("", "_blank", "width=520,height=720");
                    win.document.write(r.message.html);
                    win.document.close();
                },
            });
        },

        print_all_labels() {
            frappe.call({
                method: "frappe.client.get_list",
                args: {
                    doctype: "Sewing Bin Assignment",
                    filters: { status: ["in", ["Queued","Picked","In Progress"]] },
                    fields:  ["name","station","work_order"],
                    limit:   50,
                },
                callback: (r) => {
                    const list = r.message || [];
                    if (!list.length) {
                        frappe.msgprint("No active assignments to print.");
                        return;
                    }
                    frappe.confirm(
                        `Print labels for all <strong>${list.length}</strong> active bin(s)?`,
                        () => {
                            list.forEach(row => this._open_label(row.name));
                        }
                    );
                },
            });
        },

        // ── Pace methods ──────────────────────────────────────────────────────

        _pace_badge(operator) {
            const p = this._pace_map[operator];
            if (!p) return "";
            const pct   = p.projected_pct || 0;
            const bar_pct = Math.min(pct, 100);
            let cls, fill, label;
            if (p.pace_status === "Ahead") {
                cls = "pace-ahead"; fill = "fill-ahead";
                label = `🟢 ${pct}%`;
            } else if (p.pace_status === "On Track") {
                cls = "pace-ontrack"; fill = "fill-ontrack";
                label = `🟢 ${pct}%`;
            } else if (p.pace_status === "Behind") {
                cls = "pace-behind"; fill = "fill-behind";
                label = `🟡 ${pct}%`;
            } else if (p.pace_status === "Critical") {
                cls = "pace-critical"; fill = "fill-critical";
                label = `🔴 ${pct}%`;
            } else {
                cls = "pace-notarget"; fill = "fill-notarget";
                label = "No Target";
            }
            const enc = encodeURIComponent(operator);
            return `
                <span class="sf-pace-badge ${cls}"
                      onclick="sf._show_operator_pace('${enc}')">${label}</span>
                <div class="sf-progress-bar">
                    <div class="sf-progress-fill ${fill}" style="width:${bar_pct}%"></div>
                </div>`;
        },

        _show_operator_pace(encoded_op) {
            const operator = decodeURIComponent(encoded_op);
            const p = this._pace_map[operator];
            if (!p) return;
            frappe.msgprint({
                title: `Pace: ${operator}`,
                message: `
                    <table class="table">
                        <tr><td><b>Shift</b></td><td>${p.shift} (${p.shift_start_str}–${p.shift_end_str})</td></tr>
                        <tr><td><b>Target WOs</b></td><td>${p.target_wos || "Not set"}</td></tr>
                        <tr><td><b>Completed</b></td><td>${p.wos_completed}</td></tr>
                        <tr><td><b>In Progress</b></td><td>${p.wos_in_progress}</td></tr>
                        <tr><td><b>Queued</b></td><td>${p.wos_queued}</td></tr>
                        <tr><td><b>Avg sew time</b></td><td>${p.avg_sew_min} min/WO</td></tr>
                        <tr><td><b>Remaining shift</b></td><td>${p.remaining_shift_min} min</td></tr>
                        <tr><td><b>Projected total</b></td><td>${p.projected_total} WOs</td></tr>
                        <tr><td><b>Projected %</b></td><td>${p.projected_pct}%</td></tr>
                        <tr><td><b>Status</b></td><td><strong>${p.pace_status}</strong></td></tr>
                    </table>
                    <button class="btn btn-xs btn-default" style="margin-top:8px;"
                        onclick="sf._set_target_dialog('${encoded_op}')">
                        Set Target
                    </button>`,
            });
        },

        _set_target_dialog(encoded_op) {
            const operator = decodeURIComponent(encoded_op);
            frappe.prompt([
                { label: "Target WOs", fieldname: "target_wos", fieldtype: "Int",
                  default: (this._pace_map[operator] || {}).target_wos || 8, reqd: 1 },
                { label: "Warn at %",     fieldname: "warn_pct",     fieldtype: "Float", default: 80 },
                { label: "Critical at %", fieldname: "critical_pct", fieldtype: "Float", default: 60 },
            ],
            (values) => {
                frappe.call({
                    method: "alice_shop_floor.alice_shop_floor.api.set_shift_target",
                    args: {
                        operator,
                        target_wos:   values.target_wos,
                        warn_pct:     values.warn_pct,
                        critical_pct: values.critical_pct,
                    },
                    callback: (r) => {
                        frappe.show_alert({ message: "Target saved ✓", indicator: "green" });
                        this.refresh();
                    },
                });
            },
            "Set Shift Target",
            "Save");
        },

        show_pace_dashboard() {
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.get_sewing_pace_dashboard",
                callback: (r) => {
                    const rows = r.message || [];
                    frappe.call({
                        method: "alice_shop_floor.alice_shop_floor.api.get_rebalance_suggestions",
                        callback: (r2) => {
                            this._render_pace_dialog(rows, r2.message || []);
                        },
                    });
                },
            });
        },

        _render_pace_dialog(rows, suggestions) {
            const STATUS_ICON = {
                "Ahead":     "🟢",
                "On Track":  "🟢",
                "Behind":    "🟡",
                "Critical":  "🔴",
                "No Target": "⚪",
            };
            const tbody = rows.map(p => {
                const icon = STATUS_ICON[p.pace_status] || "⚪";
                return `<tr>
                    <td>${p.operator}</td>
                    <td>${icon} ${p.pace_status}</td>
                    <td>${p.wos_completed}/${p.target_wos || "—"}</td>
                    <td>${p.projected_pct}%</td>
                    <td>${p.avg_sew_min} min</td>
                    <td>${p.remaining_shift_min} min</td>
                    <td><button class="btn btn-xs btn-default"
                        onclick="sf._set_target_dialog('${encodeURIComponent(p.operator)}')">
                        Set</button></td>
                </tr>`;
            }).join("");

            let suggest_html = "";
            if (suggestions && suggestions.length) {
                const s_rows = suggestions.map(s => `
                    <tr>
                        <td>${s.work_order}</td>
                        <td>${s.from_operator} → ${s.to_operator}</td>
                        <td>${s.from_station} → ${s.to_station}</td>
                        <td>${s.reason}</td>
                        <td><button class="btn btn-xs btn-warning"
                            onclick="sf._apply_rebalance('${s.assignment}','${s.to_station}','${encodeURIComponent(s.to_operator)}')">
                            Move</button></td>
                    </tr>`).join("");
                suggest_html = `
                    <h6 style="margin-top:20px;color:#c00;">⚡ Rebalance Suggestions</h6>
                    <table class="table" style="font-size:12px;">
                        <thead><tr>
                            <th>Work Order</th><th>Operator Move</th>
                            <th>Station Move</th><th>Reason</th><th></th>
                        </tr></thead>
                        <tbody>${s_rows}</tbody>
                    </table>`;
            }

            frappe.msgprint({
                title: `📊 Floor Pace Dashboard — ${rows.length} active sewer(s)`,
                message: `
                    <table class="table" style="font-size:12px;">
                        <thead><tr>
                            <th>Operator</th><th>Status</th>
                            <th>Done/Target</th><th>Projected</th>
                            <th>Avg/WO</th><th>Remaining</th><th></th>
                        </tr></thead>
                        <tbody>${tbody}</tbody>
                    </table>
                    ${suggest_html}`,
                wide: true,
            });
        },

        _apply_rebalance(assignment, to_station, encoded_op) {
            const to_operator = decodeURIComponent(encoded_op);
            frappe.confirm(
                `Move this bundle to station <strong>${to_station}</strong> (${to_operator})?`,
                () => {
                    frappe.call({
                        method: "alice_shop_floor.alice_shop_floor.api.apply_rebalance",
                        args:   { assignment, to_station, to_operator },
                        callback: (r) => {
                            frappe.show_alert({ message: "Bundle rebalanced ✓", indicator: "green" });
                            this.refresh();
                        },
                    });
                }
            );
        },


        _bind_realtime() {
            // Debounce helper — coalesce rapid-fire realtime events into one refresh
            const debounced_refresh = frappe.utils.debounce
                ? frappe.utils.debounce(() => this.refresh(), 1500)
                : () => this.refresh();

            // Bin lifecycle events
            ["bin_assigned","bin_picked","bin_in_progress",
             "bin_complete","bin_returned","bin_shade_mismatch"].forEach(evt => {
                frappe.realtime.on(evt, () => debounced_refresh());
            });

            // Pace alert — show banner for Critical operators
            frappe.realtime.on("pace_alert", (data) => {
                if (!data || !data.critical_count) return;
                const names = (data.operators || [])
                    .map(o => `${o.operator} (${o.projected_pct}%)`)
                    .join(", ");
                frappe.show_alert({
                    message: `🔴 ${data.critical_count} sewer(s) Critical pace: ${names}`,
                    indicator: "red",
                }, 8);
                debounced_refresh();
            });
        },

    };  // end sf module

    sf.init();
    window.sf = sf;  // expose for onclick handlers in dynamic HTML
};
