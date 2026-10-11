"""Schedules and approvals (docs/schedules.md §7.2, §7.3), from a terminal and a session.

A schedule is a typed recurring job on a repository: the tick fires it, its
gate decides what waits for a person, and every wait lands in one inbox,
`GET /v1/approvals`. The routes are swarm-api's (§7.1, lanes S3 and S5). This
module is what `sc schedules`, `sc approvals` and the MCP tools share: the
calls, how a schedule, a firing and an approval are shown, and the refusals
said so the reader knows what to do next.

A CALLER NAMES A TYPE, NEVER A BACKEND (invariant 10). A schedule carries a
type from the catalogue, a scope, a cron, and the type's own `params`. No
image, command, resource or runner profile is accepted here -- not at the top
level and not inside `params` -- and one that is offered is refused before
anything is sent, so the refusal cannot depend on a route remembering to.

AN APPROVAL SENDS THE DIGEST IT SHOWED, as `runs.py`'s plan approval does.
Each inbox item carries the digest of what was shown -- a firing's params, a
plan, a merge's head and verdict -- and `:approve` refuses with
`approval_changed`, `merge_changed` or `plan_changed` when the item is no
longer that. So the surfaces here show the item and its digest first and
approve with THAT digest; a stale one is said plainly and never retried.

THE TENANT IS THE ROUTE'S. Every call is tenant-scoped on the server
(`tenant_scope`, §5.1): another tenant's schedule or approval answers the 404
a missing one does, and nothing here tells the two apart. Nothing printed
carries a credential: the routes serve none.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import uuid
from typing import Any

from . import render
from .client import SwarmClient, SwarmError
from .invocation import terminal_command
from .render import Style

#: The fields that would choose a backend rather than a job (invariant 10).
#: Refused wherever a schedule body is built, including inside `params`, with
#: the field named. The credentials are here too: a schedule runs as its
#: owner's tenant identity (§5.2), so no call needs one.
BACKEND_FIELDS = frozenset({
    "image", "command", "args", "entrypoint", "runner_profile", "profile", "resources",
    "cpu", "memory", "env", "backend", "token", "forge_token",
})

#: A schedule id as `swarm_api.schedules.new_schedule_id` makes it.
SCHEDULE_ID = re.compile(r"^sch_[0-9a-f]{12}$")

#: What an id put into a route path may contain. A `/`, `?` or `#` would
#: reach another route, so an id holding one is refused before it is sent.
_PATH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}$")

#: `sc schedules show` prints this many firings (§7.3): the route serves 50.
SHOWN_FIRINGS = 10

#: The inbox's kinds (`swarm_api.approvals.KINDS`).
KINDS = ("run", "plan", "merge", "proposal", "spec", "hold")


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def refuse_backend_fields(body: dict[str, Any], where: str) -> None:
    """Refuse a body naming a backend field, top level or in `params`. Nothing is sent."""
    named = sorted(BACKEND_FIELDS & set(body))
    params = body.get("params")
    if isinstance(params, dict):
        named += [f"params.{key}" for key in sorted(BACKEND_FIELDS & set(params))]
    if named:
        raise SwarmError(
            f"{where} does not take {named}: a schedule names a type from the catalogue, "
            "and the type chooses the work. No image or command is accepted (invariant 10). "
            "Nothing was sent"
        )


def path_id(value: Any, what: str) -> str:
    """`value` as a path segment, or a refusal naming `what`. Nothing is sent on a refusal."""
    text = value.strip() if isinstance(value, str) else ""
    if not _PATH_ID.match(text) or ".." in text:
        raise SwarmError(f"{what} {str(value)[:80]!r} is not an id this API serves. Nothing was sent")
    return text


def refusal(exc: SwarmError, *, subject: str | None = None) -> SwarmError:
    """The refusals a schedule's or an approval's caller acts on, said plainly.

    `status`, `code` and `detail` are the API's, unchanged, so a caller that
    branches on the refusal still can. Anything not named here is passed back
    as it came.
    """
    if exc.edge:
        return exc
    what = subject or "it"

    def said(text: str) -> SwarmError:
        return SwarmError(f"{text} The API said: {exc}", status=exc.status, edge=False,
                          code=exc.code, detail=exc.detail)

    if exc.code in ("approval_changed", "merge_changed", "plan_changed"):
        return said(
            f"{what} changed since it was shown, so its digest no longer matches. Nothing was "
            f"done. Read it again ({terminal_command('sc approvals')}, or swarm_approvals) and "
            "decide on what it holds now -- do not resend with a digest nobody was shown."
        )
    if exc.code == "already_decided":
        return said(f"{what} was already decided. Nothing was done.")
    if exc.code == "approval_expired":
        return said(f"{what} expired before it was decided; nothing was done and nothing will run.")
    if exc.code == "schedule_changed":
        return said(
            f"{what} was edited since its revision was read. Nothing changed: read it again "
            "and send the revision it has now."
        )
    if exc.code == "use_merge_switch":
        return said(
            "`gate.merge: auto` is set only through the audited merge switch (the gate card in "
            "Automate > Schedules, or POST /v1/schedules/{id}:merge-mode). Nothing changed."
        )
    if exc.status == 404 and subject:
        return said(f"{what} is not one of your tenant's, or does not exist.")
    return exc


def _call(client: SwarmClient, method: str, path: str, payload: dict[str, Any] | None = None,
          *, subject: str | None = None) -> Any:
    try:
        data = client.request(method, path, payload=payload)
    except SwarmError as exc:
        raise refusal(exc, subject=subject) from exc
    if not isinstance(data, dict):
        raise SwarmError(f"{method} {path} answered without an object; this deployment has no "
                         "schedule route this client speaks to")
    return data


def _quoted(value: str) -> str:
    return urllib.parse.quote(value, safe=":@")


# --------------------------------------------------------------------------
# The calls (§7.1)
# --------------------------------------------------------------------------

def types(client: SwarmClient) -> dict[str, Any]:
    return _call(client, "GET", "/v1/schedule-types")


def listing(client: SwarmClient) -> dict[str, Any]:
    data = _call(client, "GET", "/v1/schedules")
    if not isinstance(data.get("schedules"), list):
        raise SwarmError("GET /v1/schedules answered without a `schedules` list; this "
                         "deployment has no schedule route this client speaks to")
    return data


def resolve(client: SwarmClient, ref: Any) -> str:
    """A schedule id from an id or a name (§7.3's `<name|id>`).

    An id is used as given, with nothing read; a name is matched, case-folded,
    against the tenant's schedules -- names are unique in a tenant.
    """
    text = ref.strip() if isinstance(ref, str) else ""
    if not text:
        raise SwarmError("a schedule is named by its id or its name. Nothing was sent")
    if SCHEDULE_ID.match(text):
        return text
    rows = [r for r in listing(client)["schedules"] if isinstance(r, dict)]
    for row in rows:
        if str(row.get("name") or "").casefold() == text.casefold():
            return path_id(row.get("schedule_id"), "schedule")
    names = ", ".join(sorted(str(r.get("name")) for r in rows)) or "none"
    raise SwarmError(f"your tenant has no schedule named or numbered {text[:80]!r} (it has: "
                     f"{names}). Nothing was sent")


def schedule(client: SwarmClient, schedule_id: str) -> dict[str, Any]:
    """One schedule with its last 50 firings: `{schedule, firings}`."""
    sid = path_id(schedule_id, "schedule")
    return _call(client, "GET", f"/v1/schedules/{sid}", subject=f"schedule {sid}")


def repo_ids(client: SwarmClient, repos: list[str]) -> list[str]:
    """The registration ids of `owner/repo` names; one not registered is refused."""
    ids: list[str] = []
    for name in repos:
        owner, _, repo = str(name).strip().partition("/")
        if not owner or not repo or "/" in repo:
            raise SwarmError(f"{name!r} is not owner/repo. Nothing was sent")
        row = client.registration(owner, repo)
        if not row or not row.get("repo_id"):
            raise SwarmError(
                f"{owner}/{repo} is not registered in your tenant, so no schedule can name it. "
                f"Register it first ({terminal_command('sc access')}). Nothing was sent"
            )
        ids.append(str(row["repo_id"]))
    return ids


def scope(client: SwarmClient, mode: str | None, repos: list[str] | None) -> dict[str, Any]:
    mode = mode or "repos"
    if mode not in ("repos", "all", "platform"):
        raise SwarmError("scope is `repos`, `all` or `platform`. Nothing was sent")
    if mode == "repos":
        if not repos:
            raise SwarmError("a `repos` scope needs at least one owner/repo in `repos`. Nothing was sent")
        return {"mode": "repos", "repo_ids": repo_ids(client, repos)}
    if repos:
        raise SwarmError(f"a `{mode}` scope names no repository; drop `repos`. Nothing was sent")
    return {"mode": mode}


def create(client: SwarmClient, body: dict[str, Any]) -> dict[str, Any]:
    """`POST /v1/schedules` with a fresh `client_request_id`, so a retried create
    returns the schedule the first one made (§7.1)."""
    refuse_backend_fields(body, "a schedule")
    payload = {**body, "client_request_id": body.get("client_request_id") or uuid.uuid4().hex}
    return _call(client, "POST", "/v1/schedules", payload, subject="the schedule")


def update(client: SwarmClient, schedule_id: str, body: dict[str, Any]) -> dict[str, Any]:
    refuse_backend_fields(body, "a schedule edit")
    sid = path_id(schedule_id, "schedule")
    return _call(client, "PATCH", f"/v1/schedules/{sid}", body, subject=f"schedule {sid}")


def pause(client: SwarmClient, schedule_id: str, reason: str | None = None) -> dict[str, Any]:
    sid = path_id(schedule_id, "schedule")
    return _call(client, "POST", f"/v1/schedules/{sid}:pause", {"reason": reason} if reason else {},
                 subject=f"schedule {sid}")


def resume(client: SwarmClient, schedule_id: str) -> dict[str, Any]:
    sid = path_id(schedule_id, "schedule")
    return _call(client, "POST", f"/v1/schedules/{sid}:resume", subject=f"schedule {sid}")


def run_now(client: SwarmClient, schedule_id: str, *, dry_run: bool) -> dict[str, Any]:
    """A `run_now` firing, under the gate's `run` point (§7.1): `{firing}`."""
    sid = path_id(schedule_id, "schedule")
    return _call(client, "POST", f"/v1/schedules/{sid}:run", {"dry_run": bool(dry_run)},
                 subject=f"schedule {sid}")


def preview(client: SwarmClient, cron: str, timezone: str | None = None,
            type_name: str | None = None) -> dict[str, Any]:
    """§6.3, from the tick's own parser. Reads nothing and writes nothing."""
    payload: dict[str, Any] = {"cron": cron, "timezone": timezone or "UTC"}
    if type_name:
        payload["type"] = type_name
    return _call(client, "POST", "/v1/schedules:preview", payload)


def inbox(client: SwarmClient, kind: str | None = None) -> dict[str, Any]:
    if kind is not None and kind not in KINDS:
        raise SwarmError(f"kind is one of {', '.join(KINDS)}. Nothing was sent")
    path = "/v1/approvals" + (f"?{urllib.parse.urlencode({'kind': kind})}" if kind else "")
    data = _call(client, "GET", path)
    if not isinstance(data.get("approvals"), list):
        raise SwarmError("GET /v1/approvals answered without an `approvals` list; this "
                         "deployment has no approvals route this client speaks to")
    return data


def approval(client: SwarmClient, approval_id: str) -> dict[str, Any]:
    aid = path_id(approval_id, "approval")
    return _call(client, "GET", f"/v1/approvals/{_quoted(aid)}", subject=f"approval {aid}")


def approve(client: SwarmClient, approval_id: str, *, digest: Any,
            confirm: str | None = None) -> dict[str, Any]:
    """Approve the item whose digest is `digest` -- the one SHOWN. A projected
    plan's id is `run:<run_id>`, and this is then the run's own approve: the
    same transition `swarm_plan_approve` makes."""
    aid = path_id(approval_id, "approval")
    if not (isinstance(digest, str) and digest.strip()) and not isinstance(digest, dict):
        raise SwarmError("an approval sends the digest it was shown: a string, or a merge's "
                         "{head_sha, verdict}. Nothing was sent")
    payload: dict[str, Any] = {"digest": digest.strip() if isinstance(digest, str) else digest}
    if confirm:
        payload["confirm"] = confirm
    return _call(client, "POST", f"/v1/approvals/{_quoted(aid)}:approve", payload,
                 subject=f"approval {aid}")


def reject(client: SwarmClient, approval_id: str, *, reason: str) -> dict[str, Any]:
    aid = path_id(approval_id, "approval")
    if not isinstance(reason, str) or not reason.strip():
        raise SwarmError("a rejection carries a reason. Nothing was sent")
    return _call(client, "POST", f"/v1/approvals/{_quoted(aid)}:reject", {"reason": reason.strip()},
                 subject=f"approval {aid}")


def parse_params(pairs: list[str] | None) -> dict[str, Any]:
    """`--param k=v` pairs. A value that reads as JSON (`5`, `true`, `["a"]`) is
    that value; any other is the text as typed."""
    params: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise SwarmError(f"--param takes key=value; {pair!r} is not. Nothing was sent")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def next_step(listing_: dict[str, Any]) -> str | None:
    """What a session does with an inbox that holds pending items."""
    if any(isinstance(i, dict) and i.get("state") == "pending" for i in listing_.get("approvals") or []):
        return ("show the developer each item and its digest, and decide only with "
                "swarm_schedule_approve and THAT digest once they say so, or "
                "swarm_schedule_reject with their reason. Nothing waiting holds capacity.")
    return None


# --------------------------------------------------------------------------
# Showing them
# --------------------------------------------------------------------------

def _usd(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _spend(row: dict[str, Any], style: Style) -> str:
    spend = row.get("spend_today") if isinstance(row.get("spend_today"), dict) else {}
    if not spend:
        return style.dash
    text = _usd(spend.get("reported_usd"))
    # An attempt whose cost was never reported is not zero: the figure is a floor.
    if spend.get("coverage") == "partial":
        text += f" (partial: {spend.get('unreported_attempts')} unreported)"
    return text


def _repos(row: dict[str, Any]) -> str:
    sc_ = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    if sc_.get("mode") != "repos":
        return str(sc_.get("mode") or "-")
    ids = sc_.get("repo_ids") or []
    return ", ".join(map(str, ids)) if ids else "-"


def _last(row: dict[str, Any], style: Style) -> str:
    last = row.get("last_firing") if isinstance(row.get("last_firing"), dict) else {}
    if not last:
        return "never fired"
    return f"{last.get('state') or style.dash}" + (f" {last['outcome']}" if last.get("outcome") else "")


def schedules_lines(data: dict[str, Any], style: Style) -> list[str]:
    """The list, with the console table's columns (§6.2): one row a schedule."""
    rows = [r for r in data.get("schedules") or [] if isinstance(r, dict)]
    lines = [render.section("schedules", f"{len(rows)} in {data.get('tenant_id') or 'your tenant'}", style)]
    if not rows:
        lines.append(f"  none: {terminal_command('sc schedules new <type> --repo owner/repo --cron ...')}")
    for row in rows:
        state = str(row.get("state") or style.dash)
        tone = {"enabled": "good", "auto_paused": "bad", "paused": "warn", "disabled": "warn"}.get(state)
        pending = int(row.get("pending_approvals") or 0)
        lines.append(style.paint(
            f"  {row.get('name')}  {row.get('schedule_id')}  {row.get('type')}", "bold"))
        lines.append(f"    {style.paint(state, tone)}{style.sep}{row.get('words') or row.get('cron')}"
                     f" ({row.get('timezone') or 'UTC'})")
        lines.append(f"    next {row.get('next_run_at') or style.dash}{style.sep}last {_last(row, style)}"
                     f"{style.sep}today {_spend(row, style)}"
                     + (f"{style.sep}{pending} pending" if pending else ""))
        lines.append(f"    scope {_repos(row)}")
    return lines


def firing_line(firing: dict[str, Any], style: Style) -> str:
    work = [w for w in firing.get("work") or [] if isinstance(w, dict)]
    links = ", ".join(f"{w.get('kind')} {w.get('id')}" for w in work)
    cost = firing.get("cost") if isinstance(firing.get("cost"), dict) else {}
    skip = firing.get("skip") if isinstance(firing.get("skip"), dict) else {}
    tail = ""
    if skip.get("code"):
        tail += f"{style.sep}skipped {skip['code']}"
    if cost.get("reported_usd") is not None:
        tail += f"{style.sep}{_usd(cost.get('reported_usd'))}"
    if links:
        tail += f"{style.sep}{links}"
    return (f"  {firing.get('firing_id')}  {firing.get('slot') or style.dash}  "
            f"{firing.get('state') or style.dash}"
            + (f" {firing['outcome']}" if firing.get("outcome") else "") + tail)


def schedule_lines(data: dict[str, Any], style: Style, *, firings: int = SHOWN_FIRINGS) -> list[str]:
    """One schedule, its gate and budget, and its last `firings` firings."""
    row = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    gate = row.get("gate") if isinstance(row.get("gate"), dict) else {}
    budget = row.get("budget") if isinstance(row.get("budget"), dict) else {}
    lines = [
        render.section("schedule", f"{row.get('name')}{style.sep}{row.get('schedule_id')}", style),
        f"  type          {row.get('type')}" + (f"{style.sep}tier {row['tier']}" if row.get("tier") else ""),
        f"  state         {row.get('state')}{style.sep}revision {row.get('revision')}",
        f"  when          {row.get('words') or style.dash}{style.sep}{row.get('cron')} "
        f"({row.get('timezone') or 'UTC'})",
        f"  next          {row.get('next_run_at') or style.dash}",
        f"  scope         {_repos(row)}",
        f"  owner         {row.get('owner') or style.dash}",
        "  gate          " + ", ".join(f"{k} {gate[k]}" for k in ("run", "plan", "merge", "approvers")
                                       if gate.get(k) is not None),
        "  budget        " + ", ".join(f"{k} {budget[k]}" for k in ("per_run_usd", "per_day_usd",
                                                                    "max_concurrent")
                                       if budget.get(k) is not None),
        f"  today         {_spend(row, style)}",
    ]
    pause_ = row.get("pause") if isinstance(row.get("pause"), dict) else {}
    if pause_.get("reason") or pause_.get("code"):
        lines.append(f"  paused        {pause_.get('code') or ''} {pause_.get('reason') or ''}".rstrip())
    pending = int(row.get("pending_approvals") or 0)
    if pending:
        lines.append(f"  waiting       {pending} approval(s): {terminal_command('sc approvals')}")
    rows = [f for f in data.get("firings") or [] if isinstance(f, dict)][:firings]
    lines.append(render.section("firings", f"last {len(rows)}", style))
    if not rows:
        lines.append("  none yet")
    lines.extend(firing_line(f, style) for f in rows)
    return lines


def preview_lines(data: dict[str, Any], style: Style) -> list[str]:
    answer = data.get("preview") if isinstance(data.get("preview"), dict) else {}
    lines = [render.section("preview", str(answer.get("words") or style.dash), style)]
    for slot in answer.get("next") or []:
        lines.append(f"  {slot}")
    if answer.get("min_gap_minutes") is not None:
        lines.append(f"  shortest gap  {answer['min_gap_minutes']} minutes")
    if answer.get("refusal"):
        lines.extend(render.detail(f"refused: {answer['refusal']}", style, indent=2, tone="bad"))
    return lines


def _digest_text(digest: Any) -> str:
    if isinstance(digest, dict):
        return f"{digest.get('head_sha')}:{digest.get('verdict')}"
    return str(digest or "-")


def approval_lines(item: dict[str, Any], style: Style, *, how: bool = True) -> list[str]:
    subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    where = ", ".join(f"{k} {v}" for k, v in subject.items() if v)
    lines = [
        style.paint(f"  {item.get('approval_id')}  {item.get('kind')}  {item.get('state')}", "bold"),
    ]
    lines.extend(render.detail(str(item.get("summary") or ""), style, indent=4, tone=None))
    if where:
        lines.append(f"    {where}")
    hold = item.get("hold") if isinstance(item.get("hold"), dict) else {}
    if hold.get("code"):
        lines.append(f"    held          {hold['code']}")
    lines.append(f"    approvers     {item.get('approvers') or style.dash}"
                 f"{style.sep}expires {item.get('expires_at') or style.dash}")
    lines.append(f"    digest        {_digest_text(item.get('digest'))}")
    if how and item.get("state") == "pending":
        lines.append(f"    decide        {terminal_command('sc approvals approve ' + str(item.get('approval_id')))}"
                     f"  or  reject {item.get('approval_id')} --reason ...")
    return lines


def inbox_lines(data: dict[str, Any], style: Style) -> list[str]:
    items = [i for i in data.get("approvals") or [] if isinstance(i, dict)]
    lines = [render.section("approvals", f"{len(items)} waiting", style)]
    if not items:
        lines.append("  none")
    for item in items:
        lines.extend(approval_lines(item, style))
    return lines
