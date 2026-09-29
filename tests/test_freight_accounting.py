"""Offline tests for authorization and audit of freight accounting corrections."""

import importlib.util
import json
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


class ValidationError(Exception):
	pass


class PermissionError(Exception):
	pass


class Doc(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def set(self, key, value):
		self[key] = value

	def is_new(self):
		return bool(self.get("_new"))

	def get_doc_before_save(self):
		return self.get("_previous")

	def check_permission(self, permission):
		if permission in self.get("denied", ()):
			raise PermissionError(permission)

	def db_set(self, values, **kwargs):
		self.update(values)

	def set_onload(self, key, value):
		self.setdefault("__onload", {})[key] = value

	def get_onload(self):
		return self.get("__onload", {})


class FreightAccounting(unittest.TestCase):
	def setUp(self):
		self.frappe = types.ModuleType("frappe")
		self.frappe.whitelist = lambda *args, **kwargs: lambda func: func
		self.frappe.throw = lambda message, exc=ValidationError, **kwargs: (_ for _ in ()).throw(exc(message))
		self.frappe.PermissionError = PermissionError
		self.frappe.session = Doc(user="finance@example.com")
		self.frappe.get_roles = Mock(return_value=["Accounts Manager"])
		self.frappe.has_permission = Mock(return_value=True)
		utils = types.ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		utils.now_datetime = lambda: datetime(2026, 9, 8, 12)
		self.shipping = types.ModuleType("erpnext_shipping.sf_international.shipping")
		self.shipping._is_sf_shipment = lambda doc: doc.get("service_provider") in {"SF International", "顺丰国际"} or doc.get("carrier") == "SF"
		self.shipping.resume_freight_booking = Mock()
		self.shipping._retained_freight_bill = Mock(return_value=None)
		self.shipping.sync_freight_status = Mock()
		self.modules = {"frappe": self.frappe, "frappe.utils": utils, "erpnext_shipping.sf_international.shipping": self.shipping}
		path = Path(__file__).resolve().parents[1] / "erpnext_shipping/sf_international/freight_accounting.py"
		spec = importlib.util.spec_from_file_location("erpnext_shipping.sf_international.freight_accounting", path)
		self.guard = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, self.modules):
			spec.loader.exec_module(self.guard)
		self.doc = Doc(name="SHIP-1", doctype="Shipment", docstatus=2, service_provider="SF International", shipment_id="SF123", sf_freight_journal="JE-1", shipment_amount=233.84, sf_freight_currency="CNY", sf_freight_payload='{"waybill":"SF123","records":[{"payAmount":233.84}]}')
		self.journal = Doc(name="JE-1", doctype="Journal Entry", docstatus=1, sf_freight_shipment="SHIP-1", sf_freight_waybill="SF123", accounts=[])
		self.journal._previous = Doc(self.journal)
		self.docs = {("Shipment", "SHIP-1"): self.doc, ("Journal Entry", "JE-1"): self.journal}
		self.persisted_markers = {"JE-1": "SHIP-1"}
		self.frappe.db = Mock()
		self.frappe.db.exists.return_value = False
		self.frappe.db.get_value.side_effect = self._value
		self.frappe.get_all = Mock(side_effect=self._all)
		self.frappe.get_doc = Mock(side_effect=lambda doctype, name, **kwargs: self.docs[(doctype, name)])
		self.journal.cancel = self._cancel

	def _value(self, doctype, name, field, **kwargs):
		if field == "sf_freight_shipment":
			return self.persisted_markers.get(name)
		if isinstance(field, list):
			return self.docs.get((doctype, name))
		return self.docs.get((doctype, name), {}).get(field)

	def _all(self, doctype, filters, pluck=None, **kwargs):
		if doctype == "Shipment":
			return [self.doc.name] if filters.get("sf_freight_journal") == self.doc.sf_freight_journal else []
		return []

	def _cancel(self):
		self.guard.before_cancel(self.journal)
		self.journal.docstatus = 2
		self.journal.ignore_linked_doctypes = ("GL Entry", "Payment Ledger Entry")
		self.guard.on_cancel(self.journal)

	@contextmanager
	def imports(self):
		with patch.dict(sys.modules, self.modules):
			yield

	def test_generic_cancel_blocked_after_local_shipment_cancelled(self):
		with self.assertRaisesRegex(ValidationError, "不能直接取消"):
			self.guard.before_cancel(self.journal)
		self.assertEqual(self.journal.docstatus, 1)

	def test_history_marker_prevents_cancel_bypass_by_client_stripping_marker(self):
		self.doc.sf_freight_journal = "JE-2"
		self.journal.sf_freight_shipment = ""
		with self.assertRaises(ValidationError):
			self.guard.before_cancel(self.journal)
		with self.assertRaises(ValidationError):
			self.guard.on_trash(self.journal)

	def test_client_flags_cannot_authorize_cancellation(self):
		self.journal.flags = Doc(ignore_permissions=True, sf_freight_correction=True)
		with self.assertRaises(ValidationError):
			self.guard.before_cancel(self.journal)

	def test_guarded_cancel_retains_bill_journal_and_native_link_checks(self):
		with self.imports():
			result = self.guard.cancel_freight_journal("SHIP-1", "原凭证费用科目需要更正")
		self.assertTrue(result["holding"])
		self.assertIsNone(self.doc.sf_freight_journal)
		self.assertEqual(self.doc.sf_freight_previous_journal, "JE-1")
		self.assertEqual(self.doc.shipment_amount, 233.84)
		self.assertEqual(self.doc.sf_freight_accounting_status, "记账待处理")
		self.assertEqual(self.journal.ignore_linked_doctypes, ("GL Entry", "Payment Ledger Entry"))
		self.assertFalse(self.journal.get("ignore_links"))
		audit = json.loads(self.doc.sf_freight_correction_log)[0]
		self.assertEqual(audit["previous_journal"], "JE-1")
		self.assertEqual(audit["user"], "finance@example.com")
		self.assertEqual(audit["bill"], self.doc.sf_freight_payload)
		self.assertEqual(self.frappe.get_doc.call_args_list[0].kwargs, {"for_update": True})
		self.assertEqual(self.frappe.get_doc.call_args_list[1].kwargs, {"for_update": True})

	def test_cancel_requires_manager_reason_document_and_cancel_permissions(self):
		with self.imports():
			self.frappe.get_roles.return_value = ["Accounts User"]
			with self.assertRaises(PermissionError):
				self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")
			self.frappe.get_roles.return_value = ["Accounts Manager"]
			with self.assertRaises(ValidationError):
				self.guard.cancel_freight_journal("SHIP-1", "错")
			for doc, permission in ((self.doc, "read"), (self.doc, "write"), (self.journal, "cancel")):
				with self.subTest(permission=permission):
					doc.denied = {permission}
					with self.assertRaises(PermissionError):
						self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")
					doc.denied = set()

	def test_all_payment_and_reconciliation_evidence_blocks_cancel(self):
		for doctype in ("Payment Entry Reference", "Payment Ledger Entry"):
			with self.subTest(doctype=doctype), self.imports():
				self.frappe.db.exists.side_effect = lambda dt, filters: dt == doctype
				with self.assertRaisesRegex(ValidationError, "付款、核销"):
					self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")
				self.assertEqual(self.journal.docstatus, 1)
		self.frappe.db.exists.side_effect = None
		self.journal.clearance_date = "2026-09-08"
		with self.imports(), self.assertRaisesRegex(ValidationError, "付款或其他"):
			self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")

	def test_journal_entry_account_reference_checks_parent_status_without_child_docstatus(self):
		self.frappe.db.exists.side_effect = lambda _dt, _filters: False
		self.frappe.get_all.side_effect = lambda doctype, filters=None, **_kwargs: (
			[Doc(parent="JE-OTHER")] if doctype == "Journal Entry Account" else []
		)
		self.frappe.db.get_value.side_effect = lambda doctype, name, field, **_kwargs: (
			1 if doctype == "Journal Entry" and name == "JE-OTHER" and field == "docstatus" else self._value(doctype, name, field, **_kwargs)
		)
		with self.imports(), self.assertRaisesRegex(ValidationError, "付款、核销"):
			self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")

	def test_cancelled_journal_entry_account_parent_is_ignored(self):
		self.frappe.db.exists.side_effect = lambda _dt, _filters: False
		self.frappe.get_all.side_effect = lambda doctype, filters=None, **_kwargs: (
			[Doc(parent="JE-CANCELLED")] if doctype == "Journal Entry Account" else []
		)
		self.frappe.db.get_value.side_effect = lambda doctype, name, field, **_kwargs: (
			2 if doctype == "Journal Entry" and name == "JE-CANCELLED" and field == "docstatus" else self._value(doctype, name, field, **_kwargs)
		)
		with self.imports():
			result = self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")
		self.assertTrue(result["holding"])

	def test_bank_reconciliation_is_checked_from_parent_including_draft_children(self):
		self.docs[("Bank Transaction", "BANK-1")] = Doc(docstatus=0, status="Reconciled")
		self.frappe.get_all.side_effect = lambda doctype, **kwargs: [Doc(parent="BANK-1", parenttype="Bank Transaction", docstatus=0)] if doctype == "Bank Transaction Payments" else []
		with self.imports(), self.assertRaisesRegex(ValidationError, "银行对账关联"):
			self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")
		self.assertEqual(self.journal.docstatus, 1)

	def test_cancel_failure_rolls_back_audit_and_native_operations(self):
		def failed_cancel():
			self._cancel()
			raise ValidationError("native linked document prevents cancellation")
		self.journal.cancel = failed_cancel
		with self.imports(), self.assertRaisesRegex(ValidationError, "native linked"):
			self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")
		self.frappe.db.rollback.assert_called_once_with(save_point="sf_freight_correction_cancel")
		self.assertIsNone(self.guard._correction.get())

	def test_conflicting_shipment_links_block_correction(self):
		self.persisted_markers["JE-1"] = "SHIP-OTHER"
		with self.imports(), self.assertRaisesRegex(ValidationError, "关联存在冲突"):
			self.guard.cancel_freight_journal("SHIP-1", "费用科目有误")

	def test_generic_new_document_cannot_forge_guard_fields(self):
		for field in self.guard.SHIPMENT_FIELDS:
			with self.subTest(field=field), self.assertRaises(ValidationError):
				self.guard.validate_shipment(Doc(_new=True, **{field: "forged"}))
		for field in self.guard.JOURNAL_FIELDS:
			with self.subTest(field=field), self.assertRaises(ValidationError):
				self.guard.validate_journal(Doc(_new=True, **{field: "forged"}))

	def test_current_link_and_audit_cannot_be_removed(self):
		self.doc._previous = Doc(self.doc)
		self.doc.sf_freight_journal = ""
		with self.assertRaises(ValidationError):
			self.guard.validate_shipment(self.doc)

	def test_existing_sf_freight_amount_cannot_be_forged(self):
		self.doc._previous = Doc(self.doc)
		self.doc.shipment_amount = 0
		with self.imports(), self.assertRaisesRegex(ValidationError, "运费金额只能"):
			self.guard.validate_shipment(self.doc)

	def test_missing_or_cancelled_current_journal_holds_accounting(self):
		for status in (None, 2):
			with self.subTest(status=status):
				self.journal.docstatus = status
				self.assertTrue(self.guard.booking_is_held(self.doc))
				with self.assertRaises(ValidationError):
					self.guard.assert_can_book(self.doc)

	def test_generic_amend_cannot_rebook_protected_journal(self):
		with self.assertRaisesRegex(ValidationError, "不能直接修订"):
			self.guard.validate_journal(Doc(_new=True, amended_from="JE-1"))

	def test_existing_protected_draft_cannot_be_submitted_outside_booking(self):
		self.journal._previous.docstatus = 0
		with self.assertRaisesRegex(ValidationError, "专用记账流程提交"):
			self.guard.validate_journal(self.journal)

	def test_server_creation_context_registers_immutable_origin_and_resets(self):
		journal = Doc(_new=True)
		with self.assertRaises(ValidationError):
			self.guard.register_journal(self.doc, journal)
		with self.guard.journal_creation(self.doc, previous_journal="JE-OLD"):
			self.guard.register_journal(self.doc, journal)
			self.guard.validate_journal(journal)
		self.assertEqual(journal.sf_freight_previous_journal, "JE-OLD")
		with self.assertRaises(ValidationError):
			self.guard.validate_journal(journal)

	def test_hold_blocks_automatic_booking_even_with_client_flags(self):
		self.doc.sf_freight_accounting_hold = 1
		self.doc.flags = Doc(sf_freight_correction=True)
		with self.assertRaisesRegex(ValidationError, "暂停自动记账"):
			self.guard.assert_can_book(self.doc)
		with self.assertRaises(ValidationError), self.guard.journal_creation(self.doc):
			pass

	def test_resume_requires_create_submit_and_successful_rebooking_audits(self):
		self.doc.sf_freight_accounting_hold = 1
		for permission in ("create", "submit"):
			self.frappe.has_permission.side_effect = lambda dt, p, **kwargs: p != permission
			with self.subTest(permission=permission), self.imports(), self.assertRaises(PermissionError):
				self.guard.resume_freight_accounting("SHIP-1", "科目已核实确认重新记账")
		self.frappe.has_permission.side_effect = None
		self.docs[("Journal Entry", "JE-2")] = Doc(docstatus=1)
		def resume(doc, reason):
			self.guard.assert_can_book(doc)
			doc.sf_freight_journal = "JE-2"
			return {"journal_entry": "JE-2"}
		self.shipping.resume_freight_booking.side_effect = resume
		with self.imports():
			result = self.guard.resume_freight_accounting("SHIP-1", "科目已核实确认重新记账")
		self.assertEqual(result["journal_entry"], "JE-2")
		self.assertFalse(self.guard.booking_is_held(self.doc))
		audit = json.loads(self.doc.sf_freight_correction_log)[0]
		self.assertEqual((audit["previous_journal"], audit["journal_entry"]), ("JE-1", "JE-2"))

	def test_failed_resume_keeps_hold_and_resets_authorization(self):
		self.doc.sf_freight_accounting_hold = 1
		self.shipping.resume_freight_booking.side_effect = ValidationError("没有有效账单")
		with self.imports(), self.assertRaises(ValidationError):
			self.guard.resume_freight_accounting("SHIP-1", "科目已核实确认重新记账")
		self.frappe.db.rollback.assert_called_once_with(save_point="sf_freight_correction_resume")
		self.assertTrue(self.guard.booking_is_held(self.doc))
		with self.assertRaises(ValidationError):
			self.guard.assert_can_book(self.doc)

	def prepare_zero_bill_resume(self):
		self.doc.sf_freight_journal = None
		self.doc.sf_freight_previous_journal = "JE-1"
		self.doc.sf_freight_accounting_hold = 1
		self.doc.shipment_amount = 0
		self.doc.sf_freight_payload = '{"waybill":"SF123","records":[{"payAmount":0}]}'
		self.journal.docstatus = 2
		self.shipping._retained_freight_bill.return_value = {"waybill": "SF123", "amount": 0, "currency": "CNY", "records": [{"payAmount": 0}]}
		self.shipping.resume_freight_booking.return_value = {"journal": None, "journal_entry": None, "amount": 0, "accounting_status": "无需记账"}

	def test_valid_zero_bill_resolution_releases_hold_and_preserves_audit(self):
		self.prepare_zero_bill_resume()
		with self.imports():
			result = self.guard.resume_freight_accounting("SHIP-1", "顺丰已确认零元账单")
		self.assertFalse(result["holding"])
		self.assertEqual(result["accounting_status"], "无需记账")
		self.assertIsNone(self.doc.sf_freight_journal)
		self.assertEqual(self.doc.sf_freight_previous_journal, "JE-1")
		self.assertFalse(self.guard.booking_is_held(self.doc))
		audit = json.loads(self.doc.sf_freight_correction_log)[0]
		self.assertEqual(audit["action"], "resolve_zero_bill")
		self.assertEqual(audit["previous_journal"], "JE-1")
		self.assertEqual(audit["bill"], self.doc.sf_freight_payload)
		self.assertIsNone(self.guard._correction.get())

	def test_zero_bill_resolution_rejects_unverified_or_positive_bill(self):
		for bill in (None, {"amount": 3.5}):
			with self.subTest(bill=bill):
				self.prepare_zero_bill_resume()
				self.shipping._retained_freight_bill.return_value = bill
				with self.imports(), self.assertRaisesRegex(ValidationError, "有效零元"):
					self.guard.resume_freight_accounting("SHIP-1", "顺丰已确认零元账单")
				self.assertTrue(self.guard.booking_is_held(self.doc))
				self.assertFalse(self.doc.get("sf_freight_correction_log"))
				self.frappe.db.rollback.assert_called_with(save_point="sf_freight_correction_resume")

	def test_zero_bill_resolution_does_not_hide_existing_active_journal(self):
		for status in (0, 1, None):
			with self.subTest(status=status):
				self.prepare_zero_bill_resume()
				self.doc.sf_freight_journal = "JE-1"
				self.journal.docstatus = status
				def bad_helper(doc, reason):
					doc.sf_freight_journal = None
					return self.shipping.resume_freight_booking.return_value
				self.shipping.resume_freight_booking.side_effect = bad_helper
				with self.imports(), self.assertRaisesRegex(ValidationError, "尚未更正取消"):
					self.guard.resume_freight_accounting("SHIP-1", "顺丰已确认零元账单")
				self.assertTrue(self.guard.booking_is_held(self.doc))
				self.frappe.db.rollback.assert_called_with(save_point="sf_freight_correction_resume")

	def test_zero_bill_resolution_accepts_previously_cancelled_current_journal(self):
		self.prepare_zero_bill_resume()
		self.doc.sf_freight_journal = "JE-1"
		with self.imports():
			result = self.guard.resume_freight_accounting("SHIP-1", "顺丰已确认零元账单")
		self.assertEqual(result["accounting_status"], "无需记账")
		self.assertEqual(self.doc.sf_freight_previous_journal, "JE-1")
		self.assertIsNone(self.doc.sf_freight_journal)

	def test_zero_bill_result_requires_explicit_zero_amount_and_no_journal(self):
		for invalid in ({"amount": None}, {"amount": 2}, {"journal_entry": "JE-2"}, {"journal": "JE-2"}):
			with self.subTest(invalid=invalid):
				self.prepare_zero_bill_resume()
				self.shipping.resume_freight_booking.return_value.update(invalid)
				with self.imports(), self.assertRaisesRegex(ValidationError, "更正结果不一致"):
					self.guard.resume_freight_accounting("SHIP-1", "顺丰已确认零元账单")
				self.assertTrue(self.guard.booking_is_held(self.doc))

	def test_ordinary_journal_unchanged(self):
		journal = Doc(name="OTHER", _new=True)
		self.guard.validate_journal(journal)
		self.guard.before_cancel(journal)
		self.guard.on_cancel(journal)
		self.guard.on_trash(journal)
		self.assertFalse(journal.get("ignore_linked_doctypes"))

	def test_onload_permissions_hide_correction_for_non_finance(self):
		self.frappe.get_roles.return_value = ["Stock Manager"]
		with self.imports():
			self.guard.onload(self.doc)
		actions = self.doc.get_onload()["sf_freight_accounting"]
		self.assertFalse(actions["can_cancel"])
		self.assertFalse(actions["can_resume"])

	def test_migration_moves_cancelled_link_to_history_and_is_idempotent(self):
		self.journal.docstatus = 2
		self.journal.sf_freight_shipment = None
		self.frappe.db.exists.return_value = True
		def rows(doctype, filters=None, pluck=None, **kwargs):
			if filters is None:
				return [Doc(name=self.doc.name, service_provider=self.doc.service_provider)]
			return [Doc(name=self.doc.name, sf_freight_journal=self.doc.sf_freight_journal)] if self.doc.sf_freight_journal else []
		self.frappe.get_all.side_effect = rows
		self.shipping.sync_freight_status.side_effect = lambda doc: doc.update(sf_freight_status="账单已取得")
		with self.imports():
			self.guard.migrate_existing_freight()
			self.guard.migrate_existing_freight()
		self.assertIsNone(self.doc.sf_freight_journal)
		self.assertEqual(self.doc.sf_freight_previous_journal, "JE-1")
		self.assertEqual(self.journal.sf_freight_shipment, self.doc.name)
		self.assertEqual(self.doc.sf_freight_status, "账单已取得")
		self.assertTrue(self.guard.booking_is_held(self.doc))
		self.assertEqual(len(json.loads(self.doc.sf_freight_correction_log)), 1)

	def test_migration_persists_chinese_and_carrier_only_sf_but_skips_other_carriers(self):
		persisted = {}
		class Shipment(Doc):
			def db_set(self, values, **kwargs):
				persisted[self.name] = dict(values)
				self.update(values)
		chinese = Shipment(name="SF-CN", service_provider="顺丰国际", carrier="顺丰国际", sf_freight_status="未结算")
		carrier_only = Shipment(name="SF-CARRIER", service_provider=None, carrier="SF", sf_freight_status="未结算")
		other = Shipment(name="DHL-1", service_provider="SendCloud", carrier="DHL", sf_freight_status="Original")
		for doc in (chinese, carrier_only, other):
			self.docs[("Shipment", doc.name)] = doc
		self.frappe.get_all.side_effect = lambda doctype, filters=None, **kwargs: [] if filters else [chinese, carrier_only, other]
		self.shipping.sync_freight_status.side_effect = lambda doc: doc.update(sf_freight_status="账单已取得", sf_freight_accounting_status="已记账")
		with self.imports():
			self.guard.migrate_existing_freight()
		self.assertEqual(set(persisted), {"SF-CN", "SF-CARRIER"})
		for values in persisted.values():
			self.assertEqual(values, {"sf_freight_status": "账单已取得", "sf_freight_accounting_status": "已记账"})
		self.assertEqual(other.sf_freight_status, "Original")
		self.assertEqual(self.shipping.sync_freight_status.call_count, 2)
		for call in self.frappe.get_doc.call_args_list:
			self.assertTrue(call.kwargs.get("for_update"))
			self.assertNotEqual(call.args[1], "DHL-1")

	def test_migration_clamps_legacy_freight_status_to_child_select_values(self):
		"""Old parent labels must never be written into the current Select field."""
		persisted = {}
		class Shipment(Doc):
			def db_set(self, values, **kwargs):
				persisted[self.name] = dict(values)
				self.update(values)

		legacy_unsettled = Shipment(name="SF-LEGACY-UNSETTLED", service_provider="顺丰国际", sf_freight_status="未结算")
		legacy_settled = Shipment(name="SF-LEGACY-SETTLED", service_provider="顺丰国际", sf_freight_status="已结算")
		self.docs[("Shipment", legacy_unsettled.name)] = legacy_unsettled
		self.docs[("Shipment", legacy_settled.name)] = legacy_settled
		self.frappe.get_all.side_effect = lambda doctype, filters=None, **kwargs: [] if filters else [legacy_unsettled, legacy_settled]
		self.shipping.sync_freight_status.side_effect = lambda doc: None
		with self.imports():
			self.guard.migrate_existing_freight()

		self.assertEqual(persisted[legacy_unsettled.name]["sf_freight_status"], "待核实")
		self.assertEqual(persisted[legacy_settled.name]["sf_freight_status"], "待核实")
		self.assertTrue({values["sf_freight_status"] for values in persisted.values()} <= {"待核实", "账单已取得"})

	def test_migration_keeps_legacy_settled_label_only_with_current_bill_evidence(self):
		persisted = {}
		class Shipment(Doc):
			def db_set(self, values, **kwargs):
				persisted[self.name] = dict(values)
				self.update(values)

		legacy = Shipment(name="SF-LEGACY-BILL", service_provider="顺丰国际", sf_freight_status="已结算")
		self.docs[("Shipment", legacy.name)] = legacy
		self.frappe.get_all.side_effect = lambda doctype, filters=None, **kwargs: [] if filters else [legacy]
		self.shipping.sync_freight_status.side_effect = lambda doc: None
		self.shipping._has_current_freight_evidence = Mock(return_value=True)
		with self.imports():
			self.guard.migrate_existing_freight()

		self.assertEqual(persisted[legacy.name]["sf_freight_status"], "账单已取得")

	def test_migration_isolates_bad_shipment_and_continues_batch(self):
		"""A malformed row must not abort migration for later SF shipments."""
		persisted = {}
		class Shipment(Doc):
			def db_set(self, values, **kwargs):
				persisted[self.name] = dict(values)
				self.update(values)

		bad = Shipment(name="SF-BAD", service_provider="SF International", sf_freight_status="未结算")
		good = Shipment(name="SF-GOOD", service_provider="SF International", sf_freight_status="未结算")
		self.docs["Shipment", bad.name] = bad
		self.docs["Shipment", good.name] = good

		self.frappe.get_all.side_effect = lambda doctype, filters=None, **kwargs: [] if filters else [bad, good]
		def sync(doc):
			if doc.name == bad.name:
				raise RuntimeError("malformed legacy freight payload")
			doc.sf_freight_status = "账单已取得"
			doc.sf_freight_accounting_status = "待记账"
		self.shipping.sync_freight_status.side_effect = sync
		self.frappe.log_error = Mock()

		with self.imports():
			self.guard.migrate_existing_freight()

		self.assertNotIn(bad.name, persisted)
		self.assertEqual(persisted[good.name]["sf_freight_status"], "账单已取得")
		self.assertEqual(persisted[good.name]["sf_freight_accounting_status"], "待记账")
		self.frappe.log_error.assert_called()

	def test_journal_onload_offers_resume_for_latest_cancelled_history(self):
		self.doc.sf_freight_journal = None
		self.doc.sf_freight_previous_journal = self.journal.name
		self.doc.sf_freight_accounting_hold = 1
		self.journal.docstatus = 2
		with self.imports():
			self.guard.journal_onload(self.journal)
		actions = self.journal.get_onload()["sf_freight_accounting"]
		self.assertFalse(actions["can_cancel"])
		self.assertTrue(actions["can_resume"])
		self.assertEqual(actions["shipment"], self.doc.name)


if __name__ == "__main__":
	unittest.main()
