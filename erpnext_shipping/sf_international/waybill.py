"""Protected multi-waybill lifecycle for one local Shipment.

The parent Shipment keeps its legacy waybill fields as a compatibility
projection.  Every SF order created after this module is installed has its
own immutable SF Waybill record, including its own freight evidence.
"""

import json
import math
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from .doctype.sf_waybill.sf_waybill import clear_mutation, mutation_context


SUCCESS_FEEDBACK = {"客服确认成功", "顺丰客服确认成功", "已确认换单", "换单成功"}
FAILURE_FEEDBACK = {"客服反馈失败", "顺丰客服反馈失败", "换单失败"}
PROCESSING_FEEDBACK = {"客服处理中", "顺丰客服处理中", "客服确认成功待顺丰确认", "顺丰客服确认成功待顺丰确认"}
ACTIVE = "当前"
REPLACEMENT_CANCEL_PENDING = "取消待确认"
LEGACY_REPLACEMENT_CANCEL_PENDING = "待取消确认"
SHIPPED_STATUSES = {
	"已发货", "运输中", "派送中", "已揽收", "已签收", "已退回", "已丢失",
	"Shipped", "In Progress", "Completed", "Delivered", "Returned", "Lost",
}
SHIPPED_TRACKING_STATUSES = {"Shipped", "Delivered", "Returned", "Lost", "已发货", "已签收", "已退回", "已丢失"}
IN_TRANSIT_TRACKING_STATUSES = {"In Progress", "运输中", "派送中", "已揽收"}
PENDING_REPLACEMENT_STATUSES = {
	"创建中", "待替换", "待启用", REPLACEMENT_CANCEL_PENDING, LEGACY_REPLACEMENT_CANCEL_PENDING,
}
UNCERTAIN_CREATION_STATUS = "失败"
CANCELLED_STATUSES = {"已取消", "订单已取消", "运单已取消", "已作废", "订单已作废", "cancelled", "canceled", "order cancelled", "order canceled"}


def _waybill(doc):
	return str(doc.get("shipment_id") or doc.get("awb_number") or "").strip()


def _shipment_is_shipped(doc):
	"""Require actual dispatch evidence; a route query alone is not evidence."""
	try:
		from .interception import has_dispatch_evidence

		return bool(has_dispatch_evidence(doc))
	except (ImportError, ModuleNotFoundError):
		pass
	status = str(doc.get("status") or "").strip()
	if status in SHIPPED_STATUSES:
		return True
	tracking = str(doc.get("tracking_status") or "").strip()
	if tracking in SHIPPED_TRACKING_STATUSES:
		return True
	return tracking in IN_TRANSIT_TRACKING_STATUSES and bool(str(doc.get("tracking_status_info") or "").strip())


def _has_field(doctype, fieldname):
	try:
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False


def _record_for_shipment(shipment, name, *, for_update=False):
	"""Load one immutable carrier record and enforce its parent relationship."""
	if not name:
		frappe.throw("请选择物流面单记录。")
	record = frappe.get_doc("SF Waybill", name, for_update=for_update)
	record.check_permission("read")
	if record.get("shipment") != shipment.name:
		frappe.throw("物流记录不属于该运单。")
	return record


def _cancel_payload_is_exact(payload, waybill):
	"""Validate a carrier cancellation snapshot without trusting a free-text note."""
	if not isinstance(payload, dict) or not waybill:
		return False
	identifiers = {
		str(payload.get(key)).strip()
		for key in ("waybill", "trackingNo", "waybillNo", "sfWaybillNo")
		if payload.get(key) not in (None, "")
	}
	if identifiers != {waybill}:
		return False
	statuses = {
		" ".join(str(payload.get(key) or "").lower().split()).rstrip(".!。！")
		for key in ("orderStatus", "status", "cancelStatus")
		if payload.get(key) not in (None, "")
	}
	if not statuses.intersection({value.lower() for value in CANCELLED_STATUSES}):
		return False
	# Use the same strict parser used by the carrier cancellation workflow.  A
	# free-text ``waybill`` plus ``status=cancelled`` is not sufficient evidence:
	# it can be forged by a client and does not identify a carrier response.
	try:
		from .client import cancellation_evidence

		return bool(cancellation_evidence(payload, waybill))
	except Exception:
		return False


def _cancel_evidence_source(shipment, record):
	"""Return exact cancellation evidence from the child or its legacy parent."""
	# Cancellation evidence belongs to the immutable child identity.  Falling
	# back to the parent's current number would allow a blank/failed history row
	# to inherit proof for a different label after a replacement.
	waybill = str(record.get("waybill") or "").strip()
	if not waybill:
		return None
	candidates = [
		{
			"flag": record.get("carrier_cancelled"),
			"waybill": record.get("carrier_cancelled_waybill"),
			"at": record.get("carrier_cancelled_at"),
			"payload": record.get("carrier_cancel_payload"),
		},
	]
	if shipment is not None:
		candidates.append({
			"flag": shipment.get("sf_carrier_cancelled"),
			"waybill": shipment.get("sf_carrier_cancelled_waybill"),
			"at": shipment.get("sf_carrier_cancelled_at"),
			"payload": shipment.get("sf_carrier_cancel_payload"),
		})
	for candidate in candidates:
		if not cint(candidate.get("flag")) or str(candidate.get("waybill") or "").strip() != waybill or not candidate.get("at"):
			continue
		try:
			payload = json.loads(candidate.get("payload") or "{}")
		except (TypeError, ValueError):
			continue
		if _cancel_payload_is_exact(payload, waybill):
			return {"waybill": waybill, "at": candidate["at"], "payload": json.dumps(payload, ensure_ascii=False, default=str)}
	return None


def _parent_cancel_evidence(shipment, waybill):
	"""Validate cancellation fields copied from the legacy Shipment only."""
	waybill = str(waybill or "").strip()
	if not waybill:
		return None
	if not cint(shipment.get("sf_carrier_cancelled")):
		return None
	if str(shipment.get("sf_carrier_cancelled_waybill") or "").strip() != waybill:
		return None
	if not shipment.get("sf_carrier_cancelled_at"):
		return None
	try:
		payload = json.loads(shipment.get("sf_carrier_cancel_payload") or "{}")
	except (TypeError, ValueError):
		return None
	if not _cancel_payload_is_exact(payload, waybill):
		return None
	return {
		"waybill": waybill,
		"at": shipment.get("sf_carrier_cancelled_at"),
		"payload": json.dumps(payload, ensure_ascii=False, default=str),
	}


def _sync_parent_cancel_evidence(shipment, record):
	"""Copy a verified parent confirmation to a migrated child record once."""
	evidence = _cancel_evidence_source(shipment, record)
	if not evidence:
		return False
	if not cint(record.get("carrier_cancelled")) or record.get("carrier_cancelled_waybill") != evidence["waybill"]:
		_set(record, {
			"carrier_cancelled": 1,
			"carrier_cancelled_waybill": evidence["waybill"],
			"carrier_cancelled_at": evidence["at"],
			"carrier_cancel_payload": evidence["payload"],
		})
	return True


def _has_external_order(record):
	"""Whether a history row may represent an order at the carrier."""
	if str(record.get("waybill") or "").strip() or str(record.get("order_id") or "").strip():
		return True
	payload = record.get("carrier_create_payload")
	if payload in (None, "", {}, []):
		return False
	if isinstance(payload, str):
		if not payload.strip():
			return False
		try:
			payload = json.loads(payload)
		except (TypeError, ValueError):
			# An unreadable non-empty response is conservatively treated as an
			# external attempt; the uncertainty flag will require reconciliation.
			return True
	if not payload:
		return False
	identifier_keys = {
		"trackingno", "waybill", "waybillno", "sfwaybillno", "logisticsno",
		"orderid", "order_id", "sysorderid", "orderno", "ordernumber",
	}

	def contains_identifier(value, depth=0):
		if depth > 8:
			return False
		if isinstance(value, dict):
			for key, item in value.items():
				normalized = "".join(ch for ch in str(key).lower() if ch.isalnum() or ch == "_")
				if normalized in identifier_keys and str(item or "").strip():
					return True
				if isinstance(item, (dict, list)) and contains_identifier(item, depth + 1):
					return True
		elif isinstance(value, list):
			return any(contains_identifier(item, depth + 1) for item in value)
		return False

	return contains_identifier(payload)


def _cancel_blocker(shipment, record):
	"""Return a human-readable reason when a child order is not cancel-safe."""
	status = str(record.get("replacement_status") or "").strip()
	waybill = str(record.get("waybill") or "").strip()
	if status in PENDING_REPLACEMENT_STATUSES:
		return f"{waybill or record.name}（{status}）仍在处理中"
	if status == UNCERTAIN_CREATION_STATUS and cint(record.get("creation_uncertain")):
		return f"{waybill or record.name}（下单结果待核实）"
	if not _has_external_order(record):
		# A carrier-side validation failure with no order/waybill cannot leave an
		# external order behind. Keep its local failed audit row, then continue.
		return None
	if status not in {ACTIVE, "当前", "已替换", "已取消", UNCERTAIN_CREATION_STATUS, "取消待确认", ""}:
		return f"{waybill or record.name}（未知状态 {status}）"
	if not _cancel_evidence_source(shipment, record):
		return f"{waybill or record.name}（没有顺丰明确取消凭证）"
	return None


def _waybill_doctype_available():
	"""Return whether the history table is available on this site."""
	exists = getattr(getattr(frappe, "db", None), "exists", None)
	if not callable(exists):
		# Lightweight test doubles and older installations may not expose exists;
		# let the query itself determine availability in that case.
		return True
	try:
		return bool(exists("DocType", "SF Waybill"))
	except Exception as exc:
		frappe.throw("顺丰面单历史表状态无法核验，请稍后重试。" + str(exc)[:120])


