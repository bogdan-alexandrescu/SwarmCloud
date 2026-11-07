"""The installer offers Voice it's own speech engine (lane L11; part of #2688;
docs/design/voice-it-own-speech.md sections 14 and 15).

Stage 0 asks one optional question, only after a yes to Voice it, with the cost line the
design writes, default no; stage 1, once Compute Engine is on, picks the first
of the region's zones offering the NVIDIA L4 (`acceleratorTypes`), waits for
an NVIDIA_L4_GPUS quota of at least 1 on the Quotas page, and pins the newest
Deep Learning VM image by name. Every infra pass renders `voice_speech` with
`enabled`, `zone` and `image` from what those two steps recorded.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_install import tfvars
from swarm_install.exitcodes import MET, UNMET
from swarm_install.steps import discover, infra
from swarm_install.steps.bootstrap import Deps
from swarm_install.steps.infra import VoiceSpeechChoiceStep, VoiceSpeechZoneStep

from .test_infra_steps import ordered_ids
from .test_terraform_runner import FakeCtx, install_state

REGION = "europe-west1"
IMAGES = "projects/deeplearning-platform-release/global/images/"


def _infra(pass_name: str = "infra-1", **outputs: Any) -> dict:
    return tfvars.infra_inputs(install_state(**outputs), pass_name=pass_name, installer="admin@example.com")


class ComputeFake:
    """Compute Engine's accelerator types, regions and images, as `_call` answers them."""

    def __init__(self, *, zones: tuple[str, ...] = ("europe-west1-c", "europe-west1-b"), quota: float | None = 1,
                 images: list[dict] | None = None, exists: bool = True) -> None:
        self.zones = zones
        self.quota = [quota]
        self.images = images if images is not None else [
            {"name": "common-cu124-ubuntu-2204-v20260901", "family": "common-cu124-ubuntu-2204",
             "status": "READY", "creationTimestamp": "2026-09-01T10:00:00.000-07:00"},
            {"name": "common-cu128-ubuntu-2204-nvidia-570-v20261020", "family": "common-cu128-ubuntu-2204-nvidia-570",
             "status": "READY", "creationTimestamp": "2026-10-20T10:00:00.000-07:00"},
            {"name": "common-cu128-ubuntu-2204-nvidia-570-v20261101", "family": "common-cu128-ubuntu-2204-nvidia-570",
             "status": "READY", "creationTimestamp": "2026-11-01T10:00:00.000-07:00",
             "deprecated": {"state": "DEPRECATED"}},
            {"name": "common-cu129-ubuntu-2204-nvidia-580-v20261015", "family": "common-cu129-ubuntu-2204-nvidia-580",
             "status": "READY", "creationTimestamp": "2026-10-15T10:00:00.000-07:00"},
            {"name": "tf-2-17-cu124-v20261102-ubuntu-2204", "family": "tf-2-17-cu124-ubuntu-2204",
             "status": "READY", "creationTimestamp": "2026-11-02T10:00:00.000-07:00"},
        ]
        self.exists = exists
        self.calls: list[str] = []

    def project(self, project: str) -> dict | None:
        return {"name": "projects/123"} if self.exists else None

    def enabled_services(self, project: str) -> set[str]:
        return {"compute.googleapis.com"} if self.exists else set()

    def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append(url)
        if url.endswith("/aggregated/acceleratorTypes"):
            items: dict[str, Any] = {"zones/us-central1-a": {"acceleratorTypes": [
                {"name": "nvidia-l4", "zone": "https://x/zones/us-central1-a"}]}}
            for zone in (*self.zones, "europe-west1-ai1a"):
                items[f"zones/{zone}"] = {"acceleratorTypes": [{"name": "nvidia-l4", "zone": f"https://x/zones/{zone}"}]}
            items["zones/europe-west1-d"] = {"warning": {"code": "NO_RESULTS_ON_PAGE"}}
            return {"items": items}
        if url.endswith(f"/regions/{REGION}"):
            quota = self.quota[0] if len(self.quota) == 1 else self.quota.pop(0)
            return {"quotas": [] if quota is None else [{"metric": "NVIDIA_L4_GPUS", "limit": quota, "usage": 0}]}
        if url.endswith("/projects/deeplearning-platform-release/global/images"):
            return {"items": self.images}
        raise AssertionError(f"unexpected call {method} {url}")


def _deps(gcp: Any) -> Deps:
    return Deps(gcp=lambda: gcp)


def _asker(*answers: str) -> tuple[list[str], Any]:
    asked: list[str] = []
    queue = list(answers)

    def ask(ctx: Any, step_id: str, question: str, default: str | None = None) -> str:
        asked.append(f"{question} [{default}]")
        return queue.pop(0) if queue else (default or "")

    return asked, ask


# -- the tfvars ---------------------------------------------------------------


def test_no_choice_recorded_renders_nothing() -> None:
    assert "voice_speech" not in _infra()


