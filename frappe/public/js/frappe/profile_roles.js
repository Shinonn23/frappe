// Map the Vue role selections to the native role table and the provider's backing flags.
frappe.provide("frappe.profile_roles");
(() => {
	const api = frappe.profile_roles;
	function options(doc, catalog) {
		const selected = new Set((doc.roles || []).map((row) => row.role));
		const flags = new Map(
			(catalog.flags || []).filter((flag) => flag.role).map((flag) => [flag.role, flag])
		);
		const managed = new Set(catalog.managed_roles || []);
		return [...new Set([...(catalog.roles || []), ...selected, ...flags.keys()])]
			.filter((role) => !managed.has(role))
			.sort()
			.map((role) => {
				const flag = flags.get(role);
				return {
					value: role,
					label: __(flag?.label || role),
					checked: selected.has(role) || Boolean(flag && doc[flag.fieldname]),
					group: flag ? "Data Permissions" : "Functional Roles",
				};
			});
	}
	function sync_flags(frm, catalog) {
		const selected = new Set((frm.doc.roles || []).map((row) => row.role));
		return Promise.all(
			(catalog.flags || [])
				.filter((item) => item.role)
				.map((flag) => frm.set_value(flag.fieldname, Number(selected.has(flag.role))))
		);
	}
	async function set_role(frm, role, checked) {
		const catalog = frm.__permission_catalog;
		if (!catalog || !frappe.profile_permissions.writable(frm)) return false;
		const choices = options(frm.doc, catalog);
		const choice = choices.find((item) => item.value === role);
		if (!choice) return false;
		choice.checked = Boolean(checked);
		const selected = new Set(choices.filter((item) => item.checked).map((item) => item.value));
		const editable = new Set(choices.map((item) => item.value));
		frm.doc.roles = (frm.doc.roles || []).filter(
			(row) => !editable.has(row.role) || selected.has(row.role)
		);
		for (const role of selected) {
			if (!frm.doc.roles.some((row) => row.role === role)) frm.add_child("roles", { role });
		}
		frm.doc.roles.forEach((row, index) => {
			row.idx = index + 1;
		});
		frm.refresh_field("roles");
		frm.dirty();
		await sync_flags(frm, catalog);
		render(frm);
		return true;
	}
	async function set_flag(frm, name, checked) {
		const catalog = frm.__permission_catalog;
		if (!catalog || !frappe.profile_permissions.writable(frm)) return false;
		if (!catalog.flags?.some((flag) => !flag.role && flag.fieldname === name)) return false;
		await frm.set_value(name, Number(Boolean(checked)));
		render(frm);
		return true;
	}
	function render(frm) {
		if (!frm.__permission_catalog || !frappe.permission_ui) return;
		frappe.permission_ui.mount(frm.fields_dict.roles_html.$wrapper, "roles", {
			frm,
			catalog: frm.__permission_catalog,
		});
		frm.toggle_display("additional_permissions_html", false);
	}
	async function refresh(frm) {
		const wrapper = frm.fields_dict.roles_html?.$wrapper;
		if (!wrapper) return;
		frm.__permission_ui_hidden = false;
		const doc = frm.doc;
		const generation = (frm.__profile_roles_generation || 0) + 1;
		frm.__profile_roles_generation = generation;
		const current = () =>
			!frm.__permission_ui_hidden &&
			frm.doc === doc &&
			generation === frm.__profile_roles_generation;
		try {
			if (frm.__permission_catalog_doc !== doc) {
				delete frm.__permission_catalog_promise;
				delete frm.__permission_catalog;
				frappe.permission_ui?.unmount(wrapper);
				wrapper.empty().text(__("Loading permission choices..."));
				frm.__permission_catalog_doc = doc;
			}
			frm.__permission_catalog_promise ||= frappe.xcall(
				"frappe.core.doctype.role_profile.role_profile.get_permission_catalog",
				{ profile: frm.is_new() ? null : doc.name }
			);
			const catalog = await frm.__permission_catalog_promise;
			if (!current()) return;
			if (!catalog) {
				if (!frm.roles_editor)
					frm.roles_editor = new frappe.RoleEditor(
						wrapper,
						frm,
						!frappe.profile_permissions.writable(frm)
					);
				frm.roles_editor.show();
				return;
			}
			await frappe.permission_ui_loader.ensure();
			if (!current()) return;
			frm.__permission_catalog = catalog;
			// An older native checkbox instance must never overwrite the Vue selections at save.
			delete frm.roles_editor;
			render(frm);
		} catch (error) {
			if (!current()) return;
			delete frm.__permission_catalog_promise;
			frappe.permission_ui?.unmount(wrapper);
			wrapper.text(__("Unable to load permission choices. Refresh the form to retry."));
			console.error("Role permission catalog", error);
		}
	}
	Object.assign(api, { options, sync_flags, set_role, set_flag, render, refresh });
})();