def has_waybill_history(shipment):
	"""Check for immutable child rows without selecting arbitrary history."""
	if not _waybill_doctype_available():
		return False
	try:
		return bool(
			frappe.get_all(
				"SF Waybill",
				filters={"shipment": shipment.name},
				fields=["name"],
				limit_page_length=1,
			)
		)
	except Exception as exc:
		frappe.throw("顺丰面单历史暂时无法核验，不能删除运单记录。" + str(exc)[:120])


def assert_all_waybills_cancelled(shipment):
	"""Block local Shipment cancellation until every carrier order is resolved.

	The parent fields point only to the current label.  A replacement can leave
	older or pending carrier orders behind, so checking that pointer alone is not
	 sufficient to make local cancellation safe.
	"""
	if not _waybill_doctype_available():
		# During installation the history DocType may not exist yet; the legacy
		# parent guard remains responsible for that short compatibility window.
		return False
	try:
		rows = _records(shipment)
	except Exception as exc:
		frappe.throw("本地运单不能取消：顺丰面单历史暂时无法核验，请稍后重试。" + str(exc)[:120])
	parent_waybill = _waybill(shipment)
	represented_parent = not parent_waybill or any(
		str(row.get("waybill") or "").strip() == parent_waybill for row in rows
	)
	blockers = []
	for row in rows:
		try:
			record = frappe.get_doc("SF Waybill", row.name)
			reason = _cancel_blocker(shipment, record)
		except Exception as exc:
			reason = f"{getattr(row, 'name', '') or row.get('name') or '未知记录'}（核验失败：{str(exc)[:120]}）"
		if reason:
			blockers.append(reason)
	if blockers:
		frappe.throw("本地运单不能取消：所有顺丰面单订单必须先明确取消或确认未创建。" + "；".join(blockers[:8]))
	if not represented_parent:
		frappe.throw("本地运单不能取消：当前顺丰面单没有对应的历史记录，请先完成面单核对。")
	return bool(rows) or not parent_waybill


def _shipment(name, *, allow_cancelled=False):
	if not name:
		frappe.throw("请选择运单。")
	doc = frappe.get_doc("Shipment", name, for_update=True)
	doc.check_permission("read")
	# All endpoints using this loader either call the carrier or persist a
	# history observation. Read-only listing uses _read_shipment instead; a read
	# role must not trigger a remote query or mutate an immutable child row.
	doc.check_permission("write")
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		frappe.throw("此操作仅适用于顺丰运单。")
	if cint(doc.docstatus) not in (1, 2):
		frappe.throw("请先提交运单，再办理物流面单操作。")
	if not allow_cancelled and (cint(doc.docstatus) == 2 or str(doc.get("status") or "").strip() in {"Cancelled", "已取消发货"}):
		frappe.throw("本地运单已取消，不能创建替换面单。")
	return doc


def _read_shipment(name, *, allow_cancelled=False):
	"""Load a Shipment for a read-only carrier query without taking a write lock."""
	if not name:
		frappe.throw("请选择运单。")
	doc = frappe.get_doc("Shipment", name)
	doc.check_permission("read")
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		frappe.throw("此操作仅适用于顺丰运单。")
	if cint(doc.docstatus) not in (1, 2):
		frappe.throw("请先提交运单，再查询物流。")
	if not allow_cancelled and (cint(doc.docstatus) == 2 or str(doc.get("status") or "").strip() in {"Cancelled", "已取消发货"}):
		frappe.throw("本地运单已取消，不能查询物流。")
	return doc


def _records(shipment):
	return frappe.get_all(
		"SF Waybill",
		filters={"shipment": shipment.name},
		fields=["*"],
		order_by="creation asc",
		limit_page_length=0,
	)


def _has_waybill_read_permission():
	"""Check child-history read access without breaking lightweight test doubles."""
	checker = getattr(frappe, "has_permission", None)
	if not callable(checker):
		# Older Frappe/test doubles may not expose the helper.  The subsequent
		# query or child document permission check remains the final guard.
		return True
	try:
		return bool(checker("SF Waybill", ptype="read"))
	except TypeError:
		try:
			return bool(checker("SF Waybill", "read"))
		except Exception:
			return False
	except Exception:
		return False


def _read_history_records(shipment):
	"""Read child history for UI paths, degrading safely when unavailable."""
	try:
		if not _waybill_doctype_available():
			return [], "missing"
	except Exception:
		return [], "unavailable"
	if not _has_waybill_read_permission():
		return [], "forbidden"
	try:
		return _records(shipment), "ok"
	except Exception:
		return [], "unavailable"


def _legacy_freight_values(shipment, waybill):
	"""Bind migrated freight evidence to the waybill it actually names."""
	raw = shipment.get("sf_freight_payload") or ""
	payload = _decode_dict(raw)
	bill = _bill_snapshot(payload, waybill)
	if bill:
		return {
			"freight_amount": shipment.get("shipment_amount"),
			"freight_currency": shipment.get("sf_freight_currency") or bill.get("currency"),
			# The child Select deliberately has only the current canonical values.
			# Legacy parents used values such as ``未结算``/``已结算``; once a
			# same-waybill bill snapshot is present, the evidence—not the old label—
			# determines the migrated status.
			"freight_status": "账单已取得",
			"freight_payload": raw,
			"freight_journal": shipment.get("sf_freight_journal"),
			"freight_previous_journal": shipment.get("sf_freight_previous_journal"),
			"freight_accounting_status": shipment.get("sf_freight_accounting_status"),
			"freight_accounting_note": shipment.get("sf_freight_accounting_note"),
			"freight_accounting_hold": shipment.get("sf_freight_accounting_hold"),
		}
	# Keep malformed/foreign legacy data as an explicitly unverified observation,
	# never as a bill that can be booked against this label.
	legacy_payload = json.dumps(
		{"waybill": waybill, "legacy_unverified": payload if payload else str(raw)[:2000]},
		ensure_ascii=False,
		default=str,
	)
	return {
		"freight_amount": None,
		"freight_currency": "",
		"freight_status": "待核实",
		"freight_payload": legacy_payload if raw else "",
		"freight_journal": shipment.get("sf_freight_journal"),
		"freight_previous_journal": shipment.get("sf_freight_previous_journal"),
		"freight_accounting_status": "待核实",
		"freight_accounting_note": shipment.get("sf_freight_accounting_note"),
		"freight_accounting_hold": 1 if raw else shipment.get("sf_freight_accounting_hold"),
	}


def _legacy_record(shipment):
	"""Materialize the pre-table parent fields once, without changing them."""
	waybill = _waybill(shipment)
	if not waybill:
		return None
	rows = _records(shipment)
	for row in rows:
		if row.get("waybill") == waybill:
			return frappe.get_doc("SF Waybill", row.name)
	parent_evidence = _parent_cancel_evidence(shipment, waybill)
	values = {
		"shipment": shipment.name,
		"waybill": waybill,
		"order_id": shipment.get("sf_iuop_order_id"),
		"replacement_status": "已取消" if parent_evidence else ACTIVE,
		"form_payload": shipment.get("sf_form_json"),
		"label_url": shipment.get("sf_label_url"),
		"tracking_status": shipment.get("tracking_status"),
		"tracking_status_info": shipment.get("tracking_status_info"),
		"tracking_url": shipment.get("tracking_url"),
		"carrier_cancelled": 1 if parent_evidence else 0,
		"carrier_cancelled_waybill": parent_evidence["waybill"] if parent_evidence else "",
		"carrier_cancelled_at": parent_evidence["at"] if parent_evidence else None,
		"carrier_cancel_payload": parent_evidence["payload"] if parent_evidence else "",
		"is_active": 0 if parent_evidence else 1,
	}
	values.update(_legacy_freight_values(shipment, waybill))
	token = mutation_context()
	try:
		doc = frappe.get_doc({"doctype": "SF Waybill", **values})
		doc.insert(ignore_permissions=True)
		return doc
	finally:
		clear_mutation(token)


def _active_record(shipment):
	rows = _records(shipment)
	active = [row for row in rows if cint(row.get("is_active")) and row.get("replacement_status") == ACTIVE]
	if len(active) > 1:
		frappe.throw("该运单存在多个当前顺丰面单，请先核对面单历史后再操作。")
	if active:
		return frappe.get_doc("SF Waybill", active[0].name)
	# A carrier-cancelled label may remain the parent pointer until a new label
	# is created. Accept that row only with exact cancellation evidence; never
	# choose an arbitrary historical row when the active pointer is inconsistent.
	parent_waybill = _waybill(shipment)
	eligible = []
	for row in rows:
		if str(row.get("waybill") or "").strip() != parent_waybill:
			continue
		status = str(row.get("replacement_status") or "").strip()
		if status in {ACTIVE, "当前"}:
			eligible.append(row)
		elif status == "已取消":
			try:
				candidate = frappe.get_doc("SF Waybill", row.name)
				if _cancel_evidence_source(shipment, candidate):
					eligible.append(row)
			except Exception:
				continue
	if len(eligible) == 1:
		return frappe.get_doc("SF Waybill", eligible[0].name)
	if rows:
		frappe.throw("当前顺丰面单历史状态不一致，请先核对面单历史后再操作。")
	return _legacy_record(shipment)


