import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const pageSource = fs.readFileSync(new URL("../../public/js/frappe/ui/page.js", import.meta.url), "utf8");
const filterSource = fs.readFileSync(new URL("../../public/js/frappe/list/list_filter.js", import.meta.url), "utf8");
const method = (start, end) => pageSource.slice(pageSource.indexOf(`\t${start}(`), pageSource.indexOf(`\n\t${end}(`));

function fixture() {
  const node = (label = "", parent = null) => ({ label, parent, entries: new Map(), classes: new Set(), clicks: [] });
  class Query {
    constructor(value) { this.node = value; this.length = value ? 1 : 0; }
    find(selector) {
      if (selector === ".dropdown-menu") return new Query(this.node?.menu);
      const label = selector.match(/data-label="([^"]+)"/)?.[1];
      return new Query(this.node?.entries.get(label));
    }
    parent() { return new Query(this.node?.parent); }
    addClass(value) { this.node?.classes.add(value); return this; }
    removeClass(value) { this.node?.classes.delete(value); return this; }
    hasClass(value) { return this.node?.classes.has(value); }
    on(event, callback) { if (event === "click") this.node?.clicks.push(callback); return this; }
    appendTo(parent) {
      this.node.parent = parent.node;
      parent.node.entries.set(this.node.label, this.node);
      return this;
    }
    append(child) { if (this.node) child.appendTo(this); return this; }
    empty() {
      if (this.node) {
        for (const child of this.node.entries.values()) child.parent = null;
        this.node.entries.clear();
      }
      return this;
    }
    click() { for (const callback of this.node.clicks) callback.call(this.node); }
  }
  const filterX = node();
  const $ = (value) => {
    if (value instanceof Query) return value;
    if (typeof value === "string") {
      if (value === ".filter-x-button") return new Query(filterX);
      return new Query(node(value.match(/data-label="([^"]+)"/)?.[1] || ""));
    }
    return new Query(value);
  };
  const groups = new Map();
  const mobile = new Map();
  const page = {
    inner_toolbar: new Query(node()), menu_btn_group: new Query(node()),
    add_menu_item(label) {
      if (!mobile.has(label)) mobile.set(label, new Query(node(label, node())));
      return mobile.get(label);
    },
    get_or_add_inner_group_button(label) {
      if (!groups.has(label)) {
        const group = node(label); group.menu = node("menu", group);
        groups.set(label, new Query(group));
      }
      return groups.get(label);
    },
    btn_disable_enable() {},
  };
  const context = { $, __: (label) => label, frappe: { provide() {}, user: { has_role: () => false }, is_mobile: () => false } };
  for (const [start, end] of [["is_in_group_button_dropdown", "clear_btn_group"], ["add_inner_button", "remove_inner_button"]]) {
    page[start] = vm.runInNewContext(`({${method(start, end)}}).${start}`, context);
  }
  const ListFilter = vm.runInNewContext(filterSource.replace("export default class ListFilter", "class ListFilter") + "\nListFilter", context);
  ListFilter.prototype.append_create_new_item = function (menu) { menu.append(new Query(node("create_new"))); };
  return { page, groups, mobile, ListFilter };
}

test("duplicate grouped buttons return the existing link without replacing its callback", () => {
  const f = fixture(); let first = 0; let replacement = 0;
  const one = f.page.add_inner_button("Filters", () => first++, "Saved Filters");
  const two = f.page.add_inner_button("Filters", () => replacement++, "Saved Filters");
  assert.equal(two.node, one.node);
  assert.equal(two.parent().length, 1);
  assert.equal(f.groups.get("Saved Filters").node.menu.entries.size, 1);
  assert.equal(f.mobile.size, 1);
  two.click();
  assert.equal(first, 1);
  assert.equal(replacement, 0);
});

test("the same encoded label remains independent across groups", () => {
  const f = fixture(); const label = 'ตรวจ "A" / รายการ';
  const one = f.page.add_inner_button(label, () => {}, "Group A");
  const two = f.page.add_inner_button(label, () => {}, "Group B");
  const duplicate = f.page.add_inner_button(label, () => {}, "Group A");
  assert.notEqual(one.node, two.node);
  assert.equal(duplicate.node, one.node);
  assert.equal(f.mobile.size, 2);
});

test("ungrouped duplicate button return behavior remains unchanged", () => {
  const f = fixture();
  const one = f.page.add_inner_button("Reload", () => {});
  const two = f.page.add_inner_button("Reload", () => {});
  assert.equal(one.node, two.node);
  assert.equal(f.page.inner_toolbar.node.entries.size, 1);
});

test("actual ListFilter overlapping refresh callbacks can render saved filters without an undefined button", async () => {
  const f = fixture(); const resolvers = []; const renders = [];
  const list = new f.ListFilter({ page: f.page, get_list_filters() {
    const ready = new Promise((resolve) => resolvers.push(resolve));
    return { then(callback) { const task = ready.then(callback); renders.push(task); return task; } };
  } });
  const first = list.saved_filters_btn;
  list.refresh_list_filter();
  assert.equal(list.saved_filters_btn.node, first.node);
  resolvers.forEach((resolve) => resolve());
  await Promise.all(renders);
  assert.equal(f.groups.get("Saved Filters").node.menu.entries.has("create_new"), true);
});
