"""The QA target, held by the gate: its sign-in, its scrub and its safety rules (VQ-4).

docs/design/visual-qa-any-app.md §2.2 to §2.4 and §3 (lane VQ4-GATE-SIGNIN,
part of #1933). The worker asks swarm-api for its attempt's QA target
(`POST /v1/attempts/{id}/qa-target`, `agent_worker.qa_target`) and hands it
to the gate over ctl.sock (`qa_sign_in`, `OP`): the target's public settings
and, for an app with its own login, the test account's username and
password. The worker drops its copy once the gate answers. From then on:

THE GATE SIGNS IN, WITH THE AGENT HELD (§2.2 steps 3-4; `SignIn`, driven by
`CdpProxy.qa_sign_in`). The agent's commands are held and its results
withheld exactly as for a person's hand-off. On a tab of the gate's own, in
the default browser context (so the session's cookies are the agent's
browser's), on a CDP session the agent never sees, the gate opens
`login_url`, makes the target's start clicks in order (a chooser, or a
"Sign in" button before an identity provider: each the one visible control
a person can click whose trimmed text equals the click's `text`, plain text
reading the same not counted, waited for up to
`FORM_SECONDS` for the first and `START_CLICK_SECONDS` for each later one,
clicked once the page finished loading, or the sign-in ends
`qa_sign_in_start_click_missing`; a page that is already the form, as an
entry query can make it, skips them; clicks the page did not act on are made
again, `MAX_RECLICKS` times at most), focuses each field by its selector,
types with `Input.insertText`, presses the submit control and reads THE
PAGE'S OWN STATE (part of #2688): `signed_in_selector`, or the target's own
origin settled with no login form, no OAuth callback and not back on the
start page, is signed in; the form, an error or the start page after
`SIGNED_IN_SECONDS` is failed; a page still loading is waited for up to
`SIGNED_IN_MAX_SECONDS`. BEFORE THE FORM, a page that settled on the
target's own origin with no password or username field and no control the
last start click names (`landed`: a role's own home, reached through a
session the identity provider still held) is signed in only on the page's
own evidence, never a guess (part of #2688): `signed_in_selector`, or, after
`PAGE_SETTLED_SECONDS` of it, an HttpOnly cookie for an approved origin that
the browser did not hold, or held with another value, before `login_url`
opened (the bypass cookie never counts; values are compared as digests and
never recorded). A sign-in that ends without the form, or failed,
says why in `sentence`: which start click matched nothing, that the form
never appeared after them and where the tab is, that the tab landed on a
page that is not a login page but drew neither `signed_in_selector` nor a
session cookie, or what the page showed after the submit. Then, in the return order:
every password input on the gate's sessions is cleared, the gate's tab is
closed, the agent's tab is sent to `base_url`, the held responses are
delivered and the held commands released. The agent never had a tab on
which the password was typed.

MFA AND CAPTCHA ARE REFUSED, NEVER SOLVED (§2.4). A page that asks for a
one-time code (`autocomplete=one-time-code`), calls WebAuthn
(`navigator.credentials`, trapped on the gate's tab only), or shows a
challenge frame from the hosts mask.py knows (`CHALLENGE_FRAMES`) ends
the sign-in at once with `qa_sign_in_challenge` and `CHALLENGE_SENTENCE`.
It is never retried past and never handed to a person: a person typing
their own code would leave the agent signed in as that person.

THE PASSWORD IS SCRUBBED FROM EVERYTHING BOUND FOR THE AGENT (§2.2 step 5;
`QaTarget.scrub`, called on every agent link by the proxy). Every string in
an agent-bound CDP message has every occurrence of the password's bytes, its
URL encodings (`quote`, `quote_plus`, both hex cases) and its base64
encodings (standard and URL-safe, padded or not, and the core of each of
the three byte alignments a longer base64 text could carry it at) replaced
with `PLACEHOLDER`; a `base64Encoded` body and a `postDataEntries` chunk are
decoded, scrubbed and re-encoded. A base64 body the gate cannot decode is
refused while a password is held (`undecodable_body`). THE SCRUB FAILS CLOSED
(#2116): a dict or list nested past `MAX_SCRUB_DEPTH` becomes `SCRUB_TOO_DEEP`,
a value that is not JSON `SCRUB_NOT_JSON`, a number is read as its JSON text,
and a scrub that raises refuses the whole message (`SCRUB_REFUSED`); what
the scrub did not read is never passed on. The scrub's needles
outlive the wipe, as the QA identity's do: a frame in flight when Chromium
closes is still scrubbed. KNOWN LIMIT: a password under
`MIN_ALIGNED_BYTES` bytes is not matched at the shifted base64 alignments
(their cores would be one or two characters, which would blank unrelated
text); its own, URL and aligned base64 forms still are.

READ-ONLY BY DEFAULT, NEVER LEAVING THE ORIGIN, AT A RATE (§3;
`QaTargetGuard`, on sessions the gate owns: `Fetch.enable` at the
`Request` stage on every page, frame and worker). A paused request is judged
by `QaTarget.judge`:

* `POST`, `PUT`, `PATCH` or `DELETE` to an approved origin is answered by
  the gate (403, `qa_write_refused`), except the gate's own sign-in
  submission, a `POST` to a `read_posts` path, and anything in a flow the
  worker marked `mutating` on a `non_production` target;
* anything but `GET` and `HEAD` to any other origin is refused the same way,
  ALWAYS: nothing lifts it, a mutating flow included;
* ONE EXCEPTION, the IdP token refresh (QA-IDP-POST; BGD decided
  2026-10-23, part of #2067): a target whose platform admin approved an
  `idp_token_path` with its `extra_origin` has exactly `POST
  <extra_origin><idp_token_path>` continued, compared as the whole URL
  (`idp_token_url`: no query, no fragment, no other port, case or encoding),
  from the moment the gate's own sign-in begins (`sign_in_begun`) until the
  wipe, WHATEVER THAT SIGN-IN'S VERDICT: never before it begins, never after
  the wipe, never on a target that does not sign in. The verdict is the
  gate's reading of its own tab (until 4 November 2026 of
  `signed_in_selector` alone), not the provider's:
  on 2026-11-03 (capture steps from 17:43) five production legs' sign-ins
  ended `failed` while the provider's session was real, and every screen's
  exchange, 69 to 79 a group, was refused as a write, so the approval was
  undone by a selector. Refusing it after a `failed` or `challenge` verdict
  protects nothing: the endpoint mints a token only for a code or session
  the browser already holds (part of #2688). An app that
  refreshes its SSO token after sign-in (Keycloak's
  `/protocol/openid-connect/token`) would otherwise render signed out, and
  one that exchanges it as its page loads (a production chooser checking its
  saved session) would read the refusal as an unreachable provider: on
  2026-11-03 five production legs failed sign-in so, their exchange sent in
  the hand-back, after `signing_in` was cleared and before `signed_in` was
  set. Every other method or path to that origin outside the gate's own
  steps is refused as above, a top frame posted there still leaves the
  target, and a redirect of the POST is a pause of its own, judged by its
  own URL;
* a top-frame navigation outside the approved origins, or to an excluded
  path, gets the gate's page instead (`left_page`, `qa_target_left`). The
  login's other origin (`extra_origin`, approved with the host) is a
  top-frame destination only while the gate itself is signing in;
* top-frame navigations are spaced by the target's `navigation_ms` and
  requests to its origins by `requests_per_s`, HELD rather than failed.

The agent cannot route around the guard with its own interception: while a
target is held, the proxy refuses the agent's `Fetch.continueRequest` that
rewrites a request's `url`, `method` or `postData` (`agent_refusal`).

A CHALLENGE BYPASS ITS OWNER CONFIGURED, APPLIED BY THE GATE ALONE, ON THE
TARGET'S OWN ORIGINS ALONE (§2.5; lane CAPTCHA-BYPASS, BGD 2026-10-23, part
of #2058). The hand-off may carry `challenge_bypass: {kind, name, value}`:
a header or a cookie the app's owner set up to skip their own CAPTCHA or
WAF challenge for this platform's runs. The gate never solves a challenge
and never hides from one; it presents the owner's own pass.

* `header`: every request a gate session pauses whose URL's origin is one of
  `origins` (the base origin and the approved `extra_origin`, exactly, port
  included) is continued with the header added, a same-named header the
  request already had replaced (`QaTargetGuard._paused`). No other request
  gets it. Never `Network.setExtraHTTPHeaders`, which would send it to every
  origin a page touches. An added header follows no redirect (M5 of
  docs/design/qa-identity-measurements.md): a redirect's own request is a
  pause of its own, judged by its own URL.
* `cookie`: planted by the gate on its own pipe (`Storage.setCookies`, at
  `QaTargetGuard.start`), once per approved origin, host-only (`url`, no
  `domain`, so no subdomain is sent it), `Secure`, `HttpOnly` (no page
  script, the agent's `Runtime.evaluate` included, reads it) and
  `SameSite=None`, so the browser sends it to that host and no other. The
  agent's CDP can neither read nor write cookies (cdppolicy.py), and the
  session carry already leaves these origins' cookies out (`excluded_origins`).

The name is checked again here against swarm-api's reserved list
(`RESERVED_BYPASS_HEADERS`, `RESERVED_BYPASS_HEADER_PREFIXES`, restated:
this image does not carry swarm-api; the bypass tests hold them equal). THE
VALUE IS A SCRUB NEEDLE like the password: every agent-bound message has
it replaced (a page that echoes its request headers, Chromium's
`requestWillBeSentExtraInfo`), records carry its kind and never its name's
value, and the wipe drops it while its needles stay.

THE SESSION STAYS IN CHROMIUM (§2.3). `excluded_origins` is what the session
carry (session.py) leaves out of every export and import: the cookies those
hosts would be sent and their origins' storage.

`wipe` drops the credential and refuses the verb from then on (§2.3).

ONE LEG AN ATTEMPT (docs/design/qa-environments.md §1.5; QAENV-GATE, part of
#2136). A target carries several environments (legs: `staging`,
`production`), each with its own origins, credential and write rule, and an
attempt is one leg's. The hand-off names the leg in `environment_name` (the
entry's `name`); the first hand-off fixes `(target_id, environment_name)` and
a later one naming anything else is refused `target_changed` before any of
its fields is taken. A hand-off without the name is an old mark, from before
the target was migrated to `environments[]`, and is accepted; an absent name
is itself the value compared, so a named attempt cannot switch legs by
dropping the name, nor an unnamed one by adding one.

THE GATE'S REFUSALS ARE KNOWN TO THE RUN (QA-GATE-REFUSALS, part of #2067).
Every request the guard answers itself, a refused write (`qa_write_refused`)
or a top frame sent to the gate's page (`qa_target_left`), is kept in
`refusals()`: its wall-clock time, method, origin, path (never the query,
never the body, and scrubbed like anything bound for the agent) and the
gate's code, numbered, the last `MAX_REFUSALS` of them. The visual-QA capture
reads it at `REFUSALS_PATH` on the gate's CDP endpoint and writes each
screen's share into that screen's `errors.json` as `gate_refused`: a 403 the
gate sends to another origin carries no CORS header, so Chrome logs it as a
CORS block, and on 2026-10-23 a run filed exactly that against the app. This
records refusals; it changes none of the rules above. `undecodable_body` is
not among them: it withholds a CDP response from the agent, and the page's
own request went through, so the app saw nothing the gate answered. An IdP
token refresh the gate let through is kept apart, in `allowed`
(`note_allowed`, code `IDP_TOKEN_ALLOWED`, labelled "allowed: IdP token
refresh"), bounded the same way: a run shows what was let through, and never
counts it as refused.

NOTHING HERE LOGS OR RECORDS A CREDENTIAL, A USERNAME OR A SELECTOR'S MATCH.
Records carry the target id, causes, origins, methods and counts.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fnmatch
import hashlib
import html
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, quote_plus, urlsplit

from .. import buildinfo

log = logging.getLogger(__name__)

#: The ctl.sock operation (ctl.py `OPS`; `agent_worker.qa_target.CTL_OP`).
OP = "qa_sign_in"
#: What the password reads as in anything the agent receives.
PLACEHOLDER = "[qa-credential]"
#: How deep the scrub walks (#2116). A dict or list nested deeper, or a value
#: that is not JSON, becomes `SCRUB_TOO_DEEP` or `SCRUB_NOT_JSON`: the scrub
#: fails closed and never passes on what it did not read. 64 is far past any
#: CDP message's own nesting (a capture's rows sit about 10 levels deep).
MAX_SCRUB_DEPTH = 64
SCRUB_TOO_DEEP = "[qa-scrub: nesting too deep]"
SCRUB_NOT_JSON = "[qa-scrub: not JSON]"
#: A message whose scrub raised: the agent gets this in its place, never the original.
SCRUB_REFUSED = ("refused by the live gate: this message could not be scrubbed of the QA target's "
                 "credential, so it was withheld")

#: §2.3: at most three sign-ins an attempt, then `qa_sign_in_exhausted`.
MAX_SIGN_INS = 3
#: §2.2 step 3: how long the gate waits for the page's signed-in state after
#: submit (`SIGNED_IN_MAX_SECONDS` while it still loads), and (separately) for
#: the login form to appear.
SIGNED_IN_SECONDS = 30.0
FORM_SECONDS = 30.0
#: §2.2 step 3, a start click: how long the gate waits for the one control
#: a start click names, and then for the page it led to to finish loading. A
#: chooser is drawn by the page's own first load, which `FORM_SECONDS` already
#: bounds for a login form; ten seconds past the previous step is long enough
#: for a script-drawn button and short enough that a control that is not there
#: does not hold the agent (held throughout) for a minute per click.
START_CLICK_SECONDS = 10.0
#: A start click whose page did not act on it: the clicked document still
#: there, at its address, this long after the click (a server-drawn chooser
#: whose script was not running yet). The clicks are then made again, in
#: order, at most MAX_RECLICKS times within FORM_SECONDS.
RECLICK_SECONDS = 5.0
MAX_RECLICKS = 2
#: After the submit (part of #2688): a page still loading when
#: SIGNED_IN_SECONDS pass is waited for up to this long in all. The gate's own
#: rate limit spaces the redirect chain's navigations (2 s each by default)
#: and the app's requests (10 a second), so a signed-in app that loads 150
#: requests draws itself after 30 s; a page that settled is judged at 30 s.
SIGNED_IN_MAX_SECONDS = 60.0
#: How long the page must read as the signed-in app (or as an error on the
#: form) without change before the gate takes it as the verdict: a page
#: between two redirects reads as either for a moment.
PAGE_SETTLED_SECONDS = 3.0
#: At most this many characters of the tab's origin and path in a sentence.
MAX_WHERE = 160
#: At most three start clicks (swarm-api's `qaspec.MAX_START_CLICKS`).
MAX_START_CLICKS = 3
MAX_START_CLICK_TEXT = 60
MAX_START_CLICK_CSS = 200
#: How often the gate's tab is read while it waits.
PROBE_SECONDS = 0.25
#: The rate floor (§3): never faster than one navigation a second, nor more
#: than swarm-api's own ceiling of requests a second (`qatargets.MAX_REQUESTS_PER_S`).
MIN_NAVIGATION_MS = 1000
MAX_NAVIGATION_MS = 60_000
MAX_REQUESTS_PER_S = 10
#: A password shorter than this is not matched at shifted base64 alignments
#: (module docstring, KNOWN LIMIT).
MIN_ALIGNED_BYTES = 6

STATE_NONE = "none"
STATE_READY = "ready"            # kind none: the rules apply, nothing to sign in
STATE_SIGNING_IN = "signing_in"
STATE_SIGNED_IN = "signed_in"
STATE_CHALLENGE = "challenge"
STATE_FAILED = "failed"
STATE_WIPED = "wiped"
#: The states a sign-in ends in.
FINAL = frozenset({STATE_SIGNED_IN, STATE_CHALLENGE, STATE_FAILED})

#: Causes, each a short word the worker and the agent are told.
CHALLENGE = "qa_sign_in_challenge"
EXHAUSTED = "qa_sign_in_exhausted"
SIGN_IN_FAILED = "qa_sign_in_failed"
NO_FORM = "qa_sign_in_no_form"
BUSY = "qa_sign_in_busy"
BROWSER_GONE = "qa_sign_in_browser_gone"
START_CLICK_MISSING = "qa_sign_in_start_click_missing"
#: §2.4's sentence, verbatim.
CHALLENGE_SENTENCE = ("the target's login asks for MFA or a CAPTCHA; register a test account "
                      "without one")

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READ_METHODS = frozenset({"GET", "HEAD"})
_TARGET_ID = re.compile(r"qat_[0-9a-f]{16}")
_ORIGIN = re.compile(r"https://[a-z0-9.-]{1,253}(?::[0-9]{1,5})?")
_PATH = re.compile(r"/[^\s#]{0,511}")
_CAUSE = re.compile(r"[a-z0-9_]{1,64}")
#: An `environments[]` entry's `name` (qa-environments.md §1.1).
_ENVIRONMENT_NAME = re.compile(r"[a-z0-9-]{1,20}")
_SELECTOR_KEYS = ("username_selector", "password_selector", "submit_selector")
MAX_SELECTOR = 512
MAX_USERNAME = 256
MAX_PASSWORD = 1024
MAX_ORIGINS = 4
MAX_PATHS = 64
#: The keys whose value is base64 when the message says so.
_BODY_FIELDS = ("body", "content", "data")

#: §2.5: swarm-api's `qaspec.CHALLENGE_BYPASS_KINDS`, `RESERVED_BYPASS_HEADERS`
#: and `RESERVED_BYPASS_HEADER_PREFIXES`, and `qatargets._BYPASS_VALUE`, restated.
BYPASS_KINDS = ("header", "cookie")
RESERVED_BYPASS_HEADERS = frozenset({
    "authorization", "cookie", "host", "set-cookie", "content-length", "transfer-encoding",
    "connection", "upgrade", "te", "trailer", "keep-alive", "expect", "origin", "referer",
})
RESERVED_BYPASS_HEADER_PREFIXES = ("proxy-", "sec-")
_BYPASS_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_|~-]{1,64}")
_BYPASS_VALUE = re.compile(r"[!#-+\--:<-\[\]-~]{16,1024}")

#: QA-GATE-REFUSALS: the refusal record (module docstring). Where the capture
#: reads it on the gate's CDP endpoint, its format, and how many it keeps: a
#: capture of 57 routes at four widths and themes that refused one token
#: exchange per load is 228; past this the oldest go, counted in `dropped`.
REFUSALS_PATH = "/json/swarm/qa-refusals"
REFUSALS_FORMAT = 1
MAX_REFUSALS = 1000
#: The gate's codes for a request it answered itself.
WRITE_REFUSED = "qa_write_refused"
TARGET_LEFT = "qa_target_left"
#: QA-IDP-POST: the code and the report's words for a token refresh let through.
IDP_TOKEN_ALLOWED = "idp_token_refresh"
IDP_TOKEN_LABEL = "allowed: IdP token refresh"
#: swarm-api's `qatargets._IDP_TOKEN_PATH`, restated: one plain path, segments
#: of unreserved and sub-delim characters less the glob characters, no `%`
#: (so no encoded variant), no empty segment (no `//`, no trailing slash).
#: `.` and `..` segments are refused apart.
_IDP_TOKEN_PATH = re.compile(r"(?:/[A-Za-z0-9._~!$&'()+,;=:@-]+)+")
MAX_IDP_TOKEN_PATH = 512

Record = Callable[[str, Mapping[str, Any]], None]
Sleep = Callable[[float], Awaitable[None]]


class QaTargetRefused(ValueError):
    """The verb is refused: malformed, another target, busy, exhausted or after the wipe."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def origin_of(url: Any) -> str | None:
    """`scheme://host[:port]`, lower case, or None for a URL with no network origin."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    netloc = parts.hostname
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}"


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"


def _has_query(url: str) -> bool:
    """A `?` after the path, an empty query included."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.query) or url.split("#", 1)[0].endswith("?")


