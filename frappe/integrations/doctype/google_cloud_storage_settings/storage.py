# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib

import frappe
from frappe.utils import cint


def memory_directory():
	with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
		if not any(
			line.split(" - ", 1)[0].split()[4] == "/dev/shm"
			and line.split(" - ", 1)[1].split()[0] in ("tmpfs", "ramfs")
			for line in mounts
		):
			raise RuntimeError("File processing requires /dev/shm to be a memory-backed filesystem")
	return "/dev/shm"


def enabled():
	# A stale Redis document could otherwise send uploads to local disk after cutover.
	values = frappe.qb.get_query(
		"Singles", filters={"doctype": "Google Cloud Storage Settings", "field": "enabled"}, fields=["value"]
	).run()
	return bool(cint(values[0][0])) if values else False


def public_url_hash(file_url):
	return hashlib.sha256(file_url.encode("utf-8")).hexdigest()


def block_public(file_url):
	url_hash = public_url_hash(file_url)
	if not frappe.db.exists("GCS Public File Block", {"url_hash": url_hash}):
		frappe.get_doc({"doctype": "GCS Public File Block", "url_hash": url_hash, "file_url": file_url}).insert(
			ignore_permissions=True
		)


def unblock_public(file_url):
	frappe.db.delete("GCS Public File Block", {"url_hash": public_url_hash(file_url)})


def public_blocked(file_url):
	return bool(frappe.db.exists("GCS Public File Block", {"url_hash": public_url_hash(file_url)}))


def allow_unmanaged_public(file_url, generation):
	if not file_url.startswith("/files/") or generation is None:
		raise ValueError("A public URL and object generation are required")
	url_hash = public_url_hash(file_url)
	name = frappe.db.get_value("GCS Public File Entry", {"url_hash": url_hash}, "name")
	if name:
		frappe.db.set_value("GCS Public File Entry", name, "generation", str(generation))
	else:
		frappe.get_doc({
			"doctype": "GCS Public File Entry", "url_hash": url_hash,
			"file_url": file_url, "generation": str(generation),
		}).insert(ignore_permissions=True)


def unmanaged_public_generation(file_url):
	return frappe.db.get_value("GCS Public File Entry", {"url_hash": public_url_hash(file_url)}, "generation")


def private_thumbnail(file_url):
	return frappe.db.get_value(
		"GCS Private Thumbnail", {"url_hash": public_url_hash(file_url)}, ["source_file", "generation"], as_dict=True
	)


def object_key(prefix, file_url):
	"""Map a local Frappe file URL to an object key within one site's prefix."""
	from .migrate_files import normalize_prefix

	prefix = normalize_prefix(prefix)
	if file_url.startswith("/private/files/"):
		visibility, name = "private", file_url.removeprefix("/private/files/")
	elif file_url.startswith("/files/"):
		visibility, name = "public", file_url.removeprefix("/files/")
	else:
		raise ValueError("Not a Frappe file URL")
	if any(part in ("", ".", "..") for part in name.split("/")) or "\\" in name or "\x00" in name:
		raise ValueError("Invalid file URL")
	return f"{prefix}/{visibility}/{name}"


def get_blob(file_url):
	from google.cloud import storage

	settings = frappe.get_single("Google Cloud Storage Settings")
	client = storage.Client()
	return client.bucket(settings.bucket_name).blob(object_key(settings.object_prefix, file_url))


def staging_bucket():
	from google.cloud import storage

	settings = frappe.get_single("Google Cloud Storage Settings")
	return storage.Client().bucket(settings.bucket_name), settings.object_prefix


def read(file_url):
	return get_blob(file_url).download_as_bytes()


def read_versioned(file_url):
	blob = get_blob(file_url)
	blob.reload()
	generation = blob.generation
	return blob.download_as_bytes(if_generation_match=generation), generation


