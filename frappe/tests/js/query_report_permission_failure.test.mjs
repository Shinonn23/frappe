import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../public/js/frappe/views/reports/query_report.js", import.meta.url), "utf8").replace('import DataTable from "frappe-datatable";', "");
const denied = () => ({ status: 403, responseJSON: { exc_type: "PermissionError" } });

function fixture() {
	let route = ["query-report", "Allowed"];
	const events = [], requests = [], clearedTimers = [];
	const areas = Object.fromEntries(["report", "chart", "summary", "report_message", "status"].map((name) => [name, {
		visible: true, content: "previous data", empty() { this.content = ""; return this; },
		toggle(value) { this.visible = value; return this; }, hide() { this.visible = false; return this; },
	}]));
	let response = () => Promise.resolve({ message: { script: "allowed script", html_format: null } });
	const frappe = {
		provide() {}, views: { BaseList: class {} }, standard_pages: {}, query_reports: {},
		get_route: () => route, get_route_str: () => route.join("/"), route_options: {}, has_route_options: () => false,
		run_serially: (tasks) => tasks.reduce((p, task) => p.then(task), Promise.resolve()),
		model: { with_doc: () => Promise.resolve({ ref_doctype: "Salary Slip", report_type: "Script Report", filters: [] }), with_doctype: () => Promise.resolve() },
		call(options) { requests.push(options); return response(options); },
		xcall(method, args) { return this.call({ method, args }).then((r) => r.message, (r) => Promise.reject(r?.message)); },
		dom: { eval(value) { if (typeof value !== "string") throw Error("Malformed report script"); events.push("script"); } },
		after_ajax(fn) { fn(); },
	};
	vm.runInNewContext(source, { frappe, window: {}, DataTable: class {}, __: (value) => value, clearInterval: (id) => clearedTimers.push(id) });
	const report = Object.create(frappe.views.QueryReport.prototype);
	Object.assign(report, {
		page: Object.fromEntries(["clear_inner_toolbar", "clear_actions", "clear_custom_actions", "clear_menu", "clear_icons", "clear_fields"].map((name) => [name, () => events.push(name)])),
		...Object.fromEntries(Object.entries(areas).map(([name, area]) => ["$" + name, area])),
		get_menu_items: () => [], get_local_report_settings: () => ({ filters: [] }),
		init: () => Promise.resolve(), toggle_message: (flag, message) => events.push(["message", flag, message]),
		refresh_button: { remove: () => events.push("remove_refresh") },
		last_ajax: { abort: () => events.push("abort_old_run") },
		interval: 11, stale_report_interval: 12,
		hide_loading_screen() { this.loading = false; }, loading: true,
		$report_footer: { remove: () => events.push("remove_footer") },
		chart_fields: ["old chart field"], chart_options: { old: true },
	});
	for (const method of ["add_translate_data_checkbox", "setup_progress_bar", "setup_page_head", "add_chart_buttons_to_toolbar", "add_card_button_to_toolbar"]) report[method] = () => events.push(method);
	report.refresh_report = () => { events.push("run"); report.data = ["authorized data"]; report.toggle_report(true); };
	return { report, frappe, areas, events, requests, clearedTimers, setRoute(name) { route = ["query-report", name]; }, setResponse(value) { response = value; } };
}

test("get_script failure retains exact native403 metadata instead of undefined", async () => {
	const f = fixture(), error = denied(); f.report.report_name = "Denied"; f.setResponse(() => Promise.reject(error));
	const actual = await f.report.get_report_settings().then(() => null, (reason) => reason);
	assert.equal(actual, error);
	assert.equal(f.events.includes("script"), false);
});

test("typed permission denial resolves the complete show/load chain without running a report", async () => {
	const f = fixture(); f.setRoute("Denied"); f.setResponse(() => Promise.reject(denied()));
	const completion = f.report.show(); assert.equal(typeof completion?.then, "function"); await completion;
	assert.equal(f.report.report_name, null); assert.equal(f.events.includes("script"), false); assert.equal(f.events.includes("run"), false);
	assert.equal(f.events.includes("setup_page_head"), false);
});

