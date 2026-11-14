# Copyright (c) 2021, Frappe Technologies and contributors
# For license information, please see license.txt

import os
import subprocess
from pathlib import Path

import frappe
from frappe.model.document import Document
from frappe.modules.export_file import export_doc
from frappe.query_builder.functions import Max


class PackageRelease(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		major: DF.Int
		minor: DF.Int
		package: DF.Link
		patch: DF.Int
		path: DF.SmallText | None
		publish: DF.Check
		release_notes: DF.MarkdownEditor | None
	# end: auto-generated types

	def set_version(self):
		# set the next patch release by default
		doctype = frappe.qb.DocType("Package Release")
		if not self.major:
			self.major = (
				frappe.qb.from_(doctype)
				.where(doctype.package == self.package)
				.select(Max(doctype.minor))
				.run()[0][0]
				or 0
			)

		if not self.minor:
			self.minor = (
				frappe.qb.from_(doctype)
				.where(doctype.package == self.package)
				.select(Max("minor"))
				.run()[0][0]
				or 0
			)
		if not self.patch:
			value = (
				frappe.qb.from_(doctype)
				.where(doctype.package == self.package)
				.select(Max("patch"))
				.run()[0][0]
				or 0
			)
			self.patch = value + 1

	def autoname(self):
		self.set_version()
		self.name = "{}-{}.{}.{}".format(
			frappe.db.get_value("Package", self.package, "package_name"), self.major, self.minor, self.patch
		)

	def validate(self):
		package = frappe.get_doc("Package", self.package)
		package_path = Path(frappe.get_site_path("packages", package.package_name))
		if not package_path.resolve().is_relative_to(Path(frappe.get_site_path()).resolve()):
			frappe.throw("Invalid package path: " + package_path.as_posix())

		if self.publish:
			self.export_files(package)

	def export_files(self, package):
		"""Export all the documents in this package to site/packages folder"""
		from frappe.integrations.doctype.google_cloud_storage_settings import storage as gcs_storage

		if gcs_storage.enabled():
			self.export_to_bucket(package)
			return

		self.export_modules()
		self.export_package_files(package)
		self.make_tarfile(package)

	def export_to_bucket(self, package):
		import io
		import tarfile
		from pathlib import PurePosixPath

		from frappe.modules import scrub, scrub_dt_dn
		from frappe.modules.export_file import get_module_name, strip_default_fields

		def safe_part(value):
			if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
				raise ValueError("Invalid package archive path")
			return value

		root = safe_part(package.package_name)
		entries = {f"{root}/{root}.json": frappe.as_json(package.as_dict(no_nulls=True))}
		if package.readme:
			entries[f"{root}/README.md"] = package.readme
		if package.license:
			entries[f"{root}/LICENSE.md"] = package.license
		for module_ref in frappe.get_all("Module Def", {"package": self.package}):
			module = frappe.get_doc("Module Def", module_ref.name)
			for link in module.meta.links:
				if link.link_doctype == "Module Def":
					continue
				for reference in frappe.get_all(link.link_doctype, {"module": module_ref.name}):
					doc = frappe.get_doc(link.link_doctype, reference.name)
					module_name = get_module_name(doc)
					if frappe.db.get_value("Module Def", module_name, "package") != self.package:
						continue
					dt, dn = scrub_dt_dn(doc.doctype, doc.name)
					folder = PurePosixPath(root, safe_part(scrub(module_name)), safe_part(dt), safe_part(dn))
					data = doc.as_dict(no_nulls=True, ignore_computed_child_tables=True)
					doc.run_method("before_export", data)
					data = strip_default_fields(doc, data)
					if hasattr(doc, "get_code_fields"):
						for key, extension in doc.get_code_fields().items():
							if doc.get(key):
								entries[str(folder / f"{dn}.{safe_part(extension)}")] = doc.get(key)
								del data[key]
					entries[str(folder / f"{dn}.json")] = frappe.as_json(data) + "\n"
					for parent in (folder, folder.parent, folder.parent.parent):
						entries.setdefault(str(parent / "__init__.py"), "")
		content = io.BytesIO()
		with tarfile.open(fileobj=content, mode="w:gz") as archive:
			for name, value in entries.items():
				data = value.encode("utf-8")
				info = tarfile.TarInfo(name)
				info.size, info.mode = len(data), 0o644
				archive.addfile(info, io.BytesIO(data))
		file = frappe.get_doc({
			"doctype": "File", "file_name": f"{self.name}.tar.gz", "content": content.getvalue(),
			"attached_to_doctype": self.doctype, "attached_to_name": self.name,
		})
		file.flags.ignore_duplicate_entry_error = True
		file.insert()
		self.path = file.file_url

	def export_modules(self):
		for m in frappe.get_all("Module Def", dict(package=self.package)):
			module = frappe.get_doc("Module Def", m.name)
			for l in module.meta.links:
				if l.link_doctype == "Module Def":
					continue
				# all documents of the type in the module
				for d in frappe.get_all(l.link_doctype, dict(module=m.name)):
					export_doc(frappe.get_doc(l.link_doctype, d.name))

	def export_package_files(self, package):
		# write readme
		if package.readme:
			with open(frappe.get_site_path("packages", package.package_name, "README.md"), "w") as readme:
				readme.write(package.readme)

		# write license
		if package.license:
			with open(frappe.get_site_path("packages", package.package_name, "LICENSE.md"), "w") as license:
				license.write(package.license)

		# write package.json as `frappe_package.json`
		with open(
			frappe.get_site_path("packages", package.package_name, package.package_name + ".json"), "w"
		) as packagefile:
			packagefile.write(frappe.as_json(package.as_dict(no_nulls=True)))

	def make_tarfile(self, package):
		# make tarfile
		filename = f"{self.name}.tar.gz"
		from frappe.integrations.doctype.google_cloud_storage_settings import storage as gcs_storage

		if gcs_storage.enabled():
			import io
			import tarfile

			content = io.BytesIO()
			with tarfile.open(fileobj=content, mode="w:gz") as archive:
				archive.add(frappe.get_site_path("packages", package.package_name), arcname=package.package_name)
			file = frappe.get_doc({
				"doctype": "File", "file_name": filename, "content": content.getvalue(),
				"attached_to_doctype": self.doctype, "attached_to_name": self.name,
			})
			file.flags.ignore_duplicate_entry_error = True
			file.insert()
			self.path = file.file_url
			return
		subprocess.check_output(
			["tar", "czf", filename, package.package_name], cwd=frappe.get_site_path("packages")
		)

		# move file
		subprocess.check_output(
			["mv", frappe.get_site_path("packages", filename), frappe.get_site_path("public", "files")]
		)

		# make attachment
		file = frappe.get_doc(
			doctype="File",
			file_url="/" + os.path.join("files", filename),
			attached_to_doctype=self.doctype,
			attached_to_name=self.name,
		)

		# Set path to tarball
		self.path = file.file_url

		file.flags.ignore_duplicate_entry_error = True
		file.insert()
