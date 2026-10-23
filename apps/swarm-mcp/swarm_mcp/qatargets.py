"""QA targets from the sc plugin (lane VQ-6 CONSOLE-PLUGIN, part of #1933).

docs/design/visual-qa-any-app.md §2.1, §6 and §7. The API is VQ-2's
(`swarm_api/routes/qatargets.py`), used exactly as it is:

    GET  /v1/qa-targets                          the workspace's targets   swarm_qa_targets
    POST /v1/qa-targets/{id}:run                 a member starts a run     swarm_qa_run
    POST /v1/qa-targets/{id}/credential-links    a one-time console link   swarm_qa_target_credential_link

A QA TARGET'S TEST CREDENTIAL NEVER PASSES THROUGH CLAUDE (§2.1): it is typed
into the console's form, which the link opens, and nowhere else. So no tool
here has a field a username or a password fits in, nothing here calls the
route that stores one, and every answer is copied FIELD BY FIELD from the
API's reply -- never forwarded whole -- so a reply that (wrongly) carried a
value would still not reach a transcript. `credential` is its status: set,
when, by whom, which version.

A run is started by NAMING a target (invariant 10): the caller sends `file`,
an optional subset of the target's own screen ids, an optional
`browser_tool` (`playwright` or `jev`, by name) and optionally the
`environments` to run, by entry name, and swarm-api renders the workflow from
the registration. Registering, changing and approving a target are the
console's (§1), not the plugin's.

ONE APP, SEVERAL ENVIRONMENTS (docs/design/qa-environments.md §1, §2.1, §7;
lane QAENV-SC, part of #2136). A target serves `environments[]`, each entry a
leg with its own origin, host approval, credential and challenge bypass. The
list names each entry -- its name, its mark, its host state, whether its
credential is set (yes or no) and why it cannot run -- and carries NO
credential field and no bypass value or spec: an entry is copied field by
field like a target, and the status objects stay behind. A run names its legs
(default: every entry, the API's), never a URL, a sign-in or a credential, and
answers the run group's id and each leg's workflow.
"""

from __future__ import annotations

import re
from typing import Any

from .client import SwarmError

#: `swarm_api.qatargets._TARGET_ID`: an id is matched exactly, anything else
#: is looked up as a name in the workspace's list.
TARGET_ID = re.compile(r"^qat_[0-9a-f]{16}$")

#: `RunBody.file` in routes/qatargets.py.
FILES = ("issues", "none")

#: `RunBody.browser_tool` in routes/qatargets.py (`qaspec.FLOW_BROWSER_TOOLS`,
#: lane VQ9-TOOL): the flows' tool for one run. BGD, 2026-10-23: Playwright by
#: default, Jev when a target or a run chooses it. Left out of the body when
#: the caller names none, so the target's `flows_browser_tool` decides.
BROWSER_TOOLS = ("playwright", "jev")

#: `RunBody.screens`' bound, so a longer list is refused before anything is sent.
MAX_SCREENS = 60

#: A target's app-wide fields as `to_api` serves them, copied one by one. Its
#: spec (screens, flows, exclusions...) is the console's to show; a plugin
#: reader needs what it is, where it files, and (per entry) whether it can run.
TARGET_FIELDS = (
    "id", "name", "repository", "repo_id", "revision", "created_by", "updated_at",
)

#: One entry's fields as `_entry_api` serves them, copied one by one (§7: its
#: host state beside its name and mark). Its `credential` and
#: `challenge_bypass_value` statuses and its `challenge_bypass` spec are NOT
#: copied: the credential is said as `credential_set`, yes or no, and nothing
#: of the bypass is said at all.
ENTRY_FIELDS = ("name", "environment", "base_url", "host", "host_state")

#: `RunBody.environments`' bound and `qaspec.ENVIRONMENT_NAME`, so a list the
#: API would refuse (a URL among it) is refused before anything is sent.
MAX_ENVIRONMENTS = 8
ENVIRONMENT_NAME = re.compile(r"^[a-z0-9-]{1,20}$")

#: What `:run` answers that a caller follows: the run group, and its one-leg
#: fields, which the API keeps beside the group for a run of one leg.
RUN_FIELDS = ("group_id", "workflow_id", "file", "browser_steps")

#: One leg of the answer's `legs`: its name, whether it was submitted, its
#: workflow, and why it was not when it was not.
LEG_FIELDS = ("environment", "state", "workflow_id", "browser_steps")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _screens(doc: dict[str, Any]) -> list[str]:
    raw = doc.get("screens")
    if not isinstance(raw, list):
        return []
    return [s["id"] for s in raw if isinstance(s, dict) and isinstance(s.get("id"), str)]


