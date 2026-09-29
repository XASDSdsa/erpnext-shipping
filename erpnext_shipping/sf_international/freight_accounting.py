"""Guard SF freight accounting corrections independently of parcel status."""

import json
import math
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe.utils import cint, now_datetime


_creation = ContextVar("sf_freight_journal_creation", default=None)
_correction = ContextVar("sf_freight_correction", default=None)
SHIPMENT_FIELDS = (
	"sf_freight_journal", "sf_freight_previous_journal", "sf_freight_accounting_hold", "sf_freight_correction_reason",
	"sf_freight_correction_log", "sf_freight_payload", "sf_freight_status",
	"sf_freight_currency", "sf_freight_accounting_status", "sf_freight_accounting_note",
	"sf_freight_query_status", "sf_active_waybill_record", "sf_waybill_replacement_status",
	"sf_waybill_replacement_note", "sf_waybill_pending_record",
)
JOURNAL_FIELDS = ("sf_freight_shipment", "sf_freight_waybill", "sf_freight_previous_journal")

# ``Shipment.sf_freight_status`` and ``SF Waybill.freight_status`` are Select
# fields.  Keep migration writes inside the current option set even when a site
# still contains labels from an older app release.
_FREIGHT_STATUS_VALUES = frozenset(("待核实", "账单已取得"))
_LEGACY_FREIGHT_STATUS = {
	"未结算": "待核实",
	"已结算": "账单已取得",
	"Unsettled": "待核实",
	"Settled": "账单已取得",
}


def _waybill(doc):
	return str(doc.get("shipment_id") or doc.get("awb_number") or "").strip()


@contextmanager
def journal_creation(doc, previous_journal=None):
	"""Authorize marker assignment during the server's insert/submit operation."""
	assert_can_book(doc)
	token = _creation.set((doc.name, _waybill(doc), previous_journal or ""))
	try:
		yield
	finally:
		_creation.reset(token)


def register_journal(doc, journal):
	context = _creation.get()
	if not context or context[:2] != (doc.name, _waybill(doc)):
		frappe.throw("顺丰运费凭证只能由专用记账流程创建。")
	for field, value in zip(JOURNAL_FIELDS, context):
		journal.set(field, value)


def booking_is_held(doc):
	if cint(doc.get("sf_freight_accounting_hold")) == 1:
		return True
	journal = doc.get("sf_freight_journal")
	if journal:
		status = frappe.db.get_value("Journal Entry", journal, "docstatus")
		return status is None or cint(status) == 2
	return False


def assert_can_book(doc):
	context = _correction.get()
	if booking_is_held(doc) and not (context and context.get("action") == "resume" and context.get("shipment") == doc.name):
		frappe.throw("此运费已暂停自动记账，请由财务通过运费更正流程明确重新记账。")


def _validate_unchanged(doc, fields, message):
	previous = doc.get_doc_before_save()
	if doc.is_new():
		if any(doc.get(field) not in (None, "", 0, False) for field in fields):
			frappe.throw(message)
	elif previous:
		if any((doc.get(field) or "") != (previous.get(field) or "") for field in fields):
			frappe.throw(message)


def validate_shipment(doc, method=None):
	_validate_unchanged(doc, SHIPMENT_FIELDS, "运费账单、记账关联和更正记录只能通过专用运费操作更新。")
	previous = doc.get_doc_before_save()
	if previous and not doc.is_new() and doc.get("shipment_amount") != previous.get("shipment_amount"):
		from .shipping import _is_sf_shipment
		if _is_sf_shipment(previous):
			frappe.throw("顺丰运费金额只能根据有效运费账单通过专用运费操作更新。")