def _booking_attempt_is_pending(shipment, row):
	"""Return whether a previous first-booking attempt still owns a carrier risk."""
	status = str(row.get("replacement_status") or "").strip()
	if status in PENDING_REPLACEMENT_STATUSES:
		return True
	if status == UNCERTAIN_CREATION_STATUS and cint(row.get("creation_uncertain")):
		return True
	# A response containing a carrier identity is an external side effect even if
	# an older build did not set ``creation_uncertain``.  Without exact cancellation
	# evidence it must remain a blocker for a new order.
	return bool(_has_external_order(row) and not _cancel_evidence_source(shipment, row))


def begin_initial_booking_attempt(
	shipment,
	form_payload=None,
	*,
	reason="首次创建顺丰面单",
	allow_new_parent=False,
	commit=True,
):
	"""Persist an idempotency/audit row before the first carrier request.

	The helper is intentionally public within the app package so both the native
	``create_shipment`` endpoint and the Shipment document hook use the same
	transaction boundary.  It returns ``None`` only for a new, not-yet-persisted
	parent; callers must defer the carrier call until the parent exists.
	"""
	if not shipment or not getattr(shipment, "name", None):
		return None
	is_new = getattr(shipment, "is_new", None)
	if callable(is_new) and is_new() and not allow_new_parent:
		return None
	if _waybill(shipment):
		return None
	if not _waybill_doctype_available():
		frappe.throw("顺丰面单历史表尚未就绪，不能创建顺丰面单。")
	rows = _records(shipment)
	if any(_booking_attempt_is_pending(shipment, row) for row in rows):
		frappe.throw("已有顺丰面单下单尝试待核实，请先完成历史记录处理后再下单。")
	values = {
		"doctype": "SF Waybill",
		"shipment": shipment.name,
		"replacement_status": "创建中",
		"replacement_reason": reason[:2000],
		"form_payload": json.dumps(form_payload or {}, ensure_ascii=False, default=str),
		"is_active": 0,
		"creation_uncertain": 0,
	}
	token = mutation_context()
	try:
		record = frappe.get_doc(values)
		record.insert(ignore_permissions=True)
	finally:
		clear_mutation(token)
	_set_parent(
		shipment,
		{
			"sf_waybill_replacement_status": "创建中",
			"sf_waybill_replacement_note": reason[:2000],
			"sf_waybill_pending_record": record.name,
		},
	)
	if commit:
		db_commit = getattr(getattr(frappe, "db", None), "commit", None)
		if callable(db_commit):
			db_commit()
	return record


def load_initial_booking_attempt(shipment, record_name):
	"""Load the durable first-booking row used by an after-commit worker."""
	record = _record_for_shipment(shipment, record_name, for_update=True)
	if str(record.get("replacement_status") or "").strip() != "创建中":
		frappe.throw("首次顺丰面单下单记录已被其他流程处理，请先核对面单历史。")
	return record


def complete_initial_booking_attempt(
	shipment,
	record,
	payload,
	waybill,
	order_id=None,
	*,
	carrier_service="",
	form_payload=None,
):
	"""Finalize a pre-created first-booking row and project it to Shipment."""
	waybill = str(waybill or "").strip()
	if not waybill:
		frappe.throw("顺丰未返回有效运单号。")
	_assert_waybill_not_reused(shipment, waybill)
	values = {
		"waybill": waybill,
		"order_id": order_id or "",
		"carrier_create_payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
		"replacement_status": ACTIVE,
		"is_active": 1,
		"creation_uncertain": 0,
	}
	_set(record, values)
	parent_values = {
		"service_provider": "顺丰国际",
		"carrier": "SF International",
		"carrier_service": carrier_service or "",
		"shipment_id": waybill,
		"awb_number": waybill,
		"sf_active_waybill_record": record.name,
		"sf_waybill_replacement_status": ACTIVE,
		"sf_waybill_replacement_note": "",
		"sf_waybill_pending_record": "",
	}
	if order_id not in (None, ""):
		parent_values["sf_iuop_order_id"] = order_id
	if form_payload is not None:
		parent_values["sf_form_json"] = json.dumps(form_payload, ensure_ascii=False, default=str)
	if _has_field("Shipment", "sf_freight_status"):
		parent_values["sf_freight_status"] = "待核实"
	_set_parent(shipment, parent_values)
	from .shipping import _remember_successful_sf_receiver

	completed_form = form_payload if isinstance(form_payload, dict) else _decode_dict(record.get("form_payload"))
	_remember_successful_sf_receiver(shipment, completed_form.get("receiver"))
	commit = getattr(getattr(frappe, "db", None), "commit", None)
	if callable(commit):
		commit()
	return record


def fail_initial_booking_attempt(
	shipment,
	record,
	error,
	*,
	payload=None,
	waybill="",
	order_id="",
):
	"""Retain every carrier response when local completion fails."""
	if not record:
		return None
	error = str(error or "顺丰下单失败，未返回具体原因。")[:2000]
	values = {
		"replacement_status": UNCERTAIN_CREATION_STATUS,
		"is_active": 0,
		"creation_uncertain": 1,
		"creation_error": error,
		"replacement_feedback": error,
		"carrier_create_payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
	}
	if str(waybill or "").strip():
		values["waybill"] = str(waybill).strip()
	if order_id not in (None, ""):
		values["order_id"] = order_id
	try:
		_set(record, values)
		_set_parent(
			shipment,
			{
				"sf_waybill_replacement_status": UNCERTAIN_CREATION_STATUS,
				"sf_waybill_replacement_note": error,
				"sf_waybill_pending_record": record.name,
			},
		)
		commit = getattr(getattr(frappe, "db", None), "commit", None)
		if callable(commit):
			commit()
	except Exception:
		try:
			frappe.log_error(title="SF initial waybill failure audit")
		except Exception:
			pass
	return record


def migrate_existing_waybills():
	"""Create one immutable child record for each legacy parent waybill."""
	if not frappe.db.exists("DocType", "SF Waybill"):
		return
	from .shipping import _is_sf_shipment
	try:
		rows = frappe.get_all("Shipment", fields=["name"])
	except Exception as exc:
		try:
			frappe.log_error(message=str(exc)[:1000], title="SF waybill migration list")
		except Exception:
			pass
		return
	for row in rows:
		name = row.get("name") if hasattr(row, "get") else getattr(row, "name", "")
		try:
			doc = frappe.get_doc("Shipment", name)
			if not _is_sf_shipment(doc):
				continue
			if not _waybill(doc):
				continue
			records = _records(doc)
			# ``待取消确认`` was used briefly by an earlier build.  Normalize it
			# before Frappe validates the Select field so those rows remain visible
			# and continue to block a duplicate carrier order.
			for history_row in records:
				if history_row.get("replacement_status") == LEGACY_REPLACEMENT_CANCEL_PENDING:
					_set(frappe.get_doc("SF Waybill", history_row.name), {
						"replacement_status": REPLACEMENT_CANCEL_PENDING,
					})
					history_row.replacement_status = REPLACEMENT_CANCEL_PENDING
			if not records:
				record = _legacy_record(doc)
			else:
				active = [row for row in records if cint(row.get("is_active")) and row.get("replacement_status") == ACTIVE]
				record = frappe.get_doc("SF Waybill", active[0].name) if len(active) == 1 else None
			if record and doc.get("sf_active_waybill_record") != record.name:
				_set_parent(doc, {"sf_active_waybill_record": record.name}, update_modified=False)
		except Exception as exc:
			try:
				frappe.log_error(message=str(exc)[:1000], title="SF waybill migration: {0}".format(name or "unknown"))
			except Exception:
				pass
			continue


def _json(value):
	if isinstance(value, dict):
		return value
	try:
		result = json.loads(value or "{}")
	except (TypeError, ValueError):
		frappe.throw("面单表单数据格式不正确。")
	if not isinstance(result, dict):
		frappe.throw("面单表单数据格式不正确。")
	return result


def _set(doc, values):
	values = {key: value for key, value in values.items() if _has_field(doc.doctype, key)}
	if not values:
		return
	token = mutation_context()
	try:
		doc.db_set(values, update_modified=True)
	finally:
		clear_mutation(token)


def _set_parent(doc, values, *, update_modified=True):
	"""Write compatibility fields through one narrow server-side path."""
	values = {key: value for key, value in values.items() if _has_field("Shipment", key)}
	if values:
		doc.db_set(values, update_modified=update_modified)


