# swarm-images cleanup: read the dry run, then turn deletion on

Artifact Registry `swarm-images` (saga-agents-staging, us-central1) carries six
cleanup policies, declared in
[`terraform/modules/artifact_registry/main.tf`](../../terraform/modules/artifact_registry/main.tf)
and held by
[`tests/terraform/registry_cleanup.tftest.hcl`](../../tests/terraform/registry_cleanup.tftest.hcl).
dev ran them in **dry run** from 2026-10-21 (owner, #1886): Artifact Registry
evaluated them and deleted nothing. **Since 2026-10-22 dev deletes for real**
(`cleanup_dry_run = false` in
[`terraform/environments/dev/dev.tfvars`](../../terraform/environments/dev/dev.tfvars);
owner, "Go live via a PR", PR 1932). Data Access audit logs were off, so the
dry run wrote no list (§1); the owner approved the list computed by §2 from every
version's tags and age against the live policies instead. At 06:50Z it held 4,233
of 13,568 versions (about 1.2 TB) and none of the 11 images then deployed on
Cloud Run. Going back to a dry run is that one line set to true.

## Why

On 2026-10-21 the repository held 2,096 GB, growing about 125 GB a day. Every
build is tagged with its commit SHA and the only DELETE policy was for UNTAGGED
versions, so a tagged image was never deleted: 9,187 digests carried only a SHA
tag and had never been promoted.

## What is kept and what goes

A version that matches **any** KEEP policy is kept, whatever the DELETE
policies say and whatever order they are written in.
So a package with fewer than 20 versions is never cleaned at all:
`keep-recent` holds every one. That is why the seven misnamed one-version
`agent-runtime-base<sha>` packages need the one-off delete in §4.

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

## 4. One-off: delete the seven misnamed `agent-runtime-base<sha>` packages

Seven packages named `agent-runtime-base` with a commit SHA glued on
(`agent-runtime-baseb885502568e`… and six more, one version each, pushed
2026-09-16) were written by a build that dropped the `:` between the image
name and its tag. Nothing reads them: every release, Job and pin names
`agent-runtime-base:<tag>` or `agent-runtime-base@sha256:…`. The cleanup
policies cannot remove them, because `keep-recent` keeps the newest 20
versions of **every package** and each of these packages has one version, so
they are kept forever. The owner approved deleting them once, by hand
(BGD, 2026-10-27, #1886): the operator lists them, **shows BGD the list**, and
only then runs the delete below. This step is not a script and is not run by
CI or by an agent.

Run every block in **bash**, not the interactive zsh: the loops read lines and
zsh would not split them the same way.

### 4a. List them

```bash
mkdir -p build
gcloud artifacts packages list \
  --repository swarm-images --location us-central1 --project saga-agents-staging \
  --format 'value(name)' \
  | sed 's#.*/packages/##' \
  | grep -E '^agent-runtime-base[0-9a-f]{7,40}$' \
  | sort > build/registry-misnamed.txt
wc -l < build/registry-misnamed.txt
cat build/registry-misnamed.txt
```

The pattern is anchored and hexadecimal on purpose: the real packages
`agent-runtime-base` and `agent-runtime-base-nocodex` do **not** match it, and
must never be on this list. Expect **7** lines. Any other count, or any name
that is not `agent-runtime-base` followed only by hex digits, stops here:
bring what you found to the owner instead.

### 4b. Show each one is a stray

```bash
while IFS= read -r pkg; do
  gcloud artifacts versions list \
    --package "${pkg}" --repository swarm-images --location us-central1 \
    --project saga-agents-staging --format json \
    | jq -r --arg p "${pkg}" '
        "\($p)\tversions=\(length)\t"
        + ([.[] | .createTime] | join(","))'
  gcloud artifacts docker tags list \
    "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/${pkg}" \
    --format 'value(tag)' | sed "s#^#${pkg}\ttag=#"
done < build/registry-misnamed.txt | tee build/registry-misnamed-detail.tsv

git grep -nE 'agent-runtime-base[0-9a-f]{7,40}([^0-9a-f]|$)' -- ':!docs/' \
  || echo "repository: no reference"

for kind in services jobs; do
  gcloud run "${kind}" list --region us-central1 --project saga-agents-staging \
    --format json | jq -r '.. | .image? // empty'
done | grep -E '/agent-runtime-base[0-9a-f]{7,40}[:@]' || echo "Cloud Run: no reference"
```

What must hold before going on:

* every package has `versions=1`, created on 2026-09-16;
* no tag starts with `dev`, `prod` or `promoted-` (a channel or promotion
  mark means something released it, and the owner decides again);
* both greps print their "no reference" line: nothing in the repository and no
  Cloud Run service or Job names one of them. `docs/` is left out of the first
  because this runbook and #1886's notes name the packages on purpose.

Send BGD `build/registry-misnamed.txt` and `build/registry-misnamed-detail.tsv`
(seven names and their tags; digests, not secrets) and wait for an explicit
go-ahead on **that** list. An approval of a different list is not an approval
of this one.

### 4c. Delete them (typed confirmation)

```bash
bash <<'DELETE'
set -euo pipefail
# A destructive step never honours SWARM_ASSUME_YES (CLAUDE.md, "Writing
# scripts"): the count is typed at a terminal, every time.
if [[ -n "${SWARM_ASSUME_YES:-}" ]]; then
  echo "ignoring SWARM_ASSUME_YES: this delete always needs the count typed at a terminal" >&2
  unset SWARM_ASSUME_YES
fi
list=build/registry-misnamed.txt
[[ -s "${list}" ]] || { echo "no ${list}: run 4a first" >&2; exit 1; }

# Re-list now and refuse if the registry no longer matches what BGD approved.
gcloud artifacts packages list \
  --repository swarm-images --location us-central1 --project saga-agents-staging \
  --format 'value(name)' \
  | sed 's#.*/packages/##' \
  | { grep -E '^agent-runtime-base[0-9a-f]{7,40}$' || true; } | sort > build/registry-misnamed.now
diff -u "${list}" build/registry-misnamed.now \
  || { echo "the registry changed since the list was approved; stopping" >&2; exit 1; }

count=0
while IFS= read -r pkg; do
  [[ "${pkg}" =~ ^agent-runtime-base[0-9a-f]{7,40}$ ]] \
    || { echo "refusing: '${pkg}' is not a misnamed agent-runtime-base package" >&2; exit 1; }
  count=$((count + 1))
done < "${list}"
[[ "${count}" -eq 7 ]] || { echo "refusing: ${count} packages listed, the approval was for 7" >&2; exit 1; }

exec 3</dev/tty 2>/dev/null \
  || { echo "refusing: no terminal to type the confirmation at" >&2; exit 1; }
cat "${list}" >&2
printf 'This permanently deletes the %s packages above from swarm-images.\nType "delete %s misnamed packages" to continue: ' "${count}" "${count}" >&2
IFS= read -r answer <&3
[[ "${answer}" == "delete ${count} misnamed packages" ]] \
  || { echo "confirmation did not match; nothing deleted" >&2; exit 1; }

deleted=0
while IFS= read -r pkg; do
  gcloud artifacts packages delete "${pkg}" \
    --repository swarm-images --location us-central1 --project saga-agents-staging \
    --quiet </dev/null
  deleted=$((deleted + 1))
  echo "deleted ${deleted} of ${count}: ${pkg}"
done < "${list}"
echo "deleted ${deleted} of ${count} package(s)"
DELETE
```

`--quiet` only answers gcloud's own per-package prompt; the typed line above is
the confirmation. The script reads its answer from `/dev/tty`, so it refuses
when nothing is attached to a terminal (CI, an agent, a pipe), whatever the
environment says. It stops at the first failed delete and prints how far it
got; running it again re-lists, so the `diff` stops it and 4a starts over with
what is left.

### 4d. Check

```bash
gcloud artifacts packages list \
  --repository swarm-images --location us-central1 --project saga-agents-staging \
  --format 'value(name)' | sed 's#.*/packages/##' \
  | grep -cE '^agent-runtime-base[0-9a-f]{7,40}$' || true
```

It must print `0`, and `agent-runtime-base` and `agent-runtime-base-nocodex`
must still be listed. Record the date, the seven names and that count in a
comment on #1886. There is no undo (see Rollback); nothing referenced these
packages, which is why the owner approved deleting them rather than rebuilding.

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
