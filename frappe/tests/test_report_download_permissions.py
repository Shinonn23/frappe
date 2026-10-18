# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import gzip
import json
import logging
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
	from frappe.core.doctype.file import file as file_module
	from frappe.core.doctype.prepared_report import prepared_report
	from frappe.desk import report_permissions
	from frappe.desk.doctype.dashboard_chart import dashboard_chart

from frappe.utils.dashboard import cache_source


class TestPreparedReportDownloadPermissions(TestCase):
	def setUp(self):
		self.report = frappe._dict(name="Private Report", ref_doctype="Order")
		self.data = {
			"columns": [{"fieldname": "qty"}, {"fieldname": "secret"}],
			"result": [{"qty": 3, "secret": 100}],
		}
		self.filtered = {"columns": [{"fieldname": "qty"}], "result": [{"qty": 3}]}
		self.doc = Mock(report_name="Private Report", filters='{"mode":"Value"}')
		self.doc._read_prepared_data.return_value = (json.dumps(self.data).encode(), "private.json.gz")
		self.doc._parse_prepared_data.side_effect = json.loads
		self.doc.has_permission.return_value = True
		self.enterContext(patch.object(frappe, "get_doc", return_value=self.report))
		self.enterContext(patch.object(frappe, "has_permission", return_value=True))
		self.get_report = self.enterContext(
			patch("frappe.desk.query_report.get_report_doc", return_value=self.report)
		)
		self.filter = self.enterContext(
			patch.object(report_permissions, "filter_cached_report_data", return_value=self.filtered)
		)

	def test_download_endpoint_returns_filtered_bytes_and_original_filename(self):
		with patch("frappe.desk.query_report.get_report_doc", return_value=self.report):
			content, filename = prepared_report.PreparedReport.get_prepared_data(self.doc, with_file_name=True)
		self.doc.check_permission.assert_called_once_with("read")
		self.assertEqual(json.loads(content), self.filtered)
		self.assertEqual(filename, "private.json.gz")
		self.filter.assert_called_once_with(self.report, self.data, filters=self.doc.filters)

	def test_prepared_read_permission_is_required_before_any_cache_read(self):
		self.doc.check_permission.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			prepared_report.PreparedReport.get_prepared_data(self.doc)
		self.doc._read_prepared_data.assert_not_called()

	def test_raw_download_rechecks_actual_viewer_and_blocks_redacted_cache(self):
		self.assertFalse(prepared_report.PreparedReport.can_download_raw_data(self.doc, "reader@example.com"))
		self.filter.assert_called_once_with(
			self.report, self.data, user="reader@example.com", filters=self.doc.filters
		)
		self.doc.has_permission.assert_called_once_with("read", user="reader@example.com")

	def test_unrestricted_viewer_can_download_original_raw_cache(self):
		self.filter.return_value = self.data
		self.assertTrue(prepared_report.PreparedReport.can_download_raw_data(self.doc, "viewer@example.com"))
		self.get_report.assert_called_with(self.doc.report_name, user="viewer@example.com")

	def test_report_role_revocation_blocks_raw_cache_before_reading(self):
		self.get_report.side_effect = frappe.PermissionError
		self.assertFalse(prepared_report.PreparedReport.can_download_raw_data(self.doc, "reader@example.com"))
		self.doc._read_prepared_data.assert_not_called()

	def test_old_requested_compressed_payload_is_checked_instead_of_new_primary_attachment(self):
		self.doc._read_prepared_data.return_value = (json.dumps(self.filtered).encode(), "new.json.gz")
		self.assertFalse(prepared_report.PreparedReport.can_download_raw_data(
			self.doc, "reader@example.com", content=json.dumps(self.data).encode()
		))
		self.doc._read_prepared_data.assert_not_called()

	def test_cache_with_unverifiable_row_permissions_is_not_downloadable(self):
		self.filter.side_effect = frappe.PermissionError
		self.assertFalse(prepared_report.PreparedReport.can_download_raw_data(self.doc, "reader@example.com"))

	def test_stale_csv_is_denied_even_when_new_json_no_longer_contains_secret(self):
		doc = frappe._dict(file_url="/private/files/old.csv", content_hash="csv-content",
			attached_to_doctype="Prepared Report", attached_to_name="PREPARED-1")
		with patch.object(prepared_report, "_prepared_report_file_parents", return_value=[]):
			self.assertFalse(prepared_report.has_prepared_report_file_permission(doc, "reader@example.com"))
		self.filter.assert_not_called()

	def test_legacy_positional_cache_keeps_columns_available_for_redaction(self):
		doc = Mock()
		doc.get.return_value = '["Secret:Currency", "Qty:Int"]'
		self.assertEqual(
			prepared_report.PreparedReport._parse_prepared_data(doc, b"[[100, 3]]"),
			{"columns": ["Secret:Currency", "Qty:Int"], "result": [[100, 3]]},
		)

	def test_prepared_file_gate_precedes_owner_public_and_share_shortcuts(self):
		doc = frappe._dict(owner="reader@example.com", is_private=0)
		with patch.object(prepared_report, "has_prepared_report_file_permission", return_value=False):
			self.assertFalse(file_module.has_permission(doc, "read", "reader@example.com"))

	def test_owner_cannot_detach_or_delete_restricted_prepared_file_to_expose_a_copy(self):
		original = frappe._dict(name="FILE-1", attached_to_doctype="Prepared Report", attached_to_name="PREPARED-1")
		detached = frappe._dict(name="FILE-1", owner="reader@example.com", attached_to_doctype=None)
		with (
			patch.object(frappe, "db", SimpleNamespace(exists=Mock(return_value=True))),
			patch.object(frappe, "get_doc", return_value=original),
			patch.object(prepared_report, "has_prepared_report_file_permission", return_value=False) as check,
		):
			for ptype in ("write", "delete"):
				with self.subTest(ptype=ptype):
					self.assertFalse(file_module.has_permission(detached, ptype, "reader@example.com"))
		check.assert_called_with(original, "reader@example.com")

	def test_completed_parent_cannot_be_deleted_after_cached_data_access_is_revoked(self):
		doc = Mock(status="Completed")
		doc.can_download_raw_data.return_value = False
		with (
			patch.object(frappe, "session", frappe._dict(user="reader@example.com")),
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "throw", side_effect=frappe.PermissionError),
			patch.object(prepared_report, "_", side_effect=lambda message: message),
		):
			with self.assertRaises(frappe.PermissionError):
				prepared_report.delete_prepared_reports([{"name": "PREPARED-1"}])
		doc.delete.assert_not_called()

	def test_queued_parent_and_admin_cleanup_do_not_require_cached_data(self):
		for user, status in (("reader@example.com", "Queued"), ("Administrator", "Completed")):
			with self.subTest(user=user, status=status):
				doc = Mock(status=status)
				with (
					patch.object(frappe, "session", frappe._dict(user=user)),
					patch.object(frappe, "get_doc", return_value=doc),
				):
					prepared_report.delete_prepared_reports([{"name": "PREPARED-1"}])
				doc.can_download_raw_data.assert_not_called()
				doc.delete.assert_called_once_with(ignore_permissions=True, delete_permanently=True)

	def test_duplicate_file_record_cannot_bypass_original_prepared_report(self):
		doc = frappe._dict(
			file_url="/private/files/copied.json.gz", content_hash="same-content",
			attached_to_doctype=None, attached_to_name=None,
		)
		content = json.dumps(self.data).encode()
		doc.get_content = Mock(return_value=gzip.compress(content))
		prepared = Mock()
		prepared.can_download_raw_data.return_value = False
		with (
			patch.object(prepared_report, "_prepared_report_file_parents", return_value=["PREPARED-1"]),
			patch.object(frappe, "get_doc", return_value=prepared),
		):
			self.assertFalse(prepared_report.has_prepared_report_file_permission(doc, "reader@example.com"))
		prepared.can_download_raw_data.assert_called_once_with(
			user="reader@example.com", content=content
		)