def _parent_projection(shipment, record, *, clear_old_cancel=False):
	# Parent freight fields are a compatibility projection too. When a new
	# label becomes current, never leave the old label's amount or bill attached
	# to it. The old journal remains linked as a protected prior entry so finance
	# can correct it before a new label is booked.
	previous_parent_journal = str(shipment.get("sf_freight_journal") or "").strip()
	record_journal = str(record.get("freight_journal") or "").strip()
	prior_journal = str(
		record.get("freight_previous_journal")
		or shipment.get("sf_freight_previous_journal")
		or ""
	).strip()
	if previous_parent_journal and previous_parent_journal != record_journal:
		# The current parent's entry is the newest prior entry. Keeping an older
		# value after a second replacement would make the latest posted entry
		# unreachable from the parent correction action. Each child retains the
		# complete historical chain for audit.
		prior_journal = previous_parent_journal
	has_record_bill = record.get("freight_status") == "账单已取得" and bool(record.get("freight_payload"))
	freight_amount = record.get("freight_amount") if has_record_bill else 0
	freight_status = "账单已取得" if has_record_bill else "待核实"
	accounting_hold = cint(record.get("freight_accounting_hold"))
	accounting_status = (record.get("freight_accounting_status") or "待核实") if has_record_bill else "待核实"
	accounting_note = record.get("freight_accounting_note") or ""
	if previous_parent_journal and previous_parent_journal != record_journal and not record_journal:
		accounting_hold = 1
		accounting_status = "记账待处理"
		accounting_note = "当前面单尚未建立独立凭证；上一面单凭证已保留，需由财务先核对。"
	values = {
		"shipment_id": record.waybill,
		"awb_number": record.waybill,
		"service_provider": "顺丰国际",
		"carrier": "SF International",
		"sf_active_waybill_record": record.name,
		"sf_waybill_replacement_status": record.replacement_status,
		"sf_waybill_replacement_note": record.replacement_feedback or record.replacement_reason or "",
		"sf_waybill_pending_record": "",
		"sf_iuop_order_id": record.order_id or "",
		"sf_form_json": record.form_payload or "",
		"sf_label_url": record.label_url or "",
		"tracking_status": _official_tracking_status(record.tracking_status or ""),
		"tracking_status_info": record.tracking_status_info or "",
		"tracking_url": record.tracking_url or "",
		"shipment_amount": freight_amount,
		"sf_freight_currency": record.get("freight_currency") or "",
		"sf_freight_status": freight_status,
		"sf_freight_payload": record.get("freight_payload") or "",
		# A parent journal always belongs to the current label.  The previous
		# label's journal is retained separately so it cannot be reused for the
		# replacement amount by the normal booking path.
		"sf_freight_journal": record_journal,
		"sf_freight_previous_journal": prior_journal,
		"sf_freight_accounting_status": accounting_status,
		"sf_freight_accounting_note": accounting_note,
		"sf_freight_accounting_hold": accounting_hold,
		"sf_freight_query_status": "查询成功" if has_record_bill else "未查询",
	}
	if clear_old_cancel:
		values.update({
			"sf_intercept_status": "",
			"sf_carrier_cancelled": 0,
			"sf_carrier_cancelled_waybill": "",
			"sf_carrier_cancelled_at": None,
			"sf_carrier_cancel_payload": "",
		})
	_set_parent(shipment, values)
	if record.get("replacement_status") == ACTIVE and cint(record.get("is_active")):
		from .shipping import _remember_successful_sf_receiver

		_remember_successful_sf_receiver(shipment, _decode_dict(record.get("form_payload")).get("receiver"))


def ensure_waybill_record(shipment):
	"""Materialize the current parent booking as an immutable history row."""
	if not shipment or not _waybill(shipment):
		return None
	waybill = _waybill(shipment)
	for row in _records(shipment):
		if row.get("waybill") != waybill:
			continue
		record = frappe.get_doc("SF Waybill", row.name)
		# The parent pointer is part of the compatibility projection.  Older
		# bookings may already have a history row but no pointer, which would make
		# later replacement activation reject an otherwise valid source label.
		if (
			cint(record.get("is_active"))
			and str(record.get("replacement_status") or "").strip() in {ACTIVE, "当前"}
			and shipment.get("sf_active_waybill_record") != record.name
		):
			_set_parent(shipment, {
				"sf_active_waybill_record": record.name,
				"sf_waybill_replacement_status": ACTIVE,
			}, update_modified=False)
		return record
	record = _legacy_record(shipment)
	if record and cint(record.get("is_active")):
		_set_parent(shipment, {
			"sf_active_waybill_record": record.name,
			"sf_waybill_replacement_status": ACTIVE,
		}, update_modified=False)
	return record


def _persisted_parent_matches(shipment, waybill):
	"""Check whether a persisted Shipment already owns this exact number."""
	db = getattr(frappe, "db", None)
	if not db or not callable(getattr(db, "get_value", None)) or not getattr(shipment, "name", None):
		return False
	try:
		values = db.get_value(
			"Shipment", shipment.name, ["shipment_id", "awb_number"], as_dict=True
		)
	except Exception as exc:
		frappe.throw("顺丰面单归属暂时无法核验，不能继续关联。" + str(exc)[:120])
	if isinstance(values, dict):
		return any(str(values.get(field) or "").strip() == waybill for field in ("shipment_id", "awb_number"))
	return False


def _assert_waybill_not_reused(shipment, waybill, *, allow_current=False):
	"""Prevent a carrier response from attaching one number to two shipments.

	``allow_current`` is used only while reloading an already persisted label. It
	allows one exact history row owned by this Shipment, while still rejecting a
	second historical row or any other Shipment (including cancelled ones).
	"""
	waybill = str(waybill or "").strip()
	if not waybill:
		return
	shipment_name = str(getattr(shipment, "name", "") or "").strip()
	history_available = _waybill_doctype_available()
	if history_available:
		try:
			rows = frappe.get_all(
				"SF Waybill",
				filters={"waybill": waybill},
				fields=["name", "shipment"],
				limit_page_length=0,
			)
		except Exception as exc:
			frappe.throw("顺丰面单历史暂时无法核验，不能继续关联。" + str(exc)[:120])
	else:
		# A fresh installation can receive a booking before the child table has
		# been synced. Parent-level checks below still protect existing numbers.
		rows = []
	parent_matches = allow_current and _persisted_parent_matches(shipment, waybill)
	conflicts = []
	for row in rows:
		row_shipment = str(row.get("shipment") or "").strip()
		if parent_matches and row_shipment == shipment_name:
			continue
		conflicts.append(row)
	# A carrier number is a historical identity, even when the old local row is
	# cancelled or belongs to this same Shipment.  Reusing it would make later
	# tracking and freight callbacks ambiguous, so the response is retained as a
	# failed attempt and must be reconciled manually.
	if conflicts or (rows and not parent_matches):
		frappe.throw("顺丰返回的运单号已存在历史记录，不能重复关联，请先核对顺丰后台。")
	db = getattr(frappe, "db", None)
	if not db or not callable(getattr(db, "get_value", None)):
		frappe.throw("顺丰面单归属核验服务不可用，不能继续关联。")
	# Do not exclude cancelled Shipments: their historical number remains
	# reserved for audit and cannot be assigned to a new local Shipment.
	for field in ("shipment_id", "awb_number"):
		filters = {field: waybill}
		if allow_current and parent_matches:
			filters["name"] = ["!=", shipment_name]
		try:
			other = db.get_value("Shipment", filters, "name")
		except Exception as exc:
			frappe.throw("顺丰面单归属暂时无法核验，不能继续关联。" + str(exc)[:120])
		if other:
			frappe.throw("顺丰返回的运单号已关联其他本地运单，请先核对顺丰后台。")


def _new_record(shipment, form, old, reason, shipped):
	from .shipping import _create_order_body_from_form, _sender_from_warehouse
	from .client import create_order

	# Validate the editable replacement form before creating the local attempt
	# row.  A malformed form should not leave a misleading "creating" history
	# entry, while a carrier/network failure must leave a durable failed row.
	# The sender is the original booked origin.  It is intentionally restored
	# from the old label (or the parent form for migrated data), because the
	# replacement UI only permits destination/product/customs corrections and a
	# client must not be able to move the parcel's origin to another warehouse.
	form = dict(form or {})
	old_form = _decode_dict(old.get("form_payload")) if old else {}
	parent_form = _decode_dict(shipment.get("sf_form_json"))
	authoritative_sender = dict(_sender_from_warehouse(doc=shipment, pickup_address_name=shipment.get("pickup_address_name")) or {})
	for source in (parent_form.get("sender"), old_form.get("sender")):
		if not isinstance(source, dict):
			continue
		for field, value in source.items():
			if value is not None and (not isinstance(value, str) or value.strip()):
				authoritative_sender[field] = value
	form["sender"] = authoritative_sender
	body, _code, _name = _create_order_body_from_form(shipment, form)
	record = frappe.get_doc({
		"doctype": "SF Waybill", "shipment": shipment.name,
		"replacement_status": "创建中", "replaces_waybill": old.waybill if old else "",
		"replacement_shipped": 1 if shipped else 0,
		"replacement_reason": reason, "form_payload": json.dumps(form, ensure_ascii=False),
	})
	token = mutation_context()
	try:
		record.insert(ignore_permissions=True)
	finally:
		clear_mutation(token)
	# Commit the immutable attempt before contacting the carrier. If the process
	# dies after the request leaves this server, the surviving "创建中" row blocks
	# a duplicate retry and gives an operator something to reconcile.
	_set_parent(shipment, {
		"sf_waybill_replacement_status": "创建中",
		"sf_waybill_replacement_note": reason,
		"sf_waybill_pending_record": record.name,
	})
	commit = getattr(getattr(frappe, "db", None), "commit", None)
	if callable(commit):
		commit()
	payload = None
	data = {}
	waybill = ""
	order_id = ""
	try:
		body.setdefault("pieceorderBaseInfo", {})["userOrderid"] = record.name
		payload = create_order(body)
		if not isinstance(payload, dict):
			frappe.throw("顺丰返回了无效的下单结果。")
		data = payload.get("data") or {}
		waybill = str(data.get("trackingNo") or "").strip()
		order_id = data.get("orderId") or data.get("order_id") or data.get("sysOrderId") or ""
		if not waybill:
			frappe.throw("顺丰未返回新运单号。")
		_assert_waybill_not_reused(shipment, waybill)
		_set(record, {
			"waybill": waybill, "order_id": order_id,
			"carrier_create_payload": json.dumps(payload, ensure_ascii=False, default=str),
			"replacement_status": "待替换" if shipped else ACTIVE,
			"is_active": 0 if shipped else 1,
			"creation_uncertain": 0,
		})
		if not shipped and old:
			_set(old, {"replacement_status": "已取消", "is_active": 0})
			_parent_projection(shipment, record, clear_old_cancel=True)
		return record, None
	except Exception as exc:
		# Do not re-raise a carrier failure: an unhandled exception rolls back the
		# request transaction and erases the only local evidence that the carrier
		# may have accepted the order.  The caller receives an explicit failed
		# result, so the attempt is committed and can be reconciled manually.
		error = str(exc)[:2000] or "顺丰下单失败，未返回具体原因。"
		try:
			failure_values = {
				"replacement_status": "失败", "is_active": 0,
				"creation_uncertain": 1,
				"replacement_feedback": error,
				"creation_error": error,
				"carrier_create_payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
			}
			# Persist carrier identifiers even when local validation fails after the
			# response arrived.  The reconciliation endpoint needs the exact number
			# to query cancellation; dropping it would leave an unretryable dead end.
			if waybill:
				failure_values["waybill"] = waybill
			if order_id:
				failure_values["order_id"] = order_id
			_set(record, failure_values)
			_set_parent(shipment, {
				"sf_waybill_replacement_status": "失败",
				"sf_waybill_replacement_note": error,
				"sf_waybill_pending_record": record.name,
			})
			if callable(commit):
				commit()
		except Exception:
			frappe.log_error(title="SF replacement waybill failure audit")
		return record, error


