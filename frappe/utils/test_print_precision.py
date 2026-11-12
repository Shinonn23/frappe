"""Offline checks for per-document print precision and the legacy print lifecycle."""

import unittest
from unittest.mock import Mock, patch

import frappe
from frappe.utils.formatters import format_value
from frappe.utils.number_format import NumberFormat


class TestPrintPrecision(unittest.TestCase):
	def setUp(self):
		self.doc = frappe._dict(flags=frappe._dict(print_precision=2))
		self.enterContext(patch("frappe.get_system_settings", return_value=None))
		self.enterContext(
			patch("frappe.utils.data.get_number_format", return_value=NumberFormat.from_string("#,###.##"))
		)
		self.enterContext(patch("frappe.utils.formatters.get_field_currency", return_value=None))
		self.enterContext(
			patch("frappe.utils.formatters.get_field_precision", side_effect=lambda df, doc: df.precision or 5)
		)

	def test_float_rounding_is_print_only(self):
		df = frappe._dict(fieldtype="Float", precision=5)
		for value, expected in ((1234.567, "1,234.57"), (2, "2.00"), (0, "0.00"), (-1.236, "-1.24")):
			with self.subTest(value=value):
				self.assertEqual(format_value(value, df, self.doc), expected)
		self.assertEqual(df.precision, 5)
		self.assertEqual(format_value(1.23456, df), "1.23456")
		self.assertEqual(format_value(2, df), "2")

	def test_currency_keeps_symbol_and_two_decimals(self):
		df = frappe._dict(fieldtype="Currency", precision=5)
		db = Mock()
		db.get_default.return_value = "THB"
		db.get_value.side_effect = lambda doctype, name, field, **kw: "฿" if field == "symbol" else False
		with (
			patch.object(frappe, "db", db, create=True),
			patch("frappe.defaults.get_global_default", return_value=None),
		):
			self.assertEqual(format_value(1234.567, df, self.doc), "฿ 1,234.57")
		self.assertEqual(df.precision, 5)

	def test_docfield_object_is_copied(self):
		df = Mock(precision=5)
		df.get.side_effect = {"fieldtype": "Float", "precision": 5}.get
		df.as_dict.return_value = frappe._dict(fieldtype="Float", precision=5)
		self.assertEqual(format_value(1.23456, df, self.doc), "1.23")
		self.assertEqual(df.precision, 5)

	def test_non_numeric_fields_and_hidden_zero_keep_native_behavior(self):
		self.assertEqual(format_value(0, frappe._dict(fieldtype="Float", print_hide_if_no_value=1), self.doc), "")
		self.assertEqual(format_value(7, "Int", self.doc), "7")
		self.assertEqual(format_value(1.23456, "Percent", self.doc), "1.23%")
		self.assertEqual(format_value("INV-1.23456", "Data", self.doc), "INV-1.23456")
		self.assertEqual(format_value(None, "Int", self.doc), "")
		with patch("frappe.utils.formatters.formatdate", return_value="11/11/2026") as date:
			self.assertEqual(format_value("2026-11-11", "Date", self.doc), "11/11/2026")
			date.assert_called_once_with("2026-11-11")

	def test_legacy_invokes_before_print_once(self):
		from frappe.utils.weasyprint_legacy import PrintFormatGenerator

		generator = PrintFormatGenerator.__new__(PrintFormatGenerator)
		generator.doc = frappe._dict(flags=frappe._dict())
		generator.print_format = frappe._dict(margin_left=8, margin_right=8)
		generator.letterhead = None
		settings = Mock(pdf_page_size="A4", print_style=None)
		settings.as_dict.return_value = {}

		def before_print(doc, settings):
			doc.flags.in_print = True

		with (
			patch("frappe.get_doc", return_value=settings),
			patch("frappe.www.printview.run_before_print", side_effect=before_print) as hook,
		):
			generator.build_context()
			generator.build_context()
			hook.assert_called_once_with(generator.doc, {})
