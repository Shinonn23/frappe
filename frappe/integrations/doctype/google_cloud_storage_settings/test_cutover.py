import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock
from frappe.integrations.doctype.google_cloud_storage_settings.cutover import perform_cutover, resume_cleanup
from frappe.integrations.doctype.google_cloud_storage_settings.test_migrate_files import Bucket


class TestCutover(unittest.TestCase):
	def test_activates_only_after_verified_copy_and_deletes_originals_after_activation(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			public = root / "public/files/a.txt"
			private = root / "private/files/b.txt"
			public.write_bytes(b"public")
			private.write_bytes(b"private")
			bucket = Bucket()
			records = [
				{"name": "a", "file_url": "/files/a.txt", "is_private": 0},
				{"name": "b", "file_url": "/private/files/b.txt", "is_private": 1},
			]

			def activate(unmanaged):
				self.assertEqual(unmanaged, [])
				self.assertEqual(public.read_bytes(), b"public")
				self.assertEqual(private.read_bytes(), b"private")
				self.assertEqual(bucket.objects["site/public/a.txt"], b"public")
				self.assertEqual(bucket.objects["site/private/b.txt"], b"private")
				self.assertIn("site/.frappe-cleanup-plan.json", bucket.objects)

			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				result = perform_cutover(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site"), records=lambda: records, activate=activate)
			self.assertEqual(result, {"copied": 2, "references": 2, "stale": 0, "removed": 2})
			self.assertFalse(public.exists())
			self.assertFalse(private.exists())
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				self.assertEqual(resume_cleanup(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site")), 0)

	def test_cutover_registers_only_unmanaged_public_objects(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/managed.txt").write_bytes(b"managed")
			(root / "public/files/asset.css").write_bytes(b"asset")
			bucket = Bucket()
			activate = Mock()
			records = [{"name": "file-1", "file_url": "/files/managed.txt", "is_private": 0}]
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				perform_cutover(root, owner="site.example", bench_path="/bench", refresh=lambda: None,
					target=lambda: (bucket, "site"), records=lambda: records, activate=activate)
			self.assertEqual(activate.call_args.args[0], [("/files/asset.css", 1)])

	def test_failed_activation_keeps_all_local_files(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			path = root / "private/files/a.txt"
			path.write_bytes(b"private")
			bucket = Bucket()
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root), self.assertRaisesRegex(RuntimeError, "activation failed"):
				perform_cutover(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site"), records=lambda: [], activate=Mock(side_effect=RuntimeError("activation failed")))
			self.assertEqual(path.read_bytes(), b"private")
			self.assertEqual(bucket.objects["site/private/a.txt"], b"private")

	def test_failed_activation_can_replan_changed_files_when_originals_remain(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			path = root / "private/files/a.txt"
			path.write_bytes(b"first")
			bucket = Bucket()
			args = dict(owner="site.example", bench_path="/bench", refresh=lambda: None,
				target=lambda: (bucket, "site"), records=lambda: [])
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				with self.assertRaisesRegex(RuntimeError, "activation failed"):
					perform_cutover(root, **args, activate=Mock(side_effect=RuntimeError("activation failed")))
				path.write_bytes(b"updated")
				result = perform_cutover(root, **args, activate=lambda unmanaged: None)
			self.assertEqual(result["removed"], 1)
			self.assertFalse(path.exists())
			self.assertEqual(bucket.objects["site/private/a.txt"], b"updated")

	def test_missing_reference_aborts_before_copy_or_activation(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			bucket = Bucket()
			activate = Mock()
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root), self.assertRaisesRegex(ValueError, "does not exist"):
				perform_cutover(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site"), records=lambda: [{"name": "missing", "file_url": "/private/files/a", "is_private": 1}], activate=activate)
			activate.assert_not_called()
			self.assertEqual(bucket.objects, {})

	def test_cutover_does_not_start_while_writer_holds_lock(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			bucket = Bucket()
			records, activate = Mock(return_value=[]), Mock()
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				with cutover_lock.cutover_lock("site.example", "/bench", exclusive=False):
					with self.assertRaises(cutover_lock.CutoverBusyError):
						perform_cutover(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site"), records=records, activate=activate)
			records.assert_not_called()
			activate.assert_not_called()
			self.assertEqual(bucket.objects, {})

	def test_database_snapshot_is_refreshed_after_lock_before_reading_target_and_records(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			bucket = Bucket()
			events = []
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				perform_cutover(
					root, owner="site.example", bench_path="/bench",
					refresh=lambda: events.append("refresh"),
					target=lambda: (events.append("target") or bucket, "site"),
					records=lambda: events.append("records") or [],
					activate=lambda unmanaged: events.append("activate"),
				)
			self.assertEqual(events, ["refresh", "target", "records", "activate"])

	def test_interrupted_cleanup_can_resume_from_bucket_manifest(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			public = root / "public/files/a.txt"
			private = root / "private/files/b.txt"
			public.write_bytes(b"public")
			private.write_bytes(b"private")
			bucket = Bucket()

			def activate(unmanaged):
				bucket.objects["site/private/b.txt"] = b"changed"

			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				with self.assertRaisesRegex(RuntimeError, "differs"):
					perform_cutover(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site"), records=lambda: [], activate=activate)
				self.assertFalse(public.exists())
				self.assertEqual(private.read_bytes(), b"private")
				bucket.objects["site/private/b.txt"] = b"private"
				self.assertEqual(resume_cleanup(root, owner="site.example", bench_path="/bench", refresh=lambda: None, target=lambda: (bucket, "site")), 1)
				self.assertFalse(private.exists())