class TestDashboardCachePermissions(TestCase):
	def setUp(self):
		self.chart = frappe._dict(name="Private Chart", check_permission=Mock())
		self.cache = self.enterContext(patch.object(frappe, "cache", Mock()))
		self.cache.get_value.return_value = '{"values": [100]}'
		self.enterContext(patch.object(frappe, "get_doc", return_value=self.chart))
		self.session = self.enterContext(patch.object(frappe, "session", frappe._dict(user="reader@example.com")))
		self.function = Mock(return_value={"values": [3]})
		self.get = cache_source(self.function)

	def test_cached_results_check_chart_access_and_use_current_user_key(self):
		self.assertEqual(self.get(chart_name=self.chart.name), {"values": [100]})
		self.chart.check_permission.assert_called_once_with("read")
		self.cache.get_value.assert_called_once_with("chart-data:reader@example.com:Private Chart")
		self.session.user = "other@example.com"
		self.get(chart_name=self.chart.name)
		self.cache.get_value.assert_called_with("chart-data:other@example.com:Private Chart")

	def test_revoked_chart_permission_blocks_cached_results(self):
		self.chart.check_permission.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			self.get(chart_name=self.chart.name)
		self.cache.get_value.assert_not_called()
		self.function.assert_not_called()

	def test_serialized_persisted_chart_rechecks_read_before_cache_or_custom_source(self):
		self.chart.check_permission.side_effect = frappe.PermissionError
		with patch.object(frappe, "db", SimpleNamespace(exists=Mock(return_value=True))):
			for no_cache in (0, 1):
				with self.subTest(no_cache=no_cache), self.assertRaises(frappe.PermissionError):
					self.get(chart='{"name":"Private Chart"}', no_cache=no_cache)
		self.cache.get_value.assert_not_called()
		self.function.assert_not_called()

	def test_unsaved_preview_keeps_permission_checked_native_query_path(self):
		with patch.object(frappe, "db", SimpleNamespace(exists=Mock(return_value=False))):
			args = {"chart": '{"name":"new-chart"}', "no_cache": 1, "filters": {"company": "Allowed"}}
			self.assertEqual(self.get(**args), {"values": [3]})
		self.function.assert_called_once_with(**args)
		self.chart.check_permission.assert_not_called()

	def test_no_cache_preserves_filters_and_date_options(self):
		args = {"chart_name": self.chart.name, "no_cache": 1, "filters": {"company": "Allowed"},
			"from_date": "2026-01-01", "to_date": "2026-02-01", "timespan": "Select Date Range"}
		self.assertEqual(self.get(**args), {"values": [3]})
		self.function.assert_called_once_with(**args)
		self.cache.get_value.assert_not_called()

	def test_report_and_custom_sources_rerun_permissions_instead_of_reusing_cached_values(self):
		for chart_type in ("Report", "Custom"):
			with self.subTest(chart_type=chart_type):
				self.chart.chart_type = chart_type
				self.assertEqual(self.get(chart_name=self.chart.name), {"values": [3]})
		self.cache.get_value.assert_not_called()

	def test_heatmap_uses_permission_checked_list_query_with_parent(self):
		chart = frappe._dict(chart_type="Sum", document_type="Order Item", parent_document_type="Order",
			based_on="creation", value_based_on="qty")
		with (
			patch.object(frappe, "db", SimpleNamespace(db_type="mariadb")),
			patch.object(frappe, "get_list", return_value=[(100, 3)]) as get_list,
			patch.object(frappe, "get_all") as get_all,
		):
			result = dashboard_chart.get_heatmap_chart_config(chart, [], 2026)
		self.assertEqual(result["dataPoints"], {100: 3})
		get_list.assert_called_once()
		self.assertEqual(get_list.call_args.kwargs["parent_doctype"], "Order")
		get_all.assert_not_called()
