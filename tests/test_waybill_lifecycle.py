"""Offline tests for the multi-waybill replacement lifecycle.

The tests deliberately stub Frappe and the SF client.  They exercise the
state transitions around a local Shipment without creating a carrier order or
touching a site database.
"""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class ValidationError(Exception):
	pass


class FakeDoc:
	"""Small document double that supports the methods used by waybill.py."""

	def __init__(self, **values):
		self.__dict__.update(values)
		self.permissions = self.__dict__.get("permissions") or []
		self.denied = self.__dict__.get("denied") or set()
		self.updates = self.__dict__.get("updates") or []
		self.inserted = self.__dict__.get("inserted", False)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def __getattr__(self, key):
		# Frappe Documents return a false-y value for an unset field.  Keeping the
		# same behavior lets the test double cover newly inserted records whose
		# optional fields are not present in the input dict.
		if key.startswith("__"):
			raise AttributeError(key)
		return None

	def check_permission(self, permission):
		self.permissions.append(permission)
		if permission in self.denied:
			raise PermissionError(permission)

	def db_set(self, values, value=None, **_kwargs):
		if isinstance(values, dict):
			self.__dict__.update(values)
			self.updates.append(dict(values))
		else:
			self.__dict__[values] = value
			self.updates.append({values: value})

	def insert(self, **_kwargs):
		self.inserted = True
		return self


class WaybillStore:
	def __init__(self):
		self.shipment = None
		self.records = []
		self.by_name = {}
		self.next_number = 1

	def add(self, record):
		if not getattr(record, "name", None):
			record.name = f"WB-{self.next_number}"
			self.next_number += 1
		if record not in self.records:
			self.records.append(record)
		self.by_name[record.name] = record
		return record

def load_waybill():
	"""Load waybill.py under a synthetic package with deterministic doubles."""
	store = WaybillStore()
	frappe = types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValidationError(message))
	frappe.whitelist = lambda *args, **kwargs: lambda function: function
	frappe.get_meta = Mock(return_value=types.SimpleNamespace(has_field=lambda _field: True))
	frappe.utils = types.ModuleType("frappe.utils")
	frappe.utils.cint = lambda value: int(value or 0)
	frappe.utils.now_datetime = lambda: "2026-09-08 12:00:00"
	frappe.db = types.SimpleNamespace(get_value=lambda *args, **kwargs: None)

	client = types.ModuleType("erpnext_shipping.sf_international.client")
	client.create_order = Mock(return_value={"data": {"trackingNo": "NEW-WB", "orderId": 99}})
	client.query_case_orders = Mock(return_value={"data": {"items": []}})
	client.query_order_cancellation = Mock(return_value={"confirmed": False, "evidence": None, "rows": []})

	def cancellation_evidence(row, waybill):
		identifiers = {
			str(row.get(field)).strip()
			for field in ("trackingNo", "waybillNo", "sfWaybillNo")
			if row.get(field) not in (None, "")
		}
		status = " ".join(str(row.get("status") or "").lower().split()).rstrip(".!。！")
		return {"waybill": waybill} if identifiers == {waybill} and status == "cancelled" else None

	client.cancellation_evidence = cancellation_evidence

	shipping = types.ModuleType("erpnext_shipping.sf_international.shipping")
	shipping._is_sf_shipment = lambda doc: (doc.get("service_provider") == "SF International" or doc.get("carrier") == "SF International")
	shipping._create_order_body_from_form = Mock(return_value=({"pieceorderBaseInfo": {}}, "10", "国际小包"))
	shipping._sender_from_warehouse = Mock(return_value={})
	shipping._remember_successful_sf_receiver = Mock()
	shipping._freight_window = Mock(return_value=("2026-09-01", "2026-09-08"))

	def sum_pay_amount(payload):
		items = ((payload or {}).get("data") or {}).get("items") or []
		if not items:
			return None, "", []
		currency = str(items[0].get("currency") or "CNY").upper()
		amount = sum(float(item.get("payAmount") or 0) for item in items)
		return amount, currency, list(items)

	shipping._sum_pay_amount = Mock(side_effect=sum_pay_amount)

	mutation_module = types.ModuleType("erpnext_shipping.sf_international.doctype.sf_waybill.sf_waybill")
	mutation_module.mutation_context = lambda: None
	mutation_module.clear_mutation = lambda _token: None

	def get_doc(*args, **kwargs):
		if len(args) == 1 and isinstance(args[0], dict):
			values = dict(args[0])
			values.pop("doctype", None)
			doc = FakeDoc(**values)
			store.add(doc)
			return doc
		doctype, name = args[:2]
		if doctype == "Shipment":
			return store.shipment
		if doctype == "SF Waybill":
			try:
				return store.by_name[name]
			except KeyError:
				raise ValidationError(f"unknown waybill {name}")
		raise ValidationError(f"unexpected get_doc {args}")

	def get_all(doctype, filters=None, **_kwargs):
		if doctype == "SF Waybill":
			filters = filters or {}
			shipment_name = filters.get("shipment")
			waybill = filters.get("waybill")
			return [
				row for row in store.records
				if (shipment_name is None or row.get("shipment") == shipment_name)
				and (waybill is None or row.get("waybill") == waybill)
			]
		return []

	frappe.get_doc = get_doc
	frappe.get_all = get_all

	# Build only the package entries required for relative imports in waybill.py.
	packages = {
		"erpnext_shipping": types.ModuleType("erpnext_shipping"),
		"erpnext_shipping.sf_international": types.ModuleType("erpnext_shipping.sf_international"),
		"erpnext_shipping.sf_international.doctype": types.ModuleType("erpnext_shipping.sf_international.doctype"),
		"erpnext_shipping.sf_international.doctype.sf_waybill": types.ModuleType("erpnext_shipping.sf_international.doctype.sf_waybill"),
	}
	for package in packages.values():
		package.__path__ = [str(ROOT / package.__name__.replace(".", "/"))]

	modules = {
		"frappe": frappe,
		"frappe.utils": frappe.utils,
		"erpnext_shipping": packages["erpnext_shipping"],
		"erpnext_shipping.sf_international": packages["erpnext_shipping.sf_international"],
		"erpnext_shipping.sf_international.doctype": packages["erpnext_shipping.sf_international.doctype"],
		"erpnext_shipping.sf_international.doctype.sf_waybill": packages["erpnext_shipping.sf_international.doctype.sf_waybill"],
		"erpnext_shipping.sf_international.doctype.sf_waybill.sf_waybill": mutation_module,
		"erpnext_shipping.sf_international.shipping": shipping,
		"erpnext_shipping.sf_international.client": client,
	}
	path = ROOT / "erpnext_shipping/sf_international/waybill.py"
	spec = importlib.util.spec_from_file_location("erpnext_shipping.sf_international.waybill_under_test", path)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, modules):
		spec.loader.exec_module(module)
	# waybill.py resolves shipping/client lazily inside each endpoint.  Keep the
	# doubles registered for the lifetime of this fixture so those relative
	# imports cannot fall through to a real (and unavailable) Frappe install.
	sys.modules.update({
		"erpnext_shipping": packages["erpnext_shipping"],
		"erpnext_shipping.sf_international": packages["erpnext_shipping.sf_international"],
		"erpnext_shipping.sf_international.doctype": packages["erpnext_shipping.sf_international.doctype"],
		"erpnext_shipping.sf_international.doctype.sf_waybill": packages["erpnext_shipping.sf_international.doctype.sf_waybill"],
		"erpnext_shipping.sf_international.doctype.sf_waybill.sf_waybill": mutation_module,
		"erpnext_shipping.sf_international.shipping": shipping,
		"erpnext_shipping.sf_international.client": client,
	})
	return module, frappe, client, shipping, store


