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
   type the step instead (`classify_with_jev`) -- unless the step is one of
   the platform's own templates (`known_template`), whose type the workflow
   that made it already fixes.

A STEP WHOSE TYPE IS KNOWN ASKS JEV NOTHING (owner's choice, 4 November 2026).
SwarmCloud's own usage ledger, 3 to 4 November: 172 of 377 Jev calls were J0,
and 122 of those typed a peer application's visual-check capture steps, which the QA
renderer (`swarm_api.qaspec`) makes and whose role it names in the step id.
The step id does not reach this process (the worker writes `input` alone into
`input.json`), so a template is recognised by the lead its renderer writes:
`TEMPLATE_LEADS`, each read from the renderer it names, and
tests/unit/worker/test_tasktype_known_type.py renders the real templates and
holds them recognised. Such a step keeps the type rules 1 to 3 give it, or
implement: a template the six types do not name (a capture, a conflict fix)
gets the local-only pack, which is what J0's answer would have been for every
type but test and design, neither of which these steps are. Where the
attempt has a Jev client, the skip is recorded in its ledger as a J0 row
with outcome `skipped:known_type` (`record_known_type_skip`), so the usage
page counts it under skipped and not as a call. Merge steps run no agent and
build no pack, so they are not here.

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
import uuid
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
#: The audit outcome of a J0 that was not asked because the type was known:
#: `skipped:<reason>`, which `judge.budget.split_by_kind` and swarm-api's
#: usage tally (`jevusage.Tally.skipped`) count as a skip, never a call.
KNOWN_TYPE_SKIP = "known_type"
#: The platform's own step templates, by the lead their renderer writes into
#: the step's prompt, with the step id (or id pattern) the template names.
#: `match` anchors at the prompt's start (after leading blanks); a `search`
#: lead may follow a caller-chosen lead. Each is read from its renderer.
TEMPLATE_LEADS: tuple[tuple[str, str, "re.Pattern[str]"], ...] = (
    # qaspec._capture_prompt (`capture-<group>`)
    ("capture", "match", re.compile(r"Capture, do not review\. Visual QA of ")),
    # qaspec.render_discovery (`discover`)
    ("discover", "match", re.compile(r"Discover the screens of .{1,100}?, do not review them\.")),
    # qaspec._review_prompt (`review-<group>`)
    ("qa-review", "match", re.compile(r"Visual QA review of ")),
    # qaspec._flow_prompt (`flow-<id>`), qaspec._flow_batch_prompt (`flows-<k>`)
    ("flow", "match", re.compile(r"Goal-driven flow tests? of ")),
    # qaspec._report_prompt (`report`), qaspec._flow_report_prompt (`flow-report`)
    ("qa-report", "match", re.compile(r"Write the visual QA report of ")),
    # issueruns.planner_prompt (the planner)
    ("plan", "match", re.compile(re.escape(PLANNER_LEAD))),
    # issueruns' review steps and mergewake's advisory pick review (`review`)
    ("review", "match", re.compile(r"Review the change (?:on this branch|for GitHub issue)\b")),
    # validation.REREVIEW_PREAMBLE (`re-review`)
    ("re-review", "match", re.compile(r"You are the RE-REVIEW of this workflow\b")),
    # issueruns' and mergewake's fix steps (`fix`)
    ("fix", "match", re.compile(r"Fix every finding in verdict\.json\b")),
    # issueruns._requirements_check (`requirements-check`)
    ("requirements-check", "match", re.compile(
        r"Confirm, for GitHub issue \S+, whether the change on this branch delivers each "
        r"requirement\b")),
    # issueci.ci_fix_continuation (`ci-fix`): its lead is the caller's, its
    # round sentence and FAILING CHECKS fence are the template's
    ("ci-fix", "search", re.compile(r"\bThis is CI fix round \d+ of at most \d+\.")),
    ("ci-fix", "search", FAILING_CHECKS),
    # mergewake's conflict round (`conflict-fix`)
    ("conflict-fix", "search", re.compile(r"\bThis is conflict fix round \d+ of at most \d+\.")),
    # schedtypes.pr_shepherd.conflict_fix_workflow (its `fix`): its nonce fence
    ("conflict-fix", "search", re.compile(r"^=== FILES CHANGED ON BOTH SIDES [0-9a-f]{16} ===$",
                                          re.M)),
    # mergewake.pick_fix_continuation (`pick-fix`)
    ("pick-fix", "search", re.compile(r"\bThis is pick fix round \d+ of at most \d+\.")),
    # mergewake's advisory pick workflow (`pick-fix`)
    ("pick-fix", "match", re.compile(
        r"(?:pull request #\d+|a pull request) merged past an owner question while it was "
        r"advisory\b")),
)


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


def _prompt(payload: Mapping[str, Any]) -> str:
    return payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""


def known_template(payload: Mapping[str, Any]) -> str | None:
    """The platform template this step's prompt was rendered from
    (`TEMPLATE_LEADS`), or None for a step none of them made."""
    prompt = _prompt(payload)
    lead = prompt.lstrip()
    for name, how, pattern in TEMPLATE_LEADS:
        if (pattern.match(lead) if how == "match" else pattern.search(prompt)):
            return name
    return None


def record_known_type_skip(judge: Any) -> None:
    """One J0 audit row, outcome `skipped:known_type`, in the attempt's
    ledger, the shape `JudgeClient.decide` records a skip in: no call, no
    tokens, no effect on the breaker. Never raises: the record is the
    usage page's, and a ledger that cannot take it costs the pack nothing."""
    try:
        backend = judge.backend
        judge.ledger.record({
            "call_id": uuid.uuid4().hex[:12], "kind": "J0",
            "backend": getattr(backend, "name", None), "bytes": 0,
            "input_tokens": 0, "ms": 0,
            "outcome": f"skipped:{KNOWN_TYPE_SKIP}",
            "requested_model": getattr(backend, "model", None) or None,
            "tokens_estimated": False,
        }, succeeded=None)
    except Exception:  # noqa: BLE001 -- the audit row is advisory, as J0 is
        return


def _by_role(payload: Mapping[str, Any], stage: Mapping[str, Any]) -> str | None:
    """Rules 1 to 3; None when none applies."""
    declared = stage.get("type")
    if isinstance(declared, str) and declared in TASK_TYPES:
        return declared
    prompt = _prompt(payload)
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

    A step whose type is already known -- rules 1 to 3 type it, or a platform
    template made it (`known_template`) -- is not asked: with a Jev client,
    the skip is recorded (`record_known_type_skip`) and the type is the
    rules' answer, or implement.
    """
    if stage is None:
        stage = read_stage(Path(work_dir) / CONTEXT_DIR_NAME)
    local = _by_role(payload, stage)
    if judge is None:
        return local or DEFAULT_TYPE
    if local is not None or known_template(payload) is not None:
        record_known_type_skip(judge)
        return local or DEFAULT_TYPE
    from ..judge import shapes

    prompt = _prompt(payload)
    try:
        decision = judge.decide("J0", *shapes.j0_classify(prompt))
    except Exception:  # noqa: BLE001 -- Jev is advisory: a failure is the local answer
        return DEFAULT_TYPE
    if not decision.ok or not isinstance(decision.answers, dict):
        return DEFAULT_TYPE
    chosen = shapes.read_classify(decision.answers)
    return chosen if chosen in JEV_TYPES else DEFAULT_TYPE
