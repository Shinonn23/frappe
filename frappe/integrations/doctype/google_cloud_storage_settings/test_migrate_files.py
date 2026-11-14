import tempfile
import unittest
from datetime import UTC, datetime, timezone
from pathlib import Path

from frappe.integrations.doctype.google_cloud_storage_settings.migrate_files import (
	assert_no_incomplete_uploads,
	cleanup_files,
	cleanup_plan,
	copy_files,
	load_cleanup_plan,
	reconcile_stale_objects,
	save_cleanup_plan,
	verify_file_references,
	verify_snapshot,
)


class Blob:
	def __init__(self, bucket, key):
		self.bucket, self.key = bucket, key
		self.name = key
		self.metadata = None
		self.generation = None
		self.updated = None

	def exists(self):
		return self.key in self.bucket.objects

	def reload(self):
		from google.api_core.exceptions import NotFound

		if self.key not in self.bucket.objects:
			raise NotFound("missing object")
		self.metadata = self.bucket.metadata.get(self.key, {}).copy()
		self.generation = self.bucket.generations[self.key]
		self.updated = self.bucket.updated[self.key]

	def upload_from_file(self, source, if_generation_match):
		assert if_generation_match == self.bucket.generations.get(self.key, 0)
		self.bucket.objects[self.key] = source.read()
		self.bucket.metadata[self.key] = (self.metadata or {}).copy()
		self.bucket.generations[self.key] = if_generation_match + 1
		self.bucket.updated[self.key] = datetime.now(UTC)

	def upload_from_string(self, content, if_generation_match, content_type=None):
		import io

		from google.api_core.exceptions import PreconditionFailed

		if if_generation_match != self.bucket.generations.get(self.key, 0):
			raise PreconditionFailed("generation mismatch")
		self.upload_from_file(io.BytesIO(content), if_generation_match)

	def download_as_bytes(self, if_generation_match):
		assert if_generation_match == self.bucket.generations[self.key]
		return self.bucket.objects[self.key]

	def download_to_file(self, target, if_generation_match):
		assert if_generation_match == self.bucket.generations[self.key]
		target.write(self.bucket.objects[self.key])

	def delete(self, if_generation_match):
		from google.api_core.exceptions import NotFound, PreconditionFailed

		if self.key not in self.bucket.objects:
			raise NotFound("missing object")
		if if_generation_match != self.bucket.generations[self.key]:
			raise PreconditionFailed("generation mismatch")
		del self.bucket.objects[self.key]
		del self.bucket.metadata[self.key]
		del self.bucket.generations[self.key]
		del self.bucket.updated[self.key]


class Bucket:
	def __init__(self):
		self.objects = {}
		self.metadata = {}
		self.generations = {}
		self.updated = {}

	def blob(self, key):
		return Blob(self, key)

	def list_blobs(self, prefix):
		blobs = [self.blob(key) for key in self.objects if key.startswith(prefix)]
		for blob in blobs:
			blob.reload()
		return blobs


