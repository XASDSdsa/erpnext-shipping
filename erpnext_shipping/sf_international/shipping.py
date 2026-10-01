# Copyright (c) 2026, Leya
# SF International IUOP merchant / IECS web adapter for ERPNext Shipment.
import json
import re
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, flt, getdate
from frappe.utils.file_manager import save_file

from erpnext.stock.doctype.shipment.shipment import get_company_contact
from erpnext.stock.doctype.shipment.shipment_contents import (
	goods_from_delivery_notes as _goods_from_delivery_notes,
	goods_summary as _goods_summary,
	source_warehouse as _source_warehouse,
	warehouse_from_delivery_note as _warehouse_from_delivery_note,
)
from erpnext_shipping.sf_international import freight_accounting
from erpnext_shipping.sf_international.sf_label_rules import LabelInputError, validate_sf_address

from erpnext_shipping.sf_international.client import (
	cancel_order,
	create_order,
	download_pdf,
	get_settings,
	is_enabled,
	poll_label,
	query_case_orders,
	query_order,
	query_postcode,
	query_route,
	region_cascade,
	start_print,
)

SF_PROVIDER = "SF International"
MANUAL_PROVIDER = "其他物流（手工登记）"
DEFAULT_PRODUCT_CODE = "10"
DEFAULT_PRODUCT_NAME = "国际小包"
DEFAULT_HS_CODE = "9504200090"
DEFAULT_CNAME = "台球体育用品"
DEFAULT_ENAME = "Billiard goods"
DELIVERED_OPCODES = {"80", "8000", "delivered"}
FREIGHT_UNSETTLED = "待核实"
FREIGHT_SETTLED = "账单已取得"
STATUS_WAIT_LABEL = "待打单发货"
STATUS_SHIPPED = "已发货"
STATUS_CANCELLED_SHIP = "已取消发货"
CANCELLED_STATUSES = {"Cancelled", STATUS_CANCELLED_SHIP}
_OFFICIAL_STATUS_FALLBACKS = {
	STATUS_WAIT_LABEL: "Booked",
	STATUS_SHIPPED: "Completed",
	STATUS_CANCELLED_SHIP: "Cancelled",
}
_INITIAL_BOOKING_JOB = "erpnext_shipping.sf_international.shipping.book_sf_order_after_commit"


def _settings_or_none():
	if not is_enabled():
		return None
	return get_settings()



def _customs_presets(settings=None) -> dict:
	settings = settings or _settings_or_none()
	currency = "CNY"
	hs_code = DEFAULT_HS_CODE
	ename = DEFAULT_ENAME
	cname = DEFAULT_CNAME
	declared = 20.0
	if settings:
		currency = (getattr(settings, "declared_currency", None) or "CNY").strip() or "CNY"
		hs_code = (getattr(settings, "default_hs_code", None) or "").strip() or DEFAULT_HS_CODE
		ename = (getattr(settings, "default_ename", None) or "").strip() or DEFAULT_ENAME
		cname = (getattr(settings, "default_cname", None) or "").strip() or DEFAULT_CNAME
		declared = flt(getattr(settings, "default_declared_value", None)) or 20
	return {
		"declared_value": declared,
		"declared_currency": currency,
		"purchase_currency": currency,
		"hs_code": hs_code,
		"ename": ename,
		"cname": cname,
	}


def _is_cancelled(doc) -> bool:
	return cint(getattr(doc, "docstatus", 0)) == 2 or (getattr(doc, "status", None) or "") in {
		"Cancelled", STATUS_CANCELLED_SHIP
	}


def _is_new_doc(doc) -> bool:
	checker = getattr(doc, "is_new", None)
	return bool(checker()) if callable(checker) else False


def _shipment_status_options():
	"""Return the effective Shipment.status Select options when available."""
	try:
		meta = frappe.get_meta("Shipment")
		field = meta.get_field("status") if meta and callable(getattr(meta, "get_field", None)) else None
		options = getattr(field, "options", None) if field else None
		if isinstance(options, str):
			return {line.strip() for line in options.splitlines() if line.strip()}
	except Exception:
		pass
	return set()


def _safe_shipment_status(value):
	"""Keep SF display states valid for the official ERPNext Select field."""
	value = str(value or "").strip()
	options = _shipment_status_options()
	if not options or value in options:
		return value
	return _OFFICIAL_STATUS_FALLBACKS.get(value, value)



def _tracking_implies_shipped(tracking) -> bool:
	"""True when carrier routes show the parcel already left origin.

	Booked / empty routes must not mark shipped.  Used for existing-waybill
	backfill: query logistics after linking a live SF number that was already
	printed and handed over.
	"""
	if not tracking:
		return False
	raw = str(tracking.get("tracking_status") or "").strip()
	info = str(tracking.get("tracking_status_info") or "").strip()
	if raw in {
		"Delivered",
		"Returned",
		"Lost",
		"Shipped",
		"已发货",
		"已签收",
		"已退回",
		"已丢失",
		"已送达",
	}:
		return True
	return raw in {"In Progress", "运输中", "派送中", "已揽收"} and bool(info)


def _maybe_mark_shipped_from_tracking(doc, tracking) -> bool:
	"""Promote waiting/draft shipments to 已发货 when tracking proves dispatch."""
	if not _tracking_implies_shipped(tracking):
		return False
	if _is_cancelled(doc) or (doc.status or "") == STATUS_CANCELLED_SHIP:
		return False
	current = str(doc.status or "").strip()
	if current in {
		STATUS_SHIPPED,
		"Completed",
		"已揽收",
		"运输中",
		"派送中",
		"已签收",
		"已退回",
		"已丢失",
	}:
		return False
	# Only lift from pre-dispatch desk states (incl. existing-waybill draft).
	if current not in {"", "Draft", "草稿", "Booked", STATUS_WAIT_LABEL}:
		return False
	shipped = _safe_shipment_status(STATUS_SHIPPED)
	doc.db_set("status", shipped)
	doc.status = shipped
	return True


def _official_tracking_status(value):
	"""Map carrier-only tracking values to ERPNext's official Select options."""
	value = str(value or "").strip()
	if value == "Booked":
		return ""
	if value in {"", "In Progress", "Delivered", "Returned", "Lost"}:
		return value
	# Keep an unknown future carrier state from making Shipment impossible to save.
	return "In Progress"


def _check_read_permission(doctype, name):
	if not name:
		return
	# Unsaved desk docs use names like new-delivery-note-xxx and are not in the database.
	if str(name).startswith("new-") or not frappe.db.exists(doctype, name):
		frappe.has_permission(doctype, "read", throw=True)
		return
	frappe.has_permission(doctype, "read", doc=name, throw=True)


def _require_active_sf_shipment(doc, *, submitted=False):
	_reject_manual_shipping_api(doc)
	if not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	if _is_cancelled(doc) or doc.status == STATUS_CANCELLED_SHIP:
		frappe.throw(_("Cancelled shipments cannot be dispatched."))
	if submitted and cint(doc.docstatus) != 1:
		frappe.throw(_("Submit the shipment before printing the label and shipping."))


def _checked_delivery_notes(doc, names=None, *, permission="read"):
	linked = set(_delivery_notes_from(doc))
	if isinstance(names, str):
		names = json.loads(names)
	if names is None:
		names = sorted(linked)
	if not isinstance(names, (list, tuple)):
		frappe.throw(_("Delivery Notes must be a list."))
	for name in names:
		if not isinstance(name, str) or name not in linked:
			frappe.throw(_("Delivery Note {0} is not linked to this shipment.").format(name))
		frappe.has_permission("Delivery Note", permission, doc=name, throw=True)
	return list(dict.fromkeys(names))


def validate_shipment_delivery_notes(doc):
	from erpnext.stock.doctype.shipment.shipment_lifecycle import linked_delivery_notes, validate_shipment_links
	from erpnext.stock.doctype.shipment.accounting_validation import validate_delivery_accounting

	validate_shipment_links(doc)
	names = linked_delivery_notes(doc)
	if cint(doc.docstatus) != 2:
		for name in names:
			validate_delivery_accounting(name, frappe.db.get_value("Delivery Note", name, "company"))
	return names




def _require_shipping_mutation(doc):
	if doc.get("sf_intercept_status") or doc.get("sf_carrier_cancelled"):
		frappe.throw("运单已有拦截或取消记录，不能继续发货或重新创建面单。")


def _has_field(doctype: str, fieldname: str) -> bool:
	return frappe.get_meta(doctype).has_field(fieldname)


def _country_code(country_name: str) -> str:
	code = frappe.db.get_value("Country", country_name, "code")
	if not code:
		frappe.throw(_("Country Code not found for {0}").format(country_name))
	return str(code).strip().upper()


def _sf_address(address_name: str, label="详细地址") -> dict:
	_check_read_permission("Address", address_name)
	address = frappe.db.get_value(
		"Address",
		address_name,
		[
			"address_title",
			"address_line1",
			"address_line2",
			"city",
			"state",
			"pincode",
			"country",
			"email_id",
		],
		as_dict=1,
	)
	if not address:
		frappe.throw(_("Address {0} not found.").format(address_name))
	if not address.country:
		frappe.throw(_("Please add a valid country in Address {0}.").format(address.address_title))
	line = " ".join(p for p in [address.address_line1, address.address_line2] if p).strip()
	if not line:
		frappe.throw(_("Please add a street address in Address {0}.").format(address.address_title))
	city = (address.city or "").strip()
	state = (address.state or city).strip()
	if not city:
		frappe.throw(_("Please add a city in Address {0}.").format(address.address_title))
	return {
		"company": address.address_title or "",
		"country": _country_code(address.country),
		"postCode": str(address.pincode).replace(" ", ""),
		"regionFirst": state,
		"regionSecond": city,
		"address": _validated_sf_address(line, label),
		"email": address.email_id or "",
	}


def _clean_phone(value) -> str:
	return re.sub(r"[\s\-()]", "", str(value or "").strip())


def _international_phone(value) -> str:
	phone = _clean_phone(value)
	if not phone:
		return ""
	if phone.startswith("+"):
		return phone
	if phone.startswith("00"):
		return "+" + phone[2:]
	digits = "".join(c for c in phone if c.isdigit())
	if not digits:
		return ""
	if digits.startswith("86"):
		return "+" + digits
	if len(digits) == 11 and digits.startswith("1"):
		return "+86" + digits
	return "+86" + digits


def _pickup_origin_phone(doc) -> str:
	address_name = str(getattr(doc, "pickup_address_name", None) or "").strip()
	warehouse = None
	for dn_name in _delivery_notes_from(doc):
		if not frappe.db.exists("Delivery Note", dn_name):
			continue
		dn = frappe.get_doc("Delivery Note", dn_name)
		warehouse = warehouse or _warehouse_from_delivery_note(dn)
		if not address_name:
			address_name = _dn_value(dn, "dispatch_address_name", "company_address") or ""
	if not address_name and warehouse:
		address_name = _address_for_warehouse(warehouse) or ""
	if address_name:
		phone = _international_phone(frappe.db.get_value("Address", address_name, "phone"))
		if phone:
			return phone
	if warehouse:
		phone = _international_phone(frappe.db.get_value("Warehouse", warehouse, "phone_no"))
		if phone:
			return phone
	company = getattr(doc, "pickup_company", None) or ""
	if company:
		for name in _linked_addresses("Company", company) or []:
			row = frappe.db.get_value(
				"Address",
				name,
				["phone", "address_type", "is_shipping_address"],
				as_dict=1,
			)
			if not row:
				continue
			phone = _international_phone(row.phone)
			if phone and ((row.address_type or "") == "Warehouse" or cint(row.is_shipping_address)):
				return phone
		for name in _linked_addresses("Company", company) or []:
			phone = _international_phone(frappe.db.get_value("Address", name, "phone"))
			if phone:
				return phone
		for name in frappe.get_all("Warehouse", filters={"company": company, "disabled": 0}, pluck="name"):
			phone = _international_phone(frappe.db.get_value("Warehouse", name, "phone_no"))
			if phone:
				return phone
	return ""


def validate_phone(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	if _pickup_origin_phone(doc):
		return
	if (getattr(doc, "pickup_from_type", None) or "") != "Company" and doc.get("pickup_contact_name"):
		raw = frappe.db.get_value("Contact", doc.pickup_contact_name, "phone") or frappe.db.get_value(
			"Contact", doc.pickup_contact_name, "mobile_no"
		)
		if _international_phone(raw):
			return
	frappe.throw("请在发货仓库或提货地址上填写电话，不要使用登录用户档案里的电话。")


def _phone_from_text(text) -> str:
	found = re.findall(r"\+?\d[\d\s\-()]{6,}\d", str(text or ""))
	if not found:
		return ""
	return _clean_phone(max(found, key=len))


def _phone_from_contact_name(contact_name) -> str:
	if not contact_name:
		return ""
	_check_read_permission("Contact", contact_name)
	row = frappe.db.get_value("Contact", contact_name, ["phone", "mobile_no"], as_dict=1) or {}
	phone = _clean_phone(row.get("phone") or row.get("mobile_no"))
	if phone:
		return phone
	if not frappe.db.exists("DocType", "Contact Phone"):
		return ""
	rows = frappe.get_all(
		"Contact Phone",
		filters={"parent": contact_name, "parenttype": "Contact"},
		fields=["phone", "is_primary_phone", "is_primary_mobile_no"],
		limit=20,
	)
	rows.sort(key=lambda item: (not item.get("is_primary_mobile_no"), not item.get("is_primary_phone")))
	for item in rows:
		phone = _clean_phone(item.get("phone"))
		if phone:
			return phone
	return ""


def _phone_from_customer(customer) -> str:
	if not customer or not frappe.db.exists("Customer", customer):
		return ""
	_check_read_permission("Customer", customer)
	phone = _clean_phone(frappe.db.get_value("Customer", customer, "mobile_no"))
	if phone:
		return phone
	links = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Contact", "link_doctype": "Customer", "link_name": customer},
		pluck="parent",
		limit=5,
	)
	for name in links:
		phone = _phone_from_contact_name(name)
		if phone:
			return phone
	return ""


def _phone_number(contact) -> str:
	if not contact:
		return ""
	phone = _clean_phone(contact.get("phone") or contact.get("mobile_no"))
	if phone:
		return phone
	return _phone_from_contact_name(contact.get("name"))


def _fill_receiver_gaps(receiver, delivery_to=None, delivery_contact_name=None, delivery_contact=None):
	receiver = dict(receiver or {})
	if not (receiver.get("contact") or "").strip():
		receiver["contact"] = (delivery_to or "").strip() or (receiver.get("company") or "").strip()
	if not (receiver.get("phone") or "").strip():
		phone = (
			_phone_from_contact_name(delivery_contact_name)
			or _phone_from_text(delivery_contact)
			or _phone_from_customer(delivery_to)
			or _clean_phone(receiver.get("mobile"))
		)
		if phone:
			receiver["phone"] = phone
			if not (receiver.get("mobile") or "").strip():
				receiver["mobile"] = phone
	return receiver


def _contact_name(contact) -> str:
	return f"{contact.first_name or ''} {contact.last_name or ''}".strip()[:100]


def _pickup_contact(pickup_from_type, pickup_contact_name):
	if pickup_from_type != "Company":
		return _get_contact(pickup_contact_name)
	contact = get_company_contact(user=pickup_contact_name)
	contact.email_id = contact.pop("email", None)
	contact.setdefault("first_name", contact.get("full_name") or "")
	contact.setdefault("last_name", "")
	return contact


def _get_contact(contact_name):
	if not contact_name:
		frappe.throw(_("Contact name is required for SF International."))
	_check_read_permission("Contact", contact_name)
	contact = frappe.db.get_value(
		"Contact",
		contact_name,
		["first_name", "last_name", "email_id", "phone", "mobile_no"],
		as_dict=1,
	)
	if not contact:
		frappe.throw(_("Contact {0} not found.").format(contact_name))
	if not (contact.first_name or "").strip() and not (contact.last_name or "").strip():
		frappe.throw(_("Contact name is required for SF International."))
	if not contact.phone:
		contact.phone = contact.mobile_no
	return contact


def _parcel_totals(parcels: list[dict]) -> dict:
	qty = 0
	weight = 0.0
	length = width = height = 0.0
	for parcel in parcels:
		count = cint(parcel.get("count") or 1)
		qty += count
		weight += flt(parcel.get("weight")) * count
		length = max(length, flt(parcel.get("length")))
		width = max(width, flt(parcel.get("width")))
		height = max(height, flt(parcel.get("height")))
	return {
		"parcelQuantity": max(qty, 1),
		"parcelTotalWeight": round(weight, 3) or 0.1,
		"parcelTotalLength": round(length, 2) or None,
		"parcelTotalWidth": round(width, 2) or None,
		"parcelTotalHeight": round(height, 2) or None,
	}


