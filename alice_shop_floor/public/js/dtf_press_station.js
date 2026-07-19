/**
 * ALICE — DTF Press Station  (Task #64 + Task #73: multi-machine + operator certs)
 * ==================================================================================
 * Tablet page mounted at the pneumatic heat press.
 * Operator scans a Job Card QR → machine picker shows all active press units →
 * operator selector shows who's certified → validated press settings display →
 * "Transfer Complete" stamps Job Card and advances to QC.
 *
 * DTF workflow context:
 *   Step 1  Epson G6070 prints film      (dtf-print-station)
 *   Step 2  Dryer cures film             (automatic, ~2 min)
 *   Step 3  THIS PAGE — operator loads   garment + film, presses, peels
 *   Step 4  V6 Press QC Inspector        (camera capture, auto-triggered)
 *
 * Flow:
 *   1. Operator opens page on press-station tablet.
 *   2. Scans / types Job Card name.
 *   3. Machine picker shows all active pneumatic presses — operator taps one.
 *   4. Operator selector shows certified press operators — operator taps self.
 *   5. ALICE displays press params in large readable format: temp / dwell / PSI.
 *   6. Operator dials in press and runs the transfer.
 *   7. Taps "Transfer Complete" → Job Card stamped, advances to Press QC.
 *
 * API endpoints used:
 *   alice_shop_floor.alice_shop_floor.api.dtf_press_scan_and_load
 *   alice_shop_floor.alice_shop_floor.api.dtf_press_complete
 */

