// bind events

frappe.ui.form.on("ToDo", {
	onload: function (frm) {
		frm.set_query("reference_type", function (txt) {
			return {
				filters: {
					issingle: 0,
				},
			};
		});
	},
	refresh: function (frm) {
		const ref_type = frm.doc.task_kind ? frm.doc.task_doctype : frm.doc.reference_type;
		const ref_name = frm.doc.task_kind ? frm.doc.task_docname : frm.doc.reference_name;
		if (ref_type && ref_name) {
			frm.add_custom_button(__(ref_name), function () {
				frappe.set_route("Form", ref_type, ref_name);
			});
		}

		if (!frm.doc.__islocal) {
			if (!frm.doc.task_kind) {
				if (frm.doc.status !== "Closed") {
					frm.add_custom_button(
						__("Close"),
						function () {
							frm.set_value("status", "Closed");
							frm.save(null, function () {
								// back to list
								frappe.set_route("List", "ToDo");
							});
						},
						null
					);
				} else {
					frm.add_custom_button(
						__("Reopen"),
						function () {
							frm.set_value("status", "Open");
							frm.save();
						},
						null
					);
				}
			}
			frm.add_custom_button(
				__("New"),
				function () {
					frappe.new_doc("ToDo");
				},
				null,
				"ToDo"
			);
		}
	},
});