def shipment(**values):
	defaults = dict(
		name="SHIP-1",
		docstatus=1,
		status="待打单发货",
		service_provider="SF International",
		carrier="SF International",
		shipment_id="OLD-WB",
		awb_number="OLD-WB",
		tracking_status="",
		sf_form_json=json.dumps({"sender": {"country": "CN"}}),
		sf_carrier_cancelled=0,
		sf_active_waybill_record="WB-OLD",
		sf_waybill_replacement_status="当前",
		sf_waybill_pending_record="",
		permissions=[],
		denied=set(),
	)
	defaults.update(values)
	return FakeDoc(**defaults)


def waybill_record(**values):
	defaults = dict(
		name="WB-OLD",
		shipment="SHIP-1",
		waybill="OLD-WB",
		order_id="OLD-ORDER",
		label_url="",
		tracking_status="",
		tracking_status_info="",
		tracking_payload="",
		freight_amount=None,
		freight_currency="",
		freight_status="",
		freight_payload="",
		freight_journal="",
		freight_previous_journal="",
		freight_accounting_status="",
		freight_accounting_hold=0,
		replacement_status="当前",
		replaces_waybill="",
		carrier_cancelled=0,
		is_active=1,
		replacement_feedback="",
		replacement_reason="",
		replacement_feedback_at=None,
	)
	defaults.update(values)
	return FakeDoc(**defaults)