frappe.pages["dtf-press-station"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "DTF Press Station",
        single_column: true,
    });
    window.dtf_press = new DTFPressStation(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class DTFPressStation {
    constructor(page, wrapper) {
        this.page    = page;
        this.wrapper = wrapper;
        this.$body   = $(wrapper).find(".layout-main-section, .page-content").first();

        // State
        this.job_card           = null;
        this.press_params       = null;
        this.machine_job_id     = null;
        this.selected_machine   = null;   // machine config name from picker
        this.selected_operator  = null;   // employee id from operator selector
        this._stored_operators  = [];
        this.state              = "idle"; // idle | ready | complete | error

        this._init_styles();
        this._init_layout();
        this._bind_events();
        this._setup_realtime();
    }

    // ── Layout ────────────────────────────────────────────────────────────────

    _init_layout() {
        this.$body.html(`
            <div class="dps-wrap">

                <!-- Scan zone -->
                <div class="dps-scan-zone" id="dps-scan-zone">
                    <div class="dps-header-row">
                        <div class="dps-logo">🔥 DTF Press Station</div>
                        <div class="dps-status-chip" id="dps-chip">READY TO SCAN</div>
                    </div>
                    <div class="dps-scan-input-row">
                        <input type="text" id="dps-jc-input"
                            placeholder="Scan or type Job Card…"
                            autocomplete="off" autocorrect="off"
                            autocapitalize="characters" spellcheck="false"/>
                        <button class="dps-btn dps-btn-primary" id="dps-scan-btn">LOAD</button>
                    </div>
                    <div id="dps-scan-msg" class="dps-scan-msg"></div>
                </div>

                <!-- Machine picker — shown after scan -->
                <div id="dps-machine-picker-section" style="display:none;">
                    <div class="dps-picker-header">SELECT PRESS</div>
                    <div class="dps-machine-grid" id="dps-machine-grid"></div>
                </div>

                <!-- Operator selector — shown after machine selected -->
                <div id="dps-operator-section" style="display:none;">
                    <div class="dps-picker-header">SELECT OPERATOR</div>
                    <div class="dps-operator-grid" id="dps-operator-grid"></div>
                    <div class="dps-op-warn" id="dps-op-warn" style="display:none;"></div>
                </div>

                <!-- Press settings card — hidden until job loaded -->
                <div class="dps-press-card" id="dps-press-card" style="display:none;">

                    <!-- Job info strip -->
                    <div class="dps-job-strip" id="dps-job-strip"></div>

                    <!-- Big param blocks -->
                    <div class="dps-params-grid">
                        <div class="dps-param-block dps-param-temp">
                            <div class="dps-param-label">TEMP</div>
                            <div class="dps-param-value" id="dps-temp">—</div>
                            <div class="dps-param-unit">°F</div>
                        </div>
                        <div class="dps-param-block dps-param-dwell">
                            <div class="dps-param-label">DWELL</div>
                            <div class="dps-param-value" id="dps-dwell">—</div>
                            <div class="dps-param-unit">SEC</div>
                        </div>
                        <div class="dps-param-block dps-param-psi">
                            <div class="dps-param-label">PRESSURE</div>
                            <div class="dps-param-value" id="dps-psi">—</div>
                            <div class="dps-param-unit">PSI</div>
                        </div>
                    </div>

                    <!-- Peel type + pre-press banner -->
                    <div class="dps-peel-row" id="dps-peel-row"></div>

                    <!-- Placement / garment info -->
                    <div class="dps-garment-row" id="dps-garment-row"></div>

                    <!-- Action buttons -->
                    <div class="dps-action-row">
                        <button class="dps-btn dps-btn-complete" id="dps-complete-btn" disabled>
                            ✓ TRANSFER COMPLETE
                        </button>
                        <button class="dps-btn dps-btn-reset" id="dps-reset-btn">
                            ↩ NEW JOB
                        </button>
                    </div>

                    <!-- Result banner — shown after completion -->
                    <div class="dps-result" id="dps-result" style="display:none;"></div>
                </div>

            </div>
        `);
    }

    _bind_events() {
        const $body = this.$body;

        $body.on("click", "#dps-scan-btn",     () => this._load_job());
        $body.on("click", "#dps-complete-btn", () => this._complete_transfer());
        $body.on("click", "#dps-reset-btn",    () => this._reset());

        $body.on("keydown", "#dps-jc-input", (e) => {
            if (e.key === "Enter") this._load_job();
        });

        setTimeout(() => $body.find("#dps-jc-input").focus(), 300);
    }

    _setup_realtime() {
        frappe.realtime.on("dtf_press_dispatched", (data) => {
            if (data.job_card && data.job_card === this.job_card) {
                this._show_msg(`Press dispatched for ${data.job_card}`, "success");
            }
        });
    }

    // ── Load job ──────────────────────────────────────────────────────────────

    _load_job() {
        const jc_name = this.$body.find("#dps-jc-input").val().trim().toUpperCase();
        if (!jc_name) {
            this._show_msg("Please scan or enter a Job Card.", "warning");
            return;
        }

        this._set_chip("LOADING…", "loading");
        this._show_msg("");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtf_press_scan_and_load",
            args:   { job_card_name: jc_name },
            callback: (r) => {
                const result = r.message || {};
                if (!result.ok) {
                    this._on_load_error(result);
                    return;
                }
                this.job_card       = result.job_card || jc_name;
                this.press_params   = result.press_params || {};
                this.machine_job_id = result.machine_job_id || "";
                this.state          = "ready";
                this._render_press_card(result);
            },
            error: () => {
                this._set_chip("ERROR", "error");
                this._show_msg("Server error — please try again.", "error");
            },
        });
    }

    _on_load_error(result) {
        const msgs = {
            "not_dtf_job":         "This Job Card is not a DTF job.",
            "no_press_configured": "No active press machine configured in ALICE.",
            "press_driver_error":  "Press driver error: " + (result.detail || ""),
            "safety_violation":    "⚠️ SAFETY VIOLATION — " + (result.violations || []).join(" | "),
        };
        const msg = msgs[result.error] || ("Error: " + (result.error || "unknown"));
        this._set_chip("ERROR", "error");
        this._show_msg(msg, result.error === "safety_violation" ? "safety" : "error");
    }

    _render_press_card(result) {
        const p = this.press_params;

        // Reset picker state
        this.selected_machine  = null;
        this.selected_operator = null;
        this._stored_operators = result.certified_operators || [];

        // Update param blocks
        this.$body.find("#dps-temp").text(p.press_temp_f || "—");
        this.$body.find("#dps-dwell").text(p.dwell_time_sec || "—");
        this.$body.find("#dps-psi").text(p.pressure_psi || "—");

        // Peel type row
        const peel     = p.peel_type || "Hot";
        const pre      = p.pre_press_sec > 0 ? ` · Pre-press ${p.pre_press_sec}s` : "";
        const peel_color = { Hot: "#e74c3c", Warm: "#e67e22", Cold: "#2980b9" }[peel] || "#555";
        this.$body.find("#dps-peel-row").html(`
            <span class="dps-peel-badge" style="background:${peel_color}">
                ${peel} PEEL${pre}
            </span>
        `);

        // Garment info
        const placement = result.design_placement || result.job_card || "";
        const size      = result.garment_size || "";
        const fabric    = result.fabric_type || "";
        const garment_parts = [placement, size, fabric].filter(Boolean);
        this.$body.find("#dps-garment-row").html(
            garment_parts.length
                ? `<span class="dps-garment-info">${garment_parts.join(" · ")}</span>`
                : ""
        );

        // Job strip
        this.$body.find("#dps-job-strip").html(`
            <span class="dps-job-name">${this.job_card}</span>
            <span class="dps-job-id">${this.machine_job_id}</span>
        `);

        // Show layout — Transfer Complete stays disabled until machine + operator chosen
        this.$body.find("#dps-scan-zone").addClass("dps-scan-minimised");
        this.$body.find("#dps-press-card").show();
        this.$body.find("#dps-complete-btn").prop("disabled", true).removeClass("dps-btn-done");
        this.$body.find("#dps-result").hide();

        this._set_chip("SELECT PRESS & OPERATOR", "loading");

        // Machine picker
        this._render_machine_picker(result.press_machines || []);
        this.$body.find("#dps-operator-section").hide();

        frappe.show_alert({
            message: `Loaded ${this.job_card} — ${p.press_temp_f}°F / ${p.dwell_time_sec}s / ${p.pressure_psi} PSI`,
            indicator: "green",
        }, 5);
    }

    // ── Machine picker ────────────────────────────────────────────────────────

    _render_machine_picker(machines) {
        const $section = this.$body.find("#dps-machine-picker-section");
        const $grid    = this.$body.find("#dps-machine-grid");

        if (!machines.length) {
            $grid.html(`<div class="dps-no-machines">No active press machines found — check Machine Config.</div>`);
            $section.show();
            return;
        }

        const cards = machines.map(m => {
            const online_color = m.online ? "#22c55e" : "#ef4444";
            const busy_label   = m.busy
                ? `<span class="dps-mc-busy">BUSY — ${m.current_job || "job in progress"}</span>`
                : "";
            return `
                <div class="dps-mc-card" data-name="${m.name}">
                    <div class="dps-mc-toprow">
                        <span class="dps-mc-dot" style="background:${online_color}"></span>
                        <span class="dps-mc-id">${m.machine_id || m.name}</span>
                    </div>
                    <div class="dps-mc-status">${m.online ? "Online" : "Offline"}</div>
                    ${busy_label}
                </div>
            `;
        }).join("");

        $grid.html(cards);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".dps-mc-card", function () {
            const name = $(this).data("name");
            const m    = machines.find(x => x.name === name);
            if (!m) return;

            me.selected_machine = name;

            $grid.find(".dps-mc-card").removeClass("dps-mc-selected");
            $(this).addClass("dps-mc-selected");

            me._set_chip("SELECT OPERATOR", "loading");

            if (m.busy) {
                frappe.show_alert({
                    message: `⚠️ ${m.machine_id} has job ${m.current_job || ""} in progress`,
                    indicator: "orange",
                }, 6);
            }

            me._render_operator_selector(me._stored_operators || []);
        });
    }

    // ── Operator selector ─────────────────────────────────────────────────────

    _render_operator_selector(operators) {
        const $section = this.$body.find("#dps-operator-section");
        const $grid    = this.$body.find("#dps-operator-grid");

        const LEVEL_COLOR = { Expert: "#7c3aed", Certified: "#2563eb", Trainee: "#d97706" };
        const LEVEL_ICON  = { Expert: "★", Certified: "✓", Trainee: "◎" };

        if (!operators.length) {
            $grid.html(`<div class="dps-op-empty">No certified press operators found. Contact your supervisor.</div>`);
            $section.show();
            return;
        }

        const cards = operators.map(op => {
            const lvl   = op.proficiency_level || "Certified";
            const color = LEVEL_COLOR[lvl] || "#2563eb";
            const icon  = LEVEL_ICON[lvl]  || "✓";
            return `
                <div class="dps-op-card" data-emp="${op.employee}">
                    <div class="dps-op-name">${op.employee_name || op.employee}</div>
                    <div class="dps-op-level" style="color:${color}">${icon} ${lvl}</div>
                </div>
            `;
        }).join("");

        const not_listed = `
            <div class="dps-op-card dps-op-unlisted" data-emp="__unlisted__">
                <div class="dps-op-name">Not listed</div>
                <div class="dps-op-level" style="color:#6b7280">⚠ Supervisor override</div>
            </div>
        `;

        $grid.html(cards + not_listed);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".dps-op-card", function () {
            const emp = $(this).data("emp");
            $grid.find(".dps-op-card").removeClass("dps-op-selected");
            $(this).addClass("dps-op-selected");
            me.selected_operator = emp;

            if (emp === "__unlisted__") {
                me.$body.find("#dps-op-warn")
                    .text("⚠ Supervisor override: log this in the shift notes.")
                    .show();
            } else {
                me.$body.find("#dps-op-warn").hide();
            }

            // Enable Transfer Complete now that both machine + operator are chosen
            me.$body.find("#dps-complete-btn").prop("disabled", false);
            me._set_chip("SET PRESS & RUN", "ready");
        });
    }

    // ── Complete transfer ──────────────────────────────────────────────────────

    _complete_transfer() {
        if (!this.job_card) return;
        if (this.state === "complete") return;

        if (!this.selected_machine) {
            frappe.show_alert({ message: "Select a press machine first.", indicator: "orange" }, 4);
            return;
        }
        if (!this.selected_operator) {
            frappe.show_alert({ message: "Select your operator card first.", indicator: "orange" }, 4);
            return;
        }

        const $btn = this.$body.find("#dps-complete-btn");
        $btn.prop("disabled", true).text("SAVING…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.dtf_press_complete",
            args:   {
                job_card_name:       this.job_card,
                machine_config_name: this.selected_machine,
                operator_employee:   this.selected_operator !== "__unlisted__"
                                         ? this.selected_operator : null,
            },
            callback: (r) => {
                const result = r.message || {};
                if (result.ok) {
                    this._on_complete_success(result);
                } else {
                    $btn.prop("disabled", false).text("✓ TRANSFER COMPLETE");
                    this._show_msg("Could not mark complete: " + (result.error || "unknown"), "error");
                }
            },
            error: () => {
                $btn.prop("disabled", false).text("✓ TRANSFER COMPLETE");
                this._show_msg("Server error — please retry.", "error");
            },
        });
    }

    _on_complete_success(result) {
        this.state = "complete";
        const next = result.next_stage || "QC";

        this.$body.find("#dps-complete-btn")
            .text("✓ DONE")
            .addClass("dps-btn-done")
            .prop("disabled", true);

        this.$body.find("#dps-result").html(`
            <div class="dps-result-ok">
                <div class="dps-result-icon">✓</div>
                <div class="dps-result-text">
                    Transfer logged.<br/>
                    <strong>${this.job_card}</strong> → ${next}
                </div>
            </div>
        `).show();

        this._set_chip("COMPLETE", "complete");

        // Auto-reset after 8 seconds — ready for next garment
        setTimeout(() => this._reset(), 8000);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _reset() {
        this.job_card           = null;
        this.press_params       = null;
        this.machine_job_id     = null;
        this.selected_machine   = null;
        this.selected_operator  = null;
        this._stored_operators  = [];
        this.state              = "idle";

        this.$body.find("#dps-jc-input").val("");
        this.$body.find("#dps-scan-zone").removeClass("dps-scan-minimised");
        this.$body.find("#dps-machine-picker-section").hide();
        this.$body.find("#dps-operator-section").hide();
        this.$body.find("#dps-op-warn").hide();
        this.$body.find("#dps-press-card").hide();
        this.$body.find("#dps-result").hide();
        this.$body.find("#dps-complete-btn").prop("disabled", true).text("✓ TRANSFER COMPLETE").removeClass("dps-btn-done");
        this._set_chip("READY TO SCAN", "idle");
        this._show_msg("");

        setTimeout(() => this.$body.find("#dps-jc-input").focus(), 100);
    }

    _set_chip(text, state) {
        const colors = {
            idle:     "#6c757d",
            loading:  "#f0ad4e",
            ready:    "#27ae60",
            complete: "#2980b9",
            error:    "#e74c3c",
        };
        this.$body.find("#dps-chip")
            .text(text)
            .css("background", colors[state] || "#6c757d");
    }

    _show_msg(text, type = "") {
        const colors = {
            success: "#27ae60",
            warning: "#f39c12",
            error:   "#e74c3c",
            safety:  "#c0392b",
        };
        const $el = this.$body.find("#dps-scan-msg");
        $el.text(text).css("color", colors[type] || "#555");
    }

    // ── Styles ────────────────────────────────────────────────────────────────

    _init_styles() {
        if (document.getElementById("dps-styles")) return;
        const style = document.createElement("style");
        style.id = "dps-styles";
        style.textContent = `
/* ── Wrap ── */
.dps-wrap {
    max-width: 680px;
    margin: 0 auto;
    padding: 16px 12px 40px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
}

/* ── Scan zone ── */
.dps-scan-zone {
    background: #1a1a2e;
    border-radius: 16px;
    padding: 24px 20px 20px;
    margin-bottom: 14px;
    transition: padding 0.25s, margin 0.25s;
}
.dps-scan-zone.dps-scan-minimised {
    padding: 14px 20px;
    margin-bottom: 14px;
}
.dps-header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
}
.dps-scan-zone.dps-scan-minimised .dps-header-row {
    margin-bottom: 0;
}
.dps-logo {
    font-size: 1.15rem;
    font-weight: 700;
    color: #fff;
    letter-spacing: 0.02em;
}
.dps-status-chip {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: #fff;
    background: #6c757d;
    border-radius: 20px;
    padding: 4px 14px;
    transition: background 0.3s;
}
.dps-scan-input-row {
    display: flex;
    gap: 10px;
}
.dps-scan-zone.dps-scan-minimised .dps-scan-input-row {
    display: none;
}
.dps-scan-input-row input {
    flex: 1;
    padding: 12px 16px;
    font-size: 1rem;
    border-radius: 10px;
    border: 2px solid rgba(255,255,255,0.15);
    background: rgba(255,255,255,0.08);
    color: #fff;
    outline: none;
    transition: border-color 0.2s;
}
.dps-scan-input-row input::placeholder { color: rgba(255,255,255,0.4); }
.dps-scan-input-row input:focus { border-color: rgba(255,255,255,0.45); }
.dps-scan-msg {
    margin-top: 10px;
    font-size: 0.88rem;
    font-weight: 500;
    min-height: 1.2em;
}

/* ── Picker headers ── */
.dps-picker-header {
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    color: #6b7280;
    margin: 12px 0 8px;
    text-transform: uppercase;
}

/* ── Machine picker grid ── */
.dps-machine-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 12px;
}
.dps-mc-card {
    background: #1e293b;
    border: 2px solid #334155;
    border-radius: 10px;
    padding: 12px 16px;
    cursor: pointer;
    min-width: 150px;
    flex: 1;
    transition: border-color 0.2s, transform 0.1s;
}
.dps-mc-card:hover { border-color: #6366f1; transform: translateY(-1px); }
.dps-mc-card.dps-mc-selected { border-color: #6366f1; background: #1e1b4b; }
.dps-mc-toprow { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.dps-mc-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.dps-mc-id { font-size: 0.9rem; font-weight: 700; color: #f1f5f9; }
.dps-mc-status { font-size: 0.75rem; color: #94a3b8; }
.dps-mc-busy { font-size: 0.72rem; color: #fbbf24; font-weight: 600; margin-top: 4px; display: block; }
.dps-no-machines { font-size: 0.85rem; color: #94a3b8; padding: 8px 0; }

/* ── Operator selector ── */
.dps-operator-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 8px;
}
.dps-op-card {
    background: #1e293b;
    border: 2px solid #334155;
    border-radius: 10px;
    padding: 10px 14px;
    cursor: pointer;
    min-width: 130px;
    transition: border-color 0.2s, transform 0.1s;
}
.dps-op-card:hover { border-color: #6366f1; transform: translateY(-1px); }
.dps-op-card.dps-op-selected { border-color: #6366f1; background: #1e1b4b; }
.dps-op-unlisted { border-style: dashed; border-color: #475569; }
.dps-op-unlisted.dps-op-selected { border-color: #f59e0b; background: #451a03; }
.dps-op-name { font-size: 0.88rem; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }
.dps-op-level { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em; }
.dps-op-empty { font-size: 0.85rem; color: #fbbf24; padding: 8px 0; }
.dps-op-warn {
    font-size: 0.8rem;
    color: #fbbf24;
    background: rgba(245,158,11,0.12);
    border: 1px solid #d97706;
    border-radius: 6px;
    padding: 6px 12px;
    margin-bottom: 8px;
}

/* ── Buttons ── */
.dps-btn {
    border: none;
    border-radius: 10px;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: transform 0.1s, opacity 0.2s;
}
.dps-btn:active { transform: scale(0.97); }
.dps-btn:disabled { opacity: 0.5; cursor: default; }
.dps-btn-primary {
    background: #e74c3c;
    color: #fff;
    padding: 12px 22px;
    font-size: 0.9rem;
}
.dps-btn-complete {
    flex: 1;
    padding: 18px;
    font-size: 1.1rem;
    background: #27ae60;
    color: #fff;
    border-radius: 12px;
}
.dps-btn-complete.dps-btn-done { background: #2980b9; }
.dps-btn-reset {
    padding: 18px 20px;
    font-size: 0.95rem;
    background: #2c3e50;
    color: #fff;
    border-radius: 12px;
}

/* ── Press card ── */
.dps-press-card {
    background: #fff;
    border-radius: 16px;
    box-shadow: 0 4px 24px rgba(0,0,0,0.10);
    overflow: hidden;
    margin-top: 14px;
}
.dps-job-strip {
    background: #1a1a2e;
    color: #fff;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.dps-job-name {
    font-size: 1.05rem;
    font-weight: 700;
    letter-spacing: 0.03em;
}
.dps-job-id {
    font-size: 0.78rem;
    opacity: 0.6;
    font-family: monospace;
}

/* ── Params grid ── */
.dps-params-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    background: #e9ecef;
}
.dps-param-block {
    background: #fff;
    text-align: center;
    padding: 28px 10px 20px;
}
.dps-param-label {
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: #6c757d;
    margin-bottom: 6px;
}
.dps-param-value {
    font-size: 3.6rem;
    font-weight: 800;
    line-height: 1;
    color: #1a1a2e;
}
.dps-param-unit {
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: #6c757d;
    margin-top: 4px;
}
.dps-param-temp  .dps-param-value { color: #c0392b; }
.dps-param-dwell .dps-param-value { color: #1a6e3c; }
.dps-param-psi   .dps-param-value { color: #1a3a6e; }

/* ── Peel + garment rows ── */
.dps-peel-row {
    padding: 14px 20px 6px;
    border-top: 1px solid #f0f0f0;
}
.dps-peel-badge {
    display: inline-block;
    color: #fff;
    font-weight: 700;
    font-size: 0.9rem;
    letter-spacing: 0.06em;
    border-radius: 8px;
    padding: 6px 18px;
}
.dps-garment-row {
    padding: 8px 20px 14px;
}
.dps-garment-info {
    font-size: 0.88rem;
    color: #555;
    font-weight: 500;
}

/* ── Action row ── */
.dps-action-row {
    display: flex;
    gap: 10px;
    padding: 16px 20px;
    border-top: 1px solid #f0f0f0;
}

/* ── Result banner ── */
.dps-result {
    padding: 18px 20px;
    border-top: 1px solid #d5f0e2;
    background: #f0fff6;
}
.dps-result-ok {
    display: flex;
    align-items: center;
    gap: 16px;
}
.dps-result-icon {
    font-size: 2.5rem;
    color: #27ae60;
    line-height: 1;
}
.dps-result-text {
    font-size: 1rem;
    color: #1a4a2e;
    line-height: 1.5;
}

/* ── Responsive ── */
@media (max-width: 420px) {
    .dps-param-value { font-size: 2.8rem; }
    .dps-btn-complete { font-size: 1rem; padding: 16px; }
}
        `;
        document.head.appendChild(style);
    }
}
