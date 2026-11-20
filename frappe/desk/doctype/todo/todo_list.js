frappe.listview_settings["ToDo"] = {
	hide_name_column: true,
	add_fields: [
		"reference_type",
		"reference_name",
		"task_kind",
		"task_action",
		"task_doctype",
		"task_docname",
		"status",
		"allocated_to",
	],

	onload: function (me) {
		me.page.set_title(__("To Do"));
	},

	button: {
		show: function (doc) {
			return doc.task_kind ? doc.task_docname : doc.reference_name;
		},
		get_label: function () {
			return __("Open", null, "Access");
		},
		get_description: function (doc) {
			const doctype = doc.task_kind ? doc.task_doctype : doc.reference_type;
			const name = doc.task_kind ? doc.task_docname : doc.reference_name;
			return __("Open {0}", [`${__(doctype)}: ${name}`]);
		},
		action: function (doc) {
			const doctype = doc.task_kind ? doc.task_doctype : doc.reference_type;
			const name = doc.task_kind ? doc.task_docname : doc.reference_name;
			frappe.set_route("Form", doctype, name);
		},
	},
};