@frappe.whitelist(methods=["POST"])
def create_replacement(shipment, form_json=None, reason=None, shipped=0):
	shipment = _shipment(shipment)
	reason = str(reason or "").strip()
	if len("".join(reason.split())) < 4:
		frappe.throw("请填写具体的换单原因（至少四个字符）。")
	if len(reason) > 2000:
		frappe.throw("换单原因不能超过 2000 个字符。")
	old = _active_record(shipment)
	shipped = bool(cint(shipped))
	actual_shipped = _shipment_is_shipped(shipment)
	if shipped != actual_shipped:
		frappe.throw("换单场景与当前物流状态不一致，请先刷新并选择正确的未发货或已发货流程。")
	rows = _records(shipment)
	if any(row.get("replacement_status") in PENDING_REPLACEMENT_STATUSES for row in rows):
		if any(
			row.get("replacement_status") in {REPLACEMENT_CANCEL_PENDING, LEGACY_REPLACEMENT_CANCEL_PENDING}
			and _has_external_order(row)
			and not _cancel_evidence_source(shipment, row)
			for row in rows
		):
			frappe.throw("上一笔失败换单已有顺丰订单或运单号，必须先取得明确取消凭证后才能重试。")
		frappe.throw("已有待处理的替换面单，请先完成或标记上一笔换单。")
	if any(
		row.get("replacement_status") == UNCERTAIN_CREATION_STATUS and cint(row.get("creation_uncertain"))
		for row in rows
	):
		frappe.throw("上一笔替换面单的下单结果尚未核实，请先确认顺丰后台没有生成订单，再办理重试。")
	if any(
		row.get("replacement_status") == UNCERTAIN_CREATION_STATUS
		and _has_external_order(row)
		and not _cancel_evidence_source(shipment, row)
		for row in rows
	):
		frappe.throw("上一笔失败换单已有顺丰订单或运单号，必须先取得明确取消凭证后才能重试。")
	if not shipped:
		if not old or old.waybill != _waybill(shipment) or not _sync_parent_cancel_evidence(shipment, old):
			frappe.throw("未发货换单前，必须先取得原顺丰面单的明确取消确认。")
		if old.replacement_status not in {ACTIVE, "当前", "已取消"}:
			frappe.throw("原物流面单当前状态不允许重复换单，请先完成上一笔换单处理。")
	if shipped and not old:
		frappe.throw("已发货换单必须先有原顺丰面单记录。")
	if form_json in (None, ""):
		frappe.throw("换单必须提交修改后的寄件、收件或货物信息，不能直接复用原面单表单。")
	form = _json(form_json)
	record, creation_error = _new_record(shipment, form, old, reason, shipped)
	if creation_error:
		return {
			"ok": False,
			"record": record.name,
			"status": "失败",
			"waybill": record.get("waybill") or "",
			"message": "替换面单创建失败，失败记录已保留；请先核对顺丰后台是否已生成订单，再决定是否重试。",
			"error": creation_error,
		}
	if shipped:
		values = {"sf_waybill_replacement_status": "待替换", "sf_waybill_replacement_note": reason, "sf_waybill_pending_record": record.name}
		_set_parent(shipment, values)
	return {"ok": True, "record": record.name, "status": "待替换" if shipped else ACTIVE}


@frappe.whitelist(methods=["POST"])
def resolve_failed_replacement(shipment, waybill_record, note):
	"""Release an uncertain carrier attempt only after an explicit external check."""
	shipment = _shipment(shipment)
	record = _record_for_shipment(shipment, waybill_record, for_update=True)
	if record.replacement_status != UNCERTAIN_CREATION_STATUS or not cint(record.get("creation_uncertain")):
		frappe.throw("该替换面单没有处于待核实状态。")
	if _has_external_order(record):
		frappe.throw("该失败记录已有顺丰订单或运单号，不能按未创建处理；请先记录顺丰明确取消凭证。")
	note = str(note or "").strip()
	if len("".join(note.split())) < 4:
		frappe.throw("请填写已核对顺丰后台的说明（至少四个字符）。")
	_set(record, {
		"creation_uncertain": 0,
		"replacement_feedback": note[:2000],
		"replacement_feedback_at": now_datetime(),
	})
	return {
		"ok": True,
		"record": record.name,
		"status": UNCERTAIN_CREATION_STATUS,
		"retryable": True,
		"message": "已记录顺丰后台核对结果，可以重新发起换单；原失败尝试仍保留。",
	}


@frappe.whitelist(methods=["POST"])
def record_replacement_feedback(shipment, waybill_record, status, note):
	shipment = _shipment(shipment)
	record = _record_for_shipment(shipment, waybill_record, for_update=True)
	if record.replacement_status != "待替换":
		frappe.throw("只有待替换的物流记录可以录入外部客服反馈。")
	note = str(note or "").strip()
	if not note:
		frappe.throw("请填写外部客服反馈。")
	status = str(status or "").strip()
	if status in SUCCESS_FEEDBACK:
		new_status = "待启用"
	elif status in FAILURE_FEEDBACK:
		new_status = "失败"
	elif status in PROCESSING_FEEDBACK:
		new_status = "待替换"
	else:
		frappe.throw("顺丰客服反馈状态无效。")
	_set(record, {"replacement_status": new_status, "replacement_feedback": note[:2000], "replacement_feedback_at": now_datetime()})
	values = {"sf_waybill_replacement_status": new_status, "sf_waybill_replacement_note": note[:2000]}
	if new_status == "失败":
		# A carrier order/waybill still exists even when customer service rejects
		# the replacement. Keep it pending until exact cancellation evidence is
		# recorded; clearing this pointer would allow a duplicate live order.
		if _has_external_order(record) and not _cancel_evidence_source(shipment, record):
			new_status = REPLACEMENT_CANCEL_PENDING
			_set(record, {"replacement_status": new_status})
			values["sf_waybill_replacement_status"] = new_status
			values["sf_waybill_pending_record"] = record.name
		else:
			values["sf_waybill_pending_record"] = ""
	elif new_status in {REPLACEMENT_CANCEL_PENDING, LEGACY_REPLACEMENT_CANCEL_PENDING}:
		new_status = REPLACEMENT_CANCEL_PENDING
		_set(record, {"replacement_status": new_status})
		values["sf_waybill_pending_record"] = record.name
	_set_parent(shipment, values)
	return {"ok": True, "record": record.name, "status": new_status}


def _replacement_source_record(shipment, replacement):
	"""Resolve the exact historical label named by a replacement row."""
	old_waybill = str(replacement.get("replaces_waybill") or "").strip()
	if not old_waybill:
		frappe.throw("替换面单没有记录原面单号，不能启用。")
	rows = [
		row for row in _records(shipment)
		if str(row.get("waybill") or "").strip() == old_waybill
	]
	if len(rows) != 1:
		frappe.throw("替换面单对应的原历史记录不存在或不唯一，不能启用。")
	old = frappe.get_doc("SF Waybill", rows[0].name)
	if old.name == replacement.name:
		frappe.throw("替换面单不能把自身作为原面单。")
	return old


@frappe.whitelist(methods=["POST"])
def activate_replacement(shipment, waybill_record, reason=None):
	shipment = _shipment(shipment)
	reason = str(reason or "").strip()
	if len("".join(reason.split())) < 4:
		frappe.throw("请填写启用换单的原因。")
	record = _record_for_shipment(shipment, waybill_record, for_update=True)
	if record.replacement_status != "待启用":
		frappe.throw("只有已获外部确认的替换面单才能启用。")
	# Never trust the parent's current pointer to identify the source label.  A
	# stale/corrupted pointer could otherwise make a replacement erase the wrong
	# history row.  The replacement row's immutable ``replaces_waybill`` is the
	# only source identity, and the parent projection must still point to it.
	old = _replacement_source_record(shipment, record)
	if str(shipment.get("sf_active_waybill_record") or "").strip() != str(old.name).strip():
		frappe.throw("当前面单指针与替换记录的原面单不一致，不能启用；请先核对历史。")
	if str(_waybill(shipment)).strip() != str(old.waybill).strip():
		frappe.throw("当前面单号与替换记录的原面单不一致，不能启用；请先核对历史。")
	# The immutable creation scenario determines the activation rule. After
	# dispatch, SF support may replace a live label without cancelling it; the
	# successful feedback transition to 待启用 above is the required confirmation.
	# This must not manufacture carrier cancellation evidence for either label.
	# Before dispatch (including legacy rows without a scenario), the old label
	# still needs exact carrier cancellation evidence even if it has since shipped.
	# Preserve any real parent evidence before the projection switches numbers.
	old_cancelled = _sync_parent_cancel_evidence(shipment, old)
	if not cint(record.get("replacement_shipped")) and not old_cancelled:
		frappe.throw("必须先取得顺丰对原面单的明确取消确认，再启用替换面单。")
	_set(old, {"replacement_status": "已替换", "is_active": 0})
	_set(record, {"replacement_status": ACTIVE, "is_active": 1, "replacement_reason": reason[:2000]})
	_parent_projection(shipment, record, clear_old_cancel=True)
	_set_parent(shipment, {"sf_waybill_pending_record": ""})
	return {"ok": True, "record": record.name, "waybill": record.waybill, "status": ACTIVE}


