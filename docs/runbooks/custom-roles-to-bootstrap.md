# CI loses `roles/iam.roleAdmin`: the custom roles and the broker's `swarmSecretLister` grant move to the owner's root

**Who:** the deployment's owner, once. **Takes:** one merge, two releases and
one bootstrap apply — in that order, on paper. **What actually ran, live on
`saga-agents-staging`, took a merge, one bootstrap apply that partly failed, a
follow-up fix PR, and a second targeted apply**, because GCP refused part of
the first apply; see "What actually happened" below. **Changes:** who manages
nine existing objects — no permission of any role changes, and nothing is
created or deleted — and one binding is removed: `roles/iam.roleAdmin` from
`swarm-tf-deployer`. PR #73's `projectIamAdmin` scoping is live and, as of
#275/#277, its condition is not one `hasOnly()` call but two, one per chunk of
at most ten roles (GCP's IAM linter refuses a longer list).

The worked values are Saga's `dev` deployment (`saga-agents-staging`, state
prefix `infra/dev`). **Most of this page was written as "derived" — read from
the code and the provider's documentation, before any plan or apply ran.**
Where a plan or apply has since actually run, that is called out by date, and
it is what governs: the "Plan: 9 to import, 1 to add, 0 to change, 2 to
destroy" shape derived below assumed PR #73's single-condition
`deployer_project_iam_admin[0]` already existed in `terraform/bootstrap`'s
state and would be *replaced*. It never had — the resource did not exist in
that state until the owner's apply tried to *create* it for the first time,
in the same batch as the nine imports and the `roleAdmin` destroy. That create
is what GCP's `hasOnly()` 10-element limit refused (#275); see "What actually
happened."

## What actually happened (dates)

1. **2026-09-28 20:15Z — #239 merged and released.** `terraform/infra`'s
   `removed`/`moved` blocks ran in that release's `terraform apply (dev)`,
   exactly as Step 1 below describes: the nine objects left `infra/dev`'s
   state, nothing live changed, and `custom_roles_owner` became
   `"terraform/bootstrap"`.
