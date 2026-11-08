<template>
	<div class="space-y-6 py-2" data-permission-view="roles">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<h3 class="m-0 text-lg font-semibold text-ink-gray-9">
					{{ t("Roles and Data Permissions") }}
				</h3>
				<p class="mb-0 mt-1 text-sm text-ink-gray-5">
					{{
						t(
							"Choose access for this profile. Configure approvals in Workflow Permissions."
						)
					}}
				</p>
			</div>
			<Badge
				:label="t(writable ? 'Editable' : 'Read only')"
				:theme="writable ? 'blue' : 'gray'"
			/>
		</div>
		<div class="flex flex-wrap items-end gap-3">
			<FormControl
				v-model="query"
				type="search"
				:label="t('Find a permission')"
				:placeholder="t('Search roles and data permissions')"
				class="min-w-0 flex-1"
			/>
			<Button
				:variant="selectedOnly ? 'solid' : 'subtle'"
				@click="selectedOnly = !selectedOnly"
				>{{ t("Selected only") }} · {{ selectedCount }}</Button
			>
		</div>
		<p v-if="error" role="alert" class="text-sm text-ink-red-3">{{ error }}</p>
		<section
			v-for="group in groups"
			:key="group.title"
			class="rounded-lg border border-outline-gray-2 p-4"
		>
			<div class="mb-4 flex items-center gap-2">
				<h4 class="m-0 text-base font-semibold text-ink-gray-9">{{ t(group.title) }}</h4>
				<Badge
					:label="
						group.items.filter((item) => item.checked).length +
						' / ' +
						group.items.length
					"
				/>
			</div>
			<div class="grid grid-cols-1 gap-x-6 gap-y-3 lg:grid-cols-2">
				<Checkbox
					v-for="item in group.items"
					:key="item.value"
					:label="item.label"
					:model-value="item.checked"
					:disabled="!writable || busy"
					@update:model-value="toggle(item, $event)"
				/>
			</div>
		</section>
		<p v-if="!groups.length" class="py-8 text-center text-sm text-ink-gray-5">
			{{ t("No permissions match your search.") }}
		</p>
	</div>
</template>

<script setup>
import { computed, ref } from "vue";
import { Button } from "frappe-ui/src/components/Button";
import Badge from "frappe-ui/src/components/Badge.vue";
import Checkbox from "frappe-ui/src/components/Checkbox.vue";
import FormControl from "frappe-ui/src/components/FormControl.vue";

const props = defineProps({ frm: Object, catalog: Object, revision: Number });
const t = (value) => __(value);
const query = ref("");
const selectedOnly = ref(false);
const busy = ref(false);
const error = ref("");
const writable = computed(() => {
	props.revision;
	return frappe.profile_permissions.writable(props.frm);
});
const choices = computed(() => {
	props.revision;
	return [
		...frappe.profile_roles.options(props.frm.doc, props.catalog),
		...(props.catalog.flags || [])
			.filter((flag) => !flag.role)
			.map((flag) => ({
				value: flag.fieldname,
				label: t(flag.label),
				checked: Boolean(props.frm.doc[flag.fieldname]),
				group: "HR and Additional Permissions",
				flag: true,
			})),
	];
});
const selectedCount = computed(() => choices.value.filter((item) => item.checked).length);
const groups = computed(() => {
	const search = query.value.trim().toLowerCase();
	return ["Functional Roles", "Data Permissions", "HR and Additional Permissions"]
		.map((title) => ({
			title,
			items: choices.value.filter(
				(item) =>
					item.group === title &&
					(!selectedOnly.value || item.checked) &&
					(!search || `${item.label} ${item.value}`.toLowerCase().includes(search))
			),
		}))
		.filter((group) => group.items.length);
});
async function toggle(item, value) {
	if (!writable.value || busy.value) return;
	busy.value = true;
	error.value = "";
	try {
		await frappe.profile_roles[item.flag ? "set_flag" : "set_role"](
			props.frm,
			item.value,
			value
		);
	} catch (exception) {
		error.value = t("Unable to change this permission. Refresh the form and retry.");
		console.error(exception);
	} finally {
		busy.value = false;
	}
}
</script>
