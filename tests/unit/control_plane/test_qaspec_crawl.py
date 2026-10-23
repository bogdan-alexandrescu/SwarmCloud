"""Capture steps that discover while they capture, as the renderer writes them
(docs/design/qa-environments.md §3.1, §3.5, §10; lane QAENV-RUN of §8; BGD's
Q3 (a) and the `found_per_step` answer of 2026-10-23; part of #2136).

`qaspec.render` makes every capture step a crawling one (`swarm_qa capture
--crawl`, lane QAENV-CRAWL). Held here, pure:

  * every capture prompt carries the WHOLE run's stored list, the same in
    every step, and no path a crawl found: the list is swarm-api's, from the
    target, and the step's share is `--group`, `--step` and `--steps`;
  * `found_per_step` is 8 (BGD, §10), at most 8, cut so the leg's stored plus
    `G x found_per_step` stays within 60 (§3.1);
  * a target with `discover` and no screens renders `discover.slots` crawling
    steps (default 4, at most 8) instead of refusing `qa_target_no_screens`;
    a target with neither is still refused;
  * a crawling capture step's timeout is 2,700 s;
  * the report prompt carries §3.5's Coverage section for its leg;
  * the sign-in note names that leg's start clicks only.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from swarm_mcp.profiles import check_spec_model_classes, spec_without_model_classes
from swarm_mcp.workflows import read_spec

from swarm_api import qaspec


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_3f9a",
        "revision": 4,
        "name": "Pricing site",
        "tenant_id": "tenant-a",
        "base_url": "https://app.example.com",
        "repository": "https://github.com/example-org/pricing-site",
        "sign_in": {"kind": "none"},
        "screens": [{"id": "home", "path": "/"}, {"id": "pricing", "path": "/pricing"}],
        "flows": [],
        "environment": "production",
    }
    target.update(over)
    return target


def _screens(n: int) -> list[dict[str, Any]]:
    return [{"id": f"s{i}", "path": f"/page/{i}"} for i in range(n)]


def _captures(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in spec["steps"] if s["step_id"].startswith("capture-")]


def _route_json(prompt: str) -> dict[str, Any]:
    (block,) = re.findall(r"```json\n(.*?)\n```", prompt, flags=re.S)
    return json.loads(block)


def _flag(prompt: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)} (\S+)", prompt)
    assert match, f"{name} is not in the capture command"
    return match.group(1)


# --------------------------------------------------------------------------
# The stored list, whole, in every step; no found path
# --------------------------------------------------------------------------


def test_every_capture_prompt_carries_the_whole_runs_stored_list() -> None:
    target = _target(screens=_screens(20))
    spec = qaspec.render(target)
    captures = _captures(spec)
    assert [s["step_id"] for s in captures] == ["capture-g1", "capture-g2", "capture-g3"]
    whole = qaspec.route_list(qaspec.group_screens(_screens(20)))
    for k, step in enumerate(captures, 1):
        # MUTATION: write only the step's own group into its prompt (today's
        # `route_list([(group, members)])`) and steps 1..3 each hold a third.
        assert _route_json(step["prompt"]) == whole, step["step_id"]
        # MUTATION: drop `--crawl` from the capture command and the step only captures.
        assert " --crawl " in step["prompt"]
        # MUTATION: number every step 1, or give `--steps` the screen count, and
        # the owner rule hands two steps one route.
        assert f"--group g{k} " in step["prompt"]
        assert _flag(step["prompt"], "--step") == str(k)
        assert _flag(step["prompt"], "--steps") == "3"


def test_no_capture_prompt_carries_a_path_another_lacks() -> None:
    """The stored list is swarm-api's, from the target: no step is told of a
    route another step is not, so no found path travels in a prompt (§3.1)."""
    spec = qaspec.render(_target(screens=_screens(20), discovered=[{"id": "found", "path": "/found-it"}]))
    prompts = [s["prompt"] for s in _captures(spec)]
    normal = [re.sub(r"\bg[1-3]\b", "gK", re.sub(r"--step \d", "--step K", p)) for p in prompts]
    # MUTATION: hand each step a found path of its own and the prompts differ beyond their numbers.
    assert len(set(normal)) == 1
    # A target's `discovered` is not read while it lists `screens`.
    # MUTATION: merge `discovered` into a run of a target that lists screens and this fails.
    assert "/found-it" not in json.dumps(spec)


def test_the_route_list_is_written_once_per_step_and_names_the_run_not_the_group() -> None:
    spec = qaspec.render(_target(screens=_screens(9)))
    for step in _captures(spec):
        group = step["step_id"].removeprefix("capture-")
        assert f"/tmp/swarm-qa/routes-{group}.json" in step["prompt"]
        assert _flag(step["prompt"], "--routes") == f"/tmp/swarm-qa/routes-{group}.json"


# --------------------------------------------------------------------------
# found_per_step
# --------------------------------------------------------------------------


def test_the_capture_prompts_found_per_step_is_8() -> None:
    # BGD, 2026-10-23 (qa-environments.md §10): "8 by default", not the design's 4.
    # MUTATION: set FOUND_PER_STEP = 4 (the design's recommendation) and both of these fail.
    assert qaspec.FOUND_PER_STEP == 8
    for step in _captures(qaspec.render(_target())):
        assert _flag(step["prompt"], "--found-per-step") == "8"


@pytest.mark.parametrize("n, groups, cap", [(60, 8, 0), (44, 6, 2), (20, 3, 8), (52, 7, 1)])
def test_found_per_step_is_cut_so_the_leg_stays_within_sixty(n: int, groups: int, cap: int) -> None:
    captures = _captures(qaspec.render(_target(screens=_screens(n))))
    assert len(captures) == groups
    # MUTATION: pass `found_per_step` uncut and 60 stored routes plus 8 x 8 new pass MAX_SCREENS.
    assert {_flag(s["prompt"], "--found-per-step") for s in captures} == {str(cap)}
    assert n + groups * cap <= qaspec.MAX_SCREENS


def test_a_targets_discover_sets_found_per_step_up_to_8() -> None:
    discover = {"max_pages": 30, "max_depth": 2, "found_per_step": 3}
    captures = _captures(qaspec.render(_target(discover=discover)))
    # MUTATION: ignore `discover.found_per_step` and this is 8.
    assert {_flag(s["prompt"], "--found-per-step") for s in captures} == {"3"}
    for bad in (9, -1, "8", True):
        with pytest.raises(qaspec.QaSpecError) as err:
            qaspec.render(_target(discover={**discover, "found_per_step": bad}))
        # MUTATION: drop the 0..8 bound and 9 renders.
        assert err.value.code == "qa_target_invalid"
        # The registration's check reaches it too.
        with pytest.raises(qaspec.QaSpecError):
            qaspec.render_discovery(_target(discover={**discover, "found_per_step": bad}))


# --------------------------------------------------------------------------
# The crawl's own arguments
# --------------------------------------------------------------------------


def test_the_crawl_walks_from_the_base_path_within_the_targets_bounds() -> None:
    target = _target(
        base_url="https://app.example.com/studio",
        screens=[{"id": "home", "path": "/studio/"}],
        discover={"max_pages": 30, "max_depth": 2},
        exclusions={"paths": ["/studio/admin/*"], "controls": ["archive project"]},
        rate={"navigation_ms": 3000, "requests_per_s": 5},
    )
    (step,) = _captures(qaspec.render(target))
    prompt = step["prompt"]
    # Capture loads at the origin (QA-ORIGIN); the walk starts at the base's path.
    assert "--base https://app.example.com --presets" in prompt
    # MUTATION: start the walk at `/` whatever the base and it walks the whole origin.
    assert _flag(prompt, "--start-path") == "/studio/"
    # MUTATION: pass swarm_qa's own defaults rather than the target's bounds and these fail.
    assert _flag(prompt, "--max-pages") == "30" and _flag(prompt, "--max-depth") == "2"
    assert _flag(prompt, "--crawl-seconds") == "600"
    # MUTATION: leave the target's exclusions out of the walk and it loads /studio/admin/x.
    assert "--exclude /studio/admin/*" in prompt.replace("'", "")
    assert "--exclude /logout" in prompt.replace("'", "")
    assert "--exclude-control 'archive project'" in prompt
    # MUTATION: drop the leg's rate and the walk navigates at swarm_qa's 2 s.
    assert _flag(prompt, "--navigation-ms") == "3000"


def test_a_target_without_discover_crawls_at_the_tools_bounds() -> None:
    (step,) = _captures(qaspec.render(_target()))
    assert _flag(step["prompt"], "--start-path") == "/"
    assert _flag(step["prompt"], "--max-pages") == str(qaspec.MAX_DISCOVER_PAGES)
    assert _flag(step["prompt"], "--max-depth") == str(qaspec.MAX_DISCOVER_DEPTH)
    assert "found.json" in step["prompt"]


def test_a_crawling_capture_steps_timeout_is_2700_seconds() -> None:
    spec = qaspec.render(_target(screens=_screens(20)))
    # §3.1: "the capture timeout grows from 1,800 s to 2,700 s for a crawling step".
    # MUTATION: leave `timeout=1800` on the capture step and this fails.
    assert {s["timeout_seconds"] for s in _captures(spec)} == {2700}
    # The review and report steps are unchanged.
    steps = {s["step_id"]: s for s in spec["steps"]}
    assert steps["review-g1"]["timeout_seconds"] == 3600 and steps["report"]["timeout_seconds"] == 3600


# --------------------------------------------------------------------------
# A run with no stored screens (§3.1, "from nothing but the base URL")
# --------------------------------------------------------------------------


def test_a_discovering_target_with_no_screens_renders_slots_crawling_steps() -> None:
    target = _target(screens=None, discover={"max_pages": 30, "max_depth": 2})
    # MUTATION: keep refusing `qa_target_no_screens` in `_screens` and this raises.
    spec = qaspec.render(target)
    captures = _captures(spec)
    # Default 4 slots.
    assert [s["step_id"] for s in captures] == [f"capture-g{k}" for k in range(1, 5)]
    for k, step in enumerate(captures, 1):
        # No stored route: the run's list is empty and every step starts at the base.
        assert _route_json(step["prompt"]) == {"format": 1, "groups": {}}
        assert _flag(step["prompt"], "--step") == str(k) and _flag(step["prompt"], "--steps") == "4"
        assert _flag(step["prompt"], "--found-per-step") == "8"
        assert step["timeout_seconds"] == 2700
    steps = {s["step_id"]: s for s in spec["steps"]}
    # Each slot is reviewed and staged for the report, as a stored group is.
    assert all(f"review-g{k}" in steps for k in range(1, 5))
    assert set(steps["report"]["input_from"]) == {f"{kind}-g{k}" for kind in ("capture", "review")
                                                  for k in range(1, 5)}
    built = read_spec(spec_without_model_classes(spec))
    check_spec_model_classes(spec)
    assert [s["step_id"] for s in built["steps"]] == [s["step_id"] for s in spec["steps"]]


@pytest.mark.parametrize("slots", [1, 6, 8])
def test_discover_slots_sets_the_number_of_crawling_steps(slots: int) -> None:
    target = _target(screens=None, discover={"max_pages": 30, "max_depth": 2, "slots": slots})
    captures = _captures(qaspec.render(target))
    # MUTATION: ignore `discover.slots` and this is 4.
    assert len(captures) == slots
    assert {_flag(s["prompt"], "--steps") for s in captures} == {str(slots)}


@pytest.mark.parametrize("slots", [0, 9, "4", True])
def test_discover_slots_is_one_to_eight(slots: Any) -> None:
    target = _target(screens=None, discover={"max_pages": 30, "max_depth": 2, "slots": slots})
    # MUTATION: drop the 1..8 bound and 9 renders nine capture steps.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(target)
    assert err.value.code == "qa_target_invalid"
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render_discovery(target)


def test_a_stored_discovered_list_is_used_before_the_slots() -> None:
    target = _target(screens=None, discover={"max_pages": 30, "max_depth": 2, "slots": 6},
                     discovered=_screens(3))
    (step,) = _captures(qaspec.render(target))
    # MUTATION: render slots whenever `discover` is set and this is six empty steps.
    assert [r["path"] for r in _route_json(step["prompt"])["groups"]["g1"]] == ["/page/0", "/page/1", "/page/2"]


def test_a_target_with_neither_screens_nor_discover_is_still_refused() -> None:
    # MUTATION: render default slots for any target without screens and this renders.
    with pytest.raises(qaspec.QaSpecError) as err:
        qaspec.render(_target(screens=None))
    assert err.value.code == "qa_target_no_screens"
    # A subset of screens names stored screens: a no-screen run has none to name.
    with pytest.raises(qaspec.QaSpecError):
        qaspec.render(_target(screens=None, discover={"max_pages": 30, "max_depth": 2}), screens=["home"])


# --------------------------------------------------------------------------
# The report's Coverage section (§3.5)
# --------------------------------------------------------------------------


def test_the_report_prompt_carries_the_legs_coverage_section() -> None:
    spec = qaspec.render(qaspec.leg(_target(screens=_screens(9))))
    report = {s["step_id"]: s for s in spec["steps"]}["report"]["prompt"]
    # MUTATION: drop the Coverage sentences from `_report_prompt` and every one of these fails.
    assert 'section headed "Coverage (production)"' in report
    assert "found.json" in report
    for part in ("stored routes captured", "new routes captured", "over the cap",
                 "not reached by their owner", "stopped_by"):
        assert part in report, part
    # A crawl cut short is said plainly: a missing route is then no evidence it is gone.
    assert "`time` or `pages`" in report
    # A missing capture tar: the expected count is the stored routes', and the found count is unknown.
    assert "found routes' count is unknown" in report


def test_the_coverage_section_names_the_leg_it_reports() -> None:
    target = {
        "id": "qat_3f9a", "revision": 4, "name": "Studio", "tenant_id": "tenant-a",
        "repository": "https://github.com/example-org/studio", "screens": [{"id": "home", "path": "/"}],
        "environments": [
            {"name": "staging", "base_url": "https://staging.example.com", "environment": "non_production"},
            {"name": "production", "base_url": "https://studio.example.com"},
        ],
    }
    report = {s["step_id"]: s for s in qaspec.render(qaspec.leg(target, "staging"))["steps"]}["report"]["prompt"]
    # MUTATION: name the coverage section after the target's first entry and this is production.
    assert '"Coverage (staging)"' in report and "Coverage (production)" not in report


# --------------------------------------------------------------------------
# The sign-in note in a crawling step names its own leg's clicks
# --------------------------------------------------------------------------


def test_each_crawling_steps_sign_in_note_names_that_legs_start_clicks_only() -> None:
    def login(clicks: list[dict[str, str]]) -> dict[str, Any]:
        return {"kind": "own_login", "login_path": "/", "signed_in_selector": "nav .me", "start_clicks": clicks}

    target = {
        "id": "qat_3f9a", "revision": 4, "name": "Studio", "tenant_id": "tenant-a",
        "repository": "https://github.com/example-org/studio",
        "screens": None, "discover": {"max_pages": 30, "max_depth": 2},
        "environments": [
            {"name": "staging", "base_url": "https://studio.example.com", "environment": "non_production",
             "sign_in": login([{"text": "Staging toggle"}, {"text": "Sign in"}])},
            {"name": "production", "base_url": "https://studio.example.com",
             "sign_in": login([{"text": "Production toggle"}, {"text": "Sign in"}])},
        ],
    }
    for name, mine, theirs in (("staging", "Staging toggle", "Production toggle"),
                               ("production", "Production toggle", "Staging toggle")):
        captures = _captures(qaspec.render(qaspec.leg(target, name)))
        assert len(captures) == 4
        for step in captures:
            # MUTATION: render the sign-in note from the target's first entry and production's
            # steps are told to click the staging toggle.
            assert f"click '{mine}' then 'Sign in'" in step["prompt"]
            assert theirs not in step["prompt"]
