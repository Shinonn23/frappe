import unittest
from unittest.mock import MagicMock, patch

from frappe.integrations.doctype.google_cloud_storage_settings import storage


class TestObjectStorage(unittest.TestCase):
	def test_sqlite_failed_commit_preserves_rollback_callbacks(self):
		from frappe.database.sqlite.database import SQLiteDatabase
		from frappe.utils import CallbackManager

		connection = MagicMock()
		connection.commit.side_effect = OSError("commit failed")
		db = MagicMock()
		db._conn = connection
		db._disable_transaction_control = False
		db._gcs_rollback_failed = False
		db.before_commit = CallbackManager()
		db.before_rollback = CallbackManager()
		db.after_rollback = CallbackManager()
		restore = MagicMock()
		db.after_rollback.add(restore)
		with self.assertRaises(OSError):
			SQLiteDatabase.commit(db)
		self.assertEqual(len(db.after_rollback), 1)
		db.after_rollback.run()
		restore.assert_called_once()

	def test_storage_selection_reads_committed_database_not_document_cache(self):
		import frappe

		query = MagicMock()
		query.run.side_effect = [[], [("0",)], [("1",)]]
		qb = MagicMock()
		qb.get_query.return_value = query
		with patch.object(frappe, "qb", qb), patch.object(frappe, "get_single_value") as cached:
			self.assertFalse(storage.enabled())
			self.assertFalse(storage.enabled())
			self.assertTrue(storage.enabled())
		cached.assert_not_called()
		qb.get_query.assert_called_with("Singles", filters={"doctype": "Google Cloud Storage Settings", "field": "enabled"}, fields=["value"])

	def test_bucket_configuration_does_not_use_stale_document_cache(self):
		import frappe

		settings = frappe._dict(bucket_name="current-bucket", object_prefix="current-site")
		with patch.object(frappe, "get_single", return_value=settings), patch.object(
			frappe, "get_cached_doc"
		) as cached, patch("google.cloud.storage.Client") as client:
			storage.get_blob("/private/files/a.txt")
			storage.staging_bucket()
		cached.assert_not_called()
		client.return_value.bucket.assert_called_with("current-bucket")
		client.return_value.bucket.return_value.blob.assert_called_once_with("current-site/private/a.txt")

	@patch.object(storage, "delete")
	def test_cleanup_keeps_file_thumbnail_and_unmanaged_references(self, delete):
		import frappe

		db = MagicMock()
		with patch.object(frappe, "db", db), patch.object(storage, "unmanaged_public_generation") as unmanaged:
			db.exists.side_effect = [True, False, True, False, False, False, False]
			unmanaged.side_effect = ["12", None]
			for _ in range(4):
				storage.delete_if_unreferenced("/files/shared.png", 12)
		delete.assert_called_once_with("/files/shared.png", if_generation_match=12)

	def test_savepoint_tracking_retains_rolled_back_point_and_releases_nested_points(self):
		from frappe.database.database import Database

		db = MagicMock()
		db._gcs_savepoints = []
		Database.savepoint(db, "outer")
		Database.savepoint(db, "inner")
		Database.rollback(db, save_point="outer")
		self.assertEqual(db._gcs_savepoints, ["outer"])
		Database.release_savepoint(db, "outer")
		self.assertEqual(db._gcs_savepoints, [])

	def test_keys_keep_site_and_visibility_separate(self):
		self.assertEqual(storage.object_key("site/one", "/files/report.pdf"), "site/one/public/report.pdf")
		self.assertEqual(storage.object_key("site/one", "/private/files/report.pdf"), "site/one/private/report.pdf")
		self.assertEqual(storage.object_key("site", "/files/a%20b.pdf"), "site/public/a%20b.pdf")

	def test_rejects_unsafe_keys(self):
		for prefix, url in (("../site", "/files/a"), ("site\\other", "/files/a"), ("site", "/files/../private"),
			("site", "/private/files/a\\b"), ("site", "/assets/a"), ("", "/files/a")):
			with self.subTest(prefix=prefix, url=url), self.assertRaises(ValueError):
				storage.object_key(prefix, url)

	def test_percent_escapes_remain_distinct_from_decoded_names(self):
		self.assertNotEqual(storage.object_key("site", "/files/%73ecret.pdf"),
			storage.object_key("site", "/files/secret.pdf"))

	@patch.object(storage, "get_blob")
	def test_operations_delegate_to_blob(self, get_blob):
		blob = MagicMock()
		get_blob.return_value = blob
		blob.download_as_bytes.return_value = b"data"
		self.assertEqual(storage.read("/files/a"), b"data")
		storage.write("/files/a", b"data", if_generation_match=0)
		blob.upload_from_string.assert_called_once_with(b"data", content_type=None, if_generation_match=0)
		storage.delete("/files/a")
		blob.delete.assert_called_once_with(if_generation_match=None)

	@patch.object(storage, "get_blob")
	def test_copy_pins_generations(self, get_blob):
		source, target, copied = MagicMock(), MagicMock(), MagicMock()
		source.generation, copied.generation, target.name = 12, 17, "site/private/a"
		source.bucket.copy_blob.return_value = copied
		get_blob.side_effect = [source, target]
		self.assertEqual(storage.copy("/files/a", "/private/files/a"), (12, 17))
		source.bucket.copy_blob.assert_called_once_with(
			source, target.bucket, target.name, if_source_generation_match=12, if_generation_match=0
		)

	@patch.object(storage, "write")
	def test_multiple_overwrites_restore_first_version_using_latest_generation(self, write):
		import frappe

		db = MagicMock()
		with patch.object(frappe, "db", db):
			storage.restore_on_rollback("/private/files/a", b"original", 11)
			storage.restore_on_rollback("/private/files/a", b"intermediate", 12)
			self.assertEqual(db.after_rollback.add.call_count, 1)
			db.after_rollback.add.call_args.args[0]()
		write.assert_called_once_with("/private/files/a", b"original", if_generation_match=12)

	@patch.object(storage, "get_blob")
	def test_versioned_read_pins_the_download(self, get_blob):
		blob = get_blob.return_value
		blob.generation = 10
		blob.download_as_bytes.return_value = b"contents"
		self.assertEqual(storage.read_versioned("/files/a"), (b"contents", 10))
		blob.download_as_bytes.assert_called_once_with(if_generation_match=10)


