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
* `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` that allows a
  download only into `downloads/`, the one volume the worker reads and the gate
  does not (section 3.1); `deny` and `default` carry no path and are allowed;
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

A REFUSAL IS RECORDED BY METHOD NAME ONLY (`cdp_method_refused`), never with
its parameters, which can carry page text.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    "Browser.setDownloadBehavior": "writes files outside downloads/",
    "Page.setDownloadBehavior": "writes files outside downloads/",
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
    "Network.setCookie": "writes the browser's cookies",
    "Network.setCookies": "writes the browser's cookies",
    "Network.deleteCookies": "writes the browser's cookies",
    "Network.clearBrowserCookies": "writes the browser's cookies",
    "Storage.setCookies": "writes the browser's cookies",
    "Storage.clearCookies": "writes the browser's cookies",
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
        # Network: the read side, and the switches Playwright's network
        # manager sets that do not route, re-header or re-credential traffic.
        "Network.enable",
        "Network.disable",
        "Network.getResponseBody",
        "Network.getRequestPostData",
        "Network.getCookies",
        "Network.getAllCookies",
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
        # Storage: the read side.
        "Storage.getCookies",
        "Storage.getStorageKeyForFrame",
        # Log and IO: console capture, and reading a PDF stream the agent made.
        "Log.enable",
        "Log.disable",
        "IO.read",
        "IO.close",
    }
)

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
    if method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        if params.get("behavior") in ("deny", "default"):
            return False
        return not _inside(params.get("downloadPath"), paths.downloads_dir)
    if method == "DOM.setFileInputFiles":
        files = params.get("files", [])
        return not isinstance(files, list) or not all(_inside(f, paths.uploads_dir) for f in files)
    if method in ("Target.attachToTarget", "Target.setAutoAttach"):
        return params.get("flatten") is not True
    return False


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
    if method not in ALLOWED_METHODS:
        return Refusal(method, "not on the allowlist")
    return None
