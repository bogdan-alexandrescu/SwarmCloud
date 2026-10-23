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
an optional subset of the target's own screen ids and an optional
`browser_tool` (`playwright` or `jev`, by name), and swarm-api renders
the workflow from the registration. Registering, changing and approving a
target are the console's (§1), not the plugin's.
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

#: A target's fields as `to_api` serves them, copied one by one. Its spec
#: (screens, flows, exclusions...) is the console's to show; a plugin reader
#: needs what it is, where it files, and whether it can run.
TARGET_FIELDS = (
    "id", "name", "base_url", "host", "host_state", "approved_by", "approved_at",
    "repository", "repo_id", "revision", "environment", "created_by", "updated_at",
)

#: `credential_status` in swarm_api/qatargets.py: never a value, a length or a part of one.
CREDENTIAL_FIELDS = ("set", "set_at", "set_by", "version")

#: What `:run` answers that a caller follows: the workflow, and the target's mark.
RUN_FIELDS = ("workflow_id", "file", "browser_steps")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _screens(doc: dict[str, Any]) -> list[str]:
    raw = doc.get("screens")
    if not isinstance(raw, list):
        return []
    return [s["id"] for s in raw if isinstance(s, dict) and isinstance(s.get("id"), str)]


def cannot_run_because(doc: dict[str, Any]) -> str | None:
    """Why a run of this target would be refused now, in the API's terms, or
    None. The API is still the gate: this is said before asking, not instead."""
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
                "admin sets it from swarm_qa_target_credential_link")
    return None


def target_view(doc: Any) -> dict[str, Any]:
    """One target, field by field: never its credential's value."""
    doc = doc if isinstance(doc, dict) else {}
    credential = doc.get("credential") if isinstance(doc.get("credential"), dict) else {}
    sign_in = doc.get("sign_in") if isinstance(doc.get("sign_in"), dict) else {}
    why = cannot_run_because(doc)
    view = {name: doc.get(name) for name in TARGET_FIELDS}
    view.update({
        "sign_in": "own_login" if sign_in.get("kind") == "own_login" else "none",
        "credential": {name: credential.get(name) for name in CREDENTIAL_FIELDS},
        "screens": _screens(doc),
        "can_run": why is None,
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


def qa_run(client: Any, target: Any, *, screens: Any = None, file: Any = "issues",
           browser_tool: Any = None) -> dict[str, Any]:
    """`POST /v1/qa-targets/{id}:run` with `file`, the optional screens
    subset and the optional `browser_tool`, and nothing else. A refusal
    (`qa_target_host_pending`, `issue_writes_off`, a member's missing role, a
    jev run without the workspace's TypeSafe key) is the API's, unchanged."""
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
    target_id = resolve_target(client, target)
    answer = client.request("POST", f"/v1/qa-targets/{target_id}:run", payload=payload)
    answer = answer if isinstance(answer, dict) else {}
    mark = answer.get("qa_target") if isinstance(answer.get("qa_target"), dict) else {}
    return {
        **{name: answer.get(name) for name in RUN_FIELDS},
        "qa_target": {"id": mark.get("id"), "revision": mark.get("revision")},
    }


def credential_link(client: Any, target: Any) -> dict[str, Any]:
    """`POST /v1/qa-targets/{id}/credential-links`: the console URL and its
    expiry, nothing else (§2.1), as `keys.key_set_link` answers.

    `url` is null when the deployment has no console origin configured; the
    `path` the API also returns is NOT handed out then, because it carries the
    link's token and a bare path is not something a person can open."""
    target_id = resolve_target(client, target)
    answer = client.request("POST", f"/v1/qa-targets/{target_id}/credential-links")
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
    """The list as a person reads it: one line a target, and why one cannot run."""
    rows = view.get("qa_targets") or []
    if not rows:
        return "no QA targets registered in this workspace (a workspace admin registers one " \
               "in the console's Repositories > QA targets)"
    lines = []
    for t in rows:
        credential = t.get("credential") or {}
        login = ""
        if t.get("sign_in") == "own_login":
            login = ", own login, credential " + ("set" if credential.get("set") else "not set")
        state = "can run" if t.get("can_run") else f"cannot run: {t.get('cannot_run_because')}"
        lines.append(f"{t.get('name')} ({t.get('id')}) {t.get('base_url')} -> "
                     f"{t.get('repository')}; host {t.get('host_state')}{login}; {state}")
    return "\n".join(lines)
