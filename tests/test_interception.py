"""Interception authorization and carrier evidence, without carrier mutations."""

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from test_client_and_flow import adapter


class Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def check_permission(self, permission):
		pass

	def db_set(self, values, **kwargs):
		self.__dict__.update(values)

	def get_doc_before_save(self):
		return self.get("previous")

	def has_value_changed(self, key):
		return self.get(key) != self.previous.get(key)

	def set_onload(self, key, value):
		setattr(self, key, value)


@pytest.fixture
def workflow(adapter, monkeypatch):
	frappe, load = adapter
	client = load("client")
	utils = sys.modules["frappe.utils"]
	utils.cint = lambda value: int(value or 0)
	utils.now_datetime = lambda: "2026-09-07 12:00:00"
	utils.escape_html = lambda value: value
	frappe.session = SimpleNamespace(user="cs@example.test")
	frappe.PermissionError = PermissionError
	frappe.get_roles = Mock(return_value=[])
	frappe.has_permission = Mock(return_value=True)
	frappe.db = Mock()
	frappe.get_meta = Mock(return_value=SimpleNamespace(has_field=lambda key: True))
	frappe.publish_realtime = Mock()
	pkg = ModuleType("interception_test")
	pkg.__path__ = []
	shipping = ModuleType("interception_test.shipping")
	shipping._is_sf_shipment = lambda doc: doc.get("service_provider") == "SF International"
	for key, value in {"interception_test": pkg, "interception_test.client": client, "interception_test.shipping": shipping}.items():
		monkeypatch.setitem(sys.modules, key, value)
	path = Path(__file__).parents[1] / "erpnext_shipping/sf_international/interception.py"
	spec = importlib.util.spec_from_file_location("interception_test.interception", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	doc = Doc(name="SHIP-1", service_provider="SF International", carrier="SF International", docstatus=1,
		status="已发货", shipment_id="SF123", awb_number="SF123", sf_intercept_status="", sf_carrier_cancelled=0,
		shipment_amount=233.84, add_comment=Mock())
	frappe.get_doc = Mock(return_value=doc)
	return module, frappe, doc, client


def evidence():
	return {"confirmed": True, "evidence": {"trackingNo": "SF123", "status": "已取消"}}


def test_request_records_external_carrier_contact_without_internal_assignment(workflow):
	module, frappe, doc, _ = workflow
	frappe.get_doc.return_value = doc
	module.request_interception("SHIP-1", "客户申请退回", assigned_to="legacy-erp-user@example.test")
	assert doc.sf_intercept_status == "申请中"
	assert doc.status == "已发货" and doc.docstatus == 1 and doc.shipment_amount == 233.84
	assert not hasattr(doc, "sf_intercept_assigned_to")
	assert not module.may_cancel(doc)
	assert not frappe.publish_realtime.called
	frappe.get_doc.assert_called_once_with("Shipment", "SHIP-1", for_update=True)


def test_manual_success_is_insufficient_and_foreign_waybill_is_rejected(workflow):
	module, _, doc, _ = workflow
	doc.sf_intercept_status = "申请中"
	module.record_interception_result(doc.name, "顺丰客服确认成功待顺丰确认", "顺丰客服确认已拦截")
	assert not module.may_cancel(doc)
	assert not module.store_confirmation(doc, {"confirmed": True, "evidence": {"trackingNo": "OTHER", "status": "已取消"}})
	assert module.store_confirmation(doc, evidence())
	assert module.may_cancel(doc)
	assert doc.docstatus == 1 and doc.status == "已发货" and doc.shipment_amount == 233.84


@pytest.mark.parametrize("state", ["", "申请中", "顺丰客服处理中", "顺丰客服反馈失败"])
def test_shipped_requires_manual_success_before_query(workflow, state):
	module, _, doc, _ = workflow
	doc.sf_intercept_status = state
	module.query_order_cancellation = Mock(side_effect=AssertionError("query must not run"))
	with pytest.raises(ValueError):
		module.verify_carrier_cancellation(doc.name)


def test_empty_carrier_response_keeps_waiting(workflow):
	module, _, doc, _ = workflow
	doc.sf_intercept_status = "顺丰客服确认成功待顺丰确认"
	module.query_order_cancellation = Mock(return_value={"confirmed": False, "rows": []})
	assert not module.verify_carrier_cancellation(doc.name)["carrier_cancelled"]
	assert doc.sf_intercept_status == "顺丰客服确认成功待顺丰确认"
	assert doc.docstatus == 1 and doc.status == "已发货"


def test_cancel_request_pending_state_only_allows_recheck(workflow):
	module, _, doc, _ = workflow
	doc.docstatus, doc.status = 1, "待打单发货"
	doc.sf_intercept_status = module.CARRIER_PENDING_STATE
	doc.sf_carrier_cancelled = 0
	module.query_order_cancellation = Mock(return_value={"confirmed": False, "rows": []})
	assert not module.may_cancel(doc)
	state = module.onload(doc)
	assert doc.sf_interception["can_verify"]
	assert not doc.sf_interception["can_record"]
	assert not module.verify_carrier_cancellation(doc.name)["carrier_cancelled"]
	assert doc.sf_intercept_status == module.CARRIER_PENDING_STATE


def test_unshipped_draft_can_verify_without_interception(workflow):
	module, _, doc, _ = workflow
	doc.docstatus, doc.status = 0, "Draft"
	module.query_order_cancellation = Mock(return_value=evidence())
	assert module.verify_carrier_cancellation(doc.name)["carrier_cancelled"]
	assert doc.docstatus == 0 and doc.sf_intercept_status == "顺丰已取消"


def test_unshipped_waybill_offers_direct_cancel_instead_of_interception(workflow):
	module, _, doc, _ = workflow
	doc.status = "待打单发货"
	doc.tracking_status = ""
	module.onload(doc)
	assert not doc.sf_interception["can_request"]
	assert doc.sf_interception["can_verify"]
	with pytest.raises(ValueError, match="直接取消顺丰面单"):
		module.request_interception(doc.name, "不再发货")


def test_booked_without_tracking_is_not_dispatch_evidence(workflow):
	module, _, doc, _ = workflow
	doc.status = "Booked"
	doc.tracking_status = ""
	doc.tracking_status_info = ""

	assert not module.has_dispatch_evidence(doc)
	assert not module.is_interceptable(doc)
	module.onload(doc)
	assert not doc.sf_interception["can_request"]
	assert doc.sf_interception["can_verify"]
	with pytest.raises(ValueError, match="尚未发出"):
		module.request_interception(doc.name, "客户申请拦截")


def test_shipped_waybill_with_route_event_allows_interception_request(workflow):
	module, _, doc, _ = workflow
	doc.status = "已发货"
	doc.tracking_status = "In Progress"
	doc.tracking_status_info = "已揽收，运输中"

	assert module.has_dispatch_evidence(doc)
	assert module.is_interceptable(doc)
	module.onload(doc)
	assert doc.sf_interception["can_request"]
	result = module.request_interception(doc.name, "客户申请拦截")
	assert result["status"] == module.REQUEST_STATE
	assert doc.sf_intercept_status == module.REQUEST_STATE


@pytest.mark.parametrize("status, tracking", [
	("Completed", ""), ("已签收", ""), ("已退回", ""), ("已丢失", ""),
	("已发货", "Delivered"), ("已发货", "Returned"), ("已发货", "Lost"),
])
def test_terminal_transport_state_cannot_request_interception(workflow, status, tracking):
	module, _, doc, _ = workflow
	doc.status, doc.tracking_status = status, tracking
	module.onload(doc)
	assert not doc.sf_interception["can_request"]
	with pytest.raises(ValueError, match="终态"):
		module.request_interception(doc.name, "客户申请退回")


def test_terminal_business_status_wins_over_stale_in_progress_tracking(workflow):
	module, _, doc, _ = workflow
	doc.status, doc.tracking_status = "Completed", "In Progress"
	module.onload(doc)
	assert not doc.sf_interception["can_request"]


@pytest.mark.parametrize("value", ["[]", "invalid", '{"waybill":"SF123","status":"已取消"}'])
def test_invalid_stored_confirmation_cannot_unlock(workflow, value):
	module, _, doc, _ = workflow
	doc.sf_intercept_status = "顺丰已取消"
	doc.sf_carrier_cancelled = 1
	doc.sf_carrier_cancelled_waybill = "SF123"
	doc.sf_carrier_cancelled_at = "2026-09-07 12:00:00"
	doc.sf_carrier_cancel_payload = value
	assert not module.may_cancel(doc)


def test_forged_fields_rejected_on_insert_and_update(workflow):
	module, _, doc, _ = workflow
	doc.sf_carrier_cancelled = 1
	with pytest.raises(ValueError, match="新建单据"):
		module.validate_state(doc)
	doc.sf_carrier_cancelled = 0
	doc.previous = deepcopy(doc)
	doc.sf_carrier_cancelled = 1
	with pytest.raises(ValueError, match="专用处理按钮"):
		module.validate_state(doc)
	doc.previous = None
	doc.sf_intercept_assigned_to = "legacy-user@example.test"
	with pytest.raises(ValueError, match="新建单据"):
		module.validate_state(doc)


@pytest.mark.parametrize("row", [
	{"trackingNo": "OTHER", "status": "已取消"},
	{"trackingNo": "SF123", "waybillNo": "OTHER", "status": "已取消"},
	{"trackingNo": "SF123", "status": "not cancelled"},
	{"trackingNo": "SF123", "status": "未取消", "remark": "已取消"},
	{"trackingNo": "SF123", "orderStatus": 99},
])
def test_carrier_query_rejects_ambiguous_or_wrong_order(workflow, monkeypatch, row):
	_, _, _, client = workflow
	monkeypatch.setattr(client, "headers", lambda **kwargs: {})
	monkeypatch.setattr(client.requests, "post", Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": {"items": [row]}})))
	assert not client.query_order_cancellation("SF123")["confirmed"]


def test_carrier_query_normalizes_explicit_status(workflow, monkeypatch):
	_, _, _, client = workflow
	monkeypatch.setattr(client, "headers", lambda **kwargs: {})
	monkeypatch.setattr(client.requests, "post", Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": {"items": [{"trackingNo": "SF123", "orderStatus": 99, "cancelStatus": "CANCELLED."}]}})))
	result = client.query_order_cancellation("SF123")
	assert result["confirmed"] and result["evidence"]["status"] == "cancelled"
