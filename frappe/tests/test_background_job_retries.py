import unittest
from unittest.mock import Mock, call, patch

from werkzeug.local import Local, LocalProxy, release_local

import frappe
from frappe.utils import background_jobs


class InternalError(Exception):
	pass


class TestBackgroundJobRetries(unittest.TestCase):
	def setUp(self):
		self.callbacks = Mock()
		self.local = Local()
		self.local.site = "unit-test"
		self.frappe = Mock(
			local=self.local,
			job=LocalProxy(lambda: self.local.job),
			_dict=frappe._dict,
			RetryBackgroundJobError=frappe.RetryBackgroundJobError,
			QueryDeadlockError=frappe.QueryDeadlockError,
			QueryTimeoutError=frappe.QueryTimeoutError,
		)
		self.frappe.init.side_effect = lambda site, **kwargs: setattr(self.local, "site", site)
		self.frappe.destroy.side_effect = lambda: release_local(self.local)
		self.frappe.db.InternalError = InternalError
		self.frappe.db.is_deadlocked.side_effect = lambda error: error.args[0] in (1020, 1213)
		self.frappe.db.is_timedout.side_effect = lambda error: error.args[0] == 1205
		self.frappe.get_hooks.side_effect = lambda name: [name]
		self.job = Mock()
		self.enterContext(patch.object(background_jobs, "frappe", self.frappe))
		self.enterContext(patch.object(background_jobs, "CallbackManager", return_value=self.callbacks))
		self.sleep = self.enterContext(patch.object(background_jobs.time, "sleep"))
		self.enterContext(patch("builtins.print"))

	def run_job(self, is_async=True):
		def method():
			return self.job()

		return background_jobs.execute_job(
			"unit-test", method, None, "test-job", {}, user="job-user", is_async=is_async
		)

	def test_wrapped_database_errors_retry_then_commit(self):
		for error_type in (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			with self.subTest(error=error_type.__name__):
				self.job.reset_mock(side_effect=True)
				self.frappe.reset_mock()
				self.callbacks.reset_mock()
				self.sleep.reset_mock()
				self.job.side_effect = [error_type(InternalError(1020)), "done"]
				self.assertEqual(self.run_job(), "done")
				self.assertEqual(self.job.call_count, 2)
				self.frappe.db.rollback.assert_called_once_with(chain=True)
				self.frappe.db.commit.assert_called_once_with(chain=True)
				self.callbacks.reset.assert_called_once_with()
				self.sleep.assert_called_once_with(1)
				self.assertEqual(self.frappe.set_user.call_args_list, [call("job-user"), call("job-user")])
				self.frappe.log_error.assert_not_called()
				self.callbacks.run.assert_called_once_with()
				self.assertEqual(
					[args[0][0] for args in self.frappe.call.call_args_list],
					["before_job", "before_job", "after_job"],
				)
				self.assertFalse(hasattr(self.local, "site"))
				self.assertFalse(hasattr(self.local, "job"))

	def test_retry_cap_preserves_failure(self):
		self.job.side_effect = frappe.QueryDeadlockError(InternalError(1213))
		with self.assertRaises(frappe.QueryDeadlockError):
			self.run_job()
		self.assertEqual(self.job.call_count, 6)
		self.assertEqual(self.frappe.db.rollback.call_count, 6)
		self.assertEqual(self.callbacks.reset.call_count, 5)
		self.assertEqual(self.sleep.call_args_list, [call(delay) for delay in range(1, 6)])
		self.frappe.db.commit.assert_not_called()
		self.frappe.log_error.assert_called_once()
		self.callbacks.run.assert_called_once_with()
		self.assertFalse(hasattr(self.local, "site"))

	def test_synchronous_retry_retains_callers_context(self):
		self.job.side_effect = [frappe.QueryDeadlockError(InternalError(1213)), "done"]
		self.assertEqual(self.run_job(is_async=False), "done")
		self.assertEqual(self.local.site, "unit-test")
		self.assertEqual(self.local.job.user, "job-user")
		self.frappe.destroy.assert_not_called()
		self.callbacks.run.assert_called_once_with()

	def test_raw_database_error_still_retries(self):
		self.job.side_effect = [InternalError(1213), "done"]
		self.assertEqual(self.run_job(), "done")
		self.assertEqual(self.job.call_count, 2)
		self.sleep.assert_called_once_with(1)

	def test_other_errors_are_not_retried(self):
		for error in (InternalError(1064), ValueError("invalid job")):
			with self.subTest(error=type(error).__name__):
				self.job.reset_mock(side_effect=True)
				self.job.side_effect = error
				with self.assertRaises(type(error)):
					self.run_job()
				self.assertEqual(self.job.call_count, 1)
		self.sleep.assert_not_called()
		self.callbacks.reset.assert_not_called()