class TestFileStorage(unittest.TestCase):
	def test_shared_file_rows_update_only_their_attached_fields(self):
		import frappe
		from frappe.core.doctype.file.utils import update_existing_file_docs

		doc = frappe._dict(name="source", file_url="/private/files/a.pdf", is_private=1)
		db = MagicMock()
		db.get_values.return_value = [
			frappe._dict(name="shared", file_url="/files/a.pdf", attached_to_doctype="Invoice", attached_to_name="INV-2", attached_to_field="attachment"),
			frappe._dict(name="case", file_url="/files/A.pdf", attached_to_doctype="Invoice", attached_to_name="INV-3", attached_to_field="attachment"),
		]
		with patch.object(frappe, "db", db), patch.object(frappe, "qb", MagicMock()), patch(
			"frappe.core.doctype.file.utils.update_attachment_field"
		) as update:
			update_existing_file_docs(doc, "/files/a.pdf")
		update.assert_called_once_with("Invoice", "INV-2", "attachment", "/files/a.pdf", "/private/files/a.pdf")

	@patch.object(storage, "copy", return_value=(12, 17))
	@patch.object(storage, "block_public")
	@patch.object(storage, "enabled", return_value=True)
	def test_visibility_change_updates_attached_field_conditionally(self, enabled, block, copy):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file, file.is_private = False, 1
		file.file_url, file.thumbnail_url = "/files/a.pdf", None
		file.attached_to_doctype, file.attached_to_name, file.attached_to_field = "Invoice", "INV-1", "attachment"
		file.fetch_attached_to_field.return_value = True
		with patch.object(frappe, "db", MagicMock()), patch.object(frappe, "get_site_path", return_value="/unused"), patch(
			"frappe.core.doctype.file.file.update_existing_file_docs"
		), patch("frappe.core.doctype.file.utils.update_attachment_field") as update:
			File.handle_is_private_changed(file)
		update.assert_called_once_with("Invoice", "INV-1", "attachment", "/files/a.pdf", "/private/files/a.pdf")

	def test_attachment_field_updates_only_when_it_still_matches_old_url(self):
		import frappe
		from frappe.core.doctype.file.utils import update_attachment_field

		meta = MagicMock(issingle=False)
		meta.get_field.return_value = frappe._dict(fieldtype="Attach")
		db = MagicMock()
		db.get_value.side_effect = ["/files/a.pdf", "/files/replaced.pdf"]
		with patch.object(frappe, "db", db), patch.object(frappe, "get_meta", return_value=meta):
			update_attachment_field("Invoice", "INV-1", "attachment", "/files/a.pdf", "/private/files/a.pdf")
			update_attachment_field("Invoice", "INV-2", "attachment", "/files/a.pdf", "/private/files/a.pdf")
		db.set_value.assert_called_once_with("Invoice", "INV-1", "attachment", "/private/files/a.pdf")

	def test_single_attachment_field_uses_locked_current_value(self):
		import frappe
		from frappe.core.doctype.file.utils import update_attachment_field

		meta = MagicMock(issingle=True)
		meta.get_field.return_value = frappe._dict(fieldtype="Attach")
		db = MagicMock()
		db.get_single_value.return_value = "/files/a.pdf"
		with patch.object(frappe, "db", db), patch.object(frappe, "get_meta", return_value=meta):
			update_attachment_field("Settings", "Settings", "attachment", "/files/a.pdf", "/private/files/a.pdf")
		db.get_single_value.assert_called_once_with("Settings", "attachment", cache=False, for_update=True)
		db.set_single_value.assert_called_once_with("Settings", "attachment", "/private/files/a.pdf")

	def test_move_target_checks_exact_case(self):
		import frappe
		from frappe.core.doctype.file.file import File

		db = MagicMock()
		db.get_values.side_effect = [["/files/A.png"], [], ["/files/a.png"]]
		with patch.object(frappe, "db", db), patch("frappe.core.doctype.file.file._", lambda value: value), patch.object(
			frappe, "throw", side_effect=FileExistsError
		):
			File.validate_gcs_move_target(MagicMock(), "/files/a.png")
			with self.assertRaises(FileExistsError):
				File.validate_gcs_move_target(MagicMock(), "/files/a.png")

	@patch.object(storage, "enabled", return_value=True)
	def test_private_attachment_requires_permission_for_exact_url(self, enabled):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.file_url, file.content = "/private/files/photo.png", None
		file.flags = frappe._dict()
		frappe.local.session = frappe._dict(user="user@example.com")
		matches = [frappe._dict(name="own", owner="user@example.com", is_private=1, file_url="/private/files/Photo.png"),
			frappe._dict(name="secret", owner="other@example.com", is_private=1, file_url="/private/files/photo.png")]
		try:
			with patch.object(frappe, "get_all", return_value=matches), patch.object(frappe, "get_doc"), patch(
				"frappe.core.doctype.file.file.has_permission", return_value=False
			), patch("frappe.core.doctype.file.file._", lambda value: value), patch.object(
				frappe, "throw", side_effect=frappe.PermissionError
			), self.assertRaises(frappe.PermissionError):
				File.validate_private_file_access(file)
		finally:
			del frappe.local.session

	@patch.object(storage, "enabled", return_value=True)
	def test_private_orphan_cannot_be_attached_without_permission(self, enabled):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.file_url, file.content = "/private/files/orphan.png", None
		file.flags = frappe._dict()
		with patch.object(frappe, "get_all", return_value=[]), patch("frappe.core.doctype.file.file._", lambda value: value), patch.object(
			frappe, "throw", side_effect=frappe.PermissionError
		), self.assertRaises(frappe.PermissionError):
			File.validate_private_file_access(file)

	def setUp(self):
		guard = patch("frappe.core.doctype.file.file.register_object")
		self.object_guard = guard.start()
		self.addCleanup(guard.stop)

	@patch.object(storage, "cleanup_unreferenced_after_commit")
	@patch.object(storage, "enabled", return_value=True)
	def test_deleting_source_removes_private_thumbnail_mapping(self, enabled, cleanup):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.name = "source"
		file.is_home_folder = file.is_attachments_folder = file.is_folder = False
		file.file_url, file.thumbnail_url = "/private/files/a.png", None
		entry = frappe._dict(file_url="/private/files/extra.png", generation="12")
		db = MagicMock()
		with patch.object(frappe, "db", db), patch.object(frappe, "get_all", return_value=[entry]):
			File.on_trash(file)
		db.delete.assert_called_once_with("GCS Private Thumbnail", {"file_url": entry.file_url, "source_file": "source"})
		cleanup.assert_called_once_with(entry.file_url, "12")

	@patch.object(storage, "write", return_value=12)
	@patch.object(storage, "enabled", return_value=True)
	def test_overwrite_inside_savepoint_records_previous_content(self, enabled, write):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.flags = frappe._dict(new_file=True)
		file._content = b"contents"
		file.file_name = "a.txt"
		file.file_url = "/private/files/a.txt"
		db = MagicMock()
		db._gcs_savepoints = ["before_edit"]
		db._gcs_overwrite_history = []
		with patch.object(frappe, "db", db), patch.object(frappe, "get_value", return_value=None), patch(
			"frappe.core.doctype.file.file.call_hook_method"
		), patch.object(storage, "read_versioned", return_value=(b"old", 11)):
			File.save_file(file, overwrite=True)
		self.assertEqual(db._gcs_overwrite_history, [(file.file_url, b"old")])
		write.assert_called_once()
		frappe.local.gcs_overwrites.pop(file.file_url, None)

	@patch.object(storage, "write", return_value=42)
	@patch.object(storage, "delete")
	@patch.object(storage, "enabled", return_value=True)
	def test_new_file_is_written_to_bucket_and_rollback_deletes_only_its_generation(self, enabled, delete, write):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.flags = frappe._dict(new_file=True)
		file._content = b"contents"
		file.file_name = "a.txt"
		file.file_url = None
		file.is_private = 1
		file.check_max_file_size.return_value = 8
		db = MagicMock()
		with patch.object(frappe, "db", db), patch.object(frappe, "get_value", return_value=None), patch(
			"frappe.core.doctype.file.file.generate_file_name", return_value="a.txt"
		), patch("frappe.core.doctype.file.file.call_hook_method"):
			self.assertEqual(File.save_file(file), {"file_name": "a.txt", "file_url": "/private/files/a.txt"})
		write.assert_called_once_with("/private/files/a.txt", b"contents", content_type="text/plain", if_generation_match=0)
		self.object_guard.assert_called_with("/private/files/a.txt")
		file.check_content.assert_called_once()
		db.after_rollback.add.call_args.args[0]()
		delete.assert_called_once_with("/private/files/a.txt", if_generation_match=42)
		file.save_file_on_filesystem.assert_not_called()

	@patch.object(storage, "write")
	@patch.object(storage, "enabled", return_value=True)
	def test_invalid_file_content_is_rejected_before_bucket_upload(self, enabled, write):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.flags = frappe._dict(new_file=True)
		file._content = b"unsafe"
		file.file_name = "a.pdf"
		file.file_url = None
		file.is_private = 1
		file.check_max_file_size.return_value = 6
		file.check_content.side_effect = ValueError("unsafe PDF")
		with patch.object(frappe, "get_value", return_value=None), patch.object(
			frappe, "get_system_settings", return_value=False
		), patch("frappe.core.doctype.file.file.generate_file_name", return_value="a.pdf"), patch(
			"frappe.core.doctype.file.file.call_hook_method"
		):
			with self.assertRaisesRegex(ValueError, "unsafe PDF"):
				File.save_file(file)
		write.assert_not_called()

	@patch.object(storage, "enabled", return_value=True)
	def test_attachment_copy_rejects_missing_shared_object(self, enabled):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.flags = frappe._dict(copy_from_existing_file=True)
		file.attached_to_doctype = None
		file.file_url = "/private/files/missing.pdf"
		file.thumbnail_url = None
		file.is_folder = False
		file.is_remote_file = False
		file.exists_on_disk.return_value = False
		with patch.object(frappe, "throw", side_effect=FileNotFoundError), patch(
			"frappe.core.doctype.file.file._", lambda value: value
		):
			with self.assertRaises(FileNotFoundError):
				File.before_insert(file)
		self.object_guard.assert_called_once_with("/private/files/missing.pdf")

	@patch.object(storage, "enabled", return_value=True)
	def test_reference_changes_guard_old_and_new_urls(self, enabled):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_folder = False
		file.is_remote_file = False
		file.is_new.return_value = False
		file.has_value_changed.side_effect = lambda field: field in ("file_url", "thumbnail_url")
		file.get_doc_before_save.return_value = frappe._dict(file_url="/files/old", thumbnail_url="/files/old-thumb")
		file.get.side_effect = lambda field: {"file_url": "/files/new", "thumbnail_url": "/files/new-thumb"}[field]
		with patch.object(frappe, "form_dict", frappe._dict(file_size=None)):
			File.validate(file)
		self.assertEqual(
			[call.args[0] for call in self.object_guard.call_args_list],
			["/files/old", "/files/new", "/files/old-thumb", "/files/new-thumb"],
		)

	@patch.object(storage, "read", return_value=b"hello")
	@patch.object(storage, "enabled", return_value=True)
	def test_file_get_content_uses_bucket(self, enabled, read):
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_folder = False
		file.is_remote_file = False
		file.file_url = "/private/files/a.txt"
		file.get.return_value = None
		self.assertEqual(File.get_content(file), "hello")
		read.assert_called_once_with(file.file_url)
		file.get_full_path.assert_not_called()

	@patch.object(storage, "exists", return_value=True)
	@patch.object(storage, "enabled", return_value=True)
	def test_exists_uses_bucket(self, enabled, exists):
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.file_url = "/files/a"
		self.assertTrue(File.exists_on_disk(file))
		exists.assert_called_once_with(file.file_url)
		file.get_full_path.assert_not_called()

	@patch.object(storage, "delete")
	@patch.object(storage, "enabled", return_value=True)
	@patch.object(storage, "generation", return_value=21)
	def test_file_deletion_is_deferred_until_commit(self, generation, enabled, delete):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.file_url = "/private/files/a"
		file.thumbnail_url = None
		file.flags.gcs_rollback = False
		db = MagicMock()
		db.exists.return_value = False
		with patch.object(frappe, "db", db):
			File.delete_file_data_content(file)
			delete.assert_not_called()
			callback = db.after_commit.add.call_args.args[0]
			callback()
		delete.assert_called_once_with(file.file_url, if_generation_match=21)

	@patch.object(storage, "delete")
	@patch.object(storage, "enabled", return_value=True)
	@patch.object(storage, "generation", return_value=21)
	def test_file_deletion_keeps_object_referenced_at_commit(self, generation, enabled, delete):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.file_url = "/private/files/a"
		file.thumbnail_url = None
		file.flags.gcs_rollback = False
		db = MagicMock()
		db.exists.return_value = True
		with patch.object(frappe, "db", db):
			File.delete_file_data_content(file)
			db.after_commit.add.call_args.args[0]()
		delete.assert_not_called()

	@patch.object(storage, "generation")
	@patch.object(storage, "enabled", return_value=True)
	def test_external_thumbnail_is_not_treated_as_a_bucket_object(self, enabled, generation):
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = True
		file.thumbnail_url = "https://example.com/thumbnail.png"
		File.delete_file_data_content(file, only_thumbnail=True)
		generation.assert_not_called()

	@patch.object(storage, "copy", return_value=(12, 17))
	@patch.object(storage, "delete")
	@patch.object(storage, "enabled", return_value=True)
	@patch.object(storage, "block_public")
	def test_visibility_change_has_generation_guarded_commit_and_rollback(self, block, enabled, delete, copy):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.file_url = "/files/a.pdf"
		file.is_private = 1
		file.thumbnail_url = None
		file.attached_to_doctype = None
		db = MagicMock()
		db.get_values.return_value = ["/private/files/a.pdf"]
		with patch.object(frappe, "db", db), patch.object(frappe, "get_site_path", return_value="/unused"), patch(
			"frappe.core.doctype.file.file.update_existing_file_docs"
		):
			File.handle_is_private_changed(file)
			db.exists.side_effect = [False, False]
			db.after_commit.add.call_args.args[0]()
		self.assertEqual(file.file_url, "/private/files/a.pdf")
		copy.assert_called_once_with("/files/a.pdf", "/private/files/a.pdf")
		block.assert_called_once_with("/files/a.pdf")
		db.after_rollback.add.call_args.args[0]()
		delete.assert_called_with("/private/files/a.pdf", if_generation_match=17)
		delete.assert_any_call("/files/a.pdf", if_generation_match=12)

	@patch.object(storage, "copy", return_value=(12, 17))
	@patch.object(storage, "delete")
	@patch.object(storage, "enabled", return_value=True)
	@patch.object(storage, "block_public")
	def test_rolled_back_visibility_change_keeps_original(self, block, enabled, delete, copy):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.file_url = "/files/a.pdf"
		file.is_private = 1
		file.thumbnail_url = None
		file.attached_to_doctype = None
		db = MagicMock()
		db.exists.return_value = False
		db.get_values.return_value = []
		with patch.object(frappe, "db", db), patch.object(frappe, "get_site_path", return_value="/unused"), patch(
			"frappe.core.doctype.file.file.update_existing_file_docs"
		):
			File.handle_is_private_changed(file)
			db.after_commit.add.call_args.args[0]()
		delete.assert_called_once_with("/private/files/a.pdf", if_generation_match=17)

	@patch.object(storage, "copy", side_effect=[(12, 17), (22, 27)])
	@patch.object(storage, "delete")
	@patch.object(storage, "enabled", return_value=True)
	@patch.object(storage, "block_public")
	def test_visibility_change_moves_thumbnail(self, block, enabled, delete, copy):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file = False
		file.file_url = "/files/a.png"
		file.thumbnail_url = "/files/a_small.png"
		file.content_hash = "hash"
		file.is_private = 1
		file.attached_to_doctype = None
		db = MagicMock()
		db.exists.side_effect = [False, False]
		db.get_values.side_effect = [
			[frappe._dict(name="file-1", file_url="/files/a.png", thumbnail_url="/files/a_small.png")],
			["/private/files/a_small.png"],
		]
		with patch.object(frappe, "db", db), patch.object(frappe, "get_site_path", return_value="/unused"), patch(
			"frappe.core.doctype.file.file.update_existing_file_docs"
		):
			File.handle_is_private_changed(file)
			self.assertEqual(file.thumbnail_url, "/private/files/a_small.png")
			self.assertEqual(block.call_count, 2)
			db.set_value.assert_called_once_with(
				"File", {"name": ["in", ["file-1"]]},
				"thumbnail_url", "/private/files/a_small.png",
			)
			db.after_commit.add.call_args_list[1].args[0]()
		delete.assert_called_with("/files/a_small.png", if_generation_match=22)

	@patch.object(storage, "copy")
	@patch.object(storage, "block_public")
	@patch.object(storage, "enabled", return_value=True)
	def test_private_conversion_rejects_independently_shared_thumbnail_before_copy(self, enabled, block, copy):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file, file.is_private = False, 1
		file.file_url, file.thumbnail_url = "/files/a.png", "/files/shared.png"
		db = MagicMock()
		db.get_values.return_value = [frappe._dict(name="another-file", file_url="/files/b.png", thumbnail_url="/files/shared.png")]
		with patch.object(frappe, "db", db), patch.object(frappe, "get_site_path", return_value="/unused"), patch(
			"frappe.core.doctype.file.file._", lambda value: value
		), patch.object(frappe, "throw", side_effect=ValueError), self.assertRaises(ValueError):
			File.handle_is_private_changed(file)
		copy.assert_not_called()
		block.assert_not_called()

	@patch.object(storage, "copy", return_value=(12, 17))
	@patch.object(storage, "block_public")
	@patch.object(storage, "enabled", return_value=True)
	def test_file_used_as_its_own_thumbnail_is_copied_once(self, enabled, block, copy):
		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.is_remote_file, file.is_private = False, 1
		file.file_url = file.thumbnail_url = "/files/a.png"
		file.attached_to_doctype = None
		db = MagicMock()
		db.get_values.return_value = [frappe._dict(name="file-1", file_url="/files/a.png", thumbnail_url="/files/a.png")]
		with patch.object(frappe, "db", db), patch.object(frappe, "get_site_path", return_value="/unused"), patch(
			"frappe.core.doctype.file.file.update_existing_file_docs"
		):
			File.handle_is_private_changed(file)
		self.assertEqual(file.thumbnail_url, "/private/files/a.png")
		copy.assert_called_once_with("/files/a.png", "/private/files/a.png")
		block.assert_called_once_with("/files/a.png")


