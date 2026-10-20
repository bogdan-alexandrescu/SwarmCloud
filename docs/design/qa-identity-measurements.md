# QA identity: measurements (M1 to M5)

The measurements [qa-identity.md](qa-identity.md) §4.9 requires before
QI-API and QI-GATE are built. Each is dated and comes with the control that
shows it could have come out the other way. This file records **M3 and M5**,
which concern only the image's Chromium. M1, M2 and M4 need the QA account and
are not measured here (last section). M3's `Authorization` result was then
measured again over https, against a public echo with a real certificate
(["M3 over https"](#m3-over-https-2026-10-20)).

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
* **https could not be loaded at the loopback echoes, so every observation
  at them is over http.** (The gap this left is closed below.) The gate's method policy refuses
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
  was not observed by this lane. **That gap is closed**: lane
  QI-MEASURE-HTTPS (2026-10-20 02:18Z) sent the same `Fetch.continueRequest`
  from the same live browser to `https://postman-echo.com/headers`, over
  TLS 1.3 with a publicly trusted certificate, and the echo received
  `authorization` with the value sent. `Proxy-Authorization` was refused
  over https exactly as over http. See
  ["M3 over https"](#m3-over-https-2026-10-20).

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
continued it. **`Authorization` is accepted and reaches the origin**, over
http here and over https with a real certificate in
["M3 over https"](#m3-over-https-2026-10-20). Under §4.9 the gate therefore uses `Authorization`, and M2
must also show that swarm-api ignores an `Authorization` header when an IAP
assertion is present.

The control could have come out the other way. With the same handler,
`Authorization` arrived at A on the same run. So the missing
`Proxy-Authorization` is Chromium refusing that header, not a handler that
adds nothing. "Unsafe header" appears nowhere in this repository: it is
Chromium's message, not the gate's. The gate's refusals read "refused by the
live gate's method policy".

## M3 over https (2026-10-20)

**Measured 2026-10-20 02:18:58Z, lane QI-MEASURE-HTTPS (part of #1649), from
the same live browser as above: `Chrome/153.0.8010.12`** (`Browser.getVersion`'s
`product`), reached over CDP at `$SWARM_CDP_ENDPOINT`. It closes the one gap
QI-MEASURE-CHROMIUM left: whether Chromium sends an added `Authorization`
over a TLS connection.

**The echo.** `https://postman-echo.com/headers` answers, as JSON, the
request headers it received. The live browser's egress loaded it, so
`https://httpbin.org/headers`, the fallback, was not needed and was not
tried. Every run's main response read, from `Network.responseReceived`:
status 200, `h2`, `securityState` `secure`, TLS 1.3, certificate subject
`postman-echo.com`, issuer `YE2`, valid to 2026-12-23. The remote address was
the echo's own (`162.159.142.41:443`), not a loopback forwarder, so no proxy
took part and the TLS session ran from Chromium to the echo's front end. The
certificate is publicly trusted: no certificate error was overridden or
could have been (see above).

**Sent.** Three runs, each in a new tab, on the same messages as the harness
below: `Target.attachToTarget` flattened, `Fetch.enable` with one pattern,
`*`, at the `Request` stage, and a top-level GET of the echo. The one
`Fetch.requestPaused` was answered with `Fetch.continueRequest`, carrying the
paused request's headers plus the header in the row. Each run made its own
value at run time (`"qa-measure-" + secrets.token_hex(8)`, never a
credential), shown here only as `<value>`.

**Received.** Read from the page's own JSON body, through `Runtime.evaluate`
on `document.body.innerText`, and compared with that run's value. Each
request carried these 17 headers, plus whatever the row adds: `accept`,
`accept-encoding`, `accept-language`, `cookie`, `host`, `if-none-match`,
`priority`, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`,
`sec-fetch-dest`, `sec-fetch-mode`, `sec-fetch-site`, `sec-fetch-user`,
`upgrade-insecure-requests`, `user-agent` and `x-forwarded-proto`
(`https`). `cookie` and `if-none-match` are the echo's own, from earlier
loads in the same browser, and `x-forwarded-proto` is added by the echo's
front end. Their values are not recorded.

| run | `Fetch.continueRequest` | the echo received |
|---|---|---|
| control: nothing added | accepted | the 17 headers; **no `authorization`**, no `proxy-authorization` |
| `Authorization` added, value `Bearer <value>` | accepted | the 17 headers **plus `authorization`, value `Bearer <value>`**: the same value that was sent |
| `Proxy-Authorization` added, value `Bearer <value>` | **refused by Chromium: `Unsafe header: Proxy-Authorization`** (the request stays paused) | the request, after the harness continued it unmodified: the 17 headers, **no `proxy-authorization`** |

**The http result holds over TLS.** Chromium 153 sends an `Authorization`
added by `Fetch.continueRequest` on an https request with a valid
certificate, unchanged, and refuses `Proxy-Authorization` at the call, with
the same message as over http. The connection here was HTTP/2 where the
loopback echoes spoke HTTP/1.1, so the header also survives HTTP/2's
lower-cased header encoding. M3's answer, and §4.9's consequence (the gate
uses `Authorization`), stand as recorded above.

The control could have come out the other way. The control run went to the
same URL, in the same browser, with the same handler, and the echo showed no
`authorization`, so the header in the second run is the one the harness
added, not one the browser or the echo supplies. Its value matched the
run's own value exactly.

**The harness.** `lib.py` is the harness in [The harness](#the-harness)
below, from `import base64` up to `# ---- the echoes`, followed by its
section from `# ---- CDP over a stdlib websocket` up to
`# ---- one measurement`: the imports, the `CDP` class and `browser_ws()`,
unchanged. The run recorded above is `/opt/venv/bin/python3 https_harness.py
> out.json`, exit 0, with `lib.py` beside it. Its output, every header value
masked, is the lane's artifact `qi-measure-https/out.json`. The block
below, extracted from this file and run again at 02:19:54Z, gave the same
result in every row.

```python
"""M3 over https: the doc's raw-CDP harness, against a public echo with a real certificate.

Reuses the CDP class and browser_ws() from docs/design/qa-identity-measurements.md
(lib.py is extracted verbatim from that file's harness). Each run opens a new tab,
attaches flattened, Fetch.enable '*' at the Request stage, and answers the
top-level request's pause with Fetch.continueRequest carrying the paused
request's headers plus the one added header. What the echo received is read
from the page's own JSON body (Runtime.evaluate on document.body.innerText).
Every run has its own value, made at run time; it is not a credential.
"""
exec(open(__file__.rsplit("/", 1)[0] + "/lib.py").read())  # the doc harness's imports, CDP, browser_ws()

TARGETS = ["https://postman-echo.com/headers", "https://httpbin.org/headers"]


def run(cdp, url, header=None):
    value = "qa-measure-" + secrets.token_hex(8)
    target = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    session = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    paused, continued, outcome, tls = [], {}, None, None
    try:
        cdp.call("Page.enable", session=session)
        cdp.call("Runtime.enable", session=session)
        cdp.call("Network.enable", session=session)
        cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                 session=session)
        nav_id = cdp.post("Page.navigate", {"url": url}, session)
        deadline = time.time() + 30
        while outcome is None and time.time() < deadline:
            try:
                msg = cdp.next_message(deadline)
            except socket.timeout:
                break
            if msg.get("id") in continued:
                entry, request_id = continued.pop(msg["id"])
                if "error" in msg:
                    entry["continue_refused"] = msg["error"].get("message")
                    retry = cdp.post("Fetch.continueRequest", {"requestId": request_id}, session)
                    continued[retry] = ({"url": entry["url"], "action": "retry unmodified"}, request_id)
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
            if msg["method"] == "Network.responseReceived" and msg["params"]["response"]["url"] == url:
                r = msg["params"]["response"]
                sd = r.get("securityDetails") or {}
                tls = {"status": r.get("status"), "protocol": r.get("protocol"),
                       "security_state": r.get("securityState"),
                       "remote": f"{r.get('remoteIPAddress')}:{r.get('remotePort')}",
                       "tls_protocol": sd.get("protocol"), "subject": sd.get("subjectName"),
                       "issuer": sd.get("issuer"),
                       "valid_to": time.strftime("%Y-%m-%d", time.gmtime(sd["validTo"])) if sd.get("validTo") else None}
            elif msg["method"] == "Page.loadEventFired":
                outcome = "loaded"
            elif msg["method"] == "Fetch.requestPaused":
                ev = msg["params"]
                req = ev["request"]
                entry = {"url": req["url"], "redirect": "redirectedRequestId" in ev,
                         "header_names_at_pause": sorted(k.lower() for k in req["headers"]),
                         "action": "continued unmodified"}
                params = {"requestId": ev["requestId"]}
                if header and req["url"] == url and "redirectedRequestId" not in ev:
                    params["headers"] = [{"name": k, "value": v} for k, v in req["headers"].items()]
                    params["headers"].append({"name": header, "value": "Bearer " + value})
                    entry["action"] = f"continued with {header} added"
                paused.append(entry)
                continued[cdp.post("Fetch.continueRequest", params, session)] = (entry, ev["requestId"])
        body = cdp.call("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''",
                                             "returnByValue": True}, session=session)
        text = body.get("result", {}).get("value", "")
        href = cdp.call("Runtime.evaluate", {"expression": "location.href", "returnByValue": True},
                        session=session).get("result", {}).get("value")
    finally:
        cdp.call("Target.closeTarget", {"targetId": target})
    try:
        got = {k.lower(): v for k, v in json.loads(text)["headers"].items()}
        # Names only: a value is shown as <value> when it is this run's value,
        # and as "..." otherwise (the echo's own cookies are not recorded).
        received = {k: ("<value>" if value in v else "...") for k, v in got.items()}
        parse = "ok"
    except (ValueError, KeyError, TypeError) as exc:
        got, received, parse = {}, {}, f"not JSON: {exc}; first 200 chars: {text[:200]!r}"
    result = {"get": url, "final_url": href, "header_added": header, "outcome": outcome, "response": tls,
              "fetch_paused": paused, "body_parse": parse,
              "echo_received_names": sorted(received), "echo_received": received}
    for name in ("authorization", "proxy-authorization"):
        result[f"echo_got_{name}"] = name in got
        result[f"echo_{name}_is_bearer_value"] = got.get(name) == "Bearer " + value
    return result


ws, how, proc = browser_ws()
cdp = CDP(ws)
try:
    version = cdp.call("Browser.getVersion")
    out = {"browser": how, "product": version.get("product"),
           "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "targets": {}}
    for url in TARGETS:
        t = {"control": run(cdp, url)}
        out["targets"][url] = t
        if t["control"]["outcome"] != "loaded" or t["control"]["body_parse"] != "ok":
            continue
        t["authorization"] = run(cdp, url, "Authorization")
        t["proxy_authorization"] = run(cdp, url, "Proxy-Authorization")
        break
finally:
    cdp.sock.close()
json.dump(out, sys.stdout, indent=2)
```

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

## M1, M2, M4: measured by the operator (2026-10-20)

Run by the operator on the owner's approval. Each run used a temporary
`swarmQaJwtSigner` grant for the operator on the QA account alone, added for
the measurement and removed when it finished (06:12:33 to 06:14:12Z for the
second run). The account's own policy was then re-read: swarm-api alone holds
the role. The JWT was held in a 0700 temporary directory and never printed.
Each probe reports only the HTTP status, and whether IAP or the application
answered (`x-goog-iap-generated-response`).

### M1: IAP accepts a JWT signed as the QA account (PASSES)

| request, JWT `aud` `https://swarm.saga.xyz/*`, 15-minute `exp` | 02:26Z, account `swarm-qa`, no workspace yet | 06:12Z, account `qa-console`, workspace ready |
|---|---|---|
| `GET /`, `Proxy-Authorization: Bearer` | 200, the app | 200, the app |
| `GET /v1/tenants/me`, `Proxy-Authorization: Bearer` | 403, the app (no workspace) | 200, the app |
| `GET /v1/tenants/me`, `Authorization: Bearer` (the header the gate will use, M3) | 403, the app | 200, the app |
| the same JWT as a bearer to Cloud Resource Manager `projects.get` | 401 | 401 |
| control: a JWT with `aud` `https://other.example.com/*` | 401, IAP | 401, IAP |
| control: no header | 302, IAP sign-in | 302, IAP sign-in |

IAP admits a service-account JWT in either header, refuses another audience,
and the JWT is useless at a Google API.

### M4: the console resolves the QA account to its own workspace (PARTLY)

06:14Z, account `qa-console`, with its service workspace `w-267d01` ready
as tenant `u-qa-b260e6`. No `X-Swarm-Tenant` header was sent.

- `GET /v1/tenants/me` returns 200 with tenant `u-qa-b260e6`. A request that
  names no workspace acts in the QA workspace.
- `GET /v1/tenants/mine` returns 200 with an **empty list**. The route serves
  `AuthContext.tenant_choices`: a person's personal workspace and their
  registered groups. A service account's own workspace is the default
  (`Authenticator._with_own_service_tenant`) but is never listed. The
  console's workspace switcher would show the QA account nothing. QI-API
  decides how it is listed (this design, §4.9 M4).

### M2: not measured before the build (owner, 2026-10-20)

Nothing on swarm-api shows request headers. QI-API builds as if IAP passes the
header through, and logs `authorization_present` and
`proxy_authorization_present` (presence only, never the value). M2 is recorded
from that log after QI-API deploys.

### How the QA workspace was made (O3)

- Admin › Tenants › New workspace › Service for
  `qa-console@saga-agents-staging.iam.gserviceaccount.com`, 2026-10-20 04:31Z.
  It was approved at submission, because the operator acted as the platform
  owner's account.
- Setup did A1 to A8, then held at S2 with `FRONT_DOOR_OWNER`: the setup job
  could not read the API backend's IAP policy.
- After the owner applied the read-only `swarmFrontDoorReader` binding on that
  backend (PR 1687, applied 06:09Z), the retried run logged "front door:
  swarm-ui-backend already admits the account", passed A9, and the workspace
  was `ready` at 06:11Z.
- This contradicts the 2026-09-24 note in terraform/modules/frontend/main.tf,
  where `roles/iap.admin` bound on the backend still answered 403: a
  backend-level grant of `iap.webServices.getIamPolicy` works today.