def validate_journal(doc, method=None):
	context = _creation.get()
	if context and tuple(doc.get(field) or "" for field in JOURNAL_FIELDS) == context:
		return
	if doc.is_new() and doc.get("amended_from"):
		original = frappe.get_doc("Journal Entry", doc.amended_from)
		if _is_protected(original):
			frappe.throw("顺丰运费凭证不能直接修订，请在对应运单中办理重新记账。")
	_validate_unchanged(doc, JOURNAL_FIELDS, "顺丰运费凭证的来源和历史关联不能手工填写或修改。")
	previous = doc.get_doc_before_save()
	if previous and cint(previous.get("docstatus")) == 0 and cint(doc.get("docstatus")) == 1 and _is_protected(doc):
		frappe.throw("顺丰运费凭证只能通过专用记账流程提交。")


def _linked_shipments(journal):
	names = set(frappe.get_all("Shipment", filters={"sf_freight_journal": journal.name}, pluck="name"))
	marker = journal.get("sf_freight_shipment")
	if marker:
		names.add(marker)
	# Cancellation skips validate; a forged request can remove the in-memory marker.
	persisted = frappe.db.get_value("Journal Entry", journal.name, "sf_freight_shipment")
	if persisted:
		names.add(persisted)
	return names


def _is_protected(journal):
	return bool(journal.get("sf_freight_shipment") or _linked_shipments(journal))


def _manager():
	return frappe.session.user == "Administrator" or "Accounts Manager" in frappe.get_roles()


def _require_manager():
	if not _manager():
		frappe.throw("只有财务经理可以办理运费记账更正。", frappe.PermissionError)


def _reason(value):
	value = str(value or "").strip()
	if len("".join(value.split())) < 4:
		frappe.throw("请填写具体的运费更正原因（至少四个字符）。")
	if len(value) > 2000:
		frappe.throw("运费更正原因不能超过 2000 个字符。")
	return value


def _shipment(name):
	if not name:
		frappe.throw("请选择需要处理运费的运单。")
	doc = frappe.get_doc("Shipment", name, for_update=True)
	doc.check_permission("read")
	doc.check_permission("write")
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		frappe.throw("此操作仅适用于顺丰运单。")
	return doc


def _sync_waybill_accounting(doc, waybill, **values):
	"""Keep the exact label history aligned with a guarded correction."""
	try:
		from .waybill import sync_accounting_state
	except (ImportError, ModuleNotFoundError):
		return None
	return sync_accounting_state(doc, waybill, **values)


def _journal_waybill(doc, journal_name):
	"""Resolve a journal to its original immutable waybill history row.

	After a replacement the Shipment points at the newest label, while a
	correction may still concern an older journal.  The journal marker is the
	primary key; the child history lookup is a compatibility fallback for rows
	created before that marker was introduced.
	"""
	journal_name = str(journal_name or "").strip()
	if not journal_name:
		return ""
	try:
		marked = str(frappe.db.get_value("Journal Entry", journal_name, "sf_freight_waybill") or "").strip()
	except Exception:
		marked = ""
	if marked:
		return marked
	try:
		rows = frappe.get_all(
			"SF Waybill",
			filters={"shipment": doc.name},
			fields=["waybill", "freight_journal", "freight_previous_journal"],
			limit_page_length=0,
		)
	except Exception:
		rows = []
	matches = {
		str(row.get("waybill") or "").strip()
		for row in rows
		if journal_name in {str(row.get("freight_journal") or "").strip(), str(row.get("freight_previous_journal") or "").strip()}
		and str(row.get("waybill") or "").strip()
	}
	if len(matches) > 1:
		frappe.throw("同一运费凭证关联了多个顺丰面单，请先核对历史记录。")
	if matches:
		return next(iter(matches))
	# A legacy parent-only journal has no child marker.  It is safe to use the
	# current pointer only when this is still the parent's current journal.
	if journal_name == str(doc.get("sf_freight_journal") or "").strip():
		return _waybill(doc)
	return ""


