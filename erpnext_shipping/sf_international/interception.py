"""Manual package interception workflow.

The carrier has no interception endpoint in this integration. These methods
record contact with SF customer service and only persist carrier confirmation
after a strict, same-waybill status query.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, escape_html, now_datetime

from .client import cancellation_evidence, query_order_cancellation

SF_SUPPORT_PROCESSING = "顺丰客服处理中"
SF_SUPPORT_SUCCESS = "顺丰客服确认成功待顺丰确认"
SF_SUPPORT_FAILED = "顺丰客服反馈失败"
# Keep accepting the values written by the first version of this workflow.
LEGACY_STATE_MAP = {
	"客服处理中": SF_SUPPORT_PROCESSING,
	"客服确认成功待顺丰确认": SF_SUPPORT_SUCCESS,
	"拦截失败": SF_SUPPORT_FAILED,
}
MANUAL_STATES = {SF_SUPPORT_PROCESSING, SF_SUPPORT_SUCCESS, SF_SUPPORT_FAILED}
SUCCESS_STATES = {SF_SUPPORT_SUCCESS, "客服确认成功待顺丰确认"}
REQUEST_STATE = "申请中"
# A carrier cancel request can be accepted before the carrier's order query
# exposes the final cancelled state. Keep that intermediate state distinct
# from both manual SF support feedback and a confirmed carrier cancellation.
CARRIER_PENDING_STATE = "顺丰取消待确认"
CARRIER_STATE = "顺丰已取消"
# Only an in-flight parcel can be handed to SF customer service for
# interception. Delivered, returned, lost, and completed parcels need a
# separate after-sales/return process and must never expose this action.
INTERCEPTABLE_STATUSES = {
	"已发货", "已揽收", "运输中", "派送中", "Shipped", "In Progress",
}
TERMINAL_TRANSPORT_STATUSES = {
	"Completed", "已签收", "已退回", "已丢失", "Delivered", "Returned", "Lost",
}
TERMINAL_TRACKING_STATUSES = {"Delivered", "Returned", "Lost", "已签收", "已退回", "已丢失", "已送达"}
PROTECTED_FIELDS = (
	"sf_intercept_status", "sf_intercept_reason", "sf_intercept_note",
	"sf_intercept_requested_by", "sf_intercept_requested_at", "sf_intercept_assigned_to",
	"sf_carrier_cancelled", "sf_carrier_cancelled_waybill", "sf_carrier_cancelled_at",
	"sf_carrier_cancel_payload",
)


def has_dispatch_evidence(doc):
	"""Return whether the carrier has evidence that the parcel left origin.

	``Booked`` is only an accepted order.  A route query with no events writes
	that value and must not be treated as a shipped parcel.  In-transit labels
	count only when the carrier supplied a route description; this keeps the
	interception and cancellation paths consistent with the waybill workflow.
	"""
	status = str(doc.get("status") or "").strip()
	if status in INTERCEPTABLE_STATUSES | TERMINAL_TRANSPORT_STATUSES:
		return True
	tracking = str(doc.get("tracking_status") or "").strip()
	if tracking in TERMINAL_TRACKING_STATUSES or tracking in {"Shipped", "已发货"}:
		return True
	return tracking in {"In Progress", "运输中", "派送中", "已揽收"} and bool(
		str(doc.get("tracking_status_info") or "").strip()
	)


def carrier_confirmed(doc):
	try:
		evidence = json.loads(doc.get("sf_carrier_cancel_payload") or "{}")
	except (TypeError, ValueError):
		return False
	if not isinstance(evidence, dict):
		return False
	return bool(
		cint(doc.get("sf_carrier_cancelled")) == 1
		and _waybill(doc)
		and doc.get("sf_carrier_cancelled_waybill") == _waybill(doc)
		and doc.get("sf_carrier_cancelled_at")
		and doc.get("sf_carrier_cancel_payload")
		and evidence.get("waybill") == _waybill(doc)
		and cancellation_evidence(evidence, _waybill(doc))
	)


def requires_manual_success(doc):
	return has_dispatch_evidence(doc)


def is_interceptable(doc):
	status = str(doc.get("status") or "").strip()
	tracking = str(doc.get("tracking_status") or "").strip()
	if status in TERMINAL_TRANSPORT_STATUSES or tracking in TERMINAL_TRACKING_STATUSES:
		return False
	return status in INTERCEPTABLE_STATUSES or (
		tracking in {"In Progress", "运输中", "派送中", "已揽收"}
		and bool(str(doc.get("tracking_status_info") or "").strip())
	)


def interception_unavailable_message(doc):
	status = str(doc.get("status") or "").strip()
	tracking = str(doc.get("tracking_status") or "").strip()
	if status in TERMINAL_TRANSPORT_STATUSES or tracking in TERMINAL_TRACKING_STATUSES:
		return "包裹已进入签收、退回或丢失等终态，不能申请拦截；请按售后或退货流程处理。"
	return "包裹尚未发出，请直接取消顺丰面单；只有已发出或已有物流状态的包裹才进入拦截流程。"


def may_cancel(doc):
	state = doc.get("sf_intercept_status") or ""
	if state == CARRIER_PENDING_STATE:
		return False
	if requires_manual_success(doc) and state != CARRIER_STATE:
		return False
	return carrier_confirmed(doc) and state in {"", CARRIER_STATE}


def validate_state(doc, method=None):
	previous = doc.get_doc_before_save()
	is_new = doc.is_new() if callable(getattr(doc, "is_new", None)) else previous is None
	if is_new:
		for field in PROTECTED_FIELDS:
			value = doc.get(field)
			forged = cint(value) == 1 if field == "sf_carrier_cancelled" else value not in (None, "", 0, False)
			if forged:
				frappe.throw("顺丰取消确认和拦截记录不能在新建单据时手工填写。")
		return
	if not previous:
		return
	for field in PROTECTED_FIELDS:
		if doc.has_value_changed(field):
			frappe.throw("拦截和顺丰确认字段只能通过专用处理按钮更新。")
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(previous):
		return
	if _waybill(previous):
		for field in ("shipment_id", "awb_number", "sf_iuop_order_id", "service_provider", "carrier"):
			if (doc.get(field) or "") != (previous.get(field) or ""):
				frappe.throw("已有顺丰运单的号码和承运商必须保留；重新发货请建立新的运单。")
		if doc.get("status") != previous.get("status") and cint(doc.docstatus) != 2:
			frappe.throw("已有顺丰运单的发货状态只能通过专用处理按钮更新。")
	if previous.get("sf_intercept_status") and cint(doc.docstatus) != 2:
		# Read/query endpoints and comments are separate from document edits.
		ignored = {"modified", "modified_by", "_user_tags", "_comments", "_assign", "_liked_by"}
		for field in doc.meta.fields:
			if field.fieldname in ignored or field.fieldtype in {"Section Break", "Column Break", "Tab Break", "HTML", "Button"}:
				continue
			if doc.has_value_changed(field.fieldname):
				frappe.throw("拦截处理中不能修改运单业务内容；可继续记录顺丰客服反馈、核对物流和查询运费。")


def onload(doc, method=None):
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		return
	active = cint(doc.docstatus) == 1 and bool(_waybill(doc))
	write = active and frappe.has_permission("Shipment", "write", doc=doc)
	state = doc.get("sf_intercept_status") or ""
	doc.set_onload("sf_interception", {
		"can_request": bool(write and is_interceptable(doc) and not carrier_confirmed(doc) and state in {"", SF_SUPPORT_FAILED, "拦截失败"}),
		"can_record": bool(write and state in {REQUEST_STATE, *MANUAL_STATES, *LEGACY_STATE_MAP.keys()}),
		"can_verify": bool(write and not may_cancel(doc) and (
			state in SUCCESS_STATES | {CARRIER_PENDING_STATE} or (not state and not requires_manual_success(doc))
		)),
		"carrier_cancelled": may_cancel(doc), "blocked": not may_cancel(doc),
		"state": state,
	})


def _shipment(name, *, submitted=True):
	if not name:
		frappe.throw(_("Shipment is required."))
	doc = frappe.get_doc("Shipment", name, for_update=True)
	doc.check_permission("write")
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		frappe.throw(_("Not an SF International shipment."))
	if cint(doc.docstatus) not in ((1,) if submitted else (0, 1)) or not _waybill(doc):
		frappe.throw(_("Only a submitted shipment with an SF waybill can be intercepted."))
	if int(doc.docstatus or 0) == 2 or str(doc.status or "") in {"Cancelled", "已取消发货"}:
		frappe.throw(_("Cancelled shipments cannot be intercepted."))
	return doc


def _waybill(doc):
	return str(doc.shipment_id or doc.awb_number or "").strip()


def _set(doc, values):
	values = {key: value for key, value in values.items() if frappe.get_meta("Shipment").has_field(key)}
	if values:
		doc.db_set(values, update_modified=True)


def _audit(doc, text):
	doc.add_comment("Comment", escape_html(text))


def store_confirmation(doc, result):
	if not result.get("confirmed") or not result.get("evidence"):
		return False
	evidence = cancellation_evidence(result["evidence"], _waybill(doc))
	if not evidence:
		return False
	state = doc.get("sf_intercept_status") or ""
	if requires_manual_success(doc) and state not in SUCCESS_STATES:
		frappe.throw("已发出的包裹必须先联系顺丰客服，并在系统记录其确认拦截成功的反馈，再核对顺丰取消状态。")
	if state and state not in SUCCESS_STATES | {CARRIER_PENDING_STATE}:
		frappe.throw("请先联系顺丰客服，并在系统记录其确认拦截成功的反馈，再核对顺丰取消状态。")
	_set(doc, {
		"sf_intercept_status": CARRIER_STATE,
		"sf_carrier_cancelled": 1, "sf_carrier_cancelled_waybill": _waybill(doc),
		"sf_carrier_cancelled_at": now_datetime(),
		"sf_carrier_cancel_payload": json.dumps(evidence, ensure_ascii=False),
	})
	_audit(doc, "顺丰接口明确确认取消，运单号：" + _waybill(doc) + "。本地单据仍需单独办理取消。")
	return True


def store_cancellation_pending(doc):
	"""Record a successful cancel request whose final carrier state is pending."""
	_set(doc, {
		"sf_intercept_status": CARRIER_PENDING_STATE,
		"sf_carrier_cancelled": 0,
		"sf_carrier_cancelled_waybill": "",
		"sf_carrier_cancelled_at": None,
		"sf_carrier_cancel_payload": "",
	})
	_audit(doc, "已向顺丰提交取消请求，但接口尚未明确返回取消状态；运单号：" + _waybill(doc) + "。请稍后核对，不要重复提交取消请求。")
	return CARRIER_PENDING_STATE


@frappe.whitelist()
def request_interception(shipment, reason, assigned_to=None):
	# Keep the old argument temporarily so cached clients do not fail after deploy.
	# It is deliberately ignored: SF customer service is external to ERP.
	doc = _shipment(shipment)
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("Interception reason is required."))
	if not is_interceptable(doc):
		frappe.throw(interception_unavailable_message(doc))
	current_state = str(doc.get("sf_intercept_status") or "")
	if carrier_confirmed(doc) or current_state not in {"", SF_SUPPORT_FAILED, "拦截失败"}:
		frappe.throw(_("This shipment already has an active interception request."))
	now = now_datetime()
	_set(doc, {
		"sf_intercept_status": REQUEST_STATE,
		"sf_intercept_reason": reason[:2000],
		"sf_intercept_note": "",
		"sf_intercept_requested_by": frappe.session.user,
		"sf_intercept_requested_at": now,
		"sf_carrier_cancelled": 0,
		"sf_carrier_cancelled_waybill": "",
		"sf_carrier_cancelled_at": None,
		"sf_carrier_cancel_payload": "",
	})
	_audit(doc, f"记录拦截申请，需人工联系顺丰客服；运单号：{_waybill(doc)}；原因：{reason}")
	return {"status": REQUEST_STATE, "message": "拦截申请已记录，请人工联系顺丰客服。系统不会代为通知顺丰。"}


@frappe.whitelist()
def record_interception_result(shipment, status, note):
	doc = _shipment(shipment)
	status = str(status or "").strip()
	note = str(note or "").strip()
	if not doc.get("sf_intercept_status"):
		frappe.throw("请先记录拦截申请，再录入顺丰客服反馈。")
	status = LEGACY_STATE_MAP.get(status, status)
	if status not in MANUAL_STATES:
		frappe.throw(_("Invalid customer service interception status."))
	if not note:
		frappe.throw(_("Customer service note is required."))
	if str(doc.get("sf_intercept_status") or "") == CARRIER_STATE or cint(doc.get("sf_carrier_cancelled")) == 1:
		frappe.throw(_("Carrier cancellation is already confirmed; do not overwrite it with a manual result."))
	_set(doc, {"sf_intercept_status": status, "sf_intercept_note": note[:2000]})
	_audit(doc, f"人工录入顺丰客服反馈，{status}：{note}")
	return {"status": status, "message": "顺丰客服反馈已记录，仍需等待顺丰接口明确确认取消。"}


@frappe.whitelist()
def verify_carrier_cancellation(shipment):
	doc = _shipment(shipment, submitted=False)
	waybill = _waybill(doc)
	if may_cancel(doc):
		return {"carrier_cancelled": True, "status": doc.sf_intercept_status, "waybill": waybill}
	if doc.get("sf_intercept_status") and doc.sf_intercept_status not in SUCCESS_STATES | {CARRIER_PENDING_STATE}:
		frappe.throw("请先联系顺丰客服，并在系统记录其确认拦截成功的反馈，再核对顺丰取消状态。")
	if requires_manual_success(doc) and doc.sf_intercept_status not in SUCCESS_STATES:
		frappe.throw("已发出的包裹必须先联系顺丰客服，并在系统记录其确认拦截成功的反馈，再核对顺丰取消状态。")
	result = query_order_cancellation(waybill)
	if store_confirmation(doc, result):
		return {"carrier_cancelled": True, "status": CARRIER_STATE, "waybill": waybill, "message": _("Carrier cancellation confirmed. Local cancellation remains a separate action.")}
	return {"carrier_cancelled": False, "status": doc.sf_intercept_status or REQUEST_STATE, "waybill": waybill, "message": _("Carrier has not explicitly confirmed cancellation; local documents remain unchanged.")}
