import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../public/js/frappe/views/breadcrumbs.js", import.meta.url), "utf8");
const pageSource = fs.readFileSync(new URL("../../public/js/frappe/ui/page.js", import.meta.url), "utf8");
const method = (start, end) => pageSource.slice(pageSource.indexOf(`\t${start}(`), pageSource.indexOf(`\n\t${end}(`));

const LIST = (doctype) => ({ label: doctype, href: `/desk/${doctype.toLowerCase().replaceAll(" ", "-")}` });

// frappe.breadcrumbs.add() hands its payload to the page on screen, which resolves it on every
// read (page.get_breadcrumbs, loaded from page.js) and paints the resulting items.
function fixture({ doc = null, route = ["Form", "Attendance Request", "DENIED-001"] } = {}) {
	let titleReads = 0;
	const page = {
		renders: [],
		// copy into this realm so deepEqual compares structure, not the vm's Object prototype
		render_breadcrumbs() { this.renders.push(JSON.parse(JSON.stringify(this.get_breadcrumbs()))); },
	};
	const frappe = {
		get_route: () => route,
		get_route_str: () => route.join("/"),
		get_current_page: () => page,
		get_doc: () => doc,
		get_meta: () => ({}),
		user: { has_role: () => true },
		router: { slug: (value) => value.toLowerCase().replaceAll(" ", "-") },
		model: { user_settings: {}, get_doc_title(value) { titleReads++; return value.title || value.name; } },
		utils: { is_html: (value) => /<[^>]+>/.test(value) },
	};
	const context = { frappe, console, __: (value, args) => args ? value.replace("{0}", args[0]) : value,
		strip_html: (value) => value.replace(/<[^>]*>/g, "") };
	vm.runInNewContext(source, context);
	page.get_breadcrumbs = vm.runInNewContext(`({${method("get_breadcrumbs", "render_breadcrumbs")}}).get_breadcrumbs`, context);
	return { b: frappe.breadcrumbs, get items() { return page.renders.at(-1); },
		get titleReads() { return titleReads; }, setDoc(value) { doc = value; }, setRoute(value) { route = value; } };
}

for (const missing of [null, undefined]) {
	for (const view of ["Form", "Print"]) {
		test(`${view} with ${missing} stored document falls back to the docname without reading a title`, () => {
			const f = fixture({ doc: missing, route: [view, "Attendance Request", "DENIED-001"] });
			assert.doesNotThrow(() => f.b.add("HR", "Attendance Request"));
			assert.deepEqual(f.items[0], LIST("Attendance Request"));
			assert.equal(f.items.length, 2);
			assert.equal(f.items[1].label, "DENIED-001");
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
		assert.deepEqual(f.items, [LIST("Leave Application"), { label: "DENIED-001" }]);
		f.setRoute(["List", "Leave Application"]);
		f.b.add("HR", "Leave Application");
		assert.deepEqual(f.items, [LIST("Leave Application")]);
	}
	assert.equal(f.titleReads, 0);
});

test("a delayed authorized document gains its title on the next update", () => {
	const f = fixture();
	f.b.add("HR", "Attendance Request");
	assert.equal(f.items.at(-1).label, "DENIED-001");
	f.setDoc({ name: "DENIED-001", title: "Allowed title" });
	f.b.update();
	assert.equal(f.items.at(-1).label, "Allowed title");
	assert.equal(f.titleReads, 1);
});

for (const view of ["Form", "Print"]) {
	test(`loaded ${view} preserves title stripping and encoded document route`, () => {
		const f = fixture({ doc: { name: "A/B", title: "<b>Allowed title</b>" }, route: [view, "Lead", "A", "B"] });
		f.b.add("CRM", "Lead");
		// the form crumb is the page itself, so only the print view links back to the form
		const expected = view === "Form" ? { label: "Allowed title" } : { label: "Allowed title", href: "/desk/lead/A%2FB" };
		assert.deepEqual(f.items, [LIST("Lead"), expected]);
	});
}

test("a new document retains its synthetic title before it is cached", () => {
	const f = fixture({ route: ["Form", "Attendance Request", "new-attendance-request-1"] });
	f.b.add("HR", "Attendance Request");
	assert.equal(f.items.at(-1).label, "New Attendance Request");
	assert.equal(f.titleReads, 0);
});
