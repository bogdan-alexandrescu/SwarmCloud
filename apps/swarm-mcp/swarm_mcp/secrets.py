"""Config › Secrets from the sc plugin: links and reads only (lane SSC-SC, part of #2158).

docs/design/secrets-scopes.md §6.4, with BGD's answer Q8 of its §11: a value
arrives only through the console form a one-time link opens, or a terminal's
--stdin. The routes are on main and used exactly as they are:

    POST /v1/workspace/secrets/links               swarm_secret_set_link, workspace or personal
    POST /v1/repositories/{repo_id}/secrets/links  swarm_secret_set_link, repository
    POST /v1/repositories/{repo_id}/actions/links  swarm_actions_secret_link
    GET  /v1/secrets/overview                      swarm_secrets
    GET  /v1/admin/platform-secrets                swarm_platform_secrets (platform admins)

NO TOOL TAKES A VALUE. No schema has a field a value fits in: every string is
an enum or a pattern a value does not match, and each is checked here before
anything is sent, refused WITHOUT being repeated (a value pasted into a name
field must not be echoed into a transcript). Every answer is copied FIELD BY
FIELD from the API's reply, never forwarded whole, so a reply that (wrongly)
carried a value would still not reach one. A link tool answers `url` and
`expires_at` and nothing else; `url` is null, with a note, when the
deployment has no console origin, and the API's `path` is never handed out
then, because it carries the link's token (`keys.key_set_link`'s rule).

A personal secret lives in the caller's own personal workspace, so its link
is minted there: the overview names that workspace (`personal_tenant_id`,
derived by the API from the verified email), and the mint is sent on a copy
of the client naming it, the bridge's own client staying where it was. A
repository is named by its registration in the selected workspace, as
`owner/repo` or its `repo_...` id; one not registered there is refused before
any mint. The API stays the gate for who may set what (§2.5).
"""

from __future__ import annotations

import copy
import re
import urllib.parse
from typing import Any

from .client import SwarmError

#: `secrets_overview.SCOPES` in swarm-api.
SCOPES = ("workspace", "personal", "repository")

#: `LinkRequest.action` in routes/workspace_secrets.py.
ACTIONS = ("add", "rotate")

#: `workspace_secrets.NAME_PATTERN`: the store's secret names.
STORE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: `actions_secrets.SECRET_NAME`, less the reserved `GITHUB_` prefix, checked apart.
ACTIONS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,199}$")
RESERVED_PREFIX = "GITHUB_"

#: `actions_secrets.check_environment`, as a pattern: 1-255 characters, no
#: `/`, no control character, no leading or trailing space. `.` and `..` are
#: refused apart, as there.
ENVIRONMENT = re.compile(r"^(?! )[^/\x00-\x1f\x7f]{1,255}(?<! )$")

#: `repositories._REPO_ID`.
REPO_ID = re.compile(r"^repo_[0-9a-f]{16}$")

#: `owner/repo`, optionally as its GitHub URL.
REPO_SLUG = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)"
                       r"(?:\.git)?/?$", re.IGNORECASE)

#: One schema pattern for `repository`: a slug or a registration id.
REPOSITORY_PATTERN = (r"^(?:repo_[0-9a-f]{16}|(?:https://github\.com/)?[A-Za-z0-9_.-]{1,100}/"
                      r"[A-Za-z0-9_.-]{1,100}/?)$")

#: An overview row's fields (secrets_overview.py's rows), copied one by one.
#: Never a value, a length, `last4` or a version name.
ROW_FIELDS = (
    "section", "store", "name", "scope", "repo_id", "repository", "provider", "kind",
    "status", "set_at", "set_by", "consumers", "can_manage", "writer", "page", "opted_in",
    "environment", "field", "target_id", "site", "expires_at", "last_used_at", "secret_id",
    "tenant_id", "consumer", "enabled", "secrets", "token_id", "user", "repo_ids",
    "forge_login", "verified_at", "github_admin_checked_at_write",
)
#: One `shadowed_by` or `shadows` entry: whose value wins where, by name.
SHADOW_FIELDS = ("scope", "repo_id", "repository", "person", "where", "slot")
OVERVIEW_FIELDS = ("tenant_id", "scope", "repo_id", "repository", "personal_tenant_id",
                   "can_manage", "note")

#: A platform secret's status row (`platform_secrets._status_row`), copied one by one.
PLATFORM_FIELDS = (
    "secret_id", "family", "managed_by", "written_by", "status", "reason",
    "enabled_versions", "newest_enabled_at", "rotation_in_grace", "account_id",
    "owner_tenant", "label", "half",
)
PLATFORM_SUMMARY_FIELDS = ("accounts_unread", "accounts_refused", "accounts_truncated",
                           "read_at")

NO_CONSOLE_NOTE = (
    "this deployment has no console address configured (SWARM_CONSOLE_URL on swarm-api), "
    "so the API could not make a link. Open Config > Secrets in the console directly"
)


