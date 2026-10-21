# swarm-images cleanup: read the dry run, then turn deletion on

Artifact Registry `swarm-images` (saga-agents-staging, us-central1) carries six
cleanup policies, declared in
[`terraform/modules/artifact_registry/main.tf`](../../terraform/modules/artifact_registry/main.tf)
and held by
[`tests/terraform/registry_cleanup.tftest.hcl`](../../tests/terraform/registry_cleanup.tftest.hcl).
dev runs them in **dry run** (`cleanup_dry_run = true` in
[`terraform/environments/dev/dev.tfvars`](../../terraform/environments/dev/dev.tfvars)):
Artifact Registry evaluates them and logs what it would delete, and deletes
nothing. **Nothing is deleted until the owner has read the would-delete list
and approved turning dry run off** (owner, 2026-10-21, #1886).

## Why

On 2026-10-21 the repository held 2,096 GB, growing about 125 GB a day. Every
build is tagged with its commit SHA and the only DELETE policy was for UNTAGGED
versions, so a tagged image was never deleted: 9,187 digests carried only a SHA
tag and had never been promoted.

## What is kept and what goes

A version that matches **any** KEEP policy is kept, whatever the DELETE
policies say and whatever order they are written in.

| policy | action | matches |
|---|---|---|
| `keep-recent` | KEEP | the newest 20 versions of every package |
| `keep-channel-tags` | KEEP | any tag starting `dev` or `prod` (`:dev`, `:prod`), no age |
| `keep-promoted-30d` | KEEP | any tag starting `promoted-`, version newer than 30 days (how long a release's image manifest is kept for rollback) |
| `keep-owner-pinned` | KEEP | package `workspace-apply*` with a `promoted-` tag, no age (the owner pins it by digest in `terraform/bootstrap/terraform.tfvars`) |
| `delete-untagged` | DELETE | untagged, older than 7 days |
| `delete-tagged-stale` | DELETE | tagged, older than 14 days: above the 7-day CI reuse window (`BUILD_REUSE_MAX_AGE_DAYS`) plus release time |

Tag prefixes are prefixes. `keep-channel-tags` also keeps any version carrying
an **old** promotion mark `dev-promoted-<tag>` or `prod-promoted-<tag>`:
`scripts/registry-remark.sh` added the new `promoted-<channel>-<tag>` mark
next to the old one and removed nothing, so those digests stay off the
would-delete list until the old marks are removed. Whether to remove them is
the owner's call (the question in the REGISTRY-CLEANUP pull request).

## 1. Read the would-delete list

Artifact Registry evaluates the policies about once a day, so the first dry
run can appear up to a day after the apply that set them. Each evaluation is
logged as a `BatchDeleteVersions` call with `validateOnly=true` on the
`swarm-images` parent:

```bash
mkdir -p build
gcloud logging read '
  protoPayload.serviceName="artifactregistry.googleapis.com"
  AND protoPayload.methodName:"BatchDeleteVersions"
  AND protoPayload.request.validateOnly=true
  AND protoPayload.request.parent:"repositories/swarm-images"' \
  --project saga-agents-staging --freshness 2d --format json \
  > build/registry-would-delete.json
jq length build/registry-would-delete.json
jq -r '.[].protoPayload.request.names[]?' build/registry-would-delete.json \
  | sed -E 's#.*/packages/([^/]+)/versions/(.*)#\1\t\2#' | sort -u \
  > build/registry-would-delete.tsv
wc -l < build/registry-would-delete.tsv
```

Each line of `build/registry-would-delete.tsv` is `<package>\t<sha256:digest>`.

**An empty result is not "nothing would be deleted".** Check the first `jq
length`: 0 entries can mean the policies have not run yet, or that **Data
Access audit logging is off** for Artifact Registry in this project, in which
case the dry run's calls are never written. Turning Data Access logs on is the
owner's call (it is billed by volume); until they are on, there is no list to
read, and dry run stays on.

## 2. Compare it with the KEEP set

List every version with its tags and age, and compute what the KEEP policies
should hold:

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/saga-agents-staging/swarm-images \
  --include-tags --format json > build/registry-versions.json

jq -r --arg since "$(date -u -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
                     || date -u -v-30d +%Y-%m-%dT%H:%M:%SZ)" '
  def tags: (.tags // []) | if type == "string" then split(",") else . end;
  .[]
  | (.package | split("/") | last) as $pkg
  | select(
      any(tags[]; startswith("dev") or startswith("prod"))
      or (any(tags[]; startswith("promoted-")) and .createTime >= $since)
      or ($pkg | startswith("workspace-apply")) and any(tags[]; startswith("promoted-"))
    )
  | "\($pkg)\t\(.version)"' build/registry-versions.json | sort -u \
  > build/registry-keep.tsv

comm -12 build/registry-keep.tsv build/registry-would-delete.tsv
```

The `comm` output must be **empty**: a line there is a version a KEEP policy
should hold and the dry run would delete, and dry run stays on until it is
explained. Check by hand too:

* the digest `workspace_apply_image` pins in `terraform/bootstrap/terraform.tfvars`
  is not on the list;
* no digest named in the current release manifest (`build/images-dev.json`, or
  the `images-dev` artifact of the last release run) is on the list;
* no package has fewer than 20 versions left after the list is removed
  (`keep-recent`).

Send the owner the count of `build/registry-would-delete.tsv`, the `comm`
result and these three checks. Do not paste the files into an issue: they are
digests, not secrets, but they are thousands of lines.

## 3. Turn dry run off

Only after the owner approves, in a pull request:

```diff
-cleanup_dry_run = true
+cleanup_dry_run = false
```

in `terraform/environments/dev/dev.tfvars`. The release applies it. The infra
root's default is `true`, so `prod.tfvars`, which does not set it, stays a dry
run. **prod's root names the same `swarm-images` repository in the same
project**: if prod is ever applied with dry run on while dev has it off, the
two applies flip the repository between them. Set both, or neither.

## Rollback

**A deleted version cannot be undeleted.** Setting `cleanup_dry_run = true`
again stops further deletions from the next evaluation on; it brings nothing
back. A digest that is needed and gone is rebuilt from its commit
(`scripts/build-images.sh --tag <sha>`, or a release run of that commit), which
produces a new digest: update whatever pinned the old one (a release manifest,
`workspace_apply_image` in `terraform/bootstrap/terraform.tfvars`) to the new
one. A rebuild is not bit-for-bit, so a pin that must stay exact is what the
KEEP policies above exist for. Check that its policy covers it before turning
dry run off, not after.
