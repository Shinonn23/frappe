# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

from collections import defaultdict

import frappe
from frappe.model.document import Document


@frappe.whitelist()
def get_permission_catalog(profile=None):
	"""Read the installed application's workflow authority catalog, if one is configured."""
	providers = frappe.get_hooks("role_profile_permission_catalog")
	if not providers:
		return None
	if profile:
		frappe.get_doc("Role Profile", profile).check_permission("read")
	elif not frappe.has_permission("Role Profile", "create"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	return frappe.get_attr(providers[-1])(profile=profile)


@frappe.whitelist()
def get_user_permissions(user):
	"""Read saved authority through its owning application's permission checks."""
	providers = frappe.get_hooks("user_role_profile_permissions")
	if not providers:
		return None
	frappe.get_doc("User", user).check_permission("read")
	data = frappe.get_attr(providers[-1])(user=user)
	if data is None:
		return None
	from frappe.permissions import AUTOMATIC_ROLES

	return {**data, "implicit_roles": list(AUTOMATIC_ROLES)}


class RoleProfile(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.core.doctype.has_role.has_role import HasRole
		from frappe.types import DF

		role_profile: DF.Data
		roles: DF.Table[HasRole]
	# end: auto-generated types

	def autoname(self):
		"""set name as Role Profile name"""
		self.name = self.role_profile

	def on_update(self):
		self.clear_cache()
		self.queue_action(
			"update_all_users",
			now=frappe.in_test or frappe.flags.in_install,
			enqueue_after_commit=True,
			queue="long",
		)

	def update_all_users(self):
		"""Changes in role_profile reflected across all its user"""
		users = frappe.get_all("User Role Profile", filters={"role_profile": self.name}, pluck="parent")
		for user in users:
			user = frappe.get_doc("User", user)
			user.save()  # resaving syncs roles

	def get_permission_log_options(self, event=None):
		return {"fields": ["roles"]}
