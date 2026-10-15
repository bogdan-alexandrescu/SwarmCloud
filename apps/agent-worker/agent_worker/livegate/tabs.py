"""The gate's half of a browser that survives a park: its tabs (issue #1362).

docs/design/live-browser.md §5.5.2. MEASURED 2026-10-15 (QA batch full-1015,
S5-resume, 6/6 failed): an agent opened a page, set a localStorage value,
asked for a hand-off and parked; the resumed attempt's Chromium had only
about:blank. Nothing was restored because nothing was kept: the profile is on
a sidecar-only memory volume of the pod that parked (§3.1), the park wipes it
before its checkpoint (§5.5), Chromium is launched on `about:blank` with no
session-restore flag (`chromium_args`), and the next attempt is a new pod with
an empty volume. Chromium's own session restore (`--restore-last-session`,
`Sessions/`) therefore has nothing to read, and checkpointing the profile
directory to make it so is what §5.5 forbids: it IS the session.

So the platform keeps the TAB LIST itself and reopens it:

* `tabs_export`, at park, before the wipe: the page targets Chromium lists,
  in that order, each `{url, active}`; the active one is the tab the gate's
  page follower shows (`PageFollower.current`). The worker writes the list
  into the hand-off spool, so it travels in the checkpoint with `work/`.
* `tabs_restore`, on the resumed attempt, BEFORE THE AGENT'S FIRST CDP
  CONNECTION (refused after it, like `session_import`): each tab is opened
  in order, the one that was active is brought to the front, and the blank
  page Chromium started on is closed. Opening a URL the earlier attempt's
  agent was on is not the agent navigating: the agent has not started.

WHAT IS NOT REOPENED (`refusal`): anything but http(s); a URL carrying
userinfo; a URL longer than `MAX_URL_CHARS`; a URL whose query or fragment
names a parameter that carries a credential or a sign-in's state
(`SECRET_PARAMS`: an OAuth `code` or `state`, a `token`...), or that the
redaction filter would change; and any URL on a host of the mask's
challenge list (`mask.SHOWN_IN_CONTROL`: reCAPTCHA, hCaptcha, Cloudflare's
challenge), at any path -- a challenge is a person's to solve, and a stale one
reopened would only fail. A sign-in page is otherwise reopened like any other
page: it is the page the agent was on, the mask still covers its credential
fields, and the hand-off ledger tells the agent its sign-in was NOT DONE.

SITE STORAGE GOES WITH THE TAB THAT NEEDS IT. Chromium's DOMStorage domain
writes an origin's localStorage only through a frame OF that origin, so the
blank page cannot take it. For the first tab of each origin with carried
storage, the tab is opened on about:blank, a script is added to every new
document in an isolated world of the gate's own session
(`Page.addScriptToEvaluateOnNewDocument`), and the tab is navigated: the
script writes the items when the URL's document is created, before any script
of the page runs. The gate's session is detached once the document is on the
origin, which removes the script, so a later navigation of the agent's is
untouched. The storage itself comes only from the saved-logins route
(§5.5.1): a personal tenant's park secret. A group tenant's tabs come back
without it.

THE WHOLE RESTORE FITS INSIDE ONE ctl.sock CALL, whose client waits 10 s; the
budget is `RESTORE_SECONDS`. A tab whose document has not reached its
origin by then is left loading, its storage recorded as skipped.

NOTHING HERE IS LOGGED OR RECORDED WITH A URL OR A VALUE. Records carry counts
and refusal causes. The URLs travel only on ctl.sock to the worker, which
writes them into the agent's own spool and prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from swarm_redaction import redact as redact_text

from .mask import SHOWN_IN_CONTROL

EXPORT_OP = "tabs_export"
RESTORE_OP = "tabs_restore"
OPS = frozenset({EXPORT_OP, RESTORE_OP})

#: At most this many tabs are kept and reopened: a park keeps the agent's
#: working set, not a crawl.
MAX_TABS = 12
#: A longer URL is not kept: it is a data blob, not a place.
MAX_URL_CHARS = 2048
#: The whole restore's budget, under the worker's ctl.sock timeout of 10 s
#: (`ctl.CtlClient`), with room for the calls after the last tab.
RESTORE_SECONDS = 7.0
#: How often a tab carrying storage is asked whether its document is on its origin.
POLL_SECONDS = 0.1
#: The isolated world the storage script runs in: no page script can reach it.
RESTORE_WORLD = "swarm-restore-storage"

#: Query or fragment parameters whose name says they carry a credential or a
#: sign-in's state. Matched as a substring of the lower-cased name, so
#: `access_token`, `id_token` and `X-Amz-Signature` are all caught.
SECRET_PARAMS = (
    "token", "code", "state", "secret", "password", "passwd", "auth", "session",
    "signature", "sig", "key", "credential", "ticket", "nonce", "otp",
)

GateCall = Callable[[str, Mapping[str, Any] | None, str | None], Awaitable[dict[str, Any]]]
Record = Callable[[str, Mapping[str, Any]], None]


class TabsRefused(Exception):
    """The gate will not do this: a restore after the agent connected, a malformed list."""


def _challenge_host(host: str) -> bool:
    for allowed, _ in SHOWN_IN_CONTROL:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == allowed:
            return True
    return False


def _names(text: str) -> list[str]:
    return [name for name, _ in parse_qsl(text, keep_blank_values=True)]


def refusal(url: Any) -> str | None:
    """Why `url` is not kept or reopened, or None when it is. A cause, never the URL."""
    if not isinstance(url, str) or not url:
        return "not_a_url"
    if len(url) > MAX_URL_CHARS:
        return "too_long"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return "not_a_url"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "scheme"
    if parts.username is not None or parts.password is not None:
        return "userinfo"
    if _challenge_host(parts.hostname.lower()):
        return "challenge_host"
    names = _names(parts.query) + (_names(parts.fragment) if "=" in parts.fragment else [])
    if any(word in name.lower() for name in names for word in SECRET_PARAMS):
        return "credential_param"
    if redact_text(url).text != url:
        return "credential_shaped"
    return None


def clean_tabs(value: Any) -> tuple[list[dict[str, Any]], int]:
    """A tab list as the gate or the spool gave it: kept tabs, and how many were dropped.

    Read as untrusted on both ends: the gate's comes from pages, the spool's
    from a file in the agent's reach. At most one tab is active.
    """
    if not isinstance(value, list):
        return [], 0
    out: list[dict[str, Any]] = []
    dropped = 0
    active_seen = False
    for entry in value:
        url = entry.get("url") if isinstance(entry, Mapping) else None
        if refusal(url) is not None or len(out) >= MAX_TABS:
            dropped += 1
            continue
        active = bool(isinstance(entry, Mapping) and entry.get("active") is True and not active_seen)
        active_seen = active_seen or active
        out.append({"url": url, "active": active})
    return out, dropped


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _storage(value: Any) -> dict[str, list[list[str]]]:
    """Carried origin storage (session.py's jar `origins`), keyed by origin; malformed entries dropped."""
    out: dict[str, list[list[str]]] = {}
    if not isinstance(value, list):
        return out
    for entry in value:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("origin"), str):
            continue
        items = entry.get("localStorage")
        if not isinstance(items, list):
            continue
        kept = [[i[0], i[1]] for i in items
                if isinstance(i, list) and len(i) == 2 and all(isinstance(s, str) for s in i)]
        if kept:
            out[entry["origin"]] = kept
    return out


