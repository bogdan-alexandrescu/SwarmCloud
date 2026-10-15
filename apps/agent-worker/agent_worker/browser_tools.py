"""Which browser tool a live-browser agent gets, and how its use is seen (BSTATUS-API; part of #1030).

Owner decision 2026-10-14, "Picker on Submit, Auto by default". A
`claude-code-browser` task names one of four options in `input.browser_tool`
(contract request 77): a NAME, never a command (invariant 10). This module is
the one place in the worker that says what each name means:

  auto         every tool this attempt has, each announced with its strength
  playwright   the Playwright MCP server only
  dev-browser  the dev-browser CLI first; Playwright MCP still registered as fallback
  jev          the jev MCP server first; Playwright MCP still registered as fallback

Jev is only ever registered with its wrapper in the image and what its
judgment backend needs: a TypeSafe key, or for an internal backend the route
to Saga's decision API (`JEV_API_URL`, which the dispatcher sends only while
the tailnet egress is on). Otherwise the attempt runs without it and says
why (`no_key`, `not_installed`, `no_route`; JEV-OFF, #1345: a server the
wrapper refuses to start was registered, failed, and nothing said so). swarm-api refuses a `jev` submission that has
no key to use (`browser_tool_unavailable`), but a key deleted between
submission and run must not cost the attempt, so the runner falls back here
rather than failing.

WHAT THE CONSOLE IS TOLD, AND BY WHOM. Three parties each know one part:

  * the RUNNER knows what it registered. It writes `browser.json` into the
    hand-off spool (`write_state`), once, before the CLI starts;
  * the WORKER's hand-off poll forwards that file to the gate over ctl.sock
    (`browser_tools`), checked again here (`clean_state`): the spool is in the
    agent's reach, so the file is untrusted;
  * the GATE knows which tool connected last, because each tool connects to
    its CDP proxy with a CLIENT TAG on the endpoint (`tag_endpoint`), and the
    egress state. It puts all of it in its report as `browser`, which the
    relay passes on and swarm-api checks key by key.

THE CLIENT TAG is a path prefix, `http://127.0.0.1:9222/client/<tool>`, not a
query: a tool that builds its `/json/version` URL by string concatenation
(older Playwright did) turns `...?client=x` into `?client=x/json/version/`,
while a path survives both concatenation and the URL API. The gate echoes the
tag into the websocket URL it hands back as `?client=<tool>`, and strips it
before anything reaches Chromium (cdpproxy `split_client`). A connection with
no tag, or a tag outside `TOOLS`, is served as before and attributed to no tool.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

#: The tools, in the order a note lists them. `swarm_common.profiles`'s
#: `browser_tool` choices are `("auto", *TOOLS)`; the parity script holds the
#: two equal (scripts/lib/check-contract-parity.sh, section 16).
TOOLS = ("playwright", "dev-browser", "jev")
AUTO = "auto"
SELECTIONS = (AUTO, *TOOLS)
#: The task input key (contract request 77).
INPUT_KEY = "browser_tool"

#: Why jev is not registered on an attempt.
NO_KEY = "no_key"
NOT_INSTALLED = "not_installed"
NOT_SELECTED = "not_selected"
NO_ROUTE = "no_route"
JEV_REASONS = (NO_KEY, NOT_INSTALLED, NOT_SELECTED, NO_ROUTE)

#: The file the runner writes into the hand-off spool, and the gate's op.
STATE_NAME = "browser.json"
CTL_OP = "browser_tools"

#: The client tag's path segment (module docstring).
CLIENT_SEGMENT = "client"
_TAGGED_ENDPOINT = re.compile(r"(?P<base>https?://[^/?#]+)/?")


def selection(raw: Any) -> str:
    """`input.browser_tool` as one of `SELECTIONS`; anything else is `auto`.

    swarm-api refuses a name outside the enum at submission, so the fallback
    is only for a task stored before a name was retired (contract request 77,
    Rollback)."""
    return raw if isinstance(raw, str) and raw in SELECTIONS else AUTO


@dataclass(frozen=True)
class Choice:
    """What one attempt registers: the selection, the tools in the order the
    note names them (the selected one first), and jev's state."""

    selected: str
    tools: tuple[str, ...]
    jev_enabled: bool
    jev_reason: str | None

    def state(self) -> dict[str, Any]:
        """`browser.json`, and the gate's `browser_tools` op."""
        return {
            "tool_selected": self.selected,
            "tools_available": list(self.tools),
            "jev": {"enabled": self.jev_enabled, "reason": self.jev_reason},
        }


