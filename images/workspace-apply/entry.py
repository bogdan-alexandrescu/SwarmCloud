"""Step 0 of the personal-workspace job: the scrub (docs/workspaces.md §2.2).

The Cloud Run job `swarm-workspace-apply` runs this file as its fixed command,
`python3 -I /opt/swarm/entry.py <w-id> <mode>`, as swarm-workspace-deployer:
the identity with project-wide account-IAM power Google cannot narrow (§2.4).
Lane W6b of #847.

WHAT THIS STANDS BETWEEN. Anyone holding `run.jobs.runWithOverrides` on the
job may start an execution and choose its arguments, its environment
variables, its task count and its timeout. The scheduler, the acceptance
account and the release deployer hold that project-wide and unscopably
(§2.1), and none of them may steer the identity. The job's command is fixed in
the job spec, so the boundary is what THIS file lets an override reach:

  * THE ENVIRONMENT, NONE OF IT. Every variable handed in is discarded before
    anything else happens -- `os.environ` is emptied, so no library read below
    sees one either -- and step 1 is `execve`d with a FIXED environment written
    in this file. That is what keeps a caller from reaching the run through
    `PATH`, an exported bash function (`BASH_FUNC_*`), `BASH_ENV`,
    `SWARM_CALL_GUARD_ENFORCE`, `CLOUDSDK_*`, `http_proxy` or anything else.
    `-I` keeps Python itself from reading `PYTHON*`. What `-I` cannot stop is
    the dynamic loader reading `LD_*` before Python starts: §2.4 R6 states
    that residual risk and what would close it.
  * THE ARGUMENTS, EXACTLY TWO. `[<w-id>, <mode>]`, the id `w-<6 hex>` and the
    mode `create` or `limits`, or the execution ends here, having run nothing.
    Step 1 (scripts/workspace-apply.sh) checks the same again; neither check
    alone is load-bearing.
  * THE TASK COUNT, ONE. `CLOUD_RUN_TASK_COUNT` must be `1` and this task must
    be index `0`. A1's claim would stop a second task anyway; this stops it
    before it reads anything.
  * THE TIMEOUT, OURS. Step 1 runs under coreutils `timeout` with this file's
    1800-second deadline, whatever timeout the execution was given.

What survives from the execution is one value: `CLOUD_RUN_EXECUTION`, checked
against register-tenant.sh's `WS_BUILD_RE` and passed on as `BUILD_ID`, which
the script uses for nothing but the record and the log. The project and the
region come from the metadata server, never from the environment, and the
metadata request goes direct: no proxy, whatever was set.

The standard library only: this runs on the image's Debian python3, before
anything is installed or trusted. Nothing here may print a credential: it
handles none.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Callable, MutableMapping
from pathlib import Path

#: Where the image puts the code, read-only and root-owned
#: (images/workspace-apply/Dockerfile).
SWARM_ROOT = Path("/opt/swarm")
#: Steps 1 to 3: validate, install the guard, run register-tenant.sh.
APPLY_SCRIPT = SWARM_ROOT / "scripts" / "workspace-apply.sh"

#: Absolute, so nothing is looked up on a PATH before the fixed one exists.
BASH = "/bin/bash"
TIMEOUT = "/usr/bin/timeout"

#: The run's own deadline (§2.1: "a task timeout of 1800 seconds, as the build
#: had"). An override may lengthen the execution's timeout; it cannot lengthen
#: this. The grace after it is what `timeout` gives the script's process group
#: to exit on SIGTERM before SIGKILL.
DEADLINE_SECONDS = 1800
KILL_AFTER_SECONDS = 30

#: The image's own tool locations: Debian's, /usr/local/bin for kubectl and uv,
#: and the SDK's bin for anything the gke-gcloud-auth-plugin .deb put only
#: there. The Dockerfile's self-test resolves every tool the job needs on
#: exactly this value, so a tool that moves fails the image build.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/google-cloud-sdk/bin"
#: The real kubectl the guard runs after an allow (workspace-guard.sh).
KUBECTL = "/usr/local/bin/kubectl"
#: The Dockerfile removes gcloud's bundled interpreter and names this one, by
#: ENV; that ENV is discarded with the rest, so it is restated here as a fixed
#: value.
CLOUDSDK_PYTHON = "/usr/bin/python3"

#: §2.2 step 1. register-tenant.sh's WS_ID_RE, without its anchors.
WORKSPACE_RE = re.compile(r"w-[0-9a-f]{6}", re.ASCII)
MODES = ("create", "limits")
#: register-tenant.sh's WS_BUILD_RE, verbatim; a unit test holds the two equal.
BUILD_RE = "^[A-Za-z0-9._:-]{1,80}$"
#: A project id as Google defines it, and a region's last path segment.
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", re.ASCII)
REGION_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+", re.ASCII)

METADATA = "http://metadata.google.internal/computeMetadata/v1/"
METADATA_TIMEOUT_SECONDS = 5


class Refused(Exception):
    """The execution ends here, having run nothing."""


def read_metadata(path: str) -> str:
    """One value from the metadata server, with no proxy whatever was set."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(METADATA + path, headers={"Metadata-Flavor": "Google"})
    with opener.open(request, timeout=METADATA_TIMEOUT_SECONDS) as response:
        return response.read(256).decode("ascii", "replace").strip()


