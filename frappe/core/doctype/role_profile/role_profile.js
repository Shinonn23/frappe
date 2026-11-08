// Copyright (c) 2017, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Role Profile", {
	refresh(frm) {
		return frappe.profile_roles.refresh(frm);
	},
	async validate(frm) {
		if (frm.roles_editor) frm.roles_editor.set_roles_in_table();
		if (frm.__permission_catalog) {
			await frappe.profile_roles.sync_flags(frm, frm.__permission_catalog);
		}
	},
});
