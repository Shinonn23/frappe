import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(
  new URL("../../public/js/frappe/permission_ui_loader.js", import.meta.url),
  "utf8"
);
function load(requireBundle) {
  const events = {};
  const frappe = {
    permission_ui_loader: {},
    provide() {},
    require: () => requireBundle(frappe),
    ui: {
      form: {
        on: (doctype, handlers) => {
          events[doctype] = handlers;
        },
      },
    },
  };
  vm.runInContext(source, vm.createContext({ frappe }));
  return { frappe, events };
}

test("concurrent forms load one permission bundle and failed loads can retry", async () => {
  let calls = 0;
  let finish;
  const { frappe } = load(() => {
    calls++;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  const first = frappe.permission_ui_loader.ensure();
  const second = frappe.permission_ui_loader.ensure();
  assert.equal(calls, 1);
  frappe.permission_ui = { mount() {} };
  finish();
  assert.equal(await first, frappe.permission_ui);
  assert.equal(await second, frappe.permission_ui);
  const retry = load(async (f) => {
    if (!calls++) throw new Error("network unavailable");
    f.permission_ui = { mount() {} };
  }).frappe;
  calls = 0;
  await assert.rejects(
    retry.permission_ui_loader.ensure(),
    /network unavailable/
  );
  assert.equal(await retry.permission_ui_loader.ensure(), retry.permission_ui);
  assert.equal(calls, 2);
});

test("leaving either form invalidates pending mounts and releases embedded Vue dialogs", () => {
  const { frappe, events } = load(() => {});
  const removed = [];
  frappe.permission_ui = { unmount: (target) => removed.push(target) };
  for (const doctype of ["User", "Role Profile"]) {
    const frm = {
      fields_dict: {
        summary: { df: { fieldtype: "HTML" }, $wrapper: doctype },
        input: { df: { fieldtype: "Data" } },
      },
    };
    events[doctype].on_hide(frm);
    assert.equal(frm.__permission_ui_hidden, true);
  }
  assert.deepEqual(removed, ["User", "Role Profile"]);
});
