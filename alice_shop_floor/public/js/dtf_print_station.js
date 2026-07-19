/**
 * ALICE — DTF Print Station  (Task #65 + Task #71: multi-machine + operator certs)
 * ==================================================================================
 * Tablet page mounted at the Epson SureColor G6070 (35" DTF printer).
 * Operator scans a Job Card → machine picker shows all active DTF printers →
 * operator selector shows who's certified → "Send to Printer" dispatches.
 *
 * DTF workflow (this page handles step 1):
 *   Step 1  THIS PAGE — G6070 prints DTF film
 *   Step 2  Dryer cures film (~2 min, automatic)
 *   Step 3  dtf-press-station — operator transfers to garment
 *   Step 4  V6 Press QC Inspector — camera capture
 *
 * Flow:
 *   1. Operator opens page on print-station tablet.
 *   2. Scans / types Job Card name.
 *   3. Machine picker shows all active DTF printers — operator taps one.
 *   4. Operator selector shows certified operators — operator taps self.
 *   5. Artwork preview + print params appear.
 *   6. "Send to Printer" enabled only after machine + operator selected.
 *   7. Status badge polls every 4s: Queued → Printing → Complete.
 *   8. Taps "Film Ready — Send to Dryer" → Job Card stamped.
 *
 * API endpoints:
 *   alice_shop_floor.alice_shop_floor.api.dtf_scan_and_load
 *   alice_shop_floor.alice_shop_floor.api.dtf_start_print
 *   alice_shop_floor.alice_shop_floor.api.dtf_print_status
 *   alice_shop_floor.alice_shop_floor.api.dtf_film_ready
 */

