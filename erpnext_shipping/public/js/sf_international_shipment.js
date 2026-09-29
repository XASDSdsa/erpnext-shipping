// SF extends Shipment only for SF carriers; native defaults and manual shipping belong to ERPNext.
erpnext.shipment.register_carrier("sf_international", {
	matches(doc) {
		const value = String(doc.service_provider || doc.carrier || "").trim().toLowerCase().replace(/[_-]/g, " ");
		return ["sf international", "sf", "顺丰国际", "国际顺丰", "sf国际", "sf global", "sfglobal"].includes(value);
	},
	owns_api_ui: true,
	hidden_fields: ["shipment_id", "shipment_amount", "carrier", "carrier_service", "awb_number", "tracking_url"],
	lock_service_provider: (frm) => !!frm.doc.shipment_id,
	single_column_information: true,
});

frappe.ui.form.on("Shipment", {
	on_hide(frm) {
		frm.events.sf_cancel_form_lookups(frm);
	},

	refresh(frm) {
		frm.events.sf_prepare_section(frm);
		if (!frm.events.sf_is_sf(frm)) {
			if (frm._sf_form_active) frm.events.sf_clear_form(frm);
			return;
		}
		frm.events.sf_replace_tools(frm);
		frm.events.sf_load_form(frm);
		frm.events.sf_paint_status(frm);
		frm.events.sf_add_interception_buttons(frm);
		frm.events.sf_add_freight_accounting_buttons(frm);
		frm.events.sf_add_waybill_replacement_buttons(frm);
	},

	before_save(frm) {
		if (!frm.events.sf_is_sf(frm)) return;
		if (frm._sf_replacement_draft) {
			frm.events.sf_replacement_edit_guard(frm);
			frappe.validated = false;
			frappe.msgprint(__("Replacement edit is open. Use the replacement button to submit, or cancel the edit."));
			return;
		}
		frm.events.sf_apply_existing_waybill(frm);
	},

	service_provider(frm) {
		frm.events.sf_prepare_section(frm);
		if (frm.events.sf_is_sf(frm)) {
			frm.events.sf_replace_tools(frm);
			frm.events.sf_load_form(frm);
		} else if (frm._sf_form_active) {
			frm.events.sf_clear_form(frm);
		}
	},

	validate(frm) {
		if (!frm.events.sf_is_sf(frm)) return;
		if (frm._sf_replacement_draft) {
			frm.events.sf_replacement_edit_guard(frm);
			frappe.validated = false;
			return;
		}
		frm.events.sf_apply_existing_waybill(frm);
		const $host = frm.events.sf_host(frm);
		if (!$host.find(".sf-form").length) {
			return;
		}
		const form = frm.events.sf_read_form(frm);
		const booked_waybill = String(frm.doc.shipment_id || frm.doc.awb_number || "").trim();
		// Already linked/booked: never ask again for the hidden "existing waybill" box.
		if (booked_waybill) {
			frm._sf_existing_waybill = booked_waybill;
			form.existing_waybill = form.existing_waybill || booked_waybill;
		} else if (frm._sf_entry_mode === "existing") {
			const typed = String(form.existing_waybill || frm._sf_existing_waybill || "").trim();
			if (!typed) {
				frappe.validated = false;
				frappe.msgprint(__("Enter the existing SF waybill first."));
				return;
			}
		}
		if (!booked_waybill && frm._sf_entry_mode !== "existing" && !form.existing_waybill
			&& !frm.events.sf_validate_address_lengths(frm, form)) {
			frappe.validated = false;
			return;
		}
		if (frm.fields_dict.sf_form_json) {
			form.entry_mode = booked_waybill
				? "booked"
				: frm._sf_entry_mode || (form.existing_waybill ? "existing" : "create");
			frm.doc.sf_form_json = JSON.stringify(form);
		}
	},

	pickup_address_name(frm) {
		if (!frm.events.sf_is_sf(frm) || frm.doc.shipment_id) {
			return;
		}
		const $host = frm.events.sf_host(frm);
		if ($host && $host.find(".sf-form").length) {
			frm._sf_keep_form = frm.events.sf_read_form(frm);
		}
		frm.events.sf_load_form(frm);
	},

	shipment_delivery_note_add(frm) {
		frm.events.sf_prefill_if_empty(frm);
	},

	delivery_address_name(frm) {
		if (!frm.events.sf_is_sf(frm) || frm.doc.shipment_id) {
			return;
		}
		const $host = frm.events.sf_host(frm);
		if ($host && $host.find(".sf-form").length) {
			frm._sf_keep_form = frm.events.sf_read_form(frm);
			if (frm._sf_keep_form) {
				delete frm._sf_keep_form.receiver;
			}
		}
		frm.events.sf_load_form(frm);
	},

	sf_apply_existing_waybill(frm) {
		if (!frm.events.sf_is_sf(frm) || (frm.doc.shipment_id && !frm.is_new())) {
			return;
		}
		const $host = frm.events.sf_host(frm);
		const typed = (
			($host && $host.find("[data-sf-existing-waybill]").val()) ||
			frm._sf_existing_waybill ||
			""
		)
			.toString()
			.trim();
		if (!typed) {
			return;
		}
		frm._sf_existing_waybill = typed;
		frm.doc.shipment_id = typed;
		frm.doc.awb_number = typed;
	},

	sf_is_sf(frm) {
		return erpnext.shipment.carriers.sf_international.matches(frm.doc);
	},

	sf_prepare_section(frm) {
		// SF owns only its extension fields. Native layout uses the registered capabilities.
		const is_sf = frm.events.sf_is_sf(frm);
		if (frm.fields_dict.sf_interception_section) {
			frm.set_df_property("sf_interception_section", "hidden", is_sf && !frm.is_new() ? 0 : 1);
		}
		for (const fieldname of [
			"sf_freight_status", "sf_freight_accounting_status", "sf_freight_query_status",
			"sf_freight_accounting_hold", "sf_iuop_order_id", "sf_active_waybill_record",
			"sf_waybill_replacement_status", "sf_waybill_replacement_note", "sf_waybill_pending_record", "sf_form_json",
		]) {
			if (frm.fields_dict[fieldname]) frm.set_df_property(fieldname, "hidden", 1);
		}
		for (const fieldname of ["sf_actions_section", "sf_actions_html"]) {
			if (frm.fields_dict[fieldname]) frm.set_df_property(fieldname, "hidden", is_sf ? 0 : 1);
		}
	},

	sf_clear_form(frm) {
		frm._sf_form_active = false;
		frm.events.sf_cancel_form_lookups(frm);
		// Invalidate a pending waybill request when the carrier form is replaced.
		// Otherwise a late response could paint an old Shipment into the new form.
		frm._sf_waybill_token = (frm._sf_waybill_token || 0) + 1;
		frm._sf_waybill_view_token = (frm._sf_waybill_view_token || 0) + 1;
		frm._sf_waybill_wait_token = 0;
		frm._sf_waybill_loading = false;
		frm.events.sf_replacement_edit_guard(frm, false);
		const $host = frm.layout.wrapper.find("#sf-form-host");
		if ($host.length) {
			$host.empty();
		}
		if (frm.fields_dict.sf_actions_html && frm.fields_dict.sf_actions_html.$wrapper) {
			frm.fields_dict.sf_actions_html.$wrapper.find(".sf-form").parent().empty();
			frm.fields_dict.sf_actions_html.$wrapper.empty();
		}
	},

	sf_replacement_edit_guard(frm, editing = !!frm._sf_replacement_draft) {
		if (editing) {
			if (!frm._sf_replacement_save_disabled) {
				frm._sf_replacement_save_disabled = true;
				if (typeof frm.disable_save === "function") frm.disable_save();
			}
			return;
		}
		if (frm._sf_replacement_save_disabled) {
			frm._sf_replacement_save_disabled = false;
			if (typeof frm.enable_save === "function") frm.enable_save();
		}
	},

	sf_is_cancelled(frm) {
		const status = frm.doc.status || "";
		return status === "Cancelled" || frm.doc.docstatus === 2;
	},

	sf_label_cancelled(frm) {
		return (frm.doc.status || "") === "已取消发货" || cint(frm.doc.sf_carrier_cancelled) === 1;
	},

	sf_cancel_pending(frm) {
		return (frm.doc.sf_intercept_status || "") === "顺丰取消待确认";
	},

	sf_requires_manual_interception(frm) {
		const statuses = [
			"已发货", "Completed", "In Progress", "已揽收", "运输中", "派送中", "已签收", "已退回", "已丢失",
			"Shipped", "Delivered", "Returned", "Lost",
		];
		if (statuses.includes(String(frm.doc.status || "").trim())) return true;
		const tracking = String(frm.doc.tracking_status || "").trim();
		if (["Shipped", "Delivered", "Returned", "Lost", "已发货", "已签收", "已退回", "已丢失"].includes(tracking)) return true;
		return ["In Progress", "运输中", "派送中", "已揽收"].includes(tracking) && !!String(frm.doc.tracking_status_info || "").trim();
	},

	sf_can_cancel_waybill(frm) {
		const doc = frm.doc;
		return !!(doc.name && !frm.is_new() && (doc.shipment_id || doc.awb_number)
			&& !frm.events.sf_is_cancelled(frm) && !frm.events.sf_label_cancelled(frm)
			&& !doc.sf_intercept_status && !frm.events.sf_requires_manual_interception(frm));
	},

	sf_paint_status(frm) {
		if (!frm.events.sf_is_sf(frm)) {
			return;
		}
		if (frm.events.sf_is_cancelled(frm)) {
			frm.page.set_indicator(__("已取消发货"), "red");
		} else if (frm.events.sf_cancel_pending(frm)) {
			frm.page.set_indicator(__("顺丰取消待确认"), "orange");
		} else if (frm.events.sf_label_cancelled(frm)) {
			frm.page.set_indicator(__("顺丰已取消，待取消本地运单"), "orange");
		} else if (cint(frm.doc.docstatus) === 1 && frm.doc.tracking_status) {
			const tracking = frm.events.sf_transport_state(frm.doc);
			frm.page.set_indicator(tracking.label, tracking.color);
		}
	},

	sf_freight_summary(doc) {
		const confirmed = doc.sf_freight_status === "账单已取得";
		const amount = Number(doc.shipment_amount);
		const currency = doc.sf_freight_currency || doc.currency || "";
		const parts = [confirmed ? "账单已取得" : "账单待核实"];
		if (Number.isFinite(amount) && (amount > 0 || confirmed)) {
			parts.push(`${confirmed ? "运费金额" : "历史金额（待核实）"}: ${amount}${currency ? ` ${currency}` : ""}`);
		}
		const accounting = cint(doc.sf_freight_accounting_hold) === 1 ? "记账待处理" : doc.sf_freight_accounting_status;
		if (accounting) parts.push(`记账: ${accounting}`);
		if (["本次未查到账单", "查询失败"].includes(doc.sf_freight_query_status)) {
			parts.push(`最近查询: ${doc.sf_freight_query_status}`);
		}
		return parts.join(" · ");
	},

	/*
	 * A Shipment is the business package.  SF Waybill rows are immutable
	 * carrier-label records below it.  The parent waybill fields are only a
	 * compatibility projection, so the UI always prefers the child history
	 * when it is present and keeps the old number visible after a replacement.
	 */
	sf_waybill_is_shipped(frm) {
		return frm.events.sf_requires_manual_interception(frm);
	},

	sf_waybill_record_from_parent(frm) {
		const waybill = String(frm.doc.shipment_id || frm.doc.awb_number || "").trim();
		if (!waybill) {
			return null;
		}
		return {
			name: frm.doc.sf_active_waybill_record || "",
			waybill,
			replacement_status: frm.doc.sf_waybill_replacement_status || "当前",
			replacement_reason: frm.doc.sf_waybill_replacement_note || "",
			is_active: 1,
			carrier_cancelled: cint(frm.doc.sf_carrier_cancelled) === 1,
			carrier_cancelled_waybill: String(frm.doc.sf_carrier_cancelled_waybill || "").trim(),
			tracking_status: frm.doc.tracking_status || "",
			tracking_status_info: frm.doc.tracking_status_info || "",
			tracking_url: frm.doc.tracking_url || "",
			freight_amount: frm.doc.shipment_amount,
			freight_currency: frm.doc.sf_freight_currency || frm.doc.currency || "",
			freight_status: frm.doc.sf_freight_status || "",
			freight_accounting_status: frm.doc.sf_freight_accounting_status || "",
			freight_accounting_hold: frm.doc.sf_freight_accounting_hold,
			label_url: frm.doc.sf_label_url || "",
			creation: frm.doc.modified || "",
		};
	},

		sf_waybill_normalize_record(frm, row) {
			if (!row) {
				return null;
			}
			if (typeof row === "string") {
				row = { waybill: row };
			}
			const name = String(row.name || row.record || "").trim();
			const raw_status = String(row.replacement_status || row.status || "").trim();
			const waybill = String(row.waybill || row.shipment_id || row.awb_number || "").trim();
			// A carrier timeout can leave a durable failed/creating attempt before a
			// tracking number exists. Keep that row visible so the operator can
			// reconcile it instead of seeing a retry button that the server blocks.
			if (!waybill && !(name && ["创建中", "失败"].includes(raw_status))) {
				return null;
			}
		const parent_waybill = String(frm.doc.shipment_id || frm.doc.awb_number || "").trim();
		const active_name = String(frm.doc.sf_active_waybill_record || "").trim();
		const historical_status = ["已替换", "已取消", "失败"];
		const status = raw_status ||
				(row.is_active || row.name === active_name || (waybill === parent_waybill && !historical_status.includes(raw_status)) ? "当前" : "已替换");
			return {
				...row,
				name,
			waybill,
				replacement_status: status,
				is_active: !historical_status.includes(status) && (cint(row.is_active) === 1 || row.is_active === true || name === active_name || waybill === parent_waybill),
			carrier_cancelled: cint(row.carrier_cancelled) === 1 || row.carrier_cancelled === true,
			carrier_cancelled_waybill: String(row.carrier_cancelled_waybill || "").trim(),
			tracking_status: String(row.tracking_status || "").trim(),
			tracking_status_info: String(row.tracking_status_info || "").trim(),
			tracking_url: String(row.tracking_url || "").trim(),
			freight_amount: row.freight_amount,
			freight_currency: row.freight_currency || "",
			freight_status: row.freight_status || "",
			freight_accounting_status: row.freight_accounting_status || "",
			freight_accounting_hold: row.freight_accounting_hold,
			creation_uncertain: cint(row.creation_uncertain) === 1 || row.creation_uncertain === true,
			replacement_feedback: String(row.replacement_feedback || "").trim(),
			replacement_reason: String(row.replacement_reason || "").trim(),
			replaces_waybill: String(row.replaces_waybill || "").trim(),
			label_url: row.label_url || "",
			creation: row.creation || row.modified || "",
		};
	},

	sf_waybill_records(frm, supplied) {
		const onload = frm.doc.__onload || {};
		const source = supplied !== undefined
			? supplied
			: onload.sf_waybills !== undefined
				? onload.sf_waybills
				: onload.sf_waybill_records !== undefined
					? onload.sf_waybill_records
					: frm._sf_data && frm._sf_data.waybills !== undefined
						? frm._sf_data.waybills
						: frm._sf_waybill_records;
		const rows = Array.isArray(source) ? source : [];
		const normalized = rows.map((row) => frm.events.sf_waybill_normalize_record(frm, row)).filter(Boolean);
		const parent = frm.events.sf_waybill_record_from_parent(frm);
		if (parent && !normalized.some((row) => row.waybill === parent.waybill || (parent.name && row.name === parent.name))) {
			normalized.push(parent);
		}
		if (!normalized.length && parent) {
			return [parent];
		}
		const seen = new Set();
		return normalized.filter((row) => {
			const key = row.name || row.waybill;
			if (seen.has(key)) return false;
			seen.add(key);
			return true;
		});
	},

	sf_waybill_current_record(frm, rows) {
		const active_name = String(frm.doc.sf_active_waybill_record || "").trim();
		const parent_waybill = String(frm.doc.shipment_id || frm.doc.awb_number || "").trim();
		return (rows || []).find((row) => active_name && row.name === active_name &&
			(row.is_active || ["当前", "ACTIVE"].includes(row.replacement_status))) ||
			(rows || []).find((row) => row.is_active && ["当前", "ACTIVE"].includes(row.replacement_status)) ||
			(rows || []).find((row) => row.waybill === parent_waybill && !["已替换", "已取消", "失败"].includes(row.replacement_status)) || null;
	},

	sf_waybill_pending_record(frm, rows) {
		const pending_name = String(frm.doc.sf_waybill_pending_record || "").trim();
		const pending_statuses = ["待替换", "待启用", "创建中", "取消待确认", "待取消确认"];
		return (rows || []).find((row) => pending_name && row.name === pending_name &&
			pending_statuses.includes(row.replacement_status)) ||
			(rows || []).find((row) => pending_statuses.includes(row.replacement_status)) || null;
	},

	sf_current_label_url(frm) {
		const current = frm.events.sf_waybill_current_record(frm, frm.events.sf_waybill_records(frm));
		return current?.label_url || frm.doc.sf_label_url || "";
	},

	sf_waybill_status_state(value) {
		const status = String(value || "").trim();
			const labels = {
				当前: "当前", 创建中: "创建中", 待替换: "待替换", 待启用: "待启用",
				已替换: "已替换", 已取消: "已取消", 取消待确认: "取消待确认", 待取消确认: "取消待确认", 失败: "失败",
			};
			const colors = { 当前: "green", 待替换: "orange", 待启用: "blue", 已替换: "gray", 已取消: "gray", 失败: "red", 创建中: "orange", 取消待确认: "orange", 待取消确认: "orange" };
		return { label: __(labels[status] || status || "—"), color: colors[status] || "gray" };
	},

	sf_waybill_tracking_state(value) {
		const status = String(value || "").trim();
		const labels = {
			"In Progress": "运输中", Shipped: "已发货", Booked: "已下单", Delivered: "已送达",
			Returned: "已退回", Lost: "已丢失", "已发货": "已发货", "运输中": "运输中",
			"派送中": "派送中", "已揽收": "已揽收", "已签收": "已签收", "已退回": "已退回", "已丢失": "已丢失",
		};
		const colors = {
			"In Progress": "blue", Shipped: "blue", Booked: "blue", Delivered: "green", "已签收": "green",
			Returned: "orange", Lost: "red", "已退回": "orange", "已丢失": "red", "运输中": "blue", "派送中": "blue", "已揽收": "blue",
		};
		return { label: __(labels[status] || status), color: colors[status] || "gray" };
	},

	sf_waybill_freight_text(row) {
		const amount = Number(row.freight_amount);
		const currency = String(row.freight_currency || "").trim();
		if (!Number.isFinite(amount) || (amount === 0 && !row.freight_status)) return "";
		const amount_text = `${amount.toFixed(2)}${currency ? ` ${currency}` : ""}`;
		return `${row.freight_status === "账单已取得" ? "运费" : "历史运费（待核实）"} ${amount_text}`;
	},

	sf_waybill_old_cancel_confirmed(frm, rows) {
		const parent_waybill = String(frm.doc.shipment_id || frm.doc.awb_number || "").trim();
		if (cint(frm.doc.sf_carrier_cancelled) === 1 &&
			parent_waybill && String(frm.doc.sf_carrier_cancelled_waybill || "").trim() === parent_waybill &&
			frm.doc.sf_carrier_cancelled_at && frm.doc.sf_carrier_cancel_payload) {
			return true;
		}
		const current = frm.events.sf_waybill_current_record(frm, rows);
		return !!(current && current.waybill === parent_waybill && current.carrier_cancelled &&
			(current.carrier_cancelled_waybill || current.waybill) === parent_waybill && current.carrier_cancelled_at);
	},

	sf_waybill_reason_ok(reason) {
		return "".concat(String(reason || "").split(/\s+/).join("")).length >= 4;
	},

	sf_tracking_timeline_html(events) {
		const escape = frappe.utils.escape_html;
		return `<ol class="sf-tracking-timeline" style="max-height:55vh;overflow:auto;padding-left:24px;margin-bottom:0">${events.map((event) =>
			`<li style="padding:8px 0;overflow-wrap:anywhere"><div class="text-muted">${escape(event.time || "")}</div><div>${escape(event.description || "")}</div></li>`
		).join("")}</ol>`;
	},

	sf_saved_tracking_history_html(frm, row) {
		if (!row.waybill) return "";
		const escape = frappe.utils.escape_html;
		const events = Array.isArray(row.tracking_events) ? row.tracking_events : [];
		const queried = row.tracking_queried_at
			? `<div class="text-muted">${escape(__("Last tracking query: {0}", [row.tracking_queried_at]))}</div>` : "";
		return `<details class="sf-tracking-history" data-sf-tracking-waybill="${escape(row.waybill)}">
			<summary>${escape(__("Saved tracking history ({0})", [events.length]))}</summary>
			${queried}${events.length ? frm.events.sf_tracking_timeline_html(events) : `<p class="text-muted">${escape(__("No saved tracking events yet. Use Query Tracking to retrieve them."))}</p>`}
		</details>`;
	},

	sf_waybill_replacement_html(frm, rows, loading, load_error) {
		rows = rows || [];
		const current = frm.events.sf_waybill_current_record(frm, rows);
		const pending = frm.events.sf_waybill_pending_record(frm, rows);
		const uncertain = (rows || []).find((row) => row.creation_uncertain && row.replacement_status === "失败");
		const shipped = frm.events.sf_waybill_is_shipped(frm);
		const local_cancelled = frm.events.sf_is_cancelled(frm);
		const waybill_actions = frm.doc.__onload?.sf_waybill_actions || {};
		const can_query = waybill_actions.can_query !== false;
		const can_write = waybill_actions.can_write !== false && !local_cancelled;
		const can_operate = !!(frm.doc.name && !frm.is_new() && cint(frm.doc.docstatus) === 1 && current && can_write);
		const current_waybill = current && current.waybill;
		const sorted = rows.slice().sort((a, b) => {
			if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
			return String(a.creation || a.name || a.waybill).localeCompare(String(b.creation || b.name || b.waybill));
		});
			const history = sorted.length
				? `<div class="sf-waybill-history">${sorted.map((row) => {
					const state = frm.events.sf_waybill_status_state(row.replacement_status);
					const tracking = frm.events.sf_waybill_tracking_state(row.tracking_status);
					const freight = frm.events.sf_waybill_freight_text(row);
					const waybill_label = row.waybill || __("No replacement waybill was returned.");
				const details = [
					row.replaces_waybill ? `替换 ${row.replaces_waybill}` : "",
					tracking.label ? `${__("Transport Status")}: ${tracking.label}` : "",
						row.tracking_status_info,
						freight,
						row.carrier_cancelled ? "顺丰已确认取消" : "",
						row.creation_uncertain ? "下单结果待核实" : "",
						row.replacement_feedback,
				].filter(Boolean).join(" · ");
						const label = row.is_active ? "" : row.label_url
							? ` <a href="${frappe.utils.escape_html(frappe.urllib.get_full_url(row.label_url))}" target="_blank" rel="noopener">${__("Open Shipping Label")}</a>`
							: can_write && row.name && ["当前", "待替换", "待启用"].includes(row.replacement_status)
							? ` <button type="button" class="btn btn-default btn-xs" data-sf-waybill-action="print" data-sf-waybill-record="${frappe.utils.escape_html(row.name)}">${__("Print Shipping Label")}</button>`
							: "";
					const track = !row.is_active && can_query && row.name && row.waybill
						? ` <button type="button" class="btn btn-default btn-xs" data-sf-waybill-action="track" data-sf-waybill-record="${frappe.utils.escape_html(row.name)}" title="${frappe.utils.escape_html(__("Query historical waybill tracking"))}">${__("Query Tracking")}</button>`
						: "";
					const query_freight = can_query && row.name && row.waybill && !row.is_active
					? ` <button type="button" class="btn btn-default btn-xs" data-sf-waybill-action="freight" data-sf-waybill-record="${frappe.utils.escape_html(row.name)}" title="${frappe.utils.escape_html(__("Query historical waybill freight"))}">${__("Query Freight")}</button>`
					: "";
					return `<div class="sf-waybill-history-row ${row.is_active ? "sf-waybill-active" : ""}"><div class="sf-waybill-history-main"><strong>${frappe.utils.escape_html(waybill_label)}</strong>${row.is_active ? `<span class="indicator-pill green no-indicator-dot">${__("Current Waybill")}</span>` : ""}<span class="indicator-pill ${state.color} no-indicator-dot">${frappe.utils.escape_html(state.label)}</span>${label}${track}${query_freight}</div>${details ? `<div class="sf-waybill-history-details">${frappe.utils.escape_html(details)}</div>` : ""}${frm.events.sf_saved_tracking_history_html(frm, row)}</div>`;
			}).join("")}</div>`
			: `<p class="text-muted sf-waybill-empty">${__("No waybill history is available yet.")}</p>`;
		let action = "";
		if (loading) {
			action = `<p class="text-muted">${__("Loading waybill history")}</p>`;
		} else if (load_error) {
			action = `<p class="text-muted">${__("The waybill history could not be loaded. The current waybill remains unchanged.")}</p>`;
		} else if (can_operate && frm._sf_replacement_draft) {
			action = `<p class="text-muted sf-waybill-hint">${__("Edit the corrected recipient, product, and customs data before creating the replacement label.")}</p><button type="button" class="btn btn-default btn-sm" data-sf-waybill-action="cancel-edit">${__("Cancel replacement edit")}</button>`;
			} else if (uncertain && can_write) {
				action = `<p class="text-muted sf-waybill-hint">${__("The previous replacement request has an uncertain result. Check SF before retrying.")}</p><button type="button" class="btn btn-default btn-sm" data-sf-waybill-action="resolve-failed" data-sf-waybill-record="${frappe.utils.escape_html(uncertain.name)}">${__("Mark as checked before retry")}</button>`;
			} else if (pending && pending.replacement_status === "创建中") {
				action = `<p class="text-muted sf-waybill-hint">${__("Replacement creation is being processed. Do not submit another request.")}</p>`;
			} else if (can_operate && pending && ["取消待确认", "待取消确认"].includes(pending.replacement_status)) {
				action = `<p class="text-muted sf-waybill-hint">${__("The rejected replacement already has an SF order or waybill. Check SF cancellation before retrying; do not create another order.")}</p><button type="button" class="btn btn-default btn-sm" data-sf-waybill-action="verify-replacement-cancel" data-sf-waybill-record="${frappe.utils.escape_html(pending.name)}">${__("Verify SF cancellation")}</button>`;
			} else if (can_operate && pending && pending.replacement_status === "待替换") {
			action = `<p class="text-muted sf-waybill-hint">${__("A replacement waybill is waiting for external SF customer service feedback.")}</p><button type="button" class="btn btn-default btn-sm" data-sf-waybill-action="feedback" data-sf-waybill-record="${frappe.utils.escape_html(pending.name)}">${__("Record SF customer service feedback")}</button>`;
		} else if (can_operate && pending && pending.replacement_status === "待启用") {
			action = `<p class="text-muted sf-waybill-hint">${__("SF customer service confirmed the replacement. You can enable the new waybill.")}</p><button type="button" class="btn btn-primary btn-sm" data-sf-waybill-action="activate" data-sf-waybill-record="${frappe.utils.escape_html(pending.name)}">${__("Activate replacement waybill")}</button>`;
		} else if (can_operate && shipped) {
			action = `<button type="button" class="btn btn-primary btn-sm" data-sf-waybill-action="create-shipped">${__("Create new waybill as replacement")}</button><p class="text-muted sf-waybill-hint">${__("After shipping, create the new label, contact SF customer service on your external platform, and enable it only after they confirm the replacement.")}</p>`;
		} else if (can_operate && !shipped && frm.events.sf_waybill_old_cancel_confirmed(frm, rows)) {
			action = `<button type="button" class="btn btn-default btn-sm" data-sf-waybill-action="create-unshipped">${__("Unshipped: recreate waybill")}</button><p class="text-muted sf-waybill-hint">${__("Before shipping, first obtain exact SF cancellation confirmation for the old label.")}</p>`;
		} else if (can_operate && !shipped) {
			action = `<button type="button" class="btn btn-default btn-sm" disabled title="${frappe.utils.escape_html(__("Before shipping, first obtain exact SF cancellation confirmation for the old label."))}">${__("Create replacement waybill after SF cancellation")}</button><p class="text-muted sf-waybill-hint">${__("Before shipping, first obtain exact SF cancellation confirmation for the old label.")}</p>`;
		}
		return `<div class="sf-waybill-replacement-panel"><div class="sf-waybill-title">${__("Waybill Replacement")}</div><p class="text-muted sf-waybill-hint">${__("The old waybill and its freight history are kept.")} ${__("The replacement is not a cancellation.")}</p>${current_waybill ? `<div class="sf-waybill-current">${__("Current Waybill")}: <strong>${frappe.utils.escape_html(current_waybill)}</strong></div>` : ""}${history}<div class="sf-waybill-actions">${action}</div></div>`;
	},

	sf_bind_waybill_actions(frm, $host) {
		$host.find("[data-sf-waybill-action]").on("click", function () {
			const action = this.getAttribute("data-sf-waybill-action");
			const record = this.getAttribute("data-sf-waybill-record") || "";
			if (action === "create-unshipped") return frm.events.sf_create_waybill_replacement(frm, 0);
			if (action === "create-shipped") return frm.events.sf_create_waybill_replacement(frm, 1);
				if (action === "resolve-failed") return frm.events.sf_resolve_failed_waybill(frm, record);
				if (action === "verify-replacement-cancel") return frm.events.sf_verify_replacement_cancellation(frm, record);
			if (action === "feedback") return frm.events.sf_record_waybill_replacement_feedback(frm, record);
			if (action === "activate") return frm.events.sf_activate_waybill_replacement(frm, record);
			if (action === "print") return frm.events.sf_print_waybill_record(frm, record);
			if (action === "track") return frm.events.sf_track_waybill_record(frm, record);
			if (action === "freight") return frm.events.sf_freight_waybill_record(frm, record);
			if (action === "cancel-edit") return frm.events.sf_cancel_waybill_replacement_edit(frm);
		});
	},

	sf_fetch_waybill_records(frm, callback) {
		callback = typeof callback === "function" ? callback : function () {};
		const onload = frm.doc.__onload || {};
		const supplied = onload.sf_waybills !== undefined
			? onload.sf_waybills
			: onload.sf_waybill_records !== undefined
				? onload.sf_waybill_records
					: frm._sf_data && frm._sf_data.waybills !== undefined ? frm._sf_data.waybills : undefined;
		const initial_rows = Array.isArray(supplied) ? supplied : [];
		const shipment_name = String(frm.doc.name || "").trim();
		const parent_waybill = String(frm.doc.shipment_id || frm.doc.awb_number || "").trim();
		const can_fetch = !!(shipment_name && parent_waybill && !frm.is_new() && frappe.call);
		const token = (frm._sf_waybill_token = (frm._sf_waybill_token || 0) + 1);
		let settled = false;
		const cached = () => (frm._sf_waybill_records && Array.isArray(frm._sf_waybill_records)
			? frm._sf_waybill_records : initial_rows);
		const finish = (rows, failed) => {
			if (settled || frm._sf_waybill_token !== token) return;
			settled = true;
			const next_rows = Array.isArray(rows) ? rows : [];
			frm._sf_waybill_records = next_rows;
			callback(frm.events.sf_waybill_records(frm, next_rows), !!failed);
		};
		// Keep data supplied by the form onload as a cache, then refresh from the
		// permission-aware server method.  The caller has already painted the cache
		// while showing its loading state, so do not invoke its completion callback
		// twice for the same request.
		if (supplied !== undefined) {
			frm._sf_waybill_records = initial_rows;
		}
		if (!can_fetch) {
			if (supplied === undefined) {
				frm._sf_waybill_records = [];
				callback(frm.events.sf_waybill_records(frm, []), false);
			}
			return;
		}
		const parse_response = (response) => {
			const message = response && response.message;
			if (response && response.exc) return null;
			if (Array.isArray(message)) return message;
			if (message && Array.isArray(message.waybills)) return message.waybills;
			return message && message.waybills === undefined && typeof message === "object" ? [] : null;
		};
		const options = {
			method: "erpnext_shipping.sf_international.waybill.list_waybill_records",
			args: { shipment: shipment_name },
			callback(r) {
				const rows = parse_response(r);
				finish(rows === null ? cached() : rows, rows === null);
			},
		};
		try {
			const request = frappe.call(options);
			if (request && typeof request.then === "function") {
				request.then((r) => {
					const rows = parse_response(r);
					finish(rows === null ? cached() : rows, rows === null);
				}).catch(() => finish(cached(), true));
			}
		} catch (error) {
			finish(cached(), true);
		}
	},

	sf_render_waybill_replacement(frm, rows, loading, load_error) {
		const $host = frm.events.sf_host(frm);
		if (!$host || !$host.length || !$host.find(".sf-form").length || !(frm.doc.shipment_id || frm.doc.awb_number)) return;
		const $form = $host.find(".sf-form").first();
		const html = frm.events.sf_waybill_replacement_html(frm, rows || frm.events.sf_waybill_records(frm), loading, load_error);
		const $slot = $form.find(".sf-waybill-replacement-slot");
		if ($slot.length) {
			$slot.html(html);
		} else {
			$form.find(".sf-waybill-replacement-panel").remove();
			$form.append(html);
		}
		frm.events.sf_bind_waybill_actions(frm, $form);
	},

	sf_load_waybill_replacement(frm) {
		if (!frm.events.sf_is_sf(frm) || !frm.doc.name || !(frm.doc.shipment_id || frm.doc.awb_number) || frm.is_new()) {
			return;
		}
		if (frm._sf_waybill_loading) return;
		const $host = frm.events.sf_host(frm);
		if (!$host || !$host.length || !$host.find(".sf-form").length) return;
		const view_token = (frm._sf_waybill_view_token || 0) + 1;
		frm._sf_waybill_view_token = view_token;
		frm._sf_waybill_loading = true;
		frm.events.sf_render_waybill_replacement(frm, frm.events.sf_waybill_records(frm), true, false);
		frm.events.sf_fetch_waybill_records(frm, (rows, failed) => {
			frm._sf_waybill_loading = false;
			if (frm._sf_waybill_view_token === view_token) {
				frm.events.sf_render_waybill_replacement(frm, rows, false, failed);
			}
		});
	},

	sf_add_waybill_replacement_buttons(frm) {
		if (!frm.events.sf_is_sf(frm) || !frm.doc.name || !(frm.doc.shipment_id || frm.doc.awb_number) || frm.is_new()) return;
		// The actual buttons live inside the SF panel so they remain next to the
		// current and historical numbers. sf_render_form invokes the loader after
		// its asynchronous booking form is mounted. Avoid polling here: polling
		// made the history appear seconds after native controls and could start
		// duplicate requests.
		const $host = frm.events.sf_host(frm);
		if ($host && $host.length && $host.find(".sf-form").length) {
			frm.events.sf_load_waybill_replacement(frm);
		}
	},

	sf_create_waybill_replacement(frm, shipped) {
		const is_shipped = !!cint(shipped);
		if (!frm.doc.name || !(frm.doc.shipment_id || frm.doc.awb_number) || frm.events.sf_is_cancelled(frm) || frm._sf_replacement_draft) return;
		const rows = frm.events.sf_waybill_records(frm);
		if (!is_shipped && !frm.events.sf_waybill_old_cancel_confirmed(frm, rows)) {
			frappe.msgprint({ message: __("Before shipping, first obtain exact SF cancellation confirmation for the old label."), indicator: "orange" });
			return;
		}
		const open_form = (reason) => {
			const form = frm.events.sf_saved_form(frm);
			if (!Object.keys(form).length) {
				frappe.msgprint({ message: __("The replacement form could not be loaded. Refresh the Shipment and try again."), indicator: "red" });
				return;
			}
			delete form.existing_waybill;
			delete form.entry_mode;
			frm._sf_replacement_draft = {
				shipped: is_shipped,
				reason,
				form: JSON.parse(JSON.stringify(form)),
			};
			frm.events.sf_replacement_edit_guard(frm, true);
			frm.events.sf_render_form(frm, frm._sf_data || {});
			const $host = frm.events.sf_host(frm);
			$host?.find(".sf-replacement-editor")[0]?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
		};
		if (is_shipped) {
			open_form("");
			return;
		}
		frappe.prompt([
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Replacement reason"), reqd: 1 },
		], (values) => {
			const reason = String(values.reason || "").trim();
			if (!frm.events.sf_waybill_reason_ok(reason)) {
				frappe.msgprint(__("Please enter a specific replacement reason (at least four characters)."));
				return;
			}
			const message = __("This creates a new SF label only after the old unshipped label has an exact cancellation confirmation. The old number and freight records remain.");
			frappe.confirm(`${message}<br><br>${__("Replacement reason")}: ${frappe.utils.escape_html(reason)}<br><br>${__("Edit the corrected recipient, product, and customs data before creating the replacement label.")}`, () => {
				open_form(reason);
			});
		}, __("Unshipped: recreate waybill"));
	},

	sf_submit_waybill_replacement(frm) {
		const draft = frm._sf_replacement_draft;
		if (!draft || !frm.doc.name || frm._sf_replacement_submitting) return;
		let form;
		try {
			form = frm.events.sf_read_form(frm);
		} catch (error) {
			frappe.msgprint({ message: __("The replacement form could not be read. Refresh the Shipment and try again."), indicator: "red" });
			return;
		}
		if (!form || !Object.keys(form).length) {
			frappe.msgprint({ message: __("The replacement form could not be read. Refresh the Shipment and try again."), indicator: "red" });
			return;
		}
		if (!frm.events.sf_validate_address_lengths(frm, form)) return;
		const reason = String(draft.reason || "").trim();
		if (!frm.events.sf_waybill_reason_ok(reason)) {
			frappe.msgprint({ message: __("Please enter a specific replacement reason (at least four characters)."), indicator: "red" });
			return;
		}
		frm._sf_replacement_submitting = true;
		frappe.call({
			method: "erpnext_shipping.sf_international.waybill.create_replacement",
			type: "POST",
			args: {
				shipment: frm.doc.name,
				form_json: JSON.stringify(form),
				reason,
				shipped: draft.shipped ? 1 : 0,
			},
			freeze: true,
			freeze_message: draft.shipped ? __("Create new waybill as replacement") : __("Create replacement waybill"),
				callback(r) {
					frm._sf_replacement_submitting = false;
					if (r && r.exc) {
						frm._sf_replacement_draft = null;
						frm.events.sf_replacement_edit_guard(frm, false);
						frm.reload_doc();
						return;
					}
					const result = r && r.message || {};
					if (result.ok === false) {
						frm._sf_replacement_draft = null;
						frm.events.sf_replacement_edit_guard(frm, false);
					frappe.msgprint({
						title: __("Replacement waybill creation failed"),
						message: `${__("Replacement waybill creation failed. The failed attempt was kept.")}<br>${frappe.utils.escape_html(result.error || result.message || "")}`,
						indicator: "red",
					});
					frm.reload_doc();
					return;
					}
					frm._sf_replacement_draft = null;
					frm.events.sf_replacement_edit_guard(frm, false);
				frappe.msgprint({
					message: draft.shipped ? __("Replacement created. Contact SF customer service outside ERP before enabling it.") : __("Replacement waybill is now active. The old waybill remains in history."),
					indicator: "green",
				});
				frm.reload_doc();
			},
				error() {
					frm._sf_replacement_submitting = false;
					frm._sf_replacement_draft = null;
					frm.events.sf_replacement_edit_guard(frm, false);
				frappe.msgprint({
					title: __("Replacement waybill creation failed"),
					message: __("Replacement request result is unknown. Check SF before retrying."),
					indicator: "orange",
				});
				frm.reload_doc();
			},
		});
	},

	sf_cancel_waybill_replacement_edit(frm) {
		if (!frm._sf_replacement_draft) return;
		frm._sf_replacement_draft = null;
		frm._sf_replacement_submitting = false;
		frm.events.sf_replacement_edit_guard(frm, false);
		frm.events.sf_render_form(frm, frm._sf_data || {});
	},

	sf_resolve_failed_waybill(frm, record) {
		if (!record || !frm.doc.name) return;
		frappe.prompt([
			{ fieldname: "note", fieldtype: "Small Text", label: __("Feedback note"), reqd: 1,
				description: __("Record SF's actual replacement result for the old and new waybill numbers. An accepted request is still processing.") },
		], (values) => {
			const note = String(values.note || "").trim();
			if (!frm.events.sf_waybill_reason_ok(note)) {
				frappe.msgprint(__("Please record that you checked SF and confirmed no order was created."));
				return;
			}
			frappe.call({
				method: "erpnext_shipping.sf_international.waybill.resolve_failed_replacement",
				type: "POST",
				args: { shipment: frm.doc.name, waybill_record: record, note },
				freeze: true,
				freeze_message: __("Mark as checked before retry"),
				callback(r) {
					if (r && r.exc) return;
					frappe.msgprint({ message: r.message?.message || __("Mark as checked before retry"), indicator: "orange" });
					frm.reload_doc();
				},
			});
		}, __("Mark as checked before retry"));
	},

	sf_verify_replacement_cancellation(frm, record) {
		if (!record || !frm.doc.name || frm._sf_replacement_cancel_checking === record) return;
		frm._sf_replacement_cancel_checking = record;
		frappe.call({
			method: "erpnext_shipping.sf_international.waybill.verify_replacement_cancellation",
			type: "POST",
			args: { shipment: frm.doc.name, waybill_record: record },
			freeze: true,
			freeze_message: __("Verify SF cancellation"),
			callback(r) {
				frm._sf_replacement_cancel_checking = null;
				if (r && r.exc) return;
				const result = r && r.message || {};
				frappe.msgprint({
					message: result.carrier_cancelled ? __("SF cancellation was confirmed. You may now retry the replacement if needed.") : __("SF has not explicitly confirmed cancellation. The Shipment remains unchanged."),
					indicator: result.carrier_cancelled ? "green" : "orange",
				});
				frm.reload_doc();
			},
			error() {
				frm._sf_replacement_cancel_checking = null;
			},
		});
	},

	sf_track_waybill_record(frm, record) {
		if (!record || !frm.doc.name || frm._sf_waybill_tracking_record === record) return;
		frm._sf_waybill_tracking_record = record;
		frappe.call({
			method: "erpnext_shipping.sf_international.waybill.fetch_waybill_tracking",
			type: "POST",
			args: { shipment: frm.doc.name, waybill_record: record },
			freeze: true,
			freeze_message: __("Query historical waybill tracking"),
			callback(r) {
				frm._sf_waybill_tracking_record = null;
				if (r && r.exc) return;
				const result = r && r.message || {};
				frm.events.sf_show_tracking_result(frm, result);
				frm.reload_doc();
			},
			error() {
				frm._sf_waybill_tracking_record = null;
			},
		});
	},

	sf_freight_waybill_record(frm, record) {
		if (!record || !frm.doc.name || frm._sf_waybill_freight_record === record) return;
		frm._sf_waybill_freight_record = record;
		frappe.call({
			method: "erpnext_shipping.sf_international.waybill.fetch_waybill_freight",
			type: "POST",
			args: { shipment: frm.doc.name, waybill_record: record },
			freeze: true,
			freeze_message: __("Query historical waybill freight"),
			callback(r) {
				frm._sf_waybill_freight_record = null;
				if (r && r.exc) return;
				const result = r && r.message || {};
				const amount = result.amount === null || result.amount === undefined ? "" : ` ${frappe.utils.escape_html(result.amount)}${result.currency ? ` ${frappe.utils.escape_html(result.currency)}` : ""}`;
				frappe.msgprint({
					message: `${frappe.utils.escape_html(result.message || __("Historical waybill freight was saved for finance review."))}${amount}`,
					indicator: result.ok === false ? "orange" : "green",
				});
				frm.reload_doc();
			},
			error() {
				frm._sf_waybill_freight_record = null;
			},
		});
	},

	sf_record_waybill_replacement_feedback(frm, record) {
		if (!record || !frm.doc.name) return;
		frappe.prompt([
			{ fieldname: "status", fieldtype: "Select", options: "顺丰客服处理中\n顺丰客服确认成功\n顺丰客服反馈失败", label: __("Feedback status"), reqd: 1 },
			{ fieldname: "note", fieldtype: "Small Text", label: __("Feedback note"), reqd: 1 },
		], (values) => {
			const note = String(values.note || "").trim();
			if (!note) {
				frappe.msgprint(__("Please enter the external SF customer service feedback."));
				return;
			}
			frappe.call({
				method: "erpnext_shipping.sf_international.waybill.record_replacement_feedback",
				type: "POST",
				args: { shipment: frm.doc.name, waybill_record: record, status: values.status, note },
				freeze: true,
				freeze_message: __("Record SF customer service feedback"),
				callback(r) {
					if (r && r.exc) return;
					frappe.msgprint({ message: __("Replacement feedback is recorded. It does not cancel the old waybill."), indicator: "orange" });
					frm.reload_doc();
				},
			});
		}, __("Record SF customer service feedback"));
	},

	sf_activate_waybill_replacement(frm, record) {
		if (!record || !frm.doc.name) return;
		frappe.prompt([
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Enable reason"), reqd: 1 },
		], (values) => {
			const reason = String(values.reason || "").trim();
			if (!frm.events.sf_waybill_reason_ok(reason)) {
				frappe.msgprint(__("Please enter an enable reason (at least four characters)."));
				return;
			}
			frappe.confirm(`${__("SF customer service confirmed the replacement. You can enable the new waybill.")}<br><br>${__("The replacement is not a cancellation.")}<br>${__("Enable reason")}: ${frappe.utils.escape_html(reason)}`, () => {
				frappe.call({
					method: "erpnext_shipping.sf_international.waybill.activate_replacement",
					type: "POST",
					args: { shipment: frm.doc.name, waybill_record: record, reason },
					freeze: true,
					freeze_message: __("Activate replacement waybill"),
					callback(r) {
						if (r && r.exc) return;
						frappe.msgprint({ message: __("Replacement waybill is now active. The old waybill remains in history."), indicator: "green" });
						frm.reload_doc();
					},
				});
			});
		}, __("Activate replacement waybill"));
	},

	sf_print_waybill_record(frm, record) {
		if (!record || !frm.doc.name) return;
		const row = frm.events.sf_waybill_records(frm).find((row) => row.name === record);
		if (row?.label_url) {
			window.open(frm.events.sf_label_href(row.label_url), "_blank", "noopener");
			return;
		}
		if (frm._sf_waybill_printing === record) return;
		frm._sf_waybill_printing = record;
		frappe.call({
			method: "erpnext_shipping.sf_international.waybill.print_replacement_label",
			type: "POST",
			args: { shipment: frm.doc.name, waybill_record: record },
			freeze: true,
			freeze_message: __("Print Shipping Label"),
			always() { frm._sf_waybill_printing = null; },
			callback(r) {
				if (r && r.exc) return;
				const url = r.message;
				if (url) {
					window.open(frappe.urllib.get_full_url(url), "_blank", "noopener");
					frm.reload_doc();
				}
			},
		});
	},

	sf_add_freight_accounting_buttons(frm) {
		const group = __("运费记账");
		frm.remove_custom_button(__("更正运费凭证"), group);
		frm.remove_custom_button(__("恢复运费记账"), group);
		const state = frm.doc.__onload?.sf_freight_accounting || {};
		if (state.can_cancel === true && state.shipment && state.journal) {
			frm.add_custom_button(__("更正运费凭证"), () => frm.events.sf_freight_accounting_action(frm, "cancel"), group);
		}
		if (state.can_resume === true && state.shipment) {
			frm.add_custom_button(__("恢复运费记账"), () => frm.events.sf_freight_accounting_action(frm, "resume"), group);
		}
	},

	sf_freight_accounting_action(frm, action) {
		const state = frm.doc.__onload?.sf_freight_accounting || {};
		const cancel = action === "cancel";
		if (!["cancel", "resume"].includes(action) || state[cancel ? "can_cancel" : "can_resume"] !== true || !state.shipment || (cancel && !state.journal)) return;
		frappe.prompt([
			{ fieldname: "reason", fieldtype: "Small Text", label: cancel ? __("运费更正原因") : __("恢复记账原因"), reqd: 1 },
		], (values) => {
			const reason = String(values.reason || "").trim();
			if (!reason) {
				frappe.msgprint(__("请填写原因。"));
				return;
			}
			const run = () => frappe.call({
				method: `erpnext_shipping.sf_international.freight_accounting.${cancel ? "cancel_freight_journal" : "resume_freight_accounting"}`,
				type: "POST",
				args: { shipment: state.shipment, reason },
				freeze: true,
				callback(r) { if (!r.exc) frm.reload_doc(); },
			});
			if (cancel) {
				frappe.confirm(__("确认取消运单 {0} 的运费凭证 {1}？运费账单和历史金额将保留，自动记账将暂停。", [frappe.utils.escape_html(state.shipment), frappe.utils.escape_html(state.journal)]), run);
			} else {
				frappe.confirm(__("确认恢复运单 {0} 的运费记账？系统将允许根据有效账单重新记账。", [frappe.utils.escape_html(state.shipment)]), run);
			}
		}, cancel ? __("更正运费凭证") : __("恢复运费记账"));
	},

	sf_interception_state(frm) {
		const state = (frm.doc.__onload && frm.doc.__onload.sf_interception) || frm.doc.sf_interception || {};
		return {
			can_request: !!state.can_request,
			can_record: !!state.can_record,
			can_verify: !!state.can_verify,
			carrier_cancelled: state.carrier_cancelled === true || cint(state.carrier_cancelled) === 1 || cint(frm.doc.sf_carrier_cancelled) === 1,
			blocked: state.blocked !== false,
			state: state.state || frm.doc.sf_intercept_status || "",
		};
	},

	sf_add_interception_buttons(frm) {
		if (!frm.events.sf_is_sf(frm) || !frm.doc.name || cint(frm.doc.docstatus) !== 1 || !(frm.doc.shipment_id || frm.doc.awb_number)) {
			return;
		}
		const state = frm.events.sf_interception_state(frm);
		frm.remove_custom_button(__("Request Interception"), __("Tools"));
		frm.remove_custom_button(__("Record SF Customer Service Feedback"), __("Tools"));
		frm.remove_custom_button(__("Verify SF Cancellation"), __("Tools"));
		if (state.can_request) {
			frm.add_custom_button(__("Request Interception"), () => frm.events.sf_request_interception(frm), __("Tools"));
		}
		if (state.can_record) {
			frm.add_custom_button(__("Record SF Customer Service Feedback"), () => frm.events.sf_record_interception_result(frm), __("Tools"));
		}
		if (state.can_verify) {
			frm.add_custom_button(__("Verify SF Cancellation"), () => frm.events.sf_verify_carrier_cancellation(frm), __("Tools"));
		}
	},

	sf_request_interception(frm) {
		if (!frm.events.sf_interception_state(frm).can_request) return;
		frappe.prompt([
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Interception Reason"), reqd: 1 },
		], (values) => {
			if (!String(values.reason || "").trim()) {
				frappe.msgprint(__("Please enter the interception reason."));
				return;
			}
			frappe.call({
				method: "erpnext_shipping.sf_international.interception.request_interception",
				args: { shipment: frm.doc.name, reason: values.reason },
				freeze: true,
				callback(r) {
					if (!r.exc) {
						frappe.msgprint({ message: __("拦截申请已记录，请自行联系顺丰客服。"), indicator: "orange" });
						frm.reload_doc();
					}
				},
			});
		}, __("Request Interception"));
	},

	sf_record_interception_result(frm) {
		if (!frm.events.sf_interception_state(frm).can_record) return;
		frappe.prompt([
			{ fieldname: "status", fieldtype: "Select", options: "顺丰客服处理中\n顺丰客服确认成功待顺丰确认\n顺丰客服反馈失败", label: __("Interception Status"), reqd: 1 },
			{ fieldname: "note", fieldtype: "Small Text", label: __("Interception Note"), reqd: 1 },
		], (values) => {
			if (!String(values.note || "").trim()) {
				frappe.msgprint(__("Please enter the customer service note."));
				return;
			}
			if (!["顺丰客服处理中", "顺丰客服确认成功待顺丰确认", "顺丰客服反馈失败"].includes(values.status)) return;
			frappe.call({
				method: "erpnext_shipping.sf_international.interception.record_interception_result",
				args: { shipment: frm.doc.name, status: values.status, note: values.note },
				freeze: true,
				callback(r) {
					if (!r.exc) {
						frappe.msgprint({ message: __("顺丰客服反馈已保存。顺丰客服反馈不会取消运单，请等待顺丰明确返回取消确认。"), indicator: "orange" });
						frm.reload_doc();
					}
				},
			});
		}, __("Record SF Customer Service Feedback"));
	},

	sf_verify_carrier_cancellation(frm) {
		if (!frm.events.sf_interception_state(frm).can_verify) return;
		return frappe.call({
				method: "erpnext_shipping.sf_international.interception.verify_carrier_cancellation",
				args: { shipment: frm.doc.name },
				freeze: true,
				callback(r) {
					if (r.exc) return;
					const data = r.message || {};
						frappe.msgprint({
							message: data.carrier_cancelled
								? __("Carrier cancellation confirmed. Cancel the local Shipment first; only then cancel the Delivery Note and Sales Order in order.")
								: __("顺丰尚未确认取消，单据和运费保持不变。"),
						indicator: data.carrier_cancelled ? "green" : "orange",
					});
					frm.reload_doc();
				},
		});
	},

	sf_transport_state(doc) {
		const status = String(doc.tracking_status || "").trim();
		const labels = {
			"In Progress": "运输中",
			Shipped: "已发货",
			Booked: "已下单",
			Delivered: "已送达",
			Returned: "已退回",
			Lost: "已丢失",
			"已发货": "已发货",
			"运输中": "运输中",
			"派送中": "派送中",
			"已揽收": "已揽收",
			"已签收": "已签收",
			"已退回": "已退回",
			"已丢失": "已丢失",
		};
		const colors = {
			"In Progress": "blue", Shipped: "blue", Booked: "blue", Delivered: "green", "已签收": "green",
			Returned: "orange", "已退回": "orange", Lost: "red", "已丢失": "red",
			"运输中": "blue", "派送中": "blue", "已揽收": "blue", "已发货": "blue",
		};
		return { label: __(labels[status] || status), color: colors[status] || "gray" };
	},

	sf_transport_html(frm) {
		const doc = frm.doc;
		const details = String(doc.tracking_status_info || "").trim();
		if (!doc.tracking_status && !details) {
			return "";
		}
		const label = frm.events.sf_is_cancelled(frm) || doc.status === "已取消发货"
			? __("Historical Transport Status") : __("Transport Status");
		const state = frm.events.sf_transport_state(doc);
		return `<div class="sf-transport-result">
			${state.label ? `<div>${frappe.utils.escape_html(label)}: <span class="indicator-pill ${state.color} no-indicator-dot">${frappe.utils.escape_html(state.label)}</span></div>` : ""}
			${details ? `<div class="sf-transport-details">${__("Transport Details")}: ${frappe.utils.escape_html(details)}</div>` : ""}
		</div>`;
	},

	sf_waiting_to_ship(frm) {
		const cancelled = frm.events.sf_is_cancelled(frm) || frm.events.sf_label_cancelled(frm);
		return !!(frm.doc.shipment_id && frm.doc.docstatus === 1 && !cancelled && !frm.events.sf_requires_manual_interception(frm));
	},

	sf_replace_tools(frm) {
		if (!frm.events.sf_is_sf(frm)) return;
		// ERPNext Shipping checks owns_api_ui before creating its controls.
		// Refresh this extension's own controls without changing another app's events.
		["Print Shipping Label", "Open Shipping Label", "Print Label and Ship"].forEach((label) => {
			frm.remove_custom_button(__(label), __("Tools"));
		});
		if (!frm.doc.shipment_id || frm.events.sf_is_cancelled(frm)
			|| frm.events.sf_label_cancelled(frm) || frm.doc.sf_intercept_status) return;
		if (frm.events.sf_waiting_to_ship(frm)) {
			frm.add_custom_button(__("Print Label and Ship"), () => frm.events.sf_dispatch(frm), __("Tools"));
		} else {
			frm.add_custom_button(__(frm.events.sf_current_label_url(frm) ? "Open Shipping Label" : "Print Shipping Label"),
				() => frm.events.sf_print(frm), __("Tools"));
		}
	},

	sf_host(frm) {
		const html_field = frm.fields_dict.sf_actions_html;
		if (html_field && html_field.$wrapper) {
			return html_field.$wrapper;
		}
		let $host = frm.layout.wrapper.find("#sf-form-host");
		if ($host.length) {
			return $host;
		}
		$host = $('<div id="sf-form-host"></div>');
		const section =
			frm.fields_dict.shipment_information_section &&
			frm.fields_dict.shipment_information_section.wrapper;
		if (section) {
			$(section).append($host);
		} else {
			frm.layout.wrapper.find(".form-layout").append($host);
		}
		return $host;
	},

	sf_cancel_form_lookups(frm, scope = "") {
		const pending = frm._sf_form_lookups || {};
		Object.keys(pending).filter((key) => key.startsWith(scope)).forEach((key) => {
			const request = pending[key];
			// Invalidate before abort: jQuery runs completion handlers synchronously.
			delete pending[key];
			request.xhr?.abort();
		});
	},

	sf_form_lookup(frm, key, options) {
		frm.events.sf_cancel_form_lookups(frm, key);
		const pending = frm._sf_form_lookups ||= {};
		const doc = frm.doc;
		const name = doc.name;
		const current_route = frappe.get_route();
		if (current_route[0] !== "Form" || current_route[1] !== "Shipment"
			|| current_route[2] !== name || !frm.events.sf_is_sf(frm)) return;
		const route = current_route.join("/");
		const request = {};
		pending[key] = request;
		request.xhr = frappe.call({
			...options,
			callback(r) {
				if (pending[key] !== request || frm.doc !== doc || frm.doc.name !== name
					|| frappe.get_route().join("/") !== route || !frm.events.sf_is_sf(frm)) return;
				options.callback(r);
			},
			always() {
				if (pending[key] === request) delete pending[key];
			},
		});
		return request.xhr;
	},

	sf_load_form(frm) {
		if (!frm.events.sf_is_sf(frm)) return;
		frm._sf_form_active = true;
		frm.events.sf_cancel_form_lookups(frm);
		const args = frm.doc.name && !frm.is_new() ? { shipment: frm.doc.name } : {};
		const method = frm.is_new()
			? "erpnext_shipping.sf_international.shipping.get_sf_form_defaults"
			: "erpnext_shipping.sf_international.shipping.get_booking_options";
		if (frm.is_new()) {
			Object.assign(args, {
				pickup_from_type: frm.doc.pickup_from_type,
				pickup_address_name: frm.doc.pickup_address_name,
				delivery_address_name: frm.doc.delivery_address_name,
				pickup_contact_name: frm.doc.pickup_contact_name,
				delivery_contact_name: frm.doc.delivery_contact_name,
				delivery_contact: frm.doc.delivery_contact,
				delivery_to: frm.doc.delivery_to,
				value_of_goods: frm.doc.value_of_goods,
				total_weight: frm.doc.total_weight,
				delivery_notes: (frm.doc.shipment_delivery_note || [])
					.map((row) => row.delivery_note)
					.filter(Boolean),
			});
		}
		frm.events.sf_form_lookup(frm, "defaults", {
			method,
			args,
			callback(r) {
				if (!frm.events.sf_is_sf(frm)) return;
					frm.events.sf_prepare_section(frm);
				frm.events.sf_replace_tools(frm);
				frm.events.sf_render_form(frm, r.message || {});
				frm.events.sf_paint_status(frm);
				frm.events.sf_add_interception_buttons(frm);
				frm.events.sf_add_freight_accounting_buttons(frm);
			},
		});
	},

	sf_prefill_if_empty(frm) {
		if (!frm.events.sf_is_sf(frm) || frm.doc.shipment_id || frm._sf_user_edited) {
			return;
		}
		frm.events.sf_load_form(frm);
	},

	sf_preferred_product(products) {
		return (
			(products || []).find((row) => row.is_preferred) ||
			(products || []).find((row) => row.product_code === "10") ||
			(products || []).find((row) => row.product_name === "国际小包") ||
			(products || [])[0]
		);
	},

	sf_col(html, full) {
		return `<div class="sf-col${full ? " sf-col-12" : ""}">${html || ""}</div>`;
	},

	sf_row(left, right) {
		if (right === undefined) {
			return `<div class="sf-row">${left}</div>`;
		}
		return `<div class="sf-row">${left}${right}</div>`;
	},

	sf_field(label, attrs, value, locked) {
		const val = value === 0 || value ? frappe.utils.escape_html(String(value)) : "";
		return `
			<div class="sf-field">
				<label>${frappe.utils.escape_html(label)}</label>
				<input class="form-control" ${attrs} value="${val}" ${locked ? "disabled" : ""}>
			</div>
		`;
	},

	sf_textarea(label, attrs, value, locked) {
		const val = value ? frappe.utils.escape_html(String(value)) : "";
		return `
			<div class="sf-field">
				<label>${frappe.utils.escape_html(label)}</label>
				<textarea class="form-control" rows="2" ${attrs} ${locked ? "disabled" : ""}>${val}</textarea>
			</div>
		`;
	},

	sf_address_length(value) {
		return Array.from(String(value || "").trim()).length;
	},

	sf_address_field(frm, prefix, value, locked) {
		const length = frm.events.sf_address_length(value);
		const val = frappe.utils.escape_html(String(value || ""));
		return `<div class="sf-field">
			<label>详细地址</label>
			<textarea class="form-control" rows="2" data-sf-${prefix}-address
				aria-describedby="sf-${prefix}-address-hint" aria-invalid="${length > 60}"
				${locked ? "disabled" : ""}>${val}</textarea>
			<div id="sf-${prefix}-address-hint" data-sf-address-hint
				class="small ${length > 60 ? "text-danger" : "text-muted"}">
				${length}/60 个字符${length > 60 ? `，超出 ${length - 60} 个，修改后才能提交` : "，最多 60 个字符"}
			</div>
		</div>`;
	},

	sf_validate_address_lengths(frm, form) {
		const errors = [];
		for (const [key, label] of [["sender", "寄件人"], ["receiver", "收件人"]]) {
			const length = frm.events.sf_address_length(form?.[key]?.address);
			if (length > 60) errors.push(`${label}详细地址当前 ${length} 个字符，最多 60 个，超出 ${length - 60} 个。请修改后再提交。`);
		}
		if (!errors.length) return true;
		frappe.msgprint({ title: "详细地址超过长度限制", indicator: "red", message: errors.join("<br>") });
		return false;
	},

	sf_select(label, attrs, options, value, locked) {
		const html = (options || [])
			.map((row) => {
				const code = frappe.utils.escape_html(row.code || row.name || "");
				const name = frappe.utils.escape_html(row.name || row.code || "");
				const selected = (row.code || row.name) === value ? " selected" : "";
				return `<option value="${code}"${selected}>${name}</option>`;
			})
			.join("");
		return `
			<div class="sf-field">
				<label>${frappe.utils.escape_html(label)}</label>
				<select class="form-control" ${attrs} ${locked ? "disabled" : ""}>
					<option value=""></option>
					${html}
				</select>
			</div>
		`;
	},

	sf_suggest(label, attrs, list_id, value, locked) {
		const val = value ? frappe.utils.escape_html(String(value)) : "";
		return `
			<div class="sf-field">
				<label>${frappe.utils.escape_html(label)}</label>
				<input class="form-control" ${attrs} list="${list_id}" value="${val}" ${locked ? "disabled" : ""}>
				<datalist id="${list_id}"></datalist>
			</div>
		`;
	},

	sf_country_field(prefix, locked) {
		return `
			<div class="sf-field">
				<label>${__("Country")}</label>
				<select class="form-control" data-sf-${prefix}-country ${locked ? "disabled" : ""}></select>
			</div>
		`;
	},

	sf_postcode_field(prefix, value, locked) {
		const val = value ? frappe.utils.escape_html(String(value)) : "";
		const postcodeLocked = locked && !!val;
		const btn =
			prefix === "d" && !postcodeLocked
				? `<button type="button" class="btn btn-default btn-sm" data-sf-postcode="${prefix}">${__(
						"Lookup"
					)}</button>`
				: "";
		return `
			<div class="sf-field">
				<label>${__("Postcode")}</label>
				<div class="sf-postcode-row">
					<input class="form-control" data-sf-${prefix}-postcode value="${val}" ${postcodeLocked ? "disabled" : ""}>
					${btn}
				</div>
			</div>
		`;
	},

	sf_party_fields(frm, prefix, party, locked) {
		const row = party || {};
		const col = (html, full) => frm.events.sf_col(html, full);
		const pair = (left, right) => frm.events.sf_row(col(left), col(right));
		const full = (html) => frm.events.sf_row(col(html, true));
		if (prefix === "j") {
			return `
				${pair(
					frm.events.sf_field(__("Sender Company"), `data-sf-${prefix}-company`, row.company, locked),
					frm.events.sf_field("寄件人", `data-sf-${prefix}-contact`, row.contact, locked)
				)}
				${pair(
					frm.events.sf_field(__("Phone"), `data-sf-${prefix}-phone`, row.phone, locked),
					frm.events.sf_field(__("Mobile"), `data-sf-${prefix}-mobile`, row.mobile, locked)
				)}
				${pair(
					frm.events.sf_field(__("Email"), `data-sf-${prefix}-email`, row.email, locked),
					frm.events.sf_country_field(prefix, locked)
				)}
				${pair(
					frm.events.sf_suggest(
						__("Province"),
						`data-sf-${prefix}-province`,
						`sf-${prefix}-province-list`,
						row.province,
						locked
					),
					frm.events.sf_suggest(
						__("City"),
						`data-sf-${prefix}-city`,
						`sf-${prefix}-city-list`,
						row.city,
						locked
					)
				)}
				${pair(
					frm.events.sf_field(__("County"), `data-sf-${prefix}-county`, row.county, locked),
					frm.events.sf_postcode_field(prefix, row.post_code, locked)
				)}
				${full(frm.events.sf_address_field(frm, prefix, row.address, locked))}
			`;
		}
		return `
			${pair(
				frm.events.sf_field("收件人", `data-sf-${prefix}-contact`, row.contact, locked),
				frm.events.sf_field("电话", `data-sf-${prefix}-phone`, row.phone, locked)
			)}
			${pair(
				frm.events.sf_field(__("Email"), `data-sf-${prefix}-email`, row.email, locked),
				frm.events.sf_country_field(prefix, locked)
			)}
			${pair(
				frm.events.sf_suggest(
					__("Province"),
					`data-sf-${prefix}-province`,
					`sf-${prefix}-province-list`,
					row.province,
					locked
				),
				frm.events.sf_suggest(__("City"), `data-sf-${prefix}-city`, `sf-${prefix}-city-list`, row.city, locked)
			)}
			${pair(
				frm.events.sf_field(__("County"), `data-sf-${prefix}-county`, row.county, locked),
				frm.events.sf_postcode_field(prefix, row.post_code, locked)
			)}
			${pair(
				frm.events.sf_field(__("Doorplate"), `data-sf-${prefix}-doorplate`, row.doorplate, locked),
				""
			)}
			${full(frm.events.sf_address_field(frm, prefix, row.address, locked))}
		`;
	},

	sf_render_form(frm, data) {
		frm.events.sf_cancel_form_lookups(frm);
		const $host = frm.events.sf_host(frm);
		if (!$host || !$host.length) {
			return;
		}
		frm._sf_data = data || {};
		if (!frm._sf_data.enabled) {
			$host.html(`${frm.events.sf_transport_html(frm)}<p class="text-muted">${__("Enable SF International in SF International Settings.")}</p>`);
			return;
		}
		const booked_waybill = String(frm.doc.shipment_id || frm.doc.awb_number || frm._sf_data.shipment_id || "").trim();
		const booked = !!booked_waybill;
		const booking_status = String(frm.doc.sf_waybill_replacement_status || "").trim();
		const booking_pending = !booked && booking_status === "创建中";
		const booking_uncertain = !booked && booking_status === "失败";
		const products = frm._sf_data.products || [];
		const stored = { ...(frm._sf_data.form || {}) };
		stored.sender = { ...((frm._sf_data.form || {}).sender || {}) };
		stored.receiver = { ...((frm._sf_data.form || {}).receiver || {}) };
		const replacement_draft = frm._sf_replacement_draft || null;
		const kept = frm._sf_keep_form;
		frm._sf_keep_form = null;
		if (kept) {
			stored.receiver = kept.receiver || stored.receiver;
			stored.product_name = kept.product_name || stored.product_name;
			stored.product_code = kept.product_code || stored.product_code;
			stored.ename = kept.ename || stored.ename;
			stored.cname = kept.cname || stored.cname;
			stored.hs_code = kept.hs_code || stored.hs_code;
			stored.declared_value = kept.declared_value || stored.declared_value;
			stored.declared_currency = kept.declared_currency || stored.declared_currency;
			stored.parcel_quantity = kept.parcel_quantity || stored.parcel_quantity;
			stored.total_weight = kept.total_weight || stored.total_weight;
			stored.existing_waybill = kept.existing_waybill || stored.existing_waybill;
		}
		const cancelled = frm.events.sf_is_cancelled(frm);
		const replacement_editing = !!(booked && replacement_draft && !cancelled);
		frm.events.sf_replacement_edit_guard(frm, replacement_editing);
		if (replacement_editing && replacement_draft.form) {
			Object.assign(stored, replacement_draft.form);
			stored.sender = { ...stored.sender, ...(replacement_draft.form.sender || {}) };
			stored.receiver = { ...stored.receiver, ...(replacement_draft.form.receiver || {}) };
		}
		const preferred = frm.events.sf_preferred_product(products);
		const selected_name =
			stored.product_name || (preferred && preferred.product_name) || "国际小包";
		const product_options = products.map((row) => ({
			code: row.product_name,
			name: row.product_name,
		}));
		const sender = stored.sender || {};
		const receiver = stored.receiver || {};
		if (!(receiver.contact || "").trim()) {
			receiver.contact = (frm.doc.delivery_to || frm.doc.delivery_customer || "").trim();
		}
		if (!(receiver.phone || "").trim()) {
			const text = String(frm.doc.delivery_contact || "");
			const found = text.match(/\+?\d[\d\s\-()]{6,}\d/);
			receiver.phone = found ? found[0].replace(/[\s\-()]/g, "") : "";
		}
		const freight = frm.events.sf_freight_summary(frm.doc);
		const label_url = frm.events.sf_current_label_url(frm);
		const labelCancelled = frm.events.sf_label_cancelled(frm);
		const waiting = frm.events.sf_waiting_to_ship(frm) && !frm.doc.sf_intercept_status;
		const locked = (booked && !replacement_editing) || booking_pending || booking_uncertain || labelCancelled || cancelled;
		// A replacement may correct destination/product/customs, while the origin
		// remains the booked warehouse and cannot be changed in this operation.
		const senderLocked = booked ? true : locked || frm._sf_data.sender_preset !== false;
		const existing_typed = String(
			frm._sf_existing_waybill || stored.existing_waybill || booked_waybill || ""
		).trim();
		if (booked_waybill) {
			frm._sf_existing_waybill = booked_waybill;
			frm._sf_entry_mode = "booked";
		} else if (!frm._sf_entry_mode || frm._sf_entry_mode === "booked") {
			frm._sf_entry_mode =
				stored.entry_mode === "existing" || existing_typed ? "existing" : "create";
		}
		const entry_mode = frm._sf_entry_mode === "existing" ? "existing" : "create";
		const choose_mode = !booked && !booking_pending && !booking_uncertain && !labelCancelled && !cancelled;
		const show_create_form = choose_mode && entry_mode === "create";
		const show_details = show_create_form || replacement_editing;
		const details_html = show_details ? `${frm.events.sf_row(
					frm.events.sf_col(
						frm.events.sf_select(__("Product"), "data-sf-product", product_options, selected_name, locked)
					),
					frm.events.sf_col("")
				)}
				<div class="sf-subtitle">寄件人</div>
				<p class="text-muted sf-hint">${__("Sender follows the pickup address. Company name uses SF settings.")}</p>
				${frm.events.sf_party_fields(frm, "j", sender, senderLocked)}
				<div class="sf-subtitle">收件人</div>
				${
					!booked && frm._sf_data.receiver_remembered
						? `<p class="text-muted sf-hint">${__(
								"Receiver region reused from a successful shipment to this address."
							)}</p>`
						: ""
				}
				${frm.events.sf_party_fields(frm, "d", receiver, locked)}
				<div class="sf-subtitle">${__("Customs")}</div>
				${frm.events.sf_row(
					frm.events.sf_col(
						frm.events.sf_field(__("English Name"), "data-sf-ename", stored.ename || "Billiard goods", locked)
					),
					frm.events.sf_col(
						frm.events.sf_field(__("Chinese Name"), "data-sf-cname", stored.cname || "台球体育用品", locked)
					)
				)}
				${frm.events.sf_row(
					frm.events.sf_col(frm.events.sf_field(__("HS Code"), "data-sf-hscode", stored.hs_code, locked)),
					frm.events.sf_col(
						frm.events.sf_field(
							__("Declared Value"),
							'type="number" min="0" step="0.01" data-sf-declared',
							stored.declared_value || 20,
							locked
						)
					)
				)}
				${frm.events.sf_row(
					frm.events.sf_col(
						frm.events.sf_field(
							__("Declared Currency"),
							"data-sf-currency",
							stored.declared_currency || "CNY",
							locked
						)
					),
					frm.events.sf_col(
						frm.events.sf_field(
							__("Parcel Quantity"),
							'type="number" min="1" step="1" data-sf-qty',
							stored.parcel_quantity || 1,
							locked
						)
					)
				)}
				${frm.events.sf_row(
					frm.events.sf_col(
						frm.events.sf_field(
							__("Weight (kg)"),
							'type="number" min="0" step="0.001" data-sf-weight',
							stored.total_weight || frm.doc.total_weight || 0.1,
							locked
						)
					),
					frm.events.sf_col("")
				)}` : "";
		$host.html(`
			<div class="sf-form">
				${
					booking_pending
						? `<div class="sf-result"><strong>${__("Creating SF waybill")}</strong><div>${__("SF waybill creation is in progress. Refresh this Shipment after the background task finishes.")}</div><button type="button" class="btn btn-default btn-sm" data-sf-action="refresh">${__("Refresh Shipment")}</button></div>`
						: booking_uncertain
						? `<div class="sf-result"><strong>${__("SF waybill creation failed")}</strong><div>${__("SF waybill creation needs verification. Check the waybill history before retrying.")}</div><button type="button" class="btn btn-default btn-sm" data-sf-action="refresh">${__("Refresh Shipment")}</button></div>`
						: booked
						? `<div class="sf-result">${__("SF Waybill")}: <strong>${frappe.utils.escape_html(
									booked_waybill
								)}</strong><div>${__("Shipment Status")}: ${frappe.utils.escape_html(__(frm.doc.status || ""))}</div><div>${frappe.utils.escape_html(freight)}</div></div>`
							: labelCancelled
							? `<p class="text-muted sf-hint">${__(
									"SF label cancelled. The shipment and original waybill are kept."
								)}</p>`
							: `<div class="sf-mode">
								<button type="button" class="btn btn-sm ${entry_mode === "create" ? "btn-primary" : "btn-default"}" data-sf-entry-mode="create">${__("Create new SF label")}</button>
								<button type="button" class="btn btn-sm ${entry_mode === "existing" ? "btn-primary" : "btn-default"}" data-sf-entry-mode="existing">${__("Use existing SF waybill")}</button>
							</div>
							${
								show_create_form
									? `<p class="text-muted sf-hint">${__(
											"Fill sender, receiver, product and customs, then Save and Submit to create the SF label."
										)}</p>`
									: `<p class="text-muted sf-hint">${__(
											"Enter the existing SF International waybill, then Save and Submit. No customs form is needed."
										)}</p>
										${frm.events.sf_row(
											frm.events.sf_col(
												frm.events.sf_field(
													__("Existing SF Waybill"),
													"data-sf-existing-waybill",
													frm._sf_existing_waybill || stored.existing_waybill || "",
													false
												)
											),
											frm.events.sf_col("")
										)}`
							}`
				}
				${frm.events.sf_transport_html(frm)}
				${show_create_form ? details_html : ""}
					${
						booked && !replacement_editing
							? `<div class="sf-actions">
									${
										label_url
											? `<a class="btn btn-primary btn-sm" data-sf-open-label="1" href="${frappe.utils.escape_html(
													frappe.urllib.get_full_url(label_url)
												)}" target="_blank" rel="noopener">${__("Open Shipping Label")}</a>`
											: ""
									}
									${
										cancelled || labelCancelled || frm.doc.sf_intercept_status ? "" : waiting
											? `<button type="button" class="btn btn-primary btn-sm" data-sf-action="dispatch">${__(
													"Print Label and Ship"
												)}</button>`
											: label_url ? "" : `<button type="button" class="btn btn-default btn-sm" data-sf-action="print">${__(
													"Print Shipping Label"
												)}</button>`
									}
									<button type="button" class="btn btn-default btn-sm" data-sf-action="track">${__(
										"Query Tracking"
									)}</button>
									<button type="button" class="btn btn-default btn-sm" data-sf-action="freight">${__(
										"Query Freight"
									)}</button>
									${frm.events.sf_can_cancel_waybill(frm) ? `<button type="button" class="btn btn-default btn-sm" data-sf-action="cancel">${__("Cancel SF Waybill")}</button>` : ""}
									${(() => { const interception = frm.events.sf_interception_state(frm); return interception.state ? `<div class="sf-interception-state"><strong>${__("Interception Status")}:</strong> ${frappe.utils.escape_html(interception.state)}</div>` : ""; })()}
								</div>`
							: ""
					}
				${booked ? `<div class="sf-waybill-replacement-slot">${frm.events.sf_waybill_replacement_html(frm, frm.events.sf_waybill_records(frm), false, false)}</div>` : ""}
				${replacement_editing ? `<div class="sf-replacement-editor">
					<div class="sf-replacement-edit-hint">
						<div class="sf-subtitle">${replacement_draft.shipped ? __("Create new waybill as replacement") : __("Unshipped: recreate waybill")}</div>
						${replacement_draft.shipped ? `<p class="text-muted sf-hint">${__("This creates a new label and does not cancel or switch the old one. Contact SF customer service outside ERP, then record their exact result.")}</p>` : ""}
						${frm.events.sf_textarea(__("Replacement reason"), 'data-sf-replacement-reason required maxlength="2000"', replacement_draft.reason, false)}
						<p class="text-muted sf-hint">${__("Edit the corrected recipient, product, and customs data before creating the replacement label.")}</p>
					</div>
					${details_html}
					<div class="sf-actions">
									<button type="button" class="btn btn-primary btn-sm" data-sf-action="submit-replacement">${replacement_draft.shipped ? __("Create new waybill as replacement") : __("Create replacement waybill")}</button>
									<button type="button" class="btn btn-default btn-sm" data-sf-action="cancel-replacement-edit">${__("Cancel replacement edit")}</button>
								</div>
				</div>` : ""}
			</div>
			<style>
				.frappe-control[data-fieldname="sf_actions_html"] .control-label,
				.frappe-control[data-fieldname="sf_actions_html"] .help-box { display: none !important; }
				.frappe-control[data-fieldname="sf_actions_html"] { max-width: 100%; }
				.sf-form { padding: 0 0 8px; max-width: 100%; }
				.sf-mode { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 12px; }
				.sf-hint { margin: 0 0 10px; }
				.sf-subtitle {
					font-weight: 600;
					margin: 16px 0 8px;
					padding-top: 12px;
					border-top: 1px solid var(--border-color, var(--dark-border-color, #d1d8dd));
				}
				.sf-row { display: flex; flex-wrap: wrap; margin: 0 -8px; }
				.sf-col { width: 50%; padding: 0 8px 12px; box-sizing: border-box; min-width: 0; }
				.sf-col-12 { width: 100%; }
				.sf-field label { display: block; font-size: var(--text-sm, 12px); color: var(--text-muted); margin: 0 0 4px; }
				.sf-field textarea { min-height: 58px; resize: vertical; }
				.sf-postcode-row { display: flex; gap: 8px; align-items: center; }
				.sf-postcode-row input { flex: 1; min-width: 0; }
				.sf-result { margin: 0 0 12px; }
				.sf-transport-result { margin: 0 0 12px; }
				.sf-transport-details { margin-top: 4px; overflow-wrap: anywhere; }
				.sf-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
				.sf-waybill-replacement-panel { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border-color, var(--dark-border-color, #d1d8dd)); }
				.sf-waybill-title { font-weight: 600; margin: 0 0 8px; }
				.sf-waybill-current { margin: 0 0 8px; }
				.sf-waybill-history { display: flex; flex-direction: column; gap: 6px; margin: 8px 0 10px; }
				.sf-waybill-history-row { padding: 6px 8px; border: 1px solid var(--border-color, #d1d8dd); border-radius: 4px; }
				.sf-waybill-history-row.sf-waybill-active { background: var(--subtle-fg, rgba(68, 156, 240, .06)); border-color: var(--primary-color, #449cf0); }
				.sf-waybill-history-main { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
				.sf-waybill-history-details { margin-top: 4px; color: var(--text-muted); font-size: var(--text-sm, 12px); overflow-wrap: anywhere; }
				.sf-tracking-history { margin-top: 8px; }
				.sf-tracking-history > summary { cursor: pointer; font-weight: 500; padding: 4px 0; }
				.sf-tracking-history[open] > summary { margin-bottom: 6px; }
				.sf-waybill-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
				.sf-waybill-hint { margin: 4px 0 0; flex: 1 1 100%; }
				.sf-waybill-empty { margin: 6px 0; }
				@media (max-width: 768px) { .sf-col { width: 100%; } }
			</style>
		`);
		$host.find("input, select, textarea").on("change", () => {
			frm._sf_user_edited = true;
		});
		$host.find("[data-sf-j-address], [data-sf-d-address]").on("input change", function () {
			const length = frm.events.sf_address_length(this.value);
			$(this).attr("aria-invalid", String(length > 60));
			$(this).siblings("[data-sf-address-hint]")
				.toggleClass("text-danger", length > 60).toggleClass("text-muted", length <= 60)
				.text(`${length}/60 个字符${length > 60 ? `，超出 ${length - 60} 个，修改后才能提交` : "，最多 60 个字符"}`);
		});
		$host.find("[data-sf-replacement-reason]").on("input change", function () {
			if (frm._sf_replacement_draft) frm._sf_replacement_draft.reason = this.value;
		});
		$host.find("[data-sf-action]").on("click", function () {
			const action = this.getAttribute("data-sf-action");
			if (action === "print") {
				frm.events.sf_print(frm);
			} else if (action === "dispatch") {
				frm.events.sf_dispatch(frm);
			} else if (action === "track") {
				frm.events.sf_track(frm);
			} else if (action === "freight") {
				frm.events.sf_freight(frm);
			} else if (action === "cancel") {
				frm.events.sf_cancel(frm);
			} else if (action === "recreate") {
				frm.events.sf_recreate(frm);
			} else if (action === "submit-replacement") {
				frm.events.sf_submit_waybill_replacement(frm);
				} else if (action === "cancel-replacement-edit") {
					frm.events.sf_cancel_waybill_replacement_edit(frm);
				} else if (action === "refresh") {
					frm.reload_doc();
				}
		});
		$host.find("[data-sf-existing-waybill]").on("input", function () {
			frm._sf_existing_waybill = this.value;
		});
		$host.find("[data-sf-entry-mode]").on("click", function () {
			const mode = this.getAttribute("data-sf-entry-mode");
			frm._sf_entry_mode = mode;
			if (mode === "create") {
				frm._sf_existing_waybill = "";
			}
			frm.events.sf_render_form(frm, frm._sf_data);
		});
		$host.find("[data-sf-postcode]").on("click", function () {
			frm.events.sf_lookup_postcode(frm, this.getAttribute("data-sf-postcode"));
		});
		if (show_details) {
			frm.events.sf_bind_country(frm, "j", sender.country, senderLocked);
			frm.events.sf_bind_country(frm, "d", receiver.country, locked);
		}
		if (booked) {
			frm.events.sf_load_waybill_replacement(frm);
		}
	},

	sf_bind_country(frm, prefix, country, locked) {
		frm.events.sf_cancel_form_lookups(frm, `${prefix}:`);
		const $host = frm.events.sf_host(frm);
		const $country = $host.find(`[data-sf-${prefix}-country]`);
		if (!$country.length) {
			return;
		}
		frm.events.sf_form_lookup(frm, `${prefix}:country`, {
			method: "erpnext_shipping.sf_international.shipping.list_sf_countries",
			callback(r) {
				const rows = r.message || [];
				$country.empty().append('<option value=""></option>');
				rows.forEach((row) => {
					$country.append(
						$("<option></option>").attr("value", row.code).text(`${row.code} ${row.name}`)
					);
				});
				if (country) {
					$country.val(country);
				}
				if (country && !locked) {
					frm.events.sf_load_regions(frm, prefix, country, $host.find(`[data-sf-${prefix}-province]`).val());
				}
			},
		});
		$country.on("change", function () {
			frm.events.sf_cancel_form_lookups(frm, `${prefix}:country`);
			$host.find(`[data-sf-${prefix}-province]`).val("");
			$host.find(`[data-sf-${prefix}-city]`).val("");
			frm.events.sf_load_regions(frm, prefix, this.value, "");
		});
		$host.find(`[data-sf-${prefix}-province]`).on("change", function () {
			$host.find(`[data-sf-${prefix}-city]`).val("");
			const code = $country.val();
			frm.events.sf_load_regions(frm, prefix, code, this.value);
		});
	},

	sf_load_regions(frm, prefix, country, province) {
		frm.events.sf_cancel_form_lookups(frm, `${prefix}:regions:`);
		const $host = frm.events.sf_host(frm);
		frm.events.sf_fill_datalist($host.find(`#sf-${prefix}-province-list`), []);
		frm.events.sf_fill_datalist($host.find(`#sf-${prefix}-city-list`), []);
		if (!country) return;
		frm.events.sf_form_lookup(frm, `${prefix}:regions:province`, {
			method: "erpnext_shipping.sf_international.shipping.list_sf_regions",
			args: { country_code: country, region_one_name: "" },
			callback(r) {
				const names = (r.message || []).map((row) => row.name);
				frm.events.sf_fill_datalist($host.find(`#sf-${prefix}-province-list`), names);
			},
		});
		if (province) {
			frm.events.sf_form_lookup(frm, `${prefix}:regions:city`, {
				method: "erpnext_shipping.sf_international.shipping.list_sf_regions",
				args: { country_code: country, region_one_name: province },
				callback(r) {
					const names = (r.message || []).map((row) => row.name);
					frm.events.sf_fill_datalist($host.find(`#sf-${prefix}-city-list`), names);
				},
			});
		}
	},

	sf_fill_datalist($list, names) {
		if (!$list.length) {
			return;
		}
		$list.empty();
		(names || []).forEach((name) => {
			$list.append($("<option></option>").attr("value", name));
		});
	},

	sf_lookup_postcode(frm, prefix) {
		const $host = frm.events.sf_host(frm);
		const country = $host.find(`[data-sf-${prefix}-country]`).val();
		const post_code = $host.find(`[data-sf-${prefix}-postcode]`).val();
		if (!country || !post_code) {
			frappe.msgprint(__("Country and postcode are required."));
			return;
		}
		frappe.call({
			method: "erpnext_shipping.sf_international.shipping.lookup_sf_postcode",
			args: { country_code: country, post_code },
			freeze: true,
			freeze_message: __("Looking up postcode"),
			callback(r) {
				const rows = r.message || [];
				if (!rows.length) {
					frappe.msgprint(__("No postcode match."));
					return;
				}
				const apply = (row) => {
					if (row.country) {
						$host.find(`[data-sf-${prefix}-country]`).val(row.country);
					}
					if (row.province) {
						$host.find(`[data-sf-${prefix}-province]`).val(row.province);
					}
					if (row.city) {
						$host.find(`[data-sf-${prefix}-city]`).val(row.city);
					}
					if (row.county) {
						$host.find(`[data-sf-${prefix}-county]`).val(row.county);
					}
					if (row.post_code) {
						$host.find(`[data-sf-${prefix}-postcode]`).val(row.post_code);
					}
					if (row.country) {
						frm.events.sf_load_regions(frm, prefix, row.country, row.province);
					}
					frappe.show_alert({
						message: [row.country, row.province, row.city, row.county, row.post_code]
							.filter(Boolean)
							.join(" · "),
						indicator: "green",
					});
				};
				if (rows.length === 1) {
					apply(rows[0]);
					return;
				}
				const options = rows.map(
					(row, idx) =>
						`${idx + 1}. ${[row.country, row.province, row.city, row.county, row.post_code]
							.filter(Boolean)
							.join(" ")}`
				);
				frappe.prompt(
					{
						fieldname: "choice",
						fieldtype: "Select",
						label: __("Postcode"),
						options: options.join("\n"),
						reqd: 1,
					},
					(values) => {
						const idx = cint(String(values.choice).split(".")[0]) - 1;
						if (rows[idx]) {
							apply(rows[idx]);
						}
					},
					__("Lookup")
				);
			},
		});
	},

	sf_saved_form(frm) {
		let saved = {};
		try {
			saved = JSON.parse(frm.doc.sf_form_json || "{}") || {};
		} catch (error) { /* Server defaults remain available for legacy invalid JSON. */ }
		const source = frm._sf_data?.form;
		return JSON.parse(JSON.stringify(source && Object.keys(source).length ? source : saved));
	},

	sf_read_form(frm) {
		const $host = frm.events.sf_host(frm);
		if (!$host.find("[data-sf-product]").length) {
			const form = frm.events.sf_saved_form(frm);
			form.existing_waybill = $host.find("[data-sf-existing-waybill]").val() ||
				frm._sf_existing_waybill || frm.doc.shipment_id || frm.doc.awb_number || form.existing_waybill || "";
			return form;
		}
		const product_name = $host.find("[data-sf-product]").val() || "国际小包";
		const products = (frm._sf_data && frm._sf_data.products) || [];
		const selected = products.find((row) => row.product_name === product_name) || {};
		const party = (prefix) => ({
			company: $host.find(`[data-sf-${prefix}-company]`).val() || "",
			contact: $host.find(`[data-sf-${prefix}-contact]`).val() || "",
			phone: $host.find(`[data-sf-${prefix}-phone]`).val() || "",
			mobile: $host.find(`[data-sf-${prefix}-mobile]`).val() || "",
			email: $host.find(`[data-sf-${prefix}-email]`).val() || "",
			country: $host.find(`[data-sf-${prefix}-country]`).val() || "",
			province: $host.find(`[data-sf-${prefix}-province]`).val() || "",
			city: $host.find(`[data-sf-${prefix}-city]`).val() || "",
			county: $host.find(`[data-sf-${prefix}-county]`).val() || "",
			post_code: $host.find(`[data-sf-${prefix}-postcode]`).val() || "",
			doorplate: $host.find(`[data-sf-${prefix}-doorplate]`).val() || "",
			address: $host.find(`[data-sf-${prefix}-address]`).val() || "",
		});
		return {
			product_code: selected.product_code || "10",
			product_name,
			total_weight: $host.find("[data-sf-weight]").val(),
			declared_value: $host.find("[data-sf-declared]").val(),
			declared_currency: $host.find("[data-sf-currency]").val() || "CNY",
			purchase_currency: $host.find("[data-sf-currency]").val() || "CNY",
			hs_code: $host.find("[data-sf-hscode]").val(),
			ename: $host.find("[data-sf-ename]").val(),
			cname: $host.find("[data-sf-cname]").val(),
			parcel_quantity: $host.find("[data-sf-qty]").val() || 1,
			existing_waybill:
				$host.find("[data-sf-existing-waybill]").val() ||
				frm._sf_existing_waybill ||
				frm.doc.shipment_id ||
				frm.doc.awb_number ||
				"",
			sender: party("j"),
			receiver: party("d"),
		};
	},

	sf_label_href(url) {
		if (!url) {
			return "";
		}
		if (String(url).indexOf("http") === 0) {
			return url;
		}
		return frappe.urllib.get_full_url(url);
	},

	sf_show_label(frm, url, shipped) {
		url = url || frm.doc.sf_label_url;
		const href = frm.events.sf_label_href(url);
		if (!href) {
			frappe.msgprint({
				title: shipped ? __("Print Label and Ship") : __("Print Shipping Label"),
				indicator: "red",
				message: __("The label was not generated. Please print again."),
			});
			return;
		}
		frappe.msgprint({
			title: shipped ? __("Print Label and Ship") : __("Print Shipping Label"),
			indicator: "green",
			message: shipped
				? __("Shipped. Waybill {0}. The label is saved. Open it from the button below.", [
						frappe.utils.escape_html(frm.doc.shipment_id || frm.doc.awb_number || ""),
					])
				: __("The label is saved. Open it from the button below."),
			primary_action: {
				label: __("Open Shipping Label"),
				action() {
					window.open(href, "_blank");
				},
			},
		});
	},

	sf_print(frm) {
		if (!frm.doc.name || frm.is_new()) {
			return;
		}
		const cached = frm.events.sf_current_label_url(frm);
		if (cached) {
			window.open(frm.events.sf_label_href(cached), "_blank", "noopener");
			return;
		}
		if (frm._sf_printing) return;
		frm._sf_printing = true;
		frappe.call({
			method: "erpnext_shipping.sf_international.shipping.print_shipping_label",
			args: { shipment: frm.doc.name },
			freeze: true,
			freeze_message: __("Printing SF International label"),
			always() { frm._sf_printing = false; },
			callback(r) {
				if (r.exc) {
					return;
				}
				const url = r.message;
				const show = () => frm.events.sf_show_label(frm, url, false);
				Promise.resolve(frm.reload_doc()).then(show);
			},
		});
	},

	sf_dispatch(frm) {
		if (!frm.doc.name || frm.is_new()) {
			return;
		}
		if (frm.doc.docstatus !== 1) {
			frappe.msgprint(__("Submit the shipment before printing the label and shipping."));
			return;
		}
		frappe.call({
			method: "erpnext_shipping.sf_international.shipping.dispatch_sf_shipment",
			args: { shipment: frm.doc.name },
			freeze: true,
			freeze_message: __("Printing label and shipping"),
			callback(r) {
				if (r.exc) {
					return;
				}
				const url = r.message;
				const show = () => frm.events.sf_show_label(frm, url, true);
				Promise.resolve(frm.reload_doc()).then(show);
			},
		});
	},

	sf_show_tracking_result(frm, data) {
		const escape = frappe.utils.escape_html;
		const state = frm.events.sf_transport_state(data);
		const events = Array.isArray(data.tracking_events) ? data.tracking_events : [];
		const detail = String(data.tracking_status_info || "").trim();
		const has_tracking = events.length > 0 || !!detail || (!!data.tracking_status && data.tracking_status !== "Booked");
		const summary = has_tracking
			? [state.label, detail].filter(Boolean).map(escape).join("<br>") || __("Tracking query completed.")
			: __("SF has not returned any tracking events yet.");
		const history = events.length ? `<h5>${escape(__("Tracking history ({0})", [events.length]))}</h5>${frm.events.sf_tracking_timeline_html(events)}` : "";
		frappe.msgprint({
			title: __("Tracking query result"),
			message: `${data.awb_number || data.waybill ? `<p><strong>${escape(data.awb_number || data.waybill)}</strong></p>` : ""}<p>${summary}</p>${history}`,
			indicator: has_tracking ? state.color : "orange",
			wide: true,
		});
	},

	sf_track(frm) {
		frappe.call({
			method: "erpnext_shipping.sf_international.shipping.update_tracking",
			args: {
				shipment: frm.doc.name,
				service_provider: frm.doc.service_provider,
				shipment_id: frm.doc.shipment_id || frm.doc.awb_number,
				delivery_notes: (frm.doc.shipment_delivery_note || [])
					.map((row) => row.delivery_note)
					.filter(Boolean),
			},
			freeze: true,
			freeze_message: __("Querying SF tracking"),
			btn: frm.events.sf_host(frm).find('[data-sf-action="track"]'),
			callback(r) {
				if (r.exc) return;
				const data = r.message || {};
				frm.events.sf_show_tracking_result(frm, data);
				frm.reload_doc();
			},
		});
	},

	sf_freight(frm) {
		frappe.call({
			method: "erpnext_shipping.sf_international.shipping.fetch_sf_freight",
			args: { shipment: frm.doc.name },
			freeze: true,
			freeze_message: __("Querying SF freight"),
			btn: frm.events.sf_host(frm).find('[data-sf-action="freight"]'),
			callback(r) {
				if (r.exc) return;
				const data = r.message || {};
				frappe.msgprint({
					title: __("Freight query result"),
					message: data.message || __("Freight bill pending verification"),
					indicator: data.query_failed ? "red" : data.status === "账单已取得" && !["记账待处理", "待复核"].includes(data.accounting_status) ? "green" : "orange",
				});
				frm.reload_doc();
			},
		});
	},

	sf_cancel(frm) {
		if (!frm.events.sf_can_cancel_waybill(frm)) {
			return;
		}
		const run = () => {
			frappe.call({
				method: "erpnext_shipping.sf_international.shipping.cancel_sf_shipment",
				args: { shipment: frm.doc.name },
				freeze: true,
				freeze_message: __("Cancelling SF waybill"),
				callback(r) {
					const data = r.message || {};
					frappe.msgprint({
						title: __("Cancel SF Waybill"),
						message: data.message || __("SF label cancelled. The shipment and original waybill are kept."),
						indicator: data.ok ? "green" : "orange",
					});
					frm.reload_doc();
				},
			});
		};
		frappe.confirm(__("Cancel this unshipped SF waybill? The original waybill and all freight records will be kept."), run);
	},

	sf_recreate(frm) {
		if (!frm.doc.name || frm.is_new() || frm.doc.shipment_id || frm.doc.awb_number
			|| frm.events.sf_is_cancelled(frm) || frm.events.sf_label_cancelled(frm) || frm.doc.sf_intercept_status) {
			return;
		}
		const form = frm.events.sf_read_form(frm);
		if (!frm.events.sf_validate_address_lengths(frm, form)) return;
		frappe.call({
			method: "erpnext_shipping.sf_international.shipping.recreate_sf_shipment",
			args: { shipment: frm.doc.name, form_json: JSON.stringify(form) },
			freeze: true,
			freeze_message: __("Creating SF waybill"),
			callback() {
				frm.reload_doc();
			},
		});
	},
});
