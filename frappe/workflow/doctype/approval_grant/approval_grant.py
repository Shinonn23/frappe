# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ApprovalGrant(Document):
	"""One right of one user to decide one step: an index of the Role Profiles, rebuilt by
	``workflows/profile_approvals.sync_user_grants``. Nobody writes it by hand."""

	def validate(self):
		if not self.flags.from_sync:
			frappe.throw(_("Approval Grants are made by the system from the Role Profiles."))

	def on_trash(self):
		if not self.flags.from_sync and not frappe.flags.in_uninstall:
			frappe.throw(_("Approval Grants are made by the system from the Role Profiles."))
