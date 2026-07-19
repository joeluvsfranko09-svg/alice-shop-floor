/**
 * ALICE — Embroidery Station  (Task #67)
 * =======================================
 * Tablet page mounted at the Melco Summit 15-needle embroidery head.
 * Operator scans a Job Card → DST gate check + thread color sequence →
 * "Send to Machine" pushes DST via FTP → status poll →
 * "Hoop Off / Complete" stamps Job Card and advances stage.
 *
 * EMB workflow (this page handles the full stitch step):
 *   Step 1  THIS PAGE — Melco Summit stitches the design
 *   Step 2  Final QC / Garment Passport seal
 *
 * Key difference from DTG/DTF:
 *   - DST gate: DigitizingQueue must be Approved or Released before send.
 *   - Thread color sequence: displays needle #, thread code, and color swatch
 *     so operator loads bobbins in the correct needle order.
 *   - Status polling: FTP file presence = Queued, file absent = Complete
 *     (Melco OS / SUMMIT Manager picked up the file and is stitching).
 *   - Completion: operator physically unhoops the garment and taps
 *     "Hoop Off / Complete" — there is no automatic "cure" step.
 *
 * Flow:
 *   1. Operator opens page on Embroidery-station tablet.
 *   2. Scans / types Job Card name.
 *   3. DST gate status renders (Approved ✓ / Pending ✗).
 *   4. Thread color sequence renders with needle assignments.
 *   5. Operator loads thread bobbins in correct order.
 *   6. Taps "Send to Machine" → DST dropped to Melco FTP.
 *   7. Status badge polls every 5s: Queued → Complete.
 *   8. Taps "Hoop Off / Complete" → Job Card stamped, stage advances.
 *
 * API endpoints:
 *   alice_shop_floor.alice_shop_floor.api.emb_scan_and_load
 *   alice_shop_floor.alice_shop_floor.api.emb_start_job
 *   alice_shop_floor.alice_shop_floor.api.emb_job_status
 *   alice_shop_floor.alice_shop_floor.api.emb_job_complete
 */

frappe.pages["emb-station"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Embroidery Station",
        single_column: true,
    });
    window.emb_station = new EmbStation(page, wrapper);
};

// ─────────────────────────────────────────────────────────────────────────────

class EmbStation {
    constructor(page, wrapper) {
        this.page    = page;
        this.wrapper = wrapper;
        this.$body   = $(wrapper).find(".layout-main-section, .page-content").first();

        this.job_card           = null;
        this.machine_job_id     = null;
        this.machine_name       = null;   // name of selected MachineConfig
        this.selected_machine   = null;   // full machine record from picker
        this.selected_operator  = null;   // employee id from operator selector
        this._stored_operators  = [];
        this.poll_timer         = null;
        this._stitch_start_ts   = null;
        // idle | loaded | sending | stitching | hoop_ready | done
        this.state              = "idle";

        this._init_styles();
        this._init_layout();
        this._bind_events();
        this._setup_realtime();
    }

    // ── Layout ────────────────────────────────────────────────────────────────

