# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
import io
import json
import os
import stat
from pathlib import Path


class HashWriter(io.RawIOBase):
	def __init__(self):
		self.hash = hashlib.sha256()

	def writable(self):
		return True

	def write(self, data):
		self.hash.update(data)
		return len(data)


def normalize_prefix(prefix):
	prefix = prefix.strip("/")
	if not prefix or "\\" in prefix or "\x00" in prefix or any(part in (".", "..", "") for part in prefix.split("/")):
		raise ValueError("A unique, non-empty object prefix is required")
	return prefix


def claim_prefix(bucket, prefix, owner, *, create):
	from google.api_core.exceptions import PreconditionFailed

	if not owner:
		raise ValueError("Migration site identity is required")
	marker = bucket.blob(f"{normalize_prefix(prefix)}/.frappe-site-owner")
	if not marker.exists():
		if not create:
			raise RuntimeError("Bucket prefix has no site owner marker")
		try:
			marker.upload_from_string(owner.encode(), if_generation_match=0)
		except PreconditionFailed:
			pass
	marker.reload()
	stored_owner = marker.download_as_bytes(if_generation_match=marker.generation)
	if stored_owner != owner.encode():
		raise RuntimeError("Bucket prefix belongs to a different site")


def source_files(site_path, prefix):
	from .storage import object_key

	prefix = normalize_prefix(prefix)

	for directory, label in (("public/files", "public"), ("private/files", "private")):
		root = Path(site_path) / directory
		if not root.is_dir() or root.is_symlink():
			raise ValueError(f"Invalid attachment directory: {root}")
		for current, dirs, files in os.walk(root, followlinks=False):
			for name in dirs:
				if (Path(current) / name).is_symlink():
					raise ValueError(f"Cannot migrate symbolic link: {Path(current) / name}")
			for name in files:
				path = Path(current) / name
				if path.is_symlink() or not path.is_file():
					raise ValueError(f"Cannot migrate non-regular file: {path}")
				url_root = "/private/files/" if label == "private" else "/files/"
				key = object_key(prefix, url_root + path.relative_to(root).as_posix())
				yield path, key


def check_file(bucket, path, key, *, copy=False, owner=None):
	blob = bucket.blob(key)
	with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
		before = os.fstat(source.fileno())
		if not stat.S_ISREG(before.st_mode):
			raise ValueError(f"Cannot migrate non-regular file: {path}")
		local_hash = hashlib.file_digest(source, "sha256").digest()
		source.seek(0)
		if not blob.exists():
			if not copy:
				raise RuntimeError(f"Bucket object is missing: {key}")
			blob.metadata = {"frappe_migration_site": owner}
			blob.upload_from_file(source, if_generation_match=0)
		blob.reload()
		if owner and (blob.metadata or {}).get("frappe_migration_site") != owner:
			raise RuntimeError(f"Bucket object is not owned by this site's migration: {key}")
		generation = blob.generation
		remote_hash = HashWriter()
		blob.download_to_file(remote_hash, if_generation_match=generation)
		if remote_hash.hash.digest() != local_hash:
			if not copy or not owner or (blob.metadata or {}).get("frappe_migration_site") != owner:
				raise RuntimeError(f"Bucket object differs from local file: {key}")
			source.seek(0)
			blob.upload_from_file(source, if_generation_match=generation)
			blob.reload()
			generation = blob.generation
			remote_hash = HashWriter()
			blob.download_to_file(remote_hash, if_generation_match=generation)
		after = os.fstat(source.fileno())
		if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
			raise RuntimeError(f"File changed during migration: {path}")
		current = path.stat(follow_symlinks=False)
		if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
			current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
		):
			raise RuntimeError(f"File was replaced during migration: {path}")
		if remote_hash.hash.digest() != local_hash:
			raise RuntimeError(f"Bucket object differs from local file: {key}")
		return local_hash.hex(), generation, (
			after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
		)


