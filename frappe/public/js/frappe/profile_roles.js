// Keep Frappe's RoleEditor lifecycle; map its capability selections to the server's flags.
frappe.provide("frappe.profile_roles");
(() => {
	const api = frappe.profile_roles;
	const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
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
	function group_editor(editor) {
		const control = editor.multicheck;
		const area = control.$checkbox_area;
		for (const title of ["Functional Roles", "Data Permissions"]) {
			const choices = control.options.filter((option) => option.group === title);
			if (!choices.length) continue;
			const section = $("<section class='mb-4'>").appendTo(area);
			$("<h5>").text(__(title)).appendTo(section);

			const grid = $("<div>")
				.css({
					display: "grid",
					"grid-template-columns": "repeat(auto-fit, minmax(min(100%, 230px), 1fr))",
					gap: "4px 16px",
				})
				.appendTo(section);
			for (const option of choices) {
				option.$checkbox.appendTo(grid);
			}
		}
		area.css({ display: "block", columns: "auto" });
		control.$select_buttons.hide();
	}
	function additional(frm, catalog) {
		const field = frm.fields_dict.additional_permissions_html;
		if (!field) return;
		const flags = (catalog.flags || []).filter((flag) => !flag.role);
		const writable = frappe.profile_permissions.writable(frm);
		field.$wrapper.css("padding", "1em").html(
			`<h5>${escape(__("HR and Additional Permissions"))}</h5>
			<div style="display:grid;grid-template-columns:repeat(auto-fit,
			minmax(min(100%, 230px),1fr));gap:8px 16px">` +
				flags
					.map(
						(flag) =>
							`<div class="checkbox"><label style="display:flex;align-items:center"><input type="checkbox" data-flag="${escape(
								flag.fieldname
							)}"
			${frm.doc[flag.fieldname] ? " checked" : ""}${writable ? "" : " disabled"}>
			<span class="label-area">${escape(__(flag.label))}</span></label></div>`
					)
					.join("") +
				"</div>"
		);
		field.$wrapper
			.off("change.profile_roles")
			.on("change.profile_roles", "input[data-flag]", (event) => {
				if (!frappe.profile_permissions.writable(frm)) return;
				const name = event.currentTarget.dataset.flag;
				if (flags.some((flag) => flag.fieldname === name)) {
					frm.set_value(name, Number(event.currentTarget.checked));
				}
			});
	}
	async function refresh(frm) {
		try {
			frm.__permission_catalog_promise ||= frappe.xcall(
				"frappe.core.doctype.role_profile.role_profile.get_permission_catalog",
				{ profile: frm.is_new() ? null : frm.doc.name }
			);
			const catalog = await frm.__permission_catalog_promise;
			if (!catalog) return;
			frm.__permission_catalog = catalog;
			if (!frm.roles_editor && frm.fields_dict.roles_html) {
				frm.roles_editor = new frappe.RoleEditor(
					frm.fields_dict.roles_html.$wrapper,
					frm,
					!frappe.profile_permissions.writable(frm)
				);
			}
			const editor = frm.roles_editor;
			if (editor) {
				editor.managed_roles = new Set(catalog.managed_roles || []);
				editor.roles_loaded = true;
				const control = editor.multicheck;
				editor.disable = !frappe.profile_permissions.writable(frm);
				control.df.get_data = () => options(frm.doc, catalog);
				if (!editor.__profile_grouped) {
					const make = control.make_checkboxes.bind(control);
					control.make_checkboxes = () => {
						// Native role requests may finish after the catalog request.
						control.options = options(frm.doc, frm.__permission_catalog);
						make();
						group_editor(editor);
					};
					const change = control.df.on_change;
					control.df.on_change = () => {
						change();
						sync_flags(frm, frm.__permission_catalog);
					};
					editor.__profile_grouped = true;
				}
				control.set_options();
			}
			additional(frm, catalog);
		} catch (error) {
			delete frm.__permission_catalog_promise;
			frm.fields_dict.additional_permissions_html?.$wrapper.html(
				`<p class="text-danger">${escape(__("Unable to load permission choices."))}</p>`
			);
			console.error("Role permission catalog", error);
		}
	}
	Object.assign(api, { options, sync_flags });
	frappe.ui.form.on("Role Profile", {
		refresh,
		async validate(frm) {
			if (frm.roles_editor && frm.__permission_catalog) {
				frm.roles_editor.set_roles_in_table();
				await sync_flags(frm, frm.__permission_catalog);
			}
		},
	});
})();