def left_page(origin: str) -> str:
    """§3: what a top frame shows instead of a page off the QA target."""
    shown = html.escape(origin)
    return (
        "<!doctype html><meta charset=utf-8><title>Left the QA target</title>"
        "<h1>Left the QA target</h1>"
        f"<p>This browser was sent to <code>{shown}</code>, which is not an approved origin of the "
        "QA target or is excluded from it. The live gate keeps a QA run on the application it "
        "was approved for.</p>"
    )


def _aligned_cores(raw: bytes, *, urlsafe: bool) -> list[str]:
    """The base64 text `raw` encodes to at each of the three byte alignments,
    cut to the characters `raw` alone decides (module docstring)."""
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    out: list[str] = []
    for shift in (1, 2):
        text = encode(b"\0" * shift + raw).decode("ascii").rstrip("=")
        start = -(-8 * shift // 6)            # the first character with no pad bit
        end = (8 * (shift + len(raw))) // 6   # the last with no unknown following bit
        core = text[start:end]
        if len(core) >= 4:
            out.append(core)
    return out


def needles(password: str) -> list[str]:
    """Every text form the scrub replaces, longest first (module docstring)."""
    raw = password.encode("utf-8")
    forms: set[str] = {password}
    for encoded in (quote(password, safe=""), quote_plus(password, safe="")):
        forms.add(encoded)
        forms.add(re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), encoded))
    for urlsafe in (False, True):
        encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        full = encode(raw).decode("ascii")
        forms.update({full, full.rstrip("=")})
        if len(raw) >= MIN_ALIGNED_BYTES:
            forms.update(_aligned_cores(raw, urlsafe=urlsafe))
    forms.discard("")
    return sorted(forms, key=len, reverse=True)