def copy_files(bucket, site_path, prefix, *, owner, may_continue=None):
	"""Copy files and reconcile objects previously created by this site's migration."""
	if not owner:
		raise ValueError("Migration site identity is required")
	if may_continue is not None and not may_continue():
		raise RuntimeError("Migration copy stopped because cloud storage was enabled")
	claim_prefix(bucket, prefix, owner, create=True)
	copied = 0
	for path, key in source_files(site_path, prefix):
		if may_continue is not None and not may_continue():
			raise RuntimeError("Migration copy stopped because cloud storage was enabled")
		check_file(bucket, path, key, copy=True, owner=owner)
		copied += 1
	return copied


def verify_snapshot(bucket, site_path, prefix, *, owner):
	"""Compare all file bytes and object names; callers must stop all file writers."""
	claim_prefix(bucket, prefix, owner, create=False)
	local_keys = set()
	for path, key in source_files(site_path, prefix):
		check_file(bucket, path, key, owner=owner)
		local_keys.add(key)
	for label in ("public", "private"):
		object_prefix = f"{prefix.strip('/')}/{label}/"
		for blob in bucket.list_blobs(prefix=object_prefix):
			if blob.name not in local_keys:
				raise RuntimeError(f"Bucket contains an unexpected object: {blob.name}")
	return len(local_keys)


def assert_no_incomplete_uploads(site_path, prefix):
	for path, _ in source_files(site_path, prefix):
		if path.name.startswith(".temp-"):
			raise RuntimeError(f"An incomplete upload must be resolved before cutover: {path}")


def verify_file_references(site_path, records):
	"""Require every local File and thumbnail reference to resolve within its visibility directory."""
	root = Path(site_path)
	checked = 0
	for record in records:
		if record.get("is_folder"):
			continue
		file_url = record.get("file_url")
		if not file_url:
			raise ValueError(f"File has no URL: {record.get('name')}")
		is_private = bool(int(record.get("is_private") or 0))
		for field, url in (("file_url", file_url), ("thumbnail_url", record.get("thumbnail_url"))):
			if not url or url.startswith(("http://", "https://", "/api/method/")):
				continue
			if url.startswith("/private/files/"):
				visibility, name = "private", url.removeprefix("/private/files/")
			elif url.startswith("/files/"):
				visibility, name = "public", url.removeprefix("/files/")
			else:
				raise ValueError(f"Invalid {field} for File {record.get('name')}: {url}")
			if is_private and visibility != "private":
				raise ValueError(f"Private File has a public {field}: {record.get('name')}")
			if is_private != (visibility == "private"):
				raise ValueError(f"File privacy does not match its {field}: {record.get('name')}")
			parts = name.split("/")
			if any(part in ("", ".", "..") for part in parts) or "\\" in name:
				raise ValueError(f"Invalid {field} for File {record.get('name')}: {url}")
			base = root / visibility / "files"
			path = base.joinpath(*parts)
			if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
				raise ValueError(f"File reference does not exist: {record.get('name')} ({url})")
			checked += 1
	return checked


def reconcile_stale_objects(bucket, site_path, prefix, *, owner, records):
	"""Remove only this site's migration copies whose local source no longer exists.

	Callers must stop file writers and migration jobs for the entire operation.
	"""
	claim_prefix(bucket, prefix, owner, create=False)
	verify_file_references(site_path, records)
	local_keys = {key for _, key in source_files(site_path, prefix)}
	stale = []
	for visibility in ("public", "private"):
		for blob in bucket.list_blobs(prefix=f"{normalize_prefix(prefix)}/{visibility}/"):
			if blob.name in local_keys:
				continue
			blob.reload()
			if (blob.metadata or {}).get("frappe_migration_site") != owner:
				raise RuntimeError(f"Unexpected object is not owned by this site's migration: {blob.name}")
			stale.append((blob, blob.generation))
	for blob, generation in stale:
		blob.delete(if_generation_match=generation)
	return len(stale)


