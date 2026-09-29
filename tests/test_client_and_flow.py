"""Isolated adapter checks; no site, database, or live SF requests."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def adapter(monkeypatch):
	frappe = ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.whitelist = lambda **kwargs: lambda function: function
	def throw(message, *args, **kwargs):
		raise ValueError(message)
	frappe.throw = throw
	frappe.get_single = Mock(return_value=SimpleNamespace())
	frappe.get_doc = Mock(return_value=SimpleNamespace(name="SESSION"))
	password = ModuleType("frappe.utils.password")
	password.get_decrypted_password = Mock(return_value="test-token")
	for name, module in {"frappe": frappe, "frappe.utils": ModuleType("frappe.utils"), "frappe.utils.password": password}.items():
		monkeypatch.setitem(sys.modules, name, module)
	def load(name):
		spec = importlib.util.spec_from_file_location(f"isolated_{name}", ROOT / "erpnext_shipping" / "sf_international" / f"{name}.py")
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	return frappe, load


@pytest.mark.parametrize("message", ["cannot be cancelled", "not cancelled", "already cancellation failed", "订单未取消", "订单未取消，请检查是否已经取消"])
def test_cancel_failure_is_not_success(adapter, message):
	_, load = adapter
	assert not load("client")._already_cancelled_msg(message)


@pytest.mark.parametrize("message", ["Already cancelled.", "The order has already been canceled", "该订单已经取消。"])
def test_already_cancelled_is_idempotent(adapter, message):
	_, load = adapter
	assert load("client")._already_cancelled_msg(message)


@pytest.mark.parametrize("data", [{"items": [{"trackingNo": "SF-OTHER", "orderId": 9}]}, [{"trackingNo": "SF-OTHER", "orderId": 9}], {"items": [None, "broken"]}, None])
def test_query_never_substitutes_another_order(adapter, monkeypatch, data):
	_, load = adapter
	client = load("client")
	monkeypatch.setattr(client, "headers", lambda **kwargs: {})
	monkeypatch.setattr(client.requests, "post", Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": data})))
	with pytest.raises(ValueError, match="was not found"):
		client.query_order("SF-WANTED")


@pytest.mark.parametrize("wrapped", [True, False])
def test_query_matches_requested_waybill_in_supported_shapes(adapter, monkeypatch, wrapped):
	_, load = adapter
	client = load("client")
	rows = [{"trackingNo": "SF-OTHER", "orderId": 9}, {"trackingNo": "SF-WANTED", "orderId": 10}]
	monkeypatch.setattr(client, "headers", lambda **kwargs: {})
	monkeypatch.setattr(client.requests, "post", Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": {"items": rows} if wrapped else rows})))
	assert client.query_order(" SF-WANTED ")["orderId"] == 10


def test_invalid_json_structure_is_reported(adapter):
	_, load = adapter
	with pytest.raises(ValueError, match="invalid response"):
		load("client")._json(SimpleNamespace(status_code=200, json=lambda: []))


@pytest.mark.parametrize("status", [401, 403, 500, 502, 503, 504])
def test_http_failure_preserves_status_without_rendering_gateway_html(adapter, status):
	_, load = adapter
	response = SimpleNamespace(status_code=status, text="<html><h1>Gateway error</h1></html>")
	with pytest.raises(ValueError) as error:
		load("client")._json(response)
	assert f"HTTP {status}" in str(error.value)
	assert "<html>" not in str(error.value)
	assert "Gateway error" not in str(error.value)
	if status == 504:
		assert "timed out" in str(error.value)


def test_successful_http_response_keeps_region_results(adapter):
	_, load = adapter
	payload = {"code": 200, "data": [{"regionName": "Texas"}]}
	response = SimpleNamespace(status_code=200, json=lambda: payload)
	assert load("client")._json(response) == payload
