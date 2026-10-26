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

BIG SCRATCH DATA BELONGS UNDER TMPDIR, AND THE AGENT IS TOLD SO (issue #2436).
task_73850f24085447368991 (Product Oracle's integration lane, 2026-10-26) was
evicted twice with `EmptyDir volume "tmp" exceeds 3Gi`, after about two hours
each, and published nothing. TMPDIR already pointed here, at the 20 GiB cache
volume; the agent wrote a Postgres data dir (/tmp/pgdata), test logs (/tmp/tr)
and Chromium's unpacked libraries and fonts to a literal /tmp, the pod's 3 GiB
`tmp` emptyDir. Two things follow:

* `scratch_note` is the one sentence the claude-code runner appends to the
  agent's prompt: databases, test logs, unpacked libraries and fonts and
  downloads under $TMPDIR, never a literal /tmp, never the workspace. On Cloud
  Run, where there is no cache volume, a shorter sentence that promises none.
* `cache_env` sets PLAYWRIGHT_BROWSERS_PATH to `<cache>/ms-playwright` when the
  worker's environment has none, so a browser an agent installs lands on this
  volume by name. Only then: agent-runtime-browser's own value is the
  image's read-only Chromium, and the claude-code runner swaps it for the
  live-browser stub; both must keep winning. It is NOT in `AGENT_CACHE_ENV`:
  cliagent copies that list from the runner's environment AFTER the runner's
  own `extra_env`, which would put the image's real Chromium back where the
  stub belongs. The claude-code runner hands the cache's path on itself, and
  `image_browsers_path` tells it apart from an image's.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

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


#: Playwright's browsers directory (module docstring, issue #2436). Set by
#: agent-runtime-browser's ENV to its read-only Chromium; set here,
#: under the cache, only where the worker's environment has none.
BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"
BROWSERS_DIR_NAME = "ms-playwright"


def cache_env(
    cache: Path, tmp: Path, *, own_volume: bool, browsers_path_set: bool = False
) -> dict[str, str]:
    """The variables that point an agent's tools at `cache`, and its TMPDIR at `tmp`.

    `browsers_path_set`: the worker's environment already names a browsers
    directory (the browser image's), which is then left alone.
    """
    env = {name: str(cache / sub) for name, sub in CACHE_SUBDIRS.items()}
    env["TMPDIR"] = str(tmp)
    env.update(SEPARATE_VOLUME_ENV if own_volume else NO_CACHE_ENV)
    if not browsers_path_set:
        # Playwright makes the directory on install; nothing reads it before.
        env[BROWSERS_PATH_ENV] = str(cache / BROWSERS_DIR_NAME)
    return env


def image_browsers_path(environ: Mapping[str, str]) -> str:
    """The image's browsers directory in `environ`, or "" when there is none.

    The one `cache_env` set is not an image's: it is `<cache>/ms-playwright`,
    a sibling of XDG_CACHE_HOME's `<cache>/xdg`, and holds no browser until
    the agent installs one.
    """
    value = environ.get(BROWSERS_PATH_ENV, "").strip()
    xdg = environ.get("XDG_CACHE_HOME", "").strip()
    if value and xdg and Path(value) == Path(xdg).parent / BROWSERS_DIR_NAME:
        return ""
    return value


#: The scratch volumes' sizes the note names, MIRRORED from
#: apps/scheduler/scheduler/dispatch.py (the worker does not import the
#: scheduler): `CACHE_VOLUME_GIB`; `GKE_DISK`'s `tmp_gib`, 3 in every class;
#: and its largest `workspace_gib` (5; the browser class's is 4).
#: tests/unit/worker/test_scratch_note.py holds them to dispatch's values.
CACHE_VOLUME_GIB = 20
TMP_VOLUME_GIB = 3
WORKSPACE_VOLUME_GIB = 5


def scratch_note(own_volume: bool) -> str:
    """The one sentence an agent is told about big scratch data (issue #2436).

    `own_volume`: TMPDIR is on the GKE pod's cache volume
    (`Workspace.cache_on_own_volume`); False on Cloud Run, where it is inside
    the workspace's memory tmpfs and no cache volume exists to promise.
    """
    if own_volume:
        return (
            "Put large scratch data -- databases (e.g. a Postgres data "
            "directory), test logs, unpacked libraries and fonts, downloads -- "
            f"under $TMPDIR, which is on the {CACHE_VOLUME_GIB} GiB cache volume "
            "and never evicts the pod; never write it to a literal /tmp "
            f"({TMP_VOLUME_GIB} GiB: past it the pod is evicted and the attempt "
            f"lost) or to the workspace (at most {WORKSPACE_VOLUME_GIB} GiB, "
            "checkpointed)."
        )
    return (
        "Put scratch data under $TMPDIR, never a literal /tmp or the checkout: "
        "$TMPDIR is inside the workspace's in-memory filesystem and counts "
        "against this container's memory limit, so keep it small and delete "
        "what you no longer need."
    )


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
