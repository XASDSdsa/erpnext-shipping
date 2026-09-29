"""Regression tests for install-time compatibility with ERPNext Shipment."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def _package(name):
	module = types.ModuleType(name)
	module.__path__ = []
	return module


def load_install():
	frappe = _package("frappe")
	frappe.db = Mock()
	frappe.get_all = Mock(return_value=[])
	frappe.get_meta = Mock(return_value=None)
	frappe.clear_cache = Mock()
	frappe.utils = _package("frappe.utils")
	frappe.utils.cint = lambda value: int(value or 0)
	frappe.utils.flt = lambda value: float(value or 0)

	custom = _package("frappe.custom")
	doctype = _package("frappe.custom.doctype")
	custom_field_package = _package("frappe.custom.doctype.custom_field")
	custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
	custom_field.create_custom_fields = Mock()
	property_setter_package = _package("frappe.custom.doctype.property_setter")
	property_setter = types.ModuleType("frappe.custom.doctype.property_setter.property_setter")
	property_setter.make_property_setter = Mock()

	modules = {
		"frappe": frappe,
		"frappe.utils": frappe.utils,
		"frappe.custom": custom,
		"frappe.custom.doctype": doctype,
		"frappe.custom.doctype.custom_field": custom_field_package,
		"frappe.custom.doctype.custom_field.custom_field": custom_field,
		"frappe.custom.doctype.property_setter": property_setter_package,
		"frappe.custom.doctype.property_setter.property_setter": property_setter,
	}
	path = ROOT / "erpnext_shipping/sf_international/install.py"
	spec = importlib.util.spec_from_file_location("erpnext_shipping.sf_international.install", path)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, modules):
		spec.loader.exec_module(module)
	module._test_modules = modules
	return module, frappe, property_setter.make_property_setter


class InstallCompatibilityTests(unittest.TestCase):
	def test_sf_adds_only_its_own_option_to_native_provider_choices(self):
		module, frappe, make_property_setter = load_install()
		options = "\nLetMeShip\nSendCloud\n其他物流（手工登记）\nCustomer Carrier"
		frappe.get_meta.return_value = types.SimpleNamespace(get_field=lambda name: types.SimpleNamespace(fieldtype="Select", options=options))
		with patch.dict(sys.modules, module._test_modules):
			result = module.ensure_sf_shipment_metadata()
		self.assertTrue(result["changed"])
		make_property_setter.assert_called_once_with(
			"Shipment", "service_provider", "options", options + "\n顺丰国际", "Text",
			validate_fields_for_doctype=False,
		)
		frappe.db.delete.assert_not_called()
		frappe.db.set_value.assert_not_called()

	def test_existing_customer_options_keep_their_names_and_values(self):
		module, frappe, make_property_setter = load_install()
		rows = [
			{"name": "customer-options", "value": "\nCustomer Carrier\n其他物流（手工登记）"},
			{"name": "integration-options", "value": "\nExisting Carrier\n顺丰国际"},
		]
		frappe.get_all.return_value = rows
		frappe.get_meta.return_value = types.SimpleNamespace(get_field=lambda name: types.SimpleNamespace(fieldtype="Select", options=rows[0]["value"]))
		def set_value(doctype, name, property_name, value):
			next(row for row in rows if row["name"] == name)[property_name] = value
		frappe.db.set_value.side_effect = set_value
		with patch.dict(sys.modules, module._test_modules):
			self.assertTrue(module.ensure_sf_shipment_metadata()["changed"])
			self.assertFalse(module.ensure_sf_shipment_metadata()["changed"])
		frappe.db.set_value.assert_called_once_with("Property Setter", "customer-options", "value", "\nCustomer Carrier\n其他物流（手工登记）\n顺丰国际")
		self.assertEqual(rows[1]["value"], "\nExisting Carrier\n顺丰国际")
		make_property_setter.assert_not_called()
		frappe.db.delete.assert_not_called()

	def test_customer_fieldtype_is_not_overridden(self):
		module, frappe, make_property_setter = load_install()
		frappe.get_meta.return_value = types.SimpleNamespace(get_field=lambda name: types.SimpleNamespace(fieldtype="Data", options=""))
		with patch.dict(sys.modules, module._test_modules):
			self.assertFalse(module.ensure_sf_shipment_metadata()["changed"])
		frappe.get_all.assert_not_called()
		make_property_setter.assert_not_called()
		frappe.db.set_value.assert_not_called()

	def test_installer_does_not_register_manual_or_generic_shipment_metadata(self):
		module, frappe, make_property_setter = load_install()
		for name in (
			"ensure_shipment_address_section", "ensure_shipment_full_width_layout",
			"ensure_parcel_editable_before_booking", "ensure_shipment_status_options",
			"ensure_waybill_fields_editable_on_submit", "ensure_freight_amount_editable",
			"ensure_sales_user_can_submit_shipment",
			"ensure_sales_order_native_list",
		):
			self.assertFalse(hasattr(module, name), name)
		self.assertNotIn("ensure_sales_order_native_list", module.after_install.__code__.co_names)
		self.assertFalse((ROOT / "sf_international/manual_shipping_install.py").exists())


if __name__ == "__main__":
	unittest.main()
