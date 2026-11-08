<template>
	<div class="space-y-5 text-ink-gray-9" :data-permission-view="view">
		<div class="flex flex-wrap items-start justify-between gap-3">
			<div class="space-y-1">
				<div class="flex items-center gap-2">
					<h3 class="text-lg font-semibold">
						{{ __(notifications ? "Workflow Notifications" : "Workflow Permissions") }}
					</h3>
					<Badge :label="rows.length" />
				</div>
				<p class="text-sm text-ink-gray-5">
					{{
						notifications
							? __(
									"Ask receives tasks. Stand-in can act without receiving tasks. Notifications use the same permission scope."
							  )
							: __(
									"Choose which workflow steps this profile can act on, then set their scope and limits."
							  )
					}}
				</p>
			</div>
			<Button
				v-if="!notifications && canWrite"
				variant="solid"
				icon-left="plus"
				@click="openEditor()"
				>{{ __("Add Permission") }}</Button
			>
		</div>
		<FormControl
			v-model="query"
			type="text"
			:placeholder="__('Search document, permission or scope')"
			:aria-label="__('Search workflow permissions')"
		/>
		<div
			v-if="!rows.length"
			class="rounded-lg border border-outline-gray-2 p-6 text-center text-sm text-ink-gray-5"
		>
			{{
				__(
					notifications
						? "Add workflow permissions to configure their notifications."
						: "No workflow permissions selected."
				)
			}}
		</div>
		<div v-else-if="!groups.length" class="py-6 text-center text-sm text-ink-gray-5">
			{{ __("No matching permissions.") }}
		</div>
		<section v-for="group in groups" :key="group.label" class="space-y-3">
			<h4 class="text-sm font-medium text-ink-gray-6">{{ group.label }}</h4>
			<div
				v-for="item in group.items"
				:key="item.row.name || item.row.rule_key || item.index"
				class="rounded-lg border border-outline-gray-2 bg-surface-white p-4"
			>
				<div class="flex flex-wrap items-start justify-between gap-3">
					<div class="min-w-0 flex-1 space-y-2">
						<div class="flex flex-wrap items-center gap-2">
							<strong class="break-words text-base font-medium">{{
								permissionLabel(item.step, item.row)
							}}</strong>
							<Badge v-if="item.step?.kind" :label="item.step.kind" />
						</div>
						<p class="break-words text-sm text-ink-gray-6">
							{{ api.summary(item.row, frm.doc, item.step) }}
						</p>
						<p
							v-if="item.step?.description"
							class="break-words text-sm text-ink-gray-6"
						>
							{{ item.step.description }}
						</p>
						<p class="break-words text-xs text-ink-gray-4">{{ item.row.step }}</p>
						<p v-if="!item.step" class="text-sm text-ink-red-4">
							{{ __("This permission is no longer available in the live catalog.") }}
						</p>
						<p v-if="item.step?.needs_flags?.length" class="text-sm text-ink-gray-5">
							{{ __("Requires") }}: {{ requiredFlags(item.step) }}
						</p>
					</div>
					<div v-if="!notifications && canWrite" class="flex shrink-0 gap-2">
						<Button v-if="item.step" @click="openEditor(item.row)">{{
							__("Edit")
						}}</Button>
						<Button theme="red" variant="ghost" @click="openRemove(item.row)">{{
							__("Remove")
						}}</Button>
					</div>
				</div>
				<div v-if="notifications" class="mt-4 grid gap-3 sm:grid-cols-2">
					<FormControl
						type="select"
						:label="__('Assignment Mode')"
						:options="['Ask', 'Stand-in']"
						:model-value="item.row.ask_mode || 'Ask'"
						:disabled="!canWrite"
						@update:model-value="setNotification(item.row, 'ask_mode', $event)"
					/>
					<FormControl
						type="select"
						:label="__('Channel')"
						:options="['Bell', 'Email', 'Both']"
						:model-value="item.row.channel || item.step?.default_channel || 'Bell'"
						:disabled="!canWrite"
						@update:model-value="setNotification(item.row, 'channel', $event)"
					/>
				</div>
			</div>
		</section>
		<section
			v-if="notifications && handoffs.length"
			class="space-y-3 border-t border-outline-gray-2 pt-5"
		>
			<h3 class="text-lg font-semibold">{{ __("Hand-off Fixed Scopes") }}</h3>
			<div
				v-for="row in handoffs"
				:key="row.name || row.rule_key || row.step"
				class="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-outline-gray-2 p-4"
			>
				<div class="min-w-0 flex-1 space-y-1">
					<p class="break-words text-base font-medium">
						{{ row.step || row.handoff_rule || __("Hand-off task") }}
					</p>
					<p class="break-words text-sm text-ink-gray-5">{{ handoffSummary(row) }}</p>
				</div>
				<Button v-if="canWrite" @click="openHandoff(row)">{{
					__("Configure Fixed Values")
				}}</Button>
			</div>
		</section>

		<Dialog v-model="showDialog" :options="{ title: dialogTitle, size: '2xl' }">
			<template #body-content>
				<div class="space-y-5">
					<p v-if="dialogMode === 'remove'" class="text-base text-ink-gray-7">
						{{ __("Remove this workflow permission?") }}
						{{ permissionLabel(steps.get(selectedRow?.step), selectedRow) }}
					</p>
					<template v-else-if="dialogMode === 'handoff'">
						<p class="text-sm text-ink-gray-5">
							{{
								__(
									"Choose Departments or Warehouses for this hand-off assignment."
								)
							}}
						</p>
						<div
							v-for="(scope, index) in handoffDraft"
							:key="index"
							class="grid items-end gap-3 sm:grid-cols-[150px_minmax(0,1fr)_auto]"
						>
							<FormControl
								type="select"
								:label="__('Scope')"
								:options="['Department', 'Warehouse']"
								:model-value="scope.scope_doctype"
								@update:model-value="changeHandoffKind(scope, $event)"
							/>
							<FormControl
								type="autocomplete"
								:label="__('Value')"
								:placeholder="__('Search values')"
								:model-value="option(scope.value)"
								:options="optionsFor(scope.scope_doctype, [scope.value])"
								@update:query="searchLinks(scope.scope_doctype, $event)"
								@update:model-value="scope.value = valueOf($event)"
							/>
							<Button
								theme="red"
								variant="ghost"
								:aria-label="__('Remove scope value')"
								@click="handoffDraft.splice(index, 1)"
								>{{ __("Remove") }}</Button
							>
						</div>
						<Button icon-left="plus" @click="addHandoffValue">{{
							__("Add Value")
						}}</Button>
					</template>
					<template v-else>
						<FormControl
							type="autocomplete"
							:label="__('Workflow Permission')"
							:placeholder="__('Search workflow permissions')"
							:model-value="selectedStepOption"
							:options="stepOptions"
							@update:model-value="changeStep(valueOf($event))"
						/>
						<div
							v-if="selectedStep"
							class="space-y-2 rounded-lg bg-surface-gray-2 p-3"
						>
							<p class="text-sm text-ink-gray-6">
								{{
									[
										selectedStep.flow,
										selectedStep.reference_doctype,
										...(selectedStep.actions || []),
									]
										.filter(Boolean)
										.join(" · ")
								}}
							</p>
							<p
								v-if="selectedStep.scope_policy"
								class="text-sm font-medium text-ink-gray-9"
							>
								{{ selectedStep.scope_policy }}
							</p>
							<p
								v-if="selectedStep.description || selectedStep.note"
								class="text-sm text-ink-gray-6"
							>
								{{ selectedStep.description || selectedStep.note }}
							</p>
						</div>
						<FormControl
							type="autocomplete"
							:label="__('Company')"
							:placeholder="__('All companies')"
							:description="__('Blank = all companies')"
							:model-value="option(draft.company)"
							:options="optionsFor('Company', [draft.company])"
							@update:query="searchLinks('Company', $event)"
							@update:model-value="draft.company = valueOf($event)"
						/>
						<template v-if="selectedStep && !selectedStep.scope_policy">
							<div class="grid gap-4 sm:grid-cols-2">
								<FormControl
									type="select"
									:label="__('Scope')"
									:options="scopeOptions"
									:model-value="draft.scope_kind"
									@update:model-value="changeScope($event)"
								/>
								<FormControl
									type="select"
									:label="__('Scope Source')"
									:options="sourceOptions"
									:model-value="draft.scope_source"
									@update:model-value="changeSource($event)"
								/>
							</div>
							<FormControl
								v-if="
									draft.scope_source === 'Fixed values' &&
									draft.scope_kind &&
									draft.scope_kind !== 'All'
								"
								type="autocomplete"
								:multiple="true"
								:label="__('Fixed Values')"
								:placeholder="__('Search and select values')"
								:model-value="draft.fixed_values.map(option)"
								:options="optionsFor(draft.scope_kind, draft.fixed_values)"
								@update:query="searchLinks(draft.scope_kind, $event)"
								@update:model-value="
									draft.fixed_values = ($event || []).map(valueOf)
								"
							/>
							<Checkbox
								v-if="draft.scope_kind && draft.scope_kind !== 'All'"
								v-model="draft.include_descendants"
								:label="__('Include Descendants')"
							/>
						</template>
						<div
							v-if="limitFields.length"
							class="space-y-3 border-t border-outline-gray-2 pt-4"
						>
							<h4 class="text-sm font-medium text-ink-gray-7">{{ __("Limits") }}</h4>
							<div class="grid gap-4 sm:grid-cols-2">
								<FormControl
									v-for="field in limitFields"
									:key="field"
									v-model="draft[field]"
									type="number"
									:label="__(limitLabels[field])"
									:description="__('Blank or 0 = Unlimited')"
									min="0"
									:max="field === 'max_percent' ? 100 : undefined"
									:step="field === 'max_grade' ? 1 : 'any'"
								/>
							</div>
						</div>
					</template>
					<p
						v-if="error"
						role="alert"
						class="rounded bg-surface-red-1 p-3 text-sm text-ink-red-4"
					>
						{{ error }}
					</p>
					<p v-if="linkError" role="alert" class="text-sm text-ink-red-4">
						{{ linkError }}
					</p>
				</div>
			</template>
			<template #actions>
				<div class="flex justify-end gap-2">
					<Button @click="showDialog = false">{{ __("Cancel") }}</Button>
					<Button
						:theme="dialogMode === 'remove' ? 'red' : 'gray'"
						variant="solid"
						:disabled="!canWrite"
						@click="saveDialog"
						>{{ __(dialogMode === "remove" ? "Remove" : "Apply") }}</Button
					>
				</div>
			</template>
		</Dialog>
	</div>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, shallowRef, watch } from "vue";
