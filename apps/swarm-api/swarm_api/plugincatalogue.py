"""The platform's closed agent plugin catalogue: a runtime capability manifest entry per plugin.

docs/design/agent-plugins.md, "A closed plugin catalogue"; the owner merged
plugins into the agent quality program on 3 November 2026, so the catalogue
is a runtime capability manifest entry and a step's plugins ride in its work
contract (`capabilities.plugins`). Per plugin: its pinned version, the
read-only path the agent images install it at, the runner profiles that can
load it, and per mode the skills it may offer. A caller names plugins, modes
and skills from here by name only, never a path, command, URL or version
(invariant 10). `workcontract.PLUGIN_CATALOGUE` decodes contracts against it;
`plugins.check_step` checks a step against it at submission;
`agent_worker.plugins` restates it (the worker imports nothing from swarm-api)
and tests/unit/worker/test_plugins_start.py holds the two equal.

Pure: no Firestore, no network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class PluginEntry:
    """One catalogue plugin: a runtime capability manifest entry."""

    name: str
    version: str
    install_path: str
    runner_profiles: tuple[str, ...]
    #: Per mode, the skills a step may name.
    skills: Mapping[str, tuple[str, ...]] = field(hash=False)
    #: Where the plugin's skill folders sit under `install_path`, for a
    #: plugin whose SKILL.md reads its sibling files by relative path: the
    #: worker links each step skill's folder whole. None is gstack's shape
    #: (one `<skill>/SKILL.md` per skill, the plugin root linked by name).
    skill_folders: str | None = None


#: The modes a step may name, in `swarm_common.workcontract`'s vocabulary.
MODES = ("unattended", "planner", "designer")
#: The modes a step may run today. Planner and designer interview a person,
#: which needs the question bridge (docs/design/agent-plugins.md, "The question
#: bridge"); until it lands they are refused with `BRIDGE_REASON`.
SUPPORTED_MODES = ("unattended",)
BRIDGE_REASON = (
    "runs an interview with a person, which needs the question bridge; until it "
    "lands only unattended mode runs"
)

#: The six claude-code profiles (`swarm_common.profiles`), the only runners
#: that load the catalogue's skills. codex comes later.
CLAUDE_CODE_PROFILES = (
    "claude-code",
    "claude-code-large",
    "claude-code-medium",
    "claude-code-review",
    "claude-code-browser",
    "claude-code-lightpanda",
)

#: gstack at the release the packaging spike measured (v1.92.55.0, the build
#: mirror's pinned archive), at the path its build stage renders the skills for.
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

#: Cloudflare's security audit skill (github.com/cloudflare/security-audit-skill,
#: MIT), the second plugin (owner, 4 November 2026), at the commit the build
#: mirror pins (14 September 2026). One skill folder, skills/security-audit/,
#: whose SKILL.md reads its method files, report schema and two Node
#: validators beside it. Guidance mode answers a focused question; full audit
#: mode writes its reports to an output folder outside the checkout. No new
#: runner profile and no new image: the base image carries it, so every
#: claude-code profile's image does (docs/design/agent-plugins-packaging.md,
#: "The second plugin").
SECURITY_AUDIT = PluginEntry(
    name="security-audit",
    version="c1c8a8c1471069fb0e188eeaff69b8e8db6564a8",
    install_path="/opt/swarm/plugins/security-audit",
    runner_profiles=CLAUDE_CODE_PROFILES,
    skills={"unattended": ("security-audit",)},
    skill_folders="skills",
)

CATALOGUE: Mapping[str, PluginEntry] = {GSTACK.name: GSTACK, SECURITY_AUDIT.name: SECURITY_AUDIT}


def offered_skills() -> dict[str, dict[str, tuple[str, ...]]]:
    """Per plugin, per mode, the skills it may offer: the shape
    `workcontract.PLUGIN_CATALOGUE` decodes a contract against."""
    return {name: dict(entry.skills) for name, entry in CATALOGUE.items()}