    _init_layout() {
        this.$body.html(`
            <div class="emb-wrap">

                <!-- Top bar -->
                <div class="emb-topbar">
                    <div class="emb-machine-info">
                        <span class="emb-machine-icon">🪡</span>
                        <span class="emb-machine-label" id="emb-machine-label">Melco Summit — Embroidery</span>
                        <span class="emb-machine-dot" id="emb-machine-dot" title="Machine status"></span>
                    </div>
                    <div class="emb-status-row">
                        <span class="emb-status-dot" id="emb-status-dot"></span>
                        <span class="emb-status-text" id="emb-status-text">Ready</span>
                        <span class="emb-elapsed" id="emb-elapsed" style="display:none;"></span>
                    </div>
                </div>

                <!-- Scan input -->
                <div class="emb-scan-row" id="emb-scan-row">
                    <input
                        id="emb-jc-input"
                        class="emb-scan-input"
                        type="text"
                        placeholder="Scan or type Job Card…"
                        autocomplete="off"
                        autofocus
                    />
                    <button id="emb-load-btn" class="emb-btn emb-btn-primary">LOAD</button>
                </div>

                <!-- Message bar -->
                <div class="emb-msg-bar" id="emb-msg-bar" style="display:none;"></div>

                <!-- Main card (hidden until job loaded) -->
                <div class="emb-main-card" id="emb-main-card" style="display:none;">

                    <!-- Job header -->
                    <div class="emb-job-header">
                        <div class="emb-job-meta">
                            <div class="emb-job-id" id="emb-job-id"></div>
                            <div class="emb-job-item" id="emb-job-item"></div>
                        </div>
                        <button id="emb-reset-btn" class="emb-btn emb-btn-ghost emb-btn-sm" style="display:none;">✕ RESET</button>
                    </div>

                    <!-- DST Gate banner -->
                    <div class="emb-dst-gate" id="emb-dst-gate">
                        <div class="emb-dst-icon" id="emb-dst-icon"></div>
                        <div class="emb-dst-info">
                            <div class="emb-dst-label">DST File Gate</div>
                            <div class="emb-dst-status" id="emb-dst-status-text">Checking…</div>
                        </div>
                        <div class="emb-dst-badge" id="emb-dst-badge"></div>
                    </div>

                    <!-- Machine picker (shown after job loads) -->
                    <div class="emb-machine-picker-section" id="emb-machine-picker-section" style="display:none;">
                        <div class="emb-section-title">Select Machine — tap a head to send the job</div>
                        <div class="emb-machine-grid" id="emb-machine-grid"></div>
                    </div>

                    <!-- Operator selector (shown after machine selected) -->
                    <div class="emb-operator-section" id="emb-operator-section" style="display:none;">
                        <div class="emb-section-title">Select Operator</div>
                        <div class="emb-operator-grid" id="emb-operator-grid"></div>
                        <div class="emb-op-warn" id="emb-op-warn" style="display:none;"></div>
                    </div>

                    <!-- Stitch parameters -->
                    <div class="emb-params-grid">
                        <div class="emb-param-block">
                            <div class="emb-param-label">Stitch Count</div>
                            <div class="emb-param-value" id="emb-stitch-count">—</div>
                        </div>
                        <div class="emb-param-block">
                            <div class="emb-param-label">Hoop Size</div>
                            <div class="emb-param-value" id="emb-hoop-size">—</div>
                        </div>
                        <div class="emb-param-block">
                            <div class="emb-param-label">Speed</div>
                            <div class="emb-param-value" id="emb-speed">— spm</div>
                        </div>
                        <div class="emb-param-block">
                            <div class="emb-param-label">Underlay</div>
                            <div class="emb-param-value" id="emb-underlay">—</div>
                        </div>
                        <div class="emb-param-block">
                            <div class="emb-param-label">Density</div>
                            <div class="emb-param-value" id="emb-density">— %</div>
                        </div>
                        <div class="emb-param-block">
                            <div class="emb-param-label">Threads</div>
                            <div class="emb-param-value" id="emb-thread-count">—</div>
                        </div>
                    </div>

                    <!-- Thread color sequence -->
                    <div class="emb-thread-section">
                        <div class="emb-section-title">Thread Color Sequence — Load Bobbins in Order</div>
                        <div class="emb-thread-list" id="emb-thread-list">
                            <div class="emb-thread-empty">No thread color data</div>
                        </div>
                    </div>

                    <!-- Action buttons -->
                    <div class="emb-action-row">
                        <button id="emb-send-btn" class="emb-btn emb-btn-primary emb-btn-large">
                            🧵 SEND TO MACHINE
                        </button>
                        <button id="emb-complete-btn" class="emb-btn emb-btn-success emb-btn-large" style="display:none;">
                            ✓ HOOP OFF / COMPLETE
                        </button>
                    </div>

                    <!-- Done message -->
                    <div class="emb-done-msg" id="emb-done-msg" style="display:none;">
                        <div class="emb-done-icon">✓</div>
                        <div class="emb-done-text">Embroidery Complete</div>
                        <div class="emb-done-sub">Resetting for next garment…</div>
                    </div>

                </div>
            </div>
        `);
    }

    // ── Event Binding ─────────────────────────────────────────────────────────

    _bind_events() {
        this.$body.on("click", "#emb-load-btn", () => this._load_job());

        this.$body.on("keydown", "#emb-jc-input", (e) => {
            if (e.key === "Enter") this._load_job();
        });

        this.$body.on("click", "#emb-send-btn", () => this._send_to_machine());

        this.$body.on("click", "#emb-complete-btn", () => this._mark_complete());

        this.$body.on("click", "#emb-reset-btn", () => this._reset());
    }

    // ── Realtime ──────────────────────────────────────────────────────────────

    _setup_realtime() {
        frappe.realtime.on("emb_job_complete", (data) => {
            if (data.job_card === this.job_card && this.state !== "done") {
                this._on_done();
            }
        });
    }

    // ── Core workflow ─────────────────────────────────────────────────────────

