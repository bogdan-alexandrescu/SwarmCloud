"""`swarm team add <group>`: one team, end to end (docs/design/day-one-plan.md,
lane DAYONE-TEAM-1, part of #2688; design §5.3, §5.7; manifest stage 11).

The CLI and the console's team creation drive ONE path: swarm-api's team
route (`POST /v1/admin/setup/teams`, SETUP-API-1) writes the group workspace
record through the existing workspace path, and `swarm-workspace-apply`
(scripts/workspace-apply.sh -> scripts/register-tenant.sh --workspace) makes
the worker identity, the slots, the namespace and its quota from the record
(lesson 32). This module drives that path and proves it ended well:

| Step | What it proves | Lesson |
|---|---|---|
| `team.probe` | swarm-api's own group identity reads groups, BEFORE anything is registered | 20 |
| `team.create` | the team route answered with the team's workspace | |
| `team.workspace` | the job ran to `ready` (identity, slots, namespace, quota, its own A9 check) | 32 |
| `team.namespace` | the namespace exists and holds `swarm-spec-verify-keys` | 31 |
| `team.git-link` | the team's `-git` slot has an enabled version, filled through a one-time console link, never the terminal | CLAUDE.md |
| `team.providers` | the plans / keys / both answer is in the install state, asked once | llm-providers.md §7 |
| `team.pool-account` | what that answer chose is owned by or lent to the team: a Claude plan, an Anthropic key, or one of each | review-fix 1, §7 |
| `team.smoke` | a smoke agent ran on the team and SUCCEEDED | |

**The verify keys are the installer's to apply.** The job's identity holds no
ConfigMap grant (kubernetes/rbac/provisioner-rbac.yaml: "No ConfigMap"), so a
namespace the job makes has none (lesson 31, #1590). The scheduler hands every
Job the same settings as environment (`scheduler.dispatch.spec_job_env`), and
a worker reads the mount only without them; the ConfigMap is the fallback a
GKE pod still mounts. `ensure_verify_keys` renders it with the bundle's own
`render.py` rule from the keys the scheduler service carries, so the two can
never differ, and applies it on the install cluster as the installer.

**No forge token ever passes through here.** The team's `-git` value is
written by swarm-api's credential writer when the person named on the link
opens it in the console (`POST /v1/setup/links/redeem`); this module only
asks swarm-api to mint the link, opens it (the owner is the caller unless
`--git-owner` or `--first-admin` names another), and polls Secret Manager for
the secret's enabled version: the team is not ready until it has one (audit 4e
F12). For another owner it prints the link for them, since it redeems for
them alone, and opens Admin › Setup, where the team's row shows the link. A
value already there mints no link. It never reads stdin.

**A re-run resumes** (audit 4e F13). The team route answers a workspace already
standing as it is; a failed or `needs_owner` one is retried through
`POST /v1/admin/workspaces/{id}/retry`, the console's own retry, and every
wait's give-up names `swarm team add`, not `swarm install`.

**The smoke waits for a pool account** (review-fix finding 1): the smoke agent
is a `claude-code` task, which needs a Claude pool account the team may run
on. For the first team those are stage 12's accounts, added in Admin › Setup
and owned by the first team; for any later team, an account lent to it in
Admin › People. The wait says which.

**Plans, API keys, or both** (docs/design/llm-providers.md §7, Q10). Before
anything is registered the verb asks "Which will agents run on: plans, API
keys, or both?" and records the answer in the install state
(`outputs["llm_providers"]`, through the verb's context, which saves it to the
state bucket); a re-run that finds it recorded never asks again, and one that
finds none asks once. The pool-account gate checks that choice on the team's
`GET /v1/accounts` rows: `plans` waits for an AVAILABLE Anthropic plan in the
workspace pool, `keys` for an AVAILABLE Anthropic key owned by or lent to the
team, `both` for one of each, so a team on keys alone finishes and an operator
who meant both is not let through on one. Only Anthropic counts, because the
smoke agent is `claude-code`, whose chain is Anthropic plans then Anthropic
keys (§5.3). **A key is never typed here** (the terminal keeps scrollback and
may be recorded): the wait opens, and the summary prints, Capacity › LLM
Providers › API Keys, where the console's one-step form takes it.

**The summary** prints the choice and each chain of §5.3 with what the team has
registered now, read from the same `GET /v1/accounts` (which, for the team,
includes its own Voice-it-only accounts) and, when Voice it is on,
`GET /v1/voice/pool`; it flags a chain with nothing registered of a kind the
operator chose, names the tier the smoke agent's attempt ran on
(`credential_tier`, contract request 152), and, with Voice it on, says where
its dedicated key goes (`tfvars.voice_it_summary`). A read that fails is said
in its line and never fails a team whose smoke is green.

**`swarm team repair <team>` applies one team's tenant render again**
(DAYONE-TEAM-REPAIR; `TeamRepair`). `swarm doctor` names it for a team whose
namespace lacks what the render holds (the dispatcher RBAC, rbac.dispatcher-role).
It reuses what team creation applies and nothing else: swarm-workspace-apply
is the one writer of a team's namespace (design §5.7), so for a ready
workspace the repair writes the record's own ceiling again through the console's
ceiling route (`PUT /v1/admin/workspaces/{id}/limits`), which starts the job's
`limits` run, and that run re-runs A7, `kubernetes/apply.sh --tenant`, the
whole tenant render; a stopped workspace is retried instead, through the
console's retry, and its create run re-applies everything. The repair waits for
the run to end, puts the namespace's verify keys back as `team add` does (the
job's identity holds no ConfigMap grant), and ends only when the dispatcher and
reaper Roles and RoleBindings are in the namespace. Nothing changes until the
person types the team (`SWARM_ASSUME_YES` is not read).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

from swarm_common.identity import tenant_id_for_group, tenant_id_for_user

from . import steps, tfvars
from .events import Events
from .exitcodes import MET, UNMET, WAITING
from .waits import Clock, WaitSpec, wait_until

#: The namespace the job makes for a tenant (register-tenant.sh, `ws_expect namespace`).
NAMESPACE_PREFIX = "swarm-tenant-"
VERIFY_KEYS_CONFIG_MAP = "swarm-spec-verify-keys"
#: The settings the scheduler hands a Job (scheduler.dispatch.spec_job_env),
#: and the ones render.py's ConfigMap carries.
SPEC_SETTINGS = ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY", "SPEC_SIGNATURE_MODE", "SPEC_LEGACY_CUTOVER")
SPEC_REQUIRED = ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY")

#: The workspace states (swarm_api.workspaces) the job's run ends in.
READY = "ready"
STOPPED = ("failed", "needs_owner", "denied")
#: The ones `People.retry` starts again (swarm_api.people.RETRYABLE); `denied` is an admin's decision.
RETRYABLE = ("failed", "needs_owner")

#: The verb a person runs again to resume; the verb passes the whole command.
RESUME = "swarm team add"

#: The console's pages a wait sends the person to (apps/swarm-ui App.tsx routes).
SETUP_PAGE = "/admin/setup"
PEOPLE_PAGE = "/admin/people"

#: The task states a smoke run ends in.
SUCCEEDED = "SUCCEEDED"
TASK_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")

#: The smoke agent: the cheapest class, no repository, a short bound.
SMOKE_PROFILE = "claude-code"
SMOKE_MODEL_CLASS = "haiku"
SMOKE_TIMEOUT_S = 900
SMOKE_PROMPT = ("This is the SwarmCloud team smoke test. Reply with the single word READY and stop; "
                "change no file.")

WORKSPACE_WAIT = WaitSpec(
    reason="swarm-workspace-apply making the team's identity, slots and namespace",
    expected="2 to 10 min",
    expected_s=10 * 60,
    bound_s=30 * 60,
    look_at="Admin › People shows the workspace's step and failure; the job's execution log",
    interval_s=15,
)
GIT_WAIT = WaitSpec(
    reason="the team's GitHub credential to be stored",
    expected="as long as the link's owner takes to open it",
    expected_s=15 * 60,
    bound_s=60 * 60,
    look_at="Admin › Setup shows the team's git link, its owner and when it expires; mint a fresh one there",
    interval_s=15,
)
POOL_ACCOUNT_WAIT = WaitSpec(
    reason="a Claude pool account owned by or lent to the team",
    expected="as long as the sign-in takes",
    expected_s=15 * 60,
    bound_s=60 * 60,
    look_at="Admin › Setup › Claude pool accounts (the first team), or Admin › People › lend (any later team)",
    interval_s=15,
)
#: The install state's key for the plans / keys / both answer (llm-providers.md
#: §7, Q10): `{"choice": "plans" | "keys" | "both"}`.
LLM_PROVIDERS = "llm_providers"
PLANS = "plans"
KEYS = "keys"
BOTH = "both"
CHOICES = (PLANS, KEYS, BOTH)
LLM_PROVIDERS_QUESTION = "Which will agents run on: plans, API keys, or both? Type plans, keys or both"
#: What an answer may be typed as, after case and spacing are folded.
ANSWERS = {"plans": PLANS, "plan": PLANS, "keys": KEYS, "key": KEYS, "api keys": KEYS, "api key": KEYS,
           "both": BOTH}
#: An answer that is none of them is asked again, up to this many answers in all.
CHOICE_TRIES = 3
CHOICE_WORDS = {PLANS: "plans", KEYS: "API keys", BOTH: "plans and API keys (both)"}

#: The kinds of `account_to_api`'s `kind` (the broker, §3), and what each answer needs.
PLAN = "plan"
#: A metered developer key (§3).
METERED = "api_key"
NEEDS = {PLANS: (PLAN,), KEYS: (METERED,), BOTH: (PLAN, METERED)}
#: The broker's pools (`accounts.POOL_WORKSPACE`, `POOL_VOICE_IT`).
POOL_WORKSPACE = "workspace"
POOL_VOICE_IT = "voice-it"
#: The vendor the smoke agent runs on: `claude-code`'s chain (§5.3).
SMOKE_VENDOR = "anthropic"

#: Capacity › LLM Providers › API Keys (design §6.1), where the console's
#: one-step key form is. The one-time key link itself is swarm-api's (lane
#: LP-A1); the installer only names the page.
API_KEYS_PAGE = "/capacity/llm-providers/api-keys"

#: The chains of §5.3: (name, vendor, pool, the kinds its tiers take, whether
#: the dedicated Voice it key is its last tier). The Voice it ones are listed
#: only when Voice it is on.
CHAINS: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    ("claude-code", "anthropic", POOL_WORKSPACE, (PLAN, METERED), False),
    ("browser", "anthropic", POOL_WORKSPACE, (METERED,), False),
    ("codex", "openai", POOL_WORKSPACE, (PLAN, METERED), False),
)
VOICE_CHAINS: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    ("Voice it Live", "openai", POOL_VOICE_IT, (METERED,), True),
    ("Voice it conversation", "openai", POOL_VOICE_IT, (PLAN, METERED), True),
    ("Voice it planner", "anthropic", POOL_VOICE_IT, (PLAN, METERED), False),
)
#: How the summary names the tier the smoke agent's attempt kept
#: (`Attempt.credential_tier`, `credential_source`, request 152).
TIER_WORDS = {PLAN: "a plan", METERED: "an API key"}

SMOKE_WAIT = WaitSpec(
    reason="the team's smoke agent to finish",
    expected="1 to 5 min",
    expected_s=5 * 60,
    bound_s=20 * 60,
    look_at="the task's page in the console; swarm status <task id>",
    interval_s=10,
)


class TeamError(RuntimeError):
    """The team cannot go on; the message says why and what to do."""


class RepairRefused(TeamError):
    """The person did not type the team, so nothing was changed."""


class TeamApi(Protocol):
    """swarm-api, called as the person running the verb (a platform admin)."""

    def groups_probe(self) -> Mapping[str, Any]: ...

    def caller(self) -> str: ...

    def team_ids(self) -> Sequence[str]: ...

    def create_team(self, group: str, *, display_name: str | None,
                    first_admin: str | None) -> Mapping[str, Any]: ...

    def workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

    def team_git_link(self, team: str, owner: str) -> Mapping[str, Any]: ...

    def pool_accounts(self, team: str) -> Sequence[Mapping[str, Any]]: ...

    def summary_accounts(self, team: str) -> Sequence[Mapping[str, Any]]: ...

    def voice_pool(self) -> Mapping[str, Any]: ...

    def smoke_attempts(self, team: str, task_id: str) -> Sequence[Mapping[str, Any]]: ...

    def start_smoke(self, team: str) -> str: ...

    def task_state(self, team: str, task_id: str) -> str: ...


class Cluster(Protocol):
    """The install cluster, through the engine's own context (lesson 37)."""

    def get(self, kind: str, name: str, namespace: str = "") -> Mapping[str, Any] | None: ...

    def apply(self, manifest: str) -> None: ...


