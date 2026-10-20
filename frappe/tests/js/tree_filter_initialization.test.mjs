import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";

const treeSource = fs.readFileSync(new URL("../../public/js/frappe/views/treeview.js", import.meta.url), "utf8");
const accountSource = fs.readFileSync(new URL("../../../../erpnext/erpnext/accounts/doctype/account/account_tree.js", import.meta.url), "utf8");

function fixture({ rootQuery = false } = {}) {
  const fields = {};
  const renders = [];
  const rootQueries = [];
  const page = {
    fields_dict: fields,
    add_field(df) {
      const field = { df, value: df.default || "", get_value() { return this.value; } };
      fields[df.fieldname] = field;
      return field;
    },
    set_title() {}, add_inner_button() {}, add_divider_to_button_group() {},
  };
  function $(selector) {
    const name = selector.match(/data-fieldname='([^']+)'/)[1];
    return { trigger(event) { assert.equal(event, "change"); fields[name].df.change.call(fields[name]); } };
  }
  $.extend = Object.assign;
  $.each = (rows, fn) => rows.forEach((row, i) => fn(i, row));
  const frappe = {
    provide() {}, treeview_settings: {}, views: { Factory: class {}, ListViewSelect: class {} }, flags: {},
    boot: { user: { can_create: ["Account"] } }, meta: { has_field: () => false },
    get_meta() { return { fields: [{ fieldname: "account_type", options: "Bank" }, { fieldname: "account_category", options: "Account Category" }] }; },
    get_route_str() { return "Tree/Account"; },
  };
  const erpnext = { accounts: {}, utils: { get_tree_options: () => ["CD", "UB"], get_tree_default: () => "CD" } };
  const context = vm.createContext({ frappe, erpnext, $, window: {}, __: (s) => s, queueMicrotask });
  vm.runInContext(accountSource, context);
  vm.runInContext(treeSource, context);
  const View = frappe.views.TreeView;
  View.prototype.get_permissions = function () { this.can_create = true; };
  View.prototype.make_page = function () { this.page = page; };
  View.prototype.set_title = () => {};
  View.prototype.set_menu_item = () => {};
  View.prototype.set_primary_action = () => {};
  View.prototype.get_root = function () { rootQueries.push({ ...this.args }); };
  View.prototype.make_tree = function () {
    const allowed = !!frappe.treeview_settings.Account.toolbar[0].condition({ expandable: true, hide_add: false });
    renders.push({ allowed, company: this.args.company });
  };
  const options = { ...frappe.treeview_settings.Account, doctype: "Account", get_tree_root: rootQuery };
  return { create: () => new View(options), fields, renders, rootQueries, frappe };
}

test("default Account filter renders after native onload and all dependent fields exist", async () => {
  const f = fixture();
  f.create();
  assert.equal(f.renders.length, 0);
  assert.ok(f.fields.root_company);
  assert.ok(f.frappe.treeview_settings.Account.treeview.page);
  await Promise.resolve();
  assert.deepEqual(f.renders, [{ allowed: true, company: "CD" }]);
});

test("root queries still receive the company synchronously before deferred rendering", async () => {
  const f = fixture({ rootQuery: true });
  f.create();
  assert.deepEqual(f.rootQueries, [{ doctype: "Account", company: "CD" }]);
  await Promise.resolve();
  assert.equal(f.renders[0].allowed, true);
});

test("later filter changes retain company and root-company creation restrictions", async () => {
  const f = fixture();
  const view = f.create();
  await Promise.resolve();
  f.fields.company.value = "UB";
  f.fields.root_company.value = "Parent Company";
  f.fields.company.df.change.call(f.fields.company);
  assert.equal(view.args.company, "UB");
  await Promise.resolve();
  assert.deepEqual(f.renders.at(-1), { allowed: false, company: "UB" });
});
