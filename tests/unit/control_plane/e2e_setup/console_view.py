"""The console's view of Admin › Setup's answers, as `apps/swarm-ui/src/api.ts`
reads them (its "client of swarm-api's Admin › Setup routes" section, and
`SetupLink.tsx`'s `openedFrom` for the link page).

Two things live here, both restating api.ts so the walk reads each answer
the way the page does:

* `norm_setup_state` and the other `norm_*` functions port api.ts's
  normalisers line for line (`normSetupState`, `normEngine`, `normTeam`, ...).
  A field swarm-api renames reads as api.ts's default (null, '' or []), so an
  assertion on the normalised view fails where the page would silently go
  blank.
* `FIELDS_READ` lists, per route, every field path api.ts reads from the raw
  answer (its primary name, not a fallback kept for an older document). The
  walk records every answer and `unserved` names each listed path no answer
  of the walk ever carried: a field the UI reads that the API never serves.

`[]` in a path means "each element of this list".
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

# --------------------------------------------------------------------------
# api.ts's helpers: objOf, strOf, numOf, timeOf, labelsOf
# --------------------------------------------------------------------------


def obj_of(v: Any) -> dict[str, Any] | None:
    return v if isinstance(v, dict) else None


def str_of(v: Any) -> str | None:
    if isinstance(v, str):
        return v or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _parse_iso(v: str) -> float | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def num_of(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return v
    if isinstance(v, str) and v:
        return _parse_iso(v)
    return None


def time_of(v: Any) -> str | None:
    """api.ts `timeOf`: epoch seconds or ISO text, else null."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return datetime.fromtimestamp(v).isoformat() if math.isfinite(v) else None
    if isinstance(v, str) and v.strip().replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(v)).isoformat()
    if not isinstance(v, str) or not v:
        return None
    return v if _parse_iso(v) is not None else None


def labels_of(v: Any) -> list[str]:
    return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []


# --------------------------------------------------------------------------
# the normalisers
# --------------------------------------------------------------------------

#: api.ts `BEFORE_PASS_3`.
BEFORE_PASS_3 = frozenset({"app_id_not_configured", "app_slug_not_configured"})


def norm_self_check(raw: Any) -> dict[str, Any] | None:
    c = obj_of(raw)
    if c is None:
        return None
    ok = c.get("ok") is True
    reason = str_of(c.get("reason"))
    if ok:
        state = "passed"
    elif c.get("state") == "waiting_for_infra_pass_3" or reason in BEFORE_PASS_3:
        state = "waiting_for_infra_pass_3"
    else:
        state = "failed"
    return {"ok": ok, "reason": reason, "at": str_of(c.get("at")), "state": state}


def norm_verify(raw: Any) -> dict[str, Any] | None:
    v = obj_of(raw)
    if v is None:
        return None
    return {"check": str_of(v.get("check")), "done": num_of(v.get("done")) or 0,
            "total": num_of(v.get("total")) or 0, "started": num_of(v.get("started")),
            "expected_end": num_of(v.get("expected_end")),
            "deadline": num_of(v.get("deadline")), "result": str_of(v.get("result"))}


def norm_engine(raw: Any) -> dict[str, Any] | None:
    e = obj_of(raw)
    if e is None:
        return None
    stages = []
    for s in e.get("stages") if isinstance(e.get("stages"), list) else []:
        o = obj_of(s)
        stages.append(None if o is None or str_of(o.get("state")) is None else
                      {"state": str_of(o.get("state")), "at": time_of(o.get("at")),
                       "by": str_of(o.get("by"))})
    lease = obj_of(e.get("lease"))
    team_group = str_of((obj_of(e.get("workspace_groups")) or {}).get("team"))
    return {
        "release": str_of(e.get("release")),
        "hostname": str_of(e.get("hostname")),
        "stages": stages,
        "lease": None if lease is None else {"holder": str_of(lease.get("holder")),
                                             "heartbeat": time_of(lease.get("heartbeat"))},
        "verify": norm_verify(e.get("verify")),
        "hand_off_at": time_of(e.get("hand_off_at")),
        "ready_at": time_of(e.get("ready_at")),
        "workspace_groups": None if team_group is None else {"team": team_group},
    }


