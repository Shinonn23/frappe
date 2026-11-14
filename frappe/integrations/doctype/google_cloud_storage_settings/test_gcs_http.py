"""Exercise the installed GCS SDK against an isolated local JSON API stub."""

import base64
import json
import re
import struct
import tempfile
import threading
import unittest
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import google_crc32c
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock
from frappe.integrations.doctype.google_cloud_storage_settings import (
	google_cloud_storage_settings as settings,
)
from frappe.integrations.doctype.google_cloud_storage_settings import storage as file_storage
from frappe.integrations.doctype.google_cloud_storage_settings.chunk_upload import (
	remove_staged_chunks,
	stage_chunk,
)
from frappe.integrations.doctype.google_cloud_storage_settings.cutover import perform_cutover


class API(BaseHTTPRequestHandler):
	def log_message(self, *args):
		pass

	def respond(self, status, body=b"", content_type="application/json", headers=None):
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		for name, value in (headers or {}).items():
			self.send_header(name, value)
		self.end_headers()
		self.wfile.write(body)

	def error(self, status):
		self.respond(status, json.dumps({"error": {"code": status, "message": "test stub error"}}).encode())

	def meta(self, name):
		item = self.server.objects[name]
		checksum = base64.b64encode(struct.pack(">I", google_crc32c.value(item["data"]))).decode()
		return {"bucket": "bucket", "name": name, "generation": str(item["generation"]),
			"size": str(len(item["data"])), "metadata": item["metadata"], "updated": "2026-01-01T00:00:00Z", "crc32c": checksum}

	def matches(self, name, query):
		expected = query.get("ifGenerationMatch")
		actual = self.server.objects.get(name, {}).get("generation", 0)
		return not expected or int(expected[0]) == actual

	def do_GET(self):
		parsed = urlsplit(self.path)
		query = parse_qs(parsed.query)
		if parsed.path == "/storage/v1/b/bucket":
			return self.respond(200, json.dumps({"name": "bucket", "iamConfiguration": {"publicAccessPrevention": self.server.public_access_prevention}}).encode())
		if parsed.path == "/storage/v1/b/bucket/iam/testPermissions":
			requested = query.get("permissions", [])
			allowed = self.server.permissions
			return self.respond(200, json.dumps({"permissions": [p for p in requested if allowed is None or p in allowed]}).encode())
		match = re.fullmatch(r"/(?:download/)?storage/v1/b/bucket/o/(.*)", parsed.path)
		if match:
			name = unquote(match.group(1))
			if name not in self.server.objects:
				return self.error(404)
			if not self.matches(name, query):
				return self.error(412)
			if query.get("alt") == ["media"]:
				metadata = self.meta(name)
				return self.respond(200, self.server.objects[name]["data"], "application/octet-stream", {
					"x-goog-generation": metadata["generation"], "x-goog-hash": "crc32c=" + metadata["crc32c"]
				})
			return self.respond(200, json.dumps(self.meta(name)).encode())
		if parsed.path == "/storage/v1/b/bucket/o":
			prefix = query.get("prefix", [""])[0]
			items = [self.meta(name) for name in sorted(self.server.objects) if name.startswith(prefix)]
			return self.respond(200, json.dumps({"items": items}).encode())
		return self.error(404)

	def do_POST(self):
		parsed = urlsplit(self.path)
		query = parse_qs(parsed.query)
		copy = re.fullmatch(r"/storage/v1/b/bucket/o/(.*)/copyTo/b/bucket/o/(.*)", parsed.path)
		if copy:
			source, target = map(unquote, copy.groups())
			if source not in self.server.objects:
				return self.error(404)
			if not self.matches(target, query) or (
				query.get("ifSourceGenerationMatch")
				and int(query["ifSourceGenerationMatch"][0]) != self.server.objects[source]["generation"]
			):
				return self.error(412)
			item = self.server.objects[source]
			generation = self.server.objects.get(target, {}).get("generation", 0) + 1
			self.server.objects[target] = {"data": item["data"], "metadata": item["metadata"].copy(), "generation": generation}
			return self.respond(200, json.dumps(self.meta(target)).encode())
		if parsed.path != "/upload/storage/v1/b/bucket/o":
			return self.error(404)
		body = self.rfile.read(int(self.headers["Content-Length"]))
		if query.get("uploadType") == ["resumable"]:
			metadata = json.loads(body)
			name = metadata["name"]
			if not self.matches(name, query):
				return self.error(412)
			session = uuid4().hex
			self.server.sessions[session] = (metadata, query)
			self.send_response(200)
			self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/session/{session}")
			self.send_header("Content-Length", "0")
			self.end_headers()
			return
		message = BytesParser(policy=policy.default).parsebytes(
			b"MIME-Version: 1.0\r\nContent-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
		)
		parts = list(message.iter_parts())
		metadata = json.loads(parts[0].get_payload(decode=True))
		data = parts[1].get_payload(decode=True)
		name = metadata["name"]
		if not self.matches(name, query):
			return self.error(412)
		generation = self.server.objects.get(name, {}).get("generation", 0) + 1
		self.server.objects[name] = {"data": data, "generation": generation, "metadata": metadata.get("metadata", {})}
		self.respond(200, json.dumps(self.meta(name)).encode())

	def do_PUT(self):
		match = re.fullmatch(r"/session/([a-f0-9]+)", urlsplit(self.path).path)
		if not match or match.group(1) not in self.server.sessions:
			return self.error(404)
		metadata, query = self.server.sessions.pop(match.group(1))
		name = metadata["name"]
		if not self.matches(name, query):
			return self.error(412)
		data = self.rfile.read(int(self.headers["Content-Length"]))
		generation = self.server.objects.get(name, {}).get("generation", 0) + 1
		self.server.objects[name] = {"data": data, "generation": generation, "metadata": metadata.get("metadata", {})}
		self.respond(200, json.dumps(self.meta(name)).encode())

	def do_DELETE(self):
		parsed = urlsplit(self.path)
		match = re.fullmatch(r"/storage/v1/b/bucket/o/(.*)", parsed.path)
		if not match:
			return self.error(404)
		name = unquote(match.group(1))
		if name not in self.server.objects:
			return self.error(404)
		if not self.matches(name, parse_qs(parsed.query)):
			return self.error(412)
		del self.server.objects[name]
		self.respond(204)