frappe.pages["dtf-print-station"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "DTF Print Station",
        single_column: true,
    });
    window.dtf_print = new DTFPrintStation(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class DTFPrintStation {
    constructor(page, wrapper) {
        this.page    = page;
        this.wrapper = wrapper;
        this.$body   = $(wrapper).find(".layout-main-section, .page-content").first();

        // State
        this.job_card         = null;
        this.machine_job_id   = null;
        this.machine_name     = null;   // currently selected machine config name
        this.selected_machine = null;   // full machine record from picker
        this.selected_operator = null;  // employee id of selected operator
        this.poll_timer       = null;
        this.state            = "idle"; // idle | loaded | sending | printing | film_ready | done

        this._init_styles();
        this._init_layout();
        this._bind_events();
        this._setup_realtime();
    }

    // ── Layout ────────────────────────────────────────────────────────────────

    _init_layout() {
        this.$body.html(`
            <div class="dtp-wrap">

                <!-- Top bar: machine status + scan input -->
                <div class="dtp-topbar">
                    <div class="dtp-machine-info">
                        <span class="dtp-machine-icon">🖨</span>
                        <span class="dtp-machine-label" id="dtp-machine-label">DTF Print Station</span>
                        <span class="dtp-machine-dot" id="dtp-machine-dot" title="Select a machine"></span>
                    </div>
                    <div class="dtp-scan-row">
                        <input type="text" id="dtp-jc-input"
                            placeholder="Scan or type Job Card…"
                            autocomplete="off" autocorrect="off"
                            autocapitalize="characters" spellcheck="false"/>
                        <button class="dtp-btn dtp-btn-load" id="dtp-load-btn">LOAD</button>
                        <button class="dtp-btn dtp-btn-reset" id="dtp-reset-btn" style="display:none;">✕</button>
                    </div>
                    <div class="dtp-scan-msg" id="dtp-scan-msg"></div>
                </div>

                <!-- Machine picker — shown after scan -->
                <div id="dtp-machine-picker-section" style="display:none;">
                    <div class="dtp-picker-header">SELECT PRINTER</div>
                    <div class="dtp-machine-grid" id="dtp-machine-grid"></div>
                </div>

                <!-- Operator selector — shown after machine selected -->
                <div id="dtp-operator-section" style="display:none;">
                    <div class="dtp-picker-header">SELECT OPERATOR</div>
                    <div class="dtp-operator-grid" id="dtp-operator-grid"></div>
                    <div class="dtp-op-warn" id="dtp-op-warn" style="display:none;"></div>
                </div>

                <!-- Main card — hidden until job loaded -->
                <div class="dtp-main-card" id="dtp-main-card" style="display:none;">

                    <!-- Two-column: artwork left, info right -->
                    <div class="dtp-columns">

                        <!-- Artwork preview -->
                        <div class="dtp-artwork-panel" id="dtp-artwork-panel">
                            <div class="dtp-artwork-label">ARTWORK</div>
                            <div class="dtp-artwork-wrap" id="dtp-artwork-wrap">
                                <div class="dtp-artwork-placeholder" id="dtp-artwork-placeholder">
                                    <span>No preview</span>
                                </div>
                                <img id="dtp-artwork-img" class="dtp-artwork-img" style="display:none;" alt="Design artwork"/>
                            </div>
                            <div class="dtp-placement-badge" id="dtp-placement-badge"></div>
                        </div>

                        <!-- Right panel: order info + print params + actions -->
                        <div class="dtp-info-panel">

                            <!-- Job header -->
                            <div class="dtp-job-header" id="dtp-job-header"></div>

                            <!-- Print params: film width, resolution, ink, mode -->
                            <div class="dtp-params-section">
                                <div class="dtp-section-label">PRINT PARAMETERS</div>
                                <div class="dtp-params-grid">
                                    <div class="dtp-param-row">
                                        <span class="dtp-param-key">Film width</span>
                                        <span class="dtp-param-val" id="dtp-film-width">—</span>
                                    </div>
                                    <div class="dtp-param-row">
                                        <span class="dtp-param-key">Resolution</span>
                                        <span class="dtp-param-val" id="dtp-resolution">—</span>
                                    </div>
                                    <div class="dtp-param-row">
                                        <span class="dtp-param-key">Color mode</span>
                                        <span class="dtp-param-val" id="dtp-color-mode">—</span>
                                    </div>
                                    <div class="dtp-param-row">
                                        <span class="dtp-param-key">White ink</span>
                                        <span class="dtp-param-val" id="dtp-white-ink">—</span>
                                    </div>
                                    <div class="dtp-param-row">
                                        <span class="dtp-param-key">Peel type</span>
                                        <span class="dtp-param-val" id="dtp-peel-type">—</span>
                                    </div>
                                </div>
                            </div>

                            <!-- Garment details -->
                            <div class="dtp-garment-section" id="dtp-garment-section"></div>

                            <!-- Print status badge -->
                            <div class="dtp-status-wrap" id="dtp-status-wrap" style="display:none;">
                                <div class="dtp-status-badge" id="dtp-status-badge">QUEUED</div>
                                <div class="dtp-status-detail" id="dtp-status-detail"></div>
                            </div>

                            <!-- Actions -->
                            <div class="dtp-actions">
                                <button class="dtp-btn dtp-btn-print" id="dtp-print-btn" disabled>
                                    ▶ SEND TO PRINTER
                                </button>
                                <button class="dtp-btn dtp-btn-film-ready" id="dtp-film-btn"
                                        style="display:none;">
                                    ✓ FILM READY — SEND TO DRYER
                                </button>
                            </div>

                        </div>
                    </div>

                </div>

            </div>
        `);
    }

    _bind_events() {
        const $b = this.$body;
        $b.on("click",  "#dtp-load-btn",  () => this._load_job());
        $b.on("click",  "#dtp-reset-btn", () => this._reset());
        $b.on("click",  "#dtp-print-btn", () => this._send_to_printer());
        $b.on("click",  "#dtp-film-btn",  () => this._film_ready());
        $b.on("keydown","#dtp-jc-input",  (e) => { if (e.key === "Enter") this._load_job(); });
        $b.on("error",  "#dtp-artwork-img", () => this._show_artwork_placeholder());
        setTimeout(() => $b.find("#dtp-jc-input").focus(), 300);
    }

    _setup_realtime() {
        frappe.realtime.on("machine_offline_alert", (data) => {
            if (this.selected_machine && data.machine_name === this.selected_machine) {
                this._set_machine_dot(false);
                frappe.show_alert({
                    message: `⚠️ ${data.machine_name} went offline`,
                    indicator: "red",
                }, 8);
            }
        });
    }

    // ── Load job ──────────────────────────────────────────────────────────────

    _load_job() {
        const jc_name = this.$body.find("#dtp-jc-input").val().trim().toUpperCase();
        if (!jc_name) { this._set_msg("Scan or enter a Job Card.", "warn"); return; }

        this._set_msg("Loading…");
        this.$body.find("#dtp-load-btn").prop("disabled", true);

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtf_scan_and_load",
            args:   { job_card_name: jc_name },
            callback: (r) => {
                this.$body.find("#dtp-load-btn").prop("disabled", false);
                const res = r.message || {};
                if (!res.ok) { this._on_load_error(res); return; }
                this.job_card = res.job_card || jc_name;
                this.state    = "loaded";
                this._render_job(res);
            },
            error: () => {
                this.$body.find("#dtp-load-btn").prop("disabled", false);
                this._set_msg("Server error — please retry.", "error");
            },
        });
    }

    _on_load_error(res) {
        const msgs = {
            "not_dtf_job":  `Not a DTF job — use the correct station.`,
            "not_routed":   "Job not yet routed.",
            "no_recipe":    "No Production Recipe assigned.",
        };
        this._set_msg(msgs[res.error] || `Error: ${res.error || "unknown"}`, "error");
    }

    _render_job(res) {
        this._stored_res = res;
        const params = res.params || {};

        // Reset picker state
        this.selected_machine  = null;
        this.selected_operator = null;

        // Artwork
        const design_url = res.design_file_url || res.design_file || "";
        if (design_url && /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(design_url)) {
            this.$body.find("#dtp-artwork-img").attr("src", design_url).show();
            this.$body.find("#dtp-artwork-placeholder").hide();
        } else {
            this._show_artwork_placeholder(design_url ? "PDF / non-image" : "No file");
        }
        this.$body.find("#dtp-placement-badge")
            .text(res.design_placement || "").toggle(!!(res.design_placement));

        // Job header
        this.$body.find("#dtp-job-header").html(`
            <div class="dtp-jc-name">${this.job_card}</div>
            <div class="dtp-recipe-name">${res.recipe || ""}</div>
        `);

        // Print params
        const film_w = params.film_width_inches || "—";
        const peel   = params.peel_type || "Hot";
        this.$body.find("#dtp-film-width").text(`${film_w}"`);
        this.$body.find("#dtp-resolution").text(`${params.resolution_dpi || "1200"} DPI`);
        this.$body.find("#dtp-color-mode").text(params.color_mode || "CMYK");
        const w_ink = params.white_ink === false ? "OFF" : "ON";
        this.$body.find("#dtp-white-ink").text(w_ink)
            .toggleClass("dtp-val-on", w_ink === "ON")
            .toggleClass("dtp-val-off", w_ink === "OFF");
        this.$body.find("#dtp-peel-type").text(`${peel} Peel`);

        // Garment info
        const jc_info = [
            res.garment_color    ? `Color: ${res.garment_color}`    : "",
            res.garment_size     ? `Size: ${res.garment_size}`      : "",
            res.fabric_type      ? `Fabric: ${res.fabric_type}`     : "",
            res.customer_name    ? `Customer: ${res.customer_name}` : "",
            res.garment_passport ? `Passport: ${res.garment_passport}` : "",
        ].filter(Boolean);
        this.$body.find("#dtp-garment-section").html(
            jc_info.length
                ? `<div class="dtp-section-label">ORDER</div>
                   ${jc_info.map(i => `<div class="dtp-order-row">${i}</div>`).join("")}`
                : ""
        );

        // Show layout — send stays disabled until machine + operator chosen
        this.$body.find("#dtp-main-card").show();
        this.$body.find("#dtp-reset-btn").show();
        this.$body.find("#dtp-jc-input").prop("disabled", true);
        this.$body.find("#dtp-load-btn").hide();
        this.$body.find("#dtp-print-btn").prop("disabled", true).show();
        this.$body.find("#dtp-film-btn").hide();
        this.$body.find("#dtp-status-wrap").hide();
        this._set_msg("");

        // Render machine picker
        this._render_machine_picker(res.available_machines || []);

        // Pre-render operator selector (hidden until machine chosen)
        this._stored_operators = res.certified_operators || [];
        this.$body.find("#dtp-operator-section").hide();
    }

    // ── Machine picker ────────────────────────────────────────────────────────

    _render_machine_picker(machines) {
        const $section = this.$body.find("#dtp-machine-picker-section");
        const $grid    = this.$body.find("#dtp-machine-grid");

        if (!machines.length) {
            $grid.html(`<div class="dtp-no-machines">No active DTF printers found — check Machine Config.</div>`);
            $section.show();
            return;
        }

        const cards = machines.map(m => {
            const online_color = m.online ? "#22c55e" : "#ef4444";
            const busy_label   = m.busy ? `<span class="dtp-mc-busy">BUSY — ${m.current_job || "job in progress"}</span>` : "";
            return `
                <div class="dtp-mc-card" data-name="${m.name}">
                    <div class="dtp-mc-toprow">
                        <span class="dtp-mc-dot" style="background:${online_color}"></span>
                        <span class="dtp-mc-id">${m.machine_id || m.name}</span>
                    </div>
                    <div class="dtp-mc-status">${m.online ? "Online" : "Offline"}</div>
                    ${busy_label}
                </div>
            `;
        }).join("");

        $grid.html(cards);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".dtp-mc-card", function () {
            const name = $(this).data("name");
            const m    = machines.find(x => x.name === name);
            if (!m) return;

            me.selected_machine = name;
            me.machine_name     = name;

            // Visual selection
            $grid.find(".dtp-mc-card").removeClass("dtp-mc-selected");
            $(this).addClass("dtp-mc-selected");

            // Update topbar
            me.$body.find("#dtp-machine-label").text(`Epson G6070 — ${m.machine_id || m.name}`);
            me._set_machine_dot(m.online);

            if (m.busy) {
                frappe.show_alert({
                    message: `⚠️ ${m.machine_id} has job ${m.current_job || ""} in progress`,
                    indicator: "orange",
                }, 6);
            }

            // Show operator selector
            me._render_operator_selector(me._stored_operators || []);
        });
    }

    // ── Operator selector ─────────────────────────────────────────────────────

    _render_operator_selector(operators) {
        const $section = this.$body.find("#dtp-operator-section");
        const $grid    = this.$body.find("#dtp-operator-grid");

        const LEVEL_COLOR = { Expert: "#7c3aed", Certified: "#2563eb", Trainee: "#d97706" };
        const LEVEL_ICON  = { Expert: "★", Certified: "✓", Trainee: "◎" };

        if (!operators.length) {
            $grid.html(`<div class="dtp-op-empty">No certified operators found for DTF. Contact your supervisor.</div>`);
            $section.show();
            return;
        }

        const cards = operators.map(op => {
            const lvl   = op.proficiency_level || "Certified";
            const color = LEVEL_COLOR[lvl] || "#2563eb";
            const icon  = LEVEL_ICON[lvl]  || "✓";
            return `
                <div class="dtp-op-card" data-emp="${op.employee}">
                    <div class="dtp-op-name">${op.employee_name || op.employee}</div>
                    <div class="dtp-op-level" style="color:${color}">${icon} ${lvl}</div>
                </div>
            `;
        }).join("");

        // "Not listed" escape hatch
        const not_listed = `
            <div class="dtp-op-card dtp-op-unlisted" data-emp="__unlisted__">
                <div class="dtp-op-name">Not listed</div>
                <div class="dtp-op-level" style="color:#6b7280">⚠ Supervisor override</div>
            </div>
        `;

        $grid.html(cards + not_listed);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".dtp-op-card", function () {
            const emp = $(this).data("emp");
            $grid.find(".dtp-op-card").removeClass("dtp-op-selected");
            $(this).addClass("dtp-op-selected");
            me.selected_operator = emp;

            if (emp === "__unlisted__") {
                me.$body.find("#dtp-op-warn")
                    .text("⚠ Supervisor override: log this in the shift notes.")
                    .show();
            } else {
                me.$body.find("#dtp-op-warn").hide();
            }

            // Enable send now that both machine + operator are chosen
            me.$body.find("#dtp-print-btn").prop("disabled", false);
        });
    }

    _show_artwork_placeholder(msg) {
        this.$body.find("#dtp-artwork-img").hide();
        this.$body.find("#dtp-artwork-placeholder")
            .html(`<span>${msg || "No preview available"}</span>`)
            .show();
    }

    // ── Send to printer ───────────────────────────────────────────────────────

    _send_to_printer() {
        if (!this.job_card) return;
        if (!this.selected_machine) {
            frappe.show_alert({ message: "Select a printer first.", indicator: "orange" }, 4);
            return;
        }
        if (!this.selected_operator) {
            frappe.show_alert({ message: "Select your operator card first.", indicator: "orange" }, 4);
            return;
        }

        this.state = "sending";
        const $btn = this.$body.find("#dtp-print-btn");
        $btn.prop("disabled", true).text("SENDING…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtf_start_print",
            args:   {
                job_card_name:       this.job_card,
                machine_config_name: this.selected_machine,
                operator_employee:   this.selected_operator !== "__unlisted__"
                                         ? this.selected_operator : null,
            },
            callback: (r) => {
                const res = r.message || {};
                if (!res.ok) {
                    $btn.prop("disabled", false).text("▶ SEND TO PRINTER");
                    frappe.show_alert({ message: `Print failed: ${res.error || "unknown"}`, indicator: "red" }, 6);
                    return;
                }
                this.machine_job_id = res.machine_job_id || "";
                this.state = "printing";
                $btn.hide();
                this._start_polling();
            },
            error: () => {
                $btn.prop("disabled", false).text("▶ SEND TO PRINTER");
                frappe.show_alert({ message: "Server error — please retry", indicator: "red" }, 5);
            },
        });
    }

    // ── Status polling ────────────────────────────────────────────────────────

    _start_polling() {
        this.$body.find("#dtp-status-wrap").show();
        this._set_status("Queued");
        this._poll_once();
        this.poll_timer = setInterval(() => this._poll_once(), 4000);
    }

    _stop_polling() {
        if (this.poll_timer) { clearInterval(this.poll_timer); this.poll_timer = null; }
    }

    _poll_once() {
        if (!this.job_card || this.state === "film_ready" || this.state === "done") {
            this._stop_polling(); return;
        }
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtf_print_status",
            args:   { job_card_name: this.job_card },
            callback: (r) => {
                const res = r.message || {};
                const state = res.state || "Unknown";
                this._set_status(state, res.detail);
                if (state === "Complete" || state === "Completed") {
                    this._stop_polling();
                    this._on_print_complete();
                } else if (state === "Error") {
                    this._stop_polling();
                    frappe.show_alert({ message: "Print job error — check Epson Edge Print", indicator: "red" }, 8);
                } else if (state === "Cancelled") {
                    this._stop_polling();
                    this.$body.find("#dtp-print-btn").prop("disabled", false).text("▶ RESEND").show();
                }
            },
        });
    }

    _set_status(state, detail) {
        const colors = {
            Queued:    { bg: "#f39c12", label: "🕐 QUEUED" },
            Printing:  { bg: "#2980b9", label: "🖨 PRINTING…" },
            Complete:  { bg: "#27ae60", label: "✓ PRINT COMPLETE" },
            Completed: { bg: "#27ae60", label: "✓ PRINT COMPLETE" },
            Error:     { bg: "#e74c3c", label: "✗ ERROR" },
            Cancelled: { bg: "#95a5a6", label: "CANCELLED" },
            Unknown:   { bg: "#6c757d", label: "UNKNOWN" },
        };
        const cfg = colors[state] || colors.Unknown;
        this.$body.find("#dtp-status-badge").text(cfg.label).css("background", cfg.bg);
        const detail_text = (detail && detail.note) ? detail.note
            : (typeof detail === "string") ? detail : "";
        this.$body.find("#dtp-status-detail").text(detail_text);
    }

    _on_print_complete() {
        this.state = "print_done";
        frappe.show_alert({ message: `${this.job_card} — print complete`, indicator: "green" }, 4);
        this.$body.find("#dtp-film-btn").show();
    }

    // ── Film ready ────────────────────────────────────────────────────────────

    _film_ready() {
        if (!this.job_card) return;
        this.state = "film_ready";
        const $btn = this.$body.find("#dtp-film-btn");
        $btn.prop("disabled", true).text("SAVING…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtf_film_ready",
            args:   { job_card_name: this.job_card },
            callback: (r) => {
                const res = r.message || {};
                if (res.ok) {
                    $btn.text("✓ FILM SENT TO DRYER").addClass("dtp-btn-done");
                    this._set_status("Complete");
                    frappe.show_alert({ message: `Film ready — ${this.job_card} → Dryer`, indicator: "green" }, 5);
                    setTimeout(() => this._reset(), 10000);
                } else {
                    $btn.prop("disabled", false).text("✓ FILM READY — SEND TO DRYER");
                    frappe.show_alert({ message: "Could not save — retry", indicator: "red" }, 4);
                }
            },
        });
    }

    // ── Reset ─────────────────────────────────────────────────────────────────

    _reset() {
        this._stop_polling();
        this.job_card          = null;
        this.machine_job_id    = null;
        this.machine_name      = null;
        this.selected_machine  = null;
        this.selected_operator = null;
        this._stored_operators = [];
        this._stored_res       = null;

        this.$body.find("#dtp-jc-input").val("").prop("disabled", false);
        this.$body.find("#dtp-load-btn").show().prop("disabled", false);
        this.$body.find("#dtp-reset-btn").hide();
        this.$body.find("#dtp-main-card").hide();
        this.$body.find("#dtp-machine-picker-section").hide();
        this.$body.find("#dtp-operator-section").hide();
        this.$body.find("#dtp-op-warn").hide();
        this.$body.find("#dtp-print-btn").show().prop("disabled", true).text("▶ SEND TO PRINTER");
        this.$body.find("#dtp-film-btn").hide().prop("disabled", false)
            .text("✓ FILM READY — SEND TO DRYER").removeClass("dtp-btn-done");
        this.$body.find("#dtp-status-wrap").hide();
        this.$body.find("#dtp-machine-label").text("DTF Print Station");
        this._set_machine_dot(null);
        this._set_msg("");
        this.state = "idle";

        setTimeout(() => this.$body.find("#dtp-jc-input").focus(), 100);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _set_msg(text, type) {
        const colors = { warn: "#e67e22", error: "#e74c3c" };
        this.$body.find("#dtp-scan-msg").text(text).css("color", colors[type] || "#555");
    }

    _set_machine_dot(online) {
        const bg = online === true ? "#22c55e" : online === false ? "#ef4444" : "#6b7280";
        const title = online === true ? "Online" : online === false ? "Offline" : "No machine selected";
        this.$body.find("#dtp-machine-dot").css("background", bg).attr("title", title);
    }

    // ── Styles ────────────────────────────────────────────────────────────────

    _init_styles() {
        if (document.getElementById("dtp-styles")) return;
        const style = document.createElement("style");
        style.id = "dtp-styles";
        style.textContent = `
/* ── Wrap ── */
.dtp-wrap {
    max-width: 900px;
    margin: 0 auto;
    padding: 12px 12px 40px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
}

/* ── Top bar ── */
.dtp-topbar {
    background: #1a1a2e;
    border-radius: 14px;
    padding: 16px 20px 14px;
    margin-bottom: 14px;
}
.dtp-machine-info {
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
}
.dtp-machine-icon { font-size: 1.2rem; }
.dtp-machine-label {
    font-size: 0.88rem; font-weight: 600; color: rgba(255,255,255,0.85); letter-spacing: 0.02em;
}
.dtp-machine-dot {
    width: 10px; height: 10px; border-radius: 50%; background: #6b7280;
    margin-left: 4px; flex-shrink: 0; transition: background 0.4s;
}
.dtp-scan-row { display: flex; gap: 8px; align-items: center; }
.dtp-scan-row input {
    flex: 1; padding: 11px 16px; font-size: 1rem; border-radius: 9px;
    border: 2px solid rgba(255,255,255,0.15); background: rgba(255,255,255,0.08);
    color: #fff; outline: none; transition: border-color 0.2s;
}
.dtp-scan-row input::placeholder { color: rgba(255,255,255,0.38); }
.dtp-scan-row input:focus { border-color: rgba(255,255,255,0.42); }
.dtp-scan-msg { margin-top: 8px; font-size: 0.85rem; font-weight: 500; min-height: 1em; }

/* ── Picker headers ── */
.dtp-picker-header {
    font-size: 0.65rem; font-weight: 800; letter-spacing: 0.14em; color: #6b7280;
    margin: 12px 0 8px; text-transform: uppercase;
}

/* ── Machine picker grid ── */
.dtp-machine-grid {
    display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px;
}
.dtp-mc-card {
    background: #1e293b; border: 2px solid #334155; border-radius: 10px;
    padding: 12px 16px; cursor: pointer; min-width: 150px; flex: 1;
    transition: border-color 0.2s, transform 0.1s;
}
.dtp-mc-card:hover { border-color: #6366f1; transform: translateY(-1px); }
.dtp-mc-card.dtp-mc-selected { border-color: #6366f1; background: #1e1b4b; }
.dtp-mc-toprow { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.dtp-mc-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.dtp-mc-id { font-size: 0.9rem; font-weight: 700; color: #f1f5f9; }
.dtp-mc-status { font-size: 0.75rem; color: #94a3b8; }
.dtp-mc-busy { font-size: 0.72rem; color: #fbbf24; font-weight: 600; margin-top: 4px; display: block; }
.dtp-no-machines { font-size: 0.85rem; color: #94a3b8; padding: 8px 0; }

/* ── Operator selector ── */
.dtp-operator-grid {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px;
}
.dtp-op-card {
    background: #1e293b; border: 2px solid #334155; border-radius: 10px;
    padding: 10px 14px; cursor: pointer; min-width: 130px;
    transition: border-color 0.2s, transform 0.1s;
}
.dtp-op-card:hover { border-color: #6366f1; transform: translateY(-1px); }
.dtp-op-card.dtp-op-selected { border-color: #6366f1; background: #1e1b4b; }
.dtp-op-unlisted { border-style: dashed; border-color: #475569; }
.dtp-op-unlisted.dtp-op-selected { border-color: #f59e0b; background: #451a03; }
.dtp-op-name { font-size: 0.88rem; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }
.dtp-op-level { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em; }
.dtp-op-empty { font-size: 0.85rem; color: #fbbf24; padding: 8px 0; }
.dtp-op-warn {
    font-size: 0.8rem; color: #fbbf24; background: rgba(245,158,11,0.12);
    border: 1px solid #d97706; border-radius: 6px; padding: 6px 12px; margin-bottom: 8px;
}

/* ── Buttons ── */
.dtp-btn {
    border: none; border-radius: 9px; font-weight: 700;
    letter-spacing: 0.05em; cursor: pointer; transition: transform 0.1s, opacity 0.15s;
}
.dtp-btn:active  { transform: scale(0.97); }
.dtp-btn:disabled { opacity: 0.5; cursor: default; }
.dtp-btn-load  { background: #e74c3c; color: #fff; padding: 11px 20px; font-size: 0.9rem; }
.dtp-btn-reset { background: rgba(255,255,255,0.15); color: #fff; padding: 11px 14px; font-size: 0.9rem; }
.dtp-btn-print {
    width: 100%; padding: 16px; font-size: 1.05rem;
    background: #6366f1; color: #fff; border-radius: 10px; margin-top: 12px;
}
.dtp-btn-film-ready {
    width: 100%; padding: 16px; font-size: 1.05rem;
    background: #27ae60; color: #fff; border-radius: 10px; margin-top: 10px;
}
.dtp-btn-film-ready.dtp-btn-done { background: #2c7a50; }

/* ── Main card ── */
.dtp-main-card {
    background: #fff; border-radius: 14px;
    box-shadow: 0 3px 20px rgba(0,0,0,0.09); overflow: hidden;
}
.dtp-columns {
    display: grid; grid-template-columns: 1fr 1fr;
}

/* ── Artwork panel ── */
.dtp-artwork-panel {
    background: #0d0d1a; display: flex; flex-direction: column;
    align-items: center; padding: 18px 14px 14px; min-height: 420px;
}
.dtp-artwork-label {
    font-size: 0.65rem; font-weight: 700; letter-spacing: 0.14em;
    color: rgba(255,255,255,0.4); margin-bottom: 12px; align-self: flex-start;
}
.dtp-artwork-wrap {
    flex: 1; width: 100%; display: flex; align-items: center;
    justify-content: center; overflow: hidden; border-radius: 8px; background: #111;
}
.dtp-artwork-img { max-width: 100%; max-height: 340px; object-fit: contain; border-radius: 6px; }
.dtp-artwork-placeholder { color: rgba(255,255,255,0.3); font-size: 0.9rem; text-align: center; padding: 40px 20px; }
.dtp-placement-badge {
    margin-top: 10px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em;
    color: rgba(255,255,255,0.6); background: rgba(255,255,255,0.08);
    border-radius: 20px; padding: 4px 14px;
}

/* ── Info panel ── */
.dtp-info-panel { padding: 18px 20px; display: flex; flex-direction: column; gap: 6px; }
.dtp-job-header { margin-bottom: 8px; }
.dtp-jc-name  { font-size: 1.2rem; font-weight: 800; color: #1a1a2e; letter-spacing: 0.03em; }
.dtp-recipe-name { font-size: 0.8rem; color: #888; margin-top: 2px; }

/* ── Params ── */
.dtp-section-label { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.12em; color: #aaa; margin-bottom: 6px; margin-top: 4px; }
.dtp-params-grid { display: flex; flex-direction: column; gap: 3px; margin-bottom: 10px; }
.dtp-param-row {
    display: flex; justify-content: space-between; align-items: center;
    font-size: 0.88rem; border-bottom: 1px solid #f4f4f4; padding: 5px 0;
}
.dtp-param-key { color: #666; }
.dtp-param-val { font-weight: 700; color: #1a1a2e; }
.dtp-val-on  { color: #27ae60 !important; }
.dtp-val-off { color: #95a5a6 !important; }
.dtp-order-row { font-size: 0.84rem; color: #555; padding: 3px 0; }

/* ── Status ── */
.dtp-status-wrap { margin-top: 8px; }
.dtp-status-badge {
    display: inline-block; font-size: 0.82rem; font-weight: 800; letter-spacing: 0.08em;
    color: #fff; border-radius: 8px; padding: 6px 16px; transition: background 0.3s;
}
.dtp-status-detail { font-size: 0.78rem; color: #777; margin-top: 4px; }

/* ── Responsive ── */
@media (max-width: 600px) {
    .dtp-columns { grid-template-columns: 1fr; }
    .dtp-artwork-panel { min-height: 260px; }
}
        `;
        document.head.appendChild(style);
    }
}
