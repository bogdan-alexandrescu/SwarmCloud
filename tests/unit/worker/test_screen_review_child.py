"""TOKEN-RENDER (BGD 2026-10-27, #2524 item 2, "Yes, Sonnet child per screen").

A review, or an implement step that renders, judges its frames in ONE SONNET
CHILD PER SCREEN: the child renders that screen in both themes and both
viewports, reads its own shots against the method's checklist, and returns
its findings as TEXT only. The parent keeps those texts, plus the crop of any
frame over the compare threshold, so no screenshot sits in its context for
the rest of the lane (up to 15.7 MB of tool results in one lane before this).

No cloud, no emulator: the request is built and refused or accepted by the
worker's own local checks, and the spool is prepared on a temporary path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_worker import children as children_mod
from swarm_common.profiles import RUNNER_PROFILES

FRAMES = (
    "design/frames/settings--desktop--light.png",
    "design/frames/settings--desktop--dark.png",
    "design/frames/settings--phone-390--light.png",
    "design/frames/settings--phone-390--dark.png",
)


def _request(**overrides):
    kwargs = dict(frames=FRAMES, url_path="/settings", repository_ref="swarm/task_abc",
                  checklist="spacing, colour tokens, clipped text, contrast in dark")
    kwargs.update(overrides)
    return children_mod.screen_review_request("settings", **kwargs)


def test_a_screens_child_runs_on_sonnet_on_the_browser_profile_at_the_change():
    request = _request()
    assert request["model_class"] == "sonnet"
    assert request["runner_profile"] == "claude-code-browser"
    assert RUNNER_PROFILES[request["runner_profile"]].live_browser
    assert request["repository_ref"] == "swarm/task_abc"
    assert request["request_id"] == "screen-settings"
    assert children_mod.REQUEST_ID.fullmatch(request["request_id"])
    # Nothing a request may not carry.
    assert set(request) - {"request_id"} <= children_mod.CHILD_FIELDS


def test_the_childs_prompt_asks_for_text_only_per_frame_theme_and_viewport():
    prompt = _request()["input"]["prompt"]
    assert "TEXT only" in prompt
    assert "no image" in prompt
    assert children_mod.SCREEN_VERDICT_NAME in prompt
    for frame in FRAMES:
        assert frame in prompt
    for viewport in ("desktop", "phone-390"):
        assert f"swarm-handoff device {viewport}" in prompt
    for theme in ("light", "dark"):
        assert f"swarm-handoff theme {theme}" in prompt
    assert "swarm-handoff compare" in prompt
    assert "spacing, colour tokens, clipped text, contrast in dark" in prompt
    assert '"ok"' in prompt
    assert "/settings" in prompt


def test_a_screen_name_that_is_not_a_file_name_is_refused():
    with pytest.raises(ValueError):
        children_mod.screen_review_request("../etc", frames=FRAMES, url_path="/", repository_ref="main",
                                           checklist="x")
    with pytest.raises(ValueError):
        _request(repository_ref="")


def test_the_worker_accepts_the_screen_request_as_written(tmp_path):
    path = _bare_path(tmp_path)
    request = _request()
    spool = children_mod.ChildPath.spool(tmp_path / "work") / "requests" / f"{request['request_id']}.json"
    spool.write_text(json.dumps(request))
    accepted = path._local_refusal(spool, request["request_id"])
    assert not isinstance(accepted, children_mod.Refusal), accepted
    assert accepted["model_class"] == "sonnet"


def test_the_spool_carries_the_screen_review_guide(tmp_path):
    _bare_path(tmp_path)
    guide = (children_mod.ChildPath.spool(tmp_path / "work") / children_mod.SCREEN_REVIEW_GUIDE_NAME).read_text()
    assert guide == children_mod.SCREEN_REVIEW_GUIDE
    assert '"model_class": "sonnet"' in guide
    # The parent reads only the crop of a frame over the threshold, and
    # review.md names every frame and the child that read it.
    assert "crop.png" in guide and "only" in guide
    assert "review.md" in guide and "verdict.json" in guide
    assert "every frame" in guide


def _bare_path(tmp_path):
    cfg = SimpleNamespace(
        child_submit_retry_seconds=10, max_child_requests_per_tick=4,
        max_child_request_bytes=262144, tenant_id="tnt_screen", attempt_id="att_1",
        lease_id="lease_1", generation=1, task_id="task_1", swarm_api_url=None,
        swarm_api_audience=None, child_nonce="screen-review-test-nonce",
    )
    path = children_mod.ChildPath(cfg, logger=SimpleNamespace(info=lambda *a, **k: None,
                                                              warning=lambda *a, **k: None,
                                                              error=lambda *a, **k: None),
                                  memory=None, api=None)
    path._key = children_mod.AttemptKey()
    path.unavailable = None
    path.prepare(tmp_path / "work")
    return path
