"""SF's explicit extension of the native ERPNext Shipment carrier contract.

This adapter only reads SF identity, booking history and display state. Carrier
booking and cancellation remain independent, explicit operations in the SF app.
"""

import re

import frappe

from erpnext.stock.doctype.shipment.manual_shipping import is_manual

from erpnext_shipping.sf_international.shipment_display import get_display_values

SF_ALIASES = {"sf international", "sf", "顺丰国际", "国际顺丰", "sf国际", "sf global", "sfglobal"}


def matches(doc):
	if is_manual(doc):
		return False
	# An explicit provider owns the Shipment. The carrier text is a legacy
	# fallback only and must not override a later provider selection.
	provider = str(doc.get("service_provider") or "").strip() or doc.get("carrier")
	return re.sub(r"[_-]+", " ", str(provider or "").strip().lower()) in SF_ALIASES


def has_booking(doc):
	# History must still be checked after an attempted provider change. A changed
	# provider cannot erase SF ownership of an existing booking or its history.
	if any(doc.get(fieldname) for fieldname in ("sf_active_waybill_record", "sf_iuop_order_id", "sf_label_url")):
		return True
	if matches(doc) and (doc.get("shipment_id") or doc.get("awb_number")):
		return True
	return bool(
		doc.get("name")
		and frappe.db.exists("DocType", "SF Waybill")
		and frappe.db.exists("SF Waybill", {"shipment": doc.get("name")})
	)


def get_delivery_note_summary(doc):
	"""Extend a permission-checked native summary with SF facts only."""
	from frappe.utils import cint

	interception = str(doc.get("sf_intercept_status") or "")
	carrier_cancelled = cint(doc.get("sf_carrier_cancelled")) == 1
	return {
		**{
			fieldname: doc.get(fieldname)
			for fieldname in (
				"sf_freight_status", "sf_freight_currency", "sf_freight_accounting_status",
				"sf_freight_accounting_hold", "sf_freight_query_status",
			)
		},
		"sf_intercept_status": interception,
		"sf_carrier_cancelled": carrier_cancelled,
		"sf_interception": {"state": interception, "carrier_cancelled": carrier_cancelled},
	}


def get_sales_order_freight_summary(names):
	"""Supply SF-only bill presentation to the native carrier-neutral list interface."""
	from erpnext_shipping.sf_international.freight_summary import get_sales_order_freight_summary as summarize

	return summarize(names)
