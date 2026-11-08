// Edit the existing grant tables; the server remains the authority for every decision.
frappe.provide("frappe.profile_permissions");
(() => {
	const api = frappe.profile_permissions;
	const handoff_handlers = new Set();
	const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
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
	const ceilings = (kind) =>
		({
			Amount: ["max_amount"],
			Percent: ["max_percent"],
			"Amount+Percent": ["max_amount", "max_percent"],
			Grade: ["max_grade"],
		}[kind] || []);
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
		for (const field of ["approval_grants", "approval_scope_values", "handoff_grants"]) {
			frm.refresh_field(field);
		}
		frm.dirty();
		void render(frm);
	}
	function remove(frm, row) {
		if (!writable(frm) || !(frm.doc.approval_grants || []).includes(row)) return false;
		frm.doc.approval_grants = frm.doc.approval_grants.filter((item) => item !== row);
		frm.doc.approval_scope_values = (frm.doc.approval_scope_values || []).filter(
			(item) => !row.rule_key || item.rule_key !== row.rule_key
		);
		for (const field of ["approval_grants", "approval_scope_values"]) {
			frm.doc[field].forEach((item, index) => {
				item.idx = index + 1;
			});
		}
		changed(frm);
		return true;
	}
	function apply(frm, row, values, step) {
		if (!writable(frm) || (row && !(frm.doc.approval_grants || []).includes(row)))
			return false;
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
	function validate(values, step, catalog = {}) {
		if (!step) return __("Choose an available workflow permission.");
		if (!step.scope_policy) {
			if (!["All", ...(step.allowed_scopes || [])].includes(values.scope_kind)) {
				return __("Choose an allowed scope.");
			}
			if (!scope_sources(values.scope_kind, catalog).includes(values.scope_source)) {
				return __("Choose where the scope comes from.");
			}
			if (values.scope_source === "Fixed values" && !values.fixed_values?.length) {
				return __("Choose at least one fixed scope value.");
			}
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
	function editor_values(doc, row, step) {
		const scoped = (step?.allowed_scopes || []).some((kind) => kind !== "All");
		return {
			step: row?.step || step?.step_id || "",
			company: row?.company || "",
			scope_kind: row?.scope_kind || (step?.scope_policy || !scoped ? "All" : ""),
			scope_source: row?.scope_source || "All",
			fixed_values: row ? scope_values(doc, row).map((item) => item.value) : [],
			include_descendants: row?.include_descendants ?? 1,
			max_amount: row?.max_amount || 0,
			max_percent: row?.max_percent || 0,
			max_grade: row?.max_grade || 0,
		};
	}
	function prepare_values(doc, row, values, step, catalog = {}) {
		const next = { ...values, fixed_values: [...(values.fixed_values || [])] };
		if (step?.scope_policy) {
			Object.assign(
				next,
				row?.step === step.step_id
					? {
							scope_kind: row.scope_kind || "All",
							scope_source: row.scope_source || "All",
							include_descendants: row.include_descendants || 0,
							fixed_values: scope_values(doc, row).map((item) => item.value),
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
		if (error) return { error };
		for (const field of ["max_amount", "max_percent", "max_grade"]) {
			if (!ceilings(step.limit_kind).includes(field)) {
				next[field] = row?.step === step.step_id ? row[field] || 0 : 0;
			} else next[field] = Number(next[field] || 0);
		}
		next.ask_mode = row?.ask_mode || "Ask";
		next.channel = row?.channel || step.default_channel || "Bell";
		return { values: next, error: "" };
	}
	function set_notification(frm, row, key, value) {
		const choices = { ask_mode: ["Ask", "Stand-in"], channel: ["Bell", "Email", "Both"] };
		if (
			!writable(frm) ||
			!(frm.doc.approval_grants || []).includes(row) ||
			!choices[key]?.includes(value)
		)
			return false;
		if (row[key] === value) return true;
		row[key] = value;
		changed(frm);
		return true;
	}
	function apply_handoff_scope(frm, row, scopes) {
		if (!writable(frm) || !(frm.doc.handoff_grants || []).includes(row))
			return __("This assignment is no longer editable.");
		if (
			!scopes.length ||
			scopes.some(
				(item) => !["Department", "Warehouse"].includes(item.scope_doctype) || !item.value
			)
		) {
			return __("Choose at least one Department or Warehouse value.");
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
		return "";
	}
	function show_status(frm, message) {
		for (const name of ["workflow_permissions_html", "notifications_html"]) {
			const wrapper = frm.fields_dict[name]?.$wrapper;
			if (!wrapper) continue;
			frappe.permission_ui?.unmount(wrapper);
			wrapper.empty().text(message);
		}
	}
	async function render(frm) {
		if (frm.__permission_ui_hidden) return;
		if (!frm.fields_dict.workflow_permissions_html || !frm.__permission_catalog) return;
		const generation = (frm.__workflow_permission_generation || 0) + 1;
		frm.__workflow_permission_generation = generation;
		if (frm.get_perm && !frm.get_perm(1, "read")) {
			show_status(frm, __("Not permitted to read workflow permissions."));
			return;
		}
		const doc = frm.doc;
		try {
			await frappe.permission_ui_loader.ensure();
			if (
				frm.__permission_ui_hidden ||
				frm.doc !== doc ||
				generation !== frm.__workflow_permission_generation
			)
				return;
			for (const [field, view] of [
				["workflow_permissions_html", "workflow"],
				["notifications_html", "notifications"],
			]) {
				const wrapper = frm.fields_dict[field]?.$wrapper;
				if (wrapper)
					frappe.permission_ui.mount(wrapper, view, {
						frm,
						catalog: frm.__permission_catalog,
						view,
					});
			}
		} catch (error) {
			if (
				frm.__permission_ui_hidden ||
				frm.doc !== doc ||
				generation !== frm.__workflow_permission_generation
			)
				return;
			show_status(
				frm,
				__("Unable to load workflow permissions. Refresh the form to retry.")
			);
			console.error("Workflow permission UI", error);
		}
	}
	async function refresh(frm) {
		frm.__permission_ui_hidden = false;
		if (!frm.fields_dict.workflow_permissions_html) return;
		const doc = frm.doc;
		if (frm.__permission_catalog_doc !== doc) {
			frm.__permission_catalog_doc = doc;
			delete frm.__permission_catalog_promise;
			delete frm.__permission_catalog;
			show_status(frm, __("Loading workflow permissions..."));
		}
		try {
			frm.__permission_catalog_promise ||= frappe.xcall(
				"frappe.core.doctype.role_profile.role_profile.get_permission_catalog",
				{ profile: frm.is_new() ? null : frm.doc.name }
			);
			const catalog = await frm.__permission_catalog_promise;
			if (frm.__permission_ui_hidden || frm.doc !== doc) return;
			frm.__permission_catalog = catalog;
			if (!catalog) return;
			const child = frm.fields_dict.handoff_grants?.df?.options || catalog.handoff_doctype;
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
			await render(frm);
		} catch (error) {
			if (frm.__permission_ui_hidden || frm.doc !== doc) return;
			delete frm.__permission_catalog_promise;
			show_status(
				frm,
				__("Unable to load workflow permissions. Refresh the form to retry.")
			);
			console.error("Workflow permission catalog", error);
		}
	}
	Object.assign(api, {
		escape,
		scope_sources,
		scope_values,
		ceilings,
		summary,
		writable,
		validate,
		editor_values,
		prepare_values,
		apply,
		remove,
		set_notification,
		apply_handoff_scope,
		render,
	});
	frappe.ui.form.on("Role Profile", { refresh });
})();
