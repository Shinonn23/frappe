// A saved, read-only view of User authority using the native Desk form controls.
frappe.provide("frappe.user_permission_view");
(() => {
	const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
	const sources = (items) => (items || []).join(", ") || __("Direct role");
	const field = (fieldname, label, columns = 0, fieldtype = "Data") => ({
		fieldname,
		label: __(label),
		fieldtype,
		read_only: 1,
		in_list_view: columns ? 1 : 0,
		columns,
		formatter: escape,
	});
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
	function table(fields, name, label, rows, columns, collapsed = false, description = "") {
		fields.push({
			fieldname: `${name}_section`,
			fieldtype: "Section Break",
			label: `${__(label)} (${rows.length})`,
			collapsible: 1,
			description: __(description),
		});
		if (rows.length) {
			fields.push({
				fieldname: name,
				fieldtype: "Table",
				label: "",
				read_only: 1,
				cannot_add_rows: 1,
				cannot_delete_rows: 1,
				on_setup(grid) {
					// Standalone grids need static rows because they have no form permissions.
					grid.static_rows = true;
				},
				fields: columns,
				data: rows.map((row, index) => ({
					...row,
					idx: index + 1,
					name: `${name}-${index}`,
				})),
			});
		} else {
			fields.push({ ...field(`${name}_empty`, "Status"), default: __("No permissions") });
		}
		return collapsed ? `${name}_section` : null;
	}
	function mount(wrapper, fields) {
		wrapper.empty();
		const group = new frappe.ui.FieldGroup({ parent: wrapper, fields, doc: {} });
		group.make();
		return group;
	}
	function status(wrapper, message) {
		return mount(wrapper, [
			{
				...field("permission_status", "Permission Summary", 0, "Small Text"),
				default: message,
			},
		]);
	}
	function render(data, wrapper, { direct_roles = false } = {}) {
		const managed = new Set(
			(data.managed_roles || []).map((role) => (typeof role === "string" ? role : role.role))
		);
		const implicit = new Set(data.implicit_roles || []);
		const roles = (data.roles || []).filter((row) => !managed.has(row.role));
		const fields = [
			{
				...field("permission_status", "Permission Summary", 0, "Small Text"),
				default: data.administrator
					? __(
							"Administrator has implicit system access. Saved grants do not limit Administrator."
					  )
					: __("Saved permissions. Edit workflow authority in its source Role Profile."),
				description: __("Open a row to inspect its conditions and source profiles."),
			},
		];
		if (!direct_roles) {
			table(
				fields,
				"functional_roles",
				"Functional Roles",
				roles
					.filter((row) => !implicit.has(row.role))
					.map((row) => ({ role: row.role, sources: sources(row.sources) })),
				[field("role", "Role", 4), field("sources", "Source Profiles", 6, "Small Text")]
			);
		}
		table(
			fields,
			"data_permissions",
			"Data Permissions",
			(data.capabilities || []).map((row) => ({
				permission: row.label,
				status: row.status,
				sources: sources(row.sources),
			})),
			[
				field("permission", "Permission", 4),
				field("status", "Status", 3),
				field("sources", "Source Profiles", 3, "Small Text"),
			]
		);
		table(
			fields,
			"workflow_permissions",
			"Workflow Permissions",
			(data.grants || []).map((row) => ({
				document_type: row.reference_doctype,
				permission: row.state || row.step,
				scope_summary: scope(row),
				notification: [row.ask_mode, row.channel].filter(Boolean).join(" · "),
				permission_id: row.step,
				flow: row.flow,
				conditions: scope(row, true),
				ceilings: limits(row),
				sources: sources(row.sources),
			})),
			[
				field("document_type", "Document Type", 3),
				field("permission", "Permission", 3),
				field("scope_summary", "Scope", 2),
				field("notification", "Notification", 2),
				field("permission_id", "Permission ID"),
				field("flow", "Flow"),
				field("conditions", "Conditions", 0, "Small Text"),
				field("ceilings", "Limits", 0, "Small Text"),
				field("sources", "Source Profiles", 0, "Small Text"),
			]
		);
		table(
			fields,
			"handoff_assignments",
			"Handoff Assignments",
			handoffs(data.handoffs).map((row) => ({
				document_type: row.reference_doctype,
				permission: row.handoff_rule || row.step,
				notification: [row.ask_mode, row.channel].filter(Boolean).join(" · "),
				sources: sources(row.sources),
				permission_id: row.step,
				conditions: scope(row, true),
			})),
			[
				field("document_type", "Document Type", 3),
				field("permission", "Permission / Rule", 3),
				field("notification", "Routing", 2),
				field("sources", "Source Profiles", 2, "Small Text"),
				field("permission_id", "Permission ID"),
				field("conditions", "Conditions", 0, "Small Text"),
			]
		);
		const system_section = table(
			fields,
			"system_roles",
			"System Roles",
			roles.filter((row) => implicit.has(row.role)).map((row) => ({ role: row.role })),
			[field("role", "Role", 10)],
			true,
			"Automatically supplied by Frappe."
		);
		const group = mount(wrapper, fields);
		group.sections?.find((section) => section.df.fieldname === system_section)?.collapse(true);
		return group;
	}
	async function refresh(frm) {
		const wrapper = frm.fields_dict.profile_permissions_html?.$wrapper;
		if (!wrapper) return;
		const generation = (frm.__profile_permissions_generation || 0) + 1;
		frm.__profile_permissions_generation = generation;
		if (frm.get_perm && !frm.get_perm(1, "read")) {
			wrapper.empty();
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
		const user = frm.doc.name;
		try {
			const response = await frappe.call({
				method: "frappe.core.doctype.role_profile.role_profile.get_user_permissions",
				args: { user },
			});
			if (generation !== frm.__profile_permissions_generation || frm.doc.name !== user)
				return;
			if (!response.message) {
				wrapper.empty();
				frm.toggle_display?.("profile_permissions_html", false);
				return;
			}
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
			if (generation !== frm.__profile_permissions_generation || frm.doc.name !== user)
				return;
			frm.__user_permission_group = status(
				wrapper,
				__("Unable to load saved permissions. Check access and refresh.")
			);
		}
	}
	Object.assign(frappe.user_permission_view, { render, refresh });
	frappe.ui.form.on("User", { refresh, role_profiles: refresh, role_profile_name: refresh });
	frappe.ui.form.on("User Role Profile", {
		role_profile: refresh,
		role_profiles_remove: refresh,
		role_profiles_add: refresh,
	});
})();
