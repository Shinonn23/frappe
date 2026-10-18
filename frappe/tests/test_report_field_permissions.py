# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import json
import logging
from collections import defaultdict
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.desk import report_permissions
from frappe.utils.caching import request_cache


class TestReportFieldPermissions(TestCase):
	def setUp(self):
		self.report = frappe._dict(ref_doctype="Order")
		self.meta = {
			"Order": self.make_meta("name", "qty", "secret", "items", tables=["Order Item"]),
			"Order Item": self.make_meta("qty", "child_secret", istable=True),
			"Employee": self.make_meta("name", "salary"),
			"Public": self.make_meta("name", "salary", "secret"),
		}
		self.permitted = {
			"Order": ["name", "qty", "items"],
			"Order Item": ["qty"],
			"Employee": ["name"],
			"Public": ["name", "salary", "secret"],
		}
		self.enterContext(patch.object(frappe, "get_meta", side_effect=self.meta.__getitem__))
		self.hooks = self.enterContext(patch.object(frappe, "get_hooks", return_value=[]))
		self.user_permissions = self.enterContext(
			patch.object(report_permissions, "get_user_permissions", return_value={})
		)
		self.fields = self.enterContext(
			patch.object(
				report_permissions,
				"get_permitted_fields",
				side_effect=lambda doctype, **kwargs: self.permitted[doctype],
			)
		)

	@staticmethod
	def make_meta(*fields, tables=(), istable=False):
		return SimpleNamespace(
			fields=[frappe._dict(fieldname=name) for name in fields],
			istable=istable,
			get_table_fields=lambda: [frappe._dict(options=name) for name in tables],
		)

	def test_native_parent_and_child_permissions_filter_columns_and_undisplayed_keys(self):
		data = {
			"columns": [{"fieldname": "name"}, {"fieldname": "qty"}, {"fieldname": "secret"}],
			"result": [{"name": "ORDER-1", "qty": 3, "secret": 10, "child_secret": 20}],
			"chart": {"data": {"values": [10]}},
			"report_summary": [{"value": 10}],
			"message": "Private total: 10",
		}
		original = deepcopy(data)
		filtered = report_permissions.filter_report_data(self.report, data, "reader@example.com")
		self.assertEqual(filtered["columns"], [{"fieldname": "name"}, {"fieldname": "qty"}])
		self.assertEqual(filtered["result"], [{"name": "ORDER-1", "qty": 3}])
		self.assertIsNone(filtered["chart"])
		self.assertIsNone(filtered["report_summary"])
		self.assertIsNone(filtered["message"])
		self.assertEqual(data, original)
		self.fields.assert_any_call(
			"Order", parenttype=None, user="reader@example.com", permission_type="read"
		)
		self.fields.assert_any_call(
			"Order Item", parenttype="Order", user="reader@example.com", permission_type="read"
		)

	def test_historical_positional_rows_and_total_stay_aligned(self):
		data = {
			"columns": ["Name:Data", "Secret:Currency", "Qty:Int"],
			"result": [("ORDER-1", 25, 2), ["Total", 25, 2]],
		}
		original = deepcopy(data)
		filtered = report_permissions.filter_report_data(self.report, data, "reader@example.com")
		self.assertEqual(filtered["columns"], ["Name:Data", "Qty:Int"])
		self.assertEqual(filtered["result"], [["ORDER-1", 2], ["Total", 2]])
		self.assertEqual(data, original)

	def test_nested_result_values_cannot_expose_forbidden_parent_or_child_keys(self):
		data = {
			"columns": [{"fieldname": "qty"}],
			"result": [
				{
					"qty": 2,
					"details": [{"child_secret": 20, "qty": 2}],
					"extra": {"secret": 10, "name": "ORDER-1"},
				}
			],
			"chart": {"values": [20]},
		}
		original = deepcopy(data)
		filtered = report_permissions.filter_report_data(self.report, data, "reader@example.com")
		self.assertEqual(
			filtered["result"], [{"qty": 2, "details": [{"qty": 2}], "extra": {"name": "ORDER-1"}}]
		)
		self.assertIsNone(filtered["chart"])
		self.assertEqual(data, original)

	def test_legacy_label_metadata_and_unlabelled_positional_values_cannot_leak_fields(self):
		data = {
			"columns": [{"label": "Secret"}, {"fieldname": "qty"}],
			"result": [(123, 2, "unlabelled secret")],
			"chart": {"values": [123]},
		}
		filtered = report_permissions.filter_report_data(self.report, data, "reader@example.com")
		self.assertEqual(filtered["columns"], [{"fieldname": "qty"}])
		self.assertEqual(filtered["result"], [[2]])
		self.assertIsNone(filtered["chart"])
		self.assertEqual(
			report_permissions.filter_report_data(self.report, {"result": [[123]]}, "reader@example.com"),
			{"result": [[]]},
		)

	def test_unrestricted_and_calculated_columns_preserve_chart_summary_and_message(self):
		data = {
			"columns": [{"fieldname": "qty"}, {"fieldname": "calculated_total"}],
			"result": [{"qty": 2, "calculated_total": 5}],
			"chart": {"values": [5]},
			"report_summary": [{"value": 5}],
			"message": "Public",
		}
		self.assertEqual(report_permissions.filter_report_data(self.report, data, "reader@example.com"), data)

	def test_explicit_source_restricts_columns_and_cannot_override_ref_doctype_denial(self):
		columns = [
			{"fieldname": "salary", "doctype": "Employee"},
			{"fieldname": "secret", "doctype": "Public"},
			{"fieldname": "qty"},
		]
		self.assertEqual(
			report_permissions.get_permitted_report_columns(self.report, columns, "reader@example.com"),
			[{"fieldname": "qty"}],
		)

	def test_saved_presentation_metadata_cannot_override_original_source(self):
		permissions = report_permissions.ReportFieldPermissions(
			self.report, "reader@example.com", source_columns=[{"fieldname": "salary", "doctype": "Employee"}]
		)
		filtered = permissions.filter_data(
			{
				"columns": [{"fieldname": "salary", "doctype": "Public"}],
				"result": [{"salary": 123}],
			}
		)
		self.assertEqual(filtered, {"columns": [], "result": [{}]})

	def test_custom_column_conflicting_name_suffix_does_not_bypass_field_permissions(self):
		columns = [{"fieldname": "salary-employee", "doctype": "Employee", "link_field": "employee"}]
		self.assertEqual(
			report_permissions.get_permitted_report_columns(self.report, columns, "reader@example.com"), []
		)

	def test_source_child_uses_parent_permissions_and_filters_alternate_ids(self):
		columns = [{"fieldname": "child_secret", "id": "secret-column", "doctype": "Order Item"}]
		filtered = report_permissions.filter_report_data(
			self.report,
			{"columns": columns, "result": [{"child_secret": 10, "secret-column": 10, "qty": 2}]},
			"reader@example.com",
		)
		self.assertEqual(filtered, {"columns": [], "result": [{"qty": 2}]})

	def test_rechecks_native_permissions_after_role_change(self):
		data = {"columns": [{"fieldname": "secret"}], "result": [{"secret": 10}]}
		self.permitted["Order"].append("secret")
		self.assertEqual(report_permissions.filter_report_data(self.report, data, "reader@example.com"), data)
		self.permitted["Order"].remove("secret")
		self.assertEqual(
			report_permissions.filter_report_data(self.report, data, "reader@example.com"),
			{"columns": [], "result": [{}]},
		)

	def test_no_cached_payload_remains_without_result_or_columns(self):
		self.assertEqual(
			report_permissions.filter_report_data(
				self.report, {"prepared_report": True, "doc": None}, "reader@example.com"
			),
			{"prepared_report": True, "doc": None},
		)

	def test_report_policy_hooks_receive_actual_viewer_and_filtered_data(self):
		self.report.name = "Private Report"
		self.hooks.return_value = ["custom_app.report_filter"]
		data = {"columns": [{"fieldname": "secret"}], "result": [{"secret": 10}]}
		with patch.object(frappe, "call", return_value={"columns": [], "result": []}) as call:
			filtered = report_permissions.filter_report_data(
				self.report, data, "viewer@example.com", filters={"value_quantity": "Quantity"}
			)
		call.assert_called_once_with(
			"custom_app.report_filter",
			report_name="Private Report",
			user="viewer@example.com",
			filters={"value_quantity": "Quantity"},
			result={"columns": [], "result": [{}]},
		)
		self.assertEqual(filtered, {"columns": [], "result": []})

	def test_policy_hooks_cannot_mutate_cached_nested_payloads(self):
		self.report.name = "Orders"
		self.hooks.return_value = ["custom_app.report_filter"]
		data = {"columns": [{"fieldname": "qty"}], "result": [{"qty": 2}], "chart": {"values": [5]}}
		original = deepcopy(data)

		def mutate(method, *, report_name, result, user, filters):
			result["chart"]["values"].clear()
			result["columns"][0]["label"] = "Changed"
			return result

		with patch.object(frappe, "call", side_effect=mutate):
			filtered = report_permissions.filter_report_data(self.report, data, "reader@example.com")
		self.assertEqual(data, original)
		self.assertEqual(filtered["chart"], {"values": []})

	def test_generate_filters_custom_fields_before_fetch(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(
			name="Orders",
			custom_columns=[
				{"fieldname": "name"},
				{
					"fieldname": "secret",
					"doctype": "Public",
					"link_field": "name",
					"fieldtype": "Link",
					"options": "Company",
				},
			],
		)
		rows = [{"name": "ORDER-1", "secret": "Private company"}]
		with (
			patch.object(query_report, "get_report_result", return_value=([{"fieldname": "name"}], rows)),
			patch.object(query_report, "add_custom_column_data", return_value=rows) as fetch,
			patch.object(query_report, "get_filtered_data", return_value=rows) as acl,
			patch.object(frappe, "cache", Mock()),
		):
			result = query_report.generate_report_result.__wrapped__(self.report, user="reader@example.com")
		fetch.assert_called_once_with([], rows)
		self.assertEqual(acl.call_args.args[1], [{"fieldname": "name"}])
		self.assertEqual(result["result"], [{"name": "ORDER-1"}])
		self.assertEqual(result["columns"], [{"fieldname": "name"}])

	def test_run_filters_historical_prepared_output_without_changing_cached_payload(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(name="Orders", prepared_report=1, add_total_row=0)
		cached = {"columns": [{"fieldname": "secret"}], "result": [{"secret": 10}]}
		original = deepcopy(cached)
		with (
			patch.object(query_report, "validate_filters_permissions"),
			patch.object(
				query_report, "get_filtered_data", side_effect=lambda doctype, cols, rows, user: rows
			),
			patch.object(query_report, "get_report_doc", return_value=self.report),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(query_report, "get_prepared_report_result", return_value=cached),
		):
			result = query_report._run(report_name="Orders", user="reader@example.com")
		self.assertEqual(result["result"], [{}])
		self.assertEqual(result["columns"], [])
		self.assertEqual(cached, original)

	def test_generate_discards_aggregates_when_native_row_permissions_exclude_records(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(name="Orders", add_total_row=0)
		rows = [{"qty": 2}, {"qty": 3}]
		with (
			patch.object(
				query_report,
				"get_report_result",
				return_value=(
					[{"fieldname": "qty", "fieldtype": "Int"}],
					rows,
					"Total: 5",
					{"values": [5]},
					[{"value": 5}],
				),
			),
			patch.object(
				query_report, "get_filtered_data", side_effect=lambda doctype, cols, source, user: source[:1]
			),
			patch.object(frappe, "cache", Mock()),
		):
			result = query_report.generate_report_result.__wrapped__(self.report, user="reader@example.com")
		self.assertEqual(result["result"], [{"qty": 2}])
		self.assertIsNone(result["chart"])
		self.assertIsNone(result["report_summary"])
		self.assertIsNone(result["message"])

	def test_saved_presentation_cannot_hide_or_retype_source_links_used_for_row_permissions(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(name="Orders", add_total_row=0)
		source_columns = [
			{"fieldname": "company", "fieldtype": "Link", "options": "Company"},
			{"fieldname": "qty", "fieldtype": "Int"},
		]
		rows = [{"company": "Allowed", "qty": 2}, {"company": "Forbidden", "qty": 3}]

		def row_permissions(doctype, columns, data, user):
			self.assertEqual(columns, source_columns)
			return data[:1]

		for presentation in [
			[{"fieldname": "qty"}],
			[{"fieldname": "company", "fieldtype": "Data"}, {"fieldname": "qty"}],
		]:
			self.report.custom_columns = presentation
			with (
				self.subTest(columns=presentation),
				patch.object(
					query_report,
					"get_report_result",
					return_value=(source_columns, rows, "Total: 5", {"values": [5]}, [{"value": 5}]),
				),
				patch.object(query_report, "get_filtered_data", side_effect=row_permissions),
				patch.object(frappe, "cache", Mock()),
			):
				result = query_report.generate_report_result.__wrapped__(
					self.report, user="reader@example.com"
				)
			self.assertEqual(result["result"], [{"company": "Allowed", "qty": 2}])
			self.assertIsNone(result["chart"])
			self.assertIsNone(result["report_summary"])
			self.assertIsNone(result["message"])

	def test_report_builder_filters_selected_fields_but_preserves_filters_and_order(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.core.doctype.report.report import Report
		self.report.update(
			name="Orders",
			json=json.dumps({"fields": [["name", "Order"], ["secret", "Order"]]}),
			get_standard_report_columns=Mock(return_value=[["name", "Order"], ["secret", "Order"]]),
			get_standard_report_order_by=Mock(return_value=("secret desc", None, None)),
			get_standard_report_filters=Mock(return_value={"secret": 10}),
			build_standard_report_columns=lambda columns, group: [{"fieldname": name} for name, _ in columns],
		)
		with patch.object(frappe, "get_list", return_value=[("ORDER-1",)]) as query:
			columns, rows = Report.run_standard_report(self.report, {"secret": 10}, 20, "reader@example.com")
		self.assertEqual(query.call_args.kwargs["fields"], ["`tabOrder`.`name`"])
		self.assertEqual(query.call_args.kwargs["filters"], {"secret": 10})
		self.assertEqual(query.call_args.kwargs["order_by"], "secret desc")
		self.assertEqual((columns, rows), ([{"fieldname": "name"}], [["ORDER-1"]]))

	def test_report_document_checks_target_users_report_permission(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(name="Orders", is_permitted=Mock(return_value=True))
		with (
			patch.object(frappe, "get_doc", return_value=self.report),
			patch.object(frappe, "has_permission", return_value=False) as permission,
			patch.object(frappe, "_", side_effect=lambda text: text),
			patch.object(query_report, "_", side_effect=lambda text: text),
			patch.object(frappe, "throw", side_effect=frappe.PermissionError),
			self.assertRaises(frappe.PermissionError),
		):
			query_report.get_report_doc("Orders", user="recipient@example.com")
		self.report.is_permitted.assert_called_once_with(user="recipient@example.com")
		permission.assert_called_once_with("Order", "report", user="recipient@example.com")

	def test_report_get_data_denies_target_before_execution(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.core.doctype.report import report as report_module
		self.report.update(name="Orders", run_query_report=Mock())
		with (
			patch.object(frappe, "local", SimpleNamespace(session=frappe._dict(user="Administrator"))),
			patch.object(report_module, "get_report_doc", side_effect=frappe.PermissionError) as access,
			self.assertRaises(frappe.PermissionError),
		):
			report_module.Report.get_data(self.report, user="recipient@example.com")
		access.assert_called_once_with("Orders", user="recipient@example.com")
		self.report.run_query_report.assert_not_called()

	def test_report_source_uses_target_user_and_restores_request_context_on_failure(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.core.doctype.report import report as report_module
		original_session = frappe._dict(user="Administrator", sid="real-session", data={"token": "original"})
		original_form = frappe._dict(report_name="Orders")
		context = SimpleNamespace(
			session=original_session, form_dict=original_form, role_permissions={"original": True}
		)

		def execute(*args):
			self.assertEqual(frappe.local.session.user, "recipient@example.com")
			self.assertEqual(frappe.local.session.data, {})
			raise ValueError("source failed")

		self.report.update(
			name="Orders", report_type="Script Report", run_query_report=Mock(side_effect=execute)
		)
		with (
			patch.object(frappe, "local", context),
			patch.object(report_module, "get_report_doc"),
			self.assertRaisesRegex(ValueError, "source failed"),
		):
			report_module.Report.get_data(self.report, user="recipient@example.com")
		self.assertIs(context.session, original_session)
		self.assertIs(context.form_dict, original_form)
		self.assertEqual(
			context.session, {"user": "Administrator", "sid": "real-session", "data": {"token": "original"}}
		)
		self.assertEqual(context.role_permissions, {"original": True})
		self.assertFalse(hasattr(context, "user_perms"))

	def test_report_source_cannot_inherit_implicit_user_results_from_request_cache(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.core.doctype.report import report as report_module

		@request_cache
		def current_user():
			return frappe.local.session.user

		original_cache = defaultdict(dict)
		context = SimpleNamespace(session=frappe._dict(user="Administrator"), request_cache=original_cache)
		self.report.update(
			name="Orders",
			report_type="Script Report",
			run_query_report=lambda *args: ([], [{"viewer": current_user()}]),
		)
		with patch.object(frappe, "local", context), patch.object(report_module, "get_report_doc"):
			self.assertEqual(current_user(), "Administrator")
			_, rows = report_module.Report.get_data(self.report, user="recipient@example.com")
			self.assertEqual(rows, [{"viewer": "recipient@example.com"}])
			self.assertIs(context.request_cache, original_cache)
			self.assertEqual(current_user(), "Administrator")

	def test_user_match_checks_actual_viewer_instead_of_background_session(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		with patch.object(query_report, "has_unrestricted_read_access", return_value=True) as access:
			self.assertTrue(
				query_report.has_match(
					{"name": "ORDER-1"},
					{"Order": "name"},
					{"Order": [{}]},
					"Order",
					False,
					{},
					"recipient@example.com",
				)
			)
		access.assert_called_once_with(doctype="Order", user="recipient@example.com")

	def test_cached_row_permissions_run_before_field_pruning_and_recompute_totals(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(name="Orders", add_total_row=1)
		columns = [{"fieldname": "name", "fieldtype": "Data"}, {"fieldname": "qty", "fieldtype": "Int"}]
		data = {
			"columns": columns,
			"result": [["Visible", 2], ["Private", 3], ["Total", 5]],
			"chart": {"values": [5]},
			"report_summary": [{"value": 5}],
			"message": "Total: 5",
		}
		original = deepcopy(data)
		with (
			patch.object(query_report, "validate_filters_permissions") as validate,
			patch.object(query_report, "_", side_effect=lambda text: text),
			patch.object(
				query_report, "get_filtered_data", side_effect=lambda doctype, cols, rows, user: rows[:1]
			) as acl,
		):
			filtered = report_permissions.filter_cached_report_data(
				self.report, data, "recipient@example.com", filters={"company": "Allowed"}
			)
		validate.assert_called_once_with("Orders", {"company": "Allowed"}, "recipient@example.com")
		self.assertEqual(
			acl.call_args.args, ("Order", columns, [["Visible", 2], ["Private", 3]], "recipient@example.com")
		)
		self.assertEqual(filtered["result"], [["Visible", 2], ["Total", 2.0]])
		self.assertIsNone(filtered["chart"])
		self.assertIsNone(filtered["report_summary"])
		self.assertIsNone(filtered["message"])
		self.assertEqual(data, original)

	def test_cached_aggregate_without_user_permission_lineage_is_denied(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.meta["Order"].fields.append(
			frappe._dict(fieldname="company", fieldtype="Link", options="Company")
		)
		self.user_permissions.return_value = {"Company": [{"doc": "Allowed", "applicable_for": "Order"}]}
		with (
			patch.object(frappe, "_", side_effect=lambda text: text),
			patch.object(frappe, "throw", side_effect=frappe.PermissionError),
			self.assertRaises(frappe.PermissionError),
		):
			report_permissions.validate_cached_user_permissions(
				self.report, [{"fieldname": "qty"}], [{"qty": 10}], "reader@example.com", {}
			)

	def test_cached_total_flag_prevents_current_report_setting_from_removing_real_row(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.report.update(name="Orders", add_total_row=1)
		data = {"columns": [{"fieldname": "qty"}], "result": [{"qty": 2}, {"qty": 3}], "add_total_row": False}
		with (
			patch.object(query_report, "validate_filters_permissions"),
			patch.object(
				query_report, "get_filtered_data", side_effect=lambda doctype, cols, rows, user: rows
			),
		):
			self.assertEqual(
				report_permissions.filter_cached_report_data(self.report, data, "reader@example.com"), data
			)

	def test_cached_company_link_has_lineage_for_native_row_permissions(self):
		self.meta["Order"].fields.append(
			frappe._dict(fieldname="company", fieldtype="Link", options="Company")
		)
		self.user_permissions.return_value = {"Company": [{"doc": "Allowed", "applicable_for": "Order"}]}
		with patch.object(frappe, "throw") as reject:
			report_permissions.validate_cached_user_permissions(
				self.report,
				[{"fieldname": "company", "fieldtype": "Link", "options": "Company"}],
				[{"company": "Allowed"}],
				"reader@example.com",
				{},
			)
		reject.assert_not_called()

	def test_cached_stored_native_link_filter_rechecks_current_document_permission(self):
		self.meta["Order"].fields.append(
			frappe._dict(fieldname="company", fieldtype="Link", options="Company")
		)
		self.user_permissions.return_value = {"Company": [{"doc": "Allowed", "applicable_for": "Order"}]}
		with patch.object(frappe, "has_permission", return_value=True) as access:
			report_permissions.validate_cached_user_permissions(
				self.report,
				[{"fieldname": "qty"}],
				[{"qty": 10}],
				"reader@example.com",
				{"company": "Allowed"},
			)
		access.assert_called_once_with("Company", "read", "Allowed", user="reader@example.com", throw=True)

	def test_cached_stored_filter_cannot_bypass_permissions_scoped_to_source_doctype(self):
		self.meta["Order"].fields.append(
			frappe._dict(fieldname="company", fieldtype="Link", options="Company")
		)
		self.user_permissions.return_value = {"Company": [{"doc": "Allowed", "applicable_for": "Order"}]}
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "_", side_effect=lambda text: text),
			patch.object(frappe, "throw", side_effect=frappe.PermissionError),
			self.assertRaises(frappe.PermissionError),
		):
			report_permissions.validate_cached_user_permissions(
				self.report,
				[{"fieldname": "qty"}],
				[{"qty": 10}],
				"reader@example.com",
				{"company": "Forbidden"},
			)

	def test_cached_filters_must_cover_every_link_to_the_restricted_doctype(self):
		self.meta["Order"].fields.extend(
			[
				frappe._dict(fieldname="billing_address", fieldtype="Link", options="Address"),
				frappe._dict(fieldname="shipping_address", fieldtype="Link", options="Address"),
			]
		)
		self.user_permissions.return_value = {"Address": [{"doc": "Allowed", "applicable_for": "Order"}]}
		for filters in [
			{"billing_address": "Allowed", "shipping_address": "Forbidden"},
			{"billing_address": "Allowed"},
		]:
			with (
				self.subTest(filters=filters),
				patch.object(frappe, "has_permission", return_value=True),
				patch.object(frappe, "_", side_effect=lambda text: text),
				patch.object(frappe, "throw", side_effect=frappe.PermissionError),
				self.assertRaises(frappe.PermissionError),
			):
				report_permissions.validate_cached_user_permissions(
					self.report, [{"fieldname": "qty"}], [{"qty": 10}], "reader@example.com", filters
				)

	def test_native_row_permissions_check_both_links_to_same_doctype(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
		self.meta["Order"].get_masked_fields = lambda: []
		columns = [
			{"fieldname": "from_warehouse", "fieldtype": "Link", "options": "Warehouse"},
			{"fieldname": "to_warehouse", "fieldtype": "Link", "options": "Warehouse"},
		]
		for rows in [
			[
				{"from_warehouse": "Forbidden", "to_warehouse": "Allowed"},
				{"from_warehouse": "Allowed", "to_warehouse": "Allowed"},
			],
			[{"from_warehouse": "Forbidden", "to_warehouse": None}],
			[("Forbidden", "Allowed"), ("Allowed", "Allowed")],
		]:
			with (
				self.subTest(rows=rows),
				patch.object(frappe, "share", SimpleNamespace(get_shared=Mock(return_value=[]))),
				patch.object(frappe, "db", SimpleNamespace(exists=lambda doctype, value: bool(value))),
				patch.object(query_report, "get_role_permissions", return_value={}),
				patch.object(
					query_report,
					"get_user_match_filters",
					return_value={"Warehouse": [{"Warehouse": ["Allowed"]}]},
				),
				patch.object(query_report, "has_unrestricted_read_access", return_value=True),
			):
				filtered = query_report.get_filtered_data("Order", columns, rows, "reader@example.com")
			self.assertEqual(filtered, [rows[-1]] if len(rows) > 1 else [])

	def test_builder_columns_identify_business_source_instead_of_docfield_container(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.core.doctype.report.report import Report
		self.meta["Order"].get_field = lambda name: SimpleNamespace(
			as_dict=lambda: {
				"doctype": "DocField",
				"fieldname": name,
				"fieldtype": "Int",
			}
		)
		columns = Report.build_standard_report_columns(self.report, [["qty", "Order"]], None)
		self.assertEqual(columns[0]["doctype"], "Order")

	def test_native_cached_positional_rows_support_field_masking_and_shared_documents(self):
		with patch.object(frappe, "logger", return_value=logging.getLogger(__name__)):
			from frappe.desk import query_report
			from frappe.model import db_query
		self.meta["Order"].get_masked_fields = lambda: [frappe._dict(fieldname="secret")]
		columns = [
			{"fieldname": "name", "fieldtype": "Link", "options": "Order"},
			{"fieldname": "secret", "fieldtype": "Data"},
		]
		for row in [["ORDER-1", "sensitive"], ("ORDER-1", "sensitive")]:
			with (
				self.subTest(row=row),
				patch.object(frappe, "share", SimpleNamespace(get_shared=Mock(return_value=["ORDER-1"]))),
				patch.object(query_report, "get_role_permissions", return_value={}),
				patch.object(
					query_report, "get_user_match_filters", return_value={"Order": [{"Order": ["Other"]}]}
				),
				patch.object(db_query, "mask_field_value", return_value="***"),
			):
				filtered = query_report.get_filtered_data("Order", columns, [row], "reader@example.com")
			self.assertEqual(filtered, [["ORDER-1", "***"]])
