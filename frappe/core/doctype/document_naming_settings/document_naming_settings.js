// Copyright (c) 2022, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Document Naming Settings", {
	setup: function (frm) {
		frm.set_query("document_type", "amend_naming_override", () => {
			return {
				filters: {
					is_submittable: 1,
				},
			};
		});
	},

	refresh: function (frm) {
		frm.trigger("setup_transaction_autocomplete");
		frm.disable_save();
	},

	setup_transaction_autocomplete: function (frm) {
		frappe.call({
			method: "get_transactions_and_prefixes",
			doc: frm.doc,
			callback: function (r) {
				frm.fields_dict.transaction_type.set_data(r.message.transactions);
				frm.fields_dict.sync_transaction_type.set_data(r.message.transactions);
				frm.fields_dict.prefix.set_data(r.message.prefixes);
			},
		});
	},

	transaction_type: function (frm) {
		frm.set_value("user_must_always_select", 0);
		frappe.call({
			method: "get_options",
			doc: frm.doc,
			callback: function (r) {
				frm.set_value("naming_series_options", r.message);
				if (r.message && r.message.split("\n")[0] == "")
					frm.set_value("user_must_always_select", 1);
			},
		});
	},

	prefix: function (frm) {
		frappe.call({
			method: "get_current",
			doc: frm.doc,
			callback: function (r) {
				frm.refresh_field("current_value");
			},
		});
	},

	update: function (frm) {
		frappe.call({
			method: "update_series",
			doc: frm.doc,
			freeze: true,
			freeze_msg: __("Updating naming series options"),
			callback: function (r) {
				frm.trigger("setup_transaction_autocomplete");
				frm.trigger("transaction_type");
			},
		});
	},

	check_series_sync(frm) {
		frappe.call({
			method: "check_series_sync",
			doc: frm.doc,
			freeze: true,
			freeze_msg: __("Checking series counters"),
			callback: (r) => {
				if (r.message) {
					frm.series_sync = r.message;
					frm.trigger("render_series_sync");
				}
			},
		});
	},

	render_series_sync(frm) {
		const { rows, skipped } = frm.series_sync;
		const behind = rows.filter((row) => row.status === "behind");
		const status_label = { behind: __("Behind"), ok: __("In step"), ahead: __("Ahead") };
		const status_class = { behind: "red", ok: "green", ahead: "blue" };
		const esc = frappe.utils.escape_html;

		let html = `<p class="text-muted">${__("{0} series checked, {1} behind.", [
			rows.length,
			behind.length,
		])}</p>`;
		if (rows.length) {
			html += `<div class="table-responsive"><table class="table table-bordered table-sm">
				<thead><tr>
					<th style="width: 32px"><input type="checkbox" class="series-sync-all" /></th>
					<th>${__("Document Type")}</th><th>${__("Series")}</th><th>${__("Prefix")}</th>
					<th class="text-right">${__("Counter")}</th>
					<th class="text-right">${__("Highest Number")}</th><th>${__("Status")}</th>
				</tr></thead><tbody>`;
			rows.forEach((row, index) => {
				html += `<tr>
					<td>${
						row.status === "behind"
							? `<input type="checkbox" class="series-sync-row" data-index="${index}" />`
							: ""
					}</td>
					<td>${esc(row.doctype)}</td><td>${esc(row.series)}</td><td>${esc(row.prefix)}</td>
					<td class="text-right">${row.counter}</td><td class="text-right">${row.highest}</td>
					<td><span class="indicator-pill ${status_class[row.status]}">${
					status_label[row.status]
				}</span></td>
				</tr>`;
			});
			html += "</tbody></table></div>";
		}
		if (skipped.length) {
			html += `<p class="text-muted">${__(
				"Not checked (the prefix has a field in it or is invalid):"
			)} ${skipped.map((row) => esc(`${row.doctype}: ${row.series}`)).join(", ")}</p>`;
		}

		const wrapper = frm.fields_dict.series_sync_html.$wrapper;
		wrapper.html(html);
		wrapper.find(".series-sync-all").on("change", (e) => {
			wrapper.find(".series-sync-row").prop("checked", e.target.checked);
		});
		frm.toggle_display("sync_selected", behind.length > 0);
		frm.toggle_display("sync_all", behind.length > 0);
	},

	sync_selected(frm) {
		const selected = frm.fields_dict.series_sync_html.$wrapper
			.find(".series-sync-row:checked")
			.map((_, el) => {
				const row = frm.series_sync.rows[el.dataset.index];
				return { doctype: row.doctype, prefix: row.prefix };
			})
			.get();
		if (!selected.length) {
			frappe.msgprint(__("Select the series to sync."));
			return;
		}
		frm.events.run_series_sync(frm, selected);
	},

	sync_all(frm) {
		frappe.confirm(__("Raise the counter of every series that is behind?"), () =>
			frm.events.run_series_sync(frm, null)
		);
	},

	run_series_sync(frm, selected) {
		frappe.call({
			method: "sync_series",
			doc: frm.doc,
			args: { selected: selected },
			freeze: true,
			freeze_msg: __("Syncing series counters"),
			callback: () => frm.trigger("check_series_sync"),
		});
	},

	try_naming_series(frm) {
		frappe.call({
			method: "preview_series",
			doc: frm.doc,
			callback: function (r) {
				if (!r.exc) {
					frm.set_value("series_preview", r.message);
				} else {
					frm.set_value("series_preview", __("Failed to generate preview of series"));
				}
			},
		});
	},
});
