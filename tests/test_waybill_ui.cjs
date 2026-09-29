const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const handlers = {};
const calls = [];
const messages = [];
let prompt;
let confirmation;
const context = {
	erpnext: { shipment: { carriers: {}, register_carrier(name, carrier) { this.carriers[name] = carrier; } } },
	console,
	document: { getElementById() { return null; }, createElement() { return {}; }, head: { appendChild() {} } },
	window: { open() {} },
	cint: (value) => Number(value) || 0,
	flt: (value) => Number(value) || 0,
	__: (value) => value,
	frappe: {
		_messages: {},
		ui: { form: { on(doctype, events) { handlers[doctype] = events; } } },
		utils: { escape_html: (value) => String(value ?? "") },
		urllib: { get_full_url: (value) => value },
		prompt(fields, callback) { prompt = { fields, callback }; },
		confirm(message, callback) { confirmation = { message, callback }; },
		call(options) { calls.push(options); },
		msgprint(value) { messages.push(value); },
	},
};
vm.createContext(context);
const source = fs.readFileSync(`${__dirname}/../erpnext_shipping/public/js/sf_international_shipment.js`, "utf8");
vm.runInContext(source, context, { filename: "shipment.js" });
assert.equal(Object.keys(context.frappe._messages).length, 0, "Shipment extensions do not mutate global translations");

// Frappe loads the owning app's catalog; the form script does not inject translations.
const catalog = fs.readFileSync(`${__dirname}/../erpnext_shipping/translations/zh.csv`, "utf8");
let row = [], value = "", quoted = false;
for (let index = 0; index <= catalog.length; index += 1) {
	const character = catalog[index] || "\n";
	if (character === '"') {
		if (quoted && catalog[index + 1] === '"') { value += '"'; index += 1; }
		else quoted = !quoted;
	} else if (!quoted && (character === "," || character === "\n")) {
		row.push(value.replace(/\r$/, ""));
		value = "";
		if (character === "\n") {
			if (row.length >= 2) context.frappe._messages[row[0]] = row[1];
			row = [];
		}
	} else value += character;
}

const events = handlers.Shipment;
assert.ok(events, "Shipment handlers are registered");

const form = {
	doc: {
		name: "SHIP-1",
		docstatus: 1,
		status: "已发货",
		service_provider: "SF International",
		shipment_id: "OLD-WB",
		awb_number: "OLD-WB",
		sf_active_waybill_record: "ROW-OLD",
		tracking_status: "In Progress",
		sf_form_json: JSON.stringify({ receiver: { contact: "旧收件人" }, product_name: "国际小包" }),
		__onload: {},
	},
	events,
	is_new: () => false,
	reloads: 0,
	renders: 0,
	reload_doc() { this.reloads += 1; },
};

const rows = [
	{ name: "ROW-OLD", waybill: "OLD-WB", replacement_status: "当前", is_active: 1, tracking_status: "In Progress" },
	{ name: "ROW-HISTORY", waybill: "OLDER-WB", replacement_status: "已替换", is_active: 0, tracking_status: "Delivered" },
];
const html = events.sf_waybill_replacement_html(form, rows, false, false);
assert.match(html, /OLD-WB/);
assert.match(html, /OLDER-WB/);
assert.match(html, /data-sf-waybill-action="track"/);
assert.match(html, /data-sf-waybill-record="ROW-HISTORY"/);
assert.match(html, /data-sf-waybill-action="freight"/);
assert.match(html, /data-sf-waybill-action="create-shipped"/);
assert.match(html, /Create new waybill as replacement/);
assert.equal(context.frappe._messages["Create new waybill as replacement"], "创建新运单代替");

for (const status of ["已退回", "已丢失", "Returned", "Lost"]) {
	form.doc.status = status;
	form.doc.tracking_status = "";
	assert.equal(events.sf_waybill_is_shipped(form), true, `${status} uses the shipped replacement flow`);
}
form.doc.status = "已发货";
form.doc.tracking_status = "In Progress";

