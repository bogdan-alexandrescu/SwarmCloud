"""One leg of a QA target, as the renderer reads it (docs/design/qa-environments.md
§1.1, §1.5, §6; lane QAENV-MODEL of §8; part of #2136).

`qaspec.leg` is the target as one environment sees it: the app's fields, and
that entry's per-leg fields in their old places, so every rule `qaspec`
applies to a target it applies to the leg unchanged. Held here, pure:

  * a target with no `environments` is its own one leg, and renders byte for
    byte as it did;
  * a leg's prompts carry its own base URL and start clicks, never the other
    leg's (§6: "an agent on one leg is never told to click the other leg's
    toggle");
  * a `production` leg renders no mutating flow; a `non_production` leg on its
    own origin renders it; one sharing an origin with a `production` leg
    renders none (§1.5, §6);
  * a migrated leg's mark names it; an unmigrated one's is `{id, revision}`.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qaspec

STAGING_CLICKS = [{"text": "Staging"}, {"text": "Sign in"}]
PRODUCTION_CLICKS = [{"text": "Production"}, {"text": "Sign in"}]
FLOWS = [
    {"id": "browse", "goal": "Open the pricing page", "start_path": "/"},
    {"id": "checkout", "goal": "Buy the cheapest plan", "start_path": "/", "mutating": True},
]


def _login(clicks: list[dict[str, str]]) -> dict[str, Any]:
    return {"kind": "own_login", "login_path": "/", "signed_in_selector": "nav .me", "start_clicks": clicks}


def _flat(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_3f9a", "revision": 4, "name": "Studio", "tenant_id": "tenant-a",
        "base_url": "https://studio.example.com", "repository": "https://github.com/example-org/studio",
        "sign_in": {"kind": "none"}, "screens": [{"id": "home", "path": "/"}],
        "environment": "non_production", "flows": FLOWS,
    }
    target.update(over)
    return target


def _two(staging_base: str = "https://staging.example.com") -> dict[str, Any]:
    return {
        "id": "qat_3f9a", "revision": 4, "name": "Studio", "tenant_id": "tenant-a",
        "repository": "https://github.com/example-org/studio",
        "screens": [{"id": "home", "path": "/"}], "flows": FLOWS,
        "environments": [
            {"name": "staging", "base_url": staging_base, "environment": "non_production",
             "sign_in": _login(STAGING_CLICKS)},
            {"name": "production", "base_url": "https://studio.example.com", "environment": "production",
             "sign_in": _login(PRODUCTION_CLICKS)},
        ],
    }


def _prompts(spec: dict[str, Any]) -> str:
    return "\n".join(step["prompt"] for step in spec["steps"])


def test_a_target_without_environments_is_its_own_one_leg():
    target = _flat()
    (entry,) = qaspec.environments(target)
    assert entry["name"] == "staging" and entry["base_url"] == target["base_url"]
    # MUTATION: add a key to the view of an unmigrated target and this fails.
    assert qaspec.leg(target) == {**target, qaspec.LEG_NAME: "staging"}
    # MUTATION: filter a one-leg target's flows in `leg` and its rendered steps change.
    assert qaspec.render(qaspec.leg(target), file="none") == qaspec.render(target, file="none")
    assert qaspec.qa_target_metadata(qaspec.leg(target)) == {"id": "qat_3f9a", "revision": 4}


def test_a_target_without_environments_is_production_when_unmarked():
    (entry,) = qaspec.environments(_flat(environment=None))
    # MUTATION: name an unmarked legacy target `staging` and this fails.
    assert entry["name"] == "production"


def test_a_legs_prompts_carry_its_own_base_and_clicks_only():
    target = _two()
    staging = _prompts(qaspec.render(qaspec.leg(target, "staging"), file="none"))
    production = _prompts(qaspec.render(qaspec.leg(target, "production"), file="none"))
    # MUTATION: read the first entry's sign_in in `leg` and production's prompts click 'Staging'.
    assert "'Staging'" in staging and "'Production'" not in staging
    assert "'Production'" in production and "'Staging'" not in production
    assert "https://staging.example.com" in staging and "studio.example.com" not in staging


def test_a_production_leg_renders_no_mutating_flow():
    spec = qaspec.render(qaspec.leg(_two(), "production"), file="none")
    steps = {step["step_id"] for step in spec["steps"]}
    # MUTATION: keep mutating flows on a production leg and this renders flow-checkout
    # (or the renderer refuses the run outright).
    # QA-FLOWS-CAP: the read-only flow runs in the batch step `flows-1`.
    assert "flows-1" in steps and "flow-checkout" not in steps
    batch = next(step for step in spec["steps"] if step["step_id"] == "flows-1")
    assert "`browse`" in batch["prompt"] and "`checkout`" not in batch["prompt"]


def test_a_non_production_leg_on_its_own_origin_renders_its_mutating_flow():
    spec = qaspec.render(qaspec.leg(_two(), "staging"), file="none")
    # MUTATION: drop mutating flows from every leg of a target with a production leg and this fails.
    assert "flow-checkout" in {step["step_id"] for step in spec["steps"]}


def test_a_non_production_leg_sharing_an_origin_with_production_renders_no_mutating_flow():
    target = _two(staging_base="https://studio.example.com")
    # MUTATION: compare base URLs rather than origins in `shares_production_origin` and a path differs.
    assert qaspec.shares_production_origin(target, "staging") is True
    assert qaspec.shares_production_origin(_two(), "staging") is False
    spec = qaspec.render(qaspec.leg(target, "staging"), file="none")
    # MUTATION: drop the shared-origin rule from `leg` and this renders flow-checkout.
    assert "flow-checkout" not in {step["step_id"] for step in spec["steps"]}
    assert "may submit forms" not in _prompts(spec)


def test_an_explicit_default_port_is_the_same_origin():
    target = _two(staging_base="https://studio.example.com:443")
    # MUTATION: compare `https://h:443` and `https://h` as two origins and the staging leg may write.
    assert qaspec.shares_production_origin(target, "staging") is True
    assert qaspec.shares_production_origin(_two(staging_base="https://studio.example.com:8443"), "staging") is False


def test_a_shared_extra_origin_is_a_shared_origin():
    target = _two()
    target["environments"][0]["sign_in"] = {**_login(STAGING_CLICKS), "extra_origin": "https://sso.example.com"}
    target["environments"][1]["sign_in"] = {**_login(PRODUCTION_CLICKS), "extra_origin": "https://sso.example.com"}
    # MUTATION: compare only base origins and a shared identity provider is missed.
    assert qaspec.shares_production_origin(target, "staging") is True


def test_a_migrated_legs_mark_names_it():
    # MUTATION: leave `environment` out of `qa_target_metadata` and this is {id, revision}.
    assert qaspec.qa_target_metadata(qaspec.leg(_two(), "production")) == {
        "id": "qat_3f9a", "revision": 4, "environment": "production"}


@pytest.mark.parametrize("name", [None, "qa"])
def test_a_leg_of_two_needs_a_name_it_has(name):
    with pytest.raises(qaspec.QaSpecError) as caught:
        qaspec.leg(_two(), name)
    # MUTATION: answer entries[0] for a missing name and this does not raise.
    assert caught.value.code in ("qa_target_environment_required", "qa_target_environment_unknown")
