# Browser-agent start-up: where the 89 seconds go, and what would take them back

A **proposal**, written for the owner's decision of 2026-10-13 ~04:30 PDT,
"Investigate, then propose". No application, Terraform, image or workflow
change came with it. Every number below is labelled **measured** (the
command and the time are given), **read** (from the repository at `33ccbcb`), or
**estimate** (the arithmetic is shown). Where something could not be read, it
says so and names who can read it.

## 1. The start that was measured

`task_c6bf0368442642f784e3`, profile `claude-code-browser`, GKE Autopilot
cluster `swarm-autopilot`, namespace `swarm-tenant-eng`, read 2026-10-13
11:20 UTC from its `startup_progress` events (the pod's steps come from
`apps/reconciler/reconciler/startsteps.py::pod_start_steps`, the browser's
from the worker):

| step | at (UTC) | took | what the time is |
|---|---|---|---|
| `dispatched` | 11:20:31.8 | | the scheduler created the Job |
| `pod_scheduled` | 11:21:10 | **38 s** | the pod was bound to a node. On Autopilot this is mostly the wait for a new node, because a pod is only bound to a node once that node exists |
| `image_pulling` | 11:21:10 | 0 s | the sandbox was up |
| `image_pulled` | 11:21:52 | **42 s** | the first container started. On a live-browser pod that is the `display` sidecar, which runs the same image (`apps/scheduler/scheduler/dispatch.py::with_live_display`), so this is the image pull plus one container create |
| `browser_launching` | 11:21:53.7 | 1.7 s | |
| `browser_ready` | 11:21:56 | 2.3 s | Xvfb and headed Chromium up |
| `starting` / `running` | 11:21:59.7 | 3.7 s | worker container started, fencing and restore done |
| `first_frame` | 11:22:00 | 0.3 s | |
| **total** | | **~88 s** | |

