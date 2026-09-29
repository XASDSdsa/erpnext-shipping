const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(process.argv[2] || `${__dirname}/../erpnext_shipping/public/js/sf_international_shipment.js`, "utf8");

function harness() {
	let events;
	let route = ["Form", "Shipment", "SHIP-1"];
	const requests = [];
	const messages = [];
	const fields = new Map();
	class Field {
		constructor() { this.length = 1; this.value = ""; this.children = []; this.handlers = {}; this.attrs = {}; }
		val(value) { if (value === undefined) return this.value; this.value = value; return this; }
		empty() { this.children = []; return this; }
		append(value) { this.children.push(value); return this; }
		attr(key, value) { this.attrs[key] = value; return this; }
		text(value) { this.content = value; return this; }
		on(event, callback) { this.handlers[event] = callback; return this; }
		change(value) { this.val(value); this.handlers.change?.call(this); }
	}
	const host = {
		length: 1,
		html(value) { this.content = value; },
		empty() { this.content = ""; return this; },
		find(selector) {
			if (!fields.has(selector)) fields.set(selector, new Field());
			return fields.get(selector);
		},
	};
	const context = {
		erpnext: { shipment: { carriers: {}, register_carrier(name, carrier) { this.carriers[name] = carrier; } } },
		console, setTimeout() {}, __: (value) => value,
		cint: (value) => Number(value) || 0,
		$: () => new Field(),
		frappe: {
			_messages: {},
			get_route: () => route,
			ui: { form: { on(doctype, handlers) { events = handlers; } } },
			call(options) {
				const request = {
					options, aborted: false, settled: false,
					abort() {
						if (this.settled) return;
						this.aborted = true;
						this.settled = true;
						this.onAbort?.();
						options.always?.();
					},
					success(message) {
						if (this.settled) return;
						this.settled = true;
						options.callback({ message });
						options.always?.();
					},
					fail(message) {
						if (this.settled) return;
						this.settled = true;
						// Native Frappe displays server messages unless explicitly silenced.
						if (!options.silent) messages.push(message);
						options.always?.();
						options.error?.({ message });
					},
				};
				requests.push(request);
				return request;
			},
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	const frm = {
		doc: { doctype: "Shipment", name: "SHIP-1", service_provider: "SF International" },
		is_new: () => false,
		layout: { wrapper: host }, fields_dict: {},
		events: {
			...events,
			sf_host: () => host,
			sf_prepare_section() {}, sf_replace_tools() {},
			sf_paint_status() {}, sf_add_interception_buttons() {}, sf_add_freight_accounting_buttons() {},
			sf_replacement_edit_guard() {}, sf_transport_html: () => "",
			sf_render_form(frm, data) { frm.rendered = data; },
		},
	};
	return { frm, events, requests, messages, host, field: (selector) => host.find(selector), setRoute(value) { route = value; } };
}

test("leaving the form aborts pending lookups before completion and returning cannot revive them", () => {
	const h = harness();
	h.events.sf_load_form(h.frm);
	const old = h.requests[0];
	old.onAbort = () => assert.equal(Object.keys(h.frm._sf_form_lookups).length, 0);
	h.setRoute(["List", "Shipment", "List"]);
	h.events.on_hide(h.frm);
	assert.equal(old.aborted, true);
	old.fail("upstream timeout");
	assert.deepEqual(h.messages, []);
	h.setRoute(["Form", "Shipment", "SHIP-1"]);
	h.events.sf_load_form(h.frm);
	old.options.callback({ message: { stale: true } });
	assert.equal(h.frm.rendered, undefined);
	h.requests[1].success({ enabled: true });
	assert.equal(h.frm.rendered.enabled, true);
});

test("a late country response after leaving does not start region requests", () => {
	const h = harness();
	h.events.sf_bind_country(h.frm, "d", "US", false);
	h.events.on_hide(h.frm);
	h.requests[0].options.callback({ message: [{ code: "US", name: "United States" }] });
	assert.equal(h.requests.length, 1);
	assert.equal(h.field("[data-sf-d-country]").children.length, 0);
});

test("form refresh, direct re-render, and carrier clear cancel outstanding reads", () => {
	for (const supersede of [
		(h) => h.events.sf_load_form(h.frm),
		(h) => h.events.sf_render_form(h.frm, { enabled: false }),
		(h) => { h.frm.doc.service_provider = "Other"; h.events.sf_clear_form(h.frm); },
	]) {
		const h = harness();
		h.events.sf_load_regions(h.frm, "d", "US", "CA");
		const old = [...h.requests];
		supersede(h);
		assert.ok(old.every((request) => request.aborted));
		old.forEach((request) => request.options.callback({ message: [{ name: "stale" }] }));
		assert.equal(h.field("#sf-d-city-list").children.length, 0);
	}
});

test("captured document identity and route reject a response before native hide/refresh runs", () => {
	for (const change of [
		(h) => { h.frm.doc = { ...h.frm.doc, name: "SHIP-2" }; },
		(h) => { h.frm.doc.name = "SHIP-2"; },
		(h) => h.setRoute(["List", "Shipment", "List"]),
		(h) => { h.frm.doc.service_provider = "Other"; },
	]) {
		const h = harness();
		h.events.sf_load_form(h.frm);
		change(h);
		h.requests[0].success({ stale: true });
		assert.equal(h.frm.rendered, undefined);
	}
});

test("late form work cannot start new lookups on a different page or carrier", () => {
	for (const change of [
		(h) => h.setRoute(["List", "Shipment", "List"]),
		(h) => h.setRoute(["Form", "Shipment", "SHIP-2"]),
		(h) => { h.frm.doc.service_provider = "Other"; },
	]) {
		const h = harness();
		change(h);
		h.events.sf_load_form(h.frm);
		h.events.sf_bind_country(h.frm, "d", "US", false);
		h.events.sf_load_regions(h.frm, "d", "US", "CA");
		assert.equal(h.requests.length, 0);
	}
});

test("sender and receiver countries and regions load independently", () => {
	const h = harness();
	h.field("[data-sf-j-province]").val("Hubei");
	h.field("[data-sf-d-province]").val("CA");
	h.field("[data-sf-d-city]").val("Eureka");
	h.events.sf_bind_country(h.frm, "j", "CN", false);
	h.events.sf_bind_country(h.frm, "d", "US", false);
	h.requests[0].success([{ code: "CN", name: "China" }]);
	assert.equal(h.requests[1].aborted, false);
	h.requests[1].success([{ code: "US", name: "United States" }]);
	assert.equal(h.requests.length, 6);
	for (const request of h.requests.slice(2)) request.success([{ name: request.options.args.region_one_name || "province" }]);
	assert.equal(h.field("[data-sf-j-country]").val(), "CN");
	assert.equal(h.field("[data-sf-d-country]").val(), "US");
	assert.equal(h.field("#sf-j-city-list").children[0].attrs.value, "Hubei");
	assert.equal(h.field("#sf-d-city-list").children[0].attrs.value, "CA");
	assert.equal(h.field("[data-sf-d-city]").val(), "Eureka");
});

test("new country/province selection wins and region options never overwrite typed input", () => {
	const h = harness();
	h.field("[data-sf-d-province]").val("CA");
	h.events.sf_bind_country(h.frm, "d", "US", false);
	h.requests[0].success([{ code: "US", name: "United States" }, { code: "CN", name: "China" }]);
	const old = h.requests.slice(1);
	h.field("[data-sf-d-country]").change("CN");
	assert.ok(old.every((request) => request.aborted));
	old.forEach((request) => request.options.callback({ message: [{ name: "stale" }] }));
	h.field("[data-sf-d-province]").change("Hubei");
	const cities = h.requests.at(-1);
	h.field("[data-sf-d-city]").val("user typed city");
	cities.success([{ name: "Xianning" }]);
	assert.equal(h.field("[data-sf-d-province]").val(), "Hubei");
	assert.equal(h.field("[data-sf-d-city]").val(), "user typed city");
	assert.equal(h.field("#sf-d-city-list").children[0].attrs.value, "Xianning");
	h.field("[data-sf-d-province]").change("Guangdong");
	const pendingCity = h.requests.at(-1);
	h.field("[data-sf-d-province]").change("");
	assert.equal(pendingCity.aborted, true);
	assert.equal(h.field("#sf-d-city-list").children.length, 0);
	h.field("[data-sf-d-country]").change("");
	assert.equal(h.field("#sf-d-province-list").children.length, 0);
	assert.equal(Object.keys(h.frm._sf_form_lookups).length, 0);
});

test("changing country while its list is pending cancels the old selection", () => {
	const h = harness();
	h.events.sf_bind_country(h.frm, "d", "US", false);
	const old = h.requests[0];
	h.field("[data-sf-d-country]").change("CN");
	old.options.callback({ message: [{ code: "US", name: "United States" }] });
	assert.equal(old.aborted, true);
	assert.equal(h.field("[data-sf-d-country]").val(), "CN");
	assert.equal(h.requests.length, 2);
});

test("active upstream/authentication failures remain visible, with no retry or silent option", () => {
	for (const message of ["顺丰服务暂时超时", "顺丰登录状态已失效", "无权访问"]) {
		const h = harness();
		h.events.sf_load_regions(h.frm, "d", "US", "");
		h.requests[0].fail(message);
		assert.deepEqual(h.messages, [message]);
		assert.equal(h.requests.length, 1);
		assert.equal(Object.keys(h.frm._sf_form_lookups).length, 0);
	}
});
