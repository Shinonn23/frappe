import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../public/js/frappe/form/grid.js", import.meta.url), "utf8");
const getMethod = (start, end) => source.slice(source.indexOf(`\t${start}(`), source.indexOf(`\n\t${end}(`));
const context = { frappe: { model: { layout_fields: [] } } };
const methods = vm.runInNewContext(`({${getMethod("_apply_column_disp_overrides", "refresh_row")},
${getMethod("set_column_disp_in_list_view", "set_editable_grid_column_disp")},
${getMethod("setup_visible_columns", "clamp_column_width")}})`, context);

test("purpose columns override local list flags without changing shared metadata", () => {
	const original = { fieldname: "target_location", in_list_view: 0, hidden: 0 };
	const grid = { docfields: [original], column_disp_overrides: {}, _teardown_column_layout() {}, debounced_refresh() {}, ...methods };
	grid.set_column_disp_in_list_view("target_location", true, true);
	grid._apply_column_disp_overrides();
	assert.equal(grid.docfields[0].in_list_view, 1);
	assert.equal(original.in_list_view, 0);
	assert.equal(grid.required_list_columns.has("target_location"), true);
	grid.set_column_disp_in_list_view("target_location", false);
	assert.equal(grid.required_list_columns.has("target_location"), false);
});

test("required purpose columns survive a user's custom column selection", () => {
	const item = { fieldname: "item_code", in_list_view: 1, parent: "Material Request Item" };
	const target = { fieldname: "target_location", in_list_view: 1, parent: "Material Request Item" };
	const grid = { ...methods, visible_columns: [], docfields: [item, target], fields_map: { item_code: item, target_location: target },
		required_list_columns: new Set(["target_location"]), frm: { get_perm: () => true },
		setup_user_defined_columns() { this.user_defined_columns = [item]; }, get_column_width: () => 100 };
	grid.setup_visible_columns();
	assert.deepEqual(Array.from(grid.visible_columns, ([field]) => field.fieldname), ["item_code", "target_location"]);
});