class TestHTTPStorage(unittest.TestCase):
	def test_public_url_requires_exact_case_when_database_collation_is_insensitive(self):
		from werkzeug.exceptions import NotFound

		import frappe

		db = MagicMock()
		db.get_values.side_effect = [["/files/Photo.png"], []]
		with patch.object(frappe, "db", db), patch.object(storage, "public_blocked", return_value=False), patch.object(
			storage, "unmanaged_public_generation", return_value=None
		), patch.object(storage, "read") as read, self.assertRaises(NotFound):
			storage.read_public("/files/photo.png")
		read.assert_not_called()

	def test_private_url_requires_exact_case_when_database_collation_is_insensitive(self):
		import frappe
		from frappe.core.doctype.file.utils import find_file_by_url

		with patch.object(frappe, "get_all", side_effect=[[{"name": "file", "file_url": "/private/files/Photo.png"}], []]), patch.object(
			frappe, "get_doc"
		) as get_doc, patch.object(storage, "enabled", return_value=False):
			self.assertIsNone(find_file_by_url("/private/files/photo.png"))
		get_doc.assert_not_called()

	def test_generation_change_during_download_returns_not_found(self):
		from google.api_core.exceptions import PreconditionFailed
		from werkzeug.exceptions import NotFound

		import frappe
		from frappe.utils.response import send_gcs_file

		db = MagicMock()
		db.exists.return_value = False
		with patch.object(frappe, "db", db), patch.object(storage, "public_blocked", return_value=False), patch.object(
			storage, "unmanaged_public_generation", return_value="12"
		), patch.object(storage, "read_versioned", side_effect=PreconditionFailed("changed")):
			with self.assertRaises(NotFound):
				send_gcs_file("/files/asset.png")
			with self.assertRaises(NotFound):
				send_gcs_file("/private/files/thumbnail.png", private=True, expected_generation="12")

	@patch.object(storage, "enabled", return_value=True)
	def test_large_multipart_upload_never_spools_to_disk(self, enabled):
		import io

		from werkzeug.test import EnvironBuilder

		from frappe.app import Request

		payload = b"x" * (1024 * 1024)
		body = (b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.bin\"\r\n"
			b"Content-Type: application/octet-stream\r\n\r\n" + payload + b"\r\n--boundary--\r\n")
		environ = EnvironBuilder(method="POST", input_stream=io.BytesIO(body), content_length=len(body),
			content_type="multipart/form-data; boundary=boundary").get_environ()
		with patch("tempfile.TemporaryFile", side_effect=AssertionError("uploaded file reached disk")):
			request = Request(environ)
			self.assertEqual(request.files["file"].read(), payload)
			request.close()

	@patch.object(storage, "read_versioned", return_value=(b"changed", 13))
	def test_private_thumbnail_rejects_changed_generation(self, read):
		from werkzeug.exceptions import NotFound

		from frappe.utils.response import send_gcs_file

		with self.assertRaises(NotFound):
			send_gcs_file("/private/files/extra.png", private=True, expected_generation="12")

	@patch.object(storage, "enabled", return_value=True)
	def test_multipart_stream_stays_in_memory(self, enabled):
		import io

		from werkzeug.test import EnvironBuilder

		from frappe.app import Request

		request = Request(EnvironBuilder(method="POST").get_environ())
		self.assertIsInstance(request._get_file_stream(1024 * 1024, "application/octet-stream"), io.BytesIO)

	@patch.object(storage, "read", return_value=b"private")
	def test_private_response_does_not_cache(self, read):
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.utils.response import send_gcs_file

		frappe.local.request = MagicMock(environ=EnvironBuilder(path="/private/files/a.txt").get_environ())
		try:
			response = send_gcs_file("/private/files/a.txt", private=True)
			response.direct_passthrough = False
			self.assertEqual(response.get_data(), b"private")
			self.assertEqual(response.headers["Cache-Control"], "private, no-store")
		finally:
			del frappe.local.request

	@patch.object(storage, "read")
	def test_guest_cannot_download_private_file(self, read):
		from werkzeug.exceptions import Forbidden

		import frappe
		from frappe.utils.response import download_private_file

		frappe.local.session = frappe._dict(user="Guest")
		try:
			with self.assertRaises(Forbidden), patch("frappe.utils.response._", lambda value: value):
				download_private_file("/private/files/secret.pdf")
			read.assert_not_called()
		finally:
			del frappe.local.session

	@patch.object(storage, "read", return_value=b"<svg/>")
	@patch.object(storage, "public_blocked", return_value=False)
	def test_active_content_is_downloaded(self, public_blocked, read):
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.utils.response import send_gcs_file

		frappe.local.request = MagicMock(environ=EnvironBuilder(path="/files/a.svg").get_environ())
		try:
			db = MagicMock()
			db.get_values.return_value = ["/files/a.svg"]
			with patch.object(frappe, "db", db):
				response = send_gcs_file("/files/a.svg")
			self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
		finally:
			del frappe.local.request

	@patch.object(storage, "read")
	@patch.object(storage, "public_blocked", return_value=False)
	@patch.object(storage, "unmanaged_public_generation", return_value=None)
	def test_private_conversion_blocks_old_public_url(self, unmanaged, public_blocked, read):
		from werkzeug.exceptions import NotFound

		import frappe
		from frappe.utils.response import send_gcs_file

		db = MagicMock()
		db.exists.return_value = False
		with patch.object(frappe, "db", db), self.assertRaises(NotFound):
			send_gcs_file("/files/secret.pdf")
		read.assert_not_called()

	@patch.object(storage, "read_versioned", return_value=(b"asset", 12))
	@patch.object(storage, "unmanaged_public_generation", return_value="12")
	@patch.object(storage, "public_blocked", return_value=False)
	def test_unmanaged_public_file_requires_matching_generation(self, blocked, unmanaged, read):
		from werkzeug.exceptions import NotFound
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.utils.response import send_gcs_file

		db = MagicMock()
		db.exists.return_value = False
		frappe.local.request = MagicMock(environ=EnvironBuilder(path="/files/asset.css").get_environ())
		try:
			with patch.object(frappe, "db", db):
				response = send_gcs_file("/files/asset.css")
				response.direct_passthrough = False
				self.assertEqual(response.get_data(), b"asset")
				read.return_value = (b"changed", 13)
				with self.assertRaises(NotFound):
					send_gcs_file("/files/asset.css")
		finally:
			del frappe.local.request

	@patch.object(storage, "read")
	@patch.object(storage, "public_blocked", return_value=True)
	def test_public_tombstone_blocks_url_even_if_private_record_was_deleted(self, public_blocked, read):
		from werkzeug.exceptions import NotFound

		from frappe.utils.response import send_gcs_file

		with self.assertRaises(NotFound):
			send_gcs_file("/files/secret.pdf")
		read.assert_not_called()

	def test_local_public_route_preserves_download_and_rejects_escape(self):
		import tempfile
		from pathlib import Path

		from werkzeug.exceptions import NotFound
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.utils.response import send_public_file

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "files"
			root.mkdir()
			(root / "image.svg").write_bytes(b"<svg/>")
			(Path(directory) / "secret").write_bytes(b"private")
			frappe.local.request = MagicMock(environ=EnvironBuilder(path="/files/image.svg").get_environ())
			try:
				with patch.object(frappe, "get_site_path", return_value=str(root)), patch.object(
					frappe, "log_error"
				):
					response = send_public_file("/files/image.svg")
					self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
					response.close()
					with self.assertRaises(NotFound):
						send_public_file("/files/../secret")
			finally:
				del frappe.local.request


