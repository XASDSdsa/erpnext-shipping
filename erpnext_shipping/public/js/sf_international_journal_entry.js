frappe.ui.form.on("Journal Entry", {
	refresh(frm) {
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

	before_cancel(frm) {
		const state = frm.doc.__onload?.sf_freight_accounting || {};
		if (!frm.doc.sf_freight_shipment && !state.shipment && !state.protected) return;
		frappe.validated = false;
		frappe.msgprint(__("顺丰运费凭证须通过“运费记账”中的“更正运费凭证”处理。"));
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
});
