"""A lane head runs each check once, on its pull request (owner, 2026-11-02).

LANE CI-ONCE-PER-HEAD (part of #2688). LANE CI-EARLY (2026-10-20) added a
`push` trigger on `swarm/**` to application.yml, security.yml and
terraform.yml, so a lane head that also has a pull request ran about 100
checks twice, once per event. In the 14 days to 2 November 2026 merge steps
spent 352 hours parked on CI_PENDING along workflows' slowest chains; on 28
October one head's checks took 1.5 to 4 hours behind 23 to 28 queued merges
although no job ran over about 20 minutes. The owner chose to run CI once per
head: on the pull request.

What this holds, reading the workflow YAML:

* no workflow that a lane branch's pull request triggers is ALSO triggered by
  a push to that lane branch -- one head, one event;
* `main` keeps its own push runs (the tested-tree record main reuses, the
  image build, the release);
* every check the `main-protection` ruleset requires (read from the ruleset
  body docs/ci.md publishes) is a job of a workflow that runs on EVERY pull
  request, with no path or branch filter, and on `synchronize`, so a pull
  request re-tests when its branch is brought up to date with a moved base;
* the merge step and swarm-api's check reading decide a head that carries
  only pull_request runs (they never required a push run).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
CI_DOC = REPO / "docs" / "ci.md"
#: The platform's branches start `swarm/` (agent_worker publishes
#: `swarm/<task id>`); both shapes a lane head takes.
LANE_REFS = ("swarm/task_0123456789abcdef0123", "swarm/task_0123456789abcdef0123-fix")
#: Workflows CI-EARLY gave a lane push; each must keep main's.
MAIN_PUSH_WORKFLOWS = ("application.yml", "security.yml", "terraform.yml")


def _workflows() -> list[Path]:
    found = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert len(found) >= 10, found  # the sweep ran over the real directory
    return found


def _on(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as True.
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {event: None for event in on}
    assert isinstance(on, dict), (path.name, on)
    return on


def _glob(pattern: str) -> re.Pattern[str]:
    """GitHub's branch filter glob: `**` any characters, `*` any but `/`,
    `?` one character (docs.github.com, filter pattern cheat sheet)."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _branch_triggers(trigger: dict | None, branch: str) -> bool:
    """Whether a push/pull_request trigger block fires for `branch`
    (for pull_request, the base branch; for push, the pushed branch)."""
    trigger = trigger or {}
    if "branches" in trigger:
        included = False
        for pattern in trigger["branches"]:
            if pattern.startswith("!"):
                if _glob(pattern[1:]).match(branch):
                    included = False
            elif _glob(pattern).match(branch):
                included = True
        return included
    if "branches-ignore" in trigger:
        return not any(_glob(p).match(branch) for p in trigger["branches-ignore"])
    return True


def test_the_branch_glob_reads_githubs_patterns():
    """The matcher the sweep relies on. MUTATION: read `*` as `**`."""
    assert _glob("swarm/**").match("swarm/task_1")
    assert _glob("swarm/**").match("swarm/a/b")
    assert not _glob("swarm/*").match("swarm/a/b")
    assert not _glob("main").match("main2")
    assert _branch_triggers({"branches": ["main", "swarm/**"]}, LANE_REFS[0])
    assert not _branch_triggers({"branches": ["main"]}, LANE_REFS[0])
    assert not _branch_triggers({"branches": ["**", "!swarm/**"]}, LANE_REFS[0])
    assert not _branch_triggers({"branches-ignore": ["swarm/**"]}, LANE_REFS[0])
    assert _branch_triggers(None, LANE_REFS[0])


@pytest.mark.parametrize("lane", LANE_REFS)
def test_no_lane_head_runs_a_workflow_on_both_push_and_pull_request(lane):
    """A lane head's pull request targets main; a workflow that runs on that
    pull request must not also run on the push of the lane branch, or every
    check it holds runs twice at the same head. MUTATION: keep `swarm/**` in
    any workflow's push branches (CI-EARLY's triggers)."""
    visited, twice = 0, []
    for path in _workflows():
        visited += 1
        on = _on(path)
        if "pull_request" not in on or not _branch_triggers(on["pull_request"], "main"):
            continue
        if "push" in on and _branch_triggers(on["push"], lane):
            twice.append(path.name)
    assert visited >= 10, visited
    assert twice == [], f"run on both push and pull_request at a lane head: {twice}"


