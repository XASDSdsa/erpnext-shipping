"""SF display values consumed by ERPNext's Shipment carrier adapter."""

from frappe.utils import cint


def get_display_values(doc):
	confirmed = doc.get("sf_freight_status") == "账单已取得"
	bill = "账单已取得" if confirmed else "账单待核实"
	accounting = (
		"记账待处理" if cint(doc.get("sf_freight_accounting_hold")) else doc.get("sf_freight_accounting_status")
	)
	values = {"freight_status_display": " · ".join(value for value in (bill, accounting) if value)}

	interception = str(doc.get("sf_intercept_status") or "").strip()
	carrier_cancelled = cint(doc.get("sf_carrier_cancelled")) == 1 or interception == "顺丰已取消"
	values["interception_status_display"] = (
		"顺丰已取消，待取消本地运单" if carrier_cancelled else interception
	)

	waybill = str(doc.get("shipment_id") or doc.get("awb_number") or "").strip()
	replacement = str(doc.get("sf_waybill_replacement_status") or "").strip() or ("当前" if waybill else "")
	values["label_replacement_display"] = " ".join(value for value in (replacement, waybill) if value)
	return values
