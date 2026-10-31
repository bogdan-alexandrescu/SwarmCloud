"""`/sc` is the one front door: a verb from `$ARGUMENTS` (owner decision, 2026-10-01).

* no verb -- today's cluster state, unchanged;
* `status <wf_id|task_id>` -- one status read;
* `attach <wf_id>` -- runs `/sc:swarmcloud` with `{attach: <wf_id>}`; submits
  nothing;
* `run <spec path|JSON>` -- loads the spec and runs `/sc:swarmcloud` with the
  launch reply's `args` as given, `spec_handle` included, never a bare path
  and never args the session builds itself (a retyped spec came back altered
  and was refused, SC-LAUNCH-SPEC-2, #2034), and SAYS it is about to submit
  work before it does.

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


def test_run_launches_with_the_replys_args_and_its_spec_handle():
    """Step 3 hands the script the launch reply's `args` as given, so the run's
    submit row gets `spec_handle` and never the spec text (SC-LAUNCH-SPEC-2,
    #2034: a session that built `{spec, spec_path}` itself launched with no
    handle, and the submitting agent retyped the spec)."""
    _, body, _ = _skill()
    run = _section(body, "### `run <spec path|JSON>`")
    # MUTATION: tell step 3 to run the script with "the spec OBJECT as its args" again -> fails.
    assert "the reply's `args` AS GIVEN" in run, "step 3 must run the script with the launch reply's args"
    # MUTATION: drop `spec_handle` from the args step 3 lists, or list the
    # spec beside it again (#2875: a 90 KB spec's reply was too long) -> fails.
    assert "`{spec_handle, details, rows, titled_copy}` -- never the spec" in run
    assert "`{spec, spec_handle" not in run
    # MUTATION: delete "Never drop `spec_handle`" -> fails.
    assert "Never drop `spec_handle`" in run
    # MUTATION: delete "never pass a bare path" -> fails.
    assert "never pass a bare path" in run
    # MUTATION: restore "the args are `{spec: <the spec object>, spec_path: ...}`" -> fails.
    assert "spec_path: \"<its absolute path>\"}`;" not in run, "step 3 still builds {spec, spec_path} by hand"
    # MUTATION: restore "Run that script with the spec OBJECT as its args" -> fails.
    assert "spec OBJECT" not in run, "step 3 still tells the session to pass the spec object it built"
    # MUTATION: delete "is refused, not submitted" -> fails: the digest check stays.
    assert "is refused, not submitted" in run


def test_a_titled_launch_runs_the_copy_with_the_replys_args():
    """The launch paragraph under "Every run is titled after its workflow" names
    the reply's `args` as given, `spec_handle` included, instead of args the
    session builds."""
    _, _, flat = _skill()
    at = flat.index("then launch THAT copy with the Workflow tool")
    launch = flat[at: flat.index("### `attach <wf_id>`", at)]
    # MUTATION: write `args: ...` back in place of the reply's args -> fails.
    assert "args: <the reply's `args`, as given>" in launch, launch
    # MUTATION: drop "`spec_handle` included" -> fails.
    assert "`spec_handle` included" in launch, launch
    # MUTATION: write "with the same args" back for the by-name fallback -> fails.
    assert "with the same args" not in launch, launch


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