def _choice(value: Any, choices: tuple[str, ...], what: str) -> str:
    if value not in choices:
        # NOT REPEATED: a value pasted into the wrong place must not be echoed.
        raise SwarmError(f"{what} must be one of {list(choices)}; nothing was sent")
    return str(value)


def check_store_name(name: Any) -> str:
    if not isinstance(name, str) or not STORE_NAME.fullmatch(name):
        raise SwarmError("name must be the secret's name: lower-case letters, digits and "
                         "'-', at most 64 characters (swarm_secrets lists them); nothing "
                         "was sent")
    return name


def check_actions_name(name: Any) -> str:
    if not isinstance(name, str) or not ACTIONS_NAME.fullmatch(name):
        raise SwarmError("name must be a GitHub Actions secret name: letters, digits and "
                         "underscores, not starting with a digit, at most 200 characters; "
                         "nothing was sent")
    if name.upper().startswith(RESERVED_PREFIX):
        raise SwarmError("GitHub reserves secret names starting with GITHUB_; nothing was sent")
    return name.upper()


def check_environment(environment: Any) -> str | None:
    if environment is None:
        return None
    if not isinstance(environment, str) or not ENVIRONMENT.fullmatch(environment) \
            or environment in (".", ".."):
        raise SwarmError("environment must be the GitHub environment's name: 1-255 "
                         "characters, no '/'; nothing was sent")
    return environment


def resolve_repository(client: Any, repository: Any) -> str:
    """The `repo_id` of a registration of the selected workspace, named as
    `owner/repo` (or its GitHub URL) or by its id. Refused, unrepeated, when
    it is neither, or when the workspace has not registered it."""
    text = repository.strip() if isinstance(repository, str) else ""
    if REPO_ID.fullmatch(text):
        return text
    match = REPO_SLUG.fullmatch(text)
    if match is None:
        raise SwarmError("repository must be owner/repo or a repo_ id of the selected "
                         "workspace; nothing was sent")
    owner, repo = match.group(1), match.group(2)
    row = client.registration(owner, repo)
    repo_id = row.get("repo_id") if isinstance(row, dict) else None
    if not isinstance(repo_id, str) or not REPO_ID.fullmatch(repo_id):
        raise SwarmError(f"{owner}/{repo} is not registered in the selected workspace; a "
                         "workspace admin registers it in the console's Repositories. "
                         "Nothing was sent")
    return repo_id


def _link_reply(answer: Any) -> dict[str, Any]:
    """The url and its expiry, nothing else; a note when there is no url."""
    answer = answer if isinstance(answer, dict) else {}
    url = answer.get("url") if isinstance(answer.get("url"), str) and answer["url"] else None
    reply: dict[str, Any] = {"url": url, "expires_at": answer.get("expires_at")}
    if url is None:
        reply["note"] = NO_CONSOLE_NOTE
    return reply


def _personal_workspace(client: Any) -> str:
    view = client.request("GET", "/v1/secrets/overview?scope=personal")
    tenant = view.get("personal_tenant_id") if isinstance(view, dict) else None
    if not isinstance(tenant, str) or not tenant:
        raise SwarmError("you have no ready personal workspace to keep a personal secret in; "
                         "nothing was minted")
    return tenant


def secret_set_link(client: Any, scope: Any, action: Any, name: Any = None,
                    repository: Any = None) -> dict[str, Any]:
    """A one-time link to the store's add or rotate form (§6.3): `url` and
    `expires_at`. Every argument is checked before anything is sent."""
    scope = _choice(scope, SCOPES, "scope")
    action = _choice(action, ACTIONS, "action")
    payload: dict[str, Any] = {"action": action}
    if action == "rotate":
        if name is None:
            raise SwarmError("a rotate link names the secret (`name`); nothing was sent")
        payload["name"] = check_store_name(name)
    elif name is not None:
        raise SwarmError("an add link names no secret: the form asks for the name; nothing "
                         "was sent")
    if (scope == "repository") != (repository is not None):
        raise SwarmError("`repository` is given with scope repository, and only then; "
                         "nothing was sent")
    if scope == "repository":
        repo_id = resolve_repository(client, repository)
        answer = client.request("POST", f"/v1/repositories/{repo_id}/secrets/links",
                                payload=payload)
    elif scope == "personal":
        mint = copy.copy(client)
        mint.tenant = _personal_workspace(client)
        answer = mint.request("POST", "/v1/workspace/secrets/links", payload=payload)
    else:
        answer = client.request("POST", "/v1/workspace/secrets/links", payload=payload)
    return _link_reply(answer)