def _parse_parcels(parcels):
	if isinstance(parcels, str):
		return json.loads(parcels)
	return parcels or []


def _require_positive_parcels(parcels: list[dict]) -> list[dict]:
	if not parcels:
		frappe.throw(_("Please add parcel dimensions and weight before creating an SF International shipment."))
	clean = []
	for row in parcels:
		item = {
			"length": flt(row.get("length")),
			"width": flt(row.get("width")),
			"height": flt(row.get("height")),
			"weight": flt(row.get("weight")),
			"count": cint(row.get("count") or 1),
		}
		if item["length"] <= 0 or item["width"] <= 0 or item["height"] <= 0 or item["weight"] <= 0:
			frappe.throw(_("Parcel length, width, height and weight must be greater than zero."))
		if item["count"] < 1:
			item["count"] = 1
		clean.append(item)
	return clean


@frappe.whitelist()
def save_shipment_parcels(shipment, parcels):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_require_shipping_mutation(doc)
	if _is_cancelled(doc):
		frappe.throw(_("Cancelled shipments cannot be changed."))
	if doc.shipment_id:
		frappe.throw(_("Cannot change parcels after the carrier booking exists."))
	rows = _require_positive_parcels(_parse_parcels(parcels))
	doc.set("shipment_parcel", [])
	total = 0
	for row in rows:
		doc.append("shipment_parcel", row)
		total += row["weight"] * row["count"]
	doc.total_weight = total
	doc.flags.ignore_validate_update_after_submit = True
	doc.save()
	return {"total_weight": total, "parcels": rows}








def _item_names(item_name: str) -> tuple[str, str]:
	name = (item_name or "").strip()
	if not name:
		return DEFAULT_ENAME, DEFAULT_CNAME
	if any("\u4e00" <= ch <= "\u9fff" for ch in name):
		return DEFAULT_ENAME, name[:100]
	return name[:100], DEFAULT_CNAME


def _hscode_items(shipment_name: str | None, description: str, value_of_goods, settings, totals, hs_code=None) -> list[dict]:
	hs_code = (hs_code or settings.default_hs_code or "").strip() or DEFAULT_HS_CODE
	declared = flt(value_of_goods) or 20
	items = []
	if shipment_name:
		doc = frappe.get_doc("Shipment", shipment_name)
		for row in doc.get("shipment_delivery_note") or []:
			if not row.delivery_note:
				continue
			dn = frappe.get_doc("Delivery Note", row.delivery_note)
			for item in dn.items:
				ename, cname = _item_names(item.item_name or item.item_code or description)
				qty = flt(item.qty) or 1
				amount = flt(item.net_rate) or flt(item.rate) or declared
				weight = flt(item.total_weight) or flt(item.weight_per_unit) or totals["parcelTotalWeight"]
				items.append(
					{
						"ename": ename,
						"cname": cname,
						"parcelQuantity": str(int(qty) if qty == int(qty) else qty),
						"declaredValue": str(round(amount, 2)),
						"totalPrice": f"{round(amount * qty, 2):.2f}",
						"hsCode": hs_code,
						"weight": round(weight, 3) or 0.1,
					}
				)
	if not items:
		ename, cname = _item_names(description)
		items.append(
			{
				"ename": ename,
				"cname": cname,
				"parcelQuantity": str(totals["parcelQuantity"]),
				"declaredValue": str(round(declared, 2)),
				"totalPrice": f"{round(declared, 2):.2f}",
				"hsCode": hs_code,
				"weight": totals["parcelTotalWeight"],
			}
		)
	return items


def _product_choices(settings) -> list[dict]:
	from .products import IECS_PRODUCTS

	choices = []
	seen = set()
	for row in settings.products or []:
		code = str(row.product_code or "").strip()
		if not code or code.upper().startswith("INT"):
			continue
		choices.append(
			{
				"product_code": code,
				"product_name": (row.product_name or code).strip(),
				"is_preferred": cint(row.is_preferred),
			}
		)
		seen.add(code)
	for code, name, preferred in IECS_PRODUCTS:
		if code in seen:
			continue
		choices.append(
			{
				"product_code": str(code),
				"product_name": name,
				"is_preferred": 0 if seen else cint(preferred),
			}
		)
		seen.add(code)
	if choices and not any(row["is_preferred"] for row in choices):
		for row in choices:
			if row["product_code"] == DEFAULT_PRODUCT_CODE or row["product_name"] == DEFAULT_PRODUCT_NAME:
				row["is_preferred"] = 1
				break
	return choices


def _resolve_product(settings, service_info) -> tuple[str, str]:
	choices = _product_choices(settings)
	by_code = {row["product_code"]: row for row in choices}
	by_name = {row["product_name"]: row for row in choices}
	raw_code = str(
		(service_info or {}).get("service_id")
		or (service_info or {}).get("carrier_service")
		or ""
	).strip()
	raw_name = str((service_info or {}).get("service_name") or "").strip()
	if raw_code.upper().startswith("INT"):
		frappe.throw(_("This product code is from the old Open API. Choose an IUOP product such as 国际小包."))
	if raw_code in by_code:
		row = by_code[raw_code]
		return row["product_code"], row["product_name"]
	if raw_name in by_name:
		row = by_name[raw_name]
		return row["product_code"], row["product_name"]
	preferred = next((row for row in choices if row["is_preferred"]), None)
	if preferred:
		return preferred["product_code"], preferred["product_name"]
	if "10" in by_code:
		return by_code["10"]["product_code"], by_code["10"]["product_name"]
	return DEFAULT_PRODUCT_CODE, DEFAULT_PRODUCT_NAME


@frappe.whitelist()
def get_booking_options(shipment=None):
	if shipment:
		_check_read_permission("Shipment", shipment)
	else:
		frappe.has_permission("Shipment", "read", throw=True)
	settings = _settings_or_none()
	if not settings:
		return {
			"enabled": False,
			"products": [],
			"has_submitted_delivery_note": False,
			"delivery_notes": [],
			"goods": [],
			"source_warehouse": "",
			"shipment_id": None,
		}
	delivery_notes = []
	shipment_id = None
	has_dn = False
	remembered = False
	products = _product_choices(settings)
	if shipment and frappe.db.exists("Shipment", shipment):
		doc = frappe.get_doc("Shipment", shipment)
		has_dn = _has_submitted_delivery_note(doc)
		shipment_id = doc.shipment_id or doc.awb_number
		delivery_notes = [row.delivery_note for row in doc.get("shipment_delivery_note") or [] if row.delivery_note]
		saved_form = _parse_form_json(doc.get("sf_form_json") if _has_field("Shipment", "sf_form_json") else None)
		form = _booking_form_with_defaults(
			_defaults_from_shipment(doc, settings, remember_receiver=False), saved_form, products
		)
		form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.pickup_address_name)
		if not shipment_id:
			form.update(_customs_presets(settings))
			form["receiver"], remembered = _apply_saved_sf_receiver(
				form.get("receiver") or {},
				doc.delivery_address_name,
				remember=False,
			)
			form["receiver"] = _fill_receiver_gaps(
				form.get("receiver"),
				delivery_to=doc.delivery_to,
				delivery_contact_name=doc.delivery_contact_name,
				delivery_contact=doc.delivery_contact,
			)
	else:
		form = {}
	goods = _goods_from_delivery_notes(delivery_notes)
	return {
		"enabled": True,
		"products": products,
		"has_submitted_delivery_note": has_dn,
		"delivery_notes": delivery_notes,
		"goods": goods,
		"source_warehouse": _source_warehouse(goods),
		"shipment_id": shipment_id,
		"form": form,
		"declared_currency": (settings.declared_currency or "CNY").strip() or "CNY",
		"hs_code": (settings.default_hs_code or "").strip() or DEFAULT_HS_CODE,
		"sender_preset": True,
		"receiver_remembered": remembered,
	}


def _booking_form_with_defaults(defaults, saved, products):
	"""Fill missing form values without replacing an existing booking's choices."""
	def meaningful(value):
		return value is not None and (not isinstance(value, str) or bool(value.strip()))

	form = dict(defaults)
	for key, value in saved.items():
		if key in {"sender", "receiver"} and isinstance(value, dict):
			form[key] = dict(defaults.get(key) or {})
			form[key].update({field: item for field, item in value.items() if meaningful(item)})
		elif meaningful(value):
			form[key] = value

	# A saved name alone must not acquire a different product's default code.
	code = str(saved.get("product_code") or "").strip()
	name = str(saved.get("product_name") or "").strip()
	if code or name:
		selected = next((row for row in products if row["product_code"] == code), None)
		selected = selected or next((row for row in products if row["product_name"] == name), None)
		form["product_code"] = selected["product_code"] if selected else code
		form["product_name"] = selected["product_name"] if selected else name
	return form


def _parse_form_json(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	if not raw:
		return {}
	try:
		data = json.loads(raw)
	except Exception:
		return {}
	return data if isinstance(data, dict) else {}


def _linked_addresses(doctype: str, name: str) -> list[str]:
	if not doctype or not name:
		return []
	_check_read_permission(doctype, name)
	return frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": doctype, "link_name": name},
		pluck="parent",
	)


def _pick_address(names: list[str]):
	if not names:
		return None
	rows = frappe.get_list(
		"Address",
		filters={"name": ["in", names], "disabled": 0},
		fields=["name", "is_shipping_address", "is_primary_address", "address_type"],
		limit_page_length=0,
	)
	if not rows:
		return None
	rows.sort(
		key=lambda row: (
			0 if (row.address_type or "") == "Warehouse" else 1,
			0 if cint(row.is_shipping_address) else 1,
			0 if cint(row.is_primary_address) else 1,
		)
	)
	return rows[0].name


def _delivery_notes_from(doc=None, delivery_notes=None) -> list[str]:
	names = []
	for name in delivery_notes or []:
		if name and name not in names:
			names.append(name)
	if doc:
		for row in doc.get("shipment_delivery_note") or []:
			name = row.delivery_note
			if name and name not in names:
				names.append(name)
	return names




def _address_for_warehouse(warehouse: str | None) -> str | None:
	if not warehouse:
		return None
	linked = _pick_address(_linked_addresses("Warehouse", warehouse))
	if linked:
		return linked
	short = warehouse.split(" - ")[0].strip()
	filters = {"disabled": 0, "address_type": "Warehouse"}
	rows = frappe.get_all(
		"Address",
		filters=filters,
		fields=["name", "address_title"],
		limit=50,
	)
	for row in rows:
		title = (row.address_title or "").strip()
		if title == warehouse or title == short or short and short in (row.name or ""):
			return row.name
	if short:
		exact = frappe.db.get_value("Address", {"address_title": short, "disabled": 0}, "name")
		if exact:
			return exact
	return None


def _company_display(company: str | None) -> str:
	if not company:
		return ""
	return (frappe.db.get_value("Company", company, "company_name") or company or "").strip()


def _first_warehouse_address_with_pincode() -> str | None:
	rows = frappe.get_all(
		"Address",
		filters={"disabled": 0, "address_type": "Warehouse"},
		fields=["name", "pincode", "is_shipping_address"],
		limit=50,
	)
	rows.sort(key=lambda row: (0 if cint(row.is_shipping_address) else 1))
	for row in rows:
		if str(row.pincode or "").strip():
			return row.name
	return None


def _sender_from_warehouse(doc=None, delivery_notes=None, pickup_address_name=None, pickup_contact=None) -> dict:
	company = ""
	warehouse = None
	explicit = (pickup_address_name or (doc.pickup_address_name if doc else None) or "").strip() or None
	for dn_name in _delivery_notes_from(doc, delivery_notes):
		_check_read_permission("Delivery Note", dn_name)
		if not frappe.db.exists("Delivery Note", dn_name):
			continue
		dn = frappe.get_doc("Delivery Note", dn_name)
		company = company or dn.company
		warehouse = warehouse or _warehouse_from_delivery_note(dn)
	if doc and not company:
		company = getattr(doc, "pickup_company", None) or getattr(doc, "company", None) or ""
	address_name = explicit
	if not address_name:
		address_name = _address_for_warehouse(warehouse) if warehouse else None
	if not address_name:
		for dn_name in _delivery_notes_from(doc, delivery_notes):
			if not frappe.db.exists("Delivery Note", dn_name):
				continue
			dn = frappe.get_doc("Delivery Note", dn_name)
			address_name = _dn_value(dn, "dispatch_address_name", "company_address")
			if address_name:
				break
	if not address_name and company:
		address_name = _pick_address(_linked_addresses("Company", company))
	if not address_name:
		address_name = _first_warehouse_address_with_pincode()
	sender = _party_from_address(address_name, None, pickup_contact)
	settings = _settings_or_none()
	preset_company = (settings.sender_company or "").strip() if settings else ""
	preset_email = (settings.sender_email or "").strip() if settings else ""
	sender["company"] = preset_company or _company_display(company) or sender.get("company") or ""
	if preset_email:
		sender["email"] = preset_email
	if warehouse:
		sender["warehouse"] = warehouse
	sender["address_name"] = address_name or ""
	if not sender.get("phone"):
		if address_name:
			sender["phone"] = _international_phone(frappe.db.get_value("Address", address_name, "phone"))
		if not sender.get("phone") and warehouse:
			sender["phone"] = _international_phone(frappe.db.get_value("Warehouse", warehouse, "phone_no"))
	if not sender.get("contact"):
		sender["contact"] = sender.get("company") or ""
	if not sender.get("mobile"):
		sender["mobile"] = sender.get("phone") or ""
	if not sender.get("country"):
		sender["country"] = "CN"
	return sender


def _party_from_address(address_name, contact_name=None, company_contact=None) -> dict:
	addr = _address_row(address_name) if address_name else None
	contact = _contact_row(contact_name) if contact_name else None
	if not contact and company_contact:
		contact = company_contact
	country = ""
	if addr and addr.country:
		try:
			country = _country_code(addr.country)
		except Exception:
			country = ""
	phone = _phone_number(contact) if contact else ""
	if not phone and addr:
		phone = _clean_phone(addr.phone)
	line = ""
	if addr:
		line = " ".join(p for p in [addr.address_line1, addr.address_line2] if p).strip()
	person = _contact_name(contact) if contact else ""
	if not person:
		person = ((addr.address_title if addr else "") or "").strip()
	return {
		"company": (addr.address_title if addr else "") or "",
		"contact": person,
		"phone": phone,
		"mobile": phone,
		"email": (contact.email_id if contact else None) or (addr.email_id if addr else "") or "",
		"country": country,
		"province": (addr.state if addr else "") or "",
		"city": (addr.city if addr else "") or "",
		"county": "",
		"post_code": str(addr.pincode).replace(" ", "") if addr and addr.pincode else "",
		"address": line,
		"doorplate": "",
	}


SF_RECEIVER_FIELDS = (
	("sf_country", "country"),
	("sf_province", "province"),
	("sf_city", "city"),
	("sf_county", "county"),
	("sf_post_code", "post_code"),
	("sf_doorplate", "doorplate"),
	("sf_address", "address"),
)


def _receiver_region_snapshot(receiver):
	"""Remember geography only; recipient identity always comes from current masters."""
	if not isinstance(receiver, dict):
		return {}
	return {key: str(receiver.get(key) or "").strip() for _field, key in SF_RECEIVER_FIELDS}


def _receiver_destination_key(receiver):
	return (
		str(receiver.get("country") or "").strip().upper(),
		re.sub(r"[\s-]", "", str(receiver.get("post_code") or "")).upper(),
		" ".join(str(receiver.get("address") or "").split()).casefold(),
	)


def _receiver_matches_address(address, receiver):
	if not address or not receiver:
		return False
	country = str(address.get("country") or "").strip()
	if not country:
		return False
	country = _country_code(country)
	street = " ".join(str(address.get(key) or "").strip() for key in ("address_line1", "address_line2")).strip()
	return bool(
		street
		and str(receiver.get("province") or "").strip()
		and str(receiver.get("city") or "").strip()
		and _receiver_destination_key(receiver) == _receiver_destination_key({
			"country": country, "post_code": address.get("pincode"), "address": street,
		})
	)