def test_no_renders_voice_speech_off_on_every_infra_pass() -> None:
    for pass_name in ("registry", "infra-1", "infra-2", "infra-3", "infra-4", "infra-close"):
        assert _infra(pass_name, voice_speech={"enabled": False})["voice_speech"] == {"enabled": False}


def test_yes_renders_enabled_zone_and_image() -> None:
    chosen = {"enabled": True, "zone": "europe-west1-b", "image": IMAGES + "common-cu129-ubuntu-2204-nvidia-580-v1"}
    assert _infra(voice_speech=chosen)["voice_speech"] == chosen


def test_yes_without_a_zone_names_the_step_that_records_it() -> None:
    with pytest.raises(LookupError, match="infra.voice-speech-zone"):
        _infra(voice_speech={"enabled": True})


@pytest.mark.parametrize("chosen", [
    {"enabled": True, "zone": "us-central1-a", "image": IMAGES + "common-cu129-ubuntu-2204-nvidia-580-v1"},
    {"enabled": True, "zone": "europe-west1-ai1a", "image": IMAGES + "common-cu129-ubuntu-2204-nvidia-580-v1"},
    {"enabled": True, "zone": "europe-west1-b",
     "image": "projects/deeplearning-platform-release/global/images/family/common-cu128-ubuntu-2204"},
])
def test_a_zone_outside_the_region_or_a_family_image_is_refused(chosen: dict) -> None:
    with pytest.raises(ValueError):
        _infra(voice_speech=chosen)


# -- stage 0: the optional choice ---------------------------------------------------


def test_choice_is_a_stage_0_step_after_the_sizing() -> None:
    steps = discover()
    ids = ordered_ids(steps)
    step = next(s for s in steps if s.id == "infra.voice-speech")
    assert step.stage == 0 and step.read_only
    assert ids.index("preflight.sizing") < ids.index("infra.voice-speech") < ids.index("infra.voice-speech-zone")
    assert ids.index("foundations.apis") < ids.index("infra.voice-speech-zone") < ids.index("infra.pass-1")


def test_choice_says_the_cost_and_the_zone_and_defaults_to_no() -> None:
    ctx = FakeCtx(install_state(voice_it={"enabled": True}))
    asked, ask = _asker("")
    step = VoiceSpeechChoiceStep(_deps(ComputeFake()), ask=ask)
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert "about $0.71 an hour while used, $15 a month for its disk" in asked[0]
    assert "one stopped L4 machine" in asked[0] and "europe-west1-b" in asked[0] and asked[0].endswith("[n]")
    assert ctx.outputs["voice_speech"] == {"enabled": False}
    assert step.check(ctx).status == MET


def test_choice_yes_is_recorded_and_never_asked_again() -> None:
    ctx = FakeCtx(install_state(voice_it={"enabled": True}))
    asked, ask = _asker("y")
    step = VoiceSpeechChoiceStep(_deps(ComputeFake()), ask=ask)
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["voice_speech"] == {"enabled": True}
    assert step.check(ctx).status == MET and len(asked) == 1


def test_choice_before_the_project_exists_says_where_the_zone_comes_from() -> None:
    ctx = FakeCtx(install_state(voice_it={"enabled": True}))
    gcp = ComputeFake(exists=False)
    asked, ask = _asker("no")
    VoiceSpeechChoiceStep(_deps(gcp), ask=ask).apply(ctx, VoiceSpeechChoiceStep(_deps(gcp), ask=ask).plan(ctx))
    assert "first zone of europe-west1 that offers the L4" in asked[0]
    assert not gcp.calls
    assert ctx.outputs["voice_speech"] == {"enabled": False}


def test_choice_asks_again_on_an_answer_that_is_not_yes_or_no() -> None:
    ctx = FakeCtx(install_state(voice_it={"enabled": True}))
    asked, ask = _asker("maybe", "yes")
    step = VoiceSpeechChoiceStep(_deps(ComputeFake()), ask=ask)
    step.apply(ctx, step.plan(ctx))
    assert len(asked) == 2 and ctx.outputs["voice_speech"] == {"enabled": True}


# -- stage 1: zone, quota, image ------------------------------------------------------


def test_zone_step_is_met_while_the_choice_is_no() -> None:
    ctx = FakeCtx(install_state(voice_speech={"enabled": False}))
    assert VoiceSpeechZoneStep(_deps(ComputeFake())).check(ctx).status == MET


def test_zone_step_picks_the_first_l4_zone_of_the_region_and_pins_the_newest_image() -> None:
    ctx = FakeCtx(install_state(voice_speech={"enabled": True}))
    gcp = ComputeFake()
    step = VoiceSpeechZoneStep(_deps(gcp))
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["voice_speech"] == {
        "enabled": True, "zone": "europe-west1-b",
        "image": IMAGES + "common-cu129-ubuntu-2204-nvidia-580-v20261015"}
    assert step.check(ctx).status == MET
    rendered = _infra(voice_speech=ctx.outputs["voice_speech"])["voice_speech"]
    assert rendered["enabled"] is True and rendered["zone"] == "europe-west1-b"


