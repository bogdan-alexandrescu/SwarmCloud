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

**The TypeSafe forwarder rolls the same way.** `scripts/roll-typesafe-fwd.sh`
(lane TS-ROLL, part of #1347) does for the Deployment
`swarm-system/swarm-typesafe-fwd` what `scripts/roll-graph.sh` does for
`swarm-graph`: it applies `terraform output -raw swarm_typesafe_fwd_manifest`
(terraform/infra/typesafe_fwd.tf) through `kubernetes/apply.sh
--swarm-typesafe-fwd`, which refuses an unfilled `__PLACEHOLDER__`, Kubernetes
RBAC, a Secret and any namespace but `swarm-system`, then waits for `rollout
status` and fails unless its pod runs the manifest's digest. Same flags, same
`agents-staging` refusal; the release and hotfix run it after the swarm-graph
roll. Until `typesafe_forwarder.serve` is set the output is null, and
terraform leaves a null output out of state, so the named read fails "not
found": the script tells that apart with one unnamed `terraform output -json`,
prints a notice and exits 0 without asking gcloud or the cluster anything. A
state terraform cannot read at all is still a failure.

**swarm-voice rolls the same way** (lane VOICE-ROLL, part of #2688; Voice
it's phase one, docs/design/voice-it.md §3.4). `scripts/roll-voice.sh` does
for the one-replica StatefulSet `swarm-system/swarm-voice` what
`scripts/roll-graph.sh` does for `swarm-graph`: it applies `terraform output
-raw swarm_voice_manifest` (terraform/infra/voice.tf) through
`kubernetes/apply.sh --swarm-voice`, which refuses an unfilled
`__PLACEHOLDER__`, Kubernetes RBAC, a Secret and any namespace but
`swarm-system`, then waits for `rollout status statefulset/swarm-voice` and
fails unless its pod runs the manifest's digest. Same flags, same
`agents-staging` refusal, and one more: it applies to `swarm-autopilot`
(`GKE_CLUSTER`) only, because only that cluster's workload pool holds the
binding that lets the swarm-voice KSA act as `swarm-voice@`; any other
`swarm-` cluster is refused before its credentials are fetched. The release
and hotfix run it after the swarm-typesafe-fwd roll. One replica means a roll
replaces the pod: a live voice session loses the turn in flight, not the
conversation. Until `voice_it = "on"` with a swarm-voice digest the output is
null, and the script and `apply.sh --swarm-voice` each print a notice and exit
0 without asking gcloud or the cluster anything; a state terraform cannot read
is still a failure.
