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
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Sequence
from urllib.parse import urlsplit

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
#: `swarm_qa`'s own bounds (`MAX_SEGMENTS`, `NAME`), which the capture tool
#: refuses past; restated so a target is refused at registration, not at run.
MAX_SEGMENTS = 12
MAX_FLOWS = 10
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
#: An origin-relative path: no scheme, no authority, no whitespace, nothing a
#: prompt's command line or a JSON string would read as syntax.
_PATH = re.compile(r"/(?!/)[A-Za-z0-9._~%!$&()*+,;=:@/?-]{0,511}")
_GLOB = re.compile(r"/[A-Za-z0-9._~%!$&()*+,;=:@/?\[\]-]{0,255}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+")
_BASE_PATH = re.compile(r"(?:/[A-Za-z0-9._~%-]*)*")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}")
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
    raw = target.get("screens")
    if raw is None:
        raw = target.get("discovered")
    if not isinstance(raw, list) or not raw:
        raise QaSpecError("qa_target_no_screens",
                          "the target lists no screens and none were discovered; run its discovery first")
    excluded, _ = _exclusions(target)
    screens = [_screen(s, excluded) for s in raw]
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


def _environment(target: Mapping[str, Any]) -> str:
    # Absent is production: the safe reading of someone else's application.
    env = target.get("environment") or "production"
    if env not in ENVIRONMENTS:
        raise _invalid("environment must be production or non_production")
    return env


def _flows(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = target.get("flows") or []
    if not isinstance(raw, list) or len(raw) > MAX_FLOWS:
        raise _invalid(f"flows must be a list of at most {MAX_FLOWS}")
    production = _environment(target) == "production"
    excluded, _ = _exclusions(target)
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
        if _excluded(start, excluded):
            raise QaSpecError("qa_screen_excluded", f"flow {fid} starts on an excluded path")
        flows.append({"id": fid, "goal": _text(item.get("goal"), f"flow {fid}'s goal", limit=500),
                      "start_path": start, "mutating": mutating})
    if len({f["id"] for f in flows}) != len(flows):
        raise _invalid("two flows share an id")
    return flows


def _file_settings(target: Mapping[str, Any]) -> dict[str, Any]:
    raw = target.get("file") or {}
    if not isinstance(raw, Mapping):
        raise _invalid("file must be {min_severity, max_issues, labels, evidence}")
    out = {**DEFAULT_FILE, **raw}
    if out["min_severity"] not in SEVERITIES:
        raise _invalid(f"file.min_severity must be one of {', '.join(SEVERITIES)}")
    n = out["max_issues"]
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_ISSUES:
        raise _invalid(f"file.max_issues must be 1..{MAX_ISSUES}")
    labels = out["labels"]
    if not isinstance(labels, (list, tuple)) or len(labels) > 10 or not all(
        isinstance(x, str) and _LABEL.fullmatch(x) for x in labels
    ):
        raise _invalid("file.labels must be at most 10 label names")
    if out["evidence"] not in EVIDENCE_MODES:
        raise _invalid(f"file.evidence must be one of {', '.join(EVIDENCE_MODES)}")
    out["labels"] = list(labels)
    return out


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
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_START_CLICKS:
        raise _invalid(f"sign_in.start_clicks must list 1 to {MAX_START_CLICKS} clicks, each {{text, css?}}")
    _, controls = _exclusions(target)
    refused = [w.lower() for w in (*controls, *START_CLICK_REFUSED)]
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"text", "css"}:
            raise _invalid(f"sign_in.start_clicks must list 1 to {MAX_START_CLICKS} clicks, each {{text, css?}}")
        text = _text(item.get("text"), "a start click's text", limit=START_CLICK_TEXT_MAX)
        if any(word in text.lower() for word in refused):
            raise _invalid("a start click's text names an excluded control (the defaults, logout and the "
                           "target's own): the gate never clicks one, before a sign-in least of all")
        click = {"text": text}
        if item.get("css") is not None:
            css = _text(item["css"], "a start click's css", limit=START_CLICK_CSS_MAX)
            if _PLAYWRIGHT_SELECTOR.search(css):
                raise _invalid("a start click's css is a CSS selector the page itself takes; Playwright "
                               "syntax such as :has-text() is refused because the gate clicks over CDP, "
                               "not through Playwright. Put the control's visible text in text")
            click["css"] = css
        out.append(click)
    return out


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
               clock: str | None = None) -> dict[str, Any]:
    """The capture tool's route-list document (`swarm_qa.load_routes`, format
    1) for `groups`. `clock` only when the target sets one (§4.2)."""
    document: dict[str, Any] = {
        "format": 1,
        "groups": {
            name: [{"id": s["id"], "path": s["path"], "ready": s.get("ready", DEFAULT_READY),
                    **({"segments": s["segments"]} if "segments" in s else {})} for s in screens]
            for name, screens in groups
        },
    }
    if clock is not None:
        document["clock"] = clock
    return document


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


