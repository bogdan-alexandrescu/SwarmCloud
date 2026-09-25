# CI loses `roles/iam.roleAdmin`: the custom roles and the broker's `swarmSecretLister` grant move to the owner's root

**Who:** the deployment's owner, once. **Takes:** one merge, two releases and
one bootstrap apply, in that order. **Changes:** who manages nine existing
objects — no permission of any role changes, and nothing is created or
deleted — and one binding is removed: `roles/iam.roleAdmin` from
`swarm-tf-deployer`. If PR #73's `projectIamAdmin` scoping is live, its
condition is replaced with one that no longer names `swarmSecretLister`.

The worked values are Saga's `dev` deployment (`saga-agents-staging`, state
prefix `infra/dev`). **Everything in this page labelled "derived" was read
from the code and the provider's documentation, not produced by a plan.** No
plan of either root was run while writing it.

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
module definitions before the move. The etags below were read at the same
time. A role that is only imported keeps its etag.

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

**The plan, derived.** With PR #73 applied, which the order requires:

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

* Without PR #73 applied, the plan has no `deployer_project_iam_admin` line
  and reads `Plan: 9 to import, 0 to add, 0 to change, 1 to destroy.`
* The condition change is a **replacement**, not an update: every argument of
  a `google_project_iam_member`, `condition` included, forces a new resource.
  Terraform destroys the old binding before it creates the new one, so for a
  few seconds, and up to IAM's propagation, CI has no `projectIamAdmin`.
  Between releases nothing needs it.
* Only one expression change is expected: the list loses
  `projects/saga-agents-staging/roles/swarmSecretLister`, and its order is
  otherwise unchanged.
* Destroying `deployer_roles["roles/iam.roleAdmin"]` removes only the
  deployer's membership. The live binding also holds `user:bogdan@saga.xyz`
  (read 2026-09-25), which stays.

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

* **The first table** has no `roles/iam.roleAdmin` row. If #73 is applied,
  `roles/resourcemanager.projectIamAdmin` appears only with the condition
  titled "only the roles terraform infra grants".
* **The second** prints the broker, `serviceAccount:swarm-quota-broker@…`.
* **Each etag** equals the table under "What moves". A changed etag means a
  role was updated, which this move must never do.
* **The count** is `8`.

The next release, whatever it carries, proves the rest.

* **Its `terraform apply (dev)` plans and applies with no
  `google_project_iam_custom_role` in the plan and no 403.** CI holds no
  `iam.roles.*` permission any more. That was measured 2026-09-25 over every
  role the deployer holds: only `roleAdmin` carried any. So a plan that still
  reached a custom role would fail loudly there.
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

## Not verified

* **Neither root has been planned with this change.** Every plan above is
  derived.
  * That the provider imports all nine with no diff rests on the live values
    read on 2026-09-25 and on the fixture test.
  * That `condition` forces replacement of an IAM member rests on the
    provider's schema as read, not on a plan.
* **The `moved` plus `removed` chain for a single `for_each` instance** is the
  pattern Terraform's maintainers confirm on hashicorp/terraform#34439. It has
  not been run against this state.
* **The exact wording of Terraform 1.16.2's forget summary** is not verified.
  The plan above shows its shape.
* **Nothing measured that CI is refused `iam.roles.update` after step 2.**
