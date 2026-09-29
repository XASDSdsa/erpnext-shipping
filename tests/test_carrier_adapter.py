"""SF extends native Shipment only through carrier identity, history and display."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def adapter(monkeypatch):
	frappe = types.ModuleType("frappe")
	frappe.db = Mock()
	frappe.db.exists.return_value = False
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	manual = types.ModuleType("erpnext.stock.doctype.shipment.manual_shipping")
	manual.is_manual = lambda doc: doc.get("service_provider") == "其他物流（手工登记）"
	for module in (frappe, utils, manual):
		monkeypatch.setitem(sys.modules, module.__name__, module)
	base = Path(__file__).parents[1] / "erpnext_shipping/sf_international"
	for name in ("shipment_display", "carrier_adapter"):
		full_name = f"erpnext_shipping.sf_international.{name}"
		spec = importlib.util.spec_from_file_location(full_name, base / f"{name}.py")
		module = importlib.util.module_from_spec(spec)
		monkeypatch.setitem(sys.modules, full_name, module)
		spec.loader.exec_module(module)
	return module, frappe


@pytest.mark.parametrize("provider", ["SF", "sf international", "SF_International", "SF-Global", "顺丰国际", "国际顺丰", "SF国际", "sfglobal"])
def test_sf_aliases_match(adapter, provider):
	assert adapter[0].matches({"service_provider": provider})


def test_manual_provider_wins_over_stale_sf_carrier(adapter):
	assert not adapter[0].matches({"service_provider": "其他物流（手工登记）", "carrier": "SF"})


def test_explicit_other_provider_wins_over_stale_sf_carrier(adapter):
	assert not adapter[0].matches({"service_provider": "Independent carrier", "carrier": "SF"})
	assert adapter[0].matches({"service_provider": "", "carrier": "SF"})


@pytest.mark.parametrize("fieldname", ["sf_active_waybill_record", "sf_iuop_order_id", "sf_label_url"])
def test_sf_history_survives_provider_change(adapter, fieldname):
	module, frappe = adapter
	assert module.has_booking({"service_provider": "其他物流（手工登记）", fieldname: "HISTORY"})
	frappe.db.exists.assert_not_called()


def test_sf_native_waybill_is_booking_history(adapter):
	assert adapter[0].has_booking({"carrier": "SF International", "awb_number": "SF123"})


def test_waybill_table_history_is_checked_independent_of_provider(adapter):
	module, frappe = adapter
	frappe.db.exists.return_value = True
	assert module.has_booking({"name": "SHIPMENT-1", "service_provider": "Unrelated provider"})
	assert frappe.db.exists.call_args_list[-1].args == ("SF Waybill", {"shipment": "SHIPMENT-1"})


def test_no_history_does_not_invent_a_booking(adapter):
	assert not adapter[0].has_booking({"name": "SHIPMENT-1", "service_provider": "SF International"})


def test_display_contains_sf_extensions_only(adapter):
	doc = {
		"sf_freight_status": "账单已取得", "sf_freight_accounting_hold": 1,
		"sf_carrier_cancelled": 1, "sf_waybill_replacement_status": "已换单", "shipment_id": "SF123",
	}
	before = dict(doc)
	assert adapter[0].get_display_values(doc) == {
		"freight_status_display": "账单已取得 · 记账待处理",
		"interception_status_display": "顺丰已取消，待取消本地运单",
		"label_replacement_display": "已换单 SF123",
	}
	assert doc == before


def test_delivery_note_summary_contains_only_carrier_facts(adapter):
	values = adapter[0].get_delivery_note_summary({"sf_carrier_cancelled": 1, "sf_intercept_status": "顺丰已取消"})
	assert all(key.startswith("sf_") for key in values)
	assert values["sf_interception"] == {"state": "顺丰已取消", "carrier_cancelled": True}


def test_delivery_note_summary_preserves_pending_interception_and_currency(adapter):
	values = adapter[0].get_delivery_note_summary({
		"sf_intercept_status": "顺丰客服处理中", "sf_carrier_cancelled": 0, "sf_freight_currency": "CNY",
	})
	assert values["sf_interception"] == {"state": "顺丰客服处理中", "carrier_cancelled": False}
	assert values["sf_freight_currency"] == "CNY"
