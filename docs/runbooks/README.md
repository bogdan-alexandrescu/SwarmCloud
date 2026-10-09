# Runbooks

One line per runbook in this directory, saying when to reach for it, in the
order an operator meets them: set-up, then day-to-day, then incidents.
[`tests/unit/scripts/test_runbook_index.py`](../../tests/unit/scripts/test_runbook_index.py)
fails when a runbook is added or removed without this page following.

## Set-up

Once per deployment, roughly in the order they are done.

- [Registering the SwarmCloud GitHub App](github-app.md): the owner registers the App SwarmCloud uses to act on GitHub as the user, once per deployment, and again only to rotate its client secret or private key.
- [Create and install the merge GitHub App](merge-app.md): the owner, once, so a `ready` pull request is merged by `auto-merge.yml` instead of only while an operator runs the merge watcher.
- [One Desktop OAuth client, so developers sign in to IAP as themselves](iap-desktop-client.md): the owner, once per deployment, when a gcloud user token is refused at IAP with 401 / error code 900 and developers reach the API only by impersonating `swarm-verify`.
- [CI loses `roles/iam.roleAdmin`](custom-roles-to-bootstrap.md): the owner, once, to move the custom roles and the broker's `swarmSecretLister` grant to the bootstrap root and remove `roles/iam.roleAdmin` from `swarm-tf-deployer`.
- [Apply and verify the deployer's trust pin](deployer-trust-pin.md): the owner, once, to apply #457's `job_workflow_ref` pin on `swarm-tf-deployer` and check the live policy with `scripts/verify-deployer-trust.sh`; again after any change to `deployer_workflows` or `github_allowed_refs` in `terraform/bootstrap/wif.tf`.
- [Prove, once, that the deployer is refused a role it does not hand out](iam-refusal-probe.md): the owner, once, after PR #73's bootstrap apply, IAM propagation and the release after it.
- [Step-spec signing: rollout, rotation and revocation](spec-signing-rollout.md): rolling out step-spec signing from `legacy` to `enforce` (before 2026-10-20); return to it to rotate the signing key or revoke a version.
- [Prove the destroy guard against a real terraform plan](destroy-guard-real-plan-proof.md): before trusting `make destroy` in an environment for the first time; again after changing the destroy guard, the plan guard, the deny-list or the unlabelable types, or when the recorded plan no longer matches the real infrastructure.

## Day-to-day

- [Offboard a tenant](tenant-offboarding.md): a tenant (a Google group, or a personal `u-<user>` tenant) is leaving the platform and everything provisioned for it has to go.

## Incidents

- [Apply the GKE dispatcher RBAC and redispatch](gke-dispatch-redispatch.md): `browser` tasks fail with `jobs.batch is forbidden`, or you are landing the GKE dispatch fix for the first time.
- [Browser pods the reconciler evicts](browser-eviction.md): a browser task was re-queued or failed with `stuck_no_progress`, a task's timeline shows a `generation_fenced` event with `finding: left_running`, or you want to know whether the reconciler is evicting browser pods, and why.
