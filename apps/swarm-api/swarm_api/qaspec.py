"""The visual-QA workflow of any web application, rendered from its QA target,
and the check of the findings that workflow reports.

docs/design/visual-qa-any-app.md §4 (lane VQ-1 SPEC of
docs/design/agent-autonomy.md §6; part of #1933). PURE: no Firestore, no
client, no credential, no clock. `qa_targets` (lane VQ-2) reads a target,
calls `render`, stamps `qa_target_metadata` on the agents of `browser_steps`
and submits the spec like any other; the tests hold every rule below offline.

WHY THE TARGET, NOT THE CALLER, CARRIES EVERYTHING (§1.1, invariant 10). A
caller starts a QA run by naming a target. Every step is a `runner_profile`
by name with a prompt; no step's input carries the target, its URL as a field,
its sign-in or its credential. The worker asks swarm-api for its attempt's
target (§2.2), from metadata swarm-api wrote. So the spec this module renders
passes `swarm_mcp.workflows.read_spec` unchanged, and needs no frozen-contract
change: nothing new travels in a `RunnerInput`.

WHAT CHANGED FROM THE CONSOLE TEMPLATE (`docs/qa/console-qa.workflow.json`,
§4.2). No `npm ci`, no `npm run dev`, no `localhost`: the deployed app is
`--base`. The route list is the target's, written by the capture step from
its prompt, not `apps/swarm-ui/qa/routes.json`. `--settle generic` replaces
the console's pending-read wait; `--keep-storage` keeps an own-login app's
session; the clock is frozen only when the target sets one, because freezing
`Date` can expire an app's own session token. Everything else is the
template's: presets by name, the theme set twice, segments, the manifest,
the tar names, `on_step_failure: continue`, and a report that does not wait on
any flow, so a failed flow never costs the screen findings.

SAFETY THE RENDERER CAN HOLD (§3). The gate enforces read-only, the origin
lock and the rate limit (lane VQ-4); the prompt says the same so the agent
does not fight its own gate. What is decidable here is refused here, before
anything spends: a mutating flow on a production target, a screen on an
excluded path, a path that is not origin-relative, a base URL that is not a
plain https origin. No value from the target's `sign_in` or `credential` is
ever rendered but its start clicks' visible text, public by nature (§2.2):
the sign-in is the gate's, and the agent is told only what to click on the
chooser and what to run when it sees the login form again.

ONE LEG AT A TIME (docs/design/qa-environments.md §1.1, §1.5, §6; lane
QAENV-MODEL). A target may list its legs in `environments[]`. Every function
here reads ONE leg's view, `leg(target, name)`: the app's fields and that
entry's fields where a one-origin target keeps them, so each rule above
holds per leg unchanged, and a prompt names that leg's base URL and start
clicks only. A target with no `environments` is its own one leg and renders
byte for byte as before. Of a target with several, a `production` leg, and a
`non_production` leg sharing an origin with a `production` one, render no
mutating flow: a production leg reads only.

EVERY CAPTURE STEP DISCOVERS WHILE IT CAPTURES (docs/design/qa-environments.md
§3.1, §3.5; lane QAENV-RUN; BGD's Q3 (a), 2026-10-23). A capture step runs
`swarm_qa capture --crawl` (lane QAENV-CRAWL): its prompt carries the WHOLE
run's stored list, the same in every step, and its share is `--group`,
`--step` and `--steps`; after its own routes the tool walks the leg from its
base path and every stored route, and captures the new routes it owns by a
hash of the path, at most `found_per_step` (8, BGD's §10 answer), cut so the
leg's stored plus `G x found_per_step` stays within `MAX_SCREENS`. No path a
crawl found ever reaches a prompt: what a step finds goes to its own capture
and its `found.json`. A target that asks to `discover` and has nothing stored
renders `discover.slots` crawling steps (4 by default, at most 8) from the
base alone, instead of being refused; one with neither is still refused. The
report gains the leg's Coverage section (§3.5).

A LEG'S FLOWS RUN IN BATCHES (QA-FLOWS-CAP, part of #2136; BGD, 2026-10-26).
A target holds up to `MAX_FLOWS` (150) flows. Its read-only flows render as
ceil(n / flows_per_step) flow-batch steps, `flows-<k>`, each one agent that
signs in once and runs its batch's flows in order, every flow ending "STOP
and capture" with its own `flow-<id>.tar`; the step bundles them into
`flows-<k>.tar`, the one file it stages to `flow-report` (`input_from` stages
one file a step). A mutating flow keeps its own `flow-<id>` step, because
the gate's write grant reads that step id (`flow_batches` says why). The
batch size is the run's, else the workspace's, else 15 (`qasettings`); a leg
that would pass the workflow's step or input bounds is refused with the
batch size that fits.

A DISMISSED ROUTE IS SKIPPED BY LATER CRAWLS (lane QA-CRAWL-SKIP; BGD
2026-10-25, part of #2157). The target's `dismissed_routes` (S9b's Remove,
`qatargets.DISMISSED_FIELD`) travel in every capture step's route list as
`dismissed`: each route's `route_key` as sha256 hex, sorted, so the crawl
neither lists, loads nor captures one, and none spends a found-route slot.
Hashes, not paths: a dismissed route was found by a crawl, and no path a
crawl found reaches a prompt. A target with none writes no `dismissed`, so
its spec is byte for byte as before.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import ipaddress
import json
import math
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Sequence
from urllib.parse import urlsplit

from swarm_common.config import Settings
from swarm_common.profiles import RUNNER_PROFILES
from swarm_redaction import redact

# --------------------------------------------------------------------------
# Bounds and vocabularies
# --------------------------------------------------------------------------

#: At most 8 screens a capture step and 8 capture steps a run (§4.1): one
#: capture agent's half hour, and a run whose review fan-out stays readable.
MAX_GROUP_SCREENS = 8
MAX_GROUPS = 8
#: The discovery cap, and so the most screens one run captures (§4.1). Below
#: 8 x 8 on purpose: the discovered list is the one a run starts from.
MAX_SCREENS = 60
MAX_DISCOVER_PAGES = 60
MAX_DISCOVER_DEPTH = 3
#: A crawling capture step (§3.1; QAENV-RUN). New routes a step captures by
#: default: BGD answered 8 on 2026-10-23 (qa-environments.md §10), not the
#: design's 4, and 8 is also the ceiling (`swarm_qa.FOUND_PER_STEP`,
#: `MAX_FOUND_PER_STEP`). A target may set fewer in `discover.found_per_step`.
FOUND_PER_STEP = 8
MAX_FOUND_PER_STEP = 8
#: The crawling steps of a run with nothing stored (`discover.slots`, §3.1):
#: 4 by default, at most `MAX_GROUPS`, one capture agent each.
DEFAULT_SLOTS = 4
#: The crawl's own bound (`swarm_qa --crawl-seconds`, its default), and the
#: crawling step's timeout: 1,800 s grows to 2,700 s (§3.1), which holds the
#: tool's 900 s crawl ceiling beside the step's own captures.
CRAWL_SECONDS = 600
CRAWL_CAPTURE_TIMEOUT = 2700
#: `swarm_qa`'s own bound (`MAX_SEGMENTS`, beside `NAME`), which the capture
#: tool refuses past; restated so a target is refused at registration, not at run.
MAX_SEGMENTS = 12
#: The flows one target (each leg) may hold (QA-FLOWS-CAP, part of #2136).
#: BGD, 2026-10-26: AI Studio's ai-studio-staging needs 132 read-only
#: frame-state flows; 150 holds them with room. Stated HERE ONLY: a flow runs
#: as an agent's prompt, and the capture tool (`swarm_qa`) never reads one, so
#: there is no second statement to hold equal (docs/mirrored-values.md).
MAX_FLOWS = 150
#: How many read-only flows one flow-batch step runs (`flows_per_step`): the
#: run's own value (`:run`'s `flows_per_step`), else the workspace's
#: `qa_flows_per_step` (`qasettings`), else 15 (BGD, 2026-10-26). One agent
#: signs in once and runs its batch's flows in order. 30 at most because a
#: batch's step is held to the browser profile's 7,200 s (`FLOW_STEP_CEILING`):
#: 30 flows have 240 s each, about two widths of the bench's 44.5 s median
#: flow run (docs/browser-bench.md) plus a sign-in; more would starve them.
FLOWS_PER_STEP_DEFAULT = 15
MIN_FLOWS_PER_STEP = 1
MAX_FLOWS_PER_STEP = 30
#: One flow's budget: the timeout a one-flow step had before batching. A
#: batch step's timeout is this times its flows, held to `FLOW_STEP_CEILING`.
FLOW_SECONDS = 2700
#: The live-browser profile's own ceiling (`claude-code-browser`'s
#: `timeout_seconds`, 7,200 s), read from the frozen catalogue rather than
#: restated: the API cuts a longer step to it anyway (`validation`), so a
#: render that said more would promise time the step never gets.
FLOW_STEP_CEILING = RUNNER_PROFILES["claude-code-browser"].timeout_seconds
#: The most steps one workflow holds (`swarm_common.config.Settings.
#: max_workflow_steps`, 50), read from the frozen default as
#: `attemptstate.BUILDS_ON_MAX_DEPTH` reads it. A leg that would render more
#: is refused here, naming the smallest `flows_per_step` that fits, instead of
#: by the submission's DAG check with nothing said about flows.
MAX_WORKFLOW_STEPS: int = next(
    f.default for f in dataclasses.fields(Settings) if f.name == "max_workflow_steps"
)
#: The most bytes every step's input of one workflow comes to together
#: (`Settings.max_input_bytes`, 256 KiB; `service.validate_input_size`'s
#: "workflow input"). 150 flows of 500-character goals are ~80 KB of goals
#: alone, more in a script of several bytes a character, so a leg's prompts
#: are measured here and refused with a reason, not at submission without one.
MAX_INPUT_BYTES: int = next(
    f.default for f in dataclasses.fields(Settings) if f.name == "max_input_bytes"
)
#: The broker's per-agent issue bound (agent-autonomy.md §3.2).
MAX_ISSUES = 30

#: The live gate's presets, by name (`agent_worker.livegate.presets.PRESETS`,
#: which swarm-api cannot import; tests/unit/control_plane/test_qaspec.py
#: holds the two equal). A viewport is never sent: the gate refuses one.
PRESETS = ("desktop", "iphone-15", "phone-390", "pixel-8", "iphone-se", "ipad-7")
THEMES = ("light", "dark")
DEFAULT_WIDTHS = ("desktop", "phone-390")
DEFAULT_THEMES = ("light", "dark")
SEVERITIES = ("blocker", "major", "minor", "nit")
ENVIRONMENTS = ("production", "non_production")
#: VQ-Q3, owner decision 2026-10-22: "Console link by default; evidence branch
#: as a per-target opt-in for private repositories". Whether the repository is
#: private is the target registration's check (lane VQ-2), not this module's.
EVIDENCE_MODES = ("console_link", "branch")
FILE_MODES = ("issues", "none")
#: The browser tool a goal-driven flow drives the live browser with
#: (`flows_browser_tool`, or one run's `browser_tool`; lane VQ9-TOOL). BGD,
#: 2026-10-23, after batch bench3-2026-10-23 (docs/browser-bench.md): Playwright
#: MCP passed 8 of 10 prompts at $0.14 and 44.5 s a run (medians), Jev 6 of 10
#: at $0.17 and 51.3 s, so flows default to Playwright and a target, or one
#: run, may choose Jev. Capture and discovery stay on Playwright whatever this
#: says: they run the fixed capture tool. Both are `claude-code-browser`'s
#: `browser_tool` choices; a jev flow without the TypeSafe key its workspace
#: needs is refused at submission (`_refuse_unavailable_browser_tool`).
FLOW_BROWSER_TOOLS = ("playwright", "jev")
DEFAULT_FLOW_BROWSER_TOOL = "playwright"
#: What each tool's flow prompt says to drive the browser with.
_FLOW_TOOL_SENTENCE = {
    "playwright": "Using the Playwright MCP browser_* tools (browser_navigate, browser_snapshot, browser_click)",
    "jev": "Using the jev browser tools",
}

#: Never visited, never clicked, whatever the target says (§1): the target's
#: own lists are MERGED with these, never replace them.
DEFAULT_EXCLUDED_PATHS = ("/logout", "/logout/*", "/signout", "/signout/*")
DEFAULT_EXCLUDED_CONTROLS = (
    "delete", "remove", "pay", "buy", "purchase", "send", "invite",
    "unsubscribe", "cancel subscription",
)
#: A sign-in's start clicks (§2.2; BGD, 2026-10-23): what the gate clicks on
#: the login page before the form, a chooser or a "Sign in" button before an
#: identity provider. Three covers a toggle, a button and one interstitial.
MAX_START_CLICKS = 3
START_CLICK_TEXT_MAX = 60
START_CLICK_CSS_MAX = 200
#: Words no start click's text may contain, besides the excluded controls:
#: the gate clicks before it has signed in, and an exit is never a way in.
START_CLICK_REFUSED = ("logout", "log out", "sign out")
#: Playwright's own selector syntax, which `querySelectorAll` does not take:
#: the gate clicks over CDP, in the page, not through Playwright.
_PLAYWRIGHT_SELECTOR = re.compile(
    r":(?:has-text|text|text-is|text-matches|nth-match|left-of|right-of|above|below|near)\(|:visible\b|>>"
    r"|^\s*(?:text|css|xpath|id|role|data-testid|internal:[a-z-]+)=")
#: A challenge bypass (§2.5; lane CAPTCHA-BYPASS, BGD 2026-10-23): a header or
#: a cookie the app's owner configured to skip their own challenge, which the
#: live gate adds on the target's approved origins alone. Its name is an HTTP
#: token (RFC 9110 §5.6.2, less the backtick no prompt or command quotes).
CHALLENGE_BYPASS_KINDS = ("header", "cookie")
CHALLENGE_BYPASS_NAME_MAX = 64
_BYPASS_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_|~-]{1,64}")
#: Header names a bypass never takes, lower case. The app's own credentials
#: (`authorization`, `cookie`: a bypass header would replace the session the
#: gate signed in with), the request's routing and framing (`host`,
#: `content-length`, `transfer-encoding`, `connection`, `upgrade`, `te`,
#: `trailer`, `keep-alive`, `expect`), what the browser itself vouches for
#: (`origin`, `referer`), and a response's (`set-cookie`). Chromium refuses
#: several of them in `Fetch.continueRequest` anyway ("Unsafe header",
#: docs/design/qa-identity-measurements.md M3). Restated by the gate
#: (`agent_worker.livegate.qatarget`), which cannot import swarm-api;
#: tests/unit/worker/test_qa_target_challenge_bypass.py holds the two equal.
RESERVED_BYPASS_HEADERS = frozenset({
    "authorization", "cookie", "host", "set-cookie", "content-length", "transfer-encoding",
    "connection", "upgrade", "te", "trailer", "keep-alive", "expect", "origin", "referer",
})
#: And every name under these prefixes: `Proxy-*` is the proxy's credentials,
#: `Sec-*` is the browser's own (fetch metadata, client hints).
RESERVED_BYPASS_HEADER_PREFIXES = ("proxy-", "sec-")
DEFAULT_DESIGN_DOCS = ("DESIGN.md", "docs/design*/**", "**/style-guide*")
DEFAULT_READY = "body"
DEFAULT_FILE: Mapping[str, Any] = {
    "min_severity": "minor",
    "max_issues": 20,
    "labels": ("visual-qa",),
    "evidence": "console_link",
}

CAPTURE_PROFILE = "claude-code-browser"
REVIEW_PROFILE = "claude-code"
#: The console template's lesson (docs/browser-qa.md): haiku captures, opus
#: reviews and reports, sonnet runs flows and files.
CAPTURE_MODEL, REVIEW_MODEL, FLOW_MODEL, REPORT_MODEL, FILE_MODEL = (
    "haiku", "opus", "sonnet", "opus", "sonnet")

#: `swarm_qa.NAME`: a file-name-safe id. `--` separates a capture file's parts.
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_FINDING_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
#: A run group's id (`qatargets.open_group`): `qrg_` and 16 hex digits.
_GROUP_ID = re.compile(r"qrg_[0-9a-f]{16}")
#: An origin-relative path: no scheme, no authority, no whitespace, nothing a
#: prompt's command line or a JSON string would read as syntax. A hash route,
#: `/app#/settings`, is a path too (qa-environments.md §4.1: the crawl lists
#: `#/route` fragments, `swarm_qa.HASH_ROUTE`); `#section` is not one.
_PATH = re.compile(r"/(?!/)[A-Za-z0-9._~%!$&()*+,;=:@/?-]{0,511}(?:#/[A-Za-z0-9._~%/-]{0,255})?")
_GLOB = re.compile(r"/[A-Za-z0-9._~%!$&()*+,;=:@/?\[\]-]{0,255}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+")
_BASE_PATH = re.compile(r"(?:/[A-Za-z0-9._~%-]*)*")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}")
#: A label rule's route prefix: origin-relative like a finding's `path`, and
#: with no query, so a scheme, a host (`//h/...`) or `?` never reads as a route.
_PREFIX = re.compile(r"/(?!/)[A-Za-z0-9._~%!$&()*+,;=:@/-]{0,255}")
#: `file.label_rules` (#2062): at most this many rules, each at most
#: `MAX_LABELS` labels and `MAX_RULE_PATHS` prefixes, as `file.labels` is held
#: to `MAX_LABELS`: GitHub applies what it is given, so the bound is ours.
MAX_LABELS, MAX_LABEL_RULES, MAX_RULE_PATHS = 10, 20, 10
_EVIDENCE = re.compile(r"(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}\.(?:png|gif|jpe?g|webp)")
#: Free text a target supplies and a prompt quotes: one line, no backtick.
_FORBIDDEN_TEXT = re.compile(r"[\x00-\x1f\x7f`]")


class QaSpecError(ValueError):
    """A target, or a findings document, this module refuses. `code` is the
    API's error code; `message` never quotes a value from the target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid(message: str) -> QaSpecError:
    return QaSpecError("qa_target_invalid", message)