def norm_github_app(raw: Any) -> dict[str, Any] | None:
    g = obj_of(raw)
    if g is None:
        return None
    return {"started": g.get("started") is True or str_of(g.get("id")) is not None,
            "id": str_of(g.get("id")), "slug": str_of(g.get("slug")),
            "installed_on": str_of(g.get("installed_on")),
            "secrets_written_at": str_of(g.get("secrets_written_at")),
            "self_check": norm_self_check(g.get("self_check"))}


def norm_smoke(raw: Any) -> dict[str, Any] | None:
    smoke = obj_of(raw)
    if smoke is None:
        return None
    return {"state": str_of(smoke.get("state")) or "waiting",
            "detail": str_of(smoke.get("detail")) or str_of(smoke.get("reason")),
            "url": str_of(smoke.get("url"))}


def norm_grant(raw: Any) -> dict[str, Any] | None:
    grant = obj_of(raw)
    if grant is None or str_of(grant.get("state")) is None:
        return None
    return {"state": str_of(grant.get("state")), "account": str_of(grant.get("account")),
            "selection": str_of(grant.get("selection")),
            "repositories": labels_of(grant.get("repositories"))}


def _progress(row: dict[str, Any]) -> dict[str, Any]:
    progress = obj_of(row.get("progress")) or {}
    live = str_of(row.get("workspace_state"))
    return progress if live is None else {**progress, "workspace_state": live}


def norm_personal(raw: Any) -> dict[str, Any] | None:
    p = obj_of(raw)
    if p is None:
        return None
    return {"owner": str_of(p.get("owner")), "workspace": str_of(p.get("workspace")),
            "reason": str_of(p.get("reason")), "progress": _progress(p),
            "grant": norm_grant(p.get("grant")), "smoke": norm_smoke(p.get("smoke")),
            "pool_accounts": labels_of(p.get("pool_accounts"))}


def norm_step_need(raw: Any) -> dict[str, Any] | None:
    s = obj_of(raw)
    if s is None:
        return None
    return {"needed": s.get("needed") is not False, "state": str_of(s.get("state")) or "",
            "reason": str_of(s.get("reason"))}


def norm_team(raw: Any) -> dict[str, Any] | None:
    t = obj_of(raw)
    team_id = None if t is None else str_of(t.get("id"))
    if t is None or team_id is None:
        return None
    members = [m for m in map(obj_of, t.get("members") if isinstance(t.get("members"), list)
                              else []) if m is not None and str_of(m.get("email")) is not None]
    return {
        "id": team_id,
        "group": str_of(t.get("group")) or "",
        "workspace": str_of(t.get("workspace")) or team_id,
        "first_admin": str_of(t.get("first_admin")),
        "invites": [x for x in t.get("invites") or [] if isinstance(x, str)]
        if isinstance(t.get("invites"), list) else [],
        "progress": _progress(t),
        "smoke": norm_smoke(t.get("smoke")),
        "grant": norm_grant(t.get("grant")),
        "members": [{"email": str_of(m.get("email")),
                     "member": m.get("member") if isinstance(m.get("member"), bool) else None}
                    for m in members],
        "pool_accounts": labels_of(t.get("pool_accounts")),
    }


def norm_verify_runs(raw: Any) -> dict[str, Any] | None:
    doc = obj_of(raw)
    ids = obj_of((doc or {}).get("verify_runs"))
    if doc is None or ids is None:
        return None
    task = str_of(ids.get("console_task"))
    workflow = str_of(ids.get("console_workflow"))
    counts = obj_of(doc.get("retries"))

    def count(key: str) -> int:
        n = (counts or {}).get(key, ids.get(f"{key}_retries"))
        return max(0, int(n)) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0

    if task is None and workflow is None:
        return None
    return {"task": task, "workflow": workflow, "repository": str_of(doc.get("repository")),
            "retries": {"task": count("console_task"), "workflow": count("console_workflow")}}


