import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(
  new URL("../../public/js/frappe/user_permission_view.js", import.meta.url),
  "utf8"
);
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[
        c
      ])
  );
function wrapper() {
  return {
    current: null,
    clears: 0,
    empty() {
      this.current = null;
      this.clears++;
      return this;
    },
  };
}
function load(call = async () => ({ message: {} })) {
  class FieldGroup {
    constructor(options) {
      Object.assign(this, options);
      this.parent.current = this;
      this.fields_dict = {};
      this.sections = [];
    }
    make() {
      for (const df of this.fields) {
        this.fields_dict[df.fieldname] = { df, value: df.default };
        if (df.fieldtype === "Section Break") {
          this.sections.push({
            df,
            collapse(hidden) {
              this.collapsed = hidden;
            },
          });
        }
      }
    }
  }
  const context = {
    __: (value) => value,
    frappe: {
      provide() {},
      user_permission_view: {},
      call,
      utils: { escape_html: escape },
      ui: { FieldGroup, form: { on() {} } },
    },
  };
  vm.runInContext(source, vm.createContext(context));
  return context.frappe.user_permission_view;
}
function form(name = "holder") {
  return {
    doc: { name, role_profiles: [{ role_profile: "Position" }] },
    dirty: false,
    fields_dict: { profile_permissions_html: { $wrapper: wrapper() } },
    is_new: () => false,
    is_dirty() {
      return this.dirty;
    },
  };
}
const table = (group, name) => group.fields_dict[name]?.df;
const status = (frm) =>
  frm.fields_dict.profile_permissions_html.$wrapper.current?.fields_dict
    .permission_status.value;
function render(data, options) {
  return load().render(data, wrapper(), options);
}

