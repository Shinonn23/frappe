import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(
  new URL(
    "../../public/js/frappe/role_profile_permissions.js",
    import.meta.url
  ),
  "utf8"
);
const step = {
  step_id: "generic_approval",
  kind: "Approval",
  flow: "Service",
  reference_doctype: "Issue",
  state: "Pending Approval",
  limit_kind: "Amount+Percent",
  allowed_scopes: ["Department", "Product Group"],
  default_channel: "Both",
};
function wrapper() {
  return {
    value: "",
    handlers: {},
    html(value) {
      this.value = value;
      return this;
    },
    append(value) {
      this.value += value;
      return this;
    },
    off() {
      return this;
    },
    on(event, selector, callback) {
      this.handlers[event] = callback;
      return this;
    },
  };
}
function load(write = true) {
  const namespace = { profile_permissions: {} };
  const dialogs = [];
  const errors = [];
  class Dialog {
    constructor(options) {
      this.options = options;
      this.values = {};
      this.fields_dict = {};
      for (const field of options.fields) {
        this.fields_dict[field.fieldname] = { df: field, $wrapper: wrapper() };
        if (field.fieldtype === "MultiSelectPills") field.get_data("");
      }
      this.defaults_ready = Promise.resolve().then(() =>
        this.set_values(
          Object.fromEntries(
            options.fields
              .filter((field) => field.default !== undefined)
              .map((field) => [field.fieldname, field.default])
          )
        )
      );
      dialogs.push(this);
    }
    get_value(key) {
      return this.values[key];
    }
    async set_value(key, value) {
      await Promise.resolve();
      this.values[key] = value;
      await this.fields_dict[key].df.onchange?.();
    }
    set_values(values) {
      return Promise.all(
        Object.entries(values).map(([key, value]) => this.set_value(key, value))
      );
    }
    set_df_property(key, property, value) {
      this.fields_dict[key].df[property] = value;
    }
    show() {}
    hide() {
      this.hidden = true;
    }
  }
  let hooks;
  const frappe = {
    profile_permissions: namespace.profile_permissions,
    provide() {},
    ui: {
      Dialog,
      form: {
        on: (doctype, events) => {
          if (doctype === "Role Profile") hooks = events;
        },
      },
    },
    utils: { get_random: () => "new-key" },
    msgprint: (message) => errors.push(message),
    confirm: (_, yes) => yes(),
    db: {
      get_link_options: (doctype, txt) => [{ value: `${doctype}:${txt}` }],
    },
  };
  vm.runInNewContext(source, {
    frappe,
    __: (text) => text,
    console: { error() {} },
  });
  const frm = {
    perm: [{ write }],
    doc: { approval_grants: [], approval_scope_values: [] },
    fields_dict: {
      workflow_permissions_html: { $wrapper: wrapper() },
      notifications_html: { $wrapper: wrapper() },
    },
    __permission_catalog: {
      steps: [step],
      flags: [],
      step_doctype: "Test Approval Step",
    },
    refresh_field() {},
    dirty() {
      this.dirty_count = (this.dirty_count || 0) + 1;
    },
    add_child(field, values = {}) {
      const row = { ...values, idx: this.doc[field].length + 1 };
      this.doc[field].push(row);
      return row;
    },
  };
  return {
    api: namespace.profile_permissions,
    frm,
    dialogs,
    errors,
    frappe,
    refresh: (form) => hooks.refresh(form),
  };
}
const values = {
  step: step.step_id,
  company: "Company A",
  scope_kind: "Department",
  scope_source: "Fixed values",
  fixed_values: ["Laboratory"],
  max_amount: 500000,
  max_percent: 10,
  include_descendants: 1,
};

test("catalog scope kinds control explicit choices and fixed scopes cannot be empty", () => {
  const { api } = load();
  assert.deepEqual(Array.from(api.scope_sources("Department")), [
    "Fixed values",
    "My Department",
    "Departments I head",
  ]);
  assert.equal(api.validate(values, step), "");
  assert.match(
    api.validate({ ...values, fixed_values: [] }, step),
    /at least one/
  );
  assert.match(
    api.validate({ ...values, scope_kind: "Warehouse" }, step),
    /allowed scope/
  );
  assert.match(
    api.validate({ ...values, scope_source: "All" }, step),
    /comes from/
  );
  assert.match(
    api.validate({ ...values, max_percent: 101 }, step),
    /cannot exceed/
  );
  assert.match(
    api.validate({ ...values, max_amount: -1 }, step),
    /non-negative/
  );
  assert.equal(
    api.validate({ ...values, max_amount: 0, max_percent: 0 }, step),
    ""
  );
});

