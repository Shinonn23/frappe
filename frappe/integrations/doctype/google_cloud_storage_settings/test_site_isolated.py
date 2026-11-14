"""Optional end-to-end test in a disposable Frappe site on tmpfs."""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import MySQLdb
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

import frappe
from frappe.integrations.doctype.google_cloud_storage_settings import storage as file_storage
from frappe.integrations.doctype.google_cloud_storage_settings.test_gcs_http import API


@unittest.skipUnless(os.environ.get("FRAPPE_GCS_TEST_SITE") == "1", "optional disposable Frappe site integration test")
class TestDisposableSite(unittest.TestCase):
	def test_settings_migration_and_file_io(self):
		bench_source = Path(__file__).resolve().parents[4]
		old_cwd = os.getcwd()
		with tempfile.TemporaryDirectory(prefix="frappe-gcs-site-", dir=file_storage.memory_directory()) as directory:
			root, sites = Path(directory), Path(directory) / "sites"
			sites.mkdir()
			(root / "apps").mkdir()
			(root / "logs").mkdir()
			(root / "apps" / "frappe").symlink_to(bench_source, target_is_directory=True)
			(root / "env").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
			(sites / "apps.txt").write_text("frappe\n")
			(sites / "assets").mkdir()
			subprocess.run([
				"/usr/bin/mariadb-install-db", "--no-defaults", f"--datadir={root}/db",
				"--auth-root-authentication-method=normal", "--skip-test-db",
			], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=40)
			db_socket = root / "db.sock"
			db = subprocess.Popen([
				"/usr/sbin/mariadbd", "--no-defaults", f"--datadir={root}/db", f"--socket={db_socket}",
				f"--pid-file={root}/db.pid", f"--log-error={root}/db.log", "--skip-networking",
				"--innodb-buffer-pool-size=64M", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci",
			], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			with socket.socket() as probe:
				probe.bind(("127.0.0.1", 0))
				port = probe.getsockname()[1]
			redis_url = f"redis://127.0.0.1:{port}"
			(sites / "common_site_config.json").write_text(json.dumps({
				"redis_cache": redis_url, "redis_queue": redis_url, "redis_socketio": redis_url,
			}))
			redis = subprocess.Popen([
				"/usr/bin/redis-server", "--bind", "127.0.0.1", "--port", str(port), "--save", "", "--appendonly", "no", "--dir", directory,
			], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			api = ThreadingHTTPServer(("127.0.0.1", 0), API)
			api.objects, api.sessions = {}, {}
			api.public_access_prevention, api.permissions = "enforced", None
			thread = threading.Thread(target=api.serve_forever, daemon=True)
			thread.start()
			try:
				for _ in range(200):
					try:
						connection = MySQLdb.connect(user="root", unix_socket=str(db_socket))
						break
					except MySQLdb.OperationalError:
						if db.poll() is not None:
							raise RuntimeError((root / "db.log").read_text()[-1000:])
						time.sleep(0.05)
				else:
					raise TimeoutError("Disposable MariaDB did not start")
				with connection.cursor() as cursor:
					cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY 'disposable-root'")
				connection.commit()
				connection.close()
				for _ in range(100):
					try:
						with socket.create_connection(("127.0.0.1", port), timeout=0.2):
							break
					except OSError:
						if redis.poll() is not None:
							raise RuntimeError("Disposable Redis exited")
						time.sleep(0.05)
				command = [sys.executable, "-m", "frappe.utils.bench_helper", "frappe", "new-site", "disposable.test",
					"--db-type", "mariadb", "--db-socket", str(db_socket), "--db-root-username", "root",
					"--db-root-password", "disposable-root", "--admin-password", "disposable-test",
					"--db-name", "disposable_site", "--db-user", "disposable_site", "--db-password", "disposable-test"]
				result = subprocess.run(command, cwd=sites, capture_output=True, text=True, timeout=120)
				self.assertEqual(result.returncode, 0, result.stderr[-4000:])
				os.chdir(sites)
				from werkzeug.test import Client
				from werkzeug.wrappers import Response

				from frappe.app import application

				http = Client(application, Response, use_cookies=True)
				response = http.get("/api/resource/Google%20Cloud%20Storage%20Settings/Google%20Cloud%20Storage%20Settings", headers={"Host": "disposable.test"})
				self.assertIn(response.status_code, (401, 403))
				response = http.post("/api/method/login", data={"usr": "Administrator", "pwd": "disposable-test"}, headers={"Host": "disposable.test"})
				self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
				response = http.get("/api/resource/Google%20Cloud%20Storage%20Settings/Google%20Cloud%20Storage%20Settings", headers={"Host": "disposable.test"})
				self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
				self.assertEqual(response.json["data"]["doctype"], "Google Cloud Storage Settings")
				self.assertEqual(response.json["data"]["enabled"], 0)
				client = storage.Client(project="test", credentials=AnonymousCredentials(), client_options={
					"api_endpoint": f"http://127.0.0.1:{api.server_port}"
				})
				response = http.put("/api/resource/Google%20Cloud%20Storage%20Settings/Google%20Cloud%20Storage%20Settings",
					json={"bucket_name": "bucket", "object_prefix": "disposable", "isolation_confirmed": 1, "proxy_confirmed": 1},
					headers={"Host": "disposable.test"})
				self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
				with patch("google.cloud.storage.Client", return_value=client):
					response = http.post("/api/method/frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.test_connection",
						headers={"Host": "disposable.test"})
				self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
				self.assertIn("Connected", response.json["message"])
				frappe.init("disposable.test", sites_path=str(sites), force=True)
				frappe.connect()
				frappe.set_user("Administrator")
				frappe.local.conf.gcs_coordinated_cutover = 1
				frappe.local.conf.pause_scheduler = 1
				with patch("google.cloud.storage.Client", return_value=client):
					from frappe.integrations.doctype.google_cloud_storage_settings import (
						google_cloud_storage_settings as settings,
					)

					config = frappe.get_single("Google Cloud Storage Settings")
					self.assertEqual((config.bucket_name, config.object_prefix), ("bucket", "disposable"))
					self.assertIn("Connected", settings.test_connection())
					public = frappe.get_doc({"doctype": "File", "file_name": "public.txt", "content": b"public", "is_private": 0}).insert(ignore_permissions=True)
					private = frappe.get_doc({"doctype": "File", "file_name": "private.txt", "content": b"private", "is_private": 1}).insert(ignore_permissions=True)
					frappe.db.commit()
					public_path, private_path = Path(public.get_full_path()), Path(private.get_full_path())
					self.assertTrue(public_path.is_file() and private_path.is_file())
					settings.run_copy("Administrator", "bucket", "disposable")
					frappe.db.commit()
					settings.run_preflight("Administrator", "bucket", "disposable")
					frappe.db.commit()
					settings.run_cutover("Administrator", "bucket", "disposable")
					frappe.db.commit()
					self.assertTrue(file_storage.enabled())
					self.assertFalse(public_path.exists() or private_path.exists())
					public = frappe.get_doc("File", public.name)
					private = frappe.get_doc("File", private.name)
					self.assertEqual(public.get_content(encodings=[]), b"public")
					self.assertEqual(private.get_content(encodings=[]), b"private")
					self.assertEqual(json.loads(frappe.db.get_single_value("Google Cloud Storage Settings", "last_migration_result"))["action"], "activate")
					from werkzeug.exceptions import Forbidden, NotFound
					from werkzeug.test import EnvironBuilder

					from frappe.utils.response import download_private_file, send_gcs_file

					frappe.local.request = frappe._dict(environ=EnvironBuilder(path=public.file_url).get_environ())
					response = send_gcs_file(public.file_url)
					response.direct_passthrough = False
					self.assertEqual(response.get_data(), b"public")
					with patch("frappe.utils.response.make_access_log"):
						response = download_private_file(private.file_url)
						response.direct_passthrough = False
						self.assertEqual(response.get_data(), b"private")
					frappe.session.user = "Guest"
					try:
						with self.assertRaises(Forbidden):
							download_private_file(private.file_url)
					finally:
						frappe.session.user = "Administrator"
					old_public_url = public.file_url
					public.is_private = 1
					public.save(ignore_permissions=True)
					frappe.db.commit()
					self.assertEqual(frappe.get_doc("File", public.name).get_content(encodings=[]), b"public")
					with self.assertRaises(NotFound):
						send_gcs_file(old_public_url)
					new_file = frappe.get_doc({"doctype": "File", "file_name": "new.txt", "content": b"cloud", "is_private": 1}).insert(ignore_permissions=True)
					frappe.db.commit()
					new_file = frappe.get_doc("File", new_file.name)
					self.assertEqual(new_file.get_content(encodings=[]), b"cloud")
					self.assertFalse(Path(new_file.get_full_path()).exists())
					frappe.db.savepoint("before_overwrite")
					new_file.save_file(content=b"changed", overwrite=True, ignore_existing_file_check=True)
					new_file.db_update()
					frappe.db.rollback(save_point="before_overwrite")
					frappe.db.commit()
					self.assertEqual(frappe.get_doc("File", new_file.name).get_content(encodings=[]), b"cloud")
					result = settings.run_integrity_check("Administrator")
					frappe.db.commit()
					self.assertEqual((result["missing"], result["mismatch"]), (0, 0))
					from frappe import handler
					from frappe.app import Request, make_form_dict

					for is_private in (1, 0):
						payload = (b"private" if is_private else b"public") * (100 * 1024)
						body = (b"--boundary\r\nContent-Disposition: form-data; name=\"is_private\"\r\n\r\n"
							+ str(is_private).encode() + b"\r\n--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"uploaded.txt\"\r\n"
							+ b"Content-Type: text/plain\r\n\r\n" + payload + b"\r\n--boundary--\r\n")
						environ = EnvironBuilder(method="POST", path="/api/method/upload_file", input_stream=io.BytesIO(body),
							content_length=len(body), content_type="multipart/form-data; boundary=boundary").get_environ()
						request = Request(environ)
						frappe.local.request = request
						make_form_dict(request)
						self.assertIsInstance(request.files["file"].stream, io.BytesIO)
						uploaded = handler.upload_file()
						frappe.db.commit()
						uploaded = frappe.get_doc("File", uploaded.name)
						self.assertEqual(uploaded.get_content(encodings=[]), payload)
						self.assertEqual(uploaded.is_private, is_private)
						self.assertFalse(Path(uploaded.get_full_path()).exists())
						request.close()
					chunk_payload = b"chunk-one" * 20000 + b"chunk-two" * 20000
					chunk_split = len(b"chunk-one" * 20000)
					upload_id = str(uuid4())
					for index, chunk in enumerate((chunk_payload[:chunk_split], chunk_payload[chunk_split:])):
						fields = {"is_private": "1", "total_chunk_count": "2", "total_file_size": str(len(chunk_payload)),
							"chunk_index": str(index), "chunk_byte_offset": str(0 if index == 0 else chunk_split), "upload_id": upload_id}
						body = b"".join(
							f'--boundary\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
							for key, value in fields.items()
						) + (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="chunked.txt"\r\n'
							b'Content-Type: text/plain\r\n\r\n' + chunk + b'\r\n--boundary--\r\n')
						environ = EnvironBuilder(method="POST", path="/api/method/upload_file", input_stream=io.BytesIO(body),
							content_length=len(body), content_type="multipart/form-data; boundary=boundary").get_environ()
						request = Request(environ)
						frappe.local.request = request
						make_form_dict(request)
						uploaded = handler.upload_file()
						frappe.db.commit()
						if index == 0:
							self.assertIsNone(uploaded)
							self.assertTrue(any("/.uploads/" in key for key in api.objects))
						else:
							self.assertEqual(frappe.get_doc("File", uploaded.name).get_content(encodings=[]), chunk_payload)
							self.assertFalse(Path(uploaded.get_full_path()).exists())
							self.assertFalse(any("/.uploads/" in key for key in api.objects))
						request.close()
			finally:
				if getattr(frappe.local, "db", None):
					frappe.destroy()
				os.chdir(old_cwd)
				api.shutdown()
				api.server_close()
				thread.join()
				redis.terminate()
				db.terminate()
				redis.wait(timeout=10)
				db.wait(timeout=10)
