# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from datetime import UTC

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class GoogleCloudStorageSettings(Document):
	def validate(self):
		from .cutover_lock import register_writer
		from .migrate_files import normalize_prefix

		register_writer()
		if prefix := getattr(self, "object_prefix", None):
			try:
				normalize_prefix(prefix)
			except ValueError as exc:
				frappe.throw(_("Invalid object prefix: {0}").format(str(exc)))
		previous = self.get_doc_before_save()
		if previous and cint(previous.enabled):
			if not cint(self.enabled) or self.bucket_name != previous.bucket_name or self.object_prefix != previous.object_prefix:
				frappe.throw(_("Storage cannot be disabled or moved until a verified reverse migration is available."))
			return
		if cint(self.enabled):
			frappe.throw(_("Use the Activate Storage button to verify and migrate files before enabling."))


def get_bucket(settings=None):
	from google.cloud import storage

	from .migrate_files import normalize_prefix

	if settings is None:
		settings = frappe.get_single("Google Cloud Storage Settings")
	if not settings.bucket_name or not settings.object_prefix:
		frappe.throw(_("Enter the bucket name and a unique object prefix first."))
	try:
		prefix = normalize_prefix(settings.object_prefix)
	except ValueError as exc:
		frappe.throw(_("Invalid object prefix: {0}").format(str(exc)))
	bucket = storage.Client().get_bucket(settings.bucket_name)
	if bucket.iam_configuration.public_access_prevention != "enforced":
		frappe.throw(_("Enable Public Access Prevention on the bucket before storing files."))
	return bucket, prefix


@frappe.whitelist(methods=["POST"])
def test_connection():
	frappe.only_for("System Manager")
	bucket = get_bucket()[0]
	required = {"storage.objects.create", "storage.objects.delete", "storage.objects.get", "storage.objects.list"}
	granted = set(bucket.test_iam_permissions(sorted(required)))
	if missing := required - granted:
		frappe.throw(_("Missing bucket permissions: {0}").format(", ".join(sorted(missing))))
	return _("Connected. The bucket prevents public access and the required permissions are available.")


def save_migration_result(action, details, message, user):
	from datetime import datetime, timezone

	report = {"action": action, "completed_at_utc": datetime.now(UTC).isoformat(), "details": details}
	frappe.db.set_single_value("Google Cloud Storage Settings", "last_migration_result", frappe.as_json(report), update_modified=False)
	frappe.publish_realtime("gcs_migration_finished", {"message": message}, user=user, after_commit=True)


@frappe.whitelist(methods=["POST"])
def copy_existing_files():
	frappe.only_for("System Manager")
	from . import storage

	if storage.enabled():
		frappe.throw(_("Copy is unavailable after Google Cloud Storage has been enabled."))
	test_connection()
	settings = frappe.get_single("Google Cloud Storage Settings")
	frappe.enqueue(
		"frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.run_copy",
		queue="long",
		timeout=86400,
		job_id="gcs-copy-existing-files",
		deduplicate=True,
		enqueue_after_commit=True,
		user=frappe.session.user,
		bucket_name=settings.bucket_name, object_prefix=settings.object_prefix,
	)
	return _("Copy queued. Track its status in Background Jobs. Original files remain on the VM.")


def run_copy(user, bucket_name, object_prefix):
	from . import storage
	from .cutover_lock import register_writer
	from .migrate_files import copy_files

	register_writer()
	bucket, prefix = queued_target(bucket_name, object_prefix)
	count = copy_files(
		bucket, frappe.get_site_path(), prefix, owner=frappe.local.site, may_continue=lambda: not storage.enabled()
	)
	save_migration_result("copy", {"files": count}, _("Copied and verified {0} files. Original files remain on the VM.").format(count), user)
	return count


@frappe.whitelist(methods=["POST"])
def start_preflight():
	frappe.only_for("System Manager")
	check_local_guard()
	test_connection()
	settings = frappe.get_single("Google Cloud Storage Settings")
	frappe.enqueue(
		"frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.run_preflight",
		queue="long",
		timeout=86400,
		job_id="gcs-cutover-preflight",
		deduplicate=True,
		enqueue_after_commit=True,
		user=frappe.session.user,
		bucket_name=settings.bucket_name, object_prefix=settings.object_prefix,
	)
	return _("Preflight queued. Refresh Settings after it finishes; storage will not be enabled.")


def check_local_guard():
	from . import storage

	if storage.enabled():
		frappe.throw(_("This operation is only available before Google Cloud Storage is enabled."))
	if not cint(frappe.conf.get("gcs_coordinated_cutover")):
		frappe.throw(_("Enable the coordinated cutover guard and restart all site processes first."))