def read_public(file_url):
	from google.api_core.exceptions import NotFound as GCSNotFound
	from google.api_core.exceptions import PreconditionFailed
	from werkzeug.exceptions import NotFound

	if not file_url.startswith("/files/") or public_blocked(file_url):
		raise NotFound
	managed = any(
		file_url in frappe.db.get_values("File", {field: file_url, "is_private": 0}, field, pluck=True, order_by=None)
		for field in ("file_url", "thumbnail_url")
	)
	try:
		if managed:
			return read(file_url)
		expected = unmanaged_public_generation(file_url)
		if not expected:
			raise NotFound
		content, generation = read_versioned(file_url)
		if str(generation) != str(expected):
			raise NotFound
		return content
	except (GCSNotFound, PreconditionFailed, ValueError) as exc:
		raise NotFound from exc


def exists(file_url):
	return get_blob(file_url).exists()


def generation(file_url):
	from google.api_core.exceptions import NotFound

	blob = get_blob(file_url)
	try:
		blob.reload()
	except NotFound:
		return None
	return blob.generation


def write(file_url, content, *, content_type=None, if_generation_match=None):
	blob = get_blob(file_url)
	blob.upload_from_string(
		content, content_type=content_type, if_generation_match=if_generation_match
	)
	return blob.generation


def restore_on_rollback(file_url, original, generation):
	if getattr(frappe.db, "_gcs_savepoints", None):
		history = getattr(frappe.db, "_gcs_overwrite_history", None)
		if not isinstance(history, list):
			history = frappe.db._gcs_overwrite_history = []
		history.append((file_url, original))
	states = getattr(frappe.local, "gcs_overwrites", None)
	if states is None:
		states = frappe.local.gcs_overwrites = {}
	if file_url in states:
		states[file_url]["generation"] = generation
		return
	state = states[file_url] = {"original": original, "generation": generation}

	def restore():
		from google.api_core.exceptions import PreconditionFailed

		try:
			write(file_url, state["original"], if_generation_match=state["generation"])
		except PreconditionFailed:
			frappe.logger().error("GCS rollback skipped a concurrently modified object: %s", file_url)
			frappe.db.after_rollback.add(restore)
			raise
		except Exception:
			frappe.db.after_rollback.add(restore)
			raise
		else:
			states.pop(file_url, None)

	frappe.db.after_rollback.add(restore)
	frappe.db.after_commit.add(lambda: states.pop(file_url, None))


def rollback_savepoint_overwrites(history, offset):
	states = frappe.local.gcs_overwrites
	while len(history) > offset:
		file_url, previous_content = history[-1]
		state = states[file_url]
		state["generation"] = write(file_url, previous_content, if_generation_match=state["generation"])
		history.pop()


def copy(source_url, target_url):
	source = get_blob(source_url)
	target = get_blob(target_url)
	source.reload()
	source_generation = source.generation
	copied = source.bucket.copy_blob(
		source, target.bucket, target.name, if_source_generation_match=source_generation, if_generation_match=0
	)
	return source_generation, copied.generation


def delete(file_url, *, if_generation_match=None):
	from google.api_core.exceptions import NotFound

	if file_url:
		try:
			get_blob(file_url).delete(if_generation_match=if_generation_match)
		except NotFound:
			pass


def delete_if_unreferenced(file_url, generation):
	"""Keep the object if a committed File or matching unmanaged entry still needs it."""
	if frappe.db.exists("File", {"file_url": file_url}) or frappe.db.exists("File", {"thumbnail_url": file_url}):
		return
	if file_url.startswith("/files/") and str(unmanaged_public_generation(file_url)) == str(generation):
		return
	if file_url.startswith("/private/files/"):
		entry = private_thumbnail(file_url)
		if entry and str(entry.generation) == str(generation):
			return
	delete(file_url, if_generation_match=generation)


def cleanup_unreferenced_after_commit(file_url, generation):
	frappe.db.after_commit.add(lambda: delete_if_unreferenced(file_url, generation))


def schedule_file_deletion(file_url):
	from .cutover_lock import register_object

	if not file_url:
		return
	if not file_url.startswith(("/files/", "/private/files/")):
		raise ValueError("Not a Frappe file URL")
	register_object(file_url)
	object_generation = generation(file_url)
	if object_generation is not None:
		cleanup_unreferenced_after_commit(file_url, object_generation)
