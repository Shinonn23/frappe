"""Offline tests for the native editors' provider boundary and managed role metadata."""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.core.doctype.role_profile import role_profile
from frappe.core.doctype.user import user
from frappe.model.meta import Meta


class TestCustomLayoutOrder(unittest.TestCase):
	def test_custom_section_and_column_stay_in_their_target_tab(self):
		for fieldtype in ("Section Break", "Column Break"):
			with self.subTest(fieldtype=fieldtype):
				fields = [
					frappe._dict(fieldname="roles_tab", fieldtype="Tab Break"),
					frappe._dict(fieldname="roles_html", fieldtype="HTML"),
					frappe._dict(fieldname="workflow_tab", fieldtype="Tab Break"),
					frappe._dict(fieldname="workflow_html", fieldtype="HTML"),
					frappe._dict(
						fieldname="custom_layout",
						fieldtype=fieldtype,
						is_custom_field=True,
						insert_after="roles_html",
					),
				]
				meta = MagicMock(fields=fields, _fields={field.fieldname: field for field in fields})
				meta.field_order = []
				Meta.sort_fields(meta)
				meta._update_fields_based_on_order.assert_called_once_with(
					["roles_tab", "roles_html", "custom_layout", "workflow_tab", "workflow_html"]
				)


class TestPermissionProviders(unittest.TestCase):
	def test_no_provider_keeps_native_forms_available(self):
		with patch.object(frappe, "get_hooks", return_value=[]):
			self.assertIsNone(role_profile.get_permission_catalog("Profile"))
			self.assertIsNone(role_profile.get_user_permissions("user"))

	def test_document_access_is_checked_before_calling_provider(self):
		doc = MagicMock()
		doc.check_permission.side_effect = frappe.PermissionError
		with (
			patch.object(frappe, "get_hooks", return_value=["app.provider"]),
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "get_attr") as resolve,
		):
			with self.assertRaises(frappe.PermissionError):
				role_profile.get_permission_catalog("Profile")
			with self.assertRaises(frappe.PermissionError):
				role_profile.get_user_permissions("user")
		resolve.assert_not_called()

	def test_provider_result_and_arguments_are_preserved(self):
		provider = MagicMock(return_value={"steps": []})
		with (
			patch.object(frappe, "get_hooks", return_value=["app.provider"]),
			patch.object(frappe, "get_doc") as doc,
			patch.object(frappe, "get_attr", return_value=provider),
		):
			self.assertEqual(role_profile.get_permission_catalog("Profile"), {"steps": []})
			doc.return_value.check_permission.assert_called_once_with("read")
		provider.assert_called_once_with(profile="Profile")

	def test_workflow_classification_does_not_change_the_all_roles_api(self):
		with (
			patch.object(user, "get_all_roles", return_value=["Sales User", "Workflow Role"]),
			patch.object(frappe, "get_all", return_value=["Workflow Role"]),
		):
			self.assertEqual(
				user.get_role_editor_data(), {"roles": ["Sales User"], "managed_roles": ["Workflow Role"]}
			)
