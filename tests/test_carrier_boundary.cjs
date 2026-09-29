const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
	path.join(__dirname, "../erpnext_shipping/public/js/shipment.js"),
	"utf8",
);

function loadForm({ enabled = true, booked = false } = {}) {
	let handlers;
	const calls = [];
	const buttons = new Map();
	const context = vm.createContext({
		__: (value) => value,
		frappe: {
			ui: {
				form: { on: (doctype, events) => { handlers = events; } },
				Dialog: class {
					fields_dict = { available_services: { $wrapper: { html() {} } } };
					$body = { on() {} };
					show() {}
					hide() {}
				},
			},
			call: (request) => calls.push(request),
			render_template: () => "",
		},
	});
	vm.runInContext(source, context, { filename: "shipment.js" });
	const frm = {
		doc: {
			name: "SHIP-1",
			docstatus: 1,
			service_provider: "LetMeShip",
			shipment_id: booked ? "BOOKING-1" : "",
			shipment_parcel: [{}],
			shipment_delivery_note: [{ delivery_note: "DN-1" }],
		},
		events: { ...handlers, shipping_api_enabled: () => enabled },
		add_custom_button: (label, callback) => buttons.set(label, callback),
	};
	return { frm, context, calls, buttons };
}

for (const booked of [false, true]) {
	test(`native API disabled: no controls or RPC (${booked ? "booked" : "unbooked"})`, () => {
		const { frm, calls, buttons } = loadForm({ enabled: false, booked });
		frm.events.refresh(frm);
		frm.events.fetch_shipping_rates(frm);
		frm.events.print_shipping_label(frm);
		frm.events.update_tracking(frm, frm.doc.service_provider, frm.doc.shipment_id);
		assert.equal(buttons.size, 0);
		assert.equal(calls.length, 0);
	});
}

test("enabled unbooked shipment keeps its shipping rates action", () => {
	const { frm, calls, buttons } = loadForm();
	frm.events.refresh(frm);
	assert.deepEqual([...buttons.keys()], ["Fetch Shipping Rates"]);
	buttons.get("Fetch Shipping Rates")();
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, "erpnext_shipping.erpnext_shipping.shipping.fetch_shipping_rates");
});

test("enabled booked shipment keeps its label and tracking actions", () => {
	const { frm, calls, buttons } = loadForm({ booked: true });
	frm.events.refresh(frm);
	assert.deepEqual([...buttons.keys()], ["Print Shipping Label", "Update Tracking", "Track Status"]);
	buttons.get("Print Shipping Label")();
	buttons.get("Update Tracking")();
	assert.deepEqual(calls.map(({ method }) => method), [
		"erpnext_shipping.erpnext_shipping.shipping.print_shipping_label",
		"erpnext_shipping.erpnext_shipping.shipping.update_tracking",
	]);
	assert.equal(calls[1].args.service_provider, "LetMeShip");
	assert.equal(calls[1].args.shipment_id, "BOOKING-1");
});

test("an open rates dialog cannot book after the native carrier selection disables the API", () => {
	const { frm, context, calls } = loadForm();
	const service = { service_provider: "LetMeShip", is_preferred: true };
	context.show_service_selector(frm, [service]);
	frm.select_row(service);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, "erpnext_shipping.erpnext_shipping.shipping.create_shipment");
	frm.events.shipping_api_enabled = () => false;
	frm.select_row(service);
	assert.equal(calls.length, 1);
});
