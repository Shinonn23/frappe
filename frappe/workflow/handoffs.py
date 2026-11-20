# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

"""Core Handoff Rule cache and safe condition evaluation."""

from __future__ import annotations

import hashlib

import frappe

CACHE_KEY = "my_space_handoff_rules"
ERROR_CACHE_KEY = "my_space_handoff_error::"
ERROR_LOG_SECONDS = 24 * 60 * 60


def _rule_data(rule):
	return {
		"name": rule.name,
		"step": rule.get("step"),
		"source_doctype": rule.source_doctype,
		"condition": rule.condition,
		"action_label": rule.action_label,
		"target_doctype": rule.target_doctype,
		"make_method": rule.make_method,
		"date_field": rule.date_field,
		"department_field": rule.department_field,
		"roles": [row.role for row in rule.get("roles") or []],
		"departments": [row.department for row in rule.get("departments") or []],
	}


def _load_rules():
	if not frappe.db.table_exists("Handoff Rule"):
		return {}
	rules = {}
	for name in frappe.get_all("Handoff Rule", filters={"enabled": 1}, pluck="name", order_by="name"):
		data = _rule_data(frappe.get_doc("Handoff Rule", name))
		rules.setdefault(data["source_doctype"], []).append(data)
	return rules


def get_rules(doctype=None):
	"""Return enabled Handoff Rules grouped by source DocType (cached), or the list for one DocType."""
	try:
		rules = frappe.cache.get_value(CACHE_KEY, generator=_load_rules)
	except ImportError:
		if not frappe.flags.in_migrate:
			raise
		rules = {}
	return rules.get(doctype, []) if doctype else rules


def clear_rule_cache():
	frappe.cache.delete_value(CACHE_KEY)


def _log_condition_error(rule):
	digest = hashlib.sha256(f"{rule['name']}\0{rule['condition'] or ''}".encode()).hexdigest()[:24]
	key = ERROR_CACHE_KEY + digest
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=ERROR_LOG_SECONDS)
	frappe.log_error(
		title=f"Handoff Rule condition failed: {rule['name']}",
		message=f"Rule {rule['name']} could not evaluate its condition:\n{rule['condition']}\n\n"
		+ frappe.get_traceback(),
	)


def _db_get_value(*args, **kwargs):
	return frappe.db.get_value(*args, **kwargs)


def _db_select(query, *args, **kwargs):
	"""A select only: a condition reads, it never writes."""
	if not str(query).lstrip().lower().startswith("select"):
		frappe.throw("A hand-off condition may only select.")
	return frappe.db.sql(query, *args, **kwargs)


def evaluate_condition(condition, data):
	"""Evaluate a Handoff Rule condition safely against ``data`` (``doc.as_dict()``)."""
	namespace = {"frappe": frappe._dict(db=frappe._dict(get_value=_db_get_value, sql=_db_select))}
	return frappe.safe_eval(condition, namespace, {"doc": data})


def matches(rule, data):
	"""Return whether the rule's condition holds for ``data``; an error counts as False."""
	try:
		return bool(evaluate_condition(rule["condition"], data))
	except Exception:
		_log_condition_error(rule)
		return False


def close_rule_tasks(rule_name: str):
	"""Invoke registered hooks to close open Handoff tasks for a disabled or deleted rule."""
	for hook in frappe.get_hooks("my_work_handoff_close_rule_tasks"):
		frappe.get_attr(hook)(rule_name)
