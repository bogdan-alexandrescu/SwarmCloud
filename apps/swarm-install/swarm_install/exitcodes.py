"""The installer's exit codes (docs/design/day-one-install.md §2.1).

They follow `sc` (`apps/swarm-mcp/swarm_mcp/sc.py`: 0 ok, 1 fail, 2 usage,
3 trouble) and add two:

| Code | Meaning |
|---|---|
| 0 | every step in scope is `met` and verified |
| 1 | a step failed; the output names it and how to continue |
| 2 | usage error |
| 3 | `doctor`/`status`: something is not met, OR could not be checked |
| 4 | paused on a human step or a wait past its bound; `swarm install` resumes |
| 5 | refused: a typed confirmation was missing, a downgrade was not allowed, |
|   | or a deny-listed resource was named |

The one rule this module exists to hold: **an `unknown` is never 0.** A probe
that could not run (a 403, a timeout, a missing tool) is not evidence that the
dependency is fine, so anything this module does not recognise maps to 3.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    FAIL = 1
    USAGE = 2
    TROUBLE = 3
    PAUSED = 4
    REFUSED = 5


#: A dependency's observed status (design §2.2).
MET = "met"
UNMET = "unmet"
UNKNOWN = "unknown"
WAITING = "waiting"
STATUSES = (MET, UNMET, UNKNOWN, WAITING)

#: How a run of the engine ended, and the code each ending exits with. `ok` is
#: the only ending that exits 0.
OUTCOMES: dict[str, ExitCode] = {
    "ok": ExitCode.OK,
    "failed": ExitCode.FAIL,
    "blocked": ExitCode.FAIL,
    "usage": ExitCode.USAGE,
    "not_met": ExitCode.TROUBLE,
    "unknown": ExitCode.TROUBLE,
    "paused": ExitCode.PAUSED,
    "refused": ExitCode.REFUSED,
}


def for_outcome(outcome: str) -> ExitCode:
    """The code for an engine outcome; an outcome nobody listed is trouble."""
    return OUTCOMES.get(outcome, ExitCode.TROUBLE)


def for_statuses(statuses: Iterable[str]) -> ExitCode:
    """The code `doctor`/`status` exit with for a set of observed statuses.

    0 only when there is at least one status and every one is `met`: an empty
    set means nothing was checked, and "nothing checked" is not "fine".
    """
    seen = list(statuses)
    if seen and all(s == MET for s in seen):
        return ExitCode.OK
    return ExitCode.TROUBLE
