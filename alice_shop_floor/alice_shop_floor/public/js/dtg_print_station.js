/**
 * ALICE — DTG Print Station  (Task #66 + Task #72: multi-machine + operator certs)
 * ==================================================================================
 * Tablet page mounted at the Epson SureColor F2270 / F3070 DTG printer.
 * Operator scans a Job Card → machine picker shows all active DTG printers →
 * operator selector shows who's certified → artwork preview + platen/pretreat params →
 * "Send to Printer" dispatches to Epson Edge Print 2 via hot folder →
 * live status polling → "Sent to Cure" confirms garment left for tunnel dryer.
 *
 * DTG workflow (this page handles the full print step):
 *   Step 1  THIS PAGE — F2270 prints directly on garment
 *   Step 2  Cure tunnel / heat press (automatic, no ALICE interaction)
 *   Step 3  Final QC / Garment Passport seal
 *
 * Key difference from DTF: no film. Garment goes straight on the platen,
 * machine prints, then goes to cure. No "Film Ready" intermediate step.
 *
 * Pretreat alert: dark garments (black, navy, etc.) require spray pretreatment
 * before loading onto the platen. The page blocks "Send to Printer" with a
 * mandatory pretreat confirmation for flagged garments.
 *
 * Flow:
 *   1. Operator opens page on DTG-station tablet.
 *   2. Scans / types Job Card name.
 *   3. Machine picker shows all active DTG printers — operator taps one.
 *   4. Operator selector shows certified operators — operator taps self.
 *   5. Params + artwork preview render.
 *   6. If pretreat required → confirmation modal before Send is enabled.
 *   7. Taps "Send to Printer" → job dropped in hot folder.
 *   8. Status badge polls every 4s: Queued → Complete.
 *   9. Taps "Sent to Cure" → Job Card stamped, stage advances.
 *
 * API endpoints:
 *   alice_shop_floor.alice_shop_floor.api.dtg_scan_and_load
 *   alice_shop_floor.alice_shop_floor.api.dtg_start_print
 *   alice_shop_floor.alice_shop_floor.api.dtg_print_status
 *   alice_shop_floor.alice_shop_floor.api.dtg_print_complete
 */

