# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

"""Bring the counter of a naming series up to the numbers that already exist.

Frappe numbers a new document from a counter (`tabSeries`, one row per prefix) and never looks at the names taken. A
document renamed past the counter (SINV-0050 to SINV-0051 with the counter at 50), imported with its own names or
restored from another site makes the next document get a name that exists, and saving fails until the counter has
moved on. `check` lists the series whose counter is behind, `sync` raises it.

A series can have a prefix for every month or year (SINV-.YYYY.-), or one that depends on the document (an Employee
numbered by its Date of Joining), so the names are read back into their prefixes: the prefix of today and every prefix
that already has a counter are listed. A prefix with a part that stands for a field ({customer}) is found the same
way, from the names.
"""

import re

import frappe
from frappe.model.naming import NamingSeries
from frappe.utils import cint

BEHIND = "behind"
OK = "ok"
AHEAD = "ahead"

# What the parts of a series are in a name; a part that is none of these is text and stands for itself.
PART_PATTERNS = {
	"YY": r"\d{2}",
	"YYYY": r"\d{4}",
	"MM": r"\d{2}",
	"DD": r"\d{2}",
	"JJJ": r"\d{3}",
	"WW": r"\d{2}",
}


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


def split_series(template: str) -> tuple[list[str], int]:
	"""The parts before the running number, and how many digits it has at least."""
	parts = [part for part in NamingSeries(template).series.split(".") if part]
	for index, part in enumerate(parts):
		if part.startswith("#"):
			return parts[:index], len(part)
	raise ValueError(f"The series {template} has no running number.")


def _hook_part_pattern(part: str, method: str) -> str:
	"""A part an app defines (naming_series_variables) is as wide as its value today; unknown if it cannot be asked."""
	try:
		return f".{{{len(str(frappe.get_attr(method)(None, part)))}}}"
	except Exception:
		return r".+?"


def name_pattern(template: str) -> tuple[re.Pattern, str]:
	"""A regular expression with the groups `prefix` and `number` for the names of a series, and the text every
	such name starts with (to ask the database for fewer names)."""
	parts, digits = split_series(template)
	hooks = frappe.get_hooks("naming_series_variables", {})
	pieces, head, literal = [], "", True
	for part in parts:
		if part in PART_PATTERNS:
			pieces.append(PART_PATTERNS[part])
			literal = False
		elif part in hooks:
			pieces.append(_hook_part_pattern(part, hooks[part][0]))
			literal = False
		elif part.startswith("{"):
			pieces.append(r".+?")
			literal = False
		else:
			pieces.append(re.escape(part))
			if literal:
				head += part
	return re.compile(rf"^(?P<prefix>{''.join(pieces)})(?P<number>\d{{{digits},}})$"), head


def highest_by_prefix(pattern: re.Pattern, names: list[str]) -> dict[str, int]:
	"""The highest running number per prefix among the names that follow the series."""
	highest: dict[str, int] = {}
	for name in names:
		match = pattern.match(name or "")
		if match:
			key = match.group("prefix")
			highest[key] = max(highest.get(key, 0), int(match.group("number")))
	return highest


def rows_of(doctype: str, template: str, counters: dict[str, int]) -> list[dict]:
	"""One row per prefix of the series: that of today, and each one that has a counter and names."""
	pattern, head = name_pattern(template)
	names = frappe.get_all(
		doctype, filters={"name": ("like", f"{head}%")} if head else None, pluck="name", limit_page_length=0
	)
	highest = highest_by_prefix(pattern, names)
	try:
		today = NamingSeries(template).get_prefix()
	except Exception:
		frappe.clear_last_message()
		today = None
	keys = {key for key in highest if key in counters}
	if today and "{" not in today:
		keys.add(today)
	rows = []
	for key in sorted(keys):
		counter, top = counters.get(key, 0), highest.get(key, 0)
		rows.append(
			{
				"doctype": doctype,
				"series": template,
				"prefix": key,
				"counter": counter,
				"highest": top,
				"status": series_status(counter, top),
			}
		)
	return rows


def check(doctype: str | None = None) -> dict:
	"""Every series (of one document type, or of all) with its counter, its highest number and its status."""
	counters = {
		name: cint(current) for name, current in frappe.db.sql("select name, `current` from `tabSeries`")
	}
	rows, skipped, seen = [], [], set()
	for dt in [doctype] if doctype else doctypes_with_series():
		if not frappe.db.exists("DocType", dt):
			continue
		for template in series_templates(dt):
			try:
				found = rows_of(dt, template, counters)
			except Exception:
				frappe.clear_last_message()
				skipped.append({"doctype": dt, "series": template, "reason": "invalid"})
				continue
			for row in found:
				if (dt, row["prefix"]) not in seen:
					seen.add((dt, row["prefix"]))
					rows.append(row)
	return {"rows": rows, "skipped": skipped}


def set_counter(prefix: str, value: int) -> None:
	Series = frappe.qb.DocType("Series")
	if frappe.db.get_value("Series", prefix, "name", order_by="name") is None:
		frappe.qb.into(Series).insert(prefix, 0).columns("name", "current").run()
	frappe.qb.update(Series).set(Series.current, cint(value)).where(Series.name == prefix).run()


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
		set_counter(row["prefix"], row["highest"])
		settings.create_version_log_for_change(row["prefix"], row["counter"], row["highest"])
		done.append(row)
	return done