def namespace_of(team: str) -> str:
    return f"{NAMESPACE_PREFIX}{team}"


def git_secret_of(team: str) -> str:
    """The team's git slot (swarm_api.setup_links: `swarm-tenant-<tenant>-git`)."""
    return f"swarm-tenant-{team}-git"


def git_secret_ready(gcp: Any, project: str, team: str) -> bool:
    """True once the team's git slot exists and holds an enabled version: what
    the redeemed link writes, and what a task's forge broker reads."""
    name = git_secret_of(team)
    return gcp.secret(project, name) is not None and gcp.enabled_version(project, name) is not None


def verify_keys_manifest(tenant: str, settings: Mapping[str, str]) -> str:
    """The `swarm-spec-verify-keys` ConfigMap for the tenant's namespace, in
    kubernetes/render.py's shape (`render_spec_verify_keys`): every value a
    JSON string, so a PEM can never open a second YAML document."""
    missing = [k for k in SPEC_REQUIRED if not settings.get(k)]
    if missing:
        raise TeamError(
            f"the scheduler carries no {', '.join(missing)}, so there are no step-spec keys to put in "
            f"{namespace_of(tenant)}: every signed task would end CANNOT_START (lesson 31). "
            "Run swarm install again to finish infra pass 2, then swarm team add again.")
    try:
        keys = json.loads(settings["SPEC_VERIFY_KEYS"])
    except ValueError:
        raise TeamError("the scheduler's SPEC_VERIFY_KEYS is not JSON; swarm doctor names the fix") from None
    if not isinstance(keys, dict) or not keys:
        raise TeamError("the scheduler's SPEC_VERIFY_KEYS is empty; it would verify nothing")
    namespace = namespace_of(tenant)
    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {VERIFY_KEYS_CONFIG_MAP}",
        f"  namespace: {namespace}",
        "  labels:",
        "    managed-by: swarm-terraform",
        f"    swarm-tenant: {tenant}",
        "    app.kubernetes.io/part-of: swarm",
        "data:",
    ]
    lines += [f"  {key}: {json.dumps(settings[key])}" for key in SPEC_SETTINGS if settings.get(key)]
    return "\n".join(lines) + "\n"


