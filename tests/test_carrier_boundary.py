"""An explicit different provider cannot enter SF through stale carrier projections."""

import sys
import types
from unittest.mock import Mock

import pytest

from test_shipping_regressions import ValidationError, load_shipping, make_doc


@pytest.fixture
def boundary():
	shipping = load_shipping()
	doc = make_doc(service_provider="Independent carrier", carrier="SF", shipment_amount=23,
		sf_freight_status="Legacy freight fact")
	shipping.frappe.get_doc = Mock(return_value=doc)
	shipping.frappe.db = Mock()
	shipping._has_field = Mock(return_value=True)
	return shipping, doc


@pytest.mark.parametrize("hook", [
	"place_sf_order_on_save", "book_sf_order_after_insert", "fill_sf_fields_before_submit",
	"mark_sf_waiting_label", "align_sf_status", "cancel_sf_order_on_cancel",
])
def test_other_provider_document_events_leave_document_untouched(boundary, hook):
	shipping, doc = boundary
	before = dict(doc.__dict__)
	getattr(shipping, hook)(doc)
	assert doc.__dict__ == before
	shipping.create_order.assert_not_called()
	shipping.cancel_order.assert_not_called()
	shipping.frappe.enqueue.assert_not_called()


@pytest.mark.parametrize("endpoint,args", [
	("print_shipping_label", ("SHIP-1",)),
	("update_tracking", ("SHIP-1", "SF International", "SF123")),
	("fetch_sf_freight", ("SHIP-1",)),
	("dispatch_sf_shipment", ("SHIP-1",)),
	("track_sf_shipment_readonly", ("SHIP-1", "SF123")),
])
def test_other_provider_carrier_endpoints_reject_before_network(boundary, endpoint, args):
	shipping, doc = boundary
	with pytest.raises(ValidationError, match="Not an SF"):
		getattr(shipping, endpoint)(*args)
	for name in ("start_print", "query_route", "query_case_orders", "cancel_order", "create_order"):
		getattr(shipping, name).assert_not_called()
	assert doc.updates == []


def test_other_provider_cannot_be_booked_through_sf_service_payload(boundary):
	shipping, doc = boundary
	doc.shipment_id = doc.awb_number = ""
	with pytest.raises(ValidationError, match="Not an SF"):
		shipping.create_shipment(
			"SHIP-1", "Company", "Customer", "Origin", "Destination", [], "Goods", "2026-09-29", 20,
			{"service_provider": "SF International"},
		)
	shipping.create_order.assert_not_called()
	assert doc.updates == []


def test_other_provider_cannot_be_cancelled_by_stale_sf_carrier(boundary, monkeypatch):
	shipping, doc = boundary
	shipping.__package__ = "erpnext_shipping.sf_international"
	shipping.__spec__.name = "erpnext_shipping.sf_international.shipping"
	client = types.ModuleType("erpnext_shipping.sf_international.client")
	client.query_order_cancellation = Mock()
	interception = types.ModuleType("erpnext_shipping.sf_international.interception")
	interception.CARRIER_PENDING_STATE = "顺丰取消待确认"
	interception.may_cancel = Mock(return_value=False)
	interception.requires_manual_success = Mock(return_value=False)
	interception.store_cancellation_pending = Mock()
	interception.store_confirmation = Mock(return_value=True)
	for module in (client, interception):
		monkeypatch.setitem(sys.modules, module.__name__, module)
	with pytest.raises(ValidationError, match="Not an SF"):
		shipping.cancel_sf_shipment(doc.name)
	shipping.cancel_order.assert_not_called()
	client.query_order_cancellation.assert_not_called()
	interception.store_cancellation_pending.assert_not_called()
	assert doc.updates == []


def test_other_provider_summary_never_queries_sf_projection(boundary):
	shipping, doc = boundary
	shipping.frappe.get_list = Mock(side_effect=AssertionError("Other provider entered the SF projection query"))
	assert shipping.enrich_shipment_summary(shipments=[doc]) == {}
	shipping.frappe.get_list.assert_not_called()


def test_other_provider_unbooked_freight_sync_preserves_values(boundary):
	shipping, doc = boundary
	doc.shipment_id = doc.awb_number = ""
	before = dict(doc.__dict__)
	shipping.sync_freight_status(doc)
	assert doc.__dict__ == before
