# Roll the live relay to the release's image

**When:** the live browser's console shows origin/device "Not reported" after a
release that changed `apps/swarm-live`, the release's "roll the live relay to
this release's image" step is red, or the relay's image is not the swarm-live
digest the release pinned.

**Why this exists.** The relay (StatefulSet `swarm-system/swarm-live` on
`swarm-autopilot`) runs whatever `terraform output -raw swarm_live_manifest`
last rendered (terraform/infra/live.tf). Until lane OPS-RELAY no release applied
that manifest: only `kubernetes/apply.sh --swarm-system --confirm`, by hand. On
2026-10-12 the relay ran a pre-change image twice until the operator applied
it. `scripts/roll-live-relay.sh` is that apply plus what the hand apply never
checked: it applies through `apply.sh --swarm-system` (its manifest checks and
the live-ingress parity check, `kubernetes/network_parity.py --live-ingress`),
waits for `rollout status`, then fails unless every relay pod runs the digest
the manifest names.

```bash
scripts/roll-live-relay.sh                    # dry run: apply.sh's diff and server verdicts
scripts/roll-live-relay.sh --confirm          # apply, parity, rollout, digest read-back
scripts/roll-live-relay.sh --confirm --timeout 900   # an Autopilot node is slow to arrive
```

The release and the hotfix lane run it with `--tf-init --get-credentials
--confirm` after the tenant network policies, before the smoke test. With
`enable_live_browser` off it prints a notice and exits 0; enabled with no
swarm-live digest in `image_refs` it fails. It refuses the shared
`agents-staging` cluster (the deny-list in `scripts/lib/common.sh`) whether it
arrives as `--cluster`, as the context's cluster entry or as terraform's
`gke_cluster_name`, before get-credentials and before anything is applied.

**A pod still on the old image** after a finished rollout is reported by name
with the image it runs. Read `kubectl get pods -n swarm-system -l
app.kubernetes.io/name=swarm-live -o wide` and the StatefulSet's events; a
StatefulSet whose `updateStrategy` was changed to `OnDelete` by hand would
explain it, and is not what `kubernetes/swarm-system/swarm-live.yaml` renders.
