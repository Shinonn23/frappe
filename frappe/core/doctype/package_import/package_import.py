# Copyright (c) 2021, Frappe Technologies and contributors
# For license information, please see license.txt

import io
import json
import os
import re
import tarfile
import tempfile
from contextlib import nullcontext

import frappe
from frappe.core.doctype.file.utils import check_path_safety
from frappe.desk.form.load import get_attachments
from frappe.model.document import Document
from frappe.model.sync import get_doc_files
from frappe.modules.import_file import import_doc, import_file_by_path
from frappe.utils import get_files_path


class PackageImport(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		activate: DF.Check
		attach_package: DF.Attach | None
		force: DF.Check
		log: DF.Code | None
	# end: auto-generated types

	def validate(self):
		if self.activate:
			self.import_package()

	def import_package(self):
		attachment = get_attachments(self.doctype, self.name)

		if not attachment:
			frappe.throw(frappe._("Please attach the package"))

		attachment = attachment[0]

		# get package_name from file (package_name-0.0.0.tar.gz)
		raw_name = attachment.file_name.split(".", 1)[0].rsplit("-", 1)[0]
		package_name = re.sub(r"[^a-zA-Z0-9_-]", "", raw_name)
		if not package_name:
			frappe.throw(frappe._("Invalid Package Name"))

		from frappe.integrations.doctype.google_cloud_storage_settings import storage as gcs_storage

		if gcs_storage.enabled():
			archive = {"fileobj": io.BytesIO(gcs_storage.read(attachment.file_url))}
			workspace = tempfile.TemporaryDirectory(prefix="frappe-package-", dir=gcs_storage.memory_directory())
		else:
			archive = {"name": get_files_path(attachment.file_name, is_private=attachment.is_private)}
			extract_path = frappe.get_site_path("packages")
			os.makedirs(extract_path, exist_ok=True)
			workspace = nullcontext(extract_path)
		with workspace as extract_path:
			self.import_archive(archive, extract_path, package_name)

	def import_archive(self, archive, extract_path, package_name):
		from frappe.core.api.file import get_max_extract_size

		with tarfile.open(mode="r:gz", **archive) as tar:
			members = tar.getmembers()
			if sum(member.size for member in members if member.isfile()) > get_max_extract_size():
				frappe.throw(frappe._("Package exceeds the maximum allowed extracted size"))
			for member in members:
				if member.issym() or member.islnk():
					frappe.throw(frappe._(f"Package contains disallowed link entry: {member.name}"))

				member_path = os.path.join(extract_path, member.name)
				is_safe = check_path_safety(extract_path, member_path)
				if not is_safe:
					frappe.throw(frappe._("Package contains disallowed path entry"))

			tar.extractall(path=extract_path, filter="data")

		package_path = os.path.join(extract_path, package_name)

		# import Package
		with open(os.path.join(package_path, package_name + ".json")) as packagefile:
			doc_dict = json.loads(packagefile.read())

		frappe.flags.package = import_doc(doc_dict)

		# collect modules
		files = []
		log = []
		for module in os.listdir(package_path):
			module_path = os.path.join(package_path, module)
			if os.path.isdir(module_path):
				files = get_doc_files(files, module_path)

		# import files
		for file in files:
			import_file_by_path(file, force=self.force, ignore_version=True)
			log.append(f"Imported {file}")

		self.log = "\n".join(log)
