// Load the Vue permission screens only when a permission form needs them.
frappe.provide("frappe.permission_ui_loader");
(() => {
	let pending;
	frappe.permission_ui_loader.ensure = async () => {
		if (frappe.permission_ui) return frappe.permission_ui;
		try {
			pending ||= frappe.require("permission_ui.bundle.js");
			await pending;
			if (!frappe.permission_ui) throw new Error("Permission UI bundle did not initialize");
			return frappe.permission_ui;
		} catch (error) {
			pending = null;
			throw error;
		}
	};
	for (const doctype of ["User", "Role Profile"]) {
		frappe.ui.form.on(doctype, {
			on_hide(frm) {
				frm.__permission_ui_hidden = true;
				for (const field of Object.values(frm.fields_dict)) {
					if (field.df.fieldtype === "HTML") {
						frappe.permission_ui?.unmount(field.$wrapper);
					}
				}
			},
		});
	}
})();
