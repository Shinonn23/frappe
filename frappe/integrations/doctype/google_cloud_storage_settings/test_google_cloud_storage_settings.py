import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock, storage
from frappe.integrations.doctype.google_cloud_storage_settings import (
	google_cloud_storage_settings as settings,
)
from frappe.integrations.doctype.google_cloud_storage_settings.migrate_files import copy_files
from frappe.integrations.doctype.google_cloud_storage_settings.test_migrate_files import Bucket


@patch.object(settings, "_", lambda value: value)
class TestConnection(unittest.TestCase):
	def test_settings_save_participates_in_cutover_lock(self):
		config = SimpleNamespace(object_prefix="site", enabled=0, get_doc_before_save=lambda: None)
		with patch.object(cutover_lock, "register_writer") as register:
			settings.GoogleCloudStorageSettings.validate(config)
			register.assert_called_once()

	def test_settings_integrity_job_queues_and_publishes_after_worker_commit(self):
		import json

		import frappe
		from frappe.utils import CallbackManager
		from frappe.utils.background_jobs import execute_job

		old = {name: getattr(frappe.local, name, None) for name in ("site", "session", "conf", "flags")}
		frappe.local.site = "test-site"
		frappe.local.session = frappe._dict(user="manager@example.com")
		frappe.local.conf = frappe._dict(gcs_coordinated_cutover=0)
		frappe.local.flags = frappe._dict()
		db = Mock()
		db.after_commit, db.after_rollback = CallbackManager(), CallbackManager()
		db.InternalError = type("FakeDatabaseError", (Exception,), {})
		pending = {}
		persisted = {}
		db.set_single_value.side_effect = lambda doctype, field, value, **kwargs: pending.update({field: value})
		def commit(**kwargs):
			persisted.update(pending)
			db.after_commit.run()
		db.commit.side_effect = commit
		queue = Mock()
		try:
			with patch.object(frappe, "db", db), patch.object(frappe, "only_for"), patch.object(
				storage, "enabled", return_value=True
			), patch.object(frappe, "get_all", return_value=[]), patch.object(frappe, "get_hooks", return_value=[]), patch.object(
				frappe, "get_installed_apps", return_value=["frappe"]
			), patch(
				"frappe.utils.background_jobs.get_job", return_value=None
			), patch("frappe.utils.background_jobs.get_queue", return_value=queue), patch(
				"frappe.utils.background_jobs._check_queue_size"
			), patch("frappe.realtime.emit_via_redis") as emit:
				emit.side_effect = lambda *args: self.assertIn("last_integrity_result", persisted)
				settings.start_integrity_check()
				queue.enqueue_call.assert_not_called()
				db.commit()
				queued = queue.enqueue_call.call_args.kwargs["kwargs"]
				self.assertEqual(queued["kwargs"]["user"], "manager@example.com")
				self.assertEqual(queued["user"], "manager@example.com")
				queued["is_async"] = False
				result = execute_job(**queued)
				self.assertEqual(result["checked"], 0)
				self.assertEqual(json.loads(persisted["last_integrity_result"])["checked"], 0)
				emit.assert_called_once()
		finally:
			for name, value in old.items():
				setattr(frappe.local, name, value)
			for name in ("job", "_realtime_log"):
				if hasattr(frappe.local, name):
					delattr(frappe.local, name)

	def test_migration_result_is_saved_and_announced_after_commit(self):
		import json

		import frappe

		db = Mock()
		with patch.object(frappe, "db", db), patch.object(frappe, "publish_realtime") as publish:
			settings.save_migration_result("preflight", {"files": 3}, "done", "manager")
		report = json.loads(db.set_single_value.call_args.args[2])
		self.assertEqual(report["action"], "preflight")
		self.assertEqual(report["details"], {"files": 3})
		self.assertIn("completed_at_utc", report)
		self.assertTrue(publish.call_args.kwargs["after_commit"])

	def test_integrity_check_reports_generation_changed_during_download(self):
		from google.api_core.exceptions import PreconditionFailed

		from frappe.integrations.doctype.google_cloud_storage_settings.audit_files import check_object

		blob = Mock(generation=12)
		blob.download_to_file.side_effect = PreconditionFailed("object changed")
		self.assertEqual(check_object(blob, "hash"), "changed")
		self.assertEqual(blob.download_to_file.call_args.kwargs["if_generation_match"], 12)

	def test_integrity_check_reports_mismatch_and_skips_changed_record(self):
		import frappe

		records = [
			frappe._dict(name="one", file_url="/files/a", content_hash="hash", is_folder=0),
			frappe._dict(name="two", file_url="/private/files/b", content_hash="old", is_folder=0),
			frappe._dict(name="remote", file_url="https://example.com/a", content_hash=None, is_folder=0),
		]
		db = Mock()
		db.get_value.side_effect = [frappe._dict(file_url="/files/a", content_hash="hash"),
			frappe._dict(file_url="/private/files/b", content_hash="new")]
		with patch.object(frappe, "db", db), patch.object(frappe, "get_all", return_value=records), patch.object(
			frappe, "publish_realtime"
		), patch.object(storage, "enabled", return_value=True), patch.object(storage, "get_blob"), patch(
			"frappe.integrations.doctype.google_cloud_storage_settings.audit_files.check_object", return_value="mismatch"
		), patch("frappe.integrations.doctype.google_cloud_storage_settings.cutover_lock.register_object"):
			result = settings.run_integrity_check("manager")
		self.assertEqual((result["checked"], result["mismatch"], result["changed"], result["skipped"]), (1, 1, 1, 1))
		self.assertEqual(result["examples"], [{"file": "one", "status": "mismatch"}])
		self.assertEqual(db.rollback.call_count, 2)
		self.assertEqual(db.set_single_value.call_args.args[:2], ("Google Cloud Storage Settings", "last_integrity_result"))
		self.assertIn('"file": "one"', db.set_single_value.call_args.args[2])

	@patch.object(settings.frappe, "throw", side_effect=ValueError)
	def test_active_storage_cannot_be_disabled_or_repointed(self, throw):
		old = SimpleNamespace(enabled=1, bucket_name="bucket", object_prefix="site")
		for enabled, bucket, prefix in ((0, "bucket", "site"), (1, "other", "site"), (1, "bucket", "other")):
			doc = SimpleNamespace(
				enabled=enabled, bucket_name=bucket, object_prefix=prefix, get_doc_before_save=Mock(return_value=old)
			)
			with self.subTest(enabled=enabled, bucket=bucket, prefix=prefix), self.assertRaises(ValueError):
				settings.GoogleCloudStorageSettings.validate(doc)

	@patch.object(settings.frappe, "throw", side_effect=ValueError)
	def test_storage_stays_disabled_before_cutover_is_ready(self, throw):
		doc = SimpleNamespace(enabled=1, get_doc_before_save=Mock(return_value=None))
		with self.assertRaises(ValueError):
			settings.GoogleCloudStorageSettings.validate(doc)
	@patch.object(settings.frappe, "only_for")
	@patch.object(settings, "get_bucket")
	def test_connection_checks_required_permissions(self, get_bucket, only_for):
		bucket = SimpleNamespace(test_iam_permissions=lambda permissions: permissions)
		get_bucket.return_value = (bucket, "site")
		self.assertIn("Connected", settings.test_connection())
		only_for.assert_called_once_with("System Manager")

	@patch.object(settings.frappe, "only_for")
	@patch.object(settings, "get_bucket")
	@patch.object(settings.frappe, "throw", side_effect=ValueError)
	def test_missing_permission_fails(self, throw, get_bucket, only_for):
		bucket = SimpleNamespace(test_iam_permissions=lambda permissions: [])
		get_bucket.return_value = (bucket, "site")
		with self.assertRaises(ValueError):
			settings.test_connection()

	def test_invalid_prefix_is_rejected_before_connecting(self):
		config = SimpleNamespace(bucket_name="bucket", object_prefix="../unsafe")
		with (
			patch.object(settings.frappe, "get_single", return_value=config),
			patch.object(settings.frappe, "throw", side_effect=ValueError),
			patch("google.cloud.storage.Client") as client,
		):
			with self.assertRaises(ValueError):
				settings.get_bucket()
			client.assert_not_called()

	@patch.object(settings.frappe, "only_for")
	@patch.object(settings, "test_connection")
	@patch.object(settings.frappe, "enqueue")
	@patch.object(storage, "enabled", return_value=False)
	def test_copy_is_queued_for_system_manager(self, enabled, enqueue, test_connection, only_for):
		settings.frappe.local.session = settings.frappe._dict(user="manager@example.com")
		try:
			with patch.object(settings.frappe, "get_single", return_value=SimpleNamespace(bucket_name="bucket", object_prefix="site")):
				settings.copy_existing_files()
			enqueue.assert_called_once()
			self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
			self.assertEqual(enqueue.call_args.kwargs["user"], "manager@example.com")
			self.assertEqual(enqueue.call_args.kwargs["bucket_name"], "bucket")
			self.assertEqual(enqueue.call_args.kwargs["object_prefix"], "site")
			test_connection.assert_called_once()
		finally:
			del settings.frappe.local.session

	def test_queued_target_rejects_changed_settings_before_connecting(self):
		with patch.object(settings.frappe, "get_single", return_value=SimpleNamespace(bucket_name="other", object_prefix="site")), patch.object(settings, "get_bucket") as get_bucket:
			with self.assertRaisesRegex(RuntimeError, "settings changed"):
				settings.queued_target("bucket", "site")
			get_bucket.assert_not_called()

	def test_queued_target_connects_using_the_checked_settings(self):
		config = SimpleNamespace(bucket_name="bucket", object_prefix="site")
		with patch.object(settings.frappe, "get_single", return_value=config) as get_single, patch.object(settings, "get_bucket") as get_bucket:
			settings.queued_target("bucket", "site")
			get_single.assert_called_once()
			get_bucket.assert_called_once_with(config)

	@patch.object(settings.frappe, "only_for")
	@patch.object(settings.frappe, "throw", side_effect=ValueError)
	@patch.object(storage, "enabled", return_value=True)
	def test_copy_is_forbidden_after_activation(self, enabled, throw, only_for):
		with self.assertRaises(ValueError):
			settings.copy_existing_files()

	def test_preflight_checks_files_under_exclusive_lock_without_enabling_storage(self):
		import frappe

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "private/files/a.pdf").write_bytes(b"private")
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			record = {"name": "file-1", "file_url": "/private/files/a.pdf", "is_private": 1}
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site.example"
			try:
				with (
					patch.object(cutover_lock, "LOCK_DIRECTORY", root),
					patch.object(settings, "get_bucket", return_value=(bucket, "site")),
					patch.object(frappe, "get_single", return_value=SimpleNamespace(bucket_name="bucket", object_prefix="site")),
					patch.object(storage, "enabled", return_value=False),
					patch.object(frappe, "get_site_path", return_value=str(root)),
					patch.object(frappe, "get_all", return_value=[record]),
					patch.object(frappe, "db", Mock()),
					patch.object(frappe, "publish_realtime"),
					patch("frappe.utils.get_bench_path", return_value="/bench"),
				):
					self.assertEqual(settings.run_preflight("manager", "bucket", "site"), {"files": 1, "references": 1})
					with cutover_lock.cutover_lock("site.example", "/bench", exclusive=False):
						with self.assertRaises(frappe.RetryBackgroundJobError):
							settings.run_preflight("manager", "bucket", "site")
						with self.assertRaises(frappe.RetryBackgroundJobError):
							settings.run_reconcile("manager", "bucket", "site")
					(root / "private/files/a.pdf").unlink()
					frappe.get_all.return_value = []
					self.assertEqual(settings.run_reconcile("manager", "bucket", "site"), 1)
					self.assertNotIn("site/private/a.pdf", bucket.objects)
			finally:
				del frappe.local.conf
				del frappe.local.site


