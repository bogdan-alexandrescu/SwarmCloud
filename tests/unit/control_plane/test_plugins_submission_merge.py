"""A step's managed plugins and the workspace's defaults, merged at submission and signed with the step.

docs/design/managed-plugins.md sections 6.4, 7 and 8 (lane 7 of section 17),
with the owner's answers: a removed plugin refuses a submission naming it
(Q5, `plugin_removed`), and a default the step cannot run is skipped and
recorded, never a refusal (Q6). Every step gets `metadata.dispatch.plugins`:
the workspace's every-step defaults, plus the installs it names by id, minus
its `plugins_off`, each pinned to the install's current version, inside the
dispatch block the step spec signs whole. Writing the Allowed list again
keeps the defaults and the settings digest under a block that verifies, and
drops them under one that does not.

Plugins are installed by the catalogue's own routes (the stand-ins of
test_plugin_catalogue_routes); the spec signer is local. Offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import judgment, pluginconfig as pcfg, plugins, pluginstore as ps, workcontract as wc
from swarm_api.judgment import block_mac
from swarm_api.routes import plugin_config as rc
from swarm_api.routes.plugin_catalogue import _store

from .conftest import auth_header
from .spec_signer import LocalSpecSigner
from .test_settings_mac import new_key
from .test_plugin_catalogue_routes import (  # noqa: F401  (world is a fixture)
    _workspace,
    installed,
    world,
)

LEVEL = [{"key": "level", "type": "string", "default": "warn"}]
LARGE_ONLY = {"suggested": {"hosts": [], "runtimes": []}, "declared": {"profiles": ["claude-code-large"]}}


def _manifest(name: str) -> dict[str, Any]:
    return {"name": name, "version": "1.0.0", "description": "Lints.", "user_config": LEVEL}


@pytest.fixture
def mw(world):
    """The catalogue's world, with submission signing under the same settings key."""
    w = dict(world)
    ctx = w["ctx"]
    w["signer"] = LocalSpecSigner()
    ctx.submissions._settings = ctx.settings
    ctx.submissions._signer = w["signer"]
    w["key"] = ctx.settings.settings_key
    return w


def eng_plugin(w: dict[str, Any], name: str, **report: Any) -> dict[str, Any]:
    return installed(w, base=_workspace("eng"), name=name, manifest=_manifest(name), **report)


def platform_plugin(w: dict[str, Any], name: str = "lint-kit", **report: Any) -> dict[str, Any]:
    return installed(w, name=name, manifest=_manifest(name), **report)


def allow(w: dict[str, Any], *names: str):
    return w["client"].put("/v1/workspaces/eng/plugins", headers=auth_header("root"),
                           json={"allowed": list(names)})


def set_defaults(w: dict[str, Any], *names: str) -> None:
    response = w["client"].put(rc.DEFAULTS_ROUTE.format(tenant_id="eng"), headers=auth_header("root"),
                               json={"defaults": list(names)})
    assert response.status_code == 200, response.text


def save_level(w: dict[str, Any], install_id: str, level: str, revision: int = 0):
    base = rc.BASE.format(tenant_id="eng")
    form = w["client"].get(f"{base}/{install_id}", headers=auth_header("root")).json()["form"]
    return w["client"].put(f"{base}/{install_id}", headers=auth_header("root"),
                           json={"version_id": form["version_id"], "revision": revision,
                                 "values": {"level": level}})


def submit(w: dict[str, Any], profile: str = "claude-code", **step: Any):
    return w["client"].post("/v1/tasks", headers=auth_header("alice"), json={
        "runner_profile": profile, "input": {"prompt": "lint"}, **step})


def tasks(w: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(p.split("/", 1)[1], d) for p, d in w["db"].docs.items()
            if p.startswith("tasks/") and p.count("/") == 1]


