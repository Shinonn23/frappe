# Approval Grant

A read-only index (Role Profile Authority Design, section 5.2): one row for each right of a user to decide a step
(user, step, company, limit, scope, ask mode, channel, source Profile). The rows are rebuilt from the Role Profiles of the
user (`workflows/profile_approvals.sync_user_grants`) whenever the user or one of the Profiles changes, never in
`migrate`. The grants of a user are the union of his Profiles: rows of one step, company and scope merge and the
highest limit wins (`merge_grants`). Nothing reads this table to decide anything yet (PR 9-15); `may_act` reads the
Profiles directly.

The Role Profile seed step (`setup/role_profile_seed.run`, `sync_grant_index`) rebuilds the index of every Profile
holder in its own run, because a Profile save only queues the rebuild for a worker and a User saved during a migrate,
patch or import skips it (18 Nov 2026: the Purchase Manager's 50,000 grants were on her Profile but not in the index).
