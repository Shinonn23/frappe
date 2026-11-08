import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const events = {};
const context = vm.createContext({
  frappe: {
    profile_roles: {},
    provide() {},
    ui: {
      form: {
        on: (_doctype, handlers) => Object.assign(events, handlers),
      },
    },
  },
  __: (value) => value,
  console,
});
vm.runInContext(
  readFileSync(
    new URL("../../public/js/frappe/profile_roles.js", import.meta.url),
    "utf8"
  ),
  context
);
vm.runInContext(
  readFileSync(
    new URL("../../core/doctype/role_profile/role_profile.js", import.meta.url),
    "utf8"
  ),
  context
);
const api = context.frappe.profile_roles;
const catalog = {
  roles: ["Sales User", "Purchase Price Viewer", "Approver: Quotation"],
  managed_roles: ["Approver: Quotation", "Custom Workflow Role"],
  flags: [
    {
      fieldname: "sees_buying_prices",
      label: "Sees Buying Prices",
      role: "Purchase Price Viewer",
    },
  ],
};

test("bundle validation flushes role choices before setting backing flags", async () => {
  const doc = { roles: [] };
  const values = {};
  await events.validate({
    doc,
    __permission_catalog: catalog,
    roles_editor: {
      set_roles_in_table() {
        doc.roles = [{ role: "Purchase Price Viewer" }];
      },
    },
    set_value: async (key, value) => {
      values[key] = value;
    },
  });
  assert.equal(values.sees_buying_prices, 1);
});

test("general roles render once and exclude all metadata-managed workflow roles", () => {
  const rows = api.options(
    {
      sees_buying_prices: 1,
      roles: [{ role: "Custom Role" }, { role: "Custom Workflow Role" }],
    },
    catalog
  );
  assert.equal(rows.length, 3);
  assert.equal(
    rows.find((row) => row.value === "Purchase Price Viewer").checked,
    true
  );
  assert.equal(
    rows.find((row) => row.value === "Purchase Price Viewer").label,
    "Sees Buying Prices"
  );
  assert.equal(
    rows.find((row) => row.value === "Approver: Quotation"),
    undefined
  );
  assert.equal(
    rows.find((row) => row.value === "Custom Workflow Role"),
    undefined
  );
  assert.equal(rows.find((row) => row.value === "Custom Role").checked, true);
});

test("native role changes set and clear the matching server flags", () => {
  const values = {};
  const frm = {
    doc: { roles: [{ role: "Purchase Price Viewer" }] },
    set_value: (key, value) => {
      values[key] = value;
    },
  };
  api.sync_flags(frm, catalog);
  assert.equal(values.sees_buying_prices, 1);
  frm.doc.roles = [];
  api.sync_flags(frm, catalog);
  assert.equal(values.sees_buying_prices, 0);
});

function editableForm() {
  const doc = {
    roles: [{ role: "Custom Workflow Role" }, { role: "Custom Role" }],
  };
  return {
    doc,
    __permission_catalog: catalog,
    refresh_field() {},
    dirty() {
      this.changed = true;
    },
    add_child(field, row) {
      doc[field].push(row);
      return row;
    },
    set_value: async (key, value) => {
      doc[key] = value;
    },
  };
}
context.frappe.profile_permissions = { writable: (frm) => !frm.read_only };

test("Vue choices preserve managed roles and keep capability roles and flags aligned", async () => {
  const frm = editableForm();
  assert.equal(await api.set_role(frm, "Purchase Price Viewer", true), true);
  assert.equal(frm.doc.sees_buying_prices, 1);
  assert.ok(frm.doc.roles.some((row) => row.role === "Custom Workflow Role"));
  assert.ok(frm.doc.roles.some((row) => row.role === "Custom Role"));
  assert.equal(frm.changed, true);
  assert.equal(await api.set_role(frm, "Purchase Price Viewer", false), true);
  assert.equal(frm.doc.sees_buying_prices, 0);
  assert.ok(!frm.doc.roles.some((row) => row.role === "Purchase Price Viewer"));
  assert.deepEqual(
    frm.doc.roles.map((row) => row.idx),
    [1, 2]
  );
});

test("read-only or forged role choices cannot change saved authority", async () => {
  const frm = editableForm();
  const before = JSON.stringify(frm.doc);
  assert.equal(await api.set_role(frm, "Approver: Quotation", true), false);
  assert.equal(await api.set_role(frm, "Uncatalogued New Role", true), false);
  frm.read_only = true;
  assert.equal(await api.set_role(frm, "Sales User", true), false);
  assert.equal(JSON.stringify(frm.doc), before);
});

test("additional flag changes are restricted to catalog flags without roles", async () => {
  const frm = editableForm();
  frm.__permission_catalog = {
    ...catalog,
    flags: [
      ...catalog.flags,
      { fieldname: "reads_payroll", label: "Reads Payroll", role: "" },
    ],
  };
  assert.equal(await api.set_flag(frm, "reads_payroll", true), true);
  assert.equal(frm.doc.reads_payroll, 1);
  assert.equal(await api.set_flag(frm, "sees_buying_prices", true), false);
  assert.equal(await api.set_flag(frm, "arbitrary_field", true), false);
});
