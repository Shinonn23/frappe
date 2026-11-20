# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

"""Ensure /my-work and /my-space SPA routes bypass Frappe's website 404 cache."""

import frappe

ROUTE = "/my-space"
ROUTES = ("/my-work", ROUTE)


def is_page_path(path):
	path = (path or "").rstrip("/") or "/"
	return any(path == route or path.startswith(route + "/") for route in ROUTES)


def skip_website_404_cache():
	"""``before_request`` hook to prevent caching 404s for My Work SPA routes."""
	request = getattr(frappe.local, "request", None)
	if request is not None and is_page_path(getattr(request, "path", None)):
		frappe.local.no_cache = 1