def _assert_no_payment_evidence(journal):
	"""Native cancellation can unlink references, so inspect them beforehand."""
	checks = (
		("Payment Entry Reference", {"reference_doctype": "Journal Entry", "reference_name": journal.name, "docstatus": 1}),
		("Payment Entry Reference", {"advance_voucher_type": "Journal Entry", "advance_voucher_no": journal.name, "docstatus": 1}),
		("Payment Ledger Entry", {"against_voucher_type": "Journal Entry", "against_voucher_no": journal.name, "voucher_no": ["!=", journal.name], "delinked": 0}),
		("Payment Ledger Entry", {"voucher_type": "Journal Entry", "voucher_no": journal.name, "against_voucher_no": ["not in", [journal.name, ""]], "delinked": 0}),
	)
	for doctype, filters in checks:
		if frappe.db.exists(doctype, filters):
			frappe.throw("此运费凭证已有付款、核销或银行对账关联，请先由财务按原流程处理关联记录。")
	# ``Journal Entry Account`` is a child table.  Unlike its parent Journal
	# Entry, the official ERPNext schema has no ``docstatus`` column.  Filtering
	# that field directly produces an Unknown column SQL error during every
	# protected-journal correction.  Read the child references first, then use
	# each parent Journal Entry's status to ignore cancelled/draft historical
	# rows while blocking a submitted external reference.
	for reference_filters in (
		{"reference_type": "Journal Entry", "reference_name": journal.name},
		{"advance_voucher_type": "Journal Entry", "advance_voucher_no": journal.name},
	):
		try:
			references = frappe.get_all(
				"Journal Entry Account",
				filters={**reference_filters, "parenttype": "Journal Entry", "parent": ["!=", journal.name]},
				fields=["parent"],
				limit_page_length=0,
			)
		except Exception as exc:
			frappe.throw("运费凭证的会计引用暂时无法核验，请稍后重试。" + str(exc)[:120])
		for reference in references:
			parent = str(reference.get("parent") or "").strip()
			if not parent:
				frappe.throw("此运费凭证已有无法识别的会计引用，请先由财务核对。")
			status = frappe.db.get_value("Journal Entry", parent, "docstatus")
			if status is None or cint(status) == 1:
				frappe.throw("此运费凭证已有付款、核销或银行对账关联，请先由财务按原流程处理关联记录。")
	bank_references = frappe.get_all(
		"Bank Transaction Payments",
		filters={"payment_document": "Journal Entry", "payment_entry": journal.name},
		fields=["parent", "parenttype"],
	)
	for reference in bank_references:
		bank = frappe.db.get_value("Bank Transaction", reference.parent, ["docstatus", "status"], as_dict=True)
		if not bank or (cint(bank.docstatus) != 2 and bank.status != "Cancelled"):
			frappe.throw("此运费凭证已有银行对账关联，请先由财务核对并解除对应的对账记录。")
	if journal.get("clearance_date") or any(row.get("reference_name") or row.get("advance_voucher_no") for row in journal.get("accounts") or []):
		frappe.throw("此运费凭证已有付款或其他财务单据关联，请先核对并处理关联记录。")


def before_cancel(journal, method=None):
	if not _is_protected(journal):
		return
	context = _correction.get()
	if not context or context.get("action") != "cancel" or context.get("journal") != journal.name:
		frappe.throw("顺丰运费凭证不能直接取消，请在对应运单中办理运费更正。")
	if _linked_shipments(journal) != {context["shipment"]}:
		frappe.throw("此运费凭证的运单关联存在冲突，请先核对关联记录。")
	validate_journal(journal)
	_require_manager()
	journal.check_permission("cancel")
	_assert_no_payment_evidence(journal)


