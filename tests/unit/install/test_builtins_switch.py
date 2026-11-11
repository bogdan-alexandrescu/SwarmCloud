"""`swarm upgrade --builtins-managed on|off`: a platform admin of an existing
deployment turns the built-ins switch on or off (managed-plugins.md sections
1.4.5 and 1.4.7; the owner's choice of 10 November 2026: an installer flag).

The choice is recorded in the install state, rendered into infra's tfvars on
the next infra pass, refused "on" for a release with no bundler image
(upgrade.py's own check) and refused "off" while a built bundle is retained.
A fresh install still renders "on" (Day One)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from swarm_install import tfvars
from swarm_install import upgrade as up
from swarm_install.exitcodes import ExitCode
from swarm_install.state import InstallState, StateStore
from swarm_install.verbs import upgrade as verb

from .test_tfvars import EVERY_VERSION
from .test_terraform_runner import install_state
from .test_upgrade_engine import NEW, OLD, PROJECT, FakeBucket, FakeLease, args_for

DIGEST = "sha256:" + "a" * 64
WITH_BUNDLER = {"swarm-api": DIGEST, up.BUNDLER_IMAGE: DIGEST}
WITHOUT_BUNDLER = {"swarm-api": DIGEST}


def _rendered(state: InstallState) -> str:
    return tfvars.infra_inputs(state, pass_name="infra-4", installer="admin@example.com",
                               versions=EVERY_VERSION)["builtins_managed"]


# -- tfvars: the recorded choice renders on the next infra pass ---------------------------


def test_the_admins_choice_renders_over_what_infra_last_applied() -> None:
    for applied, chosen in (("off", "on"), ("on", "off"), (None, "on"), (None, "off")):
        outputs = {tfvars.BUILTINS_MANAGED_CHOICE_KEY: chosen}
        if applied:
            outputs[tfvars.BUILTINS_MANAGED_KEY] = applied
        state = install_state(**outputs)
        state.ready_at = 1.0
        assert tfvars.builtins_managed(state) == chosen
        assert _rendered(state) == chosen


def test_day_one_a_fresh_install_still_renders_on() -> None:
    assert _rendered(install_state()) == "on"
    ready = install_state()
    ready.ready_at = 1.0
    assert _rendered(ready) == "off", "an existing deployment without a choice keeps off"


# -- the verb -----------------------------------------------------------------------------


class Documents:
    def __init__(self, bundles: dict[str, dict[str, Any]] | None = None) -> None:
        self.data = bundles or {}

    def list(self, collection: str):
        for path in sorted(self.data):
            if path.startswith(collection + "/"):
                yield path, json.loads(json.dumps(self.data[path])), 0


def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, release: str = NEW, ready: bool = True,
         images: dict[str, dict[str, str]] | None = None, bundles: dict[str, dict[str, Any]] | None = None,
         channel: str = NEW) -> dict[str, Any]:
    from swarm_install.steps import preflight
    from swarm_install.verbs import install

    seen: dict[str, Any] = {"ran": [], "fetched": []}
    local = tmp_path / "state.json"
    store = StateStore(project=PROJECT, environment="prod", local=local)
    state = InstallState(project=PROJECT, environment="prod", release=release, region="us-central1")
    state.ready_at = 1.0 if ready else None
    store.save(state)
    published = images if images is not None else {NEW: WITH_BUNDLER, OLD: WITH_BUNDLER}

    class World:
        def __init__(self, *args: Any) -> None:
            pass

        def read_public(self, url: str) -> bytes:
            return f"{channel}\n".encode()

        def fetch(self, version: str, name: str) -> bytes:
            seen["fetched"].append((version, name))
            if name == "RELEASE.md":
                return b"# notes\n"
            assert name == "images.json"
            return json.dumps(published[version]).encode()

        def documents(self) -> Documents:
            return Documents(bundles)

        def _release(self) -> object:
            return object()

        def close_reach(self) -> None:
            return None

    monkeypatch.setattr(verb, "GcsObjectStore", FakeBucket)
    monkeypatch.setattr(verb, "StateStore", lambda **kw: StateStore(project=PROJECT, environment="prod",
                                                                    local=local))
    monkeypatch.setattr(verb, "Lease", FakeLease)
    monkeypatch.setattr(verb, "Engine", lambda *a, **kw: object())
    monkeypatch.setattr(verb, "LiveWorld", World)
    monkeypatch.setattr(install, "holder_identity", lambda: "admin@example.com")
    monkeypatch.setattr(install, "_typed", lambda prompt: "")
    monkeypatch.setattr(preflight, "installed_release", lambda: NEW)
    monkeypatch.setattr(verb, "ensure_engine", lambda target, args, **kw: None)

    class Flow:
        def __init__(self, **kw: Any) -> None:
            self.store = kw["store"]

        def run(self, target: str, *, allow_downgrade: bool = False) -> ExitCode:
            # What the upgrade's infra pass would render, read from the state it runs on.
            seen["rendered"] = tfvars.builtins_managed(self.store.load())
            seen["ran"].append(target)
            return ExitCode.OK

    monkeypatch.setattr(verb, "Upgrade", Flow)
    # A fresh store on every read: one StateStore caches the state it last saw.
    seen["store"] = lambda: StateStore(project=PROJECT, environment="prod", local=local)
    return seen


def _choice(seen: dict[str, Any]) -> Any:
    return seen["store"]().load().outputs.get(tfvars.BUILTINS_MANAGED_CHOICE_KEY)


def test_the_flag_is_on_or_off_and_optional() -> None:
    assert args_for("--project", PROJECT).builtins_managed is None
    assert args_for("--project", PROJECT, "--builtins-managed", "on").builtins_managed == "on"
    assert args_for("--project", PROJECT, "--builtins-managed", "off").builtins_managed == "off"
    with pytest.raises(SystemExit):
        args_for("--project", PROJECT, "--builtins-managed", "maybe")


@pytest.mark.parametrize("value", ["on", "off"])
def test_on_the_running_release_the_flag_records_the_choice_and_infra_renders_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str) -> None:
    seen = live(tmp_path, monkeypatch)
    assert verb.run(args_for("--project", PROJECT, "--builtins-managed", value)) == ExitCode.OK
    assert _choice(seen) == value
    assert tfvars.builtins_managed(seen["store"]().load()) == value
    assert seen["ran"] == [], "no release to upgrade to: the switch alone is recorded"
    out = capsys.readouterr().out
    assert f"builtins_managed {value}" in out and "next infra pass" in out


def test_with_an_upgrade_the_choice_is_recorded_before_its_infra_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = live(tmp_path, monkeypatch, release=OLD)
    assert verb.run(args_for("--to", NEW, "--project", PROJECT, "--builtins-managed", "on")) == ExitCode.OK
    assert seen["ran"] == [NEW] and seen["rendered"] == "on"
    assert (NEW, "images.json") in seen["fetched"], "the release checked is the one the upgrade puts live"


def test_on_without_a_bundler_image_is_refused_with_its_fixed_code(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen = live(tmp_path, monkeypatch, images={NEW: WITHOUT_BUNDLER})
    assert verb.run(args_for("--project", PROJECT, "--builtins-managed", "on")) == ExitCode.REFUSED
    assert verb.NO_BUNDLER in capsys.readouterr().err
    assert _choice(seen) is None and seen["ran"] == []


def test_on_is_refused_for_an_upgrade_to_a_release_without_a_bundler_image(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen = live(tmp_path, monkeypatch, release=OLD, images={NEW: WITHOUT_BUNDLER, OLD: WITH_BUNDLER})
    assert verb.run(args_for("--to", NEW, "--project", PROJECT, "--builtins-managed", "on")) == ExitCode.REFUSED
    assert verb.NO_BUNDLER in capsys.readouterr().err
    assert _choice(seen) is None and seen["ran"] == []


def test_the_bundler_check_is_upgrades_own() -> None:
    assert up.carries_bundle_manifests(WITH_BUNDLER) is True
    assert up.carries_bundle_manifests(WITHOUT_BUNDLER) is False


def test_off_while_a_built_bundle_is_retained_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    bundles = {f"{up.BUNDLES_COLLECTION}/b1": {"state": "ready"}}
    seen = live(tmp_path, monkeypatch, bundles=bundles)
    assert verb.run(args_for("--project", PROJECT, "--builtins-managed", "off")) == ExitCode.REFUSED
    assert verb.BUNDLES_RETAINED in capsys.readouterr().err
    assert _choice(seen) is None


def test_off_with_only_failed_or_retired_bundles_goes_ahead(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundles = {f"{up.BUNDLES_COLLECTION}/b1": {"state": "failed"}, f"{up.BUNDLES_COLLECTION}/b2": {"state": "retired"}}
    seen = live(tmp_path, monkeypatch, bundles=bundles)
    assert verb.run(args_for("--project", PROJECT, "--builtins-managed", "off")) == ExitCode.OK
    assert _choice(seen) == "off"


def test_an_install_still_running_is_left_to_day_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen = live(tmp_path, monkeypatch, ready=False)
    assert verb.run(args_for("--project", PROJECT, "--builtins-managed", "off")) == ExitCode.REFUSED
    assert verb.NOT_INSTALLED in capsys.readouterr().err
    assert _choice(seen) is None
    assert tfvars.builtins_managed(seen["store"]().load()) == "on"


def test_a_resumed_run_keeps_the_recorded_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = live(tmp_path, monkeypatch, release=OLD)
    assert verb.run(args_for("--to", NEW, "--project", PROJECT, "--builtins-managed", "on")) == ExitCode.OK

    def mid_upgrade(state: InstallState) -> None:
        state.outputs[up.UPGRADE_KEY] = {"to": NEW, "from": OLD, "done": [up.DRAIN], "failed": up.INFRA}

    seen["store"]().update(mid_upgrade)
    assert verb.run(args_for("--to", NEW, "--project", PROJECT)) == ExitCode.OK
    assert seen["ran"] == [NEW, NEW] and seen["rendered"] == "on" and _choice(seen) == "on"


def test_the_flag_rides_the_reexec_into_the_targets_engine() -> None:
    args = args_for("--to", NEW, "--project", PROJECT, "--builtins-managed", "off")
    argv = verb.reexec_argv(Path("/opt/swarm"), NEW, args)
    assert argv[argv.index("--builtins-managed") + 1] == "off"
    assert "--builtins-managed" not in verb.reexec_argv(Path("/opt/swarm"), NEW, args_for("--to", NEW, "--project",
                                                                                          PROJECT))