2. **2026-09-28 23:21:22–23:21:40Z — the owner's first bootstrap apply
   (as `bogdan@saga.xyz`), targeting all four resources in Step 2.** The nine
   imports and the `roleAdmin` destroy succeeded exactly as derived. Creating
   `google_project_iam_member.deployer_project_iam_admin[0]` failed:
   `Error 400: LintValidationUnits/ListLengthCheck: The list argument to
   hasOnly() cannot have more than 10 elements` — PR #73's single condition
   named all fifteen `deployer_grantable_project_roles` in one `hasOnly()`
   call, a limit the mock-provider `terraform test` cannot see (filed as
   [#275](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/275)). The
   same apply had already destroyed the deployer's unconditioned
   `projectIamAdmin` before attempting to create its replacement, so for about
   one minute CI held no `projectIamAdmin` grant at all. The operator restored
   the unconditioned grant by hand at ~23:22Z
   (`gcloud projects add-iam-policy-binding … --condition=None`) — live, but
   not in bootstrap state.
3. **2026-09-29 02:30:17Z — [#277](https://github.com/bogdan-alexandrescu/SwarmCloud/pull/277) merged to `main`** (`2ef21b8`, closing #275).
   `deployer_project_iam_admin` became a `for_each` of
   `chunklist(local.deployer_grantable_project_roles, 10)`, one conditioned
   binding per chunk (two chunks today, 10 roles and 4), each with its own
   `hasOnly()` call — never one condition ORing several `hasOnly()`s together,
   which GCP's own docs warn can make multi-role grant requests fail. A
   `terraform test` now holds every `hasOnly()` list at 10 or fewer and the
   chunks' union equal to the grantable set.
4. **This PR rebased onto that `main`.**
5. **2026-09-29 ~02:35Z — the owner's second apply (`bogdan@saga.xyz`, from
   `main` at `0356b39`, #277 merged), following #277's docs/ci.md six-step
   order:**
   1. `DEPLOYER=$(terraform -chdir=terraform/bootstrap output -raw github_deployer_service_account)`
   2. `scripts/bootstrap.sh --target 'google_project_iam_member.deployer_project_iam_admin'`
      — plan read exactly **"2 to add, 0 to change, 0 to destroy"**, and GCP
      accepted both chunked conditions this time (10 roles and 4, each under
      the limit).
   3. `gcloud projects get-iam-policy … --format='value(bindings.condition.title)'`
      showed **three** bindings: chunk 1 of 2, chunk 2 of 2, and one empty
      line — the hand-restored unconditioned grant, still live.
   4. `gcloud projects remove-iam-policy-binding … --condition=None` removed
      exactly that unconditioned grant.
   5. Re-running step 3 showed exactly **two** bindings: chunk 1 of 2 and
      chunk 2 of 2, nothing else.
   6. The next release's `terraform apply (dev)` is the proof of the admitted
      side. It is release run
      [36514377555](https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/36514377555)
      (`main` at `77014f0`, this PR's merge), created 2026-09-29 02:49Z, the
      first successful release after this apply: its `terraform apply (dev)`
      job ran 03:29:55–03:32:26Z with its `plan`, `shared-project guard` and
      `apply` steps all successful, so no `google_project_iam_custom_role`
      was planned or refused. Read 2026-10-08 from the public Actions API
      (run list, job and step conclusions). The job's log needs an
      authenticated read and was not grepped for `Error 403`; the evidence
      is the steps' success, not a log search.

   **Result:** the deployer now holds exactly the two chunked conditional
   `projectIamAdmin` grants and no `roles/iam.roleAdmin`. The 8 custom roles
   and the broker's `swarmSecretLister` grant were imported (no-op) in step 2
   of the first apply and were never affected by the `projectIamAdmin`
   failure or its retry.

This PR (#150) merges once CI is green on the rebased branch; nothing further
needs to be applied for it.

## Why

Owner decisions of 2026-09-25, recorded on #79, #69, #68 and #80:

* **#79.** `roles/iam.roleAdmin` comes off the CI deployer. Its
  `iam.roles.update` cannot be conditioned: IAM names no role in a condition,
  and a role has no allow policy of its own. While CI held it, one update could
  add `resourcemanager.projects.setIamPolicy` to a custom role CI already held
  unconditioned (`swarmSecretProvisioner`, `swarmDeployerProjectBuckets`). CI's
  next project `setIamPolicy`, `roles/owner` included, would then be authorised
  by that binding, and the scoped `projectIamAdmin` (#68) would never be
  evaluated. So every custom role `terraform/infra` defined now lives in
  `terraform/bootstrap`. Changing one is an owner apply, and the owner accepted
  that cost.
* **#69.** The quota broker's `swarmSecretLister` grant moves to
  `terraform/bootstrap`, and the role leaves `deployer_grantable_project_roles`.
  The role carries project-wide `secrets.setIamPolicy`, which reaches the other
  team's 63 secrets. The scoped `projectIamAdmin` limits *which* roles CI may
  modify, never *whose* member it adds, so while the role was on the list CI
  could grant it to itself.
* **#80's order.** One bootstrap change per release. This is step 4: after
  PR #63's IAP clients, after #80's six changes and their release, and after
  PR #73's `projectIamAdmin` scoping and its release.

## What moves

| object | `terraform/infra` address (forgotten) | `terraform/bootstrap` address (imported) | import id |
|---|---|---|---|
| `swarmJobDispatcher` | `module.iam.google_project_iam_custom_role.job_dispatcher` | `google_project_iam_custom_role.platform["job_dispatcher"]` | `projects/saga-agents-staging/roles/swarmJobDispatcher` |
| `swarmJobReaper` | `module.iam.google_project_iam_custom_role.job_reaper` | `…platform["job_reaper"]` | `projects/saga-agents-staging/roles/swarmJobReaper` |
| `swarmGkeDispatcher` | `module.iam.google_project_iam_custom_role.gke_dispatcher[0]` | `…platform["gke_dispatcher"]` | `projects/saga-agents-staging/roles/swarmGkeDispatcher` |
| `swarmGkeReaper` | `module.iam.google_project_iam_custom_role.gke_reaper[0]` | `…platform["gke_reaper"]` | `projects/saga-agents-staging/roles/swarmGkeReaper` |
| `swarmSecretLister` | `module.iam.google_project_iam_custom_role.secret_lister` | `…platform["secret_lister"]` | `projects/saga-agents-staging/roles/swarmSecretLister` |
| `swarmTenantWorkerFirestore` | `module.tenancy.google_project_iam_custom_role.worker_firestore` | `…platform["worker_firestore"]` | `projects/saga-agents-staging/roles/swarmTenantWorkerFirestore` |
| `swarmBucketMetadataReader` | `module.tenancy.google_project_iam_custom_role.bucket_metadata_reader` | `…platform["bucket_metadata_reader"]` | `projects/saga-agents-staging/roles/swarmBucketMetadataReader` |
| `swarmImagePuller` | `module.artifact_registry.google_project_iam_custom_role.image_puller[0]` | `…platform["image_puller"]` | `projects/saga-agents-staging/roles/swarmImagePuller` |
| broker's `swarmSecretLister` grant | `module.iam.google_project_iam_member.plain["swarm-quota-broker:projects/saga-agents-staging/roles/swarmSecretLister"]`, moved to `…broker_secret_lister_moved_to_bootstrap` and forgotten | `google_project_iam_member.broker_secret_lister["swarm-quota-broker"]` | `saga-agents-staging projects/saga-agents-staging/roles/swarmSecretLister serviceAccount:swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com` |

That is eight roles, counted from the code: `grep -rn 'resource "google_project_iam_custom_role"'`
over `terraform/infra` and `terraform/modules` on `origin/main` at `89f2e09`.
The live project has nine custom roles. The ninth, `swarmSecretProvisioner`,
was always `terraform/bootstrap`'s. The eight infra addresses above are the
ones in `gs://swarm-tfstate-saga-agents-staging/infra/dev/default.tfstate`
(read-only `gcloud storage cat`, 2026-09-25). It is the only `terraform/infra`
state in the bucket.

The import-id formats come from the pinned provider's documentation
(hashicorp/google 6.50.0). A custom role is `projects/{{project}}/roles/{{role_id}}`.
A project IAM member is `"{{project_id}} {{role}} {{member}}"`, space-separated,
with a custom role named in full. A condition title would be appended, but
the broker's grant has none.

**Nothing live changes.** `tests/terraform/platform_roles.tftest.hcl` holds
every bootstrap role's id, title, description, stage and permission set to
[`platform_custom_roles_before_the_move.json`](../../tests/terraform/platform_custom_roles_before_the_move.json).
That file was read from the live project, and in CI it was held to `main`'s
module definitions before the move. The test file that did that is not in the
tree: `tests/terraform/platform_roles_fixture.tftest.hcl` planned
`modules/iam`, `modules/tenancy` and `modules/artifact_registry` as they stood
on `main`, and the commit that moved the roles deleted it, because those
module resources no longer exist. It is readable as
`git show 15856da:tests/terraform/platform_roles_fixture.tftest.hcl`, and its
one CI result is terraform run 36118571454, where its four runs passed. The
etags below were read at the same time as the file. A role that is only
imported keeps its etag.

| role | etag, 2026-09-25 08:52 UTC |
|---|---|
| `swarmJobDispatcher` | `BwZbkv6TXtI=` |
| `swarmJobReaper` | `BwZbkv6ShuE=` |
| `swarmGkeDispatcher` | `BwZbtLf8NOQ=` |
| `swarmGkeReaper` | `BwZbtLfwRYA=` |
| `swarmSecretLister` | `BwZcFzmf-lM=` |
| `swarmTenantWorkerFirestore` | `BwZbkv6Utjk=` |
| `swarmBucketMetadataReader` | `BwZbkv6TkZA=` |
| `swarmImagePuller` | `BwZbkzNLXmo=` |

## The order, derived from the code

### Step 1: merge; the release forgets

The merge's release runs `terraform/infra`. `terraform/infra/custom_roles_moved_to_bootstrap.tf`
holds a `removed { … lifecycle { destroy = false } }` block for each role. It
also holds a `moved` block that first gives the broker's grant an address of
its own, because a `removed` block cannot name one instance of a `for_each`.
The apply drops the nine objects from `infra/dev`'s state and touches nothing
live.

* **CI still holds `roleAdmin` here, and must.** A `removed` block still
  refreshes its object during the plan. That was measured in this repository
  when the IAP bindings left `modules/frontend`, whose comment records it. For
  a role, that refresh is `iam.roles.get`.
* The same apply writes the output `custom_roles_owner = "terraform/bootstrap"`
  to `infra/dev`'s state. Step 2 waits for it.

The release's `terraform apply (dev)` plan, derived. Other changes that
release carries (image digests, and so on) come on top.

```text
  # module.artifact_registry.google_project_iam_custom_role.image_puller[0] will no longer be managed by Terraform
  # module.iam.google_project_iam_custom_role.gke_dispatcher[0] will no longer be managed by Terraform
  # module.iam.google_project_iam_custom_role.gke_reaper[0] will no longer be managed by Terraform
  # module.iam.google_project_iam_custom_role.job_dispatcher will no longer be managed by Terraform
  # module.iam.google_project_iam_custom_role.job_reaper will no longer be managed by Terraform
  # module.iam.google_project_iam_custom_role.secret_lister will no longer be managed by Terraform
  # module.iam.google_project_iam_member.broker_secret_lister_moved_to_bootstrap will no longer be managed by Terraform
  #   (moved from module.iam.google_project_iam_member.plain["swarm-quota-broker:projects/saga-agents-staging/roles/swarmSecretLister"])
  # module.tenancy.google_project_iam_custom_role.bucket_metadata_reader will no longer be managed by Terraform
  # module.tenancy.google_project_iam_custom_role.worker_firestore will no longer be managed by Terraform

Changes to Outputs:
  + custom_roles_owner = "terraform/bootstrap"
```

The summary line gains `9 to forget`. **Stop the release** if it shows any of
these:

* a `google_project_iam_custom_role` **destroy**;
* a `google_project_iam_member.plain` destroy;
* any change to `worker_firestore`, `worker_bucket_metadata`, `pullers`, `gke`
  or the other `plain` grants.

Each of those grants names its role by the same string as before, now read
from `terraform/modules/custom_role_ids`, so none should change.

`scripts/lib/plan-guard.sh --mode apply` treats a forget as neither a deletion
nor a creation. It may list the nine under its non-fatal "does not identify
itself as ours" warning: custom roles and IAM members carry no label, and the
roles' ids are camelCase. That warning is expected.

**Check step 1 landed** (read-only):

```bash
gcloud storage cat gs://swarm-tfstate-saga-agents-staging/infra/dev/default.tfstate \
  | jq '{owner: .outputs.custom_roles_owner.value,
         roles: [.resources[] | select(.type=="google_project_iam_custom_role")] | length,
         lister: [.resources[] | select(.name=="plain") | .instances[] | select(.index_key|tostring|test("SecretLister"))] | length}'
```

Expected: `{"owner": "terraform/bootstrap", "roles": 0, "lister": 0}`.

### Step 2: the owner adopts, and roleAdmin comes off

From the checkout that holds `terraform/bootstrap/terraform.tfstate`, on a
`main` that contains this merge, between releases, in zsh.

(Written while bootstrap state was a local file. Since #827 it is in the state
bucket at `bootstrap/`, and any up-to-date `main` checkout reads it. See
[operations](../operations.md#the-bootstrap-layers-state).)

**Pre-flight**, all read-only:

```bash
cd <the checkout with the bootstrap state>
git fetch origin && git status && git log -1 --oneline origin/main
gh run list --repo bogdan-alexandrescu/SwarmCloud --branch main --status in_progress
gh run list --repo bogdan-alexandrescu/SwarmCloud --branch main --status queued
echo "SWARM_ASSUME_YES=${SWARM_ASSUME_YES-<unset>}"
```

* Both run lists must be empty, and `SWARM_ASSUME_YES` must print `<unset>`.
* Step 1's check above must print the expected object.
* #80's six changes and PR #73's scoping must already be applied. Otherwise the
  untargeted plan shows them too, and one change per release is broken.

**The apply**, targeted, with a typed `apply`:

```bash
scripts/bootstrap.sh \
  --target 'google_project_iam_custom_role.platform' \
  --target 'google_project_iam_member.broker_secret_lister' \
  --target 'google_project_iam_member.deployer_roles["roles/iam.roleAdmin"]' \
  --target 'google_project_iam_member.deployer_project_iam_admin'
```

The quotes are needed, because zsh globs `[...]`. The last target is PR #73's
conditioned grant. Before #73's apply it has no instance and plans nothing.

**The plan, as derived on 2026-09-25, before any apply.** This shape is
**wrong about `deployer_project_iam_admin`** — it assumed PR #73's single
conditioned resource already existed in `terraform/bootstrap`'s state and
would be *replaced*. It never had; kept here only as the historical record of
what was expected going in, and because "what changed and why" below explains
exactly how reality diverged from it.

```text
  # google_project_iam_custom_role.platform["bucket_metadata_reader"] will be imported
  # google_project_iam_custom_role.platform["gke_dispatcher"] will be imported
  # google_project_iam_custom_role.platform["gke_reaper"] will be imported
  # google_project_iam_custom_role.platform["image_puller"] will be imported
  # google_project_iam_custom_role.platform["job_dispatcher"] will be imported
  # google_project_iam_custom_role.platform["job_reaper"] will be imported
  # google_project_iam_custom_role.platform["secret_lister"] will be imported
  # google_project_iam_custom_role.platform["worker_firestore"] will be imported
  # google_project_iam_member.broker_secret_lister["swarm-quota-broker"] will be imported
  # google_project_iam_member.deployer_project_iam_admin[0] must be replaced
      ~ condition {
          ~ expression = "api.getAttribute(…).hasOnly([… \"projects/saga-agents-staging/roles/swarmSecretLister\", …])"
                      -> "api.getAttribute(…).hasOnly([…])"   # forces replacement
        }
  # google_project_iam_member.deployer_roles["roles/iam.roleAdmin"] will be destroyed
  #   (because key ["roles/iam.roleAdmin"] is not in for_each map)

Plan: 9 to import, 1 to add, 0 to change, 2 to destroy.
```

**What the real, first apply showed instead, 2026-09-28 23:21Z.**
`deployer_project_iam_admin[0]` had never been created in bootstrap state, so
this was a **create**, not a replace — `google_project_iam_member.
deployer_project_iam_admin[0] will be created`, still counted as "1 to add, 0
to change, 1 to destroy" (the one destroy being `roleAdmin`; the plan
apparently matched the derived shape, since Terraform's mock-free plan cannot
see GCP's `hasOnly()` element-count lint). The nine imports and the `roleAdmin`
destroy applied cleanly; the create failed at the API with GCP's
`LintValidationUnits/ListLengthCheck` — `hasOnly()` refuses a list over 10
elements, and PR #73's single condition named all fifteen
`deployer_grantable_project_roles` in one call. Filed as
[#275](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/275); fixed by
[#277](https://github.com/bogdan-alexandrescu/SwarmCloud/pull/277), which
replaced the single conditioned resource with a `for_each` of one binding per
chunk of at most 10 roles (`deployer_project_iam_admin_conditions` in
`deployer_conditions.tf`). See "What actually happened" above for the full
timeline, including the one-minute gap and the hand restore, and
[docs/ci.md](../ci.md#the-deployers-refusal-is-proven-once-by-a-probe-the-owner-dispatches)
for the six-step order that landed the chunked grants without repeating that
gap.

**For a fresh project applying today's code** (#277 already merged, so
`deployer_project_iam_admin` is always the chunked `for_each`), there is no
prior unconditioned live grant to transition away from by hand, so the
out-of-band gcloud dance in docs/ci.md does not apply — it exists only to
retire *this* project's hand-restored grant from #275's incident. A first
apply that includes `deployer_project_iam_admin` in the target set creates
both chunk bindings directly: `Plan: 9 to import, 2 to add, 0 to change, 1 to
destroy` (the two chunks created, `roleAdmin` destroyed, no replace).

**On a fresh project, an infra apply before the bootstrap apply fails, and
that failure is expected.** The custom roles (`swarmJobDispatcher`,
`swarmSecretLister`, `swarmTenantWorkerFirestore` and the rest, named in
`terraform/modules/custom_role_ids`) are created only by `terraform/bootstrap`
now; `terraform/infra` grants them by id and creates none. Run infra first
and every one of those grants reaches IAM naming a role that is not there, so
the apply stops with IAM's refusal, of the form:

```text
Error 400: Role (projects/<project>/roles/swarmSecretLister) does not exist in the resource's hierarchy., badRequest
```

(the role id varies with whichever grant Terraform reached first). Nothing is
wrong with the infra code and nothing needs importing: the role it names
simply has no owner yet in that project. **The fix is the order, not the
grant:** apply `terraform/bootstrap` first (it creates the roles), then re-run
the infra apply, which then binds roles that exist. Do not work around it by
creating the role by hand with `gcloud iam roles create`: bootstrap's own
create of that role then collides with it, and a hand-made role carries
permissions no reviewed file defines.

* The condition change (for a role transitioning from unconditioned to scoped
  after this point, not `projectIamAdmin` on a fresh project) is a
  **replacement**, not an update: every argument of a
  `google_project_iam_member`, `condition` included, forces a new resource.
  Terraform destroys the old binding before it creates the new one, so for a
  few seconds, and up to IAM's propagation, CI has no grant for that role.
  Between releases nothing needs it.
* Destroying `deployer_roles["roles/iam.roleAdmin"]` removes only the
  deployer's membership. The live binding also holds one person's `user:`
  account, the owner's (read 2026-09-25), which stays.

**Type anything but `apply`** if the plan shows any of these:

* an import whose object also shows `~ update in-place` or
  `must be replaced`. That is a permission, title or description that differs
  from the live role, and the fixture test says it cannot, so stop and look;
* any `google_project_iam_custom_role` create or destroy;
* a `deployer_roles` destroy other than `roleAdmin`;
* any other resource.

#### If step 2 runs before step 1

**It is refused.** Every resource being adopted carries a precondition. For
each state named in `adopt_from_infra_states`, which `terraform.tfvars` sets
to `["infra/dev"]`, that state's `custom_roles_owner` output must read
`terraform/bootstrap`. Only the apply that runs the `removed` blocks writes it.
A `terraform.yml` plan writes nothing. So the plan fails with:

```text
Error: Resource precondition failed
  terraform/infra state infra/dev has not released the platform's custom roles: …
```

That changes nothing. Wait for step 1's release, then run the command again.

**What the precondition prevents.** Suppose the imports were accepted while
`infra/dev` still held the roles. Two states would manage each role. The same
apply would destroy CI's `roleAdmin`. Every later `terraform/infra` plan would
then 403 on `iam.roles.get` refreshing roles it still thought it owned. That
includes the step-1 release, whose `removed` blocks refresh before they forget.
Every release and every `terraform.yml plan (dev)` would stay red until
`roleAdmin` was re-granted by hand or someone ran `terraform state rm` against
`infra/dev`.

**The reverse is safe.** If step 1 lands and step 2 waits, the nine objects
exist, unmanaged and unchanged. CI still holds `roleAdmin`, as before this
change, and releases stay green. Nothing needs to be rushed.

**A fresh project** leaves `adopt_from_infra_states` empty: there is nothing
to import and no state to read. Run the first bootstrap apply with
`grant_broker_secret_lister = false`, because the broker's account is
`terraform/infra`'s. The roles must exist before `terraform/infra`'s first
apply, which grants them.

### Step 3: the next release proves it

Wait at least 7 minutes after the apply for IAM to propagate, then check it
structurally (read-only):

```bash
P=saga-agents-staging
gcloud projects get-iam-policy "$P" --flatten=bindings \
  --filter='bindings.members:swarm-tf-deployer@' \
  --format='table(bindings.role,bindings.condition.title)'
gcloud projects get-iam-policy "$P" --flatten=bindings \
  --filter='bindings.role:roles/swarmSecretLister' \
  --format='value(bindings.members)'
for r in swarmJobDispatcher swarmJobReaper swarmGkeDispatcher swarmGkeReaper \
         swarmSecretLister swarmTenantWorkerFirestore swarmBucketMetadataReader swarmImagePuller; do
  printf '%s ' "$r"; gcloud iam roles describe "$r" --project "$P" --format='value(etag)'
done
~/.local/bin/terraform -chdir=terraform/bootstrap state list | grep -c 'google_project_iam_custom_role.platform'
```

The role names in that `for` are literal words, not a variable, so zsh loops
over all eight (it does not split an unquoted variable).

* **The first table** has no `roles/iam.roleAdmin` row. `roles/resourcemanager.
  projectIamAdmin` appears exactly twice, conditioned, titled "only the roles
  terraform infra grants (chunk 1 of 2)" and "…(chunk 2 of 2)" (#277) — never
  with an empty condition title, which would mean the unconditioned grant is
  still live (this project's #275 hand restore was removed 2026-09-29; see
  "What actually happened" above).
* **The second** prints the broker, `serviceAccount:swarm-quota-broker@…`.
* **Each etag** equals the table under "What moves". A changed etag means a
  role was updated, which this move must never do.
* **The count** is `8`.

The next release, whatever it carries, proves the rest.

* **Its `terraform apply (dev)` plans and applies with no
  `google_project_iam_custom_role` in the plan and no 403.** CI holds no
  `iam.roles.*` permission any more, so a plan that still reached a custom
  role would fail loudly there. What that rests on, measured read-only on
  2026-09-25 at 14:34 UTC:
  * `gcloud projects get-iam-policy` shows the deployer in **18** project-level
    bindings, all unconditioned.
  * `gcloud iam roles describe` on each of the 18, and on
    `roles/logging.viewAccessor`, which `verify_logs.tf` grants once applied.
    **Only `roles/iam.roleAdmin` carries any `iam.roles.*` permission**: ten,
    `iam.roles.get` among them. `swarmSecretProvisioner` has 15 permissions
    and no `iam.*` one.
  * **`swarmDeployerProjectBuckets` is not measured.** `wif.tf` grants it, but
    its bootstrap apply has not happened, so `describe` returns `NOT_FOUND`.
    For that role the answer is **derived from the code**: `wif.tf` gives it
    exactly `storage.buckets.create` and `storage.buckets.list`. Being
    bootstrap's, it cannot gain a permission from CI once `roleAdmin` is
    gone.
  * Grants on single resources (`iam.serviceAccountUser` on service accounts,
    object roles on buckets) are not counted. `iam.roles.get` on a project's
    custom role is checked against the project's policy.
  `gh run view <id> --repo bogdan-alexandrescu/SwarmCloud --log | grep -c google_project_iam_custom_role`
  should print `0`.
* **It does not prove that CI is refused `iam.roles.update`.** Nothing in the
  pipeline attempts it. Only a deliberate probe as the deployer would, and
  that is the owner's call. Structurally, the missing binding is the proof.

## Revert

**Fast, out of band**, if something in CI turns out to need a role permission:

```bash
P=saga-agents-staging
gcloud projects add-iam-policy-binding "$P" \
  --member="serviceAccount:swarm-tf-deployer@${P}.iam.gserviceaccount.com" \
  --role=roles/iam.roleAdmin --condition=None
```

Terraform does not see this binding and will not remove it. Record it on #79.

**Never revert the code with a plain `git revert` and an apply.** The roles
would leave `terraform/bootstrap`'s configuration while they are still in its
state, and its next apply would **delete all eight**. A deleted custom role
grants nothing to anyone bound to it, and its id is locked for 7 days. The
revert is this move in mirror image:

1. a bootstrap change with `removed { … destroy = false }` for
   `google_project_iam_custom_role.platform` and
   `google_project_iam_member.broker_secret_lister`, and `roles/iam.roleAdmin`
   back in `deployer_roles` and the reviewed list. The owner applies it;
2. then an infra change that restores the module resources with `import`
   blocks, using the ids in the table above. CI needs `roleAdmin` from
   step 1 to read them.

## Now verified, live (2026-09-28/29) — superseding the plans above

* **Both roots have now actually been planned and applied.** #239's release
  (2026-09-28 20:15Z) ran `terraform/infra`'s `removed`/`moved` chain for real;
  the owner's two bootstrap applies (2026-09-28 23:21Z and 2026-09-29 ~02:35Z)
  ran for real. See "What actually happened" above for the exact sequence,
  including the one divergence from what was derived (`deployer_project_iam_
  admin[0]` was a *create*, not a *replace* — #275).
* **The nine imports had no diff**, exactly as derived: no permission, title,
  description or member change on any of the eight custom roles or the
  broker's grant.
* **The `moved` plus `removed` chain worked for the broker's single `for_each`
  instance** (the pattern from hashicorp/terraform#34439) — #239's release
  applied clean.
* **The deployer's `roleAdmin` binding is gone**, confirmed structurally.

## Still not verified

* **The exact wording of Terraform 1.16.2's forget and import summaries** was
  not captured verbatim from either apply's output.
* **Nothing measured that CI is refused `iam.roles.update` after step 2.**
  That is the deliberate probe in
  [docs/ci.md](../ci.md#the-deployers-refusal-is-proven-once-by-a-probe-the-owner-dispatches),
  owner-dispatched separately, and [#276](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/276)
  notes the probe script itself does not yet expect two chunked
  `projectIamAdmin` bindings.
* **`swarmDeployerProjectBuckets` carrying no `iam.roles.*` permission is
  derived, not measured.** The role is not live yet. Once its bootstrap apply
  has run, `gcloud iam roles describe swarmDeployerProjectBuckets --project
  saga-agents-staging` is the measurement.
* **The next ordinary release's `terraform apply (dev)`** — the proof that the
  admitted side (imports, `roleAdmin` gone, the two chunked
  `projectIamAdmin` grants) plans and applies clean with no custom role and no
  403 — had not yet run as of this PR.
