"""SF bill and accounting presentation for the native ERPNext freight summary interface."""

from frappe import _
from frappe.utils import cint, fmt_money


def freight_presentation(info):
	"""Only confirmed SF bill amounts are displayed; historical charges are not new bills."""
	total = cint(info.get("total"))
	if not total:
		return None
	billed = cint(info.get("billed") if info.get("billed") is not None else info.get("confirmed"))
	booked = cint(info.get("booked") if info.get("booked") is not None else info.get("settled"))
	held = cint(info.get("held"))
	review = cint(info.get("review"))
	bill = _("账单已取得") if billed >= total else _("部分账单已取得") if billed else _("账单待核实")
	accounting = (
		_("记账待处理 {0}").format(held) if held
		else _("待复核 {0}").format(review) if review
		else _("已记账 {0}/{1}").format(booked, total)
	)
	amount = info.get("billed_amount")
	currency = info.get("currency")
	# The SF source leaves billed_amount empty when currencies differ. Never
	# relabel that mixture with a default currency or use a legacy amount instead.
	amount_text = f"{fmt_money(amount, currency=currency)} {currency}" if amount is not None and currency else ""
	return {
		"text": bill,
		"color": "green" if billed >= total and not held and not review else "orange",
		"extra": " · ".join(filter(None, [accounting, amount_text])),
		"title": " · ".join(filter(None, [
			_("已取得账单 {0}/{1}").format(billed, total),
			_("已记账 {0}/{1}").format(booked, total),
			_("记账待处理 {0}").format(held) if held else "",
			_("待复核 {0}").format(review) if review else "",
			_("账单金额 {0}").format(amount_text) if amount_text else "",
		])),
	}


def get_sales_order_freight_summary(names):
	from erpnext_shipping.sf_international.shipping import get_sales_order_freight_map

	# The SF reader filters Shipment identity and enforces linked-record permissions.
	return {
		name: row for name, info in get_sales_order_freight_map(names).items()
		if (row := freight_presentation(info)) is not None
	}
