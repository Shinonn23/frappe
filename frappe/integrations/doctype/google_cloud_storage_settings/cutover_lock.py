# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Process-shared reader/writer barrier for a single bench host."""

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path

LOCK_DIRECTORY = Path("/dev/shm")


class CutoverBusyError(RuntimeError):
	pass


@contextmanager
def cutover_lock(site, bench_path, *, exclusive):
	"""Hold a shared writer or exclusive cutover lock; fail instead of waiting."""
	import fcntl

	if not site:
		raise ValueError("A site name is required")
	identity = f"{os.path.realpath(bench_path)}\0{site}".encode()
	name = "frappe-gcs-" + hashlib.sha256(identity).hexdigest() + ".lock"
	path = LOCK_DIRECTORY / name
	fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
	try:
		info = os.fstat(fd)
		if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
			raise RuntimeError("Unsafe cutover lock file")
		try:
			fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
		except BlockingIOError as exc:
			raise CutoverBusyError("The site is writing files or a cutover is in progress") from exc
		try:
			yield
		finally:
			fcntl.flock(fd, fcntl.LOCK_UN)
	finally:
		os.close(fd)


def register_writer(*, retry_if_busy=False):
	"""Keep a shared lock until the current database transaction completes."""
	import frappe
	from frappe.utils import cint, get_bench_path

	conf = getattr(frappe.local, "conf", None)
	if not conf or not cint(conf.get("gcs_coordinated_cutover")):
		return
	if getattr(frappe.local, "gcs_writer_lock", None):
		return
	lock = cutover_lock(frappe.local.site, get_bench_path(), exclusive=False)
	try:
		lock.__enter__()
	except CutoverBusyError:
		if retry_if_busy:
			raise frappe.RetryBackgroundJobError("File storage migration is in progress")
		frappe.throw(frappe._("File storage migration is in progress. Please retry shortly."))
	frappe.local.gcs_writer_lock = lock

	def release_after_callbacks(manager):
		if getattr(frappe.local, "gcs_writer_lock", None) is not lock:
			return
		if manager._functions:
			manager.add(lambda: release_after_callbacks(manager))
			# Savepoint cleanup can invoke this callback after discarding the other queue.
			other = frappe.db.after_rollback if manager is frappe.db.after_commit else frappe.db.after_commit
			other.add(lambda: release_after_callbacks(other))
			return
		for object_lock in reversed(list(getattr(frappe.local, "gcs_object_locks", {}).values())):
			object_lock.__exit__(None, None, None)
		frappe.local.gcs_object_locks = {}
		frappe.local.gcs_writer_lock = None
		lock.__exit__(None, None, None)

	frappe.db.after_commit.add(lambda: release_after_callbacks(frappe.db.after_commit))
	frappe.db.after_rollback.add(lambda: release_after_callbacks(frappe.db.after_rollback))


def register_object(file_url):
	"""Serialize references and deletion of a shared object through transaction callbacks."""
	import frappe
	from frappe.utils import get_bench_path

	if not file_url or not file_url.startswith(("/files/", "/private/files/")):
		return
	register_writer()
	if not getattr(frappe.local, "gcs_writer_lock", None):
		frappe.throw(frappe._("The coordinated storage guard must remain enabled."))
	locks = getattr(frappe.local, "gcs_object_locks", None)
	if locks is None:
		locks = frappe.local.gcs_object_locks = {}
	if file_url in locks:
		return
	lock = cutover_lock(f"{frappe.local.site}\0object\0{file_url}", get_bench_path(), exclusive=True)
	try:
		lock.__enter__()
	except CutoverBusyError:
		if getattr(frappe.local, "job", None):
			raise frappe.RetryBackgroundJobError("This file is being changed by another transaction")
		frappe.throw(frappe._("This file is being changed by another transaction. Please retry."))
	locks[file_url] = lock


def register_job(method_name):
	"""Coordinate jobs except the jobs that acquire the exclusive lock themselves."""
	module = "frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings."
	if method_name not in {
		module + "run_preflight", module + "run_reconcile", module + "run_cutover", module + "run_cleanup"
	}:
		register_writer(retry_if_busy=True)