form.doc.status = "待打单发货";
form.doc.tracking_status = "In Progress";
form.doc.tracking_status_info = "";
assert.equal(events.sf_waybill_is_shipped(form), false, "an empty route lookup does not prove dispatch");
form.doc.tracking_status_info = "已到达分拨中心";
assert.equal(events.sf_waybill_is_shipped(form), true, "an actual route event proves dispatch");
form.doc.status = "已发货";
form.doc.tracking_status = "In Progress";
form.doc.tracking_status_info = "运输中";

const originalForm = { receiver: { contact: "旧收件人", country: "US" }, product_name: "国际小包" };
const correctedForm = { receiver: { contact: "新收件人", country: "GB" }, product_name: "国际标快+" };
const bindings = new Map();
const host = {
	length: 1,
	markup: "",
	html(value) { this.markup = value; },
	find(selector) {
		return { on(event, callback) { bindings.set(`${selector}:${event}`, callback); } };
	},
};
form._sf_data = { enabled: true, form: originalForm };
form.events = {
	...events,
	sf_host: () => host,
	sf_read_form: () => originalForm,
	sf_bind_country() {},
	sf_load_waybill_replacement() {},
	sf_render_form(frm, data) { frm.renders += 1; events.sf_render_form(frm, data); },
};
const originalDoc = JSON.stringify(form.doc);
events.sf_create_waybill_replacement(form, 1);
assert.equal(prompt, undefined, "shipped replacement opens the SF form directly");
assert.equal(confirmation, undefined, "opening the form does not require a preliminary confirmation");
assert.equal(form.renders, 1);
assert.equal(form._sf_replacement_draft.reason, "");
assert.deepEqual(JSON.parse(JSON.stringify(form._sf_replacement_draft.form)), originalForm);
assert.notEqual(form._sf_replacement_draft.form.receiver, originalForm.receiver, "the original receiver is not edited in place");
assert.match(host.markup, /data-sf-replacement-reason/);
assert.match(host.markup, /data-sf-d-contact/);
assert.match(host.markup, /OLD-WB/);
assert.match(host.markup, /Contact SF customer service outside ERP/);
assert.equal(calls.length, 0, "entering replacement edit mode does not create an order");
events.sf_create_waybill_replacement(form, 1);
assert.equal(form.renders, 1, "repeated clicks do not reset the replacement draft");

form.events.sf_read_form = () => correctedForm;
events.sf_submit_waybill_replacement(form);
assert.equal(calls.length, 0, "a missing reason blocks creation");
bindings.get("[data-sf-replacement-reason]:input change").call({ value: "收件地址和产品填写错误" });
assert.equal(form._sf_replacement_draft.reason, "收件地址和产品填写错误");
events.sf_submit_waybill_replacement(form);
assert.equal(calls.length, 1);
assert.equal(calls[0].method, "erpnext_shipping.sf_international.waybill.create_replacement");
assert.equal(calls[0].type, "POST");
assert.equal(calls[0].args.shipped, 1);
assert.equal(calls[0].args.reason, "收件地址和产品填写错误");
assert.deepEqual(JSON.parse(calls[0].args.form_json), correctedForm);
events.sf_submit_waybill_replacement(form);
assert.equal(calls.length, 1, "an in-flight request cannot create a second carrier order");
assert.equal(JSON.stringify(form.doc), originalDoc, "creating a replacement does not overwrite the old Shipment fields");

calls[0].callback({ message: { ok: false, error: "顺丰未返回新运单号" } });
assert.equal(form._sf_replacement_draft, null, "a durable failed attempt exits edit mode");
assert.equal(form.reloads, 1);
assert.match(String(messages.at(-1).message), /failed attempt was kept/);

form._sf_replacement_draft = { shipped: true, reason: "测试换单", form: correctedForm };
context.frappe.validated = true;
events.before_save(form);
assert.equal(context.frappe.validated, false, "native Shipment save is blocked during replacement editing");
form._sf_replacement_draft = null;