# --------------------------------------------------------------------------
# Reading the target
# --------------------------------------------------------------------------


def _text(value: Any, what: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _FORBIDDEN_TEXT.search(value):
        raise _invalid(f"{what} must be one line of text, at most {limit} characters, with no backtick")
    return value.strip()


def base_url(target: Mapping[str, Any]) -> str:
    """The target's `base_url`, checked as a plain https origin and optional
    path, without its trailing slash. The public-address rule (no internal
    name, nothing that resolves private) is the registration's (lane VQ-2,
    `swarm_egress.policy`), not restated here."""
    raw = target.get("base_url")
    if not isinstance(raw, str) or len(raw) > 512 or "?" in raw or "#" in raw or "@" in raw:
        raise _invalid("base_url must be https://host[:port]/[path], with no userinfo, query or fragment")
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.scheme != "https" or not _HOST.fullmatch(host) or not _BASE_PATH.fullmatch(parts.path):
        raise _invalid("base_url must be https://host[:port]/[path], with no userinfo, query or fragment")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _invalid("base_url must name a host, not an IP address")
    try:
        port = parts.port
    except ValueError:
        raise _invalid("base_url's port is not a port") from None
    origin = f"https://{host.lower()}" + (f":{port}" if port else "")
    return (origin + parts.path).rstrip("/")


def screen_url(base: str, path: str) -> str:
    """The URL a base-relative `path` opens under `base` (as `base_url`
    returns it, with no trailing slash): `/` is the base itself, with its
    slash, and `/settings` is `<base>/settings`. The one join of a path to the
    base, so a sub-path base never opens `//` or its path twice (§1)."""
    return f"{base}/{path[1:]}"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _exclusions(target: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    raw = target.get("exclusions") or {}
    if not isinstance(raw, Mapping):
        raise _invalid("exclusions must be {paths, controls}")
    paths = list(DEFAULT_EXCLUDED_PATHS)
    for glob in raw.get("paths") or ():
        if not isinstance(glob, str) or not _GLOB.fullmatch(glob):
            raise _invalid("an excluded path must be an origin-relative path glob")
        if glob not in paths:
            paths.append(glob)
    controls = list(DEFAULT_EXCLUDED_CONTROLS)
    for text in raw.get("controls") or ():
        text = _text(text, "an excluded control", limit=80)
        if text not in controls:
            controls.append(text)
    return paths, controls


def _excluded(path: str, globs: Sequence[str]) -> bool:
    bare = path.split("?", 1)[0]
    return any(fnmatch.fnmatchcase(bare, g) or fnmatch.fnmatchcase(path, g) for g in globs)


def _path(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _PATH.fullmatch(value):
        raise _invalid(f"{what} must be an origin-relative path starting with one /")
    return value


def _screen(raw: Any, excluded: Sequence[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _invalid("a screen must be {id, path, ready?, segments?}")
    sid = raw.get("id")
    if not isinstance(sid, str) or not _NAME.fullmatch(sid) or "--" in sid:
        raise _invalid("a screen id must be lower-case letters, digits and single hyphens, at most 48")
    path = _path(raw.get("path"), f"screen {sid}'s path")
    if _excluded(path, excluded):
        raise QaSpecError("qa_screen_excluded", f"screen {sid} is on an excluded path")
    route: dict[str, Any] = {"id": sid, "path": path,
                             "ready": _text(raw.get("ready", DEFAULT_READY), f"screen {sid}'s ready selector",
                                            limit=200)}
    if raw.get("segments") is not None:
        segments = raw["segments"]
        if not isinstance(segments, int) or isinstance(segments, bool) or not 1 <= segments <= MAX_SEGMENTS:
            raise _invalid(f"screen {sid}'s segments must be 1..{MAX_SEGMENTS}")
        route["segments"] = segments
    return route


def _screens(target: Mapping[str, Any], subset: Sequence[str] | None) -> list[dict[str, Any]]:
    """The run's stored routes: the target's `screens`, else its stored
    `discovered`. [] for a target that asks to `discover` and has neither
    (§3.1, a run from nothing but the base): its steps crawl from the base."""
    raw = target.get("screens")
    if raw is None:
        raw = target.get("discovered")
    if (raw is None or raw == []) and target.get("discover") is not None:
        if subset is not None:
            raise _invalid("a run's screens must name screens the target lists")
        return []
    if not isinstance(raw, list) or not raw:
        raise QaSpecError("qa_target_no_screens",
                          "the target lists no screens and does not ask to discover them")
    excluded, _ = _exclusions(target)
    screens = [_screen(s, excluded) for s in raw]
    for screen, stored in zip(screens, raw):
        # A route state a crawl found (§4.3) is captured as the state it is,
        # its clicks replayed, never as its bare path.
        if isinstance(stored, Mapping) and stored.get("reveal") is not None:
            screen["reveal"] = reveal_clicks(target, stored["reveal"])
    ids = [s["id"] for s in screens]
    if len(set(ids)) != len(ids):
        raise _invalid("two screens share an id")
    if subset is not None:
        unknown = sorted(set(subset) - set(ids))
        if unknown or not subset:
            raise _invalid("a run's screens must name screens the target lists")
        screens = [s for s in screens if s["id"] in set(subset)]
    if len(screens) > MAX_SCREENS:
        raise QaSpecError("qa_target_too_many_screens",
                          f"a run captures at most {MAX_SCREENS} screens; this one lists {len(screens)}")
    return screens


def _names(value: Any, allowed: Sequence[str], default: Sequence[str], what: str) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not value or len(set(value)) != len(value) or any(
        v not in allowed for v in value
    ):
        raise _invalid(f"{what} must be a non-empty list of {', '.join(allowed)}")
    return list(value)


def _bounded(raw: Mapping[str, Any], key: str, low: int, high: int, default: int | None) -> int:
    """`raw[key]`, an int in low..high; `default` when absent, required when None."""
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise _invalid(f"discover.{key} must be {low}..{high}")
    return value


def _discover(target: Mapping[str, Any]) -> dict[str, int]:
    """The target's `discover`, checked: `{max_pages, max_depth}` (both
    required when it is set, §4.1), `slots` (1..8, 4) and `found_per_step`
    (0..8, 8; §3.1, §10). A target that does not ask to discover crawls at
    the tool's bounds, its found routes at the default cap."""
    raw = target.get("discover")
    if raw is None:
        return {"max_pages": MAX_DISCOVER_PAGES, "max_depth": MAX_DISCOVER_DEPTH, "slots": DEFAULT_SLOTS,
                "found_per_step": FOUND_PER_STEP}
    if not isinstance(raw, Mapping):
        raise _invalid("discover must be {max_pages, max_depth, slots?, found_per_step?}")
    return {
        "max_pages": _bounded(raw, "max_pages", 1, MAX_DISCOVER_PAGES, None),
        "max_depth": _bounded(raw, "max_depth", 0, MAX_DISCOVER_DEPTH, None),
        "slots": _bounded(raw, "slots", 1, MAX_GROUPS, DEFAULT_SLOTS),
        "found_per_step": _bounded(raw, "found_per_step", 0, MAX_FOUND_PER_STEP, FOUND_PER_STEP),
    }


def _environment(target: Mapping[str, Any]) -> str:
    # Absent is production: the safe reading of someone else's application.
    env = target.get("environment") or "production"
    if env not in ENVIRONMENTS:
        raise _invalid("environment must be production or non_production")
    return env


# --------------------------------------------------------------------------
# One leg of a target (docs/design/qa-environments.md §1.1, §1.5, §6;
# lane QAENV-MODEL, BGD's Q1 (a), 2026-10-23)
# --------------------------------------------------------------------------

#: An entry's name: a short label, unique in its target (§1.1).
ENVIRONMENT_NAME = re.compile(r"[a-z0-9-]{1,20}")
#: More legs than any app serves (dev, staging, production, a preview or
#: two), and a third of the ~25 maximal legs one tenant's QA map holds
#: (§1.4), so one target cannot fill the workspace's secret.
MAX_ENVIRONMENTS = 8
#: What a leg holds, and so what moves into an entry of `environments[]`:
#: WHERE the app is served and every rule that must never cross from one leg
#: to another. The routes, flows, exclusions and filing rules describe the
#: app and stay on the target once, because two copies drift (§1.1).
LEG_FIELDS = (
    "base_url", "host", "origin", "environment", "sign_in", "credential", "challenge_bypass",
    "challenge_bypass_value", "host_state", "approved_origins", "approved_by", "approved_at",
    "approved_idp_token_path", "read_posts", "rate", "discovered", "discovered_at", "discovered_by",
    "discovered_workflow", "discovered_new", "discovery_error", "discovering", "legacy_map_key",
)
#: The leg's name in a view `leg` makes; never stored.
LEG_NAME = "environment_name"
#: Set (True) in a view of, and a read of, a target stored with
#: `environments`; never stored. The mark names its leg only then, so a
#: one-entry target stored before this lane is marked, rendered and answered
#: byte for byte as before.
MIGRATED = "environments_migrated"


def legacy_environment_name(target: Mapping[str, Any]) -> str:
    """The name §1.2's migration gives a target's one leg: `production` when
    its old mark was (absent is production, `_environment`'s reading), and
    `staging` otherwise."""
    return "production" if (target.get("environment") or "production") == "production" else "staging"


def is_migrated(target: Mapping[str, Any]) -> bool:
    flag = target.get(MIGRATED)
    return flag if isinstance(flag, bool) else isinstance(target.get("environments"), list)


def environments(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The target's entries. A target stored before this lane is presented
    as a one-entry list (§1.2), its legacy name, its map key the bare target
    id (`legacy_map_key`, §1.4)."""
    raw = target.get("environments")
    if isinstance(raw, list):
        return [dict(e) for e in raw if isinstance(e, Mapping)]
    entry = {k: target[k] for k in LEG_FIELDS if k in target}
    entry["name"] = legacy_environment_name(target)
    entry["legacy_map_key"] = True
    return [entry]


def entry_named(target: Mapping[str, Any], name: str | None) -> dict[str, Any]:
    """The entry `name`, or the target's only one when `name` is None.
    Raises `QaSpecError`: a target of several entries needs a name, and the
    name must be one of them. Nothing ever falls back to another entry."""
    entries = environments(target)
    if name is None:
        if len(entries) != 1:
            raise QaSpecError("qa_target_environment_required",
                              "this QA target has several environments; name the one to use")
        return entries[0]
    for entry in entries:
        if entry.get("name") == name:
            return entry
    raise QaSpecError("qa_target_environment_unknown", "this QA target has no environment of that name")


def _leg_origins(entry: Mapping[str, Any]) -> set[str]:
    """Every origin a leg may reach: what was approved, and what is declared,
    so a pending approval never narrows the comparison below. An explicit
    `:443` is https's own port, so `https://h:443` and `https://h` are one
    origin here: read as two, they would let a write permission through."""
    origins: set[str] = set()
    for raw in (*(entry.get("approved_origins") or []), entry.get("origin"), entry.get("base_url"),
                (entry.get("sign_in") or {}).get("extra_origin")):
        if isinstance(raw, str) and raw.lower().startswith("https://"):
            origin = _origin(raw).lower()
            origins.add(origin[:-len(":443")] if origin.endswith(":443") else origin)
    return origins


def shares_production_origin(target: Mapping[str, Any], name: str) -> bool:
    """True when the entry `name` shares any origin with ANOTHER entry whose
    mark is `production` (§1.5). Such a leg is held as production for every
    write: the page, not the origin, chooses which backend a write reaches."""
    mine = _leg_origins(entry_named(target, name))
    return any(e.get("name") != name and (e.get("environment") or "production") == "production"
               and mine & _leg_origins(e) for e in environments(target))


def leg(target: Mapping[str, Any], name: str | None = None) -> dict[str, Any]:
    """The target as its leg `name` sees it: the app's fields, and that
    entry's per-leg fields where a target of one origin keeps them, so every
    rule here applies to a leg unchanged. Nothing of another entry is in it.

    A target with no `environments` is its own one leg: the view is the
    target itself, with `LEG_NAME`. Of a target with several entries, a leg
    that is `production`, or shares an origin with a `production` entry, is
    rendered with no mutating flow (§1.5, §6: a production leg reads only)."""
    entry = entry_named(target, name)
    migrated = is_migrated(target)
    view = {k: v for k, v in target.items() if k not in LEG_FIELDS and k not in ("environments", MIGRATED)}
    view.update({k: v for k, v in entry.items() if k in LEG_FIELDS and k != "legacy_map_key"})
    view[LEG_NAME] = entry["name"]
    if migrated:
        view[MIGRATED] = True
    if len(environments(target)) > 1 and isinstance(view.get("flows"), list) and (
            (entry.get("environment") or "production") == "production"
            or shares_production_origin(target, entry["name"])):
        view["flows"] = [f for f in view["flows"] if not (isinstance(f, Mapping) and f.get("mutating") is True)]
    return view


def _flows(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = target.get("flows") or []
    if not isinstance(raw, list) or len(raw) > MAX_FLOWS:
        raise _invalid(f"flows must be a list of at most {MAX_FLOWS}")
    production = _environment(target) == "production"
    excluded, _ = _exclusions(target)
    base_path = urlsplit(base_url(target)).path if raw else ""
    flows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _invalid("a flow must be {id, goal, start_path, mutating?}")
        fid = item.get("id")
        if not isinstance(fid, str) or not _NAME.fullmatch(fid) or "--" in fid or fid == "report":
            raise _invalid("a flow id must be lower-case letters, digits and single hyphens, and not 'report'")
        mutating = item.get("mutating", False)
        if not isinstance(mutating, bool):
            raise _invalid(f"flow {fid}'s mutating must be true or false")
        if mutating and production:
            # §3: only a target registered as non-production may run a flow
            # that writes. Refused here, before anything is admitted.
            raise QaSpecError("qa_mutating_flow_on_production",
                              f"flow {fid} is mutating and the target's environment is production")
        start = _path(item.get("start_path", "/"), f"flow {fid}'s start_path")
        # `start_path` is relative to the base and exclusions to the origin,
        # so both readings are checked: `/logout` under `/app` is `/app/logout`.
        opened = screen_url(base_path, start)
        if _excluded(start, excluded) or _excluded(opened, excluded):
            raise QaSpecError("qa_screen_excluded", f"flow {fid} starts on an excluded path")
        flows.append({"id": fid, "goal": _text(item.get("goal"), f"flow {fid}'s goal", limit=500),
                      "start_path": start, "mutating": mutating})
    if len({f["id"] for f in flows}) != len(flows):
        raise _invalid("two flows share an id")
    return flows


def flows_browser_tool(target: Mapping[str, Any], override: Any = None) -> str:
    """The tool this render's flows run with: one run's `override` when it
    names one, else the target's `flows_browser_tool`, else Playwright."""
    for value, what in ((override, "browser_tool"), (target.get("flows_browser_tool"), "flows_browser_tool")):
        if value is None:
            continue
        if not isinstance(value, str) or value not in FLOW_BROWSER_TOOLS:
            raise _invalid(f"{what} must be one of {', '.join(FLOW_BROWSER_TOOLS)}")
        return value
    return DEFAULT_FLOW_BROWSER_TOOL


def check_flows_per_step(value: Any = None) -> int:
    """The batch size a render uses: `value` when it names one, else
    `FLOWS_PER_STEP_DEFAULT`. Anything but an int in 1-30 is refused, a bool
    included (`True` is an int to Python and a mistake to everyone else)."""
    if value is None:
        return FLOWS_PER_STEP_DEFAULT
    if not isinstance(value, int) or isinstance(value, bool) \
            or not MIN_FLOWS_PER_STEP <= value <= MAX_FLOWS_PER_STEP:
        raise _invalid(f"flows_per_step must be a whole number {MIN_FLOWS_PER_STEP}-{MAX_FLOWS_PER_STEP}")
    return value


def flow_batches(flows: Sequence[Mapping[str, Any]], per_step: int) -> list[list[Mapping[str, Any]]]:
    """The read-only flows, in their order, cut into batches of `per_step`:
    ceil(n / per_step) of them, every one full but the last.

    A mutating flow is never in a batch. The gate grants a step writes from
    its own step id, `flow-<id>`, naming one flow the stored target declares
    mutating (`qatargets.QaTargetIssuer._mutating`); a batch that held one
    would either lose that grant or hand it to every read-only flow beside
    it. So a mutating flow keeps its own step, as before batching."""
    readonly = [f for f in flows if not f.get("mutating")]
    return [readonly[k:k + per_step] for k in range(0, len(readonly), per_step)]


def batch_timeout(size: int) -> int:
    """A flow-batch step's timeout: `FLOW_SECONDS` (2,700 s, one flow's step
    before batching) for each of its flows, held to `FLOW_STEP_CEILING` (the
    profile's 7,200 s). 1 flow: 2,700 s; 2: 5,400 s; 3 or more: 7,200 s, so a
    batch of 15 has 480 s a flow and one of 30 has 240 s."""
    return min(FLOW_SECONDS * size, FLOW_STEP_CEILING)


def _file_settings(target: Mapping[str, Any]) -> dict[str, Any]:
    raw = target.get("file") or {}
    if not isinstance(raw, Mapping):
        raise _invalid("file must be {min_severity, max_issues, labels, label_rules, evidence}")
    out = {**DEFAULT_FILE, **raw}
    if out["min_severity"] not in SEVERITIES:
        raise _invalid(f"file.min_severity must be one of {', '.join(SEVERITIES)}")
    n = out["max_issues"]
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_ISSUES:
        raise _invalid(f"file.max_issues must be 1..{MAX_ISSUES}")
    labels = out["labels"]
    if not isinstance(labels, (list, tuple)) or len(labels) > MAX_LABELS or not all(
        isinstance(x, str) and _LABEL.fullmatch(x) for x in labels
    ):
        raise _invalid(f"file.labels must be at most {MAX_LABELS} label names")
    if out["evidence"] not in EVIDENCE_MODES:
        raise _invalid(f"file.evidence must be one of {', '.join(EVIDENCE_MODES)}")
    out["labels"] = list(labels)
    out["label_rules"] = _label_rules(out.get("label_rules"))
    return out


def _label_rules(raw: Any) -> list[dict[str, list[str]]]:
    """`file.label_rules` (#2062): `[{paths, labels}]`, each refusal naming
    the field it is about and quoting no value."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LABEL_RULES:
        raise _invalid(f"file.label_rules must be a list of at most {MAX_LABEL_RULES} rules {{paths, labels}}")
    rules: list[dict[str, list[str]]] = []
    for index, rule in enumerate(raw):
        what = f"file.label_rules[{index}]"
        if not isinstance(rule, Mapping) or set(rule) != {"paths", "labels"}:
            raise _invalid(f"{what} must be {{paths, labels}} and nothing else")
        paths, labels = rule["paths"], rule["labels"]
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_RULE_PATHS or not all(
            isinstance(p, str) and _PREFIX.fullmatch(p) for p in paths
        ):
            raise _invalid(f"{what}.paths must be 1..{MAX_RULE_PATHS} origin-relative route prefixes, each "
                           f"starting with one / and with no scheme, host or query")
        if not isinstance(labels, list) or not 1 <= len(labels) <= MAX_LABELS or not all(
            isinstance(x, str) and _LABEL.fullmatch(x) for x in labels
        ):
            raise _invalid(f"{what}.labels must be 1..{MAX_LABELS} label names")
        rules.append({"paths": list(paths), "labels": list(labels)})
    return rules


def finding_labels(settings: Mapping[str, Any], path: str) -> tuple[list[str], list[int]]:
    """The labels a finding on `path` (its origin-relative route) is filed
    with, `file.labels` first and each matching rule's after, each once, and
    the indexes of the rules that matched. A rule matches when the route,
    without its query, starts with one of its prefixes (#2062)."""
    route = path.split("?", 1)[0].split("#", 1)[0]
    labels = list(settings["labels"])
    matched: list[int] = []
    for index, rule in enumerate(settings.get("label_rules") or ()):
        if any(route.startswith(prefix) for prefix in rule["paths"]):
            matched.append(index)
            labels.extend(x for x in rule["labels"] if x not in labels)
    return labels, matched


def _design_docs(target: Mapping[str, Any]) -> list[str]:
    raw = target.get("design_docs")
    if raw is None:
        return list(DEFAULT_DESIGN_DOCS)
    if not isinstance(raw, list) or len(raw) > 20 or not all(
        isinstance(g, str) and re.fullmatch(r"[A-Za-z0-9._*/\[\]-]{1,200}", g) for g in raw
    ):
        raise _invalid("design_docs must be at most 20 repository path globs")
    return list(raw)


def _own_login(target: Mapping[str, Any]) -> bool:
    sign_in = target.get("sign_in") or {"kind": "none"}
    kind = sign_in.get("kind") if isinstance(sign_in, Mapping) else None
    if kind not in ("none", "own_login"):
        raise _invalid("sign_in.kind must be none or own_login")
    return kind == "own_login"


def start_clicks(target: Mapping[str, Any]) -> list[dict[str, str]]:
    """The target's `sign_in.start_clicks`, checked (§2.2): what the gate
    clicks, in order, on the login page before the form, and what the capture
    agent is told to click when its tab shows that page again. [] when none."""
    sign_in = target.get("sign_in") or {"kind": "none"}
    raw = sign_in.get("start_clicks") if isinstance(sign_in, Mapping) else None
    if raw is None:
        return []
    if not _own_login(target):
        raise _invalid("sign_in.start_clicks is allowed only with kind own_login: they are clicks the "
                       "gate makes on the login page before it signs in")
    return _clicks(target, raw, field="sign_in.start_clicks", noun="a start click",
                   why="the gate never clicks one, before a sign-in least of all")


def reveal_clicks(target: Mapping[str, Any], raw: Any) -> list[dict[str, str]]:
    """A route state's `reveal` (qa-environments.md §4.3), held to the
    start-click rules: 1 to 3 clicks of `{text, css?}`, plain CSS, no
    exclusion word (the defaults, logout and the target's own). Capture
    replays them before it shoots, so they are clicks on the app exactly as
    a start click is."""
    return _clicks(target, raw, field="reveal", noun="a reveal click",
                   why="capture never clicks one")


def _clicks(target: Mapping[str, Any], raw: Any, *, field: str, noun: str, why: str) -> list[dict[str, str]]:
    shape = f"{field} must list 1 to {MAX_START_CLICKS} clicks, each {{text, css?}}"
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_START_CLICKS:
        raise _invalid(shape)
    _, controls = _exclusions(target)
    refused = [w.lower() for w in (*controls, *START_CLICK_REFUSED)]
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"text", "css"}:
            raise _invalid(shape)
        text = _text(item.get("text"), f"{noun}'s text", limit=START_CLICK_TEXT_MAX)
        if any(word in text.lower() for word in refused):
            raise _invalid(f"{noun}'s text names an excluded control (the defaults, logout and the "
                           f"target's own): {why}")
        click = {"text": text}
        if item.get("css") is not None:
            css = _text(item["css"], f"{noun}'s css", limit=START_CLICK_CSS_MAX)
            if _PLAYWRIGHT_SELECTOR.search(css):
                raise _invalid(f"{noun}'s css is a CSS selector the page itself takes; Playwright "
                               "syntax such as :has-text() is refused because the gate clicks over CDP, "
                               "not through Playwright. Put the control's visible text in text")
            click["css"] = css
        out.append(click)
    return out


def challenge_bypass(target: Mapping[str, Any]) -> dict[str, str] | None:
    """The target's `challenge_bypass`, checked (§2.5): `{kind, name}`, or
    None when it has none. Never its value, which is write-only and set apart
    (`PUT /v1/qa-targets/{id}/challenge-bypass`): a value here is refused, so
    one sent with the registration is never stored on the document."""
    raw = target.get("challenge_bypass")
    if raw is None:
        return None
    shape = "challenge_bypass must be {kind: header or cookie, name}; its value is set on its own"
    if not isinstance(raw, Mapping) or set(raw) != {"kind", "name"}:
        raise _invalid(shape)
    try:
        base_url(target)
    except QaSpecError:
        raise _invalid("a challenge bypass needs the target's host: the gate adds it on that host's "
                       "approved origins and nowhere else") from None
    kind, name = raw.get("kind"), raw.get("name")
    if kind not in CHALLENGE_BYPASS_KINDS:
        raise _invalid(shape)
    if not isinstance(name, str) or not _BYPASS_NAME.fullmatch(name):
        raise _invalid(f"challenge_bypass.name must be a {kind} name: 1 to {CHALLENGE_BYPASS_NAME_MAX} "
                       "letters, digits or !#$%&'*+.^_|~- with no space or colon")
    lowered = name.lower()
    if kind == "header" and (lowered in RESERVED_BYPASS_HEADERS
                             or lowered.startswith(RESERVED_BYPASS_HEADER_PREFIXES)):
        raise _invalid("challenge_bypass.name is a header the app's session, the request's framing or the "
                       "browser owns (Authorization, Cookie, Host, Proxy-* and the like); name one of your own, "
                       "such as X-QA-Bypass")
    return {"kind": kind, "name": name}


def _repository(target: Mapping[str, Any]) -> str:
    repo = target.get("repository")
    if not isinstance(repo, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+", repo):
        raise _invalid("repository must be the https URL of one of the tenant's registered repositories")
    return repo


# --------------------------------------------------------------------------
# Grouping and the route list
# --------------------------------------------------------------------------


def group_screens(screens: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """`screens` in order, as groups `g1`..`g8` of at most 8, as even as the
    count allows (17 is 6, 6, 5, not 8, 8, 1: the slowest capture sets the
    run's length)."""
    n = len(screens)
    if not 1 <= n <= MAX_SCREENS:
        raise QaSpecError("qa_target_too_many_screens" if n else "qa_target_no_screens",
                          f"a run captures 1..{MAX_SCREENS} screens; this one has {n}")
    count = -(-n // MAX_GROUP_SCREENS)
    size, extra = divmod(n, count)
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    start = 0
    for i in range(count):
        end = start + size + (1 if i < extra else 0)
        groups.append((f"g{i + 1}", [dict(s) for s in screens[start:end]]))
        start = end
    assert len(groups) <= MAX_GROUPS and all(len(g) <= MAX_GROUP_SCREENS for _, g in groups)
    return groups


def route_list(groups: Sequence[tuple[str, Sequence[Mapping[str, Any]]]], *,
               clock: str | None = None, dismissed: Sequence[str] = ()) -> dict[str, Any]:
    """The capture tool's route-list document (`swarm_qa.load_routes`, format
    1) for `groups`. `clock` only when the target sets one (§4.2);
    `dismissed` (`dismissed_hashes`) only when the target dismissed a route."""
    document: dict[str, Any] = {
        "format": 1,
        "groups": {
            name: [{"id": s["id"], "path": s["path"], "ready": s.get("ready", DEFAULT_READY),
                    **({"segments": s["segments"]} if "segments" in s else {}),
                    **({"reveal": [dict(c) for c in s["reveal"]]} if s.get("reveal") else {})}
                   for s in screens]
            for name, screens in groups
        },
    }
    if clock is not None:
        document["clock"] = clock
    if dismissed:
        document["dismissed"] = list(dismissed)
    return document


def dismissed_hashes(target: Mapping[str, Any]) -> list[str]:
    """The target's dismissed routes as the crawl reads them (QA-CRAWL-SKIP):
    each `{key, ...}` entry's key as sha256 hex (`swarm_qa._sha`), sorted and
    unique. An entry with no string key is no dismissal."""
    return sorted({hashlib.sha256(d["key"].encode("utf-8")).hexdigest()
                   for d in target.get("dismissed_routes") or []
                   if isinstance(d, Mapping) and isinstance(d.get("key"), str)})


def _clock(target: Mapping[str, Any]) -> str | None:
    clock = target.get("clock")
    if clock is None:
        return None
    if not isinstance(clock, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)",
                                                       clock):
        raise _invalid("clock must be an ISO 8601 instant with a time zone")
    return clock


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

#: The finding's shape (§4.4), quoted in the review and report prompts: the
#: checkout is the target's repository, which holds no SwarmCloud document.
FINDING_EXAMPLE: Mapping[str, Any] = {
    "id": "g2-004",
    "title": "The pricing table overflows its card at 390 in dark mode",
    "severity": "major",
    "screen": "pricing",
    "path": "/pricing",
    "width": "phone-390",
    "theme": "dark",
    "evidence": ["g2-004.png", "g2/pricing--phone-390--dark--s2.png"],
    "actual": "The third column is cut off at the card's right edge.",
    "expected": "The table wraps or scrolls inside its card, as at 1440.",
    "steps_to_reproduce": ["Open /pricing at 390 px wide", "Set the system theme to dark",
                           "Scroll to the plans table"],
    "also_seen": [{"width": "desktop", "theme": "dark"}],
}

CHECKLIST = (
    "layout overflow and clipping; alignment and spacing; truncated or overlapping text; contrast, in dark "
    "mode especially, and the axe-core report the manifest lists for every screen; what differs between "
    "the widths that should not; broken or missing images; the console errors, page errors and failed "
    "requests the manifest counts; empty, loading and error states that read wrong; text that contradicts "
    "itself or the page's purpose; and every route the manifest lists as failed, which is itself a finding"
)

NO_CHANGE = "Change no file in the repository."

#: QA-GATE-REFUSALS (part of #2067). The live gate answers some requests
#: itself (a write, anything but GET and HEAD to another origin), and its 403
#: to another origin carries no CORS header, so Chrome logs a CORS block: on
#: 2026-10-23 a run filed exactly that, an app's OIDC token exchange the gate
#: refused, into the app's repository as a blocker. The capture writes each
#: screen's refusals into its errors file as `gate_refused`, and these
#: sentences keep them the run's, from the review to the filing.
GATE_CAPTURE = (
    "The tool also writes, into each screen's errors file as `gate_refused`, the requests the live browser "
    "refused itself while that screen loaded: those are the run's limits, not the app's defects, so do not "
    "retry or work around them."
)
GATE_REVIEW = (
    "The live browser refuses some requests itself (a write, or anything but GET and HEAD to another "
    "origin), and "
    "each screen's errors file lists them as `gate_refused`, with a failed request it refused marked as the "
    "gate's. A screen whose `gate_refused` is non-empty, together with a console error naming the same URL "
    "(a CORS block, net::ERR_FAILED or a 403), or a loading or error state such a refused request explains, "
    "is a finding about the run: put it in `about_run` ({id, title, actual}, naming the method, origin and "
    "code the gate refused), never in `findings`, and never call it the app's defect. A `gate_refused` of "
    "null means the gate's record could not be read: say so in `about_run`."
)


def _gate_report_line() -> str:
    return (
        "Open report.md with the gate's refusals: add up `gate_refusals` across the capture manifests and "
        "write first \"the gate refused N requests (<count> <method> to <origin>, ...), so these screens "
        "show the app without them:\", by method and origin, then list every screen whose errors file has "
        "a non-empty `gate_refused`; when N is 0 say the gate refused none, and when a manifest's "
        "`gate_refusals` was not read say so. Then, on a line of its own, name what the gate let through: "
        "for each entry of the manifests' `gate_refusals.allowed`, write its label (\"allowed: IdP token "
        "refresh\") with its count, method, origin and path; these are not refusals and never count toward "
        "N. Carry every review's `about_run` into findings.json's "
        "`about_run`, and move there any finding whose evidence is a request in a screen's `gate_refused` "
        "or a console error naming one of its URLs: a gate refusal is never the app's defect."
    )


def _safety(target: Mapping[str, Any], origin: str, *, mutating: bool = False) -> str:
    paths, controls = _exclusions(target)
    writes = (
        "This flow may submit forms on the target, whose environment is non-production; it still never "
        "sends anything to another origin."
        if mutating else
        "Submit no form and send no write: the live browser refuses any POST, PUT, PATCH or DELETE to the "
        "target, and anything but GET to any other origin."
    )
    return (
        f"This is someone else's application. Stay on {origin}: the live browser answers a navigation "
        f"anywhere else with a page saying you left the QA target. {writes} Never visit these paths: "
        f"{', '.join(paths)}. Never click a control whose text names any of: {', '.join(controls)}. "
        "Never solve or hand over an MFA or CAPTCHA challenge."
    )


def _sign_in_note(own_login: bool, clicks: Sequence[Mapping[str, str]] = ()) -> str:
    if not own_login:
        return ""
    note = (
        " The platform signs this browser in before you start; you never see or type a credential. If a "
        "page shows the login form again, run `swarm-handoff sign-in` once and continue; if it still shows "
        "it, say so in your answer and go on with the screens that need no sign-in."
    )
    if clicks:
        # An app that keeps its token in page memory lands the agent's own tab
        # on its chooser again; one click there completes through the identity
        # provider's session, with no form (§2.2, BGD 2026-10-23).
        named = " then ".join(f"'{c['text']}'" for c in clicks)
        note += (
            f" If your tab shows the sign-in chooser, click {named}; the sign-in completes without a form. "
            "Never type into a login form; if one appears, run `swarm-handoff sign-in` once."
        )
    return note


def _leg_name(target: Mapping[str, Any]) -> str:
    """The leg a prompt reports on: the view's name (`leg`), else the name
    §1.2 gives a target of one origin."""
    name = target.get(LEG_NAME)
    if isinstance(name, str) and ENVIRONMENT_NAME.fullmatch(name):
        return name
    return legacy_environment_name(target)


def _crawl_flags(target: Mapping[str, Any], base: str, *, step: int, steps: int, found_per_step: int,
                 bounds: Mapping[str, int]) -> str:
    """`capture --crawl`'s arguments (§3.1, swarm_qa's QAENV-CRAWL): this
    step's number of the leg's `steps`, its cap of new routes, the walk's
    start (the base's path) and bounds, the target's exclusions, and the
    leg's navigation spacing when it sets one. Every value is the target's
    or this module's, each shell-quoted."""
    paths, controls = _exclusions(target)
    flags = [
        "--crawl", f"--step {step}", f"--steps {steps}", f"--found-per-step {found_per_step}",
        f"--crawl-seconds {CRAWL_SECONDS}", f"--start-path {shlex.quote(urlsplit(base).path + '/')}",
        f"--max-pages {bounds['max_pages']}", f"--max-depth {bounds['max_depth']}",
        *(f"--exclude {shlex.quote(glob)}" for glob in paths),
        *(f"--exclude-control {shlex.quote(word)}" for word in controls),
    ]
    rate = target.get("rate")
    navigation = rate.get("navigation_ms") if isinstance(rate, Mapping) else None
    if isinstance(navigation, int) and not isinstance(navigation, bool):
        flags.append(f"--navigation-ms {navigation}")
    return " ".join(flags)


def _capture_prompt(target: Mapping[str, Any], base: str, group: str, routes: Mapping[str, Any], *,
                    crawl: str, found_per_step: int, widths: Sequence[str], themes: Sequence[str],
                    own_login: bool) -> str:
    flags = ["--settle generic", "--axe"]
    if own_login:
        flags.append("--keep-storage")
    if "clock" in routes:
        flags.append("--clock")
    out = f"$SWARM_ARTIFACTS_DIR/capture-{group}"
    # `capture --base` takes the ORIGIN: route paths are origin-relative, as
    # `discover` writes them (`/todomvc/...` under a `/todomvc` base), so a base
    # with its path would load `/todomvc/todomvc/`. Discovery keeps the full
    # base, path included, to know where to start (decided 2026-10-22, QA-ORIGIN).
    return (
        f"Capture, do not review. Visual QA of {target['name']} at {base}.{_sign_in_note(own_login, start_clicks(target))} "
        f"Write this route list, exactly as given, to /tmp/swarm-qa/routes-{group}.json with the Write "
        f"tool:\n\n```json\n{json.dumps(routes, indent=2, sort_keys=True)}\n```\n\n"
        f"It is the whole run's stored list, the same in every capture step; `--group {group}` is this "
        f"step's share of it. Never add a route to it yourself: after this step's routes the tool walks the "
        f"app from the base and every stored route, and captures at most {found_per_step} new routes this "
        f"step owns, writing what it found to found.json in the tarball. "
        f"Then run `python -m swarm_qa capture --routes /tmp/swarm-qa/routes-{group}.json --group {group} "
        f"--base {_origin(base)} --presets {','.join(widths)} --themes {','.join(themes)} {' '.join(flags)} "
        f"{crawl} --out {out} --bundle {out}.tar`. It writes the tarball whatever happens: a route that failed is "
        f"listed in its manifest's `failed`, not a reason to stop. If it exits non-zero, put its last 40 "
        f"lines in your answer and still make sure capture-{group}.tar exists under $SWARM_ARTIFACTS_DIR. "
        f"{GATE_CAPTURE} {_safety(target, _origin(base))} {NO_CHANGE}"
    )


def _review_prompt(target: Mapping[str, Any], base: str, group: str, design_docs: Sequence[str]) -> str:
    return (
        f"Visual QA review of {target['name']} at {base}. First look in the repository for files matching "
        f"{', '.join(design_docs)}; if any exist, read them, and cite a deviation from them by file. Unpack "
        f"capture-{group}.tar. For every PNG its manifest.json lists, open the image with the Read tool and "
        f"look at it, against this checklist: {CHECKLIST}. One finding per defect. For each, write a crop "
        f"PNG named <id>.png showing it (ids start `{group}-`), and an entry in findings-{group}.json, an "
        f'object {{"format": 1, "group": "{group}", "findings": [...], "about_run": [...], "reviewed": <the '
        f"number of images you opened>}}, each finding in exactly this shape:\n\n```json\n"
        f"{json.dumps(FINDING_EXAMPLE, indent=2)}\n```\n\n"
        f"`severity` is one of {', '.join(SEVERITIES)}; `width` is a preset name; `evidence` lists the "
        f"crop and the capture image it came from, by their paths in the tars, and a finding without "
        f"evidence is refused. Quote no more of the page's text than the few words that name the defect. "
        f"{GATE_REVIEW} Bundle findings-{group}.json and the crops into review-{group}.tar under $SWARM_ARTIFACTS_DIR. "
        f"{NO_CHANGE}"
    )


def _flow_prompt(target: Mapping[str, Any], base: str, flow: Mapping[str, Any], widths: Sequence[str],
                 own_login: bool, tool: str) -> str:
    fid = flow["id"]
    runs = []
    for width in widths:
        runs.append(
            f"at `{width}`: run `swarm-handoff device {width}`, then `swarm-handoff record start "
            f"flow-{fid}-{width}`, reach the goal, then `swarm-handoff record stop`"
        )
    return (
        f"Goal-driven flow test of {target['name']}. Open {screen_url(base, flow['start_path'])} in the live "
        f"browser.{_sign_in_note(own_login, start_clicks(target))} {_FLOW_TOOL_SENTENCE[tool]}, reach this goal as a person "
        f"would, without typing addresses: {flow['goal']}. Do it once per width, recorded: "
        f"{'; '.join(runs)}. For each run write in flow-{fid}.json whether the goal was reached, the steps "
        f"taken, and every point where a label was missing, ambiguous or misleading, each with a "
        f"screenshot taken with browser_take_screenshot and no filename (it is saved under "
        f"$SWARM_ARTIFACTS_DIR/screenshots). List in flow-{fid}.json every screenshot you take, by its file "
        f"name, with what it shows. Bundle flow-{fid}.json, the flows/flow-{fid}-*.gif recordings and the "
        f"screenshots into flow-{fid}.tar under $SWARM_ARTIFACTS_DIR. "
        f"{_safety(target, _origin(base), mutating=flow['mutating'])} {NO_CHANGE}"
    )


def _flow_batch_prompt(target: Mapping[str, Any], base: str, batch_id: str, flows: Sequence[Mapping[str, Any]],
                       widths: Sequence[str], own_login: bool, tool: str) -> str:
    """One agent, signed in once, runs `flows` in order, each as `_flow_prompt`
    runs one: recorded at each width, then STOP and capture its own
    flow-<id>.json, recordings and tar before the next. The per-width recipe
    is said once, not under every flow, so a 150-flow leg's prompts stay
    well inside the API's 256 KiB of workflow input."""
    listing = " ".join(
        f"({n}) `{f['id']}`: open {screen_url(base, f['start_path'])}; goal: {f['goal']}."
        for n, f in enumerate(flows, 1)
    )
    return (
        f"Goal-driven flow tests of {target['name']}: {len(flows)} flows, run one after another in this "
        f"order, in the one live browser.{_sign_in_note(own_login, start_clicks(target))} The browser stays "
        f"signed in from one flow to the next: sign in no more than once. {_FLOW_TOOL_SENTENCE[tool]}, reach "
        f"each flow's goal as a person would, without typing addresses after opening its start page. For "
        f"each flow <id>, at each width of {', '.join(widths)}: run `swarm-handoff device <width>`, then "
        f"`swarm-handoff record start flow-<id>-<width>`, open the flow's start page, reach the goal, then "
        f"`swarm-handoff record stop`. Then STOP and capture that flow before you start the next: write "
        f"flow-<id>.json saying, for each width, whether the goal was reached, the steps taken, and every "
        f"point where a label was missing, ambiguous or misleading, each with a screenshot taken with "
        f"browser_take_screenshot and no filename (it is saved under $SWARM_ARTIFACTS_DIR/screenshots); list "
        f"in flow-<id>.json every screenshot you took during that flow, by its file name, with what it shows; "
        f"and bundle flow-<id>.json, the flows/flow-<id>-*.gif recordings and those screenshots into "
        f"flow-<id>.tar under $SWARM_ARTIFACTS_DIR. A flow you cannot finish still gets its flow-<id>.json, "
        f"saying where it stopped and why, and its flow-<id>.tar; then go on to the next flow. The flows: "
        f"{listing} Last, bundle every flow-<id>.tar of this batch, and nothing else, into {batch_id}.tar "
        f"under $SWARM_ARTIFACTS_DIR. {_safety(target, _origin(base))} {NO_CHANGE}"
    )


def _report_prompt(target: Mapping[str, Any], groups: Sequence[tuple[str, Sequence[Any]]],
                   widths: Sequence[str], themes: Sequence[str]) -> str:
    names = [g for g, _ in groups]
    return (
        f"Write the visual QA report of {target['name']}'s screens. Unpack every staged tar, each into its "
        f"own directory named after its step: capture-<g>.tar and review-<g>.tar for each group of "
        f"{', '.join(names)}. For each group, count the images its capture's manifest.json lists and check "
        f"the review's `reviewed` against that number; a shortfall is a finding about the run, naming both "
        f"numbers. A staged tar that is missing is a finding about the run, naming the tar; when a capture "
        f"tar is missing, the group's expected count is its stored routes x {len(widths)} widths x "
        f"{len(themes)} themes x each route's segments (4 when unset), and say which count you used and that "
        f"the found routes' count is unknown. Merge every finding "
        f"into findings.json, {{\"format\": 1, \"findings\": [...], \"about_run\": [...]}}, de-duplicating the "
        f"same defect seen at several widths or themes into one finding whose `also_seen` lists the others. "
        f"A finding is kept only when every file its `evidence` names is in a staged tar; one that names "
        f"none, or one no tar holds, moves to `about_run` with what was missing. `about_run` entries are "
        f"{{id, title, actual}} and are never filed in the application's repository. Copy every evidence "
        f"file to $SWARM_ARTIFACTS_DIR next to findings.json, keeping its path, so the console shows it. "
        f"{_gate_report_line()} Then, in report.md, one line saying the flows are reported apart, in "
        f"flow-report's flows.md; then a table of findings by severity and by screen, then each finding, severity first, with its "
        f"crop embedded by relative link. {_coverage_line(_leg_name(target))} {NO_CHANGE}"
    )


def _coverage_line(leg_name: str) -> str:
    """§3.5: the leg's Coverage section, from every capture's found.json."""
    return (
        f"End report.md with a section headed \"Coverage ({leg_name})\", read from every capture tar's "
        f"found.json: the stored routes captured; the new routes captured, with their paths "
        f"(`captured_new`); the routes found but over the cap (`over_cap`); the routes found but not reached "
        f"by their owner (in a step's `not_owned` and in no step's `captured_new` or `over_cap`); and for "
        f"each step why its crawl stopped (`crawl.stopped_by`). When a crawl stopped by `time` or `pages`, "
        f"say so plainly: a route missing then is no evidence that it is gone. A capture tar with no "
        f"found.json is said so, by name."
    )


def _flow_report_prompt(flows: Sequence[Mapping[str, Any]],
                        batches: Sequence[tuple[str, Sequence[Mapping[str, Any]]]] = ()) -> str:
    """The flows' report. `batches` is (batch step id, its flows): each batch
    tar carries one flow-<id>.tar per flow of its batch; a mutating flow's own
    step stages its flow-<id>.tar directly. Every flow is named by its tar."""
    batched = {f["id"] for _, members in batches for f in members}
    staged = []
    for batch_id, members in batches:
        inner = ", ".join(f"flow-{f['id']}.tar" for f in members)
        staged.append(f"{batch_id}.tar, holding {inner}")
    staged += [f"flow-{f['id']}.tar" for f in flows if f["id"] not in batched]
    order = ", ".join(f"`{f['id']}`" for f in flows)
    return (
        f"Write the visual QA report of the flows. Unpack every staged tar, and every flow tar a batch tar "
        f"holds, each flow's into its own directory: {'; '.join(staged)}. A flow tar that is missing, from "
        f"its batch tar or because the batch tar is missing, is itself a finding, naming the flow. The "
        f"flows, in order: {order}. Write flows.md: one section per flow, and in it, for each "
        f"width, whether the goal was reached, the steps, and every finding with its screenshot embedded by "
        f"relative link; a screenshot no json describes is listed as one nobody described. Copy every "
        f"screenshot and recording to $SWARM_ARTIFACTS_DIR next to flows.md. {NO_CHANGE}"
    )


def _file_prompt(target: Mapping[str, Any], settings: Mapping[str, Any],
                 group: Mapping[str, Any] | None = None) -> str:
    tid = target["id"]
    # §7, BGD's Q8 (a): one issue per finding per environment. The leg is in
    # the key, so the legs' concurrent file steps never race on one key, and
    # in the title, so a reader tells them apart.
    leg_name = _leg_name(target)
    labels = json.dumps(settings["labels"])
    order = SEVERITIES[: SEVERITIES.index(settings["min_severity"]) + 1]
    if settings["evidence"] == "branch":
        evidence = (
            "Push the evidence images of the findings you file, and nothing else, to the branch "
            "swarm/<this task's id> with `git_push_branch`, and embed each in its issue by its blob URL."
        )
    else:
        evidence = (
            "Link each finding's evidence by its file name in this run's Artifacts pane in the SwarmCloud "
            "console, which the repository's members can open; upload no image and push no branch. Describe "
            "the defect fully in words, so the issue stands without the image."
        )
    return (
        f"File the visual QA findings of {target['name']} in this repository. Read findings.json. File the "
        f"findings whose severity is one of {', '.join(order)}, most severe first, at most "
        f"{settings['max_issues']} issues; list the rest, one line each, in one roll-up issue titled "
        f"\"Visual QA of {target['name']}, <today's date>: <n> further findings\", so nothing is silently "
        f"dropped. Never file `about_run`. Never file a finding whose evidence is a gate refusal: one whose "
        f"evidence or text names a request a screen's `gate_refused` lists, a request the live gate refused, "
        f"or a CORS block or failed request to such a URL is the run's limit, not the app's defect; list it "
        f"in filed.json as not filed, with that reason. For each finding, first call `issue_list` for an open issue on "
        f"the same path naming the same defect; if one exists, comment on it with `issue_comment` instead "
        f"and say so in your answer. Otherwise call `issue_create` with a title that starts `[{leg_name}]` "
        f"(this run's environment) and `dedupe_key` "
        f"\"visual-qa:{tid}:{leg_name}:<screen>:<the title, lower-cased, spaces collapsed>\"; an answer "
        f"{{existing: N}} means it is filed already: comment on N with this run and the new evidence. Use "
        f"the repository's bug form when it has one, else `form: \"plain\"` with the sections Summary, "
        f"Where (path, width, theme), Actual, Expected, Steps to reproduce and Evidence, ending \"Found by "
        f"SwarmCloud visual QA\".{_group_sentence(leg_name, group)} Labels: {labels} and severity:<s>{_label_rules_sentence(settings)}; the "
        f"broker applies only labels that exist. Before any write, remove from the text every e-mail address, phone number and anything "
        f"credential-shaped the page showed: quote no more than the words that name the defect. {evidence} "
        f"Write filed.json listing each finding id with the issue number filed or commented on"
        f"{_label_rules_report(settings)}. {NO_CHANGE}"
    )


def _group_sentence(leg_name: str, group: Mapping[str, Any] | None) -> str:
    """A grouped run's link to its other legs (§7): the run group's page,
    which shows every leg's workflow. The group id and the leg names are
    swarm-api's, never a caller's text. "" for a run of one leg."""
    if not group:
        return ""
    group_id = group.get("group_id")
    others = [n for n in group.get("legs") or () if isinstance(n, str) and ENVIRONMENT_NAME.fullmatch(n)
              and n != leg_name]
    if not isinstance(group_id, str) or not _GROUP_ID.fullmatch(group_id) or not others:
        return ""
    return (f" This run is the {leg_name} leg of one dispatch over {', '.join([leg_name, *others])}: end each "
            f"issue's body with a line linking the other leg's run, the SwarmCloud console's run page "
            f"/qa-runs/{group_id}, which shows each leg's workflow.")


def _label_rules_sentence(settings: Mapping[str, Any]) -> str:
    rules = settings["label_rules"]
    if not rules:
        return ""
    stated = "; ".join(
        f"rule {n}: a finding whose `path` starts with {' or '.join(json.dumps(p) for p in rule['paths'])} "
        f"also gets {json.dumps(rule['labels'])}"
        for n, rule in enumerate(rules, 1)
    )
    return (f". Then the label rules, matched against the finding's `path` without its query, every rule that "
            f"matches adding its labels and a finding no rule matches getting none of them: {stated}")


def _label_rules_report(settings: Mapping[str, Any]) -> str:
    if not settings["label_rules"]:
        return ""
    return (", and for each the `label_rules` that labelled it, by rule number (an empty list when none "
            "did); end your answer with one line per rule naming the issues it labelled")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _step(step_id: str, profile: str, model: str, prompt: str, *, timeout: int,
          depends_on: Sequence[str] = (), input_from: Mapping[str, str] | None = None,
          browser_tool: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "runner_profile": profile, "model_class": model}
    if browser_tool:
        step["inputs"] = {"browser_tool": browser_tool}
    if depends_on:
        step["depends_on"] = list(depends_on)
    if input_from:
        step["input_from"] = dict(input_from)
    # Every step: nothing is pushed, so an empty diff is the success.
    step["allow_empty_diff"] = True
    step["timeout_seconds"] = timeout
    step["prompt"] = prompt
    return step


def _workflow(target: Mapping[str, Any], title: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "title": title,
        "label": "visual-qa",
        "repository_url": _repository(target),
        "strategy": "collect",
        # A QA run files issues and changes no code: there is nothing to merge.
        "merge": "off",
        # A failed flow or capture never costs the other steps' findings.
        "on_step_failure": "continue",
        "steps": steps,
    }


def render(target: Mapping[str, Any], *, screens: Sequence[str] | None = None,
           file: str = "issues", browser_tool: str | None = None,
           group: Mapping[str, Any] | None = None, flows_per_step: Any = None) -> dict[str, Any]:
    """The QA run's workflow spec for `target`, as `read_spec` reads it.

    `screens` narrows the run to those screen ids; `file="none"` renders the
    report without the filing step (§6); `browser_tool` runs this render's
    flows with that tool instead of the target's (`flows_browser_tool`); `group`,
    `{group_id, legs}`, is the dispatch this leg is one of, whose page the
    filed issues link (§7); `flows_per_step` how many read-only flows each
    flow-batch step runs (`check_flows_per_step`, 15 when None). Raises
    `QaSpecError` for anything the spec cannot hold; never returns a partial spec."""
    if file not in FILE_MODES:
        raise _invalid(f"file must be one of {', '.join(FILE_MODES)}")
    # `group` names a capture group below; the dispatch is kept apart.
    run_group = group
    name = _text(target.get("name"), "name", limit=100)
    target = {**target, "name": name}
    base = base_url(target)
    own_login = _own_login(target)
    widths = _names(target.get("widths"), PRESETS, DEFAULT_WIDTHS, "widths")
    themes = _names(target.get("themes"), THEMES, DEFAULT_THEMES, "themes")
    stored = _screens(target, screens)
    bounds = _discover(target)
    # §3.1: the stored routes grouped as before, or, from nothing but the
    # base, `discover.slots` steps with no stored route of their own.
    groups = group_screens(stored) if stored else [(f"g{k}", []) for k in range(1, bounds["slots"] + 1)]
    # Cut so that stored plus every step at its cap stays within MAX_SCREENS
    # (swarm_qa cuts the same way; the prompt says the figure it will use).
    found_per_step = min(bounds["found_per_step"], max(0, (MAX_SCREENS - len(stored)) // len(groups)))
    flows = _flows(target)
    per_step = check_flows_per_step(flows_per_step)
    flow_tool = flows_browser_tool(target, browser_tool)
    design_docs = _design_docs(target)
    settings = _file_settings(target)
    clock = _clock(target)
    _repository(target)
    if file == "issues":
        _target_id(target)

    steps: list[dict[str, Any]] = []
    # The whole run's stored list, the same in every step (§3.1): an empty
    # group is no group, so a run from the base writes `groups: {}`.
    # The target's dismissed routes go with it, so no step's crawl lists,
    # loads or captures one (QA-CRAWL-SKIP). `found_per_step` is not cut for
    # them: a dismissed route is never stored, so it spends none of the 60.
    run_routes = route_list([(g, m) for g, m in groups if m], clock=clock,
                            dismissed=dismissed_hashes(target))
    for k, (group, _members) in enumerate(groups, 1):
        crawl = _crawl_flags(target, base, step=k, steps=len(groups), found_per_step=found_per_step,
                             bounds=bounds)
        steps.append(_step(
            f"capture-{group}", CAPTURE_PROFILE, CAPTURE_MODEL,
            _capture_prompt(target, base, group, run_routes, crawl=crawl, found_per_step=found_per_step,
                            widths=widths, themes=themes, own_login=own_login),
            timeout=CRAWL_CAPTURE_TIMEOUT, browser_tool="playwright"))
    for group, _members in groups:
        steps.append(_step(
            f"review-{group}", REVIEW_PROFILE, REVIEW_MODEL, _review_prompt(target, base, group, design_docs),
            timeout=3600, depends_on=[f"capture-{group}"],
            input_from={f"capture-{group}": f"capture-{group}.tar"}))
    # QA-FLOWS-CAP: the read-only flows as ceil(n / per_step) flow-batch
    # steps, `flows-<k>`, one agent each. The id can be no flow's step id
    # (`flow-<id>`), so the gate's write grant never reads a batch as a flow.
    batches = [(f"flows-{k}", members) for k, members in enumerate(flow_batches(flows, per_step), 1)]
    flow_tars: dict[str, str] = {}
    for batch_id, members in batches:
        steps.append(_step(
            batch_id, CAPTURE_PROFILE, FLOW_MODEL,
            _flow_batch_prompt(target, base, batch_id, members, widths, own_login, flow_tool),
            timeout=batch_timeout(len(members)), browser_tool=flow_tool))
        flow_tars[batch_id] = f"{batch_id}.tar"
    for flow in flows:
        if not flow["mutating"]:
            continue
        steps.append(_step(
            f"flow-{flow['id']}", CAPTURE_PROFILE, FLOW_MODEL,
            _flow_prompt(target, base, flow, widths, own_login, flow_tool), timeout=FLOW_SECONDS,
            browser_tool=flow_tool))
        flow_tars[f"flow-{flow['id']}"] = f"flow-{flow['id']}.tar"
    staged = {f"{kind}-{g}": f"{kind}-{g}.tar" for kind in ("capture", "review") for g, _ in groups}
    steps.append(_step("report", REVIEW_PROFILE, REPORT_MODEL, _report_prompt(target, groups, widths, themes),
                       timeout=3600, depends_on=list(staged), input_from=staged))
    if flows:
        steps.append(_step("flow-report", REVIEW_PROFILE, REPORT_MODEL, _flow_report_prompt(flows, batches),
                           timeout=1800, depends_on=list(flow_tars), input_from=flow_tars))
    if file == "issues":
        steps.append(_step("file", REVIEW_PROFILE, FILE_MODEL, _file_prompt(target, settings, run_group),
                           timeout=2700, depends_on=["report"], input_from={"report": "findings.json"}))
    _hold_step_bound(steps, batches)
    return _workflow(target, f"Visual QA: {name}", steps)


def _hold_step_bound(steps: Sequence[Mapping[str, Any]],
                     batches: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]) -> None:
    """Refuse a leg whose prompts pass `MAX_INPUT_BYTES`, or of more than
    `MAX_WORKFLOW_STEPS` steps, naming the
    smallest `flows_per_step` that fits when one does. Everything but the
    batches is fixed by the target (at most 8 captures, 8 reviews, the
    report, the flow report, the filing and one step per mutating flow), so
    with 60 screens and no mutating flow 31 batches remain: 150 flows need 5."""
    size = sum(len(str(step["prompt"]).encode("utf-8")) for step in steps)
    if size > MAX_INPUT_BYTES:
        raise _invalid(
            f"this leg's prompts come to {size} bytes and a workflow's input holds at most {MAX_INPUT_BYTES}: "
            f"shorten the flows' goals, or raise flows_per_step (each batch says its instructions once)")
    if len(steps) <= MAX_WORKFLOW_STEPS:
        return
    fixed = len(steps) - len(batches)
    readonly = sum(len(members) for _, members in batches)
    room = MAX_WORKFLOW_STEPS - fixed
    need = math.ceil(readonly / room) if room > 0 else None
    if need is not None and need <= MAX_FLOWS_PER_STEP:
        raise _invalid(
            f"this leg renders {len(steps)} steps and a workflow holds at most {MAX_WORKFLOW_STEPS}: with "
            f"{readonly} read-only flows, flows_per_step must be at least {need}")
    raise _invalid(
        f"this leg renders {len(steps)} steps and a workflow holds at most {MAX_WORKFLOW_STEPS}, whatever "
        f"flows_per_step is: it has {fixed} steps besides the flow batches (each mutating flow is one)")


def render_discovery(target: Mapping[str, Any]) -> dict[str, Any]:
    """The one-step discovery run (§4.1 step 1): same-origin links from the
    rendered pages, bounded, exclusions and `robots.txt` honoured, written as
    `screens.json` in the route-list format. swarm-api stores the result on
    the target as `discovered`; the QA run starts from it."""
    name = _text(target.get("name"), "name", limit=100)
    target = {**target, "name": name}
    base = base_url(target)
    own_login = _own_login(target)
    if not isinstance(target.get("discover"), Mapping):
        raise _invalid("discover must be {max_pages, max_depth, slots?, found_per_step?}")
    bounds = _discover(target)
    pages, depth = bounds["max_pages"], bounds["max_depth"]
    paths, _ = _exclusions(target)
    excludes = " ".join(f"--exclude '{p}'" for p in paths)
    prompt = (
        f"Discover the screens of {target['name']}, do not review them.{_sign_in_note(own_login, start_clicks(target))} Run "
        f"`python -m swarm_qa discover --base {base} --max-pages {pages} --max-depth {depth} {excludes} "
        f"--out $SWARM_ARTIFACTS_DIR/screens.json`. It stays on the target's origin and honours its "
        f"robots.txt. If it exits non-zero, put its last 40 lines in your answer. "
        f"{_safety(target, _origin(base))} {NO_CHANGE}"
    )
    step = _step("discover", CAPTURE_PROFILE, CAPTURE_MODEL, prompt, timeout=1800, browser_tool="playwright")
    return _workflow(target, f"Visual QA discovery: {name}", [step])


def discovered_screens(target: Mapping[str, Any], document: Any) -> list[dict[str, str]]:
    """The discovery step's `screens.json` (`swarm_qa discover`'s route list,
    format 1) as the `discovered` list swarm-api stores on the target: each
    route's `{id, path}`, in the document's order (lane QA-DISCOVER).

    Held as strictly as `screens` at registration, because the QA run starts
    from it and an agent wrote it. A route off the target's origin (another
    host, scheme or port, or `//host`) is dropped, and a same-origin absolute
    URL is kept as its path; a route on an excluded path is dropped; past
    `MAX_SCREENS` the rest is cut. An ill-formed id or path, or two routes
    sharing an id, refuses the whole document: the tool never writes one, so
    it is not the tool's. Nothing left is refused too: a run needs a screen.
    Raises `QaSpecError`, whose message never quotes the document."""
    if not isinstance(document, Mapping) or document.get("format") != 1 \
            or not isinstance(document.get("groups"), Mapping):
        raise _invalid("screens.json is not a route list (format 1, groups of routes)")
    origin = _origin(base_url(target))
    excluded, _ = _exclusions(target)
    kept: list[dict[str, str]] = []
    for routes in document["groups"].values():
        if not isinstance(routes, list):
            raise _invalid("screens.json is not a route list (format 1, groups of routes)")
        for raw in routes:
            screen = _held_route(raw, origin, excluded)
            if screen is not None:
                kept.append(screen)
    ids = [s["id"] for s in kept]
    if len(set(ids)) != len(ids):
        raise _invalid("two discovered screens share an id")
    if not kept:
        raise QaSpecError("qa_target_no_screens",
                          "the discovery found no screen on the target's origin outside its exclusions")
    return kept[:MAX_SCREENS]


def _held_route(raw: Any, origin: str, excluded: Sequence[str]) -> dict[str, str] | None:
    """One route an agent's crawl wrote, as `{id, path}`, or None when it is
    dropped: off the leg's origin (another host, scheme or port, or
    `//host`) or on an excluded path. A same-origin absolute URL is kept as
    its path. An ill-formed id or path raises: the tool never writes one."""
    if not isinstance(raw, Mapping):
        raise _invalid("a discovered route must be {id, path}")
    path = raw.get("path")
    if isinstance(path, str) and (path.startswith("//") or "://" in path.split("?", 1)[0]):
        if path.startswith("//") or _origin(path).lower() != origin:
            return None
        parts = urlsplit(path)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "") \
            + (f"#{parts.fragment}" if parts.fragment.startswith("/") else "")
    try:
        screen = _screen({"id": raw.get("id"), "path": path}, excluded)
    except QaSpecError as exc:
        if exc.code == "qa_screen_excluded":
            return None
        raise
    return {"id": screen["id"], "path": screen["path"]}


#: `swarm_qa.STATE_MARK`: separates a route state's path from its reveal
#: texts in its key, the string that names one route across steps and runs.
STATE_MARK = " >> "
#: What a crawl's `crawl.stopped_by` may say (§3.5); only `done` is complete.
CRAWL_STOPS = ("done", "pages", "depth", "time")
#: found.json's route lists (§3.5): every route a step listed as new, whoever owns it.
FOUND_LISTS = ("captured_new", "not_owned", "over_cap")


def route_key(route: Mapping[str, Any]) -> str:
    """What names a route (`swarm_qa.state_key`): its path, and for a route
    state the texts it reveals by."""
    return str(route["path"]) + "".join(STATE_MARK + str(c["text"]) for c in route.get("reveal") or [])


@dataclass(frozen=True)
class FoundRoutes:
    """One capture step's `found.json`, held (`found_routes`): the new routes
    it listed (`{id, path, reveal?}`, first listing wins), every route key
    it saw (its stored list's and its new routes'), and whether its crawl
    was complete (`stopped_by: done`), the one crawl whose absence of a
    route is evidence (§5)."""

    routes: list[dict[str, Any]]
    seen: frozenset[str]
    complete: bool


def found_routes(target: Mapping[str, Any], document: Any) -> FoundRoutes:
    """A crawling capture step's `found.json` (qa-environments.md §3.5) as
    swarm-api stores it (§5; lane QAENV-SETTLE). Held as `discovered_screens`
    holds a discovery's list, because an agent wrote it: a route off the
    leg's origin or on an excluded path is dropped; an ill-formed id or path,
    an ill-formed `reveal` (held to the start-click rules, `reveal_clicks`),
    or two routes sharing an id refuse the whole document. Nothing found is
    no refusal: a crawl that found nothing new is the common case. Raises
    `QaSpecError`, whose message never quotes the document."""
    shape = "found.json is not a crawl's record (format 1, captured_new, not_owned, over_cap, crawl)"
    if not isinstance(document, Mapping) or document.get("format") != 1:
        raise _invalid(shape)
    crawl = document.get("crawl")
    stored = document.get("stored", [])
    if not isinstance(crawl, Mapping) or crawl.get("stopped_by") not in CRAWL_STOPS \
            or not isinstance(stored, list) or not all(isinstance(k, str) for k in stored):
        raise _invalid(shape)
    origin = _origin(base_url(target))
    excluded, _ = _exclusions(target)
    routes: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    for name in FOUND_LISTS:
        rows = document.get(name, [])
        if not isinstance(rows, list):
            raise _invalid(shape)
        for raw in rows:
            route: dict[str, Any] | None = _held_route(raw, origin, excluded)
            if route is None:
                continue
            if raw.get("reveal") is not None:
                route["reveal"] = reveal_clicks(target, raw["reveal"])
            key = route_key(route)
            if by_id.setdefault(route["id"], key) != key:
                raise _invalid("two found routes share an id")
            if key not in {route_key(r) for r in routes}:
                routes.append(route)
    seen = frozenset(stored) | {route_key(r) for r in routes}
    return FoundRoutes(routes=routes, seen=seen, complete=crawl["stopped_by"] == "done")


def _target_id(target: Mapping[str, Any]) -> str:
    tid = target.get("id")
    if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
        raise _invalid("the target has no id")
    return tid


def qa_target_metadata(target: Mapping[str, Any]) -> dict[str, Any]:
    """`metadata.qa_target` for each agent of `browser_steps` (§1.1): written by
    swarm-api at submission, never accepted from a caller, and what the
    worker's attempt route resolves the target from. A leg of a target stored
    with `environments` adds its name (§1.5), so the route serves that entry
    and no other; an unmigrated target's mark is `{id, revision}` as before."""
    revision = target.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise _invalid("the target has no revision")
    mark: dict[str, Any] = {"id": _target_id(target), "revision": revision}
    if target.get(MIGRATED) is True:
        name = target.get(LEG_NAME)
        if not isinstance(name, str) or not ENVIRONMENT_NAME.fullmatch(name):
            raise _invalid("the target's environment has no name")
        mark["environment"] = name
    return mark


def browser_steps(spec: Mapping[str, Any]) -> list[str]:
    """The step ids that drive the live browser, and so get the target stamped
    on their agent. A review, report or filing step never touches the app."""
    return [s["step_id"] for s in spec.get("steps", ()) if s.get("runner_profile") == CAPTURE_PROFILE]


# --------------------------------------------------------------------------
# The findings validator (§4.4)
# --------------------------------------------------------------------------


@dataclass
class FindingsCheck:
    """A findings document, split. `accepted` may be filed; `refused` each
    carry the finding's id and what is wrong (field names, never a value from
    the page); `about_run` are the report's findings about the run itself,
    reported and never filed in the application's repository."""

    accepted: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    about_run: list[dict[str, Any]] = field(default_factory=list)


def _one_line(value: Any, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and "\n" not in value


def _prose(value: Any, limit: int = 4000) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _problems(finding: Mapping[str, Any], available: Collection[str] | None) -> list[str]:
    problems: list[str] = []
    if not _one_line(finding.get("title"), 200):
        problems.append("title must be one line of at most 200 characters")
    if finding.get("severity") not in SEVERITIES:
        problems.append(f"severity must be one of {', '.join(SEVERITIES)}")
    screen = finding.get("screen")
    if not isinstance(screen, str) or not _NAME.fullmatch(screen):
        problems.append("screen must be the screen's id")
    if not isinstance(finding.get("path"), str) or not _PATH.fullmatch(finding["path"]):
        problems.append("path must be the origin-relative path the defect is on")
    if finding.get("width") not in PRESETS:
        problems.append(f"width must be a preset: {', '.join(PRESETS)}")
    if finding.get("theme") not in THEMES:
        problems.append(f"theme must be one of {', '.join(THEMES)}")
    for key in ("actual", "expected"):
        if not _prose(finding.get(key)):
            problems.append(f"{key} must say, in words, what the defect is")
    steps = finding.get("steps_to_reproduce")
    if not isinstance(steps, list) or not steps or not all(_prose(s, 500) for s in steps):
        problems.append("steps_to_reproduce must be a non-empty list of steps")
    also = finding.get("also_seen", [])
    if not isinstance(also, list) or not all(
        isinstance(a, Mapping) and a.get("width") in PRESETS and a.get("theme") in THEMES for a in also
    ):
        problems.append("also_seen must be a list of {width, theme}")
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        # §4.4: a finding nobody can see is not filed; the report moves it to
        # the run's own findings.
        problems.append("evidence must list at least one image file")
    else:
        bad = [e for e in evidence if not isinstance(e, str) or not _EVIDENCE.fullmatch(e)]
        if bad:
            problems.append("evidence must be relative image paths inside the staged tars")
        elif available is not None:
            missing = [e for e in evidence if e not in available]
            if missing:
                problems.append(f"evidence not in any staged tar: {', '.join(missing)}")
    return problems


def validate_findings(document: Any, *, available: Collection[str] | None = None) -> FindingsCheck:
    """Split a report's `findings.json` into what may be filed and what may not.

    `available`, when given, is every file name the staged tars hold: a
    finding whose evidence names one they do not is refused, as one with no
    evidence is. A document that is not format 1 with a `findings` list is
    `qa_findings_invalid`."""
    if not isinstance(document, Mapping) or document.get("format") != 1 or not isinstance(
        document.get("findings"), list
    ):
        raise QaSpecError("qa_findings_invalid", 'findings.json must be {"format": 1, "findings": [...]}')
    about = document.get("about_run", [])
    if not isinstance(about, list):
        raise QaSpecError("qa_findings_invalid", "about_run must be a list")
    check = FindingsCheck()
    seen: set[str] = set()
    for index, finding in enumerate(document["findings"]):
        fid = finding.get("id") if isinstance(finding, Mapping) else None
        named = isinstance(fid, str) and bool(_FINDING_ID.fullmatch(fid))
        label = fid if named else f"#{index}"
        if not isinstance(finding, Mapping):
            check.refused.append({"id": label, "problems": ["a finding must be an object"]})
            continue
        problems = [] if named else ["id must be lower-case letters, digits and hyphens"]
        if named and fid in seen:
            problems.append("id repeats an earlier finding's")
        problems += _problems(finding, available)
        if problems:
            check.refused.append({"id": label, "problems": [redact(p).text for p in problems]})
            continue
        seen.add(fid)
        check.accepted.append(dict(finding))
    for entry in about:
        if isinstance(entry, Mapping) and _one_line(entry.get("title"), 300):
            check.about_run.append({k: (redact(v).text if isinstance(v, str) else v) for k, v in entry.items()})
    return check
