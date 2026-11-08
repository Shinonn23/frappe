import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import frappe
from frappe.utils import background_jobs


class InternalError(Exception):
	pass


class TestBackgroundJobRetries(unittest.TestCase):
	def setUp(self):
		self.callbacks = Mock()
		self.frappe = Mock(
			local=SimpleNamespace(site="unit-test"),
			job=SimpleNamespace(after_job=self.callbacks),
			_dict=frappe._dict,
			RetryBackgroundJobError=frappe.RetryBackgroundJobError,
			QueryDeadlockError=frappe.QueryDeadlockError,
			QueryTimeoutError=frappe.QueryTimeoutError,
		)
		self.frappe.db.InternalError = InternalError
		self.frappe.db.is_deadlocked.side_effect = lambda error: error.args[0] in (1020, 1213)
		self.frappe.db.is_timedout.side_effect = lambda error: error.args[0] == 1205
		self.frappe.get_hooks.return_value = []
		self.job = Mock()
		self.enterContext(patch.object(background_jobs, "frappe", self.frappe))
		self.enterContext(patch.object(background_jobs, "CallbackManager", return_value=self.callbacks))
		self.sleep = self.enterContext(patch.object(background_jobs.time, "sleep"))
		self.enterContext(patch("builtins.print"))

	def run_job(self):
		def method():
			return self.job()

		return background_jobs.execute_job("unit-test", method, None, "test-job", {}, user="job-user")

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