    _load_job() {
        const raw = this.$body.find("#emb-jc-input").val().trim();
        if (!raw) {
            this._msg("Scan or type a Job Card name first.", "warning");
            return;
        }

        this.$body.find("#emb-load-btn").prop("disabled", true).text("Loading…");
        this.$body.find("#emb-jc-input").prop("disabled", true);

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.emb_scan_and_load",
            args:   { job_card_name: raw },
            callback: (r) => {
                this.$body.find("#emb-load-btn").prop("disabled", false).text("LOAD");
                const d = r.message || {};

                if (!d.ok) {
                    this.$body.find("#emb-jc-input").prop("disabled", false).select();
                    const msgs = {
                        not_emb_job:       "This is not an Embroidery job — check the station.",
                        job_not_found:     "Job Card not found. Check the scan.",
                        already_complete:  "Job is already complete.",
                        wrong_stage:       "Job is not ready for embroidery yet.",
                    };
                    this._msg(msgs[d.error] || (d.detail || "Could not load job."), "error");
                    return;
                }

                this._render_job(d);
            },
        });
    }

    _render_job(d) {
        this.job_card           = d.job_card || d.name;
        this.machine_name       = d.machine_name || null;
        this.selected_machine   = null;  // must be chosen from picker
        this.selected_operator  = null;  // must be chosen from operator selector
        this._stored_operators  = d.certified_operators || [];
        this.state              = "loaded";

        // Reset topbar to "no machine selected" state
        this.$body.find("#emb-machine-dot").css("background", "#6b7280").attr("title", "No machine selected");
        this.$body.find("#emb-machine-label").text("Embroidery — Select a machine");

        // Job header
        this.$body.find("#emb-job-id").text(this.job_card);
        this.$body.find("#emb-job-item").text(
            [d.item_name, d.work_order, d.garment_size ? `Size ${d.garment_size}` : null]
                .filter(Boolean).join("  ·  ")
        );

        // DST gate
        this._render_dst_gate(d.dst_status, d.dst_approved);
        this._dst_approved = d.dst_approved;

        // Machine picker — requires operator to explicitly choose a head
        this._render_machine_picker(d.available_machines || []);
        this.$body.find("#emb-operator-section").hide();

        // Params
        this.$body.find("#emb-stitch-count").text(
            d.stitch_count ? Number(d.stitch_count).toLocaleString() : "—"
        );
        this.$body.find("#emb-hoop-size").text(d.hoop_size || "—");
        this.$body.find("#emb-speed").text(d.speed_spm ? `${d.speed_spm} spm` : "—");
        this.$body.find("#emb-underlay").text(d.underlay_type || "—");
        this.$body.find("#emb-density").text(d.density != null ? `${d.density}%` : "—");
        this.$body.find("#emb-thread-count").text(d.thread_count || (d.thread_colors || []).length || "—");

        // Thread color sequence
        this._render_thread_colors(d.thread_colors || []);

        // Send button starts disabled — operator must pick a machine first
        this.$body.find("#emb-send-btn")
            .prop("disabled", true)
            .attr("title", "Select a machine above before sending.");

        // Show card
        this.$body.find("#emb-main-card").show();
        this.$body.find("#emb-reset-btn").show();
        this.$body.find("#emb-scan-row").hide();

        this._set_status("ready", "Job loaded — select machine and check thread colors");
        this._msg("", "");
    }

    _render_machine_picker(machines) {
        const me  = this;
        const $section = me.$body.find("#emb-machine-picker-section");
        const $grid    = me.$body.find("#emb-machine-grid").empty();

        if (!machines || machines.length === 0) {
            $grid.html('<div class="emb-thread-empty">No Melco machines configured. Add a MachineConfig with driver MelcoOS.</div>');
            $section.show();
            return;
        }

        machines.forEach(function (m) {
            const online_color = m.online  ? "#22c55e" : "#ef4444";
            const online_label = m.online  ? "Online"  : (m.status || "Offline");
            const busy_label   = m.busy    ? `Busy — ${m.current_job || ""}` : "Idle";
            const busy_color   = m.busy    ? "#f59e0b" : "#22c55e";
            const hoop_badge   = m.hoop_size
                ? `<span class="emb-mc-hoop">${m.hoop_size}</span>` : "";
            const compat_warn  = !m.compatible && m.hoop_size
                ? '<div class="emb-mc-warn">Hoop mismatch</div>' : "";
            const head_txt     = m.head_count > 1 ? `${m.head_count}-head` : "1-head";

            const card = `
                <div class="emb-mc-card ${!m.compatible ? "emb-mc-incompatible" : ""}"
                     data-machine="${m.name}"
                     data-online="${m.online}"
                     data-compatible="${m.compatible}"
                     title="${m.name} — ${online_label}">
                    <div class="emb-mc-toprow">
                        <span class="emb-mc-dot" style="background:${online_color}"></span>
                        <span class="emb-mc-id">${m.machine_id || m.name}</span>
                        ${hoop_badge}
                        <span class="emb-mc-heads">${head_txt}</span>
                    </div>
                    <div class="emb-mc-status" style="color:${busy_color}">${busy_label}</div>
                    ${compat_warn}
                </div>
            `;
            $grid.append(card);
        });

        // Click handler: select a machine card
        $grid.off("click").on("click", ".emb-mc-card", function () {
            const name    = $(this).data("machine");
            const online  = $(this).data("online");
            const compat  = $(this).data("compatible");

            // Visual: deselect all, select this one
            $grid.find(".emb-mc-card").removeClass("emb-mc-selected");
            $(this).addClass("emb-mc-selected");

            // Record selection
            const rec = machines.find(function (m) { return m.name === name; });
            me.selected_machine = name;

            // Update topbar
            const dot_color = online ? "#22c55e" : "#ef4444";
            const label     = (rec && rec.machine_id) ? rec.machine_id : name;
            me.$body.find("#emb-machine-dot").css("background", dot_color).attr("title", online ? "Online" : "Offline");
            me.$body.find("#emb-machine-label").text(`${label} — Selected`);

            // Keep send disabled — operator must also be chosen
            me.$body.find("#emb-send-btn")
                .prop("disabled", true)
                .attr("title", "Select an operator below before sending.");

            // Warn if incompatible hoop but allow supervisor override
            if (!compat && rec && rec.hoop_size) {
                me._msg(`Hoop mismatch: machine has ${rec.hoop_size}, job requires a different size. Confirm with supervisor before sending.`, "warning");
            } else {
                me._msg("", "");
            }

            // Show operator selector
            me._render_operator_selector(me._stored_operators || []);
        });

        $section.show();
    }

    _render_operator_selector(operators) {
        const $section = this.$body.find("#emb-operator-section");
        const $grid    = this.$body.find("#emb-operator-grid");

        const LEVEL_COLOR = { Expert: "#7c3aed", Certified: "#2563eb", Trainee: "#d97706" };
        const LEVEL_ICON  = { Expert: "★", Certified: "✓", Trainee: "◎" };

        if (!operators.length) {
            $grid.html(`<div class="emb-thread-empty">No certified operators found for Embroidery. Contact your supervisor.</div>`);
            $section.show();
            return;
        }

        const cards = operators.map(op => {
            const lvl   = op.proficiency_level || "Certified";
            const color = LEVEL_COLOR[lvl] || "#2563eb";
            const icon  = LEVEL_ICON[lvl]  || "✓";
            return `
                <div class="emb-op-card" data-emp="${op.employee}">
                    <div class="emb-op-name">${op.employee_name || op.employee}</div>
                    <div class="emb-op-level" style="color:${color}">${icon} ${lvl}</div>
                </div>
            `;
        }).join("");

        const not_listed = `
            <div class="emb-op-card emb-op-unlisted" data-emp="__unlisted__">
                <div class="emb-op-name">Not listed</div>
                <div class="emb-op-level" style="color:#6b7280">⚠ Supervisor override</div>
            </div>
        `;

        $grid.html(cards + not_listed);
        $section.show();

        const me = this;
        $grid.off("click").on("click", ".emb-op-card", function () {
            const emp = $(this).data("emp");
            $grid.find(".emb-op-card").removeClass("emb-op-selected");
            $(this).addClass("emb-op-selected");
            me.selected_operator = emp;

            if (emp === "__unlisted__") {
                me.$body.find("#emb-op-warn")
                    .text("⚠ Supervisor override: log this in the shift notes.")
                    .show();
            } else {
                me.$body.find("#emb-op-warn").hide();
            }

            // Enable send only when DST approved
            const can_send = me._dst_approved;
            me.$body.find("#emb-send-btn")
                .prop("disabled", !can_send)
                .attr("title", !can_send ? "DST file not yet Approved." : "");
        });
    }

    _render_dst_gate(dst_status, dst_approved) {
        const gate   = this.$body.find("#emb-dst-gate");
        const icon   = this.$body.find("#emb-dst-icon");
        const text   = this.$body.find("#emb-dst-status-text");
        const badge  = this.$body.find("#emb-dst-badge");

        if (dst_approved) {
            gate.removeClass("emb-dst-blocked").addClass("emb-dst-ok");
            icon.text("✓");
            text.text(`Status: ${dst_status}`);
            badge.text("APPROVED").addClass("emb-badge-ok").removeClass("emb-badge-blocked");
        } else {
            gate.removeClass("emb-dst-ok").addClass("emb-dst-blocked");
            icon.text("✗");
            text.text(`Status: ${dst_status || "Pending"} — digitizing must be approved before you can send.`);
            badge.text("NOT APPROVED").addClass("emb-badge-blocked").removeClass("emb-badge-ok");
        }
    }

    _render_thread_colors(colors) {
        const $list = this.$body.find("#emb-thread-list");
        if (!colors || colors.length === 0) {
            $list.html('<div class="emb-thread-empty">No thread color data on this Job Card.</div>');
            return;
        }

        const rows = colors.map((c, i) => {
            const needle  = c.needle  || (i + 1);
            const code    = c.code    || c.thread_code || "—";
            const name    = c.name    || c.color_name  || "";
            const hex     = c.hex     || c.hex_color   || "#888888";
            // Ensure # prefix
            const hexVal  = hex.startsWith("#") ? hex : `#${hex}`;

            // Contrast text color for swatch label
            const r = parseInt(hexVal.slice(1, 3), 16) || 0;
            const g = parseInt(hexVal.slice(3, 5), 16) || 0;
            const b = parseInt(hexVal.slice(5, 7), 16) || 0;
            const lum = 0.299 * r + 0.587 * g + 0.114 * b;
            const txtCol = lum > 140 ? "#111" : "#fff";

            return `
                <div class="emb-thread-row">
                    <div class="emb-needle-num">${needle}</div>
                    <div class="emb-thread-swatch" style="background:${hexVal}; color:${txtCol};">
                        ${code}
                    </div>
                    <div class="emb-thread-info">
                        <div class="emb-thread-code">${code}</div>
                        <div class="emb-thread-name">${name}</div>
                    </div>
                    <div class="emb-thread-hex">${hexVal}</div>
                </div>
            `;
        }).join("");

        $list.html(rows);
    }

    _send_to_machine() {
        if (this.state !== "loaded") return;
        if (!this.job_card) return;

        if (!this.selected_machine) {
            this._msg("Select a machine head above before sending.", "warning");
            return;
        }
        if (!this.selected_operator) {
            this._msg("Select your operator card before sending.", "warning");
            return;
        }

        this.state = "sending";
        this.$body.find("#emb-send-btn").prop("disabled", true).text("Sending…");
        this._set_status("sending", "Sending DST to Melco via FTP…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.emb_start_job",
            args: {
                job_card_name:       this.job_card,
                machine_config_name: this.selected_machine,
                operator_employee:   this.selected_operator !== "__unlisted__"
                                         ? this.selected_operator : null,
            },
            callback: (r) => {
                const d = r.message || {};
                if (!d.ok) {
                    this.state = "loaded";
                    this.$body.find("#emb-send-btn").prop("disabled", false).text("🧵 SEND TO MACHINE");
                    const msgs = {
                        dst_not_approved: "DST gate rejected — digitizing not yet approved.",
                        no_dst_file:      "No DST file attached to this Job Card.",
                        machine_offline:  "Melco machine is offline.",
                        ftp_error:        "FTP transfer failed — check Melco network.",
                    };
                    const detail = msgs[d.error] || (d.message || d.detail || "Send failed.");
                    this._set_status("error", detail);
                    this._msg(detail, "error");
                    return;
                }

                this.machine_job_id   = d.machine_job_id || d.job_id || "";
                this._print_start_ts  = Date.now();
                this.state            = "stitching";
                this._set_status("stitching", "Queued in Melco — waiting for SUMMIT Manager…");
                this.$body.find("#emb-send-btn").hide();
                this._start_poll();
            },
        });
    }

    // ── Status polling ────────────────────────────────────────────────────────

    _start_poll() {
        this._stop_poll();
        this.poll_timer = setInterval(() => this._poll_status(), 5000);
        this._poll_status(); // immediate first check
    }

    _stop_poll() {
        if (this.poll_timer) {
            clearInterval(this.poll_timer);
            this.poll_timer = null;
        }
    }

    _poll_status() {
        if (!this.job_card) return;

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.emb_job_status",
            args:   { job_card_name: this.job_card },
            callback: (r) => {
                const d = r.message || {};
                const state = d.state || "Unknown";

                // Update elapsed timer
                if (this._print_start_ts) {
                    const elapsed = Math.round((Date.now() - this._print_start_ts) / 1000);
                    const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
                    const ss = String(elapsed % 60).padStart(2, "0");
                    this.$body.find("#emb-elapsed").text(`${mm}:${ss}`).show();
                }

                if (state === "Complete") {
                    // FTP file gone — Melco Manager picked it up, job is stitching/done
                    this._stop_poll();
                    this.state = "hoop_ready";
                    this._set_status("complete", "Stitching complete — unhoop and inspect");
                    this.$body.find("#emb-complete-btn").show();
                } else if (state === "Error") {
                    this._stop_poll();
                    this._set_status("error", d.error || "Machine error");
                    this._msg(d.error || "Check Melco machine.", "error");
                } else if (state === "Queued") {
                    // DST file still on FTP — SUMMIT Manager hasn't picked it up yet
                    this._set_status("stitching", "DST queued — SUMMIT Manager loading…");
                } else {
                    this._set_status("stitching", "Stitching in progress…");
                }
            },
        });
    }

    // ── Completion ────────────────────────────────────────────────────────────

    _mark_complete() {
        if (this.state !== "hoop_ready") return;
        this.$body.find("#emb-complete-btn").prop("disabled", true).text("Confirming…");

        frappe.call({
            method: "alice_shop_floor.alice_shop_floor.api.emb_job_complete",
            args:   {
                job_card_name:     this.job_card,
                operator_employee: this.selected_operator !== "__unlisted__"
                                       ? this.selected_operator : null,
            },
            callback: (r) => {
                const d = r.message || {};
                if (d.ok !== false) {
                    this._on_done();
                } else {
                    this.$body.find("#emb-complete-btn").prop("disabled", false).text("✓ HOOP OFF / COMPLETE");
                    frappe.msgprint(d.message || "Failed to confirm — try again.");
                }
            },
        });
    }

    _on_done() {
        this._stop_poll();
        this.state = "done";
        this.$body.find("#emb-complete-btn").hide();
        this.$body.find("#emb-done-msg").show();
        this._set_status("done", "Complete");
        this.$body.find("#emb-elapsed").hide();

        // Auto-reset after 6s
        setTimeout(() => this._reset(), 6000);
    }

    _reset() {
        this._stop_poll();
        this.job_card           = null;
        this.machine_job_id     = null;
        this.machine_name       = null;
        this.selected_machine   = null;
        this.selected_operator  = null;
        this._stored_operators  = [];
        this._dst_approved      = false;
        this.state              = "idle";
        this._print_start_ts    = null;

        this.$body.find("#emb-main-card").hide();
        this.$body.find("#emb-machine-picker-section").hide();
        this.$body.find("#emb-machine-grid").empty();
        this.$body.find("#emb-operator-section").hide();
        this.$body.find("#emb-operator-grid").empty();
        this.$body.find("#emb-op-warn").hide();
        this.$body.find("#emb-machine-label").text("Melco Summit — Embroidery");
        this.$body.find("#emb-machine-dot").css("background", "#6b7280").attr("title", "No machine selected");
        this.$body.find("#emb-scan-row").show();
        this.$body.find("#emb-jc-input").val("").prop("disabled", false).focus();
        this.$body.find("#emb-load-btn").show().prop("disabled", false).text("LOAD");
        this.$body.find("#emb-reset-btn").hide();
        this.$body.find("#emb-send-btn").show().prop("disabled", false).text("🧵 SEND TO MACHINE");
        this.$body.find("#emb-complete-btn").hide().prop("disabled", false).text("✓ HOOP OFF / COMPLETE");
        this.$body.find("#emb-done-msg").hide();
        this.$body.find("#emb-elapsed").hide();
        this._msg("", "");
        this._set_status("ready", "Ready");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _set_status(type, text) {
        const colors = {
            ready:    "#6b7280",
            sending:  "#f59e0b",
            stitching:"#3b82f6",
            complete: "#22c55e",
            hoop_ready:"#22c55e",
            done:     "#22c55e",
            error:    "#ef4444",
        };
        const dot = this.$body.find("#emb-status-dot");
        const txt = this.$body.find("#emb-status-text");
        dot.css("background", colors[type] || "#6b7280");
        txt.text(text);
        if (type === "stitching" || type === "sending") {
            dot.addClass("emb-dot-pulse");
        } else {
            dot.removeClass("emb-dot-pulse");
        }
    }

    _msg(text, type) {
        const $bar = this.$body.find("#emb-msg-bar");
        if (!text) { $bar.hide().text(""); return; }
        const colors = { error: "#fee2e2", warning: "#fef9c3", info: "#e0f2fe" };
        $bar
            .css("background", colors[type] || "#f3f4f6")
            .text(text)
            .show();
    }

    // ── Styles ────────────────────────────────────────────────────────────────

    _init_styles() {
        if (document.getElementById("emb-station-styles")) return;
        const style = document.createElement("style");
        style.id = "emb-station-styles";
        style.textContent = `
            /* ── Wrapper ── */
            .emb-wrap {
                max-width: 720px;
                margin: 0 auto;
                padding: 12px 16px 32px;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }

            /* ── Top bar ── */
            .emb-topbar {
                display: flex;
                justify-content: space-between;
                align-items: center;
                background: #111827;
                border-radius: 10px;
                padding: 10px 16px;
                margin-bottom: 14px;
                color: #f9fafb;
            }
            .emb-machine-info {
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .emb-machine-icon { font-size: 18px; }
            .emb-machine-label {
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.03em;
            }
            .emb-machine-dot {
                width: 10px; height: 10px;
                border-radius: 50%;
                background: #6b7280;
                display: inline-block;
                flex-shrink: 0;
            }
            .emb-status-row {
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .emb-status-dot {
                width: 10px; height: 10px;
                border-radius: 50%;
                background: #6b7280;
                display: inline-block;
                flex-shrink: 0;
            }
            .emb-status-text {
                font-size: 13px;
                font-weight: 500;
                color: #d1d5db;
            }
            .emb-elapsed {
                font-size: 12px;
                color: #9ca3af;
                font-variant-numeric: tabular-nums;
                min-width: 44px;
                text-align: right;
            }

            /* ── Pulse animation for active states ── */
            @keyframes emb-pulse {
                0%   { opacity: 1; }
                50%  { opacity: 0.35; }
                100% { opacity: 1; }
            }
            .emb-dot-pulse { animation: emb-pulse 1.4s ease-in-out infinite; }

            /* ── Scan row ── */
            .emb-scan-row {
                display: flex;
                gap: 10px;
                margin-bottom: 14px;
            }
            .emb-scan-input {
                flex: 1;
                height: 48px;
                font-size: 18px;
                padding: 0 14px;
                border: 2px solid #d1d5db;
                border-radius: 8px;
                outline: none;
                background: #fff;
                color: #111;
            }
            .emb-scan-input:focus { border-color: #6366f1; }

            /* ── Buttons ── */
            .emb-btn {
                height: 48px;
                padding: 0 20px;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 700;
                letter-spacing: 0.06em;
                cursor: pointer;
                transition: opacity 0.15s, background 0.15s;
            }
            .emb-btn:disabled { opacity: 0.45; cursor: not-allowed; }
            .emb-btn-primary  { background: #6366f1; color: #fff; }
            .emb-btn-primary:hover:not(:disabled) { background: #4f46e5; }
            .emb-btn-success  { background: #16a34a; color: #fff; }
            .emb-btn-success:hover:not(:disabled) { background: #15803d; }
            .emb-btn-ghost    { background: transparent; border: 1px solid #d1d5db; color: #6b7280; }
            .emb-btn-ghost:hover:not(:disabled) { background: #f3f4f6; }
            .emb-btn-sm       { height: 36px; font-size: 12px; padding: 0 12px; }
            .emb-btn-large    { width: 100%; height: 60px; font-size: 18px; border-radius: 10px; }

            /* ── Message bar ── */
            .emb-msg-bar {
                padding: 10px 14px;
                border-radius: 8px;
                font-size: 14px;
                margin-bottom: 12px;
                color: #111;
            }

            /* ── Main card ── */
            .emb-main-card {
                background: #fff;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                padding: 20px;
                box-shadow: 0 1px 4px rgba(0,0,0,.06);
            }

            /* ── Job header ── */
            .emb-job-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 16px;
            }
            .emb-job-id {
                font-size: 22px;
                font-weight: 800;
                color: #111;
                letter-spacing: -0.01em;
            }
            .emb-job-item {
                font-size: 13px;
                color: #6b7280;
                margin-top: 3px;
            }

            /* ── DST gate ── */
            .emb-dst-gate {
                display: flex;
                align-items: center;
                gap: 14px;
                border-radius: 10px;
                padding: 14px 16px;
                margin-bottom: 16px;
                border: 2px solid transparent;
            }
            .emb-dst-ok {
                background: #f0fdf4;
                border-color: #86efac;
            }
            .emb-dst-blocked {
                background: #fef2f2;
                border-color: #fca5a5;
            }
            .emb-dst-icon {
                font-size: 24px;
                font-weight: 900;
                width: 32px;
                text-align: center;
                flex-shrink: 0;
            }
            .emb-dst-ok    .emb-dst-icon { color: #16a34a; }
            .emb-dst-blocked .emb-dst-icon { color: #dc2626; }
            .emb-dst-label {
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.07em;
                color: #6b7280;
            }
            .emb-dst-status {
                font-size: 13px;
                color: #374151;
                margin-top: 2px;
            }
            .emb-dst-badge {
                margin-left: auto;
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.08em;
                padding: 4px 10px;
                border-radius: 20px;
                white-space: nowrap;
            }
            .emb-badge-ok {
                background: #dcfce7;
                color: #15803d;
                border: 1px solid #86efac;
            }
            .emb-badge-blocked {
                background: #fee2e2;
                color: #dc2626;
                border: 1px solid #fca5a5;
            }

            /* ── Params grid ── */
            .emb-params-grid {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 10px;
                margin-bottom: 20px;
            }
            .emb-param-block {
                background: #f9fafb;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 10px 12px;
            }
            .emb-param-label {
                font-size: 10px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.07em;
                color: #9ca3af;
                margin-bottom: 4px;
            }
            .emb-param-value {
                font-size: 18px;
                font-weight: 700;
                color: #111;
                font-variant-numeric: tabular-nums;
            }

            /* ── Thread color sequence ── */
            .emb-thread-section {
                margin-bottom: 20px;
            }
            .emb-section-title {
                font-size: 11px;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                color: #6b7280;
                margin-bottom: 10px;
            }
            .emb-thread-list {
                display: flex;
                flex-direction: column;
                gap: 6px;
                max-height: 320px;
                overflow-y: auto;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 8px;
                background: #fafafa;
            }
            .emb-thread-row {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 8px 10px;
                background: #fff;
                border-radius: 6px;
                border: 1px solid #e5e7eb;
            }
            .emb-needle-num {
                width: 32px;
                height: 32px;
                background: #111827;
                color: #f9fafb;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 13px;
                font-weight: 800;
                flex-shrink: 0;
            }
            .emb-thread-swatch {
                width: 56px;
                height: 32px;
                border-radius: 6px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 10px;
                font-weight: 700;
                flex-shrink: 0;
                border: 1px solid rgba(0,0,0,.12);
            }
            .emb-thread-info {
                flex: 1;
                min-width: 0;
            }
            .emb-thread-code {
                font-size: 14px;
                font-weight: 700;
                color: #111;
            }
            .emb-thread-name {
                font-size: 12px;
                color: #6b7280;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .emb-thread-hex {
                font-size: 11px;
                color: #9ca3af;
                font-family: monospace;
                flex-shrink: 0;
            }
            .emb-thread-empty {
                padding: 16px;
                text-align: center;
                color: #9ca3af;
                font-size: 14px;
            }

            /* ── Machine picker grid ── */
            .emb-machine-picker-section {
                margin-bottom: 18px;
            }
            .emb-machine-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
            }
            .emb-mc-card {
                flex: 1;
                min-width: 140px;
                max-width: 220px;
                background: #f9fafb;
                border: 2px solid #e5e7eb;
                border-radius: 10px;
                padding: 12px 14px;
                cursor: pointer;
                transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
                user-select: none;
            }
            .emb-mc-card:hover {
                border-color: #6366f1;
                box-shadow: 0 0 0 3px rgba(99,102,241,0.12);
            }
            .emb-mc-selected {
                border-color: #6366f1 !important;
                background: #eef2ff !important;
                box-shadow: 0 0 0 3px rgba(99,102,241,0.20);
            }
            .emb-mc-incompatible {
                opacity: 0.65;
                border-style: dashed;
            }
            .emb-mc-toprow {
                display: flex;
                align-items: center;
                gap: 7px;
                margin-bottom: 5px;
                flex-wrap: wrap;
            }
            .emb-mc-dot {
                width: 10px; height: 10px;
                border-radius: 50%;
                flex-shrink: 0;
            }
            .emb-mc-id {
                font-size: 13px;
                font-weight: 700;
                color: #111;
                flex: 1;
                min-width: 0;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .emb-mc-hoop {
                font-size: 10px;
                font-weight: 700;
                background: #111827;
                color: #f9fafb;
                padding: 2px 7px;
                border-radius: 10px;
                white-space: nowrap;
            }
            .emb-mc-heads {
                font-size: 10px;
                color: #9ca3af;
                white-space: nowrap;
            }
            .emb-mc-status {
                font-size: 12px;
                font-weight: 600;
            }
            .emb-mc-warn {
                font-size: 10px;
                color: #d97706;
                font-weight: 700;
                margin-top: 4px;
            }

            /* ── Operator selector ── */
            .emb-operator-section {
                margin-bottom: 18px;
            }
            .emb-operator-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-bottom: 6px;
            }
            .emb-op-card {
                background: #f9fafb;
                border: 2px solid #e5e7eb;
                border-radius: 10px;
                padding: 10px 14px;
                cursor: pointer;
                min-width: 130px;
                transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
                user-select: none;
            }
            .emb-op-card:hover {
                border-color: #6366f1;
                box-shadow: 0 0 0 3px rgba(99,102,241,0.12);
            }
            .emb-op-card.emb-op-selected {
                border-color: #6366f1 !important;
                background: #eef2ff !important;
                box-shadow: 0 0 0 3px rgba(99,102,241,0.20);
            }
            .emb-op-unlisted {
                border-style: dashed;
                border-color: #d1d5db;
            }
            .emb-op-unlisted.emb-op-selected {
                border-color: #d97706 !important;
                background: #fffbeb !important;
            }
            .emb-op-name {
                font-size: 0.88rem;
                font-weight: 700;
                color: #111827;
                margin-bottom: 4px;
            }
            .emb-op-level {
                font-size: 0.72rem;
                font-weight: 600;
                letter-spacing: 0.04em;
            }
            .emb-op-warn {
                font-size: 0.8rem;
                color: #92400e;
                background: #fffbeb;
                border: 1px solid #d97706;
                border-radius: 6px;
                padding: 6px 12px;
                margin-bottom: 8px;
            }

            /* ── Action row ── */
            .emb-action-row {
                display: flex;
                flex-direction: column;
                gap: 10px;
            }

            /* ── Done message ── */
            .emb-done-msg {
                text-align: center;
                padding: 32px 16px;
            }
            .emb-done-icon {
                font-size: 56px;
                color: #16a34a;
                line-height: 1;
                margin-bottom: 12px;
            }
            .emb-done-text {
                font-size: 24px;
                font-weight: 800;
                color: #111;
            }
            .emb-done-sub {
                font-size: 14px;
                color: #9ca3af;
                margin-top: 6px;
            }
        `;
        document.head.appendChild(style);
    }
}
