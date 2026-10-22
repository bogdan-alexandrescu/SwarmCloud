"""Where an agent's tool caches and temp files go: never the workspace volume.

WHY (CACHE-SCRATCH, owner decision 2026-10-15). Three claude-code attempts were
evicted twice each on 2026-10-15 with `Usage of EmptyDir volume "workspace"
exceeds the limit "5Gi"` and were not retried: task_41ae250f and task_c6a07695
(CI-fix rounds on PR 1338) and task_44c61c77 (BROWSER-CLASS implement). The
workspace volume held the clone and the agent's outputs, and ALSO everything
its tools fill: uv's cache, npm's cache, pip's, terraform's providers, and
every pytest/xdist temp directory -- because HOME and TMPDIR were both inside
it. A disk emptyDir's sizeLimit is not a filesystem size: the write succeeds
and kubelet evicts the whole pod on its next scan, with no checkpoint and no
park (dispatch.GkeDisk).

WHAT THIS MODULE DECIDES. One directory per attempt, `Workspace.cache`, and
the environment that points the agent's tools at it (`cache_env`). The worker
sets it in `Workspace.child_env`, before the runner starts, and the CLI
runners carry the names on to the agent (`AGENT_CACHE_ENV`). Its place is:

* GKE -- `<SWARM_CACHE_ROOT>/<attempt>`, on the pod's own `cache` emptyDir
  (`dispatch.CACHE_MOUNT`): disk-backed, with its own sizeLimit, summed into
  the pod's ephemeral-storage with requests == limits (invariant 7). A full
  cache is then the cache volume's limit, never the workspace's.
* Cloud Run -- no SWARM_CACHE_ROOT. Every volume there is a memory tmpfs and
  the provider cannot give a Job a second disk, so the caches are KEPT SMALL
  instead: `<workspace root>/<attempt>/cache`, inside the workspace tmpfs,
  where its size counts honestly against the same limit, with uv's and pip's
  caches switched off (`UV_NO_CACHE`, `PIP_NO_CACHE_DIR`). npm has no
  no-cache mode, so its cache stays, in that tmpfs, counted.

NEVER CHECKPOINTED, NEVER AN ARTIFACT. The directory is a sibling of `work/`
(Cloud Run) or on another volume entirely (GKE), so neither the checkpoint's
walk of `work/` nor the artifact upload's walk of `artifacts/` can reach it;
`create()` empties it at the start of every attempt and `destroy()` removes it
at the end. Checkpointing (invariant 8) covers `work/` exactly as before.

ONE THING THIS DOES NOT DO. uv installs a venv by hard-linking from its cache,
and a hard link cannot cross volumes, so on GKE uv copies (`UV_LINK_MODE=copy`
says so, instead of uv warning on every install). The venv in the checkout is
therefore as large as it was; what leaves the workspace is everything a
cache holds that is not the venv's files -- npm's tarballs, pip's and uv's
downloads and builds, terraform's providers, and all of TMPDIR.

THE NPM CACHE STARTS FROM THE IMAGE'S (AU8-SEED, owner decision 2026-10-22,
"Seed the attempt cache from the image"). `agent-runtime-base` carries a
read-only npm cache warmed from `apps/swarm-ui/package-lock.json` at
`/opt/npm-cache` (AU8-IMAGE-NPM, PR 1949, about 52 MB). Pointing npm at it
directly does not work, as that lane measured: `npm_config_cache` above
overrides every npmrc, and npm 11.19.0 cannot install from a read-only cache
when any package is missing (EACCES on `_cacache/tmp`, no network fallback).
So `seed_npm_cache` copies it, `_cacache` layout intact and every entry
writable, into the attempt's own npm cache before the agent starts;
`npm_config_cache` stays the attempt's directory. The copy costs its 52 MB per
attempt where the cache lives: the workspace's memory tmpfs on Cloud Run, the
cache volume on GKE. It is an optimisation and never a reason to fail: an
absent image cache or a failed copy leaves the empty cache npm had before,
and npm fetches from the network as it always did.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

#: Set on the GKE worker container by the dispatcher (and the YAML templates):
#: the mount of the pod's own cache volume. Unset on Cloud Run.
CACHE_ROOT_ENV = "SWARM_CACHE_ROOT"

#: `Workspace.cache`'s name inside the attempt tree, on Cloud Run.
CACHE_DIR_NAME = "cache"

#: Each tool's directory inside `Workspace.cache`, by the variable that names it.
CACHE_SUBDIRS: dict[str, str] = {
    "UV_CACHE_DIR": "uv",
    "npm_config_cache": "npm",
    "PIP_CACHE_DIR": "pip",
    # terraform refuses a plugin cache directory that does not exist, so
    # `create` makes every one of these before the runner starts.
    "TF_PLUGIN_CACHE_DIR": "terraform-plugins",
    "XDG_CACHE_HOME": "xdg",
}

#: Only where the cache shares the workspace's tmpfs (Cloud Run): keep it small.
NO_CACHE_ENV: dict[str, str] = {"UV_NO_CACHE": "1", "PIP_NO_CACHE_DIR": "1"}

#: Only where the cache is its own volume (GKE): a hard link cannot cross it.
SEPARATE_VOLUME_ENV: dict[str, str] = {"UV_LINK_MODE": "copy"}

#: Every name `cache_env` can set. The CLI runners build their agent's
#: environment from scratch and carry exactly these on from their own
#: (`cliagent._PLAIN_PASSTHROUGH`, `generic._build_env`). Paths and switches,
#: never a credential.
AGENT_CACHE_ENV: tuple[str, ...] = (
    *CACHE_SUBDIRS,
    "TMPDIR",
    *NO_CACHE_ENV,
    *SEPARATE_VOLUME_ENV,
)


def cache_env(cache: Path, tmp: Path, *, own_volume: bool) -> dict[str, str]:
    """The variables that point an agent's tools at `cache`, and its TMPDIR at `tmp`."""
    env = {name: str(cache / sub) for name, sub in CACHE_SUBDIRS.items()}
    env["TMPDIR"] = str(tmp)
    env.update(SEPARATE_VOLUME_ENV if own_volume else NO_CACHE_ENV)
    return env


