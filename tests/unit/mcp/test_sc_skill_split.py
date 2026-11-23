"""The sc skill split and the `/sc:check` command (improvement plan 2026-11-23).

Measured before: plugin/skills/sc/SKILL.md was 82,918 bytes and was injected
whole on every `/sc <verb>` that reached the skill -- every firing of the
watch included, whose prompt was `/sc check` -- about 24,000 fresh tokens a
firing. Now the watch fires `/sc:check`, a command of about a kilobyte whose
reading rules come back in `swarm_check`'s own reply (`how_to_read`, and a
`say` on every flagged item), and the skill is a core of the verb table,
`check`, `watch` and arming, with every other verb's rules in a reference
file only that verb loads.
"""

from __future__ import annotations

import re

from swarm_mcp import check, watch

import sc_skill_text
from test_plugin_skills import _load
from test_sc_watch import Api, _this_session, ago, event, project, run, task  # noqa: F401 - fixtures

_COMMANDS = sc_skill_text.PLUGIN / "commands"
_CHECK_COMMAND = _COMMANDS / "check.md"

#: The owner's targets: the core at most 17 KB, the check command under 2 KB.
CORE_MAX_BYTES = 17 * 1024
CHECK_COMMAND_MAX_BYTES = 2 * 1024
#: The description is in every session's system prompt: it was 3,151 bytes.
DESCRIPTION_MAX_BYTES = 1300


def test_the_core_skill_stays_under_its_size_target():
    size = len(sc_skill_text.CORE.read_bytes())
    # MUTATION: paste a reference file back into the core -> fails.
    assert size <= CORE_MAX_BYTES, f"SKILL.md is {size} bytes, over {CORE_MAX_BYTES}"


def test_the_check_command_stays_under_two_kilobytes():
    size = len(_CHECK_COMMAND.read_bytes())
    assert size < CHECK_COMMAND_MAX_BYTES, f"commands/check.md is {size} bytes"


def test_the_core_keeps_the_verb_table_check_watch_and_arming():
    core = sc_skill_text.CORE.read_text()
    for heading in ("## The verb", "### `check`", "### `watch [interval]`", "### Arming the watch",
                    "### `watch off`"):
        assert heading in core, heading


def test_every_reference_file_is_named_by_the_core_and_every_name_exists():
    core = sc_skill_text.CORE.read_text()
    named = set(re.findall(r"`reference/([a-z-]+\.md)`", core))
    present = {path.name for path in sc_skill_text.reference_files()}
    assert present, "no reference files"
    # MUTATION: add a reference file no verb loads -> it is never read.
    assert present <= named, sorted(present - named)
    assert named <= present, sorted(named - present)


def test_a_moved_section_lives_in_exactly_one_file():
    """Word for word, once: a heading in two files is a rule that can drift."""
    seen: dict[str, str] = {}
    for path in [sc_skill_text.CORE, *sc_skill_text.reference_files()]:
        for line in path.read_text().splitlines():
            if line.startswith("## ") or line.startswith("### "):
                assert line not in seen, f"{line!r} is in {seen.get(line)} and {path.name}"
                seen[line] = path.name


def test_the_frontmatter_grants_each_tool_once_and_the_description_is_short():
    fields, _ = _load(sc_skill_text.CORE)
    tools = fields["allowed-tools"]
    assert len(tools) == len(set(tools)), sorted(t for t in tools if tools.count(t) > 1)
    assert "Read" in tools, "the skill reads its reference files"
    description = fields["description"]
    assert len(description.encode()) <= DESCRIPTION_MAX_BYTES, len(description.encode())
    # What it must still trigger on.
    for phrase in ("/sc [verb]", "quota", "is my TypeSafe key set", "what workflows are running",
                   "work on issue #N", "approve the plan", "check", "watch", "attach --all"):
        assert phrase in description, phrase


def test_the_check_command_says_what_the_old_section_said_in_order():
    text = " ".join(_CHECK_COMMAND.read_text().split())
    order = ["`swarm_check` once", "`how_to_read`", "`flagged` first", "stuck before stale",
             "`awaiting_input`", "`owner_questions`", "`swarm_task_answer`",
             "ONE `swarm_workflow_launch` call", "`new_only: true`", "`arm_watch`", "CronList",
             "`/sc:check` or `/sc check`", "`swarm_check_schedule`", "CronCreate"]
    at = 0
    for piece in order:
        # MUTATION: report owner_questions before flagged -> not found after it.
        found = text.find(piece, at)
        assert found != -1, f"{piece!r} is missing, or out of order"
        at = found + len(piece)


def test_sc_check_routes_to_the_check_command_without_the_skill():
    """A watch armed before 0.5.88 fires `/sc check`: the sc command does the
    check command's steps itself, the same words, and loads no skill."""
    steps = _CHECK_COMMAND.read_text().split("---\n", 2)[2].strip()
    sc = (_COMMANDS / "sc.md").read_text()
    assert steps in sc
    assert "`/sc:check`" in sc and "without loading the\n`sc` skill" in sc
    fields, _ = _load(_COMMANDS / "sc.md")
    assert {"CronList", "CronCreate"} <= set(fields["allowed-tools"])


def test_a_newly_armed_watch_names_the_check_command(project):  # noqa: F811
    """Every way a watch is armed hands CronCreate the new prompt."""
    assert watch.CHECK_PROMPT == "/sc:check"
    reply = watch.tool({"interval": "30m"})
    assert reply["arm_with"]["args"]["prompt"] == "/sc:check"
    assert reply["without_cron"] == "/loop 30m /sc:check"
    assert "`/sc check`" in reply["replace_first"], "an old watch is replaced, not doubled"
    arm = watch.arm_call()
    assert arm is not None and arm["args"]["prompt"] == "/sc:check"
    assert "`/sc:check` or `/sc check`" in arm["unless"]


def test_the_check_reply_carries_its_reading_rules():
    quiet = task("task_quiet", "RUNNING", heartbeat_at=ago(45), heartbeat="read")
    result = run(Api(singles=[quiet], events={"task_quiet": [event("heartbeat", 41)]}))
    assert result["how_to_read"] == check.HOW_TO_READ
    for piece in ("`flagged` first", "STUCK before STALE", "`awaiting_input`", "AskUserQuestion",
                  "swarm_task_answer", "`unchecked` was NOT checked", "nothing stuck or stale in",
                  "`rowless_tasks`", "`attach_with`", "`arm_watch`"):
        assert piece in check.HOW_TO_READ, piece
    (item,) = result["flagged"]
    assert item["next"] == "/sc status task_quiet"
    # MUTATION: drop the rule's note -> the session reports the item bare.
    assert item["say"] == f"[eng] task_quiet · STALE · {item['reason']} -- {check.RULE_SAY['no_heartbeat']}"