def ensure_verify_keys(cluster: Cluster, *, tenant: str, settings: Mapping[str, str]) -> bool:
    """Apply the namespace's verify keys when they are absent or differ from
    the scheduler's; True when it applied. Raises TeamError when the keys
    cannot be put in place or are still absent afterwards (lesson 31)."""
    namespace = namespace_of(tenant)
    if cluster.get("Namespace", namespace) is None:
        raise TeamError(f"namespace {namespace} does not exist: swarm-workspace-apply did not make it")
    manifest = verify_keys_manifest(tenant, settings)
    want = {k: settings[k] for k in SPEC_SETTINGS if settings.get(k)}
    found = cluster.get("ConfigMap", VERIFY_KEYS_CONFIG_MAP, namespace)
    if found is not None and dict(found.get("data") or {}) == want:
        return False
    cluster.apply(manifest)
    if cluster.get("ConfigMap", VERIFY_KEYS_CONFIG_MAP, namespace) is None:
        raise TeamError(f"{VERIFY_KEYS_CONFIG_MAP} is still absent from {namespace} after the apply")
    return True


def probe_explanation(result: Mapping[str, Any]) -> str:
    """Lesson 20's two causes, told apart."""
    reason = str(result.get("reason") or "")
    return (f"swarm-api cannot read {result.get('group')} ({reason or 'unreadable'}). A signJwt 403 is a "
            "missing GCP grant on swarm-api's group identity; `unauthorized_client` is the Admin console's "
            "domain-wide delegation. The Workspace Groups Reader admin role avoids both (design §8). "
            "swarm doctor names which.")


def account_kind(row: Mapping[str, Any]) -> str:
    """`plan` or `api_key`: the row's `kind`, else from its provider (`*-api` is a key)."""
    kind = row.get("kind")
    if kind in (PLAN, METERED):
        return str(kind)
    return METERED if str(row.get("provider") or "").endswith("-api") else PLAN


def account_vendor(row: Mapping[str, Any]) -> str:
    """The row's `vendor`, else its provider without `-api`; a row with
    neither is a Claude plan, as every account was before §3."""
    provider = str(row.get("provider") or "")
    return str(row.get("vendor") or provider.removesuffix("-api") or "anthropic")


def account_pools(row: Mapping[str, Any]) -> tuple[str, ...]:
    pools = row.get("pools")
    return tuple(str(p) for p in pools) if isinstance(pools, (list, tuple)) else (POOL_WORKSPACE,)


def serves_team(row: Mapping[str, Any], team: str) -> bool:
    """Owned by or lent to `team` (`lend_to`, or a capped lend in `lends`).
    A row naming no owner is the team's own listing's, as the broker scopes it."""
    owner = row.get("owner_tenant")
    if owner is None:
        return True
    lends = row.get("lends")
    return (owner == team or team in (row.get("lend_to") or ())
            or (isinstance(lends, Mapping) and team in lends))


