import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(
	new URL("../../public/js/frappe/user_permission_view.js", import.meta.url),
	"utf8"
);
function load(call = async () => ({ message: {} })) {
	const context = {
		frappe: {
			provide() {},
			user_permission_view: {},
			call,
			ui: { form: { on() {} } },
		},
	};
	vm.runInContext(source, vm.createContext(context));
	return context.frappe.user_permission_view;
}
function form(name = "holder") {
	const writes = [];
	return {
		writes,
		doc: { name },
		dirty: false,
		fields_dict: {
			profile_permissions_html: { $wrapper: { html: (value) => writes.push(value) } },
		},
		is_new: () => false,
		is_dirty() {
			return this.dirty;
		},
	};
}

test("renders all workflow families, sources, ceilings and notification routing safely", () => {
	const html = load().render({
		profiles: [{ name: "Position <A>" }],
		roles: [{ role: "Sales User", sources: ["Position <A>"] }],
		grants: [
			{
				step: "payroll",
				reference_doctype: "Payroll Entry",
				flow: "HR",
				state: "Review",
				limit_kind: "Amount+Percent",
				max_amount: 10000,
				max_percent: null,
				scope_kind: "Department",
				scope_source: "Fixed values",
				scope_values: [["Department", "<script>"]],
				sources: ["Position <A>"],
				ask_mode: "Stand-in",
				channel: "Both",
			},
		],
	});
	assert.match(html, /Payroll Entry/);
	assert.match(html, /Amount: 10000/);
	assert.match(html, /Percent: Unlimited/);
	assert.match(html, /Stand-in/);
	assert.match(html, /Both/);
	assert.match(html, /Position &lt;A&gt;/);
	assert.match(html, /&lt;script&gt;/);
	assert.doesNotMatch(html, /<script>/);
});

test("unsaved changes ask to save and make no request", async () => {
	let calls = 0;
	const api = load(async () => {
		calls++;
	});
	const frm = form();
	frm.dirty = true;
	await api.refresh(frm);
	assert.equal(calls, 0);
	assert.match(frm.writes.at(-1), /Save to refresh permissions/);
});

test("a stale response cannot replace a newer user summary", async () => {
	const pending = [];
	const api = load(() => new Promise((resolve) => pending.push(resolve)));
	const frm = form("first");
	const first = api.refresh(frm);
	frm.doc.name = "second";
	const second = api.refresh(frm);
	pending[1]({ message: { profiles: [{ name: "New profile" }] } });
	await second;
	pending[0]({ message: { profiles: [{ name: "Old profile" }] } });
	await first;
	assert.match(frm.writes.at(-1), /New profile/);
	assert.doesNotMatch(frm.writes.at(-1), /Old profile/);
});

test("changes during a request show save notice instead of stale saved permissions", async () => {
	let resolve;
	const api = load(
		() =>
			new Promise((done) => {
				resolve = done;
			})
	);
	const frm = form();
	const request = api.refresh(frm);
	frm.dirty = true;
	resolve({ message: {} });
	await request;
	assert.match(frm.writes.at(-1), /Save to refresh permissions/);
});

test("request failure leaves a readable error state", async () => {
	const frm = form();
	await load(async () => {
		throw new Error("denied");
	}).refresh(frm);
	assert.match(frm.writes.at(-1), /Unable to load saved permissions/);
});

test("data permissions, administrator exception and configured handoffs are explicit", () => {
	const html = load().render({
		administrator: true,
		capabilities: [{ label: "Sees Buying Prices", status: "Granted role", sources: ["Data"] }],
		handoffs: [
			{ step: "Make invoice", handoff_rule: "<unsafe>", ask_mode: "Ask", sources: ["Accounts"] },
		],
	});
	assert.match(html, /Functional Roles/);
	assert.match(html, /Data Permissions/);
	assert.match(html, /Granted role/);
	assert.match(html, /do not limit Administrator/);
	assert.match(html, /Handoff assignments/);
	assert.match(html, /&lt;unsafe&gt;/);
});

test("profile summary replaces disabled native list but direct roles stay editable", async () => {
	const frm = form();
	const toggles = [];
	frm.toggle_display = (field, visible) => toggles.push([field, visible]);
	frm.doc.role_profiles = [{ role_profile: "Position" }];
	await load().refresh(frm);
	assert.deepEqual(toggles.at(-1), ["roles_html", false]);
	frm.doc.role_profiles = [];
	await load().refresh(frm);
	assert.deepEqual(toggles.at(-1), ["roles_html", true]);
});

test("managed workflow roles never appear among functional roles", () => {
	const html = load().render({
		roles: [{ role: "Sales User" }, { role: "Approve Orders" }, { role: "Approve Returns" }],
		managed_roles: ["Approve Orders", { role: "Approve Returns" }],
	});
	assert.match(html, /Sales User/);
	assert.doesNotMatch(html, /Approve Orders|Approve Returns/);
});

test("native endpoint may have no permission provider and leaves native roles visible", async () => {
	const frm = form();
	frm.doc.role_profiles = [{ role_profile: "Position" }];
	const toggles = [];
	frm.toggle_display = (field, visible) => toggles.push([field, visible]);
	await load(async (request) => {
		assert.equal(
			request.method,
			"frappe.core.doctype.role_profile.role_profile.get_user_permissions"
		);
		return { message: null };
	}).refresh(frm);
	assert.equal(frm.writes.at(-1), "");
	assert.deepEqual(toggles.at(-1), ["profile_permissions_html", false]);
	assert.ok(!toggles.some(([field, visible]) => field === "roles_html" && !visible));
});

test("denied provider never hides the native role editor", async () => {
	const frm = form();
	frm.doc.role_profiles = [{ role_profile: "Position" }];
	const toggles = [];
	frm.toggle_display = (field, visible) => toggles.push([field, visible]);
	await load(async () => {
		throw new Error("denied");
	}).refresh(frm);
	assert.match(frm.writes.at(-1), /Unable to load saved permissions/);
	assert.ok(!toggles.some(([field, visible]) => field === "roles_html" && !visible));
});
