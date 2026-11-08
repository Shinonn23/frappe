import { createApp, h, markRaw, shallowReactive } from "vue";
import UserPermissions from "./UserPermissions.vue";
import ProfileRoles from "./ProfileRoles.vue";
import WorkflowPermissions from "./WorkflowPermissions.vue";
import "./permission-ui.css";

frappe.provide("frappe.permission_ui");
const views = {
	user: UserPermissions,
	roles: ProfileRoles,
	workflow: WorkflowPermissions,
	notifications: WorkflowPermissions,
};
const element = (wrapper) => wrapper?.get?.(0) || wrapper;

function unmount(wrapper) {
	const target = element(wrapper);
	const mounted = target?.__permission_ui;
	if (!mounted) return;
	mounted.app.unmount();
	mounted.element.remove();
	delete target.__permission_ui;
}

function mount(wrapper, view, props = {}) {
	const target = element(wrapper);
	if (!target || !views[view]) throw new Error("Invalid permission UI mount target or view");
	const values = { ...props, view, notificationOnly: view === "notifications" };
	if (values.frm) values.frm = markRaw(values.frm);
	let mounted = target.__permission_ui;
	if (mounted?.view === view) {
		for (const key of Object.keys(mounted.state)) {
			if (key !== "revision" && !(key in values)) delete mounted.state[key];
		}
		Object.assign(mounted.state, values, { revision: mounted.state.revision + 1 });
		return mounted;
	}
	unmount(target);
	target.replaceChildren();
	const root = document.createElement("div");
	root.className = "frappe-permission-ui";
	target.appendChild(root);
	const state = shallowReactive({ ...values, revision: 1 });
	const app = createApp({ render: () => h(views[view], state) });
	app.config.globalProperties.__ = __;
	mounted = { app, state, element: root, view, unmount: () => unmount(target) };
	target.__permission_ui = mounted;
	app.mount(root);
	return mounted;
}

Object.assign(frappe.permission_ui, { mount, unmount });
