"""Offline shipping regression tests. No bench, database, or SF network calls."""

import importlib.util
import sys
import types
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

class ValidationError(Exception):
	pass

class PermissionError(Exception):
	pass

class Doc(types.SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def check_permission(self, permission):
		self.permissions.append(permission)
		if permission in self.denied:
			raise PermissionError(permission)

	def db_set(self, field, value=None, **kwargs):
		self.updates.append((field, value))
		self.__dict__.update(field if isinstance(field, dict) else {field: value})

	def append(self, field, value):
		getattr(self, field).append(Doc(**value))

	def insert(self):
		self.check_permission("create")
		self.inserted = True
		return self

	def submit(self):
		self.check_permission("submit")
		self.docstatus = 1

	def cancel(self):
		self.check_permission("cancel")
		self.docstatus = 2

def make_doc(**values):
	defaults = dict(
		name="SHIP-1", permissions=[], denied=set(), updates=[], flags=Doc(),
		docstatus=1, status="Booked", service_provider="SF International",
		carrier="SF International", shipment_id="SF123", awb_number="SF123",
		pickup_company="Company", company="Company", sf_freight_journal=None,
		shipment_delivery_note=[], accounts=[], inserted=False,
	)
	defaults.update(values)
	return Doc(**defaults)

def load_shipping():
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *args, **kwargs: lambda function: function
	frappe.PermissionError = PermissionError
	frappe._ = lambda value: value
	frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValidationError(message))
	frappe.log_error = Mock()
	frappe.enqueue = Mock()
	frappe.has_permission = Mock(return_value=True)
	frappe.utils = types.ModuleType("frappe.utils")
	frappe.utils.flt = lambda value: float(value or 0)
	frappe.utils.cint = lambda value: int(value or 0)
	frappe.utils.getdate = lambda *args: date(2026, 9, 6)
	frappe.utils.add_days = Mock()
	frappe.utils.add_months = Mock()
	file_manager = types.ModuleType("frappe.utils.file_manager")
	file_manager.save_file = Mock()
	erp = types.ModuleType("erpnext.stock.doctype.shipment.shipment")
	erp.get_company_contact = Mock()
	contents = types.ModuleType("erpnext.stock.doctype.shipment.shipment_contents")
	contents.goods_from_delivery_notes = Mock(return_value=[])
	contents.goods_summary = Mock(return_value="")
	contents.source_warehouse = Mock(return_value="")
	contents.warehouse_from_delivery_note = Mock(return_value=None)
	client = types.ModuleType("erpnext_shipping.sf_international.client")
	accounting = types.ModuleType("erpnext_shipping.sf_international.freight_accounting")
	accounting.booking_is_held = lambda doc: bool(doc.get("sf_freight_accounting_hold"))
	def assert_can_book(doc):
		if accounting.booking_is_held(doc):
			raise ValidationError("运费记账已暂停")
	accounting.assert_can_book = assert_can_book
	accounting.journal_creation = lambda *args, **kwargs: nullcontext()
	accounting.register_journal = Mock()
	for name in (
		"cancel_order", "create_order", "download_pdf", "get_settings", "is_enabled", "query_order_cancellation",
		"poll_label", "query_case_orders", "query_order", "query_postcode",
		"query_route", "region_cascade", "start_print",
	):
		setattr(client, name, Mock())
	modules = {
		"frappe": frappe, "frappe.utils": frappe.utils,
		"frappe.utils.file_manager": file_manager,
		"erpnext.stock.doctype.shipment.shipment": erp,
		"erpnext.stock.doctype.shipment.shipment_contents": contents,
		"erpnext_shipping.sf_international.client": client,
		"erpnext_shipping.sf_international.freight_accounting": accounting,
	}
	path = Path(__file__).resolve().parents[1] / "erpnext_shipping/sf_international/shipping.py"
	spec = importlib.util.spec_from_file_location("shipping_under_test", path)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, modules):
		spec.loader.exec_module(module)
	return module

