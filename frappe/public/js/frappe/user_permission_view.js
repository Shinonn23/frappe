// A saved, read-only view; the User form remains the owner of role assignments.
frappe.provide("frappe.user_permission_view");
(() => {
	function scope(row, details = false) {
		const parts = [row.company || __("All companies")];
		if (row.scope_policy) {
			parts.push(row.scope_policy);
			if (details && row.note) parts.push(row.note);
		} else {
			parts.push(row.scope_kind, row.scope_source);
			for (const [kind, value] of row.scope_values || []) parts.push(`${kind}: ${value}`);
			if (row.scope_kind && row.scope_kind !== "All") {
				parts.push(
					row.include_descendants
						? __("Includes descendants")
						: __("Selected values only")
				);
			}
			if (row.resolved_scope_roots) {
				parts.push(
					__("Current roots") +
						": " +
						(row.resolved_scope_roots.join(", ") || __("None"))
				);
			}
		}
		return parts.filter(Boolean).join(" · ");
	}
	function limits(row) {
		const values = [];
		for (const [kind, label, value, unit] of [
			["Amount", "Amount", row.max_amount, ""],
			["Percent", "Percent", row.max_percent, "%"],
			["Grade", "Grade", row.max_grade, ""],
		]) {
			if ((row.limit_kind || "").includes(kind)) {
				values.push(`${__(label)}: ${value == null ? __("Unlimited") : value + unit}`);
			}
		}
		return values.join(" · ") || __("No ceiling");
	}
	function handoffs(rows) {
		const grouped = new Map();
		for (const row of rows || []) {
			const key = JSON.stringify(
				Object.keys(row)
					.filter((name) => name !== "sources")
					.sort()
					.map((name) => [
						name,
						name === "scope_values" ? [...(row[name] || [])].sort() : row[name],
					])
			);
			if (!grouped.has(key)) grouped.set(key, { ...row, sources: [] });
			const item = grouped.get(key);
			item.sources = [...new Set([...item.sources, ...(row.sources || [])])];
		}
		return [...grouped.values()];
	}
	function sections(data, { direct_roles = false } = {}) {
		const managed = new Set(
			(data.managed_roles || []).map((role) => (typeof role === "string" ? role : role.role))
		);
		const implicit = new Set(data.implicit_roles || []);
		const roles = (data.roles || []).filter((row) => !managed.has(row.role));
		const item = (row) => ({
			permission: row.label || row.state || row.step,
			state: row.state,
			description: row.description,
			document_type: row.reference_doctype,
			flow: row.flow,
			permission_id: row.step,
			scope_summary: scope(row),
			conditions: scope(row, true),
			ceilings: limits(row),
			notification: [row.ask_mode, row.channel].filter(Boolean).join(" · "),
			sources: [...(row.sources || [])],
		});
		return [
			...(!direct_roles
				? [
						{
							name: "functional_roles",
							label: __("Functional Roles"),
							rows: roles
								.filter((row) => !implicit.has(row.role))
								.map((row) => ({
									permission: row.role,
									sources: [...(row.sources || [])],
								})),
						},
				  ]
				: []),
			{
				name: "data_permissions",
				label: __("Data Permissions"),
				rows: (data.capabilities || []).map((row) => ({
					permission: row.label,
					status: row.status,
					sources: [...(row.sources || [])],
				})),
			},
			{
				name: "workflow_permissions",
				label: __("Workflow Permissions"),
				rows: (data.grants || []).map(item),
			},
			{
				name: "handoff_assignments",
				label: __("Handoff Assignments"),
				rows: handoffs(data.handoffs).map((row) => ({
					...item(row),
					permission: row.handoff_rule || row.step,
				})),
			},
			{
				name: "system_roles",
				label: __("System Roles"),
				collapsed: true,
				description: __("Automatically supplied by Frappe."),
				rows: roles
					.filter((row) => implicit.has(row.role))
					.map((row) => ({ permission: row.role, sources: [] })),
			},
		];
	}
	function clear(wrapper) {
		frappe.permission_ui?.unmount(wrapper);
		wrapper.empty();
	}
	function status(wrapper, message) {
		clear(wrapper);
		wrapper.text(message);
		return null;
	}
	function render(data, wrapper, { direct_roles = false } = {}) {
		return frappe.permission_ui.mount(wrapper, "user", { data, direct_roles });
	}
	async function refresh(frm) {
		const wrapper = frm.fields_dict.profile_permissions_html?.$wrapper;
		if (!wrapper) return;
		frm.__permission_ui_hidden = false;
		const generation = (frm.__profile_permissions_generation || 0) + 1;
		frm.__profile_permissions_generation = generation;
		const user = frm.doc.name;
		const current = () =>
			!frm.__permission_ui_hidden &&
			generation === frm.__profile_permissions_generation &&
			frm.doc.name === user &&
			(!frm.get_perm || frm.get_perm(1, "read"));
		if (frm.get_perm && !frm.get_perm(1, "read")) {
			clear(wrapper);
			frm.toggle_display?.("profile_permissions_html", false);
			return;
		}
		frm.layout?.sections_dict?.sb1?.set_label(__("Role Assignments"));
		frm.set_df_property?.(
			"role_profiles",
			"description",
			__("Select profiles, then save to refresh effective permissions.")
		);
		frm.toggle_display?.("roles_html", true);
		frm.toggle_display?.("profile_permissions_html", true);
		if (frm.is_new() || frm.is_dirty()) {
			frm.__user_permission_group = status(wrapper, __("Save to refresh permissions."));
			return;
		}
		frm.__user_permission_group = status(wrapper, __("Loading saved permissions..."));
		try {
			const response = await frappe.call({
				method: "frappe.core.doctype.role_profile.role_profile.get_user_permissions",
				args: { user },
			});
			if (!current()) return;
			if (!response.message) {
				clear(wrapper);
				frm.toggle_display?.("profile_permissions_html", false);
				return;
			}
			await frappe.permission_ui_loader.ensure();
			if (!current()) return;
			if (frm.is_dirty()) {
				frm.__user_permission_group = status(wrapper, __("Save to refresh permissions."));
				return;
			}
			const has_profiles = Boolean(
				frm.doc.role_profiles?.length || frm.doc.role_profile_name
			);
			frm.__user_permission_group = render(response.message, wrapper, {
				direct_roles: !has_profiles,
			});
			frm.toggle_display?.("roles_html", !has_profiles);
		} catch (error) {
			if (!current()) return;
			frm.__user_permission_group = status(
				wrapper,
				__("Unable to load saved permissions. Check access and refresh.")
			);
		}
	}
	Object.assign(frappe.user_permission_view, { sections, render, refresh });
	frappe.ui.form.on("User", { refresh, role_profiles: refresh, role_profile_name: refresh });
	frappe.ui.form.on("User Role Profile", {
		role_profile: refresh,
		role_profiles_remove: refresh,
		role_profiles_add: refresh,
	});
})();
