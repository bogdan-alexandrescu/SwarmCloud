"""A step's agent plugins at the start of a step: gstack's and the security audit skill's skills, linked from the image's read-only install.

The owner (3 November 2026) chose plugins per step, gstack first, baked
into the agent images at a pinned version (docs/design/agent-plugins.md, "The
start of a step"; docs/design/agent-plugins-packaging.md, "The image build
proposal"). This is the worker's half, modelled on `qualitygates.prepare`:
whether this step uses a plugin, the links and the environment before the
agent, and the record after.

WHICH PLUGINS. Only what was signed with the step. swarm-api stores the
step's work contract at `metadata.work_contract` and writes its digest into
the signed dispatch block (`metadata.dispatch.work_contract`); the worker
digests the stored document with `swarm_common.workcontract.contract_digest`
and reads `capabilities.plugins` only when the digest is the signed one. A
document that names plugins and does not match is refused: a tenant worker
can write the task document, never the signature. Free-form metadata with no
signed reference (SWARM_WORK_CONTRACTS off) names nothing.

CHECKED AGAIN HERE against this worker's own catalogue (`CATALOGUE`, which
restates `swarm_api.plugincatalogue`; a unit test holds them equal): a
catalogue name, a profile that loads it, unattended mode (planner and
designer interview a person and need the question bridge, which has not
landed), and skills its mode offers. Submission already refused anything
else, so a refusal here means a worker and a swarm-api that disagree.

THE INSTALL MUST BE THERE, WHOLE, BEFORE ANYTHING IS LINKED. The image
carries gstack at `/opt/swarm/plugins/gstack`, root-owned and read-only. An
image without it, or without one of the step's skills, fails the step before
the agent runs (`PluginUnavailable`, CANNOT_START) with a reason naming the
image: never an agent with a half-linked skill list.

THE LINKS. gstack's rendered skills call the literal path
`~/.claude/skills/gstack/...`, and `HOME` is the attempt's `work/`, so
`work/.claude/skills/gstack` links to the install and
`work/.claude/skills/<skill>/SKILL.md` links to the install's skill, for
ONLY the step's skills: the agent's skill list, and its prompt, carry
nothing else. Links a previous attempt left (a checkpoint restores them) are
removed first, so a resumed step never keeps a skill it no longer names.
Cloudflare's security audit skill (the second plugin, owner, 4 November
2026) is one folder whose SKILL.md reads the method files and validators
beside it by relative path, so `work/.claude/skills/security-audit` links
to that whole read-only folder instead; there is no plugin-root link.

THE ENVIRONMENT (the packaging doc's list), for a step that runs gstack
only. `GSTACK_SESSION_KIND` for the
mode; `GSTACK_HOME` under `work/` (checkpointed, so a resumed step keeps
gstack's own state); `GSTACK_EPHEMERAL=1`; `BUN_RUNTIME_TRANSPILER_CACHE_PATH=0`
(Bun's cache would otherwise land in the checkpointed HOME); and on the
browser profile `GSTACK_CHROMIUM_PATH` (the image's Playwright Chromium, as
`livegate.find_chromium` finds it) and `GSTACK_CHROMIUM_NO_SANDBOX=1`.
`GSTACK_HOME/config.yaml` turns off the update check, telemetry, the codex
second-opinion reviews and the design detector (which otherwise downloads a
binary and records a consent nobody gave). No outside AI at first.

NO HOOKS. gstack's setup registers a Stop hook in `~/.claude/settings.json`;
nothing here writes a settings file, and the runner keeps writing its own,
so no gstack settings hook is registered. `.gstack/` is added to the
checkout's `.git/info/exclude`, so nothing gstack writes in the project
reaches the step's diff.

AN OPEN QUESTION ENDS AS AN OWNER QUESTION. In unattended mode gstack answers
what carries a recommendation and leaves consents, unrecommended questions
and final gates open. Until the question bridge parks the step on them, the
prompt's last section (`guidance`) tells the agent to put each one in
`questions.json` for the owner, never to answer it itself.

THE RECORD. `record` writes, under the attempt summary's `plugins`, each
plugin's version, mode and skills and how many times each skill ran, counted
from the agent's stream (`Skill` tool calls). Names and counts only.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from swarm_common.specsign import SpecNotCanonical
from swarm_common.workcontract import contract_digest

from . import pluginload
from .errors import ConfigError


@dataclass(frozen=True)
class PluginEntry:
    """One catalogue plugin, as `swarm_api.plugincatalogue.PluginEntry`."""

    name: str
    version: str
    install_path: str
    runner_profiles: tuple[str, ...]
    skills: Mapping[str, tuple[str, ...]] = field(hash=False)
    #: Where whole skill folders sit under the install, or None for gstack's
    #: shape (`link_skills` says how each is linked).
    skill_folders: str | None = None


#: `swarm_api.plugincatalogue`, restated (the worker imports nothing from
#: swarm-api); tests/unit/worker/test_plugins_start.py holds them equal.
CLAUDE_CODE_PROFILES = (
    "claude-code",
    "claude-code-large",
    "claude-code-medium",
    "claude-code-review",
    "claude-code-browser",
    "claude-code-lightpanda",
)
GSTACK = PluginEntry(
    name="gstack",
    version="1.92.55.0",
    install_path="/opt/swarm/plugins/gstack",
    runner_profiles=CLAUDE_CODE_PROFILES,
    skills={
        # No `cso`: left out of the first catalogue (owner, 3 November 2026).
        "unattended": (
            "review", "investigate", "health", "qa-only", "qa", "design-review",
            "document-generate", "document-release", "ship", "plan-eng-review",
            "plan-design-review", "autoplan",
        ),
        "planner": (
            "office-hours", "plan-ceo-review", "plan-eng-review",
            "plan-devex-review", "autoplan", "spec",
        ),
        "designer": (
            "design-consultation", "plan-design-review", "design-review", "design-html",
        ),
    },
)
SECURITY_AUDIT = PluginEntry(
    name="security-audit",
    version="c1c8a8c1471069fb0e188eeaff69b8e8db6564a8",
    install_path="/opt/swarm/plugins/security-audit",
    runner_profiles=CLAUDE_CODE_PROFILES,
    skills={"unattended": ("security-audit",)},
    skill_folders="skills",
)
CATALOGUE: Mapping[str, PluginEntry] = {GSTACK.name: GSTACK, SECURITY_AUDIT.name: SECURITY_AUDIT}

#: The modes that run today; `swarm_api.plugincatalogue.SUPPORTED_MODES`.
SUPPORTED_MODES = ("unattended",)
BRIDGE_REASON = (
    "runs an interview with a person, which needs the question bridge; until it "
    "lands only unattended mode runs"
)

#: The profiles whose image carries a Chromium gstack's browser drives.
BROWSER_PROFILES = ("claude-code-browser",)

#: Where gstack keeps its state: under `work/` (HOME), so it is checkpointed.
HOME_DIR_NAME = ".swarm-gstack"
#: gstack's skills folder under HOME, the literal path its skills call.
SKILLS_DIR = Path(".claude") / "skills"
#: What gstack writes in the project, kept out of the diff.
PROJECT_STATE = ".gstack/"

#: gstack's own config, in GSTACK_HOME (packaging doc, finding 4).
CONFIG = (
    "# Written by the SwarmCloud worker at the start of the step.\n"
    "update_check: false\n"
    "telemetry: off\n"
    "codex_reviews: disabled\n"
    "design_detector: off\n"
)

#: Per plugin, a line the prompt's plugin section adds for it.
NOTES: Mapping[str, str] = {
    # Full audit mode writes its reports to an output folder outside the
    # target checkout; the artifacts folder is outside it and is uploaded.
    "security-audit": (
        "A full security-audit run writes its output folder (coverage-ledger.json, "
        "findings.json, REPORT.md, FINDINGS-DETAIL.md, NEEDS-VALIDATION.md) to "
        "$SWARM_ARTIFACTS_DIR/security-audit, never inside the checkout."),
}

#: The attempt summary's key.
SUMMARY_KEY = "plugins"
#: Claude Code's skill tool, whose calls `record` counts.
SKILL_TOOL = "Skill"


class PluginUnavailable(ConfigError):
    """This step's plugins cannot run here; the agent never starts.

    A ConfigError, so the step ends CANNOT_START with this message
    (`lifecycle._end_cause_of`): another attempt on the same image would meet
    the same install. The message names the plugin, and for a missing install
    the image, never anything from the step's prompt."""


