import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../public/js/frappe/views/breadcrumbs.js", import.meta.url), "utf8");

function fixture({ doc = null, route = ["Form", "Attendance Request", "DENIED-001"], mobile = false } = {}) {
	const entries = [];
	let titleReads = 0;
	const chain = { parent() { return this; }, addClass() { return this; }, find() { return this; } };
	const frappe = {
		get_route: () => route,
		get_route_str: () => route.join("/"),
		get_doc: () => doc,
		router: { slug: (value) => value.toLowerCase().replaceAll(" ", "-") },
		model: { get_doc_title(value) { titleReads++; return value.title || value.name; } },
		utils: { is_html: (value) => /<[^>]+>/.test(value) },
		is_mobile: () => mobile,
	};
	vm.runInNewContext(source, { frappe, __: (value, args) => args ? value.replace("{0}", args[0]) : value,
		strip_html: (value) => value.replace(/<[^>]*>/g, "") });
	const b = frappe.breadcrumbs;
	b.clear = () => { entries.length = 0; };
	b.toggle = () => {};
	b.set_workspace_breadcrumb = () => {};
	b.set_list_breadcrumb = ({ doctype }) => entries.push({ list: doctype });
	b.append_breadcrumb_element = (route, label) => entries.push({ route, label });
	b.$breadcrumbs = { find: () => chain };
	return { b, entries, get titleReads() { return titleReads; }, setDoc(value) { doc = value; }, setRoute(value) { route = value; } };
}

for (const missing of [null, undefined]) {
	for (const view of ["Form", "Print"]) {
		test(`${view} with ${missing} stored document retains only its list breadcrumb`, () => {
			const f = fixture({ doc: missing, route: [view, "Attendance Request", "DENIED-001"] });
			f.setDoc(missing);
			assert.doesNotThrow(() => f.b.add("HR", "Attendance Request"));
			assert.deepEqual(f.entries, [{ list: "Attendance Request" }]);
			assert.equal(f.titleReads, 0);
		});
	}
}

test("same-page denied Form to another List can initialize repeatedly while the route is pending", () => {
	const f = fixture();
	for (let i = 0; i < 3; i++) {
		f.setRoute(["Form", "Attendance Request", "DENIED-001"]);
		f.setDoc(null);
		assert.doesNotThrow(() => f.b.add("HR", "Leave Application"));
		assert.equal(f.entries.some((e) => "label" in e), false);
		f.setRoute(["List", "Leave Application"]);
		f.b.add("HR", "Leave Application");
		assert.deepEqual(f.entries, [{ list: "Leave Application" }]);
	}
	assert.equal(f.titleReads, 0);
});

test("a delayed authorized document gains its title on the next update", () => {
	const f = fixture();
	f.b.add("HR", "Attendance Request");
	f.setDoc({ name: "DENIED-001", title: "Allowed title" });
	f.b.update();
	assert.equal(f.entries.at(-1).label, "Allowed title");
	assert.equal(f.titleReads, 1);
});

for (const view of ["Form", "Print"]) {
	test(`loaded ${view} preserves title stripping and encoded document route on mobile`, () => {
		const f = fixture({ doc: { name: "A/B", title: "<b>Allowed title</b>" }, route: [view, "Lead", "A", "B"], mobile: true });
		f.b.add("CRM", "Lead");
		assert.deepEqual(f.entries.at(-1), { route: "/desk/lead/A%2FB", label: "Allowed title" });
	});
}

test("a new document retains its synthetic title before it is cached", () => {
	const f = fixture({ route: ["Form", "Attendance Request", "new-attendance-request-1"] });
	f.b.add("HR", "Attendance Request");
	assert.equal(f.entries.at(-1).label, "New Attendance Request");
	assert.equal(f.titleReads, 0);
});
