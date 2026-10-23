"""`ChildPath.pushed_tip`: the public read of the tip this attempt's last
`git_push_branch` left on a branch (GITTOOLS-FOLLOW, part of #1188).

`lifecycle.Worker._adopt_tool_pushed_tip` used to reach past ChildPath's
public surface for the relay (`getattr(self.children, "_git", None)`). It now
reads `ChildPath.pushed_tip(branch)`, beside `rebased_onto`, and this file
holds that read: the relay's sha when it recorded one, None without a relay,
for a branch it never answered, and for anything that is not a sha. Which
branches the relay answers for is `GitTools.pushed_tip`'s, in test_gittools.py.
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace

from agent_worker import children as children_mod


def _path() -> children_mod.ChildPath:
    cfg = SimpleNamespace(swarm_api_url="", swarm_api_audience="", child_nonce="")
    return children_mod.ChildPath(cfg, logger=SimpleNamespace(), memory=None)


def _relay(tips: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(pushed_tip=tips.get)


def test_the_recorded_tip_is_answered():
    path = _path()
    tip = secrets.token_hex(20)
    path._git = _relay({"swarm/task_a": tip})

    # MUTATION: return None unconditionally (or drop the method) and the
    # publish never adopts the tool's push: the 2026-10-23 publish_failed.
    assert path.pushed_tip("swarm/task_a") == tip


def test_no_relay_answers_none():
    # MUTATION: read `self._git.pushed_tip` without the None check and this
    # raises AttributeError on an attempt that has no git tools.
    assert _path().pushed_tip("swarm/task_a") is None


def test_an_unknown_branch_answers_none():
    path = _path()
    path._git = _relay({"swarm/task_a": secrets.token_hex(20)})

    # MUTATION: answer the relay's only tip whatever the branch and the
    # publish would lease another branch on this one's tip.
    assert path.pushed_tip("swarm/task_b") is None


def test_a_relay_without_the_read_or_with_a_non_sha_answers_none():
    path = _path()
    path._git = SimpleNamespace(rebased_onto=secrets.token_hex(20))
    # MUTATION: call `self._git.pushed_tip` unguarded and a relay that lacks
    # it raises instead of answering None.
    assert path.pushed_tip("swarm/task_a") is None
    path._git = _relay({"swarm/task_a": ""})
    # MUTATION: pass the relay's answer through unchecked and an empty string
    # (or any non-str) reaches the publish as a tip.
    assert path.pushed_tip("swarm/task_a") is None
