"""Regression coverage for provider event ownership and first-save booking."""

import json
import runpy
from pathlib import Path
from unittest.mock import Mock

from test_shipping_regressions import load_shipping, make_doc

ROOT = Path(__file__).resolve().parents[1]


def test_native_first_save_queues_only_after_parent_insertion():
    hooks = runpy.run_path(str(ROOT / "erpnext_shipping/hooks.py"))
    events = hooks["doc_events"]["Shipment"]
    shipping = load_shipping()
    doc = make_doc(docstatus=0, status="Draft", shipment_id="", awb_number="", sf_form_json="{}")
    shipping.is_enabled = Mock(return_value=True)
    shipping._has_field = Mock(return_value=True)
    shipping._is_new_doc = Mock(return_value=True)
    shipping._defaults_from_shipment = Mock(return_value={"receiver": {"address": "Address"}})
    shipping._ensure_official_fields_from_sf = Mock()
    shipping._sender_from_warehouse = Mock(return_value={"address": "Warehouse"})
    shipping.validate_shipment_delivery_notes = Mock()
    shipping._require_submitted_delivery_note = Mock()
    shipping.sync_freight_status = Mock()
    shipping._queue_sf_initial_booking = Mock()
    getattr(shipping, events["before_save"].rsplit(".", 1)[1])(doc)
    shipping._queue_sf_initial_booking.assert_not_called()
    getattr(shipping, events["after_insert"].rsplit(".", 1)[1])(doc)
    shipping._queue_sf_initial_booking.assert_called_once()
    assert shipping._queue_sf_initial_booking.call_args.kwargs == {"allow_new_parent": True}
    shipping.create_order.assert_not_called()


def test_provider_registration_has_single_owner_and_no_foreign_overrides():
    hooks = runpy.run_path(str(ROOT / "erpnext_shipping/hooks.py"))
    assert hooks["shipment_carrier_adapters"] == ["erpnext_shipping.sf_international.carrier_adapter"]
    assert not hooks.get("override_doctype_class")
    assert not hooks.get("override_whitelisted_methods")
    assert set(hooks["doc_events"]) == {"Shipment", "Journal Entry"}
    assert hooks["doctype_js"]["Shipment"] == [
        "public/js/shipment.js", "public/js/sf_international_shipment.js"
    ]


def test_field_contract_keeps_history_and_supports_runtime_freight_states():
    fields = json.loads((ROOT / "erpnext_shipping/sf_international/custom_fields.json").read_text())
    assert sum(len(rows) for rows in fields.values()) == 42
    shipment = {row["fieldname"]: row for row in fields["Shipment"]}
    assert shipment["sf_intercept_assigned_to"]["hidden"] == 1
    assert set(shipment["sf_freight_status"]["options"].splitlines()) == {
        "", "未结算", "已结算", "待核实", "账单已取得"
    }
    assert shipment["sf_active_waybill_record"]["options"] == "SF Waybill"
    assert all(row["read_only"] for row in fields["Journal Entry"])