def _saved_sf_receiver(address_name):
	if not address_name or not _has_field("Address", "sf_province"):
		return None
	address = _address_row(address_name)
	row = frappe.db.get_value(
		"Address", address_name, [field for field, _key in SF_RECEIVER_FIELDS], as_dict=True,
	)
	if not row:
		return None
	saved = {key: str(row.get(field) or "").strip() for field, key in SF_RECEIVER_FIELDS}
	return saved if _receiver_matches_address(address, saved) else None


def _last_sf_receiver_for_address(address_name):
	if not address_name or not _has_field("Shipment", "sf_form_json"):
		return None
	address = _address_row(address_name)
	if not address or not frappe.has_permission("Shipment", "read"):
		return None
	fields = ["name", "delivery_address_name", "service_provider", "carrier", "docstatus", "status", "shipment_id", "awb_number", "sf_form_json"]
	fields += [field for field in ("sf_carrier_cancelled", "sf_waybill_replacement_status") if _has_field("Shipment", field)]
	providers = ["SF International", "顺丰国际"]
	rows = frappe.get_list(
		"Shipment",
		filters={"delivery_address_name": address_name, "docstatus": 1},
		or_filters={"service_provider": ["in", providers], "carrier": ["in", providers]},
		fields=fields,
		order_by="modified desc",
		limit_page_length=20,
	)
	for row in rows:
		# An entered draft or an unconfirmed candidate is not a successful booking.
		if (
			row.get("delivery_address_name") != address_name
			or cint(row.get("docstatus")) != 1
			or not (_is_sf_provider(row.get("service_provider")) or _is_sf_provider(row.get("carrier")))
			or str(row.get("status") or "").strip() in CANCELLED_STATUSES | {"已取消", "Canceled"}
			or cint(row.get("sf_carrier_cancelled"))
			or not str(row.get("shipment_id") or row.get("awb_number") or "").strip()
			or str(row.get("sf_waybill_replacement_status") or "").strip() not in {"", "当前"}
			or not frappe.has_permission("Shipment", "read", doc=row.get("name"))
		):
			continue
		receiver = _receiver_region_snapshot((_parse_form_json(row.get("sf_form_json")) or {}).get("receiver"))
		if _receiver_matches_address(address, receiver):
			return receiver
	return None


def _remember_sf_receiver(address_name, receiver):
	if not address_name or not receiver or not _has_field("Address", "sf_province"):
		return False
	if not frappe.has_permission("Address", "write", doc=address_name):
		return False
	address = _address_row(address_name)
	receiver = _receiver_region_snapshot(receiver)
	if not _receiver_matches_address(address, receiver):
		return False
	# Include empty optional values so an old district/doorplate cannot survive a new selection.
	updates = {
		field: receiver[key] if field == "sf_address" else receiver[key][:140]
		for field, key in SF_RECEIVER_FIELDS
	}
	frappe.db.set_value("Address", address_name, updates, update_modified=False)
	return True


def _remember_successful_sf_receiver(doc, receiver):
	"""Best-effort cache update; never undo or misreport a successful carrier order."""
	savepoint = "sf_receiver_memory"
	created_savepoint = False
	try:
		frappe.db.savepoint(savepoint)
		created_savepoint = True
		return _remember_sf_receiver(doc.get("delivery_address_name"), receiver)
	except Exception as exc:
		if created_savepoint:
			try:
				frappe.db.rollback(save_point=savepoint)
			except Exception:
				pass
		try:
			frappe.log_error(
				title="SF receiver address memory",
				message=f"Address region cache was not updated for Shipment {doc.get('name')}: {type(exc).__name__}",
			)
		except Exception:
			pass
		return False


def _apply_saved_sf_receiver(party, address_name, *, remember=True):
	# ``remember`` remains a compatibility argument. Reading defaults never writes a master.
	party = dict(party or {})
	saved = _saved_sf_receiver(address_name) or _last_sf_receiver_for_address(address_name)
	if not saved:
		return party, False
	# A form may deliberately target another destination without editing Address.
	# Never replace that destination with the linked Address's remembered region.
	for key in ("country", "post_code", "address"):
		if key in party and key in saved and _receiver_destination_key({key: party[key]}) != _receiver_destination_key({key: saved[key]}):
			return party, False
	for _field, key in SF_RECEIVER_FIELDS:
		if key in saved:
			party[key] = str(saved[key] or "").strip()
	return party, True


def _defaults_from_shipment(doc, settings, *, remember_receiver=True) -> dict:
	pickup_contact = None
	try:
		if doc.pickup_contact_name:
			pickup_contact = _pickup_contact(doc.pickup_from_type, doc.pickup_contact_name)
	except Exception:
		pickup_contact = None
	sender = _sender_from_warehouse(doc=doc, pickup_address_name=doc.pickup_address_name, pickup_contact=pickup_contact)
	receiver, _remembered = _apply_saved_sf_receiver(
		_party_from_address(doc.delivery_address_name, doc.delivery_contact_name),
		doc.delivery_address_name,
		remember=remember_receiver,
	)
	receiver = _fill_receiver_gaps(
		receiver,
		delivery_to=getattr(doc, "delivery_to", None),
		delivery_contact_name=getattr(doc, "delivery_contact_name", None),
		delivery_contact=getattr(doc, "delivery_contact", None),
	)
	weight = flt(doc.total_weight)
	if weight <= 0:
		weight = _parcel_totals(_parcel_payload(doc)).get("parcelTotalWeight") or 0.1
	form = {
		"product_code": DEFAULT_PRODUCT_CODE,
		"product_name": DEFAULT_PRODUCT_NAME,
		"total_weight": weight or 0.1,
		"parcel_quantity": 1,
		"sender": sender,
		"receiver": receiver,
	}
	form.update(_customs_presets(settings))
	return form


@frappe.whitelist()
def get_sf_form_defaults(
	pickup_from_type=None,
	pickup_address_name=None,
	delivery_address_name=None,
	pickup_contact_name=None,
	delivery_contact_name=None,
	delivery_contact=None,
	delivery_to=None,
	value_of_goods=None,
	total_weight=None,
	delivery_notes=None,
):
	frappe.has_permission("Shipment", "read", throw=True)
	settings = _settings_or_none()
	if not settings:
		return {"enabled": False, "products": [], "form": {}}
	if isinstance(delivery_notes, str):
		delivery_notes = json.loads(delivery_notes)
	for name in delivery_notes or []:
		_check_read_permission("Delivery Note", name)
	pickup_contact = None
	try:
		if pickup_contact_name:
			pickup_contact = _pickup_contact(pickup_from_type, pickup_contact_name)
	except Exception:
		pickup_contact = None
	sender = _sender_from_warehouse(
		delivery_notes=delivery_notes,
		pickup_address_name=pickup_address_name,
		pickup_contact=pickup_contact,
	)
	receiver, remembered = _apply_saved_sf_receiver(
		_party_from_address(delivery_address_name, delivery_contact_name),
		delivery_address_name,
	)
	receiver = _fill_receiver_gaps(
		receiver,
		delivery_to=delivery_to,
		delivery_contact_name=delivery_contact_name,
		delivery_contact=delivery_contact,
	)
	form = {
		"product_code": DEFAULT_PRODUCT_CODE,
		"product_name": DEFAULT_PRODUCT_NAME,
		"total_weight": flt(total_weight) or 0.1,
		"parcel_quantity": 1,
		"sender": sender,
		"receiver": receiver,
	}
	form.update(_customs_presets(settings))
	goods = _goods_from_delivery_notes(delivery_notes)
	return {
		"enabled": True,
		"products": _product_choices(settings),
		"form": form,
		"sender_preset": True,
		"receiver_remembered": remembered,
		"goods": goods,
		"source_warehouse": _source_warehouse(goods),
	}


@frappe.whitelist()
def list_sf_countries(keyword=None):
	frappe.has_permission("Shipment", "read", throw=True)
	rows = frappe.get_all("Country", fields=["name", "code"], order_by="name", limit=400)
	keyword = (keyword or "").strip().lower()
	out = []
	for row in rows:
		code = (row.code or "").strip().upper()
		if not code:
			continue
		if keyword and keyword not in (row.name or "").lower() and keyword not in code.lower():
			continue
		out.append({"name": row.name, "code": code})
	return out


@frappe.whitelist()
def list_sf_regions(country_code, region_one_name=None):
	frappe.has_permission("Shipment", "read", throw=True)
	if not country_code:
		return []
	return [{"name": name} for name in region_cascade(country_code, region_one_name or "")]


@frappe.whitelist()
def lookup_sf_postcode(country_code, post_code):
	frappe.has_permission("Shipment", "read", throw=True)
	if not country_code or not post_code:
		frappe.throw(_("Country and postcode are required."))
	return query_postcode(country_code, post_code)




def _parcel_payload(shipment) -> list[dict]:
	rows = []
	for row in shipment.get("shipment_parcel") or []:
		rows.append(
			{
				"length": row.length,
				"width": row.width,
				"height": row.height,
				"weight": row.weight,
				"count": row.count or 1,
			}
		)
	return rows




def enrich_shipment_summary(*, shipments=None):
	"""Decorate native Shipment summaries with SF-only cancellation state.

	ERPNext owns Shipment identity and carrier-neutral transport status. This
	hook only reads the two SF fields in one permission-aware query and returns
	the presentation keys accepted by the native summary endpoint.
	"""
	rows = [
		row for row in (shipments or [])
		if cint(row.get("docstatus")) != 2
		and _is_sf_shipment(row)
	]
	names = [str(row.get("name") or "") for row in rows if row.get("name")]
	if not names:
		return {}

	fields = ["name"]
	for fieldname in ("sf_intercept_status", "sf_carrier_cancelled"):
		if _has_field("Shipment", fieldname):
			fields.append(fieldname)
	if len(fields) == 1:
		return {}

	try:
		sf_rows = frappe.get_list(
			"Shipment",
			filters={"name": ["in", names]},
			fields=fields,
			limit_page_length=0,
		)
	except frappe.PermissionError:
		return {}

	from erpnext_shipping.sf_international.interception import (
		CARRIER_PENDING_STATE,
		CARRIER_STATE,
		LEGACY_STATE_MAP,
		MANUAL_STATES,
		REQUEST_STATE,
		SF_SUPPORT_FAILED,
	)

	enriched = {}
	for row in sf_rows:
		name = row.get("name")
		if not name:
			continue
		intercept_status = str(row.get("sf_intercept_status") or "")
		intercept_status = LEGACY_STATE_MAP.get(intercept_status, intercept_status)
		carrier_cancelled = cint(row.get("sf_carrier_cancelled")) == 1
		if carrier_cancelled or intercept_status == CARRIER_STATE:
			enriched[name] = {
				"status_label": "顺丰已取消，待取消本地运单",
				"status_color": "orange",
				"extra_details": "顺丰已确认取消；本地运单仍需单独取消。",
			}
		elif intercept_status == CARRIER_PENDING_STATE:
			enriched[name] = {
				"status_label": CARRIER_PENDING_STATE,
				"status_color": "orange",
				"extra_details": "正在等待顺丰确认取消结果。",
			}
		elif intercept_status in MANUAL_STATES | {REQUEST_STATE}:
			color = "red" if intercept_status == SF_SUPPORT_FAILED else "orange"
			enriched[name] = {
				"status_label": intercept_status,
				"status_color": color,
				"extra_details": "顺丰拦截状态由客服与承运商流程维护。",
			}
	return enriched


def _select_linked_shipment_row(rows, *, include_cancelled=False):
	"""Use ERPNext's carrier-neutral Shipment selection for SF workflows."""
	from erpnext.stock.doctype.shipment.shipment_summary import select_linked_shipment_row

	return select_linked_shipment_row(rows, include_cancelled=include_cancelled)


def _find_linked_shipment(delivery_note: str, *, include_cancelled=False):
	_check_read_permission("Delivery Note", delivery_note)
	parents = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": delivery_note},
		pluck="parent",
		distinct=True,
	)
	if not parents:
		return None
	rows = frappe.get_all(
		"Shipment",
		filters={"name": ["in", parents]} if include_cancelled else {"name": ["in", parents], "docstatus": ["<", 2]},
		fields=["name", "shipment_id", "awb_number", "status", "docstatus", "modified"],
		order_by="modified desc, name asc",
	)
	chosen = _select_linked_shipment_row(rows, include_cancelled=include_cancelled)
	if not chosen:
		return None
	doc = frappe.get_doc("Shipment", chosen.name)
	doc.check_permission("read")
	return doc


def _find_historical_linked_shipment(delivery_note: str, *, active=None):
	"""Return the newest cancelled linked Shipment for the audit panel.

	Historical rows are intentionally queried separately from the active
	workflow row. They must never be reused when a new label is created.
	"""
	_check_read_permission("Delivery Note", delivery_note)
	parents = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": delivery_note},
		pluck="parent",
		distinct=True,
	)
	if not parents:
		return None
	rows = frappe.get_list(
		"Shipment",
		filters={"name": ["in", parents]},
		fields=["name", "shipment_id", "awb_number", "status", "docstatus", "modified"],
		order_by="modified desc, name asc",
		limit_page_length=0,
	)
	active_name = getattr(active, "name", None) if active else None
	historical = [
		row for row in rows
		if cint(row.get("docstatus")) == 2 and row.name != active_name
	]
	chosen = _select_linked_shipment_row(historical, include_cancelled=True)
	if not chosen:
		return None
	doc = frappe.get_doc("Shipment", chosen.name)
	doc.check_permission("read")
	return doc


def _dn_value(dn, *fieldnames):
	for fieldname in fieldnames:
		if dn.meta.has_field(fieldname) and dn.get(fieldname):
			return dn.get(fieldname)
	return None


def _address_row(address_name):
	if not address_name:
		return None
	_check_read_permission("Address", address_name)
	return frappe.db.get_value(
		"Address",
		address_name,
		[
			"name",
			"address_title",
			"address_line1",
			"address_line2",
			"city",
			"state",
			"pincode",
			"country",
			"email_id",
			"phone",
		],
		as_dict=1,
	)


def _contact_row(contact_name):
	if not contact_name:
		return None
	_check_read_permission("Contact", contact_name)
	return frappe.db.get_value(
		"Contact",
		contact_name,
		["name", "first_name", "last_name", "email_id", "phone", "mobile_no"],
		as_dict=1,
	)


def _user_contact_row(user):
	if not user:
		return None
	if user != frappe.session.user:
		_check_read_permission("User", user)
	row = frappe.db.get_value(
		"User",
		user,
		["name", "full_name", "first_name", "last_name", "phone", "mobile_no", "email"],
		as_dict=1,
	)
	if not row:
		return None
	row.first_name = row.first_name or row.full_name or ""
	row.last_name = row.last_name or ""
	row.email_id = row.email
	return row


def _party_payload(address_name, contact=None, company_fallback="", link_contact=True):
	missing = []
	address = _address_row(address_name)
	contact_name = (contact.name if contact and link_contact else "") or ""
	if not address:
		missing.append(_("Address"))
		return {
			"address_name": address_name or "",
			"contact_name": contact_name,
			"company": company_fallback or "",
			"contact": "",
			"phone": "",
			"country": "",
			"state": "",
			"city": "",
			"pincode": "",
			"address": "",
			"missing": missing,
		}
	line = " ".join(p for p in [address.address_line1, address.address_line2] if p).strip()
	if not line:
		missing.append(_("Address"))
	if not address.country:
		missing.append(_("Country"))
	if not (address.state or "").strip() and not (address.city or "").strip():
		missing.append(_("State"))
	if not (address.city or "").strip():
		missing.append(_("City"))
	person = _contact_name(contact) if contact else ""
	phone = ""
	if contact:
		phone = _phone_number(contact)
	if not phone:
		phone = _clean_phone(address.phone)
	if not phone:
		phone = _phone_from_customer(company_fallback)
	if not person:
		missing.append(_("Contact"))
	if not phone:
		missing.append(_("Phone"))
	return {
		"address_name": address.name,
		"contact_name": contact_name,
		"company": address.address_title or company_fallback or "",
		"contact": person,
		"phone": phone,
		"country": address.country or "",
		"state": (address.state or "").strip(),
		"city": (address.city or "").strip(),
		"pincode": str(address.pincode or "").strip(),
		"address": line,
		"missing": missing,
	}






def _content_from_delivery_note(dn) -> str:
	names = [item.item_name or item.item_code for item in (dn.get("items") or [])[:8]]
	text = ", ".join(name for name in names if name).strip()
	return (text or "Goods")[:140]


def _require_delivery_note_addresses(shipment):
	if not shipment.pickup_address_name:
		frappe.throw(_("Please set a company address on the Delivery Note."))
	if not shipment.delivery_address_name:
		frappe.throw(_("Please set a shipping address on the Delivery Note."))