Two earlier agents went from `dispatched` to `starting` in 79 s (the first one)
and 26 s (a resume). **Inference, not measured:** the resume most likely
landed on a node that was still up from the earlier attempt and already had the
image. That fits the 2026-10-07 reading in
[worker-images.md](worker-images.md#where-dispatched---starting-goes-measured-2026-10-07)
that `browser` on GKE reaches STARTING at p50 15 s but p90 83 s. A warm start
is already fast. **The cold start is the problem, and it is two waits:** a new
node (38 s) and a full image pull (42 s). Everything after the image arrives
takes 8 s, and none of the options below is worth building to shorten that 8 s.

## 2. The image

### 2.1 What builds it

`images/agent-runtime-browser/Dockerfile`, built by
`images/agent-runtime-browser/cloudbuild.yaml` `FROM agent-runtime-base-nocodex`
(passed by digest). That base is `images/agent-runtime-base/Dockerfile` built
with `INSTALL_CODEX=0` (BROWSER-NO-CODEX, §2.3); until 2026-10-13 it was
`agent-runtime-base` itself. The base Dockerfile is:
`python:3.11-slim-bookworm`, Node 24, the worker venv, the agent toolbox and
the two agent CLIs. The browser image then adds, in **one** `RUN`:
`pip install playwright==1.63.0` and `playwright install --with-deps chromium`.
Then come Xvfb, the `swarm-display` user, the Chromium policy,
`swarm-handoff`, `WORKDIR /workspace` and two build-time self-checks.

### 2.2 What it weighs: measured from the registry

Measured 2026-10-13 ~11:36 UTC by reading the manifest and image config of
`agent-runtime-browser:33ccbcb2a9cf` (main's head) from Artifact Registry
(`GET /v2/.../manifests/<tag>`, the same Docker Registry v2 reads that
`scripts/image-sizes.sh` makes). Note that this tag is main's head and **not
necessarily the digest deployed** on 2026-10-13. `scripts/image-sizes.sh
--manifest` on the release record gives the deployed one.

**33 layers, 1,253.3 MB compressed.** The base underneath is 25 layers and
780.6 MB. The browser's own layers add 472.6 MB, and almost all of that is one
layer:

| layer(s) | compressed MB | what (from the image config's history) |
|---|---|---|
| base 0-3 | 47.7 | Debian bookworm + Python 3.11 (`python:3.11-slim-bookworm`) |
| base 4 | 127.8 | the runtime's apt set (`build-essential`, git, ripgrep, ...) |
| base 5-6 | 49.1 | Node 24 + npm |
| base 9-12 | 57.1 | uv, uvx, the worker venv |
| base 13-17 | 256.9 | the agent toolbox: gcloud + plugin + kubectl (76.6), gh + docker CLI (36.2), terraform (36.0), shellcheck + trivy (55.7), checkov (52.4) |
| base 18 | **240.2** | `npm install -g` of Claude Code **and codex** |
| base, rest | ~1.8 | users, self-checks, `WORKDIR` |
| **browser 25** | **472.6** | playwright wheel + Chromium + headless shell + Chromium's apt deps, one layer |
| browser 26-32 | ~0 | xvfb, user, policy, handoff, `WORKDIR`, self-checks |

### 2.3 What dominates layer 25: measured from the exact pinned artefacts

Each item below was downloaded as the Dockerfile downloads it, unpacked, and
then re-tarred and `gzip -6`'d, which is how a layer is compressed. This is the
same method [worker-images.md](worker-images.md#why-the-repository-index-has-its-own-image-625)
used for the toolbox. Measured 2026-10-13 ~11:30 UTC:

| item | download | unpacked | as a layer (gzip -6) |
|---|---|---|---|
| Chromium (Chrome for Testing 153.0.8010.12, `chrome-linux64.zip`, Playwright revision 1243) | 195.8 MB | 393 MB | **180.7 MB** |
| **chromium-headless-shell**, same build (`chrome-headless-shell-linux64.zip`) | 119.8 MB | 261 MB | **108.5 MB** |
| playwright 1.63.0 wheel (manylinux1 x86_64) | 48.2 MB | 137 MB | 48.2 MB |
| … of which its **bundled Node.js** driver binary (`playwright/driver/node`) | | 121 MB | **44.6 MB** |
| `--with-deps` apt closure, estimated from Debian bookworm's `Packages` index | ~98 MB of .debs | ~313 MB | ~98 MB |

Those add up to about 436 MB against the measured 472.6 MB layer. The
remainder is the closure estimate's error and the Python files the self-check
compiles.

**So yes: Playwright's browsers are installed twice, and Node.js is in the
image twice.**

* **Two Chromiums.** Since Playwright 1.49, `playwright install chromium`
  installs both the full Chromium and `chromium-headless-shell` unless it is
  given `--no-shell`. The **headed** one is the one the live display launches
  (`apps/agent-worker/agent_worker/livegate/__main__.py::find_chromium`, which
  globs `chromium-*/chrome-linux*/chrome`). The **shell** is what
  `headless=True` launches by default: the `browser` runner
  (`apps/agent-worker/agent_worker/runners/browser.py::_launch`) and any
  headless Chromium a claude-code agent starts itself
  (`apps/agent-worker/agent_worker/runners/claude_code.py::BROWSER_NOTE`
  tells it to launch with `headless=True`). Playwright can run the full
  Chromium headless with `channel="chromium"` (Chromium's "new headless"
  mode), and then the shell is not needed.
* **Two Node.js runtimes.** The base copies Node 24 into `/usr/local/bin/node`
  (45.9 MB layer). The playwright wheel then brings its own 121 MB `node` for
  its driver. playwright-python reads `PLAYWRIGHT_NODEJS_PATH` to run the
  driver on a node it is given instead.
* **xvfb is installed twice in the Dockerfile.** Playwright's `--with-deps`
  for debian12 already includes `xvfb` and the fonts. That is why layer 26,
  the Dockerfile's own `apt-get install xvfb`, is 0.0 MB. It is harmless, and
  it is also the explicit line that keeps Xvfb if the deps list ever changes,
  so this proposal keeps it.
* **Most of the apt closure comes from Xvfb, not Chromium.** Xvfb pulls in
  Mesa (`libgl1-mesa-dri` 24.7 MB unpacked, `libllvm15` 111.9 MB, `libz3-4`
  22.2 MB). Chromium renders with its own SwiftShader and does not need any of
  it. The display sidecar needs Xvfb, though, and removing Mesa under it with
  `dpkg --force-depends` is a fragile trade that this proposal does **not**
  recommend.
* **The fonts are 26.7 MB of .debs** (61.5 MB unpacked). Most of that is CJK
  and emoji (`fonts-wqy-zenhei`, `fonts-ipafont-gothic`, `fonts-unifont`,
  `fonts-noto-color-emoji`). Removing them makes every Chinese, Japanese or
  emoji page render as boxes in the screenshots a person watches. They stay.
* **codex is in the base and so in this image: 129.7 MB** (measured: the
  `@openai/codex@0.154.0-linux-x64` tarball npm installs). The `codex` profile
  is disabled (`available=False`), and `claude-code-browser` never runs it.
  Claude Code's own platform package is 108.5 MB, and the two together make up
  base layer 18's 240.2 MB.

#### After BROWSER-ENFORCE (2026-10-13): what the slimming removed, and what it did not

Decision 4 of §9 asked for three removals. Two are in
`images/agent-runtime-browser/Dockerfile`; the third needs a choice the
Dockerfile cannot make. **The sizes below are ESTIMATES**, computed from the
per-artefact measurements in the table above and one new measurement; the
image was not built in the lane that made the change (builds run in CI), so
the registry has not been read for it. `scripts/image-sizes.sh --manifest` on
the first release after it gives the measured figure, and this paragraph
should then be replaced by it.

| change | as a layer (gzip -6) | how |
|---|---|---|
| no **chromium-headless-shell** | **−108.5 MB** | `playwright install --with-deps --no-shell chromium`; the `browser` runner and the self-test launch the full build headless with `channel="chromium"` |
| no **second Node** | **−44.6 MB** | the wheel's `driver/node` is deleted in the same `RUN` and replaced by a link to the base's `/usr/local/bin/node` (a link, not `PLAYWRIGHT_NODEJS_PATH`: the runner's environment is built by exact name) |
| **+ Playwright MCP** `@playwright/mcp@0.0.83` | **+4.1 MB** | measured 2026-10-13: `npm install` of the pinned version into an empty prefix, 19 MB unpacked, 4,126,409 bytes as `tar \| gzip -6` |
| **codex** | 0 in BROWSER-ENFORCE (129.7 MB remained) | **not removed there**: it is in base layer 18, and a derived image cannot drop an inherited layer -- deleting it in the browser Dockerfile only adds a whiteout. Put to the owner with the BROWSER-ENFORCE PR (1248); removed by BROWSER-NO-CODEX, below |

Net, estimated: layer 25 goes from 472.6 MB to about 319.5 MB, and the image
from 1,253.3 MB to about **1,104 MB, −149 MB**, against the ~283 MB that §9
named. The other 130 MB is codex, below.

#### After BROWSER-NO-CODEX (owner decision 2026-10-13 ~07:15 PDT): the browser's base has no codex

The owner's answer to the codex question was **a codex-less base for the
browser**. `images/agent-runtime-base/Dockerfile` takes `ARG INSTALL_CODEX=1`,
and its one `npm install -g` `RUN` installs `@openai/codex` only when it is 1.
Left out in that `RUN`, not deleted in a later one: a later one would only
white it out, and a check-only `RUN` can write a duplicate layer (§2.4,
PR 1239). With the default the command is the one it always was, so
`agent-runtime-base`, and every profile but the browser's, keeps codex.

`scripts/build-images.sh` builds the same Dockerfile a second time with
`--build-arg INSTALL_CODEX=0` as **its own image, `agent-runtime-base-nocodex`**
(same tag as every other image of the build), and
`images/agent-runtime-browser/cloudbuild.yaml` resolves that one to a digest
and builds the browser `FROM` it; `build_after()` holds the browser back until
it is pushed. A pull request's `--build-only` chain builds the same pair, the
codex-less base and then the browser on it; `agent-runtime-indexer`'s chain
builds the default base. A separate image name rather than a second tag
(`agent-runtime-base:<sha>-nocodex`) because everything after the build is keyed
by image name: the build record, `push-images.sh`'s promotion and scan,
`scripts/image-sizes.sh --duplicate-layers` (the release gate) and `--manifest`
(an `agent-runtime-*` image, so its size is recorded), and terraform's
`image_refs`, whose keys must be the image's own name. So the new base is
pinned, scanned, promoted and gated like every other image without a special
case. The browser image's self-test (`selftest.py`, `no_codex()`) fails the
build if `codex` is on its `PATH` or its package is installed, and if any
other tool of the base is missing.

| image | before | after, **ESTIMATED** |
|---|---|---|
| base layer 18 (`npm install -g`) in the browser's base | 240.2 MB | **~110.5 MB** (Claude Code's 108.5 MB platform package and its wrapper) |
| `agent-runtime-browser` | ~1,104 MB (after BROWSER-ENFORCE, estimated) | **~974 MB, −129.7 MB** |
| `agent-runtime-base` (claude-code, codex, indexer) | 780.6 MB | unchanged |

**ESTIMATED, not measured**: −129.7 MB is the measured size of the
`@openai/codex@0.154.0-linux-x64` tarball npm installs (above), subtracted
from the BROWSER-ENFORCE estimate. No image was built in the lane (builds run
in CI). `scripts/image-sizes.sh --manifest` on the first release that
promotes `agent-runtime-base-nocodex` measures both images; replace these
rows with its figures. The toolbox stays (§9, decision 5),
and so does the Chromium the gate runs (`chromium-*/chrome-linux*/chrome`,
`livegate/__main__.py::find_chromium`).

### 2.4 The finding that matters most: this image cannot be streamed

GKE's documentation says Autopilot clusters "automatically use Image streaming
to pull eligible images", and that "container images with duplicate layers are
not supported"
([GKE: Image streaming](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/image-streaming),
read 2026-10-13).

**The browser image has a duplicate layer.** Layers 20 and 30 are both
`sha256:4f4fb700ef54…`, the canonical empty gzip layer. The image config's
history shows where they come from:

* layer 20 is the base's `WORKDIR /workspace`
  (`images/agent-runtime-base/Dockerfile`);
* layer 30 is the browser Dockerfile's own `WORKDIR /workspace`. It repeats a
  directive the base already set, and it produces the same empty diff.

The base image alone has **no** duplicate (its 25 layer digests are all
distinct). That fits what is already known: `claude-code`, which runs the
base, starts on the same cluster in about 23 s
([worker-images.md](worker-images.md#where-dispatched---starting-goes-measured-2026-10-07)),
while the browser pod pays a full 42 s pull.

**Measured as a control:** `agent-runtime-indexer:33ccbcb2a9cf` has the same
defect, plus one more (38 layers, 906.9 MB, `4f4fb700ef54…` ×2 and
`8af345ba3690…` ×2; `images/agent-runtime-indexer/Dockerfile` also re-declares
`WORKDIR /workspace`). That image is outside this lane, so it is reported in
the PR as a finding, not fixed.

**Not proven, and how to prove it.** Two things could not be read from this
lane:

1. **Whether the pull really fell back to a normal pull.** The proof is the
   pod's `ImageStreaming` event (GKE's documented signal; older versions used
   `BackedByGCFS`). The reconciler cannot read events: its `swarm-reaper`
   Role grants `pods` and `jobs` only
   (`apps/reconciler/reconciler/startsteps.py` module docstring). The operator
   can read it with
   `kubectl get events -n swarm-tenant-<t> --field-selector involvedObject.name=<pod>`
   on `swarm-autopilot`, through `kubectl_bin` and **never** on
   `agents-staging`.
2. **Whether the Container File System API (`containerfilesystem.googleapis.com`)
   is enabled** in `saga-agents-staging`. Image streaming needs it. It is
   **not** in the list of services Terraform enables
   (`terraform/infra/main.tf`, the `services` list). The SwarmCloud worker
   account got `AUTH_PERMISSION_DENIED` on `gcloud services list` (2026-10-13
   ~11:33 UTC), so it was not read. `claude-code`'s 23 s start suggests the API
   is on, but that is an inference. The other requirements are met:
   * the images live in a STANDARD Artifact Registry repository in
     `us-central1`, the cluster's region (`terraform/modules/artifact_registry/main.tf`);
   * no CMEK is set on it (`module "artifact_registry"` in
     `terraform/infra/main.tf` passes no `kms_key_name`);
   * the manifest is Docker v2 schema 2, not schema 1;
   * the private nodes' subnet has Private Google Access
     (`terraform/modules/network/main.tf`, `private_ip_google_access = true`).

## 3. GKE image streaming (question 2)

* **Does the cluster have it?** Autopilot turns it on for every eligible
  image; there is no cluster flag to set, and `terraform/modules/gke_autopilot/main.tf`
  rightly sets none. The cluster's API prerequisite is §2.4 point 2.
* **Does it apply to this image?** **No, today.** The duplicate empty layer
  makes it ineligible (§2.4). Removing the browser Dockerfile's redundant
  `WORKDIR /workspace` makes it eligible. The base already sets that working
  directory, so dropping the line changes no behaviour.
* **What would it save?** **Estimate: 25-38 s of the 42 s.** GKE's own
  example pulls an image in 1.5 s with streaming against 23.9 s without. The
  pull step becomes a metadata fetch. The bytes Chromium, Node and Python
  actually read are then fetched lazily, so some seconds move into
  `browser_launching -> browser_ready`. The headed Chromium binary alone is
  most of a 393 MB tree, and it is read on first launch. So the estimate is the
  42 s pull falling to 2-6 s, plus 0-10 s more spent at the first launch.
  Two caveats:
  * GKE warns that "you might not notice the benefits of Image streaming
    during the first pull of an eligible image". The first cold start after
    each release may still pay a large part of the 42 s.
  * Streaming does nothing for the 38 s node wait.

## 4. The 38 s node wait (question 3)

### 4.1 What is happening

The browser pod was one pod of 8 vCPU / 16 GiB when this was measured, with
requests == limits on both containers (invariant 7;
`apps/scheduler/scheduler/dispatch.py::LIVE_DISPLAY_SHARE` split it 5/10 to the
display and 3/6 to the worker). Since contract request 79 (§9, decision 6) it
is 4 vCPU / 8 GiB, split 3/5 and 1/3. It sets no compute class,
no node selector and no priority (`apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`),
so it lands on Autopilot's default class. When no node has 8 vCPU free,
Autopilot creates one. Google quotes 80-120 s for a new node in general; 38 s
is already the fast end of that. **Read, as of DEPLOY_STATE's reading:** the
control plane is on `1.36.4-gke.1082000` and the cluster was created
2026-09-17. Autopilot clusters on 1.32+ get the container-optimized compute
platform, which keeps pre-provisioned capacity for pods up to 28 vCPU.
**Inference:** that platform is probably why the wait is 38 s rather than 80+.
So there is no cheaper compute-class switch that would make it faster.

### 4.2 Option: warm placeholder pods (a "balloon")

This is GKE's documented pattern
([Provision extra compute capacity](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/capacity-provisioning)):

* a Deployment of placeholder pods in a PriorityClass of value `-10` with
  `preemptionPolicy: Never` and `terminationGracePeriodSeconds: 0`;
* each pod requests what one browser pod requests (8 vCPU / 16 GiB when this
  was written, 4 vCPU / 8 GiB since contract request 79, and requests ==
  limits here too);
* a browser pod at the default priority 0 preempts a placeholder and takes
  its node in seconds;
* the evicted placeholder goes Pending, and Autopilot provisions a new node
  behind it, which re-arms the slot.

**Give the placeholder the browser image** (by digest, command `sleep`, in
`swarm-system`). Its node then also has the image already pulled, so a warm
slot saves the pull as well as the node wait.

**It breaks invariant 1 as written, and that should be said plainly.**
Invariant 1 says "Only LEASED/DISPATCHED/STARTING/RUNNING create
infrastructure demand." A placeholder is infrastructure demand that no agent
holds. It does not break the second sentence: it is not a pending pod used as
a backlog, because no agent ever waits in it. It does not touch admission
either. Capacity is still reserved all-or-nothing in Firestore (invariant 2)
and counted from LEASED (invariant 3), because the placeholder is in no pool.

The repository has a precedent, but not a licence. `scripts/warm-jobs.sh`
already runs one no-op execution per Cloud Run Job after each release (owner
decision (2) of 2026-10-07), and that is short, release-bound, agent-less
demand. A placeholder held for hours is a different size of thing. It needs a
**contract amendment request** (`docs/contract-change-requests.md`), written
and accepted before any of it is built.

Further constraints:

* **Stealing.** Any priority-0 pod can preempt the placeholder, including a
  4-vCPU `claude-code` pod, which then wastes the slot. Fencing the slot for
  browser pods takes Autopilot workload separation: a node selector and
  toleration on a custom key on both the placeholder and the browser pod.
  That changes `apps/scheduler/scheduler/dispatch.py` (Track A) and the
  rendered templates that `tests/unit/worker/test_kubernetes_manifests.py`
  holds equal to it.
* **One slot warms one start.** A second browser start inside the 38 s re-arm
  window starts cold.
* **The digest moves on every release,** so the placeholder must be
  re-rendered with the new `WORKER_IMAGE_REFS` digest, or it pre-pulls the
  last release's image.

**Cost at list price.** Autopilot general-purpose in `us-central1`:
$0.0445 per vCPU-hour and $0.0049225 per GiB-hour. Ephemeral storage is
$0.0000548 per GiB-hour, which is negligible. The vCPU and memory rates were
confirmed on 2026-10-13 only through secondary sources that quote Google's
list. Google's own pricing page did not render for this lane, and the
ephemeral rate is not re-verified. Confirm all three on the
[GKE pricing page](https://cloud.google.com/kubernetes-engine/pricing) before
deciding. Autopilot bills a running placeholder's requests like any pod's.

```
one slot, per hour  = 8 vCPU x $0.0445 + 16 GiB x $0.0049225 + 8 GiB x $0.0000548
                    = $0.3560 + $0.0788 + $0.0004            = $0.4352 / h
24 x 7              = $0.4352 x 24                           = $10.44 / day  (~$313 / 30 days)
weekdays 07-19 PT   = $0.4352 x 12 h x 5 d = $26.11 / week   = $3.73 / day averaged (~$112 / 30 days)
two slots           = double either figure
```

### 4.3 Option: a smaller resource class

**Done: contract request 79 (§9, decision 6) made `browser` 4 vCPU / 8 GiB
and 1 unit.** What follows is the reasoning as written before the measurement.

`browser` is 8 vCPU / 16 GiB. The 16 GiB partly pays for the 2 GiB
`/dev/shm` tmpfs (`images/agent-runtime-browser/Dockerfile` header), and the
5/3 split is "the design's proposed starting split, NOT A MEASUREMENT"
(`apps/scheduler/scheduler/dispatch.py::LIVE_DISPLAY_SHARE`). A 4 vCPU / 8 GiB
pod is more likely to fit beside existing pods than to need a node of its own.
That would save **0-38 s, and nobody can say which** without the distribution
of `pod_scheduled - dispatched` by class. It would halve the browser's own
bill:

```
browser 8/16 : $0.4352 / h of browser run time
4 / 8        : 4 x $0.0445 + 8 x $0.0049225 + 8 x $0.0000548 = $0.1780 + $0.0394 + $0.0004 = $0.2178 / h
```

So this option saves money ($/day depends on browser-hours run, and is
negative cost, not added cost) rather than costing any. The resource class
lives in the frozen catalogue (`apps/common/swarm_common/profiles.py`,
`ResourceClass("browser", cpu=8, memory_gib=16, ...)`), so changing it is a
**contract change request**. The risk is a Chromium, Xvfb and encoder sidecar
starved at about 2.5 vCPU, so it has to be measured before it is proposed.

### 4.4 Compute classes

* **Performance** (node-based billing, a node per pod) and **Accelerator**
  make starts slower or dearer, not faster.
* **Scale-Out** and **Balanced** change the CPU platform, not how fast a node
  arrives, and Balanced costs more per vCPU. Their rates were not verified for
  this document because neither is proposed.

The container-optimized default the cluster probably already runs (§4.1) is
the fast option, so this proposal recommends no compute-class change.

## 5. Pre-pulled images and secondary boot disks (question 4)

* **Secondary boot disks.** Autopilot supports them on COS from
  `1.30.1-gke.1329000`
  ([GKE: secondary boot disks](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/data-container-image-preloading)):
  * a `GCPResourceAllowlist` for the disk image's project;
  * a pod `nodeSelector`
    `cloud.google.com.node-restriction.kubernetes.io/gke-secondary-boot-disk-<DISK_IMAGE>: CONTAINER_IMAGE_CACHE.<PROJECT>`;
  * **image streaming as a prerequisite.**

  It helps only nodes created after the disk image exists. The disk image must
  be rebuilt for every new browser digest, which means a Cloud Build step in
  the release and a new disk image per release. On top of streaming it saves
  **an estimated 0-5 s**: what is left is the lazy reads at Chromium's first
  launch. Its disk cost is small. At pd-balanced's list $0.10 per GiB-month
  (not re-verified 2026-10-13), a 10 GiB disk is about $0.0033 per node-day.
  It is a lot of release machinery for little gain once streaming works, so it
  is **not recommended now**. Reconsider it if streaming's first pull after a
  release proves to be the remaining cost.
* **A pre-pulled image on a kept-warm node.** That is §4.2's placeholder
  running the browser image. On Autopilot a node cannot be "kept" any other
  way: there are no node pools to size.
* **A pre-pull DaemonSet** pulls onto every node, including the new node
  created for the browser pod, at the same moment that pod pulls. In the cold
  case it saves **~0 s** and bills a small pod on every node. **Rejected.**

## 6. Ranked options (question 5)

"Cold" means the measured 88 s start: a new node, nothing cached. Slimming is
priced at the measured cold pull rate, 1,253 MB in 42 s ≈ 30 MB/s. With
streaming on, slimming saves close to nothing, because only the bytes read
are fetched.

| # | option | seconds saved (cold start) | $/day | effort | invariant impact | risk |
|---|---|---|---|---|---|---|
| 1 | **Make the browser image streamable**: drop the redundant `WORKDIR /workspace` (§2.4); add a release check that refuses a runner image with a repeated layer digest; operator confirms the API and the `ImageStreaming` event | **25-38** of the 42 s pull (§3) | 0 | S: one Dockerfile line, one check, one test | none | low; some seconds move into the first Chromium launch; the first start after a release may pull fully |
| 2 | **Warm placeholder slot** with the browser image, browser-only via workload separation, weekday hours | **~38 + whatever pull is left** (38-80) for the first start per slot; 0 for the second start inside a re-arm | 3.73 (weekdays 07-19 PT) / 10.44 (24×7) per slot | M: PriorityClass + Deployment in `kubernetes/swarm-system/`, render by digest, dispatch toleration/selector, a schedule to scale it | **breaks invariant 1 as written**: needs an accepted contract amendment | medium: slot stolen without separation; a stale digest pre-pulls the wrong image; a forgotten scale-down bills 24×7 |
| 3 | **Drop the headless shell**: `install --no-shell`; launch headless with `channel="chromium"` in `runners/browser.py::_launch`, `BROWSER_NOTE` and the Dockerfile self-check | 3.6 cold (108.5 MB); ~0 streamed | 0 | S-M | none | low-medium: new-headless and shell screenshots can differ slightly |
| 4 | **Drop Playwright's bundled Node**: set `PLAYWRIGHT_NODEJS_PATH=/usr/local/bin/node` and delete `driver/node` in the same `RUN` | 1.5 cold (44.6 MB); ~0 streamed | 0 | S | none | low-medium: the driver runs on Node 24 rather than the Node Playwright tested with |
| 5 | **Leave codex out of the browser image** (base `INSTALL_AGENT_CLIS` split, or a base without codex) | 4.3 cold (~130 MB); ~0 streamed | 0 | S-M (base Dockerfile + build args) | none | low: codex is disabled (`available=False`) |
| 6 | **Browser image without the agent toolbox** (a slimmer base) | 8.5 cold (256.9 MB); ~0 streamed | 0 | M | none | changes what a `claude-code-browser` agent can do (no gcloud, terraform, checkov, trivy): an owner choice |
| 7 | **Smaller `browser` class** (4/8) | 0-38, unknown until measured (§4.3) | saves ~$0.22 per browser-hour | M + contract request | none (requests == limits kept) | medium: Chromium + encoder starved; re-split `LIVE_DISPLAY_SHARE` |
| 8 | Secondary boot disk preloading | 0-5 on top of #1 | ~0.003 per node-day | L: disk image per release, allowlist, node selector | none | medium: release machinery |
| 9 | Compute-class change | ~0 | ≥ 0 | S | none | — |
| 10 | Pre-pull DaemonSet | ~0 cold | small × nodes | S | none (but agent-less pods) | — |

Options 3-6 together take about 540 MB (43 %) off the image: about 18 s on a
cold non-streamed pull, and close to nothing once #1 works. They are still
worth doing for the first pull after each release, which streaming may not
cover, and for node disk. They come after #1, not instead of it.

## 7. Recommendation, and the lanes that would build it (question 6)

**Do #1 now.** It is the only option with a large measured cause behind it,
it costs nothing, and it touches no invariant. **Then measure** before
deciding #2 and #7. **Do #3-#5 as one image lane** once #1's effect is known.

* **Lane STREAM** (Track B images + Track D scripts; no contract change).
  * Remove `WORKDIR /workspace` from `images/agent-runtime-browser/Dockerfile`.
    With the owner's go-ahead, do the same in
    `images/agent-runtime-indexer/Dockerfile` and find its second duplicate
    (`8af345ba3690…`).
  * Add a duplicate-layer check to `scripts/image-sizes.sh --manifest`, which
    already reads every promoted runner manifest on release, and make it fail
    the promote on a repeated digest.
  * Tests: a unit test under `tests/unit/scripts/` that feeds the check a
    manifest fixture with a repeated digest and one without (red first: push
    the fixture test alone); and a unit test that refuses a derived runner
    Dockerfile re-declaring its base's `WORKDIR`.
  * Operator, after the first release: read `containerfilesystem.googleapis.com`
    enablement and one cold browser pod's `ImageStreaming` event, and compare
    `image_pulling -> image_pulled` before and after. **If the API is off,**
    enabling it is a project-wide change in a **shared** project. If the other
    team's `agents-staging` is an Autopilot cluster, their pulls would start
    streaming too. That is the owner's call (question 2 below), not this
    lane's.
* **Lane MEASURE** (Track D, read-only, no code). This lane fills the "unknown"
  cells of the table above.
  * `scripts/bench-coldstart.sh --profile claude-code-browser` over ten or more
    starts, split by cold and warm (`pod_scheduled - dispatched` over 10 s
    means cold), before and after STREAM.
  * The browser pod's real CPU and memory use from GKE metrics, to size
    option #7.
* **Lane SLIM** (Track B images + Track B worker).
  * `--no-shell` and `channel="chromium"` in
    `apps/agent-worker/agent_worker/runners/browser.py::_launch`, in
    `claude_code.py::BROWSER_NOTE` and in the Dockerfile's self-check.
  * `PLAYWRIGHT_NODEJS_PATH` with the bundled node deleted, and codex left out
    of the browser image.
  * Tests: the existing `tests/unit/control_plane/test_claude_code_browser_profile.py`
    and the worker's browser-runner tests, extended to pin the launch channel.
    The Dockerfile's own build-time self-check proves the headless launch in
    CI's image build.
* **Lane WARM** (only if the owner accepts the amendment).
  * A contract change request amending invariant 1 for a bounded number of
    agent-less warm slots.
  * A PriorityClass and placeholder Deployment template in
    `kubernetes/swarm-system/`, rendered with the browser digest by
    `kubernetes/render.py`.
  * Workload separation added to the browser pod in
    `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`.
  * Scale-to-zero outside the hours the owner picks.
  * Tests: `tests/unit/worker/test_kubernetes_manifests.py` parity for the
    selector and toleration; a render test that the placeholder's requests
    equal the `browser` class and its limits; and a test that it never lands
    in a tenant namespace.

## 8. Owner questions (question 7)

The same questions are filed as `questions.json` with this PR's artifacts.

1. **Build lane STREAM (drop the duplicate layer and add the release
   check)?** Recommended: yes. Free, no invariant touched, and it targets the
   larger of the two measured waits.
2. **If `containerfilesystem.googleapis.com` turns out to be off, may it be
   enabled in the shared project?** (a) yes, after confirming whether
   `agents-staging` is Autopilot; (b) yes, without checking; (c) no, and live
   with the 42 s. Recommended: (a).
3. **Warm slots: amend invariant 1?** (a) no warm slots; (b) one slot on
   weekdays 07-19 PT, ~$3.73/day; (c) one slot 24×7, ~$10.44/day; (d) decide
   after lane MEASURE. Recommended: (d).
4. **Slim the browser image (no headless shell, no second Node, no codex)?**
   (a) yes, after STREAM; (b) yes, now; (c) no. Recommended: (a).
5. **Should the browser image keep the agent toolbox?** (a) keep it, since
   agents use gh/gcloud/terraform there too; (b) drop it for a slimmer
   browser image. Recommended: (a). With streaming on, its weight barely
   shows.
6. **Measure a smaller `browser` class (4 vCPU / 8 GiB)?** (a) measure, then
   decide; (b) leave 8/16. Recommended: (a). It is a contract change either
   way.

## 9. Decisions (owner, 2026-10-13 ~05:00 PDT)

The owner answered §8 on 2026-10-13. Lane names refer to §7.

1. **STREAM: yes.**
   - Remove the redundant `WORKDIR` so the browser image has no duplicate layer.
   - Add a release check that fails on duplicate layers.
   - Read on 2026-10-13 at 12:00 UTC: `containerfilesystem.googleapis.com` is **already enabled** in `saga-agents-staging`, so the shared project needs no change.
   - In the same reading, `agents-staging` (the other team's cluster) is a Standard cluster, not Autopilot.
2. **Enabling the API:** not needed, because it is already on (see 1).
3. **Warm placeholder slots: decide after lane MEASURE.** Invariant 1 stays as written until then.
4. **Slim the image: yes, after STREAM.**
   - Remove the headless shell, the second Node and codex (about 283 MB).
   - codex, which BROWSER-ENFORCE could not remove from a derived image, was
     answered at ~07:15 PDT: **a codex-less base for the browser** (lane
     BROWSER-NO-CODEX, §2.3). The browser image is built FROM
     `agent-runtime-base-nocodex`, the base Dockerfile built with
     `INSTALL_CODEX=0`; every other image is unchanged. About 130 MB,
     estimated.
5. **Toolbox: keep.**
   - gcloud, terraform, checkov, trivy and the rest stay in the browser image.
   - The owner made no separate choice on this; it is the recommended default kept by the slimming decision in 4.
6. **Smaller `browser` class: measure first** (lane MEASURE). Any change is a contract request.

**After MEASURE (owner, 2026-10-14 ~18:10 PDT, on §11's numbers):**

3. **Warm placeholder slots: no.** A cold browser start stays at about
   125 s at p50. Invariant 1 stays as written: only `LEASED` through
   `RUNNING` create infrastructure demand. Do not reopen this without a new
   measurement and the owner's say.
6. **Smaller `browser` class: yes, as a contract request now.** It is
   [request 79](contract-change-requests.md): the `browser` class goes from
   8 vCPU / 16 GiB to 4 vCPU / 8 GiB, with 4 GiB of workspace tmpfs.

**Later the same evening (owner, 2026-10-14 ~22:55 PDT): "Drop to 1".**
Request 79 as filed kept `units` at 2. The owner dropped it to 1, so a
browser agent now weighs what a `standard` one does: a pool of 20 units admits
20 browser agents where it admitted 10. **Applied** by lane BROWSER-CLASS
(the pull request that marks request 79 APPLIED): `ResourceClass("browser",
cpu=4, memory_gib=8, disk_gib=4, units=1)`. The live display sidecar's split
of the pod became 3 vCPU / 5 GiB for the display and 1 vCPU / 3 GiB for the
worker, from §11's per-container peaks
(`apps/scheduler/scheduler/dispatch.py::LIVE_DISPLAY_SHARE`).

## 10. How to measure (lane MEASURE)

`scripts/startup-measure.sh` is the measurement decisions 3 and 6 wait for.
It is read-only. It reads every start in the window through the API's
`startup_progress` events, never Firestore, in the caller's tenant (or
`SWARM_API_TENANT`'s). For each runner_profile and resource class, split into
all, cold and warm starts, it prints n, the cold share and p50/p95 of four
segments: node wait (`dispatched -> pod_scheduled`), pull (`image_pulling ->
image_pulled`), browser (`container_started -> first_frame`) and total
(`dispatched -> first_frame`). A start is cold when its node wait is over
10 s (§7). It then takes `kubectl top` samples of the `browser` pods on
`swarm-autopilot` through `kubectl_bin`, after `assert_kube_context` has
refused any other context, so it never reaches `agents-staging`. It compares
the largest sampled pod (display plus worker) with the catalogue's `standard`
class, 4 vCPU / 8 GiB, at 80 % headroom, because requests == limits leaves no
burst. Run it after the IMG-STREAM release, while browser agents are running,
because a sample with no browser pod is reported as NOT_MEASURED, never as a
fit:

```bash
scripts/configure-kubectl.sh            # the swarm cluster's own kubeconfig
scripts/startup-measure.sh --since 7d --samples 8 --interval 15 \
  --save scratchpad/startup-measure/$(date -u +%Y%m%d)
```

Record the table, the verdict, the time it was read and the release it
measured here. Compare the pull row with §1's 42 s for STREAM's effect, and
the cold share and the cold node wait for decision 3. `--save` keeps what was
read, so the recorded numbers can be re-derived offline with
`scripts/startup_measure.py report --top <dir>/top.txt <<dir>/events.json`.
`kubectl top` samples miss any peak that falls between two samples. A FITS
verdict is therefore evidence for the contract request decision 6 asks for,
not the decision itself.

## 11. First full measurement (2026-10-15 01:00 UTC, release b93ad6cb)

Read 2026-10-15 between 00:58 and 01:05 UTC (17:58–18:05 PDT, 10-14), in
tenant `u-bogdan`, over the 2 days before it, while the live-browser QA matrix
(batch `full-1015`) was running browser agents. The deployed release was
b93ad6cb, which includes IMG-STREAM (#1239). The window also covers starts from
before that release, so the pull row is not a clean before/after.

**How it was read, and why not with the script as written.** The script's
default credential is the operator's own gcloud access token, and IAP
answered 401 to it. The account `.env.example` suggests for
`SWARM_IMPERSONATE_SA` (`swarm-verify`) is not a member of `u-bogdan`, so the
API answered 403 `tenant_not_member`. The same reads (`GET /v1/tasks`, then
every events page of each agent) were therefore made through the sc plugin's
API client as the owner, written in the script's own `events.json` stream
format. The `kubectl top` samples were taken on `swarm-autopilot` only. The
numbers below are `scripts/startup_measure.py report --since 2d --top
top.txt <events.json` over what was read. The inputs are kept in
`scratchpad/startup-measure/20261015/` on the operator's machine; they are not
committed, because they are task listings.

Visited 480 agents and 505 dispatched attempts. 392 starts fall in the window,
113 of them without a startup step. Cold means a node wait over 10 s.
Times are p50/p95 in seconds.

| profile | class | starts | n | cold | node wait | pull | browser | total |
|---|---|---|---|---|---|---|---|---|
| claude-code-browser | browser | all | 17 | 4/8 (50%) | 3.8/45.4 | 4.0/76.0 | 2.9/4.6 | 6.6/129.4 |
| claude-code-browser | browser | cold | 4 | | 38.6/45.4 | **54.0/76.0** | 2.9/4.5 | **125.2/129.4** |
| claude-code-browser | browser | warm | 4 | | -0.5/3.8 | 0.0/4.0 | 2.7/4.6 | 6.2/20.4 |
| claude-code | standard | all | 317 | 54/317 (17%) | -0.2/42.3 | 0.0/32.0 | | |
| claude-code | standard | cold | 54 | | 37.5/55.2 | 7.0/33.0 | | |
| indexer | standard | all | 57 | 11/57 (19%) | -0.3/44.7 | 2.0/47.0 | | |
| browser | browser | all | 1 | 1/1 | 38.1 | | | |

A negative node wait means `pod_scheduled` was recorded a fraction of a second
before `dispatched`: two clocks, the reconciler's and the scheduler's.

**What it says.**

* **A warm browser start takes 6 s at p50 and a cold one 125 s.** Half of the
  browser starts that carry steps were cold. The cold start is still a node
  wait plus a pull, as §1 found. The cold pull here is 54 s at p50, against
  §1's single 42 s, with n = 4. That is too few starts, and too mixed across
  the release, to show IMG-STREAM's effect either way. Re-read it once a day
  of post-release starts exists.
* **Decision 3 (warm slots).** On these numbers a warm slot saves a cold
  browser start its node wait (38.6/45.4 s) and, with the image on the node,
  its pull (54.0/76.0 s): about 90 s on half of the browser starts.
* **Decision 6 (a smaller class).** The `browser` pods sampled (4 pods,
  8 samples, 15 s apart) peaked at **2.13 cores and 1.85 GiB**. That is under
  80 % of the 4 vCPU / 8 GiB `standard` class, against the 8 vCPU / 16 GiB they
  were given then: FITS. (Applied as contract request 79, §9 decision 6.) The display sidecar peaked at 1.90 cores and 1.48 GiB,
  the worker at 0.55 cores and 0.37 GiB. Samples miss peaks between them, and
  four pods are not a load test. This is evidence for the contract request,
  not the decision.

### 11.1 A new step on every GKE start: the cache disk's attach (CACHE-PVC, unmeasured)

**What changed after this measurement.** Since CACHE-PVC (owner decision
2026-10-15 ~04:00 PDT) every GKE worker pod, `standard` and `browser` alike,
has its `cache` volume on a 20 GiB pd-balanced network disk: a generic
ephemeral volume on the `swarm-cache-pd-balanced` StorageClass
(`kubernetes/storage/cache-storage-class.yaml`), instead of a 2 GiB local
emptyDir. The 2 GiB emptyDir evicted task_247c87a8714c4977921a twice, and
Autopilot's 10 GiB ephemeral-storage cap had no more local disk to give it
(`GKE_DISK` in `apps/scheduler/scheduler/dispatch.py`).

**Why it costs start-up time.** The class binds `WaitForFirstConsumer`, so the
disk cannot be made until the pod is scheduled: after `pod_scheduled` the CSI
driver creates a disk in the node's zone and attaches it, and the container
cannot start until it is mounted. A local emptyDir cost none of that. The new
step sits between the node wait and the pull in the table above, and it is
paid on warm starts too: a warm slot (decision 3) saves the node wait and the
pull, never this attach, because the disk is the pod's own.

**What it is: unmeasured.** No number here is a measurement. The warm 6 s and
the cold 125 s above were read before the change and do not include it. The
orchestrator measures the live number after the release that carries CACHE-PVC,
with §10's `scripts/startup_measure.py`, and records it here as a row per
class with the attach split out. Until then, read every start-up number in this
document as the attach plus what it says.