def _capture_prompt(target: Mapping[str, Any], base: str, group: str, routes: Mapping[str, Any], *,
                    widths: Sequence[str], themes: Sequence[str], own_login: bool) -> str:
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
        f"Then run `python -m swarm_qa capture --routes /tmp/swarm-qa/routes-{group}.json --group {group} "
        f"--base {_origin(base)} --presets {','.join(widths)} --themes {','.join(themes)} {' '.join(flags)} "
        f"--out {out} --bundle {out}.tar`. It writes the tarball whatever happens: a route that failed is "
        f"listed in its manifest's `failed`, not a reason to stop. If it exits non-zero, put its last 40 "
        f"lines in your answer and still make sure capture-{group}.tar exists under $SWARM_ARTIFACTS_DIR. "
        f"{_safety(target, _origin(base))} {NO_CHANGE}"
    )


def _review_prompt(target: Mapping[str, Any], base: str, group: str, design_docs: Sequence[str]) -> str:
    return (
        f"Visual QA review of {target['name']} at {base}. First look in the repository for files matching "
        f"{', '.join(design_docs)}; if any exist, read them, and cite a deviation from them by file. Unpack "
        f"capture-{group}.tar. For every PNG its manifest.json lists, open the image with the Read tool and "
        f"look at it, against this checklist: {CHECKLIST}. One finding per defect. For each, write a crop "
        f"PNG named <id>.png showing it (ids start `{group}-`), and an entry in findings-{group}.json, an "
        f'object {{"format": 1, "group": "{group}", "findings": [...], "reviewed": <the number of images '
        f"you opened>}}, each finding in exactly this shape:\n\n```json\n"
        f"{json.dumps(FINDING_EXAMPLE, indent=2)}\n```\n\n"
        f"`severity` is one of {', '.join(SEVERITIES)}; `width` is a preset name; `evidence` lists the "
        f"crop and the capture image it came from, by their paths in the tars, and a finding without "
        f"evidence is refused. Quote no more of the page's text than the few words that name the defect. "
        f"Bundle findings-{group}.json and the crops into review-{group}.tar under $SWARM_ARTIFACTS_DIR. "
        f"{NO_CHANGE}"
    )


