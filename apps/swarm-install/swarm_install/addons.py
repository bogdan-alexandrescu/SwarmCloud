"""The five add-ons, each turned on or off by `swarm addon enable|disable <name>`
(docs/design/day-one-install.md §12; docs/design/day-one-plan.md lane
DAYONE-ADDON-1, part of #2688).

Core reaches READY with every add-on off (D9, D10): no install step satisfies
an `addon:*` manifest entry and the install's generated Terraform inputs set
every add-on switch off. An add-on is opt-in after READY, and enabling one
runs, in order:

| Phase | What it does |
|---|---|
| preflight | core is READY; the add-on's choices are valid; its images have digests; its own checks (below) |
| prepare | the Terraform passes that create the add-on's secret containers (key before switch, lesson 39) |
| secrets | a generated key is generated here and added as a version; a person's credential goes in through the console's one-time `addon-secret` link (or `--in-terminal`, the hidden prompt on /dev/tty), and the engine polls for its version |
| switch | the Terraform passes that turn the add-on on |
| workload | the add-on's swarm-system manifest, Terraform's own render, applied on the install cluster |
| check | the add-on's evidence that it is on |

| Add-on | Secret | Switch |
|---|---|---|
| `bright-data-egress` | `swarm-egress-key` (generated), `swarm-egress-credential` (link) | `enable_browser_egress`; the `swarm-egress` workload |
| `tailscale` | `swarm-tailscale-oauth` (link) | `enable_tailscale_egress`, `tailscale_egress_target` (asked; no default); the `tailscale-egress` workload |
| `typesafe` | `swarm-typesafe-fwd-key` (generated) | `enable_typesafe_forwarder_read` (bootstrap), `enable_typesafe_forwarder`, `typesafe_forwarder.serve`; the `swarm-typesafe-fwd` workload |
| `brave-search` | `swarm-search-api-key` (link; the one container the engine creates, as create-secrets.sh did) | `search_tool_enabled` |
| `ci` | none: Workload Identity and the merge App | `enable_github_wif`, `github_repository` (bootstrap), `enable_acceptance_fixture`; Cloud Build with its OWN service account (lesson 6); the runner label and the sandbox as repository variables |

**A secret's value never passes through the terminal.** The link is minted by
swarm-api (`POST /v1/admin/setup/links`, kind `addon-secret`) and redeemed in
the console, where swarm-api writes the version; the engine reads only
whether a version is ENABLED (metadata). A generated key is made in memory
(`secrets.token_urlsafe`, as create-secrets.sh does), handed to `addVersion`
and never printed. No flag of the verb takes a value.

**What an add-on turned on stays on.** Each add-on's recorded phase is kept in
the install state (`outputs["addons"]`), and `inputs_for(outputs, root)` is
the overlay every apply of a root carries, so enabling one add-on never turns
another off. The install's own passes merge it once `tfvars` calls
`inputs_for` (asked in the lane's PR: tfvars.py is not this lane's file).

**Disabling keeps the secret containers.** The add-on's switches go off and
its Terraform resources are destroyed, after the typed approval every destroy
needs; a plan that would delete a Secret Manager container (or the deployer)
is refused before anything is applied. Deleting the containers is a second,
typed step: `swarm addon disable <name> --delete-secrets`.
"""

from __future__ import annotations

import os
import re
import secrets as _secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO

from .events import Events
from .exitcodes import MET, UNMET, WAITING
from .waits import Clock, WaitSpec, wait_until

#: The add-ons, in design §12's order; swarm-api's `routes/setup.py` ADDONS
#: lists the same five for Admin › Setup.
NAMES: tuple[str, ...] = ("bright-data-egress", "tailscale", "typesafe", "brave-search", "ci")

#: The state key every add-on's phase is recorded under.
STATE_KEY = "addons"
#: The phases an add-on's record moves through.
OFF, PREPARED, ON = "off", "prepared", "on"

#: How long the engine polls for a version after printing a link. The link
#: itself lives 24 hours (swarm_api.setup_links.LINK_TTL); past this bound the
#: run pauses (exit 4) and the next `swarm addon enable` resumes the poll.
LINK_WAIT = WaitSpec(
    reason="the add-on's secret, entered through its one-time console link",
    expected="a few minutes once the person opens the link",
    expected_s=5 * 60,
    bound_s=30 * 60,
    look_at="the link's owner opens it in a browser signed in as themselves; run swarm addon enable again "
            "to mint a fresh link",
    interval_s=10,
)
WORKLOAD_WAIT = WaitSpec(
    reason="the add-on's workload becoming ready in swarm-system",
    expected="1 to 5 min (an Autopilot node may be added)",
    expected_s=5 * 60,
    bound_s=15 * 60,
    look_at="kubectl -n swarm-system describe on the workload: an image pull or a secret mount",
    interval_s=15,
)

#: A Tailscale MagicDNS name, as terraform/infra/tailscale.tf validates it.
MAGIC_DNS = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9-]+\.ts\.net$"
GITHUB_REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
RUNNER_LABEL = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"

