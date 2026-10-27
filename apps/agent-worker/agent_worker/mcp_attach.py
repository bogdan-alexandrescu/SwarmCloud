"""The MCP attach, the worker's half (docs/design/registered-mcp-servers.md
§4.6 and §4.7; lane MCP-WORKER, part of #1433).

Once per attempt, AFTER the workspace secrets release and BEFORE the runner's
environment is built, the worker asks swarm-api for the registered MCP
servers this attempt's SIGNED input names (`input.mcp_servers`), with their
URLs, rules and credentials:

    POST /v1/attempts/{attempt_id}/mcp:attach      body {generation}, nothing else

signed with the attempt key in this heap under ATTACH_PURPOSE, so the agent --
same uid, same ID token -- can neither ask nor choose. swarm-api reads the
names from the signed spec; nothing here names a server.

ONLY WHEN THERE IS SOMETHING TO ASK FOR: a profile with no worker action
whose input names at least one server. While SWARM_MCP_SERVERS is off,
submission refuses every `mcp_servers` (`mcp_unavailable`), so no task names
one and nothing here runs: the feature ships dark.

WHAT THE ANSWER BECOMES:

  * every credential is registered with the logger THE MOMENT THE ANSWER IS
    PARSED, before it is validated, so even an item refused here is redacted
    everywhere, the publish-time scan included (`Worker._leaks_in_added_text`
    refuses a diff that echoes a registered value);
  * each server is validated again here (defence in depth): a name the input
    named, once; an `https://` URL on 443 whose host is a DNS name, with no
    userinfo, query or fragment; a header name that is not one of MCP's or
    HTTP's own; a header-safe credential; well-formed tool names. A server
    that fails is treated as withheld;
  * the credentials go to `mcp_relay.Relay`, in this process's heap. The
    runner is handed only RELAY_ENV, the path of a 0600 file under
    `private/` (never checkpointed, never uploaded) holding the relay's
    loopback URLs, the servers' descriptions and the per-attempt LOCAL token
    -- never a credential.

WHEN IT FAILS (§4.7):

  * a 409 is the fence (invariant 5): `McpAttachFenced`, a
    `ChildSubmitFenced`, and the lifecycle stands down without the agent and
    without touching the lease;
  * a WITHHELD server fails the attempt before the agent runs
    (`mcp_server_withheld`): the caller named it because the agent needs it.
    Retryable only for `unreadable` (owner decision Q2: a retry is an attempt
    of the same task); `credential_expired` and every other reason are final
    -- EXCEPT a workflow step's per-run credential that lapsed
    (`Attached.lapsed_credentials`): its caller can re-store it (BGD
    2026-10-26, registered-mcp-servers.md §4.10), so the step parks
    CREDENTIAL_MISSING with `credential: "mcp"` instead;
  * swarm-api unreachable or refusing is `mcp_attach_unavailable`, before the
    agent, retryable when the refusal says so.

CLOSED ON EVERY EXIT: `close` stops the relay, removes the relay file and
drops every credential reference (`lifecycle.Worker._cleanup`).
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from swarm_common.models import MCP_SERVER_NAME
from swarm_common.profiles import RunnerProfile

from . import mcp_relay as relay_mod
from .children import ChildApiError, ChildSubmitFenced
from .handoff import sign_headers
from .secretrelease import _write_private

#: swarm-api's purpose for this route (`swarm_api.routes.attempts_mcp.
#: ATTACH_PURPOSE`), restated: this image does not carry swarm-api.
#: tests/unit/worker/test_mcp_attach.py holds the two equal.
ATTACH_PURPOSE = "swarm-mcp-attach/v1"

#: The runner's one variable: the relay file's PATH.
RELAY_ENV = "SWARM_MCP_RELAY"
RELAY_FILE = "mcp-relay.json"

#: §4.3: at most four servers per step (swarm-api attaches no more).
MAX_SERVERS = 4

#: The causes an attempt fails with before its agent runs.
WITHHELD = "mcp_server_withheld"
UNAVAILABLE = "mcp_attach_unavailable"

#: Withheld reasons a later attempt may not meet (owner decision Q2).
RETRYABLE_REASONS = frozenset({"unreadable"})

#: Withheld reasons that are the per-run credential alone: swarm-api's
#: `mcp_credentials.CREDENTIAL_EXPIRED` and `CREDENTIAL_MISSING`, restated
#: (this image does not carry swarm-api); tests/unit/worker/test_mcp_attach.py
#: holds them equal. A workflow's caller can re-store such a credential.
CREDENTIAL_EXPIRED = "credential_expired"
CREDENTIAL_MISSING = "credential_missing"
CREDENTIAL_REASONS = frozenset({CREDENTIAL_EXPIRED, CREDENTIAL_MISSING})

NO_KEY = "no_attempt_key"
NOT_ANSWERED = "not_answered"

#: swarm-api's registration rules (`swarm_api.mcp_servers`), restated for the
#: defence-in-depth check.
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
_FORBIDDEN_HEADERS = frozenset(h.lower() for h in (
    "Host", "Cookie", "Content-Length", "Content-Type", "Accept", "Transfer-Encoding",
    "Connection", "Mcp-Session-Id", "MCP-Protocol-Version", "Proxy-Authorization",
    "Last-Event-ID", "User-Agent", relay_mod.TASK_HEADER, relay_mod.ATTEMPT_HEADER))
_SCHEMES = ("Bearer", "")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_MAX_TOOLS = 64
_MAX_URL = 512
_MAX_DESCRIPTION = 280
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class McpAttachFenced(ChildSubmitFenced):
    """409 from the attach: this attempt is superseded (invariant 5)."""


def requested_servers(inputs: Mapping[str, Any] | None) -> list[str]:
    """The server names the input names, in order, once each, as spelt by
    the contract (`MCP_SERVER_NAME`); swarm-api reads the same list from the
    signed spec."""
    raw = inputs.get("mcp_servers") if isinstance(inputs, Mapping) else None
    if not isinstance(raw, list):
        return []
    names = [n for n in raw if isinstance(n, str) and MCP_SERVER_NAME.fullmatch(n)]
    return list(dict.fromkeys(names))[:MAX_SERVERS]


def wants_attach(profile: RunnerProfile, inputs: Mapping[str, Any] | None) -> bool:
    return profile.worker_action is None and bool(requested_servers(inputs))


@dataclass
class Attached:
    """What one attempt was attached."""

    servers: list[relay_mod.Server] = field(default_factory=list)
    #: `(name, reason)` for every named server the agent will not have.
    withheld: list[tuple[str, str]] = field(default_factory=list)
    #: None when swarm-api answered; else why not.
    unavailable: str | None = None
    unavailable_retryable: bool = False
    asked: bool = False

    def failure(self) -> tuple[str, str, bool] | None:
        """`(cause, detail, retryable)` when the attempt may not start; None
        when every named server is attached (or none was named)."""
        if self.unavailable is not None:
            return UNAVAILABLE, self.unavailable, self.unavailable_retryable
        if self.withheld:
            detail = ",".join(f"{name}:{reason}" for name, reason in self.withheld)
            retryable = all(reason in RETRYABLE_REASONS for _, reason in self.withheld)
            return WITHHELD, detail, retryable
        return None

    def lapsed_credentials(self) -> list[tuple[str, str]]:
        """`(name, reason)` for each withheld server when EVERY one was
        withheld for its per-run credential alone (expired, destroyed or
        missing) and the attach itself answered; else empty. Only then can a
        re-stored credential make the attempt startable."""
        if self.unavailable is not None or not self.withheld:
            return []
        if all(reason in CREDENTIAL_REASONS for _, reason in self.withheld):
            return list(self.withheld)
        return []


def _dns_host(host: str | None) -> bool:
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    labels = host.split(".")
    return len(labels) >= 2 and all(_LABEL.fullmatch(label) for label in labels) \
        and not labels[-1].isdigit()


def server_from_answer(item: Any, wanted: list[str]) -> tuple[relay_mod.Server | None, str]:
    """One answer item as the relay's `Server`, or `(None, why)`."""
    if not isinstance(item, Mapping):
        return None, "shape"
    name = item.get("name")
    if not isinstance(name, str) or name not in wanted:
        return None, "name"
    url = item.get("url")
    if not isinstance(url, str) or len(url) > _MAX_URL:
        return None, "url"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None, "url"
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "@" in parts.netloc
            or port not in (None, relay_mod.UPSTREAM_PORT) or not _dns_host(host)):
        return None, "url"
    auth = item.get("auth")
    if not isinstance(auth, Mapping):
        return None, "auth"
    header, scheme, credential = auth.get("header"), auth.get("scheme"), auth.get("credential")
    if credential is not None:
        if not relay_mod.header_safe(credential):
            return None, "credential"
        if not isinstance(header, str) or not _HEADER_NAME.fullmatch(header) \
                or header.lower() in _FORBIDDEN_HEADERS:
            return None, "header"
        if scheme not in _SCHEMES:
            return None, "scheme"
    tools = item.get("allowed_tools") if item.get("allowed_tools") is not None else []
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS or not all(
            isinstance(t, str) and _TOOL_NAME.fullmatch(t) for t in tools):
        return None, "allowed_tools"
    description = item.get("description")
    description = " ".join(description.split())[:_MAX_DESCRIPTION] \
        if isinstance(description, str) else ""
    revision = item.get("revision")
    return relay_mod.Server(
        name=name, url=url, host=host, path=parts.path or "/",
        header=header if isinstance(header, str) else "Authorization",
        scheme=scheme if isinstance(scheme, str) else "Bearer",
        description=description,
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool)
        else None,
        allowed_tools=frozenset(tools),
        limits=relay_mod.Limits.from_answer(item.get("limits")),
        credential=credential,
    ), ""