@dataclass(frozen=True)
class Plugin:
    """One plugin this step uses, checked and installed."""

    name: str
    version: str
    mode: str
    skills: tuple[str, ...]


@dataclass(frozen=True)
class Started:
    """What `start` set up: the plugins, the runner's variables, and the
    section closing the agent's prompt."""

    plugins: tuple[Plugin, ...]
    env: Mapping[str, str] = field(hash=False)
    guidance: str
    #: The managed plugins of the signed set (`pluginload.load`), each
    #: verified by digest and loaded whole through `--plugin-dir`.
    managed: tuple[pluginload.Loaded, ...] = ()


# --- which plugins ------------------------------------------------------------


def selected(task: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The step's plugins from its signed work contract, or {}.

    Read only from a stored contract whose digest is the one in the signed
    dispatch block. Raises PluginUnavailable for a stored contract that names
    plugins and does not match its signed reference."""
    metadata = task.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    dispatch = metadata.get("dispatch")
    reference = dispatch.get("work_contract") if isinstance(dispatch, Mapping) else None
    document = metadata.get("work_contract")
    if not isinstance(reference, Mapping) or not isinstance(document, Mapping):
        return {}
    capabilities = document.get("capabilities")
    plugins = capabilities.get("plugins") if isinstance(capabilities, Mapping) else None
    if not isinstance(plugins, Mapping) or not plugins:
        return {}
    try:
        digest = contract_digest(document)
    except SpecNotCanonical:
        digest = None
    if digest is None or digest != reference.get("digest"):
        raise PluginUnavailable(
            "the step's work contract names plugins but does not match the digest "
            "signed with the step, so none are loaded and the agent is not started")
    out: dict[str, dict[str, Any]] = {}
    for name, chosen in plugins.items():
        if not isinstance(chosen, Mapping):
            raise PluginUnavailable(f"the step's plugin {name!r} is not a map")
        skills = chosen.get("skills")
        out[str(name)] = {
            "mode": chosen.get("mode"),
            "skills": tuple(skills) if isinstance(skills, (list, tuple)) else (),
        }
    return out


def check(selection: Mapping[str, Mapping[str, Any]], *, profile: str) -> tuple[Plugin, ...]:
    """The selection against this worker's catalogue, or PluginUnavailable."""
    out = []
    for name, chosen in sorted(selection.items()):
        entry = CATALOGUE.get(name)
        if entry is None:
            raise PluginUnavailable(f"{name!r} is not in this worker's plugin catalogue")
        if profile not in entry.runner_profiles:
            raise PluginUnavailable(f"{name} does not run on the {profile} profile")
        mode = chosen.get("mode")
        if mode not in SUPPORTED_MODES:
            raise PluginUnavailable(f"{name} in {mode} mode {BRIDGE_REASON}")
        skills = tuple(chosen.get("skills") or ())
        offered = entry.skills.get(mode, ())
        extra = sorted(set(skills) - set(offered))
        if extra or not skills or not all(isinstance(s, str) for s in skills):
            raise PluginUnavailable(
                f"{name} in {mode} mode offers {', '.join(offered)}; the step named "
                f"{', '.join(map(str, extra or skills)) or 'none'}")
        out.append(Plugin(name=name, version=entry.version, mode=mode,
                          skills=tuple(sorted(set(skills)))))
    return tuple(out)


# --- the start of a step ------------------------------------------------------


def _skill_path(plugin: Plugin, install: Path, skill: str) -> Path:
    """The folder holding `skill`'s SKILL.md in the install."""
    folders = CATALOGUE[plugin.name].skill_folders
    return (install / folders / skill) if folders is not None else (install / skill)


def _install_of(plugin: Plugin, install_paths: Mapping[str, Path] | None) -> Path:
    if install_paths is not None and plugin.name in install_paths:
        return Path(install_paths[plugin.name])
    return Path(CATALOGUE[plugin.name].install_path)


def _require_install(plugin: Plugin, install: Path, *, image: str) -> None:
    """The install and every skill the step names, or PluginUnavailable
    naming the image. Checked whole before anything is linked."""
    if not install.is_dir():
        raise PluginUnavailable(
            f"the image {image} has no {plugin.name} {plugin.version} install at {install}: "
            f"this step names {plugin.name}, so it needs an image built with it")
    missing = [s for s in plugin.skills
               if not (_skill_path(plugin, install, s) / "SKILL.md").is_file()]
    if missing:
        raise PluginUnavailable(
            f"the image {image}'s {plugin.name} install at {install} lacks the skills "
            f"{', '.join(missing)}, so none are linked and the agent is not started")


def find_chromium(browsers_path: str) -> str | None:
    """The newest Playwright headed Chromium under `browsers_path`, as
    `livegate.find_chromium` finds it, or None."""
    if not browsers_path:
        return None
    found = sorted(glob.glob(os.path.join(browsers_path, "chromium-*", "chrome-linux*", "chrome")))
    return found[-1] if found else None


def _offered_anywhere() -> set[str]:
    names = set(CATALOGUE)
    for entry in CATALOGUE.values():
        for skills in entry.skills.values():
            names.update(skills)
    return names


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def clear_links(work: Path) -> Path:
    """Remove every catalogue name a previous attempt left (a checkpoint
    restores them) from `work/.claude/skills`, once, before any plugin is
    linked. Returns the skills folder."""
    skills_dir = Path(work) / SKILLS_DIR
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name in _offered_anywhere():
        _remove(skills_dir / name)
    return skills_dir


def link_skills(work: Path, plugin: Plugin, install: Path) -> Path:
    """Link ONLY the step's skills of one plugin into `work/.claude/skills`,
    which `clear_links` emptied of catalogue names. Returns the folder.

    gstack's shape: `<plugin>` to the install (its skills call
    `~/.claude/skills/gstack/...`) and one `<skill>/SKILL.md` link per step
    skill. A plugin with `skill_folders` (security-audit): `<skill>` to the
    install's whole, read-only skill folder, because its SKILL.md reads the
    method files and validators beside it by relative path; no plugin-root
    link, so nothing but the step's skills is reachable by name."""
    skills_dir = Path(work) / SKILLS_DIR
    skills_dir.mkdir(parents=True, exist_ok=True)
    if CATALOGUE[plugin.name].skill_folders is not None:
        for skill in plugin.skills:
            (skills_dir / skill).symlink_to(_skill_path(plugin, install, skill),
                                            target_is_directory=True)
        return skills_dir
    (skills_dir / plugin.name).symlink_to(install, target_is_directory=True)
    for skill in plugin.skills:
        folder = skills_dir / skill
        folder.mkdir()
        (folder / "SKILL.md").symlink_to(install / skill / "SKILL.md")
    return skills_dir


def exclude_project_state(checkout: Path | None) -> bool:
    """`.gstack/` in the checkout's `.git/info/exclude`, once. False when
    there is no checkout or its `.git` is not a folder."""
    if checkout is None:
        return False
    git_dir = Path(checkout) / ".git"
    if not git_dir.is_dir():
        return False
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if PROJECT_STATE in (line.strip() for line in text.splitlines()):
        return True
    if text and not text.endswith("\n"):
        text += "\n"
    exclude.write_text(text + PROJECT_STATE + "\n", encoding="utf-8")
    return True


def guidance(plugins: Iterable[Plugin]) -> str:
    """The prompt's closing section for a step with plugins."""
    lines = []
    for p in plugins:
        lines.append(
            f"- {p.name} {p.version}, {p.mode} mode, with only these skills: "
            f"{', '.join(p.skills)}.")
        if p.name in NOTES:
            lines.append(f"  {NOTES[p.name]}")
    return (
        "\n\n## Plugins for this step\n\n"
        + "\n".join(lines)
        + "\n\nThey run unattended: a question with a recommended option is answered with "
        "it and logged. A question a plugin leaves open (a consent, a question with no "
        "recommendation, a final gate) is the owner's: never answer it yourself. Write each "
        "one to questions.json in $SWARM_ARTIFACTS_DIR as an owner question, with its "
        "options and the recommended one if there is one, say in your answer that it is "
        "open, and finish without doing what it would decide.\n"
    )


def start(task: Mapping[str, Any], *, work: Path, checkout: Path | None, profile: str,
          image: str, environ: Mapping[str, str] | None = None,
          install_paths: Mapping[str, Path] | None = None,
          private_dir: Path | None = None, store: Any = None,
          released: pluginload.Released | None = None,
          provider_key: pluginload.ProviderKey | None = None) -> Started | None:
    """Set up the step's plugins before the agent, or None for a step with
    none. Raises PluginUnavailable, before anything is linked, for a plugin
    this worker cannot run or an image without its install, and
    `pluginload.PluginRefused` for a managed plugin set it cannot verify or a
    pinned copy that is not the signed one.

    The managed plugins (docs/design/managed-plugins.md section 4) come from
    the signed `metadata.dispatch.plugins` alone, are installed under
    `private/` (`private_dir`, by default the workspace's, beside `work/`)
    and handed to the runner as paths (`pluginload.LOAD_ENV`)."""
    selection = selected(task)
    managed_entries = pluginload.entries(task)
    if not selection and not managed_entries:
        return None
    plugins = check(selection, profile=profile) if selection else ()
    environ = os.environ if environ is None else environ
    installs = {p.name: _install_of(p, install_paths) for p in plugins}
    for p in plugins:
        _require_install(p, installs[p.name], image=image)
    chromium = None
    if profile in BROWSER_PROFILES and any(p.name == GSTACK.name for p in plugins):
        # The browser image's own PLAYWRIGHT_BROWSERS_PATH (its Dockerfile
        # states the path once), as `livegate.find_chromium` reads it.
        browsers = environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        chromium = find_chromium(browsers)
        if chromium is None:
            raise PluginUnavailable(
                f"the image {image} has no Chromium under "
                f"{browsers or 'PLAYWRIGHT_BROWSERS_PATH (not set)'} for "
                f"{', '.join(p.name for p in plugins)}'s browser on the {profile} profile")

    clear_links(Path(work))
    for p in plugins:
        link_skills(Path(work), p, installs[p.name])

    # gstack's state, config, project folder and variables, only for a step
    # that runs gstack: security-audit keeps no state and reads none of them.
    gstack = next((p for p in plugins if p.name == GSTACK.name), None)
    env: dict[str, str] = {}
    if gstack is not None:
        home = Path(work) / HOME_DIR_NAME
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
        exclude_project_state(checkout)
        env = {
            "GSTACK_SESSION_KIND": gstack.mode,
            "GSTACK_HOME": str(home),
            "GSTACK_EPHEMERAL": "1",
            "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "0",
        }
        if chromium is not None:
            env["GSTACK_CHROMIUM_PATH"] = chromium
            env["GSTACK_CHROMIUM_NO_SANDBOX"] = "1"
    managed: tuple[pluginload.Loaded, ...] = ()
    if managed_entries:
        loaded = pluginload.load(
            task, private_dir=Path(private_dir) if private_dir is not None else Path(work).parent / "private",
            store=store, environ=environ, released=released, provider_key=provider_key)
        if loaded is not None:
            managed = loaded.plugins
            env.update(loaded.env)
    return Started(plugins=plugins, env=env, guidance=guidance(plugins) if plugins else "", managed=managed)


# --- after the agent ----------------------------------------------------------


def _skill_calls(stream: Path) -> list[str]:
    """The `skill` argument of every `Skill` tool call in a stream-json capture."""
    out: list[str] = []
    try:
        handle = stream.open(encoding="utf-8", errors="replace")
    except OSError:
        return out
    with handle:
        for line in handle:
            if '"tool_use"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") if isinstance(event, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else ():
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == SKILL_TOOL
                        and isinstance(block.get("input"), dict)
                        and isinstance(block["input"].get("skill"), str)):
                    out.append(block["input"]["skill"])
    return out


