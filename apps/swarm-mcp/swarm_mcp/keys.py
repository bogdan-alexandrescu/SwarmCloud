"""Provider keys a person or an admin sets: TypeSafe (lane KEYS-PLUGIN).

The API is KEYS-API's (docs/provider-keys.md), used exactly as it is:

    GET  /v1/provider-keys/{provider}          status: yours, your tenant's, which a task uses
    POST /v1/provider-keys/{provider}/links    a one-time link to the console's key form
    PUT  /v1/provider-keys/{provider}/user     store YOUR key   (the `sc key set` terminal only)
    PUT  /v1/provider-keys/{provider}/tenant   store the tenant's key (admins; the same)

THE KEY NEVER PASSES THROUGH CLAUDE (owner, 2026-10-14). So the two MCP tools
are status and a link: neither has a schema field a key fits in, and what
they answer is copied FIELD BY FIELD from the API's reply, never forwarded
whole, so a reply that (wrongly) carried a value would still not reach a
transcript. The one place a key is handled is `store_key`, called only by the
`sc key set` terminal command, which reads it with getpass or from stdin.
"""

from __future__ import annotations

from typing import Any

from .client import SwarmError
from .invocation import terminal_command

#: What these routes accept: `validation.PROVIDER_KEY_PROVIDERS` in swarm-api.
#: Restated as an allow-list here so a tool argument is an ENUM rather than a
#: free string (a free string is a field a key fits in), and so a bad value is
#: refused before anything is sent -- without being repeated back.
PROVIDERS = ("typesafe",)

#: `user` is the caller's own key; `tenant` the selected tenant's (admins).
SCOPES = ("user", "tenant")

#: A status's fields (docs/provider-keys.md §1). Never a payload, its length,
#: a prefix or a suffix; copied one by one so nothing else can ride along.
STATUS_FIELDS = ("available", "set", "set_at", "set_by", "version", "secret_id")
RESOLUTION_FIELDS = ("tenant_id", "uses", "secret_id")


def check_provider(provider: Any) -> str:
    if provider not in PROVIDERS:
        # NOT REPEATED: a key pasted into the wrong place must not be echoed.
        raise SwarmError(f"the provider must be one of {list(PROVIDERS)}; nothing was sent")
    return str(provider)


def check_scope(scope: Any) -> str:
    if scope not in SCOPES:
        raise SwarmError(f"the scope must be one of {list(SCOPES)}; nothing was sent")
    return str(scope)


def _fields(source: Any, names: tuple[str, ...]) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    return {name: source.get(name) for name in names}


def key_status(client: Any, provider: str) -> dict[str, Any]:
    """`GET /v1/provider-keys/{provider}`, status fields only."""
    provider = check_provider(provider)
    answer = client.request("GET", f"/v1/provider-keys/{provider}")
    answer = answer if isinstance(answer, dict) else {}
    tenant = answer.get("tenant") if isinstance(answer.get("tenant"), dict) else {}
    consumed = answer.get("consumed_by")
    return {
        "provider": provider,
        "user": _fields(answer.get("user"), STATUS_FIELDS),
        "tenant": {**_fields(tenant, STATUS_FIELDS), "tenant_id": tenant.get("tenant_id")},
        "resolution": _fields(answer.get("resolution"), RESOLUTION_FIELDS),
        # Nothing reads these keys yet (no Jev runner exists): `resolution`
        # says what a task WOULD use, and this list says nothing does now.
        "consumed_by": [str(c) for c in consumed] if isinstance(consumed, list) else [],
    }


def key_set_link(client: Any, provider: str, scope: str) -> dict[str, Any]:
    """`POST /v1/provider-keys/{provider}/links`: the console URL and its expiry, nothing else.

    `url` is null when the deployment has no console origin configured; the
    `path` the API also returns is NOT handed out then, because the bridge
    does not spell console hosts (`follow.console_link`) and a bare path
    is not something a person can open."""
    provider, scope = check_provider(provider), check_scope(scope)
    answer = client.request("POST", f"/v1/provider-keys/{provider}/links",
                            payload={"scope": scope})
    answer = answer if isinstance(answer, dict) else {}
    url = answer.get("url") if isinstance(answer.get("url"), str) and answer["url"] else None
    reply: dict[str, Any] = {"url": url, "expires_at": answer.get("expires_at")}
    if url is None:
        reply["note"] = (
            "this deployment has no console address configured (SWARM_CONSOLE_URL on "
            "swarm-api), so the API could not make a link. Open the console's TypeSafe key "
            f"settings directly, or run `{terminal_command('sc key set typesafe')}` in "
            "a terminal"
        )
    return reply


def store_key(client: Any, provider: str, scope: str, value: str) -> Any:
    """The PUT, from the `sc key set` terminal command only: no MCP tool calls it."""
    provider, scope = check_provider(provider), check_scope(scope)
    answer = client.request("PUT", f"/v1/provider-keys/{provider}/{scope}",
                            payload={"api_key": value})
    status = answer.get("status") if isinstance(answer, dict) else None
    return status.get("version") if isinstance(status, dict) else None


def _status_line(label: str, status: dict[str, Any]) -> str:
    if status.get("available") is False:
        return f"{label}: unavailable (no ready personal workspace to keep it in)"
    if not status.get("set"):
        return f"{label}: not set"
    return (f"{label}: set, version {status.get('version')}, "
            f"by {status.get('set_by') or '?'} at {status.get('set_at') or '?'}")


def status_text(view: dict[str, Any]) -> str:
    """The status as a person reads it: which keys are set, and which a task would use."""
    tenant = view.get("tenant") or {}
    uses = (view.get("resolution") or {}).get("uses")
    lines = [
        _status_line("your key", view.get("user") or {}),
        _status_line(f"tenant {tenant.get('tenant_id') or '?'} key", tenant),
        {"user": "a task you submit here would use: your key",
         "tenant": "a task you submit here would use: the tenant key",
         }.get(str(uses), "a task you submit here would use: none (no key is set)"),
    ]
    if not view.get("consumed_by"):
        lines.append("nothing reads TypeSafe keys yet; this is what a task would use")
    return "\n".join(lines)
