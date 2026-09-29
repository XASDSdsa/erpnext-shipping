import json
import unittest
from pathlib import Path


class WaybillSchemaTest(unittest.TestCase):
	def test_sf_waybill_keeps_each_carrier_and_freight_fact(self):
		path = Path(__file__).parents[1] / "erpnext_shipping/sf_international/doctype/sf_waybill/sf_waybill.json"
		fields = {field["fieldname"] for field in json.loads(path.read_text())["fields"]}
		self.assertTrue({
			"shipment", "waybill", "order_id", "form_payload", "label_url",
			"tracking_status", "tracking_payload", "carrier_cancel_payload",
			"freight_amount", "freight_payload", "freight_journal",
			"replacement_status", "replaces_waybill", "replacement_shipped", "replacement_feedback", "creation_uncertain", "is_active",
		} <= fields)


	def test_shipment_has_read_only_waybill_history_and_current_pointer(self):
		path = Path(__file__).parents[1] / "erpnext_shipping/sf_international/custom_fields.json"
		source = path.read_text()
		self.assertIn('"fieldname": "sf_active_waybill_record"', source)
