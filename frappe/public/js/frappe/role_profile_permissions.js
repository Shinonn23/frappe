// Edit the existing grant tables; the server remains the authority for every decision.
frappe.provide("frappe.profile_permissions");
(() => {
	const api = frappe.profile_permissions;
	const handoff_handlers = new Set();
	const escape = (value) =>
		String(value ?? "").replace(
			/[&<>"']/g,
			(char) =>
				({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])
		);
	const scope_sources = (kind, catalog = {}) =>
		catalog.scope_sources?.[kind] ||
		{
			All: ["All"],
			Department: ["Fixed values", "My Department", "Departments I head"],
			Company: ["Fixed values", "My Company"],
			Warehouse: [
				"Fixed values",
				"Warehouses of my Department",
				"Warehouses with no Department",
			],
		}[kind] || ["Fixed values"];
	const writable = (frm) =>
		Boolean(frm.perm?.some((perm) => perm.write)) &&
		!frm.doc.docstatus &&
		!frm.read_only &&
		!frm.is_read_only?.();
	const scope_values = (doc, row) =>
		(doc.approval_scope_values || []).filter(
			(value) => row.rule_key && value.rule_key === row.rule_key
		);
	function summary(row, doc, step) {
		const kind = row.scope_kind || "All";
		const scope =
			row.scope_source === "Fixed values"
				? scope_values(doc, row)
						.map((value) => value.value)
						.join(", ")
				: row.scope_source || "All";
		const parts = [
			row.company || __("All companies"),
			step?.scope_policy || `${kind}: ${scope}`,
		];
		if (!step?.scope_policy && row.include_descendants && kind !== "All") {
			parts.push(__("Includes descendants"));
		}
		for (const [field, label] of [
			["max_amount", "Amount"],
			["max_percent", "Percent"],
			["max_grade", "Grade"],
		]) {
			if (row[field]) parts.push(`${__(label)} ≤ ${row[field]}`);
			else if (ceilings(row.limit_kind).includes(field)) {
				parts.push(`${__(label)}: ${__("Unlimited")}`);
			}
		}
		if (!row.max_amount && !row.max_percent && !row.max_grade) parts.push(__("Unlimited"));
		return parts.join(" · ");
	}
	function changed(frm) {
		frm.refresh_field("approval_grants");
		frm.refresh_field("approval_scope_values");
		frm.refresh_field("handoff_grants");
		frm.dirty();
		render(frm);
	}
	function remove(frm, row) {
		if (!writable(frm)) return;
		frm.doc.approval_grants = (frm.doc.approval_grants || []).filter((item) => item !== row);
		frm.doc.approval_scope_values = (frm.doc.approval_scope_values || []).filter(
			(item) => !row.rule_key || item.rule_key !== row.rule_key
		);
		for (const field of ["approval_grants", "approval_scope_values"]) {
			frm.doc[field].forEach((item, index) => {
				item.idx = index + 1;
			});
		}
		changed(frm);
	}
	function apply(frm, row, values, step) {
		if (!writable(frm)) return false;
		const target = row || frm.add_child("approval_grants");
		Object.assign(target, values, {
			step: step.step_id,
			doctype_label: step.reference_doctype,
			state_label: step.state,
			limit_kind: step.limit_kind,
			rule_key: target.rule_key || frappe.utils.get_random(10),
		});
		delete target.fixed_values;
		frm.doc.approval_scope_values = (frm.doc.approval_scope_values || []).filter(
			(item) => item.rule_key !== target.rule_key
		);
		if (values.scope_source === "Fixed values") {
			for (const value of values.fixed_values || []) {
				frm.add_child("approval_scope_values", {
					rule_key: target.rule_key,
					scope_doctype: values.scope_kind,
					value,
				});
			}
		}
		frm.doc.approval_scope_values.forEach((item, index) => {
			item.idx = index + 1;
		});
		changed(frm);
		return true;
	}
	const ceilings = (kind) =>
		({
			Amount: ["max_amount"],
			Percent: ["max_percent"],
			"Amount+Percent": ["max_amount", "max_percent"],
			Grade: ["max_grade"],
		}[kind] || []);
	function validate(values, step, catalog = {}) {
		if (!step) return __("Choose an available workflow permission.");
		if (!["All", ...(step.allowed_scopes || [])].includes(values.scope_kind)) {
			return __("Choose an allowed scope.");
		}
		if (!scope_sources(values.scope_kind, catalog).includes(values.scope_source)) {
			return __("Choose where the scope comes from.");
		}
		if (values.scope_source === "Fixed values" && !values.fixed_values?.length) {
			return __("Choose at least one fixed scope value.");
		}
		for (const field of ceilings(step.limit_kind)) {
			const number = Number(values[field] || 0);
			if (
				!Number.isFinite(number) ||
				number < 0 ||
				(field === "max_percent" && number > 100)
			) {
				return __("Limits must be non-negative; percent cannot exceed 100.");
			}
		}
		return "";
	}
	async function edit(frm, row) {
		if (!writable(frm)) return;
		const catalog = frm.__permission_catalog || {};
		const steps = catalog.steps || [];
		const child = frm.fields_dict.approval_grants?.df?.options || catalog.approval_doctype;
		const step_doctype =
			catalog.step_doctype || frappe.meta?.get_docfield(child, "step")?.options;
		if (!step_doctype) {
			frappe.msgprint(__("Workflow permission catalog has no step DocType."));
			return;
		}
		let updating = true;
		let dialog;
		const initial_step = steps.find((step) => step.step_id === row?.step);
		const find_step = () => steps.find((step) => step.step_id === dialog.get_value("step"));
		async function update_scope(reset) {
			if (find_step()?.scope_policy) {
				update_fixed();
				return;
			}
			const kind = dialog.get_value("scope_kind");
			const sources = scope_sources(kind, catalog);
			dialog.set_df_property("scope_source", "options", sources.join("\n"));
			const next = {};
			if (reset || !sources.includes(dialog.get_value("scope_source"))) {
				next.scope_source = kind === "All" ? "All" : "Fixed values";
			}
			if (reset) next.fixed_values = [];
			await dialog.set_values(next);
			dialog.set_df_property("include_descendants", "hidden", kind === "All");
			update_fixed();
		}
		function update_fixed() {
			if (!dialog) return;
			const fixed =
				!find_step()?.scope_policy && dialog.get_value("scope_source") === "Fixed values";
			dialog.set_df_property("fixed_values", "hidden", !fixed);
			dialog.set_df_property("fixed_values", "reqd", fixed ? 1 : 0);
		}
		async function update_step(reset) {
			if (updating || !dialog) return;
			updating = true;
			const step = find_step();
			const scopes = ["All", ...(step?.allowed_scopes || []).filter((x) => x !== "All")];
			dialog.set_df_property("scope_kind", "options", ["", ...scopes].join("\n"));
			if (reset || (step?.scope_policy && row?.step !== step.step_id)) {
				await dialog.set_values({
					scope_kind: step?.scope_policy || scopes.length === 1 ? "All" : "",
					...(step?.scope_policy ? { scope_source: "All", fixed_values: [] } : {}),
					max_amount: 0,
					max_percent: 0,
					max_grade: 0,
				});
			}
			for (const field of ["max_amount", "max_percent", "max_grade"]) {
				dialog.set_df_property(
					field,
					"hidden",
					!ceilings(step?.limit_kind).includes(field)
				);
			}
			dialog.set_df_property("limits_section", "hidden", !ceilings(step?.limit_kind).length);
			const details = step
				? [step.flow, step.reference_doctype, step.state, ...(step.actions || [])].join(
						" · "
				  )
				: __("Choose a live Approval or Maker Limit step.");
			dialog.fields_dict.details.$wrapper.html(
				`<p class="text-muted">${escape(details)}</p>` +
					(step?.scope_policy
						? `<p><strong>${escape(step.scope_policy)}</strong></p>`
						: "") +
					(step?.note ? `<p class="text-muted">${escape(step.note)}</p>` : "")
			);
			for (const field of ["scope_section", "scope_kind", "scope_source"]) {
				dialog.set_df_property(field, "hidden", Boolean(step?.scope_policy));
			}
			for (const field of ["scope_kind", "scope_source"]) {
				dialog.set_df_property(field, "reqd", step?.scope_policy ? 0 : 1);
			}
			await update_scope(reset);
			if (step?.scope_policy) dialog.set_df_property("include_descendants", "hidden", true);
			updating = false;
		}
		dialog = new frappe.ui.Dialog({
			title: __(row ? "Edit Workflow Permission" : "Add Workflow Permission"),
			size: "large",
			fields: [
				{
					fieldname: "step",
					fieldtype: "Link",
					label: __("Workflow Permission"),
					options: step_doctype,
					reqd: 1,
					get_query: () => ({
						filters: { retired: 0, kind: ["in", ["Approval", "Maker Limit"]] },
					}),
					onchange: () => update_step(true),
				},
				{ fieldname: "details", fieldtype: "HTML" },
				{
					fieldname: "company",
					fieldtype: "Link",
					options: "Company",
					label: __("Company (blank = all)"),
				},
				{ fieldname: "scope_section", fieldtype: "Section Break", label: __("Scope") },
				{
					fieldname: "scope_kind",
					fieldtype: "Select",
					label: __("Scope"),
					reqd: 1,
					options: ["", "All", ...(initial_step?.allowed_scopes || [])].join("\n"),
					onchange: () => {
						if (dialog && !updating) return update_scope(true);
					},
				},
				{
					fieldname: "scope_source",
					fieldtype: "Select",
					label: __("Scope Source"),
					reqd: 1,
					options: scope_sources(row?.scope_kind, catalog).join("\n"),
					onchange: update_fixed,
				},
				{
					fieldname: "fixed_values",
					fieldtype: "MultiSelectPills",
					label: __("Fixed Values"),
					get_data: (txt) => {
						const kind = dialog?.get_value("scope_kind");
						return kind && kind !== "All" ? frappe.db.get_link_options(kind, txt) : [];
					},
				},
				{
					fieldname: "include_descendants",
					fieldtype: "Check",
					label: __("Include Descendants"),
				},
				{ fieldname: "limits_section", fieldtype: "Section Break", label: __("Limits") },
				...[
					["max_amount", "Currency", "Max Amount"],
					["max_percent", "Percent", "Max Percent"],
					["max_grade", "Int", "Max Grade"],
				].map(([fieldname, fieldtype, label]) => ({
					fieldname,
					fieldtype,
					label: __(label),
					description: __("Blank or 0 = Unlimited"),
				})),
			],
			primary_action_label: __("Apply"),
			primary_action(values) {
				if (updating) return;
				const step = find_step();
				const next = { ...values };
				if (step?.scope_policy) {
					Object.assign(
						next,
						row?.step === step.step_id
							? {
									scope_kind: row.scope_kind || "All",
									scope_source: row.scope_source || "All",
									include_descendants: row.include_descendants || 0,
									fixed_values: scope_values(frm.doc, row).map(
										(item) => item.value
									),
							  }
							: {
									scope_kind: "All",
									scope_source: "All",
									include_descendants: 0,
									fixed_values: [],
							  }
					);
				}
				const error = validate(next, step, catalog);
				if (error) {
					frappe.msgprint(error);
					return;
				}
				for (const field of ["max_amount", "max_percent", "max_grade"]) {
					if (!ceilings(step.limit_kind).includes(field)) {
						next[field] = row?.step === step.step_id ? row[field] || 0 : 0;
					}
				}
				next.ask_mode = row?.ask_mode || "Ask";
				next.channel = row?.channel || step.default_channel || "Bell";
				if (apply(frm, row, next, step)) dialog.hide();
			},
		});
		await dialog.set_values({
			step: row?.step || "",
			company: row?.company || "",
			scope_kind: row?.scope_kind || "",
			scope_source: row?.scope_source || "",
			fixed_values: row ? scope_values(frm.doc, row).map((item) => item.value) : [],
			include_descendants: row?.include_descendants ?? 1,
			max_amount: row?.max_amount || 0,
			max_percent: row?.max_percent || 0,
			max_grade: row?.max_grade || 0,
		});
		updating = false;
		await update_step(false);
		dialog.show();
	}
	function render(frm) {
		const field = frm.fields_dict.workflow_permissions_html;
		if (!field) return;
		const catalog = frm.__permission_catalog;
		if (!catalog) return;
		const can_write = writable(frm);
		const steps = new Map(catalog.steps.map((step) => [step.step_id, step]));
		const rows = frm.doc.approval_grants || [];
		const groups = new Map();
		rows.forEach((row, index) => {
			const step = steps.get(row.step);
			const group = step
				? `${step.flow} · ${step.reference_doctype}`
				: __("Unavailable permissions");
			if (!groups.has(group)) groups.set(group, []);
			groups.get(group).push({ row, index, step });
		});
		const button = (action, index, label) => `<button type="button" class="btn btn-xs
			btn-default" data-action="${action}" data-index="${index}">${escape(__(label))}</button>`;
		let html = `<div class="frappe-profile-permissions"><p class="text-muted">
			${escape(__("Each row grants one workflow step within its own scope and limits."))}</p>`;
		if (can_write) html += button("add", "", "Add Permission");
		if (!rows.length)
			html += `<p class="text-muted mt-3">${escape(
				__("No workflow permissions selected.")
			)}</p>`;
		for (const [group, items] of groups) {
			html += `<h5 class="mt-4">${escape(group)}</h5>`;
			for (const { row, index, step } of items) {
				html += `<div style="border:1px solid var(--border-color);border-radius:var(--border-radius);
					padding:12px;margin-bottom:8px;background:var(--card-bg)">
					<div class="flex justify-between"><strong>${escape(
						step?.state || row.state_label || row.step
					)}</strong><span>`;
				if (can_write) {
					if (step) html += button("edit", index, "Edit");
					html += " " + button("remove", index, "Remove");
				}
				html += `</span></div><div>${escape(
					step?.reference_doctype || row.doctype_label || ""
				)}
					 · ${escape(step?.kind || "")}</div>
					<div class="text-muted">${escape(summary(row, frm.doc, step))}</div>`;
				if (!step)
					html += `<div class="text-danger">${escape(
						__("This permission is no longer available in the live catalog.")
					)}</div>`;
				if (step?.needs_flags?.length) {
					html += `<div class="text-muted">${escape(__("Requires"))}:
						${escape(
							step.needs_flags
								.map(
									(name) =>
										catalog.flags?.find((flag) => flag.fieldname === name)
											?.label || name
								)
								.join(", ")
						)}</div>`;
				}
				html += "</div>";
			}
		}
		field.$wrapper.html(html + "</div>");
		field.$wrapper
			.off("click.profile_permissions")
			.on("click.profile_permissions", "button[data-action]", (event) => {
				if (!writable(frm)) return;
				const target = event.currentTarget;
				const row = rows[Number(target.dataset.index)];
				if (target.dataset.action === "add") return edit(frm);
				if (target.dataset.action === "edit" && row) return edit(frm, row);
				if (target.dataset.action === "remove" && row) {
					frappe.confirm(__("Remove this workflow permission?"), () => remove(frm, row));
				}
			});
		render_notifications(frm, rows, steps, can_write);
	}
	function render_notifications(frm, rows, steps, can_write) {
		const field = frm.fields_dict.notifications_html;
		if (!field) return;
		const options = (values, selected) =>
			values
				.map(
					(value) =>
						`<option value="${escape(value)}"${
							value === selected ? " selected" : ""
						}>${escape(__(value))}</option>`
				)
				.join("");
		const select = (index, key, values, current) => `<select class="form-control input-xs"
			aria-label="${escape(__(key === "ask_mode" ? "Assignment Mode" : "Channel"))}"
			data-index="${index}" data-key="${key}"${can_write ? "" : " disabled"}>
			${options(values, current)}</select>`;
		field.$wrapper.html(
			`<p class="text-muted">${escape(
				__(
					"Ask: receive tasks and can act. Stand-in: can act, without receiving tasks. These settings use the permissions and scopes above."
				)
			)}</p><div class="table-responsive"><table class="table table-bordered">
			<thead><tr><th>${escape(__("Workflow Permission / Scope"))}</th>
			<th>${escape(__("Assignment Mode"))}</th><th>${escape(__("Channel"))}</th></tr></thead><tbody>` +
				rows
					.map(
						(row, index) => `<tr><td><strong>${escape(row.step)}</strong>
				<div class="text-muted">${escape(summary(row, frm.doc, steps.get(row.step)))}</div></td>
				<td>${select(index, "ask_mode", ["Ask", "Stand-in"], row.ask_mode || "Ask")}</td>
				<td>${select(
					index,
					"channel",
					["Bell", "Email", "Both"],
					row.channel || steps.get(row.step)?.default_channel || "Bell"
				)}</td></tr>`
					)
					.join("") +
				`</tbody></table></div>${
					rows.length
						? ""
						: `<p class="text-muted">${escape(
								__("Add workflow permissions to configure their notifications.")
						  )}</p>`
				}`
		);
		const handoffs = (frm.doc.handoff_grants || [])
			.map((row, index) => ({ row, index }))
			.filter(({ row }) => row.scope_source === "Fixed values");
		if (handoffs.length) {
			field.$wrapper.append(
				`<h5>${escape(__("Hand-off Fixed Scopes"))}</h5>` +
					handoffs
						.map(
							({ row, index }) => `<div class="mb-2">
					<strong>${escape(row.step || row.handoff_rule || __("Hand-off task"))}</strong>
					<span class="text-muted">${escape(
						scope_values(frm.doc, row)
							.map((item) => `${item.scope_doctype}: ${item.value}`)
							.join(", ")
					)}</span>
					${
						can_write
							? `<button type="button" class="btn btn-xs btn-default"
					data-handoff="${index}">${escape(__("Configure Fixed Values"))}</button>`
							: ""
					}
					</div>`
						)
						.join("")
			);
		}
		field.$wrapper
			.off("click.profile_handoff")
			.on("click.profile_handoff", "button[data-handoff]", (event) => {
				const row = frm.doc.handoff_grants?.[Number(event.currentTarget.dataset.handoff)];
				if (row && writable(frm)) edit_handoff_scope(frm, row);
			});
		field.$wrapper
			.off("change.profile_permissions")
			.on("change.profile_permissions", "select", (event) => {
				if (!writable(frm)) return;
				const target = event.currentTarget;
				const row = rows[Number(target.dataset.index)];
				const choices =
					target.dataset.key === "ask_mode"
						? ["Ask", "Stand-in"]
						: ["Bell", "Email", "Both"];
				if (
					!row ||
					!["ask_mode", "channel"].includes(target.dataset.key) ||
					!choices.includes(target.value)
				)
					return;
				row[target.dataset.key] = target.value;
				changed(frm);
			});
	}
	function edit_handoff_scope(frm, row) {
		const dialog = new frappe.ui.Dialog({
			title: __("Hand-off Fixed Values"),
			fields: [
				{
					fieldname: "values",
					fieldtype: "Table",
					label: __("Fixed Values"),
					data: scope_values(frm.doc, row).map((item) => ({
						scope_doctype: item.scope_doctype,
						value: item.value,
					})),
					fields: [
						{
							fieldname: "scope_doctype",
							fieldtype: "Select",
							label: __("Scope"),
							options: "Department\nWarehouse",
							reqd: 1,
							in_list_view: 1,
						},
						{
							fieldname: "value",
							fieldtype: "Dynamic Link",
							label: __("Value"),
							options: "scope_doctype",
							reqd: 1,
							in_list_view: 1,
						},
					],
				},
			],
			primary_action_label: __("Apply"),
			primary_action(values) {
				if (!writable(frm)) return;
				const scopes = values.values || [];
				if (
					!scopes.length ||
					scopes.some(
						(item) =>
							!["Department", "Warehouse"].includes(item.scope_doctype) ||
							!item.value
					)
				) {
					frappe.msgprint(__("Choose at least one Department or Warehouse value."));
					return;
				}
				row.rule_key ||= frappe.utils.get_random(10);
				frm.doc.approval_scope_values = (frm.doc.approval_scope_values || []).filter(
					(item) => item.rule_key !== row.rule_key
				);
				for (const item of scopes) {
					frm.add_child("approval_scope_values", {
						rule_key: row.rule_key,
						scope_doctype: item.scope_doctype,
						value: item.value,
					});
				}
				frm.doc.approval_scope_values.forEach((item, index) => {
					item.idx = index + 1;
				});
				row.scope_values_summary = scopes.map((item) => item.value).join(", ");
				changed(frm);
				dialog.hide();
			},
		});
		dialog.show();
	}
	async function refresh(frm) {
		if (!frm.fields_dict.workflow_permissions_html) return;
		try {
			frm.__permission_catalog_promise ||= frappe.xcall(
				"frappe.core.doctype.role_profile.role_profile.get_permission_catalog",
				{ profile: frm.is_new() ? null : frm.doc.name }
			);
			frm.__permission_catalog = await frm.__permission_catalog_promise;
			if (!frm.__permission_catalog) return;
			const child =
				frm.fields_dict.handoff_grants?.df?.options ||
				frm.__permission_catalog.handoff_doctype;
			if (child && !handoff_handlers.has(child)) {
				frappe.ui.form.on(child, {
					scope_source: render,
					step: render,
					handoff_rule: render,
					handoff_grants_add: render,
					handoff_grants_remove: render,
				});
				handoff_handlers.add(child);
			}
			render(frm);
		} catch (error) {
			delete frm.__permission_catalog_promise;
			for (const name of ["workflow_permissions_html", "notifications_html"]) {
				frm.fields_dict[name]?.$wrapper.html(
					`<p class="text-danger">${escape(
						__("Unable to load workflow permissions. Refresh the form to retry.")
					)}</p>`
				);
			}
			console.error("Workflow permission catalog", error);
		}
	}
	Object.assign(api, {
		escape,
		scope_sources,
		summary,
		writable,
		validate,
		apply,
		remove,
		render,
	});
	frappe.ui.form.on("Role Profile", { refresh });
})();