form.doc.status = "待打单发货";
form.doc.tracking_status_info = "";
events.sf_create_waybill_replacement(form, 0);
assert.equal(form._sf_replacement_draft, null, "an unshipped old label must be cancelled first");
assert.equal(prompt, undefined);
form.events.sf_waybill_old_cancel_confirmed = () => true;
events.sf_create_waybill_replacement(form, 0);
assert.equal(prompt.fields[0].fieldname, "reason");
prompt.callback({ reason: "收件地址和产品填写错误" });
assert.match(confirmation.message, /exact cancellation confirmation/);
confirmation.callback();
assert.equal(form._sf_replacement_draft.shipped, false);
assert.equal(calls.length, 1, "the unshipped form also waits for explicit submission");
events.sf_cancel_waybill_replacement_edit(form);
assert.equal(form._sf_replacement_draft, null);
assert.equal(form.doc.shipment_id, "OLD-WB");
form.doc.status = "已发货";
form.doc.tracking_status_info = "运输中";

const creatingRows = [{ name: "ROW-CREATING", waybill: "NEW-WB", replacement_status: "创建中", is_active: 0 }];
const creatingHtml = events.sf_waybill_replacement_html(form, creatingRows, false, false);
assert.match(creatingHtml, /Do not submit another request/);
assert.doesNotMatch(creatingHtml, /data-sf-waybill-action="create-shipped"/);

const uncertainRows = [{ name: "ROW-UNCERTAIN", waybill: "", replacement_status: "失败", creation_uncertain: 1, is_active: 0 }];
const uncertainHtml = events.sf_waybill_replacement_html(form, uncertainRows, false, false);
assert.match(uncertainHtml, /No replacement waybill was returned/);
assert.match(uncertainHtml, /data-sf-waybill-action="resolve-failed"/);
assert.doesNotMatch(uncertainHtml, /data-sf-waybill-action="create-shipped"/);

events.sf_track_waybill_record(form, "ROW-HISTORY");
assert.equal(calls.at(-1).method, "erpnext_shipping.sf_international.waybill.fetch_waybill_tracking");
assert.equal(calls.at(-1).args.waybill_record, "ROW-HISTORY");
assert.equal(calls.at(-1).type, "POST");

events.sf_freight_waybill_record(form, "ROW-HISTORY");
assert.equal(calls.at(-1).method, "erpnext_shipping.sf_international.waybill.fetch_waybill_freight");
assert.equal(calls.at(-1).args.waybill_record, "ROW-HISTORY");
assert.equal(calls.at(-1).type, "POST");

// A saved current label has one main action and uses the same PDF from either entry point.
const opened = [];
context.window.open = (url) => opened.push(url);
const labelForm = {
	...form,
	_sf_replacement_draft: null,
	doc: {
		...form.doc,
		sf_label_url: "/private/files/parent-copy.pdf",
		__onload: { sf_waybills: [
			{ ...rows[0], label_url: "/private/files/current-label.pdf" },
			{ ...rows[1], label_url: "/private/files/old-label.pdf" },
		] },
	},
};
assert.equal(events.sf_current_label_url(labelForm), "/private/files/current-label.pdf");
events.sf_render_form(labelForm, { enabled: true, products: [], form: {} });
assert.equal((host.markup.match(/data-sf-open-label="1"/g) || []).length, 1);
assert.doesNotMatch(host.markup, /data-sf-action="print"/);
assert.match(host.markup, /current-label\.pdf/);
const labelHistory = events.sf_waybill_replacement_html(labelForm, events.sf_waybill_records(labelForm), false, false);
assert.doesNotMatch(labelHistory, /current-label\.pdf/);
assert.doesNotMatch(labelHistory, /data-sf-waybill-action="track" data-sf-waybill-record="ROW-OLD"/);
assert.match(labelHistory, /old-label\.pdf/, "historical labels remain accessible");
const previousCalls = calls.length;
events.sf_print(labelForm);
events.sf_print_waybill_record(labelForm, "ROW-OLD");
assert.equal(calls.length, previousCalls, "saved labels open without another print request");
assert.deepEqual(opened, ["/private/files/current-label.pdf", "/private/files/current-label.pdf"]);

