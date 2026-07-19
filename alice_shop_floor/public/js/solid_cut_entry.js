/**
 * ALICE Shop Floor — Solid Cut Entry Tablet
 * ==========================================
 * Tablet page for cutters to log roll-to-trace data on solid fabrics.
 *
 * Flow:
 *  1. Select / scan a Work Order
 *  2. Enter fabric item (optional)
 *  3. Scan each roll barcode → enter dye lot + yardage + pieces cut
 *  4. System auto-detects dye-lot bridge (2+ lots) → red alert
 *  5. Confirm (or supervisor override if bridge)
 *
 * Frappe page JS — mounted to the "solid-cut-entry" page.
 */

frappe.pages["solid-cut-entry"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Solid Cut Entry",
        single_column: true,
    });
    window.solid_cut_entry = new SolidCutEntryApp(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class SolidCutEntryApp {
    constructor(page, wrapper) {
        this.page    = page;
        this.wrapper = wrapper;
        this.$main   = $(wrapper).find(".page-content");

        // State
        this.log_name       = null;   // current Solid Fabric Cut Log name
        this.log_data       = null;   // full dict from server
        this.work_order     = null;
        this.fabric_item    = null;

        this._init_styles();
        this._bind_realtime();
        this.show_wo_select();
    }

    // ── Styles ──────────────────────────────────────────────────────────────

    _init_styles() {
        if (document.getElementById("sce-styles")) return;
        const css = `
.sce-wrap { max-width:580px; margin:0 auto; padding:12px; }
.sce-field-row { display:flex; flex-direction:column; margin-bottom:14px; }
.sce-field-row label { font-size:0.85rem; font-weight:600; color:#555; margin-bottom:4px; }
.sce-input {
    padding:10px 14px; border:2px solid var(--border-color);
    border-radius:8px; font-size:1rem; width:100%; box-sizing:border-box;
}
.sce-input:focus { border-color:#3498db; outline:none; }
.sce-btn {
    padding:12px 24px; border:none; border-radius:10px; font-size:1rem;
    font-weight:700; cursor:pointer; transition:transform .1s;
}
.sce-btn:active { transform:scale(0.97); }
.sce-btn-primary { background:#3498db; color:#fff; }
.sce-btn-success { background:#27ae60; color:#fff; }
.sce-btn-danger  { background:#e74c3c; color:#fff; }
.sce-btn-outline { background:#fff; color:#333; border:2px solid #ccc; }
.sce-roll-card {
    border:2px solid #e0e0e0; border-radius:10px;
    padding:14px; margin-bottom:10px; background:#fafafa;
}
.sce-roll-card.bridge { border-color:#e74c3c; background:#fff5f5; }
.sce-roll-header {
    display:flex; justify-content:space-between; align-items:center;
    font-weight:700; font-size:1rem; margin-bottom:6px;
}
.sce-roll-meta { font-size:0.85rem; color:#666; }
.sce-bridge-banner {
    background:#e74c3c; color:#fff; border-radius:10px;
    padding:16px; margin:12px 0; text-align:center;
}
.sce-bridge-banner .sce-bridge-title { font-size:1.3rem; font-weight:900; }
.sce-bridge-banner .sce-bridge-sub { font-size:0.9rem; margin-top:4px; opacity:.9; }
.sce-confirmed-banner {
    background:#27ae60; color:#fff; border-radius:10px;
    padding:20px; margin:20px 0; text-align:center;
}
.sce-section-title { font-size:1rem; font-weight:700; color:#333; margin:16px 0 8px; }
.sce-add-roll-form {
    border:2px dashed #3498db; border-radius:10px;
    padding:16px; margin-top:12px; background:#f0f8ff;
}
.sce-add-roll-form .sce-section-title { color:#3498db; margin-top:0; }
.sce-two-col { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
`;
        $("<style id='sce-styles'>").text(css).appendTo("head");
    }

    // ── Realtime ─────────────────────────────────────────────────────────────

    _bind_realtime() {
        frappe.realtime.on("dye_lot_bridge_alert", (data) => {
            if (data.log === this.log_name) {
                this._refresh_log();
            }
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STEP 1 — Work Order selection
    // ══════════════════════════════════════════════════════════════════════════

    show_wo_select() {
        this.page.set_title("Solid Cut Entry — Select Work Order");
        this.page.clear_primary_action();
        this.$main.html(`
            <div class="sce-wrap">
                <div class="sce-field-row">
                    <label>Work Order</label>
                    <input class="sce-input" id="sce-wo-input"
                        type="text" placeholder="Scan or type Work Order ID…" autocomplete="off"/>
                </div>
                <div class="sce-field-row">
                    <label>Fabric Item <span style="color:#999;font-weight:400">(optional)</span></label>
                    <input class="sce-input" id="sce-fabric-input"
                        type="text" placeholder="e.g. FABRIC-COTTON-NAVY"/>
                </div>
                <div style="display:flex;gap:10px;margin-top:6px;">
                    <button class="sce-btn sce-btn-primary" id="sce-wo-go" style="flex:1">
                        Start Cut Entry →
                    </button>
                </div>
                <div id="sce-existing-logs" style="margin-top:20px;"></div>
            </div>
        `);

        const $wo = $("#sce-wo-input");
        $wo.focus();
        $wo.on("change", () => this._load_existing_logs($wo.val().trim()));
        $("#sce-wo-go").on("click", () => this._start_entry());
        $wo.on("keydown", (e) => { if (e.key === "Enter") this._start_entry(); });
    }

    _load_existing_logs(wo) {
        if (!wo) return;
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_open_cut_logs_for_wo",
            args: { work_order: wo },
            callback: (r) => {
                const logs = r.message || [];
                if (!logs.length) { $("#sce-existing-logs").empty(); return; }
                const rows = logs.map(l => `
                    <div style="display:flex;justify-content:space-between;align-items:center;
                                padding:10px;border:1px solid #ddd;border-radius:8px;margin-bottom:6px;cursor:pointer;background:#fff;"
                         class="sce-existing-row" data-name="${frappe.utils.escape_html(l.name)}">
                        <div>
                            <strong>${frappe.utils.escape_html(l.name)}</strong>
                            <span style="margin-left:8px;font-size:0.8rem;color:#666">${l.cut_date || ""}</span>
                        </div>
                        <div>
                            ${l.dye_lot_bridge_detected
                                ? `<span style="background:#e74c3c;color:#fff;padding:3px 8px;border-radius:12px;font-size:0.75rem;">BRIDGE</span>`
                                : `<span style="background:#95a5a6;color:#fff;padding:3px 8px;border-radius:12px;font-size:0.75rem;">${l.status}</span>`
                            }
                            <span style="margin-left:8px;color:#999;font-size:0.85rem">${l.total_rolls_used} roll(s)</span>
                        </div>
                    </div>`).join("");

                $("#sce-existing-logs").html(`
                    <div class="sce-section-title">Existing Draft Logs for ${frappe.utils.escape_html(wo)}</div>
                    ${rows}
                `);
                $(".sce-existing-row").on("click", (e) => {
                    const name = $(e.currentTarget).data("name");
                    this.work_order  = wo;
                    this.log_name    = name;
                    this._refresh_log();
                });
            },
        });
    }

    _start_entry() {
        const wo = $("#sce-wo-input").val().trim();
        if (!wo) {
            frappe.show_alert({ message: "Enter a Work Order ID.", indicator: "red" }, 3);
            return;
        }
        this.work_order  = wo;
        this.fabric_item = $("#sce-fabric-input").val().trim() || null;
        this.log_name    = null;
        this.log_data    = null;
        this.show_cut_form();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STEP 2 — Roll entry screen
    // ══════════════════════════════════════════════════════════════════════════

    show_cut_form() {
        this.page.set_title(`Cut Entry: ${this.work_order}`);
        this.page.set_primary_action("← Change WO", () => this.show_wo_select(), "undo");

        this._render_cut_screen();
    }

    _refresh_log() {
        if (!this.log_name) { this.show_cut_form(); return; }
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_cut_log",
            args: { log_name: this.log_name },
            callback: (r) => {
                if (r.exc || !r.message) { frappe.show_alert({ message: "Reload failed.", indicator: "red" }, 3); return; }
                this.log_data   = r.message;
                this.work_order = r.message.work_order;
                this._render_cut_screen();
            },
        });
    }

    _render_cut_screen() {
        const d     = this.log_data;
        const rolls = d ? d.rolls : [];
        const bridge = d ? d.dye_lot_bridge_detected : false;
        const confirmed = d && d.status === "Confirmed";

        const rolls_html = rolls.length
            ? rolls.map(r => this._roll_card_html(r, bridge)).join("")
            : `<div style="color:#999;text-align:center;padding:20px;">No rolls added yet.</div>`;

        const bridge_html = bridge
            ? `<div class="sce-bridge-banner">
                <div class="sce-bridge-title">⚠️ DYE-LOT BRIDGE DETECTED</div>
                <div class="sce-bridge-sub">
                    Pieces cut from ${new Set(rolls.map(r=>r.dye_lot)).size} different dye lots.<br>
                    Confirm only with supervisor approval.
                </div>
               </div>` : "";

        const confirmed_html = confirmed
            ? `<div class="sce-confirmed-banner">
                <div style="font-size:1.5rem;font-weight:900;">✓ Confirmed</div>
                <div style="margin-top:4px;opacity:.9;">Cut log locked — fabric lot propagated to bins.</div>
               </div>` : "";

        const actions_html = !confirmed
            ? `<div style="display:flex;gap:10px;margin-top:14px;">
                ${!bridge
                    ? `<button class="sce-btn sce-btn-success" id="sce-confirm-btn" style="flex:1">
                            ✓ Confirm Cut
                       </button>`
                    : `<button class="sce-btn sce-btn-danger" id="sce-override-btn" style="flex:1">
                            Supervisor Override + Confirm
                       </button>`
                }
               </div>` : "";

        this.$main.html(`
            <div class="sce-wrap">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                    <div>
                        <div style="font-weight:700;font-size:1.1rem">${frappe.utils.escape_html(this.work_order)}</div>
                        <div style="font-size:0.85rem;color:#999;">
                            ${d ? frappe.utils.escape_html(d.name) + " · " : ""}
                            ${rolls.length} roll(s) · ${d ? d.status : "New"}
                        </div>
                    </div>
                </div>

                ${bridge_html}
                ${confirmed_html}

                <div class="sce-section-title">Rolls Used</div>
                <div id="sce-rolls-list">${rolls_html}</div>

                ${!confirmed ? this._add_roll_form_html() : ""}

                ${actions_html}
            </div>
        `);

        if (!confirmed) {
            this._bind_add_roll_form();
            if (!bridge) {
                $("#sce-confirm-btn").on("click", () => this._do_confirm());
            } else {
                $("#sce-override-btn").on("click", () => this._do_override());
            }
        }
    }

    _roll_card_html(r, bridge) {
        const has_bridge_cls = bridge ? "bridge" : "";
        return `
        <div class="sce-roll-card ${has_bridge_cls}">
            <div class="sce-roll-header">
                <span>🗃 ${frappe.utils.escape_html(r.roll_id)}</span>
                <span style="font-size:0.85rem;padding:3px 10px;border-radius:12px;
                             background:${bridge?"#e74c3c":"#27ae60"};color:#fff">
                    Lot: ${frappe.utils.escape_html(r.dye_lot)}
                </span>
            </div>
            <div class="sce-roll-meta">
                ${r.yardage_used ? `${r.yardage_used} yds &nbsp;·&nbsp; ` : ""}
                ${r.piece_types_cut ? frappe.utils.escape_html(r.piece_types_cut) : ""}
            </div>
            ${r.roll_notes ? `<div style="font-size:0.8rem;color:#999;margin-top:4px;font-style:italic">${frappe.utils.escape_html(r.roll_notes)}</div>` : ""}
        </div>`;
    }

    _add_roll_form_html() {
        return `
        <div class="sce-add-roll-form" id="sce-add-roll-form">
            <div class="sce-section-title">+ Add Roll</div>
            <div class="sce-two-col">
                <div class="sce-field-row" style="margin-bottom:0">
                    <label>Roll ID / Barcode</label>
                    <input class="sce-input" id="sce-roll-id" placeholder="Scan or type…" autocomplete="off"/>
                </div>
                <div class="sce-field-row" style="margin-bottom:0">
                    <label>Dye Lot</label>
                    <input class="sce-input" id="sce-dye-lot" placeholder="e.g. DL-2026-04"/>
                </div>
            </div>
            <div class="sce-two-col" style="margin-top:10px;">
                <div class="sce-field-row" style="margin-bottom:0">
                    <label>Yardage Used</label>
                    <input class="sce-input" id="sce-yardage" type="number" min="0" step="0.1" placeholder="e.g. 3.5"/>
                </div>
                <div class="sce-field-row" style="margin-bottom:0">
                    <label>Piece Types Cut</label>
                    <input class="sce-input" id="sce-pieces" placeholder="Front, Back, Collar…"/>
                </div>
            </div>
            <div class="sce-field-row" style="margin-top:10px;">
                <label>Roll Notes <span style="color:#999;font-weight:400">(optional)</span></label>
                <input class="sce-input" id="sce-roll-notes" placeholder="Any defects or issues…"/>
            </div>
            <div style="margin-top:10px;">
                <button class="sce-btn sce-btn-primary" id="sce-add-roll-btn" style="width:100%;">
                    + Add This Roll
                </button>
            </div>
        </div>`;
    }

    _bind_add_roll_form() {
        $("#sce-roll-id").focus();
        $("#sce-add-roll-btn").on("click", () => this._add_roll());
        // Allow Enter from roll-id to jump to dye-lot
        $("#sce-roll-id").on("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#sce-dye-lot").focus(); } });
        $("#sce-dye-lot").on("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#sce-yardage").focus(); } });
    }

    _add_roll() {
        const roll_id  = $("#sce-roll-id").val().trim();
        const dye_lot  = $("#sce-dye-lot").val().trim();
        if (!roll_id || !dye_lot) {
            frappe.show_alert({ message: "Roll ID and Dye Lot are required.", indicator: "red" }, 3);
            return;
        }
        const args = {
            roll_id,
            dye_lot,
            yardage_used:    parseFloat($("#sce-yardage").val()) || 0,
            piece_types_cut: $("#sce-pieces").val().trim(),
            roll_notes:      $("#sce-roll-notes").val().trim(),
        };

        $("#sce-add-roll-btn").prop("disabled", true).text("Saving…");

        if (!this.log_name) {
            // First roll — create the log
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.create_solid_cut_log",
                args: {
                    work_order:  this.work_order,
                    fabric_item: this.fabric_item || "",
                    rolls:       JSON.stringify([args]),
                },
                callback: (r) => {
                    if (r.exc || !r.message) {
                        frappe.show_alert({ message: "Failed to create log.", indicator: "red" }, 3);
                        $("#sce-add-roll-btn").prop("disabled", false).text("+ Add This Roll");
                        return;
                    }
                    this.log_name = r.message.name;
                    frappe.show_alert({ message: `Roll ${roll_id} added`, indicator: "green" }, 2);
                    this._refresh_log();
                },
            });
        } else {
            frappe.call({
                method: "alice_shop_floor.alice_shop_floor.api.add_roll_to_cut_log",
                args: Object.assign({ log_name: this.log_name }, args),
                callback: (r) => {
                    if (r.exc || !r.message) {
                        frappe.show_alert({ message: "Failed to add roll.", indicator: "red" }, 3);
                        $("#sce-add-roll-btn").prop("disabled", false).text("+ Add This Roll");
                        return;
                    }
                    this.log_data = r.message;
                    frappe.show_alert({ message: `Roll ${roll_id} added`, indicator: "green" }, 2);
                    this._render_cut_screen();
                },
            });
        }
    }

    // ── Confirm actions ──────────────────────────────────────────────────────

    _do_confirm() {
        if (!this.log_name) {
            frappe.show_alert({ message: "Add at least one roll first.", indicator: "orange" }, 3);
            return;
        }
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.confirm_cut_log",
            args: { log_name: this.log_name },
            callback: (r) => {
                if (r.exc) return;
                frappe.show_alert({ message: "Cut log confirmed ✓", indicator: "green" }, 3);
                this._refresh_log();
            },
        });
    }

    _do_override() {
        frappe.prompt(
            [{ fieldname: "supervisor_notes", fieldtype: "Text",
               label: "Supervisor Notes (required for bridge override)", reqd: 1 }],
            (vals) => {
                frappe.call({
                    method: "alice_shop_floor.alice_shop_floor.api.override_bridge_confirm",
                    args: { log_name: this.log_name, supervisor_notes: vals.supervisor_notes },
                    callback: (r) => {
                        if (r.exc) return;
                        frappe.show_alert({ message: "Bridge override confirmed ✓", indicator: "orange" }, 4);
                        this._refresh_log();
                    },
                });
            },
            "Supervisor Bridge Override",
            "Confirm Override"
        );
    }
}