class TestActualGcsSdk(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.server = ThreadingHTTPServer(("127.0.0.1", 0), API)
		cls.server.objects = {}
		cls.server.sessions = {}
		cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
		cls.thread.start()
		cls.client = storage.Client(project="test", credentials=AnonymousCredentials(), client_options={
			"api_endpoint": f"http://127.0.0.1:{cls.server.server_port}"
		})

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()
		cls.server.server_close()
		cls.thread.join()

	def setUp(self):
		self.server.objects.clear()
		self.server.sessions.clear()
		self.server.public_access_prevention = "enforced"
		self.server.permissions = None
		self.bucket = self.client.bucket("bucket")

	def test_migration_and_cleanup_use_sdk_generation_preconditions(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/a.txt").write_bytes(b"public")
			(root / "private/files/b.txt").write_bytes(b"private")
			activated = []
			with patch.object(cutover_lock, "LOCK_DIRECTORY", root):
				result = perform_cutover(
					root, owner="site.example", bench_path="/bench", refresh=lambda: None,
					target=lambda: (self.bucket, "site"), records=lambda: [], activate=lambda unmanaged: activated.append(True),
				)
			self.assertEqual(result["removed"], 2)
			self.assertEqual(activated, [True])
			self.assertEqual(self.bucket.blob("site/public/a.txt").download_as_bytes(), b"public")
			self.assertEqual(self.bucket.blob("site/private/b.txt").download_as_bytes(), b"private")

	def test_chunk_staging_uses_real_sdk_metadata_and_conditional_updates(self):
		common = dict(session="test-session", upload_id=str(uuid4()), context={"private": 1}, count=2,
			total_size=6, max_size=6)
		self.assertEqual(stage_chunk(self.bucket, "site", index=0, offset=0, content=b"abcd", **common), (None, None))
		content, objects = stage_chunk(self.bucket, "site", index=1, offset=4, content=b"ef", **common)
		self.assertEqual(content, b"abcdef")
		remove_staged_chunks(self.bucket, objects)
		self.assertEqual(self.server.objects, {})

	def test_visibility_copy_and_generation_guard_use_actual_sdk(self):
		def blob(file_url):
			return self.bucket.blob(file_storage.object_key("site", file_url))

		with patch.object(file_storage, "get_blob", side_effect=blob):
			generation = file_storage.write("/files/a.txt", b"contents", if_generation_match=0)
			source_generation, target_generation = file_storage.copy("/files/a.txt", "/private/files/a.txt")
			self.assertEqual(source_generation, generation)
			self.assertEqual(target_generation, 1)
			file_storage.delete("/files/a.txt", if_generation_match=source_generation)
			self.assertEqual(file_storage.read("/private/files/a.txt"), b"contents")

	def test_conditional_delete_accepts_stored_generation_string(self):
		url = "/private/files/extra.png"
		with patch.object(file_storage, "get_blob", return_value=self.bucket.blob(file_storage.object_key("site", url))):
			generation = file_storage.write(url, b"thumbnail", if_generation_match=0)
			file_storage.delete(url, if_generation_match=str(generation))
			self.assertNotIn(file_storage.object_key("site", url), self.server.objects)

	def test_integrity_check_reads_generation_pinned_sdk_objects(self):
		import hashlib

		from frappe.integrations.doctype.google_cloud_storage_settings.audit_files import check_object

		blob = self.bucket.blob("site/private/a.txt")
		blob.upload_from_string(b"data", if_generation_match=0)
		self.assertEqual(check_object(blob, hashlib.md5(b"data", usedforsecurity=False).hexdigest()), "ok")
		self.assertEqual(check_object(blob, "different"), "mismatch")
		self.assertEqual(check_object(self.bucket.blob("site/private/missing.txt"), "hash"), "missing")

	def test_private_thumbnail_generation_is_checked_with_actual_sdk(self):
		from werkzeug.exceptions import NotFound
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.utils.response import send_gcs_file

		url = "/private/files/extra.png"
		blob = self.bucket.blob(file_storage.object_key("site", url))
		frappe.local.request = SimpleNamespace(environ=EnvironBuilder(path=url).get_environ())
		try:
			with patch.object(file_storage, "get_blob", return_value=blob):
				generation = file_storage.write(url, b"first", if_generation_match=0)
				response = send_gcs_file(url, private=True, expected_generation=str(generation))
				response.direct_passthrough = False
				self.assertEqual(response.get_data(), b"first")
				file_storage.write(url, b"changed", if_generation_match=generation)
				with self.assertRaises(NotFound):
					send_gcs_file(url, private=True, expected_generation=str(generation))
		finally:
			del frappe.local.request

	def test_public_and_private_responses_use_isolated_sdk_objects(self):
		from werkzeug.exceptions import Forbidden, NotFound
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.utils.response import download_private_file, send_gcs_file

		def blob(file_url):
			return self.bucket.blob(file_storage.object_key("site", file_url))

		public_url, private_url = "/files/report.txt", "/private/files/report.txt"
		private_file = SimpleNamespace(name="file-1", file_name="report.txt", file_url=private_url, thumbnail_url=None)
		db = SimpleNamespace(get_values=lambda doctype, filters, field, **kwargs: [public_url] if field == "file_url" else [])
		frappe.local.request = SimpleNamespace(environ=EnvironBuilder(path=public_url).get_environ())
		frappe.local.session = frappe._dict(user="user@example.com")
		frappe.local.form_dict = frappe._dict()
		try:
			with patch.object(file_storage, "get_blob", side_effect=blob), patch.object(frappe, "db", db), patch.object(
				file_storage, "public_blocked", return_value=False
			), patch.object(file_storage, "enabled", return_value=True), patch(
				"frappe.utils.response.make_access_log"
			), patch("frappe.utils.response._", lambda value: value), patch(
				"frappe.core.doctype.file.utils.find_file_by_url", side_effect=[None, private_file]
			):
				file_storage.write(public_url, b"public", if_generation_match=0)
				file_storage.write(private_url, b"secret", if_generation_match=0)
				response = send_gcs_file(public_url)
				response.direct_passthrough = False
				self.assertEqual(response.get_data(), b"public")
				with self.assertRaises(Forbidden):
					download_private_file(private_url)
				response = download_private_file(private_url)
				response.direct_passthrough = False
				self.assertEqual(response.get_data(), b"secret")
				self.assertEqual(response.headers["Cache-Control"], "private, no-store")
				with patch.object(file_storage, "public_blocked", return_value=True), self.assertRaises(NotFound):
					send_gcs_file(public_url)
		finally:
			for name in ("request", "session", "form_dict"):
				delattr(frappe.local, name)

	def test_rollback_does_not_overwrite_a_newer_generation(self):
		from google.api_core.exceptions import PreconditionFailed

		import frappe
		from frappe.utils import CallbackManager

		db = SimpleNamespace(after_rollback=CallbackManager(), after_commit=CallbackManager())
		def blob(file_url):
			return self.bucket.blob(file_storage.object_key("site", file_url))

		with (
			patch.object(file_storage, "get_blob", side_effect=blob), patch.object(frappe, "db", db),
			patch.object(frappe, "logger") as logger,
		):
			first = file_storage.write("/private/files/a", b"original", if_generation_match=0)
			original, generation = file_storage.read_versioned("/private/files/a")
			self.assertEqual(generation, first)
			second = file_storage.write("/private/files/a", b"intermediate", if_generation_match=generation)
			file_storage.restore_on_rollback("/private/files/a", original, second)
			file_storage.write("/private/files/a", b"concurrent", if_generation_match=second)
			with self.assertRaises(PreconditionFailed):
				db.after_rollback.run()
			self.assertEqual(file_storage.read("/private/files/a"), b"concurrent")
			logger.return_value.error.assert_called_once()
			frappe.local.gcs_overwrites.pop("/private/files/a", None)

	def test_nested_savepoints_restore_overwrites_using_sdk_generations(self):
		import types

		import frappe
		from frappe.database.database import Database
		from frappe.utils import CallbackManager

		db = SimpleNamespace(
			sql=lambda *args, **kwargs: None, begin=lambda **kwargs: None, value_cache={},
			_disable_transaction_control=False, before_rollback=CallbackManager(), after_rollback=CallbackManager(),
			before_commit=CallbackManager(), after_commit=CallbackManager(),
		)
		for name in ("savepoint", "release_savepoint", "rollback", "commit", "_rollback_gcs_savepoint"):
			setattr(db, name, types.MethodType(getattr(Database, name), db))
		url = "/private/files/a.txt"
		blob = self.bucket.blob(file_storage.object_key("site", url))
		with patch.object(file_storage, "get_blob", return_value=blob), patch.object(frappe, "db", db):
			generation = file_storage.write(url, b"original", if_generation_match=0)
			db.savepoint("outer")
			generation = file_storage.write(url, b"outer", if_generation_match=generation)
			file_storage.restore_on_rollback(url, b"original", generation)
			db.savepoint("inner")
			generation = file_storage.write(url, b"inner", if_generation_match=generation)
			file_storage.restore_on_rollback(url, b"outer", generation)
			db.rollback(save_point="inner")
			self.assertEqual(file_storage.read(url), b"outer")
			db.rollback(save_point="outer")
			self.assertEqual(file_storage.read(url), b"original")
			db.commit()
			self.assertEqual(file_storage.read(url), b"original")

	def test_failed_savepoint_storage_restore_prevents_commit(self):
		import types

		import frappe
		from frappe.database.database import Database

		db = SimpleNamespace(sql=lambda *args: None, value_cache={}, _gcs_savepoints=["point"],
			_gcs_savepoint_offsets={"point": 0}, _gcs_overwrite_history=[("/private/files/a", b"old")])
		db._rollback_gcs_savepoint = types.MethodType(Database._rollback_gcs_savepoint, db)
		with patch.object(frappe, "db", db), patch.object(file_storage, "rollback_savepoint_overwrites", side_effect=OSError("offline")):
			with self.assertRaises(OSError):
				Database.rollback(db, save_point="point")
			with self.assertRaisesRegex(RuntimeError, "roll back the entire transaction"):
				Database.commit(db)

	def test_full_rollback_after_failed_savepoint_restores_original(self):
		import types

		import frappe
		from frappe.database.database import Database
		from frappe.utils import CallbackManager

		db = SimpleNamespace(sql=lambda *args, **kwargs: None, begin=lambda **kwargs: None, value_cache={},
			_disable_transaction_control=False, before_rollback=CallbackManager(), after_rollback=CallbackManager(),
			before_commit=CallbackManager(), after_commit=CallbackManager())
		for name in ("savepoint", "rollback", "commit", "_rollback_gcs_savepoint"):
			setattr(db, name, types.MethodType(getattr(Database, name), db))
		url = "/private/files/retry.txt"
		blob = self.bucket.blob(file_storage.object_key("site", url))
		with patch.object(file_storage, "get_blob", return_value=blob), patch.object(frappe, "db", db):
			generation = file_storage.write(url, b"original", if_generation_match=0)
			db.savepoint("point")
			generation = file_storage.write(url, b"changed", if_generation_match=generation)
			file_storage.restore_on_rollback(url, b"original", generation)
			with patch.object(file_storage, "write", side_effect=OSError("offline")), self.assertRaises(OSError):
				db.rollback(save_point="point")
			with self.assertRaises(RuntimeError):
				db.commit()
			db.rollback()
			self.assertEqual(file_storage.read(url), b"original")
			db.commit()

	def test_savepoint_rollback_preserves_earlier_overwrite_until_full_rollback(self):
		import types

		import frappe
		from frappe.database.database import Database
		from frappe.utils import CallbackManager

		db = SimpleNamespace(sql=lambda *args, **kwargs: None, begin=lambda **kwargs: None, value_cache={},
			_disable_transaction_control=False, before_rollback=CallbackManager(), after_rollback=CallbackManager(),
			before_commit=CallbackManager(), after_commit=CallbackManager())
		for name in ("savepoint", "rollback", "_rollback_gcs_savepoint"):
			setattr(db, name, types.MethodType(getattr(Database, name), db))
		url = "/private/files/before.txt"
		blob = self.bucket.blob(file_storage.object_key("site", url))
		with patch.object(file_storage, "get_blob", return_value=blob), patch.object(frappe, "db", db):
			generation = file_storage.write(url, b"original", if_generation_match=0)
			generation = file_storage.write(url, b"earlier", if_generation_match=generation)
			file_storage.restore_on_rollback(url, b"original", generation)
			db.savepoint("point")
			generation = file_storage.write(url, b"later", if_generation_match=generation)
			file_storage.restore_on_rollback(url, b"earlier", generation)
			db.rollback(save_point="point")
			self.assertEqual(file_storage.read(url), b"earlier")
			db.rollback()
			self.assertEqual(file_storage.read(url), b"original")

	def test_sqlite_savepoint_rollback_keeps_database_and_bucket_in_sync(self):
		import sqlite3
		import types

		import frappe
		from frappe.database.database import Database
		from frappe.database.sqlite.database import SQLiteDatabase
		from frappe.utils import CallbackManager

		connection = sqlite3.connect(":memory:")
		connection.execute("create table files (content text)")
		connection.execute("insert into files values ('original')")
		connection.commit()
		connection.execute("begin")
		db = SimpleNamespace(_conn=connection, sql=lambda statement: connection.execute(statement),
			begin=lambda **kwargs: connection.execute("begin"), _disable_transaction_control=False,
			before_rollback=CallbackManager(), after_rollback=CallbackManager(),
			before_commit=CallbackManager(), after_commit=CallbackManager())
		for name in ("savepoint", "release_savepoint", "_rollback_gcs_savepoint"):
			setattr(db, name, types.MethodType(getattr(Database, name), db))
		for name in ("commit", "rollback"):
			setattr(db, name, types.MethodType(getattr(SQLiteDatabase, name), db))
		url = "/private/files/sqlite.txt"
		blob = self.bucket.blob(file_storage.object_key("site", url))
		try:
			with patch.object(file_storage, "get_blob", return_value=blob), patch.object(frappe, "db", db):
				generation = file_storage.write(url, b"original", if_generation_match=0)
				db.savepoint("point")
				connection.execute("update files set content = 'changed'")
				generation = file_storage.write(url, b"changed", if_generation_match=generation)
				file_storage.restore_on_rollback(url, b"original", generation)
				db.rollback(save_point="point")
				db.commit()
				self.assertEqual(connection.execute("select content from files").fetchone()[0], "original")
				self.assertEqual(file_storage.read(url), b"original")
		finally:
			connection.close()

	def test_failed_commit_retains_overwrite_rollback_callbacks(self):
		import types

		import frappe
		from frappe.database.database import Database
		from frappe.utils import CallbackManager

		for failure in ("before_commit", "sql_commit"):
			with self.subTest(failure=failure):
				def sql(statement):
					if statement == "commit" and failure == "sql_commit":
						raise OSError("commit failed")

				db = SimpleNamespace(sql=sql, begin=lambda **kwargs: None, value_cache={}, _disable_transaction_control=False,
					before_rollback=CallbackManager(), after_rollback=CallbackManager(),
					before_commit=CallbackManager(), after_commit=CallbackManager())
				for name in ("commit", "rollback", "_rollback_gcs_savepoint"):
					setattr(db, name, types.MethodType(getattr(Database, name), db))
				url = f"/private/files/{failure}.txt"
				blob = self.bucket.blob(file_storage.object_key("site", url))
				with patch.object(file_storage, "get_blob", return_value=blob), patch.object(frappe, "db", db):
					generation = file_storage.write(url, b"original", if_generation_match=0)
					generation = file_storage.write(url, b"changed", if_generation_match=generation)
					file_storage.restore_on_rollback(url, b"original", generation)
					if failure == "before_commit":
						def fail():
							raise OSError("before commit failed")
						db.before_commit.add(fail)
					with self.assertRaises(OSError):
						db.commit()
					db.rollback()
					self.assertEqual(file_storage.read(url), b"original")

	def test_settings_connection_checks_sdk_bucket_metadata_and_permissions(self):
		import frappe

		config = SimpleNamespace(bucket_name="bucket", object_prefix="site")
		with (
			patch("google.cloud.storage.Client", return_value=self.client),
			patch.object(frappe, "get_single", return_value=config), patch.object(frappe, "only_for"),
			patch.object(settings, "_", lambda value: value),
		):
			self.assertIn("Connected", settings.test_connection())

	def test_settings_rejects_missing_delete_permission_and_public_bucket(self):
		import frappe

		config = SimpleNamespace(bucket_name="bucket", object_prefix="site")
		def reject(message):
			raise ValueError(message)

		with (
			patch("google.cloud.storage.Client", return_value=self.client),
			patch.object(frappe, "get_single", return_value=config), patch.object(frappe, "only_for"),
			patch.object(frappe, "throw", side_effect=reject), patch.object(settings, "_", lambda value: value),
		):
			self.server.permissions = {"storage.objects.create", "storage.objects.get", "storage.objects.list"}
			with self.assertRaisesRegex(ValueError, "storage.objects.delete"):
				settings.test_connection()
			self.server.permissions = None
			self.server.public_access_prevention = "inherited"
			with self.assertRaisesRegex(ValueError, "Public Access Prevention"):
				settings.test_connection()