const unprintedForm = {
	...labelForm,
	doc: { ...labelForm.doc, sf_label_url: "", __onload: { sf_waybills: [] } },
};
events.sf_render_form(unprintedForm, { enabled: true, products: [], form: {} });
assert.match(host.markup, /data-sf-action="print"/);
assert.doesNotMatch(host.markup, /data-sf-open-label/);
events.sf_print(unprintedForm);
events.sf_print(unprintedForm);
assert.equal(calls.length, previousCalls + 1, "an in-flight label request is not duplicated");
calls.at(-1).always();
assert.equal(unprintedForm._sf_printing, false);

// Every returned route is visible, with its timestamp and safely escaped description.
context.frappe.utils.escape_html = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const trackingEvents = Array.from({ length: 14 }, (_, i) => ({
	time: `2026-09-15 ${String(23 - i).padStart(2, "0")}:00:00`,
	description: i === 13 ? '<img src=x onerror="bad()">最早揽收' : `中转节点 ${i + 1}`,
}));
events.sf_show_tracking_result(labelForm, {
	awb_number: "OLD-WB", tracking_status: "In Progress", tracking_status_info: "最新中转状态",
	tracking_events: trackingEvents,
});
const trackingMessage = messages.at(-1).message;
assert.equal((trackingMessage.match(/<li /g) || []).length, 14);
assert.ok(trackingMessage.includes(trackingEvents[0].time));
assert.ok(trackingMessage.includes(trackingEvents[13].time));
assert.match(trackingMessage, /最早揽收/);
assert.doesNotMatch(trackingMessage, /<img /);
assert.match(trackingMessage, /&lt;img/);

// Saved routes render on first load, independently for each label and without a query.
context.__ = (value, args = []) => (context.frappe._messages[value] || value)
	.replace(/\{(\d+)\}/g, (_, index) => args[index] ?? "");
const savedHistoryForm = {
	...labelForm,
	doc: {
		...labelForm.doc,
		__onload: { sf_waybills: [
			{ ...rows[0], tracking_events: trackingEvents, tracking_queried_at: "2026-09-16 10:00:00" },
			{ ...rows[1], tracking_events: [{ time: "2026-09-10 09:00:00", description: "旧单已签收" }] },
		] },
	},
};
const beforeHistoryRender = calls.length;
const savedHistoryHtml = events.sf_waybill_replacement_html(savedHistoryForm, events.sf_waybill_records(savedHistoryForm), false, false);
assert.equal(calls.length, beforeHistoryRender, "opening saved history does not query the carrier");
assert.equal((savedHistoryHtml.match(/<details /g) || []).length, 2);
assert.doesNotMatch(savedHistoryHtml, /<details[^>]*\sopen(?:\s|>)/, "history starts collapsed");
const currentHistory = savedHistoryHtml.match(/<details[^>]*data-sf-tracking-waybill="OLD-WB"[\s\S]*?<\/details>/)[0];
const olderHistory = savedHistoryHtml.match(/<details[^>]*data-sf-tracking-waybill="OLDER-WB"[\s\S]*?<\/details>/)[0];
assert.match(currentHistory, /物流历史（14 条）/);
assert.match(currentHistory, /最近查询：2026-09-16 10:00:00/);
assert.equal((currentHistory.match(/<li /g) || []).length, 14);
assert.match(currentHistory, /&lt;img/);
assert.doesNotMatch(currentHistory, /<img |旧单已签收/);
assert.match(olderHistory, /物流历史（1 条）/);
assert.match(olderHistory, /旧单已签收/);
assert.doesNotMatch(olderHistory, /中转节点/);
assert.match(events.sf_saved_tracking_history_html(form, rows[0]), /暂无已保存的物流轨迹/);

