# QA identity: measurements (M1 to M5)

The measurements [qa-identity.md](qa-identity.md) §4.9 requires before
QI-API and QI-GATE are built. Each is dated and comes with the control that
shows it could have come out the other way. This file records **M3 and M5**,
which concern only the image's Chromium. M1, M2 and M4 need the QA account and
are not measured here (last section).

## The browser, and what could not be measured

**Measured 2026-10-20 01:34Z, lane QI-MEASURE-CHROMIUM (part of #1649), in
the `agent-runtime-browser` image on `claude-code-browser`.**

* **Chromium: `Chrome/153.0.8010.12`** (`Browser.getVersion`'s `product`;
  Playwright's `browser.version` reads `153.0.8010.12`). This is Playwright
  1.63.0's bundled `chromium-1243`, the full build the live gate launches
  headed on Xvfb. It is the browser QI-GATE will inject into.
* **It is the live browser, reached over CDP at `$SWARM_CDP_ENDPOINT`, not
  a `chromium.launch(headless=True)`.** On `claude-code-browser` a second
  Chromium is refused by design: the launch exits with "Starting a separate
  Chromium is refused here: nobody could see it". Playwright's
  `connect_over_cdp` then failed on this browser too: its driver asserts on
  an attached `chrome-extension://` service-worker target and exits. So the
  harness speaks raw CDP over a stdlib websocket. It sends the messages the
  gate sends (`Target.attachToTarget` flattened, `Fetch.enable`,
  `Fetch.continueRequest`) with no client library in between.
* **https could not be loaded, so every observation at an echo is over
  http.** The gate's method policy refuses
  `Security.setIgnoreCertificateErrors` ("lets a man-in-the-middle
  certificate pass"), and the image's policy sets
  `SSLErrorOverrideAllowed: false`. Each https navigation to the echoes'
  self-signed certificates ended `net::ERR_CERT_AUTHORITY_INVALID`, and no
  request reached either https echo. The harness then ran the same
  measurements against the same echoes without TLS, on the same loopback.
  Two things hold regardless of scheme:
  * Chromium refused `Proxy-Authorization` at the `Fetch.continueRequest`
    call, before any connection. The refusal was identical on the https and
    http runs.
  * The loopback is on the proxy bypass list (`<local>` comes first,
    `livegate/egress.py`), so no proxy took part in either scheme.

  Whether Chromium **sends** an added `Authorization` over a TLS connection
  was not observed. That is the one gap, and it is a question in this
  lane's PR. The operator's M1 closes it for free: M1 reaches the real
  console over https with a valid certificate.

The harness (end of this file) reads what each echo received from the
echo's own server-side log, not from the page. Header values are made at run
time (`"qa-measure-" + secrets.token_hex(8)`, never a credential) and are
shown below only as `<value>`. Every request either echo received carried
these 14 headers, plus whatever the row adds: `accept`, `accept-encoding`,
`accept-language`, `connection`, `host`, `sec-ch-ua`, `sec-ch-ua-mobile`,
`sec-ch-ua-platform`, `sec-fetch-dest`, `sec-fetch-mode`, `sec-fetch-site`,
`sec-fetch-user`, `upgrade-insecure-requests` and `user-agent`.

Origins: A `http://127.0.0.1:41201`, B `http://127.0.0.1:41519`. The ports
are ephemeral and differ on every run. A's `/redirect` answers 302 to B's
`/echo`, and its `/redirect-same` answers 302 to A's own `/echo`.

## M3: does a header added by `Fetch.continueRequest` reach a non-proxy origin? (2026-10-20)

Sent: a top-level GET of A's `/echo` in a new tab. `Fetch.enable` used one
pattern, `*`, at the `Request` stage. The one `Fetch.requestPaused` was
answered with `Fetch.continueRequest`, carrying the paused request's headers
plus the header in the row.

| run | `Fetch.continueRequest` | A received |
|---|---|---|
| control: nothing added | accepted | the 14 headers; no `authorization`, no `proxy-authorization` |
| `Proxy-Authorization` added, value `Bearer <value>` | **refused by Chromium: `Unsafe header: Proxy-Authorization`** (a CDP error; the request stays paused) | the request, after the harness continued it unmodified: the 14 headers, **no `proxy-authorization`** |
| `Authorization` added, value `Bearer <value>` | accepted | the 14 headers **plus `authorization`, value `Bearer <value>`** |

**M3: no.** Chromium 153 does not send `Proxy-Authorization` added by
`Fetch.continueRequest`. It does not drop the header silently, either: it
refuses the call. A gate that adds the header this way would get a CDP error
on every console request, and the request would stay paused until something
continued it. **`Authorization` is accepted and reaches the origin** (over
http; see above). Under §4.9 the gate therefore uses `Authorization`, and M2
must also show that swarm-api ignores an `Authorization` header when an IAP
assertion is present.

The control could have come out the other way. With the same handler,
`Authorization` arrived at A on the same run. So the missing
`Proxy-Authorization` is Chromium refusing that header, not a handler that
adds nothing. "Unsafe header" appears nowhere in this repository: it is
Chromium's message, not the gate's. The gate's refusals read "refused by the
live gate's method policy".

## M5: does an added header follow a cross-origin redirect? (2026-10-20)

Sent: a top-level GET of A's `/redirect`, which answers 302 to B's `/echo`.
The header is added by `Fetch.continueRequest` **only** to that first
request. A paused redirect request is recognised by `redirectedRequestId` in
`Fetch.requestPaused`, and with no guard it is continued with no `headers`
parameter, i.e. unmodified.

| run | Fetch pattern | A received (the 302) | redirect request at its pause | B received |
|---|---|---|---|---|
| control: nothing added | `*` | the 14 headers | no auth header | the 14 headers |
| `Proxy-Authorization` added | `*` | the `continueRequest` was refused (`Unsafe header`) and the request was continued unmodified: no `proxy-authorization` | no auth header | **no `proxy-authorization`** |
| `Authorization` added (the cross-origin control) | `*` | `authorization`, value `Bearer <value>` | **no `authorization`** | **no `authorization`** |
| `Proxy-Authorization` added, B never paused | A's origin only | refused as above; no `proxy-authorization` | (not paused) | **no `proxy-authorization`** |
| `Authorization` added, B never paused | A's origin only | `authorization`, value `Bearer <value>` | (not paused) | **no `authorization`** |

**The control holds: `Authorization` did not arrive at B**, so the harness
is not carrying headers it should not, and M5 is recorded.

`Proxy-Authorization` cannot be measured past the first request: Chromium
refuses to add it (M3). So **M5 for `Proxy-Authorization` is moot** in this
build: B cannot receive what was never sent.

**Then guard 2** (§4.4): on any paused request whose host is not A's, if
its `request.headers` holds the header (case-insensitive), continue it with
the header removed.

| run | Fetch pattern | A received | redirect request at its pause | guard 2 acted | B received |
|---|---|---|---|---|---|
| `Proxy-Authorization` added, guard 2 on | `*` | refused as above; no `proxy-authorization` | no auth header | no (nothing to remove) | **no `proxy-authorization`** |
| `Authorization` added, guard 2 on | `*` | `authorization`, value `Bearer <value>` | **no `authorization`** | no (nothing to remove) | **no `authorization`** |

**With guard 2, B does not see the header**, as it must not. But guard 2 never acted. The redirected request reached its pause already without the header, so guard 2 had nothing to remove.

### Why B never saw it: the header does not follow any redirect

The design expected Chromium's cross-origin rule to be what strips
`Authorization` from B's request. Two more controls show the header follows
**no** redirect at all, same-origin included:

| run | Fetch pattern | A's first request | A's redirected request (A's `/echo`) |
|---|---|---|---|
| `Authorization` added to A's `/redirect-same` (302 to A) | `*`: the redirect is paused and continued unmodified | `authorization`, value `Bearer <value>` | **no `authorization`** (and none at its pause) |
| the same, nothing paused after the first request | the first URL only | `authorization`, value `Bearer <value>` | **no `authorization`** |

So, in Chrome 153.0.8010.12, **a header added by `Fetch.continueRequest`
applies to that one request only**. It is not carried to the redirect's
request, whatever the redirect's origin, and whether or not the redirect is
paused. Cross-origin `Authorization` stripping was never exercised, because
the header was never there to strip.

**What this records, for the reader of §4.4 and §7 risk 5:**

* **Neither redirect guard was load-bearing on this build.** In every run,
  the gate's own added header reached only the request it was added to.
  Both guards are still built (§4.9: "they are, either way"), because
  nothing here holds Chromium to this behaviour across a version bump.
* **Guard 2 had nothing to strip.** No request paused at the Fetch stage
  carried the header after a redirect. Its "removed" branch is unexercised
  against a real Chromium here. QI-GATE's fake-Chromium test is what
  covers it.
* **A redirect that stays on the console host loses the header too.** The
  redirect's request comes back to the `https://<host>/*` `Request`-stage
  pattern as its own pause, with `redirectedRequestId` set. It is
  authenticated only if the injection rules (§4.4, rules 1 to 3) add the
  header to it again. This is a fact about Chromium for QI-GATE to build
  on; this lane does not change the design.

## The harness

One file, Python 3.11, stdlib plus `playwright` (used only to locate the
bundled Chromium when there is no live browser). `openssl` is on PATH. It
prints one JSON document. With `$SWARM_CDP_ENDPOINT` set it measures the
live browser; without it, it launches Playwright's bundled Chromium headless
with `--ignore-certificate-errors` and a debugging port, and then the https
half observes as well. That launch path was **not run here**, because the
image refuses it. The run recorded above is
`/opt/venv/bin/python3 harness.py > out.json`, exit 0. Its output, values
already masked, is the lane's artifact `qi-measure-chromium-out.json`.
The block below, extracted from this file and run again at 01:35:59Z, gave
the same result in every row.

```python
"""M3 and M5 of docs/design/qa-identity.md section 4.9, in this image's Chromium.

Two echo servers on 127.0.0.1, on two ports (two origins). Each answers with
its request line and every header it received, as JSON, and logs the same
server-side; the harness reads that log. Origin A's /redirect answers 302 to
origin B's /echo, and its /redirect-same 302 to its own /echo. On a CDP session attached to a new tab the harness sends
Fetch.enable with one pattern for every request at the Request stage and
answers each Fetch.requestPaused with Fetch.continueRequest, adding a header
where the live gate would. Raw CDP, over a stdlib websocket: the same
messages the gate sends, with no client library in between.

The browser: $SWARM_CDP_ENDPOINT when set (claude-code-browser, where a second
Chromium is refused by design and the live browser is the image's Chromium),
otherwise Playwright's bundled Chromium, launched headless with
--ignore-certificate-errors and a debugging port.

https: each origin has its own self-signed certificate made at run time.
http: the same echoes without TLS, on the same loopback. Both are attempted;
a scheme the browser cannot load is recorded with the reason it gave.
The header value is made at run time and is not a credential.
"""
import base64
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

TMP = Path(tempfile.mkdtemp(prefix="qi-measure-"))
VALUE = "qa-measure-" + secrets.token_hex(8)
NAMES = ("proxy-authorization", "authorization")


# ---- the echoes -------------------------------------------------------------

def make_cert(name):
    key, crt = TMP / f"{name}.key", TMP / f"{name}.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", f"/CN=qi-measure-{name}", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", str(key), "-out", str(crt)],
        check=True, capture_output=True,
    )
    return crt, key


def start_echo(name, tls, redirects=None):
    """An echo; `redirects` maps a path to a callable giving its 302 Location."""
    log = []
    redirects = redirects or {}

    class Echo(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            log.append({"request_line": self.requestline, "headers": headers})
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path]())
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"request_line": self.requestline, "headers": headers}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*make_cert(name))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"{'https' if tls else 'http'}://127.0.0.1:{server.server_address[1]}", log


# ---- CDP over a stdlib websocket ---------------------------------------------

class CDP:
    def __init__(self, ws_url):
        u = urlsplit(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(head.split(b"\r\n")[0].decode())
        self.next_id = 0
        self.events = []

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("CDP connection closed")
            buf += chunk
        return buf

    def _recv_message(self):
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(n)
            opcode = b0 & 0x0F
            if opcode == 8:
                raise ConnectionError("CDP connection closed")
            if opcode in (0, 1, 2):
                data += payload
                if b0 & 0x80:
                    return json.loads(data)

    def _send_frame(self, text):
        payload = text.encode()
        head = bytes([0x81])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def post(self, method, params=None, session=None):
        """Send without waiting; returns the message id."""
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._send_frame(json.dumps(msg))
        return self.next_id

    def call(self, method, params=None, session=None):
        mid = self.post(method, params, session)
        while True:
            msg = self._recv_message()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def next_message(self, deadline):
        """The next event or command response, whichever comes first."""
        if self.events:
            return self.events.pop(0)
        self.sock.settimeout(max(0.1, deadline - time.time()))
        try:
            return self._recv_message()
        finally:
            self.sock.settimeout(30)


def browser_ws():
    endpoint = os.environ.get("SWARM_CDP_ENDPOINT", "").strip()
    proc = None
    if not endpoint:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        n = port.getsockname()[1]
        port.close()
        proc = subprocess.Popen([exe, "--headless", "--ignore-certificate-errors",
                                 f"--remote-debugging-port={n}", f"--user-data-dir={TMP / 'profile'}",
                                 "about:blank"], stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{n}"
        for _ in range(100):
            try:
                urllib.request.urlopen(endpoint + "/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
    with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=10) as r:
        ws = json.load(r)["webSocketDebuggerUrl"]
    return ws, ("launched" if proc else "live (SWARM_CDP_ENDPOINT)"), proc


# ---- one measurement -----------------------------------------------------------

def shown(headers):
    """Header names with their values; a value carrying VALUE is shown as <value>."""
    return {k: ("<value>" if VALUE in v else v) for k, v in headers.items()}


def run(cdp, a, b, path, header=None, guard2=False, pattern="*"):
    """GET A's `path` in a new tab with every request paused at the Request stage.

    header: the header the handler adds -- only to the first request for A's
            `path`, never to a redirect's request. None is the control.
    guard2: design section 4.4 redirect guard 2: a paused request whose host
            is not A's and whose headers hold `header` is continued with it
            removed. Every other request is continued unmodified.
    pattern: Fetch.enable's one urlPattern, `*` by default. "A" is origin
            A only, so a request to B is never paused: the gate with its `*`
            pattern (guard 2) off. "first" is the first request's URL only,
            so no redirect is paused at all.
    """
    (origin_a, log_a), (_, log_b) = a, b
    log_a.clear()
    log_b.clear()
    url = origin_a + path
    host_a = urlsplit(origin_a).netloc
    paused = []
    continued = {}  # Fetch.continueRequest message id -> (paused entry, requestId)
    target = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    outcome = None
    try:
        cdp.call("Page.enable", session=session)
        pattern = {"A": origin_a + "/*", "first": url}.get(pattern, pattern)
        cdp.call("Fetch.enable", {"patterns": [{"urlPattern": pattern, "requestStage": "Request"}]},
                 session=session)
        # Posted, not awaited: the navigation answers only after its first
        # request is continued below.
        nav_id = cdp.post("Page.navigate", {"url": url}, session)
        deadline = time.time() + 20
        while outcome is None and time.time() < deadline:
            try:
                msg = cdp.next_message(deadline)
            except socket.timeout:
                break
            if msg.get("id") in continued:
                entry, request_id = continued.pop(msg["id"])
                if "error" in msg:
                    # Chromium refused the continue: the request is still
                    # paused. Record why, then continue it unmodified, so the
                    # echo shows the request arrived and what it carried.
                    entry["continue_refused"] = msg["error"].get("message")
                    retry = cdp.post("Fetch.continueRequest", {"requestId": request_id}, session)
                    continued[retry] = ({"url": entry["url"], "action": "retry unmodified"},
                                        request_id)
                    paused.append(continued[retry][0])
                else:
                    entry["continue_result"] = "ok"
                continue
            if msg.get("id") == nav_id:
                if "error" in msg or msg.get("result", {}).get("errorText"):
                    outcome = msg.get("error", msg.get("result", {}).get("errorText"))
                continue
            if msg.get("sessionId") != session or "method" not in msg:
                continue
            if msg["method"] == "Page.loadEventFired":
                outcome = "loaded"
            elif msg["method"] == "Fetch.requestPaused":
                ev = msg["params"]
                req = ev["request"]
                carried = {k.lower(): v for k, v in req["headers"].items()}
                entry = {"url": req["url"], "redirect": "redirectedRequestId" in ev,
                         "headers_at_pause": shown(carried), "action": "continued unmodified"}
                params = {"requestId": ev["requestId"]}
                if header and req["url"] == url and "redirectedRequestId" not in ev:
                    params["headers"] = [{"name": k, "value": v} for k, v in req["headers"].items()]
                    params["headers"].append({"name": header, "value": "Bearer " + VALUE})
                    entry["action"] = f"continued with {header} added"
                elif guard2 and header and urlsplit(req["url"]).netloc != host_a \
                        and header.lower() in carried:
                    params["headers"] = [{"name": k, "value": v}
                                         for k, v in req["headers"].items()
                                         if k.lower() != header.lower()]
                    entry["action"] = f"continued with {header} removed (guard 2)"
                paused.append(entry)
                continued[cdp.post("Fetch.continueRequest", params, session)] = (
                    entry, ev["requestId"])
        time.sleep(0.3)  # let a slow echo thread append its log line
    finally:
        cdp.call("Target.closeTarget", {"targetId": target})
    result = {"get": url, "header_added": header, "guard2": guard2,
              "fetch_pattern": pattern, "outcome": outcome,
              "fetch_paused": paused,
              "a_received": [dict(r, headers=shown(r["headers"])) for r in log_a],
              "b_received": [dict(r, headers=shown(r["headers"])) for r in log_b]}
    for side, log in (("a", log_a), ("b", log_b)):
        for name in NAMES:
            result[f"{side}_got_{name}"] = any(VALUE in r["headers"].get(name, "") for r in log)
    return result


def measure(cdp, tls):
    b = start_echo("b", tls)
    a = start_echo("a", tls, {"/redirect": lambda: b[0] + "/echo",
                              "/redirect-same": lambda: a[0] + "/echo"})
    out = {"origin_a": a[0], "origin_b": b[0]}
    out["m3_control"] = run(cdp, a, b, "/echo")
    out["m3_proxy_authorization"] = run(cdp, a, b, "/echo", "Proxy-Authorization")
    out["m3_authorization"] = run(cdp, a, b, "/echo", "Authorization")
    out["m5_control"] = run(cdp, a, b, "/redirect")
    for name in ("Proxy-Authorization", "Authorization"):
        key = name.lower().replace("-", "_")
        out[f"m5_{key}"] = run(cdp, a, b, "/redirect", name)
        out[f"m5_{key}_guard2"] = run(cdp, a, b, "/redirect", name, guard2=True)
    # The second control: A's 302 to A's own /echo. If the added header
    # arrives on A's second request, Fetch's added headers do follow a
    # redirect, and only the cross-origin rule kept it from B.
    out["m5_authorization_same_origin"] = run(cdp, a, b, "/redirect-same", "Authorization")
    # B never paused: does Chromium carry a Fetch-added header across a
    # redirect that nothing intercepts?
    for name in ("Proxy-Authorization", "Authorization"):
        key = name.lower().replace("-", "_")
        out[f"m5_{key}_unpaused"] = run(cdp, a, b, "/redirect", name, pattern="A")
    # ... and the same for A's 302 to itself, with only the first request
    # paused: does the header follow a redirect nothing intercepts, at all?
    out["m5_authorization_same_origin_unpaused"] = run(
        cdp, a, b, "/redirect-same", "Authorization", pattern="first")
    return out


ws, how, proc = browser_ws()
cdp = CDP(ws)
try:
    version = cdp.call("Browser.getVersion")
    out = {"browser": how, "product": version.get("product"),
           "user_agent": version.get("userAgent"),
           "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        cdp.call("Security.setIgnoreCertificateErrors", {"ignore": True})
        out["ignore_certificate_errors"] = "accepted"
    except RuntimeError as exc:
        out["ignore_certificate_errors"] = str(exc)
    out["https"] = measure(cdp, tls=True)
    out["http"] = measure(cdp, tls=False)
finally:
    cdp.sock.close()
    if proc:
        proc.terminate()

json.dump(out, sys.stdout, indent=2)
```

## M1, M2, M4: not measured here

M1, M2 and M4: not measured here. The operator measures them with the QA
account after the owner's bootstrap apply (O2), as decided on 2026-10-19
(#1649). They need the QA account, a signed JWT and the deployed front door,
and this lane holds none of them.

Two things from the measurements above change what the operator measures:

* **M3 is "no", so M1 and M2 use `Authorization: Bearer`, not
  `Proxy-Authorization`.** M1's JWT goes in `Authorization`. M2 must then show two
  things: that swarm-api ignores an `Authorization` header when an IAP
  assertion is present (§4.9, M3), and whether IAP passes the header
  through, which decides the audit of routes that echo a request.
* **M1's first request also closes the https gap above.** It is an https
  request with a valid certificate. If the operator sends it from the
  image's Chromium, with the header added by `Fetch.continueRequest` as the
  gate will add it, a 200 shows that Chromium sends an added `Authorization`
  over TLS.
