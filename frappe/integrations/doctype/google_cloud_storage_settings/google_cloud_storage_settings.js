frappe.ui.form.on("Google Cloud Storage Settings", {
	refresh(frm) {
		const call = (method) => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save your settings first."));
				return;
			}
			frappe.call({
				method: `frappe.integrations.doctype.google_cloud_storage_settings.google_cloud_storage_settings.${method}`,
				freeze: true,
				callback: (r) => frappe.msgprint(r.message),
			});
		};
		frm.add_custom_button(__("Test Connection"), () => {
			call("test_connection");
		});
		frm.add_custom_button(__("Refresh Results"), () => frm.reload_doc());
		frm.add_custom_button(__("Copy Existing Files"), () => {
			frappe.confirm(
				__("Copy existing public and private files to the bucket? Original files will remain on the VM."),
				() => call("copy_existing_files")
			);
		});
		frm.add_custom_button(__("Run Preflight"), () => {
			call("start_preflight");
		});
		frm.add_custom_button(__("Reconcile Copied Files"), () => {
			frappe.confirm(
				__("Remove stale migration copies from the bucket? Local files will not be removed."),
				() => call("start_reconcile")
			);
		});
		if (!frm.doc.enabled) {
			frm.add_custom_button(__("Activate Storage"), () => {
				frappe.confirm(
					__("Verify and migrate files to the bucket, then remove verified local copies? All site writers must be isolated and the scheduler paused."),
					() => call("start_cutover")
				);
			});
		} else {
			frm.add_custom_button(__("Check File Integrity"), () => {
				frappe.confirm(
					__("Read all stored attachments and compare their contents with File metadata? This can take time and incur bucket read charges."),
					() => call("start_integrity_check")
				);
			});
			frm.add_custom_button(__("Resume Local Cleanup"), () => {
				frappe.confirm(
					__("Resume removal of verified local copies? All site writers must be isolated and the scheduler paused."),
					() => call("start_cleanup")
				);
			});
		}
	},
});

frappe.realtime.on("gcs_migration_finished", (data) => frappe.show_alert({ message: data.message, indicator: data.indicator || "green" }));