// Booked pages have no booking controls: reading them must retain the saved defaults.
const preset = {
	product_code: "10", product_name: "国际小包", total_weight: 1.5, parcel_quantity: 2,
	hs_code: "9504200090", ename: "Billiard goods", cname: "台球用品", declared_value: 26, declared_currency: "USD",
	sender: { contact: "仓库联系人", country: "CN", address: "仓库地址" },
	receiver: { contact: "收件人", country: "US", city: "Eureka", address: "收件地址", post_code: "95501" },
};
const presetForm = {
	...form, _sf_replacement_draft: null,
	doc: { ...form.doc, sf_form_json: JSON.stringify({ existing_waybill: "OLD-WB" }) },
	_sf_data: { enabled: true, form: preset, products: [{product_code: "10", product_name: "国际小包"}] },
};
const absentHost = { find() { return {length: 0, val() { return undefined; }}; } };
presetForm.events = { ...form.events, sf_host: () => absentHost };
const retained = events.sf_read_form(presetForm);
assert.deepEqual(JSON.parse(JSON.stringify(retained)), {...preset, existing_waybill: "OLD-WB"});
presetForm.events.sf_host = () => host;
presetForm.events.sf_read_form = () => { throw new Error("Hidden controls must not be read on opening"); };
const docBeforeEdit = JSON.stringify(presetForm.doc);
events.sf_create_waybill_replacement(presetForm, 1);
assert.deepEqual(JSON.parse(JSON.stringify(presetForm._sf_replacement_draft.form)), preset);
assert.ok(host.markup.indexOf('class="sf-waybill-replacement-slot"') < host.markup.indexOf('class="sf-replacement-editor"'));
assert.ok(host.markup.indexOf('class="sf-replacement-editor"') < host.markup.indexOf('data-sf-d-contact'));
assert.match(host.markup, /value="9504200090"/);
assert.match(host.markup, /value="收件人"/);
presetForm._sf_replacement_draft.form.receiver.contact = "修改后的收件人";
assert.equal(preset.receiver.contact, "收件人", "draft edits cannot mutate server defaults");
events.sf_cancel_waybill_replacement_edit(presetForm);
assert.equal(JSON.stringify(presetForm.doc), docBeforeEdit);
assert.doesNotMatch(host.markup, /class="sf-replacement-editor"/);

// Address validation must run before saves/carrier calls, without truncating input.
let addressForm = { sender: { address: "仓库" }, receiver: { address: "路".repeat(61) } };
const addressDoc = {
	doc: { name: "SHIP-ADDRESS", docstatus: 0, service_provider: "SF International" },
	fields_dict: { sf_form_json: {} },
	is_new: () => false,
	events: { ...events, sf_read_form: () => addressForm, sf_apply_existing_waybill() {},
		sf_host: () => ({ find: () => ({ length: 1 }) }) },
};
context.frappe.validated = true;
events.validate(addressDoc);
assert.equal(context.frappe.validated, false);
assert.equal(addressDoc.doc.sf_form_json, undefined);
assert.match(messages.at(-1).message, /61 个字符.*超出 1 个/);
const beforeAddressCalls = calls.length;
events.sf_recreate(addressDoc);
assert.equal(calls.length, beforeAddressCalls, "overlong retry cannot call SF");
addressDoc._sf_replacement_draft = { reason: "修正详细地址", shipped: true };
events.sf_submit_waybill_replacement(addressDoc);
assert.equal(calls.length, beforeAddressCalls, "overlong replacement cannot call SF");
addressDoc._sf_replacement_draft = null;
addressForm.receiver.address = "𠮷".repeat(60);
context.frappe.validated = true;
events.validate(addressDoc);
assert.equal(context.frappe.validated, true, "60 Unicode characters are accepted");
assert.equal(JSON.parse(addressDoc.doc.sf_form_json).receiver.address, addressForm.receiver.address);
addressForm.receiver.address += "A";
context.frappe.validated = true;
events.validate(addressDoc);
assert.equal(context.frappe.validated, false, "61 characters are rejected");
const addressMarkup = events.sf_address_field(addressDoc, "d", addressForm.receiver.address, false);
assert.ok(addressMarkup.includes(addressForm.receiver.address), "prefilled address is never truncated");
assert.match(addressMarkup, /61\/60 个字符/);
assert.match(addressMarkup, /text-danger/);
addressDoc.doc.shipment_id = "EXISTING-WAYBILL";
context.frappe.validated = true;
events.validate(addressDoc);
assert.equal(context.frappe.validated, true, "ordinary updates of booked shipments do not rebook them");

console.log("WAYBILL_UI_TEST_OK");