class TestImageStorage(unittest.TestCase):
	def setUp(self):
		guard = patch("frappe.core.doctype.file.file.register_object")
		guard.start()
		self.addCleanup(guard.stop)

	@patch.object(storage, "write")
	@patch.object(storage, "exists", return_value=False)
	@patch.object(storage, "enabled", return_value=True)
	def test_private_thumbnail_stays_private(self, enabled, exists, write):
		import io

		from PIL import Image

		import frappe
		from frappe.core.doctype.file.file import File

		image_data = io.BytesIO()
		Image.new("RGB", (10, 10)).save(image_data, format="PNG")
		file = MagicMock()
		file.file_url = "/private/files/photo.png"
		file.is_remote_file = False
		db = MagicMock()
		with patch.object(storage, "read", return_value=image_data.getvalue()), patch.object(frappe, "db", db):
			url = File.make_thumbnail(file)
		self.assertEqual(url, "/private/files/photo_small.png")
		self.assertEqual(write.call_args.args[0], url)
		self.assertTrue(write.call_args.args[1].startswith(b"\x89PNG"))
		self.assertEqual(write.call_args.kwargs["if_generation_match"], 0)
		file.db_set.assert_called_once_with("thumbnail_url", url)
		db.after_rollback.add.assert_called_once()

	@patch.object(storage, "delete")
	@patch.object(storage, "write", return_value=12)
	@patch.object(storage, "exists", return_value=False)
	@patch.object(storage, "enabled", return_value=True)
	def test_rolled_back_thumbnail_is_removed_after_outer_commit(self, enabled, exists, write, delete):
		import io

		from PIL import Image

		import frappe
		from frappe.core.doctype.file.file import File

		image_data = io.BytesIO()
		Image.new("RGB", (10, 10)).save(image_data, format="PNG")
		file = MagicMock(file_url="/private/files/photo.png", is_remote_file=False)
		db = MagicMock()
		db.exists.return_value = False
		with patch.object(storage, "read", return_value=image_data.getvalue()), patch.object(frappe, "db", db):
			url = File.make_thumbnail(file)
			db.after_commit.add.call_args.args[0]()
		delete.assert_called_once_with(url, if_generation_match=12)

	@patch.object(storage, "write")
	@patch.object(storage, "exists", return_value=False)
	@patch.object(storage, "enabled", return_value=True)
	def test_remote_image_thumbnail_is_written_to_bucket(self, enabled, exists, write):
		from PIL import Image

		import frappe
		from frappe.core.doctype.file.file import File

		file = MagicMock()
		file.file_url = "https://example.com/photo.png"
		file.is_remote_file = True
		db = MagicMock()
		with patch.object(frappe, "db", db), patch(
			"frappe.core.doctype.file.file.get_web_image",
			return_value=(Image.new("RGB", (10, 10)), "/files/photo", "png"),
		):
			self.assertEqual(File.make_thumbnail(file), "/files/photo_small.png")
		write.assert_called_once()
		self.assertEqual(write.call_args.args[0], "/files/photo_small.png")

	def test_private_thumbnail_requires_source_permission(self):
		import frappe
		from frappe.core.doctype.file.utils import find_file_by_url

		private_file = MagicMock(is_private=True)
		private_file.is_downloadable.return_value = False
		with patch.object(storage, "enabled", return_value=False), patch.object(frappe, "get_all", side_effect=[[], [{"name": "private"}]]), patch.object(
			frappe, "get_doc", return_value=private_file
		):
			self.assertIsNone(find_file_by_url("/private/files/photo_small.png"))

	def test_extra_private_thumbnail_requires_source_permission(self):
		import frappe
		from frappe.core.doctype.file.utils import find_file_by_url

		source = MagicMock(is_private=True)
		entry = frappe._dict(source_file="source", generation="12")
		with patch.object(storage, "enabled", return_value=True), patch.object(storage, "private_thumbnail", return_value=entry), patch.object(
			frappe, "get_all", side_effect=[[], [], [{"name": "source"}], [], [], [{"name": "source"}]]
		), patch.object(frappe, "get_doc", return_value=source):
			source.is_downloadable.return_value = False
			self.assertIsNone(find_file_by_url("/private/files/extra.png"))
			source.is_downloadable.return_value = True
			self.assertIs(find_file_by_url("/private/files/extra.png"), source)

	@patch.object(storage, "write", return_value=12)
	@patch.object(storage, "exists", return_value=False)
	@patch.object(storage, "enabled", return_value=True)
	def test_extra_private_thumbnail_registers_source_and_generation(self, enabled, exists, write):
		import io

		from PIL import Image

		import frappe
		from frappe.core.doctype.file.file import File

		image_data = io.BytesIO()
		Image.new("RGB", (10, 10)).save(image_data, format="PNG")
		file = MagicMock(file_url="/private/files/photo.png", is_remote_file=False)
		file.name = "source"
		db = MagicMock()
		with patch.object(storage, "read", return_value=image_data.getvalue()), patch.object(frappe, "db", db), patch.object(
			frappe, "get_doc"
		) as get_doc:
			url = File.make_thumbnail(file, set_as_thumbnail=False)
		self.assertEqual(get_doc.call_args.args[0]["source_file"], "source")
		self.assertEqual(get_doc.call_args.args[0]["generation"], "12")
		self.assertEqual(get_doc.call_args.args[0]["file_url"], url)
		file.db_set.assert_not_called()