def choose(selected: Any, *, jev_installed: bool, jev_key: bool, jev_route: bool = True) -> Choice:
    """The tools to register for `selected`, given whether jev can run at all.

    `jev_route` is False only for an internal judgment backend whose pod has
    no `JEV_API_URL`: the wrapper would refuse to start, so jev is not
    registered and the reason is `no_route`.

    Playwright MCP is registered on every choice: it is the only tool for
    `playwright`, and the fallback for `dev-browser` and `jev`. dev-browser is
    a CLI on PATH, not a registration, so "registered" for it means the note
    names it; with `playwright` or `jev` the note does not.
    """
    chosen = selection(selected)
    jev_ready = jev_installed and jev_route and jev_key
    if chosen in (AUTO, "jev") and not jev_ready:
        reason: str | None = NOT_INSTALLED if not jev_installed else NO_ROUTE if not jev_route else NO_KEY
    elif chosen in ("playwright", "dev-browser"):
        reason = NOT_SELECTED
    else:
        reason = None
    jev = reason is None
    if chosen == "playwright":
        tools: tuple[str, ...] = ("playwright",)
    elif chosen == "dev-browser":
        tools = ("dev-browser", "playwright")
    elif chosen == "jev":
        tools = ("jev", "playwright") if jev else ("playwright",)
    else:
        tools = ("playwright", "dev-browser", *(("jev",) if jev else ()))
    return Choice(chosen, tools, jev, reason)


def tag_endpoint(endpoint: str, tool: str) -> str:
    """`endpoint` (the dispatcher's `http://127.0.0.1:9222`) with `tool`'s client tag.

    An endpoint that is not a bare http origin is returned unchanged: the tag
    is attribution, never something to break a connection over."""
    if tool not in TOOLS:
        raise ValueError(f"{tool!r} is not a browser tool")
    found = _TAGGED_ENDPOINT.fullmatch(endpoint or "")
    if found is None:
        return endpoint
    return f"{found.group('base')}/{CLIENT_SEGMENT}/{tool}"


def clean_state(raw: Any) -> dict[str, Any] | None:
    """`raw` as a `Choice.state()` that holds together, else None.

    Read by the worker off the spool (the agent can write there) and by the
    gate off ctl.sock: exact keys, names from the enum, no repeats, the
    selection's own constraints (a `playwright` attempt registered nothing
    else), and a jev state that agrees with the tools."""
    if not isinstance(raw, Mapping) or set(raw) != {"tool_selected", "tools_available", "jev"}:
        return None
    selected, tools, jev = raw["tool_selected"], raw["tools_available"], raw["jev"]
    if selected not in SELECTIONS or not isinstance(tools, list) or not tools:
        return None
    if len(tools) > len(TOOLS) or len(set(tools)) != len(tools) \
            or not all(isinstance(t, str) and t in TOOLS for t in tools):
        return None
    if "playwright" not in tools or (selected == "playwright" and tools != ["playwright"]):
        return None
    if not isinstance(jev, Mapping) or set(jev) != {"enabled", "reason"} \
            or not isinstance(jev["enabled"], bool):
        return None
    reason = jev["reason"]
    if jev["enabled"] != ("jev" in tools) or (jev["enabled"] and reason is not None) \
            or (not jev["enabled"] and reason not in JEV_REASONS):
        return None
    return {"tool_selected": selected, "tools_available": list(tools),
            "jev": {"enabled": jev["enabled"], "reason": reason}}


def write_state(spool: str | os.PathLike[str], choice: Choice) -> Path | None:
    """`browser.json` in the hand-off spool, atomically; None without a spool. Never raises."""
    if not spool:
        return None
    root = Path(spool)
    target = root / STATE_NAME
    tmp = root / f".{STATE_NAME}.tmp"
    try:
        if not root.is_dir():
            return None
        tmp.unlink(missing_ok=True)
        tmp.write_text(json.dumps(choice.state(), separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        return None
    return target


#: The largest `browser.json` read: the state is under 200 bytes.
MAX_STATE_BYTES = 1024


def read_state(spool: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The spool's `browser.json`, cleaned, or None. Never follows a link. Never raises."""
    path = Path(spool) / STATE_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
            return None
        return clean_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