def actions_secret_link(client: Any, repository: Any, name: Any,
                        environment: Any = None) -> dict[str, Any]:
    """A one-time link to the GitHub Actions Set form of a registered
    repository, at repository level or in one environment: `url` and
    `expires_at`. The write it opens is authorised again, GitHub asked again."""
    payload: dict[str, Any] = {"name": check_actions_name(name)}
    environment = check_environment(environment)
    if environment is not None:
        payload["environment"] = environment
    repo_id = resolve_repository(client, repository)
    return _link_reply(client.request("POST", f"/v1/repositories/{repo_id}/actions/links",
                                      payload=payload))


def _fields(source: Any, names: tuple[str, ...]) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    return {name: source[name] for name in names if name in source}


def _row(raw: Any) -> dict[str, Any]:
    row = _fields(raw, ROW_FIELDS)
    for key in ("shadowed_by", "shadows"):
        entries = raw.get(key) if isinstance(raw, dict) else None
        row[key] = [_fields(e, SHADOW_FIELDS) for e in entries if isinstance(e, dict)] \
            if isinstance(entries, list) else []
    return row


def overview(client: Any, scope: Any, repository: Any = None) -> dict[str, Any]:
    """`GET /v1/secrets/overview`: the scope's rows, names and status, field
    by field. Never a value."""
    scope = _choice(scope, SCOPES, "scope")
    if (scope == "repository") != (repository is not None):
        raise SwarmError("`repository` is given with scope repository, and only then; "
                         "nothing was read")
    query = [("scope", scope)]
    if scope == "repository":
        query.append(("repo_id", resolve_repository(client, repository)))
    answer = client.request("GET", f"/v1/secrets/overview?{urllib.parse.urlencode(query)}")
    answer = answer if isinstance(answer, dict) else {}
    rows = answer.get("rows") if isinstance(answer.get("rows"), list) else []
    not_listed = answer.get("not_listed") if isinstance(answer.get("not_listed"), list) else []
    return {
        **_fields(answer, OVERVIEW_FIELDS),
        "rows": [_row(r) for r in rows if isinstance(r, dict)],
        "not_listed": [_fields(e, ("store", "why")) for e in not_listed if isinstance(e, dict)],
    }


def _shadow_text(entries: list[dict[str, Any]]) -> str:
    return "; ".join(f"{e.get('where')} (slot {e.get('slot')})" for e in entries)


def overview_text(view: dict[str, Any]) -> str:
    """The rows as a person reads them: one line each, status and who set it."""
    where = view.get("repository") or view.get("personal_tenant_id") or view.get("tenant_id")
    lines = [f"{view.get('scope')} secrets of {where or '?'} (names and status only; "
             "never a value)"]
    rows = view.get("rows") or []
    if not rows:
        lines.append("  none")
    for row in rows:
        place = f" [{row['environment']}]" if row.get("environment") else ""
        field = f" {row['field']}" if row.get("field") else ""
        line = (f"  {row.get('section')}: {row.get('name')}{place}{field}: {row.get('status')}")
        if row.get("set_by") or row.get("set_at"):
            line += f", by {row.get('set_by') or '?'} at {row.get('set_at') or '?'}"
        if row.get("consumers"):
            line += f"; granted to {', '.join(str(c) for c in row['consumers'])}"
        lines.append(line)
        if row.get("shadowed_by"):
            lines.append(f"    shadowed on {_shadow_text(row['shadowed_by'])}")
        if row.get("shadows"):
            lines.append(f"    wins over the value on {_shadow_text(row['shadows'])}")
    return "\n".join(lines)


def platform_secrets(client: Any) -> dict[str, Any]:
    """`GET /v1/admin/platform-secrets` (§5): a platform admin's. A refusal
    (403 for anyone else) is the route's, propagated unchanged."""
    answer = client.request("GET", "/v1/admin/platform-secrets")
    answer = answer if isinstance(answer, dict) else {}
    rows = answer.get("secrets") if isinstance(answer.get("secrets"), list) else []
    counts = answer.get("counts") if isinstance(answer.get("counts"), dict) else {}
    return {
        "secrets": [_fields(r, PLATFORM_FIELDS) for r in rows if isinstance(r, dict)],
        "counts": {str(k): v for k, v in counts.items() if isinstance(v, int)},
        **_fields(answer, PLATFORM_SUMMARY_FIELDS),
    }


def platform_text(view: dict[str, Any]) -> str:
    counts = ", ".join(f"{n} {status}" for status, n in sorted((view.get("counts") or {}).items()))
    lines = [f"platform secrets (status only), read at {view.get('read_at') or '?'}: "
             f"{counts or 'none'}"]
    for row in view.get("secrets") or []:
        reason = f" ({row['reason']})" if row.get("reason") else ""
        grace = ", rotation in grace" if row.get("rotation_in_grace") else ""
        lines.append(f"  {row.get('secret_id')}: {row.get('status')}{reason}, "
                     f"{row.get('enabled_versions')} enabled, newest "
                     f"{row.get('newest_enabled_at') or '-'}{grace}")
    if view.get("accounts_unread"):
        lines.append("  the quota broker's account documents could not be read")
    return "\n".join(lines)