@dataclass(frozen=True)
class Verdict:
    """What the guard does with one paused request."""

    action: str                # "continue", "refuse" (a write) or "leave" (a navigation)
    origin: str | None = None
    method: str | None = None
    #: A write continued under an exception the run is told of (`IDP_TOKEN_ALLOWED`).
    allowed: str | None = None


CONTINUE = Verdict("continue")


class QaTarget:
    """The attempt's QA target: its settings, its credential while held, the
    sign-in's state, the scrub and the request rules (module docstring).

    Lives as long as the gate: a relaunched Chromium gets a new proxy and
    guard over the same target. `clock` is monotonic seconds.
    """

    def __init__(self, record: Record, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self._record = record
        self._clock = clock
        #: Epoch seconds for the refusal record: the capture, in the agent's
        #: container on the same node, compares it with its own `time.time()`.
        self._wall = wall
        self._refusals: deque[dict[str, Any]] = deque(maxlen=MAX_REFUSALS)
        self._refused = 0
        #: QA-IDP-POST: the token refreshes let through, kept apart from the refusals.
        self._allowed: deque[dict[str, Any]] = deque(maxlen=MAX_REFUSALS)
        self._allowed_count = 0
        #: Messages the scrub refused whole (`_refuse_whole`), for the record's count.
        self._scrub_refusals = 0
        self.target_id: str | None = None
        #: The leg's `environments[]` name, None for an old mark (module docstring).
        self.environment_name: str | None = None
        self.base_url: str | None = None
        self.base_origin: str | None = None
        self.origins: tuple[str, ...] = ()
        self.environment = "production"
        self.navigation_seconds = 2.0
        self.request_spacing = 0.1
        self.read_posts: tuple[str, ...] = ()
        self.excluded_paths: tuple[str, ...] = ()
        #: QA-IDP-POST: the approved token path, and the one URL it allows.
        self.idp_token_path: str | None = None
        self.idp_token_url: str | None = None
        self.sign_in: dict[str, Any] = {}
        #: Why the last sign-in ended, in a sentence, when its cause alone does
        #: not say (a start click's text). Public settings only, never a credential.
        self.sentence: str | None = None
        self.mutating = False
        self._username: str | None = None
        self._password: str | None = None
        #: The challenge bypass (§2.5): its kind and name, and its value while held.
        self.bypass_kind: str | None = None
        self.bypass_name: str | None = None
        self._bypass_value: str | None = None
        self._needles: list[str] = []
        self._byte_needles: list[bytes] = []
        self.state = STATE_NONE
        self.cause: str | None = None
        self.sign_ins = 0
        self._signing_in = False
        #: QA-IDP-POST: the gate's own sign-in has begun (`signing_in` was set)
        #: and has not ended (`finished`, `wipe`). Longer than `signing_in`,
        #: which the proxy clears before it hands the agent back.
        self.in_flight = False
        #: QA-IDP-POST: the gate's own sign-in has begun at least once. Never
        #: cleared: the token exception holds from here until the wipe.
        self.sign_in_begun = False
        self.wiped = False
        self._next_navigation = 0.0
        self._next_request = 0.0

    # ------------------------------------------------------------- state

    @property
    def armed(self) -> bool:
        """A target was handed over: its rules apply for the rest of the gate."""
        return self.target_id is not None

    @property
    def signing_in(self) -> bool:
        """The gate's own tab is signing in: every write to an approved origin
        continues, and the login's other origin is a top-frame destination."""
        return self._signing_in

    @signing_in.setter
    def signing_in(self, value: bool) -> None:
        # Set by the proxy as the sign-in begins; it ends only at `finished`
        # or `wipe`, so clearing it here does not end `in_flight`.
        self._signing_in = value
        if value:
            self.in_flight = True
            self.sign_in_begun = True

    @property
    def held(self) -> bool:
        """A credential is in the gate (until the wipe)."""
        return self._password is not None

    @property
    def wants_sign_in(self) -> bool:
        return self.state == STATE_SIGNING_IN and self.held and not self.signing_in

    def credential(self) -> tuple[str, str]:
        """For `SignIn` alone, on the gate's own session."""
        if self._username is None or self._password is None:
            raise QaTargetRefused("no_credential")
        return self._username, self._password

    def bypass_header(self, url: Any) -> tuple[str, str] | None:
        """(name, value) to add to a request for `url`: a header bypass held,
        and `url`'s origin exactly one of the approved origins. Else None."""
        if self.bypass_kind != "header" or self._bypass_value is None or self.bypass_name is None:
            return None
        if origin_of(url) not in self.origins:
            return None
        return self.bypass_name, self._bypass_value

    def bypass_cookies(self) -> list[dict[str, Any]]:
        """`Storage.setCookies`' cookies for a cookie bypass held: one per
        approved origin, host-only, Secure and HttpOnly (module docstring)."""
        if self.bypass_kind != "cookie" or self._bypass_value is None or self.bypass_name is None:
            return []
        return [{"name": self.bypass_name, "value": self._bypass_value, "url": f"{origin}/", "path": "/",
                 "secure": True, "httpOnly": True, "sameSite": "None"}
                for origin in self.origins if origin.startswith("https://")]

    def _add_needles(self, secret: str) -> None:
        for form in needles(secret):
            if form not in self._needles:
                self._needles.append(form)
        self._needles.sort(key=len, reverse=True)
        self._byte_needles = [n.encode("utf-8") for n in self._needles]

    def excluded_origins(self) -> tuple[str, ...]:
        """§2.3: the origins whose cookies and storage no session export carries."""
        return self.origins

    def status(self) -> dict[str, Any]:
        """The verb's answer and the gate's status: never a credential."""
        out: dict[str, Any] = {"qa_sign_in": STATE_WIPED if self.wiped else self.state,
                               "sign_ins": self.sign_ins}
        if self.target_id is not None:
            out["target_id"] = self.target_id
        if self.cause:
            out["cause"] = self.cause
        if self.sentence:
            out["sentence"] = self.sentence
        return out

    # --------------------------------------------------------------- ctl

    def handle(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """`{poll: true}` reads the state; the worker's hand-off sets the target
        and, for an own login, queues one sign-in (`wants_sign_in`)."""
        if self.wiped:
            self._record("qa_sign_in_refused", {"cause": "wiped"})
            raise QaTargetRefused("wiped")
        if args.get("poll") is True and set(args) == {"poll"}:
            return self.status()
        try:
            parsed = _parse(args)
        except QaTargetRefused as exc:
            self._record("qa_sign_in_refused", {"cause": exc.code})
            raise
        if self.target_id is not None and (parsed["target_id"], parsed["environment_name"]) \
                != (self.target_id, self.environment_name):
            self._refuse("target_changed")
        if self.signing_in or self.state == STATE_SIGNING_IN:
            self._refuse(BUSY)
        own_login = parsed["sign_in"]["kind"] == "own_login"
        if own_login and self.sign_ins >= MAX_SIGN_INS:
            self.state, self.cause = STATE_FAILED, EXHAUSTED
            self._record("qa_sign_in_refused", {"cause": EXHAUSTED, "target_id": parsed["target_id"]})
            raise QaTargetRefused(EXHAUSTED)
        self.target_id = parsed["target_id"]
        self.environment_name = parsed["environment_name"]
        self.base_url = parsed["base_url"]
        self.base_origin = origin_of(parsed["base_url"])
        self.origins = tuple(parsed["origins"])
        self.environment = parsed["environment"]
        self.navigation_seconds = parsed["navigation_ms"] / 1000.0
        self.request_spacing = 1.0 / parsed["requests_per_s"]
        self.read_posts = tuple(parsed["read_posts"])
        self.excluded_paths = tuple(parsed["excluded_paths"])
        self.idp_token_path = parsed["idp_token_path"]
        self.idp_token_url = parsed["idp_token_url"]
        self.sign_in = parsed["sign_in"]
        self.mutating = parsed["mutating"] and self.environment == "non_production"
        self.cause = None
        self.sentence = None
        bypass = parsed["challenge_bypass"]
        if bypass is not None:
            self.bypass_kind, self.bypass_name, self._bypass_value = bypass
            self._add_needles(self._bypass_value)
        else:
            self.bypass_kind = self.bypass_name = self._bypass_value = None
        if own_login:
            username, password = parsed["credential"]
            self._username, self._password = username, password
            self._add_needles(password)
            self.sign_ins += 1
            self.state = STATE_SIGNING_IN
        else:
            self.state = STATE_READY
        self._record("qa_target_set", {"target_id": self.target_id, "environment_name": self.environment_name,
                                       "sign_in": parsed["sign_in"]["kind"],
                                       "sign_ins": self.sign_ins, "mutating": self.mutating,
                                       "challenge_bypass": self.bypass_kind,
                                       "idp_token_path": self.idp_token_path is not None})
        return self.status()

    def _refuse(self, code: str) -> None:
        self._record("qa_sign_in_refused", {"cause": code})
        raise QaTargetRefused(code)

    def finished(self, state: str, cause: str | None = None) -> None:
        """The sign-in ended: `signed_in`, `challenge` or `failed` with a cause."""
        self.signing_in = False
        self.in_flight = False
        if self.wiped:
            return
        self.state = state if state in FINAL else STATE_FAILED
        self.cause = cause if cause is None or _CAUSE.fullmatch(cause) else SIGN_IN_FAILED
        fields: dict[str, Any] = {"target_id": self.target_id, "state": self.state}
        if self.cause:
            fields["cause"] = self.cause
        self._record("qa_sign_in_finished", fields)

    def wipe(self) -> None:
        """§2.3: the credential goes, and the verb is refused from here.

        The scrub's needles stay, for the reason `QaIdentity.wipe` gives: an
        agent-bound frame can still be in flight when Chromium closes."""
        self.wiped = True
        self._username = None
        self._password = None
        self._bypass_value = None
        self.signing_in = False
        self.in_flight = False
        self._record("qa_target_wiped", {"target_id": self.target_id})

    # ------------------------------------------------------------- scrub

    def scrub(self, message: dict[str, Any]) -> dict[str, Any]:
        """`message` as an agent may receive it. Unchanged when nothing matched.

        Fails closed (#2116): a container past `MAX_SCRUB_DEPTH` or a value
        that is not JSON is replaced by a marker, and a scrub that raises
        refuses the whole message (`_refuse_whole`). Neither record carries the
        value, its length or the exception's text."""
        if not self._needles:
            return message
        counter, marked = [0], [0]
        try:
            out = self._walk(message, counter, 0, marked)
            result = out.get("result") if isinstance(out, dict) else None
            if isinstance(result, dict) and result.get("base64Encoded") is True:
                out = {**out, "result": self._scrub_bodies(result, counter)}
        except Exception as exc:  # noqa: BLE001 - any failure refuses; none forwards the original
            return self._refuse_whole(message, exc)
        route = self._route(message)
        if marked[0]:
            self._record("qa_target_scrub_marked", {"route": route, "count": marked[0]})
        if counter[0]:
            fields: dict[str, Any] = {"count": counter[0]}
            if route != "response":
                fields["method"] = route
            self._record("qa_target_scrubbed", fields)
        return out if counter[0] or marked[0] else message

    def _route(self, message: Mapping[str, Any]) -> str:
        """The record's name for `message`: its CDP method, or "response". A
        method carrying a needle is named "event", never echoed."""
        method = message.get("method")
        if not isinstance(method, str):
            return "response"
        return "event" if any(n in method for n in self._needles) else method[:128]

    def _refuse_whole(self, message: Mapping[str, Any], exc: BaseException) -> dict[str, Any]:
        """What the agent gets for a message the scrub could not finish: an
        error answer to its command, or the event's method with no params."""
        self._scrub_refusals += 1
        try:
            route = self._route(message)
        except Exception:  # noqa: BLE001 - the record still goes, unnamed
            route = "unknown"
        self._record("qa_target_scrub_refused", {"route": route, "count": self._scrub_refusals,
                                                 "error": type(exc).__name__})
        msg_id = message.get("id") if isinstance(message, Mapping) else None
        if isinstance(msg_id, int) and not isinstance(msg_id, bool):
            return {"id": msg_id, "error": {"code": -32000, "message": SCRUB_REFUSED}}
        out: dict[str, Any] = {"params": {"qaScrub": SCRUB_REFUSED}}
        if route not in ("response", "event", "unknown"):
            out["method"] = route
        return out

    def _walk(self, value: Any, counter: list[int], depth: int, marked: list[int]) -> Any:
        if isinstance(value, str):
            return self._replace(value, counter)
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            # A number reads as its JSON text: an all-digit password returned
            # as a number would otherwise pass as one.
            text = json.dumps(value)
            cleaned = self._replace(text, counter)
            return value if cleaned == text else cleaned
        if not isinstance(value, (dict, list)):
            marked[0] += 1
            return SCRUB_NOT_JSON
        if depth > MAX_SCRUB_DEPTH:
            marked[0] += 1
            return SCRUB_TOO_DEEP
        if isinstance(value, dict):
            changed = False
            out: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str):
                    new_key: Any = self._replace(key, counter)
                else:
                    # JSON has string keys only; anything else is read as the text it serialises to.
                    new_key = self._walk(key, counter, depth + 1, marked)
                if key == "postDataEntries" and isinstance(item, list):
                    if depth + 1 > MAX_SCRUB_DEPTH:
                        marked[0] += 1
                        new_item: Any = SCRUB_TOO_DEEP
                    else:
                        new_item = [self._scrub_entry(e, counter, depth + 2, marked) for e in item]
                else:
                    new_item = self._walk(item, counter, depth + 1, marked)
                changed = changed or new_key is not key or new_item is not item
                out[new_key] = new_item
            return out if changed else value
        items = [self._walk(item, counter, depth + 1, marked) for item in value]
        return items if any(a is not b for a, b in zip(items, value)) else value

    def _replace(self, text: str, counter: list[int]) -> str:
        hits = 0
        for needle in self._needles:
            if needle in text:
                hits += text.count(needle)
                text = text.replace(needle, PLACEHOLDER)
        counter[0] += hits
        return text

    def _replace_bytes(self, raw: bytes, counter: list[int]) -> bytes:
        for needle in self._byte_needles:
            if needle in raw:
                counter[0] += raw.count(needle)
                raw = raw.replace(needle, PLACEHOLDER.encode())
        return raw

    def _scrub_entry(self, entry: Any, counter: list[int], depth: int, marked: list[int]) -> Any:
        """A `postDataEntries` item: `{bytes: <base64>}`, at `depth` in the message."""
        if not isinstance(entry, dict) or not isinstance(entry.get("bytes"), str):
            return self._walk(entry, counter, depth, marked)
        try:
            raw = base64.b64decode(entry["bytes"], validate=True)
        except (binascii.Error, ValueError):
            return self._walk(entry, counter, depth, marked)
        before = counter[0]
        cleaned = self._replace_bytes(raw, counter)
        if counter[0] == before:
            return self._walk(entry, counter, depth, marked)
        rest = self._walk({k: v for k, v in entry.items() if k != "bytes"}, counter, depth, marked)
        return {**rest, "bytes": base64.b64encode(cleaned).decode("ascii")}

    def _scrub_bodies(self, result: dict[str, Any], counter: list[int]) -> dict[str, Any]:
        out = result
        for field_name in _BODY_FIELDS:
            body = result.get(field_name)
            if not isinstance(body, str):
                continue
            try:
                raw = base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError):
                continue
            before = counter[0]
            cleaned = self._replace_bytes(raw, counter)
            if counter[0] != before:
                out = {**out, field_name: base64.b64encode(cleaned).decode("ascii")}
        return out

    def undecodable_body(self, result: Mapping[str, Any]) -> bool:
        """A `base64Encoded` body the scrub cannot read, while a password is in play."""
        if not self._needles or result.get("base64Encoded") is not True:
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

    def agent_refusal(self, method: str, params: Mapping[str, Any]) -> str | None:
        """Why the agent may not send `method` while a target is held, or None."""
        if not self.armed or method != "Fetch.continueRequest":
            return None
        if any(k in params for k in ("url", "method", "postData")):
            return ("refused by the live gate: while a QA target is held, a paused request is "
                    "continued as it was, never rewritten")
        return None

    # --------------------------------------------------------- refusals

    def note_refusal(self, code: str, method: Any, url: Any) -> None:
        """Keep one request the guard answered itself (module docstring): its
        origin and path alone, the path scrubbed of the held secrets, and
        whether it carried a query (`query`), never the query itself: a
        refused `<token URL>?...` is another URL, and reads apart from it."""
        origin = origin_of(url) or "unknown"
        path = _path_of(str(url)) if isinstance(url, str) else "/"
        self._refused += 1
        self._refusals.append({
            "n": self._refused, "at": float(self._wall()), "method": str(method or "GET").upper()[:16],
            "origin": origin, "path": self._replace(path, [0])[:512], "code": code,
            "query": isinstance(url, str) and _has_query(url),
        })

    def note_allowed(self, code: str, method: Any, url: Any) -> None:
        """Keep one write the guard let through under an exception (QA-IDP-POST),
        apart from the refusals, in the same shape."""
        origin = origin_of(url) or "unknown"
        path = _path_of(str(url)) if isinstance(url, str) else "/"
        self._allowed_count += 1
        self._allowed.append({
            "n": self._allowed_count, "at": float(self._wall()), "method": str(method or "GET").upper()[:16],
            "origin": origin, "path": self._replace(path, [0])[:512], "code": code, "label": IDP_TOKEN_LABEL,
        })

    def refusals(self) -> dict[str, Any]:
        """What `REFUSALS_PATH` answers: the kept refusals, oldest first, and
        apart from them the token refreshes let through (`allowed`)."""
        return {"format": REFUSALS_FORMAT, "armed": self.armed, "refusals": [dict(r) for r in self._refusals],
                # Which gate made them (agent_worker.buildinfo): the capture
                # manifest's `worker`.
                "build": buildinfo.describe(),
                "dropped": self._refused - len(self._refusals),
                "allowed": [dict(r) for r in self._allowed],
                "allowed_dropped": self._allowed_count - len(self._allowed)}

    # ------------------------------------------------------------- rules

    def judge(self, method: Any, url: Any, *, top_document: bool) -> Verdict:
        """§3, for one request a gate session paused (module docstring)."""
        if not self.armed:
            return CONTINUE
        origin = origin_of(url)
        verb = str(method or "GET").upper()
        if origin is None:
            # data:, blob:, about: -- nothing leaves the browser for them.
            return CONTINUE
        on_target = origin in self.origins
        if top_document:
            allowed = origin == self.base_origin or (on_target and self.signing_in)
            if not allowed or (origin == self.base_origin and self._excluded(str(url))):
                return Verdict("leave", origin, verb)
        if verb in _READ_METHODS:
            return CONTINUE
        if not on_target:
            return Verdict("refuse", origin, verb)
        if verb not in _WRITE_METHODS:
            return CONTINUE
        if verb == "POST" and self._idp_token(url):
            # Before the sign-in's own blanket rule, so the run is told the
            # provider was reached while the gate signed in as well.
            return Verdict("continue", origin, verb, allowed=IDP_TOKEN_ALLOWED)
        if self.signing_in or self.mutating:
            return CONTINUE
        if verb == "POST" and origin == self.base_origin and self._read_post(str(url)):
            return CONTINUE
        return Verdict("refuse", origin, verb)

    def _idp_token(self, url: Any) -> bool:
        """QA-IDP-POST: `url` is exactly the approved token URL, and the gate's
        own sign-in has begun, whatever it ended as, and nothing was wiped
        (module docstring). The whole URL is compared as a string: a query,
        a fragment, a port, userinfo, case or an encoding is another URL."""
        return (self.idp_token_url is not None and isinstance(url, str) and url == self.idp_token_url
                and self.sign_in_begun and not self.wiped)

    def _excluded(self, url: str) -> bool:
        path = _path_of(url)
        return any(fnmatch.fnmatchcase(path, g) for g in self.excluded_paths)

    def _read_post(self, url: str) -> bool:
        path = _path_of(url)
        return any(path == p or fnmatch.fnmatchcase(path, p) for p in self.read_posts)

    def navigation_wait(self) -> float:
        """Seconds the next top-frame navigation waits; reserves its slot."""
        now = self._clock()
        at = max(now, self._next_navigation)
        self._next_navigation = at + self.navigation_seconds
        return at - now

    def request_wait(self) -> float:
        """Seconds the next request to the target's origins waits; reserves its slot."""
        now = self._clock()
        at = max(now, self._next_request)
        self._next_request = at + self.request_spacing
        return at - now


