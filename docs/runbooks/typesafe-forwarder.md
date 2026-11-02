# The TypeSafe forwarder: serve a workspace, see one call go through, roll back

The forwarder (`apps/swarm-typesafe-fwd/`, Deployment `swarm-typesafe-fwd`
in `swarm-system`; design in
[workspace-secrets.md §3.5](../design/workspace-secrets.md)) lets an agent pod
call TypeSafe's Jev **without the workspace's TypeSafe key ever entering the
pod**. The worker runs a loopback shim; the shim asks swarm-api for a
short-lived ticket naming the attempt (`swarm_api.typesafe_ticket`) and sends
each `POST /v1/systemone` to the forwarder with it; the forwarder checks the
ticket and the attempt's fencing generation, reads the key the ticket names
from Secret Manager, and adds it on the way to `api.typesafe.ai`. Without the
forwarder the worker writes the key to `private/jev-typesafe.key` in the
pod, where the agent's own processes can read it. That file is what this
change removes for Engineering.

What each piece is, and what holds it ([docs/jev-data-flow.md](../jev-data-flow.md)
"Which key" has the whole flow):

| Piece | Where | Held by |
|---|---|---|
| The ticket key `swarm-typesafe-fwd-key` and its two readers | `terraform/infra/typesafe_fwd.tf` (`enable_typesafe_forwarder`) | `tests/terraform/typesafe_fwd.tftest.hcl` |
| The forwarder's read of `swarm-tenant-<t>-typesafe` versions | `terraform/bootstrap/typesafe_forwarder.tf` (`enable_typesafe_forwarder_read`, owner-applied) | `tests/terraform/typesafe_forwarder.tftest.hcl` |
| The manifest and its ConfigMap (`enabled`, `tenants`) | `kubernetes/swarm-system/swarm-typesafe-fwd.yaml`, rendered as `swarm_typesafe_fwd_manifest`, rolled by the release (`scripts/roll-typesafe-fwd.sh`) | `tests/unit/scripts/test_roll_typesafe_fwd.py` |
| The tenant side's one path to it | `kubernetes/network-policies/allow-typesafe-fwd.yaml`, applied to every tenant namespace by the release | `tests/unit/scripts/test_typesafe_fwd_netpol.py` |
| The label and the worker's switch | `scheduler.dispatch.typesafe_forwarder_on`: `swarm-typesafe: inject` on the pod, `SWARM_TYPESAFE_INJECT=on` on the worker | `tests/unit/control_plane/test_dispatch_typesafe_label.py` |
| Which tenants | `typesafe_forwarder` in `terraform/environments/<env>/<env>.tfvars`: the forwarder's ConfigMap and the scheduler's `TYPESAFE_FORWARDER_ENABLED`/`_TENANTS` read the same values | dev: `tests/unit/control_plane/test_typesafe_forwarder_dev_eng.py` |

The scheduler labels a pod only when **all** of these hold: the forwarder is
served and `enabled`; the pod's tenant is in `tenants` by exact name; it is a
GKE pod with the live browser, or a step whose signed workflow switch says
`jev_judgments: on` on a profile that carries the Jev client; and the
workspace's stored judgment backend is `typesafe`. Every other pod is left
exactly as it was. A Cloud Run Job is never labelled: it cannot reach a
cluster Service.

## Serve a workspace (dev serves `eng` from 2026-11-02)

The three terraform steps in `dev.tfvars` are done in dev: the key exists
with version 1 (2026-10-16), the bootstrap read is applied (2026-10-16),
`serve` is on, and `enabled = true` with `tenants = ["eng"]` (2026-11-02).
What is left is the owner's, in this order:

1. **Let the release apply it.** The change merges and the next release
   applies `terraform/infra` (the scheduler's two variables) and rolls
   `swarm-typesafe-fwd` with the new ConfigMap. It adds no IAM row: the
   forwarder's grants are unchanged since 2026-10-16. Confirm the forwarder
   read the new setting:

   ```bash
   CTX=gke_saga-agents-staging_us-central1_swarm-autopilot
   kubectl --context "$CTX" -n swarm-system get configmap swarm-typesafe-fwd-config -o jsonpath='{.data.config\.json}'
   kubectl --context "$CTX" -n swarm-system rollout status deployment/swarm-typesafe-fwd --timeout=300s
   ```

   The first prints `"enabled":true` and `"tenants":["eng"]`.

2. **Store the workspace's own TypeSafe key.** A ticket for a GROUP
   workspace's agent names only the workspace's key,
   `swarm-tenant-eng-typesafe`, never the submitter's personal one
   (`typesafe_ticket.TypesafeTicketService._key`, the owner's Q3 answer of
   2026-10-15). Before this change an eng agent could spend its submitter's
   key through `typesafe_user_key_readers`; with the forwarder it cannot.
   With no workspace key, swarm-api refuses the ticket, the worker serves no
   shim and the attempt has no Jev tools (it never falls back to a key file).
   An eng workspace admin stores it in the console: **Config › Secrets ›
   Keys for agents and MCP**, TypeSafe, "for this workspace". The console
   writes the status record swarm-api reads; `scripts/create-secrets.sh`
   does not, so do not use it for this key.

3. **Choose TypeSafe as eng's judgment backend.** The choice is signed by
   swarm-api (`settings_mac_tenants` names eng), so make it through the
   console's same page or the route, never in Firestore:

   ```bash
   scripts/api.sh PUT /workspaces/eng/judgment-backend '{"judgment_backend":"typesafe"}'
   scripts/api.sh GET /workspaces/eng/judgment-backend
   ```

   Until this is `typesafe`, eng's pods carry no label: the forwarder serves
   only workspaces that chose TypeSafe.

## See one Engineering Jev call go through the forwarder (the deployed probe)

Start one eng agent that calls Jev on TypeSafe: in the console, workspace
eng, profile `claude-code-browser` with **Browser tool: Jev**, and a goal
that opens any page. While it runs:

```bash
CTX=gke_saga-agents-staging_us-central1_swarm-autopilot
# 1. The pod carries the forwarder's label (and so its worker the switch).
kubectl --context "$CTX" -n swarm-tenant-eng get pods -l swarm-typesafe=inject
# 2. The worker took the shim, not a key file: `delivery: inject`, and the
#    shim's forwarded line. Use the pod name from step 1.
kubectl --context "$CTX" -n swarm-tenant-eng logs <pod> -c worker | grep -E 'jev_key_resolved|typesafe ticket minted|typesafe_shim_forwarded'
# 3. The forwarder's own line for that call: tenant eng, code forwarded.
kubectl --context "$CTX" -n swarm-system logs deployment/swarm-typesafe-fwd --since=30m \
  | grep '"event": "typesafe_forwarded"' | grep '"tenant_id": "eng"'
```

The probe passes when step 3 prints a line with `"code": "forwarded"`, the
agent's `task_id`, and `"secret_id": "swarm-tenant-eng-typesafe"`. The line carries ids,
sizes and a status, never the request, the answer or the key. A
`typesafe_refused` line instead says why in its `code` (`ticket_invalid`,
`stale_generation`, `tenant_off`, `disabled`, `key_unreadable`, ...). No line at
all with a labelled pod means the shim could not mint a ticket: the worker log
from step 2 says so, usually because step 2 or 3 above is not done.

## Roll back

Set `typesafe_forwarder.enabled = false` (or remove the tenant from
`tenants`) in the environment's tfvars and merge. The next release re-renders
both the scheduler's variables and the forwarder's ConfigMap from the same
values, so no pod is labelled for a forwarder that would refuse it; pods go
back to the key file. Nothing in IAM changes either way.