@pytest.mark.parametrize("name", MAIN_PUSH_WORKFLOWS)
def test_main_keeps_its_own_push_runs(name):
    """Main's push run builds the images, records the tested tree and starts
    the release; only the lane branches lose theirs. MUTATION: drop the push
    trigger, or drop `main` from it."""
    push = _on(WORKFLOWS / name).get("push") or {}
    assert push.get("branches") == ["main"], push
    assert "paths" not in push and "paths-ignore" not in push, push


def _required_contexts() -> list[str]:
    """The `required_status_checks` contexts of the ruleset body docs/ci.md
    publishes ("Owner step: require ci-gate"), the one the owner PUTs."""
    text = CI_DOC.read_text()
    start = text.index(
        'rulesets/"$RULESET_ID" \\', text.index("### Owner step: require ci-gate")
    )
    body = text[text.index("<<'JSON'", start) + len("<<'JSON'"):text.index("\nJSON\n", start)]
    rules = json.loads(body)["rules"]
    (checks,) = [r for r in rules if r["type"] == "required_status_checks"]
    contexts = [c["context"] for c in checks["parameters"]["required_status_checks"]]
    assert "ci-gate" in contexts and len(contexts) >= 5, contexts
    return contexts


def _jobs_by_name() -> dict[str, list[str]]:
    named: dict[str, list[str]] = {}
    for path in _workflows():
        jobs = (yaml.safe_load(path.read_text()) or {}).get("jobs") or {}
        for job_id, job in jobs.items():
            named.setdefault(str(job.get("name", job_id)), []).append(path.name)
    return named


def test_every_required_check_is_produced_on_every_pull_request():
    """Each required check is a job of a workflow that runs on every pull
    request to main with no path filter -- GitHub holds a required check whose
    workflow was filtered out as pending forever -- and on `synchronize`, so
    the up-to-date rule's re-test after the base moves still reports.
    MUTATION: give security.yml's pull_request a `paths:` list, or narrow its
    `types:` to `[opened]`."""
    named = _jobs_by_name()
    contexts = _required_contexts()
    checked = 0
    for context in contexts:
        files = named.get(context)
        assert files, f"no workflow job is named {context!r}"
        (owner,) = files
        on = _on(WORKFLOWS / owner)
        assert "pull_request" in on, (context, owner)
        pull_request = on["pull_request"] or {}
        assert _branch_triggers(pull_request, "main"), (context, pull_request)
        assert "paths" not in pull_request and "paths-ignore" not in pull_request, (context, pull_request)
        types = pull_request.get("types")
        assert types is None or {"opened", "synchronize", "reopened"} <= set(types), (context, types)
        checked += 1
    assert checked == len(contexts) >= 5, (checked, contexts)


def _run(run_id: int, conclusion: str | None, status: str = "completed") -> dict:
    return {"status": status, "conclusion": conclusion, "run_id": run_id, "event": "pull_request"}


PULL_REQUEST_ONLY = [
    ([_run(1, "success")], "green"),
    ([_run(1, None, "in_progress")], "pending"),
    ([_run(1, "failure")], "failed"),
    # The up-to-date rule's re-test is a new head, but a re-run at one head
    # replaces the earlier run of its event.
    ([_run(1, "failure"), _run(2, "success")], "green"),
    ([], "pending"),
]


@pytest.mark.parametrize("runs, state", PULL_REQUEST_ONLY)
def test_the_merge_step_decides_a_head_with_only_pull_request_runs(runs, state):
    """Neither reader waits for a push run that no longer comes. MUTATION
    (merge.py or forgechecks.py): require a `push` event among the runs ->
    the green case reads pending."""
    # Imported here, not at module top: test_collection_stays_cheap holds every
    # module under tests/unit/scripts to collecting without the service stack.
    from agent_worker import merge
    from swarm_api import forgechecks

    assert merge.required_check_state(runs) == state
    # swarm-api names the worker's "failed" `RED`.
    assert forgechecks.required_check_state(runs) == {
        "green": forgechecks.GREEN, "pending": forgechecks.PENDING, "failed": forgechecks.RED}[state]