test("apply uses keyed scope siblings and leaves other authority and handoff values alone", () => {
  const { api, frm } = load();
  frm.doc.approval_scope_values.push({
    rule_key: "handoff",
    value: "Preserved",
    idx: 1,
  });
  assert.equal(api.apply(frm, null, values, step), true);
  const row = frm.doc.approval_grants[0];
  assert.equal(row.rule_key, "new-key");
  assert.equal(row.fixed_values, undefined);
  assert.equal(frm.doc.approval_scope_values[1].scope_doctype, "Department");
  assert.equal(frm.doc.approval_scope_values[1].value, "Laboratory");
  assert.match(api.summary(row, frm.doc), /Laboratory/);
  api.apply(
    frm,
    row,
    { ...values, scope_source: "Departments I head", fixed_values: [] },
    step
  );
  assert.equal(frm.doc.approval_grants.length, 1);
  assert.equal(frm.doc.approval_scope_values.length, 1);
  assert.equal(frm.doc.approval_scope_values[0].rule_key, "handoff");
  api.remove(frm, row);
  assert.equal(frm.doc.approval_grants.length, 0);
  assert.equal(frm.doc.approval_scope_values.length, 1);
});

test("read-only forms cannot mutate grants and omit edit controls", () => {
  const { api, frm } = load(false);
  const row = { ...values, rule_key: "existing" };
  frm.doc.approval_grants.push(row);
  assert.equal(api.apply(frm, row, values, step), false);
  api.remove(frm, row);
  assert.equal(frm.doc.approval_grants.length, 1);
  api.render(frm);
  assert.doesNotMatch(
    frm.fields_dict.workflow_permissions_html.$wrapper.value,
    /data-action/
  );
  assert.match(frm.fields_dict.notifications_html.$wrapper.value, /disabled/);
  assert.equal(frm.dirty_count, undefined);
});

test("render escapes document values and notification settings update the same grant", () => {
  const { api, frm } = load();
  api.apply(
    frm,
    null,
    { ...values, company: '<img src=x onerror="bad()">' },
    step
  );
  const workflow = frm.fields_dict.workflow_permissions_html.$wrapper;
  const notifications = frm.fields_dict.notifications_html.$wrapper;
  assert.doesNotMatch(workflow.value, /<img/);
  assert.match(workflow.value, /&lt;img/);
  assert.doesNotMatch(notifications.value, /<img/);
  notifications.handlers["change.profile_permissions"]({
    currentTarget: {
      dataset: { index: "0", key: "ask_mode" },
      value: "Stand-in",
    },
  });
  assert.equal(frm.doc.approval_grants.length, 1);
  assert.equal(frm.doc.approval_grants[0].ask_mode, "Stand-in");
  assert.equal(frm.doc.approval_grants[0].max_amount, 500000);
  assert.equal(frm.doc.approval_scope_values.length, 1);
});

test("opening and cancelling native editor leaves document untouched; scope reset is explicit", async () => {
  const { api, frm, dialogs } = load();
  api.apply(frm, null, values, step);
  const before = JSON.stringify(frm.doc);
  const dirty_before = frm.dirty_count;
  await frm.fields_dict.workflow_permissions_html.$wrapper.handlers[
    "click.profile_permissions"
  ]({
    currentTarget: { dataset: { action: "edit", index: "0" } },
  });
  const dialog = dialogs[0];
  assert.equal(dialog.get_value("scope_kind"), "Department");
  assert.deepEqual(Array.from(dialog.get_value("fixed_values")), [
    "Laboratory",
  ]);
  assert.equal(dialog.fields_dict.max_amount.df.hidden, false);
  assert.equal(dialog.fields_dict.max_grade.df.hidden, true);
  await dialog.set_value("scope_kind", "Product Group");
  assert.equal(dialog.get_value("scope_source"), "Fixed values");
  assert.equal(dialog.get_value("fixed_values").length, 0);
  assert.equal(JSON.stringify(frm.doc), before);
  assert.equal(frm.dirty_count, dirty_before);
  dialog.hide();
});

test("new permission does not silently choose All and catalog errors are visible", async () => {
  const { api, frm, dialogs, frappe, refresh } = load();
  api.render(frm);
  await frm.fields_dict.workflow_permissions_html.$wrapper.handlers[
    "click.profile_permissions"
  ]({
    currentTarget: { dataset: { action: "add", index: "" } },
  });
  const dialog = dialogs[0];
  await dialog.set_value("step", step.step_id);
  assert.equal(dialog.get_value("scope_kind"), "");
  assert.equal(frm.doc.approval_grants.length, 0);
  frm.is_new = () => false;
  frappe.xcall = async () => {
    throw new Error("Offline");
  };
  await refresh(frm);
  assert.match(
    frm.fields_dict.workflow_permissions_html.$wrapper.value,
    /Unable to load/
  );
  assert.equal(frm.__permission_catalog_promise, undefined);
});

