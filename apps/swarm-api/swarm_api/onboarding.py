"""The onboarding checklist, DERIVED from today's records (docs/onboarding.md
§2.1-§2.3; #780, lane OB1).

`GET /v1/onboarding` serves one resumable state machine, the same to the
console's checklist and the plugin's `/sc:setup`: nine steps in order, each
`todo`, `in_progress`, `done`, `failed` (with a §2.3 code and its recovery
copy, word for word) or `stale` (done once, its evidence older than the
re-verification interval). The next step is the first REQUIRED step that is
not `done`.

THE WORKSPACE STEPS (docs/workspaces.md §6.1; #847, lane W1). `workspace`
and `claude_account` follow `signed_in`, from the person's own workspace
record and the accounts that can serve their personal tenant. They carry
`required`: true only while WORKSPACE_GATE is on AND the caller's tenant is
the kind the gate judges (a person's own; never a group's or a service
account's, WD7). A step that is not required is served for information and
never holds back `ready`, `next_step` or `complete`, so with the gate off --
as it ships -- the checklist completes exactly as it did before.

DERIVED, NEVER SET. Every state is computed on each read from evidence that
already exists, so a step that was done and stopped being true shows as such
without anyone editing it. Phase 1 has no connection, org or grant documents
(§3.1's `forge_connections`, `forge_orgs`, `forge_grants` are OB3/OB4's), so
the evidence is today's:

  * the caller's identity and resolved tenant (`signed_in`);
  * the git token record the caller acts through -- their OWN user slot when
    they have one that is not revoked, else the tenant token -- and its
    probe (`github_connected`). Another member's user slot is never read
    into the answer: U1 lets the worker account read every member's slot,
    the checklist is the caller's alone;
  * for a connection through the GitHub App, whether the App is INSTALLED
    anywhere the person reaches (`app_installed`, #780, 2026-10-08).
    Authorising the App (Connect) and installing it are two acts at GitHub,
    and the owner did the first without the second and saw an empty Access
    page. The answer is the access service's (`AccessService.installations`):
    an owner the person enabled was installed when enabled, so that answers
    with no forge read; otherwise one owners read, which is one refresh of the
    person's token -- the read Work › Access makes on every visit. A
    connection that is a token, not the App, needs no installation;
  * that record's reach, read by the probe (OB0b, #794): the account login,
    `GET /user/orgs`, the orgs whose SAML SSO the token is not authorised
    for, the orgs that refuse a classic token, and how many orgs GitHub hid
    behind SSO (`orgs_enabled`);
  * the tenant's repository registrations, each `write` when its read found
    push and `read` otherwise -- today's stand-in for a grant
    (`repos_chosen`);
  * the token x repository checks the probe stored (`git_token_checks`):
    clone for every registration, push and pull request for a write one
    (`access_verified`).

What today's records cannot say is not guessed: `ORG_APPROVAL_PENDING`,
`FINE_GRAINED_PAT_PENDING`, `REPO_NOT_INSTALLED`, `AUTHORISATION_DENIED` and
`AUTHORISATION_EXPIRED` need the App's installation and authorisation
records (OB3, OB4), so no step derives them yet; their copy is served in
`COPY` all the same, so both surfaces print one set of words from the start.
A clone refused for no reason the evidence names is OB0b's
`ACCOUNT_CANNOT_SEE`, with `gittokens.refusal_cause`'s sentence.

READ-ONLY. Nothing here writes -- the one exception is the refresh above,
which `app_installed` causes only for an App connection with no enabled
installed owner, and which stores the person's own renewed token exactly as
every Access read does. Not the §3.1 `onboarding/` cache (a later
lane's, when there are client-set fields such as `dismissed_at` to keep),
and not the tenant default's record, which `GET /v1/git-tokens` creates
lazily -- a tenant that lists the slot but has no record yet is answered
from a record built in memory. No value is read either: the records hold
none, and nothing here asks Secret Manager. Every read is filtered on the
caller's tenant, in the query and again here (invariant 9).

A READ THAT DID NOT COME BACK IS NOT AN ANSWER (§2.2, git-tokens.md §5.3):
a 5xx, a 429 or a network error leaves the step as it was and says
`FORGE_UNREACHABLE`; it never fails one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from swarm_common.models import Tenant

from .forge import GIT_PROVIDER
from .gittokens import (
    APP_UNKNOWN,
    FINE_GRAINED_UNKNOWN,
    MISSING,
    OK,
    REVERIFY_EVERY,
    REVERIFY_RETRY,
    SSO_SETTINGS_URL,
    SYSTEM,
    GitTokenRecord,
    GitTokens,
    Scope,
    TokenState,
    provider_suffix,
    record_for_slot,
    refusal_cause,
    secret_name_for,
    store_command,
)
from .repositories import Repositories, repo_id_for
from .store import Store
from . import workspaces as ws

log = logging.getLogger(__name__)

__all__ = [
    "COPY", "STATES", "STEPS", "STALE_AFTER", "Caller", "derive", "read", "recovery_copy",
    "repo_id_for",
]

# --------------------------------------------------------------------------
# the vocabulary (§2.1)
# --------------------------------------------------------------------------

SIGNED_IN = "signed_in"
WORKSPACE = "workspace"
CLAUDE_ACCOUNT = "claude_account"
GITHUB_CONNECTED = "github_connected"
APP_INSTALLED = "app_installed"
ORGS_ENABLED = "orgs_enabled"
REPOS_CHOSEN = "repos_chosen"
ACCESS_VERIFIED = "access_verified"
READY = "ready"
STEPS = (SIGNED_IN, WORKSPACE, CLAUDE_ACCOUNT, GITHUB_CONNECTED, APP_INSTALLED,
         ORGS_ENABLED, REPOS_CHOSEN,
         ACCESS_VERIFIED, READY)

NOT_STARTED = "todo"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"
STALE = "stale"
STATES = (NOT_STARTED, IN_PROGRESS, DONE, FAILED, STALE)

#: When `done` becomes `stale`. The daily pass (`GitTokens.reverify`, inside
#: the five-minute repository poll) re-probes a pair once it is a day old,
#: and retries one it could not probe after an hour; evidence older than
#: both together means the passes have missed it, not that one is due.
STALE_AFTER = REVERIFY_EVERY + REVERIFY_RETRY

#: Registrations one read considers. The checklist needs one to say
#: `repos_chosen` is done; past this the answer says `capped`.
MAX_REGISTRATIONS = 500

#: §2.3's recovery copy, word for word -- the console and the plugin print
#: these exact sentences. `tests/unit/control_plane/test_onboarding_state.py`
#: reads the table in docs/onboarding.md and fails if either side drifts.
COPY: dict[str, str] = {
    "SSO_NOT_AUTHORISED": (
        "{owner} uses SAML single sign-on and GitHub has not linked your session to it "
        "yet. Open {url}, sign in with {owner}'s identity provider, then press Re-check. "
        "Nothing in SwarmCloud has to change."),
    "CLASSIC_PAT_BLOCKED": (
        "{owner} does not accept classic personal access tokens. Connect with the "
        "SwarmCloud GitHub App instead (recommended), or create a fine-grained token whose "
        "resource owner is {owner} and store it with `uv run sc setup token --owner "
        "{owner}`."),
    "ORG_APPROVAL_PENDING": (
        "You asked {owner}'s owners to install SwarmCloud. Nothing can be read in {owner} "
        "until one of them approves it in {owner}'s settings › GitHub Apps. You can carry "
        "on with your other orgs; this step re-checks on its own every 15 minutes, or press "
        "Re-check."),
    "FINE_GRAINED_PAT_PENDING": (
        "Your fine-grained token for {owner} is waiting for an org owner's approval. Until "
        "it is approved GitHub shows it only public repositories. Ask an owner of {owner} "
        "to approve it under Settings › Personal access tokens › Pending requests."),
    "REPO_NOT_INSTALLED": (
        "SwarmCloud is installed on {owner} but not for {repo}. Add {repo} to the "
        "installation at {url} (an org owner may have to), then press Re-check. SwarmCloud "
        "never reaches a repository the installation leaves out."),
    "AUTHORISATION_DENIED": (
        "GitHub says the authorisation was cancelled, so SwarmCloud has no access. Press "
        "Connect GitHub to start again; nothing was stored."),
    "AUTHORISATION_EXPIRED": (
        "That sign-in link has expired or was already used. Press Connect GitHub for a "
        "fresh one; links last 10 minutes and work once."),
    "REFRESH_FAILED": (
        "SwarmCloud's access as {login} has ended at GitHub, so tasks you submit will wait "
        "instead of running. Press Reconnect to authorise again; your orgs and repositories "
        "are kept."),
    "PERMISSION_MISSING": (
        "You chose write for {repo}, but GitHub does not let {login} push there through "
        "SwarmCloud. Ask for write access to {repo}, or change the grant to read."),
    "REPO_ARCHIVED": (
        "{repo} is archived on GitHub, so nothing can be pushed to it. Unarchive it, or "
        "grant it read only."),
    "FORGE_UNREACHABLE": (
        "GitHub did not answer this check, so its result is unknown, not failed. It is "
        "retried on the next pass; press Re-check to try now."),
}

FORGE_UNREACHABLE = "FORGE_UNREACHABLE"

#: The probe's own words for a read that did not come back
#: (`gittokens._Forge.get`): a 429, a 5xx, a spent rate limit, a transport
#: error. "Not probed: out of time" is not among them -- nothing was asked.
_UNANSWERED = re.compile(r"GitHub answered HTTP (?:429|5\d\d)|the read failed \(")

#: The pull-request row GitHub does not expose for these kinds: the role
#: allows it, so it passes on the push it needs (§2.2, git-tokens.md §5.2).
_BY_DESIGN = frozenset({FINE_GRAINED_UNKNOWN, APP_UNKNOWN})

#: The token kind a GitHub App connection stores (`forgeapp.METHOD_APP_USER`;
#: forgeapp imports this module, so the word is restated, not imported).
APP_USER = "app_user"

#: Kinds whose SSO is authorised on the token's own settings page.
_PAT_KINDS = frozenset({"classic_pat", "fine_grained_pat"})


def recovery_copy(code: str, *, owner: str = "", repo: str = "", url: str = "",
                  login: str = "") -> str:
    """§2.3's sentence for `code`, filled. `url` is a GitHub page, never one
    that carries a token."""
    return COPY[code].format(owner=owner, repo=repo, url=url, login=login)


@dataclass(frozen=True)
class Caller:
    """Who the checklist is for: the VERIFIED caller and the tenant
    `tenant_scope` resolved, never anything a request names."""

    email: str
    tenant_id: str
    is_admin: bool = False

    @property
    def key(self) -> str:
        return self.email.strip().lower()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _unanswered(*texts: Any) -> bool:
    return any(isinstance(t, str) and _UNANSWERED.search(t) for t in texts)


def _older(at: Any, now: datetime) -> bool:
    return not isinstance(at, datetime) or now - at > STALE_AFTER


def _lower(values: Iterable[str]) -> set[str]:
    return {v.lower() for v in values}


def _user_suffix(caller: Caller) -> str:
    return provider_suffix(Scope.USER, user=caller.key)


def _step(name: str, state: str, *, evidence: dict[str, Any], issues: list[dict] = (),
          code: str | None = None, copy: str | None = None,
          checked_at: Any = None) -> dict[str, Any]:
    issues = list(issues)
    if issues and code is None:
        code, copy = issues[0]["code"], issues[0]["copy"]
    return {"step": name, "state": state, "code": code, "copy": copy,
            "checked_at": _iso(checked_at), "evidence": evidence, "issues": issues}


def _waiting(name: str, on: str) -> dict[str, Any]:
    return _step(name, NOT_STARTED, evidence={"waiting_for": on})


# --------------------------------------------------------------------------
# workspace and claude_account (docs/workspaces.md §6.1)
# --------------------------------------------------------------------------

#: The record's state, as a checklist state. `ready` is the only `done`, and
#: only the workspace job writes it.
_WORKSPACE_STATES = {
    ws.NONE: NOT_STARTED,
    ws.REQUESTED: IN_PROGRESS,
    ws.APPROVED: IN_PROGRESS,
    ws.APPLYING: IN_PROGRESS,
    ws.NEEDS_OWNER: IN_PROGRESS,
    ws.DENIED: FAILED,
    ws.FAILED: FAILED,
    ws.READY: DONE,
}


def _workspace_step(record: dict[str, Any] | None, *, required: bool,
                    console_url: str) -> dict[str, Any]:
    shown = ws.view(record, console_url=console_url)
    state = _WORKSPACE_STATES[shown["state"]]
    evidence = {key: shown.get(key) for key in (
        "state", "workspace_id", "requested_at", "decision", "steps", "failure", "ready_at",
        "request_again_at", "setup_url", "setup_command")}
    code = copy = None
    if shown["state"] == ws.FAILED:
        code, copy = (shown["failure"] or {}).get("code"), (shown["failure"] or {}).get("copy")
    elif shown["state"] == ws.DENIED:
        reason = ((shown.get("decision") or {}).get("reason") or "").strip()
        copy = f"Not approved: {reason}" if reason else "Not approved."
    step = _step(WORKSPACE, state, evidence=evidence, code=code, copy=copy,
                 checked_at=(record or {}).get("ready_at"))
    step["required"] = required
    return step


def _claude_step(accounts: dict[str, Any], loan: dict[str, Any] | None, *, required: bool,
                 console_url: str) -> dict[str, Any]:
    """Ticked by §5.1 (3)'s rule, the gate's own: an account the tenant owns,
    one lent to it, or a provider key."""
    loan_state = (loan or {}).get("state")
    evidence = {
        "own": accounts.get("own", 0),
        "lent": accounts.get("lent", 0),
        "provider_key": bool(accounts.get("provider_key")),
        "loan_request": loan_state,
        "setup_url": ws.setup_url(console_url, "claude-account"),
        "setup_command": ws.SETUP_COMMAND,
    }
    if accounts.get("has_account"):
        state = DONE
    elif loan_state == ws.REQUESTED:
        state = IN_PROGRESS
    else:
        state = NOT_STARTED
    step = _step(CLAUDE_ACCOUNT, state, evidence=evidence)
    step["required"] = required
    return step


# --------------------------------------------------------------------------
# github_connected
# --------------------------------------------------------------------------

def _connection(caller: Caller, records: list[GitTokenRecord], tenant_lists_git: bool,
                now: datetime) -> tuple[GitTokenRecord | None, str | None]:
    """The record the caller acts through, and which scope it is: their own
    user slot first, then the tenant token. None when neither is usable."""
    mine = [r for r in records if r.tenant_id == caller.tenant_id and r.scope is Scope.USER
            and (r.user or "").lower() == caller.key and r.state is not TokenState.REVOKED]
    if mine:
        # The person's own slot before a token they keep for one owner (D5):
        # that is a second user record, and never their connection while the
        # first exists.
        mine.sort(key=lambda r: r.provider_suffix != _user_suffix(caller))
        return mine[0], "user"
    tenant = [r for r in records if r.tenant_id == caller.tenant_id
              and r.scope is Scope.TENANT]
    if tenant:
        record = tenant[0]
        return (None, None) if record.state is TokenState.REVOKED else (record, "tenant")
    if tenant_lists_git:
        # The slot exists; its record is created by the first `GET
        # /v1/git-tokens`. Built in memory here, so this read writes nothing.
        return record_for_slot(caller.tenant_id, Scope.TENANT, registered_by=SYSTEM,
                               now=now), "tenant"
    return None, None


def _fresh_at(record: GitTokenRecord) -> datetime | None:
    if record.verified_at is not None:
        return record.verified_at
    return record.probe_attempted_at if record.probe_complete else None


def _connected_step(caller: Caller, record: GitTokenRecord | None, via: str | None,
                    now: datetime) -> dict[str, Any]:
    suffix = _user_suffix(caller)
    if record is None:
        return _step(GITHUB_CONNECTED, NOT_STARTED, evidence={
            "via": None,
            # Today's route to a token of your own: the stdin store, then
            # `POST /v1/git-tokens` {"scope": "user"}. The App's Connect
            # GitHub is lane OB3's.
            "secret_name": secret_name_for(caller.tenant_id, suffix),
            "store_command": store_command(caller.tenant_id, suffix),
        })
    evidence = {
        "via": via,
        "token_id": record.token_id,
        "secret_name": record.secret_name,
        "kind": record.kind,
        "forge_login": record.forge_login,
        "token_state": record.state.value,
        "verified_at": _iso(record.verified_at),
        "probe_attempted_at": _iso(record.probe_attempted_at),
        "probe_complete": record.probe_complete,
        "probe_error": record.probe_error,
        "expires_at": _iso(record.expires_at),
    }
    checked = record.probe_attempted_at
    expired = record.state is TokenState.EXPIRED or (
        record.expires_at is not None and record.expires_at <= now)
    # A complete probe that did not make the record active: GitHub answered
    # 401 to `GET /user` (`GitTokens._store_probe`).
    refused = record.probe_complete is True and record.state is TokenState.UNVERIFIED
    if expired or refused:
        login = record.forge_login or "the stored token"
        return _step(GITHUB_CONNECTED, FAILED, evidence=evidence, checked_at=checked,
                     code="REFRESH_FAILED", copy=recovery_copy("REFRESH_FAILED", login=login))
    unanswered = record.probe_complete is not True and _unanswered(record.probe_error)
    code = FORGE_UNREACHABLE if unanswered else None
    copy = COPY[FORGE_UNREACHABLE] if unanswered else None
    if record.state is TokenState.ACTIVE:
        state = STALE if _older(_fresh_at(record), now) else DONE
    else:
        state = IN_PROGRESS
    return _step(GITHUB_CONNECTED, state, evidence=evidence, checked_at=checked,
                 code=code, copy=copy)


# --------------------------------------------------------------------------
# app_installed
# --------------------------------------------------------------------------

def uses_app(record: GitTokenRecord | None, via: str | None) -> bool:
    """Whether the caller acts through their own GitHub App connection, the
    one kind of connection that needs the App installed to reach anything."""
    return record is not None and via == "user" and record.kind == APP_USER


def _installed_step(record: GitTokenRecord, via: str,
                    installations: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Done when the App is installed on at least one owner the person
    reaches. `installations` is `AccessService.installations`'s answer, or
    None when nothing could ask."""
    if not uses_app(record, via):
        # A token (the tenant's, or a PAT of one's own) reaches what its
        # owner reaches; no installation is involved.
        return _step(APP_INSTALLED, DONE, checked_at=now,
                     evidence={"needed": False, "via": via, "kind": record.kind})
    found = installations or {}
    evidence = {
        "needed": True,
        "read": bool(found.get("read")),
        "source": found.get("source"),
        "login": record.forge_login,
        "installed": list(found.get("installed") or []),
        "not_installed": list(found.get("not_installed") or []),
        "install_url": found.get("install_url"),
    }
    if not found.get("read"):
        unreachable = bool(found.get("unreachable"))
        return _step(APP_INSTALLED, IN_PROGRESS, evidence=evidence, checked_at=now,
                     code=FORGE_UNREACHABLE if unreachable else None,
                     copy=COPY[FORGE_UNREACHABLE] if unreachable else None)
    if evidence["installed"]:
        return _step(APP_INSTALLED, DONE, evidence=evidence, checked_at=now)
    # Connected, authorised, installed nowhere: the next thing to do, not a
    # failure. The console and the plugin offer the install page.
    return _step(APP_INSTALLED, NOT_STARTED, evidence=evidence, checked_at=now)


