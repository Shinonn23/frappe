# Role Profile permissions

The Frappe fork owns the Role Profile and User permission display. Company applications supply the workflow
catalog and saved authority data through hooks; the form does not embed a company-specific permission list.

## Forms and rendering

`role_profile.json` defines the Profile, Roles, Workflow Permissions, Notifications, Visibility and Holders tabs,
including their HTML fields. `../user/user.json` adds the saved User summary field `profile_permissions_html`.
The form bundle, `frappe/public/js/form.bundle.js`, loads these renderers:

| Source under `frappe/public/js/frappe/` | Responsibility |
|---|---|
| `profile_roles.js` | Vue Functional Roles, Data Permissions and additional catalog flags; role/flag storage synchronization |
| `role_profile_permissions.js` | Catalog-driven Vue workflow permission dialogs, per-step scope and ceilings, and notification configuration |
| `user_permission_view.js` | Read-only saved User roles, data permissions, merged workflow grants, notifications and source profiles |
| `roles_editor.js` | Native role table lifecycle and preservation of assigned roles hidden from the general picker |

Display tests live in `frappe/tests/js/profile_roles.test.mjs`, `role_profile_permissions.test.mjs` and
`user_permission_view.test.mjs`. Company grant validation remains with the application that owns the grants.

## Data providers

The whitelisted endpoints in `role_profile.py` use the last configured provider for each hook:

| Endpoint | Hook | Provider input |
|---|---|---|
| `get_permission_catalog` | `role_profile_permission_catalog` | `profile`: a saved Role Profile name, or null for a new profile |
| `get_user_permissions` | `user_role_profile_permissions` | `user`: a saved User name |

Without a provider the endpoints return null. Native endpoints check access to the named document (or permission
to create a new Role Profile); providers retain their own authorization and data-access checks. A missing or denied
User provider does not replace the native role editor with a partial summary.

The Connect Diagnostics setup application registers its catalog in `workflows.profile_tools.editor_catalog` and
its optional User summary in `workflows.user_permission_view.summary_for_editor`. These supply the live grantable Approval
Step catalog, supported conditions, capability metadata and saved merged authority. The existing grant engine and
Role Profile save validation continue enforcing scope, ceilings and authorization; this display adds no direct
User workflow grants. Internal flags and grant/scope child rows remain hidden application-owned backing storage.
The preview adds no data-migration patch.

## Workflow roles

`Role.is_workflow_permission` identifies workflow/approval-only roles. The native User endpoint
`get_role_editor_data` returns general role choices separately from these managed roles. Both User and Role Profile
checkboxes exclude them, and RoleEditor preserves any assigned hidden roles when saving other role selections.
There is no Managed Workflow Roles section. Select workflow authority in Workflow Permissions instead.

The company application classifies its workflow roles during its schema setup; the framework consumes the Role
metadata rather than hardcoding company role names. This display filtering does not remove a role assignment or
replace server authorization.

## Usage

Choose ordinary roles and data permissions in Roles. In Workflow Permissions, use Add Permission to select a catalog
step, then set its Company, scope source, fixed values and supported amount, percent or grade ceilings. Edit changes
an existing row. Notifications changes Ask/Stand-in and channel for the same grant; hand-off assignments remain
separate. A notification selection does not grant additional approval authority.

Catalog steps may provide `label` and `description` for action-oriented permission names and prerequisite guidance.
The editors and User summary prefer that label while preserving the raw workflow state and permanent step key.
Descriptions render as plain text in cards and dialogs; older providers without display metadata retain the state label.

Catalog steps with `scope_policy` show their assignment policy and `note` instead of editable scope controls.
Company still applies. New grants use All for internal scope storage; editing an existing grant preserves its
historical scope values. The same policy appears in the saved User summary.

In the current company provider, blank Company means all companies; blank or zero numeric ceilings mean Unlimited,
so a finite zero ceiling cannot be represented. My Department resolves membership, while Departments I head resolves
head seats. The User summary shows saved authority and current department roots where available; save User changes
to refresh it. Users with profiles see the summary instead of the disabled native role checkbox list, while users
without profiles retain direct-role editing. The summary is not a document-specific authorization verdict.

The embedded screens use the external `frappe-ui` Vue library, loaded lazily through
`permission_ui_loader.js` and `permission_ui/permission_ui.bundle.js`. `ProfileRoles.vue` edits functional roles and
capability flags; `WorkflowPermissions.vue` edits catalog grants, scopes, ceilings and notifications;
`UserPermissions.vue` shows saved permissions with search, collapsible sections and a Details dialog. The Desk form
continues to own Save, validation and document state. Read-only User viewing never changes the User document.
Identical handoff configurations combine their sources without merging different conditions. Frappe supplies
`implicit_roles` alongside provider data for a separate collapsed System Roles section.

The framework pins `frappe-ui` 0.1.105 and Vue 3.5.12. `esbuild/permission-ui-style.js` builds the library's Tailwind
styles only for this bundle, scoped to `.frappe-permission-ui` and the library's popover/dialog portals. Its base
reset and typography do not affect the surrounding Desk. Run `bench build --app frappe` after source or dependency
changes. Forms without an application catalog keep the native RoleEditor fallback.

Workflow Settings remains global routing and validation configuration. The preview does not remove those settings
or change their meaning into per-user permissions.
