"""The built-ins switch: gstack and security-audit seeded as bundled managed installs, and a step resolving to them.

docs/design/managed-plugins.md sections 1.4.1, 1.4.3, 1.4.5 and 1.4.8 (lane
17 of section 17.1). With SWARM_BUILTINS_MANAGED exactly `on`, the first
catalogue read (or submission) seeds `builtin:gstack` and
`builtin:security-audit`, `build: "image"`, at the release image's pins; a
step naming either, or a workspace default naming either, carries it in
`metadata.dispatch.plugins` as a managed entry with the step's mode and
skills, never in `capabilities.plugins`, so work contracts need not be on.
Update works on a bundled install, Remove is refused, and an update's
intake keeps the source archive it checked at `platform/sources/<sha256>.tar`
once, where the bundle check reads it back. Off, nothing changes.

The catalogue's world and stand-ins (test_plugin_catalogue_routes); offline.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from swarm_api import plugincatalogue as pcat, pluginbundle, plugins, pluginstore as ps, workcontract as wc
from swarm_api.routes.plugin_catalogue import _store

from .conftest import auth_header
from .test_plugin_catalogue_routes import PLATFORM, _workspace, audit, notify, run_intake
from .test_plugins_submission_merge import (  # noqa: F401  (mw and world are fixtures)
    allow,
    mw,
    only_task,
    set_defaults,
    submit,
    tasks,
    world,
)

BUNDLED_IDS = ["builtin:gstack", "builtin:security-audit"]
RELEASE_PINS = Path(__file__).resolve().parents[3] / "images" / "agent-runtime-base" / "bundled-plugins.json"
UPSTREAM = "example-org/security-audit"
COMMIT_U = "d" * 40


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(pcat.SWITCH_ENV, "on")


def _catalogue(w: dict[str, Any]) -> dict[str, Any]:
    response = w["client"].get(PLATFORM, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    return response.json()


def _source_archive() -> bytes:
    """A forge-shaped archive: one top folder, the audit skill and a licence."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("top/skills/security-audit/SKILL.md", b"# audit\n"),
                           ("top/skills/security-audit/validate.cjs", b"// check\n"),
                           ("top/LICENSE", b"MIT\n"), ("top/README.md", b"readme\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- the switch and the seed source -------------------------------------------------


def test_the_switch_is_on_only_when_it_says_exactly_on():
    # MUTATION: a truthy read ("On", "1", unset as on) turns the switch on
    # where the owner has not.
    assert pcat.builtins_managed({pcat.SWITCH_ENV: "on"}) is True
    for value in ("", "off", "On", "1", "true"):
        assert pcat.builtins_managed({pcat.SWITCH_ENV: value}) is False
    assert pcat.builtins_managed({}) is False


def test_the_seed_pins_are_the_release_files():
    # The one place a release's default pins are written (section 1.4.1).
    release = json.loads(RELEASE_PINS.read_text())["plugins"]
    assert [p.install_id for p in pcat.BUNDLED] == BUNDLED_IDS
    for pin in pcat.BUNDLED:
        stated = release[pin.entry.name]
        assert (pin.install_id, pin.entry.version, pin.entry.install_path) == (
            stated["install_id"], stated["version"], stated["install_path"])
        assert (pin.source_sha256, pin.commit) == (stated["source"]["sha256"], stated["source"]["commit"])
        assert ps.VERSION_ID.match(pin.version_id)


# --- the seed, on a catalogue read --------------------------------------------------


def test_with_the_switch_on_the_catalogue_lists_the_two_bundled_installs(world, switch_on):
    w = world
    body = _catalogue(w)
    # MUTATION: keep listing the code entries and a bundled plugin shows twice.
    assert body["built_in"] == []
    bundled = {e["install_id"]: e for e in body["installs"]}
    assert sorted(bundled) == BUNDLED_IDS
    for entry in bundled.values():
        assert entry["build"] == ps.BUILD_IMAGE and entry["removable"] is False
        assert entry["updatable"] is True
    store = _store(w["ctx"])
    for pin in pcat.BUNDLED:
        install = store.install(pin.install_id)
        assert ps.is_bundled(install) and install["built_in"] is True
        current = store.current(install)
        assert current["version_id"] == pin.version_id and current["source_sha256"] == pin.source_sha256
        assert current["manifest"]["version"] == pin.entry.version
        assert current["overlay"]["modes"] == {m: list(s) for m, s in pin.entry.skills.items()}
        assert current["requires"]["declared"]["profiles"] == list(pin.entry.runner_profiles)
    assert len(audit(w["db"], ps.AUDIT_INSTALLED)) == 2
    # A second read (or a second instance) writes nothing.
    _catalogue(w)
    assert len(audit(w["db"], ps.AUDIT_INSTALLED)) == 2


def test_with_the_switch_off_nothing_is_seeded(world):
    w = world
    body = _catalogue(w)
    assert {e["install_id"] for e in body["built_in"]} == set(BUNDLED_IDS)
    assert body["installs"] == []
    assert not [p for p in w["db"].docs if p.startswith("plugin_installs/builtin:")]


def test_a_forged_bundled_install_is_never_overwritten_or_trusted(world, switch_on):
    w = world
    w["db"].docs["plugin_installs/builtin:gstack"] = {
        "install_id": "builtin:gstack", "scope": "platform", "tenant_id": None, "name": "gstack",
        "build": "image", "state": "active", "current_version": None}
    body = _catalogue(w)
    assert [e["install_id"] for e in body["installs"]] == ["builtin:security-audit"]
    assert "mac" not in w["db"].docs["plugin_installs/builtin:gstack"]


def test_the_workspace_catalogue_reads_a_bundled_install_as_allowed_by_its_name(mw, switch_on):
    w = mw
    assert allow(w, "gstack").status_code == 200
    body = w["client"].get(_workspace("eng"), headers=auth_header("root")).json()
    allowed = {e["install_id"]: e["allowed"] for e in body["platform_installs"]}
    assert allowed == {"builtin:gstack": True, "builtin:security-audit": False}


# --- a step's plugins, at submission ------------------------------------------------


def test_with_the_switch_on_a_step_naming_gstack_carries_the_managed_install(mw, switch_on, monkeypatch):
    # Work contracts off: a managed plugin needs none (section 1.4.5, step 5).
    monkeypatch.setenv(wc.SWITCH_ENV, "off")
    assert allow(mw, "gstack").status_code == 200
    response = submit(mw, plugins={"gstack": {"mode": "unattended", "skills": ["review", "qa"]}})
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    signed = task["metadata"]["dispatch"][plugins.DISPATCH_PLUGINS]
    pin = pcat.BUNDLED_BY_ID["builtin:gstack"]
    assert list(signed) == ["builtin:gstack"]
    entry = signed["builtin:gstack"]
    assert (entry["name"], entry["scope"], entry["version_id"]) == ("gstack", ps.PLATFORM, pin.version_id)
    assert (entry["build"], entry["source_sha256"]) == (ps.BUILD_IMAGE, pin.source_sha256)
    assert (entry["mode"], entry["skills"]) == ("unattended", ["review", "qa"])
    # MUTATION: keep writing it into the work contract and this fails.
    assert wc.METADATA_KEY not in task["metadata"]
    assert task.get("plugins") in (None, {})


def test_with_the_switch_on_a_default_runs_unattended_with_every_skill(mw, switch_on, monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    assert allow(mw, "gstack", "security-audit").status_code == 200
    set_defaults(mw, "gstack", "security-audit")
    response = submit(mw, plugins={"builtin:security-audit": {"mode": "unattended",
                                                             "skills": ["security-audit"]}})
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    signed = task["metadata"]["dispatch"][plugins.DISPATCH_PLUGINS]
    assert sorted(signed) == BUNDLED_IDS
    assert signed["builtin:gstack"]["mode"] == "unattended"
    assert signed["builtin:gstack"]["skills"] == list(pcat.GSTACK.skills["unattended"])
    assert signed["builtin:security-audit"]["skills"] == ["security-audit"]
    contract = task["metadata"].get(wc.METADATA_KEY)
    assert contract is None or not (contract.get("capabilities") or {}).get("plugins")


def test_with_the_switch_off_a_step_naming_gstack_resolves_as_today(mw, monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    assert allow(mw, "gstack").status_code == 200
    response = submit(mw, plugins={"gstack": {"mode": "unattended", "skills": ["review"]}})
    assert response.status_code == 201, response.text
    _, task = only_task(mw)
    assert plugins.DISPATCH_PLUGINS not in task["metadata"]["dispatch"]
    assert task["metadata"][wc.METADATA_KEY]["capabilities"]["plugins"] == {
        "gstack": {"mode": "unattended", "skills": ["review"]}}
    assert not [p for p in mw["db"].docs if p.startswith("plugin_installs/builtin:")]


def test_with_the_switch_off_and_work_contracts_off_gstack_is_refused_as_today(mw, monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "off")
    assert allow(mw, "gstack").status_code == 200
    response = submit(mw, plugins={"gstack": {"mode": "unattended", "skills": ["review"]}})
    assert response.status_code == 422, response.text
    assert tasks(mw) == []


@pytest.mark.parametrize(("chosen", "code"), [
    ({"mode": "unattended", "skills": ["cso"]}, "plugin_skill_not_offered"),
    ({"mode": "planner", "skills": ["spec"]}, "plugin_mode_unsupported"),
    ({"mode": "unattended", "skills": []}, "plugin_skills_missing"),
])
def test_with_the_switch_on_the_steps_mode_and_skills_are_checked(mw, switch_on, chosen, code):
    assert allow(mw, "gstack").status_code == 200
    response = submit(mw, plugins={"gstack": chosen})
    assert response.status_code == 422, response.text
    assert code in response.text
    assert tasks(mw) == []


def test_with_the_switch_on_a_bundled_install_the_workspace_has_not_allowed_is_refused(mw, switch_on):
    response = submit(mw, plugins={"gstack": {"mode": "unattended", "skills": ["review"]}})
    assert response.status_code == 422, response.text
    assert "plugin_not_allowed" in response.text or "does not allow" in response.text
    assert tasks(mw) == []


# --- Update and Remove (section 1.4.8) ----------------------------------------------


def test_a_bundled_install_can_be_updated_but_not_removed(world, switch_on):
    w = world
    _catalogue(w)
    removed = w["client"].delete(f"{PLATFORM}/installs/builtin:gstack?confirm=gstack", headers=auth_header("root"))
    assert removed.status_code == 422, removed.text
    assert removed.json()["code"] == "plugin_built_in"
    assert _store(w["ctx"]).install("builtin:gstack")["state"] == ps.STATE_ACTIVE

    # The seed names no repository: the first update names one.
    missing = w["client"].post(f"{PLATFORM}/installs/builtin:security-audit/update", json={"ref": "v2"},
                               headers=auth_header("root"))
    assert missing.status_code == 422 and missing.json()["code"] == "repository_invalid"
    w["github"].refs[(UPSTREAM, "v2")] = COMMIT_U
    started = w["client"].post(f"{PLATFORM}/installs/builtin:security-audit/update",
                               json={"repository": UPSTREAM, "ref": "v2"}, headers=auth_header("root"))
    # MUTATION: refuse every `builtin:` id (the code entries' rule) and this is 422.
    assert started.status_code == 202, started.text
    assert started.json()["install_id"] == "builtin:security-audit"


def test_another_installs_update_names_no_repository(world):
    from .test_plugin_catalogue_routes import installed

    w = world
    done = installed(w)
    response = w["client"].post(f"{PLATFORM}/installs/{done['install_id']}/update",
                                json={"repository": UPSTREAM, "ref": "v2.0.0"}, headers=auth_header("root"))
    assert response.status_code == 422 and response.json()["code"] == "repository_invalid"


# --- the intake keeps the checked source archive (section 1.4.3) ---------------------


def _bundled_update(w: dict[str, Any], archive: bytes, *, source_sha256: str | None = None) -> dict[str, Any]:
    w["github"].refs[(UPSTREAM, "v2")] = COMMIT_U
    w["github"].tarballs[(UPSTREAM, COMMIT_U)] = archive
    started = w["client"].post(f"{PLATFORM}/installs/builtin:security-audit/update",
                               json={"repository": UPSTREAM, "ref": "v2"}, headers=auth_header("root"))
    assert started.status_code == 202, started.text
    intake = started.json()
    run_intake(w["bucket"], intake, name="security-audit", version=COMMIT_U,
               source_sha256=source_sha256 or hashlib.sha256(archive).hexdigest(), source_bytes=len(archive))
    done = notify(w, intake["intake_id"])
    assert done.status_code == 200, done.text
    return w["client"].get(f"{PLATFORM}/intakes/{intake['intake_id']}", headers=auth_header("root")).json()


def test_a_finished_bundled_intake_keeps_its_checked_archive_once(world, switch_on):
    w = world
    _catalogue(w)
    archive = _source_archive()
    sha = hashlib.sha256(archive).hexdigest()
    first = _bundled_update(w, archive)
    assert first["state"] == ps.INTAKE_PINNED, first
    path = pluginbundle.source_object(sha)
    assert path == f"platform/sources/{sha}.tar"
    # MUTATION: drop the keep from finish_intake and nothing is at the path.
    assert w["bucket"].kept == {path: archive}
    version = _store(w["ctx"]).version("builtin:security-audit", first["version_id"])
    assert version["source_sha256"] == sha

    second = _bundled_update(w, archive)
    assert second["state"] == ps.INTAKE_PINNED, second
    assert w["bucket"].kept_writes == [path]

    # The bundle check reads it back from that path.
    class Sources:
        def source(self, source_sha256: str):
            data = w["bucket"].kept.get(pluginbundle.source_object(source_sha256))
            return None if data is None else io.BytesIO(data)

    handle = Sources().source(sha)
    assert handle is not None
    assert pluginbundle.source_files(handle) == {
        "skills/security-audit/SKILL.md": hashlib.sha256(b"# audit\n").hexdigest(),
        "skills/security-audit/validate.cjs": hashlib.sha256(b"// check\n").hexdigest(),
        "LICENSE": hashlib.sha256(b"MIT\n").hexdigest(),
    }


def test_a_bundled_intake_whose_archive_is_not_the_one_checked_is_refused(world, switch_on):
    w = world
    _catalogue(w)
    ended = _bundled_update(w, _source_archive(), source_sha256=hashlib.sha256(b"other").hexdigest())
    assert ended["state"] == ps.INTAKE_REFUSED and ended["refusal"]["code"] == "intake_digest_mismatch"
    assert w["bucket"].kept == {}


def test_another_installs_intake_keeps_no_source(world):
    from .test_plugin_catalogue_routes import installed

    w = world
    installed(w)
    assert w["bucket"].kept == {}
