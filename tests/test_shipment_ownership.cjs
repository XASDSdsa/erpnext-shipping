const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(`${__dirname}/../erpnext_shipping/public/js/sf_international_shipment.js`, "utf8");

function harness(service_provider) {
	let handlers;
	const calls = [];
	const properties = [];
	const buttons = [];
	const native = {
		set_company_contact() {}, fetch_shipping_rates() {}, print_shipping_label() {},
	};
	const context = {
		__: (value) => value,
		cint: (value) => Number(value) || 0,
		erpnext: { shipment: { carriers: {}, register_carrier(name, carrier) { this.carriers[name] = carrier; } } },
		frappe: {
			_messages: { Product: "Native Product" },
			ui: { form: { on(doctype, events) { assert.equal(doctype, "Shipment"); handlers = events; } } },
			call(options) { calls.push(options); },
		},
		setTimeout() { throw new Error("Carrier lifecycle must not schedule UI overrides"); },
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	const frm = {
		doc: { name: "SHIP-1", docstatus: 0, service_provider, carrier: "SF International" },
		events: { ...native, ...handlers },
		fields_dict: {
			service_provider: {}, status: {}, shipment_id: {}, tracking_status: {},
			sf_actions_html: {}, sf_actions_section: {}, sf_form_json: {}, sf_freight_status: {},
		},
		is_new: () => false,
		set_df_property(...args) { properties.push(args); },
		remove_custom_button(label) { buttons.push(["remove", label]); },
		add_custom_button(label) { buttons.push(["add", label]); },
	};
	return { frm, handlers, native, calls, properties, buttons, context };
}

test("loading SF registers its carrier without changing native handlers, manual events, or global translations", () => {
	const h = harness("DHL");
	for (const [name, handler] of Object.entries(h.native)) assert.equal(h.frm.events[name], handler);
	assert.equal(h.handlers.setup, undefined);
	assert.equal(h.handlers.onload, undefined);
	assert.equal(Object.keys(h.handlers).some((name) => name.startsWith("manual_")), false);
	assert.equal(h.context.frappe._messages.Product, "Native Product");
	assert.equal(Object.keys(h.context.frappe._messages).length, 1);
	assert.equal(h.context.erpnext.shipment.carriers.sf_international.owns_api_ui, true);
});

test("non-SF lifecycle leaves native data, API handlers, layout, and buttons unchanged", () => {
	for (const provider of ["DHL", "SendCloud", "其他物流（手工登记）"]) {
		const h = harness(provider);
		const before = JSON.stringify(h.frm.doc);
		for (const event of ["refresh", "service_provider", "before_save", "validate", "pickup_address_name",
			"delivery_address_name", "shipment_delivery_note_add", "on_hide"]) h.handlers[event](h.frm);
		assert.equal(JSON.stringify(h.frm.doc), before);
		assert.equal(h.calls.length, 0);
		assert.equal(h.buttons.length, 0);
		assert.ok(h.properties.every(([field]) => field.startsWith("sf_")), "SF only changes its own field metadata");
		for (const [name, handler] of Object.entries(h.native)) assert.equal(h.frm.events[name], handler);
	}
});

test("SF carrier aliases work, while an explicit non-SF provider wins over old SF projection fields", () => {
	const h = harness("SF International");
	for (const service_provider of ["SF International", "顺丰国际", "国际顺丰", "SF_GLOBAL"]) {
		h.frm.doc.service_provider = service_provider;
		assert.equal(h.handlers.sf_is_sf(h.frm), true);
	}
	h.frm.doc.service_provider = "";
	assert.equal(h.handlers.sf_is_sf(h.frm), true, "legacy SF carrier is supported without a selected provider");
	h.frm.doc.service_provider = "DHL";
	assert.equal(h.handlers.sf_is_sf(h.frm), false);
});

test("SF print controls call SF actions without replacing the standard shipping methods", () => {
	const h = harness("SF International");
	Object.assign(h.frm.doc, { docstatus: 1, shipment_id: "SF-1", status: "Submitted" });
	h.handlers.sf_replace_tools(h.frm);
	assert.ok(h.buttons.some(([action, label]) => action === "add" && label === "Print Label and Ship"));
	assert.equal(h.frm.events.fetch_shipping_rates, h.native.fetch_shipping_rates);
	assert.equal(h.frm.events.print_shipping_label, h.native.print_shipping_label);
	assert.equal(h.frm.events.set_company_contact, h.native.set_company_contact);
	h.frm.doc.service_provider = "DHL";
	const count = h.buttons.length;
	h.handlers.sf_replace_tools(h.frm);
	assert.equal(h.buttons.length, count);
});
