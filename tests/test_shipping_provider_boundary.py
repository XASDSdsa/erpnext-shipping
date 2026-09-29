"""Provider endpoints authorize the persisted Shipment before contacting carriers."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest


class Document(SimpleNamespace):
	def get(self, field):
		return getattr(self, field, None)


@pytest.fixture
def boundary(monkeypatch):
	doc = Document(
		name="SHIP-1", docstatus=1, service_provider="", shipment_id="", awb_number="",
		delivery_customer=None, delivery_supplier=None, delivery_company=None,
		shipment_delivery_note=[Document(delivery_note="DN-1")],
		check_permission=Mock(), db_set=Mock(),
	)
	frappe = ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.whitelist = lambda: lambda function: function
	frappe.get_doc = Mock(return_value=doc)
	frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	clients = {
		provider: SimpleNamespace(
			create_shipment=Mock(return_value={"service_provider": provider, "shipment_id": "NEW-ID"}),
			get_tracking_data=Mock(return_value={"awb_number": "AWB-1", "tracking_status": "Delivered"}),
		)
		for provider in ("LetMeShip", "SendCloud")
	}
	history = Mock(return_value=False)
	project = Mock()
	addresses = Mock(return_value={})
	modules = {
		"erpnext.stock.doctype.shipment.carriers": {"has_carrier_booking": history},
		"erpnext.stock.doctype.shipment.shipment": {"get_company_contact": Mock()},
		"erpnext.stock.doctype.shipment.delivery_note_update": {"update_delivery_note": project},
		"erpnext_shipping.erpnext_shipping.doctype.letmeship.letmeship": {
			"LETMESHIP_PROVIDER": "LetMeShip", "get_letmeship_utils": lambda: clients["LetMeShip"],
		},
		"erpnext_shipping.erpnext_shipping.doctype.sendcloud.sendcloud": {
			"SENDCLOUD_PROVIDER": "SendCloud", "SendCloudUtils": lambda: clients["SendCloud"],
		},
		"erpnext_shipping.erpnext_shipping.utils": {
			"get_address": addresses, "get_contact": Mock(return_value={}),
			"match_parcel_service_type_carrier": Mock(),
		},
	}
	for name, attributes in modules.items():
		module = ModuleType(name)
		module.__dict__.update(attributes)
		monkeypatch.setitem(sys.modules, name, module)
	source = Path(__file__).parents[1] / "erpnext_shipping/erpnext_shipping/shipping.py"
	spec = importlib.util.spec_from_file_location("shipping_provider_boundary", source)
	shipping = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(shipping)
	return SimpleNamespace(
		shipping=shipping, doc=doc, frappe=frappe, clients=clients,
		history=history, project=project, addresses=addresses,
	)


def book(boundary, provider="LetMeShip"):
	return boundary.shipping.create_shipment(
		"SHIP-1", "Customer", "Customer", "ADDR-1", "ADDR-2", "[]", "Goods", "2026-09-29", 10,
		json.dumps({"service_provider": provider}), delivery_notes='["UNRELATED-DN"]',
	)


def assert_no_carrier_or_document_changes(boundary):
	for client in boundary.clients.values():
		client.create_shipment.assert_not_called()
		client.get_tracking_data.assert_not_called()
	boundary.doc.db_set.assert_not_called()
	boundary.project.assert_not_called()
	boundary.addresses.assert_not_called()


@pytest.mark.parametrize("stored,requested", [
	("其他物流（手工登记）", "LetMeShip"), ("SF International", "LetMeShip"), ("Other", "SendCloud"),
	("LetMeShip", "SendCloud"), ("SendCloud", "LetMeShip"), ("", "SF International"),
])
def test_booking_cannot_impersonate_a_different_provider(boundary, stored, requested):
	boundary.doc.service_provider = stored
	with pytest.raises(ValueError, match="service provider|Select a LetMeShip"):
		book(boundary, requested)
	assert_no_carrier_or_document_changes(boundary)


@pytest.mark.parametrize("provider", ["LetMeShip", "SendCloud"])
@pytest.mark.parametrize("already_selected", [False, True])
def test_booking_preserves_first_selection_and_selected_provider_workflows(boundary, provider, already_selected):
	boundary.doc.service_provider = provider if already_selected else ""
	result = book(boundary, provider)
	assert result["service_provider"] == provider
	boundary.clients[provider].create_shipment.assert_called_once()
	other_provider = "SendCloud" if provider == "LetMeShip" else "LetMeShip"
	boundary.clients[other_provider].create_shipment.assert_not_called()
	assert boundary.frappe.get_doc.call_args_list[0] == call("Shipment", "SHIP-1", for_update=True)
	boundary.doc.check_permission.assert_called_once_with("write")
	assert boundary.doc.db_set.call_args.args[0]["service_provider"] == provider
	boundary.project.assert_called_once_with(delivery_notes=["DN-1"], shipment_info=result)


@pytest.mark.parametrize("history_source", ["shipment_id", "awb_number", "adapter"])
def test_existing_waybill_or_history_prevents_new_booking(boundary, history_source):
	if history_source == "adapter":
		boundary.history.return_value = True
	else:
		setattr(boundary.doc, history_source, "EXISTING")
	with pytest.raises(ValueError, match="already has a waybill"):
		book(boundary)
	assert_no_carrier_or_document_changes(boundary)


@pytest.mark.parametrize("docstatus", [0, 2])
def test_only_submitted_shipments_can_be_booked(boundary, docstatus):
	boundary.doc.docstatus = docstatus
	with pytest.raises(ValueError, match="Submit the Shipment"):
		book(boundary)
	assert_no_carrier_or_document_changes(boundary)


@pytest.mark.parametrize("operation", ["book", "track"])
def test_write_permission_is_checked_before_any_external_call(boundary, operation):
	boundary.doc.service_provider = "LetMeShip"
	boundary.doc.shipment_id = "BOUND-ID" if operation == "track" else ""
	boundary.doc.check_permission.side_effect = PermissionError("denied")
	with pytest.raises(PermissionError):
		if operation == "book":
			book(boundary)
		else:
			boundary.shipping.update_tracking("SHIP-1", "LetMeShip", "BOUND-ID")
	assert_no_carrier_or_document_changes(boundary)


@pytest.mark.parametrize("stored,requested,stored_id,requested_id", [
	("其他物流（手工登记）", "LetMeShip", "BOUND-ID", "BOUND-ID"),
	("SF International", "SendCloud", "BOUND-ID", "BOUND-ID"),
	("Other", "LetMeShip", "BOUND-ID", "BOUND-ID"),
	("", "LetMeShip", "BOUND-ID", "BOUND-ID"),
	("LetMeShip", "SendCloud", "BOUND-ID", "BOUND-ID"),
	("SendCloud", "LetMeShip", "BOUND-ID", "BOUND-ID"),
	("LetMeShip", "LetMeShip", "BOUND-ID", "OTHER-ID"),
	("LetMeShip", "LetMeShip", "", "OTHER-ID"),
	("SendCloud", "SF International", "BOUND-ID", "BOUND-ID"),
])
def test_tracking_rejects_unbound_provider_or_shipment_id(boundary, stored, requested, stored_id, requested_id):
	boundary.doc.service_provider = stored
	boundary.doc.shipment_id = stored_id
	with pytest.raises(ValueError, match="does not match|Select a LetMeShip"):
		boundary.shipping.update_tracking("SHIP-1", requested, requested_id)
	assert_no_carrier_or_document_changes(boundary)


@pytest.mark.parametrize("provider,shipment_id", [("LetMeShip", "BOUND-ID"), ("SendCloud", "ID-1, ID-2")])
def test_tracking_uses_bound_identity_and_server_delivery_notes(boundary, provider, shipment_id):
	boundary.doc.service_provider = provider
	boundary.doc.shipment_id = shipment_id
	boundary.shipping.update_tracking("SHIP-1", provider, shipment_id, '["UNRELATED-DN"]')
	boundary.clients[provider].get_tracking_data.assert_called_once_with(shipment_id)
	boundary.doc.check_permission.assert_called_once_with("write")
	boundary.doc.db_set.assert_called_once()
	assert boundary.doc.db_set.call_args.args[0]["tracking_status"] == "Delivered"
	boundary.project.assert_called_once_with(
		delivery_notes=["DN-1"], tracking_info=boundary.clients[provider].get_tracking_data.return_value,
	)