def usable_kinds(rows: Sequence[Mapping[str, Any]], team: str) -> set[str]:
    """The kinds the team's smoke agent could run on now: AVAILABLE, Anthropic,
    in the workspace pool, owned by or lent to the team."""
    return {account_kind(r) for r in rows
            if str(r.get("state") or "").upper() == "AVAILABLE" and account_vendor(r) == SMOKE_VENDOR
            and POOL_WORKSPACE in account_pools(r) and serves_team(r, team)}


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def chain_line(chain: tuple[str, str, str, tuple[str, ...], bool], rows: Sequence[Mapping[str, Any]],
               team: str, choice: str, voice: Mapping[str, Any] | None = None) -> str:
    """One chain of §5.3 with what is registered for it now, flagged when a
    kind the operator chose has nothing."""
    name, vendor, pool, kinds, dedicated_tier = chain
    mine = [r for r in rows if account_vendor(r) == vendor and pool in account_pools(r)
            and (pool != POOL_WORKSPACE or serves_team(r, team))]
    plans = [r for r in mine if account_kind(r) == PLAN]
    keys = [r for r in mine if account_kind(r) == METERED and not r.get("voice_dedicated")]
    dedicated = dedicated_tier and any(r.get("voice_dedicated") and account_kind(r) == METERED for r in mine)
    if name == "Voice it Live":
        counts = ("no key" if not keys and not dedicated else
                  f"{_count(len(keys), 'shared key')}, " + ("the dedicated key" if dedicated else "no dedicated key"))
        account = (voice or {}).get("account") if isinstance(voice, Mapping) else None
        if isinstance(account, Mapping):
            counts += f"; the next session leases {account.get('display_name') or account.get('label')}"
        elif isinstance(voice, Mapping) and voice.get("reason"):
            counts += f" ({voice.get('reason')})"
    else:
        parts = []
        if PLAN in kinds:
            parts.append(_count(len(plans), "plan"))
        if METERED in kinds:
            parts.append(_count(len(keys), "key"))
        if dedicated:
            parts.append("the dedicated key")
        counts = ", ".join(parts)
    missing = []
    if PLAN in kinds and PLAN in NEEDS[choice] and not plans:
        missing.append("no plan")
    if METERED in kinds and METERED in NEEDS[choice] and not keys and not dedicated:
        missing.append("no key")
    flag = f"  <- nothing registered of the kind chosen: {', '.join(missing)}" if missing else ""
    return f"  {name}: {counts}{flag}"


def smoke_tier_words(attempts: Sequence[Mapping[str, Any]]) -> str:
    """The tier the smoke agent's last attempt kept, in words."""
    rows = [a for a in attempts if isinstance(a, Mapping)]
    if not rows:
        return "a tier no attempt recorded"
    last = max(rows, key=lambda a: a.get("attempt") if isinstance(a.get("attempt"), int) else 0)
    tier = last.get("credential_tier")
    if tier in TIER_WORDS:
        return TIER_WORDS[str(tier)]
    if last.get("credential_source") == "tenant_secret":
        return "the team's own provider secret"
    return "a tier the attempt did not record"


