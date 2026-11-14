"""Reapply GCS proxy routing to a bench-generated nginx.conf."""

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

SERVER = re.compile(r"(?ms)^([ \t]*)server\s*\{.*?^\1\}")
LOCATION = r"(?ms)^([ \t]*)location\s+{name}\s*\{{(?P<body>[^{{}}]*)^\1\}}"
DIRECTIVES = {"proxy_request_buffering": "off", "proxy_buffering": "off", "proxy_max_temp_file_size": "0"}


def configure_location(body):
	for name, value in DIRECTIVES.items():
		pattern = re.compile(rf"(?m)^([ \t]*){name}\s+[^;]+;\s*$")
		matches = list(pattern.finditer(body))
		if len(matches) > 1:
			raise ValueError(f"Duplicate {name} directives")
		if matches:
			body = pattern.sub(lambda match: f"{match.group(1)}{name} {value};", body)
		else:
			body = f"\n\t\t{name} {value};" + body
	return body


def transform(config):
	changed = 0
	def configure_server(match):
		nonlocal changed
		server = match.group()
		web = list(re.finditer(LOCATION.format(name="@webserver"), server))
		if not web:
			return server
		if len(web) != 1:
			raise ValueError("Expected one @webserver location per server")
		web = web[0]
		body = web.group("body")
		proxy = re.search(r"(?m)^\s*proxy_pass\s+(http://[^;]+);", body)
		if not proxy or not re.search(r"(?m)^\s*proxy_http_version\s+1\.1\s*;", body):
			raise ValueError("Unsupported @webserver proxy configuration")
		site_header = re.search(r"(?m)^\s*proxy_set_header\s+X-Frappe-Site-Name\s+([^;]+);", body)
		if not site_header:
			raise ValueError("Missing Frappe site routing header")
		public_locations = list(re.finditer(LOCATION.format(name=r"\^~\s+/files/"), server))
		if len(public_locations) > 1:
			raise ValueError("Duplicate /files/ proxy locations")
		if public_locations:
			public = public_locations[0]
			if not re.search(rf"(?m)^\s*proxy_pass\s+{re.escape(proxy.group(1))}\s*;", public.group("body")):
				raise ValueError("Existing /files/ location uses a different upstream")
			if not re.search(rf"(?m)^\s*proxy_set_header\s+X-Frappe-Site-Name\s+{re.escape(site_header.group(1).strip())}\s*;", public.group("body")):
				raise ValueError("Existing /files/ location uses different site routing")
			if not re.search(r"(?m)^\s*proxy_http_version\s+1\.1\s*;", public.group("body")):
				raise ValueError("Existing /files/ location must use proxy HTTP/1.1")
			replacement = public.group().replace(public.group("body"), configure_location(public.group("body")), 1)
			server = server[:public.start()] + replacement + server[public.end():]
		else:
			public_body = configure_location(body)
			public = f"{web.group(1)}location ^~ /files/ {{{public_body}{web.group(1)}}}\n\n"
			server = server[:web.start()] + public + server[web.start():]
		web = re.search(LOCATION.format(name="@webserver"), server)
		server = server[:web.start()] + web.group().replace(body, configure_location(body), 1) + server[web.end():]
		changed += 1
		return server

	updated = SERVER.sub(configure_server, config)
	if not changed:
		raise ValueError("No bench @webserver locations found")
	return updated


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("path", type=Path, help="Path to bench-generated config/nginx.conf")
	parser.add_argument("--write", action="store_true", help="Apply changes; without this flag only check")
	args = parser.parse_args()
	if args.path.is_symlink() or not args.path.is_file():
		raise ValueError("Configuration must be a regular, non-symlink file")
	original = args.path.read_text(encoding="utf-8")
	updated = transform(original)
	if args.write and updated != original:
		metadata = args.path.stat()
		fd, name = tempfile.mkstemp(prefix=".gcs-nginx-", dir=args.path.parent)
		try:
			with os.fdopen(fd, "w", encoding="utf-8") as output:
				os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
				os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
				output.write(updated)
				output.flush()
				os.fsync(output.fileno())
			os.replace(name, args.path)
		finally:
			if os.path.exists(name):
				os.unlink(name)
	print("GCS proxy configuration " + ("updated" if args.write and updated != original else "already configured" if updated == original else "requires --write"))
	if not args.write and updated != original:
		raise SystemExit(1)


if __name__ == "__main__":
	main()