import { Button } from "frappe-ui/src/components/Button";
import Badge from "frappe-ui/src/components/Badge.vue";
import Checkbox from "frappe-ui/src/components/Checkbox.vue";
import Dialog from "frappe-ui/src/components/Dialog.vue";
import FormControl from "frappe-ui/src/components/FormControl.vue";

const props = defineProps({
	frm: { type: Object, required: true },
	catalog: { type: Object, required: true },
	revision: { type: Number, default: 0 },
	view: { type: String, default: "workflow" },
});
const __ = window.__;
const api = frappe.profile_permissions;
const query = ref("");
const notifications = computed(() => props.view === "notifications");
const canWrite = computed(() => {
	props.revision;
	return api.writable(props.frm);
});
const rows = computed(() => {
	props.revision;
	return props.frm.doc.approval_grants || [];
});
const handoffs = computed(() => {
	props.revision;
	return (props.frm.doc.handoff_grants || []).filter(
		(row) => row.scope_source === "Fixed values"
	);
});
const steps = computed(
	() => new Map((props.catalog.steps || []).map((step) => [step.step_id, step]))
);
const groups = computed(() => {
	props.revision;
	const filter = query.value.trim().toLowerCase();
	const grouped = new Map();
	rows.value.forEach((row, index) => {
		const step = steps.value.get(row.step);
		const label = step
			? `${step.flow} · ${step.reference_doctype}`
			: __("Unavailable permissions");
		if (
			filter &&
			![
				label,
				row.step,
				row.state_label,
				step?.label,
				step?.state,
				step?.description,
				api.summary(row, props.frm.doc, step),
			]
				.join(" ")
				.toLowerCase()
				.includes(filter)
		)
			return;
		if (!grouped.has(label)) grouped.set(label, []);
		grouped.get(label).push({ row, step, index });
	});
	return [...grouped].map(([label, items]) => ({ label, items }));
});
const showDialog = ref(false);
const dialogMode = ref("edit");
const selectedRow = shallowRef(null);
const editorDoc = shallowRef(null);
let editorSnapshot = "";
const draft = reactive({});
const handoffDraft = ref([]);
const error = ref("");
const linkError = ref("");
const linkOptions = reactive({});
const linkRequests = new Map();
const timers = new Map();
const selectedStep = computed(() => steps.value.get(draft.step));
function permissionLabel(step, row) {
	return step?.label || step?.state || row?.state_label || row?.step;
}
function stepChoiceLabel(step) {
	return [step.flow, step.reference_doctype, step.label || step.state || step.step_id]
		.filter(Boolean)
		.join(" · ");
}
const stepOptions = computed(() => {
	const groups = new Map();
	for (const step of props.catalog.steps || []) {
		const group = `${step.flow} · ${step.reference_doctype}`;
		if (!groups.has(group)) groups.set(group, []);
		groups.get(group).push({
			value: step.step_id,
			label: stepChoiceLabel(step),
			description: step.step_id,
		});
	}
	return [...groups].map(([group, items]) => ({ group, items }));
});
const selectedStepOption = computed(() =>
	selectedStep.value
		? {
				value: selectedStep.value.step_id,
				label: stepChoiceLabel(selectedStep.value),
		  }
		: null
);
const scopeOptions = computed(() => [
	"",
	...new Set(["All", ...(selectedStep.value?.allowed_scopes || [])]),
]);
const sourceOptions = computed(() => api.scope_sources(draft.scope_kind, props.catalog));
const limitFields = computed(() => api.ceilings(selectedStep.value?.limit_kind));
const limitLabels = {
	max_amount: "Max Amount",
	max_percent: "Max Percent",
	max_grade: "Max Grade",
};
const dialogTitle = computed(() =>
	__(
		dialogMode.value === "handoff"
			? "Hand-off Fixed Values"
			: dialogMode.value === "remove"
			? "Remove Workflow Permission"
			: selectedRow.value
			? "Edit Workflow Permission"
			: "Add Workflow Permission"
	)
);
function requiredFlags(step) {
	return step.needs_flags
		.map((name) => props.catalog.flags?.find((flag) => flag.fieldname === name)?.label || name)
		.join(", ");
}
function handoffSummary(row) {
	return api
		.scope_values(props.frm.doc, row)
		.map((item) => `${item.scope_doctype}: ${item.value}`)
		.join(", ");
}
function fingerprint(row) {
	return JSON.stringify([row, row ? api.scope_values(props.frm.doc, row) : []]);
}
function startDialog(mode, row) {
	if (!api.writable(props.frm)) return false;
	dialogMode.value = mode;
	selectedRow.value = row || null;
	editorDoc.value = props.frm.doc;
	editorSnapshot = fingerprint(row);
	error.value = "";
	linkError.value = "";
	showDialog.value = true;
	return true;
}
function openEditor(row) {
	if (!startDialog("edit", row)) return;
	Object.assign(draft, api.editor_values(props.frm.doc, row, steps.value.get(row?.step)));
	searchLinks("Company", "");
	if (draft.scope_kind && draft.scope_kind !== "All") searchLinks(draft.scope_kind, "");
}
function openRemove(row) {
	startDialog("remove", row);
}
function openHandoff(row) {
	if (!startDialog("handoff", row)) return;
	handoffDraft.value = api
		.scope_values(props.frm.doc, row)
		.map((item) => ({ scope_doctype: item.scope_doctype, value: item.value }));
	for (const scope of handoffDraft.value) searchLinks(scope.scope_doctype, "");
}
function changeStep(id) {
	draft.step = id;
	const step = steps.value.get(id);
	const scopes = ["All", ...(step?.allowed_scopes || []).filter((kind) => kind !== "All")];
	Object.assign(draft, {
		scope_kind: step?.scope_policy || scopes.length === 1 ? "All" : "",
		scope_source: "All",
		fixed_values: [],
		include_descendants: 1,
		max_amount: 0,
		max_percent: 0,
		max_grade: 0,
	});
	error.value = "";
}
function changeScope(kind) {
	Object.assign(draft, {
		scope_kind: kind,
		scope_source: kind === "All" ? "All" : "Fixed values",
		fixed_values: [],
	});
	if (kind && kind !== "All") searchLinks(kind, "");
}
function changeSource(source) {
	draft.scope_source = source;
	if (source === "Fixed values") searchLinks(draft.scope_kind, "");
}
function changeHandoffKind(scope, kind) {
	scope.scope_doctype = kind;
	scope.value = "";
	searchLinks(kind, "");
}
function addHandoffValue() {
	handoffDraft.value.push({ scope_doctype: "Department", value: "" });
	searchLinks("Department", "");
}
function setNotification(row, key, value) {
	api.set_notification(props.frm, row, key, value);
}
function saveDialog() {
	if (props.frm.doc !== editorDoc.value || !api.writable(props.frm)) {
		error.value = __("This Role Profile is no longer editable. Close and refresh the form.");
		return;
	}
	const row = selectedRow.value;
	const current =
		dialogMode.value === "handoff"
			? props.frm.doc.handoff_grants
			: props.frm.doc.approval_grants;
	if (row && (!current?.includes(row) || fingerprint(row) !== editorSnapshot)) {
		error.value = __("This permission changed. Close and open it again before applying.");
		return;
	}
	if (dialogMode.value === "remove") {
		if (api.remove(props.frm, row)) showDialog.value = false;
	} else if (dialogMode.value === "handoff") {
		error.value = api.apply_handoff_scope(props.frm, row, handoffDraft.value);
		if (!error.value) showDialog.value = false;
	} else {
		const step = steps.value.get(draft.step);
		const prepared = api.prepare_values(props.frm.doc, row, draft, step, props.catalog);
		error.value = prepared.error;
		if (!error.value && api.apply(props.frm, row, prepared.values, step))
			showDialog.value = false;
	}
}
function valueOf(value) {
	return value?.value ?? value ?? "";
}
function option(value) {
	return value ? { label: value, value } : null;
}
function optionsFor(kind, selected = []) {
	return [
		...new Set([
			...selected.filter(Boolean),
			...(linkOptions[kind] || []).map((item) => item.value),
		]),
	].map(option);
}
function searchLinks(kind, txt) {
	if (!kind || kind === "All") return;
	clearTimeout(timers.get(kind));
	const generation = (linkRequests.get(kind) || 0) + 1;
	linkRequests.set(kind, generation);
	timers.set(
		kind,
		setTimeout(async () => {
			try {
				const result = await frappe.xcall("frappe.desk.search.search_link", {
					doctype: kind,
					txt: txt || "",
					page_length: 30,
					reference_doctype: "Role Profile",
				});
				if (linkRequests.get(kind) === generation) {
					linkOptions[kind] = (result || []).map((item) => ({
						value: item.value,
						label: item.value,
					}));
					linkError.value = "";
				}
			} catch (failure) {
				if (linkRequests.get(kind) === generation)
					linkError.value = __(
						"Unable to search linked values. Check access and try again."
					);
			}
		}, 150)
	);
}
watch(
	() => [props.revision, props.frm.doc],
	() => {
		if (showDialog.value && (props.frm.doc !== editorDoc.value || !api.writable(props.frm)))
			showDialog.value = false;
	}
);
onBeforeUnmount(() => {
	for (const timer of timers.values()) clearTimeout(timer);
	linkRequests.clear();
});
</script>
