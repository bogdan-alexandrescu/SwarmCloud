"""`/sc` is the one front door: a verb from `$ARGUMENTS` (owner decision, 2026-10-01).

* no verb -- today's cluster state, unchanged;
* `status <wf_id|task_id>` -- one status read;
* `attach <wf_id>` -- runs `/sc:swarmcloud` with `{attach: <wf_id>}`; submits
  nothing;
* `run <spec path|JSON>` -- loads the spec and runs `/sc:swarmcloud` with the
  spec OBJECT as its args, never a bare path (a path made a haiku agent
  retype the spec), and SAYS it is about to submit work before it does.

The skill's rule changes from "reads, never writes" to "reads only, except
`run`, which submits and says so first". Read from the SKILL.md text, because
prose is what the session follows.
"""

from __future__ import annotations

import re

from test_plugin_skills import _PLUGIN, _granted, _load

_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"


def _skill() -> tuple[dict, str, str]:
    fields, body = _load(_SKILL)
    return fields, body, " ".join(body.split())


def _section(body: str, heading: str) -> str:
    """The text under a `### <heading>` up to the next heading of level 2 or 3."""
    at = body.index(heading)
    rest = body[at + len(heading):]
    stop = re.search(r"^#{2,3} ", rest, flags=re.MULTILINE)
    return " ".join((rest[: stop.start()] if stop else rest).split())


def test_every_verb_is_documented():
    fields, body, flat = _skill()
    for verb in ("`status <wf_id|task_id>`", "`attach <wf_id>`", "`run <spec path|JSON>`"):
        assert verb in flat, f"the sc skill does not document {verb}"
    assert "no verb" in flat.lower(), "the bare `/sc` must still be the cluster state"
    assert "$ARGUMENTS" in body, "the verb is read from $ARGUMENTS"


def test_the_frontmatter_names_the_verbs():
    fields, _, _ = _skill()
    hint = str(fields.get("argument-hint", ""))
    for verb in ("status", "attach", "run"):
        assert verb in hint, f"argument-hint does not offer {verb}: {hint!r}"
    assert fields.get("arguments"), "the skill must declare `arguments`"
    description = str(fields["description"])
    assert "attach" in description and "run" in description and "submit" in description.lower()


def test_run_passes_the_spec_object_not_the_path():
    _, body, _ = _skill()
    run = _section(body, "### `run <spec path|JSON>`")
    assert "spec OBJECT" in run
    assert "never a bare path" in run.lower() or "never the path" in run.lower()
    assert "{spec: <the spec object>" in run, "the args carry the object itself"


def test_run_says_it_is_about_to_submit_before_it_does():
    _, body, _ = _skill()
    run = _section(body, "### `run <spec path|JSON>`")
    assert "before" in run.lower() and "submit" in run.lower()
    assert "about to submit" in run.lower()


def test_attach_names_no_submit():
    _, body, _ = _skill()
    attach = _section(body, "### `attach <wf_id>`")
    assert "{attach: <wf_id>}" in attach
    assert "submits nothing" in attach.lower()
    assert "swarm_workflow`" not in attach and "SUBMIT" not in attach
    stripped = attach.lower().replace("submits nothing", "")
    assert "submit" not in stripped, f"the attach verb mentions submitting: {attach}"


def test_status_is_one_read():
    fields, body, _ = _skill()
    status = _section(body, "### `status <wf_id|task_id>`")
    assert "swarm_workflow_status" in status and "swarm_status" in status
    assert "one" in status.lower()
    # Read tools only: whatever this skill is granted, it is never a writer.
    granted = _granted(fields["allowed-tools"])
    assert not granted & {"swarm_workflow", "swarm_dispatch", "swarm_cancel", "swarm_workflow_cancel"}


def test_the_rule_is_reads_only_except_run():
    _, _, flat = _skill()
    assert "reads only, except `run`" in flat
    assert "never writes" not in flat.split("## ", 1)[0].lower() or "except `run`" in flat