def _audit(doc, action, reason, **values):
	try:
		entries = json.loads(doc.get("sf_freight_correction_log") or "[]")
	except (TypeError, ValueError):
		frappe.throw("运费更正历史格式异常，请先核对历史记录。")
	if not isinstance(entries, list):
		frappe.throw("运费更正历史格式异常，请先核对历史记录。")
	provided = dict(values)
	waybill = str(provided.pop("waybill", "") or "").strip()
	journal_name = provided.get("previous_journal") or provided.get("journal_entry") or ""
	if not waybill and journal_name:
		try:
			waybill = str(frappe.db.get_value("Journal Entry", journal_name, "sf_freight_waybill") or "").strip()
		except Exception:
			waybill = ""
	waybill = waybill or _waybill(doc)
	bill = provided.pop("bill", None)
	amount = provided.pop("amount", None)
	currency = provided.pop("currency", None)
	if bill is None and waybill == _waybill(doc):
		bill = doc.get("sf_freight_payload") or ""
		if amount is None:
			amount = doc.get("shipment_amount")
		if currency is None:
			currency = doc.get("sf_freight_currency")
	elif bill is None and waybill:
		# A replacement can leave the parent pointing at a different label. Read
		# the historical child by the journal's exact waybill so cancellation audit
		# entries cannot accidentally describe the replacement bill.
		try:
			rows = frappe.get_all(
				"SF Waybill",
				filters={"shipment": doc.name, "waybill": waybill},
				fields=["freight_payload", "freight_amount", "freight_currency"],
				limit_page_length=1,
			)
		except Exception:
			rows = []
		row = rows[0] if rows else None
		if row:
			bill = row.get("freight_payload") or ""
			if amount is None:
				amount = row.get("freight_amount")
			if currency is None:
				currency = row.get("freight_currency")
	entries.append({
		"action": action, "reason": reason, "user": frappe.session.user,
		"at": str(now_datetime()), "shipment": doc.name, "waybill": waybill,
		"bill": bill or "", "amount": amount, "currency": currency, **provided,
	})
	return json.dumps(entries, ensure_ascii=False, default=str)


def on_cancel(journal, method=None):
	if not _is_protected(journal):
		return
	context = _correction.get()
	if not context or context.get("action") != "cancel" or context.get("journal") != journal.name:
		frappe.throw("顺丰运费凭证必须通过运费更正流程取消。")
	doc = context["doc"]
	note = "原运费凭证已更正取消；自动记账暂停，请由财务核对后明确重新记账。"
	doc.db_set({
		"sf_freight_journal": None,
		"sf_freight_previous_journal": journal.name,
		"sf_freight_accounting_hold": 1,
		"sf_freight_accounting_status": "记账待处理",
		"sf_freight_accounting_note": note,
		"sf_freight_correction_reason": context["reason"],
		"sf_freight_correction_log": _audit(doc, "cancel", context["reason"], previous_journal=journal.name),
	}, update_modified=True)
	_sync_waybill_accounting(
		doc,
		_journal_waybill(doc, journal.name) or journal.get("sf_freight_waybill") or _waybill(doc),
		journal="",
		previous_journal=journal.name,
		status="记账待处理",
		note=note,
		hold=1,
	)


def on_trash(journal, method=None):
	if _is_protected(journal):
		frappe.throw("顺丰运费凭证需要保留更正历史，不能删除。")


