"""A bundled built-in plugin at the start of a step: checked in its image, nothing fetched.

docs/design/managed-plugins.md section 1.4 (lane 19, "Worker: bundled
entries and the review"). With the built-ins switch on, swarm-api signs
gstack and security-audit as managed entries with `build: "image"`
(`pluginconfig.bundled_entry`), keyed on their reserved install ids. Held
offline here against the entry swarm-api itself signs and the manifest the
bundle writer itself writes (images/plugin-bundler/swarm-bundle):

  * a bundled entry whose `.swarm-bundle.json` matches its pin is linked
    from the image as the code entry is, and nothing is fetched for it;
  * a missing manifest, or one naming another install, source or tree,
    refuses the step with a fixed code before anything is linked;
  * the code entry (`capabilities.plugins`) loads as it does today, and
    gstack's environment is keyed on its reserved install id on both routes;
  * the security review applies on both routes and checks the manifest
    before its validators run;
  * with the switch off (no `build: "image"` entry signed) nothing changes:
    no manifest is read, and the managed loader sees what it saw before.
"""

from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker import pluginload, plugins
from agent_worker import security_review as sr
from agent_worker.errors import ConfigError
from swarm_common.workcontract import contract_digest

REPO = Path(__file__).resolve().parents[3]
WRITER = REPO / "images" / "plugin-bundler" / "swarm-bundle"
PINS = REPO / "images" / "agent-runtime-base" / "bundled-plugins.json"
GSTACK_ID = "builtin:gstack"
AUDIT_ID = "builtin:security-audit"