class WaybillLifecycleTests(unittest.TestCase):
	def setUp(self):
		self.mod, self.frappe, self.client, self.shipping, self.store = load_waybill()
		self.store.shipment = shipment()
		self.old = waybill_record()
		self.store.add(self.old)

	def create(self, *, shipped, response=None):
		if response is not None:
			self.client.create_order.return_value = response
		return self.mod.create_replacement(
			self.store.shipment.name,
			form_json=json.dumps({"sender": {"country": "CN"}}),
			reason="面单地址填写错误",
			shipped=shipped,
		)

	def confirm_old_carrier_cancel(self):
		"""Model exact carrier cancellation evidence for the old label."""
		payload = json.dumps({
			"waybill": "OLD-WB", "trackingNo": "OLD-WB", "status": "cancelled",
		})
		self.old.carrier_cancelled = 1
		self.old.carrier_cancelled_waybill = "OLD-WB"
		self.old.carrier_cancelled_at = "2026-09-08 11:00:00"
		self.old.carrier_cancel_payload = payload
		self.store.shipment.sf_carrier_cancelled = 1
		self.store.shipment.sf_carrier_cancelled_waybill = "OLD-WB"
		self.store.shipment.sf_carrier_cancelled_at = "2026-09-08 11:00:00"
		self.store.shipment.sf_carrier_cancel_payload = payload

	def use_real_label_endpoints(self):
		from test_shipping_regressions import load_shipping

		shipping = load_shipping()
		shipping.__package__ = "erpnext_shipping.sf_international"
		shipping.frappe = self.frappe
		shipping.query_order.return_value = {"data": {"orderId": 123}}
		shipping.start_print.return_value = {"data": 456}
		shipping.poll_label.return_value = ("https://carrier.example/label.pdf", "token")
		shipping.download_pdf.return_value = b"test pdf"
		shipping.save_file.return_value = FakeDoc(file_url="/private/files/SF-generated.pdf")
		for name in ("query_order", "start_print", "poll_label", "download_pdf", "query_route"):
			setattr(self.client, name, getattr(shipping, name))
		file_manager = types.ModuleType("frappe.utils.file_manager")
		file_manager.save_file = shipping.save_file
		patcher = patch.dict(sys.modules, {
			"erpnext_shipping.sf_international.shipping": shipping,
			"erpnext_shipping.sf_international.waybill": self.mod,
			"frappe.utils.file_manager": file_manager,
		})
		patcher.start()
		self.addCleanup(patcher.stop)
		return shipping

	def test_both_current_print_endpoints_share_one_carrier_request(self):
		shipping = self.use_real_label_endpoints()
		first = self.mod.print_replacement_label("SHIP-1", self.old.name)
		second = shipping.print_sf_label("SHIP-1")
		third = self.mod.print_replacement_label("SHIP-1", self.old.name)
		self.assertEqual((first, second, third), ("/private/files/SF-generated.pdf",) * 3)
		shipping.start_print.assert_called_once_with(123)
		shipping.save_file.assert_called_once()
		self.assertEqual(self.old.label_url, self.store.shipment.sf_label_url)

	def test_current_parent_print_reuses_history_pdf_and_repairs_parent_cache(self):
		shipping = self.use_real_label_endpoints()
		self.old.label_url = "/private/files/SF-existing.pdf"
		self.store.shipment.sf_label_url = "/private/files/SF-duplicate.pdf"
		self.assertEqual(shipping.print_sf_label("SHIP-1"), self.old.label_url)
		self.assertEqual(self.store.shipment.sf_label_url, self.old.label_url)
		shipping.start_print.assert_not_called()
		shipping.query_order.assert_not_called()

	def test_current_history_print_reuses_parent_pdf_and_repairs_history_cache(self):
		shipping = self.use_real_label_endpoints()
		self.store.shipment.sf_label_url = "/private/files/SF-existing.pdf"
		self.assertEqual(self.mod.print_replacement_label("SHIP-1", self.old.name), self.store.shipment.sf_label_url)
		self.assertEqual(self.old.label_url, self.store.shipment.sf_label_url)
		shipping.start_print.assert_not_called()

	def test_pending_label_generation_never_reuses_or_overwrites_current_pdf(self):
		shipping = self.use_real_label_endpoints()
		self.store.shipment.sf_label_url = self.old.label_url = "/private/files/SF-current.pdf"
		pending = self.store.add(waybill_record(
			name="WB-NEW", waybill="NEW-WB", order_id=999, replacement_status="待替换", is_active=0,
		))
		result = self.mod.print_replacement_label("SHIP-1", pending.name)
		shipping.start_print.assert_called_once_with(999)
		self.assertEqual(result, pending.label_url)
		self.assertEqual(self.store.shipment.sf_label_url, "/private/files/SF-current.pdf")
		self.assertEqual(self.old.label_url, "/private/files/SF-current.pdf")
		self.mod.print_replacement_label("SHIP-1", pending.name)
		shipping.start_print.assert_called_once()

	def test_current_print_checks_exact_identity_and_cancellation_before_cache_reuse(self):
		shipping = self.use_real_label_endpoints()
		self.old.label_url = "/private/files/SF-current.pdf"
		self.old.waybill = "OTHER-WB"
		with self.assertRaisesRegex(ValidationError, "不一致"):
			shipping.print_sf_label("SHIP-1")
		with self.assertRaisesRegex(ValidationError, "不一致"):
			self.mod.print_replacement_label("SHIP-1", self.old.name)
		self.old.waybill = "OLD-WB"
		self.old.carrier_cancelled = 1
		with self.assertRaisesRegex(ValidationError, "已取消"):
			shipping.print_sf_label("SHIP-1")
		shipping.start_print.assert_not_called()

	def test_print_permissions_are_checked_even_when_pdf_is_cached(self):
		shipping = self.use_real_label_endpoints()
		self.old.label_url = "/private/files/SF-current.pdf"
		self.store.shipment.denied = {"write"}
		with self.assertRaises(PermissionError):
			shipping.print_sf_label("SHIP-1")
		with self.assertRaises(PermissionError):
			self.mod.print_replacement_label("SHIP-1", self.old.name)
		shipping.start_print.assert_not_called()

	def test_tracking_history_is_saved_with_all_events_for_current_and_pending_labels(self):
		shipping = self.use_real_label_endpoints()
		payload = {"data": {"ordCNList": [
			{"routeTime": "2026-09-13 12:00:00", "routeDesc": "顺丰已揽收"},
			{"routeTime": "2026-09-15 12:00:00", "routeDesc": "运输中"},
		]}}
		tracking = shipping._tracking_from_route(payload, "OLD-WB")
		self.mod.sync_tracking(self.store.shipment, tracking)
		self.assertEqual(json.loads(self.old.tracking_payload)["tracking_events"], tracking["tracking_events"])
		pending = self.store.add(waybill_record(
			name="WB-NEW", waybill="NEW-WB", order_id=999, replacement_status="待替换", is_active=0,
		))
		shipping.query_route.return_value = payload
		result = self.mod.fetch_waybill_tracking("SHIP-1", pending.name)
		self.assertEqual(result["tracking_events"], tracking["tracking_events"])
		stored = json.loads(pending.tracking_payload)
		self.assertEqual(stored["data"], payload["data"])
		self.assertEqual(stored["tracking_events"], tracking["tracking_events"])
		self.assertFalse(self.store.shipment.tracking_status)

	def test_saved_tracking_history_is_readable_on_load_without_carrier_queries(self):
		shipping = self.use_real_label_endpoints()
		raw = {"data": {"ordCNList": [
			{"routeTime": "2026-09-13 12:00:00", "routeDesc": "旧单已揽收", "private": "hidden"},
			{"routeTime": "2026-09-15 12:00:00", "routeDesc": "旧单运输中"},
		]}}
		self.old.tracking_payload = json.dumps(raw)
		self.old.tracking_queried_at = "2026-09-16 12:00:00"
		pending = self.store.add(waybill_record(
			name="WB-NEW", waybill="NEW-WB", replacement_status="待替换", is_active=0,
			tracking_payload=json.dumps({"awb_number": "NEW-WB", "tracking_events": [
				{"time": "2026-09-16 12:00:00", "description": "新单已揽收", "private": "hidden"},
			]}),
		))
		onload = {}
		self.store.shipment.set_onload = lambda key, value: onload.update({key: value})
		self.mod.onload(self.store.shipment)
		rows = self.mod.list_waybill_records("SHIP-1")["waybills"]
		self.assertEqual(rows, onload["sf_waybills"])
		self.assertEqual(rows[0]["route_count"], 2)
		self.assertEqual(rows[0]["tracking_queried_at"], "2026-09-16 12:00:00")
		self.assertEqual(rows[0]["tracking_events"][0]["description"], "旧单运输中")
		self.assertEqual(rows[1]["tracking_events"], [{"time": "2026-09-16 12:00:00", "description": "新单已揽收"}])
		for row in rows:
			self.assertNotIn("tracking_payload", row)
			for event in row["tracking_events"]:
				self.assertEqual(set(event), {"time", "description"})
		shipping.query_route.assert_not_called()
		self.assertFalse(self.old.updates)
		self.assertFalse(pending.updates)

	def test_tracking_history_respects_parent_and_child_read_permissions(self):
		self.old.tracking_payload = json.dumps({"tracking_events": [{"time": "now", "description": "private"}]})
		self.store.shipment.denied = {"read"}
		with self.assertRaises(PermissionError):
			self.mod.list_waybill_records("SHIP-1")
		self.store.shipment.denied = set()
		self.frappe.has_permission = lambda *args, **kwargs: False
		self.assertEqual(self.mod.list_waybill_records("SHIP-1")["waybills"], [])
		onload = {}
		self.store.shipment.set_onload = lambda key, value: onload.update({key: value})
		self.mod.onload(self.store.shipment)
		self.assertEqual(onload["sf_waybills"], [])

	def test_current_tracking_history_merges_partial_results_and_retains_empty_results(self):
		shipping = self.use_real_label_endpoints()
		first = {"routeTime": "2026-09-13 12:00:00", "routeDesc": "已揽收"}
		second = {"routeTime": "2026-09-14 12:00:00", "routeDesc": "运输中"}
		third = {"routeTime": "2026-09-15 12:00:00", "routeDesc": "已到达分拨中心"}
		for routes in ([first, second], [second, third], []):
			self.mod.sync_tracking(self.store.shipment, shipping._tracking_from_route({"data": {"ordCNList": routes}}, "OLD-WB"))
		stored = self.mod._public_record(self.old)
		self.assertEqual(stored["route_count"], 3)
		self.assertEqual([event["description"] for event in stored["tracking_events"]], ["已到达分拨中心", "运输中", "已揽收"])
		self.assertEqual(self.old.tracking_status_info, "已到达分拨中心")

	def test_historical_tracking_history_retains_raw_snapshot_nodes_on_empty_and_partial_queries(self):
		shipping = self.use_real_label_endpoints()
		self.old.is_active = 0
		self.old.replacement_status = "已替换"
		self.old.tracking_status = "In Progress"
		self.old.tracking_status_info = "运输中"
		first = {"routeTime": "2026-09-13 12:00:00", "routeDesc": "已揽收"}
		second = {"routeTime": "2026-09-14 12:00:00", "routeDesc": "运输中"}
		third = {"routeTime": "2026-09-15 12:00:00", "routeDesc": "已签收"}
		self.old.tracking_payload = json.dumps({"data": {"ordCNList": [first, second]}})
		shipping.query_route.side_effect = [{"data": {"ordCNList": []}}, {"data": {"ordCNList": [second, third]}}]
		empty = self.mod.fetch_waybill_tracking("SHIP-1", self.old.name)
		self.assertEqual(empty["route_count"], 0)
		self.assertEqual(self.mod._public_record(self.old)["route_count"], 2)
		self.assertEqual(self.old.tracking_status_info, "运输中")
		self.mod.fetch_waybill_tracking("SHIP-1", self.old.name)
		self.assertEqual(self.mod._public_record(self.old)["route_count"], 3)
		self.assertFalse(self.store.shipment.updates)

	def test_saved_history_ignores_foreign_or_malformed_payloads(self):
		self.use_real_label_endpoints()
		for payload in ("broken", "[]", json.dumps({"awb_number": "OTHER-WB", "tracking_events": [
			{"time": "2026-09-13", "description": "其他单号的记录"},
		]})):
			with self.subTest(payload=payload):
				self.old.tracking_payload = payload
				self.assertEqual(self.mod._public_record(self.old)["tracking_events"], [])
		self.mod.sync_tracking(self.store.shipment, {
			"awb_number": "OLD-WB", "route_count": 1,
			"tracking_events": [{"time": "2026-09-16", "description": "本单号的记录"}],
		})
		self.assertEqual(self.mod._public_record(self.old)["tracking_events"], [{"time": "2026-09-16", "description": "本单号的记录"}])

	def test_unshipped_replacement_requires_explicit_carrier_cancel(self):
		with self.assertRaisesRegex(ValidationError, "明确取消确认"):
			self.create(shipped=0)
		self.client.create_order.assert_not_called()
		self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")
		self.assertEqual(len(self.store.records), 1)

	def test_initial_booking_attempt_is_committed_before_carrier_request(self):
		self.store.shipment = shipment(
			shipment_id="",
			awb_number="",
			sf_active_waybill_record="",
			sf_waybill_replacement_status="",
		)
		self.store.records = []
		self.store.by_name = {}
		self.store.shipment.is_new = lambda: True
		self.assertIsNone(self.mod.begin_initial_booking_attempt(self.store.shipment, {"receiver": {}}))
		record = self.mod.begin_initial_booking_attempt(
			self.store.shipment,
			{"receiver": {"contact": "收件人"}},
			allow_new_parent=True,
		)
		self.assertEqual(record.replacement_status, "创建中")
		self.assertEqual(record.is_active, 0)
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, record.name)
		self.assertEqual(self.store.shipment.sf_waybill_replacement_status, "创建中")

	def test_initial_booking_attempt_retains_response_and_projects_pointer(self):
		self.store.shipment = shipment(
			shipment_id="",
			awb_number="",
			sf_active_waybill_record="",
			sf_waybill_replacement_status="",
		)
		self.store.records = []
		self.store.by_name = {}
		receiver = {"country": "US", "province": "CA", "city": "Eureka", "post_code": "95501", "address": "Main St"}
		record = self.mod.begin_initial_booking_attempt(self.store.shipment, {"receiver": receiver})
		self.shipping._remember_successful_sf_receiver.assert_not_called()
		self.mod.complete_initial_booking_attempt(
			self.store.shipment,
			record,
			{"data": {"trackingNo": "NEW-WB", "orderId": 99}},
			"NEW-WB",
			99,
			carrier_service="国际小包",
			form_payload={"receiver": receiver},
		)
		self.assertEqual(record.replacement_status, "当前")
		self.assertEqual(record.waybill, "NEW-WB")
		self.assertEqual(record.order_id, 99)
		self.assertEqual(self.store.shipment.sf_active_waybill_record, record.name)
		self.assertEqual(self.store.shipment.shipment_id, "NEW-WB")
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, "")
		self.shipping._remember_successful_sf_receiver.assert_called_once_with(self.store.shipment, receiver)

	def test_initial_booking_attempt_failure_keeps_external_identifiers(self):
		self.store.shipment = shipment(
			shipment_id="",
			awb_number="",
			sf_active_waybill_record="",
			sf_waybill_replacement_status="",
		)
		self.store.records = []
		self.store.by_name = {}
		record = self.mod.begin_initial_booking_attempt(self.store.shipment, {"receiver": {}})
		self.mod.fail_initial_booking_attempt(
			self.store.shipment,
			record,
			"本地保存失败",
			payload={"data": {"trackingNo": "NEW-WB", "orderId": 99}},
			waybill="NEW-WB",
			order_id=99,
		)
		self.assertEqual(record.replacement_status, "失败")
		self.assertTrue(record.creation_uncertain)
		self.assertEqual(record.waybill, "NEW-WB")
		self.assertEqual(record.order_id, 99)
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, record.name)
		self.shipping._remember_successful_sf_receiver.assert_not_called()

	def test_unshipped_replacement_keeps_old_record_and_promotes_new_one(self):
		self.old.carrier_cancelled = 1
		self.old.carrier_cancelled_waybill = "OLD-WB"
		self.old.carrier_cancelled_at = "2026-09-08 11:00:00"
		self.old.carrier_cancel_payload = json.dumps({"waybill": "OLD-WB", "trackingNo": "OLD-WB", "status": "cancelled"})
		self.store.shipment.sf_carrier_cancelled = 1
		self.store.shipment.sf_carrier_cancelled_waybill = "OLD-WB"
		self.store.shipment.sf_carrier_cancelled_at = "2026-09-08 11:00:00"
		self.store.shipment.sf_carrier_cancel_payload = json.dumps({"waybill": "OLD-WB", "trackingNo": "OLD-WB", "status": "cancelled"})
		result = self.create(shipped=0, response={"data": {"trackingNo": "NEW-WB", "orderId": 99}})

		self.assertEqual(result["status"], "当前")
		self.assertEqual(self.old.replacement_status, "已取消")
		self.assertEqual(self.old.is_active, 0)
		new = self.store.by_name[result["record"]]
		self.assertEqual(new.waybill, "NEW-WB")
		self.assertEqual(new.replaces_waybill, "OLD-WB")
		self.assertEqual(new.replacement_status, "当前")
		self.assertEqual(new.is_active, 1)
		self.assertEqual(self.store.shipment.shipment_id, "NEW-WB")
		self.assertEqual(self.store.shipment.awb_number, "NEW-WB")
		self.assertEqual(self.store.shipment.sf_active_waybill_record, new.name)
		self.assertEqual(self.client.create_order.call_args.args[0]["pieceorderBaseInfo"]["userOrderid"], new.name)
		self.shipping._remember_successful_sf_receiver.assert_called_once()
		self.assertEqual(self.shipping._remember_successful_sf_receiver.call_args.args[0].shipment_id, "NEW-WB")

	def test_unshipped_replacement_uses_exact_parent_cancellation_evidence(self):
		"""The interception workflow stores carrier confirmation on Shipment.

		A migrated SF Waybill row may still have its old child fields blank, so the
		replacement endpoint must recognize the exact parent evidence as well.
		"""
		self.store.shipment.sf_carrier_cancelled = 1
		self.store.shipment.sf_carrier_cancelled_waybill = "OLD-WB"
		self.store.shipment.sf_carrier_cancelled_at = "2026-09-08 11:00:00"
		self.store.shipment.sf_carrier_cancel_payload = json.dumps(
			{"waybill": "OLD-WB", "trackingNo": "OLD-WB", "status": "cancelled"}, ensure_ascii=False,
		)
		# The active child was created before the interception confirmation.
		self.old.carrier_cancelled = 0
		result = self.create(shipped=0, response={"data": {"trackingNo": "NEW-WB", "orderId": 99}})
		self.assertEqual(result["status"], "当前")
		self.assertEqual(self.store.shipment.shipment_id, "NEW-WB")

	def test_replacement_sender_is_kept_from_original_label(self):
		self.store.shipment.status = "已发货"
		self.old.form_payload = json.dumps({
			"sender": {"company": "原仓库", "contact": "原寄件人", "address": "原地址"},
		})
		malicious = json.dumps({
			"sender": {"company": "其他仓库", "contact": "冒用寄件人", "address": "其他地址"},
			"receiver": {"contact": "收件人"},
		})
		self.mod.create_replacement(
			self.store.shipment.name,
			form_json=malicious,
			reason="修正收件信息",
			shipped=1,
		)
		submitted_form = self.shipping._create_order_body_from_form.call_args.args[1]
		self.assertEqual(submitted_form["sender"]["company"], "原仓库")
		self.assertEqual(submitted_form["sender"]["address"], "原地址")

	def test_partial_original_sender_is_filled_from_trusted_defaults(self):
		self.store.shipment.status = "已发货"
		self.old.form_payload = json.dumps({"sender": {"country": "CN", "company": "", "phone": None}})
		self.store.shipment.sf_form_json = json.dumps({"sender": {"company": "原仓库"}})
		self.shipping._sender_from_warehouse.return_value = {
			"company": "仓库配置", "country": "CN", "phone": "12345678", "address": "仓库地址",
		}
		self.mod.create_replacement(
			self.store.shipment.name,
			form_json=json.dumps({"sender": {"company": "篡改", "address": "篡改地址", "phone": "000"}}),
			reason="修正收件信息", shipped=1,
		)
		sender = self.shipping._create_order_body_from_form.call_args.args[1]["sender"]
		self.assertEqual(sender, {"company": "原仓库", "country": "CN", "phone": "12345678", "address": "仓库地址"})

	def test_carrier_waybill_number_cannot_be_reused_in_same_or_other_shipment(self):
		self.store.shipment.status = "已发货"
		self.client.create_order.return_value = {"data": {"trackingNo": "OLD-WB", "orderId": 101}}
		result = self.create(shipped=1)
		self.assertFalse(result["ok"])
		self.assertEqual(result["status"], "失败")
		failed = self.store.by_name[result["record"]]
		self.assertTrue(failed.creation_uncertain)
		self.assertEqual(failed.waybill, "OLD-WB")
		self.assertEqual(failed.order_id, 101)
		self.assertIn('"trackingNo": "OLD-WB"', failed.carrier_create_payload)

	def test_waybill_history_query_failure_blocks_carrier_response(self):
		self.store.shipment.status = "已发货"
		self.frappe.get_all = Mock(side_effect=RuntimeError("history table unavailable"))
		with self.assertRaisesRegex(ValidationError, "历史暂时无法核验"):
			self.mod._assert_waybill_not_reused(self.store.shipment, "NEW-WB")

	def test_waybill_parent_query_failure_blocks_carrier_response(self):
		self.frappe.db.get_value = Mock(side_effect=RuntimeError("database unavailable"))
		with self.assertRaisesRegex(ValidationError, "归属暂时无法核验"):
			self.mod._assert_waybill_not_reused(self.store.shipment, "NEW-WB")

	def test_shipped_replacement_stays_pending_and_does_not_switch_current_waybill(self):
		self.store.shipment.status = "已发货"
		result = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})

		new = self.store.by_name[result["record"]]
		self.assertEqual(result["status"], "待替换")
		self.assertEqual(new.replacement_status, "待替换")
		self.assertEqual(new.is_active, 0)
		self.assertEqual(new.replaces_waybill, "OLD-WB")
		self.assertEqual(self.old.replacement_status, "当前")
		self.assertEqual(self.old.is_active, 1)
		self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, new.name)
		self.assertEqual(self.store.shipment.sf_waybill_replacement_status, "待替换")
		self.shipping._remember_successful_sf_receiver.assert_not_called()

	def test_shipped_success_feedback_allows_activation_without_cancelling_old_label(self):
		self.store.shipment.status = "已发货"
		self.old.tracking_status = "In Progress"
		self.old.tracking_status_info = "旧单已到达分拨中心"
		self.old.tracking_payload = '{"waybill": "OLD-WB", "status": "In Progress"}'
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record_name = created["record"]

		feedback = self.mod.record_replacement_feedback(
			self.store.shipment.name, record_name, "顺丰客服确认成功", "客服已确认可以替换面单",
		)
		self.assertEqual(feedback["status"], "待启用")
		self.assertEqual(self.store.by_name[record_name].replacement_status, "待启用")
		self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")
		self.assertEqual(self.old.carrier_cancelled, 0)
		self.assertFalse(self.old.get("carrier_cancel_payload"))

		receiver = {"country": "US", "province": "CA", "city": "Eureka", "post_code": "95501", "address": "Main St"}
		self.store.by_name[record_name].form_payload = json.dumps({"receiver": receiver})
		self.shipping._remember_successful_sf_receiver.assert_not_called()
		activated = self.mod.activate_replacement(
			self.store.shipment.name, record_name, "已核对外部客服反馈并启用新面单",
		)
		self.assertEqual(activated["status"], "当前")
		self.assertEqual(activated["waybill"], "NEW-WB")
		self.shipping._remember_successful_sf_receiver.assert_called_once_with(self.store.shipment, receiver)
		self.assertEqual(self.old.replacement_status, "已替换")
		self.assertEqual(self.old.is_active, 0)
		self.assertEqual(self.store.by_name[record_name].is_active, 1)
		self.assertEqual(self.store.shipment.shipment_id, "NEW-WB")
		self.assertEqual(self.store.shipment.sf_active_waybill_record, record_name)
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, "")
		self.assertEqual(self.old.carrier_cancelled, 0)
		self.assertFalse(self.old.get("carrier_cancel_payload"))
		self.assertFalse(self.store.by_name[record_name].get("carrier_cancelled"))
		self.assertEqual(self.store.shipment.sf_carrier_cancelled, 0)
		self.assertEqual(self.old.tracking_status, "In Progress")
		self.assertEqual(self.old.tracking_status_info, "旧单已到达分拨中心")
		self.assertEqual(self.old.tracking_payload, '{"waybill": "OLD-WB", "status": "In Progress"}')
		with self.assertRaisesRegex(ValidationError, "已获外部确认"):
			self.mod.activate_replacement(self.store.shipment.name, record_name, "重复点击启用新面单")
		# Replacing a label is not permission to cancel the business Shipment,
		# even after the new current label has obtained real cancellation proof.
		new = self.store.by_name[record_name]
		new.carrier_cancelled = 1
		new.carrier_cancelled_waybill = "NEW-WB"
		new.carrier_cancel_payload = json.dumps({
			"waybill": "NEW-WB", "trackingNo": "NEW-WB", "status": "cancelled",
		})
		with self.assertRaisesRegex(ValidationError, "OLD-WB.*没有顺丰明确取消凭证"):
			self.mod.assert_all_waybills_cancelled(self.store.shipment)

	def test_shipped_activation_preserves_real_parent_cancellation_evidence(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		self.mod.record_replacement_feedback(
			self.store.shipment.name, created["record"], "顺丰客服确认成功", "客服确认旧单替换成功",
		)
		self.confirm_old_carrier_cancel()
		parent_payload = self.store.shipment.sf_carrier_cancel_payload
		self.old.carrier_cancelled = 0
		self.old.carrier_cancelled_waybill = ""
		self.old.carrier_cancelled_at = None
		self.old.carrier_cancel_payload = ""
		self.mod.activate_replacement(self.store.shipment.name, created["record"], "核对反馈启用新单")
		self.assertEqual(self.old.carrier_cancelled, 1)
		self.assertEqual(self.old.carrier_cancelled_waybill, "OLD-WB")
		self.assertEqual(self.old.carrier_cancel_payload, parent_payload)
		self.assertEqual(self.store.shipment.shipment_id, "NEW-WB")
		self.assertEqual(self.store.shipment.sf_carrier_cancelled, 0)

	def test_shipped_activation_waits_for_success_not_just_an_accepted_request(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record_name = created["record"]
		with self.assertRaisesRegex(ValidationError, "已获外部确认"):
			self.mod.activate_replacement(self.store.shipment.name, record_name, "尚未联系外部客服")
		for status in self.mod.PROCESSING_FEEDBACK:
			with self.subTest(status=status):
				feedback = self.mod.record_replacement_feedback(
					self.store.shipment.name, record_name, status, "顺丰已受理，尚未确认实际替换成功",
				)
				self.assertEqual(feedback["status"], "待替换")
				with self.assertRaisesRegex(ValidationError, "已获外部确认"):
					self.mod.activate_replacement(self.store.shipment.name, record_name, "尝试提前启用新单")
				self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")
				self.assertEqual(self.old.is_active, 1)

	def test_unshipped_pending_activation_still_requires_exact_old_cancellation(self):
		# Even if the parent now shows dispatch, a pre-dispatch/legacy attempt
		# must not acquire the after-dispatch exemption at activation time.
		self.store.shipment.status = "已发货"
		new = self.store.add(waybill_record(
			name="WB-NEW", waybill="NEW-WB", replacement_status="待启用",
			replaces_waybill="OLD-WB", replacement_shipped=0, is_active=0,
			replacement_feedback="客服反馈已完成替换", replacement_feedback_at="2026-09-16 12:00:00",
		))
		self.store.shipment.sf_waybill_pending_record = new.name
		for shipped_flag in (0, None):
			with self.subTest(shipped_flag=shipped_flag):
				new.replacement_shipped = shipped_flag
				with self.assertRaisesRegex(ValidationError, "明确取消确认"):
					self.mod.activate_replacement(self.store.shipment.name, new.name, "启用发货前申请的新单")
				self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")
				self.assertEqual(self.old.is_active, 1)
		self.confirm_old_carrier_cancel()
		self.mod.activate_replacement(self.store.shipment.name, new.name, "已核对原单取消确认")
		self.assertEqual(self.store.shipment.shipment_id, "NEW-WB")
		self.assertEqual(self.old.carrier_cancelled, 1)

	def test_shipped_activation_keeps_source_identity_checks(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		self.mod.record_replacement_feedback(
			self.store.shipment.name, created["record"], "顺丰客服确认成功", "客服确认旧单替换为新单",
		)
		for field, value in (("sf_active_waybill_record", "WB-OTHER"), ("shipment_id", "OTHER-WB")):
			with self.subTest(field=field):
				original = self.store.shipment.get(field)
				setattr(self.store.shipment, field, value)
				with self.assertRaisesRegex(ValidationError, "与替换记录的原面单不一致"):
					self.mod.activate_replacement(self.store.shipment.name, created["record"], "核对新旧面单归属")
				self.assertEqual(self.old.is_active, 1)
				self.assertEqual(self.store.by_name[created["record"]].replacement_status, "待启用")
				setattr(self.store.shipment, field, original)

	def test_invalid_external_feedback_status_is_rejected_without_mutation(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record = self.store.by_name[created["record"]]
		before_updates = list(record.updates)
		before_parent = (
			self.store.shipment.sf_waybill_replacement_status,
			self.store.shipment.sf_waybill_replacement_note,
			self.store.shipment.sf_waybill_pending_record,
		)

		with self.assertRaises(ValidationError):
			self.mod.record_replacement_feedback(
				self.store.shipment.name,
				record.name,
				"顺丰客服给了一个无法识别的状态",
				"保留有效的外部客服反馈说明",
			)

		self.assertEqual(record.replacement_status, "待替换")
		self.assertFalse(record.get("replacement_feedback"))
		self.assertEqual(record.updates, before_updates)
		self.assertEqual(
			(
				self.store.shipment.sf_waybill_replacement_status,
				self.store.shipment.sf_waybill_replacement_note,
				self.store.shipment.sf_waybill_pending_record,
			),
			before_parent,
		)

	def test_activation_projects_new_waybill_without_old_freight_amount_or_journal(self):
		old_bill = {
			"waybill": "OLD-WB",
			"amount": 233.84,
			"currency": "CNY",
			"records": [{"payAmount": "233.84", "currency": "CNY"}],
		}
		old_payload = json.dumps(old_bill, ensure_ascii=False)
		self.store.shipment.status = "已发货"
		self.store.shipment.shipment_amount = 233.84
		self.store.shipment.sf_freight_currency = "CNY"
		self.store.shipment.sf_freight_status = "账单已取得"
		self.store.shipment.sf_freight_payload = old_payload
		self.store.shipment.sf_freight_journal = "JE-OLD"
		self.store.shipment.sf_freight_previous_journal = ""
		self.store.shipment.sf_freight_accounting_status = "已记账"
		self.store.shipment.sf_freight_accounting_hold = 0
		self.old.freight_amount = 233.84
		self.old.freight_currency = "CNY"
		self.old.freight_status = "账单已取得"
		self.old.freight_payload = old_payload
		self.old.freight_journal = "JE-OLD"
		self.old.freight_accounting_status = "已记账"
		self.old.freight_accounting_hold = 0

		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		self.mod.record_replacement_feedback(
			self.store.shipment.name, created["record"], "顺丰客服确认成功", "顺丰客服已确认替换",
		)
		self.mod.activate_replacement(
			self.store.shipment.name, created["record"], "已核对反馈并启用新面单",
		)

		new = self.store.by_name[created["record"]]
		self.assertEqual(self.old.freight_amount, 233.84)
		self.assertEqual(self.old.freight_payload, old_payload)
		self.assertEqual(self.old.freight_journal, "JE-OLD")
		self.assertEqual(self.old.carrier_cancelled, 0)
		self.assertIsNone(new.get("freight_amount"))
		self.assertFalse(new.get("freight_payload"))
		self.assertFalse(new.get("freight_journal"))
		self.assertEqual(self.store.shipment.shipment_amount, 0)
		self.assertEqual(self.store.shipment.sf_freight_status, "待核实")
		self.assertEqual(self.store.shipment.sf_freight_accounting_status, "记账待处理")
		self.assertEqual(self.store.shipment.sf_freight_accounting_hold, 1)
		self.assertFalse(self.store.shipment.sf_freight_journal)
		self.assertEqual(self.store.shipment.sf_freight_previous_journal, "JE-OLD")

	def test_replacement_child_freight_requery_keeps_previous_bill_and_last_query(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record = self.store.by_name[created["record"]]
		first_response = {
			"data": {"items": [{"payAmount": "100.00", "currency": "CNY"}]},
			"request": "first",
		}
		second_response = {
			"data": {"items": [{"payAmount": "125.00", "currency": "CNY"}]},
			"request": "second",
		}
		self.client.query_case_orders.side_effect = [first_response, second_response]

		first = self.mod.fetch_waybill_freight(self.store.shipment.name, record.name)
		second = self.mod.fetch_waybill_freight(self.store.shipment.name, record.name)

		self.assertEqual(first["amount"], 100.0)
		self.assertEqual(second["amount"], 125.0)
		stored = json.loads(record.freight_payload)
		self.assertEqual(stored["waybill"], "NEW-WB")
		self.assertEqual(stored["amount"], 125.0)
		self.assertEqual(stored["previous_bill"]["waybill"], "NEW-WB")
		self.assertEqual(stored["previous_bill"]["amount"], 100.0)
		self.assertEqual(stored["last_query"]["waybill"], "NEW-WB")
		self.assertEqual(stored["last_query"]["amount"], 125.0)
		self.assertEqual(record.freight_amount, 125.0)
		self.assertEqual(record.freight_status, "账单已取得")
		self.assertEqual(record.freight_accounting_status, "记账待处理")
		self.assertEqual(record.freight_accounting_hold, 1)
		self.assertEqual(self.client.query_case_orders.call_count, 2)

	def test_cancelled_local_shipment_still_allows_historical_freight_query(self):
		self.store.shipment.docstatus = 2
		self.store.shipment.status = "Cancelled"
		self.client.query_case_orders.return_value = {
			"data": {"items": [{"payAmount": "35.50", "currency": "CNY"}]},
		}

		result = self.mod.fetch_waybill_freight(self.store.shipment.name, self.old.name)

		self.assertTrue(result["ok"])
		self.assertEqual(result["amount"], 35.5)
		self.assertEqual(self.old.freight_amount, 35.5)
		self.assertEqual(self.old.freight_accounting_status, "记账待处理")
		self.assertEqual(self.old.freight_accounting_hold, 1)

	def test_legacy_freight_status_is_canonicalized_from_bound_bill(self):
		self.store.shipment.sf_freight_status = "未结算"
		self.store.shipment.shipment_amount = 233.84
		self.store.shipment.sf_freight_currency = "CNY"
		self.store.shipment.sf_freight_payload = json.dumps(
			{
				"waybill": "OLD-WB",
				"amount": 233.84,
				"currency": "CNY",
				"records": [{"payAmount": 233.84, "currency": "CNY"}],
			},
			ensure_ascii=False,
		)

		values = self.mod._legacy_freight_values(self.store.shipment, "OLD-WB")

		self.assertEqual(values["freight_status"], "账单已取得")
		self.assertNotEqual(values["freight_status"], "未结算")

	def test_legacy_foreign_bill_remains_unverified(self):
		self.store.shipment.sf_freight_status = "已结算"
		self.store.shipment.sf_freight_payload = json.dumps(
			{
				"waybill": "OTHER-WB",
				"amount": 233.84,
				"currency": "CNY",
				"records": [{"payAmount": 233.84}],
			},
			ensure_ascii=False,
		)

		values = self.mod._legacy_freight_values(self.store.shipment, "OLD-WB")

		self.assertEqual(values["freight_status"], "待核实")
		self.assertIsNone(values["freight_amount"])

	def test_parent_freight_booking_copies_exact_journal_to_active_waybill(self):
		self.store.shipment.sf_freight_journal = "JE-CURRENT"
		self.store.shipment.sf_freight_previous_journal = "JE-PREVIOUS"
		self.store.shipment.sf_freight_accounting_status = "已记账"
		self.store.shipment.sf_freight_accounting_note = ""
		self.store.shipment.sf_freight_accounting_hold = 0
		payload = {"data": {"items": [{"payAmount": "88.00", "currency": "CNY"}]}}

		self.mod.sync_freight(
			self.store.shipment,
			88.0,
			"CNY",
			payload["data"]["items"],
			payload,
			accounting_status="已记账",
			accounting_hold=0,
		)

		self.assertEqual(self.old.freight_journal, "JE-CURRENT")
		self.assertEqual(self.old.freight_previous_journal, "JE-PREVIOUS")
		self.assertEqual(self.old.freight_accounting_status, "已记账")
		self.assertEqual(self.old.freight_accounting_hold, 0)
		self.assertEqual(self.old.freight_amount, 88.0)

	def test_second_replacement_keeps_latest_parent_journal_as_previous(self):
		self.store.shipment.sf_freight_journal = "JE-SECOND"
		self.store.shipment.sf_freight_previous_journal = "JE-FIRST"
		new = waybill_record(name="WB-NEW", waybill="NEW-WB", replacement_status="待启用", is_active=0)
		self.store.add(new)
		self.mod._parent_projection(self.store.shipment, new)
		self.assertEqual(self.store.shipment.sf_freight_previous_journal, "JE-SECOND")

	def test_accounting_correction_updates_the_exact_historical_waybill(self):
		historical = waybill_record(
			name="WB-HISTORY",
			waybill="OLDER-WB",
			replacement_status="已替换",
			is_active=0,
			freight_journal="JE-HISTORY",
			freight_accounting_status="已记账",
		)
		self.store.add(historical)

		self.mod.sync_accounting_state(
			self.store.shipment,
			"OLDER-WB",
			journal="",
			previous_journal="JE-HISTORY",
			status="记账待处理",
			note="原凭证已取消",
			hold=1,
		)

		self.assertFalse(historical.freight_journal)
		self.assertEqual(historical.freight_previous_journal, "JE-HISTORY")
		self.assertEqual(historical.freight_accounting_status, "记账待处理")
		self.assertEqual(historical.freight_accounting_note, "原凭证已取消")
		self.assertEqual(historical.freight_accounting_hold, 1)
		self.assertNotEqual(self.old.freight_previous_journal, "JE-HISTORY")

	def test_returned_and_lost_statuses_are_treated_as_shipped(self):
		for status in ("已退回", "已丢失", "Returned", "Lost"):
			with self.subTest(status=status):
				mod, _frappe, client, _shipping, store = load_waybill()
				store.shipment = shipment(status=status)
				old = waybill_record()
				store.add(old)
				client.create_order.return_value = {"data": {"trackingNo": f"NEW-{status}", "orderId": 100}}

				result = mod.create_replacement(
					store.shipment.name,
					form_json=json.dumps({"sender": {"country": "CN"}}),
					reason="面单地址填写错误",
					shipped=1,
				)

				new = store.by_name[result["record"]]
				self.assertEqual(result["status"], "待替换")
				self.assertEqual(new.replacement_status, "待替换")
				self.assertEqual(new.is_active, 0)
				self.assertEqual(old.replacement_status, "当前")
				self.assertEqual(old.is_active, 1)
				self.assertEqual(store.shipment.shipment_id, "OLD-WB")

	def test_failed_feedback_keeps_old_waybill_active_and_cannot_be_activated(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record_name = created["record"]
		feedback = self.mod.record_replacement_feedback(
			self.store.shipment.name, record_name, "顺丰客服反馈失败", "客服拒绝替换，继续使用原面单",
		)
		self.assertEqual(feedback["status"], "取消待确认")
		self.assertEqual(self.store.by_name[record_name].replacement_status, "取消待确认")
		self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")
		self.assertEqual(self.old.replacement_status, "当前")
		self.assertEqual(self.old.is_active, 1)
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, record_name)
		with self.assertRaisesRegex(ValidationError, "已获外部确认"):
			self.mod.activate_replacement(self.store.shipment.name, record_name, "尝试强制启用")

	def test_rejected_replacement_requires_carrier_cancellation_before_retry(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record_name = created["record"]
		self.mod.record_replacement_feedback(
			self.store.shipment.name, record_name, "顺丰客服反馈失败", "顺丰客服拒绝替换，等待取消新订单",
		)
		with self.assertRaisesRegex(ValidationError, "明确取消凭证"):
			self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB-2", "orderId": 101}})
		self.assertEqual(self.client.create_order.call_count, 1)

	def test_rejected_replacement_can_be_closed_after_exact_carrier_cancellation(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record_name = created["record"]
		self.mod.record_replacement_feedback(
			self.store.shipment.name, record_name, "顺丰客服反馈失败", "顺丰客服拒绝替换，等待取消新订单",
		)
		self.client.query_order_cancellation.return_value = {
			"confirmed": True,
			"evidence": {"trackingNo": "NEW-WB", "status": "cancelled"},
			"rows": [{"trackingNo": "NEW-WB", "status": "cancelled"}],
		}
		result = self.mod.verify_replacement_cancellation(self.store.shipment.name, record_name)
		self.assertTrue(result["carrier_cancelled"])
		self.assertEqual(result["status"], "已取消")
		record = self.store.by_name[record_name]
		self.assertEqual(record.replacement_status, "已取消")
		self.assertEqual(record.carrier_cancelled, 1)
		self.assertEqual(record.carrier_cancelled_waybill, "NEW-WB")
		self.assertEqual(self.store.shipment.sf_waybill_pending_record, "")
		self.assertEqual(self.store.shipment.sf_waybill_replacement_status, "失败")

	def test_migration_materializes_legacy_parent_with_real_history_record(self):
		mod, frappe, _client, _shipping, store = load_waybill()
		legacy = shipment(sf_active_waybill_record="")
		store.shipment = legacy
		store.records = []
		frappe.db.exists = Mock(return_value=True)
		original_get_all = frappe.get_all

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Shipment":
				return [{"name": legacy.name}]
			return original_get_all(doctype, filters=filters, **kwargs)

		frappe.get_all = get_all
		mod.migrate_existing_waybills()
		self.assertEqual(len(store.records), 1)
		self.assertEqual(store.records[0].waybill, "OLD-WB")
		self.assertEqual(legacy.sf_active_waybill_record, store.records[0].name)

	def test_existing_history_row_repairs_missing_parent_pointer(self):
		self.store.shipment.sf_active_waybill_record = ""
		record = self.mod.ensure_waybill_record(self.store.shipment)
		self.assertEqual(record.name, self.old.name)
		self.assertEqual(self.store.shipment.sf_active_waybill_record, self.old.name)

	def test_cross_shipment_feedback_and_activation_are_rejected(self):
		self.store.shipment.status = "已发货"
		created = self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		record_name = created["record"]
		self.mod.record_replacement_feedback(
			self.store.shipment.name, record_name, "顺丰客服确认成功", "客服已确认可以替换面单",
		)
		other = shipment(name="SHIP-OTHER", shipment_id="OTHER-WB", awb_number="OTHER-WB", status="已发货")
		self.store.shipment = other
		with self.assertRaisesRegex(ValidationError, "不属于该运单"):
			self.mod.record_replacement_feedback(other.name, record_name, "顺丰客服确认成功", "误关联")
		# The endpoint validates both ownership and lifecycle state before any
		# mutation.  Either validation error is acceptable here; the important
		# invariant is that a record from another Shipment cannot be activated.
		with self.assertRaises(ValidationError):
			self.mod.activate_replacement(other.name, record_name, "误关联测试原因")

	def test_cancel_guard_allows_multiple_history_rows_with_exact_cancel_evidence(self):
		"""Every carrier order, including an older replacement, needs its own proof."""
		self.confirm_old_carrier_cancel()
		second = waybill_record(
			name="WB-OLDER", waybill="OLDER-WB", order_id="OLDER-ORDER", replacement_status="已替换",
			carrier_cancelled=1, carrier_cancelled_waybill="OLDER-WB",
			carrier_cancelled_at="2026-09-08 10:00:00",
			carrier_cancel_payload=json.dumps({
				"waybill": "OLDER-WB", "trackingNo": "OLDER-WB", "status": "cancelled",
			}),
			is_active=0,
		)
		self.store.add(second)

		self.assertTrue(self.mod.assert_all_waybills_cancelled(self.store.shipment))

	def test_cancel_guard_blocks_pending_unknown_and_unreadable_history_rows(self):
		cases = ("待替换", "外部未知状态", "db-error")
		for status in cases:
			with self.subTest(status=status):
				mod, frappe, _client, _shipping, store = load_waybill()
				store.shipment = shipment()
				record = waybill_record(replacement_status=status if status != "db-error" else "已替换")
				store.add(record)
				if status == "db-error":
					frappe.get_doc = Mock(side_effect=RuntimeError("history row read failed"))
				with self.assertRaisesRegex(ValidationError, "本地运单不能取消"):
					mod.assert_all_waybills_cancelled(store.shipment)

	def test_empty_external_create_response_is_not_treated_as_carrier_order(self):
		self.assertFalse(self.mod._has_external_order(waybill_record(waybill="", order_id="", carrier_create_payload={})))
		self.assertFalse(self.mod._has_external_order(waybill_record(waybill="", order_id="", carrier_create_payload="{}")))
		self.assertTrue(self.mod._has_external_order(waybill_record(waybill="NEW-WB", order_id="", carrier_create_payload="{}")))

	def test_duplicate_pending_shipped_replacement_is_rejected(self):
		self.store.shipment.status = "已发货"
		self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB", "orderId": 100}})
		with self.assertRaisesRegex(ValidationError, "已有待处理"):
			self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB-2", "orderId": 101}})
		self.assertEqual(self.client.create_order.call_count, 1)

	def test_shipped_flag_must_match_current_transport_state(self):
		with self.assertRaisesRegex(ValidationError, "状态不一致"):
			self.create(shipped=1)
		self.client.create_order.assert_not_called()

	def test_empty_tracking_query_does_not_count_as_shipped(self):
		self.store.shipment.status = "待打单发货"
		self.store.shipment.tracking_status = "In Progress"
		self.store.shipment.tracking_status_info = ""
		self.assertFalse(self.mod._shipment_is_shipped(self.store.shipment))
		self.store.shipment.tracking_status_info = "已到达分拨中心"
		self.assertTrue(self.mod._shipment_is_shipped(self.store.shipment))

	def test_missing_new_waybill_marks_new_record_failed_without_switching_old(self):
		self.old.carrier_cancelled = 1
		self.old.carrier_cancelled_waybill = "OLD-WB"
		self.old.carrier_cancelled_at = "2026-09-08 11:00:00"
		self.old.carrier_cancel_payload = json.dumps({"waybill": "OLD-WB", "trackingNo": "OLD-WB", "status": "cancelled"})
		self.client.create_order.return_value = {"data": {"orderId": 100}}
		result = self.create(shipped=0)
		self.assertFalse(result["ok"])
		self.assertEqual(result["status"], "失败")
		self.assertIn("未返回新运单号", result["error"])
		failed = [row for row in self.store.records if row is not self.old][0]
		self.assertEqual(failed.replacement_status, "失败")
		self.assertEqual(failed.is_active, 0)
		self.assertEqual(self.store.shipment.shipment_id, "OLD-WB")

	def test_uncertain_creation_blocks_retry_until_external_check_is_recorded(self):
		self.store.shipment.status = "已发货"
		self.client.create_order.side_effect = TimeoutError("carrier timeout")
		failed = self.create(shipped=1)
		self.assertFalse(failed["ok"])
		with self.assertRaisesRegex(ValidationError, "下单结果尚未核实"):
			self.create(shipped=1, response={"data": {"trackingNo": "NEW-WB-2", "orderId": 101}})
		resolved = self.mod.resolve_failed_replacement(self.store.shipment.name, failed["record"], "已核对顺丰后台没有生成订单")
		self.assertTrue(resolved["retryable"])
		self.assertEqual(self.store.by_name[failed["record"]].creation_uncertain, 0)

	def test_tracking_response_for_another_waybill_is_not_written_to_history(self):
		before = list(self.old.updates)
		result = self.mod.sync_tracking(self.store.shipment, {
			"awb_number": "OTHER-WB",
			"tracking_status": "Delivered",
			"tracking_status_info": "错误单号",
		})
		self.assertIsNone(result)
		self.assertEqual(self.old.updates, before)
		self.assertFalse(self.old.get("tracking_status"))


class WaybillDocumentGuardTests(unittest.TestCase):
	def load_model(self):
		frappe = types.ModuleType("frappe")
		frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValidationError(message))
		frappe.model = types.ModuleType("frappe.model")
		document_module = types.ModuleType("frappe.model.document")

		class Document:
			def is_new(self):
				return bool(getattr(self, "_new", False))

			def get_doc_before_save(self):
				return getattr(self, "_previous", None)

		document_module.Document = Document
		frappe.model.document = document_module
		path = ROOT / "erpnext_shipping/sf_international/doctype/sf_waybill/sf_waybill.py"
		spec = importlib.util.spec_from_file_location("sf_waybill_model_under_test", path)
		module = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, {"frappe": frappe, "frappe.model": frappe.model, "frappe.model.document": document_module}):
			spec.loader.exec_module(module)
		return module

	def test_new_record_requires_internal_mutation_context(self):
		module = self.load_model()
		doc = module.SFWaybill()
		doc._new = True
		with self.assertRaisesRegex(ValidationError, "专用物流操作"):
			doc.validate()
		token = module.mutation_context()
		try:
			doc.validate()
		finally:
			module.clear_mutation(token)

	def test_existing_record_cannot_be_edited_or_deleted_directly(self):
		module = self.load_model()
		doc = module.SFWaybill()
		doc._previous = object()
		with self.assertRaisesRegex(ValidationError, "历史记录不可直接修改"):
			doc.validate()
		with self.assertRaisesRegex(ValidationError, "历史记录不可删除"):
			doc.on_trash()


if __name__ == "__main__":
	unittest.main()
