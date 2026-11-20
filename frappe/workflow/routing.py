# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

"""Generic task routing engine: Ask vs Stand-in narrowing and availability-based backup."""

from __future__ import annotations

import frappe


def unavailable(users):
	"""Return users from ``users`` who cannot take a task today.

	Core checks disabled Users (``User.enabled == 0``). Applications (such as HRMS) may register
	additional checks via the ``user_unavailability_checks`` hook (callable receiving a set of user IDs
	and returning an iterable of unavailable user IDs, e.g. inactive employees or approved leaves).
	"""
	users = set(users)
	if not users:
		return set()
	away = set(frappe.get_all("User", filters={"name": ["in", sorted(users)], "enabled": 0}, pluck="name"))
	remaining = users - away
	if remaining:
		for hook in frappe.get_hooks("user_unavailability_checks"):
			away.update(frappe.get_attr(hook)(remaining) or ())
	return away & users


def narrow(users, team, *, able=None, away=None):
	"""Pick who is asked from ``users``:

	1. Prefer available members of ``team`` (Ask holders) who can act (``able``).
	2. If every Ask holder is away or unable, fall back to available members of ``users`` (Stand-in holders)
	   who can act.
	3. If nobody available can act, fall back to ``able(users)`` so a task is never left unassigned.
	"""
	pool = set(users)
	if not pool:
		return []
	able = able or (lambda group: group)
	gone = set(away(pool)) & pool if away else set()
	team = pool & set(team or ())
	groups = []
	if team and team != pool:
		groups.append(team - gone)
	if gone:
		groups.append(pool - gone)
	for group in groups:
		if group and (picked := able(group)):
			return picked
	return able(pool)
