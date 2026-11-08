"""The installer offers Voice it (lane installer-offers-voice-it; part of #2688;
the owner, 2026-11-05): the installer never turned Voice it on, so a yes to its own
speech engine made nothing.

Stage 0 asks whether to turn Voice it on, default no. A no keeps infra's
`voice_it` "off"; a yes renders terraform/infra/voice.tf's three steps in its
order ("off" on the registry pass, "account" on infra-1, "on" from infra-2 with
the swarm-voice image). The own-speech question is nested under it: never asked
without a yes to Voice it.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_install import tfvars
from swarm_install.exitcodes import MET, UNMET
from swarm_install.steps import discover
from swarm_install.steps.infra import (VOICE_IT_POOL, VoiceItChoiceStep, VoiceSpeechChoiceStep,
                                       VoiceSpeechZoneStep)

from .test_infra_steps import ordered_ids
from .test_terraform_runner import FakeCtx, install_state
from .test_voice_speech_tfvars import ComputeFake, _asker, _deps

PASSES = ("registry", "infra-1", "infra-2", "infra-3", "infra-4", "infra-close")
IMAGES = "projects/deeplearning-platform-release/global/images/"


def _digest() -> str:
    return "sha256:" + secrets.token_hex(32)


def _digests(*names: str) -> dict[str, str]:
    return {name: _digest() for name in ("swarm-api", *names)}


def _infra(pass_name: str, **outputs: Any) -> dict:
    return tfvars.infra_inputs(install_state(**outputs), pass_name=pass_name, installer="admin@example.com")


def _said(ctx: Any) -> str:
    return "\n".join(str(event.get("message", "")) for event in ctx.events.record)


# -- the tfvars ---------------------------------------------------------------


def test_no_answer_renders_no_voice_it() -> None:
    assert "voice_it" not in _infra("infra-2", image_digests=_digests("swarm-voice"))


def test_no_keeps_voice_it_off_on_every_infra_pass() -> None:
    for pass_name in PASSES:
        inputs = _infra(pass_name, voice_it={"enabled": False}, image_digests=_digests("swarm-voice"))
        assert inputs["voice_it"] == "off", pass_name


def test_yes_renders_voice_tf_steps_in_order_with_the_swarm_voice_image() -> None:
    digests = _digests("swarm-voice")
    rendered = {p: _infra(p, voice_it={"enabled": True}, image_digests=digests) for p in PASSES}
    assert [rendered[p]["voice_it"] for p in PASSES] == ["off", "account", "on", "on", "on", "on"]
    for pass_name in PASSES[2:]:
        assert rendered[pass_name]["image_refs"]["swarm-voice"].endswith("/swarm-voice@" + digests["swarm-voice"])


def test_yes_without_the_swarm_voice_image_names_the_step_that_copies_it() -> None:
    assert _infra("infra-1", voice_it={"enabled": True}, image_digests=_digests())["voice_it"] == "account"
    with pytest.raises(LookupError, match="images.copy"):
        _infra("infra-2", voice_it={"enabled": True}, image_digests=_digests())


def test_a_malformed_answer_is_refused() -> None:
    with pytest.raises(ValueError, match="voice_it"):
        _infra("infra-1", voice_it={"enabled": "yes"})


def test_own_speech_recorded_under_a_no_to_voice_it_renders_off() -> None:
    chosen = {"enabled": True, "zone": "europe-west1-b", "image": IMAGES + "common-cu128-ubuntu-2204-v1"}
    assert _infra("infra-2", voice_it={"enabled": False}, voice_speech=chosen)["voice_speech"] == {"enabled": False}


# -- stage 0: the choice --------------------------------------------------------


def test_choice_is_a_stage_0_step_before_the_own_speech_question() -> None:
    steps = discover()
    ids = ordered_ids(steps)
    step = next(s for s in steps if s.id == "infra.voice-it")
    assert step.stage == 0 and step.read_only
    assert ids.index("preflight.sizing") < ids.index("infra.voice-it") < ids.index("infra.voice-speech")


def test_choice_defaults_to_no_and_says_nothing_more() -> None:
    ctx = FakeCtx(install_state())
    asked, ask = _asker("")
    step = VoiceItChoiceStep(ask=ask)
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert len(asked) == 1 and "Voice it" in asked[0] and asked[0].endswith("[n]")
    assert ctx.outputs["voice_it"] == {"enabled": False}
    assert step.check(ctx).status == MET
    assert "Capacity › Accounts" not in _said(ctx)


def test_yes_is_recorded_and_says_where_the_pool_accounts_go_in_one_line() -> None:
    ctx = FakeCtx(install_state())
    asked, ask = _asker("maybe", "y")
    step = VoiceItChoiceStep(ask=ask)
    step.apply(ctx, step.plan(ctx))
    assert len(asked) == 2 and ctx.outputs["voice_it"] == {"enabled": True}
    assert "\n" not in VOICE_IT_POOL and "Capacity › Accounts" in VOICE_IT_POOL
    assert VOICE_IT_POOL in _said(ctx)
    assert step.check(ctx).status == MET


# -- own speech is nested under Voice it -------------------------------------------


def test_own_speech_is_not_offered_after_a_no_to_voice_it() -> None:
    ctx = FakeCtx(install_state(voice_it={"enabled": False}))
    asked, ask = _asker("y")
    gcp = ComputeFake()
    step = VoiceSpeechChoiceStep(_deps(gcp), ask=ask)
    assert step.check(ctx).status == MET
    step.apply(ctx, step.plan(ctx))
    assert not asked and "voice_speech" not in ctx.outputs and not gcp.calls
    assert VoiceSpeechZoneStep(_deps(gcp)).check(ctx).status == MET


def test_own_speech_waits_for_the_voice_it_answer() -> None:
    ctx = FakeCtx(install_state())
    asked, ask = _asker("y")
    step = VoiceSpeechChoiceStep(_deps(ComputeFake()), ask=ask)
    assert step.check(ctx).status == UNMET
    with pytest.raises(LookupError, match="infra.voice-it"):
        step.apply(ctx, step.plan(ctx))
    assert not asked


def test_own_speech_is_offered_after_a_yes_to_voice_it() -> None:
    ctx = FakeCtx(install_state(voice_it={"enabled": True}))
    asked, ask = _asker("y")
    step = VoiceSpeechChoiceStep(_deps(ComputeFake()), ask=ask)
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert len(asked) == 1 and ctx.outputs["voice_speech"] == {"enabled": True}