@dataclass
class TeamAdd:
    api: TeamApi
    cluster: Callable[[], Cluster]
    spec_settings: Callable[[], Mapping[str, str]]
    #: True once `swarm-tenant-<team>-git` has an enabled version (`git_secret_ready`).
    git_secret: Callable[[str], bool]
    #: The console's origin, `https://<hostname>`.
    console: str
    clock: Clock
    events: Events
    out: TextIO
    #: The command a give-up tells the person to run again.
    resume: str = RESUME
    open_page: Callable[..., None] = steps.open_page
    #: The verb's context (`steps.infra.VerbContext`): `state`, the install
    #: state the plans / keys / both answer is read from, and `save(change)`,
    #: which records it in the state bucket.
    ctx: Any = None
    #: Asks one plain question and returns the answer; the terminal by default.
    #: Never a key: only the plans / keys / both question is ever asked.
    ask: Callable[[str], str] | None = None

    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

    def page(self, path: str, why: str) -> None:
        self.open_page(f"{self.console}{path}", why=why, say=self.say)

    def wait(self, step: str, spec: WaitSpec, check: Callable[[], str]) -> None:
        wait_until(step, spec, check, clock=self.clock, events=self.events, resume=self.resume)

    def _terminal_ask(self, question: str) -> str:
        from .secret_entry import DevTty, NoTerminal

        try:
            terminal = DevTty.open()
        except NoTerminal:
            raise TeamError(f"{question!r} needs an answer typed in a terminal; nothing was recorded. In your own "
                            f"terminal run: {self.resume}") from None
        try:
            terminal.write(f"{question}: ")
            return terminal.read_line()
        finally:
            terminal.close()

    # -- the steps ---------------------------------------------------------

    def recorded_choice(self) -> str | None:
        """The plans / keys / both answer in the install state, or None while unanswered."""
        if self.ctx is None:
            raise TeamError("swarm team add has no install state to read or record the plans / keys / both "
                            "answer in; run swarm install --status")
        found = self.ctx.state.outputs.get(LLM_PROVIDERS)
        if found is None:
            return None
        choice = found.get("choice") if isinstance(found, Mapping) else None
        if choice not in CHOICES:
            raise TeamError(f"the install state's {LLM_PROVIDERS} is {found!r}, not one of plans, keys or both; "
                            "nothing was asked")
        return str(choice)

    def choose(self) -> str:
        """Asked once: a recorded answer stands, so a resumed install never asks again."""
        recorded = self.recorded_choice()
        if recorded is not None:
            self.events.emit("step_done", step="team.providers", state=MET, detail=f"{recorded} (recorded)")
            return recorded
        ask = self.ask or self._terminal_ask
        asked = LLM_PROVIDERS_QUESTION
        for _ in range(CHOICE_TRIES):
            answer = " ".join(ask(asked).strip().lower().split())
            choice = ANSWERS.get(answer)
            if choice is not None:
                def record(state: Any, chosen: str = choice) -> None:
                    state.outputs[LLM_PROVIDERS] = {"choice": chosen}

                self.ctx.save(record)
                self.events.emit("step_done", step="team.providers", state=MET, detail=choice)
                return choice
            asked = f"{answer!r} is not plans, keys or both. {LLM_PROVIDERS_QUESTION}"
        raise TeamError(f"no plans, keys or both after {CHOICE_TRIES} answers; nothing was recorded. "
                        f"Run {self.resume} again.")

    def probe(self, group: str) -> None:
        """Lesson 20: before anything is registered, swarm-api's own group
        identity must read groups. The answer is met/unmet per group swarm-api
        knows, never a member list."""
        self.events.emit("step_check", step="team.probe", detail=group)
        answer = self.api.groups_probe()
        results = [r for r in answer.get("groups") or () if isinstance(r, Mapping)]
        if not results:
            raise TeamError("swarm-api's groups probe named no group, so it proved nothing; "
                            "stage 8 configures the admin group. Run swarm install --status.")
        unmet = [r for r in results if r.get("state") != MET]
        if unmet:
            raise TeamError(probe_explanation(unmet[0]) + " Nothing was registered.")
        self.events.emit("step_done", step="team.probe", state=MET)

    def create(self, group: str, display_name: str | None, first_admin: str | None) -> tuple[str, str]:
        """The team route; on a re-run it answers the workspace already
        standing, and a stopped one is retried here, as the console's retry
        does, so the re-run resumes instead of stopping on the same failure."""
        self.events.emit("step_apply", step="team.create", detail=group)
        answer = self.api.create_team(group, display_name=display_name, first_admin=first_admin)
        team = str(answer.get("team") or "")
        workspace = answer.get("workspace") or {}
        workspace_id = str(workspace.get("workspace_id") or "") if isinstance(workspace, Mapping) else ""
        if not team or not workspace_id:
            raise TeamError("swarm-api's team route answered without the team or its workspace")
        state = str(workspace.get("state") or "")
        if state in RETRYABLE:
            self.events.emit("step_apply", step="team.create", detail=f"retry workspace {workspace_id} ({state})")
            self.api.retry_workspace(workspace_id)
            self.say(f"The team's workspace {workspace_id} had stopped at {state}; it is being retried.")
        self.events.emit("step_done", step="team.create", detail=f"team {team}, workspace {workspace_id}")
        return team, workspace_id

    def await_workspace(self, workspace_id: str) -> None:
        def check() -> str:
            view = self.api.workspace(workspace_id)
            state = str(view.get("state") or "")
            if state == READY:
                return MET
            if state in STOPPED:
                failure = view.get("failure") or {}
                code = failure.get("code") if isinstance(failure, Mapping) else None
                step = (failure.get("step") if isinstance(failure, Mapping) else None) or view.get(
                    "needs_owner_step")
                then = (f" Fix what it names, then run {self.resume} again: it retries the workspace."
                        if state in RETRYABLE else " An admin denied it in Admin › People; nothing retries it.")
                raise TeamError(
                    f"swarm-workspace-apply stopped the team's workspace {workspace_id} at {state}"
                    + (f", step {step}" if step else "") + (f", code {code}" if code else "")
                    + ". Admin › People shows it." + then)
            return WAITING

        self.wait("team.workspace", WORKSPACE_WAIT, check)
        self.events.emit("step_done", step="team.workspace", state=MET)

    def namespace(self, team: str) -> None:
        applied = ensure_verify_keys(self.cluster(), tenant=team, settings=self.spec_settings())
        self.events.emit("step_done", step="team.namespace",
                         detail=f"{namespace_of(team)}: verify keys " + ("applied" if applied else "present"))

    def git_link(self, team: str, owner: str, *, caller: str) -> None:
        """The team is not ready until its `-git` slot holds a value: mint
        the link, open it (or Admin › Setup, when it is another person's),
        and poll Secret Manager for the enabled version."""
        secret = git_secret_of(team)
        if self.git_secret(team):
            self.events.emit("step_done", step="team.git-link", state=MET, detail=f"{secret} has a value")
            return
        link = self.api.team_git_link(team, owner)
        path = str(link.get("path") or "")
        where = str(link.get("url") or (f"{self.console}{path}" if path.startswith("/") else ""))
        if not where:
            raise TeamError("swarm-api minted the team's git link but answered no address for it")
        self.events.emit("step_apply", step="team.git-link", detail=f"link minted for {owner}")
        until = link.get("expires_at")
        if owner == caller:
            self.open_page(where, why=(
                f"The team's GitHub credential goes in through a one-time link, for you only, until {until}. "
                "Opening it in your browser; the value is never typed here."), say=self.say)
        else:
            self.say(f"The team's GitHub credential goes in through a one-time link, for {owner} only, "
                     f"until {until}. Send it to them to open in a browser signed in as themselves:")
            self.say(f"  {where}")
            self.say("The value is never typed here.")
            self.page(SETUP_PAGE, f"Admin › Setup shows the team's git link and whether {owner} has used it.")

        def check() -> str:
            return MET if self.git_secret(team) else WAITING

        spec = WaitSpec(reason=f"{GIT_WAIT.reason} ({secret}, through {owner}'s link)", expected=GIT_WAIT.expected,
                        expected_s=GIT_WAIT.expected_s, bound_s=GIT_WAIT.bound_s, look_at=GIT_WAIT.look_at,
                        interval_s=GIT_WAIT.interval_s)
        self.wait("team.git-link", spec, check)
        self.events.emit("step_done", step="team.git-link", state=MET, detail=f"{secret} has a value")

    def await_pool_account(self, team: str, *, first_team: bool, choice: str = PLANS) -> None:
        """The gate checks the operator's choice (llm-providers.md §7): `plans`
        a Claude plan, `keys` an Anthropic key, `both` one of each, each owned
        by or lent to the team."""
        if first_team:
            plan_how = ("this is the first team, so its accounts are stage 12's: add them in "
                        "Admin › Setup › Claude pool accounts (they are owned by this team)")
            plan_where = SETUP_PAGE
            key_how = ("add an Anthropic API key owned by this team in Capacity › LLM Providers › API Keys, "
                       "with the console's one-step form")
        else:
            plan_how = "lend one of the pool's accounts to this team in Admin › People"
            plan_where = PEOPLE_PAGE
            key_how = "lend an Anthropic API key to this team, with its caps, in Capacity › LLM Providers › API Keys"
        what = {PLAN: ("a Claude plan", plan_how, plan_where), METERED: ("an Anthropic API key", key_how, API_KEYS_PAGE)}
        needs = NEEDS[choice]
        how = "; and ".join(what[k][1] for k in needs)
        reason = (POOL_ACCOUNT_WAIT.reason if needs == (PLAN,) else
                  " and ".join(what[k][0] for k in needs) + (" each" if len(needs) > 1 else "")
                  + " owned by or lent to the team")

        opened: set[str] = set()

        def check() -> str:
            usable = usable_kinds(self.api.pool_accounts(team), team)
            missing = [k for k in needs if k not in usable]
            if not missing:
                return MET
            for kind in missing:
                if kind in opened:
                    continue
                # Only while there is none yet: the page is where the person adds or lends one.
                opened.add(kind)
                name, kind_how, where = what[kind]
                never = " The key is never typed here." if kind == METERED else ""
                self.page(where, f"The smoke agent starts once {name} is owned by or lent to {team}; "
                                 f"{kind_how}.{never}")
            return WAITING

        spec = WaitSpec(reason=f"{reason} ({how})", expected=POOL_ACCOUNT_WAIT.expected,
                        expected_s=POOL_ACCOUNT_WAIT.expected_s, bound_s=POOL_ACCOUNT_WAIT.bound_s,
                        look_at=POOL_ACCOUNT_WAIT.look_at + ("; Capacity › LLM Providers › API Keys"
                                                             if METERED in needs else ""),
                        interval_s=POOL_ACCOUNT_WAIT.interval_s)
        self.wait("team.pool-account", spec, check)
        self.events.emit("step_done", step="team.pool-account", state=MET, detail=choice)

    def smoke(self, team: str) -> str:
        task_id = self.api.start_smoke(team)
        if not task_id:
            raise TeamError("swarm-api accepted the smoke agent but answered no task id")
        self.events.emit("step_apply", step="team.smoke", detail=f"task {task_id}")
        final: list[str] = []

        def check() -> str:
            state = self.api.task_state(team, task_id).upper()
            if state in TASK_TERMINAL:
                final.append(state)
                return MET if state == SUCCEEDED else UNMET
            return WAITING

        def check_until_terminal() -> str:
            status = check()
            if status == UNMET:
                raise TeamError(f"the smoke agent {task_id} ended {final[-1]}; its page in the console says why")
            return status

        self.wait("team.smoke", SMOKE_WAIT, check_until_terminal)
        self.events.emit("step_done", step="team.smoke", state=MET, detail=f"task {task_id} SUCCEEDED")
        return task_id

    def summary(self, team: str, choice: str, task_id: str) -> None:
        """The choice, each chain of §5.3 with what it has now (flagged when a
        chosen kind has nothing), Voice it's line, the smoke's tier and, for a
        keys answer, the console page where a key goes. Informational: a read
        that fails says so and never fails the team."""
        self.say(f"Agents run on: {CHOICE_WORDS[choice]}")
        voice_on = bool(tfvars.voice_it_chosen(self.ctx.state))
        try:
            rows = [r for r in self.api.summary_accounts(team) if isinstance(r, Mapping)]
        except Exception as exc:  # noqa: BLE001 - the summary must still print the rest
            rows = None
            self.say(f"  could not read the accounts ({type(exc).__name__}); Capacity › LLM Providers shows them")
        voice: Mapping[str, Any] | None = None
        if voice_on:
            try:
                voice = self.api.voice_pool()
            except Exception as exc:  # noqa: BLE001
                self.say(f"  could not read the Voice it pool ({type(exc).__name__})")
        if rows is not None:
            for chain in CHAINS + (VOICE_CHAINS if voice_on else ()):
                self.say(chain_line(chain, rows, team, choice, voice))
        line = tfvars.voice_it_summary(self.ctx.state, self.console)
        if line:
            self.say(line)
        try:
            tier = smoke_tier_words(self.api.smoke_attempts(team, task_id))
        except Exception as exc:  # noqa: BLE001
            tier = f"a tier that could not be read ({type(exc).__name__})"
        self.say(f"The smoke agent {task_id} ran on {tier}.")
        if METERED in NEEDS[choice]:
            self.say("API keys go in through the console's one-step form, never typed here: "
                     f"{self.console}{API_KEYS_PAGE}")

    # -- the whole -------------------------------------------------------------

    def run(self, group: str, *, display_name: str | None = None, first_admin: str | None = None,
            git_owner: str | None = None) -> dict[str, Any]:
        choice = self.choose()
        self.probe(group)
        caller = self.api.caller().strip().lower()
        if not caller:
            raise TeamError("swarm-api did not say who is signed in; run swarm login, then swarm team add again")
        before = list(self.api.team_ids())
        team, workspace_id = self.create(group, display_name, first_admin)
        # The first team owns stage 12's pool accounts; a later one is lent one.
        # On a re-run the team itself is already listed, and is still the first.
        first_team = not [t for t in before if t != team]
        self.await_workspace(workspace_id)
        self.namespace(team)
        self.git_link(team, (git_owner or first_admin or caller).strip().lower(), caller=caller)
        self.await_pool_account(team, first_team=first_team, choice=choice)
        task_id = self.smoke(team)
        self.say(f"Team {team} is ready: namespace {namespace_of(team)}, quota, verify keys, worker identity, "
                 f"its GitHub credential ({git_secret_of(team)}), slots, and a green smoke agent ({task_id}).")
        self.summary(team, choice, task_id)
        return {"team": team, "workspace": workspace_id, "smoke_task": task_id}


