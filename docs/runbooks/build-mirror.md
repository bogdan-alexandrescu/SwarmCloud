# The build mirrors: fill them, check them, bump a pin through them

Image builds do not fetch from ghcr.io, Docker Hub or a GitHub release
download. They read this project's own mirrors, declared in
[`terraform/infra/build_mirror.tf`](../../terraform/infra/build_mirror.tf):

| what | where | filled by |
|---|---|---|
| every base image (`FROM`) and every `# syntax=` frontend | Artifact Registry **remote** repositories `swarm-mirror-ghcr` (upstream `https://ghcr.io`) and `swarm-mirror-dockerhub` (Docker Hub) | the first pull of a digest, or `scripts/refresh-build-mirror.sh` ahead of it |
| the release files agent-runtime-base installs: gh, terraform, tofu, tflint, shellcheck, trivy | `gs://swarm-build-mirror-<project>/<upstream URL without https://>` | only `scripts/refresh-build-mirror.sh` |

## Why

On 2026-10-12 three release image builds failed inside Cloud Build on fetches
from outside Google, with nothing wrong in the images:

* twice `Get https://ghcr.io/v2/: … Client.Timeout exceeded`, pulling
  `ghcr.io/astral-sh/uv` for `FROM ${UV_IMAGE} AS uvbin`;
* once `curl: (22) … error: 504`, downloading shellcheck from a github.com
  release in `images/agent-runtime-base/Dockerfile`.

A build now depends on GitHub and Docker Hub only the first time a pinned
digest or file enters the mirror, not on every build.

## What a build does

* **Images.** Each Dockerfile declares `MIRROR_DOCKER_HUB` and/or
  `MIRROR_GHCR` (the full repository path, in the dev project) and builds
  `FROM ${MIRROR_GHCR}/${UV_IMAGE}`, `FROM ${MIRROR_DOCKER_HUB}/library/${PYTHON_IMAGE}`
  and so on. The image ARGs keep the digests they had before. A remote
  repository serves a cached digest without asking upstream.
  `NODE_IMAGE` is still `node:<major>-…` in swarm-ui's Dockerfile because
  `application.yml` reads the Node major from that exact line.
* **Release files.** `scripts/build-images.sh` puts a `tool-mirror` step
  first in agent-runtime-base's Cloud Build (and in any `--build-only` build
  that builds it). That step copies every object listed in
  [`images/agent-runtime-base/tool-mirror.sha256`](../../images/agent-runtime-base/tool-mirror.sha256)
  into `build/tool-mirror` and checks each one's sha256. The Dockerfile
  bind-mounts that directory and checks the **same sha256 again** before it
  unpacks anything.
* **A missing object fails the build and names it.** The build does not fall
  back to upstream:

      build mirror object gs://swarm-build-mirror-saga-agents-staging/github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.gz is missing or unreadable: run scripts/refresh-build-mirror.sh

* **A pull request's build** (`--build-only --local`, on a GitHub runner)
  has no Google identity, by the owner's decision of 2026-10-05, so it cannot
  read either mirror. It reads upstream instead:
  * the release files, from their URLs, checked against the same manifest;
  * the images, through `--build-arg MIRROR_DOCKER_HUB=docker.io`,
    `MIRROR_GHCR=ghcr.io` and `BUILDKIT_SYNTAX=docker.io/docker/dockerfile:…@sha256:…`.

  The pull request's build therefore never exercises the mirrors: the first
  build that does is main's, in Cloud Build.

## Fill the mirrors (once, BEFORE the change that introduced them merges)

The order matters. `application.yml` builds every changed image on a push to
main, and `release.yml` runs `build` before `infrastructure`. A merge that
lands before the repositories exist and the bucket is filled therefore fails
main's image builds: every `FROM` names a repository that is not there, and
the `tool-mirror` step names the first missing object. So, from the branch
that adds `terraform/infra/build_mirror.tf`, before merging it:

```bash
make infra                                  # creates the two repositories and the bucket
scripts/refresh-build-mirror.sh             # uploads every release file, pulls every pinned image through
scripts/refresh-build-mirror.sh --check     # changes nothing; exit 0 means every build input is mirrored
```

Each upload is checked against its sha256 first. A file whose upstream
download does not match is refused and named. Every other file still goes up,
and the run exits 1.

The script needs `gcloud` signed in as someone who can write objects in the
bucket and read the repositories, plus `curl`, `jq` and `python3`. It writes
only to `gs://swarm-build-mirror-<project>` and deletes nothing.

Run it again whenever `--check` says something is missing. A build that
needs a missing object fails at its `tool-mirror` step, naming the object.

## Bump a pinned version

1. Change the version and sha256 ARGs in `images/agent-runtime-base/Dockerfile`
   as before, with the comment saying where the checksum came from.
2. Change the matching line in `images/agent-runtime-base/tool-mirror.sha256`.
   `tests/unit/scripts/test_build_mirror.py` fails until the two agree.
3. Run `scripts/refresh-build-mirror.sh --tools-only` **before** the change
   reaches main, so main's build finds the new object.

A base image bump (a new digest in a `*_IMAGE` ARG) needs nothing extra: the
remote repository fetches the new digest on its first pull. Running
`scripts/refresh-build-mirror.sh --images-only` caches it ahead of time.

## What is not mirrored

These fetches still go out from the build:

* apt (`deb.debian.org`), apk, PyPI and npm;
* `packages.cloud.google.com` (gcloud debs), `dl.k8s.io` (kubectl),
  `download.docker.com` (docker CLI);
* `go.dev` (the indexer's Go toolchain).

None of them failed on 2026-10-12, and none is on GitHub or Docker Hub. A
build that starts failing on one of them can be mirrored the same way: a row
in the manifest and a `file:///tmp/tool-mirror/…` URL in the Dockerfile.