def _prepare_mapped_shipment(shipment, dn):
	totals = {}
	for row in shipment.get("shipment_delivery_note") or []:
		name = row.delivery_note or dn.name
		totals[name] = totals.get(name, 0) + flt(row.grand_total)
	if not totals:
		totals[dn.name] = flt(dn.grand_total)
	shipment.set("shipment_delivery_note", [])
	for name, total in totals.items():
		shipment.append(
			"shipment_delivery_note",
			{"delivery_note": name, "grand_total": total or flt(dn.grand_total)},
		)
	if not shipment.pickup_from_type:
		shipment.pickup_from_type = "Company"
	if not shipment.delivery_to_type:
		shipment.delivery_to_type = "Customer"
	if not shipment.pickup_date:
		shipment.pickup_date = getdate()
	if not shipment.pickup_from:
		shipment.pickup_from = "09:00:00"
	if not shipment.pickup_to:
		shipment.pickup_to = "17:00:00"
	if not shipment.description_of_content:
		shipment.description_of_content = _content_from_delivery_note(dn)
	if not flt(shipment.value_of_goods):
		shipment.value_of_goods = _customs_presets()["declared_value"]
	if shipment.service_provider and _is_sf_provider(shipment.service_provider) and not shipment.shipment_id:
		shipment.service_provider = None
		shipment.carrier = None
		shipment.carrier_service = None
	_require_delivery_note_addresses(shipment)
	if not shipment.delivery_contact_name:
		frappe.throw(_("Contact name is required for SF International."))


def _ensure_shipment_for_delivery_note(dn, parcels):
	shipment = _find_linked_shipment(dn.name)
	if shipment:
		_reject_manual_shipping_api(shipment)
		if shipment.shipment_id:
			return shipment
		save_shipment_parcels(shipment.name, parcels)
		return frappe.get_doc("Shipment", shipment.name)

	frappe.has_permission("Shipment", "create", throw=True)
	from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment

	mapped = make_shipment(dn.name)
	_prepare_mapped_shipment(mapped, dn)
	mapped.insert()
	save_shipment_parcels(mapped.name, parcels)
	return frappe.get_doc("Shipment", mapped.name)


@frappe.whitelist()
def get_sales_order_freight_map(sales_orders=None):
	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders)
	names = [str(name) for name in (sales_orders or []) if name]
	blank = {"total": 0, "settled": 0, "booked": 0, "confirmed": 0, "billed": 0, "held": 0, "review": 0, "amount": None, "billed_amount": None, "currency": "CNY"}
	out = {name: dict(blank) for name in names}
	if not names:
		return out
	for name in names:
		_check_read_permission("Sales Order", name)
	dn_items = frappe.get_all(
		"Delivery Note Item",
		# Keep cancelled Delivery Notes: a cancelled/intercepted package can
		# still receive a carrier freight bill after the source document closes.
		filters={"against_sales_order": ["in", names]},
		fields=["parent", "against_sales_order"],
	)
	if not dn_items:
		return out
	dn_names = list({row.parent for row in dn_items if row.parent})
	valid_dns = {
		row.name
		for row in frappe.get_list(
			"Delivery Note",
			filters={"name": ["in", dn_names]},
			fields=["name"],
			limit_page_length=0,
		)
	}
	so_by_dn = {}
	for row in dn_items:
		if row.parent in valid_dns and row.against_sales_order:
			so_by_dn.setdefault(row.parent, set()).add(row.against_sales_order)
	if not so_by_dn:
		return out
	links = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": ["in", list(so_by_dn)]},
		fields=["delivery_note", "parent"],
	)
	parent_names = list({row.parent for row in links if row.parent})
	if not parent_names:
		return out
	fields = [
		"name",
		"shipment_id",
		"awb_number",
		"status",
		"docstatus",
		"service_provider",
		"carrier",
		"shipment_amount",
	]
	if _has_field("Shipment", "sf_freight_status"):
		fields.append("sf_freight_status")
	for field in ("sf_freight_accounting_status", "sf_freight_currency", "sf_freight_accounting_hold"):
		if _has_field("Shipment", field):
			fields.append(field)
	ships = {
		row.name: row
		for row in frappe.get_list(
			"Shipment",
			# Historical cancelled Shipments retain the waybill and freight
			# evidence and must remain visible in Sales Order totals.
			filters={"name": ["in", parent_names]},
			fields=fields,
			limit_page_length=0,
		)
	}
	# A Shipment is the package, but each SF Waybill row is a separate carrier
	# order. After a replacement the parent projection contains only the newest
	# label; use the immutable rows for totals so an old billed label is never
	# silently omitted. Sites upgrading before the DocType exists keep the parent
	# fallback below.
	child_rows = []
	try:
		child_rows = frappe.get_all(
			"SF Waybill",
			filters={"shipment": ["in", parent_names]},
			fields=[
				"name", "shipment", "waybill", "replacement_status", "freight_amount", "freight_currency",
				"freight_status", "freight_accounting_status", "freight_accounting_hold", "creation_uncertain",
			],
			limit_page_length=0,
		)
	except Exception:
		child_rows = []
	children_by_shipment = {}
	for child in child_rows:
		if not child.get("shipment"):
			continue
		status = str(child.get("replacement_status") or "").strip()
		# A failed validation with no carrier order is an audit row, not another
		# package. Pending or uncertain attempts remain visible because they may
		# represent an order that still needs external reconciliation.
		counts_as_unit = bool(str(child.get("waybill") or "").strip()) or status in {"创建中", "待替换", "待启用"} or (
			status == "失败" and cint(child.get("creation_uncertain"))
		)
		if not counts_as_unit:
			continue
		shipment_name = child.get("shipment")
		waybill = str(child.get("waybill") or "").strip()
		key = ("waybill", waybill) if waybill else ("record", child.get("name"))
		bucket = children_by_shipment.setdefault(shipment_name, {})
		previous = bucket.get(key)
		if previous:
			# A database uniqueness constraint prevents new duplicates. For legacy
			# rows, retain the copy with the strongest freight/accounting evidence so
			# Sales Order totals are not doubled or downgraded.
			def evidence_score(row):
				return (
					int(row.get("freight_status") == FREIGHT_SETTLED) * 4
					+ int(row.get("freight_amount") not in (None, "")) * 2
					+ int(row.get("freight_accounting_status") == "已记账")
				)
			if evidence_score(child) <= evidence_score(previous):
				continue
		bucket[key] = child
	freight_units = {}
	for name, ship in ships.items():
		children = list((children_by_shipment.get(name) or {}).values())
		if not children:
			freight_units[name] = [ship]
			continue
		parent_waybill = str(ship.get("shipment_id") or ship.get("awb_number") or "").strip()
		parent_values = {
			"freight_amount": ship.get("shipment_amount"),
			"freight_currency": ship.get("sf_freight_currency"),
			"freight_status": ship.get("sf_freight_status") if ship.get("sf_freight_status") in {FREIGHT_UNSETTLED, FREIGHT_SETTLED} else FREIGHT_UNSETTLED,
			"freight_accounting_status": ship.get("sf_freight_accounting_status"),
			"freight_accounting_hold": ship.get("sf_freight_accounting_hold"),
		}
		units = []
		for child in children:
			# Fill only missing current-label values from the compatibility parent;
			# historical rows remain independent facts.
			if str(child.get("waybill") or "").strip() == parent_waybill:
				for field, value in parent_values.items():
					if child.get(field) in (None, "") and value not in (None, ""):
						child[field] = value
			units.append(child)
		freight_units[name] = units or [ship]
	so_ships = {name: {} for name in names}
	for link in links:
		ship = ships.get(link.parent)
		if not ship:
			continue
		if not _is_sf_shipment(ship):
			continue
		if not (ship.shipment_id or ship.awb_number or flt(ship.shipment_amount) or children_by_shipment.get(ship.name)):
			continue
		for so in so_by_dn.get(link.delivery_note) or []:
			if so in so_ships:
				so_ships[so][ship.name] = freight_units.get(ship.name) or [ship]
	for so, by_name in so_ships.items():
		rows = [unit for units in by_name.values() for unit in units]
		total = len(rows)
		settled = 0
		confirmed = 0
		held = 0
		review = 0
		settled_amt = 0.0
		billed_amount = 0.0
		currencies = set()
		billed_currencies = set()
		booked_currencies = set()
		billed_amount_complete = True
		booked_amount_complete = True
		for ship in rows:
			raw_amount = ship.get("freight_amount") if "freight_amount" in ship else ship.get("shipment_amount")
			amount = flt(raw_amount)
			status = ship.get("freight_status") if "freight_status" in ship else ship.get("sf_freight_status")
			confirmed += int(status == FREIGHT_SETTLED)
			if status == FREIGHT_SETTLED:
				billed_amount += amount
				currency = str(ship.get("freight_currency") or ship.get("sf_freight_currency") or "").strip().upper()
				if currency:
					billed_currencies.add(currency)
					currencies.add(currency)
				else:
					billed_amount_complete = False
				if raw_amount in (None, ""):
					billed_amount_complete = False
			accounting_status = ship.get("freight_accounting_status") or ship.get("sf_freight_accounting_status")
			held += int(bool(cint(ship.get("freight_accounting_hold") if "freight_accounting_hold" in ship else ship.get("sf_freight_accounting_hold"))) or accounting_status == "记账待处理")
			accounting_without_bill = accounting_status == "已记账" and status != FREIGHT_SETTLED
			review += int(accounting_status == "待复核" or accounting_without_bill)
			if accounting_status == "已记账" and status == FREIGHT_SETTLED and amount > 0:
				settled += 1
				settled_amt += amount
				currency = str(ship.get("freight_currency") or ship.get("sf_freight_currency") or "").strip().upper()
				if currency:
					booked_currencies.add(currency)
					currencies.add(currency)
				else:
					booked_amount_complete = False
				if raw_amount in (None, ""):
					booked_amount_complete = False
		out[so] = {
			"total": total,
			"settled": settled,
			"booked": settled,
			"confirmed": confirmed,
			"billed": confirmed,
			"held": held,
			"review": review,
			"amount": round(settled_amt, 2) if settled and booked_amount_complete and len(booked_currencies) == 1 else None,
			"billed_amount": round(billed_amount, 2) if confirmed and billed_amount_complete and len(billed_currencies) == 1 else None,
			"currency": next(iter(currencies)) if len(currencies) == 1 else "",
		}
	return out




@frappe.whitelist()
def create_label_from_delivery_note(
	delivery_note,
	product_code=None,
	product_name=None,
	parcels=None,
	declared_value=None,
	declared_currency=None,
	hs_code=None,
):
	if not delivery_note:
		frappe.throw(_("Please submit the Delivery Note first."))
	dn = frappe.get_doc("Delivery Note", delivery_note, for_update=True)
	dn.check_permission("read")
	if cint(dn.docstatus) != 1:
		frappe.throw(_("Please submit the Delivery Note first."))
	parcels = _require_positive_parcels(_parse_parcels(parcels))
	shipment = _ensure_shipment_for_delivery_note(dn, parcels)
	if shipment.shipment_id:
		return {
			"service_provider": SF_PROVIDER,
			"shipment_id": shipment.shipment_id,
			"carrier": shipment.carrier or SF_PROVIDER,
			"carrier_service": shipment.carrier_service or product_name or "",
			"awb_number": shipment.awb_number or shipment.shipment_id,
			"shipment": shipment.name,
			"already_created": True,
		}
	if declared_value not in (None, ""):
		shipment.value_of_goods = flt(declared_value)
		shipment.db_set("value_of_goods", shipment.value_of_goods, update_modified=False)
	pickup_contact_name = (
		shipment.pickup_contact_person if shipment.pickup_from_type == "Company" else shipment.pickup_contact_name
	)
	info = create_shipment(
		shipment=shipment.name,
		pickup_from_type=shipment.pickup_from_type,
		delivery_to_type=shipment.delivery_to_type,
		pickup_address_name=shipment.pickup_address_name,
		delivery_address_name=shipment.delivery_address_name,
		shipment_parcel=parcels,
		description_of_content=shipment.description_of_content,
		pickup_date=str(shipment.pickup_date),
		value_of_goods=shipment.value_of_goods,
		service_data={
			"service_provider": SF_PROVIDER,
			"carrier": SF_PROVIDER,
			"service_name": product_name,
			"service_id": product_code,
			"carrier_service": product_code,
			"declared_currency": declared_currency,
			"hs_code": hs_code,
		},
		pickup_contact_name=pickup_contact_name,
		delivery_contact_name=shipment.delivery_contact_name,
		delivery_notes=[dn.name],
	)
	info["shipment"] = shipment.name
	return info


def _is_sf_provider(value) -> bool:
	text = (value or "").strip().lower().replace("_", " ").replace("-", " ")
	return text in {
		"sf international",
		"sf",
		"顺丰国际",
		"国际顺丰",
		"sf国际",
		"sf global",
		"sfglobal",
	}


def _is_sf_shipment(doc) -> bool:
	# Explicit selection takes precedence over a stale projected carrier name.
	provider = doc.get("service_provider")
	return _is_sf_provider(provider if provider else doc.get("carrier"))


def _is_manual_shipment(doc) -> bool:
	return str(doc.get("service_provider") or "").strip() == MANUAL_PROVIDER


def _reject_manual_shipping_api(doc):
	if _is_manual_shipment(doc):
		frappe.throw("其他物流未接入下单、面单打印、物流或运费查询接口，请在运单的手工物流区域登记已有单号、运输状态和运费。")


def _clear_zero_amount(doc):
	amount = None if doc.shipment_amount in (None, "") else flt(doc.shipment_amount)
	if amount is None or amount <= 0:
		doc.shipment_amount = 0
		return True
	return False


def _has_submitted_delivery_note(doc) -> bool:
	for row in doc.get("shipment_delivery_note") or []:
		name = row.delivery_note
		if not name:
			continue
		if cint(frappe.db.get_value("Delivery Note", name, "docstatus")) == 1:
			return True
	return False


def _require_submitted_delivery_note(doc):
	names = validate_shipment_delivery_notes(doc)
	if not names or any(cint(frappe.db.get_value("Delivery Note", name, "docstatus")) != 1 for name in names):
		frappe.throw(_("Please create and submit a Delivery Note first."))


def get_sf_rates(_pickup_address_name=None, _delivery_address_name=None, _parcels=None) -> list[dict]:
	# Local drafts must not receive a selectable SF quote. Freight is filled later from payAmount.
	return []


def _create_order_body(
	shipment,
	pickup_from_type,
	pickup_address_name,
	delivery_address_name,
	parcels,
	description_of_content,
	value_of_goods,
	service_info,
	pickup_contact_name=None,
	delivery_contact_name=None,
):
	settings = get_settings()
	pickup = _sf_address(pickup_address_name, "发件详细地址")
	delivery = _sf_address(delivery_address_name, "收件详细地址")
	pickup_contact = _pickup_contact(pickup_from_type, pickup_contact_name)
	delivery_contact = _get_contact(delivery_contact_name)
	totals = _parcel_totals(parcels)
	sender_name = _contact_name(pickup_contact)
	receiver_name = _contact_name(delivery_contact)
	sender_phone = _phone_number(pickup_contact)
	receiver_phone = _phone_number(delivery_contact)
	if not sender_name:
		frappe.throw(_("Contact name is required for SF International."))
	if not receiver_name:
		frappe.throw(_("Contact name is required for SF International."))
	if not sender_phone:
		frappe.throw(_("Mobile or landline is required for SF International contact {0}.").format(sender_name))
	if not receiver_phone:
		frappe.throw(_("Mobile or landline is required for SF International contact {0}.").format(receiver_name))
	sender_company = pickup.get("company") or sender_name

	product_code, product_name = _resolve_product(settings, service_info)
	currency = (
		str((service_info or {}).get("declared_currency") or "").strip()
		or (settings.declared_currency or "CNY").strip()
		or "CNY"
	)
	declared = flt(value_of_goods) or 20
	hs_code = str((service_info or {}).get("hs_code") or "").strip() or None
	hscodes = _hscode_items(shipment, description_of_content, value_of_goods, settings, totals, hs_code=hs_code)
	body = {
		"isSaveCustoms": 0,
		"pieceorderBaseInfo": {
			"isInsurance": 0,
			"userOrderid": shipment,
			"totalWeight": str(totals["parcelTotalWeight"]),
			"expressType": str(product_code),
			"isBat": 0,
			"isSaveBasic": 0,
			"declaredValueCurrency": currency,
			"totalDeclaredValue": declared,
			"purchaseCurrency": currency,
			"purchaseTotal": f"{round(declared, 2):.2f}",
		},
		"pieceorderAddressInfo": {
			"isSaveReceiver": 0,
			"isSaveSender": 0,
			"dContact": receiver_name,
			"dCountry": delivery["country"],
			"dProvince": delivery["regionFirst"],
			"dCity": delivery["regionSecond"],
			"dPostCode": delivery["postCode"],
			"dTel": receiver_phone,
			"dAddress": delivery["address"],
			"jCompany": sender_company,
			"jContact": sender_name,
			"jProvince": pickup["regionFirst"],
			"jCity": pickup["regionSecond"],
			"jPostCode": pickup["postCode"],
			"jTel": sender_phone,
			"jMobile": sender_phone,
			"jAddress": pickup["address"],
			"jDeparture": pickup.get("country") or "CN",
		},
		"pieceorderHscodeInfos": hscodes,
		"pieceorderExtendInfo": {"isSaveExtend": 0},
		"isDocall": 0,
		"pieceordercustomInfo": {},
		"pieceorderImporterInfo": {},
	}
	if pickup.get("email") or pickup_contact.get("email_id"):
		body["pieceorderAddressInfo"]["jEmail"] = pickup.get("email") or pickup_contact.get("email_id")
	return body, product_code, product_name