class McpAttachment:
    """The attach and the relay for one attempt. `children` is its
    `ChildPath`, whose key and API this signs with and calls."""

    def __init__(self, cfg: Any, *, children: Any, logger: Any,
                 wall: Callable[[], float] = time.time,
                 relay_factory: Callable[..., relay_mod.Relay] = relay_mod.Relay) -> None:
        self.cfg = cfg
        self.log = logger
        self._children = children
        self._wall = wall
        self._relay_factory = relay_factory
        self.result: Attached | None = None
        self.relay: relay_mod.Relay | None = None
        self.path: Path | None = None
        self._fenced = False

    def route(self) -> str:
        return f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/mcp:attach"

    @property
    def env(self) -> dict[str, str]:
        return {RELAY_ENV: str(self.path)} if self.relay is not None and self.path else {}

    def attach(self, *, profile: RunnerProfile, inputs: Mapping[str, Any] | None) -> Attached:
        """Ask once; raises only `McpAttachFenced`."""
        if self.result is not None:
            return self.result
        result = Attached()
        self.result = result
        if not wants_attach(profile, inputs):
            return result
        wanted = requested_servers(inputs)
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None or not getattr(self._children, "keyed", False):
            return self._unavailable(result, NO_KEY, retryable=False)
        result.asked = True
        body = json.dumps({"generation": int(self.cfg.generation)}).encode("utf-8")
        route = self.route()
        try:
            headers = sign_headers(key, ATTACH_PURPOSE, "POST", route, body, now=self._wall())
            _, payload = api.call("POST", route, body=body, headers=headers)
        except ChildApiError as exc:
            if exc.status == 409:
                self._fenced = True
                raise McpAttachFenced(exc.status, exc.code, exc.message,
                                      retryable=False) from None
            status = exc.status
            retryable = exc.retryable is not False and not (
                isinstance(status, int) and 400 <= status < 500 and status != 429)
            return self._unavailable(result, exc.code or "swarm_api_unreachable",
                                     retryable=retryable)
        items = payload.get("servers") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            return self._unavailable(result, "bad_answer", retryable=True)
        # FIRST, before anything reads or refuses an item: each value is now
        # in this heap, so it is redacted everywhere from here on.
        for item in items:
            auth = item.get("auth") if isinstance(item, Mapping) else None
            value = auth.get("credential") if isinstance(auth, Mapping) else None
            if isinstance(value, str) and value:
                self.log.register_secret(value)
        seen: set[str] = set()
        for item in items:
            server, why = server_from_answer(item, wanted)
            if server is not None and server.name in seen:
                server, why = None, "duplicate"
            if server is None:
                name = item.get("name") if isinstance(item, Mapping) else None
                shown = name if isinstance(name, str) and name in wanted else None
                self.log.warning("mcp server refused by this worker", name=shown, reason=why)
                if shown is not None and shown not in seen:
                    seen.add(shown)
                    result.withheld.append((shown, f"refused_by_worker_{why}"))
                continue
            seen.add(server.name)
            result.servers.append(server)
        withheld = payload.get("withheld") if isinstance(payload.get("withheld"), list) else []
        for entry in withheld:
            name = entry.get("name") if isinstance(entry, Mapping) else None
            reason = entry.get("reason") if isinstance(entry, Mapping) else None
            if isinstance(name, str) and name in wanted and name not in seen:
                seen.add(name)
                result.withheld.append(
                    (name, reason if isinstance(reason, str) and re.fullmatch(
                        r"[a-z_]{1,40}", reason) else "unspecified"))
        for name in wanted:
            if name not in seen:
                result.withheld.append((name, NOT_ANSWERED))
        self.log.info("mcp servers attached", names=[s.name for s in result.servers],
                      withheld=[f"{n}:{r}" for n, r in result.withheld])
        return result

    def _unavailable(self, result: Attached, why: str, *, retryable: bool) -> Attached:
        result.unavailable = why
        result.unavailable_retryable = retryable
        self.log.warning("mcp servers not attached", reason=why, retryable=retryable)
        return result

    def start(self, private_dir: Path) -> Path | None:
        """Serve the relay for the attached servers and write the relay file;
        its path, or None when nothing was attached. Raises OSError."""
        if self.result is None or not self.result.servers or self.relay is not None:
            return self.path
        relay = self._relay_factory(self.result.servers, logger=self.log,
                                    task_id=self.cfg.task_id, attempt_id=self.cfg.attempt_id)
        if self._fenced:
            relay.mark_fenced()
        try:
            relay.start()
            path = Path(private_dir) / RELAY_FILE
            _write_private(path, json.dumps({"token": relay.token,
                                             "servers": relay.endpoints()},
                                            indent=2, sort_keys=True))
        except OSError:
            relay.close()
            raise
        self.relay = relay
        self.path = path
        self.log.info("mcp relay serving", names=relay.names)
        return path

    def mark_fenced(self) -> None:
        """Invariant 5: the lifecycle saw the fence; nothing more goes out."""
        self._fenced = True
        if self.relay is not None:
            self.relay.mark_fenced()

    def close(self) -> None:
        """Stop the relay, remove its file, drop every credential reference.
        Never raises."""
        relay, self.relay = self.relay, None
        if relay is not None:
            relay.close()
        if self.result is not None:
            self.result.servers.clear()
        path, self.path = self.path, None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "ATTACH_PURPOSE", "MAX_SERVERS", "RELAY_ENV", "RELAY_FILE", "RETRYABLE_REASONS",
    "UNAVAILABLE", "WITHHELD", "Attached", "McpAttachFenced", "McpAttachment",
    "requested_servers", "server_from_answer", "wants_attach",
]