def plan(
    argv: list[str],
    environ: MutableMapping[str, str],
    *,
    metadata: Callable[[str], str] = read_metadata,
    scratch_root: str = "/tmp",
    target: Path = APPLY_SCRIPT,
) -> tuple[str, list[str], dict[str, str]]:
    """The program, its arguments and its environment, or Refused.

    `environ` is EMPTIED first, whatever happens after: given `os.environ`, that
    is this process's own environment, so nothing later in it reads a value a
    caller chose.
    """
    execution = environ.get("CLOUD_RUN_EXECUTION")
    task_count = environ.get("CLOUD_RUN_TASK_COUNT")
    task_index = environ.get("CLOUD_RUN_TASK_INDEX")
    environ.clear()

    if len(argv) != 2:
        raise Refused(f"the job takes exactly two arguments, <w-id> <mode>; it was given {len(argv)}")
    workspace_id, mode = argv
    if not WORKSPACE_RE.fullmatch(workspace_id):
        raise Refused("the first argument is not a workspace id of the form w-<6 hex digits>")
    if mode not in MODES:
        raise Refused("the second argument is neither create nor limits")
    if task_count != "1":
        raise Refused("the execution has a task count other than one")
    if task_index != "0":
        raise Refused("this is not the execution's first task")
    if execution is None or not re.fullmatch(BUILD_RE, execution):
        raise Refused("CLOUD_RUN_EXECUTION is missing or is not an execution name")

    try:
        project = metadata("project/project-id")
        region = metadata("instance/region").rsplit("/", 1)[-1]
    except OSError as exc:
        raise Refused(f"the metadata server did not answer ({type(exc).__name__})") from None
    if not PROJECT_RE.fullmatch(project):
        raise Refused("the metadata server's project id is not one")
    if not REGION_RE.fullmatch(region):
        raise Refused("the metadata server's region is not one")

    # Private to this run, in the execution's in-memory filesystem and outside
    # /opt/swarm: gcloud's config, the job's kubeconfig and the guard's
    # expectation file (§2.2 A1) all live under it.
    work = Path(tempfile.mkdtemp(prefix="swarm-workspace-apply.", dir=scratch_root))
    home = work / "home"
    guard = work / "guard"
    home.mkdir(mode=0o700)
    guard.mkdir(mode=0o700)

    env = {
        "PATH": SAFE_PATH,
        "HOME": str(home),
        "PROJECT_ID": project,
        "REGION": region,
        # load_env (scripts/lib/common.sh) sources this if it exists. It never
        # does: nothing writes to it.
        "SWARM_ENV_FILE": str(work / "no-env-file"),
        "SWARM_CALL_GUARD": str(guard / "expect.json"),
        # A report-only request is ignored while this is set (workspace-guard.sh):
        # the deployer identity never runs unguarded.
        "SWARM_CALL_GUARD_ENFORCE": "1",
        "SWARM_KUBECTL": KUBECTL,
        "CLOUDSDK_PYTHON": CLOUDSDK_PYTHON,
        "BUILD_ID": execution,
        "NO_COLOR": "1",
    }
    args = [
        TIMEOUT,
        f"--kill-after={KILL_AFTER_SECONDS}s",
        f"{DEADLINE_SECONDS}s",
        BASH,
        str(target),
        workspace_id,
        mode,
    ]
    return TIMEOUT, args, env


def main(
    argv: list[str] | None = None,
    environ: MutableMapping[str, str] | None = None,
    *,
    metadata: Callable[[str], str] = read_metadata,
    scratch_root: str = "/tmp",
    target: Path = APPLY_SCRIPT,
    execve: Callable[[str, list[str], dict[str, str]], object] = os.execve,
) -> int:
    try:
        program, args, env = plan(
            sys.argv[1:] if argv is None else argv,
            os.environ if environ is None else environ,
            metadata=metadata,
            scratch_root=scratch_root,
            target=target,
        )
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr, flush=True)
        return 1
    # The workspace id and the execution only (§2.6): both are opaque.
    print(f"workspace {args[-2]}: mode {args[-1]}, execution {env['BUILD_ID']}", flush=True)
    execve(program, args, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