class ShippingRegressions(unittest.TestCase):
	def setUp(self):
		self.shipping = load_shipping()
		self.doc = make_doc()
		self.journal = make_doc(name="JE-1", docstatus=0)
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.frappe.new_doc = Mock(return_value=self.journal)
		self.shipping.frappe.db = Mock()
		self.shipping._has_field = Mock(return_value=True)

	def test_sf_form_rejects_overlong_sender_and_receiver_before_request(self):
		for key, label in (("sender", "发件"), ("receiver", "收件")):
			with self.subTest(key=key), self.assertRaisesRegex(ValidationError, label + "详细地址最多 60"):
				self.shipping._create_order_body_from_form(self.doc, {key: {"address": "街" * 61}})
		self.shipping.create_order.assert_not_called()

	def test_sf_form_preserves_exactly_60_characters_in_carrier_payload(self):
		self.shipping._resolve_product = Mock(return_value=("10", "Product"))
		self.shipping._customs_presets = Mock(return_value={
			"declared_value": 20, "declared_currency": "CNY", "hs_code": "9504200090",
			"ename": "Goods", "cname": "货物",
		})
		party = dict(company="Company", contact="Contact", phone="+86 12345678901",
			country="CN", province="Hubei", city="Xianning", address="  " + "街" * 60 + "  ")
		body, _, _ = self.shipping._create_order_body_from_form(
			make_doc(total_weight=1), {"sender": party, "receiver": party}
		)
		self.assertEqual(body["pieceorderAddressInfo"]["jAddress"], "街" * 60)
		self.assertEqual(body["pieceorderAddressInfo"]["dAddress"], "街" * 60)

	def test_sf_queue_rejects_long_address_before_attempt_or_enqueue(self):
		with self.assertRaisesRegex(ValidationError, "当前 61 个"):
			self.shipping._queue_sf_initial_booking(self.doc, {"receiver": {"address": "a" * 61}})
		self.shipping.frappe.enqueue.assert_not_called()
		self.shipping.create_order.assert_not_called()

	def test_submit_old_unbooked_draft_checks_address_again(self):
		self.doc.shipment_id = self.doc.awb_number = ""
		self.shipping._ensure_official_fields_from_sf = Mock()
		for key in ("sender", "receiver"):
			form = {"sender": {"address": "仓库"}, "receiver": {"address": "街道"}}
			form[key]["address"] = "街" * 61
			self.shipping._parse_form_json = Mock(return_value=form)
			self.shipping._sender_from_warehouse = Mock(return_value=form["sender"])
			with self.subTest(key=key), self.assertRaisesRegex(ValidationError, "最多 60"):
				self.shipping.fill_sf_fields_before_submit(self.doc)
		self.shipping._ensure_official_fields_from_sf.assert_not_called()
		self.shipping.create_order.assert_not_called()
		form["receiver"]["address"] = "街" * 60
		self.shipping.fill_sf_fields_before_submit(self.doc)
		self.shipping._ensure_official_fields_from_sf.assert_called_once_with(self.doc)

	def test_legacy_address_rejects_long_value_without_truncating(self):
		self.shipping._check_read_permission = Mock()
		self.shipping._country_code = Mock(return_value="US")
		self.shipping.frappe.db.get_value.return_value = Doc(
			country="United States", address_title="Customer", address_line1="a" * 61,
			address_line2="", city="City", state="State", pincode="12345", email_id="",
		)
		with self.assertRaisesRegex(ValidationError, "收件详细地址最多 60"):
			self.shipping._sf_address("Address-1", "收件详细地址")

	def configure_freight(self, *, company_currency="CNY", account_currency="CNY", account_company="Company"):
		self.shipping._freight_masters = Mock(return_value=("Expense", "Payable", "Supplier"))
		self.shipping._je_naming_series = Mock(return_value="JE-")
		def value(doctype, name, fields, **kwargs):
			if doctype == "Company":
				return company_currency if fields == "default_currency" else "CC"
			if doctype == "Account":
				return Doc(company=account_company, account_currency=account_currency)
			self.fail((doctype, name, fields))
		self.shipping.frappe.db.get_value.side_effect = value

	def configure_receiver_memory(self):
		self.address = Doc(country="United States", state="California", city="Eureka", pincode="95501-1234", address_line1="1118 6th Street", address_line2="Suite 2")
		self.receiver = {
			"country": "US", "province": "CA", "city": "EUREKA", "post_code": "95501 1234",
			"address": "1118 6th Street Suite 2", "county": "", "doorplate": "",
			"contact": "Previous recipient", "phone": "old-phone", "company": "Old company", "email": "old@example.com",
		}
		self.shipping._address_row = Mock(return_value=self.address)
		self.shipping._country_code = Mock(return_value="US")
		self.shipping.frappe.db.get_value.return_value = Doc(**{
			field: self.receiver[key] for field, key in self.shipping.SF_RECEIVER_FIELDS
		})

	def test_saved_region_matches_normalized_destination_and_excludes_identity(self):
		self.configure_receiver_memory()
		self.address.address_line1 = "  1118   6TH STREET "
		result = self.shipping._saved_sf_receiver("Address-1")
		self.assertEqual(result["province"], "CA")
		self.assertEqual(result["city"], "EUREKA")
		self.assertNotIn("contact", result)
		self.assertNotIn("phone", result)

	def test_changed_destination_rejects_saved_region_and_prevents_overwrite(self):
		for key, value in (("pincode", "90000"), ("address_line1", "Different Street"), ("country", "Canada")):
			with self.subTest(key=key):
				self.configure_receiver_memory()
				setattr(self.address, key, value)
				if key == "country":
					self.shipping._country_code.return_value = "CA"
				self.assertIsNone(self.shipping._saved_sf_receiver("Address-1"))
				self.assertFalse(self.shipping._remember_sf_receiver("Address-1", self.receiver))
		self.shipping.frappe.db.set_value.assert_not_called()

	def test_new_region_snapshot_clears_optional_stale_values(self):
		self.configure_receiver_memory()
		self.assertTrue(self.shipping._remember_sf_receiver("Address-1", self.receiver))
		updates = self.shipping.frappe.db.set_value.call_args.args[2]
		self.assertEqual(updates["sf_county"], "")
		self.assertEqual(updates["sf_doorplate"], "")
		self.assertEqual(set(updates), {field for field, _ in self.shipping.SF_RECEIVER_FIELDS})

	def test_receiver_memory_requires_address_write_permission(self):
		self.configure_receiver_memory()
		self.shipping.frappe.has_permission.return_value = False
		self.assertFalse(self.shipping._remember_sf_receiver("Address-1", self.receiver))
		self.shipping._address_row.assert_not_called()
		self.shipping.frappe.db.set_value.assert_not_called()

	def test_long_address_memory_roundtrip_keeps_complete_street(self):
		self.configure_receiver_memory()
		self.address.address_line1 = "Long Street " * 24
		street = f"{self.address.address_line1.strip()} {self.address.address_line2}"
		self.receiver["address"] = street
		self.assertGreater(len(street), 200)
		self.assertTrue(self.shipping._remember_sf_receiver("Address-1", self.receiver))
		updates = self.shipping.frappe.db.set_value.call_args.args[2]
		self.assertEqual(updates["sf_address"], street)
		self.shipping.frappe.db.get_value.return_value = Doc(**updates)
		saved = self.shipping._saved_sf_receiver("Address-1")
		self.assertEqual(saved["address"], street)
		self.assertEqual(saved["province"], self.receiver["province"])

	def test_fallback_uses_only_successful_readable_matching_sf_shipments(self):
		self.configure_receiver_memory()
		valid = dict(delivery_address_name="Address-1", sf_form_json={"receiver": self.receiver})
		invalid = [
			make_doc(**valid, docstatus=0), make_doc(**valid, docstatus=2),
			make_doc(**valid, shipment_id="", awb_number=""),
			make_doc(**valid, status="Cancelled"), make_doc(**valid, sf_carrier_cancelled=1),
			make_doc(**valid, sf_waybill_replacement_status="失败"),
			make_doc(**valid, sf_waybill_replacement_status="待启用"),
			make_doc(**valid, service_provider="Other", carrier="Other"),
			make_doc(**{**valid, "delivery_address_name": "Address-2"}),
			make_doc(**{**valid, "sf_form_json": {"receiver": {**self.receiver, "post_code": "00000"}}}),
			make_doc(**valid, name="DENIED"),
		]
		self.shipping.frappe.has_permission.side_effect = lambda *args, **kwargs: kwargs.get("doc") != "DENIED"
		for provider, carrier in (("SF International", ""), ("顺丰国际", ""), ("", "SF International")):
			with self.subTest(provider=provider, carrier=carrier):
				self.shipping.frappe.get_list = Mock(return_value=invalid + [make_doc(**valid, service_provider=provider, carrier=carrier)])
				result = self.shipping._last_sf_receiver_for_address("Address-1")
				self.assertEqual(result["city"], "EUREKA")
				self.assertNotIn("contact", result)
				query = self.shipping.frappe.get_list.call_args.kwargs
				self.assertEqual(query["filters"]["docstatus"], 1)
				self.assertEqual(query["limit_page_length"], 20)
		self.shipping.frappe.db.set_value.assert_not_called()

	def test_reading_fallback_never_writes_or_restores_previous_recipient(self):
		self.configure_receiver_memory()
		self.shipping._saved_sf_receiver = Mock(return_value=None)
		self.shipping._last_sf_receiver_for_address = Mock(return_value=self.receiver)
		self.shipping._remember_sf_receiver = Mock()
		party = {"contact": "Current recipient", "phone": "new-phone", "company": "Current company", "email": "new@example.com", "county": "old district", "doorplate": "old door"}
		for remember in (True, False):
			result, remembered = self.shipping._apply_saved_sf_receiver(party, "Address-1", remember=remember)
			self.assertTrue(remembered)
			for key in ("contact", "phone", "company", "email"):
				self.assertEqual(result[key], party[key])
			self.assertEqual(result["county"], "")
			self.assertEqual(result["doorplate"], "")
		self.shipping._remember_sf_receiver.assert_not_called()
		self.shipping.frappe.db.set_value.assert_not_called()

	def test_remembered_region_does_not_overwrite_a_destination_changed_in_form(self):
		self.configure_receiver_memory()
		self.shipping._saved_sf_receiver = Mock(return_value=self.receiver)
		for key, value in (("country", "CA"), ("post_code", "99999"), ("address", "Another Street")):
			with self.subTest(key=key):
				party = {**self.receiver, key: value}
				result, remembered = self.shipping._apply_saved_sf_receiver(party, "Address-1")
				self.assertFalse(remembered)
				self.assertEqual(result, party)

	def test_successful_booking_cache_failure_only_rolls_back_savepoint(self):
		self.doc.delivery_address_name = "Address-1"
		self.shipping._remember_sf_receiver = Mock(side_effect=RuntimeError("private address should not be logged"))
		self.assertFalse(self.shipping._remember_successful_sf_receiver(self.doc, {"city": "Eureka"}))
		self.shipping.frappe.db.savepoint.assert_called_once_with("sf_receiver_memory")
		self.shipping.frappe.db.rollback.assert_called_once_with(save_point="sf_receiver_memory")
		self.shipping.frappe.db.commit.assert_not_called()
		self.assertNotIn("private address", str(self.shipping.frappe.log_error.call_args))

	def test_successful_booking_cache_savepoint_failure_does_not_rollback_order(self):
		self.doc.delivery_address_name = "Address-1"
		self.shipping._remember_sf_receiver = Mock()
		self.shipping.frappe.db.savepoint.side_effect = RuntimeError("database unavailable")
		self.assertFalse(self.shipping._remember_successful_sf_receiver(self.doc, {}))
		self.shipping._remember_sf_receiver.assert_not_called()
		self.shipping.frappe.db.rollback.assert_not_called()

	def test_ordinary_booking_without_history_uses_safe_memory_hook(self):
		waybill = types.ModuleType("erpnext_shipping.sf_international.waybill")
		for name in ("_assert_waybill_not_reused", "complete_initial_booking_attempt", "fail_initial_booking_attempt"):
			setattr(waybill, name, Mock())
		waybill.begin_initial_booking_attempt = Mock(return_value=None)
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		for name in ("_require_shipping_mutation", "validate_shipment_delivery_notes", "_require_submitted_delivery_note", "_ensure_official_fields_from_sf", "_clear_zero_amount"):
			setattr(self.shipping, name, Mock())
		self.shipping._is_new_doc = Mock(return_value=False)
		self.shipping._create_order_body_from_form = Mock(return_value=({}, "10", "Product"))
		self.shipping.create_order.return_value = {"data": {"trackingNo": "SF-NEW"}}
		self.shipping._order_id_after_create = Mock(return_value=None)
		self.shipping._remember_successful_sf_receiver = Mock(return_value=False)
		receiver = {"province": "CA", "city": "Eureka"}
		with patch.dict(sys.modules, {"erpnext_shipping.sf_international.waybill": waybill}):
			self.assertEqual(self.shipping._book_sf_order(self.doc, {"receiver": receiver}), "SF-NEW")
		self.shipping._remember_successful_sf_receiver.assert_called_once_with(self.doc, receiver)
		waybill.fail_initial_booking_attempt.assert_not_called()

	def test_legacy_booking_remembers_actual_carrier_doorplate_field(self):
		waybill = types.ModuleType("erpnext_shipping.sf_international.waybill")
		for name in ("_assert_waybill_not_reused", "complete_initial_booking_attempt", "fail_initial_booking_attempt"):
			setattr(waybill, name, Mock())
		waybill.begin_initial_booking_attempt = Mock(return_value=None)
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		self.doc.shipment_id = ""
		for name in ("_require_shipping_mutation", "validate_shipment_delivery_notes", "_require_submitted_delivery_note"):
			setattr(self.shipping, name, Mock())
		body = {"pieceorderAddressInfo": {"dCountry": "US", "dProvince": "CA", "dCity": "Eureka", "dPostCode": "95501", "dAddress": "6th Street", "destDoorplate": "1118"}}
		self.shipping._create_order_body = Mock(return_value=(body, "10", "Product"))
		self.shipping._require_positive_parcels = Mock(return_value=[])
		self.shipping.create_order.return_value = {"data": {"trackingNo": "SF-NEW"}}
		self.shipping._order_id_after_create = Mock(return_value=None)
		self.shipping._remember_successful_sf_receiver = Mock(return_value=True)
		with patch.dict(sys.modules, {"erpnext_shipping.sf_international.waybill": waybill}):
			self.shipping.create_sf_shipment(self.doc.name, "Company", "Sender Address", "Receiver Address", [], "Goods", 20, {})
		receiver = self.shipping._remember_successful_sf_receiver.call_args.args[1]
		self.assertEqual(receiver["doorplate"], "1118")
		self.assertEqual(receiver["address"], "6th Street")
		waybill.fail_initial_booking_attempt.assert_not_called()

	def test_mixed_currency_freight_is_rejected(self):
		payload = {"data": {"items": [{"payAmount": 100, "currency": "USD"}, {"payAmount": 100, "currency": "CNY"}]}}
		with self.assertRaisesRegex(ValidationError, "multiple currencies"):
			self.shipping._sum_pay_amount(payload)

	def test_decimal_freight_and_invalid_amounts(self):
		payload = {"data": {"items": [{"payAmount": "0.10", "currency": "CNY"}, {"payAmount": "0.20", "currency": "CNY"}]}}
		self.assertEqual(self.shipping._sum_pay_amount(payload)[:2], (0.3, "CNY"))
		for value in ("nan", "Infinity", "invalid"):
			with self.subTest(value=value), self.assertRaises(ValidationError):
				self.shipping._sum_pay_amount({"data": {"items": [{"payAmount": value}]}})

	def test_optional_order_lookup_failure_keeps_successful_booking(self):
		self.shipping.query_order.side_effect = TimeoutError("lookup timeout")
		self.assertIsNone(self.shipping._order_id_after_create({"trackingNo": "SF123"}, "SF123"))
		self.shipping.frappe.log_error.assert_called_once()

	def test_non_sf_document_events_do_not_change_official_shipment(self):
		doc = make_doc(service_provider="SendCloud", carrier="DHL", shipment_id="DHL123")
		self.shipping._ensure_official_fields_from_sf = Mock()
		for function in (self.shipping.place_sf_order_on_save, self.shipping.fill_sf_fields_before_submit, self.shipping.mark_sf_waiting_label, self.shipping.align_sf_status, self.shipping.cancel_sf_order_on_cancel):
			function(doc)
		self.assertEqual(doc.status, "Booked")
		self.assertEqual(doc.updates, [])
		self.shipping._ensure_official_fields_from_sf.assert_not_called()

	def test_after_insert_defers_new_booking_and_never_calls_carrier_in_hook(self):
		doc = make_doc(shipment_id="", awb_number="", docstatus=1)
		self.shipping._is_sf_shipment = Mock(return_value=True)
		self.shipping.is_enabled = Mock(return_value=True)
		self.shipping._is_cancelled = Mock(return_value=False)
		self.shipping._sf_waybill = Mock(return_value="")
		self.shipping._has_field = Mock(return_value=False)
		self.shipping._defaults_from_shipment = Mock(return_value={"receiver": {}})
		self.shipping._sender_from_warehouse = Mock(return_value={})
		self.shipping.validate_shipment_delivery_notes = Mock()
		self.shipping._require_submitted_delivery_note = Mock()
		self.shipping._queue_sf_initial_booking = Mock()

		self.shipping.book_sf_order_after_insert(doc)

		self.shipping._queue_sf_initial_booking.assert_called_once_with(
			doc, {"receiver": {}, "sender": {}}, allow_new_parent=True
		)
		self.shipping.create_order.assert_not_called()

	def test_flow_saves_defer_native_queue_until_local_checks_finish(self):
		doc = make_doc(shipment_id="", awb_number="", flags=Doc(flow_sf_defer_booking=True))
		self.assertIsNone(self.shipping._queue_sf_initial_booking(doc, {"receiver": {}}))
		self.shipping.frappe.enqueue.assert_not_called()
		self.shipping.create_order.assert_not_called()

	def test_flow_attempt_cannot_use_unguarded_native_booking_path(self):
		guard = types.ModuleType("erpnext_shipping.sf_international.reviewed_booking")
		guard.validate_reviewed_booking = Mock(side_effect=ValidationError("尚未批准"))
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		with patch.dict(sys.modules, {"erpnext_shipping.sf_international.reviewed_booking": guard}):
			with self.assertRaisesRegex(ValidationError, "尚未批准"):
				self.shipping._book_sf_order(make_doc(), {}, attempt=Doc(replacement_reason="Flow 整体批准创建顺丰面单"))
		self.shipping.create_order.assert_not_called()

	def test_initial_booking_queue_uses_after_commit_and_stable_job_id(self):
		attempt = Doc(name="WB-ATTEMPT")
		waybill = types.ModuleType("erpnext_shipping.sf_international.waybill")
		waybill.begin_initial_booking_attempt = Mock(return_value=attempt)
		waybill.load_initial_booking_attempt = Mock()
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		doc = make_doc(shipment_id="", awb_number="")
		with patch.dict(sys.modules, {"erpnext_shipping.sf_international.waybill": waybill}):
			self.shipping._queue_sf_initial_booking(doc, {"receiver": {}}, allow_new_parent=True)

		waybill.begin_initial_booking_attempt.assert_called_once_with(
			doc, {"receiver": {}}, allow_new_parent=True, commit=False
		)
		self.shipping.frappe.enqueue.assert_called_once_with(
			self.shipping._INITIAL_BOOKING_JOB,
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			job_id="sf-initial-booking:SHIP-1",
			deduplicate=True,
			shipment_name="SHIP-1",
			attempt_name="WB-ATTEMPT",
		)

	def test_after_commit_worker_reuses_persisted_attempt(self):
		attempt = Doc(name="WB-ATTEMPT", shipment="SHIP-1", replacement_status="创建中", form_payload='{"receiver": {}}')
		doc = make_doc(shipment_id="", awb_number="")
		waybill = types.ModuleType("erpnext_shipping.sf_international.waybill")
		waybill.load_initial_booking_attempt = Mock(return_value=attempt)
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		self.shipping.frappe.get_doc = Mock(return_value=doc)
		self.shipping._is_sf_shipment = Mock(return_value=True)
		self.shipping._is_cancelled = Mock(return_value=False)
		self.shipping._sf_waybill = Mock(return_value="")
		self.shipping._sender_from_warehouse = Mock(return_value={})
		self.shipping._book_sf_order = Mock(return_value="NEW-WB")
		self.shipping._persist_sf_order_projection = Mock()
		with patch.dict(sys.modules, {"erpnext_shipping.sf_international.waybill": waybill}):
			result = self.shipping.book_sf_order_after_commit("SHIP-1", "WB-ATTEMPT")

		self.assertEqual(result, "NEW-WB")
		self.shipping._book_sf_order.assert_called_once()
		self.assertIs(self.shipping._book_sf_order.call_args.kwargs["attempt"], attempt)
		self.shipping._persist_sf_order_projection.assert_called_once_with(doc)

	def test_unbooked_sf_draft_keeps_provider_for_after_commit_worker(self):
		doc = make_doc(
			shipment_id="",
			awb_number="",
			carrier_service="国际小包",
			shipment_amount=0,
			sf_freight_status="待核实",
		)
		self.shipping.sync_freight_status(doc)
		self.assertEqual(doc.service_provider, "SF International")
		self.assertEqual(doc.carrier, "SF International")
		self.assertEqual(doc.carrier_service, "国际小包")

	def test_new_existing_waybill_is_attached_without_creating_another_order(self):
		doc = make_doc(shipment_id="EXIST-WB", awb_number="EXIST-WB", docstatus=0)
		self.shipping._is_sf_shipment = Mock(return_value=True)
		self.shipping.is_enabled = Mock(return_value=True)
		self.shipping._is_cancelled = Mock(return_value=False)
		self.shipping._sf_waybill = Mock(return_value="EXIST-WB")
		self.shipping._has_field = Mock(return_value=False)
		self.shipping._attach_existing_sf_waybill = Mock()
		self.shipping._persist_sf_order_projection = Mock()
		waybill = types.ModuleType("erpnext_shipping.sf_international.waybill")
		waybill.ensure_waybill_record = Mock(return_value=True)
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		with patch.dict(sys.modules, {"erpnext_shipping.sf_international.waybill": waybill}):
			self.shipping.book_sf_order_after_insert(doc)

		self.shipping._attach_existing_sf_waybill.assert_called_once_with(doc)
		waybill.ensure_waybill_record.assert_called_once_with(doc)
		self.shipping._persist_sf_order_projection.assert_called_once_with(doc)
		self.shipping.create_order.assert_not_called()

	def test_custom_cancelled_status_is_terminal(self):
		doc = make_doc(status="已取消发货", docstatus=1)
		self.assertTrue(self.shipping._is_cancelled(doc))
		self.shipping.mark_sf_waiting_label(doc)
		self.assertEqual(doc.status, "已取消发货")

	def test_interception_blocks_status_alignment(self):
		doc = make_doc(status="Submitted", sf_intercept_status="申请中")
		self.shipping.mark_sf_waiting_label(doc)
		self.shipping.align_sf_status(doc)
		self.assertEqual(doc.status, "Submitted")

	def test_linked_shipment_selection_delegates_to_erpnext(self):
		module = types.ModuleType("erpnext.stock.doctype.shipment.shipment_summary")
		module.select_linked_shipment_row = Mock(return_value=self.doc)
		rows = [self.doc]
		with patch.dict(sys.modules, {module.__name__: module}):
			self.assertIs(self.shipping._select_linked_shipment_row(rows), self.doc)
			module.select_linked_shipment_row.assert_called_once_with(rows, include_cancelled=False)
			module.select_linked_shipment_row.reset_mock()
			self.assertIs(self.shipping._select_linked_shipment_row(rows, include_cancelled=True), self.doc)
			module.select_linked_shipment_row.assert_called_once_with(rows, include_cancelled=True)

	def test_historical_panel_filters_native_cancelled_docs_before_shared_selection(self):
		active = make_doc(name="ACTIVE", docstatus=1, status="已取消发货", sf_carrier_cancelled=1)
		historical = make_doc(name="HISTORY", docstatus=2)
		self.shipping._check_read_permission = Mock()
		self.shipping.frappe.get_all = Mock(return_value=[active.name, historical.name])
		self.shipping.frappe.get_list = Mock(return_value=[active, historical])
		self.shipping._select_linked_shipment_row = Mock(return_value=historical)
		self.shipping.frappe.get_doc.return_value = historical
		self.assertIs(self.shipping._find_historical_linked_shipment("DN-1", active=active), historical)
		self.shipping._select_linked_shipment_row.assert_called_once_with([historical], include_cancelled=True)
		self.shipping.frappe.get_doc.assert_called_once_with("Shipment", "HISTORY")
		self.assertEqual(historical.permissions, ["read"])

	def load_interception_for_summary(self):
		"""Use actual provider state constants with offline Frappe/client dependencies."""
		self.shipping.frappe.utils.escape_html = str
		self.shipping.frappe.utils.now_datetime = Mock()
		client = types.ModuleType("erpnext_shipping.sf_international.client")
		client.cancellation_evidence = Mock()
		client.query_order_cancellation = Mock()
		path = Path(__file__).resolve().parents[1] / "erpnext_shipping/sf_international/interception.py"
		name = "erpnext_shipping.sf_international.interception"
		spec = importlib.util.spec_from_file_location(name, path)
		module = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, {
			"frappe": self.shipping.frappe, "frappe.utils": self.shipping.frappe.utils,
			client.__name__: client,
		}):
			spec.loader.exec_module(module)
		return module

	def test_summary_enricher_reads_active_sf_rows_once_and_only_returns_provider_fields(self):
		interception = self.load_interception_for_summary()
		rows = [
			{"name": "SF-CANCELLED", "service_provider": "SF International", "docstatus": 1},
			{"name": "SF-PENDING", "carrier": "顺丰国际", "docstatus": 1},
			{"name": "SF-NORMAL", "service_provider": "SF", "docstatus": 1, "tracking_status": "Delivered"},
			{"name": "OTHER", "service_provider": "Other carrier", "docstatus": 1},
			{"name": "LOCAL-CANCELLED", "service_provider": "SF", "docstatus": 2},
		]
		self.shipping.frappe.get_list = Mock(return_value=[
			{"name": "SF-CANCELLED", "sf_carrier_cancelled": 1},
			{"name": "SF-PENDING", "sf_intercept_status": interception.CARRIER_PENDING_STATE},
			{"name": "SF-NORMAL", "sf_intercept_status": "", "sf_carrier_cancelled": 0},
		])
		with patch.dict(sys.modules, {interception.__name__: interception}):
			result = self.shipping.enrich_shipment_summary(shipments=rows)
		self.shipping.frappe.get_list.assert_called_once_with(
			"Shipment", filters={"name": ["in", ["SF-CANCELLED", "SF-PENDING", "SF-NORMAL"]]},
			fields=["name", "sf_intercept_status", "sf_carrier_cancelled"], limit_page_length=0,
		)
		self.assertEqual(set(result), {"SF-CANCELLED", "SF-PENDING"})
		self.assertEqual(result["SF-CANCELLED"]["status_label"], "顺丰已取消，待取消本地运单")
		self.assertEqual(result["SF-PENDING"]["status_label"], interception.CARRIER_PENDING_STATE)
		for result_row in result.values():
			self.assertEqual(set(result_row), {"status_label", "status_color", "extra_details"})
		self.assertEqual(rows[0]["docstatus"], 1, "Carrier cancellation must not mutate the native document state")
		self.assertEqual(rows[2]["tracking_status"], "Delivered")

	def test_summary_enricher_handles_interception_request_legacy_and_confirmed_states(self):
		interception = self.load_interception_for_summary()
		states = {
			"REQUEST": (interception.REQUEST_STATE, interception.REQUEST_STATE, "orange"),
			"LEGACY": ("拦截失败", interception.SF_SUPPORT_FAILED, "red"),
			"CONFIRMED": (interception.CARRIER_STATE, "顺丰已取消，待取消本地运单", "orange"),
			"SUCCESS": (interception.SF_SUPPORT_SUCCESS, interception.SF_SUPPORT_SUCCESS, "orange"),
		}
		self.shipping.frappe.get_list = Mock(return_value=[
			{"name": name, "sf_intercept_status": state[0]} for name, state in states.items()
		])
		with patch.dict(sys.modules, {interception.__name__: interception}):
			result = self.shipping.enrich_shipment_summary(shipments=[
				{"name": name, "service_provider": "SF", "docstatus": 1} for name in states
			])
		for name, (_, label, color) in states.items():
			self.assertEqual(result[name]["status_label"], label)
			self.assertEqual(result[name]["status_color"], color)
		self.shipping.frappe.get_doc.assert_not_called()

	def test_summary_enricher_does_not_query_without_applicable_rows_or_fields(self):
		self.shipping.frappe.get_list = Mock()
		for rows in ([], [{"name": "OTHER", "carrier": "UPS"}],
			[{"name": "CANCELLED", "carrier": "SF", "docstatus": 2}]):
			self.assertEqual(self.shipping.enrich_shipment_summary(shipments=rows), {})
		self.shipping._has_field.return_value = False
		self.assertEqual(self.shipping.enrich_shipment_summary(shipments=[{"name": "SF", "carrier": "SF"}]), {})
		self.shipping.frappe.get_list.assert_not_called()

	def test_summary_enricher_permission_failure_never_exposes_provider_details(self):
		self.shipping.frappe.get_list = Mock(side_effect=PermissionError("Shipment"))
		self.assertEqual(self.shipping.enrich_shipment_summary(shipments=[{"name": "SF", "carrier": "SF"}]), {})

	def test_cancel_waybill_uses_full_shipped_state_guard(self):
		self.doc.status = "运输中"
		self.doc.tracking_status = ""
		client = types.ModuleType("erpnext_shipping.sf_international.client")
		client.query_order_cancellation = Mock()
		interception = types.ModuleType("erpnext_shipping.sf_international.interception")
		interception.CARRIER_PENDING_STATE = "顺丰取消待确认"
		interception.may_cancel = Mock(return_value=False)
		interception.requires_manual_success = Mock(return_value=True)
		interception.store_cancellation_pending = Mock()
		interception.store_confirmation = Mock()
		self.shipping.__spec__.name = "erpnext_shipping.sf_international.shipping"
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		with patch.dict(sys.modules, {
			"erpnext_shipping.sf_international.client": client,
			"erpnext_shipping.sf_international.interception": interception,
		}):
			with self.assertRaisesRegex(ValidationError, "不能直接取消面单"):
				self.shipping.cancel_sf_shipment(self.doc.name)
		self.shipping.cancel_order.assert_not_called()
		client.query_order_cancellation.assert_not_called()

	def test_cancel_waybill_requires_cancel_permission_before_carrier_call(self):
		self.doc.denied = {"cancel"}
		with self.assertRaises(PermissionError):
			self.shipping.cancel_sf_shipment(self.doc.name)
		self.shipping.cancel_order.assert_not_called()

	def test_cancel_request_is_not_submitted_twice_while_carrier_status_is_pending(self):
		self.doc.status = "待打单发货"
		self.shipping.cancel_order.reset_mock()
		client = types.ModuleType("erpnext_shipping.sf_international.client")
		client.query_order_cancellation = Mock(return_value={"confirmed": False, "rows": []})
		interception = types.ModuleType("erpnext_shipping.sf_international.interception")
		interception.CARRIER_PENDING_STATE = "顺丰取消待确认"
		interception.may_cancel = Mock(return_value=False)
		interception.requires_manual_success = Mock(return_value=False)
		interception.store_cancellation_pending = Mock(side_effect=lambda doc: doc.db_set("sf_intercept_status", interception.CARRIER_PENDING_STATE))
		interception.store_confirmation = Mock(return_value=False)
		self.shipping.__spec__.name = "erpnext_shipping.sf_international.shipping"
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		with patch.dict(sys.modules, {
			"erpnext_shipping.sf_international.client": client,
			"erpnext_shipping.sf_international.interception": interception,
		}):
			first = self.shipping.cancel_sf_shipment(self.doc.name)
			second = self.shipping.cancel_sf_shipment(self.doc.name)
		self.assertFalse(first["ok"])
		self.assertFalse(second["ok"])
		self.shipping.cancel_order.assert_called_once_with(["SF123"])
		self.assertEqual(client.query_order_cancellation.call_count, 2)

	def test_cancel_request_persists_pending_before_carrier_failure_and_blocks_retry(self):
		"""A carrier timeout must leave a durable pending state before retrying."""
		self.doc.status = "待打单发货"
		events = []
		client = types.ModuleType("erpnext_shipping.sf_international.client")
		client.query_order_cancellation = Mock(return_value={"confirmed": False, "rows": []})
		interception = types.ModuleType("erpnext_shipping.sf_international.interception")
		interception.CARRIER_PENDING_STATE = "顺丰取消待确认"
		interception.may_cancel = Mock(return_value=False)
		interception.requires_manual_success = Mock(return_value=False)
		interception.store_confirmation = Mock(return_value=False)

		def persist_pending(doc):
			doc.db_set("sf_intercept_status", interception.CARRIER_PENDING_STATE)
			events.append(("persist", doc.get("sf_intercept_status")))

		interception.store_cancellation_pending = Mock(side_effect=persist_pending)

		def fail_carrier_call(waybills):
			events.append(("carrier", self.doc.get("sf_intercept_status"), waybills))
			self.assertEqual(self.doc.get("sf_intercept_status"), interception.CARRIER_PENDING_STATE)
			raise RuntimeError("carrier timeout")

		self.shipping.cancel_order.side_effect = fail_carrier_call
		self.shipping.__spec__.name = "erpnext_shipping.sf_international.shipping"
		self.shipping.__package__ = "erpnext_shipping.sf_international"
		with patch.dict(sys.modules, {
			"erpnext_shipping.sf_international.client": client,
			"erpnext_shipping.sf_international.interception": interception,
		}):
			# Implementations may surface the carrier error or convert it to a
			# pending response; either way the durable state is the contract.
			try:
				self.shipping.cancel_sf_shipment(self.doc.name)
			except RuntimeError:
				pass
			self.assertEqual(self.doc.sf_intercept_status, interception.CARRIER_PENDING_STATE)
			self.assertEqual(events[0], ("persist", interception.CARRIER_PENDING_STATE))
			self.assertEqual(events[1][0], "carrier")

			# A retry only rechecks the carrier and must never submit a second
			# cancellation request after the first attempt was persisted.
			second = self.shipping.cancel_sf_shipment(self.doc.name)
		self.assertFalse(second["ok"])
		self.shipping.cancel_order.assert_called_once_with(["SF123"])
		self.assertEqual(client.query_order_cancellation.call_count, 1)

	def test_parcel_write_permission_is_checked_before_save(self):
		self.doc.denied = {"write"}
		with self.assertRaises(PermissionError):
			self.shipping.save_shipment_parcels("SHIP-1", [])
		self.shipping.frappe.get_doc.assert_called_with("Shipment", "SHIP-1", for_update=True)

	def test_foreign_currency_cannot_be_booked_as_company_currency(self):
		self.configure_freight()
		with self.assertRaisesRegex(ValidationError, "company currency"):
			self.shipping._book_sf_freight_journal(self.doc, 100, "USD")
		self.shipping.frappe.new_doc.assert_not_called()

	def test_cross_company_and_foreign_currency_accounts_are_rejected(self):
		for configuration in ({"account_company": "Other"}, {"account_currency": "USD"}):
			with self.subTest(configuration=configuration):
				self.configure_freight(**configuration)
				with self.assertRaises(ValidationError):
					self.shipping._book_sf_freight_journal(self.doc, 100, "CNY")
		self.shipping.frappe.new_doc.assert_not_called()

	def test_new_journal_obeys_create_and_submit_permissions(self):
		self.configure_freight()
		for permission in ("create", "submit"):
			with self.subTest(permission=permission):
				self.journal.denied = {permission}
				with self.assertRaises(PermissionError):
					self.shipping._book_sf_freight_journal(self.doc, 100, "CNY")
				self.assertFalse(self.journal.inserted)

	def test_duplicate_freight_uses_fresh_locked_journal_link(self):
		self.configure_freight()
		stale = make_doc(sf_freight_journal=None)
		self.doc.sf_freight_journal = "EXISTING-JE"
		existing = make_doc(
			name="EXISTING-JE",
			accounts=[Doc(debit_in_account_currency=100)],
			sf_freight_shipment="SHIP-1",
			sf_freight_waybill="SF123",
		)
		self.shipping.frappe.get_doc.side_effect = [self.doc, existing]
		self.assertEqual(self.shipping._book_sf_freight_journal(stale, 100), "EXISTING-JE")
		self.shipping.frappe.get_doc.assert_any_call("Shipment", "SHIP-1", for_update=True)
		self.shipping.frappe.get_doc.assert_any_call("Journal Entry", "EXISTING-JE", for_update=True)
		self.shipping.frappe.new_doc.assert_not_called()

	def test_changed_freight_keeps_posted_journal_for_review(self):
		self.configure_freight()
		self.doc.sf_freight_journal = "EXISTING-JE"
		existing = make_doc(
			name="EXISTING-JE",
			accounts=[Doc(debit_in_account_currency=90)],
			denied={"cancel"},
			sf_freight_shipment="SHIP-1",
			sf_freight_waybill="SF123",
		)
		self.shipping.frappe.get_doc.side_effect = [self.doc, existing]
		self.assertEqual(self.shipping._book_sf_freight_journal(self.doc, 100), "EXISTING-JE")
		self.assertTrue(getattr(self.doc, "_sf_freight_review_required", False))
		self.assertEqual(existing.docstatus, 1)

	def test_valid_freight_is_booked_normally(self):
		self.configure_freight()
		self.assertEqual(self.shipping._book_sf_freight_journal(self.doc, 100), "JE-1")
		self.assertTrue(self.journal.inserted)
		self.assertEqual(self.journal.docstatus, 1)
		self.assertEqual(self.doc.sf_freight_journal, "JE-1")
		self.assertFalse(getattr(self.journal.flags, "ignore_permissions", False))

	def test_cancelled_freight_can_query_but_draft_cannot(self):
		self.shipping.query_case_orders.reset_mock()
		self.shipping.query_case_orders.return_value = {"data": {"items": []}}
		for status, docstatus in (("Cancelled", 2), (self.shipping.STATUS_CANCELLED_SHIP, 1)):
			with self.subTest(status=status):
				self.doc.status, self.doc.docstatus = status, docstatus
				self.shipping.fetch_sf_freight(self.doc.name)
		self.assertEqual(self.shipping.query_case_orders.call_count, 2)
		self.doc.status, self.doc.docstatus = "Draft", 0
		with self.assertRaises(ValidationError):
			self.shipping.fetch_sf_freight(self.doc.name)

	def test_tracking_rejects_different_waybill(self):
		with self.assertRaisesRegex(ValidationError, "does not match"):
			self.shipping.track_sf_shipment("SHIP-1", "SF999", None)
		self.shipping.query_route.assert_not_called()

	def test_tracking_without_route_events_remains_booked(self):
		self.doc.sf_iuop_order_id = 100
		self.shipping.query_route.return_value = {"data": []}
		tracking = self.shipping.track_sf_shipment("SHIP-1", "SF123", None)
		self.assertEqual(tracking["tracking_status"], "Booked")
		self.assertFalse(tracking["tracking_status_info"])
		self.assertEqual(tracking["tracking_events"], [])

	def test_tracking_with_route_event_is_in_progress(self):
		self.doc.sf_iuop_order_id = 100
		self.shipping.query_route.return_value = {
			"data": [{"acceptTime": "2026-09-09 10:00:00", "remark": "已到达分拨中心"}],
		}
		tracking = self.shipping.track_sf_shipment("SHIP-1", "SF123", None)
		self.assertEqual(tracking["tracking_status"], "In Progress")
		self.assertEqual(tracking["tracking_status_info"], "已到达分拨中心")

	def test_tracking_iuop_chinese_routes_selects_latest_event(self):
		self.doc.sf_iuop_order_id = 100
		self.shipping.query_route.return_value = {"code": 200, "data": {
			"ordCNList": [
				{"routeTime": "2026-09-15 20:56:02", "routeDesc": "包裹已到达上海市中转中心"},
				{"routeTime": "2026-09-15 22:14:40", "routeDesc": "包裹已离开上海市中转中心,正发往下一站"},
				{"routeTime": "2026-09-13 17:17:06", "routeDesc": "顺丰速运 已收取包裹"},
			], "ordENList": None, "sfWaybillNo": "SF123", "routeTime": None, "routeDesc": None,
		}}
		tracking = self.shipping.track_sf_shipment("SHIP-1", "SF123", None)
		self.assertEqual(tracking["route_count"], 3)
		self.assertEqual(tracking["tracking_status"], "In Progress")
		self.assertEqual(tracking["tracking_status_info"], "包裹已离开上海市中转中心,正发往下一站")
		self.assertEqual(tracking["tracking_events"], [
			{"time": "2026-09-15 22:14:40", "description": "包裹已离开上海市中转中心,正发往下一站"},
			{"time": "2026-09-15 20:56:02", "description": "包裹已到达上海市中转中心"},
			{"time": "2026-09-13 17:17:06", "description": "顺丰速运 已收取包裹"},
		])
		self.assertTrue(self.shipping._tracking_implies_shipped(tracking))

	def test_route_history_preserves_all_events_and_full_descriptions_in_chinese(self):
		rows = [
			{"routeTime": f"2026-09-{day:02d} 12:00:00", "routeDesc": "物流说明" * 50 + str(day)}
			for day in range(1, 21)
		]
		tracking = self.shipping._tracking_from_route({"data": {
			"ordCNList": rows,
			"ordENList": [{"routeTime": "2026-09-21 12:00:00", "routeDesc": "English duplicate"}],
		}}, "SF123")
		self.assertEqual(tracking["route_count"], 20)
		self.assertEqual(tracking["tracking_events"], [
			{"time": row["routeTime"], "description": row["routeDesc"]} for row in reversed(rows)
		])
		self.assertEqual(len(tracking["tracking_status_info"]), 140)

	def test_tracking_english_routes_when_chinese_list_is_empty(self):
		tracking = self.shipping._tracking_from_route({"data": {
			"ordCNList": [], "ordENList": [{"routeTime": "2026-09-15 22:14:40", "routeDesc": "Arrived at sorting center"}],
		}}, "SF123")
		self.assertEqual(tracking["route_count"], 1)
		self.assertEqual(tracking["tracking_status_info"], "Arrived at sorting center")
		self.assertEqual(tracking["tracking_events"], [
			{"time": "2026-09-15 22:14:40", "description": "Arrived at sorting center"},
		])

	def test_tracking_empty_alias_does_not_hide_legacy_routes(self):
		tracking = self.shipping._tracking_from_route({"data": {
			"routes": [], "routeList": [{"acceptTime": "2026-09-15 12:00:00", "remark": "已签收"}],
		}}, "SF123")
		self.assertEqual(tracking["tracking_status"], "Delivered")

	def test_tracking_unrecognized_structure_is_not_reported_as_empty(self):
		with self.assertRaisesRegex(ValidationError, "无法识别顺丰物流返回结构"):
			self.shipping._tracking_from_route({"data": {"unexpectedRoutes": [{"text": "in transit"}]}}, "SF123")

	def test_tracking_empty_result_does_not_erase_saved_status(self):
		self.doc.delivery_contact_name = None
		self.doc.tracking_status = "Delivered"
		self.doc.tracking_status_info = "已签收"
		self.shipping.track_sf_shipment = Mock(return_value={
			"awb_number": "SF123", "route_count": 0,
			"tracking_status": "Booked", "tracking_status_info": "", "tracking_url": "",
		})
		result = self.shipping.update_tracking("SHIP-1", "SF International", "SF123")
		self.assertEqual(result["route_count"], 0)
		self.assertEqual(self.doc.tracking_status, "Delivered")
		self.assertEqual(self.doc.tracking_status_info, "已签收")
		self.assertFalse(self.doc.updates)

	def test_sales_order_freight_map_includes_replaced_waybill_history(self):
		"""Old billed labels remain visible after the parent points to a replacement."""
		class Row(Doc):
			def __contains__(self, key):
				return hasattr(self, key)

			def __setitem__(self, key, value):
				setattr(self, key, value)

		dn_item = Row(parent="DN-1", against_sales_order="SO-1")
		dn = Row(name="DN-1")
		link = Row(delivery_note="DN-1", parent="SHIP-1")
		parent = Row(
			name="SHIP-1", shipment_id="NEW-WB", awb_number="NEW-WB", status="已发货", docstatus=1,
			service_provider="SF International", carrier="SF International", shipment_amount=0,
			sf_freight_status="待核实", sf_freight_currency="CNY",
			sf_freight_accounting_status="记账待处理", sf_freight_accounting_hold=1,
		)
		old = Row(
			name="WB-OLD", shipment="SHIP-1", waybill="OLD-WB", replacement_status="已替换",
			freight_amount=233.84, freight_currency="CNY", freight_status="账单已取得",
			freight_accounting_status="已记账", freight_accounting_hold=0, creation_uncertain=0,
		)
		current = Row(
			name="WB-NEW", shipment="SHIP-1", waybill="NEW-WB", replacement_status="当前",
			freight_amount=None, freight_currency="", freight_status="待核实",
			freight_accounting_status="记账待处理", freight_accounting_hold=1, creation_uncertain=0,
		)

		def get_all(doctype, filters=None, **kwargs):
			return {
				"Delivery Note Item": [dn_item],
				"Shipment Delivery Note": [link],
				"SF Waybill": [old, current],
			}.get(doctype, [])

		def get_list(doctype, filters=None, **kwargs):
			return {"Delivery Note": [dn], "Shipment": [parent]}.get(doctype, [])

		self.shipping.frappe.get_all = Mock(side_effect=get_all)
		self.shipping.frappe.get_list = Mock(side_effect=get_list)
		result = self.shipping.get_sales_order_freight_map(["SO-1"])["SO-1"]

		self.assertEqual(result["total"], 2)
		self.assertEqual(result["confirmed"], 1)
		self.assertEqual(result["billed"], 1)
		self.assertEqual(result["billed_amount"], 233.84)
		self.assertEqual(result["settled"], 1)
		self.assertEqual(result["amount"], 233.84)
		self.assertEqual(result["held"], 1)
		self.assertEqual(result["currency"], "CNY")

	def test_unlinked_delivery_note_is_not_updated(self):
		with self.assertRaisesRegex(ValidationError, "not linked"):
			self.shipping._checked_delivery_notes(self.doc, ["DN-OTHER"], permission="write")

	def configure_booking_options(self, saved=None, *, booked=True):
		self.doc.sf_form_json = saved or {}
		self.doc.shipment_id = "SF123" if booked else ""
		self.doc.awb_number = self.doc.shipment_id
		self.doc.pickup_contact_name = None
		self.doc.pickup_address_name = "Warehouse Address"
		self.doc.delivery_address_name = "Customer Address"
		self.doc.delivery_contact_name = "Customer Contact"
		self.doc.delivery_to = "Customer"
		self.doc.delivery_contact = "Customer Contact"
		self.doc.total_weight = 2.5
		self.shipping._settings_or_none = Mock(return_value=Doc(
			declared_currency="USD", default_hs_code="950420", default_declared_value=25,
			default_cname="台球用品", default_ename="Billiard goods",
		))
		self.shipping._product_choices = Mock(return_value=[
			{"product_code": "10", "product_name": "国际小包", "is_preferred": 1},
			{"product_code": "20", "product_name": "国际标快", "is_preferred": 0},
		])
		self.shipping._has_submitted_delivery_note = Mock(return_value=True)
		self.shipping._goods_from_delivery_notes = Mock(return_value=[])
		self.shipping._sender_from_warehouse = Mock(return_value={
			"contact": "Warehouse", "phone": "12345678", "address": "Warehouse Road",
		})
		self.shipping._party_from_address = Mock(return_value={
			"contact": "Customer", "phone": "87654321", "address": "Customer Road", "country": "US",
		})
		self.shipping._saved_sf_receiver = Mock(return_value={})
		self.shipping._last_sf_receiver_for_address = Mock(return_value={"province": "CA", "city": "Eureka"})
		self.shipping._remember_sf_receiver = Mock()

	def assert_booking_options_are_read_only(self):
		self.shipping._remember_sf_receiver.assert_not_called()
		self.shipping.frappe.db.set_value.assert_not_called()
		self.assertEqual(self.doc.updates, [])
		for name in ("create_order", "query_order", "region_cascade", "query_postcode", "start_print"):
			getattr(self.shipping, name).assert_not_called()

	def test_partial_booked_form_is_filled_from_shipment_and_settings(self):
		self.configure_booking_options({"entry_mode": "existing", "existing_waybill": "SF123"})
		form = self.shipping.get_booking_options(self.doc.name)["form"]
		self.assertEqual(form["entry_mode"], "existing")
		self.assertEqual(form["existing_waybill"], "SF123")
		self.assertEqual(form["total_weight"], 2.5)
		self.assertEqual(form["parcel_quantity"], 1)
		self.assertEqual(form["declared_value"], 25)
		self.assertEqual(form["declared_currency"], "USD")
		self.assertEqual(form["hs_code"], "950420")
		self.assertEqual(form["receiver"]["city"], "Eureka")
		self.assertEqual(form["receiver"]["address"], "Customer Road")
		self.assertEqual(form["sender"]["contact"], "Warehouse")
		self.assert_booking_options_are_read_only()

	def test_empty_saved_parties_do_not_erase_defaults(self):
		for value in ({}, None, {"contact": " ", "address": "", "phone": None}):
			with self.subTest(value=value):
				self.configure_booking_options({"sender": value, "receiver": value})
				form = self.shipping.get_booking_options(self.doc.name)["form"]
				self.assertEqual(form["sender"]["contact"], "Warehouse")
				self.assertEqual(form["receiver"]["contact"], "Customer")
				self.assertEqual(form["receiver"]["phone"], "87654321")
				self.assert_booking_options_are_read_only()

	def test_booked_form_keeps_saved_values_but_sender_stays_warehouse(self):
		saved = {
			"product_code": "20", "product_name": "国际标快", "total_weight": 5,
			"parcel_quantity": 2, "declared_value": 0, "declared_currency": "EUR",
			"purchase_currency": "EUR", "hs_code": "CUSTOM-HS", "ename": "Custom goods",
			"cname": "自定义货物", "custom_flag": False,
			"sender": {"contact": "Old Sender"},
			"receiver": {"contact": "Saved Customer", "phone": "55555555", "city": "Saved City"},
		}
		self.configure_booking_options(saved)
		form = self.shipping.get_booking_options(self.doc.name)["form"]
		for field, value in saved.items():
			if field not in {"sender", "receiver"}:
				self.assertEqual(form[field], value)
		self.assertEqual(form["receiver"]["contact"], "Saved Customer")
		self.assertEqual(form["receiver"]["city"], "Saved City")
		self.assertEqual(form["receiver"]["phone"], "55555555")
		self.assertEqual(form["receiver"]["address"], "Customer Road")
		self.assertEqual(form["sender"]["contact"], "Warehouse")
		self.assertNotIn("address", saved["receiver"])
		self.assertEqual(saved["sender"]["contact"], "Old Sender")
		self.assert_booking_options_are_read_only()

	def test_saved_product_name_and_code_remain_a_consistent_pair(self):
		cases = [
			({"product_name": "国际标快"}, ("20", "国际标快")),
			({"product_code": "20"}, ("20", "国际标快")),
			({"product_code": "20", "product_name": "国际小包"}, ("20", "国际标快")),
			({"product_name": "Legacy product"}, ("", "Legacy product")),
			({"product_code": "LEGACY"}, ("LEGACY", "")),
		]
		for saved, expected in cases:
			with self.subTest(saved=saved):
				self.configure_booking_options(saved)
				form = self.shipping.get_booking_options(self.doc.name)["form"]
				self.assertEqual((form["product_code"], form["product_name"]), expected)

	def test_unbooked_form_keeps_current_customs_preset_rule(self):
		for saved in ({}, {"declared_value": 100, "declared_currency": "EUR"}):
			with self.subTest(saved=saved):
				self.configure_booking_options(saved, booked=False)
				result = self.shipping.get_booking_options(self.doc.name)
				self.assertEqual(result["form"]["declared_value"], 25)
				self.assertEqual(result["form"]["declared_currency"], "USD")
				self.assertEqual(result["form"]["receiver"]["city"], "Eureka")
				self.assertTrue(result["receiver_remembered"])
				self.assert_booking_options_are_read_only()

	def test_read_endpoints_check_document_permission(self):
		self.shipping.frappe.has_permission.side_effect = PermissionError("read")
		for function, args in (
			(self.shipping.get_booking_options, ("SHIP-1",)),
			(self.shipping.get_sales_order_freight_map, (["SO-1"],)),
		):
			with self.subTest(function=function.__name__), self.assertRaises(PermissionError):
				function(*args)

if __name__ == "__main__":
	unittest.main()
