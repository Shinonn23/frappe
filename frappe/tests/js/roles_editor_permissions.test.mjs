import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

function editor(loaded = true) {
  const removed = [];
  const frappe = {
    model: { clear_doc: (_doctype, name) => removed.push(name) },
  };
  vm.runInNewContext(
    readFileSync(
      new URL("../../public/js/frappe/roles_editor.js", import.meta.url),
      "utf8"
    ),
    { frappe }
  );
  const instance = Object.create(frappe.RoleEditor.prototype);
  Object.assign(instance, {
    roles_loaded: loaded,
    managed_roles: new Set(["Workflow Role"]),
    role_fieldname: "role",
    table_fieldname: "roles",
    frm: {
      doc: {
        roles: [
          { name: "workflow", doctype: "Has Role", role: "Workflow Role" },
          { name: "sales", doctype: "Has Role", role: "Sales User" },
        ],
      },
    },
    multicheck: { get_checked_options: () => [] },
  });
  return { instance, removed };
}

test("saving ordinary roles preserves hidden workflow role assignments", () => {
  const { instance, removed } = editor();
  instance.set_roles_in_table();
  assert.deepEqual(removed, ["sales"]);
});

test("saving before role metadata loads cannot clear existing roles", () => {
  const { instance, removed } = editor(false);
  instance.set_roles_in_table();
  assert.deepEqual(removed, []);
});
