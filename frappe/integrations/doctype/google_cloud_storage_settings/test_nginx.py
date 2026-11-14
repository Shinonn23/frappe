import http.client
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from frappe.integrations.doctype.google_cloud_storage_settings.nginx import transform

SERVER = """server {
    listen 8080;
    location / {
        try_files $uri @webserver;
    }
    location @webserver {
        proxy_http_version 1.1;
        proxy_set_header X-Frappe-Site-Name $host;
        proxy_set_header Host $host;
        proxy_pass http://frappe;
    }
}
"""


class TestNginxConfiguration(unittest.TestCase):
	def test_command_applies_changes_and_is_repeatable(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "nginx.conf"
			path.write_text(SERVER)
			path.chmod(0o640)
			command = [sys.executable, "-m", "frappe.integrations.doctype.google_cloud_storage_settings.nginx", str(path), "--write"]
			subprocess.run(command, check=True, capture_output=True, text=True)
			updated = path.read_text()
			subprocess.run(command, check=True, capture_output=True, text=True)
			self.assertEqual(path.read_text(), updated)
			self.assertEqual(path.stat().st_mode & 0o777, 0o640)

	def test_generated_servers_route_files_and_stream_uploads_idempotently(self):
		updated = transform(SERVER + SERVER.replace("8080", "8081"))
		self.assertEqual(updated.count("location ^~ /files/"), 2)
		self.assertEqual(updated.count("proxy_request_buffering off;"), 4)
		self.assertEqual(updated.count("proxy_max_temp_file_size 0;"), 4)
		self.assertEqual(transform(updated), updated)

	def test_rejects_existing_file_location_with_different_upstream(self):
		config = SERVER.replace("    location / {", "    location ^~ /files/ {\n        proxy_pass http://other;\n    }\n    location / {")
		with self.assertRaisesRegex(ValueError, "different upstream"):
			transform(config)

	def test_rejects_missing_site_routing_header(self):
		with self.assertRaisesRegex(ValueError, "site routing"):
			transform(SERVER.replace("proxy_set_header X-Frappe-Site-Name $host;", ""))

	def test_rejects_existing_file_location_with_different_site(self):
		updated = transform(SERVER)
		updated = updated.replace("proxy_set_header X-Frappe-Site-Name $host;", "proxy_set_header X-Frappe-Site-Name wrong-site;", 1)
		with self.assertRaisesRegex(ValueError, "different site routing"):
			transform(updated)

	@unittest.skipUnless(shutil.which("nginx") or Path("/usr/sbin/nginx").exists(), "nginx not installed")
	def test_rewritten_proxy_streams_upload_and_bypasses_local_public_file(self):
		first_chunk = threading.Event()
		class Upstream(BaseHTTPRequestHandler):
			def log_message(self, *args):
				pass

			def do_GET(self):
				self.send_response(200)
				self.end_headers()
				self.wfile.write(b"upstream")

			def do_POST(self):
				length = int(self.headers["Content-Length"])
				self.rfile.read(16)
				first_chunk.set()
				self.rfile.read(length - 16)
				self.send_response(200)
				self.end_headers()
				self.wfile.write(b"received")

		backend = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
		thread = threading.Thread(target=backend.serve_forever, daemon=True)
		thread.start()
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "files").mkdir()
			(root / "files" / "a.txt").write_bytes(b"local")
			(root / "body").mkdir()
			(root / "proxy").mkdir()
			with socket.socket() as probe:
				probe.bind(("127.0.0.1", 0))
				port = probe.getsockname()[1]
			server = SERVER.replace("listen 8080;", f"listen 127.0.0.1:{port};").replace(
				"location / {", f"location / {{\n        root {root};"
			)
			config = root / "nginx.conf"
			config.write_text(f"pid {root}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{\naccess_log off;\n"
				f"client_body_temp_path {root}/body;\nproxy_temp_path {root}/proxy;\n"
				f"upstream frappe {{ server 127.0.0.1:{backend.server_port}; }}\n" + transform(server) + "}\n")
			nginx = shutil.which("nginx") or "/usr/sbin/nginx"
			process = subprocess.Popen([nginx, "-c", str(config), "-p", directory, "-g", "daemon off;"],
				stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			try:
				deadline = time.monotonic() + 5
				while True:
					try:
						connection = socket.create_connection(("127.0.0.1", port), timeout=1)
						break
					except OSError:
						if process.poll() is not None or time.monotonic() >= deadline:
							raise RuntimeError("Test Nginx did not start")
						time.sleep(0.05)
				with connection:
					length = 128 * 1024
					connection.sendall(f"POST /api/method/upload_file HTTP/1.1\r\nHost: test\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n".encode() + b"a" * 16)
					self.assertTrue(first_chunk.wait(3), "Nginx buffered the upload before forwarding it")
					connection.sendall(b"b" * (length - 16))
					response = http.client.HTTPResponse(connection)
					response.begin()
					self.assertEqual(response.read(), b"received")
				client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
				client.request("GET", "/files/a.txt")
				self.assertEqual(client.getresponse().read(), b"upstream")
				client.close()
				self.assertFalse(any(path.is_file() for path in (root / "body").rglob("*")))
				self.assertFalse(any(path.is_file() for path in (root / "proxy").rglob("*")))
			finally:
				process.terminate()
				process.wait(timeout=5)
				backend.shutdown()
				backend.server_close()
				thread.join()

	@unittest.skipUnless(shutil.which("nginx") or Path("/usr/sbin/nginx").exists(), "nginx not installed")
	def test_rewritten_configuration_passes_nginx_syntax_check(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "nginx.conf"
			path.write_text(f"pid {directory}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{\naccess_log off;\nupstream frappe {{ server 127.0.0.1:8000; }}\n" + transform(SERVER) + "}\n")
			nginx = shutil.which("nginx") or "/usr/sbin/nginx"
			result = subprocess.run([nginx, "-t", "-c", str(path), "-p", directory], capture_output=True, text=True)
			self.assertEqual(result.returncode, 0, result.stderr)