PUBLIC_FIELDS = (
	"name", "shipment", "waybill", "order_id", "replacement_status", "replaces_waybill", "replacement_shipped",
	"label_url", "tracking_url", "tracking_status", "tracking_status_info", "tracking_queried_at",
	"carrier_cancelled", "carrier_cancelled_waybill", "carrier_cancelled_at",
	"freight_amount", "freight_currency", "freight_status", "freight_journal",
	"freight_previous_journal", "freight_accounting_status", "freight_accounting_hold",
	"freight_accounting_note", "freight_queried_at", "creation_uncertain",
	"replacement_reason", "replacement_feedback", "replacement_feedback_at", "creation_error", "is_active", "creation",
)


def _public_record(row):
	result = {field: row.get(field) for field in PUBLIC_FIELDS if row.get(field) not in (None, "") or field in {"name", "shipment", "waybill", "is_active"}}
	result["tracking_events"] = _saved_tracking_events(row)
	result["route_count"] = len(result["tracking_events"])
	return result


def _tracking_events(payload, waybill):
	"""Read safe events from normalized snapshots or older carrier responses."""
	payload = _decode_dict(payload)
	waybill = str(waybill or "").strip()
	if not payload or not waybill:
		return []
	for source in (payload, payload.get("data")):
		if not isinstance(source, dict):
			continue
		for field in ("awb_number", "waybill", "trackingNo", "waybillNo", "sfWaybillNo"):
			if source.get(field) not in (None, "") and str(source[field]).strip() != waybill:
				return []
	rows = payload.get("tracking_events")
	if not isinstance(rows, list):
		try:
			rows = _tracking_from_route(payload, waybill).get("tracking_events") or []
		except Exception:
			# A malformed old snapshot must not prevent the Shipment from opening.
			return []
	return _unique_tracking_events(rows)


def _unique_tracking_events(rows):
	events = {}
	for row in rows:
		if not isinstance(row, dict):
			continue
		time = str(row.get("time") or "").strip()
		description = str(row.get("description") or "").strip()
		if time or description:
			events[(time, description)] = {"time": time, "description": description}
	return sorted(events.values(), key=lambda event: event["time"], reverse=True)


def _saved_tracking_events(record):
	return _tracking_events(record.get("tracking_payload"), record.get("waybill"))


def _tracking_payload_with_history(record, tracking, payload=None):
	"""Retain previously observed nodes when the carrier returns a shorter list."""
	events = _unique_tracking_events([
		*_saved_tracking_events(record),
		*_tracking_events(tracking, record.get("waybill")),
	])
	stored = dict(tracking if payload is None else payload)
	stored.update({
		"awb_number": record.get("waybill"),
		"tracking_events": events,
		"route_count": len(events),
	})
	return json.dumps(stored, ensure_ascii=False, default=str)


def _parent_record_snapshot(doc):
	"""Read-only fallback used before migration has materialized a history row."""
	waybill = _waybill(doc)
	if not waybill:
		return None
	parent_evidence = _parent_cancel_evidence(doc, waybill)
	local_cancelled = cint(doc.get("docstatus")) == 2 or str(doc.get("status") or "").strip() in {
		"Cancelled", "已取消发货"
	}
	cancelled = bool(parent_evidence or local_cancelled)
	return {
		"name": doc.get("sf_active_waybill_record") or "",
		"shipment": doc.name,
		"waybill": waybill,
		"order_id": doc.get("sf_iuop_order_id") or "",
		"replacement_status": "已取消" if cancelled else (doc.get("sf_waybill_replacement_status") or ACTIVE),
		"label_url": doc.get("sf_label_url") or "",
		"tracking_url": doc.get("tracking_url") or "",
		"tracking_status": doc.get("tracking_status") or "",
		"tracking_status_info": doc.get("tracking_status_info") or "",
		"carrier_cancelled": cint(doc.get("sf_carrier_cancelled")),
		"carrier_cancelled_waybill": doc.get("sf_carrier_cancelled_waybill") or "",
		"carrier_cancelled_at": doc.get("sf_carrier_cancelled_at"),
		"freight_amount": doc.get("shipment_amount"),
		"freight_currency": doc.get("sf_freight_currency") or "",
		"freight_status": doc.get("sf_freight_status") or "",
		"freight_journal": doc.get("sf_freight_journal") or "",
		"freight_accounting_status": doc.get("sf_freight_accounting_status") or "",
		"freight_accounting_hold": doc.get("sf_freight_accounting_hold") or 0,
		"is_active": 0 if cancelled else 1,
	}


def _read_shipment(name):
	if not name:
		frappe.throw("请选择运单。")
	doc = frappe.get_doc("Shipment", name)
	doc.check_permission("read")
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		frappe.throw("此操作仅适用于顺丰运单。")
	return doc


@frappe.whitelist()
def list_waybill_records(shipment):
	"""Return safe, parent-authorized history data for the Shipment form."""
	doc = _read_shipment(shipment)
	raw_rows, history_state = _read_history_records(doc)
	rows = [_public_record(row) for row in raw_rows]
	# A missing table is an upgrade window: expose the legacy projection until
	# migration creates the child row.  Permission failures must not leak that
	# row as if the caller had child-history access.
	if not rows and history_state in {"missing", "unavailable"}:
		fallback = _parent_record_snapshot(doc)
		if fallback:
			rows = [fallback]
	result = {"waybills": rows}
	if history_state != "ok":
		result["history_available"] = False
	return result


def onload(doc, method=None):
	"""Expose history on the native Shipment form without delaying its first paint."""
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc) or not _waybill(doc) or not getattr(doc, "set_onload", None):
		return
	raw_rows, history_state = _read_history_records(doc)
	rows = [_public_record(row) for row in raw_rows]
	if not rows and history_state in {"missing", "unavailable"}:
		fallback = _parent_record_snapshot(doc)
		if fallback:
			rows = [fallback]
	doc.set_onload("sf_waybills", rows)
	try:
		can_write = bool(frappe.has_permission("Shipment", "write", doc=doc))
	except Exception:
		can_write = True
	doc.set_onload("sf_waybill_actions", {
		"can_create": bool(cint(doc.docstatus) == 1 and not str(doc.get("status") or "").strip() in {"Cancelled", "已取消发货"}),
		"can_query": history_state == "ok",
		"can_write": can_write,
		"history_available": history_state == "ok",
		"current": doc.get("sf_active_waybill_record") or "",
		"pending": doc.get("sf_waybill_pending_record") or "",
	})


def sync_tracking(shipment, tracking):
	"""Persist the latest carrier status on the exact current history row."""
	if not tracking:
		return None
	doc = shipment if hasattr(shipment, "get") else frappe.get_doc("Shipment", shipment)
	provided_waybill = str(tracking.get("awb_number") or "").strip()
	waybill = provided_waybill or _waybill(doc)
	if not waybill:
		return None
	record = None
	for row in _records(doc):
		if (provided_waybill and str(row.get("waybill") or "").strip() == provided_waybill) or (
			not provided_waybill and row.get("name") == doc.get("sf_active_waybill_record")
		):
			record = frappe.get_doc("SF Waybill", row.name, for_update=True)
			break
	if provided_waybill and not record:
		# Never attach a carrier response for another number to the current label.
		return None
	if not record:
		record = ensure_waybill_record(doc)
	if not record:
		return None
	values = {
		"tracking_payload": _tracking_payload_with_history(record, tracking),
		"tracking_queried_at": now_datetime(),
	}
	if tracking.get("route_count") != 0:
		values.update({
			"tracking_status": tracking.get("tracking_status") or "",
			"tracking_status_info": tracking.get("tracking_status_info") or "",
			"tracking_url": tracking.get("tracking_url") or "",
		})
	_set(record, values)
	return record


def _official_tracking_status(value):
	"""Map the SF-only booking label to an ERPNext-compatible value."""
	return "" if str(value or "").strip() == "Booked" else value


def _tracking_from_route(payload, waybill):
	from .shipping import _tracking_from_route as parse_route

	return parse_route(payload, waybill)