def create_sf_shipment(
	shipment,
	pickup_from_type,
	pickup_address_name,
	delivery_address_name,
	shipment_parcel,
	description_of_content,
	value_of_goods,
	service_info,
	pickup_contact_name=None,
	delivery_contact_name=None,
	delivery_notes=None,
):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_reject_manual_shipping_api(doc)
	_require_shipping_mutation(doc)
	if _is_cancelled(doc):
		frappe.throw(_("Cancelled shipments cannot be changed."))
	if (doc.service_provider or doc.carrier) and not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	if not is_enabled():
		frappe.throw(_("Enable SF International in SF International Settings."))
	delivery_notes = _checked_delivery_notes(doc, delivery_notes, permission="write") if delivery_notes else []
	if doc.shipment_id:
		return {
			"service_provider": SF_PROVIDER,
			"shipment_id": doc.shipment_id,
			"carrier": SF_PROVIDER,
			"carrier_service": doc.carrier_service or "",
			"awb_number": doc.awb_number or doc.shipment_id,
		}

	validate_shipment_delivery_notes(doc)
	_require_submitted_delivery_note(doc)
	parcels = _require_positive_parcels(_parse_parcels(shipment_parcel))
	body, product_code, product_name = _create_order_body(
		shipment=shipment,
		pickup_from_type=pickup_from_type,
		pickup_address_name=pickup_address_name,
		delivery_address_name=delivery_address_name,
		parcels=parcels,
		description_of_content=description_of_content,
		value_of_goods=value_of_goods,
		service_info=service_info,
		pickup_contact_name=pickup_contact_name,
		delivery_contact_name=delivery_contact_name,
	)
	# The Shipment already exists when this endpoint is called.  Persist a local
	# idempotency row before contacting SF so a timeout or process crash cannot
	# turn a carrier order into an untraceable retry.
	from .waybill import (
		_assert_waybill_not_reused,
		begin_initial_booking_attempt,
		complete_initial_booking_attempt,
		fail_initial_booking_attempt,
	)
	form_payload = {
		"pickup_from_type": pickup_from_type,
		"pickup_address_name": pickup_address_name,
		"delivery_address_name": delivery_address_name,
		"shipment_parcel": parcels,
		"description_of_content": description_of_content,
		"value_of_goods": value_of_goods,
		"service_info": service_info or {},
		"pickup_contact_name": pickup_contact_name,
		"delivery_contact_name": delivery_contact_name,
		"receiver": {
			key: (body.get("pieceorderAddressInfo") or {}).get(field) or ""
			for key, field in (
				("country", "dCountry"), ("province", "dProvince"), ("city", "dCity"),
				("county", "dCounty"), ("post_code", "dPostCode"),
				("address", "dAddress"), ("doorplate", "destDoorplate"),
			)
		},
	}
	attempt = begin_initial_booking_attempt(doc, form_payload)
	if attempt:
		body.setdefault("pieceorderBaseInfo", {})["userOrderid"] = attempt.name
	payload = None
	data = {}
	waybill = ""
	order_id = None
	try:
		payload = create_order(body)
		if not isinstance(payload, dict):
			frappe.throw("顺丰返回了无效的下单结果。")
		data = payload.get("data") or {}
		if not isinstance(data, dict):
			data = {}
		waybill = str(data.get("trackingNo") or "").strip()
		if not waybill:
			frappe.throw(_("SF International did not return trackingNo."))
		order_id = _order_id_after_create(data, waybill)
		if attempt:
			complete_initial_booking_attempt(
				doc,
				attempt,
				payload,
				waybill,
				order_id,
				carrier_service=product_name or product_code,
				form_payload=form_payload,
			)
		else:
			_assert_waybill_not_reused(doc, waybill)
		info = {
			"service_provider": SF_PROVIDER,
			"shipment_id": waybill,
			"carrier": SF_PROVIDER,
			"carrier_service": product_name or product_code,
			"awb_number": waybill,
		}
		if order_id is not None and _has_field("Shipment", "sf_iuop_order_id"):
			info["sf_iuop_order_id"] = order_id
		if not attempt:
			_remember_successful_sf_receiver(doc, form_payload["receiver"])
		if delivery_notes:
			from erpnext.stock.doctype.shipment.delivery_note_update import update_delivery_note

			update_delivery_note(delivery_notes=delivery_notes, shipment_info=info)
		return info
	except Exception as exc:
		if attempt:
			fail_initial_booking_attempt(
				doc,
				attempt,
				exc,
				payload=payload,
				waybill=waybill,
				order_id=order_id,
			)
		raise


def _numeric_order_id(value):
	if isinstance(value, bool) or value in (None, ""):
		return None
	if isinstance(value, int):
		return value if value > 0 else None
	text = str(value).strip()
	if text.isdigit():
		number = int(text)
		return number if number > 0 else None
	return None


def _extract_iuop_order_id(source):
	found = _numeric_order_id(source)
	if found:
		return found
	if not isinstance(source, dict):
		return None
	for key in ("orderId", "order_id", "sysOrderId"):
		found = _numeric_order_id(source.get(key))
		if found:
			return found
	data = source.get("data")
	if isinstance(data, dict):
		return _extract_iuop_order_id(data)
	return None


def _order_id_after_create(data, waybill):
	order_id = _extract_iuop_order_id(data)
	if order_id or not waybill:
		return order_id
	try:
		return _extract_iuop_order_id(query_order(waybill))
	except Exception:
		# The tracking number confirms booking; orderId can be fetched later.
		frappe.log_error(title="SF International orderId lookup after booking")
		return None


def _print_task_id(payload: dict):
	data = payload.get("data")
	if data in (None, "", [], 0):
		frappe.throw(_("SF International did not return a print task id."))
	if isinstance(data, int) and data > 0:
		return data
	if isinstance(data, str) and str(data).strip():
		return data
	if isinstance(data, dict):
		for key in ("id", "taskId", "task_id", "downloadId"):
			if data.get(key) not in (None, ""):
				return data.get(key)
	if isinstance(data, list) and data:
		first = data[0]
		if isinstance(first, (str, int)):
			return first
		if isinstance(first, dict):
			return first.get("id") or first.get("taskId") or first.get("task_id")
	frappe.throw(_("SF International did not return a print task id."))


def _stored_file_url(url: str | None) -> str:
	if not url:
		return ""
	text = str(url)
	if "/files/" in text or "/private/files/" in text:
		return text
	return ""


def _shipment_order_id(doc) -> int:
	waybill = doc.shipment_id or doc.awb_number
	order_id = None
	if waybill:
		order_id = _extract_iuop_order_id(query_order(waybill))
	if not order_id and _has_field("Shipment", "sf_iuop_order_id"):
		order_id = _numeric_order_id(getattr(doc, "sf_iuop_order_id", None))
	if not order_id:
		frappe.throw(_("SF International did not return orderId for {0}.").format(waybill or doc.name))
	if _has_field("Shipment", "sf_iuop_order_id"):
		doc.db_set("sf_iuop_order_id", order_id)
	return order_id


def print_sf_label(shipment_name: str):
	doc = frappe.get_doc("Shipment", shipment_name, for_update=True)
	doc.check_permission("write")
	_require_active_sf_shipment(doc, submitted=True)
	_require_shipping_mutation(doc)
	from .waybill import current_label_record, sync_current_label

	# Lock the parent before its exact current record in both print endpoints.
	# The immutable carrier record is authoritative when both caches exist.
	record = current_label_record(doc)
	stored = _stored_file_url(record.get("label_url") if record else None) or _stored_file_url(doc.get("sf_label_url"))
	if stored:
		sync_current_label(doc, record, stored)
		return stored
	order_id = _shipment_order_id(doc)
	task_id = _print_task_id(start_print(order_id))
	url, v2token = poll_label(task_id)
	content = download_pdf(url, v2token)
	waybill = doc.shipment_id or doc.awb_number or shipment_name
	file_doc = save_file(
		f"SF-{waybill}.pdf",
		content,
		"Shipment",
		shipment_name,
		is_private=1,
	)
	file_url = file_doc.file_url
	sync_current_label(doc, record, file_url)
	return file_url


@frappe.whitelist()
def dispatch_sf_shipment(shipment: str):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_require_shipping_mutation(doc)
	if not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	if not _sf_waybill(doc):
		frappe.throw(_("No SF waybill to ship."))
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Submit the shipment before printing the label and shipping."))
	if _is_cancelled(doc):
		frappe.throw(_("Cancelled shipments cannot be dispatched."))
	file_url = print_sf_label(shipment)
	if not file_url:
		frappe.throw(_("The label was not generated. Please print again."))
	doc.db_set("status", _safe_shipment_status(STATUS_SHIPPED))
	return file_url


def _route_rows(payload: dict) -> list[dict]:
	data = payload.get("data")
	if data is None or data == "":
		return []
	if isinstance(data, list):
		return [row for row in data if isinstance(row, dict)]
	if isinstance(data, dict):
		route_keys = ("ordCNList", "ordENList", "routes", "items", "routeList", "trackDetails", "trackDetailItems", "list")
		for key in route_keys:
			rows = data.get(key)
			if isinstance(rows, list) and rows:
				return [row for row in rows if isinstance(row, dict)]
		if any(data.get(key) for key in ("routeDesc", "remark", "trackOutRemark", "opCode", "opDesc", "routeDescription")):
			return [data]
		if not data or any(key in data and data[key] in (None, []) for key in route_keys):
			return []
	frappe.throw("无法识别顺丰物流返回结构，已有物流状态保持不变，请联系管理员核对。")


def _route_time(row: dict) -> str:
	return str(
		row.get("routeTime")
		or row.get("acceptTime")
		or row.get("barScanTm")
		or row.get("localTm")
		or row.get("time")
		or row.get("createTime")
		or ""
	)

def _route_description(row: dict) -> str:
	return str(
		row.get("routeDesc")
		or row.get("remark")
		or row.get("trackOutRemark")
		or row.get("opDesc")
		or row.get("status")
		or row.get("routeDescription")
		or ""
	)


def _tracking_from_route(payload, waybill):
	rows = sorted(_route_rows(payload), key=_route_time, reverse=True)
	latest = rows[0] if rows else {}
	remark = _route_description(latest)
	opcode = str(latest.get("opCode") or latest.get("opcode") or "").lower()
	delivered = opcode in DELIVERED_OPCODES or "delivered" in remark.lower() or "已签收" in remark
	return {
		"awb_number": waybill,
		"route_count": len(rows),
		"tracking_events": [{"time": _route_time(row), "description": _route_description(row)} for row in rows],
		"tracking_status": "Booked" if not rows else "Delivered" if delivered else "In Progress",
		"tracking_status_info": remark[:140],
		"tracking_url": "",
	}


def track_sf_shipment(shipment_name: str, shipment_id: str, delivery_contact_name: str | None):
	doc = frappe.get_doc("Shipment", shipment_name)
	doc.check_permission("write")
	_require_active_sf_shipment(doc)
	waybill = _sf_waybill(doc)
	if shipment_id and str(shipment_id).strip() != waybill:
		frappe.throw(_("The supplied waybill does not match this shipment."))
	if not waybill:
		frappe.throw(_("SF International order {0} was not found.").format(shipment_name))
	order_id = _shipment_order_id(doc)
	return _tracking_from_route(query_route(order_id), waybill)


def track_sf_shipment_readonly(shipment_name: str, shipment_id: str):
	"""Query the current SF route without persisting status or requiring write access."""
	doc = frappe.get_doc("Shipment", shipment_name)
	doc.check_permission("read")
	_require_active_sf_shipment(doc)
	waybill = _sf_waybill(doc)
	if shipment_id and str(shipment_id).strip() != waybill:
		frappe.throw(_("The supplied waybill does not match this shipment."))
	if not waybill:
		frappe.throw(_("SF International order {0} was not found.").format(shipment_name))
	order_id = _shipment_order_id(doc)
	return _tracking_from_route(query_route(order_id), waybill)


def _freight_window(doc) -> tuple[str, str]:
	end = getdate()
	begin = getdate(doc.get("pickup_date") or doc.get("creation")) if doc.get("pickup_date") or doc.get("creation") else add_months(end, -6)
	return begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _sum_pay_amount(payload: dict) -> tuple[float | None, str, list]:
	if not isinstance(payload, dict):
		frappe.throw(_("SF International returned an invalid freight bill."))
	data = payload.get("data")
	items = data.get("items") if isinstance(data, dict) else data
	if not isinstance(items, list):
		frappe.throw(_("SF International returned an invalid freight bill."))
	if isinstance(data, dict) and cint(data.get("totalNum") or 0) > len(items):
		frappe.throw(_("The freight query returned an incomplete bill. Review all bill pages before booking."))
	total = Decimal("0")
	currency = ""
	currencies = set()
	records = []
	for item in items:
		if not isinstance(item, dict) or item.get("payAmount") in (None, ""):
			frappe.throw(_("SF International returned an invalid freight amount."))
		try:
			amount = Decimal(str(item.get("payAmount")))
		except (InvalidOperation, ValueError):
			frappe.throw(_("SF International returned an invalid freight amount."))
		if not amount.is_finite() or amount < 0:
			frappe.throw(_("SF International returned an invalid freight amount."))
		currency = str(item.get("currency") or "").strip().upper()
		if not re.fullmatch(r"[A-Z]{3}", currency):
			frappe.throw(_("SF International returned an invalid freight currency."))
		total += amount
		currencies.add(currency)
		records.append(dict(item, currency=currency))
	if len(currencies) > 1:
		frappe.throw(_("SF International returned freight in multiple currencies; settle each currency separately."))
	if not records:
		return None, currency, records
	if total >= Decimal("1000000000000") or (total > 0 and round(total, 2) <= 0):
		frappe.throw(_("SF International returned an invalid freight amount."))
	return float(round(total, 2)), currency, records


def _retained_freight_bill(doc):
	try:
		payload = json.loads(doc.get("sf_freight_payload") or "{}")
	except (TypeError, ValueError):
		return None
	if not isinstance(payload, dict):
		return None
	for bill in (payload, payload.get("previous_bill")):
		if not isinstance(bill, dict) or bill.get("waybill") != _sf_waybill(doc) or not bill.get("records"):
			continue
		try:
			amount, currency, records = _sum_pay_amount({"data": {"items": bill["records"]}})
			if bill.get("amount") is None or abs(flt(bill["amount"]) - amount) >= 0.005:
				continue
			if str(bill.get("currency") or "").upper() != currency:
				continue
		except Exception:
			continue
		return {"waybill": bill["waybill"], "amount": amount, "currency": currency, "records": records, "raw": bill.get("raw")}
	return None


def _freight_accounting_state(doc, bill=None):
	if freight_accounting.booking_is_held(doc):
		return "记账待处理"
	bill = bill or _retained_freight_bill(doc)
	journal = doc.get("sf_freight_journal")
	if journal:
		entry = frappe.db.get_value("Journal Entry", journal, ["docstatus", "total_debit", "company"], as_dict=True)
		if not entry or cint(entry.docstatus) != 1:
			return "记账待处理"
		if not bill or abs(flt(entry.total_debit) - bill["amount"]) >= 0.005:
			return "待复核"
		company = _shipment_company(doc)
		if not company or entry.company != company:
			return "待复核"
		if bill["currency"] != frappe.db.get_value("Company", company, "default_currency"):
			return "待复核"
		return "已记账"
	if bill and bill["amount"] == 0:
		return "无需记账"
	return "待记账" if bill else "待核实"


