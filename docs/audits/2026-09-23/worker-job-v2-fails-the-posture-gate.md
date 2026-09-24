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

## Resolved, 2026-09-23

**A was chosen.** The gate now holds each template to the posture that matches
the isolation it declares, in `scripts/lib/validate-manifests.sh`:

- no `runtimeClassName: gvisor` → `runAsNonRoot: true` and
  `readOnlyRootFilesystem: true`, exactly as before;
- with it → `runAsUser` must be stated explicitly, because an implicit uid on
  a deliberately-root pod is indistinguishable from an oversight and that is
  the thing review has to be able to see;
- **both classes** → `allowPrivilegeEscalation: false`, `drop: ["ALL"]`, and
  `restartPolicy: Never` where a restartPolicy is set at all. The last two of
  those are new: the old gate asserted neither for any template.

It is not an opt-out. A gvisor template is checked against a different and
equally specific list, and one that names gvisor while granting capabilities or
leaving its uid implicit fails.

**Mutation-proven, not asserted.** Each branch was broken in turn and the gate
went red each time:

| mutation | result |
|---|---|
| remove `runAsNonRoot: true` from `worker-job.yaml` | RED — missing runAsNonRoot (no gvisor sandbox) |
| remove `runAsUser: 0` from `worker-job-v2.yaml` | RED — gvisor template must state runAsUser explicitly |
| remove `drop: ["ALL"]` from `worker-job-v2.yaml` | RED — does not drop ALL capabilities |

A first attempt at this table produced three false greens, because the mutation
removed only the *first* occurrence of a string that appears two or three times
per file. That is worth recording twice over: it nearly shipped a gate believed
to be proven, and it exposes the residual limitation below.

### Known residual: the check is whole-file, not per-container

Both the old gate and this one `grep` the file rather than parsing it, so a
field set on **one** container satisfies the check for **all** of them. Today
the templates happen to be internally consistent — `worker-job-v2.yaml` sets
`allowPrivilegeEscalation: false` and `drop: ["ALL"]` on both its init and main
containers — so nothing is currently hidden by this. It would not stay true by
itself. Parsing the rendered YAML and asserting per container is the stronger
form; `validate-manifests.sh` already requires `python3` and the job already
installs PyYAML, so the cost is small and it is not done here only because it
is a separate piece of work from the class distinction.

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