def _parse(args: Mapping[str, Any]) -> dict[str, Any]:
    """The worker's hand-off, checked field by field; `QaTargetRefused(<field>)` on the first bad one."""
    target_id = args.get("target_id")
    if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        raise QaTargetRefused("target_id")
    environment_name = args.get("environment_name")
    if "environment_name" in args and not (isinstance(environment_name, str)
                                           and _ENVIRONMENT_NAME.fullmatch(environment_name)):
        raise QaTargetRefused("environment_name")
    origins = args.get("origins")
    if (not isinstance(origins, list) or not 1 <= len(origins) <= MAX_ORIGINS
            or not all(isinstance(o, str) and _ORIGIN.fullmatch(o) for o in origins)):
        raise QaTargetRefused("origins")
    base_url = args.get("base_url")
    if not isinstance(base_url, str) or origin_of(base_url) not in origins \
            or not base_url.startswith("https://"):
        raise QaTargetRefused("base_url")
    environment = args.get("environment", "production")
    if environment not in ("production", "non_production"):
        raise QaTargetRefused("environment")
    rate = args.get("rate") or {}
    nav, rps = (rate.get("navigation_ms", 2000), rate.get("requests_per_s", 10)) \
        if isinstance(rate, Mapping) else (None, None)
    if (not isinstance(nav, int) or isinstance(nav, bool) or not MIN_NAVIGATION_MS <= nav <= MAX_NAVIGATION_MS
            or not isinstance(rps, int) or isinstance(rps, bool) or not 1 <= rps <= MAX_REQUESTS_PER_S):
        raise QaTargetRefused("rate")
    read_posts = args.get("read_posts") or []
    if not isinstance(read_posts, list) or len(read_posts) > MAX_PATHS \
            or not all(isinstance(p, str) and _PATH.fullmatch(p) for p in read_posts):
        raise QaTargetRefused("read_posts")
    exclusions = args.get("exclusions") or {}
    paths = exclusions.get("paths") if isinstance(exclusions, Mapping) else None
    paths = paths or []
    if not isinstance(paths, list) or len(paths) > MAX_PATHS \
            or not all(isinstance(p, str) and _PATH.fullmatch(p) for p in paths):
        raise QaTargetRefused("exclusions")
    mutating = args.get("mutating", False)
    if not isinstance(mutating, bool):
        raise QaTargetRefused("mutating")
    sign_in = _parse_sign_in(args.get("sign_in"), origins)
    token_path, token_url = _parse_idp_token_path(args.get("idp_token_path"), origins,
                                                  origin_of(base_url), sign_in["kind"])
    credential = None
    if sign_in["kind"] == "own_login":
        raw = args.get("credential")
        username = raw.get("username") if isinstance(raw, Mapping) else None
        password = raw.get("password") if isinstance(raw, Mapping) else None
        if (not isinstance(username, str) or not 1 <= len(username) <= MAX_USERNAME
                or not isinstance(password, str) or not 1 <= len(password) <= MAX_PASSWORD):
            raise QaTargetRefused("credential")
        credential = (username, password)
    elif "credential" in args:
        raise QaTargetRefused("credential")
    return {"target_id": target_id, "environment_name": environment_name, "origins": origins, "base_url": base_url,
            "environment": environment, "navigation_ms": nav, "requests_per_s": rps,
            "read_posts": read_posts, "excluded_paths": paths, "mutating": mutating,
            "idp_token_path": token_path, "idp_token_url": token_url,
            "sign_in": sign_in, "credential": credential,
            "challenge_bypass": _parse_bypass(args.get("challenge_bypass"))}


