# Apply and verify the deployer's trust pin (#457)

**Who:** the owner. Only the owner applies `terraform/bootstrap`. **When:**
once, as soon as convenient: until it is done, any workflow on `main` that
grants itself `id-token: write` can become the deployer. Again after any change
to `deployer_workflows` or `github_allowed_refs` in
[`terraform/bootstrap/wif.tf`](../../terraform/bootstrap/wif.tf). **Takes:** ten
minutes plus the next push to `main`. **Changes:** the deployer service
account's `roles/iam.workloadIdentityUser` members, and nothing else.

## Why

`swarm-tf-deployer` is CI's identity: project IAM admin (scoped), Cloud Run,
storage, secret provisioning, in a project shared with another team. Its
workload identity binding used to name
`principalSet://.../attribute.repo_ref/<repo>@refs/heads/main`, and **every**
workflow on `main` presents that principal, whatever file it runs. With
`application.yml`, `terraform.yml` and `security.yml` granting `id-token:
write` at workflow level, a merged test or `conftest.py` that `pytest` loads in
the `python` or `integration` job could mint a token and become the deployer.
Merging to `main` was code execution as the deployer
([docs/merge-step.md](../merge-step.md) M4, R4).

The owner chose two independent controls on 2026-10-02, and #465 wrote both:

1. **Job-level grants.** Every workflow grants `id-token: write` only to the
   jobs that run `google-github-actions/auth`.
   `tests/unit/scripts/test_workflow_id_token_scope.py` and
   `test_release_id_token_scope.py` keep it that way. **This already holds:** it
   took effect when #465 merged.
2. **A trust pin.** The binding names `attribute.job_workflow_ref` =
   `<repo>/.github/workflows/<file>@<ref>` for each file in `deployer_workflows`
   and each ref in `github_allowed_refs`. GitHub sets that value from the file
   the job runs, and a caller cannot choose it. So a workflow that is not on the
   list is refused, whatever it grants itself. **This is IAM in the bootstrap
   root, and it does not hold until this runbook is done.** Until then
   `ci-fix.yml`, which grants `id-token: write` at workflow level and is not on
   the list, can still present a token the old binding accepts.

Without a check against the live policy, "the code says it is pinned" and
"it is pinned" can't be told apart.
[`scripts/verify-deployer-trust.sh`](../../scripts/verify-deployer-trust.sh)
reads the live policy and compares it with the Terraform-rendered
`github_principals` output. The script never keeps a list of its own. It fails
on any of these:

- a `repo_ref`, `repository` or `subject` member;
- any member `github_principals` does not render, such as an unlisted workflow
  file or ref;
- an expected member that is missing;
- zero members;
- a provider `attribute_condition` that is empty or admits `refs/pull/`.

It only reads. It calls `terraform init`/`output` on the bootstrap root and
`gcloud ... get-iam-policy` / `describe`, nothing else. gcloud errors are
printed through `redact`.

## Before you start

- Work from a clean checkout of current `main`, with your own gcloud
  credentials. The bootstrap root's state is remote
  (`gs://<state bucket>/bootstrap`, #827).
- **PR #897 edits `terraform/bootstrap/wif.tf`.** If it has merged, apply from a
  `main` that includes it. If it merges after you finish, its own `wif.tf`
  change needs the same targeted apply and the same verification. Run this
  runbook again for it.
- Pick a moment when no `release.yml` or `hotfix.yml` run is in flight. Step 2
  removes one binding and creates others in the same apply. A job that
  authenticates in between can be refused, and then it only needs a re-run.

## Steps

1. **Verify BEFORE.**

   ```bash
   scripts/verify-deployer-trust.sh
   ```

   Expected: it **fails**, with a `repo_ref member:` line that names
   `principalSet://.../attribute.repo_ref/bogdan-alexandrescu/SwarmCloud@refs/heads/main`
   and with `the bootstrap apply has not happened`. Copy the output into the log
   below. It is the control: it shows the check can fail, so step 3's pass means
   something.

   If it **passes** already, someone has applied the pin. Skip to step 4. If it
   fails any other way (an init or read error, an empty `github_principals`),
   stop. That is not a verdict on the policy.

2. **Apply only the deployer's binding.**

   ```bash
   scripts/bootstrap.sh --target 'google_service_account_iam_member.deployer_wif'
   ```

   This uses the same pinned terraform, remote state and typed `apply` as an
   untargeted bootstrap. Don't pass `--yes`, and unset `SWARM_ASSUME_YES`: either
   one skips the prompt where you read the plan. Read the plan before you type `apply`. It must show:

   - **N to add**, one `deployer_wif["<file>@<ref>"]` per (file in
     `deployer_workflows`) × (ref in `github_allowed_refs`). Today that is
     6 × 1 = **6**: `release.yml`, `hotfix.yml`, `application.yml`,
     `terraform.yml`, `security.yml` and `iam-refusal-probe.yml`, each at
     `refs/heads/main`. Every `member` names `attribute.job_workflow_ref`.
   - **1 to destroy**, the old binding whose `member` names
     `attribute.repo_ref/...@refs/heads/main`.
   - **0 to change**, and no other resource address.

   **Stop if the plan shows anything else.** Examples: a different count, a
   `ci-fix.yml` member, a member on `attribute.repository`, a change to the
   provider, or a resource outside `deployer_wif`. Answer anything but `apply`
   and nothing is written. The targeted plan leaves the rest of the bootstrap
   root pending, and the script says so.

3. **Verify AFTER.**

   ```bash
   scripts/verify-deployer-trust.sh
   ```

   Expected: exit 0, ending `checked 6 members, all pinned to workflow files`
   (N from step 2). Copy the output into the log.

4. **The next push to `main` still authenticates.** IAM can take about 7
   minutes to propagate. After that, read the first runs on `main` that start
   after the apply:

   ```bash
   gh run list --branch main --workflow release.yml     --limit 1
   gh run list --branch main --workflow application.yml --limit 1
   gh run list --branch main --workflow terraform.yml   --limit 1
   gh run view <id> --log-failed
   ```

   `release.yml` (its `build` job onwards), `application.yml` `build images`
   and `terraform.yml` `plan` must get past `google-github-actions/auth`. Each of
   them presents its own file's `job_workflow_ref`. A file missing from the list
   fails there with `Permission 'iam.serviceAccounts.getAccessToken' denied`.
   If that happens, re-run once in case it was propagation. If it fails again,
   compare step 3's output with the file the failing run came from.
   `security.yml` `images` (scheduled) and `iam-refusal-probe.yml`
   (owner-dispatched) are checked the next time they run.

5. **Record and close.** Fill in a row of the log below: the date, who applied
   it, the `main` SHA applied from, both verifier outputs and the run IDs from
   step 4. Then close #457. The pull request that added this runbook only says
   "part of #457", on purpose.

## Revert

The old binding is the exposure, so don't put it back. If a legitimate
workflow is refused, add its file to `deployer_workflows` in `wif.tf`.
`test_workflow_id_token_scope.py` requires that list to equal the files whose
auth step names the deployer. Then run steps 1–4 again.

## Log

| date | applied by | main SHA | BEFORE (step 1) | AFTER (step 3) | runs read (step 4) |
|---|---|---|---|---|---|
| | | | | | |