@frappe.whitelist(methods=["POST"])
def cancel_freight_journal(shipment, reason):
	_require_manager()
	reason = _reason(reason)
	doc = _shipment(shipment)
	journal_name = doc.get("sf_freight_journal")
	# After a waybill replacement the old posted entry is deliberately moved to
	# the previous-journal field.  It remains cancellable through this guarded
	# workflow, but is never treated as the current label's journal.
	if not journal_name:
		previous = doc.get("sf_freight_previous_journal")
		if previous and frappe.db.exists("Journal Entry", previous):
			previous_status = frappe.db.get_value("Journal Entry", previous, "docstatus")
			if cint(previous_status) == 1:
				journal_name = previous
	if not journal_name:
		frappe.throw("此运单没有可办理更正的运费凭证。")
	journal = frappe.get_doc("Journal Entry", journal_name, for_update=True)
	journal.check_permission("read")
	journal.check_permission("cancel")
	if cint(journal.docstatus) != 1:
		frappe.throw("只有已提交的运费凭证可以办理更正取消。")
	if len(journal.get("accounts") or []) > 100:
		frappe.throw("此运费凭证明细超过同步更正处理范围，请先由财务核对凭证。")
	if _linked_shipments(journal) != {doc.name}:
		frappe.throw("此运费凭证的运单关联存在冲突，请先核对关联记录。")
	_assert_no_payment_evidence(journal)
	context = {"action": "cancel", "shipment": doc.name, "journal": journal.name, "reason": reason, "doc": doc}
	frappe.db.savepoint("sf_freight_correction_cancel")
	token = _correction.set(context)
	try:
		# Native cancellation/link/ledger checks run inside the same transaction as the audit.
		if not journal.get("sf_freight_shipment"):
			journal.db_set({"sf_freight_shipment": doc.name, "sf_freight_waybill": _journal_waybill(doc, journal.name) or _waybill(doc)}, update_modified=False)
		journal.cancel()
	except Exception:
		frappe.db.rollback(save_point="sf_freight_correction_cancel")
		raise
	finally:
		_correction.reset(token)
	return {"shipment": doc.name, "journal_entry": journal.name, "accounting_status": "记账待处理", "holding": True}


def record_rebooking(doc, old_journal, new_journal):
	context = _correction.get()
	if not context or context.get("action") != "resume" or context.get("shipment") != doc.name:
		frappe.throw("重新记账必须通过财务更正流程执行。")
	if not new_journal or cint(frappe.db.get_value("Journal Entry", new_journal, "docstatus")) != 1:
		frappe.throw("重新记账尚未生成有效凭证，自动记账保持暂停。")
	doc.db_set({
		"sf_freight_accounting_hold": 0,
		"sf_freight_correction_reason": context["reason"],
		"sf_freight_correction_log": _audit(doc, "rebook", context["reason"], previous_journal=old_journal, journal_entry=new_journal),
	}, update_modified=True)
	_sync_waybill_accounting(
		doc,
		# The replacement/current label receives the new journal.  The old
		# journal's label is immutable history and must never be overwritten by a
		# later rebooking operation.
		_waybill(doc),
		journal=new_journal,
		previous_journal=old_journal or "",
		status="已记账",
		note="",
		hold=0,
	)


def _record_zero_bill_resolution(doc, old_journal, prior_current_journal):
	context = _correction.get()
	if not context or context.get("action") != "resume" or context.get("shipment") != doc.name:
		frappe.throw("零元账单必须通过财务更正流程核实。")
	from .shipping import _retained_freight_bill
	bill = _retained_freight_bill(doc)
	if not bill or bill.get("amount") != 0:
		frappe.throw("没有有效零元运费账单，不能结束运费更正。")
	# Recheck the original current link too: a helper must not hide a live entry by clearing it.
	for name in {prior_current_journal, doc.get("sf_freight_journal")} - {None, ""}:
		status = frappe.db.get_value("Journal Entry", name, "docstatus")
		if status is None or cint(status) != 2:
			frappe.throw("当前运费凭证尚未更正取消，不能按零元账单结束处理。")
	note = "已由财务核实有效零元账单，无需生成运费凭证。"
	doc.db_set({
		"sf_freight_journal": None,
		"sf_freight_previous_journal": old_journal,
		"sf_freight_accounting_hold": 0,
		"sf_freight_accounting_status": "无需记账",
		"sf_freight_accounting_note": note,
		"sf_freight_correction_reason": context["reason"],
		"sf_freight_correction_log": _audit(doc, "resolve_zero_bill", context["reason"], previous_journal=old_journal),
	}, update_modified=True)
	_sync_waybill_accounting(
		doc,
		# A zero bill resolves the current label.  Keep any cancelled prior
		# journal on its own historical row; do not mark that old label as a
		# zero-cost current label.
		_waybill(doc),
		journal="",
		previous_journal=old_journal or "",
		status="无需记账",
		note=note,
		hold=0,
	)