def _parse_idp_token_path(raw: Any, origins: list[str], base_origin: str | None,
                          sign_in_kind: str) -> tuple[str | None, str | None]:
    """(path, the one URL it allows), checked again (QA-IDP-POST): swarm-api
    took it only from a platform admin's approval with the extra origin, and
    the hand-off crossed the worker. The extra origin is the one approved
    origin that is not the base's; with none, or more than one, or a target
    that never signs in, there is nothing to bound it, and it is refused."""
    if raw is None:
        return None, None
    if (not isinstance(raw, str) or len(raw) > MAX_IDP_TOKEN_PATH or not _IDP_TOKEN_PATH.fullmatch(raw)
            or any(segment in (".", "..") for segment in raw.split("/"))):
        raise QaTargetRefused("idp_token_path")
    extra = [o for o in origins if o != base_origin]
    if sign_in_kind != "own_login" or len(extra) != 1:
        raise QaTargetRefused("idp_token_path")
    return raw, f"{extra[0]}{raw}"


def _parse_bypass(raw: Any) -> tuple[str, str, str] | None:
    """(kind, name, value), checked again: swarm-api refused a reserved name and
    a value no header or cookie carries, and the hand-off crossed the worker."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {"kind", "name", "value"}:
        raise QaTargetRefused("challenge_bypass")
    kind, name, value = raw.get("kind"), raw.get("name"), raw.get("value")
    if kind not in BYPASS_KINDS or not isinstance(name, str) or not _BYPASS_NAME.fullmatch(name) \
            or not isinstance(value, str) or not _BYPASS_VALUE.fullmatch(value):
        raise QaTargetRefused("challenge_bypass")
    lowered = name.lower()
    if kind == "header" and (lowered in RESERVED_BYPASS_HEADERS
                             or lowered.startswith(RESERVED_BYPASS_HEADER_PREFIXES)):
        raise QaTargetRefused("challenge_bypass")
    return kind, name, value


def _parse_sign_in(raw: Any, origins: Iterable[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("kind") not in ("none", "own_login"):
        raise QaTargetRefused("sign_in")
    if raw["kind"] == "none":
        return {"kind": "none"}
    out: dict[str, Any] = {"kind": "own_login"}
    login_url = raw.get("login_url")
    if not isinstance(login_url, str) or not login_url.startswith("https://") \
            or origin_of(login_url) not in set(origins):
        raise QaTargetRefused("login_url")
    out["login_url"] = login_url
    for key in ("signed_in_selector", *_SELECTOR_KEYS):
        value = raw.get(key)
        if value is None and key != "signed_in_selector":
            continue
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_SELECTOR or "\n" in value:
            raise QaTargetRefused(key)
        out[key] = value
    clicks = _parse_start_clicks(raw.get("start_clicks"))
    if clicks:
        out["start_clicks"] = clicks
    return out


def _parse_start_clicks(raw: Any) -> list[dict[str, str]]:
    """swarm-api checked these at registration (`qaspec.start_clicks`); the
    gate checks their shape again, since the hand-off crossed the worker."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_START_CLICKS:
        raise QaTargetRefused("start_clicks")
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"text", "css"}:
            raise QaTargetRefused("start_clicks")
        text, css = item.get("text"), item.get("css")
        if (not isinstance(text, str) or text != text.strip() or not 1 <= len(text) <= MAX_START_CLICK_TEXT
                or "\n" in text):
            raise QaTargetRefused("start_clicks")
        click = {"text": text}
        if css is not None:
            if not isinstance(css, str) or not 1 <= len(css) <= MAX_START_CLICK_CSS or "\n" in css:
                raise QaTargetRefused("start_clicks")
            click["css"] = css
        out.append(click)
    return out


# --------------------------------------------------------------------------- #
# The guard: §3 on the gate's own sessions                                    #
# --------------------------------------------------------------------------- #


class GateCdp(Protocol):
    async def call(self, method: str, params: Mapping[str, Any] | None = None,
                   session_id: str | None = None) -> dict[str, Any]: ...


@dataclass
class _Session:
    target_id: str
    kind: str  # "page", "iframe", "worker", ...


