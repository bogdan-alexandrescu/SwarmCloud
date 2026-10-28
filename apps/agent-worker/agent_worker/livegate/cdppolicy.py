"""The CDP method policy: what the agent may ask Chromium, always (live browser L2).

docs/design/live-browser.md section 5.4.4. Holding the agent's commands while a
person is in control (cdpproxy.py) does not undo state the agent set BEFORE it
asked for help: a browser context routed through the agent's own proxy, TLS
errors ignored, a page's CSP turned off, headers added to every request. Each
of those makes the person's sign-in readable later, so the defence cannot be
"pause the agent during control"; it has to be "the agent can never set that
state". This module is that rule, applied by the proxy to every agent command
from the first one.

THE ALLOWLIST IS THE CONTROL. `ALLOWED_METHODS` names the methods Playwright's
`connectOverCDP` sends for page work; anything else is refused. A method
Chromium adds next year is refused because it is not on the list, not because
someone remembered to deny it. `DENIED` is the documentation of the refusals
that matter most, with the reason each one changes how the browser trusts or
routes traffic; it is checked first so a refusal says why.

THREE PARAMETER RULES sit inside allowed methods, because the method itself is
needed and only one shape of it is dangerous:

* `Target.createBrowserContext` without `proxyServer`, `proxyBypassList` or
  `originsWithUniversalNetworkAccess` (Playwright's `newContext()` sends
  none of them unless asked for a proxy);
* `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` with one of
  Chromium's four behaviours. `deny` and `default` carry no path and pass as
  sent. `allow` and `allowAndName` download only into `downloads/`, the one
  volume the worker reads and the gate does not (section 3.1): a path outside
  it, or none, is CONFINED -- `confine` rewrites it to `downloads/` before the
  command is held or forwarded -- rather than refused. MEASURED 2026-10-13
  (task_1f28d5bfa5a14f618f83, LB-POLISH): Playwright's `connect_over_cdp`
  names its own artifacts directory (a mkdtemp under the CLIENT's tmpdir) for
  the default context, so refusing it refused the whole connection and the
  agent fell back to raw CDP. Chromium writes where the gate says, never
  where the agent said; a path the agent names is never forwarded;
* `DOM.setFileInputFiles` only from `uploads/`. Not in the design's table, and
  added here for the same reason as the download rule from the other side: the
  path is opened by Chromium, in the sidecar, so a path outside `uploads/`
  uploads the sidecar's own files -- the profile's cookie database among them
  -- to whatever site the page posts to.

and two that keep the proxy the only path: `Target.attachToTarget` and
`Target.setAutoAttach` must ask for `flatten`, because a non-flat session
tunnels CDP through `Target.sendMessageToTarget`, which this policy would
never see.

PATHS ARE RESOLVED ON CHROMIUM'S SIDE. The gate runs in the sidecar, beside
Chromium, so `os.path.realpath` here sees the symlinks Chromium would follow;
a link the worker planted in `uploads/` pointing at the profile resolves
outside and is refused. The check is at the time of the call: a link swapped
between the check and Chromium's open is not caught.

DEVICE EMULATION IS THE GATE'S (section 6.1, LB-6). The gate sets the device
from a named preset; an agent picks a preset through `swarm-handoff device`,
never by sending metrics. So the Emulation methods that change the device --
metrics, user agent, touch -- are refused, and the rest of Emulation, which
Playwright sends when it attaches to every page (focus, media, locale,
timezone), stays allowed. Playwright sends `Emulation.setDeviceMetricsOverride`
only for a context with a viewport, so the agent's `connectOverCDP` default
context is unaffected and `browser.newContext()` needs `viewport: null`.

COOKIES ARE THE GATE'S (section 5.5.1, LB-7). A signed-in session moves gate
to gate (session.py), read and written on the gate's own pipe session, where
this policy does not apply. The agent's CDP can neither read the jar
(`Network.getCookies`, `Network.getAllCookies`, `Storage.getCookies`) nor
plant, change or clear one (`Network.setCookie(s)`, `Storage.setCookies`,
`Network.deleteCookies`, `Network.clearBrowserCookies`, `Storage.clearCookies`,
`Storage.clearDataForOrigin`). Playwright's `context.cookies()`,
`addCookies()` and `clearCookies()` therefore fail loudly in the agent, which
is the intended outcome. Not covered, as the design says: a cookie without
`HttpOnly` is readable by page script through `Runtime.evaluate`.

PUPPETEER OPENS PAGES TOO (DEVB-GATE; part of #1030). dev-browser v1 drives
Chromium with puppeteer-core 25.8.0, whose `newPage()` fails outright when
`Performance.enable` or `Audits.enable` errors (CdpPage#initialize,
FrameManager.initialize; the sequence is pinned in
tests/unit/worker/fixtures/puppeteer-core-25.8.0-*.json). So:

* `Performance.enable` / `getMetrics` / `disable` are allowed. The domain
  reports counters -- `Metric` is a name and a number, the `metrics` event a
  list of those and a title -- and nothing of a page's content;
* `Audits.enable` and `Audits.disable` are ANSWERED BY THE GATE with `{}`
  (`LOCAL_REPLIES`) and never reach Chromium, so Chromium never enables the
  domain on an agent session, and the proxy drops any `Audits.*` event
  besides. CHECKED 2026-10-14 against the CDP protocol definition
  (devtools-protocol 0.0.1666840, the one puppeteer-core 25.8.0 pins):
  `Audits.issueAdded` carries `InspectorIssueDetails.cookieIssueDetails`,
  whose `rawCookieLine` is, in the protocol's words, "the raw Set-Cookie header
  string" when the cookie is malformed and no `AffectedCookie` could be made --
  a header with the cookie's value. Forwarded, it would hand the agent cookies
  the rule above keeps from it (LB-7). The domain's other methods stay off the
  allowlist: `getEncodedResponse` returns a response body, `checkFormsIssues`
  reads a page's forms;
* `WebMCP.enable` and `Network.emulateNetworkConditionsByRule` stay refused
  (the first is not on the list; the second would set network conditions
  by URL pattern) but are `QUIET_REFUSALS`: Puppeteer sends them, catches the
  error and goes on, so each page it opened recorded a refusal that meant
  nothing (ANALYSIS issue 19, refusal noise). The proxy counts them and logs
  them at debug instead of recording `cdp_method_refused`, and writes no
  refused action line either: that line is what the theater's timeline draws
  as "was refused", so a quiet refusal that wrote it was not quiet;
* `Emulation.setUserAgentOverride` and `Network.setUserAgentOverride` are quiet
  only in the shape of the clients' page-setup probe (`quiet_refusal`): on a
  page session, with an EMPTY `userAgent` and no other field set. Playwright
  and Puppeteer send exactly that on every page they attach and swallow the
  refusal (images/agent-runtime-browser/selftest.py reads it off Playwright's
  protocol log), so each page dev-browser opened wrote a refusal that meant
  nothing (#1346 comment 4). It stays refused -- the device is the gate's, by
  preset name -- and a user agent the agent chose is recorded as before.

A REFUSAL IS RECORDED BY METHOD NAME ONLY (`cdp_method_refused`), never with
its parameters, which can carry page text.

THE QA IDENTITY (QI-GATE; docs/design/qa-identity.md §4.4, part of #1649).
Once the task's QA identity is set (qaidentity.py), the gate adds its
credential to console requests on its own sessions, and `qa_identity_check`
keeps the agent's `Fetch` from undoing the rules around it:

* `Fetch.continueRequest` with `url` is refused (`qa_identity_redirect`): a
  rewritten URL is a redirect the redirect guards never judged;
* `Fetch.continueRequest` with `headers` for a request the proxy saw paused
  on the console host is refused (`qa_identity_headers`): the agent could
  otherwise replace the gate's header, or read it back by echoing it.

The rest of `Fetch` stays the agent's: `continueResponse` and
`fulfillRequest` change the response, not the request's headers;
`failRequest` sends nothing; `continueWithAuth` answers with the agent's own
credentials. A body that reflects the request is the one way in, and the
proxy scrubs every result for it (`QaIdentity.scrub`). The rule applies from
the moment the host is set, not only while a credential is unexpired, so an
expiry between two messages cannot open a window.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PolicyPaths:
    """The two shared volumes, at the path Chromium sees them (section 3.1)."""

    downloads_dir: Path
    uploads_dir: Path


@dataclass(frozen=True)
class Refusal:
    method: str
    reason: str
    #: A refusal's code where the record names one (`qa_identity_*`).
    cause: str | None = None

    def message(self) -> str:
        return f"refused by the live gate's method policy: {self.method} ({self.reason})"


# Section 5.4.4's table, plus the method-level refusals that follow from it.
# A `Target.createBrowserContext` and the two download methods are refused
# only for the parameter shapes named; the rest of this table is refused
# whole.
DENIED: Mapping[str, str] = {
    "Target.createBrowserContext": "a proxy routes the context's traffic, a sign-in included, through a server the agent chose",
    "Security.setIgnoreCertificateErrors": "lets a man-in-the-middle certificate pass",
    "Security.handleCertificateError": "lets a man-in-the-middle certificate pass",
    "Security.setOverrideCertificateErrors": "lets a man-in-the-middle certificate pass",
    "Page.setBypassCSP": "turns off the site's own script defences",
    "Network.setExtraHTTPHeaders": "adds headers the agent chose to every request",
    "Browser.setDownloadBehavior": "a download behavior Chromium does not name",
    "Page.setDownloadBehavior": "a download behavior Chromium does not name",
    "DOM.setFileInputFiles": "uploads a file from outside uploads/",
    "Target.attachToTarget": "a non-flat session tunnels CDP past this policy",
    "Target.setAutoAttach": "a non-flat session tunnels CDP past this policy",
    "Target.sendMessageToTarget": "tunnels CDP past this policy",
    "Target.exposeDevToolsProtocol": "hands CDP to a page",
    "Target.attachToBrowserTarget": "a second browser session is the proxy's to open",
    "Browser.close": "the browser is the platform's",
    "Input.setIgnoreInputEvents": "blocks the person's input",
    "Emulation.setDeviceMetricsOverride": "the device is the gate's, by preset name",
    "Emulation.clearDeviceMetricsOverride": "the device is the gate's, by preset name",
    "Emulation.setUserAgentOverride": "the device is the gate's, by preset name",
    "Emulation.setTouchEmulationEnabled": "the device is the gate's, by preset name",
    "Emulation.setEmitTouchEventsForMouse": "the device is the gate's, by preset name",
    "Emulation.setVisibleSize": "the device is the gate's, by preset name",
    "Network.setUserAgentOverride": "the device is the gate's, by preset name",
    "Network.getCookies": "reads the browser's cookies, a carried session's included",
    "Network.getAllCookies": "reads the browser's cookies, a carried session's included",
    "Storage.getCookies": "reads the browser's cookies, a carried session's included",
    "Network.setCookie": "writes the browser's cookies",
    "Network.setCookies": "writes the browser's cookies",
    "Network.deleteCookies": "writes the browser's cookies",
    "Network.clearBrowserCookies": "writes the browser's cookies",
    "Storage.setCookies": "writes the browser's cookies",
    "Storage.clearCookies": "writes the browser's cookies",
    "Storage.clearDataForOrigin": "writes the browser's cookies",
    "Network.emulateNetworkConditionsByRule": "sets the network's conditions by URL pattern; Puppeteer survives the refusal",
}

# Methods refused only for one shape of their parameters; every other entry
# in DENIED is refused outright.
_PARAMETER_RULED = frozenset(
    {
        "Target.createBrowserContext",
        "Browser.setDownloadBehavior",
        "Page.setDownloadBehavior",
        "DOM.setFileInputFiles",
        "Target.attachToTarget",
        "Target.setAutoAttach",
    }
)

_PROXY_PARAMETERS = ("proxyServer", "proxyBypassList", "originsWithUniversalNetworkAccess")

_DOWNLOAD_METHODS = ("Browser.setDownloadBehavior", "Page.setDownloadBehavior")
#: Chromium's download behaviours: the two that write nowhere, and the two
#: that write into `downloadPath`, which `confine` makes `downloads/`.
_DOWNLOAD_PATHLESS = ("deny", "default")
_DOWNLOAD_WRITING = ("allow", "allowAndName")

# What Playwright's Chromium driver sends over `connectOverCDP` for page work
# (crBrowser, crPage, crNetworkManager, crExecutionContext, crInput,
# crAccessibility, crDragDrop, crProtocolHelper). Grouped by domain; a method
# is added here only with a reason the agent needs it.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        # Browser: the version handshake, the window a page is in, downloads
        # (path-ruled above).
        "Browser.getVersion",
        "Browser.getWindowForTarget",
        "Browser.setDownloadBehavior",
        # Target: attach (flat only) and create-target, the agent's own
        # contexts (proxy-ruled above).
        "Target.setAutoAttach",
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Target.detachFromTarget",
        "Target.getTargets",
        "Target.getTargetInfo",
        "Target.createTarget",
        "Target.closeTarget",
        "Target.activateTarget",
        "Target.createBrowserContext",
        "Target.disposeBrowserContext",
        "Target.getBrowserContexts",
        # Page
        "Page.enable",
        "Page.disable",
        "Page.getFrameTree",
        "Page.setLifecycleEventsEnabled",
        "Page.addScriptToEvaluateOnNewDocument",
        "Page.removeScriptToEvaluateOnNewDocument",
        "Page.createIsolatedWorld",
        "Page.navigate",
        "Page.reload",
        "Page.stopLoading",
        "Page.getNavigationHistory",
        "Page.navigateToHistoryEntry",
        "Page.handleJavaScriptDialog",
        "Page.captureScreenshot",
        "Page.getLayoutMetrics",
        "Page.bringToFront",
        "Page.close",
        "Page.printToPDF",
        "Page.startScreencast",
        "Page.stopScreencast",
        "Page.screencastFrameAck",
        "Page.setInterceptFileChooserDialog",
        "Page.setFontFamilies",
        "Page.getResourceTree",
        "Page.getResourceContent",
        "Page.setDownloadBehavior",
        # Runtime
        "Runtime.enable",
        "Runtime.disable",
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Runtime.getProperties",
        "Runtime.releaseObject",
        "Runtime.releaseObjectGroup",
        "Runtime.awaitPromise",
        "Runtime.addBinding",
        "Runtime.removeBinding",
        "Runtime.runIfWaitingForDebugger",
        # DOM
        "DOM.enable",
        "DOM.disable",
        "DOM.getDocument",
        "DOM.describeNode",
        "DOM.resolveNode",
        "DOM.requestNode",
        "DOM.querySelector",
        "DOM.querySelectorAll",
        "DOM.getOuterHTML",
        "DOM.getAttributes",
        "DOM.getContentQuads",
        "DOM.getBoxModel",
        "DOM.getFrameOwner",
        "DOM.getNodeForLocation",
        "DOM.scrollIntoViewIfNeeded",
        "DOM.focus",
        "DOM.setFileInputFiles",
        # Input
        "Input.dispatchKeyEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchTouchEvent",
        "Input.dispatchDragEvent",
        "Input.insertText",
        "Input.imeSetComposition",
        "Input.setInterceptDrags",
        # Emulation, less the device methods (section 6.1)
        "Emulation.setFocusEmulationEnabled",
        "Emulation.setEmulatedMedia",
        "Emulation.setEmulatedVisionDeficiency",
        "Emulation.setLocaleOverride",
        "Emulation.setTimezoneOverride",
        "Emulation.setGeolocationOverride",
        "Emulation.clearGeolocationOverride",
        "Emulation.setScriptExecutionDisabled",
        "Emulation.setDefaultBackgroundColorOverride",
        "Emulation.setAutoDarkModeOverride",
        "Emulation.setIdleOverride",
        "Emulation.clearIdleOverride",
        # Accessibility
        "Accessibility.enable",
        "Accessibility.disable",
        "Accessibility.getFullAXTree",
        "Accessibility.getPartialAXTree",
        "Accessibility.queryAXTree",
        "Accessibility.getChildAXNodes",
        "Accessibility.getRootAXNode",
        "Accessibility.getAXNodeAndAncestors",
        # Network: the read side less cookies (section 5.5.1), and the
        # switches Playwright's network manager sets that do not route,
        # re-header or re-credential traffic.
        "Network.enable",
        "Network.disable",
        "Network.getResponseBody",
        "Network.getRequestPostData",
        "Network.setCacheDisabled",
        "Network.setBypassServiceWorker",
        "Network.emulateNetworkConditions",
        # Fetch: `page.route()`. In control the proxy answers every pause
        # itself (cdpproxy.py), so interception cannot touch the sign-in.
        "Fetch.enable",
        "Fetch.disable",
        "Fetch.continueRequest",
        "Fetch.continueResponse",
        "Fetch.continueWithAuth",
        "Fetch.fulfillRequest",
        "Fetch.failRequest",
        "Fetch.getResponseBody",
        # Storage: the storage key only. Its cookie methods are the gate's
        # (section 5.5.1), refused above.
        "Storage.getStorageKeyForFrame",
        # Performance: run-time counters only (`Metric` is a name and a
        # number). Puppeteer's newPage() fails without Performance.enable.
        "Performance.enable",
        "Performance.disable",
        "Performance.getMetrics",
        # Log and IO: console capture, and reading a PDF stream the agent made.
        "Log.enable",
        "Log.disable",
        "IO.read",
        "IO.close",
    }
)

# Methods the gate answers itself, with this result, without forwarding them
# (module docstring): a client that needs the call to succeed gets its yes,
# and Chromium never turns the domain on for an agent session.
LOCAL_REPLIES: Mapping[str, Mapping[str, Any]] = {
    "Audits.enable": {},
    "Audits.disable": {},
}

# Events no agent session receives, by domain prefix, whatever enabled them.
DROPPED_EVENT_DOMAINS: tuple[str, ...] = ("Audits.",)

# Refused like any method off the allowlist, but known probes a client
# survives: counted and logged at debug, never recorded (module docstring).
QUIET_REFUSALS: frozenset[str] = frozenset({"WebMCP.enable", "Network.emulateNetworkConditionsByRule"})

# Refused always, quiet only as the clients' page-setup probe: an empty user
# agent on a page session and nothing else (`quiet_refusal`, module docstring).
QUIET_PROBES: frozenset[str] = frozenset({"Emulation.setUserAgentOverride", "Network.setUserAgentOverride"})


def quiet_refusal(method: str, params: Mapping[str, Any] | None, *, page_session: bool) -> bool:
    """Whether a refusal of `method` is counted and logged at debug instead of
    recorded (module docstring): a `QUIET_REFUSALS` method, or a `QUIET_PROBES`
    one sent on a page session with `userAgent` "" and no other field set."""
    if method in QUIET_REFUSALS:
        return True
    if method not in QUIET_PROBES or not page_session:
        return False
    given = params if isinstance(params, Mapping) else {}
    if given.get("userAgent") != "":
        return False
    # Playwright sends `acceptLanguage` and `userAgentMetadata` as undefined,
    # which JSON drops; a value in either, or any other field, is a choice.
    return all(value is None for key, value in given.items() if key != "userAgent")

# A response carrying page content. One that completes while a person is in
# control was sent before control and resolved during it -- an `awaitPromise`
# waiting on the password field, a screenshot of the form -- so the proxy
# answers it with an error instead (cdpproxy.py).
CONTENT_METHODS: frozenset[str] = frozenset(
    {
        "Runtime.evaluate",
        "Runtime.callFunctionOn",
        "Runtime.awaitPromise",
        "Runtime.getProperties",
        "Page.captureScreenshot",
        "Page.printToPDF",
        "Page.getResourceContent",
        "DOM.getDocument",
        "DOM.getOuterHTML",
        "DOM.getAttributes",
        "DOM.querySelector",
        "DOM.querySelectorAll",
        "Accessibility.getFullAXTree",
        "Accessibility.getPartialAXTree",
        "Accessibility.queryAXTree",
        "Accessibility.getChildAXNodes",
        "Accessibility.getRootAXNode",
        "Accessibility.getAXNodeAndAncestors",
        "Network.getResponseBody",
        "Network.getRequestPostData",
        "Fetch.getResponseBody",
        "IO.read",
    }
)

# Methods whose answer is a request's body. After return they are refused for
# a request that started while a person was in control (section 5.4.3).
REQUEST_BODY_METHODS: frozenset[str] = frozenset({"Network.getRequestPostData", "Network.getResponseBody"})


def _inside(path: Any, root: Path) -> bool:
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return False
    resolved = os.path.realpath(path)
    base = os.path.realpath(str(root))
    return resolved == base or resolved.startswith(base + os.sep)


def _parameter_refusal(method: str, params: Mapping[str, Any], paths: PolicyPaths) -> bool:
    if method == "Target.createBrowserContext":
        return any(key in params for key in _PROXY_PARAMETERS)
    if method in _DOWNLOAD_METHODS:
        if params.get("behavior") in _DOWNLOAD_PATHLESS:
            return False
        return params.get("behavior") not in _DOWNLOAD_WRITING or not _inside(
            params.get("downloadPath"), paths.downloads_dir
        )
    if method == "DOM.setFileInputFiles":
        files = params.get("files", [])
        return not isinstance(files, list) or not all(_inside(f, paths.uploads_dir) for f in files)
    if method in ("Target.attachToTarget", "Target.setAutoAttach"):
        return params.get("flatten") is not True
    return False


def confine(method: str, params: Mapping[str, Any], paths: PolicyPaths) -> Mapping[str, Any]:
    """`params` as Chromium may receive them: a download that writes is
    pointed into `downloads/` when it named anywhere else (module docstring).
    Every other command is returned as it came; `check` still runs after."""
    if (
        method in _DOWNLOAD_METHODS
        and params.get("behavior") in _DOWNLOAD_WRITING
        and not _inside(params.get("downloadPath"), paths.downloads_dir)
    ):
        return {**params, "downloadPath": str(paths.downloads_dir)}
    return params


def check(method: str, params: Mapping[str, Any] | None, paths: PolicyPaths) -> Refusal | None:
    """None when the agent may send `method` with `params`; the refusal when not."""
    params = params if isinstance(params, Mapping) else {}
    if method.startswith("Security."):
        return Refusal(method, DENIED.get(method, "the Security domain decides what TLS protects"))
    if method in _PARAMETER_RULED:
        if _parameter_refusal(method, params, paths):
            return Refusal(method, DENIED[method])
        return None
    if method in DENIED:
        return Refusal(method, DENIED[method])
    if method in LOCAL_REPLIES:
        return None
    if method not in ALLOWED_METHODS:
        return Refusal(method, "not on the allowlist")
    return None


#: `qa_identity_check`'s refusals, by cause (module docstring).
QA_IDENTITY_REFUSALS: Mapping[str, str] = {
    "qa_identity_redirect": "a rewritten URL is a redirect the QA identity's guards never judged",
    "qa_identity_headers": "a console request's headers carry the QA identity and are the gate's",
}


def qa_identity_check(method: str, params: Mapping[str, Any] | None,
                      console_request: Callable[[Any], bool]) -> Refusal | None:
    """None when the agent may send `method` while the QA identity is set.

    `console_request(requestId)` says whether the proxy saw that request
    paused on the console host; an id it never saw counts as one (fail closed)."""
    if method != "Fetch.continueRequest":
        return None
    params = params if isinstance(params, Mapping) else {}
    if "url" in params:
        return Refusal(method, QA_IDENTITY_REFUSALS["qa_identity_redirect"], "qa_identity_redirect")
    if "headers" in params and console_request(params.get("requestId")):
        return Refusal(method, QA_IDENTITY_REFUSALS["qa_identity_headers"], "qa_identity_headers")
    return None
