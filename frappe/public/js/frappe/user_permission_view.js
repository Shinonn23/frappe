// A saved, read-only view of User authority. Grants remain owned by Role Profiles.
(() => {
	const escape = (value) =>
		String(value ?? "").replace(
			/[&<>"']/g,
			(c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
		);
	const sources = (items) => (items || []).map(escape).join(", ") || "User / system roles";
	const table = (headers, rows) =>
		rows.length
			? `<div class="table-responsive"><table class="table table-bordered table-sm">
			<thead><tr>${headers.map((h) => `<th>${escape(h)}</th>`).join("")}</tr></thead>
			<tbody>${rows
				.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
				.join("")}</tbody></table></div>`
			: '<p class="text-muted">No permissions.</p>';
	const step_label = (row) =>
		[row.reference_doctype, row.state, row.step].filter(Boolean).map(escape).join(" / ");
	const scope = (row) => {
		const parts = [row.company || "All companies", row.scope_kind, row.scope_source];
		for (const [kind, value] of row.scope_values || []) parts.push(`${kind}: ${value}`);
		if (row.scope_kind && row.scope_kind !== "All") {
			parts.push(row.include_descendants ? "Includes descendants" : "Selected values only");
		}
		if (row.resolved_scope_roots) {
			parts.push(`Current roots: ${row.resolved_scope_roots.join(", ") || "None"}`);
		}
		return parts.filter(Boolean).map(escape).join("<br>");
	};
	const limits = (row) => {
		const result = [];
		if ((row.limit_kind || "").includes("Amount")) {
			result.push(`Amount: ${row.max_amount == null ? "Unlimited" : row.max_amount}`);
		}
		if ((row.limit_kind || "").includes("Percent")) {
			result.push(
				`Percent: ${row.max_percent == null ? "Unlimited" : `${row.max_percent}%`}`
			);
		}
		if (row.limit_kind === "Grade") {
			result.push(`Grade: ${row.max_grade == null ? "Unlimited" : row.max_grade}`);
		}
		return result.map(escape).join("<br>") || "No ceiling";
	};
	function render(data) {
		const managed_roles = new Set(
			(data.managed_roles || []).map((role) => (typeof role === "string" ? role : role.role))
		);
		const roles = (data.roles || [])
			.filter((row) => !managed_roles.has(row.role))
			.map((r) => [escape(r.role), sources(r.sources)]);
		const flags = (data.capabilities || []).map((r) => [
			escape(r.label),
			escape(r.status),
			sources(r.sources),
		]);
		const grants = data.grants || [];
		let html =
			'<p class="text-muted">Saved permissions. Edit workflow permissions in the source ' +
			"Role Profile. Document conditions and server permission checks still apply.</p>";
		html += `<p><strong>Role Profiles:</strong> ${
			(data.profiles || []).map((profile) => escape(profile.name)).join(", ") || "None"
		}</p>`;
		if (data.administrator)
			html +=
				'<p class="text-muted">Administrator has implicit system ' +
				"access. The saved grants below do not limit Administrator.</p>";
		html += "<h4>Functional Roles</h4>" + table(["Role", "Source profiles"], roles);
		html +=
			"<h4>Data Permissions</h4>" +
			table(["Permission", "Status", "Source profiles"], flags);
		html +=
			"<h4>Workflow Permissions</h4>" +
			table(
				["Flow / Permission", "Scope", "Ceilings", "Source profiles"],
				grants.map((r) => [
					`${escape(r.flow)}<br>${step_label(r)}`,
					scope(r),
					limits(r),
					sources(r.sources),
				])
			);
		html +=
			'<h4>Notifications</h4><p class="text-muted">Ask receives the task; Stand-in is ' +
			"used by workflow routing when needed. Notification delivery follows the grant scope.</p>" +
			table(
				["Permission", "Routing", "Channel", "Scope", "Source profiles"],
				grants.map((r) => [
					step_label(r),
					escape(r.ask_mode),
					escape(r.channel),
					scope(r),
					sources(r.sources),
				])
			);
		if (data.handoffs?.length)
			html +=
				"<h5>Handoff assignments</h5>" +
				'<p class="text-muted">Configured assignments; Handoff Rule seats, document access and ' +
				"routing determine actual recipients.</p>" +
				table(
					["Permission / Rule", "Routing", "Scope", "Source profiles"],
					data.handoffs.map((r) => [
						step_label(r) + (r.handoff_rule ? `<br>${escape(r.handoff_rule)}` : ""),
						escape(r.ask_mode),
						scope(r),
						sources(r.sources),
					])
				);
		return html;
	}
	async function refresh(frm) {
		const wrapper = frm.fields_dict.profile_permissions_html?.$wrapper;
		if (!wrapper) return;
		if (frm.get_perm && !frm.get_perm(1, "read")) return;
		frm.toggle_display?.("roles_html", true);
		frm.toggle_display?.("profile_permissions_html", true);
		const generation = (frm.__profile_permissions_generation || 0) + 1;
		frm.__profile_permissions_generation = generation;
		if (frm.is_new() || frm.is_dirty()) {
			wrapper.html('<p class="text-muted">Save to refresh permissions.</p>');
			return;
		}
		wrapper.html('<p class="text-muted">Loading saved permissions...</p>');
		const user = frm.doc.name;
		try {
			const response = await frappe.call({
				method: "frappe.core.doctype.role_profile.role_profile.get_user_permissions",
				args: { user },
			});
			if (generation !== frm.__profile_permissions_generation || frm.doc.name !== user)
				return;
			if (!response.message) {
				wrapper.html("");
				frm.toggle_display?.("profile_permissions_html", false);
				return;
			}
			wrapper.html(
				frm.is_dirty()
					? '<p class="text-muted">Save to refresh permissions.</p>'
					: render(response.message)
			);
			if (!frm.is_dirty())
				frm.toggle_display?.(
					"roles_html",
					!(frm.doc.role_profiles?.length || frm.doc.role_profile_name)
				);
		} catch (error) {
			if (generation !== frm.__profile_permissions_generation || frm.doc.name !== user)
				return;
			wrapper.html(
				'<p class="text-muted">Unable to load saved permissions. Check access and refresh.</p>'
			);
		}
	}
	frappe.provide("frappe.user_permission_view");
	Object.assign(frappe.user_permission_view, { render, refresh });
	frappe.ui.form.on("User", {
		refresh,
		role_profiles: refresh,
		role_profile_name: refresh,
	});
	frappe.ui.form.on("User Role Profile", {
		role_profile: refresh,
		role_profiles_remove: refresh,
		role_profiles_add: refresh,
	});
})();
