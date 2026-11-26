#!/usr/bin/env python3
"""The runner git tools' acceptance: archived issue 1225's five checks on a deployed environment (G7).

docs/runner-git-tools.md §7, lane G7, and docs/acceptance.md, "git-tools".
`scripts/acceptance/groups/git-tools.sh` submits the tasks, waits on them with
the suite's own `acc_settle`, and saves what each produced into one directory;
this file writes the prompts that group sends and JUDGES that directory:

  rebase        a lane (`rebase`) continues another task's open pull request
                (`lane`), whose branch was cut from a commit before main added
                tests/acceptance/fixtures/git-tools/planted.txt and adds its
                own planted.txt, so the two conflict. It fetches its branch,
                `git_rebase_onto main` stops on planted.txt, the agent
                resolves it, `git_push_branch` replaces the branch under the
                lease swarm-api recorded (Q1 = A, audited `rewrite: true`),
                and `pr_checks` reads the pull request at the pushed head. Its
                environment carries no forge credential (judge_env).
  read_private  a reader reads an allowlisted repository's file at a branch,
                then at the sha that read resolved to; a repository not on
                the list is refused `repo_not_allowlisted` (the control).
  push_main     the reader's push to `main` is refused, and main's tip, as
                swarm-api fetched it before and after, did not move.
  push_other    the reader's push to the lane's branch -- another task's -- is
                refused, and that branch moved only by its own lane's pushes.
  push_fails    an `integrate` contributor (the build step a review builds on)
                pushes its branch mid-task, then makes a commit that does not
                descend from it, so its final push cannot fast-forward and is
                refused (never forced). It ends FAILED with `publish_failed:`
                (G1), not SUCCEEDED with no branch.

WHAT IS EVIDENCE. Every platform fact is read from swarm-api's audit of the
task's tool calls (`GET /v1/tasks/{id}/git-calls`, docs/runner-git-tools.md
§4) and the task document -- not from what the agent said. Two things only
the agent sees are read from its artifacts: the refusals the WORKER answers
before any request leaves (§1.2: a push to a branch the task may not push is
refused there, so it never reaches the audit), and the conflicts a rebase
listed. Each is cross-checked against the audit: no push of the reader's
ever reached the forge, and the branch it aimed at did not move.

The pull request's content on GitHub -- that its head now contains main and
planted.txt holds the resolution -- is read where a sandbox token is:
scripts/acceptance/github-verify.sh, after the suite, through
`expected-planted` and `pr_kind` below.

STANDARD LIBRARY ONLY: it runs in the swarm-verify image (alpine, python3
with no pip). No function here is named `test_*`: this is a program the
suite runs, not a pytest module (pyproject's testpaths is tests/unit; the
unit tests are tests/unit/scripts/test_runner_git_tools_acceptance.py).

    test_runner_git_tools.py prompt <lane|rebase|reader|build|integrate> --run-id ID [...]
    test_runner_git_tools.py expected-planted
    test_runner_git_tools.py judge --dir DIR --results FILE

The results file gets one JSON line per check, in CHECKS order:
`{"check": key, "verdict": "PASS"|"FAIL"|"SKIP", "reason": "..."}`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from typing import Any, Iterable

#: The checks, in the order git-tools.sh lists them.
CHECKS = ("rebase", "read_private", "push_main", "push_other", "push_fails")

#: The roles git-tools.sh saves a task under, one directory each.
ROLES = ("lane", "rebase", "reader", "build")

FIXTURE_DIR = "tests/acceptance/fixtures/git-tools"
PLANTED = f"{FIXTURE_DIR}/planted.txt"
LANE_FILE = f"{FIXTURE_DIR}/lane.txt"
BUILD_FILE = f"{FIXTURE_DIR}/build.txt"
ENV_REPORT = f"{FIXTURE_DIR}/env_report.py"
#: The line main's planted.txt holds (this repository's fixture, which
#: sandbox-sync.sh puts on the sandbox's main). A unit test holds it equal.
MAIN_LINE = "main: the default branch wrote this line"
#: The line the lane writes into ITS planted.txt, on a base without the file.
LANE_LINE = "lane: the pull request wrote this line"
#: The sandbox branch sandbox-sync.sh points at the commit before main first
#: added planted.txt. Outside `swarm/`, so no task can push it.
BASE_BRANCH = "acceptance/git-tools-base"
#: The PR body line github-verify.sh recognises the lane's pull request by.
PR_MARKER = "acceptance-git-tools: rebase"
#: The line the build step's integrator writes into its pull request's body,
#: so github-verify.sh tells that pull request from the claude-code group's
#: integrate one (`pr_kind`).
INTEGRATE_MARKER = "acceptance-git-tools: integrate"
#: The platform's first line of an integrate pull request's body.
_INTEGRATES = re.compile(r"^Integrates [0-9]+ contributor branch", re.MULTILINE)
#: The repository the reader's control read names: on no tenant's list, and
#: on no forge (§2.3 gives both one answer). Suffixed with the run id.
UNLISTED_PREFIX = "swarm-acceptance/not-allowlisted-"
#: The file the agents append each tool answer to, as `{"step", "answer"}`.
ANSWERS = "git-answers.jsonl"
ENV_REPORT_ARTIFACT = "env-report.json"

#: What the worker answers a push to a branch this task may not push
#: (`agent_worker.gittools.branch_refusal`). `protected_branch` when the
#: worker knows the default branch, else `not_this_tasks_branch`; swarm-api
#: answers `protected_branch` for main (§1.2). Either is a refusal of main.
MAIN_REFUSALS = ("protected_branch", "not_this_tasks_branch")
OTHER_REFUSALS = ("not_this_tasks_branch",)

#: The owner's narrowing of `env | grep -i token` (2026-10-13 ~21:40 PDT):
#: the Claude CLI's own login is expected.
ENV_ALLOWED = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
#: Names that carry a forge credential to git or gh, whatever their value.
ENV_FORBIDDEN = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_PAT", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PASSWORD", "GIT_TOKEN",
    "GIT_CREDENTIALS", "GIT_HTTP_EXTRAHEADER",
})
#: A GIT_* name with one of these in it is a credential variable. The
#: dispatcher's GIT_AUTHOR_* and GIT_COMMITTER_* are an identity, not one.
_GIT_CREDENTIAL_PART = re.compile(r"PASS|CRED|TOKEN|AUTH(?!OR)|HEADER|SECRET")
#: A git config key, set through GIT_CONFIG_KEY_<n>, that hands git a credential.
_CREDENTIAL_CONFIG_KEY = re.compile(r"^(credential\b|http\..*extraheader$|.*\.insteadof$)",
                                    re.IGNORECASE)

#: The `step` of the lines the WORKER appends to the agent's answers file
#: (`agent_worker.gittools.ANSWERS_STEP`, restated; a unit test holds them
#: equal): every refusal its relay answered, with the tool but no arguments.
WORKER_STEP = "worker"
#: `Role.recorded`'s answer for a step the agent recorded nothing for.
_NOTHING = object()

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")
_SHA = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------
# What the tasks are asked
# --------------------------------------------------------------------------

#: How the agents reach the tools: the CLI form of the swarm-git shim, run in
#: their Bash tool. NOT the MCP server: since archived pull request 3113 the
#: worker hands swarm-git's MCP server only to the steps on
#: `agent_worker.stepservers`' list (a review, a CI fix, a step publishing onto
#: a pull request, a single-pr reader or amender, a conflict round), and none
#: of this group's agents -- a `continues` lane, a `collect` reader with
#: `carrier: branches`, an `integrate` contributor -- is on it. Asked for "the
#: swarm-git MCP tools" they had none, made no call and recorded nothing, so
#: every check FAILED on 2026-11-21. The worker still exports the spool
#: ($SWARM_GIT) to every attempt it offers the tools, and the CLI writes to it:
#: the same relay, the same swarm-api route, the same audit.
#:
#: THE VENV'S INTERPRETER BY ITS FULL PATH, NOT `python3`. The claude-code
#: runner gives its agent `cliagent.BASE_PATH`, which starts at
#: /usr/local/bin and puts /opt/venv/bin first only on the browser image, so
#: on agent-runtime-base `python3` is the image's bare interpreter, which has
#: no `agent_worker`: every command answered "No module named agent_worker",
#: nothing reached the relay, and the run on 2026-11-21 read as "answered with
#: no refusal" and "0 fetch(es)". `swarm_rollup.agentimage.VENV_PYTHON`,
#: restated (this file is standard library only; a unit test holds them
#: equal). `-I`, as `children.git_shim_argv`: the checkout the agent runs in
#: is never on `sys.path`, so no module it wrote there is what runs.
VENV_PYTHON = "/opt/venv/bin/python"
GIT_CLI = f"{VENV_PYTHON} -I -m agent_worker.gitmcp"


def git_cli(tool: str, **arguments: str) -> str:
    """The shell command for one tool call, as the agent is asked to run it.

    `agent_worker.gitmcp.__main__` takes `--ref`, `--branch`, `--repo`,
    `--path` and `--pr`; a unit test holds every command a prompt carries to
    that vocabulary and to the shim's tool names.
    """
    words = [GIT_CLI, tool]
    for name, value in arguments.items():
        words += [f"--{name}", shlex.quote(value)]
    return " ".join(words)


_RECORD = (
    "After EVERY such command, append one line of JSON to "
    "$SWARM_ARTIFACTS_DIR/" + ANSWERS + ": {\"step\": \"<the step name given>\", "
    "\"answer\": <the whole JSON object the command printed>}. "
    "A refused call is an answer too (the command exits 1): record it and go on to the "
    "next step. "
)
_TOOLS = (
    "The platform's git tools (git_fetch, git_rebase_onto, git_push_branch, read_private, "
    f"pr_checks) are run from your Bash tool as `{GIT_CLI} <tool> [--ref R] [--branch B] "
    "[--repo R] [--path P]`, each as its own Bash command with a timeout of 600000 ms; "
    "the command prints the tool's answer as one JSON object. Run each command exactly as "
    "given below. This container holds no git credential: never run git fetch, git pull "
    "or git push yourself. "
)


def expected_planted() -> str:
    """planted.txt as the rebase lane resolves it: main's line, then the lane's."""
    return f"{MAIN_LINE}\n{LANE_LINE}\n"


def prompt(name: str, *, run_id: str, lane_branch: str = "", read_repo: str = "",
           read_path: str = "", read_ref: str = "") -> str:
    """The prompt each task of the group is sent. Steps are spelled out
    because each is what a check reads; every check costs subscription quota,
    so nothing else is asked."""
    if name == "lane":
        return (
            f"Create the file {PLANTED} containing exactly the one line: {LANE_LINE} -- "
            f"and the file {LANE_FILE} containing exactly the one line: acceptance-run:{run_id}. "
            "Change no other file. Write a one-line pull request title saying this adds the "
            "git-tools acceptance lane's files, with no task id, to $SWARM_ARTIFACTS_DIR/pr-title.txt, "
            f"and write exactly two lines to $SWARM_ARTIFACTS_DIR/pr-body.md: acceptance-run:{run_id} "
            f"and {PR_MARKER}. Do not run anything else and do not commit."
        )
    if name == "rebase":
        return (
            _TOOLS + _RECORD
            + f"1) Run {git_cli('git_fetch', ref=lane_branch)} (step fetch-own). "
            f"2) Run {git_cli('git_rebase_onto', ref='main')} (step rebase). It stops on a "
            "conflict. "
            f"3) Resolve it: write {PLANTED} with exactly these two lines, in this order: "
            f"{MAIN_LINE} -- then: {LANE_LINE}. For every OTHER conflicted path keep main's side "
            "(git checkout --ours -- <path>). git add the resolved paths and run "
            "GIT_EDITOR=true git rebase --continue; repeat until the rebase has finished. "
            f"4) Run: python3 {ENV_REPORT} > $SWARM_ARTIFACTS_DIR/{ENV_REPORT_ARTIFACT} "
            f"5) Run {git_cli('git_push_branch')} (step push). "
            f"6) Run {git_cli('pr_checks')} (step pr-checks). "
            "Change nothing else and make no other commit. Reply with one word: done."
        )
    if name == "reader":
        steps = [
            f"1) Run {git_cli('git_fetch', ref='main')} (step fetch-main-before).",
            f"2) Run {git_cli('git_push_branch', branch='main')} (step push-main).",
            f"3) Run {git_cli('git_fetch', ref='main')} (step fetch-main-after).",
            f"4) Run {git_cli('git_fetch', ref=lane_branch)} (step fetch-other-before).",
            f"5) Run {git_cli('git_push_branch', branch=lane_branch)} (step push-other).",
            f"6) Run {git_cli('git_fetch', ref=lane_branch)} (step fetch-other-after).",
        ]
        if read_repo:
            steps += [
                f"7) Run {git_cli('read_private', repo=read_repo, path=read_path, ref=read_ref)} "
                "(step read-ref).",
                f"8) Run {git_cli('read_private', repo=read_repo, path=read_path, ref='<SHA>')}, "
                "with <SHA> replaced by the resolved_sha that step 7 answered (step read-sha).",
            ]
        steps.append(
            f"{len(steps) + 1}) Run "
            f"{git_cli('read_private', repo=UNLISTED_PREFIX + run_id, path='README.md', ref='main')} "
            "(step read-unlisted)."
        )
        return (_TOOLS + _RECORD + " ".join(steps)
                + " Change no file and make no commit. Reply with one word: done.")
    if name == "build":
        return (
            _TOOLS + _RECORD
            + f"1) Create {BUILD_FILE} containing the one line: first {run_id} -- then "
            "git add it and git commit -m 'git-tools acceptance: first'. "
            f"2) Run {git_cli('git_push_branch')} (step push-first). "
            f"3) Run git reset --hard HEAD~1, then create {BUILD_FILE} containing the one line: "
            f"second {run_id} -- and git add it and git commit -m 'git-tools acceptance: second'. "
            "Change no other file. Reply with one word: done."
        )
    if name == "integrate":
        return (
            "Change no repository file. Write a one-line pull request title saying this "
            "integrates the git-tools acceptance build step, with no task id, to "
            "$SWARM_ARTIFACTS_DIR/pr-title.txt, and exactly two lines to "
            f"$SWARM_ARTIFACTS_DIR/pr-body.md: acceptance-run:{run_id} and {INTEGRATE_MARKER}."
        )
    raise ValueError(f"no prompt named {name!r}")


# --------------------------------------------------------------------------
# Reading what the group saved
# --------------------------------------------------------------------------


class Role:
    """One task's saved evidence: `<dir>/<role>/` as git-tools.sh writes it."""

    def __init__(self, base: str, role: str) -> None:
        self.dir = os.path.join(base, role)
        self.task = _read_text(os.path.join(self.dir, "task")).strip()
        #: The state acc_settle printed: a terminal state, or PARKED:... /
        #: HELD:... / UPSTREAM:... when the platform held it on purpose.
        self.state = _read_text(os.path.join(self.dir, "state")).strip()
        self.doc = _read_json(os.path.join(self.dir, "doc.json"))
        calls = _read_json(os.path.join(self.dir, "calls.json"))
        self.calls_read = isinstance(calls, dict) and isinstance(calls.get("calls"), list)
        rows = calls.get("calls") if self.calls_read else []
        #: Oldest first. The route serves newest first (§4's id order).
        self.calls: list[dict[str, Any]] = sorted(
            (r for r in rows if isinstance(r, dict)), key=lambda r: str(r.get("at") or ""))
        self.answers = read_answers(os.path.join(self.dir, ANSWERS))
        self.env = _read_json(os.path.join(self.dir, ENV_REPORT_ARTIFACT))

    @property
    def submitted(self) -> bool:
        return bool(self.task)

    def held(self) -> str | None:
        """Why the platform never ran it on purpose (a SKIP), or None."""
        if self.state.startswith(("PARKED:", "HELD:", "UPSTREAM:")):
            return self.state
        return None

    def last_error(self) -> str:
        doc = self.doc if isinstance(self.doc, dict) else {}
        return str(doc.get("last_error") or "no error")[:300]

    def answer(self, step: str) -> dict[str, Any] | None:
        """The LAST answer the agent recorded for a step (a retry supersedes)."""
        found = None
        for entry in self.answers:
            if entry.get("step") == step and isinstance(entry.get("answer"), dict):
                found = entry["answer"]
        return found

    def recorded(self, step: str) -> Any:
        """The LAST thing the agent recorded for a step, whatever its shape
        (a shell error is a string), or `_NOTHING` when it recorded none."""
        found: Any = _NOTHING
        for entry in self.answers:
            if entry.get("step") == step:
                found = entry.get("answer")
        return found

    @property
    def agent_answers(self) -> list[dict[str, Any]]:
        """The agent's own lines: the worker appends its refusals to the same
        file under `WORKER_STEP`, and those say nothing of what the agent ran."""
        return [e for e in self.answers if e.get("step") != WORKER_STEP]

    def worker_refusals(self, tool: str) -> list[str]:
        """The codes of the refusals of `tool` the worker recorded itself."""
        return [str(refusal_code(e.get("answer")))
                for e in self.answers
                if e.get("step") == WORKER_STEP and e.get("tool") == tool
                and refusal_code(e.get("answer"))]

    def rows(self, tool: str, *, result: str | None = None, **fields: Any) -> list[dict[str, Any]]:
        out = []
        for row in self.calls:
            if row.get("tool") != tool or (result is not None and row.get("result") != result):
                continue
            if all(row.get(k) == v for k, v in fields.items()):
                out.append(row)
        return out


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _read_json(path: str) -> Any:
    text = _read_text(path)
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def read_answers(path: str) -> list[dict[str, Any]]:
    """The agent's recorded answers. A line that is not a JSON object is
    skipped: the agent wrote it, and a check reading it would be reading prose."""
    out = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            if isinstance(entry.get("answer"), str):
                try:
                    entry["answer"] = json.loads(entry["answer"])
                except ValueError:
                    pass
            out.append(entry)
    return out


