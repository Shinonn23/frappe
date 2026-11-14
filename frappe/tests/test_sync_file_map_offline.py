import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.model.sync import create_entity_file_map


class TestEntityFileMap(unittest.TestCase):
	def test_source_entities_are_found_without_following_dependency_symlinks(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			app = root / "app"
			fixtures = {
				app / "module/doctype/source/source.json": {"name": "Source"},
				app / "module/doctype/list_source/list_source.json": [{"name": "List Source"}],
				app / "module/test_dashboard/nested/dashboard.json": {"name": "Dashboard"},
				root / "dependencies/pkg/doctype/dependency/dependency.json": {"name": "Dependency"},
			}
			for path, content in fixtures.items():
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_text(json.dumps(content))
			(app / "public").mkdir()
			(app / "public/node_modules").symlink_to(root / "dependencies", target_is_directory=True)
			with (
				patch.object(frappe, "get_installed_apps", return_value=["app"]),
				patch.object(frappe, "get_app_path", return_value=str(app)),
			):
				result = create_entity_file_map(["DocType", "Dashboard"])
			self.assertEqual(
				result,
				{
					"DocType": {
						"Source": str(app / "module/doctype/source/source.json"),
						"List Source": str(app / "module/doctype/list_source/list_source.json"),
					},
					"Dashboard": {"Dashboard": str(app / "module/test_dashboard/nested/dashboard.json")},
				},
			)
