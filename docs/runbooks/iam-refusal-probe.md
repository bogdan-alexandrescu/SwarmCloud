# Prove, once, that the deployer is refused a role it does not hand out

**Who:** the owner. **When:** once, after step 3 of the order recorded on #80:
PR #73 merged, its targeted bootstrap apply done, about 7 minutes for IAM to
propagate, and the release after it. **Takes:** a two-minute run.
**Changes:** nothing, if the condition works. If it does not, the deployer holds
`roles/browser` for the seconds between the grant and the revert. That role
adds nothing it does not already have (below).

This is issue #68's refusal-side proof, decided by the owner on 2026-09-25. What
a pass proves, and what it cannot, is in
[docs/ci.md](../ci.md#the-deployers-refusal-is-proven-once-by-a-probe-the-owner-dispatches).

## What runs

`.github/workflows/iam-refusal-probe.yml`, dispatch only, on `main` only. It
signs in as `swarm-tf-deployer` with the same auth step `release.yml` uses and
runs [`scripts/iam-refusal-probe.sh`](../../scripts/iam-refusal-probe.sh) in
four steps:

| step | what it does | writes? |
|---|---|---|
| `preflight (read-only)` | Stops unless the verdict can only be the condition's. The conditioned `projectIamAdmin` is live, and the unconditioned one is gone. The condition does not list `roles/browser`. The deployer holds no `roles/browser` binding yet. No other role it holds carries `resourcemanager.projects.setIamPolicy`. | no |
| `ask for roles/browser as the deployer` | `gcloud projects add-iam-policy-binding`, deployer to itself, `--condition=None`. Passes **only** on a PERMISSION_DENIED for `saga-agents-staging:setIamPolicy`. | only if the condition fails |
| `revert and read back` | Runs on every path after preflight, including a failed or cancelled grant. Reads the policy. If `roles/browser` is on the deployer, it removes exactly that binding, reads again to confirm, and fails. | only to undo the grant |
| `what this run means` | Writes one line to the run summary. | no |

The project policy is never printed: the repository is public and the policy
names the other team's identities. gcloud errors are printed through `redact`.

**Why `roles/browser`.** Nothing in `terraform/` grants it, and it is not in
`deployer_grantable_project_roles`, so the condition must refuse it. If the
condition is broken and the grant lands, the role gives the deployer nothing new.
Its six permissions (`gcloud iam roles describe roles/browser`, 2026-09-25) are
project, folder and organisation reads. `projectIamAdmin` already has the
project ones, and this project has no organisation or folder.

## Before you dispatch (read-only)

0. The pull request that adds `iam-refusal-probe.yml` is merged. GitHub
   dispatches only a workflow that exists on the default branch. Before the
   merge, `gh workflow run` and `gh run list --workflow iam-refusal-probe.yml`
   answer `HTTP 404: workflow iam-refusal-probe.yml not found on the default
   branch` (measured 2026-09-25, with the pull request open).

1. The targeted apply has landed:

   ```bash
   gcloud projects get-iam-policy saga-agents-staging --format=json \
     | jq -r '.bindings[]
         | select(.role == "roles/resourcemanager.projectIamAdmin")
         | select(any(.members[]; startswith("serviceAccount:swarm-tf-deployer@")))
         | (.condition.title // "NO CONDITION")'
   ```

   Expect exactly one line, `only the roles terraform infra grants`. A
   `NO CONDITION` line means the apply has not landed, or has been reverted.
   Preflight would stop there anyway, and nothing would be written.

2. At least 7 minutes have passed since the apply.

3. No release is running (`gh run list --workflow release.yml --limit 3`). Both
   runs write the project policy. That is not dangerous, but a concurrent change
   makes gcloud retry on 409, which adds noise to a run you will want to read
   once.

Run it **before step 4 (PR #150)**. That step takes `roles/iam.roleAdmin` off
the deployer, and with it the permission to read the deployer's custom roles.
After it, preflight reports those roles as unread. The run still works: a
refusal is still proof, because no role admitted the write. But a grant could
then no longer be pinned on the condition.

## Run it

```bash
gh workflow run iam-refusal-probe.yml --ref main
gh run list --workflow iam-refusal-probe.yml --limit 1   # note the run id
gh run watch <run-id> --exit-status
```

`--ref main` is required. The workload identity provider mints the deployer's
token for `refs/heads/main` only (`terraform/bootstrap/wif.tf`). The first step
refuses any other ref by name, before the auth step is reached.

## What each outcome means

| the run shows | it means | do |
|---|---|---|
| green; summary **PASS** | IAM refused the deployer's grant of `roles/browser` with PERMISSION_DENIED on `saga-agents-staging:setIamPolicy`. The policy read back afterwards has no such binding. | Comment on #68 with the run URL: the refusal side is proven. #68's fifth criterion (#79) is **not** met by this. |
| red at `refuse any ref but main` | It was dispatched on another ref. Nothing authenticated. | Dispatch again with `--ref main`. |
| red at `google-github-actions/auth` | WIF refused the token, or a repository variable is missing. Nothing was attempted. | Read the step. The auth step and variables are the same ones `release.yml` uses. |
| red at `preflight (read-only)` | Nothing was written. Its last line names the reason: the unconditioned `projectIamAdmin` is still there, the condition lists `roles/browser`, the deployer already holds `roles/browser`, a role it holds carries `setIamPolicy`, or the policy could not be read. | Fix what it names, then dispatch again. **A role that carries `setIamPolicy` is a finding in itself.** Take it to #79, because it means CI can already write the project policy without the condition. |
| red at `ask for roles/browser`, **GRANTED — reopen #68**; the revert step says the binding was removed and the read-back confirms it | The condition admitted a role it does not list. | Reopen #68 with the run URL. The binding is already gone. Whether to revert the scoping or fix the condition is an owner decision. |
| red at `revert and read back`, **REVERT FAILED** | The grant landed, and the probe could not remove it. The deployer may still hold `roles/browser`. | Remove it now ([below](#if-the-revert-did-not-finish)). Reopen #68. |
| red at `ask for roles/browser`, **inconclusive**; the revert step is green | Neither a refusal nor a grant: an auth failure, a network error, a disabled API, or a gcloud wording the probe does not recognise. The green revert means the read-back found nothing bound. | Read the grant step's redacted error. If it is a refusal in words `classify` does not know, fix `classify` in `scripts/iam-refusal-probe.sh` in a pull request and run again. **Do not read it as a pass.** |
| red at `ask for roles/browser`, **inconclusive**; the revert step is red and says it removed the binding | gcloud failed, but the grant had landed (for example, a timeout after the server applied it). | The same as GRANTED: reopen #68. |
| cancelled | The revert still runs, under `always()`, unless the run was force-cancelled. | Open the revert step. If it did not run or did not finish, use the check below. |

## If the revert did not finish

Run these as yourself. They print only a count, never the policy:

```bash
PROJECT=saga-agents-staging
DEPLOYER="serviceAccount:swarm-tf-deployer@${PROJECT}.iam.gserviceaccount.com"

gcloud projects get-iam-policy "${PROJECT}" --format=json \
  | jq --arg m "${DEPLOYER}" '[.bindings[] | select(.role == "roles/browser" and any(.members[]; . == $m))] | length'
```

`0`: nothing to do. `1`: remove it, then run the count again, which must print `0`:

```bash
gcloud projects remove-iam-policy-binding "${PROJECT}" \
  --member="${DEPLOYER}" --role=roles/browser --condition=None --format=none
```

`--condition=None` is needed because the project policy already has
conditional bindings, and gcloud refuses a binding change without it when it
cannot prompt. `--format=none` stops gcloud printing the whole new policy.

## Not verified before the run

- **The error text on the runner's gcloud.** gcloud does not print the status
  word PERMISSION_DENIED. The probe matches the sentence gcloud renders for an
  HTTP 403 on `projects/<project>:setIamPolicy`. That sentence was read in the
  Cloud SDK source (`api_lib/util/exceptions.py`, `HttpErrorPayload`, SDK
  483.0.0), not seen on the runner. If a later gcloud words it differently, the
  run is **inconclusive**, never a pass.
- **That IAM evaluates the condition as documented.** Nothing but this run
  shows that.
- **The script itself.** `tests/unit/scripts/test_iam_refusal_probe.py` runs it
  against a fake `gcloud` and passes in CI. It has never run against the live
  project.
