<template>
	<div class="space-y-5" data-permission-view="user">
		<div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
			<div class="space-y-1">
				<h3 class="text-lg font-semibold text-ink-gray-9">
					{{ t("Effective Permissions") }}
				</h3>
				<p class="max-w-2xl text-sm text-ink-gray-6">{{ notice }}</p>
			</div>
			<Badge :label="t('Saved permissions')" theme="gray" />
		</div>
		<FormControl
			v-model="query"
			type="search"
			:label="t('Find a permission')"
			:placeholder="t('Search roles, documents, conditions or source profiles')"
		>
			<template #prefix><FeatherIcon name="search" class="h-4 w-4" /></template>
		</FormControl>
		<section
			v-for="section in visibleSections"
			:key="section.name"
			class="overflow-hidden rounded-lg border border-outline-gray-2"
			:data-permission-section="section.name"
		>
			<Button
				variant="ghost"
				class="w-full !justify-between rounded-none px-4 py-3"
				:aria-expanded="isExpanded(section)"
				:aria-controls="`user-permissions-${section.name}`"
				@click="expanded[section.name] = !isExpanded(section)"
			>
				<template #default>
					<span class="flex min-w-0 items-center gap-2 text-left">
						<span class="font-semibold">{{ section.label }}</span>
						<Badge :label="String(section.filtered.length)" />
					</span>
				</template>
				<template #suffix>
					<FeatherIcon
						:name="isExpanded(section) ? 'chevron-up' : 'chevron-down'"
						class="h-4 w-4"
					/>
				</template>
			</Button>
			<div
				v-if="isExpanded(section)"
				:id="`user-permissions-${section.name}`"
				class="space-y-3 border-t border-outline-gray-2 p-4"
			>
				<p v-if="section.description" class="text-sm text-ink-gray-5">
					{{ section.description }}
				</p>
				<p v-if="!section.filtered.length" class="py-3 text-sm text-ink-gray-5">
					{{ query ? t("No matching permissions") : t("No permissions") }}
				</p>
				<div
					v-for="(row, index) in section.filtered"
					:key="index"
					class="space-y-3 rounded-md border border-outline-gray-2 p-3"
					data-permission-item
				>
					<div class="flex items-start justify-between gap-3">
						<div class="min-w-0 space-y-1">
							<p class="break-words text-base font-medium text-ink-gray-9">
								{{ row.permission }}
							</p>
							<p v-if="row.description" class="break-words text-sm text-ink-gray-6">
								{{ row.description }}
							</p>
							<div
								v-if="row.document_type || row.status"
								class="flex flex-wrap gap-2"
							>
								<Badge v-if="row.document_type" :label="row.document_type" />
								<Badge v-if="row.status" :label="row.status" theme="blue" />
								<span v-if="row.flow" class="text-sm text-ink-gray-5">{{
									row.flow
								}}</span>
							</div>
						</div>
						<Button
							v-if="row.permission_id"
							:label="t('Details')"
							@click="showDetails(row)"
						/>
					</div>
					<p v-if="row.scope_summary" class="break-words text-sm text-ink-gray-6">
						{{ row.scope_summary }}
					</p>
					<div v-if="row.notification || row.ceilings" class="flex flex-wrap gap-2">
						<Badge v-if="row.ceilings" :label="row.ceilings" variant="outline" />
						<Badge v-if="row.notification" :label="row.notification" theme="blue" />
					</div>
					<div
						v-if="section.name !== 'system_roles'"
						class="flex flex-wrap items-center gap-1"
					>
						<span class="text-xs text-ink-gray-5">{{ t("Source Profiles") }}:</span>
						<Button
							v-for="source in row.sources"
							:key="source"
							:label="source"
							variant="ghost"
							class="max-w-full !whitespace-normal !text-left"
							@click="openProfile(source)"
						/>
						<span v-if="!row.sources.length" class="text-sm text-ink-gray-5">{{
							t("Direct role")
						}}</span>
					</div>
				</div>
			</div>
		</section>
		<Dialog
			v-model="detailsOpen"
			:options="{ title: selected?.permission || t('Permission Details'), size: '2xl' }"
		>
			<template #body-content>
				<div v-if="selected" class="max-h-[65vh] space-y-5 overflow-y-auto">
					<dl class="space-y-4">
						<div v-for="[label, value] in detailFields" :key="label" class="space-y-1">
							<dt class="text-sm font-medium text-ink-gray-5">{{ t(label) }}</dt>
							<dd class="break-words whitespace-pre-wrap text-base text-ink-gray-9">
								{{ value }}
							</dd>
						</div>
					</dl>
					<div class="space-y-2">
						<p class="text-sm font-medium text-ink-gray-5">
							{{ t("Source Profiles") }}
						</p>
						<div class="flex flex-wrap gap-2">
							<Button
								v-for="source in selected.sources"
								:key="source"
								:label="source"
								class="max-w-full !whitespace-normal !text-left"
								@click="openProfile(source)"
							/>
							<span
								v-if="!selected.sources.length"
								class="text-sm text-ink-gray-5"
								>{{ t("Direct role") }}</span
							>
						</div>
					</div>
				</div>
			</template>
			<template #actions>
				<div class="flex justify-end">
					<Button :label="t('Close')" @click="detailsOpen = false" />
				</div>
			</template>
		</Dialog>
	</div>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { Button } from "frappe-ui/src/components/Button";
import Badge from "frappe-ui/src/components/Badge.vue";
import Dialog from "frappe-ui/src/components/Dialog.vue";
import FeatherIcon from "frappe-ui/src/components/FeatherIcon.vue";
import FormControl from "frappe-ui/src/components/FormControl.vue";

const props = defineProps({
	data: { type: Object, required: true },
	direct_roles: { type: Boolean, default: false },
});
const t = (value) => __(value);
const query = ref("");
const expanded = ref({});
const selected = ref(null);
const detailsOpen = ref(false);
const notice = computed(() =>
	props.data.administrator
		? t("Administrator has implicit system access. Saved grants do not limit Administrator.")
		: t("Saved permissions. Edit workflow authority in its source Role Profile.")
);
const visibleSections = computed(() => {
	const search = query.value.trim().toLocaleLowerCase();
	return frappe.user_permission_view
		.sections(props.data, { direct_roles: props.direct_roles })
		.map((section) => ({
			...section,
			filtered: section.rows.filter((row) =>
				Object.values(row).flat().join(" ").toLocaleLowerCase().includes(search)
			),
		}));
});
const detailFields = computed(() =>
	[
		["Document Type", selected.value?.document_type],
		["Permission ID", selected.value?.permission_id],
		["Workflow State", selected.value?.state],
		["Description", selected.value?.description],
		["Flow", selected.value?.flow],
		["Conditions", selected.value?.conditions],
		["Limits", selected.value?.ceilings],
		["Notification", selected.value?.notification],
	].filter(([, value]) => value)
);
function isExpanded(section) {
	return expanded.value[section.name] ?? (!section.collapsed || Boolean(query.value.trim()));
}
function showDetails(row) {
	selected.value = row;
	detailsOpen.value = true;
}
function openProfile(name) {
	detailsOpen.value = false;
	frappe.set_route("Form", "Role Profile", name);
}
watch(
	() => props.data,
	() => {
		detailsOpen.value = false;
		selected.value = null;
	}
);
</script>
