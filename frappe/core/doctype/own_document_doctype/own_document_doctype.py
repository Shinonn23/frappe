# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document


class OwnDocumentDocType(Document):
	"""A DocType whose holders see only their own documents. The registry of allowed DocTypes is in code (PR 6)."""

	pass