@frappe.whitelist(methods=["POST"])
def fetch_waybill_tracking(shipment, waybill_record):
	"""Query route details for any historical or pending label independently."""
	doc = _shipment(shipment, allow_cancelled=True)
	record = _record_for_shipment(doc, waybill_record, for_update=True)
	waybill = str(record.get("waybill") or "").strip()
	if not waybill:
		frappe.throw("物流记录没有运单号，不能查询物流。")
	from .client import query_order, query_route
	from .shipping import _extract_iuop_order_id
	order_id = record.get("order_id")
	if not order_id:
		order_id = _extract_iuop_order_id(query_order(waybill))
	if not order_id:
		frappe.throw("顺丰没有返回该面单的订单编号，暂时不能查询物流。")
	payload = query_route(order_id)
	tracking = _tracking_from_route(payload, waybill)
	tracking_payload = _tracking_payload_with_history(record, tracking, payload)
	if tracking.get("route_count") == 0:
		_set(record, {
			"tracking_payload": tracking_payload,
			"tracking_queried_at": now_datetime(),
		})
		return {"ok": True, "waybill": waybill, **tracking}
	_set(record, {
		"tracking_status": tracking["tracking_status"],
		"tracking_status_info": tracking["tracking_status_info"],
		"tracking_url": tracking.get("tracking_url") or "",
		"tracking_payload": tracking_payload,
		"tracking_queried_at": now_datetime(),
	})
	if cint(record.get("is_active")) and record.replacement_status == ACTIVE:
		_set_parent(doc, {
			"tracking_status": _official_tracking_status(tracking["tracking_status"]),
			"tracking_status_info": tracking["tracking_status_info"],
			"tracking_url": tracking.get("tracking_url") or "",
		})
		try:
			from .shipping import _maybe_mark_shipped_from_tracking

			# Reload so status checks see the just-written tracking fields on parent.
			doc.reload()
			_maybe_mark_shipped_from_tracking(doc, tracking)
		except Exception:
			frappe.log_error(title="SF auto mark shipped from waybill tracking")
	return {"ok": True, "waybill": waybill, **tracking}


def fetch_waybill_tracking_readonly(shipment, waybill_record):
	"""Query one historical label without changing Shipment or SF Waybill."""
	doc = _read_shipment(shipment, allow_cancelled=True)
	record = _record_for_shipment(doc, waybill_record, for_update=False)
	waybill = str(record.get("waybill") or "").strip()
	if not waybill:
		frappe.throw("物流记录没有运单号，不能查询物流。")
	from .client import query_order, query_route
	from .shipping import _extract_iuop_order_id
	order_id = record.get("order_id")
	if not order_id:
		order_id = _extract_iuop_order_id(query_order(waybill))
	if not order_id:
		frappe.throw("顺丰没有返回该面单的订单编号，暂时不能查询物流。")
	tracking = _tracking_from_route(query_route(order_id), waybill)
	public = _public_record(record.as_dict())
	return {"ok": True, "waybill": waybill, **tracking,
		"saved_tracking_events": public.get("tracking_events") or [], "persisted": False}


@frappe.whitelist(methods=["POST"])
def verify_replacement_cancellation(shipment, waybill_record):
	"""Verify cancellation of a rejected replacement order without switching it."""
	doc = _shipment(shipment, allow_cancelled=True)
	record = _record_for_shipment(doc, waybill_record, for_update=True)
	if record.replacement_status not in {REPLACEMENT_CANCEL_PENDING, LEGACY_REPLACEMENT_CANCEL_PENDING}:
		frappe.throw("只有等待取消确认的失败换单可以核对顺丰取消状态。")
	if not _has_external_order(record):
		frappe.throw("该失败换单没有顺丰订单，不能查询取消状态。")
	waybill = str(record.get("waybill") or "").strip()
	if not waybill:
		frappe.throw("该失败换单没有运单号，不能查询取消状态。")
	from .client import query_order_cancellation
	result = query_order_cancellation(waybill)
	confirmed = bool(
		result.get("confirmed")
		and result.get("evidence")
		and _cancel_payload_is_exact(result.get("evidence"), waybill)
	)
	if confirmed:
		evidence = result.get("evidence")
		_set(record, {
			"carrier_cancelled": 1,
			"carrier_cancelled_waybill": waybill,
			"carrier_cancelled_at": now_datetime(),
			"carrier_cancel_payload": json.dumps(evidence, ensure_ascii=False, default=str),
			"replacement_status": "已取消",
			"is_active": 0,
		})
		if doc.get("sf_waybill_pending_record") == record.name:
			_set_parent(doc, {
				"sf_waybill_pending_record": "",
				"sf_waybill_replacement_status": "失败",
			})
		return {"ok": True, "carrier_cancelled": True, "status": "已取消", "waybill": waybill}
	if record.replacement_status == LEGACY_REPLACEMENT_CANCEL_PENDING:
		_set(record, {"replacement_status": REPLACEMENT_CANCEL_PENDING})
		_set_parent(doc, {
			"sf_waybill_replacement_status": REPLACEMENT_CANCEL_PENDING,
			"sf_waybill_pending_record": record.name,
		})
	return {
		"ok": False,
		"carrier_cancelled": False,
		"status": REPLACEMENT_CANCEL_PENDING,
		"waybill": waybill,
		"message": "顺丰尚未明确返回已取消，原运单和本地单据保持不变。",
	}


@frappe.whitelist(methods=["POST"])
def print_replacement_label(shipment, waybill_record):
	"""Share current-label printing while keeping pending labels independent."""
	doc = _shipment(shipment)
	record = _record_for_shipment(doc, waybill_record, for_update=True)
	if record.replacement_status not in {ACTIVE, "当前", "待替换", "待启用"}:
		frappe.throw("当前物流记录不能打印面单。")
	from .shipping import _extract_iuop_order_id, _print_task_id, _stored_file_url, print_sf_label

	if record.replacement_status == ACTIVE:
		if not _is_current_label(doc, record):
			frappe.throw("当前面单记录与运单号不一致，请先核对面单历史。")
		return print_sf_label(doc.name)
	stored = _stored_file_url(record.get("label_url"))
	if stored:
		return stored
	from .client import download_pdf, poll_label, query_order, start_print
	order_id = record.get("order_id") or _extract_iuop_order_id(query_order(record.waybill))
	if not order_id:
		frappe.throw("顺丰没有返回该面单的订单编号，暂时不能打印。")
	task_id = _print_task_id(start_print(order_id))
	url, token = poll_label(task_id)
	content = download_pdf(url, token)
	from frappe.utils.file_manager import save_file
	# SF Waybill rows are immutable and intentionally grant no direct file-write
	# permission.  Attach the generated PDF to the authorized parent Shipment;
	# the child keeps only the immutable URL projection.
	file_doc = save_file(f"SF-{record.waybill}.pdf", content, "Shipment", doc.name, is_private=1)
	_set(record, {"label_url": file_doc.file_url})
	return file_doc.file_url


def _is_current_label(shipment, record):
	return bool(
		record.get("shipment") == shipment.name
		and str(record.get("waybill") or "").strip() == _waybill(shipment)
		and (not shipment.get("sf_active_waybill_record") or shipment.get("sf_active_waybill_record") == record.name)
		and cint(record.get("is_active"))
		and record.get("replacement_status") == ACTIVE
	)


def current_label_record(shipment):
	"""Resolve the exact label while the caller holds the Shipment row lock."""
	name = shipment.get("sf_active_waybill_record")
	if not name:
		rows = [row for row in _records(shipment) if str(row.get("waybill") or "").strip() == _waybill(shipment)]
		if len(rows) > 1:
			frappe.throw("该运单存在重复的顺丰面单记录，请先核对面单历史。")
		if not rows:
			return None
		name = rows[0].name
	record = _record_for_shipment(shipment, name, for_update=True)
	if not _is_current_label(shipment, record) or cint(record.get("carrier_cancelled")):
		frappe.throw("当前面单记录与运单号不一致或已取消，不能打印。")
	return record


def sync_current_label(shipment, record, file_url):
	"""Keep the two URL projections identical without touching other labels."""
	if record:
		if not _is_current_label(shipment, record):
			frappe.throw("当前面单记录与运单号不一致，不能更新面单文件。")
		if record.get("label_url") != file_url:
			_set(record, {"label_url": file_url})
	if shipment.get("sf_label_url") != file_url:
		_set_parent(shipment, {"sf_label_url": file_url})


def _decode_dict(value):
	if isinstance(value, dict):
		return dict(value)
	try:
		decoded = json.loads(value or "{}")
	except (TypeError, ValueError):
		return {}
	return dict(decoded) if isinstance(decoded, dict) else {}


def _bill_snapshot(payload, waybill):
	"""Extract a durable bill from either the current or legacy payload shape."""
	if not isinstance(payload, dict) or str(payload.get("waybill") or "").strip() != str(waybill or "").strip():
		return None
	records = payload.get("records")
	if not isinstance(records, list) or not records or not all(isinstance(row, dict) for row in records):
		return None
	amount = payload.get("amount")
	currency = str(payload.get("currency") or "").strip().upper()
	if not (currency.isascii() and len(currency) == 3 and currency.isalpha()):
		return None
	try:
		if isinstance(amount, bool):
			return None
		numeric_amount = Decimal(str(amount))
	except (InvalidOperation, TypeError, ValueError):
		return None
	if not numeric_amount.is_finite() or numeric_amount < 0 or numeric_amount >= Decimal("1000000000000"):
		return None
	# A top-level total is only valid when every carrier detail is itself a
	# complete, non-negative amount in the same currency and the sum agrees with
	# that total.  Without this check a forged/partial row could turn an old
	# parent amount into a bill that is safe to post.
	total = Decimal("0")
	normalized_records = []
	for row in records:
		value = row.get("payAmount")
		if value in (None, "") or isinstance(value, bool):
			return None
		try:
			detail_amount = Decimal(str(value))
		except (InvalidOperation, TypeError, ValueError):
			return None
		if not detail_amount.is_finite() or detail_amount < 0:
			return None
		detail_currency = str(row.get("currency") or "").strip().upper()
		if not (detail_currency.isascii() and len(detail_currency) == 3 and detail_currency.isalpha()):
			return None
		if detail_currency != currency:
			return None
		total += detail_amount
		normalized_records.append(dict(row, currency=detail_currency))
	if total >= Decimal("1000000000000") or (total > 0 and total.quantize(Decimal("0.01")) == 0):
		return None
	if abs(total - numeric_amount) >= Decimal("0.005"):
		return None
	return {
		"waybill": str(waybill).strip(),
		"amount": amount,
		"currency": currency,
		"records": normalized_records,
		"raw": payload.get("raw"),
	}


