"""Read-only checks for attachment metadata and object content."""

import hashlib
import io

from google.api_core.exceptions import NotFound, PreconditionFailed


class HashWriter(io.RawIOBase):
	def __init__(self):
		self.digest = hashlib.md5(usedforsecurity=False)

	def writable(self):
		return True

	def write(self, data):
		self.digest.update(data)
		return len(data)


def check_object(blob, expected_hash):
	"""Return a mismatch status while pinning the generation being read."""
	try:
		blob.reload()
		writer = HashWriter()
		blob.download_to_file(writer, if_generation_match=blob.generation)
	except NotFound:
		return "missing"
	except PreconditionFailed:
		return "changed"
	return "ok" if writer.digest.hexdigest() == expected_hash else "mismatch"