def _flow_prompt(target: Mapping[str, Any], base: str, flow: Mapping[str, Any], widths: Sequence[str],
                 own_login: bool) -> str:
    fid = flow["id"]
    runs = []
    for width in widths:
        runs.append(
            f"at `{width}`: run `swarm-handoff device {width}`, then `swarm-handoff record start "
            f"flow-{fid}-{width}`, reach the goal, then `swarm-handoff record stop`"
        )
    return (
        f"Goal-driven flow test of {target['name']}. Open {base}{flow['start_path']} in the live "
        f"browser.{_sign_in_note(own_login, start_clicks(target))} Using the jev browser tools, reach this goal as a person "
        f"would, without typing addresses: {flow['goal']}. Do it once per width, recorded: "
        f"{'; '.join(runs)}. For each run write in flow-{fid}.json whether the goal was reached, the steps "
        f"taken, and every point where a label was missing, ambiguous or misleading, each with a "
        f"screenshot taken with browser_take_screenshot and no filename (it is saved under "
        f"$SWARM_ARTIFACTS_DIR/screenshots). List in flow-{fid}.json every screenshot you take, by its file "
        f"name, with what it shows. Bundle flow-{fid}.json, the flows/flow-{fid}-*.gif recordings and the "
        f"screenshots into flow-{fid}.tar under $SWARM_ARTIFACTS_DIR. "
        f"{_safety(target, _origin(base), mutating=flow['mutating'])} {NO_CHANGE}"
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
        f"tar is missing, the group's expected count is its routes x {len(widths)} widths x {len(themes)} "
        f"themes x each route's segments (4 when unset), and say which count you used. Merge every finding "
        f"into findings.json, {{\"format\": 1, \"findings\": [...], \"about_run\": [...]}}, de-duplicating the "
        f"same defect seen at several widths or themes into one finding whose `also_seen` lists the others. "
        f"A finding is kept only when every file its `evidence` names is in a staged tar; one that names "
        f"none, or one no tar holds, moves to `about_run` with what was missing. `about_run` entries are "
        f"{{id, title, actual}} and are never filed in the application's repository. Copy every evidence "
        f"file to $SWARM_ARTIFACTS_DIR next to findings.json, keeping its path, so the console shows it. "
        f"Write report.md: one line first saying the flows are reported apart, in flow-report's flows.md; "
        f"then a table of findings by severity and by screen, then each finding, severity first, with its "
        f"crop embedded by relative link. {NO_CHANGE}"
    )


def _flow_report_prompt(flows: Sequence[Mapping[str, Any]]) -> str:
    tars = ", ".join(f"flow-{f['id']}.tar" for f in flows)
    return (
        f"Write the visual QA report of the flows. Unpack every staged tar: {tars}. A tar that is missing "
        f"is itself a finding, naming the flow. Write flows.md: one section per flow, and in it, for each "
        f"width, whether the goal was reached, the steps, and every finding with its screenshot embedded by "
        f"relative link; a screenshot no json describes is listed as one nobody described. Copy every "
        f"screenshot and recording to $SWARM_ARTIFACTS_DIR next to flows.md. {NO_CHANGE}"
    )


