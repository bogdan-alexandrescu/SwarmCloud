"""A step whose brief says it changes nothing must declare `allow_empty_diff`.

Owner's request, 2026-11-07 (part of #2688). A workflow of read-only analysis
steps -- each classified branches, wrote its result as artifacts and was told
to change nothing in the repository -- was submitted with strategy
`direct-pr` and no `allow_empty_diff` on its steps. Every step ran clean,
committed nothing, and the worker failed each `published_nothing:`, because
under `direct-pr` a step is held to opening a pull request. The declaration
that makes an empty diff a result already existed; nothing told the submitter
it was missing. Seven steps read FAILED and the work was recovered from the
artifacts by hand.

So a submission is refused, before anything is stored, when a step

  * would be held to opening a pull request: an agent step under `direct-pr`
    that is not a reader (`validation.reader_step_ids`, already stored with
    the flag), or the integrator under `integrate` (a contributor that
    changes nothing already succeeds); a gated step, a verdict source the
    gate reads, the re-review and the security review are exempt;
  * left `allow_empty_diff` out of the submitted step altogether (an explicit
    `false` is a declaration: the step must commit);
  * and whose prompt says, in words only an analysis step writes, that it
    changes nothing (`NO_CHANGE_PHRASES`).

`collect` is never refused: no step there opens a pull request. The refusal
is new, so it ships report-only (`refusals.SWITCHES`, `step_declares_no_change`).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any

from .errors import ValidationFailed
from .refusals import refuse
from .validation import PROMPT_INPUT_KEY, StepSpec

#: Phrases that say a step changes nothing in the repository, matched
#: case-insensitively on word boundaries in its prompt. Kept SHORT on purpose:
#: each is something only an analysis step's brief says, so a step that does
#: commit is never refused. Left out, because a committing step says them too:
#: "do not commit secrets", "finish without changes" (a no-op is allowed, not
#: the deliverable), "read-only" alone (a read-only token, a read-only mount)
#: and "no changes outside" (a scope limit on a step that changes something).
NO_CHANGE_PHRASES: tuple[str, ...] = (
    # The measured brief's words (2026-11-07), and their third person.
    "change nothing",
    "changes nothing",
    # Said of the commit itself: the step is told to leave the branch empty.
    "commit nothing",
    # The imperative an analysis brief uses for the same rule.
    "make no change",
    "make no changes",
    # The passive a brief writes when it states the outcome, not the order.
    "nothing in the repository is changed",
    # The explicit prohibitions, which name the repository as a whole and
    # not a part of it.
    "do not change the repository",
    "do not modify the repository",
    "leave the repository unchanged",
    # A step or task that calls ITSELF read-only; "read-only" alone is not.
    "read-only step",
    "read-only task",
)

#: Words that, right after a phrase, turn it into a scope limit on a step that
#: does commit: "change nothing else" (the platform's own review-fix round),
#: "make no changes outside X", "commit nothing that looks like a credential",
#: "change nothing for it". Such a phrase is not a match. "read-only step" and
#: "read-only task" describe the step itself ("a read-only step that writes
#: triage.tsv"), so no word after them narrows them.
_SCOPE_WORDS = ("else", "outside", "except", "but", "for", "that", "which", "beyond")
_SELF_DESCRIPTIONS = ("read-only step", "read-only task")


def _alternation(phrases: Sequence[str]) -> str:
    return "|".join(re.escape(phrase) for phrase in phrases)


_NO_CHANGE = re.compile(
    r"\b(?:(?:"
    + _alternation([p for p in NO_CHANGE_PHRASES if p not in _SELF_DESCRIPTIONS])
    + r")\b(?![^\S\n]*(?:" + "|".join(_SCOPE_WORDS) + r")\b)|(?:"
    + _alternation(_SELF_DESCRIPTIONS) + r")\b)",
    re.IGNORECASE,
)


#: The served sentence. Fixed: the step id is in `detail`, never in here.
NO_CHANGE_UNDECLARED_MESSAGE = (
    "a step whose brief says it changes nothing must say allow_empty_diff: true "
    "(its deliverable is its artifacts) or allow_empty_diff: false (it must commit)"
)


class StepDeclaresNoChange(ValidationFailed):
    """422 `step_declares_no_change`: see the module docstring."""

    code = "step_declares_no_change"


def says_it_changes_nothing(prompt: Any) -> bool:
    """Whether `prompt` states, in `NO_CHANGE_PHRASES`' words, that its step
    changes nothing in the repository."""
    return isinstance(prompt, str) and _NO_CHANGE.search(prompt) is not None


def _opens_pull_request(
    spec: StepSpec, *, strategy: str, readers: Collection[str],
    integrator_step_id: str | None, verdict_sources: Collection[str],
    rereview: str | None,
) -> bool:
    if spec.when_step is not None or spec.security_review:
        return False
    if spec.step_id in verdict_sources or spec.step_id == rereview:
        return False
    if strategy == "direct-pr":
        return spec.step_id not in readers
    if strategy == "integrate":
        return spec.step_id == integrator_step_id
    return False


def refuse_undeclared_no_change(
    steps: Sequence[Any], step_specs: Sequence[StepSpec], *, strategy: str,
    readers: Collection[str], integrator_step_id: str | None, rereview: str | None,
) -> None:
    """Refuse the first step that says it changes nothing, would be held to a
    pull request, and was submitted without `allow_empty_diff`.

    `steps` are the submitted `WorkflowStepCreate` models: `model_fields_set`
    tells a step that left the field out from one that said `false`.
    """
    if strategy not in ("direct-pr", "integrate"):
        return
    by_id = {spec.step_id: spec for spec in step_specs}
    verdict_sources = {spec.when_step for spec in step_specs if spec.when_step}
    for step in steps:
        spec = by_id.get(step.step_id)
        if spec is None or "allow_empty_diff" in step.model_fields_set:
            continue
        if not _opens_pull_request(
            spec, strategy=strategy, readers=readers,
            integrator_step_id=integrator_step_id,
            verdict_sources=verdict_sources, rereview=rereview,
        ):
            continue
        if says_it_changes_nothing((step.input or {}).get(PROMPT_INPUT_KEY)):
            refuse(StepDeclaresNoChange(
                NO_CHANGE_UNDECLARED_MESSAGE,
                detail={"step_id": step.step_id, "field": "allow_empty_diff"},
            ))
