# Approval Step

One row for each step a Role Profile can grant (Role Profile Authority Design, section 3): an approval, a maker limit,
a hand-off, or a transition that needs no grant (Apply). The rows are written in code, in `workflows/steps.py`;
`migrate` syncs them here (`setup/role_profile_authority.sync_approval_steps`). Nobody edits or deletes a row: the
`step_id` never changes and a step that is no longer used is kept with `retired` ticked.

| Field | Meaning |
|---|---|
| `step_id`, `kind`, `flow`, `sequence` | The permanent key; Approval / Maker Limit / Handoff / System / Maker / Cancel / Apply; the loop (P2P, O2C, Stock, Borrowings, Service, HR, Access, Master data) and the order inside it |
| `reference_doctype`, `state`, `also_states`, `actions`, `trigger` | The workflow transitions the step covers (a hand-off has a trigger instead of a state) |
| `amount_field`, `limit_kind` | The document amount compared with a grant's limit; None, Amount, Percent, Amount+Percent or Grade |
| `allowed_scopes`, `scope_fields` | The scope types a grant may choose, and the document field each is read from |
| `allow_edit`, `needs_flags`, `reads_all_in_scope`, `privileged`, `default_channel` | The work role that edits in the state; the flags the approver's Profile needs; whether holders read every document in scope; whether only a System Manager may change a Profile that holds it; the default notification channel |

Role Profile Approval and Role Profile Handoff rows pick a step through a Link field (Approval and Maker Limit steps
only, or Handoff steps only, never a retired one). The Workflow sidebar lists the DocType.

The IT Access Administrator, the Managing Director and the Chief Executive Officer read the catalog and open the Script
Report Workflow Responsibility (`report/workflow_responsibility`, `workflows/workflow_responsibility.py`): one row for
each step in the order of its loop, with the Profiles, Designations and people that can do it and the gaps. The report
and the daily `ho_coverage_gap` task (`my_space/coverage_tasks.py`) write nothing to a Role Profile.

The code catalog can supply `label` and `description` as editor-only metadata through the existing permission APIs.
They do not rename workflow states or permanent step keys and do not add database fields. `pr_goods_inspection`
appears as **Accept / Reject Purchase Receipt**, with the prerequisite that items requiring QI need a submitted,
Accepted inspection before receipt acceptance. The assigned QI team performs QI separately from receipt acceptance.