frappe.pages["dtg-print-station"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "DTG Print Station",
        single_column: true,
    });
    window.dtg_print = new DTGPrintStation(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class DTGPrintStation {
    constructor(page, wrapper) {
        this.page    = page;
        this.wrapper = wrapper;
        this.$body   = $(wrapper).find(".layout-main-section, .page-content").first();

        this.job_card           = null;
        this.machine_job_id     = null;
        this.machine_name       = null;       // selected machine config name (backward compat)
        this.selected_machine   = null;       // full machine record from picker
        this.selected_operator  = null;       // employee id of selected operator
        this._stored_operators  = [];
        this.poll_timer         = null;
        this.pretreat_confirmed = false;
        this.pretreat_required  = false;
        // idle | loaded | pretreat_gate | sending | printing | cure_ready | done
        this.state              = "idle";

        this._init_styles();
        this._init_layout();
        this._bind_events();
        this._setup_realtime();
    }

    // ── Layout ────────────────────────────────────────────────────────────────

    _init_layout() {
        this.$body.html(`
            <div class="dtg-wrap">

                <!-- Top bar -->
                <div class="dtg-topbar">
                    <div class="dtg-machine-info">
                        <span class="dtg-machine-icon">🖨</span>
                        <span class="dtg-machine-label" id="dtg-machine-label">DTG Print Station</span>
                        <span class="dtg-machine-dot" id="dtg-machine-dot" title="Select a machine"></span>
                    </div>
                    <div class="dtg-scan-row">
                        <input type="text" id="dtg-jc-input"
                            placeholder="Scan or type Job Card…"
                            autocomplete="off" autocorrect="off"
                            autocapitalize="characters" spellcheck="false"/>
                        <button class="dtg-btn dtg-btn-load" id="dtg-load-btn">LOAD</button>
                        <button class="dtg-btn dtg-btn-reset" id="dtg-reset-btn" style="display:none;">✕</button>
                    </div>
                    <div class="dtg-scan-msg" id="dtg-scan-msg"></div>
                </div>

                <!-- Machine picker — shown after scan -->
                <div id="dtg-machine-picker-section" style="display:none;">
                    <div class="dtg-picker-header">SELECT PRINTER</div>
                    <div class="dtg-machine-grid" id="dtg-machine-grid"></div>
                </div>

                <!-- Operator selector — shown after machine selected -->
                <div id="dtg-operator-section" style="display:none;">
                    <div class="dtg-picker-header">SELECT OPERATOR</div>
                    <div class="dtg-operator-grid" id="dtg-operator-grid"></div>
                    <div class="dtg-op-warn" id="dtg-op-warn" style="display:none;"></div>
                </div>

                <!-- Pretreat confirmation modal -->
                <div class="dtg-modal-overlay" id="dtg-pretreat-modal" style="display:none;">
                    <div class="dtg-modal">
                        <div class="dtg-modal-icon">⚠️</div>
                        <div class="dtg-modal-title">PRETREAT REQUIRED</div>
                        <div class="dtg-modal-body">
                            This garment requires spray pretreatment before printing.<br>
                            <strong id="dtg-modal-color-note"></strong><br><br>
                            Apply pretreatment spray evenly and heat-press to dry before loading.
                        </div>
                        <div class="dtg-modal-actions">
                            <button class="dtg-btn dtg-btn-confirm-pretreat" id="dtg-confirm-pretreat-btn">
                                ✓ PRETREAT APPLIED — CONTINUE
                            </button>
                            <button class="dtg-btn dtg-btn-cancel-modal" id="dtg-cancel-pretreat-btn">
                                Cancel
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Main job card — hidden until loaded -->
                <div class="dtg-main-card" id="dtg-main-card" style="display:none;">
                    <div class="dtg-columns">

                        <!-- Artwork panel -->
                        <div class="dtg-artwork-panel" id="dtg-artwork-panel">
                            <div class="dtg-artwork-label">ARTWORK</div>
                            <div class="dtg-artwork-wrap" id="dtg-artwork-wrap">
                                <div class="dtg-artwork-placeholder" id="dtg-artwork-placeholder">
                                    <span>No preview</span>
                                </div>
                                <img id="dtg-artwork-img" class="dtg-artwork-img" style="display:none;" alt="Design"/>
                            </div>
                            <div class="dtg-placement-badge" id="dtg-placement-badge"></div>
                        </div>

                        <!-- Info + actions panel -->
                        <div class="dtg-info-panel">

                            <!-- Job header -->
                            <div class="dtg-job-header" id="dtg-job-header"></div>

                            <!-- Pretreat alert banner -->
                            <div class="dtg-pretreat-banner" id="dtg-pretreat-banner" style="display:none;">
                                <span class="dtg-pretreat-icon">⚠️</span>
                                <span>PRETREAT REQUIRED — Dark garment</span>
                                <span class="dtg-pretreat-status" id="dtg-pretreat-status"></span>
                            </div>

                            <!-- Print parameters -->
                            <div class="dtg-params-section">
                                <div class="dtg-section-label">PRINT PARAMETERS</div>
                                <div class="dtg-params-grid">
                                    <div class="dtg-param-row">
                                        <span class="dtg-param-key">Platen size</span>
                                        <span class="dtg-param-val" id="dtg-platen-size">—</span>
                                    </div>
                                    <div class="dtg-param-row">
                                        <span class="dtg-param-key">Ink profile</span>
                                        <span class="dtg-param-val" id="dtg-ink-profile">—</span>
                                    </div>
                                    <div class="dtg-param-row">
                                        <span class="dtg-param-key">Resolution</span>
                                        <span class="dtg-param-val" id="dtg-resolution">—</span>
                                    </div>
                                    <div class="dtg-param-row">
                                        <span class="dtg-param-key">Underbase passes</span>
                                        <span class="dtg-param-val" id="dtg-underbase">—</span>
                                    </div>
                                    <div class="dtg-param-row">
                                        <span class="dtg-param-key">Cure temp</span>
                                        <span class="dtg-param-val" id="dtg-cure-temp">—</span>
                                    </div>
                                    <div class="dtg-param-row">
                                        <span class="dtg-param-key">Cure time</span>
                                        <span class="dtg-param-val" id="dtg-cure-time">—</span>
                                    </div>
                                </div>
                            </div>

                            <!-- Garment color chip -->
                            <div class="dtg-garment-row" id="dtg-garment-row" style="display:none;">
                                <div class="dtg-section-label">GARMENT</div>
                                <div class="dtg-garment-color">
                                    <div class="dtg-color-chip" id="dtg-color-chip"></div>
                                    <span id="dtg-garment-color-label"></span>
                                </div>
                            </div>

                            <!-- Status + action area -->
                            <div class="dtg-action-area">
                                <div class="dtg-status-row">
                                    <div class="dtg-status-badge" id="dtg-status-badge">
                                        <span class="dtg-status-dot" id="dtg-status-dot"></span>
                                        <span class="dtg-status-text" id="dtg-status-text">Ready</span>
                                    </div>
                                    <div class="dtg-elapsed" id="dtg-elapsed" style="display:none;"></div>
                                </div>

                                <button class="dtg-btn dtg-btn-send" id="dtg-send-btn" disabled>
                                    🖨 SEND TO PRINTER
                                </button>

                                <button class="dtg-btn dtg-btn-cure" id="dtg-cure-btn" style="display:none;">
                                    ✓ SENT TO CURE
                                </button>

                                <div class="dtg-done-msg" id="dtg-done-msg" style="display:none;">
                                    <span class="dtg-done-icon">✅</span>
                                    <span>Job complete — garment in cure</span>
                                </div>
                            </div>

                        </div><!-- /info-panel -->
                    </div><!-- /columns -->
                </div><!-- /main-card -->

            </div><!-- /dtg-wrap -->
        `);
    }

    // ── Events ────────────────────────────────────────────────────────────────

    _bind_events() {
        const $b = this.$body;

        $b.on("keydown", "#dtg-jc-input", (e) => { if (e.key === "Enter") this._load_job(); });
        $b.on("click",   "#dtg-load-btn",  () => this._load_job());
        $b.on("click",   "#dtg-reset-btn", () => this._reset());

        // Pretreat modal
        $b.on("click", "#dtg-confirm-pretreat-btn", () => this._confirm_pretreat());
        $b.on("click", "#dtg-cancel-pretreat-btn",  () => this._reset());

        // Send + cure
        $b.on("click", "#dtg-send-btn", () => this._send_to_printer());
        $b.on("click", "#dtg-cure-btn", () => this._sent_to_cure());

        $b.on("error", "#dtg-artwork-img", () => {
            this.$body.find("#dtg-artwork-img").hide();
            this.$body.find("#dtg-artwork-placeholder").show();
        });

        setTimeout(() => $b.find("#dtg-jc-input").focus(), 300);
    }

    _setup_realtime() {
        frappe.realtime.on("dtg_print_complete", (data) => {
            if (data.job_card === this.job_card) this._on_done();
        });
        frappe.realtime.on("machine_offline_alert", (data) => {
            if (this.machine_name && data.machine_name === this.machine_name) {
                this._set_machine_dot(false);
                frappe.show_alert({
                    message: `⚠️ ${data.machine_name} went offline`,
                    indicator: "red",
                }, 8);
            }
        });
    }

    // ── Core flow ─────────────────────────────────────────────────────────────

    _load_job() {
        const jc_name = this.$body.find("#dtg-jc-input").val().trim().toUpperCase();
        if (!jc_name) { this._msg("Scan or type a Job Card name", "warn"); return; }
        this._msg("Loading…", "info");
        this.$body.find("#dtg-load-btn").prop("disabled", true);

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtg_scan_and_load",
            args:   { job_card_name: jc_name },
            callback: (r) => {
                this.$body.find("#dtg-load-btn").prop("disabled", false);
                const d = r.message || {};
                if (!d.ok) { this._msg(d.detail || d.error || "Failed to load", "error"); return; }
                this.job_card = d.job_card || jc_name;
                this.state    = "loaded";
                this._render_job(d);
            },
            error: () => {
                this.$body.find("#dtg-load-btn").prop("disabled", false);
                this._msg("Server error — try again", "error");
            },
        });
    }

    _render_job(d) {
        // Reset picker state
        this.selected_machine   = null;
        this.selected_operator  = null;
        this.pretreat_required  = !!d.pretreat_required;
        this.pretreat_confirmed = false;

        // Artwork
        if (d.design_file_url) {
            this.$body.find("#dtg-artwork-img").attr("src", d.design_file_url).show();
            this.$body.find("#dtg-artwork-placeholder").hide();
        } else {
            this.$body.find("#dtg-artwork-img").hide();
            this.$body.find("#dtg-artwork-placeholder").show();
        }
        this.$body.find("#dtg-placement-badge").text(d.design_placement || "Full Front");

        // Job header
        this.$body.find("#dtg-job-header").html(`
            <div class="dtg-jc-name">${d.job_card}</div>
            <div class="dtg-jc-meta">
                ${d.params?.item_name || d.params?.item_code || ""}
                ${d.garment_color ? ` &bull; ${d.garment_color}` : ""}
            </div>
        `);

        // Print params
        const inkLabel = d.ink_profile === "dark_garment" ? "Dark Garment (CMYK+W)" : "Light Garment (CMYK)";
        this.$body.find("#dtg-platen-size").text(d.platen_size || "L");
        this.$body.find("#dtg-ink-profile").text(inkLabel);
        this.$body.find("#dtg-resolution").text(d.resolution_dpi ? `${d.resolution_dpi} DPI` : "1200 DPI");
        this.$body.find("#dtg-underbase").text(
            d.underbase_passes ? `${d.underbase_passes} pass${d.underbase_passes > 1 ? "es" : ""}` : "None"
        );
        this.$body.find("#dtg-cure-temp").text(d.cure_temp_f ? `${d.cure_temp_f}°F` : "320°F");
        this.$body.find("#dtg-cure-time").text(d.cure_time_sec ? `${d.cure_time_sec}s` : "90s");

        // Garment color chip
        if (d.garment_color) {
            this.$body.find("#dtg-garment-row").show();
            this.$body.find("#dtg-garment-color-label").text(d.garment_color);
            this.$body.find("#dtg-color-chip").css("background", d.garment_color.toLowerCase());
        } else {
            this.$body.find("#dtg-garment-row").hide();
        }

        // Pretreat banner
        if (this.pretreat_required) {
            this.$body.find("#dtg-pretreat-banner").show();
            this.$body.find("#dtg-pretreat-status").text("— Confirmation required");
            this.$body.find("#dtg-modal-color-note").text(
                `Garment color: ${d.garment_color || "dark"}`
            );
        } else {
            this.$body.find("#dtg-pretreat-banner").hide();
        }

        this._set_status("ready", "Ready to Print");
        this.$body.find("#dtg-main-card").show();
        this.$body.find("#dtg-reset-btn").show();
        this.$body.find("#dtg-jc-input").prop("disabled", true);
        this.$body.find("#dtg-load-btn").hide();
        this.$body.find("#dtg-send-btn").prop("disabled", true).show();
        this.$body.find("#dtg-cure-btn").hide();
        this.$body.find("#dtg-done-msg").hide();
        this._msg("");

        // Machine picker
        this._stored_operators = d.certified_operators || [];
        this._render_machine_picker(d.available_machines || []);
        this.$body.find("#dtg-operator-section").hide();
    }

    // ── Machine picker ────────────────────────────────────────────────────────

    _render_machine_picker(machines) {
        const $section = this.$body.find("#dtg-machine-picker-section");
        const $grid    = this.$body.find("#dtg-machine-grid");

        if (!machines.length) {
            $grid.html(`<div class="dtg-no-machines">No active DTG printers found — check Machine Config.</div>`);
            $section.show();
            return;
        }

        const cards = machines.map(m => {
            const online_color = m.online ? "#22c55e" : "#ef4444";
            const busy_label   = m.busy
                ? `<span class="dtg-mc-busy">BUSY — ${m.current_job || "job in progress"}</span>`
                : "";
            return `
                <div class="dtg-mc-card" data-name="${m.name}">
                    <div class="dtg-mc-toprow">
                        <span class="dtg-mc-dot" style="background:${online_color}"></span>
                        <span class="dtg-mc-id">${m.machine_id || m.name}</span>
                    </div>
                    <div class="dtg-mc-status">${m.online ? "Online" : "Offline"}</div>
                    ${busy_label}
                </div>
            `;
        }).join("");

        $grid.html(cards);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".dtg-mc-card", function () {
            const name = $(this).data("name");
            const m    = machines.find(x => x.name === name);
            if (!m) return;

            me.selected_machine = name;
            me.machine_name     = name;

            // Visual selection
            $grid.find(".dtg-mc-card").removeClass("dtg-mc-selected");
            $(this).addClass("dtg-mc-selected");

            // Update topbar
            me.$body.find("#dtg-machine-label").text(`Epson F2270 — ${m.machine_id || m.name}`);
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
        const $section = this.$body.find("#dtg-operator-section");
        const $grid    = this.$body.find("#dtg-operator-grid");

        const LEVEL_COLOR = { Expert: "#7c3aed", Certified: "#2563eb", Trainee: "#d97706" };
        const LEVEL_ICON  = { Expert: "★", Certified: "✓", Trainee: "◎" };

        if (!operators.length) {
            $grid.html(`<div class="dtg-op-empty">No certified operators found for DTG. Contact your supervisor.</div>`);
            $section.show();
            return;
        }

        const cards = operators.map(op => {
            const lvl   = op.proficiency_level || "Certified";
            const color = LEVEL_COLOR[lvl] || "#2563eb";
            const icon  = LEVEL_ICON[lvl]  || "✓";
            return `
                <div class="dtg-op-card" data-emp="${op.employee}">
                    <div class="dtg-op-name">${op.employee_name || op.employee}</div>
                    <div class="dtg-op-level" style="color:${color}">${icon} ${lvl}</div>
                </div>
            `;
        }).join("");

        const not_listed = `
            <div class="dtg-op-card dtg-op-unlisted" data-emp="__unlisted__">
                <div class="dtg-op-name">Not listed</div>
                <div class="dtg-op-level" style="color:#6b7280">⚠ Supervisor override</div>
            </div>
        `;

        $grid.html(cards + not_listed);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".dtg-op-card", function () {
            const emp = $(this).data("emp");
            $grid.find(".dtg-op-card").removeClass("dtg-op-selected");
            $(this).addClass("dtg-op-selected");
            me.selected_operator = emp;

            if (emp === "__unlisted__") {
                me.$body.find("#dtg-op-warn")
                    .text("⚠ Supervisor override: log this in the shift notes.")
                    .show();
            } else {
                me.$body.find("#dtg-op-warn").hide();
            }

            // Enable Send now that machine + operator are both chosen
            me.$body.find("#dtg-send-btn").prop("disabled", false);
        });
    }

    // ── Send to printer ───────────────────────────────────────────────────────

    _send_to_printer() {
        if (this.state !== "loaded") return;

        // Guard: machine + operator required first
        if (!this.selected_machine) {
            frappe.show_alert({ message: "Select a printer first.", indicator: "orange" }, 4);
            return;
        }
        if (!this.selected_operator) {
            frappe.show_alert({ message: "Select your operator card first.", indicator: "orange" }, 4);
            return;
        }

        // Gate on pretreat confirmation for dark garments
        if (this.pretreat_required && !this.pretreat_confirmed) {
            this.$body.find("#dtg-pretreat-modal").show();
            return;
        }

        this.state = "sending";
        const $btn = this.$body.find("#dtg-send-btn");
        $btn.prop("disabled", true).text("Sending…");
        this._set_status("sending", "Sending to Printer…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtg_start_print",
            args:   {
                job_card_name:       this.job_card,
                machine_config_name: this.selected_machine,
                operator_employee:   this.selected_operator !== "__unlisted__"
                                         ? this.selected_operator : null,
            },
            callback: (r) => {
                const d = r.message || {};
                if (!d.ok) {
                    this.state = "loaded";
                    $btn.prop("disabled", false).text("🖨 SEND TO PRINTER");
                    this._set_status("error", d.error || "Send failed");
                    return;
                }
                this.machine_job_id = d.machine_job_id || "";
                this.state = "printing";
                $btn.hide();
                this._set_status("printing", "Queued — Printing…");
                this._start_poll();
            },
            error: () => {
                this.state = "loaded";
                $btn.prop("disabled", false).text("🖨 SEND TO PRINTER");
                this._set_status("error", "Server error");
            },
        });
    }

    _confirm_pretreat() {
        this.pretreat_confirmed = true;
        this.$body.find("#dtg-pretreat-modal").hide();
        this.$body.find("#dtg-pretreat-status").text(" ✓ Confirmed");
        this.$body.find("#dtg-pretreat-banner").css("background", "#d1fae5");
        this._send_to_printer();
    }

    // ── Status polling ────────────────────────────────────────────────────────

    _start_poll() {
        this._stop_poll();
        this._print_start_ts = Date.now();
        this.poll_timer = setInterval(() => this._poll_status(), 4000);
    }

    _stop_poll() {
        if (this.poll_timer) { clearInterval(this.poll_timer); this.poll_timer = null; }
    }

    _poll_status() {
        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtg_print_status",
            args:   { job_card_name: this.job_card },
            callback: (r) => {
                const d     = r.message || {};
                const state = d.state || "Unknown";

                // Elapsed timer
                if (this._print_start_ts) {
                    const elapsed = Math.round((Date.now() - this._print_start_ts) / 1000);
                    const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
                    const ss = String(elapsed % 60).padStart(2, "0");
                    this.$body.find("#dtg-elapsed").text(`${mm}:${ss}`).show();
                }

                if (state === "Complete" || state === "Completed") {
                    this._stop_poll();
                    this.state = "cure_ready";
                    this._set_status("complete", "Print Complete — Load Platen for Cure");
                    this.$body.find("#dtg-cure-btn").show();
                } else if (state === "Error") {
                    this._stop_poll();
                    this._set_status("error", d.error || "Printer error");
                } else {
                    this._set_status("printing", state === "Queued" ? "Queued in Edge Print…" : "Printing…");
                }
            },
        });
    }

    // ── Sent to cure ──────────────────────────────────────────────────────────

    _sent_to_cure() {
        if (this.state !== "cure_ready") return;
        const $btn = this.$body.find("#dtg-cure-btn");
        $btn.prop("disabled", true).text("Confirming…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtg_print_complete",
            args:   {
                job_card_name:     this.job_card,
                operator_employee: this.selected_operator !== "__unlisted__"
                                       ? this.selected_operator : null,
            },
            callback: (r) => {
                const d = r.message || {};
                if (d.ok) {
                    this._on_done();
                } else {
                    $btn.prop("disabled", false).text("✓ SENT TO CURE");
                    frappe.msgprint(d.message || "Failed to confirm — try again.");
                }
            },
        });
    }

    _on_done() {
        this._stop_poll();
        this.state = "done";
        this.$body.find("#dtg-cure-btn").hide();
        this.$body.find("#dtg-done-msg").show();
        this._set_status("done", "Complete");
        this.$body.find("#dtg-elapsed").hide();

        // Auto-reset after 6s so station is ready for next garment
        setTimeout(() => this._reset(), 6000);
    }

    // ── Reset ─────────────────────────────────────────────────────────────────

    _reset() {
        this._stop_poll();
        this.job_card           = null;
        this.machine_job_id     = null;
        this.machine_name       = null;
        this.selected_machine   = null;
        this.selected_operator  = null;
        this._stored_operators  = [];
        this.pretreat_confirmed = false;
        this.pretreat_required  = false;
        this.state              = "idle";

        this.$body.find("#dtg-main-card").hide();
        this.$body.find("#dtg-machine-picker-section").hide();
        this.$body.find("#dtg-operator-section").hide();
        this.$body.find("#dtg-op-warn").hide();
        this.$body.find("#dtg-pretreat-modal").hide();
        this.$body.find("#dtg-jc-input").val("").prop("disabled", false);
        this.$body.find("#dtg-load-btn").show().prop("disabled", false);
        this.$body.find("#dtg-reset-btn").hide();
        this.$body.find("#dtg-send-btn").show().prop("disabled", true).text("🖨 SEND TO PRINTER");
        this.$body.find("#dtg-cure-btn").hide().prop("disabled", false).text("✓ SENT TO CURE");
        this.$body.find("#dtg-done-msg").hide();
        this.$body.find("#dtg-elapsed").hide();
        this.$body.find("#dtg-machine-label").text("DTG Print Station");
        this._set_machine_dot(null);
        this._msg("");

        setTimeout(() => this.$body.find("#dtg-jc-input").focus(), 100);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _set_status(type, text) {
        const colors = {
            ready:     "#6b7280",
            sending:   "#f59e0b",
            printing:  "#3b82f6",
            complete:  "#22c55e",
            cure_ready:"#22c55e",
            done:      "#22c55e",
            error:     "#ef4444",
        };
        const dot = this.$body.find("#dtg-status-dot");
        const txt = this.$body.find("#dtg-status-text");
        dot.css("background", colors[type] || "#6b7280");
        txt.text(text);
        if (type === "printing" || type === "sending") {
            dot.addClass("dtg-dot-pulse");
        } else {
            dot.removeClass("dtg-dot-pulse");
        }
    }

    _set_machine_dot(online) {
        const bg    = online === true ? "#22c55e" : online === false ? "#ef4444" : "#6b7280";
        const title = online === true ? "Online" : online === false ? "Offline" : "No machine selected";
        this.$body.find("#dtg-machine-dot").css("background", bg).attr("title", title);
    }

    _msg(text, type) {
        const el = this.$body.find("#dtg-scan-msg");
        el.text(text).removeClass("msg-info msg-warn msg-error");
        if (type) el.addClass(`msg-${type}`);
    }

    // ── Styles ────────────────────────────────────────────────────────────────

    _init_styles() {
        if (document.getElementById("dtg-station-styles")) return;
        const style = document.createElement("style");
        style.id = "dtg-station-styles";
        style.textContent = `
            .dtg-wrap { padding: 16px; max-width: 1100px; margin: 0 auto; font-family: var(--font-stack); }

            /* Topbar */
            .dtg-topbar { background: #1e293b; border-radius: 10px; padding: 14px 18px; margin-bottom: 14px; }
            .dtg-machine-info { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
            .dtg-machine-icon { font-size: 20px; }
            .dtg-machine-label { color: #94a3b8; font-size: 13px; font-weight: 600; letter-spacing: .5px; }
            .dtg-machine-dot { width: 10px; height: 10px; border-radius: 50%; background: #6b7280;
                                margin-left: 4px; flex-shrink: 0; transition: background 0.4s; }
            .dtg-scan-row { display: flex; gap: 8px; }
            .dtg-scan-row input {
                flex: 1; padding: 10px 14px; border-radius: 6px; border: 1.5px solid #334155;
                background: #0f172a; color: #f1f5f9; font-size: 16px; font-family: monospace;
            }
            .dtg-scan-row input:focus { outline: none; border-color: #6366f1; }
            .dtg-scan-msg { margin-top: 6px; font-size: 12px; min-height: 18px; color: #94a3b8; }
            .dtg-scan-msg.msg-error { color: #f87171; }
            .dtg-scan-msg.msg-warn  { color: #fbbf24; }
            .dtg-scan-msg.msg-info  { color: #60a5fa; }

            /* Picker headers */
            .dtg-picker-header {
                font-size: 0.65rem; font-weight: 800; letter-spacing: 0.14em; color: #6b7280;
                margin: 12px 0 8px; text-transform: uppercase;
            }

            /* Machine picker grid */
            .dtg-machine-grid { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
            .dtg-mc-card {
                background: #1e293b; border: 2px solid #334155; border-radius: 10px;
                padding: 12px 16px; cursor: pointer; min-width: 150px; flex: 1;
                transition: border-color 0.2s, transform 0.1s;
            }
            .dtg-mc-card:hover { border-color: #6366f1; transform: translateY(-1px); }
            .dtg-mc-card.dtg-mc-selected { border-color: #6366f1; background: #1e1b4b; }
            .dtg-mc-toprow { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
            .dtg-mc-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
            .dtg-mc-id { font-size: 0.9rem; font-weight: 700; color: #f1f5f9; }
            .dtg-mc-status { font-size: 0.75rem; color: #94a3b8; }
            .dtg-mc-busy { font-size: 0.72rem; color: #fbbf24; font-weight: 600; margin-top: 4px; display: block; }
            .dtg-no-machines { font-size: 0.85rem; color: #94a3b8; padding: 8px 0; }

            /* Operator selector */
            .dtg-operator-grid { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
            .dtg-op-card {
                background: #1e293b; border: 2px solid #334155; border-radius: 10px;
                padding: 10px 14px; cursor: pointer; min-width: 130px;
                transition: border-color 0.2s, transform 0.1s;
            }
            .dtg-op-card:hover { border-color: #6366f1; transform: translateY(-1px); }
            .dtg-op-card.dtg-op-selected { border-color: #6366f1; background: #1e1b4b; }
            .dtg-op-unlisted { border-style: dashed; border-color: #475569; }
            .dtg-op-unlisted.dtg-op-selected { border-color: #f59e0b; background: #451a03; }
            .dtg-op-name { font-size: 0.88rem; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }
            .dtg-op-level { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em; }
            .dtg-op-empty { font-size: 0.85rem; color: #fbbf24; padding: 8px 0; }
            .dtg-op-warn {
                font-size: 0.8rem; color: #fbbf24; background: rgba(245,158,11,0.12);
                border: 1px solid #d97706; border-radius: 6px; padding: 6px 12px; margin-bottom: 8px;
            }

            /* Buttons */
            .dtg-btn { padding: 10px 20px; border: none; border-radius: 6px; font-weight: 700;
                        font-size: 13px; cursor: pointer; letter-spacing: .5px; transition: opacity .15s, transform .1s; }
            .dtg-btn:active { transform: scale(0.97); }
            .dtg-btn:disabled { opacity: .5; cursor: not-allowed; }
            .dtg-btn-load   { background: #6366f1; color: #fff; }
            .dtg-btn-reset  { background: #475569; color: #fff; }
            .dtg-btn-send   { width: 100%; padding: 16px; font-size: 17px; background: #6366f1;
                               color: #fff; border-radius: 8px; margin-top: 12px; }
            .dtg-btn-cure   { width: 100%; padding: 16px; font-size: 17px; background: #22c55e;
                               color: #fff; border-radius: 8px; margin-top: 12px; }
            .dtg-btn-confirm-pretreat { background: #f59e0b; color: #000; width: 100%; margin-bottom: 8px; padding: 14px; font-size: 14px; }
            .dtg-btn-cancel-modal { background: #475569; color: #fff; width: 100%; padding: 10px; }

            /* Pretreat modal */
            .dtg-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7);
                                  display: flex; align-items: center; justify-content: center; z-index: 9999; }
            .dtg-modal { background: #1e293b; border-radius: 12px; padding: 28px; max-width: 420px;
                          width: 90%; text-align: center; border: 2px solid #f59e0b; }
            .dtg-modal-icon { font-size: 42px; margin-bottom: 10px; }
            .dtg-modal-title { font-size: 20px; font-weight: 800; color: #fbbf24; margin-bottom: 12px; letter-spacing: 1px; }
            .dtg-modal-body { color: #cbd5e1; font-size: 14px; line-height: 1.6; margin-bottom: 20px; }
            .dtg-modal-actions { display: flex; flex-direction: column; gap: 8px; }

            /* Main card */
            .dtg-main-card { background: #1e293b; border-radius: 10px; padding: 20px; margin-top: 14px; }
            .dtg-columns { display: grid; grid-template-columns: 1fr 1.3fr; gap: 20px; }
            @media (max-width: 700px) { .dtg-columns { grid-template-columns: 1fr; } }

            /* Artwork */
            .dtg-artwork-panel { display: flex; flex-direction: column; gap: 8px; }
            .dtg-artwork-label { font-size: 11px; font-weight: 700; color: #64748b; letter-spacing: 1px; }
            .dtg-artwork-wrap { background: #0f172a; border-radius: 8px; min-height: 220px;
                                 display: flex; align-items: center; justify-content: center; overflow: hidden; }
            .dtg-artwork-placeholder { color: #475569; font-size: 13px; }
            .dtg-artwork-img { max-width: 100%; max-height: 280px; object-fit: contain; border-radius: 6px; }
            .dtg-placement-badge { background: #334155; color: #94a3b8; font-size: 11px;
                                    border-radius: 4px; padding: 4px 8px; text-align: center; font-weight: 600; }

            /* Info panel */
            .dtg-info-panel { display: flex; flex-direction: column; gap: 14px; }
            .dtg-jc-name { font-size: 22px; font-weight: 800; color: #f1f5f9; font-family: monospace; }
            .dtg-jc-meta { font-size: 13px; color: #94a3b8; margin-top: 2px; }

            /* Pretreat banner */
            .dtg-pretreat-banner { background: #451a03; border: 1.5px solid #f59e0b; border-radius: 6px;
                                    padding: 10px 14px; display: flex; align-items: center; gap: 8px;
                                    font-size: 13px; font-weight: 700; color: #fbbf24; }
            .dtg-pretreat-status { margin-left: auto; font-weight: 400; color: #fbbf24; }

            /* Params */
            .dtg-section-label { font-size: 11px; font-weight: 700; color: #64748b; letter-spacing: 1px; margin-bottom: 8px; }
            .dtg-params-grid { display: flex; flex-direction: column; gap: 4px; }
            .dtg-param-row { display: flex; justify-content: space-between; padding: 5px 0;
                              border-bottom: 1px solid #334155; font-size: 13px; }
            .dtg-param-key { color: #94a3b8; }
            .dtg-param-val { color: #f1f5f9; font-weight: 600; font-family: monospace; }

            /* Garment color */
            .dtg-garment-row { }
            .dtg-garment-color { display: flex; align-items: center; gap: 10px; font-size: 13px; color: #f1f5f9; }
            .dtg-color-chip { width: 28px; height: 28px; border-radius: 4px; border: 1px solid #475569; flex-shrink: 0; }

            /* Status */
            .dtg-action-area { display: flex; flex-direction: column; gap: 4px; }
            .dtg-status-row { display: flex; align-items: center; justify-content: space-between; }
            .dtg-status-badge { display: flex; align-items: center; gap: 8px; }
            .dtg-status-dot { width: 12px; height: 12px; border-radius: 50%; background: #6b7280; flex-shrink: 0; }
            .dtg-status-text { font-size: 14px; font-weight: 600; color: #e2e8f0; }
            .dtg-elapsed { font-size: 13px; color: #64748b; font-family: monospace; font-weight: 700; }
            .dtg-done-msg { display: flex; align-items: center; gap: 10px; font-size: 15px;
                             color: #4ade80; font-weight: 700; margin-top: 12px; }
            .dtg-done-icon { font-size: 24px; }

            @keyframes dtgPulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
            .dtg-dot-pulse { animation: dtgPulse 1.2s ease-in-out infinite; }
        `;
        document.head.appendChild(style);
    }
}
