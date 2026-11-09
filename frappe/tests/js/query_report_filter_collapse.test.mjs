import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../public/js/frappe/views/reports/query_report.js", import.meta.url), "utf8");

for (const separateChecks of [false, true]) {
	test(`report filters expand and collapse with separate checkbox area: ${separateChecks}`, () => {
		const frappe = { provide() {}, views: { BaseList: class {} }, standard_pages: {} };
		vm.runInNewContext(source.replace(/^import .*;$/gm, ""), { frappe, window: {}, DataTable: class {}, $: (value) => value });
		const report = Object.create(frappe.views.QueryReport.prototype);
		let hidden = false;
		let display;
		let icon;
		Object.assign(report, {
			filters_hidden: true,
			filter_row_length: 1,
			filters: [{ wrapper: {} }, { wrapper: {
				addClass(name) { assert.equal(name, "hidden"); hidden = true; },
				removeClass(name) { assert.equal(name, "hidden"); hidden = false; },
			} }],
			$collapse_button: { find: () => ({ attr: (name, value) => { icon = value; } }) },
		});
		if (separateChecks) report.check_filter_area = {
			css(name, value) { assert.equal(name, "display"); display = value; },
		};
		for (let cycle = 0; cycle < 2; cycle++) {
			report.toggle_filter_visiblity();
			assert.equal(hidden, true);
			assert.equal(icon, "#icon-chevron-down");
			if (separateChecks) assert.equal(display, "none");
			report.toggle_filter_visiblity();
			assert.equal(hidden, false);
			assert.equal(icon, "#icon-chevron-up");
			if (separateChecks) assert.equal(display, "flex");
		}
	});
}
