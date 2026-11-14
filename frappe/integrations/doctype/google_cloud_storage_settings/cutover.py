# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Cutover steps for a site whose writers have been isolated."""

from .cutover_lock import cutover_lock
from .migrate_files import (
	assert_no_incomplete_uploads,
	cleanup_files,
	cleanup_plan,
	copy_files,
	load_cleanup_plan,
	reconcile_stale_objects,
	save_cleanup_plan,
	verify_file_references,
)


def perform_cutover(site_path, *, owner, bench_path, refresh, target, records, activate):
	"""Reconcile and verify a stopped site before committing the storage switch."""
	with cutover_lock(owner, bench_path, exclusive=True):
		refresh()
		bucket, prefix = target()
		assert_no_incomplete_uploads(site_path, prefix)
		records = list(records())
		references = verify_file_references(site_path, records)
		copied = copy_files(bucket, site_path, prefix, owner=owner)
		stale = reconcile_stale_objects(bucket, site_path, prefix, owner=owner, records=records)
		plan = cleanup_plan(bucket, site_path, prefix, owner=owner)
		save_cleanup_plan(bucket, prefix, plan, owner=owner, site_path=site_path)
		managed = {
			url for record in records if not int(record.get("is_private") or 0)
			for url in (record.get("file_url"), record.get("thumbnail_url")) if url and url.startswith("/files/")
		}
		unmanaged = [
			("/files/" + entry["path"].removeprefix("public/files/"), entry["generation"])
			for entry in plan if entry["path"].startswith("public/files/")
			and "/files/" + entry["path"].removeprefix("public/files/") not in managed
		]
		activate(unmanaged)
		removed = cleanup_files(bucket, site_path, prefix, plan, owner=owner)
		return {"copied": copied, "references": references, "stale": stale, "removed": removed}


def resume_cleanup(site_path, *, owner, bench_path, refresh, target):
	"""Resume cleanup using the manifest saved before the storage switch."""
	with cutover_lock(owner, bench_path, exclusive=True):
		refresh()
		bucket, prefix = target()
		plan = load_cleanup_plan(bucket, prefix, owner=owner)
		return cleanup_files(bucket, site_path, prefix, plan, owner=owner)
