"""Optional integration test with disposable MariaDB and a local GCS API stub."""

import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import MySQLdb
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

import frappe
from frappe.database.database import Database
from frappe.integrations.doctype.google_cloud_storage_settings import storage as file_storage
from frappe.integrations.doctype.google_cloud_storage_settings.test_gcs_http import API
from frappe.utils import CallbackManager


@unittest.skipUnless(os.environ.get("FRAPPE_GCS_TEST_MARIADB") == "1", "optional isolated MariaDB integration test")
class TestIsolatedMariaDB(unittest.TestCase):
	def test_savepoint_rollback_and_commit_failure_with_real_mariadb(self):
		with tempfile.TemporaryDirectory(prefix="frappe-gcs-mariadb-", dir=file_storage.memory_directory()) as directory:
			root = Path(directory)
			subprocess.run([
				"/usr/bin/mariadb-install-db", "--no-defaults", f"--datadir={root}/data",
				"--auth-root-authentication-method=normal", "--skip-test-db",
			], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=40)
			socket = root / "mysql.sock"
			process = subprocess.Popen([
				"/usr/sbin/mariadbd", "--no-defaults", f"--datadir={root}/data", f"--socket={socket}",
				f"--pid-file={root}/mysql.pid", f"--log-error={root}/mysql.log", "--skip-networking",
				"--innodb-buffer-pool-size=64M", "--max-connections=10",
			], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			server = ThreadingHTTPServer(("127.0.0.1", 0), API)
			server.objects, server.sessions = {}, {}
			server.public_access_prevention, server.permissions = "enforced", None
			thread = threading.Thread(target=server.serve_forever, daemon=True)
			thread.start()
			connection = None
			try:
				deadline = time.monotonic() + 20
				while time.monotonic() < deadline:
					try:
						connection = MySQLdb.connect(user="root", unix_socket=str(socket))
						break
					except MySQLdb.OperationalError:
						if process.poll() is not None:
							raise RuntimeError((root / "mysql.log").read_text()[-1000:])
						time.sleep(0.1)
				if connection is None:
					raise TimeoutError("Isolated MariaDB did not start")
				cursor = connection.cursor()
				cursor.execute("create database disposable")
				cursor.execute("use disposable")
				cursor.execute("create table files (content varchar(100))")
				cursor.execute("insert into files values ('original')")
				connection.commit()
				db = SimpleNamespace(sql=lambda query: cursor.execute(query), begin=lambda **kwargs: cursor.execute("start transaction"),
					value_cache={}, _disable_transaction_control=False, before_rollback=CallbackManager(),
					after_rollback=CallbackManager(), before_commit=CallbackManager(), after_commit=CallbackManager())
				for name in ("savepoint", "release_savepoint", "rollback", "commit", "_rollback_gcs_savepoint"):
					setattr(db, name, types.MethodType(getattr(Database, name), db))
				client = storage.Client(project="test", credentials=AnonymousCredentials(), client_options={
					"api_endpoint": f"http://127.0.0.1:{server.server_port}"
				})
				blob = client.bucket("bucket").blob("site/private/a.txt")
				url = "/private/files/a.txt"
				with patch.object(frappe, "db", db), patch.object(file_storage, "get_blob", return_value=blob):
					generation = file_storage.write(url, b"original", if_generation_match=0)
					db.savepoint("point")
					cursor.execute("update files set content = 'changed'")
					generation = file_storage.write(url, b"changed", if_generation_match=generation)
					file_storage.restore_on_rollback(url, b"original", generation)
					db.rollback(save_point="point")
					db.commit()
					cursor.execute("select content from files")
					self.assertEqual(cursor.fetchone()[0], "original")
					self.assertEqual(file_storage.read(url), b"original")
					cursor.execute("create table `tabSingles` (doctype varchar(140), field varchar(140), value text)")
					db.db_type = "mariadb"
					def query(statement, values=(), **kwargs):
						cursor.execute(statement, values)
						rows = cursor.fetchall() if cursor.description else ()
						if kwargs.get("as_dict") and rows:
							return [frappe._dict(zip((column[0] for column in cursor.description), row, strict=True)) for row in rows]
						return rows
					db.sql = query
					old_qb = getattr(frappe.local, "qb", None)
					old_flags = getattr(frappe.local, "flags", None)
					old_session = getattr(frappe.local, "session", None)
					old_db = getattr(frappe.local, "db", None)
					old_conf = getattr(frappe.local, "conf", None)
					try:
						from frappe.query_builder.utils import get_query_builder
						frappe.local.qb = get_query_builder("mariadb")
						frappe.local.flags = frappe._dict()
						frappe.local.session = frappe._dict(user="Administrator")
						frappe.local.db = db
						frappe.local.conf = frappe._dict(db_type="mariadb")
						with patch.object(frappe, "get_hooks", return_value=[]):
							self.assertFalse(file_storage.enabled())
							cursor.execute("insert into `tabSingles` values (%s, %s, %s)", ("Google Cloud Storage Settings", "enabled", "1"))
							self.assertTrue(file_storage.enabled())
							cursor.execute("create table `tabFile` (name varchar(140), file_url text, is_private int, content_hash varchar(140))")
							cursor.executemany("insert into `tabFile` values (%s, %s, %s, %s)", [
								("source", "/files/a.txt", 0, "same"), ("shared", "/files/a.txt", 0, "different"),
								("separate", "/files/b.txt", 0, "same"), ("case", "/files/A.txt", 0, "same"),
							])
							def get_file_values(doctype, filters, fields, **kwargs):
								if "thumbnail_url" in filters:
									return []
								cursor.execute("select name, file_url from `tabFile` where file_url = %s for update", (filters["file_url"],))
								rows = cursor.fetchall()
								return [url for name, url in rows] if kwargs.get("pluck") else [frappe._dict(name=name, file_url=url) for name, url in rows]
							db.get_values = get_file_values
							from frappe.core.doctype.file.utils import update_existing_file_docs
							update_existing_file_docs(SimpleNamespace(name="source", file_url="/private/files/a.txt", is_private=1), "/files/a.txt")
							cursor.execute("select name, file_url, is_private from `tabFile` order by name")
							self.assertEqual(cursor.fetchall(), (
								("case", "/files/A.txt", 0), ("separate", "/files/b.txt", 0),
								("shared", "/private/files/a.txt", 1), ("source", "/files/a.txt", 0),
							))
							cursor.execute("insert into `tabFile` values ('photo', '/files/Photo.png', 0, 'hash')")
							cursor.execute("select count(*) from `tabFile` where file_url = '/files/photo.png'")
							self.assertEqual(cursor.fetchone()[0], 1)
							from werkzeug.exceptions import NotFound
							with patch.object(file_storage, "public_blocked", return_value=False), patch.object(
								file_storage, "unmanaged_public_generation", return_value=None
							), self.assertRaises(NotFound):
								file_storage.read_public("/files/photo.png")
					finally:
						frappe.local.qb, frappe.local.flags, frappe.local.session, frappe.local.db, frappe.local.conf = old_qb, old_flags, old_session, old_db, old_conf
					generation = file_storage.generation(url)
					generation = file_storage.write(url, b"new", if_generation_match=generation)
					file_storage.restore_on_rollback(url, b"original", generation)
					cursor.execute("update files set content = 'new'")
					db.before_commit.add(lambda: (_ for _ in ()).throw(OSError("commit refused")))
					with self.assertRaises(OSError):
						db.commit()
					db.rollback()
					cursor.execute("select content from files")
					self.assertEqual(cursor.fetchone()[0], "original")
					self.assertEqual(file_storage.read(url), b"original")
			finally:
				if connection:
					connection.close()
				server.shutdown()
				server.server_close()
				thread.join()
				process.terminate()
				try:
					process.wait(timeout=10)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait()