class TestMigration(unittest.TestCase):
	def test_preflight_rejects_private_thumbnail_on_public_file(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/a.png").write_bytes(b"image")
			(root / "private/files/a_small.png").write_bytes(b"thumbnail")
			record = {"name": "file-1", "file_url": "/files/a.png", "thumbnail_url": "/private/files/a_small.png", "is_private": 0}
			with self.assertRaisesRegex(ValueError, "privacy does not match its thumbnail_url"):
				verify_file_references(root, [record])

	def test_copy_is_verified_and_does_not_overwrite(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for folder in ("public/files", "private/files"):
				(root / folder).mkdir(parents=True)
			(root / "public/files/a.txt").write_bytes(b"public")
			(root / "private/files/a.txt").write_bytes(b"private")
			bucket = Bucket()
			self.assertEqual(copy_files(bucket, root, "site", owner="site.example"), 2)
			self.assertEqual(copy_files(bucket, root, "site", owner="site.example"), 2)
			bucket.objects["site/private/a.txt"] = b"different"
			bucket.metadata["site/private/a.txt"] = {"frappe_migration_site": "other.example"}
			with self.assertRaisesRegex(RuntimeError, "not owned"):
				copy_files(bucket, root, "site", owner="site.example")
			self.assertEqual((root / "private/files/a.txt").read_bytes(), b"private")

	def test_rejects_symlink(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/link").symlink_to(root / "private/files")
			with self.assertRaises(ValueError):
				copy_files(Bucket(), root, "site", owner="site.example")

	def test_rejects_source_names_that_cannot_be_read_as_bucket_urls(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/a\\b.txt").write_bytes(b"file")
			with self.assertRaises(ValueError):
				copy_files(Bucket(), root, "site", owner="site.example")

	def test_snapshot_detects_changes_missing_and_extra_objects(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			file = root / "public/files/a.txt"
			file.write_bytes(b"original")
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			self.assertEqual(verify_snapshot(bucket, root, "site", owner="site.example"), 1)
			file.write_bytes(b"changed")
			with self.assertRaisesRegex(RuntimeError, "differs"):
				verify_snapshot(bucket, root, "site", owner="site.example")
			file.unlink()
			with self.assertRaisesRegex(RuntimeError, "unexpected"):
				verify_snapshot(bucket, root, "site", owner="site.example")
			del bucket.objects["site/public/a.txt"]
			del bucket.generations["site/public/a.txt"]
			del bucket.metadata["site/public/a.txt"]
			del bucket.updated["site/public/a.txt"]
			file.write_bytes(b"new")
			with self.assertRaisesRegex(RuntimeError, "missing"):
				verify_snapshot(bucket, root, "site", owner="site.example")

	def test_copy_updates_only_its_own_migration_objects_and_stops_when_enabled(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			path = root / "public/files/a.txt"
			path.write_bytes(b"old")
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			path.write_bytes(b"new")
			copy_files(bucket, root, "site", owner="site.example")
			self.assertEqual(bucket.objects["site/public/a.txt"], b"new")
			self.assertEqual(bucket.generations["site/public/a.txt"], 2)
			with self.assertRaisesRegex(RuntimeError, "stopped"):
				copy_files(bucket, root, "site", owner="site.example", may_continue=lambda: False)

	def test_copy_does_not_replace_an_unmarked_existing_object(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/a.txt").write_bytes(b"site file")
			bucket = Bucket()
			bucket.objects["site/public/a.txt"] = b"existing object"
			bucket.generations["site/public/a.txt"] = 5
			bucket.updated["site/public/a.txt"] = datetime.now(UTC)
			with self.assertRaisesRegex(RuntimeError, "not owned"):
				copy_files(bucket, root, "site", owner="site.example")
			self.assertEqual(bucket.objects["site/public/a.txt"], b"existing object")
			self.assertEqual(bucket.generations["site/public/a.txt"], 5)

	def test_prefix_cannot_be_shared_between_sites(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="first.example")
			with self.assertRaisesRegex(RuntimeError, "different site"):
				copy_files(bucket, root, "site", owner="second.example")

	def test_incomplete_upload_prevents_cutover(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "private/files/.temp-photo.jpg").write_bytes(b"partial")
			with self.assertRaisesRegex(RuntimeError, "incomplete upload"):
				assert_no_incomplete_uploads(root, "site")

	def test_file_references_reject_missing_and_privacy_mismatch(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "private/files/a.pdf").write_bytes(b"private")
			record = {"name": "file-1", "file_url": "/private/files/a.pdf", "is_private": 1}
			self.assertEqual(verify_file_references(root, [record]), 1)
			with self.assertRaisesRegex(ValueError, "privacy"):
				verify_file_references(root, [dict(record, is_private=0)])
			with self.assertRaisesRegex(ValueError, "public thumbnail"):
				verify_file_references(root, [dict(record, thumbnail_url="/files/a_small.jpg")])
			(root / "private/files/a.pdf").unlink()
			with self.assertRaisesRegex(ValueError, "does not exist"):
				verify_file_references(root, [record])

	def test_reconcile_stale_only_deletes_owned_migration_objects(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			path = root / "public/files/deleted.txt"
			path.write_bytes(b"old")
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			path.unlink()
			unrelated = bucket.blob("site/public/unrelated.txt")
			unrelated.upload_from_string(b"important", if_generation_match=0)
			with self.assertRaisesRegex(RuntimeError, "not owned"):
				reconcile_stale_objects(bucket, root, "site", owner="site.example", records=[])
			self.assertIn("site/public/deleted.txt", bucket.objects)
			unrelated.reload()
			unrelated.delete(if_generation_match=unrelated.generation)
			missing_record = {"name": "file-1", "file_url": "/files/deleted.txt", "is_private": 0}
			with self.assertRaisesRegex(ValueError, "does not exist"):
				reconcile_stale_objects(bucket, root, "site", owner="site.example", records=[missing_record])
			self.assertIn("site/public/deleted.txt", bucket.objects)
			self.assertEqual(reconcile_stale_objects(bucket, root, "site", owner="site.example", records=[]), 1)
			self.assertNotIn("site/public/deleted.txt", bucket.objects)

	def test_cleanup_is_resumable_and_preserves_changed_files(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			first = root / "public/files/a.txt"
			second = root / "private/files/b.txt"
			first.write_bytes(b"public")
			second.write_bytes(b"private")
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			plan = cleanup_plan(bucket, root, "site", owner="site.example")
			second.write_bytes(b"changed")
			with self.assertRaises(RuntimeError):
				cleanup_files(bucket, root, "site", plan, owner="site.example")
			self.assertFalse(first.exists())
			self.assertEqual(second.read_bytes(), b"changed")
			second.write_bytes(b"private")
			self.assertEqual(cleanup_files(bucket, root, "site", plan, owner="site.example"), 1)
			self.assertEqual(cleanup_files(bucket, root, "site", plan, owner="site.example"), 0)

	def test_cleanup_rejects_path_escape(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			entry = {"path": "public/files/../../secret", "key": "site/public/../../secret"}
			with self.assertRaises(ValueError):
				cleanup_files(bucket, root, "site", [entry], owner="site.example")

	def test_cleanup_plan_is_persisted_without_overwriting_an_existing_plan(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "public/files").mkdir(parents=True)
			(root / "private/files").mkdir(parents=True)
			(root / "public/files/a.txt").write_bytes(b"content")
			bucket = Bucket()
			copy_files(bucket, root, "site", owner="site.example")
			plan = cleanup_plan(bucket, root, "site", owner="site.example")
			save_cleanup_plan(bucket, "site", plan, owner="site.example")
			save_cleanup_plan(bucket, "site", plan, owner="site.example")
			self.assertEqual(load_cleanup_plan(bucket, "site", owner="site.example"), plan)
			with self.assertRaisesRegex(RuntimeError, "different cleanup plan"):
				save_cleanup_plan(bucket, "site", [], owner="site.example")