def _child_freight_history(record, previous_payload):
	history = _decode_dict(record.get("freight_query_history")).get("items")
	if not isinstance(history, list):
		history = previous_payload.get("query_history") if isinstance(previous_payload.get("query_history"), list) else []
	return [item for item in history if isinstance(item, dict)][-50:]


def _same_bill(left, right):
	if not left or not right:
		return False
	try:
		amount_same = abs(float(left.get("amount") or 0) - float(right.get("amount") or 0)) < 0.005
	except (TypeError, ValueError):
		amount_same = False
	return bool(
		str(left.get("waybill") or "") == str(right.get("waybill") or "")
		and str(left.get("currency") or "").upper() == str(right.get("currency") or "").upper()
		and amount_same
		and left.get("records") == right.get("records")
	)


def _store_child_freight_query(record, payload, *, amount=None, currency="", bill_rows=None, query_status="查询失败"):
	"""Append a query observation while retaining the last valid bill forever."""
	previous = _decode_dict(record.get("freight_payload"))
	waybill = str(record.get("waybill") or "").strip()
	history = _child_freight_history(record, previous)
	old_bill = _bill_snapshot(previous, waybill)
	now = str(now_datetime())
	if amount is not None:
		current = {
			"waybill": waybill,
			"amount": amount,
			"currency": str(currency or "").strip().upper(),
			"records": list(bill_rows or []),
			"raw": payload,
		}
		if old_bill and not _same_bill(old_bill, current):
			history.append({**old_bill, "superseded_at": now})
		stored = dict(current)
		if old_bill:
			stored["previous_bill"] = old_bill
		stored_status = "账单已取得"
		observation = {**current, "status": "账单已取得", "queried_at": now}
	else:
		last_query = {
			"waybill": waybill,
			"amount": None,
			"currency": str(currency or "").strip().upper(),
			"records": list(bill_rows or []),
			"raw": payload,
			"status": query_status,
			"queried_at": now,
		}
		stored = dict(previous) if previous else {"waybill": waybill}
		stored["last_query"] = last_query
		stored_status = "账单已取得" if old_bill else "待核实"
		if old_bill and "amount" not in stored:
			stored.update(old_bill)
	observation = last_query if amount is None else observation
	if not history or history[-1] != observation:
		history.append(observation)
	stored["query_history"] = history[-20:]
	stored["last_query_at"] = now
	stored["last_query_status"] = query_status
	updates = {
		"freight_payload": json.dumps(stored, ensure_ascii=False, default=str),
		"freight_query_history": json.dumps({"items": history[-20:]}, ensure_ascii=False, default=str),
		"freight_queried_at": now,
		"freight_status": stored_status,
	}
	if amount is not None:
		stored["query_history"] = history[-20:]
		stored["last_query"] = {
			"waybill": waybill, "amount": amount, "currency": str(currency or "").strip().upper(),
			"records": list(bill_rows or []), "raw": payload, "status": "账单已取得", "queried_at": now,
		}
		updates["freight_payload"] = json.dumps(stored, ensure_ascii=False, default=str)
		updates["freight_amount"] = amount
		updates["freight_currency"] = str(currency or "").strip().upper()
	return updates, old_bill


def sync_freight(
	shipment,
	amount,
	currency,
	bill_rows,
	payload,
	*,
	query_status="查询成功",
	error=None,
	accounting_status=None,
	accounting_hold=None,
):
	"""Copy a parent freight query into the exact active label history row."""
	doc = shipment if hasattr(shipment, "get") else frappe.get_doc("Shipment", shipment)
	# Booking may reload and update the parent in a separate locked Document;
	# read the persisted projection before copying journal/accounting identity.
	try:
		fresh = frappe.get_doc("Shipment", doc.name)
		if fresh:
			doc = fresh
	except Exception:
		pass
	record = _active_record(doc)
	if not record:
		return None
	if error:
		query_payload = {"waybill": record.get("waybill"), "last_query_error": str(error)[:500]}
		updates, old_bill = _store_child_freight_query(record, query_payload, query_status="查询失败")
		if not old_bill:
			updates.update({"freight_accounting_status": "查询失败", "freight_accounting_hold": 1})
	else:
		updates, old_bill = _store_child_freight_query(
			record,
			payload,
			amount=amount,
			currency=currency,
			bill_rows=bill_rows,
			query_status=query_status,
		)
		if amount is None and not old_bill:
			updates.update({"freight_accounting_status": "待核实", "freight_accounting_hold": 1})
		elif amount is not None:
			updates["freight_accounting_status"] = accounting_status or record.get("freight_accounting_status") or "待记账"
			updates["freight_accounting_hold"] = (
				cint(accounting_hold) if accounting_hold is not None else cint(record.get("freight_accounting_hold"))
			)
	# The Journal Entry is created against the compatibility Shipment fields.
	# Copy its exact identity and review state back to this label so a later
	# replacement cannot detach the accounting evidence from the waybill.
	updates.update({
		"freight_journal": doc.get("sf_freight_journal") or "",
		"freight_previous_journal": doc.get("sf_freight_previous_journal") or "",
		"freight_accounting_status": accounting_status or doc.get("sf_freight_accounting_status") or updates.get("freight_accounting_status") or "待核实",
		"freight_accounting_note": doc.get("sf_freight_accounting_note") or "",
		"freight_accounting_hold": cint(accounting_hold) if accounting_hold is not None else cint(doc.get("sf_freight_accounting_hold")),
	})
	_set(record, updates)
	return record


def sync_accounting_state(
	shipment,
	waybill,
	*,
	journal=None,
	previous_journal=None,
	status=None,
	note=None,
	hold=None,
):
	"""Update the accounting state on the exact historical label."""
	doc = shipment if hasattr(shipment, "get") else frappe.get_doc("Shipment", shipment)
	waybill = str(waybill or "").strip()
	if not waybill:
		return None
	record = None
	for row in _records(doc):
		if str(row.get("waybill") or "").strip() == waybill:
			record = frappe.get_doc("SF Waybill", row.name)
			break
	if not record and waybill == _waybill(doc):
		record = ensure_waybill_record(doc)
	if not record:
		return None
	updates = {}
	if journal is not None:
		updates["freight_journal"] = journal or ""
	if previous_journal is not None:
		updates["freight_previous_journal"] = previous_journal or ""
	if status is not None:
		updates["freight_accounting_status"] = status or ""
	if note is not None:
		updates["freight_accounting_note"] = note or ""
	if hold is not None:
		updates["freight_accounting_hold"] = cint(hold)
	_set(record, updates)
	return record


@frappe.whitelist(methods=["POST"])
def fetch_waybill_freight(shipment, waybill_record):
	"""Store a bill against one label; replacement labels never overwrite old evidence."""
	doc = _shipment(shipment, allow_cancelled=True)
	record = _record_for_shipment(doc, waybill_record, for_update=True)
	if not record.get("waybill"):
		frappe.throw("物流记录没有运单号，不能查询运费。")
	if (
		cint(doc.docstatus) == 1
		and cint(record.get("is_active"))
		and record.get("replacement_status") == ACTIVE
		and str(record.get("waybill") or "").strip() == _waybill(doc)
	):
		# The current label follows the normal accounting workflow. Only historical
		# or pending replacement labels are forced into manual finance review.
		from .shipping import fetch_sf_freight

		return fetch_sf_freight(doc.name)
	from .client import query_case_orders
	from .shipping import _freight_window, _sum_pay_amount
	begin, end = _freight_window(doc)
	try:
		payload = query_case_orders(record.waybill, begin, end)
		amount, currency, bill_rows = _sum_pay_amount(payload)
	except Exception as exc:
		updates, old_bill = _store_child_freight_query(
			record,
			{"waybill": record.waybill, "last_query_error": str(exc)[:500]},
			query_status="查询失败",
		)
		if not old_bill:
			updates.update({"freight_accounting_status": "查询失败", "freight_accounting_hold": 1})
		_set(record, updates)
		return {"ok": False, "waybill": record.waybill, "status": "查询失败", "message": "运费查询失败，历史运费记录未删除。"}
	if amount is None:
		updates, old_bill = _store_child_freight_query(
			record, payload, currency=currency, bill_rows=bill_rows, query_status="未查到账单"
		)
		if not old_bill:
			updates.update({"freight_accounting_status": "待核实", "freight_accounting_hold": 1})
		_set(record, updates)
		return {
			"ok": True,
			"waybill": record.waybill,
			"status": updates["freight_status"],
			"amount": old_bill["amount"] if old_bill else record.get("freight_amount"),
			"message": "本次未查到账单，已有金额和历史凭证保留。",
		}
	# Replacement-label bills are intentionally held for finance review. The
	# parent journal workflow is keyed to the active Shipment projection and
	# must never silently book a historical/pending label against the old bill.
	updates, _old_bill = _store_child_freight_query(
		record, payload, amount=amount, currency=currency, bill_rows=bill_rows, query_status="查询成功"
	)
	updates.update({"freight_accounting_status": "记账待处理", "freight_accounting_hold": 1})
	_set(record, updates)
	return {"ok": True, "waybill": record.waybill, "status": "账单已取得", "amount": amount, "currency": currency, "accounting_status": "记账待处理", "message": "账单已按该面单单独保存，记账需由财务复核。"}
