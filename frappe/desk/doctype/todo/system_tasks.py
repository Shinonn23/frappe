# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

"""Core System ToDo lock and lifecycle helpers.

System tasks (Approval, Revision, Handoff, Inspection) are native ToDos that name their document in
``task_doctype`` / ``task_docname`` rather than ``reference_type`` / ``reference_name``, so Frappe's
assignment machinery does not treat them as manual assignments. Users cannot create, close, cancel,
reassign, or delete a system task directly; they close automatically when the underlying work finishes.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import now_datetime

SYSTEM_FLAG = "my_space_system"
TODO_FLAG = "my_space_add_todo"
DOC_TYPE = "task_doctype"
DOC_NAME = "task_docname"
APPROVAL = "Approval"
REVISION = "Revision"
HANDOFF = "Handoff"
INSPECTION = "Inspection"
KINDS = (APPROVAL, REVISION, HANDOFF, INSPECTION)
CLOSED_STATUSES = ("Closed", "Cancelled")
LOCK_MESSAGE = "This task closes by itself when the work is done. Open {0} {1} to do it."

LOCKED_FIELDS = (
	"status",
	"allocated_to",
	"assigned_by",
	"reference_type",
	"reference_name",
	"task_doctype",
	"task_docname",
	"task_kind",
	"task_action",
	"task_key",
	"handoff_rule",
	"task_from",
	"task_closed_by",
	"task_closed_on",
	"task_outcome",
)

SYSTEM_FIELDS = (
	"task_key",
	"handoff_rule",
	"task_from",
	"task_closed_by",
	"task_closed_on",
	"task_outcome",
	"task_doctype",
	"task_docname",
)


@contextmanager
def system_writes():
	"""Allow system task mutations while active."""
	previous = frappe.flags.get(SYSTEM_FLAG)
	frappe.flags[SYSTEM_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[SYSTEM_FLAG] = previous


@contextmanager
def manual_todo_writes():
	"""Allow shared manual to-do creation to write task_key and metadata while active."""
	previous = frappe.flags.get(TODO_FLAG)
	frappe.flags[TODO_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[TODO_FLAG] = previous


def task_document(row):
	"""Return (doctype, name) of the document a ToDo row refers to."""
	if row.get("task_kind") or row.get(DOC_TYPE):
		return row.get(DOC_TYPE), row.get(DOC_NAME)
	return row.get("reference_type"), row.get("reference_name")


def _may_manage():
	flags = frappe.flags
	return bool(
		flags.get(SYSTEM_FLAG)
		or flags.in_migrate
		or flags.in_patch
		or flags.in_install
		or frappe.session.user == "Administrator"
		or "System Manager" in frappe.get_roles()
	)


def _locked(doc):
	doctype, name = task_document(doc)
	frappe.throw(_(LOCK_MESSAGE).format(doctype or _("the document"), name or ""), frappe.PermissionError)


def _refuse_task_of_a_task(doc):
	"""A to-do about a system task is refused: assign the document, not its task."""
	task = frappe.db.get_value(
		"ToDo", doc.get("reference_name"), ["task_kind", DOC_TYPE, DOC_NAME], as_dict=True
	)
	if task and task.task_kind:
		frappe.throw(
			_("Assign the document {0} {1} instead of its task.").format(
				task.task_doctype, task.task_docname
			),
			frappe.PermissionError,
		)


def _changed(doc, before, fields):
	return [field for field in fields if (doc.get(field) or None) != ((before or {}).get(field) or None)]


def _stamp_closed_on(doc, before, *, now=None):
	"""Stamp task_closed_on when a ToDo moves to Closed or Cancelled, and clear it if reopened."""
	if not frappe.db.has_column("ToDo", "task_closed_on"):
		return
	if doc.get("status") in CLOSED_STATUSES:
		if not doc.get("task_closed_on") or not before or before.get("status") not in CLOSED_STATUSES:
			doc.task_closed_on = (now or now_datetime)()
	elif doc.get("task_closed_on"):
		doc.task_closed_on = None


def validate_system_todo(doc, *, now=None):
	"""Enforce system task immutability for non-privileged users and maintain task_closed_on."""
	before = None if doc.is_new() else doc.get_doc_before_save()
	was_system = bool(before and before.get("task_kind"))
	manage = _may_manage()
	if (
		not manage
		and doc.get("reference_type") == "ToDo"
		and (doc.is_new() or _changed(doc, before, ("reference_type", "reference_name")))
	):
		_refuse_task_of_a_task(doc)
	if not manage and (doc.get("task_kind") or was_system):
		if doc.is_new():
			frappe.throw(_("Tasks of this kind are made by the system."), frappe.PermissionError)
		if _changed(doc, before, LOCKED_FIELDS):
			_locked(before)
	elif not manage and not frappe.flags.get(TODO_FLAG):
		if _changed(doc, before, SYSTEM_FIELDS):
			frappe.throw(_("Tasks of this kind are made by the system."), frappe.PermissionError)
	_stamp_closed_on(doc, before, now=now)


def validate_trash_system_todo(doc):
	"""Refuse deletion of a system task by a non-privileged user."""
	if doc.get("task_kind") and not _may_manage():
		_locked(doc)