@frappe.whitelist(methods=["POST"])
def start_reconcile():
	frappe.only_for("System Manager")
	check_local_guard()
	test_connection()
	settings = frappe.get_single("Google Cloud Storage Settings")
	frappe.enqueue(
		"frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.run_reconcile",
		queue="long",
		timeout=86400,
		job_id="gcs-copy-reconcile",
		deduplicate=True,
		enqueue_after_commit=True,
		user=frappe.session.user,
		bucket_name=settings.bucket_name, object_prefix=settings.object_prefix,
	)
	return _("Reconciliation queued. Check Background Jobs for its result. Local files will not be removed.")


def run_reconcile(user, bucket_name, object_prefix):
	from frappe.utils import get_bench_path

	from . import storage
	from .cutover_lock import CutoverBusyError, cutover_lock
	from .migrate_files import assert_no_incomplete_uploads, reconcile_stale_objects

	if not cint(frappe.conf.get("gcs_coordinated_cutover")) or storage.enabled():
		raise RuntimeError("Reconciliation requires the cutover guard and local storage")
	try:
		with cutover_lock(frappe.local.site, get_bench_path(), exclusive=True):
			frappe.db.commit()
			bucket, prefix = queued_target(bucket_name, object_prefix)
			site_path = frappe.get_site_path()
			assert_no_incomplete_uploads(site_path, prefix)
			records = frappe.get_all(
				"File", fields=["name", "file_url", "thumbnail_url", "is_private", "is_folder"], limit_page_length=0
			)
			removed = reconcile_stale_objects(bucket, site_path, prefix, owner=frappe.local.site, records=records)
	except CutoverBusyError as exc:
		raise frappe.RetryBackgroundJobError("File writers are still active") from exc
	message = _("Removed {0} stale migration copies from the bucket. Local files were not removed.").format(removed)
	save_migration_result("reconcile", {"removed": removed}, message, user)
	return removed


def run_preflight(user, bucket_name, object_prefix):
	from frappe.utils import get_bench_path

	from . import storage
	from .cutover_lock import CutoverBusyError, cutover_lock
	from .migrate_files import assert_no_incomplete_uploads, verify_file_references, verify_snapshot

	if not cint(frappe.conf.get("gcs_coordinated_cutover")) or storage.enabled():
		raise RuntimeError("Preflight requires the cutover guard and local storage")
	try:
		with cutover_lock(frappe.local.site, get_bench_path(), exclusive=True):
			frappe.db.commit()
			bucket, prefix = queued_target(bucket_name, object_prefix)
			site_path = frappe.get_site_path()
			assert_no_incomplete_uploads(site_path, prefix)
			records = frappe.get_all(
				"File", fields=["name", "file_url", "thumbnail_url", "is_private", "is_folder"], limit_page_length=0
			)
			references = verify_file_references(site_path, records)
			files = verify_snapshot(bucket, site_path, prefix, owner=frappe.local.site)
	except CutoverBusyError as exc:
		raise frappe.RetryBackgroundJobError("File writers are still active") from exc
	message = _("Preflight verified {0} local files and {1} File references. Storage is still disabled.").format(
		files, references
	)
	save_migration_result("preflight", {"files": files, "references": references}, message, user)
	return {"files": files, "references": references}


def check_cutover_prerequisites(*, active):
	from . import storage

	if not cint(frappe.conf.get("gcs_coordinated_cutover")):
		frappe.throw(_("Enable the cutover guard and restart every site process first."))
	if not cint(frappe.conf.get("pause_scheduler")):
		frappe.throw(_("Pause the scheduler before migrating file storage."))
	settings = frappe.get_single("Google Cloud Storage Settings")
	if not cint(settings.isolation_confirmed) or not cint(settings.proxy_confirmed):
		frappe.throw(_("Confirm process isolation and Nginx routing before migrating file storage."))
	if storage.enabled() != active:
		frappe.throw(_("File storage is already enabled.") if not active else _("File storage is not enabled."))
	return settings


@frappe.whitelist(methods=["POST"])
def start_cutover():
	frappe.only_for("System Manager")
	settings = check_cutover_prerequisites(active=False)
	test_connection()
	frappe.enqueue(
		"frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.run_cutover",
		queue="long", timeout=86400, job_id="gcs-storage-cutover", deduplicate=True,
		enqueue_after_commit=True, user=frappe.session.user,
		bucket_name=settings.bucket_name, object_prefix=settings.object_prefix,
	)
	return _("Migration queued. Check Background Jobs for its result.")


@frappe.whitelist(methods=["POST"])
def start_cleanup():
	frappe.only_for("System Manager")
	settings = check_cutover_prerequisites(active=True)
	test_connection()
	frappe.enqueue(
		"frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.run_cleanup",
		queue="long", timeout=86400, job_id="gcs-storage-cleanup", deduplicate=True,
		enqueue_after_commit=True, user=frappe.session.user,
		bucket_name=settings.bucket_name, object_prefix=settings.object_prefix,
	)
	return _("Cleanup queued. Check Background Jobs for its result.")


