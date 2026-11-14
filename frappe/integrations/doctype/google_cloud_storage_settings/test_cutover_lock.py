import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock


class TestCutoverLock(unittest.TestCase):
	def test_settings_validation_is_rejected_during_cutover(self):
		import frappe
		from frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings import (
			GoogleCloudStorageSettings,
		)

		old_conf, old_site = getattr(frappe.local, "conf", None), getattr(frappe.local, "site", None)
		try:
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site"
			config = SimpleNamespace(object_prefix="site", enabled=0, get_doc_before_save=lambda: None)
			with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)), patch("frappe.utils.get_bench_path", return_value="/bench"), patch.object(frappe, "_", lambda value: value), patch.object(frappe, "throw", side_effect=RuntimeError):
				with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
					with self.assertRaises(RuntimeError):
						GoogleCloudStorageSettings.validate(config)
		finally:
			frappe.local.conf, frappe.local.site = old_conf, old_site

	def test_read_only_mode_uses_primary_when_storage_is_coordinated(self):
		import frappe
		from frappe.app import setup_read_only_mode

		db = SimpleNamespace(begin=unittest.mock.Mock())
		old_conf = getattr(frappe.local, "conf", None)
		old_flags = getattr(frappe.local, "flags", None)
		try:
			frappe.local.conf = frappe._dict(read_from_replica=1, gcs_coordinated_cutover=1)
			frappe.local.flags = frappe._dict()
			with patch.object(frappe, "db", db), patch.object(frappe, "connect_replica") as replica:
				setup_read_only_mode()
			self.assertTrue(frappe.flags.read_only)
			db.begin.assert_called_once_with(read_only=True)
			replica.assert_not_called()
		finally:
			frappe.local.conf = old_conf
			frappe.local.flags = old_flags

	def test_request_takes_guard_before_multipart_parsing(self):
		from werkzeug.test import EnvironBuilder

		import frappe
		from frappe.app import Request, init_request

		events = []
		request = Request(EnvironBuilder(method="OPTIONS", path="/", headers={"X-Frappe-Site-Name": "site"}).get_environ())
		old_conf = getattr(frappe.local, "conf", None)
		try:
			frappe.local.conf = frappe._dict(db_name="test")
			with (
				patch.object(frappe, "init"), patch.object(frappe, "connect"),
				patch.object(frappe, "get_hooks", return_value=[]),
				patch.object(cutover_lock, "register_writer", side_effect=lambda: events.append("guard")),
				patch("frappe.app.make_form_dict", side_effect=lambda request: events.append("parse")),
			):
				init_request(request)
			self.assertEqual(events, ["guard", "parse"])
		finally:
			frappe.local.conf = old_conf
			for name in ("request", "is_ajax"):
				if hasattr(frappe.local, name):
					delattr(frappe.local, name)

	def test_shared_writers_exclude_cutover_across_processes(self):
		code = """
import sys
from pathlib import Path
from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock as lock
lock.LOCK_DIRECTORY = Path(sys.argv[1])
try:
    with lock.cutover_lock('site', '/bench', exclusive=True):
        pass
except lock.CutoverBusyError:
    sys.exit(17)
"""
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			with cutover_lock.cutover_lock("site", "/bench", exclusive=False):
				result = subprocess.run([sys.executable, "-c", code, directory], check=False)
				self.assertEqual(result.returncode, 17)
				with cutover_lock.cutover_lock("site", "/bench", exclusive=False):
					pass
			result = subprocess.run([sys.executable, "-c", code, directory], check=False)
			self.assertEqual(result.returncode, 0)

	def test_other_sites_are_independent(self):
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			with cutover_lock.cutover_lock("site-a", "/bench", exclusive=True):
				with cutover_lock.cutover_lock("site-b", "/bench", exclusive=False):
					pass
				with self.assertRaises(cutover_lock.CutoverBusyError):
					with cutover_lock.cutover_lock("site-a", "/bench", exclusive=False):
						pass

	def test_process_exit_releases_lock(self):
		code = """
import sys
from pathlib import Path
from frappe.integrations.doctype.google_cloud_storage_settings import cutover_lock as lock
lock.LOCK_DIRECTORY = Path(sys.argv[1])
with lock.cutover_lock('site', '/bench', exclusive=True):
    print('held', flush=True)
    sys.stdin.read()
"""
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			process = subprocess.Popen([sys.executable, "-c", code, directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
			try:
				self.assertEqual(process.stdout.readline().strip(), "held")
				with self.assertRaises(cutover_lock.CutoverBusyError):
					with cutover_lock.cutover_lock("site", "/bench", exclusive=False):
						pass
			finally:
				process.terminate()
				process.communicate(timeout=5)
			with cutover_lock.cutover_lock("site", "/bench", exclusive=False):
				pass

	def test_symlink_lock_is_rejected(self):
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			with cutover_lock.cutover_lock("site", "/bench", exclusive=False):
				path = next(Path(directory).iterdir())
			path.unlink()
			path.symlink_to(Path(directory) / "victim")
			with self.assertRaises(OSError):
				with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
					pass

	def test_jobs_are_guarded_except_exclusive_coordinators(self):
		with patch.object(cutover_lock, "register_writer") as register:
			cutover_lock.register_job("other.job")
			register.assert_called_once_with(retry_if_busy=True)
			register.reset_mock()
			base = "frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings."
			cutover_lock.register_job(base + "run_preflight")
			cutover_lock.register_job(base + "run_reconcile")
			cutover_lock.register_job(base + "run_cutover")
			cutover_lock.register_job(base + "run_cleanup")
			register.assert_not_called()

	def test_background_job_takes_guard_before_before_job_hooks(self):
		import frappe
		from frappe.utils.background_jobs import execute_job

		events = []
		db = SimpleNamespace(commit=lambda **kwargs: events.append("commit"))
		frappe.local.site = "site"
		try:
			with (
				patch.object(frappe, "db", db),
				patch.object(frappe, "get_hooks", side_effect=lambda name: ["hook"] if name == "before_job" else []),
				patch.object(frappe, "call", side_effect=lambda *args, **kwargs: events.append("hook")),
				patch.object(cutover_lock, "register_job", side_effect=lambda name: events.append("guard")),
			):
				execute_job("site", lambda: events.append("job"), None, "job", {}, is_async=False)
			self.assertEqual(events, ["guard", "hook", "job", "commit"])
		finally:
			for name in ("job", "site"):
				if hasattr(frappe.local, name):
					delattr(frappe.local, name)

	def test_writer_lock_lasts_until_transaction_completion(self):
		import frappe
		from frappe.utils import CallbackManager

		db = SimpleNamespace(after_commit=CallbackManager(), after_rollback=CallbackManager())
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site"
			try:
				with patch.object(frappe, "db", db), patch("frappe.utils.get_bench_path", return_value="/bench"):
					cutover_lock.register_writer()
					cutover_lock.register_writer()
					self.assertEqual(len(db.after_commit), 1)
					with self.assertRaises(cutover_lock.CutoverBusyError):
						with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
							pass
					db.after_commit.run()
					db.after_rollback.run()
					with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
						pass
			finally:
				for name in ("gcs_writer_lock", "conf", "site"):
					if hasattr(frappe.local, name):
						delattr(frappe.local, name)

	def test_discarded_savepoint_callbacks_do_not_leak_writer_lock_on_commit(self):
		import frappe
		from frappe.utils import CallbackManager

		db = SimpleNamespace(after_commit=CallbackManager(), after_rollback=CallbackManager())
		db.after_rollback.add(lambda: None)
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site"
			try:
				with patch.object(frappe, "db", db), patch("frappe.utils.get_bench_path", return_value="/bench"):
					cutover_lock.register_writer()
					db.after_commit.truncate(0)
					for callback in db.after_rollback.cut(1):
						callback()
					db.after_rollback.reset()
					db.after_commit.run()
					with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
						pass
			finally:
				if lock := getattr(frappe.local, "gcs_writer_lock", None):
					lock.__exit__(None, None, None)
				for name in ("gcs_writer_lock", "conf", "site"):
					if hasattr(frappe.local, name):
						delattr(frappe.local, name)

	def test_background_writer_retries_when_cutover_is_active(self):
		import frappe

		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site"
			try:
				with patch("frappe.utils.get_bench_path", return_value="/bench"):
					with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
						with self.assertRaises(frappe.RetryBackgroundJobError):
							cutover_lock.register_writer(retry_if_busy=True)
			finally:
				for name in ("conf", "site"):
					if hasattr(frappe.local, name):
						delattr(frappe.local, name)

	def test_writer_lock_covers_rollback_callbacks(self):
		import frappe
		from frappe.utils import CallbackManager

		db = SimpleNamespace(after_commit=CallbackManager(), after_rollback=CallbackManager())
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site"
			try:
				with patch.object(frappe, "db", db), patch("frappe.utils.get_bench_path", return_value="/bench"):
					cutover_lock.register_writer()

					def rollback_file():
						with self.assertRaises(cutover_lock.CutoverBusyError):
							with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
								pass

					db.after_rollback.add(rollback_file)
					db.after_rollback.run()
					with cutover_lock.cutover_lock("site", "/bench", exclusive=True):
						pass
			finally:
				for name in ("gcs_writer_lock", "conf", "site"):
					if hasattr(frappe.local, name):
						delattr(frappe.local, name)

	def test_object_lock_remains_held_until_deletion_callback_finishes(self):
		import frappe
		from frappe.utils import CallbackManager

		db = SimpleNamespace(after_commit=CallbackManager(), after_rollback=CallbackManager())
		with tempfile.TemporaryDirectory() as directory, patch.object(cutover_lock, "LOCK_DIRECTORY", Path(directory)):
			frappe.local.conf = frappe._dict(gcs_coordinated_cutover=1)
			frappe.local.site = "site"
			object_identity = "site\0object\0/private/files/shared.pdf"
			try:
				with patch.object(frappe, "db", db), patch("frappe.utils.get_bench_path", return_value="/bench"):
					cutover_lock.register_object("/private/files/shared.pdf")
					cutover_lock.register_object("/private/files/shared.pdf")

					def deletion_callback():
						with self.assertRaises(cutover_lock.CutoverBusyError):
							with cutover_lock.cutover_lock(object_identity, "/bench", exclusive=True):
								pass

					db.after_commit.add(deletion_callback)
					db.after_commit.run()
					with cutover_lock.cutover_lock(object_identity, "/bench", exclusive=True):
						pass
			finally:
				for name in ("gcs_object_locks", "gcs_writer_lock", "conf", "site"):
					if hasattr(frappe.local, name):
						delattr(frappe.local, name)