@frappe.whitelist(methods=["POST"])
def resume_freight_accounting(shipment, reason):
	_require_manager()
	reason = _reason(reason)
	doc = _shipment(shipment)
	if not booking_is_held(doc):
		frappe.throw("此运费没有处于暂停记账状态。")
	for permission in ("create", "submit"):
		if not frappe.has_permission("Journal Entry", permission):
			frappe.throw("重新记账需要会计凭证的创建及提交权限。", frappe.PermissionError)
	from .shipping import resume_freight_booking
	prior_current_journal = doc.get("sf_freight_journal")
	old_journal = prior_current_journal or doc.get("sf_freight_previous_journal")
	frappe.db.savepoint("sf_freight_correction_resume")
	token = _correction.set({"action": "resume", "shipment": doc.name, "reason": reason})
	try:
		result = resume_freight_booking(doc, reason)
		if result.get("accounting_status") == "无需记账":
			if result.get("journal_entry") or result.get("journal") or result.get("amount") != 0:
				frappe.throw("零元账单更正结果不一致，请重新核对账单和凭证。")
			_record_zero_bill_resolution(doc, old_journal, prior_current_journal)
			return {**result, "holding": False, "accounting_status": "无需记账"}
		record_rebooking(doc, old_journal, result.get("journal_entry") or result.get("journal"))
		return {**result, "holding": False, "accounting_status": "已记账"}
	except Exception:
		frappe.db.rollback(save_point="sf_freight_correction_resume")
		raise
	finally:
		_correction.reset(token)


def onload(doc, method=None):
	from .shipping import _is_sf_shipment
	if not _is_sf_shipment(doc):
		return
	allowed = _manager() and frappe.has_permission("Shipment", "read", doc=doc) and frappe.has_permission("Shipment", "write", doc=doc)
	holding = booking_is_held(doc)
	journal_name = doc.get("sf_freight_journal") or doc.get("sf_freight_previous_journal")
	journal = frappe.get_doc("Journal Entry", journal_name) if allowed and journal_name else None
	actions = {
		"holding": holding,
		"hold": holding, "shipment": doc.name, "journal": doc.get("sf_freight_journal") or doc.get("sf_freight_previous_journal"),
		"can_cancel": bool(journal and cint(journal.docstatus) == 1 and frappe.has_permission("Journal Entry", "cancel", doc=journal) and frappe.has_permission("Journal Entry", "read", doc=journal)),
		"can_resume": bool(allowed and holding and frappe.has_permission("Journal Entry", "create") and frappe.has_permission("Journal Entry", "submit")),
	}
	doc.set_onload("sf_freight_actions", actions)
	doc.set_onload("sf_freight_accounting", actions)


def journal_onload(journal, method=None):
	names = _linked_shipments(journal)
	if not names:
		return
	actions = {"can_cancel": False, "can_resume": False, "journal": journal.name, "protected": True}
	if len(names) == 1:
		name = next(iter(names))
		if frappe.has_permission("Shipment", "read", name):
			doc = frappe.get_doc("Shipment", name)
			onload(doc)
			actions.update(doc.get_onload().get("sf_freight_accounting") or {})
			if journal.name not in {doc.get("sf_freight_journal"), doc.get("sf_freight_previous_journal")}:
				actions.update(can_cancel=False, can_resume=False, journal=journal.name)
	journal.set_onload("sf_freight_accounting", actions)


def _migration_freight_status(doc, shipping):
	"""Return a value accepted by the current freight-status Select field.

	A valid value produced by ``sync_freight_status`` is authoritative.  For a
	legacy value, a settled label is only carried forward when the current
	waybill still has verifiable bill evidence; otherwise migration deliberately
	falls back to ``待核实``.  This prevents an old display label from becoming a
	new accounting assertion.
	"""
	value = str(doc.get("sf_freight_status") or "").strip()
	if value in _FREIGHT_STATUS_VALUES:
		return value
	legacy = _LEGACY_FREIGHT_STATUS.get(value)
	if legacy == "待核实":
		return legacy
	if legacy == "账单已取得":
		try:
			checker = getattr(shipping, "_has_current_freight_evidence", None)
			if callable(checker):
				has_evidence = bool(checker(doc))
			else:
				has_evidence = _legacy_bill_evidence(doc)
			if has_evidence:
				return legacy
		except Exception:
			pass
	return "待核实"


