"""SF bill presentation remains behind the native carrier extension interface."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class FreightSummaryTests(unittest.TestCase):
	def setUp(self):
		frappe = types.ModuleType("frappe")
		frappe._ = lambda value: value
		utils = types.ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		utils.fmt_money = lambda amount, currency=None: f"{amount:.2f}"
		shipping = types.ModuleType("erpnext_shipping.sf_international.shipping")
		self.reader = Mock(return_value={})
		shipping.get_sales_order_freight_map = self.reader
		self.modules = patch.dict(sys.modules, {module.__name__: module for module in [frappe, utils, shipping]})
		self.modules.start()
		self.addCleanup(self.modules.stop)
		path = Path(__file__).parents[1] / "erpnext_shipping/sf_international/freight_summary.py"
		spec = importlib.util.spec_from_file_location("sf_freight_summary_under_test", path)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def test_bill_and_accounting_states_are_distinct_and_keep_hold_and_review_details(self):
		row = self.module.freight_presentation({"total": 3, "billed": 2, "booked": 1, "held": 1, "review": 1, "billed_amount": 340, "currency": "CNY"})
		self.assertEqual(row["text"], "部分账单已取得")
		self.assertEqual(row["color"], "orange")
		for text in ["记账待处理 1", "待复核 1", "已记账 1/3", "340.00 CNY"]:
			self.assertIn(text, row["title"])
		self.assertNotIn("已结算", str(row))
		self.assertNotIn("已付款", str(row))

	def test_legacy_amount_is_not_promoted_to_confirmed_bill_and_mixed_currency_is_not_relabelled(self):
		row = self.module.freight_presentation({"total": 1, "settled": 1, "amount": 233.84})
		self.assertEqual(row["text"], "账单待核实")
		self.assertIn("已记账 1/1", row["extra"])
		self.assertNotIn("233.84", str(row))
		mixed = self.module.freight_presentation({"total": 2, "billed": 2, "billed_amount": None, "currency": None, "amount": 50})
		self.assertNotIn("CNY", str(mixed))
		self.assertNotIn("50.00", str(mixed))

	def test_confirmed_zero_bill_is_visible(self):
		row = self.module.freight_presentation({"total": 1, "billed": 1, "booked": 1, "billed_amount": 0, "currency": "CNY"})
		self.assertEqual(row["color"], "green")
		self.assertIn("0.00 CNY", row["extra"])

	def test_orders_without_sf_shipments_are_omitted_and_manual_data_is_not_rendered(self):
		self.reader.return_value = {
			"SO-MANUAL": {"total": 0, "manual": {"total": 1, "recorded": 1, "amounts": {"USD": 90}}},
			"SO-SF": {"total": 1, "billed": 1, "booked": 1, "billed_amount": 12, "currency": "CNY"},
		}
		result = self.module.get_sales_order_freight_summary(["SO-MANUAL", "SO-SF"])
		self.reader.assert_called_once_with(["SO-MANUAL", "SO-SF"])
		self.assertEqual(list(result), ["SO-SF"])
		self.assertNotIn("manual", str(result))

	def test_permission_failures_propagate_to_native_list_owner(self):
		self.reader.side_effect = PermissionError("Shipment")
		with self.assertRaises(PermissionError):
			self.module.get_sales_order_freight_summary(["SO-1"])


if __name__ == "__main__":
	unittest.main()