#: Lesson 6: a build never runs as Google's default service accounts, which
#: hold Editor (or did) on every project they live in.
DEFAULT_BUILD_ACCOUNTS = ("-compute@developer.gserviceaccount.com", "@cloudbuild.gserviceaccount.com")
#: The CI add-on's own Cloud Build service account (no key, no project role
#: but what its builds need, granted by the release pipeline's own runbook).
CI_BUILD_ACCOUNT = "swarm-ci-build"

#: Resources an add-on's apply never destroys: Secret Manager containers (a
#: second, typed step deletes them) and the deployer every pass runs as.
PROTECTED_TYPES = ("google_secret_manager_secret",)
PROTECTED_ADDRESSES = ("google_service_account.deployer",)

#: The GitHub environments the CI add-on's release promotion uses (lesson 25:
#: a prod tag moved before approval); `prod` must carry a protection rule.
CI_ENVIRONMENTS = ("dev", "prod")
CI_PROTECTED = ("prod",)
#: The repository variables the CI add-on writes (the runner label the
#: workflow patch reads, and the acceptance sandbox scripts/acceptance/config.sh reads).
CI_VARIABLES = {"runner_label": "SWARM_CI_RUNNER_LABEL", "sandbox": "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL"}


class AddonError(RuntimeError):
    """The add-on could not be turned on or off; the message says what to do."""


class AddonRefused(AddonError):
    """A typed approval was missing, or a deny-listed resource was named (exit 5)."""


# ---------------------------------------------------------------------------
# What each add-on is
# ---------------------------------------------------------------------------

LINK, GENERATED = "link", "generated"


@dataclass(frozen=True)
class SecretSpec:
    name: str
    #: `link` (a person's credential: the console's one-time link, or the
    #: hidden prompt) or `generated` (a platform key the engine makes).
    how: str
    #: The engine creates the container (only where no Terraform does).
    engine_container: bool = False


@dataclass(frozen=True)
class Choice:
    key: str
    question: str
    pattern: str
    default: str | None = None


@dataclass(frozen=True)
class Page:
    """An outside page with no API: what to do there, before the link."""

    url: str
    what: str


Overlay = tuple[tuple[str, Mapping[str, Any]], ...]


@dataclass(frozen=True)
class Addon:
    name: str
    summary: str
    secrets: tuple[SecretSpec, ...] = ()
    choices: tuple[Choice, ...] = ()
    images: tuple[str, ...] = ()
    apis: tuple[str, ...] = ()
    page: Page | None = None
    #: (kind/name, Terraform output holding its manifest)
    workload: tuple[str, str] | None = None
    #: The manifest entries enabling it satisfies (install/manifest.d/addon-1.yaml).
    satisfies: tuple[str, ...] = ()
    #: phase -> (choices -> [(root, overlay)]), in apply order.
    overlays: Mapping[str, Callable[[Mapping[str, str]], Overlay]] = field(default_factory=dict)

    @property
    def scope(self) -> str:
        return f"addon:{self.name}"

    def overlay(self, phase: str, choices: Mapping[str, str] | None = None) -> Overlay:
        make = self.overlays.get(phase)
        return make(dict(choices or {})) if make is not None else ()

    def roots(self) -> tuple[str, ...]:
        found: list[str] = []
        for phase in (PREPARED, ON, OFF):
            for root, _ in self.overlay(phase, _sample_choices(self)):
                if root not in found:
                    found.append(root)
        return tuple(found)


def _sample_choices(addon: Addon) -> dict[str, str]:
    return {c.key: c.default or "" for c in addon.choices}