@frappe.whitelist(methods=["POST"])
def start_integrity_check():
	frappe.only_for("System Manager")
	from . import storage

	if not storage.enabled():
		frappe.throw(_("Enable Google Cloud Storage before checking file integrity."))
	frappe.enqueue(
		"frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.run_integrity_check",
		queue="long", timeout=86400, job_id="gcs-file-integrity", deduplicate=True,
		enqueue_after_commit=True, user=frappe.session.user,
	)
	return _("Integrity check queued. Refresh Settings after the job finishes to view the report.")


def run_integrity_check(user):
	from datetime import datetime, timezone

	from . import storage
	from .audit_files import check_object
	from .cutover_lock import register_object

	if not storage.enabled():
		raise RuntimeError("Google Cloud Storage is not enabled")
	records = frappe.get_all("File", fields=["name", "file_url", "content_hash", "is_folder"], limit_page_length=0)
	result = {"checked": 0, "skipped": 0, "missing": 0, "mismatch": 0, "changed": 0, "examples": []}
	for record in records:
		url = record.file_url
		if record.is_folder or not url or not url.startswith(("/files/", "/private/files/")) or not record.content_hash:
			result["skipped"] += 1
			continue
		register_object(url)
		try:
			current = frappe.db.get_value("File", record.name, ["file_url", "content_hash"], as_dict=True, for_update=True)
			if not current or current.file_url != url or current.content_hash != record.content_hash:
				result["changed"] += 1
				continue
			status = check_object(storage.get_blob(url), current.content_hash)
			result["checked"] += 1
			if status != "ok":
				result[status] += 1
				if len(result["examples"]) < 100:
					result["examples"].append({"file": record.name, "status": status})
		finally:
			frappe.db.rollback()
	result["completed_at_utc"] = datetime.now(UTC).isoformat()
	frappe.db.set_single_value("Google Cloud Storage Settings", "last_integrity_result", frappe.as_json(result), update_modified=False)
	message = _("Integrity check finished: {0} checked, {1} missing, {2} mismatched, {3} changed or skipped.").format(
		result["checked"], result["missing"], result["mismatch"], result["changed"] + result["skipped"]
	)
	frappe.publish_realtime("gcs_migration_finished", {
		"message": message, "indicator": "orange" if result["missing"] or result["mismatch"] or result["changed"] else "green"
	}, user=user, after_commit=True)
	return result


def queued_target(bucket_name, object_prefix):
	settings = frappe.get_single("Google Cloud Storage Settings")
	if settings.bucket_name != bucket_name or settings.object_prefix != object_prefix:
		raise RuntimeError("Bucket settings changed after this job was queued")
	return get_bucket(settings)


def _cutover_target(bucket_name, object_prefix, *, active):
	settings = check_cutover_prerequisites(active=active)
	if settings.bucket_name != bucket_name or settings.object_prefix != object_prefix:
		raise RuntimeError("Bucket settings changed after this job was queued")
	test_connection()
	return get_bucket(settings)


def run_cutover(user, bucket_name, object_prefix):
	from frappe.utils import get_bench_path

	from .cutover import perform_cutover
	from .cutover_lock import CutoverBusyError

	def activate(unmanaged):
		from .storage import allow_unmanaged_public

		check_cutover_prerequisites(active=False)
		for file_url, generation in unmanaged:
			allow_unmanaged_public(file_url, generation)
		frappe.db.set_single_value("Google Cloud Storage Settings", "enabled", 1)
		frappe.db.commit()
		frappe.clear_document_cache("Google Cloud Storage Settings", "Google Cloud Storage Settings")

	try:
		result = perform_cutover(
			frappe.get_site_path(), owner=frappe.local.site, bench_path=get_bench_path(), refresh=frappe.db.commit,
			target=lambda: _cutover_target(bucket_name, object_prefix, active=False),
			records=lambda: frappe.get_all(
				"File", fields=["name", "file_url", "thumbnail_url", "is_private", "is_folder"], limit_page_length=0
			),
			activate=activate,
		)
	except CutoverBusyError as exc:
		raise frappe.RetryBackgroundJobError("File writers are still active") from exc
	save_migration_result("activate", result, _("Cloud storage activated and {0} local files removed.").format(result["removed"]), user)
	return result


def run_cleanup(user, bucket_name, object_prefix):
	from frappe.utils import get_bench_path

	from .cutover import resume_cleanup
	from .cutover_lock import CutoverBusyError

	try:
		removed = resume_cleanup(
			frappe.get_site_path(), owner=frappe.local.site, bench_path=get_bench_path(), refresh=frappe.db.commit,
			target=lambda: _cutover_target(bucket_name, object_prefix, active=True),
		)
	except CutoverBusyError as exc:
		raise frappe.RetryBackgroundJobError("File writers are still active") from exc
	save_migration_result("cleanup", {"removed": removed}, _("Removed {0} verified local files.").format(removed), user)
	return removed
