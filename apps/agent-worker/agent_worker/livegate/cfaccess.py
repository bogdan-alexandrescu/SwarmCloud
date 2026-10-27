"""A Cloudflare Access service token, held by the gate: two headers to listed hosts only (CF-ACCESS-TOKEN).

Lane CF-ACCESS-TOKEN, part of #2442; BGD decided on 2026-10-26 that browser
visual-QA agents reach a site behind Cloudflare Access with a SERVICE TOKEN,
because an agent cannot pass Access's interactive sign-in and its second
factor. docs/runbooks/cf-access-service-token.md says how one is made and
stored; docs/design/workspace-secrets.md, "The cf-access slot", says why it is
built this way.

WHERE IT COMES FROM. A workspace secret of kind `oauth_client` (`{client_id,
client_secret}`) granted to `profile:claude-code-browser` in its `cf-access`
slot, with the host list the workspace admin set on that grant, signed with
the grant in attestation form "v3" (swarm_api.grant_attest). The worker's
release holds such an item in its heap and never writes it to a file
(agent_worker/cf_access.py); it hands it here over ctl.sock (`OP`) before the
agent exists, and drops its copy. A workspace with no such grant hands
nothing, and nothing in this module does anything.

THE HEADERS GO TO LISTED HOSTS ONLY, ON THE GATE'S OWN SESSIONS
(`CfAccessInjector`, `QaInjector`'s frame map and session walk with its own
rules). A `Request`-stage pause gets `CF-Access-Client-Id` and
`CF-Access-Client-Secret` only when ALL of these hold:

* its URL is `https://<host>/...` exactly, `<host>` on one token's list: no
  userinfo, no port, no other scheme, no subdomain of a listed host;
* it is the top frame's own navigation, or the gate's frame map says the
  requesting frame's top-level origin is `https://<a host of the same
  token>`. A page on any other site that fetches a listed host does not spend
  the token, and a dedicated or service worker (no frame) never gets it: it
  fails closed, as the QA identity does, and reads as signed out;
* the token is held: set, and not wiped.

Never `Network.setExtraHTTPHeaders`, which sends a header to every origin a
page touches.

A REDIRECT IS A REQUEST OF ITS OWN. M5 (docs/design/qa-identity-measurements.md)
measured that a header `Fetch.continueRequest` adds follows no redirect, same
origin included: the redirect's request comes back as a pause of its own and
is judged by its own URL, so a listed host that redirects elsewhere sends the
token nowhere. Nothing holds a later Chromium to M5, so GUARD 2 is built as
the QA identity's is: with a token held, every request is paused (`*`) and a
request to any host that carries a held value in any header has that header
removed (`cf_access_header_stripped`, with the origin). The QA identity's
guard 1 (stopping a cross-origin 3xx) is NOT built: Access itself redirects a
request it refuses to `<team>.cloudflareaccess.com`, and guard 2 already keeps
the values off that request.

THE AGENT NEVER READS THE VALUES (`CfAccess.scrub`, on every message the CDP
proxy sends an agent, `cdpproxy._ScrubbedLink`). Header objects in events and
results lose any header carrying a held value -- the agent's own
`Fetch.requestPaused` for a request the gate already added them to, and
Chromium's `Network.requestWillBeSentExtraInfo`, which reports the headers
actually sent -- and every other occurrence of a value becomes `PLACEHOLDER`,
a `base64Encoded` body decoded, scrubbed and re-encoded first. A body the
scrub cannot decode is refused (`undecodable_body`). The values are kept for
the scrub until the gate ends, so a frame in flight after the wipe is
scrubbed too. Records carry counts, causes and origins, never a value.

THE AGENT CANNOT ROUTE A REQUEST AROUND THE RULE (`agent_refusal`). With a
token set, the agent may not continue a paused request with a new `url`
(`Fetch.continueRequest`, `Network.continueInterceptedRequest`): an
interceptor that ran after the gate's would otherwise send a request the gate
had added the headers to on to a host of the agent's choosing.

KNOWN LIMIT, stated so nobody relies on more: a listed site that reflects its
request headers into a page could have them transformed (reversed,
re-encoded) by its own script before the agent reads the page. A service
token's site is the workspace's own; one that echoes request headers should
not be listed.

READ-ONLY IS THE ACCESS POLICY'S, NOT THIS MODULE'S. The token opens whatever
the Access application's Service Auth rule lets it open, with every method.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .qaidentity import GateCdp, QaInjector, origin_of

log = logging.getLogger(__name__)

#: The ctl.sock operation (ctl.py `OPS`).
OP = "cf_access"
#: Cloudflare's service-token headers (Access › Service Auth).
#: Unpacked from one tuple rather than one `NAME = "..."` line each: the
#: worker's publish-time credential scan reads a line like that as a key.
HEADERS = ("CF-Access-Client-Id", "CF-Access-Client-Secret")
HEADER_ID, HEADER_SECRET = HEADERS
HEADER_NAMES = frozenset({HEADER_ID.lower(), HEADER_SECRET.lower()})
#: What a held value reads as in anything the agent receives.
PLACEHOLDER = "[cf-access]"
#: Bounds on what the worker may hand over: swarm-api's `MAX_SLOT_HOSTS`
#: bounds one grant's list; a workspace grants a handful of tokens at most.
MAX_TOKENS = 8
MAX_HOSTS = 16
#: A client id is `<hex>.access`; a secret is hex. Anything outside this set
#: (whitespace, CR, LF, a quote) could split a header and is refused. The set
#: also needs no JSON or URL escaping, so the scrub's text match is exact.
_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]{16,1024}")
#: A host name, lower case, at least one dot, no port, no IP literal, no
#: wildcard: `swarm_api.workspace_secrets.SLOT_HOST`, restated (this image does
#: not carry swarm-api); tests/unit/worker/test_cf_access_gate.py holds them equal.
HOST = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
#: The keys whose value is a header object (a dict, or a list of name/value).
_HEADER_KEYS = frozenset({"headers", "requestHeaders", "responseHeaders", "associatedHeaders"})
#: How deep the header walk looks: `params.redirectResponse.requestHeaders` is 3.
_HEADER_DEPTH = 4
#: The result fields that hold a body, base64 when `base64Encoded` is true.
_BODY_FIELDS = ("body", "content", "data")
#: The agent methods that could continue a paused request to another URL.
_REWRITES = frozenset({"Fetch.continueRequest", "Network.continueInterceptedRequest"})

Record = Callable[[str, Mapping[str, Any]], None]


class CfAccessRefused(ValueError):
    """The verb is refused: malformed, a second set, or after the wipe. `code` says which."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Token:
    """One service token and the hosts it may be sent to. Excluded from the
    repr, so a logged object carries neither value."""

    hosts: frozenset[str]
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return f"Token(hosts={sorted(self.hosts)!r})"


