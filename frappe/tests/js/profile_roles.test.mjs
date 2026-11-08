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
});
vm.runInContext(
  readFileSync(
    new URL("../../public/js/frappe/profile_roles.js", import.meta.url),
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