def _apply_freight(doc, amount, currency, records, payload=None, *, accounting_status=None, accounting_note=None, query_status=None):
	updates = {}
	if amount is not None:
		updates["shipment_amount"] = amount
		if _has_field("Shipment", "sf_freight_currency"):
			updates["sf_freight_currency"] = currency
	if accounting_status is not None and _has_field("Shipment", "sf_freight_accounting_status"):
		updates["sf_freight_accounting_status"] = accounting_status
	if accounting_note is not None and _has_field("Shipment", "sf_freight_accounting_note"):
		updates["sf_freight_accounting_note"] = accounting_note
	retained = _retained_freight_bill(doc)
	if _has_field("Shipment", "sf_freight_payload"):
		try:
			previous = json.loads(doc.get("sf_freight_payload") or "{}")
		except (TypeError, ValueError):
			previous = {}
		waybill = _sf_waybill(doc)
		current_query = {"waybill": waybill, "amount": amount, "currency": currency, "records": records, "raw": payload}
		# An empty query is an observation, not a reversal of the retained bill.
		evidence = dict(current_query if amount is not None else retained or current_query)
		if retained and amount is not None and (retained["amount"] != amount or retained["currency"] != currency):
			evidence["previous_bill"] = retained
		elif isinstance(previous, dict) and previous.get("previous_bill") and previous.get("waybill") == waybill:
			evidence["previous_bill"] = previous["previous_bill"]
		evidence["last_query"] = current_query
		evidence["accounting_status"] = accounting_status or doc.get("sf_freight_accounting_status") or "待核实"
		evidence["accounting_note"] = accounting_note if accounting_note is not None else doc.get("sf_freight_accounting_note") or ""
		updates["sf_freight_payload"] = json.dumps(evidence, ensure_ascii=False, default=str)
	if _has_field("Shipment", "sf_freight_status"):
		updates["sf_freight_status"] = FREIGHT_SETTLED if amount is not None or retained else FREIGHT_UNSETTLED
	if _has_field("Shipment", "sf_freight_query_status"):
		updates["sf_freight_query_status"] = query_status or ("查询成功" if amount is not None else "本次未查到账单")
	if updates:
		doc.db_set(updates)
	return updates


def _has_current_freight_evidence(doc):
	"""Carrier bill evidence is independent of ERP posting and payment."""
	return bool(_retained_freight_bill(doc))


def _sync_active_waybill_freight(
	doc,
	amount,
	currency,
	records,
	payload,
	*,
	query_status="查询成功",
	error=None,
	accounting_status=None,
	accounting_hold=None,
):
	"""Keep the immutable current-label history in step with the legacy parent."""
	try:
		from .waybill import sync_freight
	except (ImportError, ModuleNotFoundError):
		# The helper is absent only during an upgrade/import-time compatibility
		# window.  Once the DocType is installed, failures must surface so parent
		# and child evidence cannot silently diverge.
		return None
	return sync_freight(
		doc,
		amount,
		currency,
		records,
		payload,
		query_status=query_status,
		error=error,
		accounting_status=accounting_status,
		accounting_hold=accounting_hold,
	)


def _shipment_company(doc):
	return (
		getattr(doc, "pickup_company", None)
		or getattr(doc, "company", None)
		or frappe.defaults.get_global_default("company")
	)


def _je_naming_series():
	df = frappe.get_meta("Journal Entry").get_field("naming_series")
	options = (df.options if df else "") or "ACC-JV-.YYYY.-"
	for line in options.split("\n"):
		if line.strip():
			return line.strip()
	return "ACC-JV-.YYYY.-"


def _freight_masters(company):
	settings = get_settings()
	expense = str(getattr(settings, "freight_expense_account", None) or "").strip()
	supplier = str(getattr(settings, "freight_supplier", None) or "").strip()
	payable = str(getattr(settings, "freight_payable_account", None) or "").strip()
	if not payable:
		payable = frappe.db.get_value("Company", company, "default_payable_account")
	return expense, payable, supplier


def _book_sf_freight_journal(doc, amount, currency="CNY"):
	amount = flt(amount)
	if amount <= 0:
		return None
	# Reload under a row lock so repeated requests see the latest journal link.
	doc = frappe.get_doc("Shipment", doc.name, for_update=True)
	doc.check_permission("write")
	freight_accounting.assert_can_book(doc)
	if not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	if cint(doc.docstatus) not in (1, 2):
		frappe.throw(_("Submit the shipment before booking freight."))
	company = _shipment_company(doc)
	if not company:
		frappe.throw(_("Company is required to book SF freight."))
	expense, payable, supplier = _freight_masters(company)
	if not expense:
		frappe.throw(_("Please create account {0}.").format("顺丰国际运费 - LEYA"))
	if not payable:
		frappe.throw(_("Payable account is missing."))
	if not supplier:
		frappe.throw(_("Please create supplier {0}.").format("顺丰国际"))
	currency = str(currency or "CNY").strip().upper()
	company_currency = frappe.db.get_value("Company", company, "default_currency")
	if currency != company_currency:
		frappe.throw(
			_("Freight currency {0} must match company currency {1} before automatic booking.").format(
				currency, company_currency
			)
		)
	for account_name in (expense, payable):
		account = frappe.db.get_value("Account", account_name, ["company", "account_currency"], as_dict=True)
		if not account or account.company != company:
			frappe.throw(_("Freight account {0} must belong to company {1}.").format(account_name, company))
		if (account.account_currency or company_currency) != currency:
			frappe.throw(
				_("Freight currency {0} must match account {1} currency before automatic booking.").format(
					currency, account_name
				)
			)
	cost_center = frappe.db.get_value("Company", company, "cost_center")
	waybill = _sf_waybill(doc)
	existing_name = doc.get("sf_freight_journal") if _has_field("Shipment", "sf_freight_journal") else None
	if existing_name and frappe.db.exists("Journal Entry", existing_name):
		existing = frappe.get_doc("Journal Entry", existing_name, for_update=True)
		existing.check_permission("read")
		if existing.company != company:
			frappe.throw(_("The linked freight journal belongs to a different company."))
		linked_shipment = str(existing.get("sf_freight_shipment") or "").strip()
		linked_waybill = str(existing.get("sf_freight_waybill") or "").strip()
		if linked_shipment != doc.name or linked_waybill != waybill:
			frappe.throw("关联运费凭证不属于当前顺丰面单，请先通过运费更正流程核对旧凭证。")
		old = sum(flt(row.debit_in_account_currency) for row in existing.accounts or [])
		if cint(existing.docstatus) == 1 and abs(old - amount) < 0.005:
			return existing.name
		if cint(existing.docstatus) == 1:
			# Keep the new carrier evidence and existing posted journal intact. The
			# caller reports this discrepancy for accounting review instead of
			# silently cancelling or replacing a posted entry.
			doc._sf_freight_review_required = True
			return existing.name
		if cint(existing.docstatus) == 0:
			frappe.throw("关联运费凭证仍为草稿，请先完成更正处理，不能重复创建凭证。")
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.naming_series = _je_naming_series()
	je.company = company
	je.posting_date = getdate()
	je.user_remark = _("SF International freight {0} / {1}").format(waybill, doc.name)
	je.append(
		"accounts",
		{
			"account": expense,
			"debit_in_account_currency": amount,
			"cost_center": cost_center,
			"user_remark": waybill,
		},
	)
	je.append(
		"accounts",
		{
			"account": payable,
			"credit_in_account_currency": amount,
			"party_type": "Supplier",
			"party": supplier,
			"cost_center": cost_center,
			"user_remark": waybill,
		},
	)
	je.check_permission("create")
	je.check_permission("submit")
	with freight_accounting.journal_creation(doc, previous_journal=existing_name or doc.get("sf_freight_previous_journal")):
		freight_accounting.register_journal(doc, je)
		je.insert()
		je.submit()
	if _has_field("Shipment", "sf_freight_journal"):
		doc.db_set("sf_freight_journal", je.name)
	return je.name


def _form_party(form: dict, key: str) -> dict:
	party = form.get(key) or {}
	return party if isinstance(party, dict) else {}


def _require_filled(value, label):
	if not str(value or "").strip():
		frappe.throw(_("Please fill {0}").format(label))


def _validated_sf_address(value, label):
	try:
		return validate_sf_address(value, label)
	except LabelInputError as exc:
		frappe.throw(str(exc))


def _validate_sf_form_addresses(form):
	for key, label in (("sender", "发件详细地址"), ("receiver", "收件详细地址")):
		_validated_sf_address(_form_party(form, key).get("address"), label)


def _create_order_body_from_form(doc, form: dict) -> tuple[dict, str, str]:
	settings = get_settings()
	sender = _form_party(form, "sender")
	receiver = _form_party(form, "receiver")
	_validate_sf_form_addresses(form)
	_require_filled(receiver.get("contact"), _("Receiver"))
	_require_filled(receiver.get("phone"), _("Receiver Phone"))
	_require_filled(receiver.get("country"), _("Receiver Country"))
	_require_filled(receiver.get("province"), _("Receiver Province"))
	_require_filled(receiver.get("city"), _("Receiver City"))
	_require_filled(receiver.get("address"), _("Receiver Address"))
	_require_filled(sender.get("company"), _("Sender Company"))
	_require_filled(sender.get("contact"), _("Sender"))
	_require_filled(sender.get("province"), _("Sender Province"))
	_require_filled(sender.get("city"), _("Sender City"))
	_require_filled(sender.get("address"), _("Sender Address"))
	if not str(sender.get("phone") or "").strip() and not str(sender.get("mobile") or "").strip():
		frappe.throw(_("Please fill {0}").format(_("Sender Phone")))

	product_code, product_name = _resolve_product(
		settings,
		{"service_id": form.get("product_code"), "service_name": form.get("product_name")},
	)
	weight = flt(form.get("total_weight"))
	if weight <= 0:
		weight = flt(doc.total_weight) or 0.1
	qty = cint(form.get("parcel_quantity") or 1)
	if qty < 1:
		qty = 1
	presets = _customs_presets(settings)
	declared = flt(form.get("declared_value")) or presets["declared_value"]
	if declared <= 0:
		frappe.throw(_("Please fill {0}").format(_("Declared Value")))
	currency = (form.get("declared_currency") or presets["declared_currency"]).strip() or "CNY"
	hs_code = (form.get("hs_code") or presets["hs_code"]).strip() or DEFAULT_HS_CODE
	ename = (form.get("ename") or "").strip() or presets["ename"]
	cname = (form.get("cname") or "").strip() or presets["cname"]
	sender_phone = str(sender.get("phone") or sender.get("mobile") or "").strip()
	sender_mobile = str(sender.get("mobile") or sender.get("phone") or "").strip()
	body = {
		"isSaveCustoms": 0,
		"pieceorderBaseInfo": {
			"isInsurance": 0,
			"userOrderid": doc.name,
			"totalWeight": str(round(weight, 3)),
			"expressType": product_code,
			"isBat": 0,
			"isSaveBasic": 0,
			"declaredValueCurrency": currency,
			"totalDeclaredValue": declared,
			"purchaseCurrency": (form.get("purchase_currency") or currency).strip() or currency,
			"purchaseTotal": f"{round(declared, 2):.2f}",
		},
		"pieceorderAddressInfo": {
			"isSaveReceiver": 0,
			"isSaveSender": 0,
			"dContact": str(receiver.get("contact") or "").strip(),
			"dCountry": str(receiver.get("country") or "").strip().upper(),
			"dProvince": str(receiver.get("province") or "").strip(),
			"dCity": str(receiver.get("city") or "").strip(),
			"dCounty": str(receiver.get("county") or "").strip(),
			"dPostCode": str(receiver.get("post_code") or "").replace(" ", ""),
			"dTel": str(receiver.get("phone") or "").strip(),
			"dAddress": str(receiver.get("address") or "").strip(),
			"destDoorplate": str(receiver.get("doorplate") or "").strip(),
			"jCompany": str(sender.get("company") or "").strip(),
			"jContact": str(sender.get("contact") or "").strip(),
			"jProvince": str(sender.get("province") or "").strip(),
			"jCity": str(sender.get("city") or "").strip(),
			"jCounty": str(sender.get("county") or "").strip(),
			"jPostCode": str(sender.get("post_code") or "").replace(" ", ""),
			"jTel": sender_phone,
			"jMobile": sender_mobile,
			"jAddress": str(sender.get("address") or "").strip(),
			"jDeparture": str(sender.get("country") or "CN").strip().upper() or "CN",
		},
		"pieceorderHscodeInfos": [
			{
				"ename": ename[:100],
				"cname": cname[:100],
				"parcelQuantity": str(qty),
				"declaredValue": str(round(declared, 2)),
				"totalPrice": f"{round(declared, 2):.2f}",
				"hsCode": hs_code,
				"weight": round(weight, 3) or 0.1,
			}
		],
		"pieceorderExtendInfo": {"isSaveExtend": 0},
		"isDocall": 0,
		"pieceordercustomInfo": {},
		"pieceorderImporterInfo": {},
	}
	if sender.get("email"):
		body["pieceorderAddressInfo"]["jEmail"] = str(sender.get("email")).strip()
	if receiver.get("email"):
		body["pieceorderAddressInfo"]["dEmail"] = str(receiver.get("email")).strip()
	return body, product_code, product_name


def _ensure_official_fields_from_sf(doc, form=None):
	if form is None:
		form = _parse_form_json(doc.get("sf_form_json") if _has_field("Shipment", "sf_form_json") else None)
	if not form:
		form = {}
	if not doc.get("shipment_parcel"):
		weight = flt(form.get("total_weight")) or flt(doc.total_weight) or 0.1
		qty = cint(form.get("parcel_quantity") or 1) or 1
		per = weight / qty if qty else weight
		if per <= 0:
			per = 0.1
		doc.append(
			"shipment_parcel",
			{
				"length": flt(form.get("length")),
				"width": flt(form.get("width")),
				"height": flt(form.get("height")),
				"weight": per,
				"count": qty,
			},
		)
	total = 0
	for row in doc.get("shipment_parcel") or []:
		total += flt(row.weight) * cint(row.count or 1)
	if total > 0:
		doc.total_weight = total
	if not flt(doc.value_of_goods):
		doc.value_of_goods = flt(form.get("declared_value")) or 20
	goods = _goods_from_delivery_notes(
		[row.delivery_note for row in doc.get("shipment_delivery_note") or [] if row.delivery_note]
	)
	summary = _goods_summary(goods)
	content = str(doc.description_of_content or "").strip()
	if summary and (not content or content in ("默认", "Goods", "goods")):
		doc.description_of_content = summary
	elif not content:
		doc.description_of_content = (form.get("cname") or form.get("ename") or DEFAULT_CNAME)[:140]
	if not doc.pickup_date:
		doc.pickup_date = getdate()
	if not doc.pickup_from:
		doc.pickup_from = "09:00:00"
	if not doc.pickup_to:
		doc.pickup_to = "17:00:00"
	if not doc.pickup_address_name:
		sender = _sender_from_warehouse(doc=doc, pickup_address_name=doc.pickup_address_name)
		address_name = sender.get("address_name")
		if address_name:
			doc.pickup_address_name = address_name
			from frappe.contacts.doctype.address.address import get_address_display

			doc.pickup_address = get_address_display(address_name) or sender.get("address") or ""
	elif not doc.pickup_address:
		from frappe.contacts.doctype.address.address import get_address_display

		doc.pickup_address = get_address_display(doc.pickup_address_name) or ""