class TestFrameworkConsumers(unittest.TestCase):
	@patch.object(storage, "enabled", return_value=True)
	def test_legacy_delete_helpers_schedule_generation_guarded_bucket_cleanup(self, enabled):
		import frappe
		from frappe.core.doctype.file.utils import delete_file as delete_core_file
		from frappe.utils import CallbackManager
		from frappe.utils.file_manager import delete_file as delete_manager_file

		db = MagicMock()
		db.after_commit = CallbackManager()
		db.exists.return_value = False
		db.get_value.return_value = None
		with patch.object(frappe, "db", db), patch(
			"frappe.integrations.doctype.google_cloud_storage_settings.cutover_lock.register_writer"
		), patch("frappe.integrations.doctype.google_cloud_storage_settings.cutover_lock.register_object"), patch.object(
			storage, "generation", return_value=12
		), patch.object(storage, "delete") as delete:
			delete_core_file("files/a.txt")
			delete_manager_file("/private/files/b.txt")
			delete.assert_not_called()
			db.after_commit.run()
		self.assertEqual(delete.call_count, 2)
		delete.assert_any_call("/files/a.txt", if_generation_match=12)
		delete.assert_any_call("/private/files/b.txt", if_generation_match=12)

	@patch.object(storage, "enabled", return_value=True)
	def test_legacy_delete_data_delegates_to_cloud_aware_file(self, enabled):
		from frappe.utils.file_manager import delete_file_data_content

		file = MagicMock()
		delete_file_data_content(file, only_thumbnail=True)
		file.delete_file_data_content.assert_called_once_with(only_thumbnail=True)

	@patch.object(storage, "enabled", return_value=True)
	def test_email_and_typst_reject_changed_private_thumbnail(self, enabled):
		from google.api_core.exceptions import PreconditionFailed

		import frappe
		from frappe.email.email_body import get_filecontent_from_path
		from frappe.utils.typst_emitter import TypstEmitter

		file = MagicMock(file_url="/private/files/original.png", thumbnail_url=None)
		file.name = "source"
		entry = frappe._dict(source_file="source", generation="12")
		with patch("frappe.core.doctype.file.utils.find_file_by_url", return_value=file), patch.object(
			storage, "private_thumbnail", return_value=entry
		), patch.object(storage, "read_versioned", side_effect=PreconditionFailed("changed")):
			self.assertIsNone(get_filecontent_from_path("/private/files/extra.png"))
			self.assertIsNone(TypstEmitter._read_site_file(None, "/private/files/extra.png"))

	@patch.object(storage, "enabled", return_value=True)
	def test_wkhtmltopdf_subprocess_uses_temporary_memory_environment(self, enabled):
		from pathlib import Path

		from frappe.utils.pdf import FrappePDFKit, OriginalPDFKit

		kit = object.__new__(FrappePDFKit)
		kit.environ = {"PATH": "/usr/bin"}
		def render(instance, path):
			directory = Path(instance.environ["TMPDIR"])
			self.assertTrue(directory.is_relative_to("/dev/shm") and directory.is_dir())
			self.assertEqual(instance.environ["XDG_CACHE_HOME"], str(directory))
			self.assertEqual(instance.environ["XDG_RUNTIME_DIR"], str(directory))
			return str(directory)
		with patch.object(OriginalPDFKit, "to_pdf", render):
			directory = kit.to_pdf()
		self.assertFalse(Path(directory).exists())
		self.assertEqual(kit.environ, {"PATH": "/usr/bin"})

	def test_chromium_startup_failure_does_not_wait_for_timeout(self):
		from frappe.utils.pdf_generator.chrome_pdf_generator import ChromePDFGenerator

		generator = object.__new__(ChromePDFGenerator)
		generator._chromium_process = MagicMock()
		generator._chromium_process.stderr.readline.return_value = ""
		generator._chromium_process.poll.return_value = 127
		generator._devtools_url = None
		generator.START_TIMEOUT = 60
		with self.assertRaisesRegex(RuntimeError, "Chromium exited"):
			generator._set_devtools_url()
		generator._chromium_process.stderr.readline.assert_called_once()

	@patch.object(storage, "enabled", return_value=True)
	def test_chromium_uses_memory_backed_profile_and_removes_it(self, enabled):
		from pathlib import Path

		import frappe
		from frappe.utils.pdf_generator.chrome_pdf_generator import ChromePDFGenerator

		generator = object.__new__(ChromePDFGenerator)
		generator._chromium_path = "/usr/bin/chromium"
		generator._chromium_process = MagicMock()
		generator._browsers = []
		try:
			with patch.object(generator, "_start_chromium_process") as start, patch.object(frappe, "log"):
				generator.start_chromium_process()
				root = Path(generator._memory_workspace.name)
				args = start.call_args.args[0]
				self.assertTrue(root.is_relative_to("/dev/shm"))
				self.assertIn(f"--user-data-dir={root}/profile", args)
				self.assertIn(f"--disk-cache-dir={root}/cache", args)
				self.assertNotIn("--disable-dev-shm-usage", args)
				self.assertEqual(generator._chromium_env["TMPDIR"], str(root))
				generator._close_browser()
				self.assertFalse(root.exists())
		finally:
			if workspace := getattr(generator, "_memory_workspace", None):
				workspace.cleanup()

	@patch.object(storage, "enabled", return_value=True)
	def test_wkhtmltopdf_inlines_private_thumbnail_bytes(self, enabled):
		from frappe.utils.pdf import _get_base64_image

		file = MagicMock(is_private=True, file_url="/private/files/original.png", thumbnail_url="/private/files/small.png")
		with patch("frappe.utils.pdf.find_file_by_url", return_value=file), patch.object(
			storage, "read", return_value=b"thumbnail"
		) as read:
			self.assertEqual(_get_base64_image("/private/files/small.png"), "data:image/png;base64,dGh1bWJuYWls")
		read.assert_called_once_with("/private/files/small.png")
		file.get_content.assert_not_called()

	@patch.object(storage, "enabled", return_value=True)
	def test_wkhtmltopdf_cookie_and_header_use_tmpfs_and_are_removed(self, enabled):
		from pathlib import Path

		from bs4 import BeautifulSoup

		import frappe
		from frappe.utils.pdf import cleanup, get_cookie_options, prepare_header_footer

		old_session = getattr(frappe.local, "session", None)
		old_request = getattr(frappe.local, "request", None)
		old_sites_path = getattr(frappe.local, "sites_path", None)
		frappe.local.session = frappe._dict(sid="session-id")
		frappe.local.request = MagicMock()
		frappe.local.sites_path = "/site"
		options = {}
		try:
			with patch("frappe.utils.get_host_name", return_value="example.test"), patch(
				"frappe.utils.pdf.bundled_asset", return_value="/assets/print.css"
			), patch.object(frappe, "read_file", return_value=""), patch.object(
				frappe, "get_hooks", return_value=["hook"]
			), patch.object(frappe, "call", return_value="<p>header</p>"):
				options.update(get_cookie_options())
				options.update(prepare_header_footer(BeautifulSoup("<html><head></head><body><div id='header-html'>Header</div></body></html>", "html.parser")))
			paths = [Path(options["cookie-jar"]), Path(options["header-html"])]
			self.assertTrue(all(path.is_relative_to("/dev/shm") and path.is_file() for path in paths))
			cleanup(options)
			self.assertTrue(all(not path.exists() for path in paths))
		finally:
			cleanup(options)
			frappe.local.session = old_session
			frappe.local.request = old_request
			frappe.local.sites_path = old_sites_path

	@patch.object(storage, "enabled", return_value=True)
	def test_typst_uses_memory_backed_font_cache_and_compile_workspace(self, enabled):
		import os
		import shutil
		from pathlib import Path

		import frappe
		from frappe.utils.typst_emitter import compile_typst_source, typst_font_root

		old_site = getattr(frappe.local, "site", None)
		frappe.local.site = "test-typst-storage"
		root = None
		paths = []
		try:
			with patch.object(frappe, "get_site_path", return_value="/test/typst-storage"), patch(
				"frappe.utils.typst_emitter.typst_font_paths", return_value=[]
			), patch("typst.compile", side_effect=lambda path, **kwargs: paths.append(path) or b"pdf"):
				root = Path(typst_font_root())
				self.assertTrue(root.is_relative_to("/dev/shm"))
				self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
				self.assertEqual(compile_typst_source("test"), b"pdf")
				self.assertTrue(Path(paths[0]).is_relative_to("/dev/shm"))
				self.assertFalse(Path(paths[0]).exists())
		finally:
			if root:
				shutil.rmtree(root)
			frappe.local.site = old_site

	def test_package_import_rejects_oversized_archive_before_extracting(self):
		import io
		import tarfile
		import tempfile
		from pathlib import Path

		import frappe
		from frappe.core.doctype.package_import.package_import import PackageImport

		content = io.BytesIO()
		with tarfile.open(fileobj=content, mode="w:gz") as archive:
			info = tarfile.TarInfo("demo/demo.json")
			info.size = 4
			archive.addfile(info, io.BytesIO(b"data"))
		content.seek(0)
		with tempfile.TemporaryDirectory(dir="/dev/shm") as directory, patch(
			"frappe.core.api.file.get_max_extract_size", return_value=3
		), patch.object(frappe, "_", lambda value: value), patch.object(frappe, "throw", side_effect=ValueError):
			with self.assertRaises(ValueError):
				PackageImport.import_archive(MagicMock(), {"fileobj": content}, directory, "demo")
			self.assertEqual(list(Path(directory).iterdir()), [])

	@patch.object(storage, "enabled", return_value=True)
	def test_package_import_uses_and_cleans_tmpfs_workspace(self, enabled):
		import io
		import tarfile
		from pathlib import Path

		import frappe
		from frappe.core.doctype.package_import.package_import import PackageImport

		content = io.BytesIO()
		with tarfile.open(fileobj=content, mode="w:gz") as archive:
			for name, data in (("demo/demo.json", b'{"doctype":"Package","name":"Demo"}'),
				("demo/module/doctype/example/example.json", b'{"doctype":"DocType","name":"Example"}')):
				info = tarfile.TarInfo(name)
				info.size = len(data)
				archive.addfile(info, io.BytesIO(data))
		attachment = frappe._dict(file_name="demo-1.0.0.tar.gz", file_url="/private/files/demo.tar.gz")
		obj = MagicMock()
		obj.doctype, obj.name, obj.force = "Package Import", "import-1", False
		obj.import_archive.side_effect = lambda *args: PackageImport.import_archive(obj, *args)
		old_flags = getattr(frappe.local, "flags", None)
		seen = []
		def import_file(path, **kwargs):
			seen.append(Path(path))
			self.assertTrue(Path(path).is_file())
		try:
			frappe.local.flags = frappe._dict()
			with patch.object(storage, "read", return_value=content.getvalue()), patch(
				"frappe.core.doctype.package_import.package_import.get_attachments", return_value=[attachment]
			), patch("frappe.core.api.file.get_max_extract_size", return_value=1024), patch(
				"frappe.core.doctype.package_import.package_import.import_doc", return_value="package"
			), patch(
				"frappe.core.doctype.package_import.package_import.get_doc_files",
				side_effect=lambda files, module_path: [str(Path(module_path) / "doctype/example/example.json")],
			), patch("frappe.core.doctype.package_import.package_import.import_file_by_path", side_effect=import_file), patch.object(
				frappe, "get_site_path", side_effect=AssertionError("site disk used")
			):
				PackageImport.import_package(obj)
			self.assertEqual(len(seen), 1)
			self.assertTrue(seen[0].is_relative_to("/dev/shm"))
			self.assertFalse(seen[0].exists())
		finally:
			frappe.local.flags = old_flags

	@patch.object(storage, "enabled", return_value=True)
	def test_package_release_exports_documents_without_creating_staging_files(self, enabled):
		import io
		import json
		import tarfile

		import frappe
		from frappe.core.doctype.package_release.package_release import PackageRelease

		release = MagicMock()
		release.name, release.doctype, release.package = "demo-1.0.0", "Package Release", "Demo"
		release.export_to_bucket.side_effect = lambda package: PackageRelease.export_to_bucket(release, package)
		package = MagicMock(package_name="demo", readme="readme", license=None)
		package.as_dict.return_value = {"doctype": "Package", "name": "Demo"}
		module = MagicMock()
		module.meta.links = [frappe._dict(link_doctype="Server Script")]
		doc = MagicMock()
		doc.doctype, doc.name, doc.module = "Server Script", "Example", "Demo Module"
		doc.as_dict.return_value = {"doctype": "Server Script", "name": "Example", "script": "x = 1"}
		doc.meta.get_table_fields.return_value = []
		doc.get_code_fields.return_value = {"script": "py"}
		doc.get.side_effect = lambda key: "x = 1" if key == "script" else None
		file = MagicMock(file_url="/files/demo.tar.gz")
		file.flags = frappe._dict()
		db = MagicMock()
		db.get_value.return_value = "Demo"
		with patch.object(frappe, "db", db), patch.object(frappe, "get_all", side_effect=[
			[frappe._dict(name="Demo Module")], [frappe._dict(name="Example")]
		]), patch.object(frappe, "get_doc", side_effect=[module, doc, file]) as get_doc, patch.object(
			frappe, "create_folder", side_effect=AssertionError("staging folder created")
		):
			PackageRelease.export_files(release, package)
		payload = get_doc.call_args.args[0]["content"]
		with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
			base = "demo/demo_module/server_script/example/"
			self.assertEqual(archive.extractfile(base + "example.py").read(), b"x = 1")
			doc_json = json.loads(archive.extractfile(base + "example.json").read())
			self.assertNotIn("script", doc_json)
			self.assertEqual(archive.extractfile("demo/README.md").read(), b"readme")
		self.assertEqual(release.path, file.file_url)

	@patch.object(storage, "enabled", return_value=True)
	def test_package_release_creates_bucket_attachment_without_local_archive(self, enabled):
		import io
		import tarfile
		import tempfile
		from pathlib import Path

		import frappe
		from frappe.core.doctype.package_release.package_release import PackageRelease

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "packages" / "demo").mkdir(parents=True)
			(root / "packages" / "demo" / "demo.json").write_text("{}")
			release = MagicMock()
			release.name, release.doctype = "demo-1.0.0", "Package Release"
			file = MagicMock(file_url="/files/demo-1.0.0.tar.gz")
			file.flags = frappe._dict()
			with patch.object(frappe, "get_site_path", side_effect=lambda *parts: str(root.joinpath(*parts))), patch.object(
				frappe, "get_doc", return_value=file
			) as get_doc, patch("subprocess.check_output") as subprocess_call:
				PackageRelease.make_tarfile(release, frappe._dict(package_name="demo"))
				payload = get_doc.call_args.args[0]["content"]
			with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
				self.assertEqual(archive.extractfile("demo/demo.json").read(), b"{}")
			self.assertEqual(release.path, file.file_url)
			self.assertFalse((root / "packages" / "demo-1.0.0.tar.gz").exists())
			subprocess_call.assert_not_called()
			file.insert.assert_called_once()

	@patch.object(storage, "enabled", return_value=True)
	def test_email_embeds_public_and_authorized_private_images(self, enabled):
		from frappe.email.email_body import get_filecontent_from_path

		file = MagicMock(file_url="/private/files/a.png", thumbnail_url=None)
		with patch.object(storage, "read_public", return_value=b"public"), patch.object(
			storage, "read", return_value=b"private"
		) as read, patch("frappe.core.doctype.file.utils.find_file_by_url", side_effect=[file, None]):
			self.assertEqual(get_filecontent_from_path("/files/a.png"), b"public")
			self.assertEqual(get_filecontent_from_path("/private/files/a.png"), b"private")
			self.assertIsNone(get_filecontent_from_path("/private/files/a.png"))
		read.assert_called_once_with("/private/files/a.png")

	@patch.object(storage, "enabled", return_value=True)
	def test_chrome_pdf_interceptor_fetches_bucket_files_and_denies_private_guests(self, enabled):
		import frappe
		from frappe.utils.pdf_generator.page import Page

		session = MagicMock()
		page = object.__new__(Page)
		page.session, page.session_id, page.target_id, page.frame_id = session, "s", "t", "f"
		frappe.local.session = frappe._dict(user="Guest")
		try:
			with patch("frappe.utils.pdf_generator.page.get_host_url", return_value="https://site/"), patch(
				"frappe.utils.get_bench_path", return_value="/bench"
			), patch.object(frappe.utils, "get_site_path", return_value="/site"), patch.object(
				storage, "read_public", return_value=b"public"
			):
				page.intercept_request_for_local_resources()
				callback = session.start_listener.call_args.args[1]
				for path in ("files/a.png", "private/files/a.png"):
					callback(None, {"params": {"requestId": path, "request": {"url": "https://site/" + path}}})
			self.assertEqual(session.send.call_args_list[-2].args[0], "Fetch.fulfillRequest")
			self.assertEqual(session.send.call_args_list[-2].args[1]["body"], "cHVibGlj")
			self.assertEqual(session.send.call_args_list[-1].args[0], "Fetch.failRequest")
		finally:
			del frappe.local.session

	@patch.object(storage, "enabled", return_value=True)
	def test_typst_reads_public_and_authorized_private_images_from_bucket(self, enabled):
		from frappe.utils.typst_emitter import TypstEmitter

		file = MagicMock(file_url="/private/files/a.png", thumbnail_url=None)
		with patch.object(storage, "read_public", return_value=b"public") as public, patch.object(
			storage, "read", return_value=b"private"
		) as private, patch("frappe.core.doctype.file.utils.find_file_by_url", side_effect=[file, None]):
			self.assertEqual(TypstEmitter._read_site_file(None, "/files/a.png"), b"public")
			self.assertEqual(TypstEmitter._read_site_file(None, "/private/files/a.png"), b"private")
			self.assertIsNone(TypstEmitter._read_site_file(None, "/private/files/a.png"))
		public.assert_called_once_with("/files/a.png")
		private.assert_called_once_with("/private/files/a.png")

	@patch.object(storage, "enabled", return_value=True)
	@patch.object(storage, "read", return_value=b"%PDF-1.7\n")
	def test_file_manager_reads_private_bytes_from_bucket(self, read, enabled):
		from frappe.utils.file_manager import read_file_bytes

		with patch("frappe.utils.file_manager.get_files_path", side_effect=lambda is_private: (
			"/site/private/files" if is_private else "/site/public/files"
		)):
			self.assertEqual(read_file_bytes("/site/private/files/invoice.pdf"), b"%PDF-1.7\n")
		read.assert_called_once_with("/private/files/invoice.pdf")

	@patch.object(storage, "enabled", return_value=True)
	def test_attached_xlsx_is_read_from_bucket(self, enabled):
		import io

		import openpyxl

		import frappe
		from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

		output = io.BytesIO()
		workbook = openpyxl.Workbook()
		workbook.active.append(["A", 42])
		workbook.save(output)
		file = MagicMock(file_url="/private/files/a.xlsx")
		with patch.object(frappe, "get_doc", return_value=file), patch.object(storage, "read", return_value=output.getvalue()):
			self.assertEqual(read_xlsx_file_from_attached_file(file_url=file.file_url), [["A", 42]])
		file.get_full_path.assert_not_called()

	@patch.object(storage, "write", return_value=12)
	@patch.object(storage, "exists", return_value=False)
	@patch.object(storage, "enabled", return_value=True)
	def test_website_theme_compiles_to_bucket_without_creating_a_file(self, enabled, exists, write):
		import frappe
		from frappe.website.doctype.website_theme.website_theme import WebsiteTheme

		theme = MagicMock()
		theme.name = "Test Theme"
		theme.custom = False
		process = MagicMock()
		process.communicate.return_value = (b".x{color:red}", b"")
		db = MagicMock()
		with patch.object(frappe, "db", db), patch.object(frappe, "msgprint"), patch.object(
			frappe, "get_app_source_path", return_value="/app"
		), patch("frappe.website.doctype.website_theme.website_theme.get_scss", return_value=".x {color: red}"), patch(
			"frappe.website.doctype.website_theme.website_theme._", lambda value: value
		), patch("frappe.utils.get_files_path", return_value="/site/public/files/website_theme"), patch(
			"subprocess.Popen", return_value=process
		) as popen, patch.object(frappe, "create_folder") as create_folder, patch(
			"frappe.integrations.doctype.google_cloud_storage_settings.cutover_lock.register_object"
		), patch.object(storage, "allow_unmanaged_public") as allow:
			WebsiteTheme.generate_bootstrap_theme(theme)
		self.assertEqual(popen.call_args.args[0][2], "-")
		self.assertRegex(theme.theme_url, r"^/files/website_theme/test_theme_[a-z0-9]{8}\.css$")
		write.assert_called_once_with(theme.theme_url, b".x{color:red}", content_type="text/css", if_generation_match=0)
		allow.assert_called_once_with(theme.theme_url, 12)
		create_folder.assert_not_called()