test("uses native readonly grids and one workflow row for conditions and notifications", () => {
  const data = {
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
  };
  const before = JSON.stringify(data);
  const group = render(data);
  const grid = table(group, "workflow_permissions");
  assert.equal(grid.fieldtype, "Table");
  assert.equal(grid.data.length, 1);
  const row = grid.data[0];
  assert.equal(row.document_type, "Payroll Entry");
  assert.equal(row.permission, "Review");
  assert.equal(row.notification, "Stand-in · Both");
  assert.match(row.ceilings, /Amount: 10000.*Percent: Unlimited/);
  assert.match(row.conditions, /Department: <script>/);
  assert.equal(row.sources, "Position <A>");
  assert.ok(!group.fields.some((df) => /notifications/i.test(df.fieldname)));
  for (const df of group.fields.filter((df) => df.fieldtype === "Table")) {
    assert.equal(df.read_only, 1);
    assert.equal(df.cannot_add_rows, 1);
    assert.equal(df.cannot_delete_rows, 1);
    assert.equal(
      df.label,
      "",
      "the section already supplies the table heading"
    );
    const nativeGrid = {};
    df.on_setup(nativeGrid);
    assert.equal(
      nativeGrid.static_rows,
      true,
      "standalone grids cannot reorder rows"
    );
    for (const column of df.fields) {
      assert.equal(column.read_only, 1);
      assert.equal(column.formatter("<script>"), "&lt;script&gt;");
    }
  }
  assert.equal(JSON.stringify(data), before, "viewing never mutates authority");
  assert.deepEqual(Object.keys(group.doc), []);
  assert.ok(!group.fields.some((df) => df.fieldtype === "HTML"));
  assert.doesNotMatch(source, /<table|style=|\.html\(/);
});

test("separates automatic system roles and hides metadata-managed workflow roles", () => {
  const group = render({
    roles: [
      { role: "Sales User" },
      { role: "All" },
      { role: "Desk User" },
      { role: "Approve Orders" },
      { role: "Approve Returns" },
    ],
    managed_roles: ["Approve Orders", { role: "Approve Returns" }],
    implicit_roles: ["All", "Desk User"],
  });
  assert.deepEqual(
    [...table(group, "functional_roles").data].map((row) => row.role),
    ["Sales User"]
  );
  assert.deepEqual(
    [...table(group, "system_roles").data].map((row) => row.role),
    ["All", "Desk User"]
  );
  assert.equal(
    group.sections.find(
      (section) => section.df.fieldname === "system_roles_section"
    ).collapsed,
    true
  );
});

test("exact duplicate handoffs combine sources without broadening other assignments", () => {
  const base = {
    step: "Receive goods",
    handoff_rule: "Rule A",
    reference_doctype: "Purchase Order",
    company: "Company A",
    scope_source: "Fixed values",
    scope_values: [["Warehouse", "First"]],
    ask_mode: "Ask",
    sources: ["First profile"],
  };
  const data = {
    handoffs: [
      base,
      { ...base, sources: ["Second profile"] },
      { ...base, ask_mode: "Stand-in" },
      { ...base, company: "Company B" },
      { ...base, scope_values: [["Warehouse", "Second"]] },
      { ...base, handoff_rule: "Rule B" },
    ],
  };
  const before = JSON.stringify(data);
  const rows = table(render(data), "handoff_assignments").data;
  assert.equal(rows.length, 5);
  assert.equal(rows[0].sources, "First profile, Second profile");
  assert.ok(rows.some((row) => row.notification.includes("Stand-in")));
  assert.ok(rows.some((row) => row.conditions.includes("Company B")));
  assert.ok(rows.some((row) => row.conditions.includes("Warehouse: Second")));
  assert.ok(rows.some((row) => row.permission === "Rule B"));
  assert.equal(JSON.stringify(data), before);
});

test("relation-owned scopes show the actual policy and omit ineffective stored scopes", () => {
  const group = render({
    grants: [
      {
        step: "generic",
        company: "Company A",
        scope_kind: "Department",
        scope_source: "Fixed values",
        scope_values: [["Department", "Obsolete root"]],
        scope_policy: "Manager assigned on Unit",
        note: "Named <manager> decides.",
      },
    ],
  });
  const grid = table(group, "workflow_permissions");
  const row = grid.data[0];
  assert.match(
    row.conditions,
    /Manager assigned on Unit.*Named <manager> decides/
  );
  assert.doesNotMatch(
    row.conditions,
    /Obsolete root|Fixed values|Selected values/
  );
  assert.doesNotMatch(row.scope_summary, /Named <manager>/);
  assert.match(
    grid.fields
      .find((field) => field.fieldname === "conditions")
      .formatter(row.conditions),
    /Named &lt;manager&gt;/
  );
});

test("data permission status, sources and Administrator exception remain explicit", () => {
  const group = render({
    administrator: true,
    capabilities: [
      {
        label: "Sees Buying Prices",
        status: "Granted role",
        sources: ["Data"],
      },
    ],
  });
  assert.match(
    group.fields_dict.permission_status.value,
    /do not limit Administrator/
  );
  const row = table(group, "data_permissions").data[0];
  assert.equal(row.permission, "Sees Buying Prices");
  assert.equal(row.status, "Granted role");
  assert.equal(row.sources, "Data");
});

test("unsaved changes use a native save notice and make no request", async () => {
  let calls = 0;
  const api = load(async () => calls++);
  const frm = form();
  frm.dirty = true;
  await api.refresh(frm);
  assert.equal(calls, 0);
  assert.match(status(frm), /Save to refresh permissions/);
});

test("a stale response cannot replace the summary for a newer User", async () => {
  const pending = [];
  const api = load(() => new Promise((resolve) => pending.push(resolve)));
  const frm = form("first");
  const first = api.refresh(frm);
  frm.doc.name = "second";
  const second = api.refresh(frm);
  pending[1]({ message: { roles: [{ role: "New role" }] } });
  await second;
  pending[0]({ message: { roles: [{ role: "Old role" }] } });
  await first;
  const rows = table(frm.__user_permission_group, "functional_roles").data;
  assert.deepEqual(
    [...rows].map((row) => row.role),
    ["New role"]
  );
});

test("changes during a request show save notice instead of stale permissions", async () => {
  let resolve;
  const api = load(() => new Promise((done) => (resolve = done)));
  const frm = form();
  const request = api.refresh(frm);
  frm.dirty = true;
  resolve({ message: { roles: [{ role: "Saved role" }] } });
  await request;
  assert.match(status(frm), /Save to refresh permissions/);
  assert.equal(
    table(frm.__user_permission_group, "functional_roles"),
    undefined
  );
});

test("request failure leaves native roles visible and a readable native status", async () => {
  const frm = form();
  const toggles = [];
  frm.toggle_display = (field, visible) => toggles.push([field, visible]);
  await load(async () => {
    throw new Error("denied");
  }).refresh(frm);
  assert.match(status(frm), /Unable to load saved permissions/);
  assert.ok(
    !toggles.some(([field, visible]) => field === "roles_html" && !visible)
  );
});

test("profile users get native summary while direct users retain their editable role editor", async () => {
  const frm = form();
  const toggles = [];
  frm.toggle_display = (field, visible) => toggles.push([field, visible]);
  const api = load(async () => ({
    message: { roles: [{ role: "Sales User" }] },
  }));
  await api.refresh(frm);
  assert.deepEqual(toggles.at(-1), ["roles_html", false]);
  assert.ok(table(frm.__user_permission_group, "functional_roles"));
  frm.doc.role_profiles = [];
  await api.refresh(frm);
  assert.deepEqual(toggles.at(-1), ["roles_html", true]);
  assert.equal(
    table(frm.__user_permission_group, "functional_roles"),
    undefined
  );
});

test("an absent provider clears the mount and leaves native roles available", async () => {
  const frm = form();
  const toggles = [];
  frm.toggle_display = (field, visible) => toggles.push([field, visible]);
  await load(async (request) => {
    assert.equal(
      request.method,
      "frappe.core.doctype.role_profile.role_profile.get_user_permissions"
    );
    return { message: null };
  }).refresh(frm);
  assert.equal(frm.fields_dict.profile_permissions_html.$wrapper.current, null);
  assert.deepEqual(toggles.at(-1), ["profile_permissions_html", false]);
  assert.ok(
    !toggles.some(([field, visible]) => field === "roles_html" && !visible)
  );
});

test("level-one read permission is checked before fetching and invalidates pending responses", async () => {
  const pending = [];
  const api = load(() => new Promise((resolve) => pending.push(resolve)));
  const frm = form();
  frm.get_perm = () => true;
  const request = api.refresh(frm);
  frm.get_perm = () => false;
  await api.refresh(frm);
  pending[0]({ message: { roles: [{ role: "Secret role" }] } });
  await request;
  assert.equal(pending.length, 1);
  assert.equal(frm.fields_dict.profile_permissions_html.$wrapper.current, null);
});
