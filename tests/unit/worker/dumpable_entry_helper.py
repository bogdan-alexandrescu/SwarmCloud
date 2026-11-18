"""Two processes for `test_worker_memory_protection.py`: the worker, and a reader.

    python dumpable_entry_helper.py entry <report-file> [--without-prctl]
    python dumpable_entry_helper.py read <pid> <canary>

`entry` runs the REAL worker entrypoint, `agent_worker.__main__.main`, in this
process. One thing is replaced: `Settings.from_env`, the entrypoint's
configuration step. The configuration is the first step that can lead to a
credential. The stand-in writes `{"pid", "dumpable"}` to the report file and
then waits to be killed. `dumpable` is what the kernel says, asked with this
file's own `prctl(PR_GET_DUMPABLE)` rather than the worker's code, from inside
the worker process at the moment it reaches its configuration.

`--without-prctl` is the control. The entrypoint's call is replaced by one that
does nothing, so the same process stays dumpable. It shows that the reader can
read the process when nothing protects it. So a refusal in the real case comes
from the worker's protection, and not from this host.

`read` is a second process. It runs as the same uid, and it is not an ancestor
of the entry. It tries the two reads the agent would try and prints one JSON
line: `{"environ": ..., "mem": ...}`. Each value is "found" when the canary was
read out of the entry process, "not-found" when the read worked but the canary
was not there, or "denied:<exception>" when the kernel refused.
`read_process` is also imported by the test, which runs it from the entry's
parent. Ptrace policy may treat a parent differently from a sibling.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: `<linux/prctl.h>`, restated here on purpose: this file measures the worker
#: and must not borrow the worker's own constant to do it.
_PR_GET_DUMPABLE = 3

#: The environment variable the test puts the canary in. The canary is also
#: kept in this process's heap (`_HELD`), where the token would be.
CANARY_ENV = "SWARM_MEMORY_CANARY"

#: The largest mapping `read_process` will read. Big enough for the heap and
#: the stack of a Python process that has imported the worker. Anything larger
#: is a reserved region with nothing in it.
_MAX_REGION_BYTES = 256 * 1024 * 1024

_HELD: list[str] = []


def _kernel_dumpable() -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return int(libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0))


def _entry(report: Path, *, without_prctl: bool) -> int:
    # A fresh string object, so the canary is in the heap as well as in the
    # environment block, as a token read from Secret Manager would be.
    _HELD.append("".join(list(os.environ.get(CANARY_ENV, ""))))

    import agent_worker.__main__ as entrypoint

    if without_prctl:
        try:
            from agent_worker import hardening
        except ImportError:  # the code before the fix has nothing to disable
            hardening = None
        if hardening is not None:
            hardening.make_non_dumpable = lambda **_kw: hardening.MemoryProtection(
                hardening.UNSUPPORTED, "the test's control: prctl was not called"
            )

    def stop_at_configuration(cls):  # noqa: ANN001 - stands in for a classmethod
        tmp = report.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "dumpable": _kernel_dumpable()}))
        tmp.replace(report)
        while True:  # the test kills this process when it has finished reading it
            time.sleep(0.1)

    entrypoint.Settings.from_env = classmethod(stop_at_configuration)
    return entrypoint.main()


def _scan_memory(pid: int, needle: bytes) -> bool:
    """Whether `needle` is anywhere in `pid`'s readable memory.

    The kernel's access check is in the `open` of `/proc/<pid>/mem`. An
    unreadable process raises PermissionError there, before a byte is read.
    """
    fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as fh:
            maps = fh.read().splitlines()
        for line in maps:
            fields = line.split()
            if len(fields) < 2 or not fields[1].startswith("r"):
                continue
            start, end = (int(part, 16) for part in fields[0].split("-"))
            if end - start > _MAX_REGION_BYTES:
                continue
            try:
                chunk = os.pread(fd, end - start, start)
            except (OSError, OverflowError, ValueError):
                continue  # [vvar] and similar mappings cannot be read this way
            if needle in chunk:
                return True
        return False
    finally:
        os.close(fd)


def read_process(pid: int, canary: str) -> dict[str, str]:
    needle = canary.encode()
    out: dict[str, str] = {}
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
        out["environ"] = "found" if needle in data else "not-found"
    except OSError as exc:
        out["environ"] = f"denied:{type(exc).__name__}"
    try:
        out["mem"] = "found" if _scan_memory(pid, needle) else "not-found"
    except OSError as exc:
        out["mem"] = f"denied:{type(exc).__name__}"
    return out


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "entry":
        return _entry(Path(argv[1]), without_prctl="--without-prctl" in argv[2:])
    if len(argv) == 3 and argv[0] == "read":
        print(json.dumps(read_process(int(argv[1]), argv[2])), flush=True)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
