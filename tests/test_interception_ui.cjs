const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const events = {};
const context = {
	erpnext: { shipment: { carriers: {}, register_carrier(name, carrier) { this.carriers[name] = carrier; } } },
	console,
	document: { getElementById() { return null; }, createElement() { return {}; }, head: { appendChild() {} } },
	window: {},
	cint: (value) => Number(value) || 0,
	__: (value) => value,
	frappe: {
		_messages: {},
		ui: { form: { on(doctype, handlers) { events[doctype] = handlers; } } },
		utils: { escape_html: (value) => String(value ?? "") },
		msgprint() {},
	},
};
vm.createContext(context);
const source = fs.readFileSync(`${__dirname}/../erpnext_shipping/public/js/sf_international_shipment.js`, "utf8");
vm.runInContext(source, context, { filename: "shipment.js" });

const shipment = events.Shipment;
assert.ok(shipment, "Shipment handlers are registered");
assert.match(source, /method:\s*"erpnext_shipping\.sf_international\.interception\.request_interception"/);
assert.match(source, /method:\s*"erpnext_shipping\.sf_international\.interception\.record_interception_result"/);
assert.match(source, /method:\s*"erpnext_shipping\.sf_international\.interception\.verify_carrier_cancellation"/);

const frm = {
	doc: { __onload: { sf_interception: { state: "顺丰客服确认成功待顺丰确认", can_request: false, can_record: true, can_verify: true, carrier_cancelled: false } }, sf_intercept_status: "顺丰客服确认成功待顺丰确认", sf_carrier_cancelled: 0 },
};
const state = shipment.sf_interception_state(frm);
assert.equal(state.state, "顺丰客服确认成功待顺丰确认");
assert.equal(state.carrier_cancelled, false, "manual customer-service success is not carrier cancellation");
assert.equal(state.can_verify, true);

const buttons = [];
const buttonForm = {
	doc: { name: "S-1", docstatus: 1, shipment_id: "SF-1", __onload: { sf_interception: { can_request: false, can_record: true, can_verify: true, state: "顺丰客服确认成功待顺丰确认" } } },
	events: { sf_is_sf: () => true, sf_interception_state: shipment.sf_interception_state },
	remove_custom_button() {},
	add_custom_button(label, handler, group) { buttons.push({ label, handler, group }); },
};
shipment.sf_add_interception_buttons(buttonForm);
assert.deepEqual(buttons.map((row) => row.label), ["Record SF Customer Service Feedback", "Verify SF Cancellation"]);
assert.equal(buttons.some((row) => row.label === "Request Interception"), false, "request is hidden once one exists");
const cancelCheck = {
	doc: { name: "S-1", shipment_id: "SF-1", docstatus: 1, status: "已发货", tracking_status: "In Progress" },
	is_new: () => false,
	events: {
		sf_is_cancelled: shipment.sf_is_cancelled,
		sf_label_cancelled: shipment.sf_label_cancelled,
		sf_requires_manual_interception: shipment.sf_requires_manual_interception,
	},
};
assert.equal(shipment.sf_can_cancel_waybill(cancelCheck), false, "dispatched and tracked shipments cannot cancel the carrier waybill");

for (const status of ["已揽收", "运输中", "派送中", "已签收", "已退回", "已丢失", "Shipped", "Delivered", "Returned", "Lost"]) {
	const form = {
		doc: { name: "S-1", shipment_id: "SF-1", docstatus: 1, status, tracking_status: "" },
		is_new: () => false,
		events: {
			sf_is_cancelled: shipment.sf_is_cancelled,
			sf_label_cancelled: shipment.sf_label_cancelled,
			sf_requires_manual_interception: shipment.sf_requires_manual_interception,
		},
	};
	assert.equal(shipment.sf_can_cancel_waybill(form), false, `${status} cannot cancel the carrier waybill directly`);
}

assert.match(source, /frm\.events\.sf_can_cancel_waybill\(frm\).*data-sf-action="cancel"/s, "cancel action is guarded by the pre-dispatch predicate");
assert.match(source, /sf_can_cancel_waybill\(frm\)\s*\{[\s\S]*?sf_requires_manual_interception\(frm\)/s);

const calls = [];
let prompt;
context.frappe.prompt = (fields, callback) => { prompt = { fields, callback }; };
context.frappe.call = (args) => { calls.push(args); };
const requestForm = {
	doc: { name: "S-1", shipment_id: "SF-1" },
	events: { sf_interception_state: () => ({ can_request: true, can_record: true, can_verify: true }) },
	reload_doc() {},
};
shipment.sf_request_interception(requestForm);
assert.equal(prompt.fields.length, 1);
assert.ok(prompt.fields[0].reqd, "interception reason is required");
prompt.callback({ reason: "   " });
assert.equal(calls.length, 0, "blank reason makes no request");
prompt.callback({ reason: "customer requested return" });
assert.equal(calls.length, 1);
assert.equal("assigned_to" in calls[0].args, false);

shipment.sf_record_interception_result(requestForm);
assert.ok(prompt.fields.every((field) => field.reqd), "manual status and note are required");
assert.equal(prompt.fields[0].options.includes("顺丰已取消"), false, "manual result cannot claim carrier cancellation");
prompt.callback({ status: "顺丰客服确认成功待顺丰确认", note: "   " });
assert.equal(calls.length, 1);
prompt.callback({ status: "顺丰已取消", note: "forged" });
assert.equal(calls.length, 1, "unapproved manual status makes no request");
prompt.callback({ status: "顺丰客服确认成功待顺丰确认", note: "顺丰客服已反馈拦截成功" });
assert.equal(calls.length, 2);
assert.equal(calls[1].args.status, "顺丰客服确认成功待顺丰确认");
assert.equal("carrier_cancelled" in calls[1].args, false);

const noPermissions = { ...requestForm, events: { sf_interception_state: () => ({}) } };
shipment.sf_verify_carrier_cancellation(noPermissions);
assert.equal(calls.length, 2);
shipment.sf_verify_carrier_cancellation(requestForm);
assert.equal(calls.length, 3);
assert.equal(calls[2].method, "erpnext_shipping.sf_international.interception.verify_carrier_cancellation");

console.log("INTERCEPTION_UI_TEST_OK");
