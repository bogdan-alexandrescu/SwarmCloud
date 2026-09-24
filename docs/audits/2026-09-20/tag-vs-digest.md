# A rebuilt tag leaves a stale revision, and the service reports healthy

**Found** 2026-09-20, during the image recovery described below.
**Owner** Track C (`terraform/`). Reported rather than changed, per CLAUDE.md.
**Severity** Silent. Nothing fails; the wrong code serves and every check passes.

---

## What happens

`terraform/infra` takes a single `image_tag` variable and stamps it on every
service and every job:

* `terraform/infra/main.tf:270,304,319,334,354` — the five Cloud Run services
* `terraform/infra/locals.tf:226` — the runner-profile jobs

`scripts/lib/deploy.sh` reads one tag out of the promote manifest and passes it
as that variable. So the manifest — which records a **digest** per image — is
not what Terraform consumes. The tag is.

Cloud Run resolves a tag to a digest **once, when the revision is created**,
and pins it. So:

1. Build image X at tag `T`. Tag `T` → digest `D1`.
2. Deploy. Revision pins `D1`.
3. Rebuild image X at the same tag `T`. Tag `T` → digest `D2`.
4. Deploy again. Terraform's config is unchanged (`image = ...:T` both times),
   so it plans no change to that service and creates no revision.
5. The service keeps serving `D1`, reports `HEALTHY`, and `latestReady ==
   latestCreated == serving`. Every check passes.

The new code is in the registry, promoted, scanned, and never runs.

## The evidence

On 2026-09-19, `swarm-ui` was built twice at tag `7f6a80cb32d5` — once alone,
then again as part of a full build.

```
revision swarm-ui-00003-psf serves
  us-central1-docker.pkg.dev/.../swarm-ui@sha256:6bcd77ab2288...   (the FIRST build)

build/deployed-images-dev.json records for swarm-ui
  sha256:5b58e934de1565...                                        (the SECOND build)
```

`make deploy` reported `Apply complete! Resources: 0 added, 7 changed, 0
destroyed` and exit 0. All five services verified as `HEALTHY` with
`created == ready == serving`. The UI was still the pre-fix build.

Nothing in the pipeline could have caught it: the promote step was correct, the
scan was correct, the apply was correct, and the readiness check was correct.
They were all answering a different question from "is the code I just built the
code that is running".

## Why this is the same failure as everything else in this repository

A stale revision is indistinguishable from a current one by every signal the
platform exposes. It is the deployment-layer instance of the pattern CLAUDE.md
already records for jq (`false // true`), for `fs_request`, and for the build
manifest: **a thing that did not happen renders identically to a thing that
succeeded.**

## The fix, which is small and is Track C's

The plumbing already exists on both sides:

* `scripts/lib/deploy.sh:94` already prefers a variable named `image_refs` over
  `image_tag`, and builds the map from the manifest:
  `jq -c '[.images[] | {key:.name, value:.ref}] | from_entries'`, where `.ref`
  is the `image@sha256:...` form.
* `build/deployed-images-dev.json` already carries a digest and a `ref` for
  every image.

So the change is to declare `variable "image_refs"` in `terraform/infra` and
index it where `${var.image_tag}` is interpolated today. A digest changes when
the content changes, so Terraform sees a real diff and creates a revision — and
a rebuilt tag can no longer be silently ignored.

Deploying by digest also removes the tag-mutability question entirely:
`var.immutable_image_tags` exists (`variables.tf:186`) precisely because
mutable tags are a hazard, and pinning by digest is the stronger form of the
same intent.

## What was done instead, and what it does not cover

`scripts/lib/deploy.sh` gained `assert_tag_is_complete`, which refuses to
deploy a tag that not every image carries. That fixes a **different** failure
found the same day — a partial build minting a tag only one image has, which
produced six unservable revisions before the apply gave up.

It does **not** cover this one. A rebuilt tag is complete by definition; every
image carries it. The guard passes, and the stale revision survives.

## Reproducing it

```bash
./scripts/build-images.sh swarm-ui          # tag T -> digest D1
make push deploy                            # revision pins D1
./scripts/build-images.sh swarm-ui          # tag T -> digest D2
make push deploy                            # no diff, no new revision
gcloud run revisions describe <serving> --format='value(status.imageDigest)'
# prints D1
```

## Resolved, 2026-09-24

The fix above, and the two places it did not reach.

**terraform/infra takes `image_refs` and nothing else.** `image_tag` is gone.
Every Cloud Run service, every worker job and the verification job name
`<registry>/<name>@sha256:<digest>` from that map, and the root refuses to plan
while any image it deploys has no entry — a precondition on
`google_cloud_run_v2_job.verify`, because a module call cannot carry one. There
is no fallback to a tag, because a fallback is how a tag gets deployed without
anybody choosing it. The variable's validation refuses a tag, a tag beside a
digest, a truncated digest, another image's digest under this name, and an
image from another registry.

**The worker path, which this audit did not cover.** The scheduler built every
GKE Job and every Cloud Run job it created from `WORKER_IMAGE_TAG`, resolved at
pull time with `IfNotPresent` — the same staleness one layer down, with no
revision to inspect. It now receives `WORKER_IMAGE_REFS` from the same map,
refuses a non-digest value at start, refuses to dispatch a profile with no
digest, and moves a job it created earlier onto the current digest before it
runs again.

**The check this audit said nothing could make.** `scripts/lib/deploy.sh
--verify-only` runs after every release apply and compares, digest to digest,
every service's template, every terraform-managed job and the scheduler's
worker map against the manifest the apply pinned. A service serving an older
digest now fails the release, naming both digests.

Where the value comes from: `scripts/lib/image-refs.sh` turns the promotion
manifest (a deploy), the channel tag resolved once (a redeploy without a
build), or the `image_refs` output in state (a plan that is not a deploy) into
the tfvars file. Pinned by `tests/terraform/image_refs.tftest.hcl`,
`tests/unit/control_plane/test_worker_image_by_digest.py` and
`tests/integration/test_deploy_by_digest.py`.

**Not verified here:** an apply against the live project. Nothing in this
change has been applied; the first release after it merges is the first time
the services move from `:<tag>` to `@sha256:` (same content, one new revision
each).
