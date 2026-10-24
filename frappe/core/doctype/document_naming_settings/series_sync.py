# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

"""Bring the counter of a naming series up to the numbers that already exist.

Frappe numbers a new document from a counter (`tabSeries`, one row per prefix) and never looks at the names taken. A
document renamed past the counter (SINV-0050 to SINV-0051 with the counter at 50), imported with its own names or
restored from another site makes the next document get a name that exists, and saving fails until the counter has
moved on. `check` lists the series whose counter is behind, `sync` raises it.

Only the prefix of today is checked for a series with dates (YY, MM...), since a new document is always named from
today's date. A series whose prefix has a field in it ({customer}) cannot be evaluated and is reported as skipped.
"""

import re

import frappe
from frappe.model.naming import NamingSeries
from frappe.utils import cint

BEHIND = "behind"
OK = "ok"
AHEAD = "ahead"


def series_status(counter: int, highest: int) -> str:
	"""behind: the next number is already taken. ahead: numbers are skipped, never repeated (a deleted document)."""
	if counter < highest:
		return BEHIND
	return AHEAD if counter > highest else OK


def doctypes_with_series() -> list[str]:
	standard = frappe.get_all("DocField", {"fieldname": "naming_series"}, pluck="parent")
	custom = frappe.get_all("Custom Field", {"fieldname": "naming_series"}, pluck="dt")
	numbered = frappe.get_all(
		"DocType",
		filters=[
			["autoname", "like", "%.#%"],
			["autoname", "not like", "format:%"],
			["autoname", "not like", "naming_series:%"],
		],
		pluck="name",
	)
	return sorted({*standard, *custom, *numbered})


def series_templates(doctype: str) -> list[str]:
	"""The series a document type is numbered with: the options of its Series field, or its own autoname."""
	meta = frappe.get_meta(doctype)
	autoname = meta.autoname or ""
	if meta.get_field("naming_series") and autoname.startswith("naming_series:"):
		return [option for option in meta.get_naming_series_options() if option]
	if ".#" in autoname and not autoname.startswith(("format:", "field:")):
		return [autoname]
	return []


def highest_number(doctype: str, prefix: str) -> int:
	"""The highest running number among the names of the form <prefix><digits>."""
	pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
	names = frappe.get_all(
		doctype, filters={"name": ("like", f"{prefix}%")}, pluck="name", limit_page_length=0
	)
	return max((int(m.group(1)) for m in map(pattern.match, names) if m), default=0)


def check(doctype: str | None = None) -> dict:
	"""Every series (of one document type, or of all) with its counter, its highest number and its status."""
	rows, skipped, seen = [], [], set()
	for dt in [doctype] if doctype else doctypes_with_series():
		if not frappe.db.exists("DocType", dt):
			continue
		for template in series_templates(dt):
			try:
				prefix = NamingSeries(template).get_prefix()
			except Exception:
				frappe.clear_last_message()
				skipped.append({"doctype": dt, "series": template, "reason": "invalid"})
				continue
			if "{" in prefix:
				skipped.append({"doctype": dt, "series": template, "reason": "field in prefix"})
				continue
			if (dt, prefix) in seen:
				continue
			seen.add((dt, prefix))
			counter = cint(frappe.db.get_value("Series", prefix, "current", order_by="name"))
			highest = highest_number(dt, prefix)
			rows.append(
				{
					"doctype": dt,
					"series": template,
					"prefix": prefix,
					"counter": counter,
					"highest": highest,
					"status": series_status(counter, highest),
				}
			)
	return {"rows": rows, "skipped": skipped}


def sync(selected: list[dict] | None = None, doctype: str | None = None) -> list[dict]:
	"""Raise the counter of the series that are behind: the selected ones ({doctype, prefix}), else all of them
	(of one document type, if given). The numbers are worked out again here, never taken from the page."""
	chosen = {(row["doctype"], row["prefix"]) for row in selected} if selected is not None else None
	settings = frappe.get_doc("Document Naming Settings")
	done = []
	for row in check(doctype)["rows"]:
		if row["status"] != BEHIND:
			continue
		if chosen is not None and (row["doctype"], row["prefix"]) not in chosen:
			continue
		NamingSeries(row["series"]).update_counter(row["highest"])
		settings.create_version_log_for_change(row["prefix"], row["counter"], row["highest"])
		done.append(row)
	return done
