import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// messages.js is bundled as an ES module; drop its imports (the dialog side-effect import and
// has_unsafe_scheme, which only show_alert uses) so the script runs in a plain vm context.
const source = fs
	.readFileSync(new URL("../../public/js/frappe/ui/messages.js", import.meta.url), "utf8")
	.replace(/^import\s[^;]*;$/gm, "");

function fixture(previous = ["List", "Attendance Request", "List"]) {
	let path = "attendance-request/DENIED-001";
	let current = ["Form", "Attendance Request", "DENIED-001"];
	const calls = [];
	const chain = { removeClass() { return this; }, addClass() { return this; }, css() { return this; } };
	const frappe = {
		provide() {}, messages: {}, route_flags: {},
		router: { get_sub_path: () => path },
		get_route: () => current,
		get_prev_route: () => previous,
		set_route: (value) => calls.push(value),
		utils: { replace_newlines: (value) => value },
		msg_dialog: {
			msg_area: { html: () => "", append() {} },
			set_title() {}, show() {}, indicator: chain, $wrapper: chain,
			wrapper: { classList: { contains: () => false, add() {}, remove() {} } },
		},
	};
	vm.runInNewContext(source, { frappe, window: {}, has_unsafe_scheme: () => false, __: (value) => value, $: { isPlainObject: (value) => !!value && typeof value === "object" } });
	return { frappe, calls, previous, setPath(value) { path = value; }, setCurrent(value) { current = value; },
		register() { frappe.msgprint({ message: "Not permitted", re_route: true }); },
		hide() { frappe.msg_dialog.custom_onhide(); } };
}

test("dismissing a denial on its original URL returns to the previous route", () => {
	const f = fixture(); f.register(); f.hide();
	assert.deepEqual(f.calls[0], ["List", "Attendance Request", "List"]);
	assert.equal(f.frappe.route_flags.replace_route, true);
});

test("delayed dismissal keeps the previous route captured when the message appeared", () => {
	const f = fixture(); f.register();
	f.previous.splice(0, f.previous.length, "Form", "Leave Application", "OTHER");
	f.hide();
	assert.deepEqual(f.calls[0], ["List", "Attendance Request", "List"]);
});

for (const routeAlreadyParsed of [false, true]) {
	test(`delayed dismissal cannot override a new URL (new route parsed: ${routeAlreadyParsed})`, () => {
		const f = fixture(); f.register();
		f.setPath("leave-application/view/list");
		if (routeAlreadyParsed) f.setCurrent(["List", "Leave Application", "List"]);
		f.hide();
		assert.equal(f.calls.length, 0);
		assert.equal(Object.hasOwn(f.frappe.route_flags, "replace_route"), false);
	});
}

test("dismissing a denial without route history retains the home fallback", () => {
	const f = fixture([]); f.register(); f.hide();
	assert.deepEqual(f.calls, ["", []]);
	assert.equal(f.frappe.route_flags.replace_route, true);
});

test("a stale callback cannot override a new visit to the same document URL", () => {
	const f = fixture(); f.register();
	f.setCurrent(["Form", "Attendance Request", "DENIED-001"]);
	f.hide();
	assert.equal(f.calls.length, 0);
	assert.equal(Object.hasOwn(f.frappe.route_flags, "replace_route"), false);
});

test("a normal message does not register a navigation callback", () => {
	const f = fixture(); f.frappe.msgprint({ message: "Information" });
	assert.equal(f.frappe.msg_dialog.custom_onhide, undefined);
	assert.equal(f.calls.length, 0);
});