def cannot_run_because(doc: dict[str, Any]) -> str | None:
    """Why a run of this entry (or of a target of one origin) would be refused
    now, in the API's terms, or None. The API is still the gate: this is said
    before asking, not instead."""
    state = doc.get("host_state")
    host = _text(doc.get("host")) or "its host"
    if state == "pending":
        return (f"{host} is pending: a platform admin approves each host before any run "
                "starts against it (qa_target_host_pending)")
    if state == "revoked":
        return f"{host}'s approval was revoked; a platform admin approves it again"
    if state != "approved":
        return f"{host} is not approved"
    sign_in = doc.get("sign_in") if isinstance(doc.get("sign_in"), dict) else {}
    credential = doc.get("credential") if isinstance(doc.get("credential"), dict) else {}
    if sign_in.get("kind") == "own_login" and not credential.get("set"):
        return ("it signs in with its own login and no test credential is set; a workspace "
                "admin sets it from swarm_qa_target_credential_link, naming this environment")
    return None


def _entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The target's entries as the API serves them. A target stored before
    `environments[]` answers flat, its one leg's fields at the top: that is
    its one entry, named as §1.2's migration names it (`qaspec.
    legacy_environment_name`: `production` unless its mark says otherwise)."""
    raw = doc.get("environments")
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    mark = doc.get("environment") or "production"
    return [{**doc, "name": "production" if mark == "production" else "staging",
             "environment": mark}]


def entry_view(entry: dict[str, Any]) -> dict[str, Any]:
    """One entry, field by field: whether its credential is set, never the
    credential's status object, and nothing of its challenge bypass."""
    credential = entry.get("credential") if isinstance(entry.get("credential"), dict) else {}
    sign_in = entry.get("sign_in") if isinstance(entry.get("sign_in"), dict) else {}
    why = cannot_run_because(entry)
    view = {name: entry.get(name) for name in ENTRY_FIELDS}
    view.update({
        "sign_in": "own_login" if sign_in.get("kind") == "own_login" else "none",
        "credential_set": credential.get("set") is True,
        "can_run": why is None,
        "cannot_run_because": why,
    })
    return view


def target_view(doc: Any) -> dict[str, Any]:
    """One target, field by field, and its entries: never a credential's
    value or status object, nor a bypass's. It can run when any entry can:
    a run of the others is refused by the API leg by leg, before anything
    starts (§2.1), so the caller names the runnable ones."""
    doc = doc if isinstance(doc, dict) else {}
    entries = [entry_view(e) for e in _entries(doc)]
    view = {name: doc.get(name) for name in TARGET_FIELDS}
    runnable = [e for e in entries if e["can_run"]]
    if runnable:
        why = None
    elif entries:
        why = "no environment can run: " + "; ".join(
            f"{e['name']}: {e['cannot_run_because']}" for e in entries)
    else:
        why = "it has no environments"
    view.update({
        "environments": entries,
        "screens": _screens(doc),
        "can_run": not why,
        "cannot_run_because": why,
    })
    return view


def _repo_slug(value: Any) -> str:
    """`owner/repo`, lower-case, from a registration's URL or a caller's slug."""
    text = value.strip() if isinstance(value, str) else ""
    text = re.sub(r"^https://github\.com/", "", text, flags=re.IGNORECASE).rstrip("/")
    return text.removesuffix(".git").lower()


def _list(client: Any) -> list[dict[str, Any]]:
    # Spelled out, not a constant: test_qa_targets_tools reads the routes out of
    # this source and checks each against VQ-2's router.
    answer = client.request("GET", "/v1/qa-targets")
    raw = answer.get("qa_targets") if isinstance(answer, dict) else None
    return [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []


def qa_targets(client: Any, repository: Any = None) -> dict[str, Any]:
    """`GET /v1/qa-targets`, each target field by field, narrowed to one
    registered repository (`owner/repo` or its `repo_...` id) when given."""
    docs = _list(client)
    if repository is not None:
        if not isinstance(repository, str) or not repository.strip():
            raise SwarmError("repository must be owner/repo or a repo_ id; nothing was read")
        wanted = repository.strip()
        docs = [d for d in docs if d.get("repo_id") == wanted
                or _repo_slug(d.get("repository")) == _repo_slug(wanted)]
    return {"qa_targets": [target_view(d) for d in docs]}


def resolve_target(client: Any, target: Any) -> str:
    """The id of `target`, an id or a name in the selected workspace. An
    unknown one is refused WITHOUT repeating it: a credential pasted into the
    wrong argument must not be echoed into the transcript."""
    if not isinstance(target, str) or not target.strip():
        raise SwarmError("target must be a QA target's id (qat_...) or its name; nothing was sent")
    wanted = target.strip()
    if TARGET_ID.fullmatch(wanted):
        return wanted
    for doc in _list(client):
        if doc.get("name") == wanted and isinstance(doc.get("id"), str):
            return str(doc["id"])
    raise SwarmError("no QA target of the selected workspace has that name or id; "
                     "swarm_qa_targets lists them. Nothing was sent")


def _environment_names(environments: Any) -> list[str]:
    """`environments` as entry names, or a refusal that repeats none of it:
    a URL, a sign-in or a pasted credential is not a name, and is refused
    before the target is even resolved."""
    if (not isinstance(environments, list) or not 1 <= len(environments) <= MAX_ENVIRONMENTS
            or not all(isinstance(n, str) and ENVIRONMENT_NAME.fullmatch(n) for n in environments)
            or len(set(environments)) != len(environments)):
        raise SwarmError(f"environments must be 1 to {MAX_ENVIRONMENTS} distinct names of the "
                         "target's entries ([a-z0-9-], at most 20 characters), as swarm_qa_targets "
                         "lists them; nothing was sent")
    return list(environments)


def _leg(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    reason = raw.get("reason") if isinstance(raw.get("reason"), dict) else None
    return {
        **{name: raw.get(name) for name in LEG_FIELDS},
        "reason": {"code": reason.get("code"), "message": reason.get("message")} if reason else None,
    }


def qa_run(client: Any, target: Any, *, screens: Any = None, file: Any = "issues",
           browser_tool: Any = None, environments: Any = None) -> dict[str, Any]:
    """`POST /v1/qa-targets/{id}:run` with `file`, the optional screens
    subset, the optional `browser_tool` and the optional `environments` by
    name, and nothing else. Left out, `environments` is every entry (the
    API's default). A refusal (`qa_target_host_pending`, `issue_writes_off`,
    a member's missing role, a jev run without the workspace's TypeSafe key,
    an entry the target lacks) is the API's, unchanged.

    Answers the run group's id and each leg's workflow, field by field."""
    if file not in FILES:
        raise SwarmError(f"file must be one of {list(FILES)}; nothing was sent")
    payload: dict[str, Any] = {"file": file}
    if screens is not None:
        if not isinstance(screens, list) or not screens or len(screens) > MAX_SCREENS \
                or not all(isinstance(s, str) and s for s in screens):
            raise SwarmError(f"screens must be 1 to {MAX_SCREENS} of the target's screen ids; "
                             "nothing was sent")
        payload["screens"] = list(screens)
    if browser_tool is not None:
        if browser_tool not in BROWSER_TOOLS:
            raise SwarmError(f"browser_tool must be one of {list(BROWSER_TOOLS)}; nothing was sent")
        payload["browser_tool"] = browser_tool
    if environments is not None:
        payload["environments"] = _environment_names(environments)
    target_id = resolve_target(client, target)
    answer = client.request("POST", f"/v1/qa-targets/{target_id}:run", payload=payload)
    answer = answer if isinstance(answer, dict) else {}
    mark = answer.get("qa_target") if isinstance(answer.get("qa_target"), dict) else {}
    legs = answer.get("legs") if isinstance(answer.get("legs"), list) else []
    return {
        **{name: answer.get(name) for name in RUN_FIELDS},
        "qa_target": {"id": mark.get("id"), "revision": mark.get("revision")},
        "legs": [_leg(leg) for leg in legs],
    }


def credential_link(client: Any, target: Any, environment: Any = None) -> dict[str, Any]:
    """`POST /v1/qa-targets/{id}/credential-links`: the console URL and its
    expiry, nothing else (§2.1), as `keys.key_set_link` answers. The link is
    bound to one entry (qa-environments.md §1.4): `environment` names it, and
    a target of several entries needs it named.

    `url` is null when the deployment has no console origin configured; the
    `path` the API also returns is NOT handed out then, because it carries the
    link's token and a bare path is not something a person can open."""
    payload = None
    if environment is not None:
        payload = {"environment": _environment_names([environment])[0]}
    target_id = resolve_target(client, target)
    answer = client.request("POST", f"/v1/qa-targets/{target_id}/credential-links",
                            payload=payload)
    answer = answer if isinstance(answer, dict) else {}
    url = _text(answer.get("url"))
    reply: dict[str, Any] = {"url": url, "expires_at": answer.get("expires_at")}
    if url is None:
        reply["note"] = (
            "this deployment has no console address configured (SWARM_CONSOLE_URL on "
            "swarm-api), so the API could not make a link. A workspace admin sets the "
            "credential from the console's Repositories > QA targets page instead"
        )
    return reply


def targets_text(view: dict[str, Any]) -> str:
    """The list as a person reads it: one line a target, an indented line per
    entry, and why one cannot run."""
    rows = view.get("qa_targets") or []
    if not rows:
        return "no QA targets registered in this workspace (a workspace admin registers one " \
               "in the console's Repositories > QA targets)"
    lines = []
    for t in rows:
        lines.append(f"{t.get('name')} ({t.get('id')}) -> {t.get('repository')}")
        for e in t.get("environments") or []:
            login = ""
            if e.get("sign_in") == "own_login":
                login = ", own login, credential " + ("set" if e.get("credential_set") else "not set")
            state = "can run" if e.get("can_run") else f"cannot run: {e.get('cannot_run_because')}"
            lines.append(f"  {e.get('name')} [{e.get('environment')}] {e.get('base_url')}; "
                         f"host {e.get('host_state')}{login}; {state}")
        if not t.get("environments"):
            lines.append(f"  cannot run: {t.get('cannot_run_because')}")
    return "\n".join(lines)
