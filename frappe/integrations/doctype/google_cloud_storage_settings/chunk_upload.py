# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from google.api_core.exceptions import NotFound, PreconditionFailed

from .migrate_files import normalize_prefix


def stage_chunk(bucket, prefix, *, session, upload_id, context, index, count, offset, total_size, content, max_size):
	"""Stage one ordered, session-bound chunk; never use a local temporary file."""
	try:
		upload_id = UUID(str(upload_id)).hex
		index, count, offset, total_size = map(int, (index, count, offset, total_size))
	except (ValueError, TypeError, AttributeError) as exc:
		raise ValueError("Invalid upload identifier or chunk parameters") from exc
	if not session or not 1 < count <= 10000 or not 0 <= index < count or not 0 < total_size <= max_size:
		raise ValueError("Invalid upload size or chunk count")
	if not content or offset < 0 or offset + len(content) > total_size:
		raise ValueError("Invalid chunk offset or size")

	owner = hashlib.sha256(str(session).encode()).hexdigest()
	root = f"{normalize_prefix(prefix)}/.uploads/{owner}/{upload_id}"
	manifest = bucket.blob(f"{root}/manifest.json")
	fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
	if index == 0:
		initial = {"fingerprint": fingerprint, "count": count, "total": total_size, "next": 0, "offset": 0}
		try:
			manifest.upload_from_string(json.dumps(initial).encode(), if_generation_match=0, content_type="application/json")
		except PreconditionFailed:
			pass
	try:
		manifest.reload()
	except NotFound as exc:
		raise ValueError("Upload is missing or has expired; restart it") from exc
	manifest_generation = manifest.generation
	state = json.loads(manifest.download_as_bytes(if_generation_match=manifest_generation))
	if not isinstance(state, dict) or type(state.get("next")) is not int or type(state.get("offset")) is not int:
		raise ValueError("Invalid upload manifest")
	if not 0 <= state["next"] <= count or not 0 <= state["offset"] <= total_size:
		raise ValueError("Invalid upload manifest")
	if state.get("fingerprint") != fingerprint or state.get("count") != count or state.get("total") != total_size:
		raise ValueError("Upload parameters do not match the original upload")
	if index > state["next"] or (index == state["next"] and offset != state["offset"]):
		raise ValueError("Chunks must be uploaded in order")

	part = bucket.blob(f"{root}/{index:08d}")
	part.metadata = {"chunk_offset": str(offset)}
	try:
		part.upload_from_string(content, if_generation_match=0, content_type="application/octet-stream")
	except PreconditionFailed:
		part.reload()
		if (part.metadata or {}).get("chunk_offset") != str(offset) or part.download_as_bytes(
			if_generation_match=part.generation
		) != content:
			raise ValueError("A different chunk has already been uploaded")
	if index == state["next"]:
		state["next"] += 1
		state["offset"] += len(content)
		if (state["next"] == count) != (state["offset"] == total_size):
			raise ValueError("Chunk sequence does not match the declared file size")
		manifest.upload_from_string(json.dumps(state).encode(), if_generation_match=manifest_generation, content_type="application/json")
	if index != count - 1:
		return None, None
	if state["next"] != count or state["offset"] != total_size:
		raise ValueError("Upload is incomplete")

	parts = []
	objects = []
	for part_index in range(count):
		blob = bucket.blob(f"{root}/{part_index:08d}")
		try:
			blob.reload()
		except NotFound as exc:
			raise ValueError("An upload chunk is missing; restart the upload") from exc
		generation = blob.generation
		parts.append(blob.download_as_bytes(if_generation_match=generation))
		objects.append((blob.name, generation))
	payload = b"".join(parts)
	if len(payload) != total_size:
		raise ValueError("Uploaded file size does not match the declared size")
	manifest.reload()
	objects.append((manifest.name, manifest.generation))
	return payload, objects


def remove_staged_chunks(bucket, objects):
	"""Delete only the object generations used to assemble the committed file."""
	for name, generation in objects:
		try:
			bucket.blob(name).delete(if_generation_match=generation)
		except (NotFound, PreconditionFailed):
			continue


def clean_expired_chunks(bucket, prefix, *, now=None):
	"""Remove incomplete uploads that have had no object updates for a day."""
	cutoff = (now or datetime.now(UTC)) - timedelta(days=1)
	base = f"{normalize_prefix(prefix)}/.uploads/"
	pattern = re.compile(r"([0-9a-f]{64})/([0-9a-f]{32})/(manifest\.json|[0-9]{8})\Z")
	groups = {}
	for blob in bucket.list_blobs(prefix=base):
		match = pattern.fullmatch(blob.name.removeprefix(base))
		if match:
			groups.setdefault((match.group(1), match.group(2)), []).append(blob)

	removed = 0
	for blobs in groups.values():
		if any(blob.updated is None or blob.updated >= cutoff for blob in blobs):
			continue
		manifest = next((blob for blob in blobs if blob.name.endswith("/manifest.json")), None)
		if manifest:
			try:
				manifest.reload()
				if manifest.updated is None or manifest.updated >= cutoff:
					continue
				manifest.delete(if_generation_match=manifest.generation)
				removed += 1
			except PreconditionFailed:
				continue
			except NotFound:
				pass
		for blob in blobs:
			if blob is manifest:
				continue
			try:
				blob.delete(if_generation_match=blob.generation)
				removed += 1
			except (NotFound, PreconditionFailed):
				continue
	return removed


def cleanup_abandoned_uploads():
	from . import storage

	if storage.enabled():
		bucket, prefix = storage.staging_bucket()
		return clean_expired_chunks(bucket, prefix)