#: Where `agent-runtime-base` leaves its warmed, read-only npm cache
#: (`images/agent-runtime-base/Dockerfile`, AU8-IMAGE-NPM). Absent in any
#: other image, where seeding is a no-op.
IMAGE_NPM_CACHE = Path("/opt/npm-cache")


def seed_npm_cache(cache: Path, log: Any, *, image_cache: Path | None = None) -> int:
    """Copy the image's npm cache into the attempt's, writable; the bytes copied.

    A no-op (0) when the image cache is absent or empty, and when the
    attempt's npm cache already holds content, so a second call copies
    nothing. A copy that fails is logged and emptied, never raised: the
    attempt then runs with the empty cache it would have had anyway.
    `image_cache` defaults to `IMAGE_NPM_CACHE`, read at call time.
    """
    if image_cache is None:
        image_cache = IMAGE_NPM_CACHE
    dest = cache / CACHE_SUBDIRS["npm_config_cache"]
    if not image_cache.is_dir() or not any(image_cache.iterdir()):
        log.info("npm image cache absent or empty; not seeded", image_cache=str(image_cache))
        return 0
    if dest.is_dir() and any(dest.iterdir()):
        log.info("npm cache already holds content; not seeded", npm_cache=str(dest))
        return 0
    started = time.monotonic()
    copied = 0
    try:
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(image_cache):
            target = dest / Path(dirpath).relative_to(image_cache)
            # Not copytree: its copystat would carry the image's read-only
            # modes over, and npm must write here (`_cacache/tmp` first).
            target.mkdir(mode=0o700, exist_ok=True)
            os.chmod(target, 0o700)
            for name in filenames:
                out = target / name
                shutil.copyfile(os.path.join(dirpath, name), out)
                os.chmod(out, 0o600)
                copied += out.stat().st_size
    except OSError as exc:
        # A half-copied cache is worse than none: drop it and leave npm the
        # empty, writable directory it had before. If even that fails, npm
        # makes the directory itself on first use.
        shutil.rmtree(dest, ignore_errors=True)
        try:
            dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
        log.warning(
            "npm cache seed failed; continuing with an empty cache",
            error_type=type(exc).__name__,
            error=str(exc),
            npm_cache=str(dest),
        )
        return 0
    log.info(
        "npm cache seeded from the image",
        bytes=copied,
        seconds=round(time.monotonic() - started, 3),
        npm_cache=str(dest),
    )
    return copied
