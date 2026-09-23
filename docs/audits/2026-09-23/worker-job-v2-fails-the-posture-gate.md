# The posture gate and worker-job-v2 disagree, and both are deliberate

Found by the first CI run this branch has ever had, 2026-09-23. It was failing
on the previous run too, so it is **pre-existing** and not a regression from
this branch's work.

## What CI says

`application.yml` → `kubernetes manifests` → `security posture assertions`:

```
##[error]missing runAsNonRoot
##[error]missing readOnlyRootFilesystem
checked 3 worker template(s)
```

## What is actually true

Of the three templates under `kubernetes/worker-templates/`:

| template | `runAsNonRoot: true` | `allowPrivilegeEscalation: false` | `readOnlyRootFilesystem: true` |
|---|---|---|---|
| `worker-job.yaml` | yes | yes | yes |
| `worker-job-browser.yaml` | yes | yes | yes |
| `worker-job-v2.yaml` | **no** | yes | **no** |

`worker-job-v2.yaml` is not missing these by oversight. It sets the opposite,
explicitly, and says why in the file:

```yaml
securityContext:
  # Root, and stated once here rather than implied by omission.
  runAsUser: 0
  runAsGroup: 0
  fsGroup: 0
```

```yaml
securityContext:
  # Root with a writable root filesystem -- the point of v2.
  runAsUser: 0
  readOnlyRootFilesystem: false
```

and it carries a compensating control the other two do not:

```yaml
# THE LOAD-BEARING LINE IN THIS FILE. Without it, every securityContext
# below is a plain privilege grant on a shared node.
runtimeClassName: gvisor
```

So v2 is a **gVisor-sandboxed root worker**, on purpose. The gate asserts a
blanket non-root, read-only posture on *every* worker template. The two cannot
both be satisfied, and neither side is a mistake in isolation.

## The decision this needs

This is a security-posture decision and belongs to the owner. Two coherent
answers:

**A. v2's design stands, and the gate learns the class.** The gate stops
asserting one posture for every template and instead asserts the posture that
matches the isolation each one declares:

- a template WITHOUT `runtimeClassName: gvisor` must be non-root and
  read-only, exactly as today;
- a template WITH it must declare `runtimeClassName: gvisor`, drop `ALL`
  capabilities, set `allowPrivilegeEscalation: false`, and state `runAsUser`
  explicitly rather than by omission.

This keeps a real gate on both classes. It must not collapse into "skip the
checks if gvisor is present", which would make the word `gvisor` a way to opt
out of the gate entirely.

**B. v2 becomes non-root and read-only.** The gate is right as written and the
template changes. This is the stricter answer and costs whatever v2 needed root
and a writable root filesystem for — that reason is not recorded in the file
beyond "the point of v2", so it must be established before choosing this.

## Ownership

`kubernetes/` is **Track C**. `.github/` is **Track D**. Whichever answer is
chosen, the patch belongs to a different track from the one that found it, so
this is filed rather than applied.

## The second finding, which is the more important one

**`make lint` never ran this assertion.** It lives only in
`.github/workflows/application.yml`; `scripts/lib/validate-manifests.sh`
contains neither `runAsNonRoot` nor `readOnlyRootFilesystem`. That is why a
locally-green tree produced a red CI job, and it is the same shape as every
other finding on this branch: a check that exists in one place and is believed
to run everywhere.

Whatever is decided above, the assertion should live in
`scripts/lib/validate-manifests.sh` and be *called* by the workflow, so that
`make lint` and CI cannot drift apart again. The repository already states this
rule for scripts in `CLAUDE.md`: "every rule that got restated in a second
place here has since drifted."
