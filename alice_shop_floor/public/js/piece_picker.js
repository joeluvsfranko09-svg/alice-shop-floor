/**
 * ALICE Shop Floor — Piece Picker Tablet
 * =======================================
 * Virtual light-assist kitting station for pickers.
 *
 * Flow:
 *  1. Home screen: list of Queued / Kitting bins (priority sorted)
 *  2. Tap a bin (or scan barcode) → load its pick list
 *  3. Steps through pieces one at a time — BIG rack + slot display
 *  4. Tap ✓ PICKED  or ✗ SHORT for each piece
 *  5. When all pieces picked → Kit Ready celebration screen
 *
 * Frappe page JS — mounted to the "piece-picker" page.
 */

frappe.pages["piece-picker"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Piece Picker",
        single_column: true,
    });
    window.piece_picker = new PiecePickerApp(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class PiecePickerApp {
    constructor(page, wrapper) {
        this.page     = page;
        this.wrapper  = wrapper;
        this.$main    = $(wrapper).find(".page-content");

        // State
        this.assignment   = null;   // full assignment dict
        this.pick_list    = [];     // enriched rows
        this.current_idx  = 0;      // index into pending_rows
        this.pending_rows = [];     // rows still in Pending status

        this._init_styles();
        this._bind_realtime();
        this.show_home();
    }

    // ── Styles ──────────────────────────────────────────────────────────────

    _init_styles() {
        if (document.getElementById("pp-styles")) return;
        const css = `
.pp-bin-list { display:flex; flex-direction:column; gap:10px; max-width:600px; margin:0 auto; padding:10px; }
.pp-bin-card {
    border:2px solid var(--border-color);
    border-radius:10px; padding:14px 18px; cursor:pointer;
    background:#fff; transition:transform .1s, box-shadow .1s;
    display:flex; justify-content:space-between; align-items:center;
}
.pp-bin-card:active { transform:scale(0.98); }
.pp-bin-card.rush { border-color:#e74c3c; background:#fff5f5; }
.pp-bin-card.high { border-color:#e67e22; background:#fffaf5; }
.pp-bin-item { font-size:1.1rem; font-weight:600; color:#1a1a2e; }
.pp-bin-meta { font-size:0.85rem; color:#666; margin-top:3px; }
.pp-bin-progress { font-size:1.2rem; font-weight:700; }
.pp-badge { padding:3px 10px; border-radius:20px; font-size:0.75rem; font-weight:700; }
.pp-badge.rush    { background:#e74c3c; color:#fff; }
.pp-badge.high    { background:#e67e22; color:#fff; }
.pp-badge.normal  { background:#95a5a6; color:#fff; }
.pp-badge.kitting { background:#3498db; color:#fff; }
.pp-pick-screen { max-width:480px; margin:0 auto; padding:12px; text-align:center; }
.pp-progress-bar-wrap { background:#ecf0f1; border-radius:20px; height:10px; margin:10px 0 20px; overflow:hidden; }
.pp-progress-bar { height:100%; border-radius:20px; background:linear-gradient(90deg,#27ae60,#2ecc71); transition:width .4s; }
.pp-step-counter { font-size:0.9rem; color:#999; margin-bottom:4px; }
.pp-piece-name { font-size:1.5rem; font-weight:700; color:#1a1a2e; margin-bottom:20px; }
.pp-light-box {
    background:linear-gradient(135deg,#1a1a2e,#16213e);
    border-radius:16px; padding:28px 20px; margin:0 auto 24px;
    max-width:340px; box-shadow:0 8px 32px rgba(26,26,46,.4);
    position:relative; overflow:hidden;
}
.pp-light-box::before {
    content:''; position:absolute; top:0; left:0; right:0; bottom:0;
    background:radial-gradient(circle at 50% 30%, rgba(52,152,219,.25) 0%, transparent 70%);
    pointer-events:none;
}
.pp-light-label { font-size:0.8rem; letter-spacing:2px; text-transform:uppercase; color:#7f8c8d; margin-bottom:6px; }
.pp-rack-row { display:flex; justify-content:center; align-items:baseline; gap:20px; }
.pp-rack-val {
    font-size:5rem; font-weight:900; color:#3498db;
    text-shadow:0 0 30px rgba(52,152,219,.8), 0 0 60px rgba(52,152,219,.4); line-height:1;
}
.pp-slot-val {
    font-size:3.5rem; font-weight:900; color:#ecf0f1;
    text-shadow:0 0 20px rgba(255,255,255,.5); line-height:1;
}
.pp-rack-sep { font-size:3rem; color:#7f8c8d; line-height:1; }
.pp-loc-label { font-size:0.85rem; color:#bdc3c7; margin-top:8px; letter-spacing:1px; }
.pp-actions { display:flex; gap:12px; justify-content:center; margin-top:8px; }
.pp-btn {
    flex:1; max-width:180px; padding:18px 12px;
    border:none; border-radius:14px; font-size:1.15rem; font-weight:700;
    cursor:pointer; transition:transform .1s, box-shadow .1s;
    display:flex; flex-direction:column; align-items:center; gap:4px;
}
.pp-btn:active { transform:scale(0.96); }
.pp-btn-picked { background:#27ae60; color:#fff; box-shadow:0 4px 16px rgba(39,174,96,.4); }
.pp-btn-short  { background:#e74c3c; color:#fff; box-shadow:0 4px 16px rgba(231,76,60,.4); }
.pp-btn-icon { font-size:1.6rem; }
.pp-kit-ready { text-align:center; max-width:440px; margin:40px auto; padding:20px; }
.pp-kr-icon  { font-size:5rem; margin-bottom:12px; }
.pp-kr-title { font-size:2rem; font-weight:900; color:#27ae60; margin-bottom:8px; }
.pp-kr-sub   { font-size:1.1rem; color:#666; margin-bottom:30px; }
.pp-btn-home {
    background:#3498db; color:#fff; border:none; border-radius:12px;
    padding:16px 36px; font-size:1.1rem; font-weight:700; cursor:pointer;
}
.pp-scan-bar { display:flex; gap:8px; max-width:600px; margin:0 auto 16px; padding:0 10px; }
.pp-scan-bar input {
    flex:1; padding:10px 14px; border:2px solid var(--border-color);
    border-radius:8px; font-size:1rem;
}
.pp-scan-bar button {
    padding:10px 18px; background:#3498db; color:#fff;
    border:none; border-radius:8px; font-weight:700; cursor:pointer;
}
.pp-no-loc {
    background:#ffeeba; border:1px solid #ffc107; border-radius:10px;
    padding:20px; margin-bottom:20px; font-size:1rem; color:#856404;
}
`;
        $("<style id='pp-styles'>").text(css).appendTo("head");
    }

    // ── Realtime ─────────────────────────────────────────────────────────────

    _bind_realtime() {
        frappe.realtime.on("bin_kit_ready", () => {
            if (!this.assignment) this.show_home();
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HOME SCREEN
    // ══════════════════════════════════════════════════════════════════════════

    show_home() {
        this.assignment   = null;
        this.pick_list    = [];
        this.pending_rows = [];
        this.current_idx  = 0;
        this.page.set_title("Piece Picker — Kitting Queue");
        this.page.clear_primary_action();
        this.page.set_primary_action("Refresh", () => this.show_home(), "refresh");

        this.$main.empty().append(`
            <div>
                <div class="pp-scan-bar">
                    <input id="pp-scan-input" type="text"
                        placeholder="Scan bin barcode or enter assignment ID…"
                        autocomplete="off"/>
                    <button id="pp-scan-btn">Go</button>
                </div>
                <div id="pp-bin-list-wrap">
                    <div style="text-align:center;padding:30px;color:#999">Loading queue…</div>
                </div>
            </div>
        `);

        $("#pp-scan-btn").on("click", () => this._handle_scan());
        $("#pp-scan-input").on("keydown", (e) => { if (e.key === "Enter") this._handle_scan(); });
        setTimeout(() => $("#pp-scan-input").focus(), 300);
        this._load_queue();
    }

    _load_queue() {
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_queued_assignments_for_picker",
            callback: (r) => {
                if (r.exc) { $("#pp-bin-list-wrap").html("<p style='color:red;padding:20px'>Failed to load queue.</p>"); return; }
                this._render_queue(r.message || []);
            },
        });
    }

    _render_queue(rows) {
        if (!rows.length) {
            $("#pp-bin-list-wrap").html(
                `<div style="text-align:center;padding:40px;color:#27ae60;font-size:1.1rem;">
                    ✅ All bins kitted — nothing pending!
                </div>`
            );
            return;
        }
        const cards = rows.map(row => {
            const pct = row.total_pieces > 0
                ? Math.round(row.picked_pieces / row.total_pieces * 100) : 0;
            const prio_cls = (row.priority || "Normal").toLowerCase();
            const st_badge = row.status === "Kitting"
                ? `<span class="pp-badge kitting">Kitting ${pct}%</span>`
                : `<span class="pp-badge ${prio_cls}">${row.priority}</span>`;
            return `
            <div class="pp-bin-card ${prio_cls}" data-name="${frappe.utils.escape_html(row.name)}">
                <div>
                    <div class="pp-bin-item">${frappe.utils.escape_html(row.production_item || row.work_order)}</div>
                    <div class="pp-bin-meta">${frappe.utils.escape_html(row.name)} · ${frappe.utils.escape_html(row.station || "—")}</div>
                    <div class="pp-bin-meta" style="margin-top:4px">${st_badge}</div>
                </div>
                <div class="pp-bin-progress" style="color:${prio_cls === "rush" ? "#e74c3c" : "#333"}">
                    ${row.picked_pieces}/${row.total_pieces}
                </div>
            </div>`;
        }).join("");
        $("#pp-bin-list-wrap").html(`<div class="pp-bin-list">${cards}</div>`);
        $(".pp-bin-card").on("click", (e) => {
            this.load_assignment($(e.currentTarget).data("name"));
        });
    }

    _handle_scan() {
        const val = $("#pp-scan-input").val().trim();
        if (!val) return;
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_pick_assignment",
            args: { assignment_name: val },
            callback: (r) => {
                if (r.exc || !r.message) {
                    frappe.db.get_value("Sewing Bin Assignment", { bin_barcode: val }, "name", (v) => {
                        if (v && v.name) {
                            this.load_assignment(v.name);
                        } else {
                            frappe.show_alert({ message: `Bin "${val}" not found.`, indicator: "red" }, 3);
                        }
                    });
                } else {
                    this._on_assignment_loaded(r.message);
                }
            },
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // PICK SCREEN
    // ══════════════════════════════════════════════════════════════════════════

    load_assignment(name) {
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_pick_assignment",
            args: { assignment_name: name },
            callback: (r) => {
                if (r.exc || !r.message) {
                    frappe.show_alert({ message: "Could not load assignment.", indicator: "red" }, 3);
                    return;
                }
                this._on_assignment_loaded(r.message);
            },
        });
    }

    _on_assignment_loaded(data) {
        this.assignment   = data;
        this.pick_list    = data.pick_list || [];
        this.pending_rows = this.pick_list.filter(r => r.status === "Pending");
        this.current_idx  = 0;

        if (data.kit_status === "Kit Ready") { this.show_kit_ready(); return; }
        if (!this.pending_rows.length) { this._check_done(); return; }
        this._render_pick_step();
    }

    _render_pick_step() {
        const row    = this.pending_rows[this.current_idx];
        if (!row) { this._check_done(); return; }

        const total  = this.pick_list.length;
        const picked = this.pick_list.filter(r => r.status === "Picked").length;
        const pct    = total > 0 ? Math.round(picked / total * 100) : 0;

        const has_loc = !!(row.rack);
        const loc_block = has_loc
            ? `<div class="pp-light-box">
                    <div class="pp-light-label">Go to location</div>
                    <div class="pp-rack-row">
                        <div class="pp-rack-val">${frappe.utils.escape_html(row.rack)}</div>
                        <div class="pp-rack-sep">-</div>
                        <div class="pp-slot-val">${row.slot}</div>
                    </div>
                    <div class="pp-loc-label">${frappe.utils.escape_html(row.location_label || row.rack + "-" + row.slot)}</div>
                    ${row.qty_required > 1
                        ? `<div style="color:#bdc3c7;font-size:0.85rem;margin-top:6px;">Qty: ${row.qty_required}</div>`
                        : ""}
               </div>`
            : `<div class="pp-no-loc">⚠️ No storage location — ask supervisor to assign one.
                    ${row.qty_required > 1 ? `<br>Qty needed: ${row.qty_required}` : ""}
               </div>`;

        this.page.set_title(`Bin: ${this.assignment.name}`);
        this.$main.html(`
            <div class="pp-pick-screen">
                <div class="pp-step-counter">
                    Step ${this.current_idx + 1} of ${this.pending_rows.length}
                    &nbsp;·&nbsp; ${picked}/${total} pieces picked
                </div>
                <div class="pp-progress-bar-wrap">
                    <div class="pp-progress-bar" style="width:${pct}%"></div>
                </div>
                <div class="pp-piece-name">${frappe.utils.escape_html(row.piece_type)}</div>
                ${loc_block}
                <div class="pp-actions">
                    <button class="pp-btn pp-btn-picked" id="pp-btn-confirm">
                        <span class="pp-btn-icon">✓</span>PICKED
                    </button>
                    <button class="pp-btn pp-btn-short" id="pp-btn-short">
                        <span class="pp-btn-icon">✗</span>SHORT
                    </button>
                </div>
                <div style="margin-top:20px;">
                    <a href="#" id="pp-back-home" style="color:#999;font-size:0.85rem;">← Back to queue</a>
                </div>
            </div>
        `);

        $("#pp-btn-confirm").on("click", () => this._confirm_pick(row));
        $("#pp-btn-short").on("click", () => this._confirm_short(row));
        $("#pp-back-home").on("click", (e) => { e.preventDefault(); this.show_home(); });
    }

    _confirm_pick(row) {
        const $btn = $("#pp-btn-confirm").prop("disabled", true).text("Saving…");
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.confirm_piece_pick",
            args: { assignment_name: this.assignment.name, idx: row.idx, qty_picked: row.qty_required },
            callback: (r) => {
                if (r.exc) {
                    frappe.show_alert({ message: "Save failed — retry.", indicator: "red" }, 3);
                    $btn.prop("disabled", false).text("PICKED");
                    return;
                }
                const local = this.pick_list.find(x => x.idx === row.idx);
                if (local) local.status = "Picked";
                this.pending_rows = this.pick_list.filter(x => x.status === "Pending");
                this.current_idx  = 0;
                frappe.show_alert({ message: `✓ ${row.piece_type} picked`, indicator: "green" }, 2);
                if (r.message && r.message.all_done) {
                    this.show_kit_ready();
                } else {
                    this._render_pick_step();
                }
            },
        });
    }

    _confirm_short(row) {
        frappe.confirm(
            `Mark <strong>${frappe.utils.escape_html(row.piece_type)}</strong> as SHORT?<br>
            <small>This will alert the supervisor.</small>`,
            () => {
                frappe.call({
                    method: "alice_shop_floor.alice_shop_floor.api.mark_piece_short",
                    args: { assignment_name: this.assignment.name, idx: row.idx },
                    callback: () => {
                        const local = this.pick_list.find(x => x.idx === row.idx);
                        if (local) local.status = "Short";
                        this.pending_rows = this.pick_list.filter(x => x.status === "Pending");
                        this.current_idx  = 0;
                        frappe.show_alert({ message: `${row.piece_type} marked SHORT`, indicator: "orange" }, 3);
                        if (!this.pending_rows.length) { this._check_done(); } else { this._render_pick_step(); }
                    },
                });
            }
        );
    }

    _check_done() {
        const any_short = this.pick_list.some(r => r.status === "Short");
        if (any_short) { this._show_short_summary(); } else { this.show_kit_ready(); }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // KIT READY SCREEN
    // ══════════════════════════════════════════════════════════════════════════

    show_kit_ready() {
        this.page.set_title("Kit Ready ✓");
        const a = this.assignment || {};
        this.$main.html(`
            <div class="pp-kit-ready">
                <div class="pp-kr-icon">🎉</div>
                <div class="pp-kr-title">Kit Ready!</div>
                <div class="pp-kr-sub">
                    All pieces picked for<br>
                    <strong>${frappe.utils.escape_html(a.production_item || a.work_order || "")}</strong><br>
                    <span style="color:#999;font-size:0.9rem">${frappe.utils.escape_html(a.name || "")}</span>
                </div>
                <div style="color:#999;font-size:0.85rem;margin-bottom:24px;">
                    Carry all pieces to the sewing station.
                </div>
                <button class="pp-btn-home" id="pp-next-bin">Pick Next Bin →</button>
            </div>
        `);
        $("#pp-next-bin").on("click", () => this.show_home());
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SHORT SUMMARY
    // ══════════════════════════════════════════════════════════════════════════

    _show_short_summary() {
        const shorts = this.pick_list.filter(r => r.status === "Short");
        const picked = this.pick_list.filter(r => r.status === "Picked").length;
        const items_html = shorts.map(r =>
            `<li style="margin:4px 0">${frappe.utils.escape_html(r.piece_type)}</li>`
        ).join("");
        this.page.set_title("Kit Incomplete — Short Pieces");
        this.$main.html(`
            <div style="max-width:480px;margin:30px auto;padding:16px;text-align:center;">
                <div style="font-size:3rem;margin-bottom:10px;">⚠️</div>
                <div style="font-size:1.4rem;font-weight:700;color:#e74c3c;margin-bottom:8px;">Short Pieces Found</div>
                <div style="color:#666;margin-bottom:20px;">
                    ${picked} of ${this.pick_list.length} pieces picked.
                    Supervisor has been notified.
                </div>
                <div style="background:#fff5f5;border:1px solid #e74c3c;border-radius:10px;
                            padding:16px;margin-bottom:24px;text-align:left;">
                    <strong>Missing:</strong>
                    <ul style="margin:8px 0 0 18px;">${items_html}</ul>
                </div>
                <button class="pp-btn-home" id="pp-goto-home" style="background:#7f8c8d;">
                    Back to Queue
                </button>
            </div>
        `);
        $("#pp-goto-home").on("click", () => this.show_home());
    }
}
