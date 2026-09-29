"""Install SF International provider metadata without changing business records.

SF configuration, addresses, freight and waybill history belong to Shipping.
Generic Shipment fields belong to ERPNext. Existing-site ownership migration is
separate and must preserve credentials, documents and accounting links.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, flt

from .products import IECS_PRODUCTS

SF_SETTINGS = "SF International Settings"


def after_install():
    """Create provider metadata and seed product codes only for a fresh setup."""
    sync_metadata()
    settings = frappe.get_single(SF_SETTINGS)
    if not settings.get("products"):
        seed_iecs_products()


def sync_metadata():
    """Keep native extension metadata current; do not repair shipment history."""
    ensure_custom_fields()
    ensure_create_label_section_labels()
    ensure_sf_shipment_metadata()


def get_custom_fields():
    """Return the SF field contract captured from the verified running schema.

    Freight status retains historical options and adds the two statuses written
    by shipping.sync_freight_status; updating its schema never rewrites records.
    """
    import json
    from pathlib import Path

    return json.loads(Path(__file__).with_name("custom_fields.json").read_text())


def ensure_custom_fields():
    create_custom_fields(get_custom_fields(), ignore_validate=True)


def ensure_create_label_section_labels():
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	if frappe.db.exists("Custom Field", "Shipment-sf_actions_section"):
		frappe.db.set_value(
			"Custom Field",
			"Shipment-sf_actions_section",
			{"insert_after": "tracking_status_info", "hidden": 0, "label": " "},
			update_modified=False,
		)
	if frappe.db.exists("Custom Field", "Shipment-sf_actions_html"):
		frappe.db.set_value(
			"Custom Field",
			"Shipment-sf_actions_html",
			{"insert_after": "sf_actions_section", "label": " "},
			update_modified=False,
		)
	make_property_setter(
		"Shipment",
		"sf_actions_html",
		"label",
		" ",
		"Data",
		validate_fields_for_doctype=False,
	)
	frappe.clear_cache(doctype="Shipment")


def ensure_sf_shipment_metadata():
	"""Add only the SF carrier choice; ERPNext owns the Shipment form and schema."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	provider = frappe.get_meta("Shipment", cached=False).get_field("service_provider")
	if not provider or provider.fieldtype != "Select":
		return {"doctype": "Shipment", "provider": "顺丰国际", "changed": False}
	setters = frappe.get_all(
		"Property Setter",
		filters={"doc_type": "Shipment", "field_name": "service_provider", "property": "options"},
		fields=["name", "value"],
	)
	changed = False
	if setters:
		for setter in setters:
			options = (setter.get("value") or "").split("\n")
			if "顺丰国际" not in options:
				frappe.db.set_value("Property Setter", setter["name"], "value", "\n".join([*options, "顺丰国际"]))
				changed = True
	else:
		options = (provider.options or "").split("\n")
		if "顺丰国际" not in options:
			make_property_setter(
				"Shipment", "service_provider", "options", "\n".join([*options, "顺丰国际"]), "Text",
				validate_fields_for_doctype=False,
			)
			changed = True
	if changed:
		frappe.clear_cache(doctype="Shipment")
	return {"doctype": "Shipment", "provider": "顺丰国际", "changed": changed}


def _all_int_codes(rows) -> bool:
	codes = [str(row.product_code or "").strip() for row in rows]
	live = [code for code in codes if code]
	return bool(live) and all(code.upper().startswith("INT") for code in live)


def seed_iecs_products():
	if not frappe.db.exists("DocType", SF_SETTINGS):
		return
	doc = frappe.get_single(SF_SETTINGS)
	rows = doc.get("products") or []
	if _all_int_codes(rows):
		doc.set("products", [])
		rows = []
	existing = {str(row.product_code or "").strip() for row in (doc.get("products") or [])}
	changed = not existing
	for code, name, preferred in IECS_PRODUCTS:
		if code in existing:
			continue
		doc.append(
			"products",
			{
				"product_code": code,
				"product_name": name,
				"is_preferred": preferred if not existing else 0,
			},
		)
		existing.add(code)
		changed = True
	if not any(cint(row.is_preferred) for row in doc.get("products") or []):
		for row in doc.get("products") or []:
			if str(row.product_code).strip() == "10":
				row.is_preferred = 1
				changed = True
				break
	if not doc.declared_currency:
		doc.declared_currency = "CNY"
		changed = True
	if not doc.default_hs_code:
		doc.default_hs_code = "9504200090"
		changed = True
	if not (doc.get("default_ename") or "").strip():
		doc.default_ename = "Billiard goods"
		changed = True
	if not (doc.get("default_cname") or "").strip():
		doc.default_cname = "台球体育用品"
		changed = True
	if not flt(doc.get("default_declared_value")):
		doc.default_declared_value = 20
		changed = True
	if changed:
		doc.flags.ignore_validate = True
		doc.save(ignore_permissions=True)
