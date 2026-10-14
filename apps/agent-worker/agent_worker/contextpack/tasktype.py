"""Which of the six pipelines a step gets (§4.5, "How the task type is known").

By the step's role, in this order, never by a model:

1. the stager's `stage.json` `type`, when it is one of `TASK_TYPES`: the
   stager knows the step's role from the signed spec, which this process
   does not read;
2. the platform's own templates, recognisable from what the worker staged
   and how the control plane worded the prompt:
     * plan -- the planner's lead, `issueruns.planner_prompt`
       ("Plan the work for GitHub issue ...");
     * fix -- a CI fix round's FAILING CHECKS fence (`issueci.ci_fix_continuation`),
       or a staged `verdict.json` (the issue run's fix step, `issueruns.compile_plan`);
     * review -- a staged `swarm-work.patch` and a prompt asking for
       `verdict.json` (the issue run's review step);
3. the brief's lead verb, for an ad-hoc task: "write/add tests" is test-writing,
   "design/architect" is design;
4. otherwise implement, which §4.5 makes the default for a task with no role.

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


def staged_names(payload: Mapping[str, Any]) -> set[str]:
    """The base names of what the worker staged into `work/` for this step."""
    names: set[str] = set()
    for entry in payload.get("staged_inputs") or ():
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(raw, str) and raw:
            names.add(posixpath.basename(raw))
    return names


def classify(payload: Mapping[str, Any], work_dir: Path,
             stage: Mapping[str, Any] | None = None) -> str:
    if stage is None:
        stage = read_stage(Path(work_dir) / CONTEXT_DIR_NAME)
    declared = stage.get("type")
    if isinstance(declared, str) and declared in TASK_TYPES:
        return declared
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
    staged = staged_names(payload)
    if prompt.lstrip().startswith(PLANNER_LEAD):
        return "plan"
    if FAILING_CHECKS.search(prompt) or VERDICT_FILE in staged:
        return "fix"
    if PATCH_FILE in staged and VERDICT_FILE in prompt:
        return "review"
    if _TEST_LEAD.search(prompt):
        return "test"
    if _DESIGN_LEAD.search(prompt):
        return "design"
    return "implement"
