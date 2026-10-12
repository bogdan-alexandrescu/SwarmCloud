# Re-apply the tenant network policies to every tenant namespace

**When:** a tenant's live browser cannot reach the relay, `kubectl get netpol -n
swarm-tenant-<id>` lacks one of `swarm-default-deny`,
`swarm-deny-cross-tenant-ingress`, `swarm-allow-worker-egress` or
`swarm-allow-live-egress`, or a release ran before the step below was in
`release.yml`.

**Why this exists.** On 2026-10-11 the live browser needed
`swarm-allow-live-egress` in each tenant namespace and no release applied it;
the operator ran `kubernetes/apply.sh --tenant` by hand for eng, smoke and
u-sw-c90291. The release's full tenant apply is switched off by
`vars.APPLY_TENANT_NAMESPACES` and walks only Terraform's tenants, never a
workspace tenant (`swarm-tenant-u-*`). `scripts/apply-tenant-network-policies.sh`
walks the cluster's own label instead (`swarm-tenant`) and applies **only** the
NetworkPolicies, so a workspace tenant's quota and service account are never
re-rendered from defaults.

```bash
scripts/apply-tenant-network-policies.sh              # dry run: diff + server verdicts, per namespace
scripts/apply-tenant-network-policies.sh --confirm    # apply, read back by name, parity sweep
scripts/apply-tenant-network-policies.sh --list-files # the policy files it applies (offline)
```

The release runs it with `--get-credentials --confirm` after the tenant
namespace step. It refuses another team's cluster (the deny-list in
`scripts/lib/common.sh`), a context whose cluster entry is not
`gke_<project>_<location>_<cluster>`, and a labelled namespace outside
`swarm-tenant-`. It exits non-zero when no namespace is labelled at all. It
deletes nothing: a policy removed from the repository stays on the cluster
until someone deletes it.

**A new tenant policy file** goes into `kubernetes/render.py` `TENANT_FILES`.
`tests/unit/scripts/test_tenant_netpol_apply.py` fails until it does, and from
then on the release applies it everywhere. It also has to be added to the
workspace deployer's allowed names in
`kubernetes/policies/workspace-provisioner-scope.yaml`.