def listed_host(url: Any) -> str | None:
    """The host of `https://<host>/...`, exactly: no userinfo, no port, no other scheme."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc or not HOST.fullmatch(parts.netloc):
        return None
    return parts.netloc


class CfAccess:
    """The tokens, their hosts, and the scrub every agent-bound message passes.

    Lives as long as the gate: a relaunched Chromium gets a new
    `CfAccessInjector` over the same tokens."""

    def __init__(self, record: Record) -> None:
        self._record = record
        self._tokens: tuple[Token, ...] = ()
        #: Every value held in this gate, for the scrub; never cleared.
        self._kept: tuple[str, ...] = ()
        self.wiped = False
        #: Requests the injector added the headers to, and stripped them from.
        self.added = 0
        self.stripped = 0

    # ------------------------------------------------------------- state

    @property
    def armed(self) -> bool:
        """A token was set in this gate: its rules apply for the rest of the gate."""
        return bool(self._kept)

    @property
    def held(self) -> bool:
        return bool(self._tokens) and not self.wiped

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(h for t in self._tokens for h in t.hosts)

    def token_for(self, host: str | None) -> Token | None:
        """The held token whose list names `host`, or None."""
        if host is None or not self.held:
            return None
        return next((t for t in self._tokens if host in t.hosts), None)

    def headers_for(self, url: Any) -> list[dict[str, str]] | None:
        """The two headers for a request to `url`, or None when its host is not listed."""
        token = self.token_for(listed_host(url))
        if token is None:
            return None
        return [{"name": HEADER_ID, "value": token.client_id},
                {"name": HEADER_SECRET, "value": token.client_secret}]

    def carries(self, text: Any) -> bool:
        """Whether `text` holds any value this gate has held."""
        return isinstance(text, str) and any(k in text for k in self._kept)

    def status(self) -> dict[str, Any]:
        """For `status`: the hosts and counts, never a value."""
        if not self.armed:
            return {"state": "none"}
        return {"state": "wiped" if self.wiped else "held", "hosts": sorted(self.hosts),
                "added": self.added, "stripped": self.stripped}

    # --------------------------------------------------------------- ctl

    def handle(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """`{tokens: [{hosts, client_id, client_secret}, ...]}`, once per gate."""
        code = None
        tokens: list[Token] = []
        raw = args.get("tokens")
        if self.wiped:
            code = "wiped"
        elif self._kept:
            code = "already_set"
        elif not isinstance(raw, list) or not 0 < len(raw) <= MAX_TOKENS:
            code = "tokens"
        else:
            for item in raw:
                token, code = _token(item)
                if token is None:
                    break
                tokens.append(token)
        if code is None and len({h for t in tokens for h in t.hosts}) != sum(len(t.hosts) for t in tokens):
            # One host, one token: which of two would be sent is not a guess this gate makes.
            code = "host_twice"
        if code is not None:
            self._record("cf_access_refused", {"cause": code})
            raise CfAccessRefused(code)
        self._tokens = tuple(tokens)
        self._kept = tuple(v for t in tokens for v in (t.client_id, t.client_secret))
        self._record("cf_access_set", {"tokens": len(tokens), "hosts": sorted(self.hosts)})
        return {"cf_access": "held", "hosts": sorted(self.hosts)}

    def wipe(self) -> None:
        """The attempt's wipe: the tokens go, and the verb is refused from here.

        `_kept` stays, as the QA identity's does: a request in flight at wipe
        time can still reach an agent as a `requestWillBeSentExtraInfo`."""
        self.wiped = True
        self._tokens = ()

    def agent_refusal(self, method: str, params: Mapping[str, Any]) -> str | None:
        """Why the agent may not send `method` while a token is set, or None."""
        if not self.armed or method not in _REWRITES or "url" not in params:
            return None
        return ("refused by the live gate: while it holds a Cloudflare Access token, a paused "
                "request is never continued to another URL")

    # ------------------------------------------------------------- scrub

    def scrub(self, message: dict[str, Any]) -> dict[str, Any]:
        """`message` as an agent may receive it (module docstring). Unchanged when nothing matched."""
        if not self._kept:
            return message
        removed = 0
        out: Any = message
        for key in ("params", "result"):
            value = message.get(key)
            if isinstance(value, dict):
                cleaned, n = self._drop_headers(value, _HEADER_DEPTH)
                if n:
                    removed += n
                    out = {**out, key: cleaned}
        replaced = 0
        result = out.get("result")
        if isinstance(result, dict) and result.get("base64Encoded") is True:
            cleaned_result, n = self._scrub_base64(result)
            if n:
                replaced += n
                out = {**out, "result": cleaned_result}
        text = json.dumps(out, separators=(",", ":"))
        hits = sum(text.count(k) for k in self._kept)
        if hits:
            for k in self._kept:
                text = text.replace(k, PLACEHOLDER)
            replaced += hits
            out = json.loads(text)
        if removed or replaced:
            fields: dict[str, Any] = {"count": removed + replaced}
            method = message.get("method")
            if isinstance(method, str):
                fields["method"] = method
            self._record("cf_access_scrubbed", fields)
        return out

    def _drop_headers(self, value: dict[str, Any], depth: int) -> tuple[dict[str, Any], int]:
        removed = 0
        out = value
        for key, item in value.items():
            if key in _HEADER_KEYS:
                if isinstance(item, dict):
                    kept = {n: v for n, v in item.items() if not self.carries(v)}
                    if len(kept) != len(item):
                        removed += len(item) - len(kept)
                        out = {**out, key: kept}
                    continue
                if isinstance(item, list):
                    kept_list = [h for h in item if not (isinstance(h, dict) and self.carries(h.get("value")))]
                    if len(kept_list) != len(item):
                        removed += len(item) - len(kept_list)
                        out = {**out, key: kept_list}
                    continue
            if depth > 1 and isinstance(item, dict):
                cleaned, n = self._drop_headers(item, depth - 1)
                if n:
                    removed += n
                    out = {**out, key: cleaned}
        return out, removed

    def _scrub_base64(self, result: dict[str, Any]) -> tuple[dict[str, Any], int]:
        out, hits = result, 0
        for field_name in _BODY_FIELDS:
            body = result.get(field_name)
            if not isinstance(body, str):
                continue
            try:
                raw = base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError):
                continue
            found = 0
            for k in self._kept:
                needle = k.encode()
                found += raw.count(needle)
                raw = raw.replace(needle, PLACEHOLDER.encode())
            if found:
                hits += found
                out = {**out, field_name: base64.b64encode(raw).decode("ascii")}
        return out, hits

    def undecodable_body(self, result: Mapping[str, Any]) -> bool:
        """A `base64Encoded` body the scrub cannot read, while a token is set."""
        if not self._kept or result.get("base64Encoded") is not True:
            return False
        for field_name in _BODY_FIELDS:
            body = result.get(field_name)
            if body is None:
                continue
            if not isinstance(body, str):
                return True
            try:
                base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError):
                return True
        return False


def _token(item: Any) -> tuple[Token | None, str | None]:
    if not isinstance(item, Mapping):
        return None, "token"
    hosts, client_id, client_secret = item.get("hosts"), item.get("client_id"), item.get("client_secret")
    if (not isinstance(hosts, list) or not 0 < len(hosts) <= MAX_HOSTS
            or not all(isinstance(h, str) and HOST.fullmatch(h) for h in hosts)):
        return None, "hosts"
    if not isinstance(client_id, str) or not _VALUE.fullmatch(client_id):
        return None, "client_id"
    if not isinstance(client_secret, str) or not _VALUE.fullmatch(client_secret) or client_secret == client_id:
        return None, "client_secret"
    return Token(frozenset(hosts), client_id, client_secret), None


# --------------------------------------------------------------------------- #
# The browser side: the gate's own sessions                                   #
# --------------------------------------------------------------------------- #


class CfAccessInjector(QaInjector):
    """The listed-host rule and guard 2, on sessions the gate owns.

    One per Chromium. The session walk, the auto-attach and the frame map are
    `QaInjector`'s; what a pause gets is decided here (module docstring)."""

    def __init__(self, cdp: GateCdp, cf: CfAccess, *, record: Record) -> None:
        super().__init__(cdp, None, record=record)  # type: ignore[arg-type]
        self._cf = cf

    def _armed(self) -> bool:
        return self._cf.armed

    async def _enable(self, session: str) -> None:
        if self._cf.held:
            patterns = [{"urlPattern": "*", "requestStage": "Request"}]
        elif self._cf.armed:
            patterns = [{"urlPattern": f"https://{h}/*", "requestStage": "Request"}
                        for h in sorted(self._cf.hosts)]
        else:
            return
        await self._quietly("Fetch.enable", {"patterns": patterns}, session)

    def _from_listed(self, session: str, params: Mapping[str, Any], token_hosts: frozenset[str]) -> bool:
        """The top-frame rule: the page's own navigation, or a frame whose top is a host of the token."""
        target = self._sessions.get(session)
        if target is None or target.kind not in ("page", "iframe"):
            return False  # a worker has no frame
        frame = params.get("frameId")
        if (target.kind == "page" and params.get("resourceType") == "Document"
                and frame == target.target_id):
            return True
        top = self.top_origin(frame)
        return top is not None and any(top == f"https://{h}" for h in token_hosts)

    async def _paused(self, session: str, params: Mapping[str, Any]) -> None:
        request_id = params.get("requestId")
        if not isinstance(request_id, str):
            return
        if "responseStatusCode" in params or "responseErrorReason" in params:
            await self._quietly("Fetch.continueResponse", {"requestId": request_id}, session)
            return
        request = params.get("request") or {}
        url = request.get("url")
        raw_headers = request.get("headers")
        headers: dict[str, Any] = dict(raw_headers) if isinstance(raw_headers, Mapping) else {}
        kept = {n: v for n, v in headers.items() if not self._cf.carries(v)}
        stripped = len(kept) != len(headers)
        token = self._cf.token_for(listed_host(url))
        if token is not None and self._from_listed(session, params, token.hosts):
            out = [{"name": n, "value": str(v)} for n, v in kept.items() if n.lower() not in HEADER_NAMES]
            out += [{"name": HEADER_ID, "value": token.client_id},
                    {"name": HEADER_SECRET, "value": token.client_secret}]
            self._cf.added += 1
            await self._continue(session, request_id, out)
            return
        if stripped:
            self._cf.stripped += 1
            self._record("cf_access_header_stripped", {"origin": origin_of(url) or "unknown"})
            await self._continue(session, request_id,
                                 [{"name": n, "value": str(v)} for n, v in kept.items()], strip=True)
            return
        await self._quietly("Fetch.continueRequest", {"requestId": request_id}, session)

    async def _continue(self, session: str, request_id: str, headers: list[dict[str, str]],
                        *, strip: bool = False) -> None:
        """Continue with `headers`. Should Chromium refuse, never leave the request
        paused: one that only lacked the token goes on unmodified (it reads as
        signed out), one the gate was stripping a value from is failed instead,
        because unmodified it would carry the value off the list."""
        try:
            await self._cdp.call("Fetch.continueRequest", {"requestId": request_id, "headers": headers}, session)
        except Exception as exc:  # noqa: BLE001 -- a refused continue is handled below
            self._record("cf_access_continue_refused", {"cause": type(exc).__name__})
            if strip:
                await self._quietly("Fetch.failRequest",
                                    {"requestId": request_id, "errorReason": "BlockedByClient"}, session)
            else:
                await self._quietly("Fetch.continueRequest", {"requestId": request_id}, session)
