# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

"""Apply native field permissions to report output with identifiable source fields."""

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy

import frappe
from frappe.model import get_permitted_fields
from frappe.permissions import get_allowed_docs_for_doctype, get_user_permissions


@contextmanager
def _report_user(user):
	if not user or user == frappe.local.session.user:
		yield
		return
	keys = (
		"session",
		"cache",
		"form_dict",
		"jenv_restricted",
		"jenv_unrestricted",
		"role_permissions",
		"new_doc_templates",
		"user_perms",
		"request_cache",
	)
	previous = {key: getattr(frappe.local, key) for key in keys if hasattr(frappe.local, key)}
	try:
		frappe.local.session = previous["session"].copy()
		frappe.set_user(user)
		frappe.local.request_cache = defaultdict(dict)
		yield
	finally:
		for key in keys:
			if key in previous:
				setattr(frappe.local, key, previous[key])
			elif hasattr(frappe.local, key):
				delattr(frappe.local, key)


class ReportFieldPermissions:
	def __init__(self, report, user=None, source_columns=()):
		self.report = report
		self.ref_doctype = report.ref_doctype
		self.user = user or frappe.session.user
		self._restricted = {}
		meta = frappe.get_meta(self.ref_doctype)
		self.hidden_fields = set(self.restricted_fields(self.ref_doctype))
		for table in meta.get_table_fields():
			self.hidden_fields.update(self.restricted_fields(table.options, self.ref_doctype))
		# Saved presentation columns must not replace the original report's permission source.
		for column in source_columns:
			if not self.is_permitted(column):
				self.hidden_fields.update(self.column_names(column))

	def restricted_fields(self, doctype, parenttype=None):
		key = doctype, parenttype
		if key not in self._restricted:
			meta = frappe.get_meta(doctype)
			permitted = set(
				get_permitted_fields(doctype, parenttype=parenttype, user=self.user, permission_type="read")
			)
			self._restricted[key] = {field.fieldname for field in meta.fields} - permitted
		return self._restricted[key]

	@staticmethod
	def column_dict(column):
		if isinstance(column, str):
			return {"fieldname": frappe.scrub(column.split(":")[0])}
		if "fieldname" not in column and column.get("label"):
			return column | {"fieldname": frappe.scrub(column["label"])}
		return column

	def column_names(self, column):
		column = self.column_dict(column)
		names = {name for name in (column.get("fieldname"), column.get("id")) if name}
		if column.get("link_field") and column.get("fieldname"):
			names.add(column["fieldname"].split("-")[0])
		return names

	def is_permitted(self, column):
		column = self.column_dict(column)
		if self.column_names(column) & self.hidden_fields:
			return False
		if doctype := column.get("doctype"):
			parenttype = column.get("parenttype")
			if not parenttype and frappe.get_meta(doctype).istable:
				parenttype = self.ref_doctype
			return not self.column_names(column) & self.restricted_fields(doctype, parenttype)
		# Aliases and calculated columns need source metadata or a report-specific policy.
		return True

	def filter_data(self, data, filters=None):
		columns = data.get("columns") or []
		hidden_indexes = {index for index, column in enumerate(columns) if not self.is_permitted(column)}
		hidden_names = self.hidden_fields.copy()
		for index in hidden_indexes:
			hidden_names.update(self.column_names(columns[index]))

		filtered = data.copy()
		redacted = bool(hidden_indexes)

		def filter_keys(value):
			nonlocal redacted
			if isinstance(value, dict):
				redacted = redacted or bool(value.keys() & hidden_names)
				return {key: filter_keys(item) for key, item in value.items() if key not in hidden_names}
			if isinstance(value, list | tuple):
				return [filter_keys(item) for item in value]
			return value

		if "columns" in data:
			filtered["columns"] = [
				column for index, column in enumerate(columns) if index not in hidden_indexes
			]
		if "result" in data:
			rows = []
			for row in data.get("result") or []:
				if isinstance(row, dict):
					row = filter_keys(row)
				elif isinstance(row, list | tuple):
					redacted = redacted or len(row) > len(columns)
					row = [
						filter_keys(value)
						for index, value in enumerate(row)
						if index < len(columns) and index not in hidden_indexes
					]
				rows.append(row)
			filtered["result"] = rows
		if redacted:
			# Aggregates and free-form messages do not identify the fields they disclose.
			for key in ("chart", "report_summary", "message"):
				if key in filtered:
					filtered[key] = None
		methods = frappe.get_hooks("report_data_filters")
		if methods:
			# Hooks can mutate nested chart/summary payloads; preserve the cached input.
			filtered = {key: value if key == "doc" else deepcopy(value) for key, value in filtered.items()}
		for method in methods:
			filtered = frappe.call(
				method, report_name=self.report.name, result=filtered, user=self.user, filters=filters
			)
		return filtered