ADDONS: dict[str, Addon] = {
    "bright-data-egress": Addon(
        name="bright-data-egress",
        summary="residential egress for live-browser agents through Bright Data (swarm-egress)",
        secrets=(SecretSpec("swarm-egress-key", GENERATED), SecretSpec("swarm-egress-credential", LINK)),
        images=("swarm-egress",),
        page=Page("https://brightdata.com/cp/zones",
                  "create the residential zone and copy its credential JSON; you paste it into the console "
                  "link below, never into this terminal"),
        workload=("deployment/swarm-egress", "swarm_egress_manifest"),
        satisfies=("secret.swarm-egress-key.value", "secret.swarm-egress-credential.value",
                   "workload.swarm-egress", "addon.bright-data-egress"),
        # Both containers are unconditional in terraform/infra/egress.tf, so
        # there is nothing to prepare; `browser_egress.enabled` (the per-tenant
        # kill switch) stays off until an admin routes a tenant.
        overlays={
            ON: lambda c: (("infra", {"enable_browser_egress": True}),),
            OFF: lambda c: (("infra", {"enable_browser_egress": False}),),
        },
    ),
    "tailscale": Addon(
        name="tailscale",
        summary="the decision-api tailnet egress (tailscale-egress), for internal judgment backends",
        secrets=(SecretSpec("swarm-tailscale-oauth", LINK),),
        choices=(Choice("tailscale_egress_target",
                        "The tailnet host the forwarder dials, <host>.<tailnet>.ts.net", MAGIC_DNS),),
        page=Page("https://login.tailscale.com/admin/settings/oauth",
                  "generate an OAuth client with the tag:swarmcloud tag (Devices: write) and copy its "
                  "client secret (it starts tskey-client-); you paste it into the console link below"),
        workload=("deployment/tailscale-egress", "tailscale_egress_manifest"),
        satisfies=("addon.tailscale-egress-target", "secret.swarm-tailscale-oauth.value",
                   "workload.tailscale-egress"),
        overlays={
            ON: lambda c: (("infra", {"enable_tailscale_egress": True,
                                      "tailscale_egress_target": c["tailscale_egress_target"]}),),
            OFF: lambda c: (("infra", {"enable_tailscale_egress": False}),),
        },
    ),
    "typesafe": Addon(
        name="typesafe",
        summary="the TypeSafe/Jev forwarder (swarm-typesafe-fwd): agents' judgment requests with a person's key",
        secrets=(SecretSpec("swarm-typesafe-fwd-key", GENERATED),),
        images=("swarm-typesafe-fwd",),
        workload=("deployment/swarm-typesafe-fwd", "swarm_typesafe_fwd_manifest"),
        satisfies=("secret.swarm-typesafe-fwd-key.value", "switch.enable_typesafe_forwarder_read",
                   "workload.swarm-typesafe-fwd", "addon.typesafe"),
        # enable_typesafe_forwarder creates the key's container (count), so it
        # goes on before the key and STAYS on when the add-on is disabled:
        # disabling turns serving and forwarding off and keeps the container.
        overlays={
            PREPARED: lambda c: (("bootstrap", {"enable_typesafe_forwarder_read": True}),
                                 ("infra", {"enable_typesafe_forwarder": True})),
            ON: lambda c: (("bootstrap", {"enable_typesafe_forwarder_read": True}),
                           ("infra", {"enable_typesafe_forwarder": True,
                                      "typesafe_forwarder": {"serve": True}})),
            OFF: lambda c: (("infra", {"enable_typesafe_forwarder": True,
                                       "typesafe_forwarder": {"serve": False, "enabled": False}}),
                            ("bootstrap", {"enable_typesafe_forwarder_read": False})),
        },
    ),
    "brave-search": Addon(
        name="brave-search",
        summary="web search for agents (swarm-api's search tool, Brave)",
        secrets=(SecretSpec("swarm-search-api-key", LINK, engine_container=True),),
        page=Page("https://api-dashboard.search.brave.com/app/keys",
                  "create an API key for the Data for Search plan; you paste it into the console link below"),
        satisfies=("secret.swarm-search-api-key.container", "secret.swarm-search-api-key.value",
                   "addon.brave-search"),
        overlays={
            ON: lambda c: (("infra", {"search_tool_enabled": True}),),
            OFF: lambda c: (("infra", {"search_tool_enabled": False}),),
        },
    ),
    "ci": Addon(
        name="ci",
        summary="SwarmCloud's own CI machinery: GitHub Actions through Workload Identity, Cloud Build with its "
                "own account, the acceptance fixture, self-hosted runners and the acceptance sandbox",
        choices=(
            Choice("github_repository", "The repository whose GitHub Actions deploy (owner/repo)", GITHUB_REPO),
            Choice("sandbox_repository", "The PRIVATE acceptance sandbox repository (owner/repo)", GITHUB_REPO),
            Choice("runner_label", "The self-hosted runners' label", RUNNER_LABEL, "swarm-ci"),
        ),
        apis=("cloudbuild.googleapis.com", "sts.googleapis.com"),
        satisfies=("api.cloudbuild", "api.sts", "switch.enable_github_wif", "other.wif-github-pool-and-provider",
                   "declared-value.github_repository", "switch.enable_acceptance_fixture",
                   "bucket.acceptance-fixture", "iam-binding.acceptance-fixture-allusers",
                   "sa.ci-build", "other.ci-runner-label", "other.ci-environments-protected", "addon.ci"),
        overlays={
            ON: lambda c: (("bootstrap", {"enable_github_wif": True,
                                          "github_repository": c["github_repository"]}),
                           ("infra", {"enable_acceptance_fixture": True})),
            OFF: lambda c: (("infra", {"enable_acceptance_fixture": False}),
                            ("bootstrap", {"enable_github_wif": False})),
        },
    ),
}


def get(name: str) -> Addon:
    if name not in ADDONS:
        raise AddonError(f"no add-on {name!r}; one of {', '.join(NAMES)}")
    return ADDONS[name]