# --------------------------------------------------------------------------
# orgs_enabled
# --------------------------------------------------------------------------

def _sso_url(record: GitTokenRecord, owner: str) -> str:
    if record.kind in _PAT_KINDS or record.kind is None:
        return SSO_SETTINGS_URL
    return f"https://github.com/orgs/{owner}/sso"


def _owner_issue(caller: Caller, record: GitTokenRecord, owner: str,
                 repository: str | None = None) -> dict[str, Any] | None:
    if owner.lower() in _lower(record.sso_required_orgs):
        url = _sso_url(record, owner)
        issue = {"code": "SSO_NOT_AUTHORISED", "owner": owner, "url": url,
                 "copy": recovery_copy("SSO_NOT_AUTHORISED", owner=owner, url=url)}
    elif owner.lower() in _lower(record.classic_blocked_orgs):
        suffix = _user_suffix(caller)
        issue = {"code": "CLASSIC_PAT_BLOCKED", "owner": owner,
                 "copy": recovery_copy("CLASSIC_PAT_BLOCKED", owner=owner),
                 # What stores a fine-grained token today, by stdin, until
                 # `sc setup token` (lane OB9) exists.
                 "store_command": store_command(caller.tenant_id, suffix)}
    else:
        return None
    if repository is not None:
        issue["repository"] = repository
    return issue