def record(started: Started | None, stream: Path | None) -> dict[str, Any] | None:
    """The attempt summary's `plugins`: per plugin its version, mode, skills
    and how many times each ran, and per managed plugin, under its install
    id, `pluginload.record` (version, digest, hook runs counted by the
    launcher, MCP servers started, skills called). None for a step with
    none. Never raises."""
    if started is None:
        return None
    calls = _skill_calls(Path(stream)) if stream is not None else []
    out: dict[str, Any] = {}
    for loaded in started.managed:
        try:
            out[loaded.entry.install_id] = pluginload.record(loaded, calls)
        except Exception as exc:  # noqa: BLE001 -- a record is never worth a failed step
            out[loaded.entry.install_id] = {"managed": True, "record_error": type(exc).__name__}
    for p in started.plugins:
        runs = {s: 0 for s in p.skills}
        for call in calls:
            name = call.split(":", 1)[1] if call.startswith(f"{p.name}:") else call
            if name in runs:
                runs[name] += 1
        out[p.name] = {"version": p.version, "mode": p.mode, "skills": list(p.skills),
                       "skill_runs": runs}
    return out


__all__ = [
    "BROWSER_PROFILES",
    "CATALOGUE",
    "CONFIG",
    "GSTACK",
    "HOME_DIR_NAME",
    "Plugin",
    "NOTES",
    "PluginUnavailable",
    "SECURITY_AUDIT",
    "SUMMARY_KEY",
    "SUPPORTED_MODES",
    "Started",
    "check",
    "clear_links",
    "exclude_project_state",
    "find_chromium",
    "guidance",
    "link_skills",
    "record",
    "selected",
    "start",
]