def get_permitted_report_columns(report, columns, user=None):
	permissions = ReportFieldPermissions(report, user)
	return [column for column in columns if permissions.is_permitted(column)]


def filter_report_data(report, data, user=None, filters=None):
	return ReportFieldPermissions(report, user).filter_data(data, filters)


def filter_cached_report_data(report, data, user=None, filters=None):
	from frappe.desk.query_report import add_total_row, get_filtered_data, validate_filters_permissions

	user = user or frappe.session.user
	validate_filters_permissions(report.name, filters, user)
	filtered = data.copy()
	rows = deepcopy(data.get("result") or [])
	columns = data.get("columns") or []
	# shortcut: legacy caches lack this flag; regenerate them after changing total settings.
	has_total_row = data.get(
		"add_total_row", report.add_total_row and not data.get("skip_total_row")
	) and bool(rows)
	if has_total_row:
		rows.pop()
	if rows or any(data.get(key) for key in ("chart", "report_summary", "message")):
		validate_cached_user_permissions(report, columns, rows, user, filters)
	if rows and columns:
		allowed_rows = get_filtered_data(report.ref_doctype, columns, deepcopy(rows), user)
		if rows != allowed_rows:
			for key in ("chart", "report_summary", "message"):
				if key in filtered:
					filtered[key] = None
		rows = allowed_rows
	if has_total_row and rows:
		rows = add_total_row(rows, columns)
	if "result" in data:
		filtered["result"] = rows
	return filter_report_data(report, filtered, user, filters)


def validate_cached_user_permissions(report, columns, rows, user, filters):
	from frappe.desk.query_report import get_column_as_dict, get_linked_doctypes

	user_permissions = get_user_permissions(user)
	if not user_permissions:
		return
	meta = frappe.get_meta(report.ref_doctype)
	source_doctypes = {report.ref_doctype, *(table.options for table in meta.get_table_fields())}
	links = {
		field.fieldname: field.options
		for doctype in source_doctypes
		for field in frappe.get_meta(doctype).fields
		if field.fieldtype == "Link" and not field.get("ignore_user_permissions")
	}
	links.update(
		{field.fieldname: field.options for field in report.filters or [] if field.fieldtype == "Link"}
	)
	linked_doctypes = get_linked_doctypes(columns, rows)
	column_fields = {}
	for column in columns:
		column = get_column_as_dict(column)
		if column.get("fieldtype") == "Link" and column.get("options") in linked_doctypes:
			column_fields.setdefault(column["options"], set()).add(column["fieldname"])
	filters = frappe.parse_json(filters) or {}
	for doctype, permissions in user_permissions.items():
		if doctype not in source_doctypes | set(links.values()) or not any(
			not permission.get("applicable_for") or permission.get("applicable_for") in source_doctypes
			for permission in permissions
		):
			continue
		required_links = [
			fieldname for fieldname, linked_doctype in links.items() if linked_doctype == doctype
		]
		covered = bool(required_links) or doctype in linked_doctypes
		allowed_by_source = [get_allowed_docs_for_doctype(permissions, source) for source in source_doctypes]
		for fieldname in required_links:
			if fieldname in column_fields.get(doctype, set()):
				continue
			value = filters.get(fieldname)
			if (
				isinstance(value, str)
				and value
				and not any(allowed and value not in allowed for allowed in allowed_by_source)
			):
				frappe.has_permission(doctype, "read", value, user=user, throw=True)
			else:
				covered = False
		if not covered:
			frappe.throw(
				frappe._(
					"This cached report cannot be checked against your current User Permissions. Run the report again."
				),
				frappe.PermissionError,
			)
