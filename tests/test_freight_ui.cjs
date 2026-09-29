const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const handlers = {};
const calls = [];
const confirmations = [];
let prompt;
const escape = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const context = {
	erpnext: { shipment: { carriers: {}, register_carrier(name, carrier) { this.carriers[name] = carrier; } } },
	console,
	document: { getElementById() { return null; }, createElement() { return {}; }, head: { appendChild() {} } },
	window: {},
	cint: (value) => Number(value) || 0,
	flt: (value) => Number(value) || 0,
	format_currency: (value, currency) => `${Number(value).toFixed(2)} ${currency || ""}`.trim(),
	__: (value, args = []) => value.replace(/\{(\d+)\}/g, (_, n) => args[n]),
	frappe: {
		_messages: {},
		listview_settings: {},
		ui: { form: { on(doctype, events) { handlers[doctype] = events; } } },
		utils: { escape_html: escape },
		msgprint() {},
		prompt(fields, callback) { prompt = { fields, callback }; },
		confirm(message, callback) { confirmations.push({ message, callback }); },
		call(options) { calls.push(options); },
	},
};
vm.createContext(context);
for (const file of ["shipment.js", "journal_entry.js"]) {
	vm.runInContext(fs.readFileSync(`${__dirname}/../erpnext_shipping/public/js/${file === "shipment.js" ? "sf_international_shipment.js" : "sf_international_journal_entry.js"}`, "utf8"), context, { filename: file });
}

const historical = {
	service_provider: "顺丰国际",
	shipment_id: "SF-1",
	shipment_amount: 233.84,
	sf_freight_currency: "CNY",
	sf_freight_status: "待核实",
	sf_freight_accounting_status: "记账待处理",
	sf_freight_accounting_hold: 1,
	sf_freight_query_status: "本次未查到账单",
};
const booked = {
	...historical,
	sf_freight_status: "账单已取得",
	sf_freight_accounting_status: "已记账",
	sf_freight_accounting_hold: 0,
};
for (const render of [
	(row) => handlers.Shipment.sf_freight_summary(row),
]) {
	const history = render(historical);
	assert.match(history, /233\.84/);
	assert.match(history, /历史金额（待核实）/);
	assert.match(history, /记账待处理/);
	assert.match(history, /本次未查到账单/);
	assert.doesNotMatch(history, /已结算|未结算|已付款/);
	const confirmed = render(booked);
	assert.match(confirmed, /账单已取得/);
	assert.match(confirmed, /已记账/);
	assert.match(confirmed, /本次未查到账单/);
	assert.doesNotMatch(confirmed, /历史金额|账单待核实|已结算|已付款/);
	const zero = render({ ...booked, shipment_amount: 0, sf_freight_accounting_status: "无需记账" });
	assert.match(zero, /账单已取得/);
	assert.match(zero, /无需记账/);
}

for (const doctype of ["Shipment", "Journal Entry"]) {
	const events = handlers[doctype];
	const buttons = [];
	let reloads = 0;
	const form = {
		doc: {
			name: doctype === "Shipment" ? "S-1" : "J-1",
			docstatus: 2,
			__onload: { sf_freight_accounting: { can_cancel: false, can_resume: true, shipment: "S-1", journal: "J-1", hold: true } },
		},
		events,
		remove_custom_button() {},
		add_custom_button(label, callback) { buttons.push({ label, callback }); },
		reload_doc() { reloads += 1; },
	};
	const addButtons = doctype === "Shipment" ? events.sf_add_freight_accounting_buttons : events.refresh;
	addButtons(form);
	assert.deepEqual(buttons.map((button) => button.label), ["恢复运费记账"], "cancelled shipments retain authorized accounting actions");
	const before = calls.length;
	events.sf_freight_accounting_action(form, "cancel");
	assert.equal(calls.length, before, "no cancel permission means no request");
	events.sf_freight_accounting_action(form, "resume");
	assert.equal(prompt.fields[0].reqd, 1);
	prompt.callback({ reason: "   " });
	assert.equal(calls.length, before, "whitespace-only reason cannot resume accounting");
	prompt.callback({ reason: "  核实账单后重新记账  " });
	assert.equal(calls.length, before, "resume requires explicit confirmation");
	confirmations.at(-1).callback();
	assert.equal(calls.at(-1).method, "erpnext_shipping.sf_international.freight_accounting.resume_freight_accounting");
	assert.equal(calls.at(-1).type, "POST");
	assert.equal(calls.at(-1).args.reason, "核实账单后重新记账");
	calls.at(-1).callback({ exc: "failed" });
	assert.equal(reloads, 0);
	calls.at(-1).callback({ message: {} });
	assert.equal(reloads, 1);

	form.doc.__onload.sf_freight_accounting = { can_cancel: true, can_resume: false, shipment: "S-<1>", journal: "J-<1>" };
	events.sf_freight_accounting_action(form, "cancel");
	prompt.callback({ reason: "误用科目" });
	const confirmation = confirmations.at(-1);
	assert.match(confirmation.message, /S-&lt;1>/);
	assert.match(confirmation.message, /J-&lt;1>/);
	assert.match(confirmation.message, /自动记账将暂停/);
	confirmation.callback();
	assert.equal(calls.at(-1).method, "erpnext_shipping.sf_international.freight_accounting.cancel_freight_journal");
	assert.equal(calls.at(-1).args.shipment, "S-<1>");
	assert.equal(calls.at(-1).args.reason, "误用科目");

	form.doc.__onload.sf_freight_accounting = { can_cancel: "false", shipment: "S-1", journal: "J-1" };
	const promptsBefore = prompt;
	events.sf_freight_accounting_action(form, "cancel");
	assert.equal(prompt, promptsBefore, "only explicit server authorization exposes a financial action");
}

context.frappe.validated = true;
handlers["Journal Entry"].before_cancel({ doc: { sf_freight_shipment: "S-1" } });
assert.equal(context.frappe.validated, false, "native cancellation cannot skip the freight correction flow");
context.frappe.validated = true;
handlers["Journal Entry"].before_cancel({ doc: { __onload: { sf_freight_accounting: { protected: true } } } });
assert.equal(context.frappe.validated, false, "restricted linked-shipment visibility does not remove journal protection");
context.frappe.validated = true;
handlers["Journal Entry"].before_cancel({ doc: {} });
assert.equal(context.frappe.validated, true, "ordinary journals retain native cancellation");

console.log("FREIGHT_UI_TEST_OK");
