# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

"""A hand-off rule: when a document is done, the next team makes the next document."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

DATE_FIELDTYPES = ("Date", "Datetime")


class HandoffRule(Document):
	def validate(self):
		meta = frappe.get_meta(self.source_doctype)
		if meta.istable or meta.issingle:
			frappe.throw(
				_("{0} cannot be the source of a hand-off: it is not a document.").format(self.source_doctype)
			)
		self.validate_fields(meta)
		self.validate_condition()
		self.validate_make_method()
		if not self.get("roles"):
			frappe.throw(
				_("Name at least one role: the task goes to the users who hold it, never to a named user.")
			)

	def validate_fields(self, meta):
		if self.date_field:
			field = meta.get_field(self.date_field)
			if not field or field.fieldtype not in DATE_FIELDTYPES:
				frappe.throw(
					_("Due Date Field {0} is not a Date or Datetime field of {1}.").format(
						self.date_field, self.source_doctype
					)
				)
		if self.department_field:
			field = meta.get_field(self.department_field)
			if not field or field.fieldtype != "Link" or field.options != "Department":
				frappe.throw(
					_("Department Field {0} is not a Link to Department on {1}.").format(
						self.department_field, self.source_doctype
					)
				)
		if self.target_doctype:
			target = frappe.get_meta(self.target_doctype)
			if target.istable or target.issingle:
				frappe.throw(_("{0} cannot be created as the next document.").format(self.target_doctype))

	def validate_condition(self):
		try:
			compile((self.condition or "").strip(), "<condition>", "eval")
		except SyntaxError as exc:
			frappe.throw(_("The condition is not a valid expression: {0}").format(exc.msg))
		try:
			from frappe.workflow.handoffs import evaluate_condition

			evaluate_condition(self.condition, frappe.new_doc(self.source_doctype).as_dict())
		except (SyntaxError, NameError, ImportError) as exc:
			frappe.throw(_("The condition cannot be evaluated: {0}").format(exc))
		except Exception:
			pass

	def validate_make_method(self):
		if not self.make_method:
			return
		try:
			method = frappe.get_attr(self.make_method)
		except Exception:
			frappe.throw(_("Make Method {0} does not exist.").format(self.make_method))
		if method not in frappe.whitelisted:
			frappe.throw(_("Make Method {0} is not a whitelisted method.").format(self.make_method))

	def on_update(self):
		"""A rule applies only to documents saved after it exists. Switching a rule off closes its open tasks."""
		self.clear_rules()
		if not cint(self.enabled):
			from frappe.workflow.handoffs import close_rule_tasks

			close_rule_tasks(self.name)

	def on_trash(self):
		from frappe.workflow.handoffs import close_rule_tasks

		self.clear_rules()
		close_rule_tasks(self.name)

	def clear_rules(self):
		"""Forget the cached rules now, and again after commit."""
		from frappe.workflow.handoffs import clear_rule_cache

		clear_rule_cache()
		frappe.db.after_commit.add(clear_rule_cache)