test("hand-off fixed values use a native Dynamic Link table and preserve approval scopes", () => {
  const { api, frm, dialogs, errors } = load();
  api.apply(frm, null, values, step);
  frm.doc.handoff_grants = [
    {
      step: "handoff_generic",
      scope_source: "Fixed values",
      rule_key: "handoff-existing",
    },
  ];
  frm.doc.approval_scope_values.push({
    rule_key: "handoff-existing",
    scope_doctype: "Warehouse",
    value: "Old Warehouse",
  });
  api.render(frm);
  const before = JSON.stringify(frm.doc);
  frm.fields_dict.notifications_html.$wrapper.handlers["click.profile_handoff"](
    {
      currentTarget: { dataset: { handoff: "0" } },
    }
  );
  const dialog = dialogs[0];
  const table = dialog.fields_dict.values.df;
  assert.equal(table.fields[1].fieldtype, "Dynamic Link");
  assert.equal(table.fields[1].options, "scope_doctype");
  assert.equal(JSON.stringify(frm.doc), before);
  dialog.options.primary_action({ values: [] });
  assert.match(errors[0], /at least one/);
  assert.equal(JSON.stringify(frm.doc), before);
  dialog.options.primary_action({
    values: [{ scope_doctype: "Department", value: "Support" }],
  });
  assert.equal(frm.doc.approval_scope_values.length, 2);
  assert.equal(frm.doc.approval_scope_values[0].rule_key, "new-key");
  assert.equal(frm.doc.approval_scope_values[1].rule_key, "handoff-existing");
  assert.equal(frm.doc.approval_scope_values[1].value, "Support");
  assert.equal(frm.doc.approval_scope_values[1].idx, 2);
});

test("queued native defaults and unchanged Apply preserve existing scope and ceilings", async () => {
  const { api, frm, dialogs } = load();
  api.apply(
    frm,
    null,
    { ...values, ask_mode: "Stand-in", channel: "Email" },
    step
  );
  const row = frm.doc.approval_grants[0];
  row.custom_existing_value = "keep";
  const before = JSON.stringify(frm.doc);
  await frm.fields_dict.workflow_permissions_html.$wrapper.handlers[
    "click.profile_permissions"
  ]({
    currentTarget: { dataset: { action: "edit", index: "0" } },
  });
  const dialog = dialogs[0];
  await dialog.defaults_ready;
  await Promise.resolve();
  assert.equal(JSON.stringify(frm.doc), before);
  assert.equal(dialog.get_value("scope_kind"), "Department");
  assert.equal(dialog.get_value("scope_source"), "Fixed values");
  assert.deepEqual(Array.from(dialog.get_value("fixed_values")), [
    "Laboratory",
  ]);
  assert.equal(dialog.get_value("max_amount"), 500000);
  assert.equal(dialog.get_value("max_percent"), 10);
  dialog.options.primary_action(dialog.values);
  assert.equal(row.max_amount, 500000);
  assert.equal(row.max_percent, 10);
  assert.equal(row.scope_kind, "Department");
  assert.equal(row.scope_source, "Fixed values");
  assert.equal(row.ask_mode, "Stand-in");
  assert.equal(row.channel, "Email");
  assert.equal(row.custom_existing_value, "keep");
  assert.equal(frm.doc.approval_scope_values[0].value, "Laboratory");
});

test("intentional step changes settle resets before new scope selections", async () => {
  const { api, frm, dialogs } = load();
  const other = {
    ...step,
    step_id: "other_approval",
    limit_kind: "Grade",
    allowed_scopes: ["Product Group"],
  };
  frm.__permission_catalog.steps.push(other);
  api.apply(frm, null, values, step);
  await frm.fields_dict.workflow_permissions_html.$wrapper.handlers[
    "click.profile_permissions"
  ]({
    currentTarget: { dataset: { action: "edit", index: "0" } },
  });
  const dialog = dialogs[0];
  await dialog.set_value("step", other.step_id);
  assert.equal(dialog.get_value("scope_kind"), "");
  assert.equal(dialog.get_value("fixed_values").length, 0);
  assert.equal(dialog.get_value("max_amount"), 0);
  assert.equal(dialog.get_value("max_percent"), 0);
  await dialog.set_value("scope_kind", "Product Group");
  await dialog.set_values({ fixed_values: ["POCT"], max_grade: 5 });
  dialog.options.primary_action(dialog.values);
  assert.equal(frm.doc.approval_grants[0].step, other.step_id);
  assert.equal(frm.doc.approval_grants[0].scope_kind, "Product Group");
  assert.equal(frm.doc.approval_grants[0].max_grade, 5);
  assert.equal(frm.doc.approval_grants[0].max_amount, 0);
  assert.equal(frm.doc.approval_scope_values[0].value, "POCT");
});

test("provider metadata controls scope sources without frontend step-specific rules", () => {
  const { api } = load();
  const catalog = {
    scope_sources: { Department: ["Fixed values", "Custom Department Source"] },
  };
  assert.deepEqual(Array.from(api.scope_sources("Department", catalog)), [
    "Fixed values",
    "Custom Department Source",
  ]);
  assert.equal(
    api.validate(
      { ...values, scope_source: "Custom Department Source", fixed_values: [] },
      step,
      catalog
    ),
    ""
  );
  assert.match(
    api.validate(
      { ...values, scope_source: "My Department", fixed_values: [] },
      step,
      catalog
    ),
    /comes from/
  );
});
