"""A step's agent plugins at the start of a step (`agent_worker.plugins`).

The owner chose plugins per step, gstack first, baked into the images at a
pinned version (docs/design/agent-plugins.md, "The start of a step"). Held
here, offline, against a fake install folder:

  * only plugins signed with the step are read: the stored work contract's
    digest must be the one in the signed dispatch block;
  * `work/.claude/skills/gstack` links to the install, and ONLY the step's
    skills are linked beside it, a previous attempt's links removed;
  * the environment the packaging doc lists, GSTACK_HOME under work/, and
    gstack's config with the update check, telemetry, codex reviews and the
    design detector off; `.gstack/` excluded from the diff; no settings hook;
  * an image without the install, or without one of the step's skills,
    fails before anything is linked, naming the image;
  * planner and designer modes are refused until the question bridge lands;
  * the record names plugin, version, mode, skills and per-skill run counts.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker import plugins
from agent_worker.errors import ConfigError
from swarm_common.workcontract import contract_digest

SKILLS = ("qa", "review")


def _document(mode: str = "unattended", skills=SKILLS) -> dict[str, Any]:
    return {
        "schema": 1, "contract_id": "wc-review", "tenant_id": "eng", "revision": 1,
        "previous_digest": None, "strictness": "strict",
        "requirements": [{"id": "REQ-1", "text": "Review it.", "verification": []}],
        "repositories": [], "scope": {"read": [], "write": []},
        "protected": {"paths": [], "interfaces": []},
        "capabilities": {"runtime": [], "plugins": {"gstack": {"mode": mode,
                                                               "skills": list(skills)}}},
        "limits": {"runner_profile": "claude-code", "model_class": None, "budget": None,
                   "max_turns": None, "timeout_seconds": None},
        "artifacts": [],
        "ownership": {"owner": "alice@example.com", "depends_on": [], "withdrawn": []},
    }


def _task(document: dict[str, Any] | None = None, *, digest: str | None = None) -> dict[str, Any]:
    document = _document() if document is None else document
    reference = {"id": "wc-review", "revision": 1,
                 "digest": digest if digest is not None else contract_digest(document),
                 "strictness": "strict", "requirements": ["REQ-1"]}
    return {"metadata": {"dispatch": {"strategy": "collect", "work_contract": reference},
                         "work_contract": document}}


@pytest.fixture
def install(tmp_path) -> Path:
    """A fake read-only gstack install: one SKILL.md per catalogue skill."""
    root = tmp_path / "opt" / "gstack"
    for mode_skills in plugins.GSTACK.skills.values():
        for skill in mode_skills:
            (root / skill).mkdir(parents=True, exist_ok=True)
            (root / skill / "SKILL.md").write_text(f"# {skill}\n")
    (root / "bin").mkdir()
    return root


@pytest.fixture
def work(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def checkout(work) -> Path:
    path = work / "repo"
    (path / ".git" / "info").mkdir(parents=True)
    (path / ".git" / "info" / "exclude").write_text("# git ls-files --others\n*.swp")
    return path


def _start(task, work, checkout, install, profile="claude-code", environ=None):
    return plugins.start(task, work=work, checkout=checkout, profile=profile,
                         image="agent-runtime-base", environ=environ or {},
                         install_paths={"gstack": install})


def test_the_catalogue_is_swarm_apis():
    from swarm_api import plugincatalogue

    # MUTATION: let either copy drift (a skill, the version, a profile).
    assert set(plugins.CATALOGUE) == set(plugincatalogue.CATALOGUE)
    for name, entry in plugincatalogue.CATALOGUE.items():
        mine = plugins.CATALOGUE[name]
        assert (mine.version, mine.install_path, mine.runner_profiles) == (
            entry.version, entry.install_path, entry.runner_profiles)
        assert dict(mine.skills) == dict(entry.skills)
    assert plugins.SUPPORTED_MODES == plugincatalogue.SUPPORTED_MODES
    assert "cso" not in plugins.GSTACK.skills["unattended"]


def test_a_step_without_plugins_gets_nothing(work, checkout, install):
    assert _start({"metadata": {"dispatch": {}}}, work, checkout, install) is None
    assert not (work / ".claude").exists()
    # Free-form metadata with no signed reference (the switch off) names nothing.
    task = _task()
    del task["metadata"]["dispatch"]["work_contract"]
    assert _start(task, work, checkout, install) is None
    assert not (work / ".claude").exists()


def test_only_the_steps_skills_are_linked_beside_the_install(work, checkout, install):
    # A previous attempt's links, restored by a checkpoint.
    stale = work / ".claude" / "skills" / "ship"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").symlink_to(install / "ship" / "SKILL.md")
    started = _start(_task(), work, checkout, install)
    assert started is not None
    skills_dir = work / ".claude" / "skills"
    assert (skills_dir / "gstack").is_symlink()
    assert (skills_dir / "gstack").resolve() == install.resolve()
    # MUTATION: link every offered skill and `ship`, `health` ... are here too.
    assert sorted(p.name for p in skills_dir.iterdir()) == ["gstack", "qa", "review"]
    for skill in SKILLS:
        link = skills_dir / skill / "SKILL.md"
        assert link.is_symlink() and link.resolve() == (install / skill / "SKILL.md").resolve()
    assert started.plugins == (plugins.Plugin("gstack", "1.92.55.0", "unattended", SKILLS),)


def test_the_environment_and_gstacks_config(work, checkout, install):
    started = _start(_task(), work, checkout, install)
    home = work / plugins.HOME_DIR_NAME
    # MUTATION: drop a variable, or put GSTACK_HOME outside work/ (not checkpointed).
    assert dict(started.env) == {
        "GSTACK_SESSION_KIND": "unattended",
        "GSTACK_HOME": str(home),
        "GSTACK_EPHEMERAL": "1",
        "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "0",
    }
    config = (home / "config.yaml").read_text()
    for line in ("update_check: false", "telemetry: off", "codex_reviews: disabled",
                 "design_detector: off"):
        assert line in config.splitlines()


def test_no_gstack_settings_hook_is_registered(work, checkout, install):
    _start(_task(), work, checkout, install)
    # MUTATION: run gstack's setup, or write a Stop hook, and a settings file appears.
    assert not (work / ".claude" / "settings.json").exists()
    assert not (work / ".claude" / "settings.local.json").exists()
    assert not any(p.name.startswith("settings") for p in (work / ".claude").iterdir())


def test_gstacks_project_folder_is_kept_out_of_the_diff_once(work, checkout, install):
    _start(_task(), work, checkout, install)
    _start(_task(), work, checkout, install)
    lines = (checkout / ".git" / "info" / "exclude").read_text().splitlines()
    # MUTATION: skip `exclude_project_state` and .gstack/ is not excluded.
    assert lines.count(".gstack/") == 1
    assert lines[:2] == ["# git ls-files --others", "*.swp"]


def test_the_browser_profile_drives_the_images_chromium(work, checkout, install, tmp_path):
    chrome = tmp_path / "playwright" / "chromium-1243" / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    started = _start(_task(), work, checkout, install, profile="claude-code-browser",
                     environ={"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "playwright")})
    assert started.env["GSTACK_CHROMIUM_PATH"] == str(chrome)
    assert started.env["GSTACK_CHROMIUM_NO_SANDBOX"] == "1"
    # Not on a profile with no browser.
    other = _start(_task(), work, checkout, install)
    assert "GSTACK_CHROMIUM_PATH" not in other.env


def test_a_browser_image_without_chromium_fails_naming_it(work, checkout, install, tmp_path):
    with pytest.raises(plugins.PluginUnavailable, match="agent-runtime-base has no Chromium"):
        _start(_task(), work, checkout, install, profile="claude-code-browser",
               environ={"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "none")})
    assert not (work / ".claude").exists()


def test_an_image_without_the_install_fails_before_anything_is_linked(work, checkout, tmp_path):
    with pytest.raises(plugins.PluginUnavailable) as raised:
        _start(_task(), work, checkout, tmp_path / "missing")
    message = str(raised.value)
    # MUTATION: link first and check after, and a half-linked folder is left.
    assert "the image agent-runtime-base has no gstack 1.92.55.0 install" in message
    assert not (work / ".claude").exists()
    assert not (work / plugins.HOME_DIR_NAME).exists()
    # A ConfigError: the step ends CANNOT_START, never a retry on the same image.
    assert isinstance(raised.value, ConfigError)


def test_an_install_missing_one_of_the_steps_skills_links_none(work, checkout, install):
    (install / "qa" / "SKILL.md").unlink()
    with pytest.raises(plugins.PluginUnavailable, match="lacks the skills qa"):
        _start(_task(), work, checkout, install)
    assert not (work / ".claude").exists()


@pytest.mark.parametrize("mode, skills", [("planner", ("spec",)), ("designer", ("design-html",))])
def test_planner_and_designer_wait_for_the_question_bridge(work, checkout, install, mode, skills):
    with pytest.raises(plugins.PluginUnavailable, match="question bridge"):
        _start(_task(_document(mode, skills)), work, checkout, install)
    assert not (work / ".claude").exists()


def test_a_skill_outside_the_catalogue_or_a_profile_that_cannot_load_it_is_refused(
        work, checkout, install):
    with pytest.raises(plugins.PluginUnavailable, match="cso"):
        _start(_task(_document(skills=("review", "cso"))), work, checkout, install)
    with pytest.raises(plugins.PluginUnavailable, match="codex"):
        _start(_task(), work, checkout, install, profile="codex")


def test_a_contract_that_does_not_match_its_signed_digest_is_refused(work, checkout, install):
    task = _task()
    # A tenant worker adds a skill to the stored document; the signature stays.
    forged = copy.deepcopy(task)
    forged["metadata"]["work_contract"]["capabilities"]["plugins"]["gstack"]["skills"].append(
        "ship")
    # MUTATION: skip the digest check and `ship` is linked.
    with pytest.raises(plugins.PluginUnavailable, match="does not match the digest"):
        _start(forged, work, checkout, install)
    assert not (work / ".claude").exists()


def test_the_guidance_makes_an_open_question_an_owner_question(work, checkout, install):
    started = _start(_task(), work, checkout, install)
    text = started.guidance
    assert "gstack 1.92.55.0, unattended mode, with only these skills: qa, review." in text
    assert "questions.json" in text and "never answer it yourself" in text


def test_the_record_counts_each_skills_runs(work, checkout, install, tmp_path):
    started = _start(_task(), work, checkout, install)
    stream = tmp_path / "claude-code.stdout.log"

    def call(skill: str) -> str:
        return json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": skill}}]}})

    stream.write_text("\n".join([
        call("review"), call("gstack:review"), call("qa"), call("health"),
        "not json", json.dumps({"type": "result"}),
    ]) + "\n")
    # MUTATION: record names only, or count every call, and this differs.
    assert plugins.record(started, stream) == {"gstack": {
        "version": "1.92.55.0", "mode": "unattended", "skills": ["qa", "review"],
        "skill_runs": {"qa": 1, "review": 2}}}
    assert plugins.record(started, tmp_path / "no-such.log")["gstack"]["skill_runs"] == {
        "qa": 0, "review": 0}
    assert plugins.record(None, stream) is None


LIFECYCLE = Path(plugins.__file__).with_name("lifecycle.py")


def test_the_lifecycle_starts_the_plugins_before_the_agent_and_records_them():
    text = LIFECYCLE.read_text()
    # MUTATION: drop any of the four calls and the step runs without its
    # skills, its variables, its prompt section or its record.
    start = text.index("self._plugins = plugins_mod.start(")
    # Before input.json, which the runner reads its prompt from.
    assert start < text.index("ws.input_path.write_text(json.dumps(payload", start)
    assert "payload[\"prompt\"] += self._plugins.guidance" in text
    assert "base.update(self._plugins.env)" in text
    assert "summary[plugins_mod.SUMMARY_KEY] = plugins_record" in text