async def _asyncio_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class QaTargetGuard:
    """§3's rules on sessions the gate owns. One per Chromium (per proxy).

    `start` attaches the gate to every page and, through each page's
    auto-attach, to its frames and workers, and enables `Fetch` at the
    `Request` stage on each. `on_event` is fed every message from the pipe and
    never waits: each answer runs on a task.
    """

    def __init__(self, cdp: GateCdp, target: QaTarget, *, record: Record,
                 sleep: Sleep | None = None) -> None:
        self._cdp = cdp
        self._target = target
        self._record = record
        self._sleep = sleep or _asyncio_sleep
        self.started = False
        self._sessions: dict[str, _Session] = {}
        self._attaching: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    async def start(self) -> None:
        if self.started or self._closed or not self._target.armed:
            return
        self.started = True
        cookies = self._target.bypass_cookies()
        if cookies:
            # §2.5: on the gate's own pipe, host-only, for the approved origins alone.
            planted = await self._quietly("Storage.setCookies", {"cookies": cookies}, None)
            self._record("qa_bypass_cookie" if planted is not None else "qa_bypass_cookie_refused",
                         {"origins": len(cookies)})
        await self._quietly("Target.setDiscoverTargets", {"discover": True}, None)
        listed = await self._quietly("Target.getTargets", {}, None) or {}
        for info in listed.get("targetInfos") or []:
            if isinstance(info, Mapping) and info.get("type") == "page" and isinstance(info.get("targetId"), str):
                await self._attach_page(info["targetId"])

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def owns(self, session: Any) -> bool:
        return isinstance(session, str) and session in self._sessions

    def _spawn(self, work: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(work)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _quietly(self, method: str, params: Mapping[str, Any], session: str | None) -> dict[str, Any] | None:
        try:
            return await self._cdp.call(method, params, session)
        except Exception as exc:  # noqa: BLE001 -- a target that went is not an error here
            log.debug("qa target: %s failed (%s)", method, type(exc).__name__)
            return None

    async def _attach_page(self, target_id: str) -> None:
        if target_id in self._attaching or any(s.target_id == target_id for s in self._sessions.values()):
            return
        self._attaching.add(target_id)
        try:
            result = await self._quietly("Target.attachToTarget", {"targetId": target_id, "flatten": True}, None)
            session = (result or {}).get("sessionId")
            if isinstance(session, str):
                self._sessions[session] = _Session(target_id, "page")
                await self._setup(session)
        finally:
            self._attaching.discard(target_id)

    async def _setup(self, session: str, *, waiting: bool = False) -> None:
        try:
            await self._quietly("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                                session)
            await self._quietly("Target.setAutoAttach",
                                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, session)
        finally:
            if waiting:
                await self._quietly("Runtime.runIfWaitingForDebugger", {}, session)

    def on_event(self, message: Mapping[str, Any]) -> None:
        if self._closed or not self.started:
            return
        method = message.get("method")
        raw = message.get("params")
        params: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        session = message.get("sessionId")
        if not isinstance(method, str):
            return
        if session is None:
            if method == "Target.targetCreated":
                info = params.get("targetInfo") or {}
                if info.get("type") == "page" and isinstance(info.get("targetId"), str):
                    self._spawn(self._attach_page(info["targetId"]))
            elif method == "Target.detachedFromTarget":
                self._sessions.pop(str(params.get("sessionId")), None)
            return
        if session not in self._sessions:
            return
        if method == "Fetch.requestPaused":
            self._spawn(self._paused(session, params))
        elif method == "Target.attachedToTarget":
            child = params.get("sessionId")
            info = params.get("targetInfo") or {}
            if isinstance(child, str):
                self._sessions[child] = _Session(str(info.get("targetId", "")), str(info.get("type", "")))
                self._spawn(self._setup(child, waiting=params.get("waitingForDebugger") is True))
        elif method == "Target.detachedFromTarget":
            self._sessions.pop(str(params.get("sessionId")), None)

    def _top_document(self, session: str, params: Mapping[str, Any]) -> bool:
        owner = self._sessions.get(session)
        return (owner is not None and owner.kind == "page" and params.get("resourceType") == "Document"
                and params.get("frameId") == owner.target_id)

    async def _paused(self, session: str, params: Mapping[str, Any]) -> None:
        request_id = params.get("requestId")
        if not isinstance(request_id, str):
            return
        request = params.get("request") or {}
        url = request.get("url")
        top = self._top_document(session, params)
        verdict = self._target.judge(request.get("method"), url, top_document=top)
        if verdict.allowed is not None:
            # QA-IDP-POST: let through, and the run is told so, apart from the refusals.
            self._record(verdict.allowed, {"method": verdict.method, "origin": verdict.origin})
            self._target.note_allowed(verdict.allowed, verdict.method, url)
        if verdict.action == "refuse":
            self._record(WRITE_REFUSED, {"method": verdict.method, "origin": verdict.origin})
            self._target.note_refusal(WRITE_REFUSED, verdict.method, url)
            await self._answer(session, request_id, 403, "text/plain; charset=utf-8",
                               b"refused by the live gate: this QA run is read-only here")
            return
        if verdict.action == "leave":
            self._record(TARGET_LEFT, {"origin": verdict.origin})
            self._target.note_refusal(TARGET_LEFT, verdict.method, url)
            await self._answer(session, request_id, 200, "text/html; charset=utf-8",
                               left_page(verdict.origin or "unknown").encode())
            return
        wait = 0.0
        if top:
            wait = self._target.navigation_wait()
        elif origin_of(url) in self._target.origins:
            wait = self._target.request_wait()
        if wait > 0:
            await self._sleep(wait)
        bypass = self._target.bypass_header(url)
        if bypass is not None:
            await self._continue_with(session, request_id, request.get("headers"), bypass)
            return
        await self._quietly("Fetch.continueRequest", {"requestId": request_id}, session)

    async def _continue_with(self, session: str, request_id: str, raw_headers: Any,
                             bypass: tuple[str, str]) -> None:
        """§2.5: the request's own headers, a same-named one dropped, and the bypass's."""
        name, value = bypass
        headers = raw_headers if isinstance(raw_headers, Mapping) else {}
        out = [{"name": str(n), "value": str(v)} for n, v in headers.items() if str(n).lower() != name.lower()]
        out.append({"name": name, "value": value})
        try:
            await self._cdp.call("Fetch.continueRequest", {"requestId": request_id, "headers": out}, session)
        except Exception as exc:  # noqa: BLE001 -- never leave a request paused: it goes without the bypass
            self._record("qa_bypass_continue_refused", {"cause": type(exc).__name__})
            await self._quietly("Fetch.continueRequest", {"requestId": request_id}, session)
        finally:
            out.clear()

    async def _answer(self, session: str, request_id: str, status: int, content_type: str, body: bytes) -> None:
        done = await self._quietly("Fetch.fulfillRequest", {
            "requestId": request_id, "responseCode": status,
            "responseHeaders": [{"name": "Content-Type", "value": content_type},
                                {"name": "Cache-Control", "value": "no-store"}],
            "body": base64.b64encode(body).decode("ascii"),
        }, session)
        if done is None:
            # Never sent: a request that fails is the safe failure here.
            await self._quietly("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"},
                                session)


# --------------------------------------------------------------------------- #
# The sign-in, on a tab the gate owns                                          #
# --------------------------------------------------------------------------- #

#: Installed on the gate's own tab before it loads the login page: a WebAuthn
#: request is a challenge (§2.4), recorded on the page and refused.
_WEBAUTHN_TRAP = """(() => {
  const trap = () => { window.__swarmQaWebAuthn = true;
    return Promise.reject(new DOMException('refused by the live gate', 'NotAllowedError')); };
  try { if (navigator.credentials) {
    navigator.credentials.get = trap; navigator.credentials.create = trap; } } catch (e) {}
})()"""

#: The challenge frames §2.4 refuses: `mask.SHOWN_IN_CONTROL`, restated
#: (written when mask.py imported the proxy, which imports this module);
#: tests/unit/worker/test_qa_target_signin.py holds the two equal.
CHALLENGE_FRAMES = (
    ("www.google.com", "/recaptcha/"),
    ("www.recaptcha.net", "/recaptcha/"),
    ("*.hcaptcha.com", "/"),
    ("challenges.cloudflare.com", "/"),
)
_CHALLENGE_HOSTS = json.dumps([[host, path] for host, path in CHALLENGE_FRAMES])

#: Every expression the sign-in evaluates starts `/*swarm-qa:<op>*/` and calls
#: one function with one JSON argument, so what it does is read off its name.
#: How a control is shown and named, for a start click and for the probe's
#: start page: visible, its text trimmed with runs of white space as one space
#: (a label laid out over two lines, or with a no-break space, is still its
#: words), exactly equal to the click's `text`; the outermost of a nested pair
#: dropped (a button and the span inside it are one control). For a click,
#: `clickable` keeps of those only what a person can click (part of #2688):
#: the match, or an ancestor reading the same text, that is a button, an `a`
#: with an href, an input of type submit or button, role button or link, a
#: summary or an element with a non-negative tabindex; failing those, one
#: whose computed cursor is a pointer. A heading's span that reads "Sign in"
#: beside the "Sign in" button is text, not a second control. `named` says
#: what matched, by tag and role, for a refusal's sentence.
_CONTROLS = """
  const shown = (el) => { const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && el.getClientRects().length > 0; };
  const label = (el) => ((el instanceof HTMLInputElement ? el.value : el.innerText) || '')
    .replace(/\\s+/g, ' ').trim();
  const controls = (c) => {
    let all;
    try { all = [...document.querySelectorAll(c.css || '*')]; } catch (e) { return null; }
    const hits = all.filter((el) => label(el) === c.text && shown(el));
    return hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
  };
  const role = (el) => (el.getAttribute('role') || '').trim().toLowerCase().split(/\\s+/)[0];
  const operable = (el) => {
    const tag = el.tagName.toLowerCase(), r = role(el), t = el.getAttribute('tabindex');
    return tag === 'button' || tag === 'summary' || (tag === 'a' && el.hasAttribute('href'))
      || (tag === 'input' && ['submit', 'button'].includes((el.getAttribute('type') || '').toLowerCase()))
      || r === 'button' || r === 'link' || (t !== null && parseInt(t, 10) >= 0);
  };
  const clickable = (c, hits) => {
    const found = [];
    for (const el of hits) {
      const chain = [];
      for (let n = el; n && label(n) === c.text; n = n.parentElement) chain.push(n);
      const hit = chain.find(operable) || chain.find((n) => getComputedStyle(n).cursor === 'pointer');
      if (hit && !found.includes(hit)) found.push(hit);
    }
    return found;
  };
  const named = (els) => els.slice(0, 3).map((el) => ({tag: el.tagName.toLowerCase(), role: role(el)}));"""

#: What the gate's tab shows. Before the submit: `form` once both fields are
#: there. After it (`a.after`), the page's own state: `form` while a password
#: field is still shown, `error` when one is shown with an alert, `chooser`
#: when the app is back on its start page (the last start click's control
#: shown), `pending` while a document loads or carries an OAuth callback's
#: `code`/`state`, `app` once it settled on the target's own origin with
#: neither, and `loading` otherwise (the identity provider's page, a blank
#: one). Before the submit, given `a.home` and `a.marks` (the last start
#: click), a page that settled on the target's own origin, drew itself and
#: shows no password or username field and no control a mark names is
#: `landed`: a role's own home that the session reached without the form
#: (part of #2688), which `SignIn` takes as signed in only with a session
#: cookie the sign-in set. `signed_in` and `challenge` first, either way. A selector the page's
#: `querySelector` does not take matches nothing and is named (`bad:<key>`)
#: instead of throwing the whole probe, which read as "no form" forever.
#: `signed_in_selector` is looked for through open shadow roots too.
_PROBE = """/*swarm-qa:probe*/((a) => {%s
  const bad = [];
  const query = (root, sel, key) => {
    try { return root.querySelector(sel); } catch (e) { if (!bad.includes(key)) bad.push(key); return null; }
  };
  const deep = (root, sel, key) => {
    const hit = query(root, sel, key);
    if (hit || bad.includes(key)) return hit;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) { const inner = deep(el.shadowRoot, sel, key); if (inner) return inner; }
    }
    return null;
  };
  const one = (sel, fallback, key) => {
    if (sel) return query(document, sel, key);
    const all = document.querySelectorAll(fallback);
    return all.length === 1 ? all[0] : null;
  };
  if (a.signed && deep(document, a.signed, 'signed_in_selector')) return 'signed_in';
  if (window.__swarmQaWebAuthn) return 'challenge';
  if (document.querySelector('input[autocomplete~="one-time-code"]')) return 'challenge';
  const hosts = %s;
  for (const f of document.querySelectorAll('iframe')) {
    try { const u = new URL(f.src, location.href);
      for (const [h, p] of hosts) {
        const hit = h.startsWith('*.') ? u.hostname.endsWith(h.slice(1)) : u.hostname === h;
        if (hit && u.pathname.startsWith(p)) return 'challenge';
      } } catch (e) {}
  }
  const user = one(a.user, 'input[autocomplete~="username"], input[type=email]', 'username_selector');
  const pass = one(a.pass, 'input[type=password]', 'password_selector');
  if (!a.after) {
    if (user && pass) return 'form';
  } else if ((pass && shown(pass)) || [...document.querySelectorAll('input[type=password]')].some(shown)) {
    const alerted = [...document.querySelectorAll('[role=alert], [aria-live=assertive], input[aria-invalid=true]')]
      .some((el) => shown(el) && (el instanceof HTMLInputElement || label(el) !== ''));
    return alerted ? 'error' : 'form';
  }
  if (bad.length) return 'bad:' + bad.join(',');
  const settled = document.readyState === 'complete'
    && !/[?&#](code|state|session_state)=/.test(location.search + location.hash);
  if (!a.after) {
    if (!a.home || !settled || location.origin !== a.home) return 'loading';
    const fields = 'input[type=password], input[autocomplete~="username"], input[type=email]';
    if ([...document.querySelectorAll(fields)].some(shown)) return 'loading';
    for (const c of a.marks || []) { const hits = controls(c); if (hits === null || hits.length) return 'loading'; }
    return label(document.body || document.documentElement) ? 'landed' : 'loading';
  }
  if (!settled) return 'pending';
  if (location.origin !== a.home) return 'loading';
  if (a.chooser) { const hits = controls(a.chooser); if (hits && hits.length) return 'chooser'; }
  if (!label(document.body || document.documentElement)) return 'loading';
  return 'app';
})(%%s)""" % (_CONTROLS, _CHALLENGE_HOSTS)

_FOCUS = """/*swarm-qa:focus*/((a) => {
  const all = a.sel ? [document.querySelector(a.sel)] : [...document.querySelectorAll(a.fallback)];
  if (all.length !== 1 || !all[0]) return false;
  all[0].focus(); all[0].value = ''; return document.activeElement === all[0];
})(%s)"""

_SUBMIT = """/*swarm-qa:submit*/((a) => {
  if (a.sel) { const b = document.querySelector(a.sel); if (!b) return false; b.click(); return true; }
  const p = document.querySelector('input[type=password]');
  if (!p || !p.form) return false;
  if (p.form.requestSubmit) p.form.requestSubmit(); else p.form.submit();
  return true;
})(%s)"""

_CLEAR = """/*swarm-qa:clear*/((a) => {
  let n = 0;
  const walk = (doc) => {
    for (const el of doc.querySelectorAll('input[type=password], input[autocomplete~="current-password"], '
      + 'input[autocomplete~="new-password"], input[autocomplete~="one-time-code"]')) { el.value = ''; n++; }
    for (const f of doc.querySelectorAll('iframe, frame')) {
      try { if (f.contentDocument) walk(f.contentDocument); } catch (e) {}
    }
  };
  walk(document); return n;
})(%s)"""

#: A start click (§2.2 step 3): the controls `_CONTROLS` names for the click
#: (`css` narrowing the candidates, any element without one). Clicks only when
#: exactly one of them is `clickable` (whatever plain text also reads the
#: same), or none is and there is exactly one match, AND the page has finished
#: loading (a server-drawn chooser is shown before its script runs, and a
#: click then does nothing), unless `a.now` (the wait for the load ran out).
#: Returns 1 for a click, 0 for no match, -1 when `css` is not a selector the
#: page's `querySelectorAll` takes, -2 for one control on a page still
#: loading, and for several `{count, clickable, matched}`: how many matched,
#: how many of them a person can click, and the first three of the clickable
#: ones (of every match when none is) by tag and role. A click marks the
#: document with its address and whether it began to unload, for `_AFTER_CLICK`.
_START_CLICK = """/*swarm-qa:start-click*/((a) => {%s
  const hits = controls(a);
  if (hits === null) return -1;
  if (!hits.length) return 0;
  const live = clickable(a, hits);
  const pick = live.length ? live : hits;
  if (pick.length !== 1) return {count: pick.length, clickable: live.length, matched: named(pick)};
  if (document.readyState !== 'complete' && !a.now) return -2;
  window.__swarmQaClicked = location.href;
  if (!window.__swarmQaLeaveHook) { window.__swarmQaLeaveHook = true;
    addEventListener('beforeunload', () => { window.__swarmQaLeaving = true; }); }
  pick[0].click();
  return 1;
})(%%s)""" % _CONTROLS

#: After a start click: `moved` once another document (or another address)
#: finished loading, `loading` while one loads or the clicked one unloads,
#: `same` while the clicked document is still there, at the same address.
_AFTER_CLICK = """/*swarm-qa:after-click*/((a) => {
  if (window.__swarmQaClicked === undefined || window.__swarmQaClicked !== location.href)
    return document.readyState === 'complete' ? 'moved' : 'loading';
  return window.__swarmQaLeaving ? 'loading' : 'same';
})(%s)"""

#: Where the gate's tab is, for a cause's sentence: origin and path, never the
#: query or fragment (an IdP's carries its state and code).
_WHERE = """/*swarm-qa:where*/((a) => location.origin + location.pathname)(%s)"""

_USER_FALLBACK = 'input[autocomplete~="username"], input[type=email]'
_PASS_FALLBACK = "input[type=password]"


def _js(template: str, argument: Mapping[str, Any]) -> str:
    return template % json.dumps(dict(argument))


#: What a sentence never carries from the page's address: white space,
#: control characters, quotes and backticks.
_UNSAID = re.compile(r"[\x00-\x20\x7f`'\"]")
_SELECTOR_NAMES = frozenset({"signed_in_selector", "username_selector", "password_selector"})


def _bad_selectors(state: Any) -> str | None:
    """`bad:<key>,<key>` from the probe as "a, b", only names the gate set."""
    if not isinstance(state, str) or not state.startswith("bad:"):
        return None
    keys = [k for k in state[4:].split(",") if k in _SELECTOR_NAMES]
    return " and ".join(keys) or None


#: What a refusal's sentence keeps of a match's tag or role: lower-case
#: letters, digits and hyphens, at most this many.
_NAME_CHARS = re.compile(r"[^a-z0-9-]")
MAX_MATCH_NAME = 24


def _match_name(match: Any) -> str | None:
    """One match `_START_CLICK` named, as "button" or "span (role link)"."""
    if not isinstance(match, Mapping):
        return None
    tag = _NAME_CHARS.sub("", str(match.get("tag") or "").lower())[:MAX_MATCH_NAME]
    role = _NAME_CHARS.sub("", str(match.get("role") or "").lower())[:MAX_MATCH_NAME]
    if not tag:
        return None
    return f"{tag} (role {role})" if role else tag


def _start_click_sentence(text: str, found: Any, seconds: float) -> str:
    """Why the start click `text` was not made: what `_START_CLICK` last
    returned, saying for several matches how many a person can click and
    which elements they were (tag and role, at most three)."""
    rule = "and the gate clicks only when exactly one is a control a person can click"
    if isinstance(found, Mapping):
        count, live = found.get("count"), found.get("clickable")
        names = [n for n in (_match_name(m) for m in list(found.get("matched") or ())[:3]) if n]
        count = count if type(count) is int and count > 1 else max(len(names), 2)
        named = f" ({', '.join(names)})" if names else ""
        if type(live) is int and live > 1:
            what = f"{live} visible controls a person can click with exactly that text{named}"
        else:
            what = (f"{count} visible elements with exactly that text{named}, none of them a control "
                    "a person can click")
        return f"the sign-in's start click '{text}' found {what} in {seconds:.0f} s, {rule}"
    if type(found) is int and found == -1:
        what = "none (its css is not a selector the page takes)"
    elif type(found) is not int or found < 2:
        what = "none"
    else:
        what = str(found)
    return (f"the sign-in's start click '{text}' found {what} visible controls with exactly that text "
            f"in {seconds:.0f} s, {rule}")


class _Landed:
    """Before the submit: whether a `landed` page is signed in (part of
    #2688). Only on the page's own evidence, never a guess: it read `landed`
    for PAGE_SETTLED_SECONDS without change, and the browser holds a session
    cookie for an approved origin that it did not hold before `login_url`
    opened, or holds with another value (the sign-in set or renewed it).
    The cookies are read once the page has settled, then each probe."""

    def __init__(self, sign_in: SignIn) -> None:
        self._sign_in = sign_in
        self._since: float | None = None

    async def signed_in(self, state: Any) -> bool:
        if state != "landed":
            self._since = None
            return False
        now = self._sign_in._clock()
        if self._since is None:
            self._since = now
        before = self._sign_in._cookies_before
        if now - self._since < PAGE_SETTLED_SECONDS or before is None:
            return False
        held = await self._sign_in._session_cookies()
        return held is not None and bool(held - before)


class SignIn:
    """§2.2 step 3 on a tab of the gate's own. `run` returns (state, cause).

    The proxy holds the agent around it (`CdpProxy.qa_sign_in`). Nothing here
    records or logs the username, the password or a selector's match."""

    def __init__(self, cdp: GateCdp, target: QaTarget, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep | None = None, probe_seconds: float = PROBE_SECONDS) -> None:
        self._cdp = cdp
        self._target = target
        self._clock = clock
        self._sleep = sleep or _asyncio_sleep
        self._probe_seconds = probe_seconds
        #: The gate's own tab, while it is open.
        self.tab: str | None = None
        self.session: str | None = None
        #: How many times `_wait_form` made the start clicks again.
        self._reclicks = 0
        #: The session cookies before `login_url` opened (`_session_cookies`),
        #: None when the browser did not say.
        self._cookies_before: frozenset[tuple[str, str, str, str]] | None = None
        #: A start click that never acted, or none: the last start click,
        #: whose control on the page marks the start page (`landed`).
        self._mark: Mapping[str, str] | None = None

    async def run(self) -> tuple[str, str | None]:
        settings = self._target.sign_in
        try:
            created = await self._cdp.call("Target.createTarget", {"url": "about:blank"})
            self.tab = str(created["targetId"])
            attached = await self._cdp.call("Target.attachToTarget", {"targetId": self.tab, "flatten": True})
            self.session = str(attached["sessionId"])
            await self._cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": _WEBAUTHN_TRAP},
                                 self.session)
            self._cookies_before = await self._session_cookies()
            await self._cdp.call("Page.navigate", {"url": settings["login_url"]}, self.session)
        except Exception as exc:  # noqa: BLE001 -- the browser went, or refused the tab
            log.warning("qa target: the sign-in tab did not open (%s)", type(exc).__name__)
            return STATE_FAILED, BROWSER_GONE
        clicks: list[Mapping[str, str]] = list(settings.get("start_clicks") or ())
        self._mark = clicks[-1] if clicks else None
        made: list[Mapping[str, str]] = []
        for index, click in enumerate(clicks):
            # The login page's own first load draws the first control, as it
            # draws a form: FORM_SECONDS bounds it. Each later one follows a click.
            ended = await self._start_click(click, FORM_SECONDS if index == 0 else START_CLICK_SECONDS)
            if ended == "form":
                # The page is already the form (an entry query, or the IdP's
                # session redirect, skipped the chooser): no click is left to make.
                break
            if ended is not None:
                return ended
            made.append(click)
        found = await self._wait_form(made)
        if found == "challenge":
            return STATE_CHALLENGE, CHALLENGE
        if found == "signed_in":
            return STATE_SIGNED_IN, None
        if found != "form":
            self._target.sentence = (await self._landed_sentence() if found == "landed"
                                     else await self._no_form_sentence(made, found))
            return STATE_FAILED, NO_FORM
        username, password = self._target.credential()
        typed = (await self._type(settings.get("username_selector"), _USER_FALLBACK, username)
                 and await self._type(settings.get("password_selector"), _PASS_FALLBACK, password))
        del username, password
        if not typed or not await self._evaluate(_js(_SUBMIT, {"sel": settings.get("submit_selector")})):
            self._target.sentence = ("the gate found the login form but could not focus exactly one username "
                                     "and one password field, or press the submit control")
            return STATE_FAILED, NO_FORM
        return await self._after_submit(clicks[-1] if clicks else None)

    def _argument(self, *, after: bool = False, chooser: Mapping[str, str] | None = None) -> dict[str, Any]:
        settings = self._target.sign_in
        argument: dict[str, Any] = {"signed": settings.get("signed_in_selector"),
                                    "user": settings.get("username_selector"),
                                    "pass": settings.get("password_selector")}
        if not after:
            argument.update(home=self._target.base_origin,
                            marks=[dict(self._mark)] if self._mark is not None else [])
        else:
            argument.update(after=True, home=self._target.base_origin,
                            chooser=dict(chooser) if chooser is not None else None)
        return argument

    async def _start_click(self, click: Mapping[str, str], seconds: float) -> tuple[str, str | None] | str | None:
        """One start click: None once it was made and the page settled,
        "form" when the login form is already there (no click is made), or
        how the sign-in ends. A challenge seen first ends it as a challenge;
        the signed-in state seen first ends it signed in (the IdP's session
        held), as does a `landed` page with its evidence (`_Landed`); a
        `landed` page without it when the time runs out is said as such, not
        as a missing control. The click waits for the page to finish loading (`_START_CLICK`)
        until the last probe interval of `seconds`. Where the click leads is
        judged by the guard like any navigation of the gate's tab: an origin
        the target was not approved for gets the left page, and the form then
        never appears."""
        argument = self._argument()
        deadline = self._clock() + seconds
        count: Any = None
        landed = _Landed(self)
        while True:
            state = await self._evaluate(_js(_PROBE, argument))
            if state == "challenge":
                return STATE_CHALLENGE, CHALLENGE
            if state == "signed_in" or await landed.signed_in(state):
                return STATE_SIGNED_IN, None
            if state == "form":
                return "form"
            last = self._clock() + self._probe_seconds >= deadline
            count = await self._evaluate(_js(_START_CLICK, {"text": click["text"], "css": click.get("css"),
                                                            "now": last}))
            if type(count) is int and count == 1:
                break
            if self._clock() >= deadline:
                self._target.sentence = (await self._landed_sentence() if state == "landed"
                                         else _start_click_sentence(click["text"], count, seconds))
                return STATE_FAILED, START_CLICK_MISSING
            await self._sleep(self._probe_seconds)
        await self._settle_after_click()
        return None

    async def _settle_after_click(self) -> str | None:
        """Wait for where a click led to finish loading: `moved`, or `same`
        when the clicked document stayed (a toggle; or a control whose page
        has not acted yet, which `_wait_form` clicks again)."""
        await self._sleep(self._probe_seconds)
        settle_by = self._clock() + START_CLICK_SECONDS
        while True:
            after = await self._evaluate(_js(_AFTER_CLICK, {}))
            if after in ("moved", "same") or self._clock() >= settle_by:
                return after if isinstance(after, str) else None
            await self._sleep(self._probe_seconds)

    async def _wait_form(self, made: list[Mapping[str, str]]) -> str | None:
        """Wait up to FORM_SECONDS for the form (or a challenge, or the
        signed-in state: `signed_in_selector`, or a `landed` page with its
        evidence, `_Landed`). After start clicks, a page that is still the
        document the last one was made on, at the same address, after
        RECLICK_SECONDS has not acted on it (its script was not running yet):
        the clicks are made again, in order, at most MAX_RECLICKS times, and
        only while each still names exactly one control."""
        argument = self._argument()
        start = self._clock()
        deadline = start + FORM_SECONDS
        clicked_at = start
        self._reclicks = 0
        state: Any = None
        landed = _Landed(self)
        while True:
            state = await self._evaluate(_js(_PROBE, argument))
            if state in ("form", "signed_in", "challenge"):
                return str(state)
            if await landed.signed_in(state):
                return "signed_in"
            now = self._clock()
            if now >= deadline:
                return state if isinstance(state, str) else None
            if made and self._reclicks < MAX_RECLICKS and now - clicked_at >= RECLICK_SECONDS \
                    and await self._evaluate(_js(_AFTER_CLICK, {})) == "same":
                if not await self._reclick(made):
                    made = []
                clicked_at = self._clock()
                continue
            await self._sleep(self._probe_seconds)

    async def _reclick(self, made: list[Mapping[str, str]]) -> bool:
        """Make the clicks again, in order; False (and no further click) as
        soon as one names other than exactly one control."""
        self._reclicks += 1
        for click in made:
            count = await self._evaluate(_js(_START_CLICK, {"text": click["text"], "css": click.get("css"),
                                                            "now": True}))
            if not (type(count) is int and count == 1):
                return False
            await self._settle_after_click()
        return True

    async def _where(self) -> str:
        """The gate's tab's origin and path for a sentence, plain characters only."""
        where = await self._evaluate(_js(_WHERE, {}))
        if not isinstance(where, str) or not where:
            return "a page the gate could not read"
        return _UNSAID.sub("", where)[:MAX_WHERE] or "a page the gate could not read"

    async def _no_form_sentence(self, made: list[Mapping[str, str]], state: Any) -> str:
        where = await self._where()
        bad = _bad_selectors(state)
        if bad:
            return (f"the target's {bad} is not a CSS selector the page's querySelector takes, so the gate "
                    f"could not find the login form at {where}")
        fields = f"no username and password field the target's selectors name in {FORM_SECONDS:.0f} s"
        if not made:
            return f"the login form never appeared: the gate's tab is at {where} with {fields}"
        names = ", ".join(f"'{c['text']}'" for c in made)
        if self._reclicks and await self._evaluate(_js(_AFTER_CLICK, {})) == "same":
            return (f"the gate made the start clicks {names} ({self._reclicks + 1} times) and the page at {where} "
                    f"did not change after '{made[-1]['text']}', so the login form never appeared")
        return f"after the start clicks {names} the login form never appeared: the gate's tab is at {where} with {fields}"

    async def _landed_sentence(self) -> str:
        """The tab settled on a page of the target's own origin that is not a
        login page (`landed`), but nothing the page gives says the account
        is signed in: neither signed_in_selector nor a session cookie the
        sign-in set. A role whose pages are walled (a reviewer's own home) lands
        so when signed_in_selector names what only other roles' pages draw.
        At most MAX_WHERE characters of the address, so the whole stays
        within the worker's one plain line of 400."""
        where = await self._where()
        return (f"the tab is at {where}, the target's page with no login form and no start-click control, "
                "but without signed_in_selector or a session cookie the sign-in set, so not proven signed in: "
                "signed_in_selector must name what all this account's pages show")

    async def _session_cookies(self) -> frozenset[tuple[str, str, str, str]] | None:
        """The HttpOnly cookies the browser holds for the target's approved
        origins, as (name, domain, path, value digest): a script cannot set
        one, so each is a server's, and the bypass cookie the gate planted
        is left out. None when the browser did not answer with a list. The
        value is kept only as its digest, and nothing here is recorded."""
        urls = [f"{origin}/" for origin in self._target.origins]
        try:
            result = await self._cdp.call("Network.getCookies", {"urls": urls}, self.session)
        except Exception:  # noqa: BLE001 -- a jar the browser does not read is no evidence
            return None
        cookies = (result or {}).get("cookies")
        if not isinstance(cookies, list):
            return None
        planted = self._target.bypass_name if self._target.bypass_kind == "cookie" else None
        held: set[tuple[str, str, str, str]] = set()
        for cookie in cookies:
            if not isinstance(cookie, Mapping) or cookie.get("httpOnly") is not True:
                continue
            name = cookie.get("name")
            if not isinstance(name, str) or name == planted:
                continue
            digest = hashlib.sha256(str(cookie.get("value")).encode()).hexdigest()
            held.add((name, str(cookie.get("domain")), str(cookie.get("path")), digest))
        return frozenset(held)

    async def _after_submit(self, last_click: Mapping[str, str] | None) -> tuple[str, str | None]:
        """THE VERDICT IS THE PAGE'S OWN STATE (part of #2688). Signed in as
        soon as `signed_in_selector` shows, or once the page has settled for
        PAGE_SETTLED_SECONDS on the target's own origin with no login form,
        no OAuth callback in its address and not back on the start page (the
        last start click's control): the app drew itself for a signed-in
        user. Failed when SIGNED_IN_SECONDS pass on the form, an error, the
        start page or another page; a page still loading at that point is
        waited for up to SIGNED_IN_MAX_SECONDS (the gate's own rate limit
        spaces every navigation and request of the redirect chain)."""
        argument = self._argument(after=True, chooser=last_click)
        start = self._clock()
        deadline, hard = start + SIGNED_IN_SECONDS, start + SIGNED_IN_MAX_SECONDS
        since: float | None = None
        last: Any = None
        while True:
            state = await self._evaluate(_js(_PROBE, argument))
            now = self._clock()
            if state == "challenge":
                return STATE_CHALLENGE, CHALLENGE
            if state == "signed_in":
                return STATE_SIGNED_IN, None
            since = since if state == last and since is not None else now
            last = state
            if state == "app" and now - since >= PAGE_SETTLED_SECONDS:
                return STATE_SIGNED_IN, None
            if state == "error" and now - since >= PAGE_SETTLED_SECONDS:
                break
            waiting = state in ("pending", "app")
            if now >= hard or (now >= deadline and not waiting):
                break
            await self._sleep(self._probe_seconds)
        self._target.sentence = await self._failed_sentence(state, last_click, now - start)
        return STATE_FAILED, SIGN_IN_FAILED

    async def _failed_sentence(self, state: Any, last_click: Mapping[str, str] | None, waited: float) -> str:
        if state == "form":
            return (f"after the submit the page still shows the login form {waited:.0f} s later: the test "
                    "account was not accepted, or the submit control did not submit")
        if state == "error":
            return "after the submit the login page shows an error: the test account was not accepted"
        if state == "chooser" and last_click is not None:
            return (f"after the submit the app is back on its start page, showing '{last_click['text']}', "
                    "so it did not sign in")
        where = await self._where()
        bad = _bad_selectors(state)
        if bad:
            return (f"after the submit the page at {where} could not be read: the target's {bad} is not a CSS "
                    "selector the page's querySelector takes")
        if state == "pending":
            return f"after the submit the page at {where} was still loading {waited:.0f} s later"
        return (f"after the submit the page at {where} neither drew signed_in_selector nor settled as the app "
                f"in {waited:.0f} s")

    async def close(self) -> None:
        """Clear the tab's password inputs, then close it. Never raises."""
        if self.session is not None:
            await self._evaluate(_js(_CLEAR, {}))
        if self.tab is not None:
            try:
                await self._cdp.call("Target.closeTarget", {"targetId": self.tab})
            except Exception as exc:  # noqa: BLE001 -- a tab already gone is closed
                log.debug("qa target: closing the sign-in tab failed (%s)", type(exc).__name__)
        self.tab = self.session = None

    async def _type(self, selector: str | None, fallback: str, text: str) -> bool:
        if not await self._evaluate(_js(_FOCUS, {"sel": selector, "fallback": fallback})):
            return False
        try:
            await self._cdp.call("Input.insertText", {"text": text}, self.session)
        except Exception:  # noqa: BLE001 -- never the exception's text: it could carry what was typed
            return False
        return True

    async def _evaluate(self, expression: str) -> Any:
        try:
            result = await self._cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                                          self.session)
        except Exception:  # noqa: BLE001 -- a navigation destroyed the context: read again
            return None
        return ((result or {}).get("result") or {}).get("value")


