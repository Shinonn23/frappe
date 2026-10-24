# Copyright (c) 2022, Frappe Technologies and Contributors
# See license.txt

import frappe
from frappe.core.doctype.doctype.test_doctype import new_doctype
from frappe.core.doctype.document_naming_settings import series_sync
from frappe.core.doctype.document_naming_settings.document_naming_settings import (
	DocumentNamingSettings,
)
from frappe.model.naming import NamingSeries, get_default_naming_series
from frappe.tests import IntegrationTestCase
from frappe.utils import cint


class TestNamingSeries(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ns_doctype = (
			new_doctype(
				fields=[
					{
						"label": "Series",
						"fieldname": "naming_series",
						"fieldtype": "Select",
						"options": f"\n{frappe.generate_hash()}-.###",
					}
				],
				autoname="naming_series:",
				is_submittable=1,
			)
			.insert()
			.name
		)

	def setUp(self):
		self.dns: DocumentNamingSettings = frappe.get_doc("Document Naming Settings")

	def tearDown(self):
		frappe.db.rollback()

	def get_valid_serieses(self):
		VALID_SERIES = ["SINV-", "SI-.{field}.", "SI-#.###", ""]
		existing_series = self.dns.get_transactions_and_prefixes()["prefixes"]
		return VALID_SERIES + existing_series

	def test_naming_preview(self):
		self.dns.transaction_type = self.ns_doctype

		self.dns.try_naming_series = "AXBZ.####"
		serieses = self.dns.preview_series().split("\n")
		self.assertEqual(["AXBZ0001", "AXBZ0002", "AXBZ0003"], serieses)

		self.dns.try_naming_series = "AXBZ-.{currency}.-"
		serieses = self.dns.preview_series().split("\n")

	def test_get_transactions(self):
		naming_info = self.dns.get_transactions_and_prefixes()
		self.assertIn(self.ns_doctype, naming_info["transactions"])

		existing_naming_series = frappe.get_meta(self.ns_doctype).get_field("naming_series").options

		for series in existing_naming_series.split("\n"):
			self.assertIn(NamingSeries(series).get_prefix(), naming_info["prefixes"])

	def test_default_naming_series(self):
		self.assertIsNone(get_default_naming_series("DocType"))

	def test_updates_naming_options(self):
		self.dns.transaction_type = self.ns_doctype
		test_series = "KOOHBEW.###"
		self.dns.naming_series_options = self.dns.get_options() + "\n" + test_series
		self.dns.update_series()
		self.assertIn(test_series, frappe.get_meta(self.ns_doctype).get_naming_series_options())

	def test_update_series_counter(self):
		for series in self.get_valid_serieses():
			if not series:
				continue
			self.dns.prefix = series
			current_count = cint(self.dns.get_current())
			new_count = self.dns.current_value = current_count + 1
			self.dns.update_series_start()

			self.assertEqual(self.dns.get_current(), new_count, f"Incorrect update for {series}")

	def test_amended_naming(self):
		self.dns.amend_naming_override = []
		self.dns.default_amend_naming = "Amend Counter"
		self.dns.update_amendment_rule()

		submittable_doc = frappe.get_doc(
			doctype=self.ns_doctype, some_fieldname="test doc with submit"
		).submit()
		submittable_doc.cancel()

		amended_doc = frappe.get_doc(
			doctype=self.ns_doctype, some_fieldname="test doc with submit", amended_from=submittable_doc.name
		).insert()

		self.assertIn(submittable_doc.name, amended_doc.name)
		amended_doc.delete()

		self.dns.default_amend_naming = "Default Naming"
		self.dns.update_amendment_rule()

		new_amended_doc = frappe.get_doc(
			doctype=self.ns_doctype, some_fieldname="test doc with submit", amended_from=submittable_doc.name
		).insert()
		self.assertNotIn(submittable_doc.name, new_amended_doc.name)


class TestSeriesSync(IntegrationTestCase):
	"""The counter of a series follows the names that exist (series_sync.py)."""

	def setUp(self):
		self.series = f"{frappe.generate_hash(length=6)}-.###"
		self.doctype = (
			new_doctype(
				fields=[
					{
						"label": "Series",
						"fieldname": "naming_series",
						"fieldtype": "Select",
						"options": f"\n{self.series}",
					}
				],
				autoname="naming_series:",
			)
			.insert()
			.name
		)
		self.prefix = NamingSeries(self.series).get_prefix()

	def tearDown(self):
		frappe.db.rollback()

	def make(self, number):
		doc = frappe.new_doc(self.doctype)
		doc.naming_series = self.series
		doc.name = f"{self.prefix}{number:03d}"
		doc.flags.name_set = True
		doc.insert(set_name=doc.name)
		return doc

	def row(self):
		rows = series_sync.check(self.doctype)["rows"]
		self.assertEqual(len(rows), 1)
		return rows[0]

	def test_status_of_a_counter(self):
		self.assertEqual(series_sync.series_status(49, 51), series_sync.BEHIND)
		self.assertEqual(series_sync.series_status(51, 51), series_sync.OK)
		self.assertEqual(series_sync.series_status(60, 51), series_sync.AHEAD)

	def test_a_document_renamed_past_the_counter_makes_the_series_behind(self):
		first = self.make(1)
		NamingSeries(self.series).update_counter(1)
		self.assertEqual(self.row()["status"], series_sync.OK)

		frappe.rename_doc(self.doctype, first.name, f"{self.prefix}051", force=True)

		row = self.row()
		self.assertEqual((row["counter"], row["highest"], row["status"]), (1, 51, series_sync.BEHIND))

	def test_sync_raises_the_counter_to_the_highest_number_and_the_next_name_is_free(self):
		self.make(51)
		NamingSeries(self.series).update_counter(50)

		done = series_sync.sync([{"doctype": self.doctype, "prefix": self.prefix}])

		self.assertEqual([row["highest"] for row in done], [51])
		self.assertEqual(self.row()["status"], series_sync.OK)
		doc = frappe.new_doc(self.doctype)
		doc.naming_series = self.series
		doc.insert()
		self.assertEqual(doc.name, f"{self.prefix}052")

	def test_a_counter_ahead_of_the_names_is_left_alone(self):
		self.make(1)
		NamingSeries(self.series).update_counter(9)
		self.assertEqual(series_sync.sync(), [])
		self.assertEqual(self.row()["counter"], 9)

	def test_only_the_selected_series_are_synced(self):
		self.make(51)
		NamingSeries(self.series).update_counter(50)
		self.assertEqual(series_sync.sync([]), [])
		self.assertEqual(self.row()["status"], series_sync.BEHIND)