test("allowed to denied to allowed clears old content and recovers on one report instance", async () => {
	const f = fixture(); await f.report.show(); assert.equal(f.events.includes("run"), true);
	f.setRoute("Denied"); f.setResponse(() => Promise.reject(denied())); await f.report.show();
	for (const name of ["report", "chart", "summary", "report_message"]) { assert.equal(f.areas[name].visible, false); assert.equal(f.areas[name].content, ""); }
	assert.equal(f.report.data.length, 0); assert.equal(f.report.raw_data.length, 0); assert.equal(f.report.columns.length, 0);
	assert.equal(f.report.report_settings, null); assert.equal(f.report.chart, null); assert.equal(f.report.filters.length, 0);
	assert.equal(f.report.chart_fields, null); assert.equal(f.report.chart_options, null); assert.equal(f.report.loading, false);
	assert.ok(f.events.includes("remove_footer"));
	assert.ok(f.events.includes("clear_actions") && f.events.includes("clear_menu") && f.events.includes("remove_refresh") && f.events.includes("abort_old_run"));
	assert.ok(f.clearedTimers.includes(11) && f.clearedTimers.includes(12));
	f.setRoute("Allowed again"); f.setResponse(() => Promise.resolve({ message: { script: "allowed script" } })); await f.report.show();
	assert.equal(f.report.report_name, "Allowed again"); assert.equal(f.areas.report.visible, true); assert.equal(f.report.data[0], "authorized data");
});

for (const [label, error] of [["server500", { status: 500 }], ["network", { status: 0 }], ["CSRF403", { status: 403, responseJSON: { exc_type: "CSRFTokenError" } }], ["untyped403", { status: 403 }], ["undefined", undefined]]) {
	test(`${label} failures remain rejected and do not run a report`, async () => {
		const f = fixture(); f.setResponse(() => Promise.reject(error));
		const result = await f.report.show().then(() => ({ resolved: true }), (reason) => ({ resolved: false, reason }));
		assert.equal(result.resolved, false); assert.equal(result.reason, error); assert.equal(f.events.includes("run"), false);
	});
}

test("malformed script response remains a visible rejected failure", async () => {
	const f = fixture(); f.setResponse(() => Promise.resolve({ message: {} }));
	await assert.rejects(f.report.show(), /Malformed report script/); assert.equal(f.events.includes("run"), false);
});

test("a delayed denied load cannot clear a newer allowed report", async () => {
	const f = fixture(); let rejectOld; f.setRoute("Denied old");
	f.setResponse(() => new Promise((resolve, reject) => { rejectOld = reject; })); const old = f.report.show();
	while (!rejectOld) await new Promise((resolve) => setImmediate(resolve));
	f.setRoute("Allowed new"); f.setResponse(() => Promise.resolve({ message: { script: "allowed script" } })); await f.report.show();
	rejectOld(denied()); await old;
	assert.equal(f.report.report_name, "Allowed new"); assert.equal(f.report.data[0], "authorized data"); assert.equal(f.areas.report.visible, true);
});

test("cached allowed report settings retain their normal path", async () => {
	const f = fixture(); const settings = { filters: [] }; f.frappe.query_reports.Allowed = settings;
	await f.report.show(); assert.equal(f.requests.length, 0); assert.equal(f.report.report_settings, settings); assert.equal(f.events.includes("run"), true);
});

test("a denied report can be retried on the same route", async () => {
	const f = fixture(); f.setRoute("Denied"); f.setResponse(() => Promise.reject(denied()));
	await f.report.show(); await f.report.show();
	assert.equal(f.requests.length, 2); assert.equal(f.report.report_name, null); assert.equal(f.events.includes("run"), false);
});