def refusal_code(answer: dict[str, Any] | None) -> str | None:
    """The code of a refused tool answer (`{"refused": {"code": ...}}`), or None."""
    if not isinstance(answer, dict):
        return None
    refused = answer.get("refused")
    if isinstance(refused, dict) and isinstance(refused.get("code"), str):
        return refused["code"]
    return None


def refusal_message(answer: dict[str, Any] | None, limit: int = 400) -> str | None:
    """A refused answer's sentence, on one line and cut (archived issue 1188): which read,
    which HTTP status. swarm-api scrubs it of the call's token before it
    leaves; it is a constant sentence about the forge, never a credential."""
    refused = answer.get("refused") if isinstance(answer, dict) else None
    message = refused.get("message") if isinstance(refused, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return " ".join(message.split())[:limit]


def result_of(answer: dict[str, Any] | None) -> dict[str, Any]:
    """A successful answer's result (`{"tool", "result"}`), else {}."""
    if isinstance(answer, dict) and isinstance(answer.get("result"), dict):
        return answer["result"]
    return {}


def pull_request_number(doc: Any) -> int | None:
    git = ((doc or {}).get("result_summary") or {}).get("git") if isinstance(doc, dict) else None
    pull = git.get("pull_request") if isinstance(git, dict) else None
    number = pull.get("number") if isinstance(pull, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) else None


# --------------------------------------------------------------------------
# The environment (check 1's last clause)
# --------------------------------------------------------------------------


def judge_env(report: Any) -> list[str]:
    """What in the runner's environment is a forge credential; [] when none.

    `report` is env_report.py's output: names, GIT_CONFIG_KEY_<n> keys, and
    the names whose value has a forge credential's shape. An unreadable report
    is a finding, never a clean environment.
    """
    if not isinstance(report, dict) or not isinstance(report.get("names"), list):
        return ["the environment report is missing or unreadable, so the environment is not known clean"]
    names = [n for n in report["names"] if isinstance(n, str)]
    if not names:
        return ["the environment report lists no variable at all, so it did not read the runner's"]
    found = []
    for name in names:
        upper = name.upper()
        if name in ENV_ALLOWED:
            continue
        if "TOKEN" in upper:
            found.append(f"{name} (a name `env | grep -i token` finds)")
        elif upper in ENV_FORBIDDEN:
            found.append(f"{name} (hands git or gh a credential)")
        elif upper.startswith("GIT_") and not upper.startswith("GIT_CONFIG_") \
                and _GIT_CREDENTIAL_PART.search(upper[4:]):
            found.append(f"{name} (a GIT_* credential variable)")
    keys = report.get("git_config_keys")
    for name, key in sorted((keys or {}).items() if isinstance(keys, dict) else []):
        if isinstance(key, str) and _CREDENTIAL_CONFIG_KEY.match(key.strip()):
            found.append(f"{name}={key} (a git config key that supplies a credential)")
    for name in report.get("forge_shaped") or []:
        found.append(f"{name}'s value has a forge credential's shape")
    return found


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


Verdict = tuple[str, str]


def _not_run(role: Role, what: str) -> Verdict | None:
    """The verdict when `role` did not run to an end, or None when it did."""
    if not role.submitted:
        return "FAIL", f"the {what} task was not created (see the submission above)"
    held = role.held()
    if held:
        return "SKIP", f"not measured: the platform held the {what} task on purpose ({held})"
    if role.state not in TERMINAL:
        return "FAIL", f"the {what} task did not finish (last state {role.state or 'unknown'})"
    if not role.calls_read:
        return "FAIL", f"the {what} task's git-calls audit could not be read"
    return None


def _is_tool_answer(answer: Any) -> bool:
    """Whether `answer` is what the tool prints: `{"tool", "result"}` or `{"refused"}`."""
    return isinstance(answer, dict) and (
        isinstance(answer.get("result"), dict) or isinstance(answer.get("refused"), dict))


def _snippet(value: Any, limit: int = 240) -> str:
    """What the agent recorded, on one line and cut."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return " ".join(text.split())[:limit]


def _missing_module_hint(text: str) -> str:
    if "No module named" in text or "not found" in text or "No such file" in text:
        return (f" -- the tool is MISSING from the agent's shell: `{GIT_CLI}` does not run "
                "on the agent's PATH (is it the image's venv interpreter?)")
    return ""


def describe_step(role: Role, step: str, tool: str) -> str:
    """Which of three things the agent's record shows for one step, so a red
    run names it (2026-11-21, when three checks said only "answered with no
    refusal", "0 fetch(es)" and "not audited"):

      * TOOL NOT CALLED: no line for the step -- and, when the worker itself
        recorded refusing that tool, REFUSAL NOT RECORDED by the agent;
      * TOOL MISSING: a line that is not a tool's answer (a shell error);
      * the tool's own answer: its refusal code, or that it succeeded.
    """
    recorded = role.recorded(step)
    if recorded is _NOTHING:
        worker = role.worker_refusals(tool)
        if worker:
            return (f"step {step}: refusal not recorded -- the agent recorded no answer, but the "
                    f"worker recorded refusing {tool} ({', '.join(worker)})")
        return f"step {step}: tool not called -- the agent recorded no answer for it"
    if not _is_tool_answer(recorded):
        text = _snippet(recorded)
        return (f"step {step}: tool missing -- the agent recorded {text or 'an empty answer'}, "
                f"which is not a tool's answer{_missing_module_hint(text)}")
    code = refusal_code(recorded)
    if code:
        return f"step {step}: refused {code}"
    return f"step {step}: answered with no refusal (the call succeeded)"


def _no_tool_reached(role: Role, what: str) -> Verdict | None:
    """A FAIL naming the cause when `role` ran but its agent never reached the
    tools at all -- no call audited and no tool answer recorded -- or None.

    Without it each check listed what was missing ("0 fetch(es)", "not
    audited", "answered with no refusal") as if the platform had refused or
    moved something, when the agent had simply been handed no tool
    (2026-11-21, `GIT_CLI`'s comment). The worker's own lines in the answers
    file are not the agent's: they once made a task that never ran the tool
    look as if it had."""
    if role.calls:
        return None
    mine = role.agent_answers
    if any(_is_tool_answer(e.get("answer")) for e in mine):
        return None
    ended = f"the task ended {role.state}: {role.last_error()}"
    if mine:
        text = _snippet(mine[0].get("answer"))
        return "FAIL", (f"tool missing: the {what} task's agent made no git tool call (none audited) "
                        f"and none of its {len(mine)} recorded answer(s) in {ANSWERS} is a tool's "
                        f"answer, so it never reached the tools: step {mine[0].get('step')!r} "
                        f"recorded {text or 'an empty answer'}{_missing_module_hint(text)}; {ended}")
    worker = [str(refusal_code(e.get("answer"))) for e in role.answers]
    return "FAIL", (f"tool not called: the {what} task's agent made no git tool call (none audited) "
                    f"and recorded no answer in {ANSWERS}: it never reached the tools, so nothing "
                    f"here was measured. Its prompt runs them as `{GIT_CLI} <tool>`, which needs "
                    "$SWARM_GIT, exported only to an attempt the worker offers git tools "
                    "(the worker logs 'git tools offered for this attempt')"
                    + (f"; the worker itself recorded {len(worker)} refusal(s): {', '.join(worker)}"
                       if worker else "")
                    + f"; {ended}")


def _fetched(role: Role, ref: str) -> list[str | None]:
    """What each git_fetch of `ref` resolved to, oldest first; None for a refused one."""
    return [row.get("resolved_sha") if row.get("result") == "ok" else None
            for row in role.rows("git_fetch", ref=ref)]


def check_rebase(lane: Role, rebase: Role) -> Verdict:
    early = _not_run(lane, "lane (the pull request's opener)")
    if early:
        return early
    if lane.state != "SUCCEEDED":
        return "FAIL", (f"the lane that opens the pull request ended {lane.state}: {lane.last_error()} "
                        f"(it clones {BASE_BRANCH}, which sandbox-sync.sh keeps)")
    number = pull_request_number(lane.doc)
    if number is None:
        return "FAIL", "the lane ended SUCCEEDED but opened no pull request to rebase"
    early = _not_run(rebase, "rebase") or _no_tool_reached(rebase, "rebase")
    if early:
        return early
    branch = f"swarm/{lane.task}"
    problems: list[str] = []
    own = [s for s in _fetched(rebase, branch) if s]
    if not own:
        problems.append(f"no git_fetch of {branch} succeeded, so swarm-api recorded no tip to lease")
    main = [s for s in _fetched(rebase, "main") if s]
    if not main:
        problems.append("no git_fetch of main succeeded (git_rebase_onto fetches it)")
    conflicts = result_of(rebase.answer("rebase")).get("conflicts")
    if not (isinstance(conflicts, list) and PLANTED in conflicts):
        problems.append(f"the rebase answer did not list {PLANTED} as conflicted "
                        f"(it listed {conflicts!r}): the planted conflict was not met")
    pushes = rebase.rows("git_push_branch", result="ok", branch=branch)
    push = pushes[-1] if pushes else None
    if push is None:
        refused = [f"{r.get('code')}" for r in rebase.rows("git_push_branch")]
        problems.append(f"no push of {branch} succeeded (audited: {refused or 'none'}; "
                        f"the agent was answered {refusal_code(rebase.answer('push')) or 'nothing'})")
    else:
        if push.get("rewrite") is not True:
            problems.append("the push was not audited as a lease-guarded rewrite (rewrite: true)")
        if own and push.get("old_sha") != own[0]:
            problems.append(f"the push replaced {push.get('old_sha')}, not the tip its fetch "
                            f"recorded ({own[0]})")
        if not _SHA.match(str(push.get("new_sha") or "")):
            problems.append("the push audit names no new sha")
    checks = rebase.rows("pr_checks", result="ok")
    reading = checks[-1] if checks else None
    if reading is None:
        # The audit rows and the agent's answer tell a forge_refused (the
        # sandbox credential's scope) from a call the agent never made.
        audited = [f"{r.get('result')} {r.get('code')}" for r in rebase.rows("pr_checks")]
        answered = rebase.answer('pr-checks')
        sentence = refusal_message(answered)
        problems.append(f"no pr_checks call succeeded (audited: {audited or 'none'}; "
                        f"the agent was answered "
                        f"{refusal_code(answered) or 'nothing'})"
                        + (f": {sentence}" if sentence else ""))
    else:
        if reading.get("pr") != number:
            problems.append(f"pr_checks read #{reading.get('pr')}, not the lane's #{number}")
        if push is not None and reading.get("resolved_sha") != push.get("new_sha"):
            problems.append(f"pr_checks read head {reading.get('resolved_sha')}, not the pushed "
                            f"{push.get('new_sha')}")
    env = judge_env(rebase.env)
    if env:
        problems.append("the runner's environment carries a forge credential: " + "; ".join(env))
    if rebase.state != "SUCCEEDED":
        problems.append(f"the lane ended {rebase.state}: {rebase.last_error()}")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", (
        f"fetched {branch} at {own[0][:12]}, rebased onto main {main[-1][:12]} through the "
        f"planted conflict in {PLANTED}, pushed {push['new_sha'][:12]} as an audited rewrite, "
        f"and pr_checks read #{number} at that head "
        f"({result_of(rebase.answer('pr-checks')).get('state') or 'state not recorded'}, "
        f"from {result_of(rebase.answer('pr-checks')).get('source') or 'an unnamed source'}); the "
        f"runner's {len(rebase.env['names'])} environment variables hold no forge credential"
    )


def check_read_private(reader: Role, read: Any) -> Verdict:
    """`read`: {"repo", "path", "ref", "expected_sha256"?} or {"skip": reason}."""
    if not isinstance(read, dict) or read.get("skip") or not read.get("repo"):
        why = read.get("skip") if isinstance(read, dict) else None
        return "SKIP", f"not measured: {why or 'no allowlisted repository was chosen'}"
    early = _not_run(reader, "reader") or _no_tool_reached(reader, "reader")
    if early:
        return early
    repo = str(read["repo"])
    problems: list[str] = []
    first = reader.rows("read_private", repository=repo, ref=read.get("ref"))
    resolved = first[-1].get("resolved_sha") if first and first[-1].get("result") == "ok" else None
    if not resolved:
        problems.append(f"read_private of {repo} at {read.get('ref')} did not succeed "
                        f"({(first[-1].get('code') if first else None) or 'not audited'})")
    at_sha = reader.rows("read_private", result="ok", repository=repo, ref=resolved) if resolved else []
    if resolved and not at_sha:
        problems.append(f"no read_private of {repo} at the sha {resolved[:12]} succeeded")
    elif at_sha and at_sha[-1].get("resolved_sha") != resolved:
        problems.append("the read at a sha resolved to another sha")
    text = result_of(reader.answer("read-sha")).get("text")
    expected = read.get("expected_sha256")
    if expected:
        got = hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else None
        if got != expected:
            problems.append(f"the file read at {(resolved or '?')[:12]} is not {read.get('path')} "
                            "as this build has it")
    elif not (isinstance(text, str) and text) and \
            not result_of(reader.answer("read-sha")).get("entries"):
        problems.append("the read at a sha returned no content")
    unlisted = [r for r in reader.rows("read_private")
                if str(r.get("repository") or "").startswith(UNLISTED_PREFIX)]
    if not unlisted or unlisted[-1].get("code") != "repo_not_allowlisted":
        problems.append("the control read of a repository on no list was not refused "
                        f"repo_not_allowlisted ({unlisted[-1].get('code') if unlisted else 'not audited'})")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", (f"read {repo}:{read.get('path')} at {read.get('ref')} -> {resolved[:12]}, then at "
                    f"that sha; a repository on no list was refused repo_not_allowlisted")


def _no_push_reached(reader: Role) -> str | None:
    ok = reader.rows("git_push_branch", result="ok")
    if ok:
        return f"swarm-api ACCEPTED a push from the reader, to {ok[-1].get('branch')}"
    return None


def check_push_main(reader: Role) -> Verdict:
    early = _not_run(reader, "reader") or _no_tool_reached(reader, "reader")
    if early:
        return early
    problems = []
    code = refusal_code(reader.answer("push-main"))
    if code not in MAIN_REFUSALS:
        problems.append(f"the push to main was not refused {' or '.join(MAIN_REFUSALS)} "
                        f"({describe_step(reader, 'push-main', 'git_push_branch')})")
    reached = _no_push_reached(reader)
    if reached:
        problems.append(reached)
    tips = [s for s in _fetched(reader, "main") if s]
    if len(tips) < 2:
        problems.append(f"main was not fetched before and after the push ({len(tips)} fetch(es) "
                        f"audited; {describe_step(reader, 'fetch-main-before', 'git_fetch')}; "
                        f"{describe_step(reader, 'fetch-main-after', 'git_fetch')})")
    elif tips[0] != tips[-1]:
        problems.append(f"main moved from {tips[0][:12]} to {tips[-1][:12]} across the refused push")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", (f"refused {code}; swarm-api accepted no push from the task, and main read "
                    f"{tips[0][:12]} before and after")


def check_push_other(reader: Role, rebase: Role, lane_task: str) -> Verdict:
    early = _not_run(reader, "reader") or _no_tool_reached(reader, "reader")
    if early:
        return early
    if not lane_task:
        return "FAIL", "there is no other task's branch to aim at: the lane was not created"
    branch = f"swarm/{lane_task}"
    problems = []
    code = refusal_code(reader.answer("push-other"))
    if code not in OTHER_REFUSALS:
        problems.append(f"the push to {branch} was not refused {OTHER_REFUSALS[0]} "
                        f"({describe_step(reader, 'push-other', 'git_push_branch')})")
    reached = _no_push_reached(reader)
    if reached:
        problems.append(reached)
    tips = _fetched(reader, branch)
    # The rebase lane may move that branch meanwhile; only its own audited
    # pushes may account for a move.
    own_moves = {r.get("new_sha") for r in rebase.rows("git_push_branch", result="ok", branch=branch)}
    if len(tips) < 2:
        problems.append(f"{branch} was not fetched before and after the push ({len(tips)} fetch(es) "
                        f"audited; {describe_step(reader, 'fetch-other-before', 'git_fetch')}; "
                        f"{describe_step(reader, 'fetch-other-after', 'git_fetch')})")
    elif tips[-1] != tips[0] and tips[-1] not in own_moves:
        problems.append(f"{branch} moved from {tips[0]} to {tips[-1]}, which no push of its own "
                        "lane accounts for")
    if problems:
        return "FAIL", "; ".join(problems)
    where = f"{tips[0][:12]}" if tips and tips[0] else "absent"
    return "PASS", f"refused {code}; swarm-api accepted no push from the task, and {branch} ({where}) " \
                   "moved only by its own lane's pushes"


def check_push_fails(build: Role) -> Verdict:
    early = _not_run(build, "build") or _no_tool_reached(build, "build")
    if early:
        return early
    pushed = build.rows("git_push_branch", result="ok", branch=f"swarm/{build.task}")
    if not pushed:
        return "FAIL", (f"the push this check makes fail was never set up: the mid-task push of "
                        f"swarm/{build.task} did not succeed (none audited; "
                        f"{describe_step(build, 'push-first', 'git_push_branch')}); "
                        f"the step ended {build.state}: {build.last_error()}")
    if build.state == "SUCCEEDED":
        doc = build.doc if isinstance(build.doc, dict) else {}
        git = (doc.get("result_summary") or {}).get("git") or {}
        if git.get("published") is True:
            return "FAIL", ("the final push this check makes fail SUCCEEDED (published: true): "
                            "the end-of-task publish no longer refuses a branch that does not "
                            "fast-forward, so this check needs another way to fail a push")
        return "FAIL", ("the build step ended SUCCEEDED with its branch not pushed -- the "
                        f"#1225 regression G1 fixed ({git.get('publish_reason') or 'no reason'})")
    if build.state != "FAILED":
        return "FAIL", f"the build step ended {build.state}, not FAILED: {build.last_error()}"
    error = build.last_error()
    if "publish_failed" not in error:
        return "FAIL", f"the build step ended FAILED, but not for its push: {error}"
    return "PASS", (f"after its mid-task push ({pushed[-1].get('new_sha', '')[:12]}) the step's "
                    f"final push could not fast-forward, and it ended FAILED: {error[:160]}")


def judge(base: str) -> list[tuple[str, str, str]]:
    roles = {name: Role(base, name) for name in ROLES}
    read = _read_json(os.path.join(base, "read.json"))
    verdicts = {
        "rebase": lambda: check_rebase(roles["lane"], roles["rebase"]),
        "read_private": lambda: check_read_private(roles["reader"], read),
        "push_main": lambda: check_push_main(roles["reader"]),
        "push_other": lambda: check_push_other(roles["reader"], roles["rebase"], roles["lane"].task),
        "push_fails": lambda: check_push_fails(roles["build"]),
    }
    out = []
    for check in CHECKS:
        try:
            verdict, reason = verdicts[check]()
        except Exception as exc:  # a crash judges nothing: FAIL, named  # noqa: BLE001
            verdict, reason = "FAIL", f"the judge crashed ({type(exc).__name__}: {exc})"
        out.append((check, verdict, reason))
    return out


def pr_kind(body: str, files: Iterable[str] = ()) -> str:
    """github-verify.sh's question: which group's pull request is this?

    `git-tools` for the rebase lane's (its marker); `git-tools-integrate`
    for the build step's integrator's -- its marker, or, for an integrator
    that wrote none (every run before the marker), the platform's
    "Integrates" block over files that ALL lie under FIXTURE_DIR, where no
    other group writes; `other` for the rest, which github-verify.sh classifies
    further (the fix loop's, the claude-code group's, or unclassified). On 2026-11-21 the integrator's pull request
    was read as claude-code's and failed its calc.py assertion."""
    body = body or ""
    if PR_MARKER in body:
        return "git-tools"
    if INTEGRATE_MARKER in body:
        return "git-tools-integrate"
    changed = [f for f in files if f]
    if changed and all(f.startswith(FIXTURE_DIR + "/") for f in changed) and _INTEGRATES.search(body):
        return "git-tools-integrate"
    return "other"


def test_a_refused_pr_checks_call_is_named_by_its_code(tmp_path: Any) -> None:
    """pytest's entry (as test_caller_mcp.py's), offline and standard library
    only: when no pr_checks call succeeded, check [1]'s reason names the
    audited refusal and the agent's answer, so a forge_refused is told from no
    call at all (F16, archived issue 2379). The full matrix is in
    tests/unit/scripts/test_runner_git_tools_acceptance.py."""
    files = {
        "lane/task": "task_lane\n", "lane/state": "SUCCEEDED\n",
        "lane/doc.json": json.dumps({"result_summary": {"git": {"pull_request": {"number": 7}}}}),
        "lane/calls.json": json.dumps({"calls": []}),
        "rebase/task": "task_rebase\n", "rebase/state": "SUCCEEDED\n",
        "rebase/calls.json": json.dumps({"calls": [
            {"tool": "pr_checks", "result": "refused", "code": "forge_refused", "at": "1"}]}),
        f"rebase/{ANSWERS}": json.dumps({"step": "pr-checks", "answer": {
            "refused": {"code": "forge_refused"}}}) + "\n",
    }
    for name, text in files.items():
        path = os.path.join(str(tmp_path), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    verdict, reason = check_rebase(Role(str(tmp_path), "lane"), Role(str(tmp_path), "rebase"))
    assert verdict == "FAIL", reason
    assert ("no pr_checks call succeeded (audited: ['refused forge_refused']; "
            "the agent was answered forge_refused)") in reason, reason


def test_a_refused_pr_checks_call_names_the_refusals_sentence(tmp_path: Any) -> None:
    """archived issue 1188: from 2026-10-23 to 2026-10-27 every red run of this group said
    `forge_refused` and nothing more, and swarm-api's log -- where the
    sentence was -- is not readable from the tenant that measured it. The
    agent records the whole answer, sentence and all: check [1] names it,
    so a 403 on the check-runs read is told from a 401 or another read."""
    sentence = ("GitHub refused the tenant's forge credential for the check runs at "
                "o/r@0123456789ab (HTTP 403): the credential needs `checks: read`")
    files = {
        "lane/task": "task_lane\n", "lane/state": "SUCCEEDED\n",
        "lane/doc.json": json.dumps({"result_summary": {"git": {"pull_request": {"number": 7}}}}),
        "lane/calls.json": json.dumps({"calls": []}),
        "rebase/task": "task_rebase\n", "rebase/state": "SUCCEEDED\n",
        "rebase/calls.json": json.dumps({"calls": [
            {"tool": "pr_checks", "result": "refused", "code": "forge_refused", "at": "1"}]}),
        f"rebase/{ANSWERS}": json.dumps({"step": "pr-checks", "answer": {
            "refused": {"code": "forge_refused", "message": sentence}}}) + "\n",
    }
    for name, text in files.items():
        path = os.path.join(str(tmp_path), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    verdict, reason = check_rebase(Role(str(tmp_path), "lane"), Role(str(tmp_path), "rebase"))
    assert verdict == "FAIL", reason
    # MUTATION: name the code alone (as before) and the sentence is not in the reason.
    assert "check runs at o/r@0123456789ab (HTTP 403)" in reason, reason


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prompt")
    p.add_argument("name", choices=("lane", "rebase", "reader", "build", "integrate"))
    p.add_argument("--run-id", required=True)
    p.add_argument("--lane-branch", default="")
    p.add_argument("--read-repo", default="")
    p.add_argument("--read-path", default="")
    p.add_argument("--read-ref", default="")
    sub.add_parser("expected-planted")
    k = sub.add_parser("pr-kind")
    k.add_argument("--body-file", required=True)
    k.add_argument("--files-file", default="",
                   help="the pull request's changed paths, one per line")
    j = sub.add_parser("judge")
    j.add_argument("--dir", required=True)
    j.add_argument("--results", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "prompt":
        sys.stdout.write(prompt(args.name, run_id=args.run_id, lane_branch=args.lane_branch,
                                read_repo=args.read_repo, read_path=args.read_path,
                                read_ref=args.read_ref))
        return 0
    if args.command == "expected-planted":
        sys.stdout.write(expected_planted())
        return 0
    if args.command == "pr-kind":
        files = _read_text(args.files_file).splitlines() if args.files_file else []
        sys.stdout.write(pr_kind(_read_text(args.body_file), files) + "\n")
        return 0
    with open(args.results, "a", encoding="utf-8") as fh:
        for check, verdict, reason in judge(args.dir):
            fh.write(json.dumps({"check": check, "verdict": verdict, "reason": reason}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