def _legacy_bill_evidence(doc):
	"""Small dependency-free fallback for sites upgrading an older shipping module."""
	try:
		payload = json.loads(doc.get("sf_freight_payload") or "{}")
	except (TypeError, ValueError):
		return False
	if not isinstance(payload, dict):
		return False
	waybill = str(doc.get("shipment_id") or doc.get("awb_number") or "").strip()
	records = payload.get("records")
	try:
		amount = float(payload.get("amount"))
	except (TypeError, ValueError):
		return False
	currency = str(payload.get("currency") or "").strip().upper()
	return bool(
		waybill
		and str(payload.get("waybill") or "").strip() == waybill
		and isinstance(records, list)
		and bool(records)
		and all(isinstance(item, dict) for item in records)
		and math.isfinite(amount)
		and amount >= 0
		and len(currency) == 3
		and currency.isalpha()
	)


def _log_migration_error(stage, name, error):
	"""Keep one malformed legacy row from aborting the remaining migration."""
	try:
		frappe.log_error(
			message=str(error)[:1000],
			title="SF freight migration {0}: {1}".format(stage, name or "unknown"),
		)
	except Exception:
		# Logging must not turn an isolated migration failure into a batch failure.
		pass


def migrate_existing_freight():
	"""Backfill durable markers using unambiguous structured Shipment links."""
	from . import shipping
	from .shipping import _is_sf_shipment, sync_freight_status
	rows = frappe.get_all("Shipment", filters={"sf_freight_journal": ["is", "set"]}, fields=["name", "sf_freight_journal"])
	linked = {}
	for row in rows:
		linked.setdefault(row.sf_freight_journal, []).append(row.name)
	for journal_name, names in linked.items():
		if len(names) != 1:
			continue
		try:
			doc = frappe.get_doc("Shipment", names[0], for_update=True)
			if not _is_sf_shipment(doc) or not frappe.db.exists("Journal Entry", journal_name):
				continue
			journal = frappe.get_doc("Journal Entry", journal_name, for_update=True)
			marker = journal.get("sf_freight_shipment")
			if marker and marker != doc.name:
				continue
			if not marker:
				journal.db_set({"sf_freight_shipment": doc.name, "sf_freight_waybill": _waybill(doc)}, update_modified=False)
			if cint(journal.docstatus) == 2:
				reason = "历史关联运费凭证已取消，等待财务核实后重新记账。"
				doc.db_set({
					"sf_freight_journal": None, "sf_freight_previous_journal": journal.name,
					"sf_freight_accounting_hold": 1, "sf_freight_accounting_status": "记账待处理",
					"sf_freight_correction_reason": reason,
					"sf_freight_correction_log": _audit(doc, "migrate_cancelled_journal", reason, previous_journal=journal_name),
				}, update_modified=False)
		except Exception as exc:
			_log_migration_error("journal", journal_name, exc)
			continue
	candidates = frappe.get_all("Shipment", fields=["name", "service_provider", "carrier"])
	for candidate in candidates:
		if not _is_sf_shipment(candidate):
			continue
		try:
			doc = frappe.get_doc("Shipment", candidate.name, for_update=True)
			if not _is_sf_shipment(doc):
				continue
			sync_freight_status(doc)
			doc.db_set({
				"sf_freight_status": _migration_freight_status(doc, shipping),
				"sf_freight_accounting_status": doc.get("sf_freight_accounting_status"),
			}, update_modified=False)
		except Exception as exc:
			_log_migration_error("shipment", getattr(candidate, "name", ""), exc)
			continue
