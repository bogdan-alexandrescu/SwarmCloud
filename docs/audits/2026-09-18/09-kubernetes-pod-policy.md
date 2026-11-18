# task_e62e10d3ebeb4114a4ce  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit kubernetes/ - manifests, render.py, and policies/pod-security.yaml - for any way a rendered pod could violate the policy that is supposed to constrain it, or for a policy rule that does not actually constrain what its comment claims. Note worker-job-v2.yaml runs as root under runtimeClassName gvisor. At most 5.

## Report

I audited `kubernetes/policies/pod-security.yaml`, `render.py`, and the worker templates. Four real gaps, ordered by severity:

**1. `swarm-worker-pod-hardening` lets a container re-add capabilities after "dropping ALL"** (`policies/pod-security.yaml:320-326`)
The check is `capabilities.drop.exists(d, d == 'ALL')` — it never inspects `capabilities.add`. A container can set `drop: ["ALL"], add: ["SYS_ADMIN"]` and still pass. The policy's own comment claims this mirrors "exactly what Pod Security Admission's `restricted` level requires," but PSA `restricted` actually caps `add` to `NET_BIND_SERVICE` only — this CEL doesn't replicate that half of the rule, so it's weaker than what it claims to enforce.

**2. `runAsNonRoot` check is satisfiable by the pod-level field alone, even when a container overrides it to run as root** (`policies/pod-security.yaml:328-338`)
The expression is `(pod.securityContext.runAsNonRoot == true) || (all containers have runAsNonRoot == true)`. Because it's an OR, setting `spec.securityContext.runAsNonRoot: true` short-circuits the whole check — no container is ever examined. But Kubernetes lets a container's own `securityContext.runAsNonRoot`/`runAsUser` override the pod-level default for that container. So a pod with `spec.securityContext.runAsNonRoot: true` plus one container carrying `securityContext: {runAsNonRoot: false, runAsUser: 0}` passes admission while that container actually runs as root — the opposite of what the message ("runAsNonRoot must be true") claims to guarantee for every container.

**3. That same hardening policy would refuse the committed `worker-job-v2.yaml` (root + gvisor) pod** (`worker-templates/worker-job-v2.yaml:97-103,143-161` vs `policies/pod-security.yaml:328-338`)
v2's pod-level `securityContext` sets `runAsUser: 0`/`runAsGroup: 0`/`fsGroup: 0` but never sets `runAsNonRoot`, and neither the `install-credential` init container nor the `worker` container sets it either. `swarm-worker-pod-hardening`'s binding matches every namespace labeled `app.kubernetes.io/part-of: swarm` unconditionally — it doesn't relax for `baseline`-enforced namespaces. `render.py` (`render.py:282-299`) only checks that native PSA `restricted` would reject a gvisor job and requires `--pss-enforce baseline` to route around *that*; it says nothing about this second, custom `Deny` policy, which still runs regardless of the namespace's PSS level and denies `runAsNonRoot`. As committed, the v2 job as rendered cannot actually be admitted into any `part-of: swarm` namespace — the "defense moved to gVisor, not a regression" argument in the file's header never gets to run.

**4. `requests == limits` enforcement silently excludes ephemeral-storage** (`policies/pod-security.yaml:88-102` and `:208-218`)
Both the pod- and job-level CEL expressions compare only the `cpu` and `memory` keys of `resources.limits`/`resources.requests`; `ephemeral-storage` is never checked. The file's own header states invariant 7 unqualified ("`requests == limits`. A container that requests less than its limit is a container that gets OOM-killed..."), and `CONTRACT.md` states it the same way. A pod that sets `ephemeral-storage` requests below its limits — bursting disk instead of CPU/memory — passes this admission policy untouched, even though the templates themselves (correctly) always set the three resources equal.