def _attach_existing_sf_waybill(doc):
	waybill = _sf_waybill(doc)
	if not waybill and _has_field("Shipment", "sf_form_json"):
		form = _parse_form_json(doc.sf_form_json)
		waybill = str((form or {}).get("existing_waybill") or "").strip()
		if waybill:
			doc.shipment_id = waybill
			doc.awb_number = waybill
	waybill = _sf_waybill(doc)
	if not waybill:
		return
	old = ""
	if not _is_new_doc(doc):
		old = str(frappe.db.get_value("Shipment", doc.name, "shipment_id") or "").strip()
		from .waybill import _assert_waybill_not_reused

		_assert_waybill_not_reused(doc, waybill, allow_current=(old == waybill))
		if old == waybill and getattr(doc, "sf_iuop_order_id", None):
			return
	elif waybill:
		from .waybill import _assert_waybill_not_reused

		_assert_waybill_not_reused(doc, waybill, allow_current=True)
	filters = {"shipment_id": waybill, "docstatus": ["<", 2]}
	if not _is_new_doc(doc):
		filters["name"] = ["!=", doc.name]
	other = frappe.db.get_value("Shipment", filters, "name")
	if other:
		frappe.throw(_("SF waybill {0} is already on shipment {1}.").format(waybill, other))
	try:
		row = query_order(waybill)
	except Exception:
		if old == waybill:
			return
		raise
	tracking = str((row or {}).get("trackingNo") or waybill).strip()
	from .waybill import _assert_waybill_not_reused

	_assert_waybill_not_reused(doc, tracking, allow_current=(old == tracking))
	order_id = _extract_iuop_order_id(row)
	doc.service_provider = "顺丰国际"
	doc.carrier = SF_PROVIDER
	doc.shipment_id = tracking
	doc.awb_number = tracking
	if order_id is not None and _has_field("Shipment", "sf_iuop_order_id"):
		doc.sf_iuop_order_id = order_id
	if hasattr(doc, "status") and cint(doc.docstatus) == 0:
		doc.status = _safe_shipment_status("Draft")
	if _has_field("Shipment", "sf_freight_status") and not doc.get("sf_freight_status"):
		doc.sf_freight_status = FREIGHT_UNSETTLED
	_clear_zero_amount(doc)


def _booking_form_for_doc(doc):
	"""Build the persisted form used by an asynchronous initial booking."""
	form = {}
	if _has_field("Shipment", "sf_form_json"):
		form = _parse_form_json(doc.get("sf_form_json"))
	if not form:
		form = _defaults_from_shipment(doc, get_settings())
	form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.get("pickup_address_name"))
	return form


def _persist_sf_order_projection(doc):
	"""Persist fields populated by a background booking worker."""
	values = {
		"service_provider": doc.get("service_provider"),
		"carrier": doc.get("carrier"),
		"carrier_service": doc.get("carrier_service"),
		"shipment_id": doc.get("shipment_id"),
		"awb_number": doc.get("awb_number"),
		"status": doc.get("status"),
	}
	for fieldname in (
		"sf_iuop_order_id",
		"sf_form_json",
		"sf_freight_status",
		"sf_label_url",
		"description_of_content",
		"total_weight",
		"pickup_date",
		"pickup_from",
		"pickup_to",
		"pickup_address",
		"pickup_address_name",
		"tracking_status",
		"tracking_status_info",
		"tracking_url",
	):
		if _has_field("Shipment", fieldname):
			values[fieldname] = doc.get(fieldname)
	values = {key: value for key, value in values.items() if value is not None}
	if values:
		doc.db_set(values, update_modified=False)


def _queue_sf_initial_booking(doc, form, *, allow_new_parent=False):
	"""Create the local attempt and enqueue carrier work after the parent commits."""
	_validate_sf_form_addresses(form)
	if getattr(doc, "flags", {}).get("flow_sf_defer_booking"):
		return None
	from .waybill import begin_initial_booking_attempt, load_initial_booking_attempt

	pending_name = str(doc.get("sf_waybill_pending_record") or "").strip()
	if pending_name:
		attempt = load_initial_booking_attempt(doc, pending_name)
	else:
		attempt = begin_initial_booking_attempt(
			doc,
			form,
			allow_new_parent=allow_new_parent,
			commit=False,
		)
	if not attempt:
		return None
	enqueue = getattr(frappe, "enqueue", None)
	if not callable(enqueue):
		frappe.throw("后台任务组件未就绪，不能安全创建顺丰面单。")
	enqueue(
		_INITIAL_BOOKING_JOB,
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=f"sf-initial-booking:{doc.name}",
		deduplicate=True,
		shipment_name=doc.name,
		attempt_name=attempt.name,
	)
	return attempt


def place_sf_order_on_save(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	if doc.get("sf_intercept_status") or doc.get("sf_carrier_cancelled"):
		# Interception records are changed only through their dedicated APIs.
		return
	if not is_enabled() or _is_cancelled(doc):
		sync_freight_status(doc, method)
		return
	if (doc.status or "") == STATUS_CANCELLED_SHIP:
		sync_freight_status(doc, method)
		return
	# ``before_save`` runs before a brand-new Shipment has a database row.  Defer
	# the carrier side effect until ``after_insert`` so the pre-request history
	# record can link to a real parent and be committed durably.
	if _is_new_doc(doc):
		form = _parse_form_json(doc.get("sf_form_json")) if _has_field("Shipment", "sf_form_json") else {}
		if not form:
			form = _defaults_from_shipment(doc, get_settings())
		existing = str((form or {}).get("existing_waybill") or "").strip()
		current = _sf_waybill(doc)
		if not existing and not current:
			_validate_sf_form_addresses(form)
		if existing and current and existing != current:
			frappe.throw("出库单中的已有顺丰运单号不一致，请只保留一个单号。")
		if existing and not current:
			doc.shipment_id = existing
			doc.awb_number = existing
		_ensure_official_fields_from_sf(doc, form)
		sync_freight_status(doc, method)
		return
	if not _is_sf_provider(doc.service_provider) and not _sf_waybill(doc):
		sync_freight_status(doc, method)
		return
	if doc.shipment_id:
		if _has_field("Shipment", "sf_form_json"):
			form = _parse_form_json(doc.get("sf_form_json"))
			existing = str((form or {}).get("existing_waybill") or "").strip()
			if existing and existing != _sf_waybill(doc):
				frappe.throw("出库单中的已有顺丰运单号不一致，请只保留一个单号。")
		if _is_sf_provider(doc.service_provider) or _is_sf_provider(doc.carrier):
			_attach_existing_sf_waybill(doc)
		_ensure_official_fields_from_sf(doc)
		sync_freight_status(doc, method)
		return
	if not _is_sf_provider(doc.service_provider):
		sync_freight_status(doc, method)
		return
	form = {}
	if _has_field("Shipment", "sf_form_json"):
		form = _parse_form_json(doc.sf_form_json)
	existing = str((form or {}).get("existing_waybill") or "").strip()
	if existing:
		doc.shipment_id = existing
		doc.awb_number = existing
		_attach_existing_sf_waybill(doc)
		_ensure_official_fields_from_sf(doc, form)
		sync_freight_status(doc, method)
		return
	if not form:
		form = _defaults_from_shipment(doc, get_settings())
	form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.pickup_address_name)
	validate_shipment_delivery_notes(doc)
	_require_submitted_delivery_note(doc)
	_ensure_official_fields_from_sf(doc, form)
	_queue_sf_initial_booking(doc, form)
	sync_freight_status(doc, method)


def book_sf_order_after_insert(doc, method=None):
	"""Book an SF label after a new Shipment has been inserted."""
	if not _is_sf_shipment(doc) or doc.get("sf_intercept_status"):
		return
	if not is_enabled() or _is_cancelled(doc) or (doc.status or "") == STATUS_CANCELLED_SHIP:
		return
	form = {}
	if _has_field("Shipment", "sf_form_json"):
		form = _parse_form_json(doc.sf_form_json)
	# A user-supplied existing label is an attachment workflow. Validate ownership
	# and carrier existence after the parent row exists; never create a second order.
	existing = str((form or {}).get("existing_waybill") or "").strip()
	current = _sf_waybill(doc)
	if existing and current and existing != current:
		frappe.throw("出库单中的已有顺丰运单号不一致，请只保留一个单号。")
	if current or existing:
		if existing and not current:
			doc.shipment_id = existing
			doc.awb_number = existing
		_attach_existing_sf_waybill(doc)
		from .waybill import ensure_waybill_record

		if not ensure_waybill_record(doc):
			frappe.throw("已有顺丰运单未能保存历史记录，暂时不能继续。")
		_persist_sf_order_projection(doc)
		return
	form = form or _defaults_from_shipment(doc, get_settings())
	form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.get("pickup_address_name"))
	validate_shipment_delivery_notes(doc)
	_require_submitted_delivery_note(doc)
	_queue_sf_initial_booking(doc, form, allow_new_parent=True)


def book_sf_order_after_commit(shipment_name, attempt_name=None):
	"""Run the first carrier request in a transaction independent of Shipment save."""
	doc = frappe.get_doc("Shipment", shipment_name, for_update=True)
	if not _is_sf_shipment(doc) or _is_cancelled(doc) or doc.get("sf_intercept_status"):
		return None
	if _sf_waybill(doc):
		return doc.get("shipment_id") or doc.get("awb_number")
	from .waybill import load_initial_booking_attempt

	if not attempt_name:
		attempt_name = doc.get("sf_waybill_pending_record")
	attempt = load_initial_booking_attempt(doc, attempt_name)
	form = _parse_form_json(attempt.get("form_payload"))
	if not form:
		form = _booking_form_for_doc(doc)
	else:
		form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.get("pickup_address_name"))
	try:
		waybill = _book_sf_order(doc, form, attempt=attempt)
		_persist_sf_order_projection(doc)
		try:
			doc.notify_update()
		except Exception:
			pass
		return waybill
	except Exception as exc:
		frappe.log_error(message=str(exc)[:2000], title="SF initial booking background job")
		raise


def _book_sf_order(doc, form: dict, *, allow_new_parent=False, attempt=None) -> str:
	if attempt and attempt.get("replacement_reason") == "Flow 整体批准创建顺丰面单":
		from .reviewed_booking import validate_reviewed_booking

		validate_reviewed_booking(doc, form, attempt)
	_require_shipping_mutation(doc)
	validate_shipment_delivery_notes(doc)
	_require_submitted_delivery_note(doc)
	body, product_code, product_name = _create_order_body_from_form(doc, form)
	from .waybill import (
		_assert_waybill_not_reused,
		begin_initial_booking_attempt,
		complete_initial_booking_attempt,
		fail_initial_booking_attempt,
	)
	if attempt is None:
		attempt = begin_initial_booking_attempt(doc, form, allow_new_parent=allow_new_parent)
	if not attempt and _is_new_doc(doc) and not allow_new_parent:
		frappe.throw("请先保存出库单，再创建顺丰面单。")
	if attempt:
		body.setdefault("pieceorderBaseInfo", {})["userOrderid"] = attempt.name
	payload = None
	data = {}
	waybill = ""
	order_id = None
	try:
		payload = create_order(body)
		if not isinstance(payload, dict):
			frappe.throw("顺丰返回了无效的下单结果。")
		data = payload.get("data") or {}
		if not isinstance(data, dict):
			data = {}
		waybill = str(data.get("trackingNo") or "").strip()
		if not waybill:
			frappe.throw(_("SF International did not return trackingNo."))
		order_id = _order_id_after_create(data, waybill)
		if attempt:
			complete_initial_booking_attempt(
				doc,
				attempt,
				payload,
				waybill,
				order_id,
				carrier_service=product_name or product_code,
				form_payload=form,
			)
		else:
			_assert_waybill_not_reused(doc, waybill)
		doc.service_provider = "顺丰国际"
		doc.carrier = SF_PROVIDER
		doc.carrier_service = product_name or product_code
		doc.shipment_id = waybill
		doc.awb_number = waybill
		if hasattr(doc, "status") and cint(doc.docstatus) == 0:
			doc.status = _safe_shipment_status("Draft")
		if order_id is not None and _has_field("Shipment", "sf_iuop_order_id"):
			doc.sf_iuop_order_id = order_id
		if _has_field("Shipment", "sf_form_json"):
			doc.sf_form_json = json.dumps(form, ensure_ascii=False)
		if _has_field("Shipment", "sf_freight_status"):
			doc.sf_freight_status = FREIGHT_UNSETTLED
		_clear_zero_amount(doc)
		_ensure_official_fields_from_sf(doc, form)
		if not attempt:
			_remember_successful_sf_receiver(doc, _form_party(form, "receiver"))
		return waybill
	except Exception as exc:
		if attempt:
			fail_initial_booking_attempt(
				doc,
				attempt,
				exc,
				payload=payload,
				waybill=waybill,
				order_id=order_id,
			)
		raise


def fill_sf_fields_before_submit(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	if not _sf_waybill(doc):
		form = _parse_form_json(doc.get("sf_form_json")) or _defaults_from_shipment(doc, get_settings())
		if not str(form.get("existing_waybill") or "").strip():
			form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.get("pickup_address_name"))
			_validate_sf_form_addresses(form)
	_ensure_official_fields_from_sf(doc)