def _file_prompt(target: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    tid = target["id"]
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
        f"dropped. Never file `about_run`. For each finding, first call `issue_list` for an open issue on "
        f"the same path naming the same defect; if one exists, comment on it with `issue_comment` instead "
        f"and say so in your answer. Otherwise call `issue_create` with `dedupe_key` "
        f"\"visual-qa:{tid}:<screen>:<the title, lower-cased, spaces collapsed>\"; an answer "
        f"{{existing: N}} means it is filed already: comment on N with this run and the new evidence. Use "
        f"the repository's bug form when it has one, else `form: \"plain\"` with the sections Summary, "
        f"Where (path, width, theme), Actual, Expected, Steps to reproduce and Evidence, ending \"Found by "
        f"SwarmCloud visual QA\". Labels: {labels} and severity:<s>; the broker applies only labels that "
        f"exist. Before any write, remove from the text every e-mail address, phone number and anything "
        f"credential-shaped the page showed: quote no more than the words that name the defect. {evidence} "
        f"Write filed.json listing each finding id with the issue number filed or commented on. {NO_CHANGE}"
    )


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
           file: str = "issues") -> dict[str, Any]:
    """The QA run's workflow spec for `target`, as `read_spec` reads it.

    `screens` narrows the run to those screen ids; `file="none"` renders the
    report without the filing step (§6). Raises `QaSpecError` for anything
    the spec cannot hold; never returns a partial spec."""
    if file not in FILE_MODES:
        raise _invalid(f"file must be one of {', '.join(FILE_MODES)}")
    name = _text(target.get("name"), "name", limit=100)
    target = {**target, "name": name}
    base = base_url(target)
    own_login = _own_login(target)
    widths = _names(target.get("widths"), PRESETS, DEFAULT_WIDTHS, "widths")
    themes = _names(target.get("themes"), THEMES, DEFAULT_THEMES, "themes")
    groups = group_screens(_screens(target, screens))
    flows = _flows(target)
    design_docs = _design_docs(target)
    settings = _file_settings(target)
    clock = _clock(target)
    _repository(target)
    if file == "issues":
        _target_id(target)

    steps: list[dict[str, Any]] = []
    for group, members in groups:
        steps.append(_step(
            f"capture-{group}", CAPTURE_PROFILE, CAPTURE_MODEL,
            _capture_prompt(target, base, group, route_list([(group, members)], clock=clock),
                            widths=widths, themes=themes, own_login=own_login),
            timeout=1800, browser_tool="playwright"))
    for group, _members in groups:
        steps.append(_step(
            f"review-{group}", REVIEW_PROFILE, REVIEW_MODEL, _review_prompt(target, base, group, design_docs),
            timeout=3600, depends_on=[f"capture-{group}"],
            input_from={f"capture-{group}": f"capture-{group}.tar"}))
    for flow in flows:
        steps.append(_step(
            f"flow-{flow['id']}", CAPTURE_PROFILE, FLOW_MODEL,
            _flow_prompt(target, base, flow, widths, own_login), timeout=2700, browser_tool="jev"))
    staged = {f"{kind}-{g}": f"{kind}-{g}.tar" for kind in ("capture", "review") for g, _ in groups}
    steps.append(_step("report", REVIEW_PROFILE, REPORT_MODEL, _report_prompt(target, groups, widths, themes),
                       timeout=3600, depends_on=list(staged), input_from=staged))
    if flows:
        flow_tars = {f"flow-{f['id']}": f"flow-{f['id']}.tar" for f in flows}
        steps.append(_step("flow-report", REVIEW_PROFILE, REPORT_MODEL, _flow_report_prompt(flows),
                           timeout=1800, depends_on=list(flow_tars), input_from=flow_tars))
    if file == "issues":
        steps.append(_step("file", REVIEW_PROFILE, FILE_MODEL, _file_prompt(target, settings),
                           timeout=2700, depends_on=["report"], input_from={"report": "findings.json"}))
    return _workflow(target, f"Visual QA: {name}", steps)


def render_discovery(target: Mapping[str, Any]) -> dict[str, Any]:
    """The one-step discovery run (§4.1 step 1): same-origin links from the
    rendered pages, bounded, exclusions and `robots.txt` honoured, written as
    `screens.json` in the route-list format. swarm-api stores the result on
    the target as `discovered`; the QA run starts from it."""
    name = _text(target.get("name"), "name", limit=100)
    target = {**target, "name": name}
    base = base_url(target)
    own_login = _own_login(target)
    raw = target.get("discover")
    if not isinstance(raw, Mapping):
        raise _invalid("discover must be {max_pages, max_depth}")
    pages, depth = raw.get("max_pages"), raw.get("max_depth")
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= MAX_DISCOVER_PAGES:
        raise _invalid(f"discover.max_pages must be 1..{MAX_DISCOVER_PAGES}")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= MAX_DISCOVER_DEPTH:
        raise _invalid(f"discover.max_depth must be 0..{MAX_DISCOVER_DEPTH}")
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


def _target_id(target: Mapping[str, Any]) -> str:
    tid = target.get("id")
    if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
        raise _invalid("the target has no id")
    return tid


def qa_target_metadata(target: Mapping[str, Any]) -> dict[str, Any]:
    """`metadata.qa_target` for each agent of `browser_steps` (§1.1): written by
    swarm-api at submission, never accepted from a caller, and what the
    worker's attempt route resolves the target from."""
    revision = target.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise _invalid("the target has no revision")
    return {"id": _target_id(target), "revision": revision}


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