def test_zone_step_turns_it_off_and_says_so_when_no_zone_offers_the_l4() -> None:
    ctx = FakeCtx(install_state(voice_speech={"enabled": True}))
    step = VoiceSpeechZoneStep(_deps(ComputeFake(zones=())))
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["voice_speech"]["enabled"] is False
    assert "europe-west1" in ctx.outputs["voice_speech"]["unavailable"]
    assert _infra(voice_speech=ctx.outputs["voice_speech"])["voice_speech"] == {"enabled": False}


def test_zone_step_opens_the_quota_page_and_waits_for_one_l4(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(infra, "open_page", lambda url, *, why: opened.append((url, why)))
    ctx = FakeCtx(install_state(voice_speech={"enabled": True}))
    gcp = ComputeFake()
    gcp.quota = [0, 0, 1]
    step = VoiceSpeechZoneStep(_deps(gcp))
    step.apply(ctx, step.plan(ctx))
    assert len(opened) == 1 and "nvidia_l4_gpus" in opened[0][0] and "NVIDIA_L4_GPUS" in opened[0][1]
    assert ctx.outputs["voice_speech"]["zone"] == "europe-west1-b"


def test_zone_step_never_starts_or_makes_a_machine() -> None:
    ctx = FakeCtx(install_state(voice_speech={"enabled": True}))
    gcp = ComputeFake()
    VoiceSpeechZoneStep(_deps(gcp)).apply(ctx, VoiceSpeechZoneStep(_deps(gcp)).plan(ctx))
    assert all("/instances" not in url for url in gcp.calls)


# -- the NVIDIA driver floor: vLLM 0.33 is built on CUDA 13, which needs 580 (PR 3538) ----------


def _image(name: str, family: str, created: str) -> dict:
    return {"name": name, "family": family, "status": "READY", "creationTimestamp": f"{created}T10:00:00.000-07:00"}


def test_the_newest_580_image_wins_over_a_newer_image_with_an_older_driver() -> None:
    gcp = ComputeFake(images=[
        _image("common-cu129-ubuntu-2204-nvidia-580-v20261001", "common-cu129-ubuntu-2204-nvidia-580", "2026-10-01"),
        _image("common-cu130-ubuntu-2204-nvidia-590-v20261020", "common-cu130-ubuntu-2204-nvidia-590", "2026-10-20"),
        _image("common-cu128-ubuntu-2204-nvidia-570-v20261105", "common-cu128-ubuntu-2204-nvidia-570", "2026-11-05"),
        _image("common-cu124-ubuntu-2204-v20261106", "common-cu124-ubuntu-2204", "2026-11-06"),
    ])
    assert infra.speech_image(gcp, "p") == IMAGES + "common-cu130-ubuntu-2204-nvidia-590-v20261020"


def test_no_image_with_a_580_driver_is_refused_naming_the_floor() -> None:
    gcp = ComputeFake(images=[
        _image("common-cu128-ubuntu-2204-nvidia-570-v20261105", "common-cu128-ubuntu-2204-nvidia-570", "2026-11-05"),
        _image("common-cu124-ubuntu-2204-v20261106", "common-cu124-ubuntu-2204", "2026-11-06"),
    ])
    with pytest.raises(LookupError) as raised:
        infra.speech_image(gcp, "p")
    assert str(raised.value).startswith(infra.DRIVER_FLOOR_REFUSAL)
    assert "580" in infra.DRIVER_FLOOR_REFUSAL


@pytest.mark.parametrize("name", ["common-cu128-ubuntu-2204-nvidia-570-v20261101", "common-cu124-ubuntu-2204-v20260901"])
def test_an_operator_image_with_an_older_driver_is_refused_and_a_580_image_pinned(name: str) -> None:
    ctx = FakeCtx(install_state(voice_speech={"enabled": True, "zone": "europe-west1-b", "image": IMAGES + name}))
    step = VoiceSpeechZoneStep(_deps(ComputeFake()))
    seen = step.check(ctx)
    assert seen.status == UNMET and seen.detail.startswith(infra.DRIVER_FLOOR_REFUSAL) and name in seen.detail
    with pytest.raises(ValueError, match="580 or later"):
        infra.check_speech_image(IMAGES + name)
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["voice_speech"]["image"] == IMAGES + "common-cu129-ubuntu-2204-nvidia-580-v20261015"
    assert step.check(ctx).status == MET


def test_an_operator_image_with_a_580_driver_is_kept() -> None:
    image = IMAGES + "common-cu129-ubuntu-2204-nvidia-580-v20260901"
    ctx = FakeCtx(install_state(voice_speech={"enabled": True, "zone": "europe-west1-b", "image": image}))
    assert VoiceSpeechZoneStep(_deps(ComputeFake())).check(ctx).status == MET
    assert infra.check_speech_image(image) == image
