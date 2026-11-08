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
const component = readFileSync(
  new URL(
    "../../public/js/frappe/permission_ui/WorkflowPermissions.vue",
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
    empty() {
      this.value = "";
      return this;
    },
    text(value) {
      this.value = value;
      return this;
    },
  };
}
function load(write = true) {
  const mounts = [];
  const unmounts = [];
  let hooks;
  const frappe = {
    profile_permissions: {},
    provide() {},
    ui: {
      form: {
        on: (doctype, events) => {
          if (doctype === "Role Profile") hooks = events;
        },
      },
    },
    utils: {
      get_random: () => "new-key",
      escape_html: (value) => value.replaceAll("<", "&lt;"),
    },
    permission_ui_loader: { ensure: async () => {} },
    permission_ui: {
      mount: (wrapper, view, props) => mounts.push({ wrapper, view, props }),
      unmount: (wrapper) => unmounts.push(wrapper),
    },
  };
  vm.runInNewContext(source, {
    frappe,
    __: (text) => text,
    console: { error() {} },
  });
  const frm = {
    perm: [{ write }],
    doc: {
      name: "Profile A",
      approval_grants: [],
      approval_scope_values: [],
      handoff_grants: [],
    },
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
    is_new: () => false,
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
    api: frappe.profile_permissions,
    frm,
    frappe,
    mounts,
    unmounts,
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
const plain = (value) => JSON.parse(JSON.stringify(value));

test("catalog scope kinds and sources control choices; fixed scopes and ceilings validate", () => {
  const { api } = load();
  assert.deepEqual(plain(api.scope_sources("Department")), [
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
  for (const max_percent of [101, -1, Infinity, "invalid"])
    assert.match(
      api.validate({ ...values, max_percent }, step),
      /non-negative/
    );
  assert.match(
    api.validate({ ...values, max_amount: -1 }, step),
    /non-negative/
  );
  assert.equal(
    api.validate({ ...values, max_amount: 0, max_percent: 0 }, step),
    ""
  );
  const catalog = {
    scope_sources: { Department: ["Fixed values", "Custom Department Source"] },
  };
  assert.equal(
    api.validate(
      { ...values, scope_source: "Custom Department Source", fixed_values: [] },
      step,
      catalog
    ),
    ""
  );
  assert.match(
    api.validate({ ...values, scope_source: "My Department" }, step, catalog),
    /comes from/
  );
});

test("apply uses keyed scope siblings and removal leaves other authority and handoff values intact", () => {
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
  api.remove(frm, row);
  assert.equal(frm.doc.approval_grants.length, 0);
  assert.equal(frm.doc.approval_scope_values[0].rule_key, "handoff");
});

test("read-only and removed rows cannot mutate grants, notifications or handoff scopes", () => {
  const { api, frm } = load(false);
  const row = { ...values, rule_key: "existing" };
  frm.doc.approval_grants.push(row);
  frm.doc.handoff_grants.push(row);
  const before = JSON.stringify(frm.doc);
  assert.equal(api.apply(frm, row, values, step), false);
  assert.equal(api.remove(frm, row), false);
  assert.equal(api.set_notification(frm, row, "ask_mode", "Stand-in"), false);
  assert.match(api.apply_handoff_scope(frm, row, []), /no longer editable/);
  assert.equal(JSON.stringify(frm.doc), before);
  assert.equal(frm.dirty_count, undefined);
  frm.perm[0].write = true;
  assert.equal(api.apply(frm, { ...row }, values, step), false);
  assert.equal(api.remove(frm, { ...row }), false);
  assert.equal(
    api.set_notification(frm, { ...row }, "channel", "Email"),
    false
  );
});

test("Vue library renders both tabs without custom HTML or native dialogs", async () => {
  const { api, frm, mounts } = load();
  await api.render(frm);
  assert.deepEqual(
    mounts.map((item) => item.view),
    ["workflow", "notifications"]
  );
  assert.equal(mounts[0].props.frm, frm);
  assert.equal(mounts[1].props.view, "notifications");
  assert.match(component, /frappe-ui\/src\/components\/Dialog.vue/);
  assert.match(component, /frappe-ui\/src\/components\/FormControl.vue/);
  assert.doesNotMatch(component, /v-html/);
  assert.doesNotMatch(source, /new frappe\.ui\.Dialog|\.html\(|<table|<button/);
});

test("notifications edit the same grant and reject unexpected keys and values", () => {
  const { api, frm } = load();
  api.apply(frm, null, values, step);
  const row = frm.doc.approval_grants[0];
  assert.equal(api.set_notification(frm, row, "ask_mode", "Stand-in"), true);
  assert.equal(api.set_notification(frm, row, "channel", "Email"), true);
  assert.equal(api.set_notification(frm, row, "scope_kind", "All"), false);
  assert.equal(api.set_notification(frm, row, "channel", "Unexpected"), false);
  assert.equal(row.ask_mode, "Stand-in");
  assert.equal(row.channel, "Email");
  assert.equal(row.max_amount, 500000);
  assert.equal(frm.doc.approval_grants.length, 1);
  assert.equal(frm.doc.approval_scope_values.length, 1);
});

test("editor values are a detached draft; cancelling and unchanged Apply preserve authority", () => {
  const { api, frm } = load();
  api.apply(
    frm,
    null,
    { ...values, ask_mode: "Stand-in", channel: "Email", max_grade: 4 },
    step
  );
  const row = frm.doc.approval_grants[0];
  row.custom_existing_value = "keep";
  const before = JSON.stringify(frm.doc);
  const dirtyBefore = frm.dirty_count;
  const draft = api.editor_values(frm.doc, row, step);
  draft.fixed_values.push("Another department");
  draft.max_percent = 20;
  assert.equal(JSON.stringify(frm.doc), before);
  assert.equal(frm.dirty_count, dirtyBefore);
  const prepared = api.prepare_values(
    frm.doc,
    row,
    api.editor_values(frm.doc, row, step),
    step
  );
  assert.equal(prepared.error, "");
  api.apply(frm, row, prepared.values, step);
  assert.equal(row.max_amount, 500000);
  assert.equal(row.max_percent, 10);
  assert.equal(
    row.max_grade,
    4,
    "unsupported stored ceiling survives unchanged step"
  );
  assert.equal(row.ask_mode, "Stand-in");
  assert.equal(row.channel, "Email");
  assert.equal(row.custom_existing_value, "keep");
  assert.equal(frm.doc.approval_scope_values[0].value, "Laboratory");
});

test("new multi-scope permission never silently chooses All", () => {
  const { api, frm } = load();
  const draft = api.editor_values(frm.doc, null, step);
  assert.equal(draft.scope_kind, "");
  assert.match(
    api.prepare_values(frm.doc, null, draft, step).error,
    /allowed scope/
  );
  const globalStep = { ...step, allowed_scopes: [] };
  assert.equal(api.editor_values(frm.doc, null, globalStep).scope_kind, "All");
});

test("intentional step change resets unsupported ceilings rather than copying them to another grant", () => {
  const { api, frm } = load();
  api.apply(frm, null, values, step);
  const row = frm.doc.approval_grants[0];
  const other = {
    ...step,
    step_id: "other_approval",
    limit_kind: "Grade",
    allowed_scopes: ["Product Group"],
  };
  const draft = {
    ...values,
    scope_kind: "Product Group",
    fixed_values: ["POCT"],
    max_grade: 5,
  };
  const prepared = api.prepare_values(frm.doc, row, draft, other);
  assert.equal(prepared.error, "");
  api.apply(frm, row, prepared.values, other);
  assert.equal(row.step, other.step_id);
  assert.equal(row.max_grade, 5);
  assert.equal(row.max_amount, 0);
  assert.equal(row.max_percent, 0);
  assert.equal(frm.doc.approval_scope_values[0].value, "POCT");
});

test("relation-owned scopes preserve existing values and display policy instead of ineffective conditions", () => {
  const { api, frm } = load();
  const assigned = {
    ...step,
    allowed_scopes: [],
    scope_policy: "Manager assigned on Unit",
    note: "The named <manager> decides.",
  };
  api.apply(
    frm,
    null,
    { ...values, max_grade: 0, ask_mode: "Ask", channel: "Both" },
    assigned
  );
  const row = frm.doc.approval_grants[0];
  const prepared = api.prepare_values(
    frm.doc,
    row,
    {
      ...api.editor_values(frm.doc, row, assigned),
      scope_kind: "",
      fixed_values: [],
    },
    assigned
  );
  assert.equal(prepared.error, "");
  assert.equal(prepared.values.scope_kind, "Department");
  assert.deepEqual(plain(prepared.values.fixed_values), ["Laboratory"]);
  assert.match(api.summary(row, frm.doc, assigned), /Manager assigned on Unit/);
  assert.doesNotMatch(
    api.summary(row, frm.doc, assigned),
    /Department: Laboratory/
  );
  const fresh = api.prepare_values(
    frm.doc,
    null,
    { ...values, company: "Company B" },
    assigned
  );
  assert.equal(fresh.error, "");
  assert.equal(fresh.values.company, "Company B");
  assert.equal(fresh.values.scope_kind, "All");
  assert.equal(fresh.values.scope_source, "All");
  assert.deepEqual(plain(fresh.values.fixed_values), []);
});

test("handoff fixed scopes validate Departments/Warehouses while preserving approval scopes", () => {
  const { api, frm } = load();
  api.apply(frm, null, values, step);
  const row = {
    step: "handoff_generic",
    scope_source: "Fixed values",
    rule_key: "handoff-existing",
  };
  frm.doc.handoff_grants.push(row);
  frm.doc.approval_scope_values.push({
    rule_key: row.rule_key,
    scope_doctype: "Warehouse",
    value: "Old Warehouse",
  });
  const before = JSON.stringify(frm.doc);
  assert.match(api.apply_handoff_scope(frm, row, []), /at least one/);
  assert.match(
    api.apply_handoff_scope(frm, row, [
      { scope_doctype: "Company", value: "Invalid" },
    ]),
    /at least one/
  );
  assert.equal(JSON.stringify(frm.doc), before);
  assert.equal(
    api.apply_handoff_scope(frm, row, [
      { scope_doctype: "Department", value: "Support" },
    ]),
    ""
  );
  assert.equal(frm.doc.approval_scope_values.length, 2);
  assert.equal(frm.doc.approval_scope_values[0].rule_key, "new-key");
  assert.equal(frm.doc.approval_scope_values[1].rule_key, "handoff-existing");
  assert.equal(frm.doc.approval_scope_values[1].value, "Support");
  assert.equal(frm.doc.approval_scope_values[1].idx, 2);
});

test("async UI load cannot mount a stale document and failure shows plain safe text", async () => {
  const { api, frm, frappe, mounts, unmounts } = load();
  let done;
  frappe.permission_ui_loader.ensure = () =>
    new Promise((resolve) => {
      done = resolve;
    });
  const pending = api.render(frm);
  frm.doc = { ...frm.doc, name: "Profile B" };
  done();
  await pending;
  assert.equal(mounts.length, 0);
  frappe.permission_ui_loader.ensure = async () => {
    throw new Error("Offline");
  };
  await api.render(frm);
  assert.match(
    frm.fields_dict.workflow_permissions_html.$wrapper.value,
    /Unable to load/
  );
  assert.equal(unmounts.length, 2);
});

test("catalog failures discard rejected promise and stale responses never populate another profile", async () => {
  const { frm, frappe, refresh, mounts } = load();
  frappe.xcall = async () => {
    throw new Error("Offline");
  };
  await refresh(frm);
  assert.equal(frm.__permission_catalog_promise, undefined);
  assert.match(
    frm.fields_dict.workflow_permissions_html.$wrapper.value,
    /Unable to load/
  );
  let done;
  frappe.xcall = () =>
    new Promise((resolve) => {
      done = resolve;
    });
  const pending = refresh(frm);
  frm.doc = { ...frm.doc, name: "Profile B" };
  done({ steps: [step] });
  await pending;
  assert.equal(mounts.length, 0);
});

test("read denial invalidates a pending UI mount", async () => {
  const { api, frm, frappe, mounts } = load();
  let done;
  frappe.permission_ui_loader.ensure = () =>
    new Promise((resolve) => {
      done = resolve;
    });
  const pending = api.render(frm);
  frm.get_perm = () => false;
  await api.render(frm);
  done();
  await pending;
  assert.equal(mounts.length, 0);
  assert.match(
    frm.fields_dict.workflow_permissions_html.$wrapper.value,
    /Not permitted/
  );
});

test("switching Role Profile reauthorizes its catalog instead of reusing the previous promise", async () => {
  const { frm, frappe, refresh, mounts } = load();
  const calls = [];
  frappe.xcall = async (_method, args) => {
    calls.push(args.profile);
    return { steps: [step] };
  };
  await refresh(frm);
  await refresh(frm);
  frm.doc = { ...frm.doc, name: "Profile B" };
  await refresh(frm);
  assert.deepEqual(calls, ["Profile A", "Profile B"]);
  assert.equal(mounts.length, 6);
});

function vueEditor(env) {
  const props = {
    frm: env.frm,
    catalog: env.frm.__permission_catalog,
    revision: 0,
    view: "workflow",
  };
  const script = component
    .split("<script setup>")[1]
    .split("</script>")[0]
    .replace(/^import .*;\n/gm, "");
  const context = {
    props,
    frappe: env.frappe,
    window: { __: (value) => value },
    defineProps: () => props,
    computed: (getter) => ({
      get value() {
        return getter();
      },
    }),
    ref: (value) => ({ value }),
    shallowRef: (value) => ({ value }),
    reactive: (value) => value,
    watch() {},
    onBeforeUnmount() {},
    setTimeout: () => 1,
    clearTimeout() {},
  };
  vm.runInNewContext(
    script +
      "\nglobalThis.editor = { draft, error, showDialog, openEditor, changeStep, changeScope, saveDialog, permissionLabel, stepChoiceLabel, query, groups, stepOptions, selectedStepOption };",
    context
  );
  return context.editor;
}

test("Vue draft resets scopes explicitly and applies supported choices through the existing grant helper", () => {
  const env = load();
  const editor = vueEditor(env);
  const before = JSON.stringify(env.frm.doc);
  editor.openEditor();
  editor.changeStep(step.step_id);
  assert.equal(editor.draft.scope_kind, "");
  assert.equal(JSON.stringify(env.frm.doc), before);
  editor.changeScope("Department");
  editor.draft.fixed_values = ["Laboratory"];
  editor.draft.max_amount = "12000";
  editor.draft.max_percent = "10";
  editor.saveDialog();
  assert.equal(editor.error.value, "");
  assert.equal(editor.showDialog.value, false);
  assert.equal(env.frm.doc.approval_grants.length, 1);
  assert.equal(env.frm.doc.approval_grants[0].max_amount, 12000);
  assert.equal(env.frm.doc.approval_scope_values[0].value, "Laboratory");
});

test("Vue modal rejects changed rows and permission revocation before Apply", () => {
  const env = load();
  env.api.apply(env.frm, null, values, step);
  const row = env.frm.doc.approval_grants[0];
  const editor = vueEditor(env);
  editor.openEditor(row);
  row.channel = "Email";
  const before = JSON.stringify(env.frm.doc);
  editor.draft.max_amount = 1;
  editor.saveDialog();
  assert.match(editor.error.value, /permission changed/);
  assert.equal(JSON.stringify(env.frm.doc), before);
  editor.openEditor(row);
  env.frm.read_only = true;
  editor.saveDialog();
  assert.match(editor.error.value, /no longer editable/);
  assert.equal(JSON.stringify(env.frm.doc), before);
});

test("catalog display labels override states without changing stored workflow identifiers", () => {
  const env = load();
  const labelled = {
    ...step,
    label: "Accept / Reject Receipt",
    description:
      "Submitted <Accepted> inspection is required only for inspection items.",
  };
  env.frm.__permission_catalog.steps = [labelled];
  env.api.apply(env.frm, null, values, labelled);
  const row = env.frm.doc.approval_grants[0];
  const before = JSON.stringify(env.frm.doc);
  const editor = vueEditor(env);
  editor.openEditor(row);
  assert.equal(editor.permissionLabel(labelled, row), labelled.label);
  assert.match(
    editor.selectedStepOption.value.label,
    /Accept \/ Reject Receipt/
  );
  assert.doesNotMatch(
    editor.stepOptions.value[0].items[0].label,
    /Pending Approval/
  );
  assert.equal(row.step, step.step_id);
  assert.equal(row.state_label, step.state);
  for (const query of [labelled.label, step.state, labelled.description]) {
    editor.query.value = query;
    assert.equal(editor.groups.value.length, 1);
  }
  assert.equal(JSON.stringify(env.frm.doc), before);
  assert.equal(editor.permissionLabel(step, row), step.state);
  assert.equal(editor.permissionLabel(undefined, row), step.state);
  assert.equal(
    editor.permissionLabel(undefined, { step: "retired-step" }),
    "retired-step"
  );
});

test("catalog descriptions use plain Vue text in cards and replace legacy dialog notes", () => {
  assert.match(component, /\{\{ item\.step\.description \}\}/);
  assert.match(
    component,
    /\{\{ selectedStep\.description \|\| selectedStep\.note \}\}/
  );
  assert.doesNotMatch(component, /v-html|innerHTML/);
});