def recorded(outputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each add-on's record in the install state (`outputs["addons"]`)."""
    found = outputs.get(STATE_KEY) or {}
    return {str(k): dict(v) for k, v in found.items() if isinstance(v, Mapping)} if isinstance(found, Mapping) else {}


def _merge(into: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(into.get(key), Mapping):
            merged = dict(into[key])
            _merge(merged, value)
            into[key] = merged
        else:
            into[key] = dict(value) if isinstance(value, Mapping) else value


def inputs_for(outputs: Mapping[str, Any], root: str) -> dict[str, Any]:
    """The Terraform inputs every add-on's recorded phase adds to `root`.

    Empty for an install that never enabled one: core's inputs are its own.
    """
    inputs: dict[str, Any] = {}
    records = recorded(outputs)
    for name in NAMES:
        record = records.get(name)
        if not record:
            continue
        phase = str(record.get("phase") or OFF)
        for overlay_root, overlay in ADDONS[name].overlay(phase, record.get("choices") or {}):
            if overlay_root == root:
                _merge(inputs, overlay)
    return inputs


def build_account(project: str) -> str:
    return f"{CI_BUILD_ACCOUNT}@{project}.iam.gserviceaccount.com"


def build_account_problem(email: str) -> str | None:
    """Lesson 6: the CI add-on's builds run as their own account, never a default one."""
    if not email or any(email.endswith(suffix) for suffix in DEFAULT_BUILD_ACCOUNTS):
        return (f"{email or 'no account'} is a default service account; the CI add-on's builds run as their "
                f"own account ({CI_BUILD_ACCOUNT}), never the default compute or legacy Cloud Build account "
                "(lesson 6)")
    return None


def protected_changes(plan: Mapping[str, Any]) -> list[str]:
    """Addresses a Terraform plan would delete that an add-on never deletes."""
    found = []
    for change in plan.get("resource_changes") or ():
        actions = (change.get("change") or {}).get("actions") or ()
        if "delete" not in actions:
            continue
        address = str(change.get("address") or "")
        bare = re.sub(r"\[[^\]]*\]", "", address.split("module.", 1)[-1] if address.startswith("module.") else address)
        if change.get("type") in PROTECTED_TYPES or any(bare.endswith(p) for p in PROTECTED_ADDRESSES):
            found.append(address)
    return found


# ---------------------------------------------------------------------------
# The world an add-on touches, each piece a fake in tests
# ---------------------------------------------------------------------------


class World(Protocol):
    project: str

    def secret_exists(self, name: str) -> bool: ...

    def has_version(self, name: str) -> bool: ...

    def create_secret(self, name: str, labels: Mapping[str, str]) -> None: ...

    def add_version(self, name: str, data: bytes) -> int: ...

    def delete_secret(self, name: str) -> None: ...

    def enabled_services(self) -> set[str]: ...

    def enable_services(self, names: Sequence[str]) -> None: ...

    def service_account(self, email: str) -> Mapping[str, Any] | None: ...

    def create_service_account(self, account_id: str, display_name: str) -> None: ...

    def mint_link(self, addon: str, owner: str) -> Mapping[str, Any]: ...

    def apply(self, root: str, overlay: Mapping[str, Any], *,
              guard: Callable[[Mapping[str, Any]], None]) -> Mapping[str, Any]: ...

    def kube(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]: ...

    def sync_tailscale_secret(self) -> None: ...

    def github(self, *args: str) -> tuple[int, str, str]: ...


@dataclass
class Flow:
    """One `swarm addon enable|disable`, against a world and a state."""

    world: World
    state: Any
    save: Callable[[Callable[[Any], None]], None]
    events: Events
    clock: Clock
    out: TextIO
    ask: Callable[[str, str | None], str]
    confirm: Callable[[str], str]
    #: The hidden prompt on /dev/tty, for `--in-terminal` (secret_entry.enter_secret).
    hidden_entry: Callable[[str, str], int] | None = None

    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

    # -- the record ---------------------------------------------------------

    def _record(self, name: str, **fields: Any) -> None:
        def change(state: Any) -> None:
            addons = dict(state.outputs.get(STATE_KEY) or {})
            entry = dict(addons.get(name) or {})
            entry.update(fields)
            addons[name] = entry
            state.outputs[STATE_KEY] = addons

        self.save(change)

    def _outputs(self) -> Mapping[str, Any]:
        return self.state.outputs

    # -- preflight ------------------------------------------------------------

    def _refuse_denied(self, addon: Addon) -> None:
        denied = set(self.state.deny_list or ())
        named = [s.name for s in addon.secrets if s.name in denied]
        if self.world.project in denied:
            named.append(self.world.project)
        if named:
            raise AddonRefused(f"{', '.join(named)} is on the shared deny-list; the add-on touches none of it")

    def choices(self, addon: Addon, given: Mapping[str, str]) -> dict[str, str]:
        known = dict((recorded(self._outputs()).get(addon.name) or {}).get("choices") or {})
        chosen: dict[str, str] = {}
        for choice in addon.choices:
            value = given.get(choice.key) or known.get(choice.key)
            if not value:
                value = self.ask(choice.question, choice.default)
            value = str(value or "").strip()
            if not re.match(choice.pattern, value):
                raise AddonError(f"{choice.key}: {value!r} does not match {choice.pattern}")
            chosen[choice.key] = value
        if addon.name == "ci" and chosen["sandbox_repository"].lower() == chosen["github_repository"].lower():
            raise AddonError("the acceptance sandbox must be a repository of its own, never the one CI deploys "
                             "from (#628): every fixture pull request would run full CI there")
        return chosen

    def preflight(self, addon: Addon, chosen: Mapping[str, str]) -> None:
        self.events.emit("step_check", step=f"addon.{addon.name}.preflight")
        if not getattr(self.state, "ready_at", None):
            raise AddonError("core is not READY yet: an add-on is turned on after swarm install reaches READY. "
                             "Run swarm install")
        self._refuse_denied(addon)
        digests = self._outputs().get("image_digests") or {}
        missing = [image for image in addon.images if image not in digests]
        if missing:
            raise AddonError(f"the release carries no digest for {', '.join(missing)}; run swarm upgrade to a "
                             "release that ships the add-on's image")
        if addon.name == "ci":
            self._ci_preflight(chosen)
        self.events.emit("step_done", step=f"addon.{addon.name}.preflight", state=MET)

    def _ci_preflight(self, chosen: Mapping[str, str]) -> None:
        problem = build_account_problem(build_account(self.world.project))
        if problem:
            raise AddonError(problem)
        repo = chosen["github_repository"]
        code, out, err = self.world.github("api", f"repos/{repo}/environments", "--jq",
                                           ".environments[] | [.name, (.protection_rules | length)] | @tsv")
        if code != 0:
            raise AddonError(f"could not read {repo}'s environments through gh ({err.strip()[:200]}); sign in "
                             "with gh auth login as an admin of the repository")
        rules: dict[str, int] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].isdigit():
                rules[parts[0]] = int(parts[1])
        absent = [e for e in CI_ENVIRONMENTS if e not in rules]
        open_ = [e for e in CI_PROTECTED if e in rules and rules[e] < 1]
        if absent or open_:
            page = f"https://github.com/{repo}/settings/environments"
            why = ([f"missing environment(s) {', '.join(absent)}"] if absent else []) + (
                [f"{', '.join(open_)} has no protection rule (lesson 25: a prod tag moved before approval)"]
                if open_ else [])
            raise AddonError(f"{'; '.join(why)}. Create them with a required reviewer on prod at {page}, then "
                             "run swarm addon enable ci again")
        code, out, err = self.world.github("api", f"repos/{chosen['sandbox_repository']}", "--jq", ".private")
        if code != 0 or out.strip() != "true":
            raise AddonError(f"{chosen['sandbox_repository']} is not a private repository gh can read; the "
                             "acceptance sandbox is private (#628)")

    # -- secrets ------------------------------------------------------------

    def _owner(self, owner: str | None) -> str:
        found = owner or str(self._outputs().get("installer") or "")
        if not found:
            raise AddonError("no one to send the secret's link to: pass --owner <email>")
        return found

    def secret(self, addon: Addon, spec: SecretSpec, *, owner: str | None, in_terminal: bool) -> None:
        step = f"addon.{addon.name}.{spec.name}"
        if spec.engine_container and not self.world.secret_exists(spec.name):
            self.world.create_secret(spec.name, {"managed-by": "swarm-install", "addon": addon.name})
            self.events.emit("step_apply", step=step, detail=f"created the container {spec.name}")
        if not self.world.secret_exists(spec.name):
            raise AddonError(f"{spec.name} does not exist after the add-on's Terraform pass; run swarm addon "
                             f"enable {addon.name} again")
        if self.world.has_version(spec.name):
            self.events.emit("step_done", step=step, state=MET, detail=f"{spec.name} has a version")
            return
        if spec.how == GENERATED:
            value = _secrets.token_urlsafe(48).encode()
            number = self.world.add_version(spec.name, value)
            self.events.emit("step_done", step=step, state=MET,
                             detail=f"generated version {number} of {spec.name}; its value was never shown")
            return
        if addon.page is not None:
            self.say(f"{addon.summary}: first, at {addon.page.url}, {addon.page.what}.")
        if in_terminal:
            if self.hidden_entry is None:
                raise AddonError("there is no terminal to prompt on; open the console link instead")
            number = self.hidden_entry(spec.name, f"{spec.name} for the {addon.name} add-on")
            self.events.emit("step_done", step=step, state=MET, detail=f"version {number} of {spec.name}")
            return
        who = self._owner(owner)
        link = self.world.mint_link(addon.name, who)
        where = link.get("url") or link.get("path")
        if not where:
            raise AddonError("swarm-api minted the add-on's link but answered no address for it")
        self.say(f"{spec.name} goes in through a one-time link, for {who} only, until {link.get('expires_at')}:")
        self.say(f"  {where}")
        self.say("Open it in a browser signed in as that person. The value is never typed here.")
        self.events.emit("step_apply", step=step, detail=f"link minted for {who}")

        def check() -> str:
            return MET if self.world.has_version(spec.name) else WAITING

        wait_until(step, LINK_WAIT, check, clock=self.clock, events=self.events)
        self.events.emit("step_done", step=step, state=MET, detail=f"{spec.name} has a version")

    # -- Terraform ----------------------------------------------------------

    def _guard(self, plan: Mapping[str, Any]) -> None:
        kept = protected_changes(plan)
        if kept:
            raise AddonRefused(f"the plan would delete {', '.join(kept)}; an add-on never deletes a secret "
                               "container or the deployer. Nothing was applied")

    def apply_phase(self, addon: Addon, phase: str, chosen: Mapping[str, str]) -> dict[str, Any]:
        self._record(addon.name, phase=phase, choices=dict(chosen))
        outputs: dict[str, Any] = {}
        for root, _ in addon.overlay(phase, chosen):
            self.events.emit("step_apply", step=f"addon.{addon.name}.{phase}", detail=f"terraform {root}")
            outputs.update(self.world.apply(root, inputs_for(self._outputs(), root), guard=self._guard))
        return outputs

    # -- workloads and checks -------------------------------------------------

    def workload(self, addon: Addon, outputs: Mapping[str, Any]) -> None:
        if addon.workload is None:
            return
        name, output = addon.workload
        manifest = outputs.get(output)
        if not manifest:
            raise AddonError(f"terraform output {output} is empty after the switch; the add-on's image or key "
                             "is missing (swarm doctor names which)")
        code, _, err = self.world.kube("apply", "--server-side", "--namespace=swarm-system", "-f", "-",
                                       stdin=str(manifest))
        if code != 0:
            raise AddonError(f"kubectl apply of {output} failed: {err.strip()[:300]}")
        if addon.name == "tailscale":
            self.world.sync_tailscale_secret()

        def check() -> str:
            code, out, _ = self.world.kube("get", name, "--namespace=swarm-system", "-o",
                                           "jsonpath={.status.readyReplicas}/{.spec.replicas}")
            ready, _, wanted = out.strip().partition("/")
            return MET if code == 0 and wanted and ready == wanted and int(wanted) > 0 else WAITING

        wait_until(f"addon.{addon.name}.workload", WORKLOAD_WAIT, check, clock=self.clock, events=self.events)

    def check(self, addon: Addon) -> tuple[str, str]:
        """The add-on's evidence: (met|unmet, why)."""
        record = recorded(self._outputs()).get(addon.name) or {}
        if record.get("phase") != ON:
            return UNMET, f"{addon.name} is off"
        missing = [s.name for s in addon.secrets if not self.world.has_version(s.name)]
        if missing:
            return UNMET, f"{', '.join(missing)} has no version"
        if addon.workload is not None:
            code, out, _ = self.world.kube("get", addon.workload[0], "--namespace=swarm-system", "-o",
                                           "jsonpath={.status.readyReplicas}/{.spec.replicas}")
            ready, _, wanted = out.strip().partition("/")
            if code != 0 or not wanted or ready != wanted:
                return UNMET, f"{addon.workload[0]} is not ready ({out.strip() or 'absent'})"
        if addon.name == "ci":
            if self.world.service_account(build_account(self.world.project)) is None:
                return UNMET, f"{build_account(self.world.project)} does not exist"
            missing_apis = [a for a in addon.apis if a not in self.world.enabled_services()]
            if missing_apis:
                return UNMET, f"{', '.join(missing_apis)} not enabled"
            repo = str((record.get("choices") or {}).get("github_repository") or "")
            for key, variable in CI_VARIABLES.items():
                code, out, _ = self.world.github("variable", "get", variable, "--repo", repo)
                if code != 0 or out.strip() != self._ci_variable(key, record.get("choices") or {}):
                    return UNMET, f"{repo}'s repository variable {variable} is not set"
        return MET, f"{addon.name} is on"

    @staticmethod
    def _ci_variable(key: str, chosen: Mapping[str, str]) -> str:
        if key == "sandbox":
            return f"https://github.com/{chosen['sandbox_repository']}.git"
        return str(chosen["runner_label"])

    def _ci_switch(self, chosen: Mapping[str, str]) -> None:
        missing = [a for a in ADDONS["ci"].apis if a not in self.world.enabled_services()]
        if missing:
            self.world.enable_services(missing)
        email = build_account(self.world.project)
        if self.world.service_account(email) is None:
            self.world.create_service_account(CI_BUILD_ACCOUNT, "SwarmCloud CI builds (the CI add-on, lesson 6)")
        self.events.emit("step_done", step="addon.ci.build-account", detail=email)
        for key, variable in CI_VARIABLES.items():
            code, _, err = self.world.github("variable", "set", variable, "--repo", chosen["github_repository"],
                                             "--body", self._ci_variable(key, chosen))
            if code != 0:
                raise AddonError(f"could not set {variable} on {chosen['github_repository']}: {err.strip()[:200]}")

    # -- the verbs ----------------------------------------------------------

    def enable(self, name: str, *, given: Mapping[str, str] | None = None, owner: str | None = None,
               in_terminal: bool = False) -> str:
        try:
            return self._enable(name, given=given, owner=owner, in_terminal=in_terminal)
        finally:
            self._close_reach()

    def disable(self, name: str, *, delete_secrets: bool = False) -> str:
        try:
            return self._disable(name, delete_secrets=delete_secrets)
        finally:
            self._close_reach()

    def _close_reach(self) -> None:
        """Decision 2: the control plane admits this machine only while `kube` runs (finding 16)."""
        close = getattr(self.world, "close_reach", None)
        left = close() if close is not None else None
        if left:
            self.say(left)

    def _enable(self, name: str, *, given: Mapping[str, str] | None = None, owner: str | None = None,
                in_terminal: bool = False) -> str:
        addon = get(name)
        chosen = self.choices(addon, given or {})
        self.preflight(addon, chosen)
        if addon.overlays.get(PREPARED):
            self.apply_phase(addon, PREPARED, chosen)
        for spec in addon.secrets:
            self.secret(addon, spec, owner=owner, in_terminal=in_terminal)
        if addon.name == "ci":
            self._ci_switch(chosen)
        outputs = self.apply_phase(addon, ON, chosen)
        self.workload(addon, outputs)
        status, why = self.check(addon)
        if status != MET:
            raise AddonError(f"{addon.name} was switched on but its check is {status}: {why}")
        self.events.emit("step_done", step=f"addon.{addon.name}", state=MET, detail=why)
        return why

    def _disable(self, name: str, *, delete_secrets: bool = False) -> str:
        addon = get(name)
        self._refuse_denied(addon)
        record = recorded(self._outputs()).get(addon.name) or {}
        chosen = dict(record.get("choices") or _sample_choices(addon))
        self.apply_phase(addon, OFF, chosen)
        if addon.workload is not None:
            self.world.kube("delete", addon.workload[0], "--namespace=swarm-system", "--ignore-not-found")
        kept = [s.name for s in addon.secrets]
        self.events.emit("step_done", step=f"addon.{addon.name}", detail=f"{addon.name} is off")
        if not delete_secrets:
            if kept:
                self.say(f"{addon.name} is off. Its secret containers stay: {', '.join(kept)}. Deleting them is "
                         f"a second, typed step: swarm addon disable {addon.name} --delete-secrets")
            return f"{addon.name} is off"
        if not kept:
            return f"{addon.name} is off; it has no secret containers"
        answer = self.confirm(f"Delete {', '.join(kept)} and every version of them? Type the project id "
                              f"({self.world.project}) to delete, anything else keeps them: ")
        if answer.strip() != self.world.project:
            raise AddonRefused("the secret containers were not deleted: the project id was not typed")
        for secret_name in kept:
            if self.world.secret_exists(secret_name):
                self.world.delete_secret(secret_name)
        return f"{addon.name} is off and its secret containers are deleted"


# ---------------------------------------------------------------------------
# The real world: Google, swarm-api, Terraform, the cluster and gh
# ---------------------------------------------------------------------------


@dataclass
class RealWorld:
    """Each method is one call; the `Flow` decides which and when."""

    project: str
    ctx: Any
    gcp: Any
    deps: Any
    api_context: str | None = None
    _kube: Any = None
    _reach: Any = None

    # Secret Manager: metadata, a generated key's addVersion, and the typed delete.

    def secret_exists(self, name: str) -> bool:
        return self.gcp.secret(self.project, name) is not None

    def has_version(self, name: str) -> bool:
        return self.secret_exists(name) and self.gcp.enabled_version(self.project, name) is not None

    def create_secret(self, name: str, labels: Mapping[str, str]) -> None:
        self.gcp.create_secret(self.project, name, labels)

    def add_version(self, name: str, data: bytes) -> int:
        return int(self.gcp.add_secret_version(self.project, name, data))

    def delete_secret(self, name: str) -> None:
        from .steps.preflight import SECRET_MANAGER_API

        self.gcp._call("DELETE", f"{SECRET_MANAGER_API}/projects/{self.project}/secrets/{name}")

    def enabled_services(self) -> set[str]:
        return set(self.gcp.enabled_services(self.project))

    def enable_services(self, names: Sequence[str]) -> None:
        self.gcp.enable_services(self.project, list(names))

    def service_account(self, email: str) -> Mapping[str, Any] | None:
        return self.gcp.service_account(email)

    def create_service_account(self, account_id: str, display_name: str) -> None:
        self.gcp._call("POST", f"https://iam.googleapis.com/v1/projects/{self.project}/serviceAccounts",
                       body={"accountId": account_id, "serviceAccount": {"displayName": display_name}})

    # swarm-api, as the person (a platform admin signed in with `swarm login`).

    def mint_link(self, addon: str, owner: str) -> Mapping[str, Any]:
        from swarm_mcp.client import SwarmClient  # the swarm_mcp wheel install.sh installs beside this one

        return SwarmClient(context=self.api_context).request(
            "POST", "/v1/admin/setup/links", payload={"kind": "addon-secret", "label": addon, "owner": owner}) or {}

    # Terraform: the root's latest applied inputs plus every add-on's overlay.

    def apply(self, root: str, overlay: Mapping[str, Any], *,
              guard: Callable[[Mapping[str, Any]], None]) -> Mapping[str, Any]:
        from . import tfvars
        from .state import state_bucket
        from .steps import bootstrap
        from .steps.infra import REOPEN_PASS, REOPENED
        from .terraform import Terraform, run_pass

        applied = [p for p in tfvars.PASSES if tfvars.ROOT_OF[p] == root and p != "registry"
                   and self.ctx.outputs.get(bootstrap.applied_key(p))]
        if not applied:
            raise AddonError(f"no {root} pass has been applied; run swarm install to READY first")
        pass_name = applied[-1]
        if pass_name == "infra-close" and self.ctx.outputs.get(REOPENED):
            # `kube` reopened the control plane: keep this machine's address until the close (finding 16).
            pass_name = REOPEN_PASS
        inputs = bootstrap.generate(self.ctx, pass_name, self.gcp)
        _merge(inputs, overlay)
        tf = Terraform(root=root, root_dir=self.deps.module_root(self.ctx, root),
                       state_bucket=state_bucket(self.project), environment=self.ctx.environment,
                       exec_=self.deps.exec_)
        original = tf.plan

        def plan(**kwargs: Any) -> dict[str, Any]:
            found = original(**kwargs)
            guard(found)
            return found

        tf.plan = plan  # type: ignore[method-assign]
        guards = bootstrap.guards_for(self.gcp, lambda: self.deps.as_deployer(
            bootstrap.deployer_email(self.project)), self.project)
        return run_pass(tf, pass_name, inputs, project=self.project, bucket=self.ctx.bucket, guards=guards,
                        confirm=self.deps.confirm,
                        announce=lambda line: self.ctx.events.emit("step_apply", step=f"addon.{root}", message=line))

    # The install cluster (kube_for refuses any other and every deny-listed one).

    def reach_context(self) -> Any:
        """An engine-shaped context for the reopen after the closing pass (finding
        16): it saves through the state store and keeps `ctx` current."""
        if self._reach is None:
            from .state import StateStore, local_path
            from .steps.infra import VerbContext

            store = StateStore(project=self.project, environment=self.ctx.environment,
                               local=local_path(self.project), bucket=self.ctx.bucket)

            def save(change: Callable[[Any], None]) -> Any:
                self.ctx.state = store.update(change)
                self.ctx.outputs = self.ctx.state.outputs
                return self.ctx.state

            self._reach = VerbContext(project=self.project, environment=self.ctx.environment,
                                      holder=self.ctx.holder, state=self.ctx.state, events=self.ctx.events,
                                      bucket=self.ctx.bucket, save=save)
        return self._reach

    def kube(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        if self._kube is None:
            from .steps.cluster import kube_for

            self._kube = kube_for(self.reach_context())
            self._kube.credentials()
        return self._kube(*args, stdin=stdin)

    def close_reach(self) -> str | None:
        """The closing pass again when `kube` reopened the control plane; the line to print if it failed."""
        if self._reach is None:
            return None
        from .steps.infra import REACH

        return REACH.close_after(self._reach, "swarm addon")

    def sync_tailscale_secret(self) -> None:
        """Secret Manager -> a 0600 file -> `kubectl create --dry-run` -> `kubectl apply`,
        as kubernetes/apply.sh does; the value is never an argument or a line of output."""
        gcloud = shutil.which("gcloud")
        if gcloud is None:
            raise AddonError("gcloud is not on PATH; swarm install fetches it (run it once)")
        with tempfile.TemporaryDirectory(prefix="swarm-tailscale-") as work:
            os.chmod(work, 0o700)
            path = Path(work) / "client_secret"
            path.touch(mode=0o600)
            done = subprocess.run([gcloud, "secrets", "versions", "access", "latest", "--secret",
                                   "swarm-tailscale-oauth", "--project", self.project, f"--out-file={path}"],
                                  capture_output=True, text=True, timeout=120, check=False)
            if done.returncode != 0:
                raise AddonError("could not read swarm-tailscale-oauth's latest version (gcloud secrets versions "
                                 "access); the forwarder waits for it")
            if not path.read_bytes().startswith(b"tskey-client-"):
                raise AddonError("swarm-tailscale-oauth's latest version is not a Tailscale OAuth client secret "
                                 "(it does not start tskey-client-); enter the right one through a new link")
            code, rendered, _ = self.kube("create", "secret", "generic", "operator-oauth",
                                          "--namespace=swarm-system", f"--from-file=client_secret={path}",
                                          "--dry-run=client", "-o", "yaml")
            if code != 0:
                raise AddonError("kubectl could not render swarm-system/operator-oauth")
            code, _, _ = self.kube("apply", "-f", "-", stdin=rendered)
            if code != 0:
                raise AddonError("kubectl could not apply swarm-system/operator-oauth")

    def github(self, *args: str) -> tuple[int, str, str]:
        gh = shutil.which("gh")
        if gh is None:
            return 127, "", "gh (the GitHub CLI) is not on PATH: install it and run gh auth login"
        done = subprocess.run([gh, *args], capture_output=True, text=True, timeout=120, check=False)
        return done.returncode, done.stdout, done.stderr