def cleanup_plan(bucket, site_path, prefix, *, owner):
	"""Prepare a resumable deletion plan while all file writers are stopped."""
	verify_snapshot(bucket, site_path, prefix, owner=owner)
	root = Path(site_path)
	plan = []
	for path, key in source_files(root, prefix):
		digest, generation, _ = check_file(bucket, path, key, owner=owner)
		plan.append({"path": path.relative_to(root).as_posix(), "key": key, "sha256": digest, "generation": generation})
	return plan


def save_cleanup_plan(bucket, prefix, plan, *, owner, site_path=None):
	"""Persist the plan in the private bucket before any local file is removed."""
	from google.api_core.exceptions import PreconditionFailed

	claim_prefix(bucket, prefix, owner, create=False)
	prefix = normalize_prefix(prefix)
	payload = json.dumps({"owner": owner, "prefix": prefix, "entries": plan}, sort_keys=True).encode()
	blob = bucket.blob(f"{prefix}/.frappe-cleanup-plan.json")
	try:
		blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
	except PreconditionFailed:
		blob.reload()
		old_payload = blob.download_as_bytes(if_generation_match=blob.generation)
		if old_payload == payload:
			return
		if site_path is None:
			raise RuntimeError("A different cleanup plan already exists for this site")
		old = json.loads(old_payload)
		if old.get("owner") != owner or old.get("prefix") != prefix or not isinstance(old.get("entries"), list):
			raise ValueError("Invalid existing cleanup plan")
		for entry in old["entries"]:
			path = cleanup_path(Path(site_path), prefix, entry)
			if not path.is_file() or path.is_symlink():
				raise RuntimeError(f"Cannot replace cleanup plan after a local file was removed: {path}")
		blob.upload_from_string(payload, if_generation_match=blob.generation, content_type="application/json")


def load_cleanup_plan(bucket, prefix, *, owner):
	claim_prefix(bucket, prefix, owner, create=False)
	prefix = normalize_prefix(prefix)
	blob = bucket.blob(f"{prefix}/.frappe-cleanup-plan.json")
	blob.reload()
	payload = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
	if payload.get("owner") != owner or payload.get("prefix") != prefix or not isinstance(payload.get("entries"), list):
		raise ValueError("Invalid cleanup plan")
	return payload["entries"]


def cleanup_path(root, prefix, entry):
	from .storage import object_key

	relative = Path(entry["path"])
	parts = relative.parts
	if relative.is_absolute() or len(parts) < 3 or parts[0] not in ("public", "private") or parts[1] != "files":
		raise ValueError("Invalid cleanup path")
	if any(part in (".", "..") for part in parts):
		raise ValueError("Invalid cleanup path")
	url_root = "/private/files/" if parts[0] == "private" else "/files/"
	expected_key = object_key(prefix, url_root + "/".join(parts[2:]))
	if entry["key"] != expected_key:
		raise ValueError("Cleanup object key does not match local path")
	parent = root
	for part in parts[:-1]:
		parent /= part
		if parent.is_symlink():
			raise ValueError(f"Cannot use symbolic link in cleanup path: {parent}")
	return root / relative


def cleanup_files(bucket, site_path, prefix, plan, *, owner):
	"""Remove verified originals; all file writers must remain stopped until completion."""
	claim_prefix(bucket, prefix, owner, create=False)
	root = Path(site_path)
	removed = 0
	for entry in plan:
		path = cleanup_path(root, prefix, entry)
		if not os.path.lexists(path):
			continue
		if path.is_symlink():
			raise ValueError(f"Cannot remove symbolic link: {path}")
		digest, generation, identity = check_file(bucket, path, entry["key"], owner=owner)
		if digest != entry["sha256"] or generation != entry["generation"]:
			raise RuntimeError(f"File or bucket object changed after cleanup was planned: {path}")
		current = path.stat(follow_symlinks=False)
		if identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
			raise RuntimeError(f"File changed before cleanup: {path}")
		path.unlink()
		removed += 1
	return removed