def only_task(w: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (found,) = tasks(w)
    return found


def block(w: dict[str, Any]) -> dict[str, Any]:
    return w["db"].docs["workspace_settings/eng"]["plugins"]


# --- the merge ---------------------------------------------------------------------


def test_defaults_merge_with_the_steps_own_choices_and_its_off_list(mw, monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    kept = eng_plugin(mw, "eng-kit")
    turned_off = eng_plugin(mw, "off-kit")
    shared = platform_plugin(mw)
    assert allow(mw, "gstack", shared["install_id"]).status_code == 200
    set_defaults(mw, kept["install_id"], turned_off["install_id"], "gstack")
    assert save_level(mw, kept["install_id"], "error").status_code == 200

    response = submit(mw, plugins={shared["install_id"]: {"overrides": {"level": "info"}}},
                      plugins_off=[turned_off["install_id"]])
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    signed = task["metadata"]["dispatch"][plugins.DISPATCH_PLUGINS]
    # MUTATION: drop the defaults, the step's own install or the off list
    # from the merge and this set changes.
    assert list(signed) == sorted([kept["install_id"], shared["install_id"]])
    assert signed[kept["install_id"]]["env"] == {"CLAUDE_PLUGIN_OPTION_LEVEL": "error"}
    assert signed[shared["install_id"]]["env"] == {"CLAUDE_PLUGIN_OPTION_LEVEL": "info"}
    assert plugins.DISPATCH_SKIPPED not in task["metadata"]["dispatch"]
    # The built-in default rides in the work contract, unattended, every skill.
    contract = task["metadata"][wc.METADATA_KEY]
    assert contract["capabilities"]["plugins"]["gstack"]["mode"] == "unattended"
    assert sorted(contract["capabilities"]["plugins"]["gstack"]["skills"]) == sorted(
        plugins.CATALOGUE["gstack"].skills["unattended"])


def test_a_managed_install_is_named_by_id_and_pinned_to_its_installed_version(mw):
    done = eng_plugin(mw, "eng-kit")
    response = submit(mw, plugins={done["install_id"]: {}})
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    entry = task["metadata"]["dispatch"]["plugins"][done["install_id"]]
    store = _store(mw["ctx"])
    current = store.current(store.install(done["install_id"]))
    assert current is not None
    assert (entry["name"], entry["scope"], entry["version_id"]) == ("eng-kit", ps.WORKSPACE, done["version_id"])
    assert entry["runnable_sha256"] == current["runnable_sha256"]
    assert entry["object"] == current["object"]
    assert entry["skills"] is None and entry["disabled_skills"] == []


def test_a_managed_plugin_needs_no_work_contract(mw, monkeypatch):
    monkeypatch.delenv(wc.SWITCH_ENV, raising=False)
    done = eng_plugin(mw, "eng-kit")
    assert submit(mw, plugins={done["install_id"]: {}}).status_code == 201
    _, task = only_task(mw)
    assert wc.METADATA_KEY not in task["metadata"]
    assert list(task["metadata"]["dispatch"]["plugins"]) == [done["install_id"]]


def test_a_removed_install_is_refused_with_its_fixed_code_before_anything_else(mw):
    done = eng_plugin(mw, "eng-kit")
    removed = mw["client"].delete(f"{_workspace('eng')}/installs/{done['install_id']}?confirm=eng-kit",
                                  headers=auth_header("root"))
    assert removed.status_code == 200, removed.text
    # A mode on a managed install is refused too, but the removal is named first.
    response = submit(mw, plugins={done["install_id"]: {"mode": "unattended"}})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "plugin_removed"
    assert body["detail"]["install_id"] == done["install_id"]
    assert body["detail"]["field"] == plugins.STEP_FIELD
    assert tasks(mw) == []


def test_a_removed_install_refuses_a_workflow_step_naming_it(mw):
    done = eng_plugin(mw, "eng-kit")
    mw["client"].delete(f"{_workspace('eng')}/installs/{done['install_id']}?confirm=eng-kit",
                        headers=auth_header("root"))
    response = mw["client"].post("/v1/workflows", headers=auth_header("alice"), json={
        "on_step_failure": "continue", "strategy": "direct-pr",
        "repository_url": "https://github.com/example-org/widgets.git",
        "steps": [{"step_id": "lint", "runner_profile": "claude-code", "input": {"prompt": "lint"},
                   "plugins": {done["install_id"]: {}}}]})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "plugin_removed"
    assert response.json()["detail"]["step_id"] == "lint"


def test_an_incompatible_default_is_skipped_and_recorded_not_refused(mw):
    runs = eng_plugin(mw, "eng-kit")
    large = eng_plugin(mw, "big-kit", requires=LARGE_ONLY)
    set_defaults(mw, runs["install_id"], large["install_id"])
    response = submit(mw)
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    dispatch = task["metadata"]["dispatch"]
    assert list(dispatch["plugins"]) == [runs["install_id"]]
    assert dispatch["plugins_skipped"] == [{"install_id": large["install_id"],
                                            "code": "plugin_profile_unsupported"}]



def test_with_the_platform_switch_off_a_step_naming_a_managed_plugin_is_refused(mw, monkeypatch):
    done = eng_plugin(mw, "eng-kit")
    monkeypatch.setenv(plugins.SWITCH_ENV, "off")
    # MUTATION: drop the switch from the merge and a managed install, which
    # never reaches check_step, runs with plugins off on the platform.
    response = submit(mw, plugins={done["install_id"]: {}})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["plugins"] == [done["install_id"]]
    assert tasks(mw) == []


def test_with_the_platform_switch_off_the_defaults_are_skipped_and_recorded(mw, monkeypatch):
    kept = eng_plugin(mw, "eng-kit")
    turned_off = eng_plugin(mw, "off-kit")
    set_defaults(mw, kept["install_id"], turned_off["install_id"])
    monkeypatch.setenv(plugins.SWITCH_ENV, "off")
    response = submit(mw, plugins_off=[turned_off["install_id"]])
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    dispatch = task["metadata"]["dispatch"]
    assert plugins.DISPATCH_PLUGINS not in dispatch
    assert dispatch[plugins.DISPATCH_SKIPPED] == [{"install_id": kept["install_id"],
                                                   "code": plugins.SKIP_PLATFORM_OFF}]

def test_the_same_plugin_named_by_the_step_on_a_profile_that_cannot_run_it_is_refused(mw):
    large = eng_plugin(mw, "big-kit", requires=LARGE_ONLY)
    response = submit(mw, plugins={large["install_id"]: {}})
    assert response.status_code == 422 and response.json()["code"] == "plugin_profile_unsupported"
    assert tasks(mw) == []


def test_a_built_in_default_with_work_contracts_off_is_skipped_and_recorded(mw, monkeypatch):
    monkeypatch.delenv(wc.SWITCH_ENV, raising=False)
    assert allow(mw, "gstack").status_code == 200
    set_defaults(mw, "gstack")
    assert submit(mw).status_code == 201
    _, task = only_task(mw)
    assert task["metadata"]["dispatch"]["plugins_skipped"] == [
        {"install_id": "gstack", "code": plugins.SKIP_CONTRACTS_OFF}]
    assert wc.METADATA_KEY not in task["metadata"]


def test_the_signed_dispatch_carries_the_plugins(mw):
    done = eng_plugin(mw, "eng-kit")
    set_defaults(mw, done["install_id"])
    assert submit(mw).status_code == 201
    task_id, task = only_task(mw)
    assert mw["signer"].verifies(task, task_id)
    # MUTATION: write the set outside the signed dispatch block and a changed
    # digest still verifies.
    forged = {**task, "metadata": {**task["metadata"], "dispatch": {
        **task["metadata"]["dispatch"], "plugins": {done["install_id"]: {
            **task["metadata"]["dispatch"]["plugins"][done["install_id"]], "runnable_sha256": "f" * 64}}}}}
    assert not mw["signer"].verifies(forged, task_id)


def test_a_step_with_no_plugins_and_no_defaults_is_what_it_was(mw):
    assert submit(mw).status_code == 201
    _, task = only_task(mw)
    assert plugins.DISPATCH_PLUGINS not in task["metadata"]["dispatch"]
    assert plugins.DISPATCH_SKIPPED not in task["metadata"]["dispatch"]


def test_a_workflow_step_turns_a_default_off_and_its_sibling_keeps_it(mw):
    done = eng_plugin(mw, "eng-kit")
    set_defaults(mw, done["install_id"])
    steps = [{"step_id": "plain", "runner_profile": "claude-code", "input": {"prompt": "lint"},
              "plugins_off": [done["install_id"]]},
             {"step_id": "linted", "runner_profile": "claude-code", "input": {"prompt": "lint"}}]
    response = mw["client"].post("/v1/workflows", headers=auth_header("alice"), json={
        "on_step_failure": "continue", "strategy": "direct-pr",
        "repository_url": "https://github.com/example-org/widgets.git", "steps": steps})
    assert response.status_code == 201, response.text
    by_step = {t["metadata"]["workflow_step"]: t for _, t in tasks(mw)}
    assert "plugins" not in by_step["plain"]["metadata"]["dispatch"]
    assert list(by_step["linted"]["metadata"]["dispatch"]["plugins"]) == [done["install_id"]]


@pytest.mark.parametrize("entry, off, words", [
    ({"mode": "unattended"}, [], "takes no mode or skills"),
    ({"skills": ["lint"]}, [], "takes no mode or skills"),
    ({}, ["SELF"], "both loaded and turned off"),
])
def test_a_managed_entry_takes_only_overrides(mw, entry, off, words):
    done = eng_plugin(mw, "eng-kit")
    off = [done["install_id"] if n == "SELF" else n for n in off]
    response = submit(mw, plugins={done["install_id"]: entry}, plugins_off=off)
    assert response.status_code == 422, response.text
    assert words in response.json()["message"]
    assert tasks(mw) == []


@pytest.mark.parametrize("entry, words", [
    ({"mode": "unattended", "skills": ["review"], "overrides": {"level": "x"}}, "no settings to override"),
    ({"skills": ["review"]}, "names a mode"),
])
def test_a_built_in_entry_takes_a_mode_and_skills_and_no_overrides(mw, monkeypatch, entry, words):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    assert allow(mw, "gstack").status_code == 200
    response = submit(mw, plugins={"gstack": entry})
    assert response.status_code == 422, response.text
    assert words in response.json()["message"]


def test_an_override_for_a_secret_or_an_undeclared_field_is_refused(mw):
    done = eng_plugin(mw, "eng-kit")
    response = submit(mw, plugins={done["install_id"]: {"overrides": {"nope": "x"}}})
    assert response.status_code == 422 and response.json()["code"] == "field_unknown"


# --- the Allowed list ----------------------------------------------------------------


def test_the_allowed_list_names_active_platform_installs_beside_the_built_ins(mw):
    shared = platform_plugin(mw)
    own = eng_plugin(mw, "eng-kit")
    response = allow(mw, "gstack", shared["install_id"])
    assert response.status_code == 200, response.text
    assert response.json()["plugins"]["allowed"] == sorted(["gstack", shared["install_id"]])
    for name in (own["install_id"], "p-000000000000", "not-a-plugin"):
        refused = allow(mw, name)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["unknown"] == [name]


def test_a_platform_install_the_workspace_has_not_allowed_is_refused_at_submission(mw):
    shared = platform_plugin(mw)
    response = submit(mw, plugins={shared["install_id"]: {}})
    assert response.status_code == 422 and response.json()["code"] == "plugin_not_allowed"


def test_the_allowed_list_written_again_keeps_defaults_and_settings_when_the_block_verifies(mw):
    assert allow(mw, "gstack").status_code == 200
    done = eng_plugin(mw, "eng-kit")
    assert save_level(mw, done["install_id"], "error").status_code == 200
    set_defaults(mw, done["install_id"])
    before = dict(block(mw))
    assert allow(mw, "gstack", "security-audit").status_code == 200
    after = block(mw)
    # MUTATION: drop the carry-over in PluginsStore.put and both are lost.
    assert after["allowed"] == ["gstack", "security-audit"]
    assert after["defaults"] == [done["install_id"]]
    assert after["configs_digest"] == before["configs_digest"]
    assert after["config_digests"] == before["config_digests"]
    assert judgment.block_verifies("eng", "plugins", after, (mw["key"],))
    # The settings still read, at the revision they were saved at.
    configs = rc._configs(mw["ctx"])
    assert configs.defaults("eng") == [done["install_id"]]
    assert configs.config("eng", done["install_id"])["fields"]["level"]["value"] == "error"
    assert save_level(mw, done["install_id"], "info", revision=1).status_code == 200


def test_the_allowed_list_written_again_drops_defaults_and_settings_when_the_block_does_not_verify(mw):
    assert allow(mw, "gstack").status_code == 200
    done = eng_plugin(mw, "eng-kit")
    assert save_level(mw, done["install_id"], "error").status_code == 200
    set_defaults(mw, done["install_id"])
    # A direct write plants another default: the block no longer verifies.
    block(mw)["defaults"] = [done["install_id"], "gstack"]
    assert allow(mw, "gstack", "security-audit").status_code == 200
    after = block(mw)
    for key in plugins.CARRIED_FIELDS:
        assert key not in after
    assert judgment.block_verifies("eng", "plugins", after, (mw["key"],))
    configs = rc._configs(mw["ctx"])
    assert configs.defaults("eng") == []
    assert "level" not in configs.config("eng", done["install_id"])["fields"]


def test_a_block_signed_under_an_unknown_key_is_not_carried_over(mw):
    planted: dict[str, Any] = {"allowed": ["gstack"], "defaults": ["gstack"]}
    planted["mac"] = block_mac("eng", "plugins", planted, (new_key(),))
    mw["db"].docs["workspace_settings/eng"] = {"tenant_id": "eng", "plugins": planted}
    assert allow(mw, "gstack").status_code == 200
    assert "defaults" not in block(mw)


def test_the_carried_keys_are_the_ones_pluginconfig_writes():
    assert (plugins.DEFAULTS, plugins.CONFIGS_DIGEST, plugins.CONFIG_DIGESTS) == (
        pcfg.DEFAULTS, pcfg.CONFIGS_DIGEST, pcfg.CONFIG_DIGESTS)
    assert judgment.WRITER_FIELDS["plugins"] == (plugins.ALLOWED, pcfg.DEFAULTS, pcfg.CONFIGS_DIGEST)
    assert judgment.unowned_covered_keys("plugins") == ()
