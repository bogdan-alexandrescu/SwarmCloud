"""The worker's memory is closed to the agent it runs.

THE GAP THIS CLOSES. The worker reads the tenant's git token at the clone and
keeps it for the rest of the attempt: the publish path needs it again at the
end, and `secrets.resolve_git_token` registers it with the logger so every line
can be scrubbed of it. The agent runs for the whole of that time, next to the
worker, as the same uid. Where the kernel lets one process of a uid ptrace
another, or open another's `/proc/<pid>/mem`, the agent could read the token
out of the worker's heap while it runs, long before the pre-publish reap
(`procman.reap_foreign_processes`) kills anything.

THE FIX. `prctl(PR_SET_DUMPABLE, 0)` marks the worker non-dumpable. Linux then
refuses to let another process ptrace-attach to it, or open its
`/proc/<pid>/mem` or `environ`, unless that process holds CAP_SYS_PTRACE over
the worker; being the same uid is no longer enough. The pod drops every
capability, so the agent has none. The entrypoint calls this once, straight
after its first log line and before the configuration, any client or any
credential is read (`__main__.main`).

WHAT IT LEAVES AS IT WAS, checked against what the worker does afterwards:

* **The children.** The kernel makes a process dumpable again when it execs a
  program without changing uid, so the runner, the agent and git run exactly
  as before. An agent that debugs its own processes still can. The worker has
  no child that forks without exec'ing.
* **The worker's own reads of /proc.** The files inside a non-dumpable
  process's `/proc/<pid>` become root-owned. Its `/proc/<pid>` directory
  keeps the process's owner. The worker reads the world-readable `status`,
  `stat` and `statm` of OTHER processes (`procman._live_foreign_pids`,
  `metrics`), and none of its own. Those processes are the runner, the
  agent and whatever they started.
* **The SIGTERM stack dump.** `faulthandler` writes from inside the process.
* **Signals**, including the reap's `kill(-1)`, which dumpability does not
  touch.

WHAT IT COSTS. The worker leaves no core dump. A debugger (py-spy, gdb) cannot
attach to it from a process without CAP_SYS_PTRACE, which in these pods is
every process. The SIGTERM stack dump (`startup.route_signals`) is what is
left for a hung worker, and it is the tool the startup incidents were
diagnosed with.

WHAT IT DOES NOT COVER. The container's environment is also PID 1's (tini's),
at `/proc/1/environ`, and this changes nothing there. The worker keeps no
credential in its environment for that reason (kubernetes/README.md, "No
provider key in a Job's environment").

IF IT FAILS. On Linux a failed call means the agent may be able to read the
worker's memory. The worker then never reads the tenant's git token: the clone
runs without it, and the publish is refused with the reason
(`MemoryProtection.git_token_refusal`). On any other platform `prctl` does not
exist. That is a developer's machine, so the entrypoint logs a warning and
carries on.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

#: `<linux/prctl.h>`. Stable kernel ABI since 2.3.20.
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4

#: The call succeeded and the kernel reads the process back as non-dumpable.
PROTECTED = "protected"
#: This platform has no prctl. The worker is not running on Linux, so it is not
#: the production worker; it warns and continues.
UNSUPPORTED = "unsupported"
#: On Linux, and the process could not be made non-dumpable. It holds no git token.
FAILED = "failed"


@dataclass(frozen=True)
class MemoryProtection:
    """What `make_non_dumpable` established at startup."""

    status: str
    detail: str

    @property
    def git_token_refusal(self) -> str | None:
        """Why this worker must not hold the tenant's git token, or None.

        Only FAILED refuses. PROTECTED is the property holding. UNSUPPORTED is
        a platform with no prctl at all, which is not where tenant tokens are
        served, and refusing there would only stop a developer testing the
        publish path locally.
        """
        if self.status == FAILED:
            return (
                "the worker could not make its memory unreadable to the agent "
                f"(prctl PR_SET_DUMPABLE: {self.detail}), so it holds no tenant git token"
            )
        return None


def _libc_prctl() -> Callable[[int, int], int]:
    """libc's `prctl`, as `prctl(option, arg2) -> int`, raising OSError on -1.

    `CDLL(None)` is the process's own symbol table, where the dynamically
    linked interpreter already has libc. Nothing is searched for on disk and
    no subprocess is started (`ctypes.util.find_library` runs ldconfig).
    Imported here rather than at module level so that importing this module
    costs nothing on a platform that will never call it.
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    raw = libc.prctl
    raw.restype = ctypes.c_int
    raw.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)

    def prctl(option: int, arg2: int) -> int:
        rc = raw(option, arg2, 0, 0, 0)
        if rc == -1:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return rc

    return prctl


def make_non_dumpable(
    *,
    platform: str | None = None,
    prctl: Callable[[int, int], int] | None = None,
) -> MemoryProtection:
    """Mark this process non-dumpable, then read the flag back from the kernel.

    Never raises: the entrypoint calls it before anything else can report a
    failure, and a crash here would be a worker that never starts over a
    hardening step. A failure comes back as FAILED instead, and the lifecycle
    turns it into a refusal to hold the git token.

    The read-back is part of the check. A call that returned 0 and left the
    process dumpable would otherwise be recorded as a success.

    `platform` and `prctl` are injectable so a unit test can exercise every
    branch without changing the test process's own dumpability.
    """
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return MemoryProtection(
            UNSUPPORTED,
            f"prctl does not exist on {platform}; another process of this uid may be "
            "able to read this worker's memory",
        )
    try:
        call = prctl if prctl is not None else _libc_prctl()
        call(PR_SET_DUMPABLE, 0)
        now = call(PR_GET_DUMPABLE, 0)
    except Exception as exc:  # noqa: BLE001 - see the docstring: never raise
        return MemoryProtection(FAILED, f"{type(exc).__name__}: {exc}")
    if now != 0:
        return MemoryProtection(
            FAILED, f"the kernel still reads the process as dumpable ({now}) after the call"
        )
    return MemoryProtection(PROTECTED, "PR_SET_DUMPABLE 0; PR_GET_DUMPABLE reads 0")
