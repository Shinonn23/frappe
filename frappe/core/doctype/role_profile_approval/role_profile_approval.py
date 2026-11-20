# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document


class RoleProfileApproval(Document):
	"""One approval step a Role Profile grants, with its limit and scope. No can_approve: a row is the right."""

	pass
