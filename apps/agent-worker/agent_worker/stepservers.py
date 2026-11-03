"""Which of the platform's own MCP servers a step's agent is handed (ONE-PLACE).

WHY. The owner asked on 3 November 2026 for prompts without bloat. Measured in
experiment batches one and two: swarm-git and swarm-state, 22 tools, were
registered in 91 steps and never called, and every registered server costs its
tool names and its instructions in every turn of the session.

WHO DECIDES. The worker, per step, from what it holds signed: the step's
`step_id`, its `metadata.expected_outputs` and its dispatch block (owner's pick
on PR #3113, `explicit-list`, over reading the brief for the servers' names).
The list reaches the runner as `$SWARM_STEP_MCP_SERVERS`, set by the worker and
never from `input` (invariant 10); `runners.claude_code` registers swarm-git
and swarm-state only when the list names them. Unset -- a worker that predates
the variable -- the runner registers every server the attempt was offered, as
before. Either way the spools stay exported, so the `swarm-git` CLI in the
agent's Bash tool and the runner's pre-publish rebase are unaffected.

WHICH STEPS. Those that read the forge or the platform's state as part of
their job:

  * a review or re-review (`expected_outputs.is_review_step`, or a step whose
    dependants stage its `verdict.json`): the pull request's checks and the
    runs it judges -- swarm-git and swarm-state;
  * a CI fix (the signed `ci_fix_head`): the red run's logs -- both;
  * a step that publishes onto an existing pull request (`publish_to_pr`): a
    pick fix reads the question it answers, a fix round the pull request --
    both;
  * a `single-pr` reader or amender, and a merge step's conflict round: the
    pull request and the default branch -- swarm-git;
  * a supervisor (`step_id` whose last word is `supervisor`): its sibling
    agents -- swarm-state.

A build step is none of these and gets neither. The live browser's screen
review pushes the branch first, so the runner adds swarm-git on a live-browser
step whatever this list says (`runners.claude_code`).

Kept out of lifecycle.py on purpose; lifecycle calls `step_servers` once, in
`Worker._build_child_env`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import cifixstart
from . import expected_outputs as expected_mod
from .gitmcp.server import SERVER_NAME as GIT_SERVER
from .statemcp.server import SERVER_NAME as STATE_SERVER

#: The variable the worker sets on every runner start; platform-set, never
#: from `input`.
ENV = "SWARM_STEP_MCP_SERVERS"

#: The value for a step that is handed none of them. Not "": an empty value
#: and an absent one must not read alike, and "none" is no server's name.
NONE = "none"

#: The servers this list governs. Every other server (the live browser,
#: search, the graph, Jev, Lightpanda, a tenant's own) keeps its own rule.
GOVERNED = (GIT_SERVER, STATE_SERVER)

#: The dispatch block's mark of a merge step's conflict fix round, as
#: `lifecycle.CONFLICT_ROUND_FIELD` restates `swarm_api.validation`'s.
CONFLICT_ROUND_FIELD = "conflict_round"

_WORD_BREAK = re.compile(r"[-_.\s]+")


def _last_word(step_id: Any) -> str:
    if not isinstance(step_id, str):
        return ""
    words = [w for w in _WORD_BREAK.split(step_id.strip().lower()) if w]
    while words and words[-1].isdigit():
        words.pop()
    return words[-1] if words else ""


def step_servers(
    *,
    step_id: Any,
    metadata: Any,
    dispatch: Mapping[str, Any],
    pr_role: str,
    onto_pull_request: bool,
) -> tuple[str, ...]:
    """The governed servers this step's agent is handed, in GOVERNED order."""
    review = expected_mod.REVIEW_VERDICT_NAME in expected_mod.declared_outputs(
        metadata, step_id=step_id).names
    ci_fix = isinstance(dispatch.get(cifixstart.FIELD), Mapping)
    conflict = dispatch.get(CONFLICT_ROUND_FIELD) is True
    supervisor = _last_word(step_id) == "supervisor"
    git = review or ci_fix or onto_pull_request or conflict or pr_role in ("reader", "amender")
    state = review or ci_fix or onto_pull_request or supervisor
    return tuple(name for name, on in ((GIT_SERVER, git), (STATE_SERVER, state)) if on)


def render(servers: Iterable[str]) -> str:
    """The variable's value: the names, comma-separated, or NONE."""
    return ",".join(servers) or NONE


def parse(environ: Mapping[str, str]) -> frozenset[str] | None:
    """The servers the worker named, or None when it named none at all (the
    variable is unset: a worker that predates it)."""
    raw = environ.get(ENV)
    if raw is None:
        return None
    return frozenset(n.strip() for n in raw.split(",") if n.strip() and n.strip() != NONE)