def mark_sf_waiting_label(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	if doc.get("sf_intercept_status") or doc.get("sf_carrier_cancelled"):
		return
	if (doc.status or "") in CANCELLED_STATUSES | {STATUS_SHIPPED, "Completed"}:
		return
	doc.status = _safe_shipment_status(STATUS_WAIT_LABEL)
	doc.db_set("status", _safe_shipment_status(STATUS_WAIT_LABEL))
	try:
		from .waybill import ensure_waybill_record

		ensure_waybill_record(doc)
	except Exception:
		frappe.log_error(title="SF waybill history materialization after submit")


def align_sf_status(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	if doc.get("sf_intercept_status") or doc.get("sf_carrier_cancelled"):
		return
	if cint(doc.docstatus) != 1:
		return
	if (doc.status or "") in CANCELLED_STATUSES | {STATUS_SHIPPED, "Completed", STATUS_WAIT_LABEL}:
		return
	doc.status = _safe_shipment_status(STATUS_WAIT_LABEL)
	doc.db_set("status", _safe_shipment_status(STATUS_WAIT_LABEL), update_modified=False)


def _sf_waybill(doc) -> str:
	return str(doc.shipment_id or doc.awb_number or "").strip()




def cancel_sf_order_on_cancel(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	stored = frappe.get_doc("Shipment", doc.name, for_update=True)
	from .interception import may_cancel
	history_resolved = False
	if _sf_waybill(stored):
		if method == "on_trash":
			frappe.throw("已有顺丰运单号的记录必须保留，用于核对后续运费和取消记录。")
	if method in {"before_cancel", "before_discard"}:
		# Check every immutable child row, including labels that are no longer the
		# parent's current projection.
		from .waybill import assert_all_waybills_cancelled

		history_resolved = bool(assert_all_waybills_cancelled(stored))
		if not history_resolved and _sf_waybill(stored) and not may_cancel(stored):
			frappe.throw("顺丰尚未明确确认取消，不能取消本地运单。已发出的包裹请先联系顺丰客服、在系统记录其确认成功的反馈，再核对顺丰取消状态。")
	if method == "on_trash":
		from .waybill import has_waybill_history

		if has_waybill_history(stored):
			frappe.throw("已有顺丰运单历史记录必须保留，用于核对后续运费和取消记录。")
	if method == "on_cancel" and hasattr(doc, "status"):
		doc.db_set("status", _safe_shipment_status(STATUS_CANCELLED_SHIP))


@frappe.whitelist()
def cancel_sf_shipment(shipment: str):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	doc.check_permission("cancel")
	if _is_cancelled(doc):
		frappe.throw(_("Cancelled shipments cannot be dispatched."))
	if not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	waybill = _sf_waybill(doc)
	if not waybill:
		frappe.throw(_("No SF waybill to cancel."))
	from .client import query_order_cancellation
	from .interception import (
		CARRIER_PENDING_STATE,
		may_cancel,
		requires_manual_success,
		store_cancellation_pending,
		store_confirmation,
	)
	if may_cancel(doc):
		return {"ok": True, "waybill": waybill, "message": "顺丰已确认取消，请继续办理本地运单取消。"}
	if doc.get("sf_intercept_status") == CARRIER_PENDING_STATE:
		try:
			confirmed = store_confirmation(doc, query_order_cancellation(waybill))
		except Exception:
			frappe.log_error(title="SF International cancellation status query")
			confirmed = False
		return {
			"ok": confirmed,
			"waybill": waybill,
			"message": "顺丰已确认取消，原运单号和运费记录已保留。请继续办理本地运单取消。" if confirmed else "顺丰取消仍待接口确认，请稍后再次核对；系统未重复提交取消请求。",
		}
	if requires_manual_success(doc) or doc.get("sf_intercept_status"):
		frappe.throw("已发出或正在拦截的包裹不能直接取消面单；请先联系顺丰客服处理拦截，再核对顺丰取消状态。")
	# Persist the pending state before the external side effect. A timeout or a
	# process restart must never make the cancel button reusable.
	store_cancellation_pending(doc)
	try:
		commit = getattr(getattr(frappe, "db", None), "commit", None)
		if callable(commit):
			commit()
	except Exception as exc:
		frappe.log_error(message=str(exc)[:1000], title="SF International cancellation pending state")
		return {
			"ok": False,
			"pending": True,
			"waybill": waybill,
			"message": "取消状态暂未成功保存，系统未向顺丰提交取消请求；请稍后重试。",
		}
	try:
		cancel_order([waybill])
	except Exception as exc:
		# The request may have reached the carrier even when no response arrived.
		# Keep the pending marker and force the next click down the read-back path.
		frappe.log_error(message=str(exc)[:1000], title="SF International cancellation request")
		return {
			"ok": False,
			"pending": True,
			"waybill": waybill,
			"message": "取消请求结果待核实；系统已保留待确认状态，不会重复提交。请稍后核对顺丰取消状态。",
		}
	try:
		confirmed = store_confirmation(doc, query_order_cancellation(waybill))
	except Exception:
		frappe.log_error(title="SF International cancellation status query")
		confirmed = False
	return {
		"ok": confirmed,
		"waybill": waybill,
		"message": "顺丰已确认取消，原运单号和运费记录已保留。请继续办理本地运单取消。" if confirmed else "取消请求已提交，但尚未查询到明确取消状态。请稍后核对顺丰取消状态，暂时不能取消本地单据。",
	}


@frappe.whitelist()
def recreate_sf_shipment(shipment: str, form_json=None):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_require_shipping_mutation(doc)
	if _is_cancelled(doc):
		frappe.throw(_("Cancelled shipments cannot be dispatched."))
	if not _is_sf_provider(doc.service_provider):
		frappe.throw(_("Not an SF International shipment."))
	if not is_enabled():
		frappe.throw(_("Enable SF International in SF International Settings."))
	if _sf_waybill(doc):
		frappe.throw(_("Cancel the existing SF label first."))
	form = _parse_form_json(form_json) if form_json else {}
	if not form and _has_field("Shipment", "sf_form_json"):
		form = _parse_form_json(doc.get("sf_form_json"))
	if not form:
		form = _defaults_from_shipment(doc, get_settings())
	form["sender"] = _sender_from_warehouse(doc=doc, pickup_address_name=doc.pickup_address_name)
	waybill = _book_sf_order(doc, form)
	if cint(doc.docstatus) == 1:
		doc.status = _safe_shipment_status(STATUS_WAIT_LABEL)
	updates = {
		"service_provider": doc.service_provider,
		"carrier": doc.carrier,
		"carrier_service": doc.carrier_service,
		"shipment_id": doc.shipment_id,
		"awb_number": doc.awb_number,
		"status": doc.status,
	}
	if _has_field("Shipment", "sf_iuop_order_id"):
		updates["sf_iuop_order_id"] = doc.get("sf_iuop_order_id") or ""
	if _has_field("Shipment", "sf_form_json"):
		updates["sf_form_json"] = doc.get("sf_form_json") or ""
	if _has_field("Shipment", "sf_label_url"):
		updates["sf_label_url"] = ""
	if hasattr(doc, "description_of_content"):
		updates["description_of_content"] = doc.description_of_content
	doc.db_set(updates)
	return {"ok": True, "shipment_id": waybill, "status": doc.status}


def sync_freight_status(doc, method=None):
	if not _is_sf_shipment(doc):
		return
	booked = bool(doc.shipment_id)
	is_sf = _is_sf_shipment(doc)
	if not booked:
		# A draft without a label still represents a deliberate SF booking intent.
		# Keep the provider fields while the durable ``SF Waybill`` attempt is
		# queued; clearing them here makes the after-commit worker reload a non-SF
		# Shipment and leaves the attempt stuck at 创建中. The fields are only
		# changed by an explicit provider switch or by the successful projection.
		_clear_zero_amount(doc)
		if _has_field("Shipment", "sf_freight_status"):
			doc.sf_freight_status = None
		return

	if is_sf and _has_field("Shipment", "sf_freight_status"):
		doc.sf_freight_status = FREIGHT_SETTLED if _has_current_freight_evidence(doc) else FREIGHT_UNSETTLED
		if _has_field("Shipment", "sf_freight_accounting_status"):
			doc.sf_freight_accounting_status = _freight_accounting_state(doc)


def _freight_result(doc, message, *, review_required=False, query_failed=False):
	bill = _retained_freight_bill(doc)
	return {
		"status": FREIGHT_SETTLED if bill else FREIGHT_UNSETTLED,
		"amount": doc.get("shipment_amount"),
		"confirmed_amount": bill["amount"] if bill else None,
		"currency": bill["currency"] if bill else doc.get("sf_freight_currency"),
		"journal": doc.get("sf_freight_journal"),
		"accounting_status": doc.get("sf_freight_accounting_status") or "待核实",
		"accounting_hold": bool(freight_accounting.booking_is_held(doc)),
		"query_status": doc.get("sf_freight_query_status"),
		"review_required": review_required,
		"query_failed": query_failed,
		"message": message,
	}


def resume_freight_booking(doc, reason):
	"""Called only inside the correction endpoint's permission-checked scope."""
	freight_accounting.assert_can_book(doc)
	bill = _retained_freight_bill(doc)
	if not bill:
		frappe.throw("尚无可核对的有效运费账单，不能恢复自动记账；请先查询并核实账单。")
	if bill["amount"] == 0:
		if doc.get("sf_freight_journal"):
			frappe.throw("零金额账单仍有关联凭证，请先完成原凭证更正。")
		doc.db_set({"sf_freight_accounting_status": "无需记账", "sf_freight_accounting_note": "已核实零金额账单，保留原凭证更正历史。"})
		return {"journal": None, "journal_entry": None, "amount": 0, "accounting_status": "无需记账"}
	journal = _book_sf_freight_journal(doc, bill["amount"], bill["currency"])
	if getattr(doc, "_sf_freight_review_required", False):
		frappe.throw("现有凭证与账单金额不一致，请先完成凭证更正。")
	if not journal:
		frappe.throw("运费凭证未生成，记账暂停状态保持不变。")
	entry = frappe.db.get_value("Journal Entry", journal, ["docstatus", "total_debit"], as_dict=True)
	if not entry or cint(entry.docstatus) != 1 or abs(flt(entry.total_debit) - bill["amount"]) >= 0.005:
		frappe.throw("凭证未完成有效记账，不能恢复自动记账。")
	doc.db_set({"sf_freight_journal": journal, "sf_freight_accounting_status": "已记账", "sf_freight_accounting_note": ""})
	return {"journal": journal, "journal_entry": journal, "amount": bill["amount"]}


@frappe.whitelist()
def fetch_sf_freight(shipment):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_reject_manual_shipping_api(doc)
	if not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	if cint(doc.docstatus) not in (1, 2):
		frappe.throw(_("Submit the shipment before querying freight."))
	waybill = doc.shipment_id or doc.awb_number
	if not waybill:
		frappe.throw(_("No SF waybill to query."))
	begin, end = _freight_window(doc)
	try:
		payload = query_case_orders(waybill, begin, end)
		amount, currency, records = _sum_pay_amount(payload)
	except Exception as exc:
		frappe.log_error(title="SF freight bill query failed")
		_apply_freight(doc, None, "", [], query_status="查询失败", accounting_status=_freight_accounting_state(doc))
		_sync_active_waybill_freight(
			doc, None, "", [], {"waybill": waybill, "last_query_error": str(exc)[:500]},
			query_status="查询失败", error=exc,
		)
		return _freight_result(doc, "本次运费账单查询失败，已有账单和记账记录保留。", query_failed=True)
	if amount is None:
		_apply_freight(doc, amount, currency, records, payload, accounting_status=_freight_accounting_state(doc))
		_sync_active_waybill_freight(
			doc, amount, currency, records, payload,
			query_status="本次未查到账单", accounting_status=_freight_accounting_state(doc),
			accounting_hold=doc.get("sf_freight_accounting_hold"),
		)
		return _freight_result(doc, "本次未查到账单，已有运费金额、有效账单和凭证记录保留。")
	# Store carrier evidence before attempting accounting. A missing account,
	# permission error, or transient ERP failure must not discard the bill.
	_apply_freight(doc, amount, currency, records, payload, accounting_status="待记账")
	if freight_accounting.booking_is_held(doc):
		doc.db_set({"sf_freight_accounting_status": "记账待处理", "sf_freight_accounting_hold": 1})
		_sync_active_waybill_freight(
			doc, amount, currency, records, payload,
			accounting_status="记账待处理", accounting_hold=1,
		)
		return _freight_result(doc, "账单已取得。运费记账已暂停，请通过运费更正流程核实并恢复记账。", review_required=True)
	if amount == 0:
		accounting_status = "待复核" if doc.get("sf_freight_journal") else "无需记账"
		_apply_freight(doc, amount, currency, records, payload, accounting_status=accounting_status)
		_sync_active_waybill_freight(
			doc, amount, currency, records, payload,
			accounting_status=accounting_status, accounting_hold=doc.get("sf_freight_accounting_hold"),
		)
		return _freight_result(doc, "已取得零金额账单；已有凭证保留，需核对费用调整。" if doc.get("sf_freight_journal") else "已取得零金额账单，无需创建运费凭证。", review_required=bool(doc.get("sf_freight_journal")))
	frappe.db.savepoint("sf_freight_accounting")
	try:
		journal = _book_sf_freight_journal(doc, amount, currency)
	except Exception as exc:
		frappe.db.rollback(save_point="sf_freight_accounting")
		note = str(exc)[:1000]
		_apply_freight(doc, amount, currency, records, payload, accounting_status="待复核", accounting_note=note)
		_sync_active_waybill_freight(
			doc, amount, currency, records, payload,
			accounting_status="待复核", accounting_hold=doc.get("sf_freight_accounting_hold"),
		)
		frappe.log_error(title="SF freight accounting failed")
		return _freight_result(doc, "账单已保存，但记账未完成，请检查记账复核说明。", review_required=True)
	review_required = bool(getattr(doc, "_sf_freight_review_required", False))
	if not review_required and journal:
		# _book_sf_freight_journal locks/reloads the document, so inspect the
		# linked posted entry here as well to carry the review signal back to the
		# response object used by the request.
		try:
			linked = frappe.get_doc("Journal Entry", journal)
			if cint(linked.docstatus) == 1:
				posted = sum(flt(row.debit_in_account_currency) for row in linked.accounts or [])
				review_required = abs(posted - amount) >= 4.999e-3
		except Exception:
			pass
	accounting_status = "待复核" if review_required else "已记账"
	doc.sf_freight_journal = journal
	accounting_note = (
		_("The carrier amount differs from the existing posted journal; review before making an adjustment.")
		if review_required
		else ""
	)
	_apply_freight(
		doc,
		amount,
		currency,
		records,
		payload,
		accounting_status=accounting_status,
		accounting_note=accounting_note,
	)
	_sync_active_waybill_freight(
		doc, amount, currency, records, payload,
		accounting_status=accounting_status, accounting_hold=doc.get("sf_freight_accounting_hold"),
	)
	if review_required and _has_field("Shipment", "sf_freight_payload"):
		# Persist the discrepancy alongside the carrier response so a later
		# accounting review can see why the posted journal was left untouched.
		try:
			stored = json.loads(doc.get("sf_freight_payload") or "{}")
			if isinstance(stored, dict):
				stored["accounting_review_required"] = True
				doc.db_set("sf_freight_payload", json.dumps(stored, ensure_ascii=False, default=str))
		except (TypeError, ValueError):
			pass
	return _freight_result(doc,
		"账单金额已变化，原凭证保留，请进行运费更正复核。" if review_required else "账单已取得，运费已记账；付款情况需另行核对。",
		review_required=review_required,
	)


@frappe.whitelist()
def create_shipment(
	shipment,
	pickup_from_type,
	delivery_to_type,
	pickup_address_name,
	delivery_address_name,
	shipment_parcel,
	description_of_content,
	pickup_date,
	value_of_goods,
	service_data,
	shipment_notific_email=None,
	tracking_notific_email=None,
	pickup_contact_name=None,
	delivery_contact_name=None,
	delivery_notes=None,
):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_reject_manual_shipping_api(doc)
	if _is_cancelled(doc):
		frappe.throw(_("Cancelled shipments cannot be changed."))
	_require_shipping_mutation(doc)
	if delivery_notes:
		delivery_notes = _checked_delivery_notes(doc, delivery_notes, permission="write")
	service_info = json.loads(service_data) if isinstance(service_data, str) else service_data
	if not isinstance(service_info, dict):
		service_info = {}
	_reject_manual_shipping_api(service_info)
	if _sf_waybill(doc) and _is_sf_shipment(doc) != _is_sf_provider(service_info.get("service_provider")):
		frappe.throw(_("Cancel the existing carrier booking before changing shipping providers."))
	if isinstance(delivery_notes, str):
		delivery_notes = json.loads(delivery_notes)
	if _is_sf_provider(service_info.get("service_provider")):
		already_booked = bool(_sf_waybill(doc))
		info = create_sf_shipment(
			shipment=shipment,
			pickup_from_type=pickup_from_type,
			pickup_address_name=pickup_address_name,
			delivery_address_name=delivery_address_name,
			shipment_parcel=shipment_parcel,
			description_of_content=description_of_content,
			value_of_goods=value_of_goods,
			service_info=service_info,
			pickup_contact_name=pickup_contact_name,
			delivery_contact_name=delivery_contact_name,
			delivery_notes=delivery_notes,
		)
		if already_booked:
			return info
		doc = frappe.get_doc("Shipment", shipment)
		doc.check_permission("write")
		updates = {
			"service_provider": info["service_provider"],
			"carrier": info["carrier"],
			"carrier_service": info["carrier_service"],
			"shipment_id": info["shipment_id"],
			"awb_number": info["awb_number"],
			"status": "Booked",
		}
		if info.get("sf_iuop_order_id") is not None and _has_field("Shipment", "sf_iuop_order_id"):
			updates["sf_iuop_order_id"] = info["sf_iuop_order_id"]
		if _has_field("Shipment", "sf_freight_status"):
			updates["sf_freight_status"] = FREIGHT_UNSETTLED
		doc.db_set(updates)
		# create_sf_shipment has already finalized the immutable history row before
		# returning.  A missing row here indicates a broken installation and must be
		# visible to the operator rather than silently creating an untracked order.
		try:
			from .waybill import ensure_waybill_record

			if not ensure_waybill_record(doc):
				frappe.throw("顺丰面单历史未保存，暂时不能完成出库单下单。")
		except ImportError:
			frappe.throw("顺丰面单历史模块未安装，暂时不能完成出库单下单。")
		except Exception as exc:
			frappe.log_error(message=str(exc)[:2000], title="SF waybill history materialization after create")
			frappe.throw("顺丰面单历史保存失败，订单已进入待核实状态，请先核对后再操作。")
		return info

	frappe.throw(_("Not an SF International shipment."))


@frappe.whitelist()
def print_shipping_label(shipment: str):
	doc = frappe.get_doc("Shipment", shipment)
	doc.check_permission("write")
	_reject_manual_shipping_api(doc)
	if _is_sf_shipment(doc):
		return print_sf_label(shipment)
	frappe.throw(_("Not an SF International shipment."))


@frappe.whitelist()
def update_tracking(shipment, service_provider, shipment_id, delivery_notes=None):
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	_reject_manual_shipping_api(doc)
	if delivery_notes:
		delivery_notes = _checked_delivery_notes(doc, delivery_notes, permission="write")
	if _is_sf_shipment(doc):
		_require_active_sf_shipment(doc)
		tracking = track_sf_shipment(shipment, shipment_id, doc.delivery_contact_name)
		if not tracking or tracking.get("route_count") == 0:
			return tracking
		doc.db_set(
			{
				"awb_number": tracking.get("awb_number"),
				"tracking_status": _official_tracking_status(tracking.get("tracking_status")),
				"tracking_status_info": tracking.get("tracking_status_info"),
				"tracking_url": tracking.get("tracking_url"),
			}
		)
		# 补填已有顺丰单号：查到实质轨迹则自动标已发货，方便走已发货换单等流程。
		_maybe_mark_shipped_from_tracking(doc, tracking)
		try:
			from .waybill import sync_tracking

			sync_tracking(doc, tracking)
		except Exception:
			frappe.log_error(title="SF waybill tracking history synchronization")
		if delivery_notes:
			from erpnext.stock.doctype.shipment.delivery_note_update import update_delivery_note

			if isinstance(delivery_notes, str):
				delivery_notes = json.loads(delivery_notes)
			update_delivery_note(delivery_notes=delivery_notes, tracking_info=tracking)
		return tracking

	frappe.throw(_("Not an SF International shipment."))


def update_tracking_info(*args, **kwargs):
	return update_tracking(*args, **kwargs)