# ---------------------------------------------------------------------------
# `swarm team repair <team>`: the team's tenant render, applied again
# ---------------------------------------------------------------------------

#: What the doctor's rbac.dispatcher-role probe reads, and what the tenant
#: render holds for it (kubernetes/rbac/dispatcher-rbac.yaml): checked present
#: after the run.
DISPATCHER_RBAC = (("Role", "swarm-dispatcher"), ("RoleBinding", "swarm-dispatcher"),
                   ("Role", "swarm-reaper"), ("RoleBinding", "swarm-reaper"))

REPAIR_RUN_WAIT = WaitSpec(
    reason="swarm-workspace-apply applying the team's tenant render again",
    expected="2 to 10 min",
    expected_s=10 * 60,
    bound_s=30 * 60,
    look_at="Admin › People shows the workspace's run and failure; the job's execution log",
    interval_s=15,
)


class RepairApi(Protocol):
    """swarm-api, called as the person running the verb (a platform admin)."""

    def workspaces(self) -> Sequence[Mapping[str, Any]]: ...

    def workspace(self, workspace_id: str) -> Mapping[str, Any]: ...

    def set_ceiling(self, workspace_id: str, max_active: int) -> Mapping[str, Any]: ...

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]: ...


def tenant_of(record: Mapping[str, Any]) -> str:
    """The tenant a workspace record makes, by the frozen contract's rule (the
    admin view carries no tenant id): a group's from its address, a person's
    or a service account's with the `u-` prefix (swarm_api.workspaces)."""
    principal = str(record.get("principal") or "").strip().lower()
    if not principal:
        return ""
    if str(record.get("kind") or "personal") == "group":
        return tenant_id_for_group(principal)
    return tenant_id_for_user(principal)