@patch.object(settings, "_", lambda value: value)
class TestActivation(unittest.TestCase):
	def test_cutover_commits_activation_before_removing_local_files(self):
		import frappe

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			local = root / "private/files/a.pdf"
			local.write_bytes(b"private")
			bucket = Bucket()
			config = SimpleNamespace(bucket_name="bucket", object_prefix="site", isolation_confirmed=1, proxy_confirmed=1)
			state = {"enabled": False, "commits": 0}
			db = Mock()

			def set_enabled(doctype, field, value, **kwargs):
				if field == "last_migration_result":
					return
				self.assertEqual((doctype, field, value), ("Google Cloud Storage Settings", "enabled", 1))
				self.assertTrue(local.exists())
				self.assertIn("site/.frappe-cleanup-plan.json", bucket.objects)
				state["enabled"] = True

			db.set_single_value.side_effect = set_enabled
			db.commit.side_effect = lambda: state.update(commits=state["commits"] + 1)
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1, pause_scheduler=1)
			frappe.local.site = "site.example"
			try:
				with (
					patch.object(cutover_lock, "LOCK_DIRECTORY", root), patch.object(frappe, "db", db),
					patch.object(frappe, "get_single", return_value=config), patch.object(frappe, "get_site_path", return_value=str(root)),
					patch.object(frappe, "get_all", return_value=[{"name": "file-1", "file_url": "/private/files/a.pdf", "is_private": 1}]),
					patch.object(frappe, "clear_document_cache"), patch.object(frappe, "publish_realtime"),
					patch.object(settings, "get_bucket", return_value=(bucket, "site")), patch.object(settings, "test_connection"),
					patch.object(storage, "enabled", side_effect=lambda: state["enabled"]),
					patch("frappe.utils.get_bench_path", return_value="/bench"),
				):
					result = settings.run_cutover("manager", "bucket", "site")
					self.assertEqual(settings.run_cleanup("manager", "bucket", "site"), 0)
				self.assertTrue(state["enabled"])
				self.assertGreaterEqual(state["commits"], 2)
				self.assertEqual(result["removed"], 1)
				self.assertFalse(local.exists())
				self.assertEqual(bucket.objects["site/private/a.pdf"], b"private")
			finally:
				del frappe.local.conf
				del frappe.local.site

	def test_cutover_rejects_unconfirmed_isolation(self):
		import frappe

		frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1, pause_scheduler=1)
		config = SimpleNamespace(isolation_confirmed=0, proxy_confirmed=1)
		try:
			with patch.object(frappe, "get_single", return_value=config), patch.object(frappe, "throw", side_effect=ValueError):
				with self.assertRaises(ValueError):
					settings.check_cutover_prerequisites(active=False)
		finally:
			del frappe.local.conf

	def test_queued_job_rejects_changed_bucket_before_connection(self):
		import frappe

		frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1, pause_scheduler=1)
		config = SimpleNamespace(bucket_name="new-bucket", object_prefix="site", isolation_confirmed=1, proxy_confirmed=1)
		try:
			with (
				patch.object(frappe, "get_single", return_value=config), patch.object(storage, "enabled", return_value=False),
				patch.object(settings, "get_bucket") as get_bucket,
			):
				with self.assertRaisesRegex(RuntimeError, "changed"):
					settings._cutover_target("old-bucket", "site", active=False)
				get_bucket.assert_not_called()
		finally:
			del frappe.local.conf

	def test_cleanup_refuses_to_run_before_storage_is_active(self):
		import frappe

		frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1, pause_scheduler=1)
		config = SimpleNamespace(isolation_confirmed=1, proxy_confirmed=1)
		try:
			with (
				patch.object(frappe, "get_single", return_value=config), patch.object(storage, "enabled", return_value=False),
				patch.object(frappe, "throw", side_effect=ValueError),
			):
				with self.assertRaises(ValueError):
					settings.check_cutover_prerequisites(active=True)
		finally:
			del frappe.local.conf
