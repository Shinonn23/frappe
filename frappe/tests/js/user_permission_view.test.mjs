import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(
  new URL("../../public/js/frappe/user_permission_view.js", import.meta.url),
  "utf8"
);
function wrapper() {
  return {
    current: null,
    message: "",
    empty() {
      this.current = null;
      this.message = "";
      return this;
    },
    text(message) {
      this.message = message;
      return this;
    },
  };
}
function load(call = async () => ({ message: {} }), ensure = async () => {}) {
  const context = {
    __: (value) => value,
    frappe: {
      provide() {},
      user_permission_view: {},
      call,
      permission_ui_loader: { ensure },
      permission_ui: {
        mount(target, view, props) {
          assert.equal(view, "user");
          target.message = "";
          target.current = { view, props };
          return target.current;
        },
        unmount(target) {
          target.current = null;
        },
      },
      ui: { form: { on() {} } },
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
const rows = (sections, name) =>
  sections.find((section) => section.name === name)?.rows;
const status = (frm) =>
  frm.fields_dict.profile_permissions_html.$wrapper.message;
const renderedRows = (frm, name) => {
  const props = frm.__user_permission_group.props;
  return rows(load().sections(props.data, props), name);
};

test("workflow conditions and notifications share one readonly item without mutating saved data", () => {
  const data = {
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
  const sections = load().sections(data);
  const workflow = rows(sections, "workflow_permissions");
  assert.equal(workflow.length, 1);
  const row = workflow[0];
  assert.equal(row.document_type, "Payroll Entry");
  assert.equal(row.permission, "Review");
  assert.equal(row.notification, "Stand-in · Both");
  assert.match(row.ceilings, /Amount: 10000.*Percent: Unlimited/);
  assert.match(row.conditions, /Department: <script>/);
  assert.deepEqual([...row.sources], ["Position <A>"]);
  row.sources.push("Another profile");
  assert.ok(!sections.some((section) => /notifications/i.test(section.name)));
  assert.equal(
    JSON.stringify(data),
    before,
    "view data is independent of saved authority"
  );
});

test("separates automatic system roles and hides metadata-managed workflow roles", () => {
  const sections = load().sections({
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
    [...rows(sections, "functional_roles")].map((row) => row.permission),
    ["Sales User"]
  );
  assert.deepEqual(
    [...rows(sections, "system_roles")].map((row) => row.permission),
    ["All", "Desk User"]
  );
  assert.equal(
    sections.find((section) => section.name === "system_roles").collapsed,
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
  const result = rows(load().sections(data), "handoff_assignments");
  assert.equal(result.length, 5);
  assert.deepEqual([...result[0].sources], ["First profile", "Second profile"]);
  assert.ok(result.some((row) => row.notification.includes("Stand-in")));
  assert.ok(result.some((row) => row.conditions.includes("Company B")));
  assert.ok(result.some((row) => row.conditions.includes("Warehouse: Second")));
  assert.ok(result.some((row) => row.permission === "Rule B"));
  assert.equal(JSON.stringify(data), before);
});

test("relation-owned scopes show actual policy instead of ineffective stored values", () => {
  const sections = load().sections({
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
  const row = rows(sections, "workflow_permissions")[0];
  assert.match(
    row.conditions,
    /Manager assigned on Unit.*Named <manager> decides/
  );
  assert.doesNotMatch(
    row.conditions,
    /Obsolete root|Fixed values|Selected values/
  );
  assert.doesNotMatch(row.scope_summary, /Named <manager>/);
});

test("data permission status and source profiles remain explicit", () => {
  const sections = load().sections({
    capabilities: [
      {
        label: "Sees Buying Prices",
        status: "Granted role",
        sources: ["Data"],
      },
    ],
  });
  const row = rows(sections, "data_permissions")[0];
  assert.equal(row.permission, "Sees Buying Prices");
  assert.equal(row.status, "Granted role");
  assert.deepEqual([...row.sources], ["Data"]);
});

test("new and dirty Users show a save notice without requesting or loading the UI", async () => {
  let calls = 0;
  const api = load(
    async () => calls++,
    async () => calls++
  );
  const frm = form();
  frm.dirty = true;
  await api.refresh(frm);
  assert.equal(calls, 0);
  assert.match(status(frm), /Save to refresh permissions/);
  frm.dirty = false;
  frm.is_new = () => true;
  await api.refresh(frm);
  assert.equal(calls, 0);
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
  assert.deepEqual(
    [...renderedRows(frm, "functional_roles")].map((row) => row.permission),
    ["New role"]
  );
});

test("changes while the library loads show a save notice instead of stale permissions", async () => {
  let resolve;
  const api = load(
    async () => ({ message: { roles: [{ role: "Saved role" }] } }),
    () => new Promise((done) => (resolve = done))
  );
  const frm = form();
  const request = api.refresh(frm);
  await new Promise(setImmediate);
  frm.dirty = true;
  resolve();
  await request;
  assert.match(status(frm), /Save to refresh permissions/);
  assert.equal(frm.__user_permission_group, null);
});

test("navigation while the library loads cannot mount the previous User", async () => {
  let resolve;
  const api = load(
    async () => ({ message: { roles: [{ role: "Saved role" }] } }),
    () => new Promise((done) => (resolve = done))
  );
  const frm = form();
  const request = api.refresh(frm);
  await new Promise(setImmediate);
  frm.doc.name = "another";
  resolve();
  await request;
  assert.equal(frm.fields_dict.profile_permissions_html.$wrapper.current, null);
});

test("request or library failure leaves native roles available and readable status", async () => {
  for (const failure of ["request", "library"]) {
    const frm = form();
    const toggles = [];
    frm.toggle_display = (field, visible) => toggles.push([field, visible]);
    await load(
      async () => {
        if (failure === "request") throw new Error("denied");
        return { message: {} };
      },
      async () => {
        if (failure === "library") throw new Error("load failed");
      }
    ).refresh(frm);
    assert.match(status(frm), /Unable to load saved permissions/);
    assert.ok(
      !toggles.some(([field, visible]) => field === "roles_html" && !visible)
    );
  }
});

test("profile users get Vue summary while direct users retain editable native roles", async () => {
  const frm = form();
  const toggles = [];
  frm.toggle_display = (field, visible) => toggles.push([field, visible]);
  const api = load(async () => ({
    message: { roles: [{ role: "Sales User" }] },
  }));
  await api.refresh(frm);
  assert.deepEqual(toggles.at(-1), ["roles_html", false]);
  assert.equal(frm.__user_permission_group.view, "user");
  assert.ok(renderedRows(frm, "functional_roles"));
  frm.doc.role_profiles = [];
  await api.refresh(frm);
  assert.deepEqual(toggles.at(-1), ["roles_html", true]);
  assert.equal(renderedRows(frm, "functional_roles"), undefined);
});

test("an absent provider clears the mount without loading the library", async () => {
  const frm = form();
  const toggles = [];
  frm.toggle_display = (field, visible) => toggles.push([field, visible]);
  await load(
    async (request) => {
      assert.equal(
        request.method,
        "frappe.core.doctype.role_profile.role_profile.get_user_permissions"
      );
      return { message: null };
    },
    async () => assert.fail("absent provider must not load the library")
  ).refresh(frm);
  assert.equal(frm.fields_dict.profile_permissions_html.$wrapper.current, null);
  assert.deepEqual(toggles.at(-1), ["profile_permissions_html", false]);
});

test("level-one read permission is checked before fetch and invalidates pending responses", async () => {
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

test("Vue uses real frappe-ui controls and text interpolation for untrusted values", () => {
  const component = readFileSync(
    new URL(
      "../../public/js/frappe/permission_ui/UserPermissions.vue",
      import.meta.url
    ),
    "utf8"
  );
  assert.match(component, /frappe-ui\/src\/components\/Dialog.vue/);
  assert.match(component, /frappe-ui\/src\/components\/FormControl.vue/);
  assert.match(component, /\{\{ row\.permission \}\}/);
  assert.match(component, /\{\{ value \}\}/);
  assert.match(component, /frappe\.set_route\("Form", "Role Profile", name\)/);
  assert.doesNotMatch(component, /v-html|innerHTML|\.html\(/);
  assert.doesNotMatch(source, /FieldGroup|\.html\(/);
});

test("saved grant display metadata changes presentation while preserving raw state and ID", () => {
  const grant = {
    step: "generic_receipt",
    state: "Pending Inspection",
    label: "Accept / Reject Receipt",
    description:
      "Require submitted <Accepted> inspection only for inspection items.",
    reference_doctype: "Receipt",
    sources: ["Warehouse Duty"],
  };
  const data = {
    grants: [grant, { step: "fallback", state: "Review" }, { step: "unknown" }],
  };
  const before = JSON.stringify(data);
  const result = rows(load().sections(data), "workflow_permissions");
  assert.equal(result[0].permission, grant.label);
  assert.equal(result[0].state, grant.state);
  assert.equal(result[0].permission_id, grant.step);
  assert.equal(result[0].description, grant.description);
  assert.equal(result[1].permission, "Review");
  assert.equal(result[2].permission, "unknown");
  assert.equal(JSON.stringify(data), before);
});

test("User Vue searches display label and original state; description appears in cards and details as text", () => {
  const component = readFileSync(
    new URL(
      "../../public/js/frappe/permission_ui/UserPermissions.vue",
      import.meta.url
    ),
    "utf8"
  );
  const grant = {
    step: "generic_receipt",
    state: "Pending Inspection",
    label: "Accept / Reject Receipt",
    description: "Submitted <Accepted> inspection for required items.",
  };
  const props = { data: { grants: [grant] }, direct_roles: false };
  const script = component
    .split("<script setup>")[1]
    .split("</script>")[0]
    .replace(/^import .*;\n/gm, "");
  const context = {
    defineProps: () => props,
    frappe: { user_permission_view: load() },
    __: (value) => value,
    computed: (getter) => ({
      get value() {
        return getter();
      },
    }),
    ref: (value) => ({ value }),
    watch() {},
  };
  vm.runInNewContext(
    script +
      "\nglobalThis.view = { query, visibleSections, showDetails, detailFields };",
    context
  );
  for (const query of [grant.label, grant.state, grant.description]) {
    context.view.query.value = query;
    const filtered = context.view.visibleSections.value.find(
      (section) => section.name === "workflow_permissions"
    ).filtered;
    assert.equal(filtered.length, 1);
    context.view.showDetails(filtered[0]);
  }
  assert.equal(
    context.view.detailFields.value.find(
      ([label]) => label === "Description"
    )[1],
    grant.description
  );
  assert.match(component, /\{\{ row\.description \}\}/);
  assert.match(component, /\{\{ value \}\}/);
  assert.doesNotMatch(component, /v-html|innerHTML/);
});