def storage_script(origin: str, items: list[list[str]]) -> str:
    """The new-document script that writes `items` into `origin`'s localStorage, once per document.

    JSON is valid JavaScript; ASCII-escaped, so no line separator reaches the source."""
    return (
        "(function () {"
        f" var origin = {json.dumps(origin, ensure_ascii=True)};"
        f" var items = {json.dumps(items, ensure_ascii=True)};"
        " if (location.origin !== origin) return;"
        " try { for (var i = 0; i < items.length; i++) localStorage.setItem(items[i][0], items[i][1]); }"
        " catch (e) {}"
        " })();"
    )


class TabKeeper:
    """`tabs_export` and `tabs_restore`, on the gate's own CDP session.

    `call` is the gate's command path (`CdpProxy.call`, through the current
    browser); `shown` names the target the page follower shows;
    `agent_connected` says whether any agent CDP connection has been accepted.
    """

    def __init__(
        self,
        call: GateCall,
        *,
        record: Record,
        agent_connected: Callable[[], bool],
        shown: Callable[[], str | None],
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget: float = RESTORE_SECONDS,
    ) -> None:
        self._call = call
        self._record = record
        self._agent_connected = agent_connected
        self._shown = shown
        self._sleep = sleep
        self._clock = clock
        self._budget = budget

    async def handle(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if op == EXPORT_OP:
            return await self.export()
        if op == RESTORE_OP:
            return await self.restore(args.get("tabs"), args.get("origins"))
        raise TabsRefused(f"not a tabs operation: {op}")

    async def _pages(self) -> list[Mapping[str, Any]]:
        result = await self._call("Target.getTargets", {}, None)
        return [t for t in result.get("targetInfos") or [] if isinstance(t, Mapping) and t.get("type") == "page"]

    # -------------------------------------------------------------- export

    async def export(self) -> dict[str, Any]:
        """The open tabs, in the order Chromium lists them, and which one is shown."""
        shown = self._shown()
        raw: list[dict[str, Any]] = []
        blank = 0
        for page in await self._pages():
            url = page.get("url")
            if not isinstance(url, str) or url in ("", "about:blank"):
                blank += 1
                continue
            raw.append({"url": url, "active": page.get("targetId") == shown and shown is not None})
        tabs, dropped = clean_tabs(raw)
        if tabs and not any(t["active"] for t in tabs):
            # The shown tab was one not kept (a challenge, a blank): the last kept comes forward.
            tabs[-1]["active"] = True
        self._record("browser_tabs_exported", {"tabs": len(tabs), "dropped": dropped, "blank": blank})
        return {"tabs": tabs}

    # ------------------------------------------------------------- restore

    async def restore(self, tabs_arg: Any, origins_arg: Any) -> dict[str, Any]:
        """Reopen `tabs` before the agent connects. Returns the URLs opened, in order."""
        if self._agent_connected():
            self._record("browser_tabs_restore_refused", {"cause": "agent_connected"})
            raise TabsRefused("an agent has already connected; tabs are restored only before it")
        if not isinstance(tabs_arg, list):
            self._record("browser_tabs_restore_refused", {"cause": "malformed"})
            raise TabsRefused("the tab list is not a list")
        tabs, dropped = clean_tabs(tabs_arg)
        storage = _storage(origins_arg)
        deadline = self._clock() + self._budget
        before = await self._pages()
        opened: list[str] = []
        active_target: str | None = None
        written = skipped = 0
        for tab in tabs:
            url = tab["url"]
            items = storage.pop(_origin(url), None)
            try:
                if items:
                    target, ok = await self._open_with_storage(url, items, deadline)
                    written += ok
                    skipped += not ok
                else:
                    result = await self._call("Target.createTarget", {"url": url, "background": True}, None)
                    target = result.get("targetId")
            except Exception:  # one tab that would not open costs that tab only
                dropped += 1
                continue
            opened.append(url)
            if tab["active"] and isinstance(target, str):
                active_target = target
        if opened:
            for page in before:
                if page.get("url") in ("", "about:blank") and isinstance(page.get("targetId"), str):
                    try:
                        await self._call("Target.closeTarget", {"targetId": page["targetId"]}, None)
                    except Exception:
                        pass
            if active_target is not None:
                try:
                    await self._call("Target.activateTarget", {"targetId": active_target}, None)
                except Exception:
                    pass
        self._record("browser_tabs_restored", {
            "tabs": len(opened), "dropped": dropped, "storage_origins": written, "storage_skipped": skipped,
        })
        return {"opened": opened, "storage_origins": written}

    async def _open_with_storage(self, url: str, items: list[list[str]], deadline: float) -> tuple[Any, bool]:
        """Open `url` with its origin's storage written before the page's scripts run."""
        result = await self._call("Target.createTarget", {"url": "about:blank", "background": True}, None)
        target = result.get("targetId")
        if not isinstance(target, str):
            raise TabsRefused("no target")
        attached = await self._call("Target.attachToTarget", {"targetId": target, "flatten": True}, None)
        session = attached.get("sessionId")
        if not isinstance(session, str):
            await self._call("Target.closeTarget", {"targetId": target}, None)
            raise TabsRefused("no session")
        origin = _origin(url)
        ok = False
        try:
            await self._call("Page.enable", {}, session)
            await self._call("Page.addScriptToEvaluateOnNewDocument",
                             {"source": storage_script(origin, items), "worldName": RESTORE_WORLD}, session)
            await self._call("Page.navigate", {"url": url}, session)
            while True:
                try:
                    answer = await self._call(
                        "Runtime.evaluate", {"expression": "location.origin", "returnByValue": True}, session)
                    value = (answer.get("result") or {}).get("value")
                except Exception:  # the document is being swapped
                    value = None
                if value == origin:
                    ok = True
                    break
                if self._clock() >= deadline:
                    break
                await self._sleep(POLL_SECONDS)
        finally:
            # Detaching the gate's session removes its new-document script.
            try:
                await self._call("Target.detachFromTarget", {"sessionId": session}, None)
            except Exception:
                pass
        return target, ok
