"""Carrier scope tests that run without a Frappe site or provider credentials."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest


class Document(SimpleNamespace):
	def get(self, field):
		return getattr(self, field, None)


@pytest.fixture
def shipping_utils(monkeypatch):
	frappe = ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.db = SimpleNamespace(get_value=Mock())

	def throw(message):
		raise ValueError(message)

	frappe.throw = throw
	data = ModuleType("frappe.utils.data")
	data.get_link_to_form = Mock()
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils.data", data)
	source = Path(__file__).parents[1] / "erpnext_shipping/erpnext_shipping/utils.py"
	spec = importlib.util.spec_from_file_location("shipping_carrier_scope_utils", source)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module, frappe


@pytest.mark.parametrize("provider", [None, "", "其他物流（手工登记）", "Other", "SF International"])
def test_other_carriers_do_not_read_user_phone(shipping_utils, provider):
	utils, frappe = shipping_utils
	# Omit contact fields: unrelated carriers must return before reading them.
	utils.validate_phone(Document(service_provider=provider))
	frappe.db.get_value.assert_not_called()


@pytest.mark.parametrize("provider", ["LetMeShip", "SendCloud"])
@pytest.mark.parametrize("phone", [None, "12345678", "+86 12345678901"])
def test_supported_carriers_still_validate_user_phone(shipping_utils, provider, phone):
	utils, frappe = shipping_utils
	frappe.db.get_value.return_value = phone
	doc = Document(service_provider=provider, pickup_from_type="Company", pickup_contact_person="user-1")
	if phone == "+86 12345678901":
		utils.validate_phone(doc)
	else:
		with pytest.raises(ValueError, match="Pickup contact phone"):
			utils.validate_phone(doc)
	frappe.db.get_value.assert_called_once_with("User", "user-1", "phone")


def test_supported_carrier_reads_contact_for_non_company_pickup(shipping_utils):
	utils, frappe = shipping_utils
	frappe.db.get_value.return_value = "+49 12345678"
	utils.validate_phone(Document(service_provider="SendCloud", pickup_from_type="Customer", pickup_contact_name="contact-1"))
	frappe.db.get_value.assert_called_once_with("Contact", "contact-1", "phone")


def test_daily_tracking_only_updates_active_supported_carriers(shipping_utils, monkeypatch):
	utils, frappe = shipping_utils
	rows = [
		Document(name=f"SHIP-{index}", service_provider=provider, docstatus=1, status="Booked",
			shipment_id=f"BOOKING-{index}", tracking_status="In Transit", shipment_delivery_note=[])
		for index, provider in enumerate(["LetMeShip", "SendCloud", "SF International", "其他物流（手工登记）", "Other", ""])
	]
	rows.extend([
		Document(**{**vars(rows[0]), "name": "DELIVERED", "tracking_status": "Delivered"}),
		Document(**{**vars(rows[0]), "name": "DRAFT", "docstatus": 0}),
		Document(**{**vars(rows[0]), "name": "UNBOOKED", "shipment_id": ""}),
	])

	def matches(value, condition):
		if isinstance(condition, list):
			operator, expected = condition
			assert operator in ("in", "!=")
			return value in expected if operator == "in" else value != expected
		return value == condition

	def get_all(doctype, filters):
		assert doctype == "Shipment"
		return [row for row in rows if all(matches(row.get(field), value) for field, value in filters.items())]

	frappe.get_all = get_all
	frappe.get_doc = Mock(side_effect=lambda doctype, name: next(row for row in rows if row.name == name))
	shipping = ModuleType("erpnext_shipping.erpnext_shipping.shipping")
	shipping.update_tracking = Mock(return_value=None)
	monkeypatch.setitem(sys.modules, shipping.__name__, shipping)
	utils.update_tracking_info_daily()
	assert frappe.get_doc.call_args_list == [call("Shipment", "SHIP-0"), call("Shipment", "SHIP-1")]
	assert shipping.update_tracking.call_args_list == [
		call("SHIP-0", "LetMeShip", "BOOKING-0", []),
		call("SHIP-1", "SendCloud", "BOOKING-1", []),
	]
