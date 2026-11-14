import unittest
from datetime import UTC
from uuid import uuid4

from frappe.integrations.doctype.google_cloud_storage_settings.chunk_upload import (
	clean_expired_chunks,
	remove_staged_chunks,
	stage_chunk,
)
from frappe.integrations.doctype.google_cloud_storage_settings.test_migrate_files import Bucket


class TestChunkUpload(unittest.TestCase):
	def setUp(self):
		self.bucket = Bucket()
		self.upload_id = str(uuid4())
		self.context = {"filename": "private.pdf", "private": 1, "doctype": "Employee", "docname": "E-1"}

	def send(self, index, content, *, offset, **overrides):
		args = {
			"session": "session-secret",
			"upload_id": self.upload_id,
			"context": self.context,
			"index": index,
			"count": 2,
			"offset": offset,
			"total_size": 6,
			"content": content,
			"max_size": 10,
		}
		args.update(overrides)
		return stage_chunk(self.bucket, "site", **args)

	def test_chunked_upload_assembles_and_deletes_temporary_objects(self):
		self.assertEqual(self.send(0, b"abcd", offset=0), (None, None))
		self.assertEqual(self.send(0, b"abcd", offset=0), (None, None))
		content, objects = self.send(1, b"ef", offset=4)
		self.assertEqual(content, b"abcdef")
		self.assertEqual(len(objects), 3)
		self.assertTrue(all("session-secret" not in name for name, _ in objects))
		remove_staged_chunks(self.bucket, objects)
		self.assertEqual(self.bucket.objects, {})

	def test_upload_metadata_cannot_change_between_chunks(self):
		self.send(0, b"abcd", offset=0)
		with self.assertRaisesRegex(ValueError, "parameters"):
			self.send(1, b"ef", offset=4, context={"filename": "private.pdf", "private": 0})
		with self.assertRaisesRegex(ValueError, "order"):
			self.send(1, b"ef", offset=3)

	def test_replayed_chunk_must_have_identical_content(self):
		self.send(0, b"abcd", offset=0)
		with self.assertRaisesRegex(ValueError, "different chunk"):
			self.send(0, b"xxxx", offset=0)
		with self.assertRaisesRegex(ValueError, "different chunk"):
			self.send(0, b"abcd", offset=1)

	def test_rejects_bad_upload_id_and_oversized_file(self):
		with self.assertRaises(ValueError):
			self.send(0, b"abcd", offset=0, upload_id="../other")
		with self.assertRaises(ValueError):
			self.send(0, b"abcd", offset=0, max_size=5)
		self.assertEqual(self.bucket.objects, {})

	def test_different_session_cannot_finish_an_upload(self):
		self.send(0, b"abcd", offset=0)
		with self.assertRaisesRegex(ValueError, "missing or has expired"):
			self.send(1, b"ef", offset=4, session="another-session")

	def test_abandoned_uploads_expire_without_touching_active_or_unrelated_objects(self):
		from datetime import datetime, timedelta, timezone

		self.send(0, b"abcd", offset=0)
		now = datetime.now(UTC)
		old_objects = list(self.bucket.objects)
		for key in old_objects:
			self.bucket.updated[key] = now - timedelta(days=2)
		self.upload_id = str(uuid4())
		self.send(0, b"abcd", offset=0)
		active_objects = set(self.bucket.objects) - set(old_objects)
		self.bucket.blob("site/.uploads/unrelated").upload_from_string(b"keep", if_generation_match=0)
		self.assertEqual(clean_expired_chunks(self.bucket, "site", now=now), 2)
		self.assertTrue(active_objects.issubset(self.bucket.objects))
		self.assertIn("site/.uploads/unrelated", self.bucket.objects)


class TestUploadHandler(unittest.TestCase):
	def test_chunked_handler_saves_only_after_final_chunk_and_cleans_after_commit(self):
		import io
		import mimetypes
		from types import SimpleNamespace
		from unittest.mock import MagicMock, patch

		import frappe
		from frappe import handler
		from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock, storage

		bucket = Bucket()
		upload_id = str(uuid4())
		form = frappe._dict(
			is_private=1, doctype=None, docname=None, fieldname=None, file_url=None, folder="Home",
			method=None, file_name="a.pdf", optimize=0, total_chunk_count=2, total_file_size=6,
			chunk_index=0, chunk_byte_offset=0, upload_id=upload_id,
		)
		request = SimpleNamespace(files={"file": SimpleNamespace(filename="a.pdf", stream=io.BytesIO(b"abcd"))})
		session = frappe._dict(user="Administrator", sid="secret")
		user = MagicMock()
		user.has_desk_access.return_value = True
		file_doc = MagicMock()
		file_doc.save.return_value = "saved-file"
		db = MagicMock()
		mimetypes.init()
		with (
			patch.object(frappe, "request", request), patch.object(frappe, "session", session),
			patch.object(frappe, "form_dict", form), patch.object(frappe, "db", db),
			patch.object(frappe, "get_lazy_doc", return_value=user), patch.object(frappe, "get_doc", return_value=file_doc) as get_doc,
			patch.object(handler, "check_write_permission"), patch.object(storage, "enabled", return_value=True),
			patch.object(storage, "staging_bucket", return_value=(bucket, "site")), patch.object(cutover_lock, "register_writer"),
			patch("frappe.core.api.file.get_max_file_size", return_value=10),
			patch("builtins.open", side_effect=AssertionError("local file access")),
		):
			self.assertIsNone(handler.upload_file())
			get_doc.assert_not_called()
			db.after_commit.add.assert_not_called()
			form.chunk_index = 1
			form.chunk_byte_offset = 4
			request.files["file"].stream = io.BytesIO(b"ef")
			self.assertEqual(handler.upload_file(), "saved-file")
			self.assertEqual(get_doc.call_args.args[0]["content"], b"abcdef")
			self.assertEqual(get_doc.call_args.args[0]["is_private"], 1)
			db.after_commit.add.call_args.args[0]()
			self.assertEqual(bucket.objects, {})
