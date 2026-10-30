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
- [Workspace-secret grant attestation: turn it on, rotate the key](workspace-grant-attestation.md): the owner, once, to serve `swarm-workspace-grant-key`, sign the grants written before SEC-GRANT-ATTEST and turn `grant_attestation_required` on; again to rotate the key, or twice after revoking a sensitive grant.
- [Step-spec signing: rollout, rotation and revocation](spec-signing-rollout.md): rolling out step-spec signing from `legacy` to `enforce` (before 2026-10-20); return to it to rotate the signing key or revoke a version.
- [Onboarding acceptance: the four checks of #780](onboarding-acceptance.md): the owner, once the release carrying OB10 is in dev, to prove a new user from the console and from `/sc` works as themselves in two orgs, an unchosen repository is refused and removing an org revokes access; again after a change to onboarding, grants or the forge credential.
- [Fill and check the build mirrors](build-mirror.md): once, after the apply that creates `swarm-mirror-ghcr`, `swarm-mirror-dockerhub` and the `swarm-build-mirror-<project>` bucket and before the change that points the Dockerfiles at them merges; again whenever a pinned release file in `images/agent-runtime-base/tool-mirror.sha256` changes, or a build fails naming a missing mirror object.
- [Turn on the merge train](merge-train.md): the owner, once, after a dry-run train has gone green, to set `MERGE_TRAIN=on` and turn strict up-to-date off in ruleset 24160219 in favour of the train's `merge-train` status; again to roll it back.
- [The decision-api tailnet egress](tailscale-egress.md): the owner and the tailnet admin, once, to put Jev's internal judgment backends ("Internal Jev", "Internal Laya") on the company tailnet through one in-cluster egress Service; again to rotate the Tailscale OAuth client secret, to verify a labelled pod reaches the decision API, or to roll the egress back.
- [Cut tenant workers over off direct Firestore](isolation-cutover.md): the owner, once per environment, to take each tenant's worker onto swarm-api's state routes (u-bogdan, then eng, then every tenant) and then remove the worker Firestore role (#1457 design B); return to it to roll a step back.
- [Prove the destroy guard against a real terraform plan](destroy-guard-real-plan-proof.md): before trusting `make destroy` in an environment for the first time; again after changing the destroy guard, the plan guard, the deny-list or the unlabelable types, or when the recorded plan no longer matches the real infrastructure.

## Day-to-day

- [Re-apply the tenant network policies to every tenant namespace](tenant-network-policies.md): a tenant namespace lacks one of the tenant NetworkPolicies (the live browser cannot reach its relay), or a policy file was added and the release that applies it has not run.
- [Roll the live relay to the release's image](live-relay-rollout.md): the live browser reports origin/device "Not reported" after a release that changed `apps/swarm-live`, or the release's relay rollout step is red.
- [Read the swarm-images cleanup dry run, then turn deletion on](registry-cleanup.md): the owner or an operator, once dev's release has applied the cleanup policies of #1886 in dry run, to read the would-delete list from the audit logs and compare it with what the KEEP policies hold; again before turning dry run off, or when a needed digest has been deleted.
- [Let browser agents reach a site behind Cloudflare Access with a service token](cf-access-service-token.md): a site the browser agents must open sits behind Cloudflare Access, whose sign-in an agent cannot pass: make a service token, add it to the Access application's policy as a Service Auth rule, store it as a workspace secret granted to `claude-code-browser`'s `cf-access` slot with the hosts it may be sent to; again to rotate it, change its hosts or remove it.
- [Let SwarmCloud's visual QA past the CAPTCHA on an app you own](qa-challenge-bypass.md): an app's owner wants a QA target's runs to pass their own CAPTCHA or WAF challenge (a sign-in ending `qa_sign_in_challenge`, or screens captured as a challenge page): the vendors' always-pass test keys in staging, a skip rule on a header in production, and the target's write-only challenge bypass; again to rotate or remove its value.
- [Publish a SwarmCloud release, then promote it](release.md): cutting a SwarmCloud release: dispatch `publish-release` with the version and a main commit, approve the `release` environment, then promote by hand after the manual test; once beforehand, grant CI's identity the three release roles.
- [Offboard a tenant](tenant-offboarding.md): a tenant (a Google group, or a personal `u-<user>` tenant) is leaving the platform and everything provisioned for it has to go.

## Incidents

- [Apply the GKE dispatcher RBAC and redispatch](gke-dispatch-redispatch.md): `browser` agents fail with `jobs.batch is forbidden`, or you are landing the GKE dispatch fix for the first time.
- [The Cloud Run Jobs start-time support case](cloud-run-start-support-case.md): the owner is filing the Google Cloud support case decided on 2026-10-07 (#625), or a Cloud Run profile's DISPATCHED -> STARTING has jumped again with nothing of ours changed.
- [Raising a throttled tenant](quota-throttled-tenant.md): a tenant queues behind `provider:<p>:tenant:<t> full` with its pool's `hard_limit` and quota's `configured_hard_max` well above what runs, because a 429 halved its AIMD target and it has not climbed back; a platform admin resets the target, clamped and audited.
- [Browser pods the reconciler evicts](browser-eviction.md): a browser agent was re-queued or failed with `stuck_no_progress`, an agent's timeline shows a `generation_fenced` event with `finding: left_running`, or you want to know whether the reconciler is evicting browser pods, and why.
