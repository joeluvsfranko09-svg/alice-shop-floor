/**
 * ALICE — Sewing Bin Scan  (Task #41 + Task #46 enhanced)
 * ========================================================
 * Mobile-optimised page for sewers to scan bin barcodes, advance status,
 * and follow step-by-step sewing instructions in their preferred language.
 *
 * Flow:
 *  1. Sewer opens page on tablet.
 *  2. Taps "Scan Bin" (camera) or types barcode in manual field.
 *  3. Result card: WO, item, station, shade status, machine.
 *  4. Big action button: Pick / Start / Done.
 *  5. [NEW] Instruction panel loads below — garment photo + notes +
 *     step-by-step cards with stitch type, machine setting, step photo,
 *     and instruction text in the operator's preferred language.
 *  6. Language switcher lets sewer change language on the fly.
 *
 * API endpoints used:
 *  - alice_shop_floor.api.scan_bin_action
 *  - alice_shop_floor.api.get_bin_instructions  (Task #43)
 *  - alice_shop_floor.api.get_active_languages   (Task #43)
 */

frappe.pages["sewing-bin-scan"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Scan Bin",
        single_column: true,
    });
    window.sewing_scan = new SewingBinScanApp(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class SewingBinScanApp {
    constructor(page, wrapper) {
        this.page    = page;
        this.wrapper = wrapper;
        this.$body   = $(wrapper).find(".layout-main-section, .page-content").first();

        // State
        this.current_assignment = null;
        this.active_step        = 0;   // 0-based index
        this.instructions       = null; // from get_bin_instructions
        this.language           = null; // set after load

        this._init_styles();
        this._init_layout();
        this._bind_events();
        this._load_languages();
    }

    // ── Layout ────────────────────────────────────────────────────────────────

    _init_layout() {
        this.$body.html(`
            <div class="sbs-wrap">
                <div class="sbs-scan-zone">
                    <div class="sbs-hero">
                        <div class="sbs-icon">📦</div>
                        <button class="sbs-btn sbs-btn-scan" id="sbs-open-btn">Scan Bin</button>
                        <div class="sbs-manual-row">
                            <input type="text" id="sbs-manual-input"
                                placeholder="Or type / paste barcode…"
                                autocomplete="off" autocorrect="off" spellcheck="false"/>
                            <button id="sbs-manual-go" class="sbs-btn-sm">Go</button>
                        </div>
                    </div>
                    <div id="sbs-result"></div>
                </div>
                <div id="sbs-instructions" class="sbs-instructions" style="display:none;"></div>
            </div>
        `);
    }

    _bind_events() {
        this.$body.on("click", "#sbs-open-btn", () => this._open_scanner());
        this.$body.on("click", "#sbs-manual-go", () => this._manual_scan());
        this.$body.on("keydown", "#sbs-manual-input", (e) => {
            if (e.key === "Enter") this._manual_scan();
        });
    }

    // ── Language ──────────────────────────────────────────────────────────────

    _load_languages() {
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_active_languages",
            callback: (r) => {
                this._active_languages = r.message || [{ code: "en", name: "English" }];
            },
        });
        // Detect operator's preferred language
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_operator_language_code",
            callback: (r) => {
                this.language = r.message || "en";
            },
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SCAN FLOW (preserved from Task #41)
    // ══════════════════════════════════════════════════════════════════════════

    _open_scanner() {
        if (frappe.ui.Scanner) {
            const scanner = new frappe.ui.Scanner({
                dialog: true,
                multiple: false,
                on_scan: (data) => {
                    const barcode = data && data.decodedText
                        ? data.decodedText.trim()
                        : (typeof data === "string" ? data.trim() : "");
                    if (barcode) this.handle_scan(barcode);
                },
            });
            scanner.show();
        } else {
            frappe.msgprint("Camera scanner not available — use the manual input below.");
        }
    }

    _manual_scan() {
        const val = $("#sbs-manual-input").val().trim();
        if (val) this.handle_scan(val);
    }

    handle_scan(barcode) {
        if (!barcode) return;
        frappe.show_alert({ message: `Scanning ${barcode}…`, indicator: "blue" });
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.scan_bin_action",
            args: { barcode, operator: frappe.session.user },
            callback: (r) => {
                if (r.message) {
                    this._render_result(r.message);
                    this.current_assignment = r.message.assignment;
                    this.active_step = 0;
                    this._load_instructions(r.message.assignment);
                }
            },
            error: (err) => {
                this._render_error(err.message || "Barcode not found.");
                $("#sbs-instructions").hide();
            },
        });
    }

    _render_result(res) {
        const s_lower = (res.new_status || "").toLowerCase().replace(/\s+/g, "");
        const status_class = {
            queued:     "status-queued",
            picked:     "status-picked",
            inprogress: "status-inprog",
            complete:   "status-complete",
            returned:   "status-returned",
            kitting:    "status-queued",
            kitready:   "status-picked",
        }[s_lower] || "status-queued";

        const priority_html = res.priority === "Rush"
            ? `<span class="sbs-priority-rush">🔴 RUSH</span> `
            : res.priority === "High"
            ? `<span class="sbs-priority-high">🟠 HIGH</span> `
            : "";

        const shade_badge    = this._shade_badge(res.bundle_shade_status);
        const action_btn     = this._action_btn(res);
        const confirm_banner = res.action_taken !== "none"
            ? `<div class="sbs-confirm ok">${frappe.utils.escape_html(res.message)}</div>`
            : "";
        const machine_warn = res.machine_match === 0
            ? `<span style="color:#c00;font-weight:600;">⚠ Machine mismatch</span>` : "";

        $("#sbs-result").html(`
            <div class="sbs-result-card ${status_class}"
                 data-assignment="${frappe.utils.escape_html(res.assignment)}">
                <div class="sbs-rc-status">Status: ${frappe.utils.escape_html(res.new_status)}</div>
                <div class="sbs-rc-wo">${priority_html}${frappe.utils.escape_html(res.work_order)}</div>
                <div class="sbs-rc-item">${frappe.utils.escape_html(res.production_item)}</div>
                <div class="sbs-rc-row">🪡 Station: <strong>${frappe.utils.escape_html(res.station_code || "—")}</strong>
                    ${machine_warn}</div>
                <div class="sbs-rc-row">👤 Operator: <strong>${frappe.utils.escape_html(res.operator || "Unassigned")}</strong></div>
                ${shade_badge}
                ${confirm_banner}
                ${action_btn}
            </div>
        `);
    }

    _shade_badge(shade_status) {
        if (!shade_status) return "";
        const { status, zones, cleared, pieces_cut, pieces_expected } = shade_status;
        if (status === "Complete")
            return `<span class="sbs-shade shade-ok">✓ Bundle complete</span>`;
        if (status === "Shade Warning")
            return `<span class="sbs-shade shade-warning">⚠ Shade variation · ${zones} zones — cleared</span>`;
        if (status === "Shade Mismatch" && cleared)
            return `<span class="sbs-shade shade-warning">⚠ Shade mismatch — supervisor cleared</span>`;
        if (status === "Shade Mismatch")
            return `<span class="sbs-shade shade-mismatch">🔴 Shade mismatch — awaiting supervisor</span>`;
        if (status === "Incomplete")
            return `<span class="sbs-shade shade-incomplete">⚪ ${pieces_cut}/${pieces_expected} pieces cut</span>`;
        return "";
    }

    _action_btn(res) {
        const s = res.new_status;
        const a = (action, label, cls) =>
            `<button class="sbs-action-btn ${cls}"
                onclick="sewing_scan.do_action('${action}','${res.assignment}')">
                ${label}
             </button>`;
        if (s === "Queued" || s === "Kit Ready") return a("pick",  "PICK THIS BIN", "btn-pick");
        if (s === "Picked")                       return a("start", "START SEWING",  "btn-start");
        if (s === "In Progress")                  return a("done",  "✓ MARK DONE",   "btn-done");
        return `<button class="sbs-action-btn btn-noaction" disabled>
                    ${s === "Complete" ? "✓ Complete" : frappe.utils.escape_html(s)}
                </button>`;
    }

    do_action(action, assignment_name) {
        const METHOD_MAP = {
            pick:  "alice_shop_floor.alice_shop_floor.api.bin_mark_picked",
            start: "alice_shop_floor.alice_shop_floor.api.bin_mark_in_progress",
            done:  "alice_shop_floor.alice_shop_floor.api.bin_mark_complete",
        };
        if (action === "done") {
            frappe.confirm(
                "Mark this bundle as <strong>complete</strong>?",
                () => {
                    frappe.call({
                        method: METHOD_MAP.done,
                        args: { assignment_name },
                        callback: () => {
                            frappe.show_alert({ message: "Bundle complete ✓", indicator: "green" });
                            this.handle_scan(assignment_name);
                        },
                    });
                }
            );
            return;
        }
        frappe.call({
            method: METHOD_MAP[action],
            args: { assignment_name },
            callback: () => {
                frappe.show_alert({
                    indicator: "green",
                    message: action === "pick" ? "Picked ✓" : "Started ✓",
                });
                this.handle_scan(assignment_name);
            },
        });
    }

    _render_error(msg) {
        $("#sbs-result").html(
            `<div class="sbs-confirm err">⛔ ${frappe.utils.escape_html(msg)}</div>`
        );
    }

    // ══════════════════════════════════════════════════════════════════════════
    // INSTRUCTIONS PANEL (Task #46)
    // ══════════════════════════════════════════════════════════════════════════

    _load_instructions(assignment_name) {
        const $panel = $("#sbs-instructions");
        $panel.html(`<div style="text-align:center;padding:20px;color:#999">Loading instructions…</div>`).show();

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.get_bin_instructions",
            args: {
                assignment_name,
                language_code: this.language || "en",
            },
            callback: (r) => {
                if (r.exc || !r.message || !r.message.steps || !r.message.steps.length) {
                    $panel.html(`
                        <div class="sbs-no-instructions">
                            <span style="font-size:1.5rem">📋</span><br>
                            No sewing instructions found for this item.
                        </div>
                    `).show();
                    return;
                }
                this.instructions = r.message;
                this.language     = r.message.language_used || this.language || "en";
                this.active_step  = 0;
                this._render_instruction_panel();
            },
        });
    }

    _render_instruction_panel() {
        const d = this.instructions;
        if (!d) return;

        const steps   = d.steps || [];
        const total   = steps.length;
        const step    = steps[this.active_step];

        // Garment photo + notes header
        const photo_html = d.garment_photo
            ? `<img src="${frappe.utils.escape_html(d.garment_photo)}"
                    class="sbs-garment-photo" alt="Garment reference photo"/>`
            : "";
        const notes_html = d.notes
            ? `<div class="sbs-garment-notes">${frappe.utils.escape_html(d.notes)}</div>`
            : "";

        // Language switcher
        const langs = this._active_languages || [{ code: "en", name: "English" }];
        const lang_opts = langs.map(l =>
            `<option value="${l.code}" ${l.code === this.language ? "selected" : ""}>${l.name}</option>`
        ).join("");
        const lang_html = langs.length > 1
            ? `<div class="sbs-lang-row">
                <label class="sbs-lang-label">Language:</label>
                <select id="sbs-lang-select" class="sbs-lang-select">${lang_opts}</select>
               </div>` : "";

        // Step navigation dots
        const dots = steps.map((_, i) =>
            `<span class="sbs-dot ${i === this.active_step ? "active" : ""}"
                  data-step="${i}"></span>`
        ).join("");

        // Current step content
        const step_html = step ? this._step_card_html(step, this.active_step, total) : "";

        $("#sbs-instructions").html(`
            <div class="sbs-instr-panel">
                <div class="sbs-instr-header">
                    ${photo_html}
                    <div class="sbs-instr-title">Sewing Instructions</div>
                    <div class="sbs-instr-item">${frappe.utils.escape_html(d.item || d.work_order || "")}</div>
                    ${notes_html}
                    ${lang_html}
                </div>

                <div class="sbs-dots">${dots}</div>

                <div id="sbs-step-body">${step_html}</div>

                <div class="sbs-step-nav">
                    <button class="sbs-nav-btn" id="sbs-prev-btn"
                            ${this.active_step === 0 ? "disabled" : ""}>← Prev</button>
                    <span class="sbs-step-counter">${this.active_step + 1} / ${total}</span>
                    <button class="sbs-nav-btn sbs-nav-btn-next" id="sbs-next-btn"
                            ${this.active_step >= total - 1 ? "disabled" : ""}>Next →</button>
                </div>
            </div>
        `).show();

        // Navigation bindings
        $("#sbs-prev-btn").off("click").on("click", () => {
            if (this.active_step > 0) { this.active_step--; this._render_instruction_panel(); }
        });
        $("#sbs-next-btn").off("click").on("click", () => {
            if (this.active_step < total - 1) { this.active_step++; this._render_instruction_panel(); }
        });
        // Dot clicks
        $(".sbs-dot").off("click").on("click", (e) => {
            this.active_step = parseInt($(e.target).data("step"));
            this._render_instruction_panel();
        });
        // Language switch
        $("#sbs-lang-select").off("change").on("change", (e) => {
            const new_lang = $(e.target).val();
            if (new_lang !== this.language) {
                this.language = new_lang;
                this._load_instructions(this.current_assignment);
            }
        });

        // Swipe gesture on the step body
        this._bind_swipe("#sbs-step-body", steps.length);
    }

    _step_card_html(step, idx, total) {
        const step_photo_html = step.step_photo
            ? `<img src="${frappe.utils.escape_html(step.step_photo)}"
                    class="sbs-step-photo" alt="Step ${idx + 1} reference photo"/>`
            : "";
        const meta_parts = [
            step.piece_type  ? `<span class="sbs-step-tag">${frappe.utils.escape_html(step.piece_type)}</span>` : "",
            step.stitch_type ? `<span class="sbs-step-tag sbs-tag-stitch">🪡 ${frappe.utils.escape_html(step.stitch_type)}</span>` : "",
            step.machine_setting ? `<span class="sbs-step-tag sbs-tag-machine">⚙ ${frappe.utils.escape_html(step.machine_setting)}</span>` : "",
        ].filter(Boolean).join(" ");

        return `
        <div class="sbs-step-card">
            <div class="sbs-step-num">Step ${idx + 1}</div>
            ${step_photo_html}
            <div class="sbs-step-meta">${meta_parts}</div>
            <div class="sbs-step-text">${step.instruction_text || ""}</div>
        </div>`;
    }

    // ── Swipe support ─────────────────────────────────────────────────────────

    _bind_swipe(selector, total) {
        const $el = $(selector);
        let startX = null;
        $el.off("touchstart touchend").on("touchstart", (e) => {
            startX = e.originalEvent.touches[0].clientX;
        }).on("touchend", (e) => {
            if (startX === null) return;
            const dx = e.originalEvent.changedTouches[0].clientX - startX;
            startX = null;
            if (Math.abs(dx) < 40) return;
            if (dx < 0 && this.active_step < total - 1) {
                this.active_step++;
                this._render_instruction_panel();
            } else if (dx > 0 && this.active_step > 0) {
                this.active_step--;
                this._render_instruction_panel();
            }
        });
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STYLES
    // ══════════════════════════════════════════════════════════════════════════

    _init_styles() {
        if (document.getElementById("sbs-styles")) return;
        const css = `
/* ── Wrapper ── */
.sbs-wrap { max-width:480px; margin:0 auto; padding:0 10px 40px; }

/* ── Scan hero ── */
.sbs-hero { text-align:center; padding:28px 16px; }
.sbs-icon { font-size:64px; line-height:1; margin-bottom:10px; }
.sbs-btn-scan {
    width:100%; padding:16px; font-size:1.15rem; font-weight:700;
    border:none; border-radius:12px; cursor:pointer;
    background:#4895ef; color:#fff;
    box-shadow:0 4px 12px rgba(72,149,239,.35);
    transition:opacity .15s;
}
.sbs-btn-scan:hover { opacity:.88; }
.sbs-manual-row { margin-top:14px; display:flex; gap:8px; }
.sbs-manual-row input {
    flex:1; padding:10px 12px; border:1.5px solid #ddd;
    border-radius:8px; font-size:14px;
}
.sbs-btn-sm {
    padding:10px 14px; border:none; border-radius:8px;
    background:#6c757d; color:#fff; cursor:pointer; font-size:13px;
}

/* ── Result card ── */
.sbs-result-card {
    border-radius:12px; padding:18px 16px;
    box-shadow:0 2px 12px rgba(0,0,0,.1);
    margin-top:16px;
}
.sbs-result-card.status-queued  { background:#fffbea; border:2px solid #f9c74f; }
.sbs-result-card.status-picked  { background:#eff6ff; border:2px solid #4895ef; }
.sbs-result-card.status-inprog  { background:#e8faf0; border:2px solid #38b000; }
.sbs-result-card.status-complete{ background:#f0fdf4; border:2px solid #15803d; }
.sbs-result-card.status-returned{ background:#fef2f2; border:2px solid #dc2626; }
.sbs-rc-status {
    font-size:11px; font-weight:700; text-transform:uppercase;
    letter-spacing:.06em; color:#666; margin-bottom:5px;
}
.sbs-rc-wo   { font-size:1.3rem; font-weight:800; color:#111; }
.sbs-rc-item { font-size:13px; color:#555; margin-bottom:7px; }
.sbs-rc-row  { font-size:13px; color:#444; margin:3px 0; }
.sbs-shade {
    display:inline-block; font-size:11px; font-weight:600;
    padding:2px 8px; border-radius:99px; margin-top:4px;
}
.shade-ok         { background:#d1fae5; color:#065f46; }
.shade-warning    { background:#fef3c7; color:#92400e; }
.shade-mismatch   { background:#fee2e2; color:#991b1b; }
.shade-incomplete { background:#f3f4f6; color:#6b7280; }
.sbs-action-btn {
    width:100%; margin-top:14px; padding:14px;
    font-size:1.1rem; font-weight:700;
    border:none; border-radius:10px; cursor:pointer; transition:opacity .15s;
}
.sbs-action-btn:hover { opacity:.88; }
.btn-pick    { background:#4895ef; color:#fff; }
.btn-start   { background:#38b000; color:#fff; }
.btn-done    { background:#2d6a4f; color:#fff; }
.btn-noaction{ background:#e5e5e5; color:#888; cursor:default; }
.sbs-confirm {
    border-radius:10px; padding:12px 14px; margin-top:10px;
    font-size:14px; font-weight:600; text-align:center;
}
.sbs-confirm.ok  { background:#d1fae5; color:#065f46; }
.sbs-confirm.err { background:#fee2e2; color:#991b1b; }
.sbs-priority-rush { color:#c00; font-weight:700; }
.sbs-priority-high { color:#e07800; font-weight:600; }

/* ── Instructions panel ── */
.sbs-instructions { margin-top:20px; }
.sbs-no-instructions {
    text-align:center; color:#999; padding:24px;
    background:#f9f9f9; border-radius:10px;
    font-size:0.9rem;
}
.sbs-instr-panel { background:#fff; border-radius:14px; overflow:hidden;
    box-shadow:0 2px 16px rgba(0,0,0,.1); }
.sbs-instr-header { padding:16px 16px 0; }
.sbs-garment-photo {
    width:100%; max-height:200px; object-fit:cover;
    border-radius:10px; margin-bottom:10px; display:block;
}
.sbs-instr-title { font-size:0.75rem; font-weight:700; text-transform:uppercase;
    letter-spacing:.08em; color:#999; margin-bottom:2px; }
.sbs-instr-item { font-size:1rem; font-weight:700; color:#1a1a2e; margin-bottom:6px; }
.sbs-garment-notes {
    background:#fef9e7; border-left:3px solid #f39c12;
    padding:8px 10px; font-size:0.85rem; color:#7d6608;
    border-radius:0 6px 6px 0; margin-bottom:10px;
}
.sbs-lang-row {
    display:flex; align-items:center; gap:8px; padding-bottom:12px;
}
.sbs-lang-label { font-size:0.8rem; color:#999; font-weight:600; }
.sbs-lang-select {
    border:1.5px solid #ddd; border-radius:6px;
    padding:5px 10px; font-size:0.85rem; background:#fff;
}

/* ── Navigation dots ── */
.sbs-dots { display:flex; justify-content:center; gap:7px; padding:10px 0 2px; }
.sbs-dot {
    width:8px; height:8px; border-radius:50%;
    background:#ddd; cursor:pointer; transition:background .2s, transform .2s;
}
.sbs-dot.active { background:#4895ef; transform:scale(1.35); }

/* ── Step card ── */
.sbs-step-card { padding:14px 16px 6px; }
.sbs-step-num {
    font-size:0.75rem; font-weight:700; text-transform:uppercase;
    letter-spacing:.1em; color:#4895ef; margin-bottom:6px;
}
.sbs-step-photo {
    width:100%; max-height:180px; object-fit:cover;
    border-radius:10px; margin-bottom:10px; display:block;
}
.sbs-step-meta { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
.sbs-step-tag {
    background:#eef2ff; color:#3730a3;
    padding:3px 9px; border-radius:99px; font-size:0.78rem; font-weight:600;
}
.sbs-tag-stitch  { background:#ecfdf5; color:#065f46; }
.sbs-tag-machine { background:#fff7ed; color:#92400e; }
.sbs-step-text {
    font-size:0.95rem; line-height:1.6; color:#333;
    padding-bottom:10px;
}
/* strip extra HTML tags from rich text gracefully */
.sbs-step-text p { margin:0 0 6px; }

/* ── Step navigation ── */
.sbs-step-nav {
    display:flex; justify-content:space-between; align-items:center;
    padding:10px 16px 16px; border-top:1px solid #f0f0f0; margin-top:4px;
}
.sbs-step-counter { font-size:0.85rem; color:#999; font-weight:600; }
.sbs-nav-btn {
    padding:9px 18px; border:none; border-radius:8px;
    font-size:0.9rem; font-weight:700; cursor:pointer;
    background:#4895ef; color:#fff; transition:opacity .15s;
}
.sbs-nav-btn:disabled { background:#e5e5e5; color:#aaa; cursor:default; }
.sbs-nav-btn-next { background:#38b000; }
.sbs-nav-btn-next:disabled { background:#e5e5e5; color:#aaa; }
`;
        $("<style id='sbs-styles'>").text(css).appendTo("head");
    }
}