def _run_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    run = record.get("run")
    return run if isinstance(run, Mapping) else {}


@dataclass
class TeamRepair:
    api: RepairApi
    cluster: Callable[[], Cluster]
    spec_settings: Callable[[], Mapping[str, str]]
    #: Reads the typed answer (verbs/install.py `_typed`: the terminal, never the environment).
    confirm: Callable[[str], str]
    clock: Clock
    events: Events
    out: TextIO
    #: The command a give-up tells the person to run again.
    resume: str = "swarm team repair"

    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()

    def find(self, team: str) -> tuple[str, Mapping[str, Any]]:
        """(tenant, record) for a team id (`eng`, as swarm doctor names it) or
        its group's address."""
        wanted = team.strip().lower()
        for record in self.api.workspaces():
            tenant = tenant_of(record)
            if wanted in (tenant, str(record.get("principal") or "").strip().lower()):
                return tenant, record
        raise TeamError(f"no workspace makes team {team}: swarm doctor names the team, and Admin › People "
                        "lists every workspace. Nothing was changed.")

    def start(self, tenant: str, record: Mapping[str, Any]) -> str:
        """Start the run that applies the tenant render again; the mode it runs."""
        workspace_id = str(record.get("workspace_id") or "")
        state = str(record.get("state") or "")
        if state in RETRYABLE:
            self.api.retry_workspace(workspace_id)
            self.events.emit("step_apply", step="team.repair", detail=f"retry workspace {workspace_id} ({state})")
            return "create"
        limits = dict(record.get("limits") or {})
        answer = self.api.set_ceiling(workspace_id, int(limits["max_active"]))
        dispatch = answer.get("dispatch") or {}
        if not isinstance(dispatch, Mapping) or dispatch.get("published") is not True:
            reason = dispatch.get("reason") if isinstance(dispatch, Mapping) else None
            raise TeamError(
                f"swarm-api did not start swarm-workspace-apply for {workspace_id} ({reason or 'no run'}), so "
                f"nothing was applied. With publishing off the deployment builds no workspace; otherwise run "
                f"{self.resume} again.")
        after = answer.get("workspace") or {}
        changed = [f"{k} {limits.get(k)} -> {v}" for k, v in sorted(dict(after.get("limits") or {}).items())
                   if isinstance(after, Mapping) and limits.get(k) != v]
        if changed:
            self.say("The ceiling route wrote the ceiling's own limits: " + ", ".join(changed) + ".")
        self.events.emit("step_apply", step="team.repair", detail=f"limits run for workspace {workspace_id}")
        return "limits"

    def await_run(self, workspace_id: str, before: Mapping[str, Any], mode: str) -> str:
        """Wait for the run started after `before` to end; its build id."""
        done: list[str] = []

        def check() -> str:
            view = self.api.workspace(workspace_id)
            run = _run_of(view)
            fresh = run.get("claimed_at") and (run.get("claimed_at"), run.get("build_id")) != (
                before.get("claimed_at"), before.get("build_id"))
            if not fresh or not run.get("finished_at"):
                return WAITING
            failure = view.get("failure")
            if isinstance(failure, Mapping) and failure:
                raise TeamError(
                    f"swarm-workspace-apply's {mode} run for {workspace_id} stopped at step "
                    f"{failure.get('step') or '?'}, code {failure.get('code') or '?'}. Admin › People shows it; "
                    f"fix what it names, then run {self.resume} again.")
            if mode == "create" and str(view.get("state") or "") != READY:
                return WAITING
            done.append(str(run.get("build_id") or ""))
            return MET

        wait_until("team.repair", REPAIR_RUN_WAIT, check, clock=self.clock, events=self.events, resume=self.resume)
        return done[-1]

    def verify(self, tenant: str) -> None:
        cluster = self.cluster()
        ensure_verify_keys(cluster, tenant=tenant, settings=self.spec_settings())
        namespace = namespace_of(tenant)
        absent = [f"{kind} {name}" for kind, name in DISPATCHER_RBAC if cluster.get(kind, name, namespace) is None]
        if absent:
            raise TeamError(f"{', '.join(absent)} still absent from {namespace} after swarm-workspace-apply's run; "
                            "its execution log says what A7 applied.")

    def run(self, team: str) -> dict[str, Any]:
        tenant, record = self.find(team)
        workspace_id = str(record.get("workspace_id") or "")
        state = str(record.get("state") or "")
        if state != READY and state not in RETRYABLE:
            raise TeamError(f"the workspace {workspace_id} of team {tenant} is {state or 'without a state'}: only "
                            "a ready or a stopped workspace is applied again. Admin › People shows it.")
        limits = dict(record.get("limits") or {})
        if state == READY and not isinstance(limits.get("max_active"), int):
            raise TeamError(f"the workspace {workspace_id} records no ceiling (limits.max_active)")
        how = (f"retries the stopped workspace {workspace_id} ({state}): its create run applies everything again"
               if state in RETRYABLE else
               f"writes the workspace {workspace_id}'s ceiling again (limits: "
               + ", ".join(f"{k} {v}" for k, v in sorted(limits.items()))
               + "); the ceiling route writes the ceiling's own quota, and its limits run re-applies the "
               "namespace's whole tenant render")
        typed = self.confirm(f"This has swarm-workspace-apply apply team {tenant}'s tenant render to "
                             f"{namespace_of(tenant)} again: it {how}.\nType {team} to go on: ")
        if typed.strip() != team:
            raise RepairRefused(f"{team} was not typed; nothing was changed")
        before = _run_of(record)
        mode = self.start(tenant, record)
        build = self.await_run(workspace_id, before, mode)
        self.verify(tenant)
        self.events.emit("step_done", step="team.repair", state=MET, detail=f"{namespace_of(tenant)} applied again")
        self.say(f"Team {tenant}'s tenant render is applied again in {namespace_of(tenant)} (run {build}): the "
                 "dispatcher and reaper RBAC and the verify keys are there.")
        return {"team": tenant, "workspace": workspace_id, "run": build}


# ---------------------------------------------------------------------------
# The real seams: swarm-api as the person, the install cluster, the scheduler
# ---------------------------------------------------------------------------