def _orgs_step(caller: Caller, record: GitTokenRecord, via: str,
               regs: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    owners: dict[str, dict[str, Any]] = {}

    def add(login: str | None, owner_type: str, source: str) -> None:
        if login and login.lower() not in owners:
            owners[login.lower()] = {"owner": login, "owner_type": owner_type,
                                     "source": source}

    add(record.forge_login, "User", "account")
    for org in record.orgs or []:
        add(org, "Organization", "orgs")
    for org in [*record.sso_required_orgs, *record.classic_blocked_orgs]:
        add(org, "Organization", "refusal")
    if via == "tenant":
        # A registration was read with the tenant token: its owner was reached.
        for reg in regs:
            if (reg.get("access") or {}).get("can_read"):
                add(reg.get("owner"), "Organization", "registration")
    issues = []
    for key, entry in owners.items():
        issue = _owner_issue(caller, record, entry["owner"])
        entry["reach"] = ({"SSO_NOT_AUTHORISED": "sso_required",
                           "CLASSIC_PAT_BLOCKED": "classic_blocked"}[issue["code"]]
                          if issue else "reachable")
        entry["registered"] = sum(1 for reg in regs if (reg.get("owner") or "").lower() == key)
        if issue:
            issues.append(issue)
    evidence = {
        "owners": list(owners.values()),
        "orgs_read": record.orgs is not None,
        "orgs_capped": record.orgs_capped,
        "sso_hidden_orgs": len(record.sso_partial_org_ids),
        "read_at": _iso(record.access_evidence_at),
    }
    checked = record.access_evidence_at
    if issues:
        return _step(ORGS_ENABLED, FAILED, evidence=evidence, issues=issues, checked_at=checked)
    if any(entry["reach"] == "reachable" for entry in owners.values()):
        at = record.access_evidence_at or _fresh_at(record)
        return _step(ORGS_ENABLED, STALE if _older(at, now) else DONE, evidence=evidence,
                     checked_at=checked)
    unanswered = _unanswered(record.probe_error)
    return _step(ORGS_ENABLED, IN_PROGRESS, evidence=evidence, checked_at=checked,
                 code=FORGE_UNREACHABLE if unanswered else None,
                 copy=COPY[FORGE_UNREACHABLE] if unanswered else None)


# --------------------------------------------------------------------------
# repos_chosen
# --------------------------------------------------------------------------

def _repository(reg: dict[str, Any]) -> str:
    return f"{reg.get('owner')}/{reg.get('repo')}"


def _mode(reg: dict[str, Any]) -> str:
    """Today's stand-in for a grant: write when the registration's read found push."""
    return "write" if (reg.get("access") or {}).get("can_push") else "read"


def _repos_step(caller: Caller, record: GitTokenRecord, regs: list[dict[str, Any]],
                capped: bool, now: datetime) -> dict[str, Any]:
    rows = [{"repository": _repository(reg), "repo_id": reg.get("repo_id"),
             "owner": reg.get("owner"), "mode": _mode(reg), "archived": bool(reg.get("archived"))}
            for reg in regs]
    evidence = {"repositories": rows, "capped": capped}
    if not rows:
        return _step(REPOS_CHOSEN, NOT_STARTED, evidence=evidence, checked_at=now)
    issues = []
    for row in rows:
        if row["archived"] and row["mode"] == "write":
            issues.append({"code": "REPO_ARCHIVED", "owner": row["owner"],
                           "repository": row["repository"],
                           "copy": recovery_copy("REPO_ARCHIVED", repo=row["repository"])})
        owner_issue = _owner_issue(caller, record, row["owner"] or "", row["repository"])
        if owner_issue:
            issues.append(owner_issue)
    return _step(REPOS_CHOSEN, FAILED if issues else DONE, evidence=evidence, issues=issues,
                 checked_at=now)


# --------------------------------------------------------------------------
# access_verified
# --------------------------------------------------------------------------

def _clone_issue(record: GitTokenRecord, reg: dict[str, Any]) -> dict[str, Any]:
    owner, repo = reg.get("owner") or "", reg.get("repo") or ""
    cause = refusal_cause(owner, repo, record)
    issue = {"code": cause["code"], "owner": owner, "repository": _repository(reg)}
    if cause["code"] == "SSO_NOT_AUTHORISED":
        issue["url"] = _sso_url(record, owner)
        issue["copy"] = recovery_copy("SSO_NOT_AUTHORISED", owner=owner, url=issue["url"])
    elif cause["code"] in COPY:
        issue["copy"] = recovery_copy(cause["code"], owner=owner, repo=_repository(reg))
    else:
        # OB0b's ACCOUNT_CANNOT_SEE: its own sentence, from the same evidence.
        issue["copy"] = cause["message"]
    return issue


def _verified_step(record: GitTokenRecord, regs: list[dict[str, Any]],
                   pair_docs: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    pairs = {doc.get("repo_id"): doc for doc in pair_docs
             if doc.get("tenant_id") == record.tenant_id
             and doc.get("token_id") == record.token_id}
    login = record.forge_login or "the stored token"
    rows, issues = [], []
    pending = unanswered = stale = False
    checked: datetime | None = None
    for reg in regs:
        mode = _mode(reg)
        needed = ("clone", "push", "open_pull_requests") if mode == "write" else ("clone",)
        doc = pairs.get(reg.get("repo_id"))
        caps = (doc or {}).get("capabilities") or {}
        row_checks: dict[str, str] = {}
        result = "passed"
        for cap in needed:
            cell = caps.get(cap) if isinstance(caps.get(cap), dict) else {}
            state = cell.get("state") or "unknown"
            row_checks[cap] = state
            if state == OK or (cap == "open_pull_requests" and cell.get("reason") in _BY_DESIGN):
                continue
            if state == MISSING:
                if result != "failed":
                    issues.append(_clone_issue(record, reg) if cap == "clone" else {
                        "code": "PERMISSION_MISSING", "owner": reg.get("owner"),
                        "repository": _repository(reg),
                        "copy": recovery_copy("PERMISSION_MISSING", repo=_repository(reg),
                                              login=login)})
                result = "failed"
            elif result == "passed":
                result = "pending"
                if _unanswered((doc or {}).get("error"), cell.get("reason")):
                    unanswered = True
        at = (doc or {}).get("verified_at")
        if isinstance((doc or {}).get("attempted_at"), datetime):
            attempted = doc["attempted_at"]
            checked = attempted if checked is None or attempted > checked else checked
        if result == "pending":
            pending = True
        elif result == "passed" and _older(at, now):
            stale = True
        rows.append({"repository": _repository(reg), "repo_id": reg.get("repo_id"),
                     "mode": mode, "checks": row_checks, "result": result,
                     "verified_at": _iso(at)})
    evidence = {"token_id": record.token_id, "repositories": rows}
    if issues:
        return _step(ACCESS_VERIFIED, FAILED, evidence=evidence, issues=issues,
                     checked_at=checked)
    if pending:
        return _step(ACCESS_VERIFIED, IN_PROGRESS, evidence=evidence, checked_at=checked,
                     code=FORGE_UNREACHABLE if unanswered else None,
                     copy=COPY[FORGE_UNREACHABLE] if unanswered else None)
    return _step(ACCESS_VERIFIED, STALE if stale else DONE, evidence=evidence,
                 checked_at=checked)


# --------------------------------------------------------------------------
# the derivation
# --------------------------------------------------------------------------

def derive(
    caller: Caller,
    *,
    records: list[GitTokenRecord],
    pair_docs: list[dict[str, Any]],
    registrations: list[dict[str, Any]],
    tenant_lists_git: bool,
    now: datetime,
    registrations_capped: bool = False,
    installations: dict[str, Any] | None = None,
    workspace: dict[str, Any] | None = None,
    accounts: dict[str, Any] | None = None,
    loan: dict[str, Any] | None = None,
    workspace_required: bool = False,
    console_url: str = "",
) -> dict[str, Any]:
    """The caller's checklist from evidence. Pure: no read, no write, no clock.
    `installations` is `AccessService.installations`'s answer for an App
    connection, or None when it was not asked.

    Every input is filtered on `caller.tenant_id` again here, whatever the
    reads returned, and only the caller's own user slot is ever considered.
    The workspace record and loan request are the caller's PERSONAL tenant's
    (`tenant_id_for_user` of their email), whatever tenant they act in, and
    are dropped here if they name any other."""
    personal = ws.personal_tenant_id(caller.email)
    if workspace is not None and workspace.get("tenant_id") != personal:
        workspace = None
    if loan is not None and loan.get("tenant_id") != personal:
        loan = None
    records = [r for r in records if r.tenant_id == caller.tenant_id]
    regs = sorted((r for r in registrations if r.get("tenant_id") == caller.tenant_id),
                  key=lambda r: _repository(r).lower())
    pair_docs = [d for d in pair_docs if d.get("tenant_id") == caller.tenant_id]
    suffix = _user_suffix(caller)

    steps = [_step(SIGNED_IN, DONE, checked_at=now, evidence={
        "email": caller.email, "tenant_id": caller.tenant_id, "is_admin": caller.is_admin})]
    steps.append(_workspace_step(workspace, required=workspace_required,
                                 console_url=console_url))
    steps.append(_claude_step(accounts or {}, loan, required=workspace_required,
                              console_url=console_url))
    record, via = _connection(caller, records, tenant_lists_git, now)
    connected = _connected_step(caller, record, via, now)
    steps.append(connected)
    if record is None or via is None or connected["state"] not in (DONE, STALE):
        steps += [_waiting(name, GITHUB_CONNECTED)
                  for name in (APP_INSTALLED, ORGS_ENABLED, REPOS_CHOSEN, ACCESS_VERIFIED)]
    elif (installed := _installed_step(record, via, installations, now))["state"] == NOT_STARTED:
        # Installed nowhere: an App connection reaches no org and no
        # repository until it is, so the rest waits for the install.
        steps.append(installed)
        steps += [_waiting(name, APP_INSTALLED)
                  for name in (ORGS_ENABLED, REPOS_CHOSEN, ACCESS_VERIFIED)]
    else:
        steps.append(installed)
        steps.append(_orgs_step(caller, record, via, regs, now))
        chosen = _repos_step(caller, record, regs, registrations_capped, now)
        steps.append(chosen)
        steps.append(_waiting(ACCESS_VERIFIED, REPOS_CHOSEN) if chosen["state"] == NOT_STARTED
                     else _verified_step(record, regs, pair_docs, now))
    def holds(s: dict[str, Any]) -> bool:
        return s["state"] != DONE and s.get("required", True)

    before = next((s["step"] for s in steps if holds(s)), None)
    if before is None:
        passed = [row["repository"] for row in steps[-1]["evidence"]["repositories"]
                  if row["result"] == "passed"]
        steps.append(_step(READY, DONE, checked_at=now,
                           evidence={"first_repository": passed[0] if passed else None}))
    else:
        steps.append(_waiting(READY, before))
    next_step = next((s["step"] for s in steps if holds(s)), None)
    return {
        "tenant_id": caller.tenant_id,
        "user": caller.email,
        # §3.1's user_hash: the 16 hex `provider_suffix` names the user slot by.
        "user_hash": suffix.rsplit("-", 1)[-1],
        "derived_at": _iso(now),
        "steps": steps,
        "next_step": next_step,
        "complete": next_step is None,
        "source": ("derived on this read from the git token record, its probe, the App's "
                   "installations, the tenant's registrations and the stored checks; "
                   "nothing is stored by it but an App connection's own token refresh"),
    }


def read(db: Any, caller: Caller, *, tenant: Tenant | None, now: datetime,
         installations: Callable[[], dict[str, Any]] | None = None,
         workspaces: "ws.Workspaces | None" = None,
         workspace_required: bool = False) -> dict[str, Any]:
    """Read today's records for the caller's tenant and derive. Reads only,
    but for `installations`: called only for an active App connection, it is
    `AccessService.installations` for this caller (see the module note).

    `workspaces` reads the caller's personal workspace, its loan request and
    the accounts that serve it; without it the two steps derive from nothing
    (`todo`). `workspace_required` is the route's: the gate is on and judges
    this caller's tenant."""
    tokens = GitTokens(db, now=lambda: now)
    records = tokens.list(caller.tenant_id)
    pair_docs = tokens.pair_docs(caller.tenant_id)
    regs, more = Repositories(db, now=lambda: now).list(caller.tenant_id,
                                                         limit=MAX_REGISTRATIONS)
    workspace = loan = None
    accounts: dict[str, Any] = {}
    if workspaces is not None:
        personal = ws.personal_tenant_id(caller.email)
        workspace = workspaces.get(personal)
        loan = workspaces.loan_request(personal)
        own = tenant if tenant is not None and tenant.tenant_id == personal \
            else Store(db).get_tenant(personal)
        accounts = workspaces.claude_accounts(personal, own)
    lists_git = tenant is not None and GIT_PROVIDER in (tenant.credentials or [])
    record, via = _connection(caller, [r for r in records if r.tenant_id == caller.tenant_id],
                              lists_git, now)
    found = None
    if installations is not None and uses_app(record, via) \
            and record.state is TokenState.ACTIVE:
        found = installations()
    view = derive(
        caller,
        records=records,
        pair_docs=pair_docs,
        registrations=regs,
        tenant_lists_git=lists_git,
        now=now,
        registrations_capped=more is not None,
        installations=found,
        workspace=workspace,
        accounts=accounts,
        loan=loan,
        workspace_required=workspace_required,
        console_url=workspaces.console_url if workspaces is not None else "",
    )
    log.info("onboarding read tenant=%s user_hash=%s next=%s", caller.tenant_id,
             view["user_hash"], view["next_step"])
    return view

