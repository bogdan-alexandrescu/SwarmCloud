"""The gate's half of a carried browser session (live browser L4c).

docs/design/live-browser.md section 5.5.1 (LB-7). A signed-in session is a
cookie jar plus the local storage of the signed-in origins, and it moves GATE
TO GATE: this module reads it on the gate's own pipe session when a step ends
(`session_export`) and writes it into the next pod's Chromium before the
agent's first CDP connection (`session_import`). The worker carries the jar
between the two ends over ctl.sock and Secret Manager (browser_login.py); the
agent never holds it, and the agent's own CDP can neither read nor plant a
cookie (cdppolicy.py).

ONLY A HAND-OFF'S SESSION IS EXPORTED. The design exports "a session its
hand-off produced": the gate calls `note_returned` when a person returns
control, and `export` answers None until then. A step that only used a carried
or named session writes nothing back, so it cannot overwrite a good jar with
whatever the agent did to it since. THE ONE EXCEPTION IS A PARK
(`{"scope": "park"}`, §5.5.2, issue #1362): the jar then goes to the same
task's own resumed attempt, never to a workflow carry or a login, so the
browser it describes is exactly what that attempt should get back -- with or
without a hand-off that returned.

THE IMPORT IS REFUSED ONCE AN AGENT HAS CONNECTED. The worker sends it after
registering on ctl.sock and before it spawns the agent (Worker._prepare), so a
connected agent means the order went wrong; planting cookies under a live
agent's feet is refused and recorded `browser_session_import_refused`.

EVERY CONTEXT IS EXPORTED, THE DEFAULT ONE IMPORTED. A hand-off on the fresh
page signs in inside a gate-owned context that is then handed to the agent
(cdpproxy.py), so export reads `Storage.getCookies` for the default context
and every `Target.getBrowserContexts` entry. Import writes into the default
context, which is the one `connectOverCDP` gives the agent; a context the
agent creates itself with `newContext()` starts empty, as it would anywhere.

ORIGIN STORAGE GOES THROUGH `DOMStorage` on a gate session attached to a page:
the https origins of the pages open at export are the "signed-in origins", at
most `MAX_ORIGINS` of them. That an origin's local storage can be written
through `DOMStorage.setDOMStorageItem` from a page on another origin is design,
not measured; a failure there loses the storage and keeps the cookies.

THE JAR IS BOUNDED. A Secret Manager version holds at most 64 KiB and a ctl.sock
line the same, so a jar over `MAX_JAR_BYTES` sheds its origin storage first and,
still too large, is not carried at all (`browser_session_too_large`): the next
step signs in again, which is the group-tenant behaviour and safe.

THE QA IDENTITY'S HOST IS NEVER CARRIED (QI-GATE; docs/design/qa-identity.md
§4.4, part of #1649). While the task names the QA identity, `excluded_host`
is the console host, and every export -- carry, login, park -- leaves out the
cookies that host would be sent (its own and its parent domains') and its
origin's storage: nothing IAP sets for the QA account may enter LB-7's
storage, where a later step or another task would carry it. An import made
then leaves them out too: a person's own console session, carried from an
earlier hand-off, would otherwise sign the agent in as that person beside the
QA identity.

NOTHING HERE IS LOGGED OR RECORDED WITH A VALUE. Records carry counts; a cookie
value, a storage value, a domain or a cookie name never appears in one.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

#: The ctl.sock operations this module answers (livegate/ctl.py's OPS must
#: name them; see the PR's request to lane L3).
EXPORT_OP = "session_export"
IMPORT_OP = "session_import"
OPS = frozenset({EXPORT_OP, IMPORT_OP})
#: `session_export`'s scope for a park's capture (§5.5.2).
PARK_SCOPE = "park"

#: The stored jar's format version.
JAR_VERSION = 1

#: Under Secret Manager's 64 KiB version limit and ctl.sock's 64 KiB line, with
#: room for the envelope, the MAC and JSON's \u-escaping of non-ASCII text
#: (measured with ensure_ascii, the encoding both carriers use).
MAX_JAR_BYTES = 56 * 1024

#: The signed-in origins whose local storage is carried.
MAX_ORIGINS = 8

# What `Storage.setCookies` takes (Network.CookieParam). Everything else a
# `Storage.getCookies` answer carries -- `size`, `session` -- is read-only.
_COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "secure",
    "httpOnly",
    "sameSite",
    "expires",
    "priority",
    "sameParty",
    "sourceScheme",
    "sourcePort",
    "partitionKey",
)

GateCall = Callable[[str, Mapping[str, Any] | None, str | None], Awaitable[dict[str, Any]]]
Record = Callable[[str, Mapping[str, Any]], None]


class SessionRefused(Exception):
    """The gate will not do this: an import after the agent connected, a malformed jar."""


def _size(jar: Mapping[str, Any]) -> int:
    return len(json.dumps(jar, separators=(",", ":"), ensure_ascii=True))


def _cookie_param(cookie: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
        return None
    if not isinstance(cookie.get("domain"), str) or not cookie["domain"]:
        return None
    out = {k: cookie[k] for k in _COOKIE_FIELDS if k in cookie}
    # A session cookie reads back with expires -1; carried, it stays a session
    # cookie (no expiry), which Storage.setCookies takes as an absent field.
    if cookie.get("session") is True or not isinstance(out.get("expires"), (int, float)) or out["expires"] <= 0:
        out.pop("expires", None)
    return out


def _origin(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    return f"https://{parts.netloc}"


def sent_to(cookie: Mapping[str, Any], host: str) -> bool:
    """Whether a browser would send `cookie` to `host`: its domain is the host
    or one the host is under (RFC 6265 domain-match), leading dot or not."""
    domain = str(cookie.get("domain", "")).lower().lstrip(".")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def valid_jar(jar: Any) -> bool:
    """The shape a stored jar must have before it reaches Chromium."""
    if not isinstance(jar, Mapping) or jar.get("v") != JAR_VERSION:
        return False
    cookies, origins = jar.get("cookies"), jar.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        return False
    if not all(isinstance(c, Mapping) and _cookie_param(c) is not None for c in cookies):
        return False
    for entry in origins:
        if not isinstance(entry, Mapping) or _origin(entry.get("origin")) != entry.get("origin"):
            return False
        items = entry.get("localStorage")
        if not isinstance(items, list) or not all(
            isinstance(i, list) and len(i) == 2 and all(isinstance(s, str) for s in i) for i in items
        ):
            return False
    return True


class SessionCarry:
    """Export and import of the browser's session, on the gate's own CDP session.

    `call` is the gate's own command path (`CdpProxy.call`): the method policy
    does not apply to it, which is exactly what lets the gate read cookies the
    agent may not. `agent_connected` says whether any agent CDP connection has
    been accepted yet.
    """

    def __init__(
        self,
        call: GateCall,
        *,
        record: Record,
        agent_connected: Callable[[], bool],
        now: Callable[[], float] = time.time,
        excluded_host: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._call = call
        self._excluded_host = excluded_host
        self._record = record
        self._agent_connected = agent_connected
        self._now = now
        self._returned = False

    def note_returned(self) -> None:
        """A person returned control: the browser now holds a session worth carrying."""
        self._returned = True

    @property
    def has_session(self) -> bool:
        return self._returned

    # ----------------------------------------------------------------- ctl

    async def handle(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one ctl.sock operation. The jar travels only on this MAC'd channel."""
        if op == EXPORT_OP:
            return {"jar": await self.export(park=args.get("scope") == PARK_SCOPE)}
        if op == IMPORT_OP:
            return await self.import_jar(args.get("jar"))
        raise SessionRefused(f"not a session operation: {op}")

    # -------------------------------------------------------------- export

    async def export(self, *, park: bool = False) -> dict[str, Any] | None:
        """The session a hand-off produced, or None (no hand-off, or too large).

        `park`: the browser's session for the same task's resumed attempt,
        whether or not a hand-off produced it (§5.5.2)."""
        if not self._returned and not park:
            return None
        now = self._now()
        contexts: list[str | None] = [None]
        listed = await self._call("Target.getBrowserContexts", {}, None)
        contexts += [c for c in listed.get("browserContextIds") or [] if isinstance(c, str)]
        seen: set[tuple[Any, ...]] = set()
        cookies: list[dict[str, Any]] = []
        for context in contexts:
            params = {"browserContextId": context} if context is not None else {}
            result = await self._call("Storage.getCookies", params, None)
            for raw in result.get("cookies") or []:
                if not isinstance(raw, Mapping):
                    continue
                expires = raw.get("expires")
                if raw.get("session") is not True and isinstance(expires, (int, float)) and 0 < expires <= now:
                    continue
                cookie = _cookie_param(raw)
                if cookie is None or self._excluded(cookie):
                    continue
                key = (cookie["name"], cookie["domain"], cookie.get("path"), json.dumps(cookie.get("partitionKey")))
                if key in seen:
                    continue
                seen.add(key)
                cookies.append(cookie)

        origins = await self._export_storage()
        jar: dict[str, Any] = {"v": JAR_VERSION, "cookies": cookies, "origins": origins}
        if _size(jar) > MAX_JAR_BYTES and origins:
            jar["origins"] = []
        if _size(jar) > MAX_JAR_BYTES:
            self._record("browser_session_too_large", {"cookies": len(cookies)})
            return None
        self._record("browser_session_exported", {"cookies": len(cookies), "origins": len(jar["origins"])})
        return jar

    def _excluded(self, cookie: Mapping[str, Any]) -> bool:
        host = self._excluded_host()
        return host is not None and sent_to(cookie, host)

    def _excluded_origin(self, origin: str | None) -> bool:
        host = self._excluded_host()
        return host is not None and origin is not None and urlsplit(origin).hostname == host

    async def _pages(self) -> list[Mapping[str, Any]]:
        result = await self._call("Target.getTargets", {}, None)
        return [t for t in result.get("targetInfos") or [] if isinstance(t, Mapping) and t.get("type") == "page"]

    async def _attached(self, target_id: str) -> str | None:
        try:
            result = await self._call("Target.attachToTarget", {"targetId": target_id, "flatten": True}, None)
        except Exception:  # a page that closed between the listing and the attach
            return None
        session = result.get("sessionId")
        return session if isinstance(session, str) else None

    async def _detach(self, session: str) -> None:
        try:
            await self._call("Target.detachFromTarget", {"sessionId": session}, None)
        except Exception:
            pass

    async def _export_storage(self) -> list[dict[str, Any]]:
        by_origin: dict[str, str] = {}
        for page in await self._pages():
            origin = _origin(page.get("url"))
            target = page.get("targetId")
            if self._excluded_origin(origin):
                continue
            if origin and isinstance(target, str) and origin not in by_origin and len(by_origin) < MAX_ORIGINS:
                by_origin[origin] = target
        out: list[dict[str, Any]] = []
        for origin, target in by_origin.items():
            session = await self._attached(target)
            if session is None:
                continue
            try:
                await self._call("DOMStorage.enable", {}, session)
                result = await self._call(
                    "DOMStorage.getDOMStorageItems",
                    {"storageId": {"securityOrigin": origin, "isLocalStorage": True}},
                    session,
                )
                items = [
                    [e[0], e[1]]
                    for e in result.get("entries") or []
                    if isinstance(e, list) and len(e) == 2 and all(isinstance(s, str) for s in e)
                ]
                if items:
                    out.append({"origin": origin, "localStorage": items})
            except Exception:
                self._record("browser_session_storage_skipped", {"step": "export"})
            finally:
                await self._detach(session)
        return out

    # -------------------------------------------------------------- import

    async def import_jar(self, jar: Any) -> dict[str, Any]:
        """Write a carried jar into the default context, before any agent connects."""
        if self._agent_connected():
            self._record("browser_session_import_refused", {"cause": "agent_connected"})
            raise SessionRefused("an agent has already connected; a session is imported only before it")
        if not valid_jar(jar):
            self._record("browser_session_import_refused", {"cause": "malformed"})
            raise SessionRefused("the jar is not one this gate exported")
        cookies = [c for c in (_cookie_param(c) for c in jar["cookies"]) if c is not None and not self._excluded(c)]
        if cookies:
            await self._call("Storage.setCookies", {"cookies": cookies}, None)
        origins = [e for e in jar.get("origins") or [] if not self._excluded_origin(e.get("origin"))]
        written = await self._import_storage(origins)
        self._record("browser_session_imported", {"cookies": len(cookies), "origins": written})
        return {"cookies": len(cookies), "origins": written}

    async def _import_storage(self, origins: list[Mapping[str, Any]]) -> int:
        if not origins:
            return 0
        pages = await self._pages()
        target = next((p.get("targetId") for p in pages if isinstance(p.get("targetId"), str)), None)
        session = await self._attached(target) if target else None
        if session is None:
            self._record("browser_session_storage_skipped", {"step": "import"})
            return 0
        written = 0
        try:
            await self._call("DOMStorage.enable", {}, session)
            for entry in origins[:MAX_ORIGINS]:
                storage_id = {"securityOrigin": entry["origin"], "isLocalStorage": True}
                for key, value in entry["localStorage"]:
                    await self._call(
                        "DOMStorage.setDOMStorageItem", {"storageId": storage_id, "key": key, "value": value}, session
                    )
                written += 1
        except Exception:
            self._record("browser_session_storage_skipped", {"step": "import"})
        finally:
            await self._detach(session)
        return written