async def handle_ctl(target: QaTarget, proxy: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    """The gate's `qa_sign_in`: set the target, then start its guard and its
    sign-in on the proxy running now. Returns at once; the worker polls
    (`{poll: true}`) for the sign-in's end. Raises `QaTargetRefused`."""
    result = target.handle(args)
    if args.get("poll") is True:
        return result
    if proxy is None:
        # No Chromium now (closed, or relaunching): the sign-in cannot run, and
        # the worker is told so rather than left polling a sign-in that never starts.
        if target.wants_sign_in:
            target.finished(STATE_FAILED, BROWSER_GONE)
        return target.status()
    await proxy.start_qa_target()
    return target.status()


__all__ = [
    "BYPASS_KINDS", "RESERVED_BYPASS_HEADERS", "RESERVED_BYPASS_HEADER_PREFIXES",
    "BUSY", "CHALLENGE", "CHALLENGE_FRAMES", "CHALLENGE_SENTENCE", "EXHAUSTED", "FINAL", "MAX_SIGN_INS", "OP", "PLACEHOLDER",
    "QaTarget", "QaTargetGuard", "QaTargetRefused", "SIGN_IN_FAILED", "START_CLICK_MISSING", "SignIn", "Verdict",
    "handle_ctl",
    "left_page", "needles", "origin_of",
]
