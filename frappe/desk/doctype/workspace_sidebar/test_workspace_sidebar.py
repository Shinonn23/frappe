# Copyright (c) 2025, Frappe Technologies and Contributors
# See license.txt

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import WorkspaceSidebar


class TestWorkspaceSidebarPermissions(unittest.TestCase):
	def setUp(self):
		self.sidebar = WorkspaceSidebar.__new__(WorkspaceSidebar)
		self.sidebar.user = Mock(can_read=[])
		self.sidebar.restricted_doctypes = {"Sales Invoice", "Purchase Invoice"}

	def test_cold_permission_cache_preserves_allowed_doctypes(self):
		allowed = ["Sales Invoice"]
		self.sidebar.user.build_permissions.side_effect = lambda: setattr(self.sidebar.user, "can_read", allowed)
		user = "sidebar-test@example.com"

		with (
			patch.object(frappe, "session", SimpleNamespace(user=user)),
			patch.object(frappe, "cache") as cache,
			patch.object(frappe, "has_permission", return_value=True) as has_permission,
		):
			cache.get_value.return_value = None
			self.sidebar.can_read = self.sidebar.get_cached("user_perm_can_read", self.sidebar.get_can_read_items)

			self.assertIs(self.sidebar.can_read, allowed)
			self.sidebar.user.build_permissions.assert_called_once_with()
			cache.set_value.assert_called_once_with("user_perm_can_read", allowed, user, 21600)
			self.assertTrue(self.sidebar.is_item_allowed("Sales Invoice", "DocType"))
			self.assertFalse(self.sidebar.is_item_allowed("Purchase Invoice", "DocType"))
			has_permission.assert_called_once_with("Sales Invoice")
			has_permission.return_value = False
			self.assertFalse(self.sidebar.is_item_allowed("Sales Invoice", "DocType"))

	def test_existing_user_permissions_do_not_rebuild(self):
		allowed = self.sidebar.user.can_read = ["Sales Invoice"]

		self.assertIs(self.sidebar.get_can_read_items(), allowed)
		self.sidebar.user.build_permissions.assert_not_called()