def _writer():
    name = "swarm_bundle_writer"
    if name in sys.modules:
        return sys.modules[name]
    loader = importlib.machinery.SourceFileLoader(name, str(WRITER))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def _signed(name: str, wanted: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The entry swarm-api signs for a bundled install's current version."""
    from swarm_api import pluginconfig, plugincatalogue

    pin = plugincatalogue.BUNDLED_BY_ID[plugincatalogue.bundled_id(name)]
    install, version = plugincatalogue.seed_records(pin, datetime.now(timezone.utc))
    entry, missing = pluginconfig.step_entry(install, version, {})
    assert missing is None
    return pin.install_id, pluginconfig.bundled_entry(entry, version, wanted)


def _task(block: dict[str, Any] | None = None, *, contract: dict[str, Any] | None = None) -> dict[str, Any]:
    dispatch: dict[str, Any] = {}
    metadata: dict[str, Any] = {"dispatch": dispatch}
    task: dict[str, Any] = {"id": "t1", "tenant_id": "eng", "metadata": metadata,
                            "spec_signature": secrets.token_hex(32), "spec_key_version": "spec-key/1"}
    if block is not None:
        dispatch["plugins"] = block
    if contract is not None:
        document = {
            "schema": 1, "contract_id": "wc-review", "tenant_id": "eng", "revision": 1,
            "previous_digest": None, "strictness": "strict",
            "requirements": [{"id": "REQ-1", "text": "Review it.", "verification": []}],
            "repositories": [], "scope": {"read": [], "write": []},
            "protected": {"paths": [], "interfaces": []},
            "capabilities": {"runtime": [], "plugins": contract},
            "limits": {"runner_profile": "claude-code", "model_class": None, "budget": None,
                       "max_turns": None, "timeout_seconds": None},
            "artifacts": [],
            "ownership": {"owner": "owner@example.invalid", "depends_on": [], "withdrawn": []},
        }
        metadata["work_contract"] = document
        dispatch["work_contract"] = {"id": "wc-review", "revision": 1, "digest": contract_digest(document),
                                     "strictness": "strict", "requirements": ["REQ-1"]}
    return task


def _gstack_tree(root: Path) -> Path:
    for mode_skills in plugins.GSTACK.skills.values():
        for skill in mode_skills:
            (root / skill).mkdir(parents=True, exist_ok=True)
            (root / skill / "SKILL.md").write_text(f"# {skill}\n")
    (root / "bin").mkdir(exist_ok=True)
    return root


def _audit_tree(root: Path) -> Path:
    skill = root / "skills" / "security-audit"
    skill.mkdir(parents=True)
    for name in ("SKILL.md", sr.VALIDATE_FINDINGS, sr.VALIDATE_LEDGER):
        (skill / name).write_text("x\n")
    (root / "LICENSE").write_text("MIT\n")
    return root


def _manifest(root: Path, name: str, **change: Any) -> dict[str, Any]:
    """Write the tree's `.swarm-bundle.json` as the bundle writer writes it,
    with the release's pins and `root` as its install path."""
    writer = _writer()
    pins = copy.deepcopy(writer.plugin_pins(PINS, name))
    pins["install_path"] = str(root)
    recipe = root.parent / f"{name}.recipe"
    recipe.write_text("#!/bin/sh\n")
    document = writer.manifest(name, pins, [root], {"build": recipe})
    for key, value in change.items():
        document[key] = value
    (root / pluginload.BUNDLE_MANIFEST).write_text(json.dumps(document))
    return document


class NoStore:
    """An object store that fails the test when anything is fetched."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a bundled plugin fetched something ({name})")


@pytest.fixture
def opt(tmp_path: Path) -> Path:
    return tmp_path / "opt" / "swarm" / "plugins"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def checkout(work: Path) -> Path:
    path = work / "repo"
    (path / ".git" / "info").mkdir(parents=True)
    return path


def _start(task: dict[str, Any], work: Path, checkout: Path, opt: Path, **kwargs: Any):
    paths = {"gstack": opt / "gstack", "security-audit": opt / "security-audit"}
    return plugins.start(task, work=work, checkout=checkout, profile="claude-code",
                         image="agent-runtime-base", environ={}, install_paths=paths,
                         private_dir=work.parent / "private", store=NoStore(), **kwargs)


def _refused(excinfo: pytest.ExceptionInfo, code: str, install_id: str) -> None:
    exc = excinfo.value
    assert isinstance(exc, pluginload.PluginRefused) and isinstance(exc, ConfigError)
    assert exc.code == code
    assert str(exc) == pluginload.REFUSALS[code]
    assert exc.detail()["install_id"] == install_id


# --- the signed entry ------------------------------------------------------------


def test_the_entry_swarm_api_signs_for_a_bundled_install_is_read_as_bundled():
    install_id, entry = _signed("security-audit")
    task = _task({install_id: entry})
    (found,) = pluginload.bundled(task)
    assert found.install_id == AUDIT_ID and found.name == "security-audit"
    assert found.source_sha256 == entry["source_sha256"]
    assert found.mode == "unattended" and found.skills == ("security-audit",)
    # Nothing to fetch: the managed loader sees no entry and loads nothing.
    assert pluginload.entries(task) == ()
    assert pluginload.load(task, private_dir=Path("/nonexistent"), store=NoStore(), environ={}) is None
    assert not pluginload.wants_release(task)


@pytest.mark.parametrize("change", [
    {"runnable_sha256": "a" * 64},
    {"object": "platform/x.tar"},
    {"scope": "workspace"},
    {"name": "gstack"},
    {"source_sha256": "not-a-digest"},
    {"skills": []},
    {"mode": None},
    {"tree_sha256": "short"},
])
def test_a_bundled_entry_not_in_the_signed_shape_is_refused(change):
    install_id, entry = _signed("security-audit")
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        pluginload.bundled(_task({install_id: {**entry, **change}}))
    assert excinfo.value.code == "plugin_set_invalid"


def test_a_bundled_entry_with_settings_is_refused_with_its_reason():
    install_id, entry = _signed("gstack", {"mode": "unattended", "skills": ["qa"]})
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        pluginload.bundled(_task({install_id: {**entry, "env": {"GSTACK_FLAG": "1"}}}))
    _refused(excinfo, "plugin_bundle_settings", GSTACK_ID)


# --- a bundled entry loads from its image -----------------------------------------


def test_a_bundled_entry_whose_manifest_matches_its_pin_loads_with_no_fetch(work, checkout, opt):
    _manifest(_gstack_tree(opt / "gstack"), "gstack")
    install_id, entry = _signed("gstack", {"mode": "unattended", "skills": ["qa", "review"]})

    started = _start(_task({install_id: entry}), work, checkout, opt)

    (plugin,) = started.plugins
    assert (plugin.name, plugin.install_id, plugin.version_id) == ("gstack", GSTACK_ID, entry["version_id"])
    assert plugin.skills == ("qa", "review") and plugin.mode == "unattended"
    assert started.managed == () and not (work.parent / "private").exists()
    skills = work / ".claude" / "skills"
    assert (skills / "gstack").resolve() == (opt / "gstack").resolve()
    assert sorted(p.name for p in skills.iterdir()) == ["gstack", "qa", "review"]
    # gstack's environment, keyed on its reserved install id.
    assert started.env["GSTACK_HOME"] == str(work / plugins.HOME_DIR_NAME)
    assert started.env["GSTACK_SESSION_KIND"] == "unattended"
    record = plugins.record(started, None)
    assert record["gstack"]["install_id"] == GSTACK_ID
    assert record["gstack"]["version_id"] == entry["version_id"]


def test_a_signed_tree_digest_that_matches_the_manifest_loads(work, checkout, opt):
    written = _manifest(_audit_tree(opt / "security-audit"), "security-audit")
    install_id, entry = _signed("security-audit")
    entry = {**entry, "tree_sha256": written["tree"]["sha256"], "recipe_digest": written["recipe"]["digest"]}
    started = _start(_task({install_id: entry}), work, checkout, opt)
    assert [p.install_id for p in started.plugins] == [AUDIT_ID]


@pytest.mark.parametrize("change", [
    {"install_id": "builtin:gstack"},
    {"plugin": "gstack"},
    {"source": {"sha256": "0" * 64}},
    {"install_path": "/elsewhere/security-audit"},
    {"tree": {"sha256": "short"}},
    {"recipe": {}},
])
def test_a_manifest_that_does_not_match_its_pin_refuses_before_anything_is_linked(work, checkout, opt, change):
    _manifest(_audit_tree(opt / "security-audit"), "security-audit", **change)
    install_id, entry = _signed("security-audit")
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _start(_task({install_id: entry}), work, checkout, opt)
    _refused(excinfo, "plugin_bundle_mismatch", AUDIT_ID)
    assert not (work / ".claude" / "skills").exists()


@pytest.mark.parametrize("signed", ["tree_sha256", "recipe_digest"])
def test_a_signed_digest_the_manifest_does_not_carry_is_refused(work, checkout, opt, signed):
    _manifest(_audit_tree(opt / "security-audit"), "security-audit")
    install_id, entry = _signed("security-audit")
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _start(_task({install_id: {**entry, signed: "f" * 64}}), work, checkout, opt)
    _refused(excinfo, "plugin_bundle_mismatch", AUDIT_ID)


def test_an_image_without_the_manifest_refuses_with_its_reason(work, checkout, opt):
    _audit_tree(opt / "security-audit")
    install_id, entry = _signed("security-audit")
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _start(_task({install_id: entry}), work, checkout, opt)
    _refused(excinfo, "plugin_bundle_manifest_missing", AUDIT_ID)
    assert not (work / ".claude" / "skills").exists()


def test_a_manifest_that_is_a_link_is_not_read(work, checkout, opt, tmp_path):
    root = _audit_tree(opt / "security-audit")
    _manifest(root, "security-audit")
    elsewhere = tmp_path / "copied.json"
    (root / pluginload.BUNDLE_MANIFEST).rename(elsewhere)
    (root / pluginload.BUNDLE_MANIFEST).symlink_to(elsewhere)
    install_id, entry = _signed("security-audit")
    with pytest.raises(pluginload.PluginRefused) as excinfo:
        _start(_task({install_id: entry}), work, checkout, opt)
    _refused(excinfo, "plugin_bundle_manifest_missing", AUDIT_ID)


def test_one_plugin_named_on_both_routes_is_refused(work, checkout, opt):
    _manifest(_audit_tree(opt / "security-audit"), "security-audit")
    install_id, entry = _signed("security-audit")
    task = _task({install_id: entry},
                 contract={"security-audit": {"mode": "unattended", "skills": ["security-audit"]}})
    with pytest.raises(plugins.PluginUnavailable):
        _start(task, work, checkout, opt)


# --- the code entry, as today ------------------------------------------------------


def test_a_code_entry_loads_as_today_and_reads_no_manifest(work, checkout, opt):
    _gstack_tree(opt / "gstack")  # no .swarm-bundle.json: the code route never reads one
    started = _start(_task(contract={"gstack": {"mode": "unattended", "skills": ["qa"]}}), work, checkout, opt)
    (plugin,) = started.plugins
    assert (plugin.name, plugin.version, plugin.install_id) == ("gstack", plugins.GSTACK.version, GSTACK_ID)
    assert plugin.version_id is None
    assert started.env["GSTACK_HOME"] == str(work / plugins.HOME_DIR_NAME)
    assert plugins.record(started, None)["gstack"] == {
        "version": plugins.GSTACK.version, "mode": "unattended", "skills": ["qa"], "skill_runs": {"qa": 0}}


def test_with_the_switch_off_a_managed_only_set_is_read_as_before():
    """Switch off: swarm-api signs no `build: "image"` entry, so the set holds
    managed entries alone and the loader reads them exactly as it did."""
    digest = "d" * 64
    managed = {"name": "jev-belay", "scope": "platform", "version_id": "v-" + "c" * 12,
               "runnable_sha256": digest, "object": f"platform/{digest}.tar", "env": {}, "files": {},
               "secrets": {}, "skills": None, "disabled_skills": []}
    task = _task({"p-" + "a" * 12: managed})
    assert [e.install_id for e in pluginload.entries(task)] == ["p-" + "a" * 12]
    assert pluginload.bundled(task) == ()


# --- the security review -------------------------------------------------------------


def test_the_validators_sit_at_the_known_path():
    assert sr.INSTALL == Path("/opt/swarm/plugins/security-audit")
    assert sr.install_dir() == sr.INSTALL
    assert sr.skill_dir(Path("/nonexistent/security-audit")) == Path("/nonexistent/security-audit")


def test_the_security_review_applies_on_both_routes(work, checkout, opt):
    _manifest(_audit_tree(opt / "security-audit"), "security-audit")
    install_id, entry = _signed("security-audit")
    bundled = _start(_task({install_id: entry}), work, checkout, opt)
    code = _start(_task(contract={"security-audit": {"mode": "unattended", "skills": ["security-audit"]}}),
                  work, checkout, opt)
    assert sr.applies(bundled) and sr.applies(code)
    assert sr.signed_entry(bundled).install_id == AUDIT_ID
    assert sr.signed_entry(code) is None


class Validators:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "PASS", "")


def _audit_output(artifacts: Path) -> None:
    out = sr.output_dir(artifacts)
    out.mkdir(parents=True)
    (out / sr.FINDINGS).write_text("[]")
    (out / sr.LEDGER).write_text("[]")


def test_the_review_checks_the_manifest_before_the_validators_run(work, checkout, opt, tmp_path):
    root = _audit_tree(opt / "security-audit")
    _manifest(root, "security-audit")
    install_id, entry = _signed("security-audit")
    started = _start(_task({install_id: entry}), work, checkout, opt)
    artifacts = tmp_path / "artifacts"
    _audit_output(artifacts)
    run = Validators()
    record = sr.finish(artifacts, task_id="t1", step_id="security-review", skill=sr.skill_dir(root),
                       node="/usr/bin/node", run=run, started=started)
    assert record["verdict"] == "MERGE" and len(run.calls) == 2

    # The manifest no longer matches the signed pin: the validators never run.
    _manifest(root, "security-audit", source={"sha256": "0" * 64})
    run = Validators()
    with pytest.raises(sr.SecurityReviewInvalid) as excinfo:
        sr.finish(artifacts, task_id="t1", step_id="security-review", skill=sr.skill_dir(root),
                  node="/usr/bin/node", run=run, started=started)
    assert "plugin_bundle_mismatch" in str(excinfo.value)
    assert run.calls == []
    assert not (artifacts / sr.VERDICT_FILE).exists()


def test_the_review_on_the_code_route_runs_the_validators_as_today(work, checkout, opt, tmp_path):
    root = _audit_tree(opt / "security-audit")
    started = _start(_task(contract={"security-audit": {"mode": "unattended", "skills": ["security-audit"]}}),
                     work, checkout, opt)
    artifacts = tmp_path / "artifacts"
    _audit_output(artifacts)
    run = Validators()
    sr.finish(artifacts, task_id="t1", step_id="security-review", skill=sr.skill_dir(root),
              node="/usr/bin/node", run=run, started=started)
    assert len(run.calls) == 2


def test_the_worker_hands_the_review_its_started_plugins(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_worker import lifecycle

    seen: dict[str, Any] = {}

    def finish(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"verdict": "MERGE", "blocking": 0, "owner_questions": 0}

    monkeypatch.setattr(lifecycle.security_review_mod, "finish", finish)
    started = object()
    worker = SimpleNamespace(ws=SimpleNamespace(artifacts=tmp_path), _security_review={}, _plugins=started,
                             _task={"step_id": "security-review"}, cfg=SimpleNamespace(task_id="t1"),
                             log=SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None))
    lifecycle.Worker._finish_security_review(worker)
    assert seen["started"] is started
    assert seen["skill"] == sr.skill_dir(sr.INSTALL)
