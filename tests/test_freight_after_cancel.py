"""Freight lifecycle checks for cancelled SF shipments (offline only)."""

import json
import unittest
from unittest.mock import Mock

from test_shipping_regressions import Doc, ValidationError, load_shipping, make_doc


class FreightAfterCancel(unittest.TestCase):
	def setUp(self):
		self.shipping = load_shipping()
		self.doc = make_doc(docstatus=2, status="已取消发货", shipment_amount=233.84)
		self.shipping._has_field = Mock(return_value=True)
		self.shipping.frappe.db = Mock()

	def test_cancelled_waybill_can_query_and_empty_result_keeps_amount(self):
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.query_case_orders = Mock(return_value={"data": {"items": []}})
		result = self.shipping.fetch_sf_freight(self.doc.name)
		self.assertEqual(result["status"], self.shipping.FREIGHT_UNSETTLED)
		self.assertEqual(self.doc.shipment_amount, 233.84)
		self.shipping.query_case_orders.assert_called_once()

	def test_evidence_is_bound_to_current_waybill(self):
		self.doc.sf_freight_payload = json.dumps(
			{"waybill": "OTHER", "amount": 1, "records": [{"payAmount": 1, "currency": "CNY"}], "currency": "CNY"}
		)
		self.doc.sf_freight_journal = "JE-1"
		self.shipping.frappe.db.get_value.return_value = 1
		self.assertFalse(self.shipping._has_current_freight_evidence(self.doc))
		self.doc.sf_freight_payload = json.dumps(
			{"waybill": "SF123", "amount": 1, "records": [{"payAmount": 1, "currency": "CNY"}], "currency": "CNY"}
		)
		self.assertTrue(self.shipping._has_current_freight_evidence(self.doc))

	def test_negative_carrier_amount_is_rejected(self):
		with self.assertRaises(ValidationError):
			self.shipping._sum_pay_amount({"data": {"items": [{"payAmount": "-1", "currency": "CNY"}]}})

	def test_empty_query_does_not_clear_posted_journal_link(self):
		self.doc.sf_freight_journal = "JE-1"
		self.shipping._apply_freight(self.doc, None, "CNY", [], {"data": {"items": []}})
		self.assertEqual(self.doc.sf_freight_journal, "JE-1")
		self.assertEqual(self.doc.shipment_amount, 233.84)

	def test_accounting_failure_keeps_bill_and_marks_review(self):
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.query_case_orders = Mock(
			return_value={"data": {"items": [{"payAmount": "12.34", "currency": "CNY"}]}}
		)
		self.shipping._book_sf_freight_journal = Mock(side_effect=ValidationError("missing account"))
		result = self.shipping.fetch_sf_freight(self.doc.name)
		self.assertTrue(result["review_required"])
		self.assertEqual(self.doc.sf_freight_accounting_status, "待复核")
		stored = json.loads(self.doc.sf_freight_payload)
		self.assertEqual(stored["records"][0]["payAmount"], "12.34")
		self.assertEqual(stored["accounting_status"], "待复核")
		self.shipping.frappe.db.savepoint.assert_called_once_with("sf_freight_accounting")
		self.shipping.frappe.db.rollback.assert_called_once_with(save_point="sf_freight_accounting")

	def test_payload_is_not_truncated_and_remains_json(self):
		self.shipping._apply_freight(
			self.doc,
			1.25,
			"CNY",
			[{"payAmount": "1.25", "currency": "CNY"}],
			{"large": "x" * 100000},
			accounting_status="待记账",
		)
		stored = json.loads(self.doc.sf_freight_payload)
		self.assertEqual(len(stored["raw"]["large"]), 100000)

	def bill(self, amount=233.84, waybill="SF123"):
		return {"waybill": waybill, "amount": amount, "currency": "CNY", "records": [{"payAmount": amount, "currency": "CNY"}]}

	def test_empty_query_preserves_confirmed_bill_and_posted_state(self):
		self.doc.sf_freight_payload = json.dumps(self.bill())
		self.doc.sf_freight_journal = "JE-1"
		self.doc.sf_freight_accounting_status = "已记账"
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.frappe.db.get_value.side_effect = lambda dt, *args, **kwargs: "CNY" if dt == "Company" else Doc(docstatus=1, total_debit=233.84, company="Company")
		self.shipping.query_case_orders = Mock(return_value={"data": {"items": []}})
		self.shipping._book_sf_freight_journal = Mock()
		result = self.shipping.fetch_sf_freight(self.doc.name)
		self.assertEqual(result["status"], "账单已取得")
		self.assertEqual(result["accounting_status"], "已记账")
		self.assertEqual(result["confirmed_amount"], 233.84)
		self.assertEqual(result["query_status"], "本次未查到账单")
		self.assertTrue(self.shipping._has_current_freight_evidence(self.doc))
		self.shipping._book_sf_freight_journal.assert_not_called()

	def test_empty_query_cannot_confirm_wrong_currency_or_company(self):
		for company, currency in (("Other Company", "CNY"), ("Company", "USD")):
			with self.subTest(company=company, currency=currency):
				bill = self.bill()
				bill["currency"] = bill["records"][0]["currency"] = currency
				self.doc.sf_freight_payload = json.dumps(bill)
				self.doc.sf_freight_journal = "JE-1"
				self.shipping.frappe.get_doc = Mock(return_value=self.doc)
				self.shipping.frappe.db.get_value.side_effect = lambda dt, *args, **kwargs: "CNY" if dt == "Company" else Doc(docstatus=1, total_debit=233.84, company=company)
				self.shipping.query_case_orders = Mock(return_value={"data": {"items": []}})
				result = self.shipping.fetch_sf_freight(self.doc.name)
				self.assertEqual(result["accounting_status"], "待复核")

	def test_authorized_zero_bill_resolution_creates_no_journal(self):
		self.doc.sf_freight_payload = json.dumps(self.bill(amount=0))
		self.doc.sf_freight_accounting_hold = 1
		self.shipping.freight_accounting.assert_can_book = Mock()
		self.shipping._book_sf_freight_journal = Mock()
		result = self.shipping.resume_freight_booking(self.doc, "已核实顺丰费用调整为零")
		self.assertEqual(result["accounting_status"], "无需记账")
		self.assertIsNone(result["journal_entry"])
		self.assertEqual(self.doc.sf_freight_accounting_hold, 1)
		self.shipping._book_sf_freight_journal.assert_not_called()
		self.doc.sf_freight_journal = "JE-1"
		with self.assertRaisesRegex(ValidationError, "仍有关联凭证"):
			self.shipping.resume_freight_booking(self.doc, "已核实顺丰费用调整为零")

	def test_legacy_previous_bill_recovers_evidence_but_not_other_waybill(self):
		self.doc.sf_freight_payload = json.dumps({"waybill": "SF123", "records": [], "amount": None, "previous_bill": self.bill()})
		self.assertTrue(self.shipping._has_current_freight_evidence(self.doc))
		self.doc.shipment_id = self.doc.awb_number = "SF999"
		self.assertFalse(self.shipping._has_current_freight_evidence(self.doc))

	def test_missing_response_data_is_invalid_not_an_empty_bill(self):
		for payload in ({}, {"data": {}}, {"data": None}, {"data": {"items": None}}):
			with self.subTest(payload=payload), self.assertRaises(ValidationError):
				self.shipping._sum_pay_amount(payload)

	def test_carrier_only_sf_status_is_synchronized(self):
		self.doc.service_provider = ""
		self.doc.carrier = "SF"
		self.doc.sf_freight_payload = json.dumps(self.bill(amount=0))
		self.shipping.sync_freight_status(self.doc)
		self.assertEqual(self.doc.sf_freight_status, "账单已取得")
		self.assertEqual(self.doc.sf_freight_accounting_status, "无需记账")

	def test_zero_bill_is_confirmed_without_journal(self):
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.query_case_orders = Mock(return_value={"data": {"items": [{"payAmount": 0, "currency": "CNY"}]}})
		self.shipping._book_sf_freight_journal = Mock()
		result = self.shipping.fetch_sf_freight(self.doc.name)
		self.assertEqual(result["status"], "账单已取得")
		self.assertEqual(result["confirmed_amount"], 0)
		self.assertEqual(result["accounting_status"], "无需记账")
		self.shipping._book_sf_freight_journal.assert_not_called()

	def test_query_failure_does_not_erase_confirmed_bill(self):
		self.doc.sf_freight_payload = json.dumps(self.bill())
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.query_case_orders = Mock(side_effect=TimeoutError())
		result = self.shipping.fetch_sf_freight(self.doc.name)
		self.assertTrue(result["query_failed"])
		self.assertEqual(result["status"], "账单已取得")
		self.assertEqual(result["confirmed_amount"], 233.84)

	def test_accounting_hold_retains_new_bill_without_posting(self):
		self.doc.sf_freight_accounting_hold = 1
		self.shipping.frappe.get_doc = Mock(return_value=self.doc)
		self.shipping.query_case_orders = Mock(return_value={"data": {"items": [{"payAmount": 250, "currency": "CNY"}]}})
		self.shipping._book_sf_freight_journal = Mock()
		result = self.shipping.fetch_sf_freight(self.doc.name)
		self.assertEqual(result["status"], "账单已取得")
		self.assertEqual(result["accounting_status"], "记账待处理")
		self.assertEqual(result["confirmed_amount"], 250)
		self.assertTrue(result["accounting_hold"])
		self.shipping._book_sf_freight_journal.assert_not_called()


if __name__ == "__main__":
	unittest.main()
