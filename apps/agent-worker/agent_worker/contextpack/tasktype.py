"""Which of the six pipelines a step gets (§4.5, "How the task type is known").

By the step's role, in this order, never by a model:

1. the stager's `stage.json` `type`, when it is one of `TASK_TYPES`: the
   stager knows the step's role from the signed spec, which this process
   does not read;
2. the platform's own templates, recognisable from what the worker staged
   and how the control plane worded the prompt:
     * plan -- the planner's lead, `issueruns.planner_prompt`
       ("Plan the work for GitHub issue ...");
     * fix -- a CI fix round's FAILING CHECKS fence (`issueci.ci_fix_continuation`);
     * review -- a re-review: a staged `swarm-work.patch` and the earlier
       review's staged `verdict.json`, with `verdict.json` among the step's own
       `expected_outputs` (below);
     * fix -- any other staged `verdict.json` (the issue run's fix step,
       `issueruns.compile_plan`);
     * review -- a staged `swarm-work.patch` and a prompt asking for
       `verdict.json` (the issue run's review step);
3. the brief's lead verb, for an ad-hoc task: "write/add tests" is test-writing,
   "design/architect" is design;
4. otherwise implement, which §4.5 makes the default for a task with no role.
   Only here, and only when the workspace opted in to Jev for code, J0 may
   type the step instead (`classify_with_jev`).

A RE-REVIEW STAGES A VERDICT TOO. Before 2026-11-02 any staged `verdict.json`
made a step a fix, so a re-review, which stages the review it re-reads, was a
fix: its pack seeded from the earlier findings and its own verdict got no J3
triage (`judge.poststeps.review_post_step`). What tells the two apart is what
the step OWES: the worker writes `input.expected_outputs` from the task's
signed `step_id` (`expected_outputs.declared_outputs`, `is_review_step`), and
a review or re-review owes `verdict.json` there whether or not a later step
stages it; a fix owes none. A caller's own `input.expected_outputs` never
reaches the runner (the worker drops it), so the claim is the platform's.

A caller who words a brief like a template changes only which checklist and
quotas its own step gets: the pack is advisory and decides nothing.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any, Mapping

from .layout import CONTEXT_DIR_NAME, TASK_TYPES, read_stage

PLANNER_LEAD = "Plan the work for GitHub issue"
FAILING_CHECKS = re.compile(r"^=== FAILING CHECKS [0-9a-f]{16} ===$", re.M)
PATCH_FILE = "swarm-work.patch"
VERDICT_FILE = "verdict.json"
_TEST_LEAD = re.compile(r"^\s*(?:please\s+)?(?:write|add|cover)\b[^.\n]{0,40}?\btests?\b",
                        re.I)
_DESIGN_LEAD = re.compile(r"^\s*(?:please\s+)?(?:design|architect)\b", re.I)
#: §4.5: a task with no role is an implementer.
DEFAULT_TYPE = "implement"
#: What J0 may turn the default into (`classify_with_jev`).
JEV_TYPES = ("test", "design")


def staged_names(payload: Mapping[str, Any]) -> set[str]:
    """The base names of what the worker staged into `work/` for this step."""
    names: set[str] = set()
    for entry in payload.get("staged_inputs") or ():
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(raw, str) and raw:
            names.add(posixpath.basename(raw))
    return names


def owed_names(payload: Mapping[str, Any]) -> set[str]:
    """The names this step owes, as the worker wrote them into
    `input.expected_outputs` (`expected_outputs.METADATA_KEY`)."""
    owed = payload.get("expected_outputs")
    if not isinstance(owed, list):
        return set()
    return {name for name in owed if isinstance(name, str)}


def _by_role(payload: Mapping[str, Any], stage: Mapping[str, Any]) -> str | None:
    """Rules 1 to 3; None when none applies."""
    declared = stage.get("type")
    if isinstance(declared, str) and declared in TASK_TYPES:
        return declared
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
    staged = staged_names(payload)
    if prompt.lstrip().startswith(PLANNER_LEAD):
        return "plan"
    if FAILING_CHECKS.search(prompt):
        return "fix"
    if VERDICT_FILE in staged:
        if PATCH_FILE in staged and VERDICT_FILE in owed_names(payload):
            return "review"
        return "fix"
    if PATCH_FILE in staged and VERDICT_FILE in prompt:
        return "review"
    if _TEST_LEAD.search(prompt):
        return "test"
    if _DESIGN_LEAD.search(prompt):
        return "design"
    return None


def classify(payload: Mapping[str, Any], work_dir: Path,
             stage: Mapping[str, Any] | None = None) -> str:
    if stage is None:
        stage = read_stage(Path(work_dir) / CONTEXT_DIR_NAME)
    return _by_role(payload, stage) or DEFAULT_TYPE


def classify_with_jev(payload: Mapping[str, Any], work_dir: Path,
                      stage: Mapping[str, Any] | None = None, judge: Any = None) -> str:
    """`classify`, and where it falls back to implement, J0 (§5.2) when the
    attempt has a Jev client (`judge.attempt.load`: Jev for code is on).

    J0 is one call over §5.2's six roles, recorded in the attempt's ledger by
    the client (`kind` "J0"). Its answer is taken only at or above
    `shapes.CLASSIFY_MIN_CONFIDENCE` and only when it is one of `JEV_TYPES`:
    test and design read the brief exactly as implement does (`seeds.seeds_for`)
    and change only the ranking's emphasis, quotas and checklist. Review, fix
    and plan seed from what a step these rules typed implement has not staged
    (a patch, a verdict, a FAILING CHECKS fence, the issue file), so their
    packs would come out emptier, and the runner's post-steps
    (`runners.cliagent._jev_post_steps`, which call `classify`) would not
    agree with the pack. Anything else, a failed call included, is implement:
    the pack is then byte for byte the local-only one.
    """
    if stage is None:
        stage = read_stage(Path(work_dir) / CONTEXT_DIR_NAME)
    local = _by_role(payload, stage)
    if local is not None or judge is None:
        return local or DEFAULT_TYPE
    from ..judge import shapes

    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
    try:
        decision = judge.decide("J0", *shapes.j0_classify(prompt))
    except Exception:  # noqa: BLE001 -- Jev is advisory: a failure is the local answer
        return DEFAULT_TYPE
    if not decision.ok or not isinstance(decision.answers, dict):
        return DEFAULT_TYPE
    chosen = shapes.read_classify(decision.answers)
    return chosen if chosen in JEV_TYPES else DEFAULT_TYPE
