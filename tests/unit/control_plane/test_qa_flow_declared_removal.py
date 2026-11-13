"""A writing QA flow whose writes name a removal may click Remove on what it created (#3977).

the owner, 2026-11-13, "a mutating QA flow may click a Remove control only when
its declared writes name that removal AND the item was created by the same flow
run. Every other Remove or Delete click stays refused, including on any item the
run did not create" (docs/design/qa-environments.md §6, "Declared removal"):

  * a mutating flow whose `writes` names a removal (remove, removes, removed,
    removing, removal; whole words, any case) gets a brief whose never-click
    list drops 'remove', and one exception: Remove, and the Undo it shows, only
    on the item this same run created earlier, and nothing it staged left behind;
  * 'delete' and every other default or target word stays refused, and so does
    'remove' when the target's own `exclusions.controls` names it;
  * a read-only flow, a flow batch, a screen capture and the crawl are unchanged;
  * the flow's json records what it removed, and that this run created it,
    beside `this_flow_wrote`.

No credentials, no network: the briefs are rendered through `qaspec.render`.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from swarm_api import qaspec

REMOVES = "Stages and removes one web page on the draft."
WRITES = "One order for the cheapest plan, in the shop's test catalogue."
EXCEPTION = "may click a Remove control, and the Undo it shows, only on the item this same run created earlier"


def _flow(**over: Any) -> dict[str, Any]:
    return {"id": "pages", "goal": "stage a page, remove it, undo", "start_path": "/draft", "mutating": True,
            "writes": REMOVES, **over}


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_0123456789abcdef", "name": "Site", "base_url": "https://site.example.com",
        "repository": "https://github.com/example-org/site", "environment": "non_production",
        "screens": [{"id": "home", "path": "/"}], "flows": [_flow()],
        "exclusions": {"controls": ["archive"]},
    }
    target.update(over)
    return target


def _steps(spec: dict[str, Any]) -> dict[str, str]:
    return {s["step_id"]: s.get("prompt", "") for s in spec["steps"]}


def _never_click(prompt: str) -> list[str]:
    match = re.search(r"Never click a control whose text names any of: (.*?)\. ", prompt)
    assert match, prompt
    return match.group(1).split(", ")


# --------------------------------------------------------------------------
# The word
# --------------------------------------------------------------------------


@pytest.mark.parametrize("writes", [
    REMOVES, "Removal of the page it staged.", "Adds a tag, then REMOVED it.", "Removing one draft note.",
    "One page; remove it after.",
])
def test_a_writes_naming_the_remove_family_declares_a_removal(writes) -> None:
    assert qaspec.declares_removal(_flow(writes=writes)) is True


@pytest.mark.parametrize("writes", [
    WRITES, "Deletes one draft page.", "Marks one removable banner as seen.", "A remover tool setting.",
    "   ", None,
])
def test_a_writes_naming_no_removal_declares_none(writes) -> None:
    # MUTATION: match `remov` as a substring and 'removable' / 'remover' count.
    assert qaspec.declares_removal(_flow(writes=writes)) is False


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------


def test_a_removal_flow_may_click_remove_on_what_its_run_created_and_nothing_else() -> None:
    brief = _steps(qaspec.render(_target(), file="none"))["flow-pages"]
    controls = _never_click(brief)
    # MUTATION: leave `removal` off `_safety` in `_flow_prompt` and 'remove' is still refused.
    assert "remove" not in controls
    assert controls == [c for c in qaspec.DEFAULT_EXCLUDED_CONTROLS if c != "remove"] + ["archive"]
    assert "delete" in controls
    assert EXCEPTION in brief
    assert "never clicks Remove or Delete on anything this run did not create" in brief
    assert "After it sees Undo, it leaves the item removed, or removes it, so nothing it staged is left behind" \
        in brief
    # The writes sentence is still the limit of what it may write.
    assert f'which is the limit of what it may write: "{REMOVES}"' in brief


def test_a_writing_flow_whose_writes_name_no_removal_still_refuses_remove() -> None:
    brief = _steps(qaspec.render(_target(flows=[_flow(writes=WRITES)]), file="none"))["flow-pages"]
    assert "remove" in _never_click(brief)
    assert EXCEPTION not in brief
    assert "this_flow_removed" not in brief


def test_a_writes_naming_delete_lifts_neither_delete_nor_remove() -> None:
    brief = _steps(qaspec.render(_target(flows=[_flow(writes="Deletes one draft page.")]),
                                 file="none"))["flow-pages"]
    controls = _never_click(brief)
    assert "delete" in controls and "remove" in controls
    assert EXCEPTION not in brief


@pytest.mark.parametrize("word", ["remove", "Remove", "REMOVE"])
def test_a_target_that_excludes_remove_itself_keeps_refusing_it(word) -> None:
    target = _target(exclusions={"controls": [word]})
    brief = _steps(qaspec.render(target, file="none"))["flow-pages"]
    # MUTATION: drop the target check from `_safety` and the target's own word is lifted.
    assert {"remove", word} & set(_never_click(brief))
    assert EXCEPTION not in brief
    assert "this_flow_removed" not in brief


# --------------------------------------------------------------------------
# What is unchanged
# --------------------------------------------------------------------------


def test_a_read_only_flow_a_batch_a_capture_and_the_crawl_still_refuse_remove() -> None:
    target = _target(flows=[_flow(), {"id": "look", "goal": "find pricing", "start_path": "/",
                                      "writes": "Nothing; removes nothing."}])
    steps = _steps(qaspec.render(target, file="none"))
    for step_id in ("flows-1", "capture-g1"):
        assert "remove" in _never_click(steps[step_id]), step_id
        assert EXCEPTION not in steps[step_id], step_id
    flags = qaspec._crawl_flags(target, "https://site.example.com", step=1, steps=1, found_per_step=10,
                                bounds={"max_pages": 10, "max_depth": 2})
    assert "--exclude-control remove" in flags
    assert "remove" in qaspec.DEFAULT_EXCLUDED_CONTROLS


def test_writing_flows_off_renders_the_flow_read_only_with_no_exception() -> None:
    view = qaspec.leg(_target(), writing_flows=False)
    steps = _steps(qaspec.render(view, file="none"))
    assert "flow-pages" not in steps
    assert "remove" in _never_click(steps["flows-1"])
    assert all(EXCEPTION not in p and "this_flow_removed" not in p for p in steps.values())


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_a_removal_flow_records_what_it_removed_and_that_its_run_created_it() -> None:
    brief = _steps(qaspec.render(_target(), file="none"))["flow-pages"]
    assert f"`this_flow_wrote` to this sentence, word for word: {REMOVES}" in brief
    # MUTATION: drop the removal case from `_wrote_line` and the json says nothing it removed.
    assert ("`this_flow_removed` to what this flow removed, each as "
            '{"item": "<what it removed>", "created_by_this_run": true}') in brief