def _n(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
        else None


def norm_sizing(raw: Any) -> dict[str, Any] | None:
    d = obj_of(raw)
    if d is None or d.get("mode") not in ("individual", "team"):
        return None
    limits = obj_of(d.get("pool_limits")) or {}
    services = obj_of(d.get("service_max_instances")) or {}
    return {"mode": d["mode"], "members": _n(d.get("members")),
            "pool_limits": {"global": _n(limits.get("global")),
                            "default_tenant": _n(limits.get("default_tenant")),
                            "provider_tenant": _n(limits.get("provider_tenant"))},
            "service_max_instances": {k: v for k, v in services.items() if _n(v) is not None},
            "pool_accounts": _n(d.get("pool_accounts")), "budget_usd": _n(d.get("budget_usd"))}


def norm_setup_state(raw: Any) -> dict[str, Any]:
    """api.ts `normSetupState`: `GET /v1/admin/setup` as the page reads it."""
    d = obj_of(raw) or {}
    accounts = obj_of(d.get("accounts")) or {}
    finished = obj_of(d.get("finished"))
    sizing = norm_sizing(d.get("sizing"))
    mode = d.get("mode") if d.get("mode") in ("individual", "team") else \
        (sizing or {}).get("mode", "team")
    steps = obj_of(d.get("steps"))
    return {
        "env": str_of(d.get("env")) or "",
        "engine": norm_engine(d.get("engine")),
        "cli_running": d.get("cli_running") is True,
        "github_app": norm_github_app(d.get("github_app")),
        "teams": [t for t in map(norm_team, d.get("teams") if isinstance(d.get("teams"), list)
                                 else []) if t is not None],
        "accounts": [{"label": label, "owner": str_of((obj_of(accounts[label]) or {}).get("owner")),
                      "link_expires": str_of((obj_of(accounts[label]) or {}).get("link_expires")),
                      "signed_in_at": str_of((obj_of(accounts[label]) or {}).get("signed_in_at"))}
                     for label in sorted(accounts)],
        "finished": None if finished is None else {"at": str_of(finished.get("at")),
                                                   "by": str_of(finished.get("by"))},
        "addons": [{"name": str_of(a.get("name")),
                    "enable": str_of(a.get("enable")) or f"swarm addon enable {a.get('name')}",
                    "secret_link": a.get("secret_link") is True}
                   for a in map(obj_of, d.get("addons") if isinstance(d.get("addons"), list)
                                else []) if a is not None and str_of(a.get("name")) is not None],
        "hostname": str_of(d.get("hostname")),
        "github_org": str_of(d.get("github_org")),
        "verify_runs": norm_verify_runs(d.get("verify_runs")),
        "sizing": sizing,
        "mode": mode,
        "personal": norm_personal(d.get("personal")) if mode == "individual" else None,
        "steps": None if steps is None else {"first_team": norm_step_need(steps.get("first_team")),
                                             "invites": norm_step_need(steps.get("invites"))},
    }


def norm_manifest_start(raw: Any) -> dict[str, Any]:
    """api.ts `startGitHubApp`."""
    d = obj_of(raw)
    return {"manifest": obj_of((d or {}).get("manifest")) or {},
            "action": str_of((d or {}).get("action")) or ""}


def norm_conversion(raw: Any) -> dict[str, Any]:
    """api.ts `convertGitHubApp`."""
    d = obj_of(raw) or {}
    return {"id": str_of(d.get("id")), "slug": str_of(d.get("slug"))}


def norm_installation(raw: Any) -> dict[str, Any]:
    """api.ts `checkGitHubAppInstallation`."""
    d = obj_of(raw) or {}
    return {"installed": d.get("installed") is True, "org": str_of(d.get("org")),
            "slug": str_of(d.get("slug"))}


def norm_invite(raw: Any) -> dict[str, Any] | None:
    """api.ts `normInvite`: only a Google Admin page is kept as the members page."""
    d = obj_of(raw)
    email = None if d is None else str_of(d.get("email"))
    if d is None or email is None:
        return None
    url = str_of(d.get("add_members_url"))
    return {"email": email,
            "add_members_url": url if url and url.startswith("https://admin.google.com/") else None,
            "member": d.get("member") if isinstance(d.get("member"), bool) else None}


def norm_team_create(raw: Any) -> dict[str, Any]:
    """api.ts `createSetupTeam`."""
    d = obj_of(raw) or {}
    return {"team": str_of(d.get("team")),
            "invites": [i for i in map(norm_invite, d.get("invites") if
                                       isinstance(d.get("invites"), list) else []) if i]}


def norm_link(raw: Any, body: dict[str, str]) -> dict[str, Any]:
    """api.ts `mintSetupLink`."""
    d = obj_of(raw) or {}
    return {"kind": str_of(d.get("kind")) or body["kind"],
            "label": str_of(d.get("label")) or body["label"],
            "owner": str_of(d.get("owner")) or body["owner"], "url": str_of(d.get("url")),
            "path": str_of(d.get("path")) or "", "expires_at": str_of(d.get("expires_at"))}


def norm_restart_smoke(raw: Any) -> dict[str, Any]:
    """api.ts `restartSetupSmoke`."""
    d = obj_of(raw) or {}
    return {"workspace": str_of(d.get("workspace")), "smoke": norm_smoke(d.get("smoke"))}


def norm_finish(raw: Any) -> dict[str, Any]:
    """api.ts `finishSetup`."""
    return {"at": str_of((obj_of((obj_of(raw) or {}).get("finished")) or {}).get("at"))}


def opened_from(raw: Any) -> dict[str, Any] | None:
    """SetupLink.tsx `openedFrom`: the link page's reading of the redeem answer."""
    d = obj_of(raw)
    if d is None:
        return None
    label = d.get("label") if isinstance(d.get("label"), str) else None
    if d.get("kind") == "pool-account":
        if label is None or not isinstance(d.get("tenant"), str) or not d["tenant"]:
            return None
        return {"kind": "pool-account", "label": label, "tenant": d["tenant"]}
    if d.get("kind") in ("team-git", "addon-secret"):
        return {"kind": d["kind"], "label": label,
                "secret": d.get("secret") if isinstance(d.get("secret"), str) else None}
    return None


# --------------------------------------------------------------------------
# every field api.ts reads, per route
# --------------------------------------------------------------------------

SETUP = "GET /v1/admin/setup"
MANIFEST = "POST /v1/admin/setup/github-app/manifest"
CONVERSION = "POST /v1/admin/setup/github-app/conversion"
INSTALLATION = "GET /v1/admin/setup/github-app/installation"
SELF_CHECK = "POST /v1/admin/setup/github-app/self-check"
TEAMS = "POST /v1/admin/setup/teams"
INVITES = "POST /v1/admin/setup/teams/{id}/invites"
LINKS = "POST /v1/admin/setup/links"
REDEEM = "POST /v1/setup/links/redeem"
SMOKE = "POST /v1/admin/setup/smoke"
VERIFY_RUNS = "POST /v1/admin/setup/verify-runs"
FINISH = "POST /v1/admin/setup/finish"

_ENGINE = ["release", "hostname", "stages", "stages[].state", "stages[].at", "stages[].by",
           "lease", "lease.holder", "lease.heartbeat", "verify", "hand_off_at", "ready_at"]
_GRANT = ["state", "account", "selection", "repositories"]
_SMOKE = ["state", "detail", "url"]
_SELF_CHECK = ["ok", "reason", "at", "state"]

#: Route -> the field paths api.ts reads from its answer (primary names).
FIELDS_READ: dict[str, list[str]] = {
    SETUP: [
        "env", "engine", "cli_running", "github_app", "teams", "accounts", "finished", "addons",
        "hostname", "github_org", "verify_runs", "sizing", "mode", "personal", "steps",
        *(f"engine.{f}" for f in _ENGINE),
        "github_app.started", "github_app.id", "github_app.slug", "github_app.installed_on",
        "github_app.secrets_written_at",
        *(f"github_app.self_check.{f}" for f in _SELF_CHECK),
        "teams[].id", "teams[].group", "teams[].workspace", "teams[].first_admin",
        "teams[].invites", "teams[].progress", "teams[].workspace_state",
        *(f"teams[].smoke.{f}" for f in _SMOKE), *(f"teams[].grant.{f}" for f in _GRANT),
        "teams[].members[].email", "teams[].members[].member", "teams[].pool_accounts",
        "finished.at", "finished.by",
        "addons[].name", "addons[].enable", "addons[].secret_link",
        "verify_runs.verify_runs.console_task", "verify_runs.verify_runs.console_workflow",
        "verify_runs.repository", "verify_runs.retries.console_task",
        "verify_runs.retries.console_workflow",
        "sizing.mode", "sizing.members", "sizing.pool_limits.global",
        "sizing.pool_limits.default_tenant", "sizing.pool_limits.provider_tenant",
        "sizing.service_max_instances", "sizing.pool_accounts", "sizing.budget_usd",
        "steps.first_team.needed", "steps.first_team.state", "steps.first_team.reason",
        "steps.invites.needed", "steps.invites.state", "steps.invites.reason",
    ],
    MANIFEST: ["manifest", "action"],
    CONVERSION: ["id", "slug"],
    INSTALLATION: ["installed", "org", "slug"],
    SELF_CHECK: list(_SELF_CHECK),
    TEAMS: ["team", "invites[].email", "invites[].add_members_url", "invites[].member"],
    INVITES: ["email", "add_members_url", "member"],
    LINKS: ["kind", "label", "owner", "url", "path", "expires_at"],
    REDEEM: ["kind", "label", "tenant", "secret"],
    SMOKE: ["workspace", *(f"smoke.{f}" for f in _SMOKE)],
    VERIFY_RUNS: ["verify_runs.console_task", "verify_runs.console_workflow", "repository",
                  "retries.console_task", "retries.console_workflow"],
    FINISH: ["finished.at"],
}

#: One person's page reads `personal` where a team's reads `teams[]`.
PERSONAL_FIELDS = ["personal.owner", "personal.workspace", "personal.progress",
                   "personal.workspace_state", *(f"personal.grant.{f}" for f in _GRANT),
                   *(f"personal.smoke.{f}" for f in _SMOKE), "personal.pool_accounts"]

_MISSING = object()


def _served(value: Any, parts: list[str]) -> bool:
    """Whether `value` carries the path `parts` with a non-null value."""
    if not parts:
        return value is not None
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        items = value.get(head[:-2]) if isinstance(value, dict) else None
        return isinstance(items, list) and any(_served(item, rest) for item in items) \
            if rest else isinstance(items, list)
    found = value.get(head, _MISSING) if isinstance(value, dict) else _MISSING
    return found is not _MISSING and _served(found, rest)


def unserved(answers: dict[str, list[Any]], fields: dict[str, list[str]]) -> dict[str, list[str]]:
    """Per route, every field path no recorded answer carried with a value."""
    gaps: dict[str, list[str]] = {}
    for route, paths in fields.items():
        bodies = answers.get(route) or []
        missing = [p for p in paths if not any(_served(b, p.split(".")) for b in bodies)]
        if missing:
            gaps[route] = missing
    return gaps