@dataclass
class SwarmApi:
    """swarm-api through the `swarm` CLI's own client (`swarm_mcp.client`),
    signed in as the person with the deployment's Desktop client (lesson 18);
    every `/v1/admin/setup/*` route is a platform admin's (plan §5.2). The
    teammate wheel is imported only here, at run time."""

    context: str | None = None
    _clients: dict[str, Any] = field(default_factory=dict)

    def _client(self, tenant: str | None = None) -> Any:
        key = tenant or ""
        if key not in self._clients:
            from swarm_mcp.client import SwarmClient  # the swarm_mcp wheel install.sh installs beside this one

            self._clients[key] = SwarmClient(context=self.context, tenant=tenant)
        return self._clients[key]

    def groups_probe(self) -> Mapping[str, Any]:
        return self._client().request("POST", "/v1/admin/setup/health/groups-probe", payload={}) or {}

    def caller(self) -> str:
        found = self._client().request("GET", "/v1/tenants/me") or {}
        principal = found.get("principal") or {}
        return str(principal.get("email") or "") if isinstance(principal, Mapping) else ""

    def team_ids(self) -> Sequence[str]:
        found = self._client().request("GET", "/v1/admin/setup") or {}
        return [str(t.get("id")) for t in found.get("teams") or () if isinstance(t, Mapping) and t.get("id")]

    def create_team(self, group: str, *, display_name: str | None, first_admin: str | None) -> Mapping[str, Any]:
        body: dict[str, Any] = {"group": group}
        if display_name:
            body["display_name"] = display_name
        if first_admin:
            body["first_admin"] = first_admin
        return self._client().request("POST", "/v1/admin/setup/teams", payload=body) or {}

    def workspace(self, workspace_id: str) -> Mapping[str, Any]:
        # The route answers {"workspace": <the admin view>} (routes/people.py).
        found = self._client().request("GET", f"/v1/admin/workspaces/{workspace_id}") or {}
        inner = found.get("workspace")
        return inner if isinstance(inner, Mapping) else found

    def workspaces(self) -> Sequence[Mapping[str, Any]]:
        found = self._client().request("GET", "/v1/admin/workspaces") or {}
        return [w for w in found.get("workspaces") or () if isinstance(w, Mapping)]

    def set_ceiling(self, workspace_id: str, max_active: int) -> Mapping[str, Any]:
        return self._client().request("PUT", f"/v1/admin/workspaces/{workspace_id}/limits",
                                      payload={"max_active": int(max_active)}) or {}

    def retry_workspace(self, workspace_id: str) -> Mapping[str, Any]:
        return self._client().request("POST", f"/v1/admin/workspaces/{workspace_id}/retry", payload={}) or {}

    def team_git_link(self, team: str, owner: str) -> Mapping[str, Any]:
        return self._client().request("POST", "/v1/admin/setup/links",
                                      payload={"kind": "team-git", "label": team, "owner": owner}) or {}

    def pool_accounts(self, team: str) -> Sequence[Mapping[str, Any]]:
        found = self._client(team).request("GET", "/v1/accounts") or {}
        return [a for a in found.get("accounts") or () if isinstance(a, Mapping)]

    def summary_accounts(self, team: str) -> Sequence[Mapping[str, Any]]:
        # The team's listing, with its own Voice-it-only accounts (routes/accounts.py).
        return self.pool_accounts(team)

    def voice_pool(self) -> Mapping[str, Any]:
        # The account the caller's next Voice it session would lease, never leased (routes/voice.py).
        return self._client().request("GET", "/v1/voice/pool") or {}

    def smoke_attempts(self, team: str, task_id: str) -> Sequence[Mapping[str, Any]]:
        found = self._client(team).request("GET", f"/v1/tasks/{task_id}/attempts") or {}
        return [a for a in found.get("attempts") or () if isinstance(a, Mapping)]

    def start_smoke(self, team: str) -> str:
        task = self._client(team).dispatch(prompt=SMOKE_PROMPT, runner_profile=SMOKE_PROFILE,
                                           model_class=SMOKE_MODEL_CLASS, timeout_seconds=SMOKE_TIMEOUT_S,
                                           metadata={"source": "swarm team add", "smoke": True})
        return str(task.get("id") or task.get("task_id") or "")

    def task_state(self, team: str, task_id: str) -> str:
        return str((self._client(team).task(task_id) or {}).get("state") or "")


@dataclass
class KubeCluster:
    """The engine's `Kube` (steps/cluster.py): its own kubeconfig, the install
    cluster's context only, kubectl through `prefer_local_bin`'s rule."""

    kube: Any

    def get(self, kind: str, name: str, namespace: str = "") -> Mapping[str, Any] | None:
        args = ["get", kind.lower(), name, "-o", "json"]
        if namespace:
            args[1:1] = ["-n", namespace]
        code, out, err = self.kube(*args)
        if code != 0:
            if "NotFound" in err or "not found" in err:
                return None
            raise TeamError(f"kubectl get {kind.lower()} {name} failed: {err.strip()[:300]}")
        return json.loads(out or "{}")

    def apply(self, manifest: str) -> None:
        code, _out, err = self.kube("apply", "-f", "-", stdin=manifest)
        if code != 0:
            raise TeamError(f"kubectl apply of {VERIFY_KEYS_CONFIG_MAP} failed: {err.strip()[:300]}")


def scheduler_spec_settings(gcp: Any, project: str, region: str) -> dict[str, str]:
    """The step-spec settings the `swarm-scheduler` service carries, which it
    hands every Job it creates (`scheduler.dispatch.spec_job_env`)."""
    service = gcp.run_service(project, region, "swarm-scheduler") or {}
    out: dict[str, str] = {}
    for container in (service.get("template") or {}).get("containers") or ():
        for var in container.get("env") or ():
            if var.get("name") in SPEC_SETTINGS and var.get("value"):
                out[str(var["name"])] = str(var["value"])
    return out


__all__ = [
    "Cluster", "KubeCluster", "RepairApi", "RepairRefused", "SwarmApi", "TeamAdd", "TeamApi", "TeamError",
    "TeamRepair", "ensure_verify_keys", "tenant_of",
    "git_secret_of", "git_secret_ready", "namespace_of", "scheduler_spec_settings", "verify_keys_manifest",
]
