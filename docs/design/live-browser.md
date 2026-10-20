# Live browser: watch a browser agent work, and take control to sign in

**Status: design (2026-10-11). Nothing in it is built.** It answers the owner's
request of 2026-10-11: click a running browser agent in the console, see its
browser as it moves across pages, and **take control** -- chiefly so a person
can step in when the agent needs someone to sign in -- then hand control back
so the agent carries on. The owner decided four things in the same request
(section 2). The rest of this document is the design built on them, with the
options turned down kept on record, and with the open decisions written to
`questions.json` (section 10).

It builds on the `claude-code-browser` runner profile that lane PROFILE
(wf_f4fbad03) is adding in parallel: the claude-code agent on the
`agent-runtime-browser` image (Playwright and Chromium), GKE Autopilot only.
That profile is not on main at `124a1e3`, where every citation below was read.
Where this design needs something from it, section 8 says so.

Citations name a symbol as `path::qualname`. Lines move; symbols are what to
search for.

---

## 1. Why this is harder than a screencast

Showing a picture of a browser is easy. Three facts about this platform make
the requested feature, an **interactive** browser that a person types a
password into, a security design first and a streaming design second:

* **The agent and the worker are the same uid, in the same pod.** The worker
  says so where it reaps escaped processes before a publish
  (`apps/agent-worker/agent_worker/procman.py` (`The agent and the worker run as the`)), and the egress policy says the network namespace is shared too
  (`kubernetes/network-policies/allow-egress.yaml` (`WHAT RULE 2 COSTS`)).
  Anything the platform runs beside the agent -- an X server, a VNC server, a
  debugging port -- is reachable by the agent and by any process it escapes
  into, unless the design puts a real boundary between them. A localhost-only
  VNC server is **not** a boundary in a pod: localhost is the agent's too.
* **X11 is a keylogger's dream.** Any client that can connect to an X display
  can read every key pressed on it (XRecord, XInput2) and copy every pixel.
  If the agent can reach the display while a person types a password, the
  agent has the password. So the agent must not be able to reach the display at
  all, which is why Chromium cannot simply be launched by the agent's
  Playwright on a `DISPLAY` the agent can see (section 3.2).
* **The Chrome DevTools Protocol (CDP) tells its client everything.** The agent
  drives the browser over CDP. CDP delivers network events with POST bodies (a
  login form's password among them), lets a client read any input's value,
  and lets it install script on every page. An agent that keeps its CDP
  connection while a person signs in can read the password after the fact,
  even if it saw no frame. So the agent's CDP connection has to pass through a
  gate the platform owns, and that gate has to change behaviour while a person
  is in control (section 5.4).

And one fact about the product: **the person typing is trusting the agent's
browser with their own credentials, on a page the agent chose.** A prompt
injection that sends the agent to a look-alike sign-in page and then asks for
help is phishing with the platform's console as the delivery mechanism.
Section 5.7 is about that.

---

## 2. The owner's decisions (2026-10-11)

| Decision | What was decided | Where this document answers it |
|---|---|---|
| **LB-1** | An **interactive** remote browser: headed Chromium on a virtual display, VNC proxied through the platform into the console via noVNC, with a "take control" button. Not a view-only screencast. | §3, §4, §6 |
| **LB-2** | Frames are visible **only to members of the task's tenant**, checked on every connection (invariant 9). | §5.1, §5.2 |
| **LB-3** | Password and secret fields are **masked before anything leaves the pod**. | §5.3 |
| **LB-4** | Frames and recordings are kept **7 days**, under the tenant's GCS prefix. | §7 |
| **LB-5** | The console presentation is **a Browser tab on the agent page, with theater mode available from it** (owner's choice, 2026-10-11, from five in-console alternatives; recorded on #1030). This replaces the earlier phased plan of separate split, picture-in-picture and wall views. | §6 |
| **LB-6** | The browser can run **as a mobile device, not only desktop**, and a person in control works it **by touch**. Presets: Desktop 1440×900, iPhone 15 393×852, Pixel 8 412×915, iPhone SE 375×667, iPad (gen 7) 810×1080, with rotate. | §6.1 |
| **LB-7** | **Signed-in sessions are reusable, but only inside a person's own personal workspace** (2026-10-11, revising Q7). In a personal tenant (`u-<user>`), a session signed in once carries to that workflow's later browser steps and is destroyed when the workflow ends. The person can also save it as a **named browser login** of that workspace, and a task in that workspace opts in by name. **Group tenants (e.g. `eng`) never store or carry a session**: every browser step there signs in fresh. The person's own workspace identity enforces this by IAM, because every worker of a group tenant shares one identity and could otherwise read another member's session. Any account may be saved, at the person's judgement. | §5.5, §5.5.1 |

The brief also described a shape: "a VNC server bound to localhost only" and
"Chromium launched by the agent's Playwright on that display", with "swarm-api
proxying per-task websockets". This design keeps the intent of each and changes
the mechanism of the first two, for the reasons in section 1. Each change is
in section 3 with the option it replaced, and the one the owner may want to
overrule is a question (Q9, Q12).

---

## 3. Architecture

```
 GKE pod (tenant namespace swarm-tenant-<t>)                                   swarm-system            Cloud Run          browser
 +-------------------------------------------------------------+
 | container `worker` (uid swarm)  | native sidecar `display`  |
 |                                 | (uid swarm-display)       |
 |  worker (non-dumpable)          |  Xvfb :0 (unix socket,    |
 |   |  attempt key (heap)         |    sidecar-private /tmp)  |
 |   |                             |  Chromium (headed, on :0, |
 |   |  ctl.sock (MAC'd, first     |    profile on a private   |
 |   +---registration wins)------->|    memory emptyDir)       |
 |                                 |  live gate:               |
 |  agent (claude-code +           |   capture -> MASK -> encode|
 |  Playwright, connectOverCDP)    |   input injection (XTEST) |    outbound WS,  
 |   +--- 127.0.0.1:9222 --------->|   CDP proxy  <-pipe-> Chromium   ticket from      +-----------+      +-----------+     +---------+
 |                                 |   tunnel client ---------------- swarm-api ----->| swarm-live|<---->| swarm-api |<--->| console |
 |                                 |                           |    (ws, TCP 8080)    | relay     |      | (IAP, per-|     | noVNC   |
 +-------------------------------------------------------------+                      | rendezvous|      | connection|     +---------+
                                                                                      | + recorder|      | tenant    |
                                                                                      +-----------+      | check)    |
                                                                                                         +-----------+
```

### 3.1 In the pod: a display sidecar with its own uid

The `display` container is a Kubernetes **native sidecar** (an init container
with `restartPolicy: Always`) running the same `agent-runtime-browser` image
with a different entrypoint and a **different uid**. It holds three things:

1. **Xvfb** on display `:0`, listening on a unix socket in the sidecar's own
   `/tmp` only (`-nolisten tcp`), at a fixed 1280x800x24. The worker
   container does not mount that directory, so nothing in it, the agent
   included, can open the display.
2. **Chromium, headed, on that display**, launched by the gate with
   `--remote-debugging-pipe` (no debugging port at all), a user-data directory
   on a memory-backed `emptyDir` mounted into the sidecar only, and the
   enterprise policies of section 5.6. One window, maximised, with its address
   bar visible (no `--kiosk`, no `--app`): the address bar is part of the
   anti-phishing design.
3. **The live gate**, one process that: captures the framebuffer, masks it
   (section 5.3), encodes it as RFB, injects input when and only when a person
   holds control, exposes a **CDP proxy** on `127.0.0.1:9222` for the agent,
   and opens the outbound tunnel. Frames are produced inside the sidecar and
   leave it only through the tunnel, already masked. They never pass through
   the worker container.

**Why a sidecar and not processes in the worker container.** A separate uid is
the only boundary available inside one pod without gVisor or a second pod.
Same-uid processes can read each other's `/proc/<pid>/environ`, signal each
other, and open each other's sockets and files; a different uid in a different
container, with no shared mount for the display, cannot. The worker container
keeps its existing `securityContext`; the sidecar runs as a second non-root uid
(`runAsUser` distinct from the worker's), drops all capabilities, and mounts
only: its own `/tmp`, the browser profile volume, `/dev/shm`, and the two
shared volumes below.

**Why native and not an ordinary second container.** A Job's pod completes when
its containers exit. An ordinary second container that keeps running holds the
Job open after the worker exits, so the attempt would never end. A native
sidecar is terminated after the main container exits, and starts before it, so
the gate is up before the worker's lifecycle begins (and therefore before the
agent exists -- section 5.4 depends on that ordering).

**Shared volumes, and only these.** `downloads` (sidecar writes, worker reads:
Chromium saves downloads there and the worker moves them into `work/`) and
`uploads` (worker writes, sidecar reads: `setInputFiles` paths must exist on
Chromium's side), plus `/run/swarm-gate` holding `ctl.sock`. All three are
memory `emptyDir`s, so they are inside the pod's memory limit, and none is
under `work/`, so none is checkpointed.

### 3.2 Chromium is launched by the platform, and the agent connects to it

The brief said "Chromium launched by the agent's Playwright on that display".
The design launches Chromium from the gate and has the agent's Playwright
**connect** to it (`chromium.connectOverCDP("http://127.0.0.1:9222")`, or the
Playwright MCP server's `--cdp-endpoint`, whichever lane PROFILE settles on).
Four reasons, each a requirement elsewhere in this document:

* **The agent must not reach the display** (section 1). An agent that launches
  a headed Chromium needs `DISPLAY` and the X socket, which is exactly the
  keylogging channel.
* **The mask needs its own CDP session** to learn where the password fields
  are (section 5.3). The gate holds Chromium's pipe; the agent gets the proxy.
* **The pause needs a choke point** (section 5.4). With the proxy between the
  agent and Chromium, "the agent's actions are paused" is enforced, not asked.
* **The browser profile must live where the agent's files do not** (section
  5.5): a sidecar-only volume, never in `work/`, wiped at attempt end.

What the agent loses is the choice of browser flags. `claude-code-browser`
does not need them; the profile's agent instructions point Playwright at the
CDP endpoint. The worker sets no `DISPLAY` in the agent's environment
(`Workspace.child_env`,
`apps/agent-worker/agent_worker/workspace.py::Workspace.child_env`), so a headed launch fails
loudly rather than drawing somewhere nobody sees.

**The agent's own Chromium is refused, not merely unshown** (BROWSER-ENFORCE,
owner decision 2026-10-13, part of #1030). This section used to accept that a
headless Chromium the agent started itself was not shown and could not be
prevented, and task_7cda0d5335f049b4b442 showed what that costs: the agent launched one with
Playwright, did its work there, and the person watching the live browser saw
nothing. Now, whenever the live browser exists (`SWARM_CDP_ENDPOINT` is set),
the claude-code runner (`runners/claude_code.py::body`):

* points the **agent's** `PLAYWRIGHT_BROWSERS_PATH` at
  `/opt/swarm/live-browser-only`, a directory in which every Chromium
  Playwright would launch is the stub `images/agent-runtime-browser/live-browser-only`.
  `chromium.launch()` and `launch_persistent_context()`, headless or not,
  fail at once with *"This agent's browser is the live browser the person
  watches: connect with
  chromium.connect_over_cdp(os.environ['SWARM_CDP_ENDPOINT'])"*. Only the
  agent's variable changes: the image's ENV, which the gate
  (`livegate/__main__.py::find_chromium`) and the `browser` runner read, is
  still `/opt/playwright`, so the stub cannot reach the platform's own
  Chromium. A `sitecustomize` wrapping `BrowserType.launch` was the
  alternative; the stub directory was chosen because it also holds for
  Node's Playwright and for any interpreter, and needs no code in the
  agent's process. The image's build-time self-test (`selftest.py`) proves
  each launch form ends in that message;
* registers the Playwright MCP server (`@playwright/mcp`, pinned in the
  Dockerfile) through `--mcp-config` with `--cdp-endpoint
  $SWARM_CDP_ENDPOINT`, so `browser_navigate`, `browser_tabs` and the rest
  act on the live browser, and the tabs they open are the ones the console
  shows. It connects; it launches nothing;
* tells the agent so, and names `from swarm_browser import live`, which
  returns `(playwright, browser, context)` connected over CDP to the
  browser's existing context.

On the browser image the CLI's PATH also puts `/opt/venv/bin` first, so the
agent's `python` is the venv that has Playwright and `swarm_browser`.

What this does **not** stop: an agent that names a binary itself
(`executable_path=` pointing into `/opt/playwright`) still gets a hidden
browser. The stub turns the default path, the one an agent reaches for, into
an instruction; it is not a sandbox, and the pod remains the boundary.

### 3.2.1 dev-browser (opt-in)

Owner decision 2026-10-14 (DEVB-INSTALL, part of #1030): the browser image
carries [dev-browser](https://github.com/SawyerHood/dev-browser) v1
(`1.0.0-rc.3`, npm dist-tag `next`) as an **opt-in** CLI beside the
Playwright MCP server, which stays registered and stays the default. It is for
scripted multi-step browsing: a Puppeteer script per call, named pages that
outlive the call. Whether it ever becomes the default is for a benchmark to
decide, not this lane.

**Attach only, never launch.** dev-browser's normal mode starts its own
Chrome, found on PATH, in `~/.cache/ms-playwright`, in its own Chrome for
Testing download (`dev-browser install`) or at `DEV_BROWSER_CHROME`, and it
can start it headless. Any of those is the hidden browser
task_7cda0d5335f049b4b442 used, which the person watching never saw. The
stub directory above does not cover it: dev-browser never reads
`PLAYWRIGHT_BROWSERS_PATH` (the string is not in the binary; checked
2026-10-14). So it is used only in ATTACH mode, `--connect
$SWARM_CDP_ENDPOINT`, which reaches the same browser the console shows,
through the gate. Its own launch is closed off in two places:

* `dev-browser` on PATH is a wrapper
  (`images/agent-runtime-browser/dev-browser`). It passes `--connect
  "$SWARM_CDP_ENDPOINT"` on every call. It refuses `install`, `chrome`,
  `--headless` and any `--connect` other than the live browser's: a bare
  `--connect` probes 127.0.0.1:9222-9229, which could reach Chromium's own
  port behind the gate. It also refuses every call when the agent's
  `PLAYWRIGHT_BROWSERS_PATH` is the stub directory but the endpoint is gone.
  The real binary is at `/opt/swarm/dev-browser/dev-browser-bin`, off PATH.
* `DEV_BROWSER_CHROME`, which dev-browser reads before any other lookup, is
  a copy of the `live-browser-only` stub. The claude-code runner sets it for
  the agent's whole environment, so even the real binary, run by its path,
  fails a launch with the stub's message.

**Why a wrapper, and what it is not.** The wrapper is guidance,
not a sandbox. It turns the paths an agent reaches for into an instruction that
names the live browser. An agent can still run the real binary with a
`--connect` of its choosing. As this section already says of
`executable_path=`, the pod is the boundary, not the wrapper.

**The runner** (`runners/claude_code.py::body`), only when
`SWARM_CDP_ENDPOINT` is set:

* puts `DEV_BROWSER_HOME` at `work/.dev-browser`, so the daemon's socket and
  the named-page map travel with the workspace across a park. The checkpoint
  skips the socket and keeps the map;
* adds one paragraph to the note: dev-browser is available, it drives the
  same live browser, and after every `swarm-handoff wait` the agent runs
  `dev-browser stop` before its next script. On an attached browser `stop`
  only disconnects, and the next call re-attaches its named pages by target
  id, as the person left them.

**What it costs.** The binary is the upstream release's standalone Bun
build, with puppeteer-core 25.8.0 inside. It needs libc only, so no Node is
added. It is 96,442,496 bytes (92.0 MiB) uncompressed and 36,429,474 bytes
(34.7 MiB) gzip-compressed, which is about what the new layer adds to a pull.
That figure was measured on the file, not on a pushed image: CI's image-size
report for this change is the number to trust. The build reads it from the
tool mirror at its pinned sha256, like the base's release files
(docs/runbooks/build-mirror.md).

**Not yet working through the gate.** Until the gate's CDP policy lane
lands, the gate refuses `Performance.enable` and `Audits.enable`, which
Puppeteer sends when it opens a page. So dev-browser cannot open a page
through the gate yet. That policy change is a separate lane and is not part
of this one.

### 3.2.2 Judgment backends (JUDGE-SELECT)

jev-browser (Jev, on the claude-code-browser image, attached to this live
browser) asks typed questions about page state -- "is this a login page", "which
of these buttons submits" -- and sends the page text it judges to a **judgment
backend**. Owner decisions, 2026-10-14:

| backend | where page text goes | needs |
|---|---|---|
| **Internal Jev** (`internal-jev`, the DEFAULT) | the internal decision API, `/jev/v1/decide`, on the company tailnet | the cluster's tailnet egress |
| **Internal Laya** (`internal-laya`) | the same API's `/laya/v1/decide` | the cluster's tailnet egress |
| **TypeSafe** (`typesafe`) | api.typesafe.ai -- outside your network | a TypeSafe key ([provider-keys.md](../provider-keys.md)) |

**Why internal is the default.** It keeps page text -- which can be a signed-in
person's own data -- inside your network, and it needs no TypeSafe key, so a workspace
can use Jev before anyone has set one. TypeSafe stays a choice, and the console
says what it costs: "sends page text to TypeSafe; needs a key".

**How internal is reached.** Neither the laptop nor GKE was on the tailnet. The
owner chose Tailscale egress in our cluster (JUDGE-NET): the Tailscale
Kubernetes operator in `swarm-system` with ONE egress Service to
`decision-api-staging`, its OAuth client (`tag:swarmcloud`, ACL limited to
`decision-api-staging:443`) in Secret Manager as `swarm-tailscale-oauth`, stored
by the owner and never in the repository, a tfvars file, an environment or a log.
A pod reaches it **by the decision API's tailnet name**,
`https://decision-api-staging.taile0bda9.ts.net`, which its `hostAliases`
entry pins to the egress Service's fixed clusterIP (`10.48.6.120`, stated once
as `terraform/infra/tailscale.tf` `local.decision_api_cluster_ip`). The Service
passes raw TLS through to the tailnet, so the pod verifies the API's real
certificate end to end (owner decision 2026-10-15, #1380). The in-cluster name
it replaced, `http://decision-api.swarm-system.svc.cluster.local`, never
worked: the Service has no port 80, and over https the certificate is the
tailnet name's. `terraform/infra` `internal_decision_api_enabled` (off by
default) renders the tailnet origin as `INTERNAL_DECISION_API_URL` onto
swarm-api and the scheduler, which accept no other value.

**The setting** is per workspace: `workspace_settings/{tenant}.judgment_backend`,
read and written with `GET`/`PUT /v1/workspaces/{id}/judgment-backend` by the
workspace's owner (a personal workspace's principal) or a platform admin, every
change an `admin_audit` entry (`swarm_api/judgment.py`). It is a document of its
own because the frozen `Tenant` has no field for it and no per-workspace
settings store existed. `GET /v1/judgment`, and `jev` on
`GET /v1/provider-keys/typesafe`, give the chosen backend, its label ("Jev
(Internal)", "Jev (TypeSafe)") and its availability for the Submit picker and
the browser strip; Settings › API keys has the radio.

**What a pod gets** (`scheduler/judgment.py`, live-browser pods only):
`SWARM_JEV_BACKEND`, always; for an internal backend with the egress on,
`JEV_API_URL` (`https://decision-api-staging.taile0bda9.ts.net/jev/v1/decide`
or `/laya/v1/decide`), the pod label `swarm-judge: internal`, which JUDGE-NET's
NetworkPolicy keys the decision API's egress on, and the `hostAliases` entry
that resolves that name to the egress Service -- the three together or none. The worker passes both variables to the CLI
(`handoff.AGENT_PASSTHROUGH_ENV`), whose MCP servers inherit them.

**No fallback, in either direction.** An internal choice with the egress off is
*unavailable*, not TypeSafe: the pod gets no URL, and `jev-browser-mcp` refuses
to start rather than let jev-browser use its TypeSafe default. The wrapper
passes `JEV_API_URL` through only when it is exactly the decision API's
tailnet path for the chosen backend, and sets `JEV_API_COMPAT=internal` with it, so the
patched jev-browser reshapes each request for the internal API (§3.2.3). For an
internal backend it sends **no TypeSafe key**: upstream jev-browser 0.2.0
refuses to start without `TYPESAFE_API_KEY` and sends it as a Bearer token, so
the wrapper replaces it with a fixed marker that is not a key, and the adapter
sends its own placeholder in its place on every request. A stored value the code does not know, or a settings read that fails, is
read as the default -- the backend that sends nothing outside your network.

**The code-context work reuses this setting.** The context-quality design
(CTXQ-DESIGN) judges code context with the same decision API; it reads this
same per-workspace `judgment_backend` and its availability rather than adding a
second switch, so a workspace that chose to keep page text inside your network keeps
its code there too.

**Not verified here.** No unit test can reach the tailnet. The decision API
does not accept jev-browser's body as upstream sends it; the operator's
measurement and the adapter that reshapes it are §3.2.3. That the reshaped body
is accepted end to end from a pod is unmeasured until JUDGE-NET's egress is up.

### 3.2.3 jev-browser: the internal request adapter (JUDGE-ADAPT)

jev-browser (pinned 0.2.0, patched to attach over CDP: JEV-IMAGE) is an MCP
server where a model, "Jev", picks each step of a browsing task. It asks only
two kinds of question: `noul` (a yes/no probability) and `choice` (one option
of several). On 2026-10-14 the owner made the backend selectable: **Internal
Jev** (the default), **Internal Laya** or **TypeSafe**. The network path and
the setting belong to lanes JUDGE-NET and JUDGE-SELECT. This section is about
the request shape (JUDGE-ADAPT).

Upstream jev-browser speaks TypeSafe's API. The internal decision API
(`decision-api-staging` on the tailnet, `/jev` or `/laya` +
`/v1/decide`) answers in the same shape (`choice`, `probabilities`,
`confidence`, `noul`), but it is **not wire-compatible on the request side**.
The operator measured it over Tailscale from the laptop on 2026-10-14:

| check | `/jev` | `/laya` |
|---|---|---|
| `noul`, string `state` | 200, 0.3-0.4 s | 200, 1.7-5 s |
| `state` as an object (what jev-browser sends) | 400 `state: Input should be a valid string` | 400 |
| `choice` with criteria | 200, 0.34 s, TypeSafe's shape, correct pick, confidence 0.98 | 200, 2.9 s, same shape, confidence 0.04 on the same question |
| `choice` without criteria (jev-browser's element and group picks) | 400 `criteria Field required` | same validator expected |
| `score`, criteria as a dict | 400 `criteria Input should be a valid list` | 500 `predict failed` |

The internal API ignores `model` and `Authorization`. jev-browser, however,
refuses to make any call without a non-empty `TYPESAFE_API_KEY`.

**The adapter.** The image's patch adds `src/internal-compat.mjs` to the
package, plus two lines in `src/jev.mjs` that pass every request through it.
That one function is where every request leaves, so this is the smallest
change. Wrapping `fetch` from the MCP wrapper would mean preloading a module
into node that rewrites every HTTP call the process makes. With
`JEV_API_COMPAT` unset, the request is returned untouched, so TypeSafe sees
exactly what upstream sends. With `JEV_API_COMPAT=internal`:

* an object `state` is sent as JSON with sorted keys and no whitespace, so the
  same page always makes the same request;
* a `choice` option without a description is described from the page the
  question names: `page.elements` by `i` as `"<role> <name>"`, cut at 80
  characters, or `page.groups` by `g` as the group's summary. jev-browser's
  picks send only the keys (`{"7": null}`). A described option keeps its
  description. A keys-only choice that names neither (dropdown options,
  keyboard keys) is described by its own key, which is the option's whole
  text. A choice with no criteria at all that names neither, or an option that
  is not on the page, **fails that call with its reason**. Nothing is guessed;
* a `score` with dict criteria is refused, because the list's element shape
  was never measured and jev-browser asks no `score`;
* the key sent is the fixed, non-secret placeholder
  `internal-backend-takes-no-key`. `TYPESAFE_API_KEY` is never sent to an
  internal backend, even when it is set;
* `answers` are passed through unchanged;
* `internal` together with TypeSafe's URL is refused. The reshaped body and
  the placeholder never go to TypeSafe.

**Who sets it.** `JEV_API_COMPAT=internal` must be set by whatever sets
`JEV_API_URL` to an internal backend. That is the wrapper
(`bin/jev-browser-mcp`): for `internal-jev` or `internal-laya` (§3.2.2) it
passes the egress Service's URL through and sets `JEV_API_COMPAT=internal`
beside it. For TypeSafe it drops `JEV_API_URL` and passes `JEV_API_COMPAT`
through as it came, so a stray `internal` fails loudly instead of reaching
TypeSafe.

**Why Internal Jev is the default.** On one element-pick probe, Laya was 8x
slower than Jev (2.9 s against 0.34 s) and far less confident (0.04 against
0.98). That is one probe, not a benchmark, but it supports the owner's choice
of Internal Jev as the default.

**What proves it.** `tests/unit/worker/test_jev_internal_compat.py` takes the
module out of the patch and runs each transform with node on the exact shapes
the internal API refused. The image's self-test (`selftest.py` `jev_compat`,
through `jev-browser/compat-probe.mjs`) runs the installed package's own
`decide()` and `resolve()` over `jev-browser/fixtures/internal-decide.json`.
That fixture holds the internal API's answers in the shape and with the values
the measurement recorded. It is not a byte capture: the measurement kept shapes
and values, not bodies. The self-test checks that a one-stage and a two-stage
pick both come back as numeric element indexes, that the TypeSafe path is
unchanged, and that `internal` with no key does not throw.

### 3.2.4 Choosing and seeing the browser tool (BSTATUS-API)

Owner decision 2026-10-14 (BSTATUS-API, part of #1030): "Picker on Submit,
Auto by default". A `claude-code-browser` task names its tool in
`input.browser_tool`, one of four names the frozen catalogue declares
(contract request 77): `auto` (the default), `playwright`, `dev-browser` or
`jev`. A NAME, never a command, an MCP config or a URL (invariant 10): the
runner and the image's wrappers own what each name runs.

**What each name registers** (`agent_worker/browser_tools.py`, `choose`):

| `browser_tool` | registered | note names first |
|---|---|---|
| `auto` | Playwright MCP, dev-browser, and jev when it can run | every tool, each with its strength |
| `playwright` | Playwright MCP only | Playwright |
| `dev-browser` | dev-browser first, Playwright MCP as fallback | dev-browser |
| `jev` | jev MCP first, Playwright MCP as fallback | jev |

Playwright MCP is registered on every choice because it is the one tool
that has worked through the gate since L2: a choice must never leave the
agent with no working tool. Jev runs only when the image carries its
wrapper and the runner has a TypeSafe key; otherwise the attempt runs
without it and says why (`no_key`, `not_installed`). It does not fail: a
key deleted between submission and run must not cost the attempt.

**The API refuses what cannot run.** A `jev` submission with no TypeSafe key
for its submitter or its tenant (the resolution `GET
/v1/provider-keys/typesafe` serves), or on a deployment whose image has no
jev (`BROWSER_JEV_INSTALLED=false`), is refused 422
`browser_tool_unavailable`. It is a new refusal, so it ships switched off
(`REFUSAL_BROWSER_TOOL_UNAVAILABLE`): until the Submit form disables Jev when
no key resolves, the refusal is logged and the task runs on Playwright.

**How the console learns which tool is in use.** Three parties each know
one part, and none can see the others' part:

* the RUNNER knows what it registered. It writes `browser.json` to the
  hand-off spool before the CLI starts;
* the WORKER's hand-off poll forwards that file to the gate over ctl.sock
  (`browser_tools`), checked again, because the spool is in the agent's
  reach. The spool's copy is removed at every attempt's start, so a restored
  checkpoint never speaks for a new attempt;
* the GATE knows which tool drove the browser last. Each tool connects with
  a CLIENT TAG on the endpoint, `http://127.0.0.1:9222/client/<tool>`: the
  runner gives Playwright MCP its tag in `--cdp-endpoint`, and the dev-browser
  and jev wrappers add theirs. The tag is a path segment, not a query,
  because a client that builds `/json/version` by string concatenation turns
  `?client=x` into `?client=x/json/version/`. The CDP proxy reads the tag,
  echoes it into the websocket URL it hands back, and STRIPS it: nothing of
  it reaches Chromium. An untagged connection, such as `swarm_browser.live()`
  from the agent's Python, works as before and is attributed to no tool.

The gate puts it all in its report as `browser`: the registration, the last
tool and when, and the egress. The relay passes the key on, and swarm-api
checks it key by key (`clean_browser`): an unknown key anywhere in it drops
the whole of it.

**The egress state** is the forwarder's (§ "browser egress",
docs/browser-egress.md): `residential` or `direct`; `ok`, `failing` or `off`;
and `last_error`, one of the gate's FIXED sentences for the forwarder's
cause word. The forwarder never reads a vendor's answer past its status and
a cause word, so no vendor's text can reach the console. A refusal of one
request (a destination the policy refuses, a rate) does not make the path
`failing`; a refusal of the path (the vendor unreachable, a budget used up)
does, until the next connection is served. The exit country is not the
gate's to know: swarm-api adds it from `BROWSER_EGRESS_COUNTRY`, the setting
that mints the ticket.

**What the console reads** is `browser` on `GET
/v1/tasks/{id}/live/session`, beside `live_browser`, served for a live-browser
task before, during and after its session:

```json
{"tool_selected": "auto",
 "tools_available": ["playwright", "dev-browser", "jev"],
 "tool_last_used": {"name": "dev-browser", "at": "2026-10-14T15:02:11+00:00"},
 "jev": {"enabled": true, "reason": null, "key_scope": "user"},
 "egress": {"mode": "residential", "country": "us", "state": "ok", "last_error": null}}
```

`tool_selected` is the task's own input, so it is there from submission.
`tools_available` is empty and `egress` null until the current attempt's gate
reports them: never invented. `jev.key_scope` says whose TypeSafe key jev runs
on while it is registered, and is null otherwise.

**Not verified here.** That dev-browser's `--connect` and jev's
`connectOverCDP` keep the path of a tagged endpoint was not run against the
real binaries, nor was Playwright MCP's: recent Playwright appends
`json/version/` to the endpoint URL's path, which keeps the tag, but that was
not checked against the pinned 1.63 here. A tool that drops the path still
connects, untagged, and is attributed to nothing; the proxy also reads a
`client=` query, for a client that keeps a query and not a path.

### 3.2.5 jev-browser: which backend, which key, how the key arrives (JEV-RUNNER)

Owner decision 2026-10-14 (JEV-RUNNER, part of #1030): a live-browser task
also gets [jev-browser](https://github.com/ying-kai-liao/jev-browser)'s MCP
tools, beside Playwright MCP and dev-browser, **according to the workspace's
judgment backend (§3.2.2, JUDGE-SELECT, PR 1312) and the task's `browser_tool` (§3.2.4) and, for TypeSafe, when its
submitter or its tenant has a TypeSafe key**. jev-browser 0.2.0 (MIT) is a Node MCP server over
Playwright. Its `browser_do(goal, values)` and `browser_read(question)` hand
a goal-driven step, or a question about a long page, to TypeSafe's Jev
models. That means it **sends the page's text and labels to TypeSafe
(`api.typesafe.ai`)**. The owner accepted that. A TypeSafe-backend task with no key never
loads it, and the prompt says nothing about it. jev-ultrafast stays out.

**The gate is two conditions**, both required: (a) the task's `browser_tool`
(§3.2.4) is `jev` or `auto`, never `playwright` or `dev-browser`; and (b) the
workspace's backend allows it. Condition (b) is the backend, not the key (`SWARM_JEV_BACKEND`, set by the
scheduler's `judgment.py` and passed to the worker and the runner):

| `SWARM_JEV_BACKEND` (with `browser_tool` jev or auto) | key | `jev` registered | key file |
|---|---|---|---|
| `internal-jev` or `internal-laya` | not needed | yes | never written |
| `typesafe` | resolved | yes | yes (below) |
| `typesafe` | none | no | none |
| unset (an old pod) | resolved | yes | yes |
| unset (an old pod) | none | no | none |

An internal backend starts `jev-browser-mcp` directly: no `jev_launch.py`,
no `TYPESAFE_API_KEY`, and `jevkey.prepare` returns before it reads a
secret. The wrapper refuses by itself when the egress URL is absent, and the
runner does not repeat that check. Its note leaves out the sentence about
TypeSafe, because nothing goes there. The key path below applies to the
`typesafe` backend only; the per-key paragraphs that follow describe it.

**Why per key, and which key** (the `typesafe` backend). jev-browser does nothing without
`TYPESAFE_API_KEY`, and the keys are the ones docs/provider-keys.md stores.
The worker resolves the key with swarm-api's rule (`resolve_provider_key`): the
submitter's own key, `swarm-tenant-u-<user>-typesafe`, wins, else the task
tenant's, else none. "The submitter's own" means their confirmed personal
tenant, whose document names their email. A namesake on another domain
derives the same `u-` id, so the id alone is not enough.
`agent_worker/jevkey.py` restates the order, and
`tests/unit/worker/test_jev_runner.py` holds it to swarm-api's function over
every combination. The worker reads the secret as its own service account,
the same way it reads every tenant credential. Nothing asks swarm-api for the
value, because no route serves a credential.

**A personal key on a group task needs a grant.** A task bogdan submits in
`eng` runs as eng's worker account. swarm-api's write granted
`swarm-tenant-u-bogdan-typesafe` to u-bogdan's worker only, so eng's worker
gets PERMISSION_DENIED. It logs `jev_key_unreadable` (the secret id and the
error type) and uses eng's key if eng has one.
`terraform/bootstrap/typesafe_user_keys.tf` grants one group's worker read
on one person's key, one row at a time (`typesafe_user_key_readers`; the
owner applies it). **A row lets every task in that group read that person's
key**, because every member runs as the same account in the same container.
It is the same trade as the GitHub user slots (#780 D7 U1), so a person is
listed only by consent.

**How the key reaches only the jev server.** This is the narrowest path that
works:

1. The worker process (`lifecycle._build_child_env` → `jevkey.prepare`), only
   when `SWARM_CDP_ENDPOINT` is set, writes the key to
   `<attempt>/private/jev-typesafe.key`: mode 0600, created afresh
   (`O_EXCL`, `O_NOFOLLOW`), and registered for log redaction before
   anything else. `private/` is never checkpointed and never uploaded
   (workspace.py). The runner is given the file's **path**,
   `SWARM_JEV_KEY_FILE`, and never the key.
2. The claude-code runner registers an MCP server `jev` whose command is
   `jev_launch.py` (in the runner package, run by absolute path with `-I`)
   with the key file and `/opt/swarm/jev-browser/bin/jev-browser-mcp` as
   arguments. The MCP config therefore holds a path, not the key. The CLI's
   environment, and so the agent's Bash tool, never gets the key or the
   variable.
3. `jev_launch.py` reads the file and execs the server with
   `TYPESAFE_API_KEY` set in that one process's environment. It also sets
   `JEV_BROWSER_NO_INSTALL=1` (from the config's `env`), so jev-browser never
   downloads a Chromium of its own. It does **not** delete the file. The CLI
   is launched more than once in one attempt (a pending-work resume, a repair
   turn, an account move), and each launch starts its MCP servers again. A
   file deleted on first read would leave every later turn without jev. The
   runner deletes it when the agent has finished, and it is gone with the
   workspace anyway.

**Claude Code's `${VAR}` expansion in MCP configs was not used.** It expands
from the CLI's own environment, so the key would have to sit in the
environment every Bash command inherits. Writing the key into the config's
`env` would put it in `work/`, which is checkpointed to GCS.

**What this does not stop.** The agent runs as the same uid in the same
container. It can read the MCP config, find the path and read the file
while the attempt runs. That is the same trust level as every in-pod
credential, including the provider key already in the CLI's environment.
The pod is the boundary, as §3.2 says of `executable_path=`. What this path
does guarantee is that the key is in no log, no checkpoint, no artifact, no
environment dump of the agent's shell, and no file under `work/`.

**Attach, not launch.** jev-browser 0.2.0 only launches a browser of its own
(`chromium.launch` / `launchPersistentContext`, a new context at 1280×800).
It has no CDP-connect option, and with the agent's `PLAYWRIGHT_BROWSERS_PATH`
on the stub (§3.2) that launch fails. The image lane (JEV-IMAGE) installs
`/opt/swarm/jev-browser/bin/jev-browser-mcp` as the variant that attaches to
`$SWARM_CDP_ENDPOINT` with `viewport: null` and the browser's **existing**
default context. The gate refuses `Emulation.setDeviceMetricsOverride` and
the cookie methods, so `newContext` cannot be used. The runner passes the
endpoint in the server's `env`. If the binary is absent from the image, the
runner skips the registration and logs `jev_unavailable`, and the attempt
runs with Playwright MCP and dev-browser.

**The note** is §3.2.4's: the selected tool first, then each registered
tool's strength line. The `jev` line says it sends page text to TypeSafe,
which is true only on the `typesafe` backend (`BROWSER_TOOL_STRENGTH`); on an
internal backend the runner uses `BROWSER_TOOL_STRENGTH_JEV_INTERNAL`, which
says nothing is sent to TypeSafe.

**The benchmark** (scripts/browser-bench.sh, docs/browser-bench.md) has a
third arm, `jev`, chosen by its own sentence in front of the prompt like the
other two. It is opt-in (`--tools playwright-mcp,dev-browser,jev`) because
its tools exist only for a keyed submitter and each run spends TypeSafe
credits too. Each challenger gets its own verdict against Playwright MCP.

swarm-api's `browser_tool_unavailable` refusal of `browser_tool: jev` for a
missing key applies only when the workspace's backend is `typesafe`; Internal
Jev and Internal Laya need no key, so they are never refused for its absence.

### 3.3 The path to the console: outbound only, through a relay

No inbound port on the pod and no public endpoint: the gate **dials out**.

```
gate --(WS, TCP 8080, in-cluster)--> swarm-live <--(WS, TCP 8080, internal LB)-- swarm-api <--(WSS via IAP LB)-- console
```

1. **The worker asks swarm-api for a tunnel ticket** (`POST
   /v1/attempts/{attempt_id}/live-session`), signed with the attempt key
   (section 5.4.1). swarm-api checks the fence -- the task is `RUNNING` under
   this lease and generation -- that the task's profile is a live-browser
   profile, and that the tenant is the task's, then writes
   `live_sessions/{session_id}` and returns a one-use ticket, an HMAC under
   `swarm-live-key` (held by `swarm-api` and `swarm-live` only, value created by
   `scripts/create-secrets.sh`, never a Terraform secret version) over
   `(session_id, tenant_id, task_id, attempt_id, generation, role=tunnel, exp,
   nonce)`, valid 60 seconds. The exact format is the relay's
   (`apps/swarm-live/swarm_live/rendezvous.py::mint_ticket`), restated by
   `apps/swarm-api/swarm_api/live.py::mint_ticket`;
   `tests/unit/control_plane/test_live_relay_wire.py` feeds what swarm-api
   mints to the real relay.
2. **The worker hands the ticket to the gate** over `ctl.sock` (section 5.4.2),
   and the gate opens the tunnel to `swarm-live` with the ticket as its first
   message. The ticket never sits in an environment variable, a file or
   `work/`.
3. **The console never talks to the relay directly.** It asks for a viewer
   ticket (`POST /v1/tasks/{task_id}/live/ticket`, in the response body) and
   opens `wss://<console host>/v1/live/{session_id}/stream` through the
   existing IAP load balancer (the same origin the console already uses for
   `/v1`; `apps/swarm-ui/README.md`: "IAP sits in front"), sending the ticket
   as its first message. swarm-api authenticates the connection exactly as
   every route does (`Authenticator.authenticate`,
   `apps/swarm-api/swarm_api/auth.py::Authenticator.authenticate`), checks the
   tenant, the task, the role (section 5.1) and the generation, and proxies the
   websocket to `swarm-live` as plain ws over the internal load balancer
   (`SWARM_LIVE_URL`, `ws://<reserved internal address>:8080`, + `/v1/view`),
   presenting the viewer ticket as the relay's first message.

#### The agent's browser does not wait on the view (NOLIVE-GUARD, #1366)

The tunnel is the **human's** view. The agent's browser is the gate's CDP
proxy on the pod's loopback, up before ctl.sock is served, and it needs
nothing from the relay. Until 2026-10-15 the worker treated the two as one
"live path": measured on benchmark devb-1015, 8 of 60 claude-code-browser
attempts logged `tunnel_refused {"cause": "relay unreachable: gaierror"}`
about 4 s after `worker registered`, then "no live path for this attempt",
and their agents started with no `SWARM_CDP_ENDPOINT` and no Playwright MCP
entry -- half fell back to the image's headless Python Playwright. So
(`agent_worker.handoff.HandoffPath`):

* **The browser follows the gate.** Once the gate took the worker's
  registration (`browser_ready`), the agent gets `SWARM_CDP_ENDPOINT` and the
  runner wires Playwright MCP / dev-browser on it, whatever happened to the
  tunnel, the attempt key or the heap check. Those decide only the view and
  the hand-off.
* **A tunnel that failed costs the view, and says so.** The attempt's events
  carry `cause: live_browser_unavailable`, `phase: live_view`, `message: "live
  view unavailable: <code>; ..."`; the console's tile reads "live view
  unavailable". A hand-off is still offered: its own poll retries the tunnel,
  which is exactly when a person needs it, and a hold nobody could open ends
  as any unwatched hold does.
* **The tunnel is retried for the attempt's life.** A refused ticket
  (`tunnel_refused`, the gate's relay did not answer) or a swarm-api error is
  tried again past a backoff of 5 s doubling to 60 s, jittered to 75-125 % so
  a burst of pods that met the same DNS failure does not knock again in the
  same instant. Each retry logs `live view not open; retrying` with its code
  and count, never a ticket; the first open after failures logs `live view
  opened`. Only swarm-api's ended codes (`live_session_closed`,
  `live_fenced`, `live_not_available`) stop it.
* **A gate that is not ready never starts the agent.** No ctl.sock, or a
  registration refused (a restarted gate refuses every one), means no CDP
  proxy: `Worker._prepare` fails the ATTEMPT retryably with
  `live_gate_unavailable` before the agent runs -- the start-failure path an
  unreachable forge takes. Its lease and capacity are released at once
  (invariant 1), the task goes back to READY 30 s later while it has attempts
  left, and ends CANNOT_START when they are spent. Not a park: nothing about
  this pod's sidecar improves by waiting, only a new pod brings a new one, and
  a SCHEDULED_RETRY park is promoted by nothing.

**The DNS side, read from the manifests (2026-10-15).** The gate dials
`ws://swarm-live.swarm-system.svc.cluster.local:8080` (`livegate.tunnel.
DEFAULT_RELAY_URL`, or `SWARM_LIVE_URL`). Every GKE worker pod already runs
`dnsPolicy: None` with `searches: []`, `ndots:1` and NodeLocal DNSCache
(169.254.20.10) as its only nameserver (`scheduler.dispatch.
GKE_POD_DNS_CONFIG`, mirrored in `kubernetes/worker-templates/
worker-job-browser.yaml`), so that name is already queried once, absolute,
with no search-path expansion: a trailing dot or a lower `ndots` would change
nothing. The cluster is Cloud DNS (cluster scope) behind NodeLocal DNSCache.
The failures came only inside a burst of ~60 pod starts, and the worker in
the same pod had just resolved its own hosts; what is not yet measured is
whether node-local-dns answered SERVFAIL/timeout under that burst (EAI_AGAIN)
or the Service name was briefly absent (EAI_NONAME). The worker's retry
covers either. From this change the gate's record names which
(`tunnel_refused {"cause": "relay unreachable: gaierror(EAI_AGAIN)"}`,
`livegate.tunnel.unreachable_cause`); a manifest change waits for that
reading rather than guessing at one.

#### The relay's wire: owner decisions 2026-10-11

Lanes L7 (the relay), L8 (its infrastructure) and L9 (swarm-api's routes) were
built in parallel and disagreed: L7's relay read `live_sessions` from
Firestore, which L8 had granted it no access to; L7 served TLS on 8443 with a
mounted key file, while L8 rendered a Secret Manager version reference; and L7
and L9 minted different ticket shapes. The owner resolved it on 2026-10-11
(~14:35 PDT), and lane LB-L8d wired it:

* **(a) Relay auth: a signed ticket, closed by swarm-api.** The relay has **no
  Firestore access** and never reads `live_sessions`. swarm-api mints every
  ticket, MAC'd with `swarm-live-key`, carrying `tenant_id`, `task_id`,
  `attempt_id`, `generation`, `session_id`, `role` (`tunnel` or `viewer`),
  `exp` (at most 60 s ahead) and a `nonce`. The relay checks the MAC, the
  expiry and single use, and trusts only the ticket: a tunnel ticket defines
  the session, and every later ticket for it must name the same tenant, task,
  attempt and generation. When a session closes or its generation goes stale,
  swarm-api calls the relay's `POST /internal/close` with the session id,
  MAC'd with the same key under its own domain (so a ticket is never a close
  request), and the relay drops every connection of that session and refuses
  every ticket for it still in flight.
  *Why:* Firestore IAM cannot name a document or a collection -- the narrowest
  binding is the database (`terraform/infra/live.tf` records the measurement)
  -- so "the session document only" is not a grant that exists, and the
  faithful grant is none. Everything the relay must check already travels
  under the MAC. The cost is that the relay no longer notices a stale
  generation on its own: swarm-api must tell it, which is why the close route
  exists and why swarm-api calls it on every close it commits, on a fenced
  worker's request, on a new attempt's session, and on a socket that sees the
  session closed.
* **(b) Relay transport: proxied through swarm-api, plain ws inside the VPC.**
  Browsers never reach the relay. They open wss to swarm-api (the existing IAP
  and HTTPS) at `/v1/live/{session_id}/stream`, and swarm-api proxies that
  socket to the relay as plain ws over the internal load balancer. The relay
  listens on plain WS on **port 8080**, with no TLS and no certificate
  anywhere; a NetworkPolicy admits only swarm-api's egress range and the
  tenant gates. Port 8080 and plain ws are fixed for both integration lanes
  (L8d: code and swarm-api's environment; L8e: image and deploy).
  *Why:* one public surface and one authenticator (R2 below): the tenant check
  is not restated in a second service. A certificate on the relay would have
  to be issued, mounted and rotated for a hop that never leaves the VPC and
  whose every connection is already authenticated by a one-use ticket. The
  cost, stated plainly: what crosses swarm-api -> relay -> gate is not
  encrypted by this platform, so a person's keystrokes during control are in
  clear on those internal hops (section 5.11). TLS ends at the load balancer
  in front of swarm-api.
* **(c) Key: read from Secret Manager by version reference.** The relay reads
  `swarm-live-key` by the version reference in `SWARM_LIVE_KEY_SECRET` (L8's
  interface, rendered by `live.tf` into the manifest) with its own Workload
  Identity principal, the secret's only reader besides swarm-api. There is no
  `SWARM_LIVE_KEY_FILE`. swarm-api reads the same secret as `SWARM_LIVE_KEY`,
  by Cloud Run secret reference (`terraform/infra/locals.tf`
  `live_key_secret_env`), never as a value.
  *Why:* a mounted file needs a secret volume or CSI driver that nothing here
  provisions, and L8 had already built the reference and the grant; reading
  it once into memory keeps the key out of every file, environment and crash
  dump.

#### Why a relay at all, and why this shape

The meeting point cannot be swarm-api alone. swarm-api is a Cloud Run service
with `max_instances` above one (`terraform/infra/main.tf`, the `swarm-api`
service), a 300-second default request timeout
(`terraform/modules/cloud_run/variables.tf` (`request_timeout`)) and one CPU. The
gate's tunnel lands on one instance and the viewer's websocket on another, and
Cloud Run instances are not addressable, so the two cannot be joined there.

| Option | What it is | Verdict |
|---|---|---|
| **R1** relay inside swarm-api | both ends connect to swarm-api | **Rejected.** No rendezvous across instances; frames on a 1-CPU API. |
| **R2** swarm-api fronts, `swarm-live` rendezvous | console -> IAP -> swarm-api (auth, tenant, role) -> `swarm-live`; gate -> `swarm-live` | **Recommended (Q9).** One public surface, and the one authenticator, so the tenant check is not restated in a second service. Costs a second hop and a swarm-api instance held per viewer; a viewer reconnects when Cloud Run's request timeout ends the socket (noVNC reconnects on its own; the `/live` route asks for the 3600-second maximum). |
| **R3** console -> `swarm-live` directly | its own IAP backend, swarm-api only mints tickets | Kept for scale: no Cloud Run hop. Costs a second IAP backend and an IAP-assertion verifier in `swarm-live`, a second copy of the auth rule this repository has learned not to keep twice. |
| **R4** Pub/Sub or Redis fan-out between swarm-api instances | | Rejected: latency on keystrokes, a new stateful dependency. |

**`swarm-live` is a single-replica StatefulSet in `swarm-system` on the GKE
cluster, not a Cloud Run service.** The relay's one job is that both ends of a
session meet in the same process. A StatefulSet of one replica guarantees at
most one pod; a Cloud Run service with `max_instances = 1` does not (two
revisions overlap during a rollout, and the two ends can land on different
revisions). A relay restart drops every live session for the seconds it takes
to come back; both ends reconnect, and nothing about the task changes because
the relay holds no task state. The console reconnects with a new viewer
ticket. The gate cannot mint its own -- only the worker holds the attempt key
that proves one -- so the WORKER reconnects it: when the gate's `status` says
its tunnel closed, the worker asks the live-session route again (an open
session is answered with a fresh one-use ticket, counted in `tunnel_tickets`)
and hands it over, backing off to one try a minute and stopping when swarm-api
says the session is over (`agent_worker.handoff.HandoffPath._keep_tunnel`).
The gate closes a link the relay has been silent on for 75 s, its pings
included, so a relay pod that vanished without a FIN is noticed rather than
waited on (`livegate.tunnel.SILENT_SECONDS`). Until LB-R6 neither existed, and
a dropped tunnel stayed dropped for the rest of the attempt: its session stayed
open, still serving the last origin and device, so the console drew LIVE, the
strip and the device chips over a frame no viewer could be painted into --
what re-QA pass 8 (2026-10-12) saw on a resumed attempt. Scale-out, when measured to be needed, is sharding by
`session_id` across named replicas with the shard written on the session
document.

**The relay restarts only for a new image, not under a person's hands, and
says so when it does (RELAY-ROLL).** One replica holding every rendezvous in
memory means every restart drops every live session and every person's
control. MEASURED 2026-10-12/14: 40 restarts in ~48 h, because every release
rolled it whether or not its image had changed, and each one cost the console
the ordinary 2-4-8 s reconnect backoff and the gate up to a minute (the 5 s
doubling above). Three things now hold, and none of them makes a restart free:

* `scripts/roll-live-relay.sh` compares the digest the release rendered with
  the one the relay's pods run, prints both, and does not apply when they are
  equal (`--force` rolls anyway). A release that does not roll the relay does
  not re-apply swarm-system's ingress policies either; a policy-only change is
  applied by hand with `kubernetes/apply.sh --swarm-system --confirm`.
* Before a roll it reads the relay's own counts -- `/healthz` now reports
  `controlled`, sessions a person controls, beside `tunnels` and `viewers` --
  inside the pod (`kubectl exec ... python -m swarm_live.status`), so the read
  is authenticated by the Kubernetes API and no NetworkPolicy is widened for
  it. While anyone is watching or in control it waits, up to 480 s
  (`--defer-timeout`; with the 600 s rollout wait it fits the release step's
  20 minutes), then rolls anyway and says why. A person who stays on the page
  for longer than that is interrupted, once.
* On SIGTERM the relay closes every gate AND every viewer with 1012 ("restarting,
  reconnect now", `swarm_live.rendezvous.CLOSE_RESTARTING`) before uvicorn's own
  shutdown cuts the sockets -- queues drained, the recording's last segment
  written -- and refuses every connection that arrives during the drain with
  the same code. 1012 is not 4410: the session is still open, on the next relay.
  The console reconnects on 1012 after ~250 ms, jittered, doubling to at most
  4 s and never shown as failed (`LiveBrowser.tsx` `liveReconnectDelay`);
  swarm-api closes a viewer with 1012 whenever its relay link ends, so the
  console reads the same code either way. The worker reopens the gate's tunnel
  in the same poll when the gate's `status` reports `tunnel_close_code` 1012,
  retrying on the same short schedule for up to 3 s and on every later poll
  (`livegate.tunnel.reconnect_delay`, `handoff.RESTART_BURST_SECONDS`).
  Control comes back through swarm-api: a reconnected viewer's first status
  re-sends the controller's control ticket through the relay (CONTROL-LAPSE).

What is still not immediate: the worker only learns the tunnel closed at its
next poll of the gate (`CONTROL_POLL_SECONDS`, 10 s), and the new relay pod is
not serving until Autopilot has placed it; both ends rejoin within about a
second of that, not of the SIGTERM.

**What the relay is not.** It is not trusted with authorisation: it verifies
tickets it did not mint against a key it shares only with swarm-api, and it
refuses a ticket whose tenant, task, attempt or generation does not match the
session's tunnel ticket. It has **no Firestore access at all** (decision (a)
above; the audit of section 5.8 is written by swarm-api in the transaction of
the change it records), and its only GCS grant is object creation under the
`live/` key of an attempt (section 7).

### 3.4 In the console: noVNC on the agent's page

The agent's detail view (`apps/swarm-ui/src/AgentDetail.tsx`) gains a **Live
browser** panel for a task whose profile is a live-browser profile and whose
state is `RUNNING`. It embeds noVNC (`@novnc/novnc`, pinned), which draws
**pixels into a canvas**. Above the canvas, outside it, the console draws a
strip the page cannot influence: the top frame's real origin as Chromium
reports it to the gate over CDP, the handoff reason, who holds control, and the
buttons (Take control / Return control / Let any member take control).

Pixels are a deliberate choice. A DOM-streaming viewer (rrweb and the like)
would render the agent's page's HTML inside the console's origin: an XSS
surface for every page an agent visits, and a DOM snapshot that carries input
values. noVNC renders nothing a page wrote.

---

## 4. The hand-off protocol

### 4.1 The agent asks for help

```
agent: swarm-handoff request --reason "Sign in to GitHub as the bot account" --expect github.com
agent: swarm-handoff wait            # blocks until resolved, up to 9 minutes a call, then "still waiting" (exit 75)
```

`swarm-handoff` is a small command in the browser image. Like the child-task
spool (`docs/design/child-tasks.md` (`**Spool, not a localhost endpoint`)) it
**writes a file** -- `$SWARM_HANDOFF/requests/<id>.json`, atomically -- and
opens no socket, for the same reasons: no new listener in the agent's reach,
and the request survives a checkpoint. `wait` blocks for at most 9 minutes
because claude-code's Bash tool kills a command at 10 (`#736`, recorded in
`apps/agent-worker/agent_worker/runners/stdin_hook.py`); it exits

| exit | meaning | what the agent should do |
|---|---|---|
| 0 | control was returned; a JSON summary is on stdout (section 5.9) | look at the page once and continue |
| 75 | still waiting | call `wait` again, at once |
| 3 | the wait expired; the worker is checkpointing and parking | write its notes into `work/` and stop; the worker ends it either way |
| 4 | the task is being cancelled | stop |
| 5 | refused (rate limit, no live path, reason invalid) | carry on without help, or fail with that reason |

**Wait, do not poll (HANDOFF-POLICY, 2026-10-13).** Measured on
task_2ac5d7e22a51460995a2: while it waited, the agent ran `curl
/json/version` against CDP in a loop after `wait` returned. Polling a paused
browser learns nothing and spends the agent's turns. So every answer `wait`
prints carries `next`, the instruction for that exit -- on 0 "look once and
carry on", on 75 "run `wait` again; do not poll the browser" -- and the
helper itself reads the worker's answer no faster than every 100 ms
(`MIN_POLL_SECONDS`, whatever `--poll` says). The agent is told the same
twice before it ever asks: the claude-code runner's browser note
(`LIVE_BROWSER_NOTE`, `apps/agent-worker/agent_worker/runners/claude_code.py`:
"wait, do not poll the browser") and the spool's guide (`AGENT_GUIDE`,
`apps/agent-worker/agent_worker/handoff.py`). A command that "blocks until
resolved" still cannot outlive the Bash tool's 10 minutes, and a watched hold
may last 30 (section 4.3); exit 75 is that seam, and `next` is what keeps the
agent on it instead of in a loop of its own.

The **worker** picks the request up on its control poll (10 seconds,
`WorkerConfig.control_poll_seconds`,
`apps/agent-worker/agent_worker/config.py::WorkerConfig`) or sooner by inotify, and:

1. validates it: `reason` at most 200 characters, passed through the
   redaction filter before it is stored or shown (`redact`,
   `apps/redaction/swarm_redaction/rules.py::redact`); `expect` a host that
   `url_refusal` (`apps/common/swarm_common/profiles.py::url_refusal`) accepts;
   at most `max_handoffs_per_attempt` (default 3);
2. tells the gate to **pause the agent's CDP** (section 5.4.3), so from this
   moment the agent cannot act on the browser, cooperative or not;
3. records the wait through a worker route signed with the attempt key, `POST
   /v1/tasks/{task_id}/handoff`. swarm-api sets the task's `human_wait`
   (`{reason, expect, requested_at, deadline, controller: null}`), writes a
   `human_handoff_requested` event, and raises the console banner.

The task **stays `RUNNING`** (section 4.3 says why that is the recommendation,
and Q2 asks the owner).

**Notification (HANDOFF-POLICY, owner decision 2026-10-13: "notify, park
fast, extend while watched").** The moment the wait is recorded, the
notification feed (`GET /v1/notifications`,
`apps/swarm-api/swarm_api/notifications.py::handoffs`) derives an unread
**"Agent needs you"** item -- the redacted reason as its detail, a link to the
task's Browser tab, and a count on the bell's badge (`unread_needs_you`). It
goes to everyone who may take control (section 5.1, `handoff.may_control`):
the submitter, and every member of the tenant while "Let any member take
control" is on for the attempt's session (or when the task has no human
owner). A personal workspace's member-scoped reader is told of their own
tasks only, and nothing crosses a tenant (invariant 9). The item is derived,
not stored (docs/notifications.md): it is gone on the next read once the
hand-off resolves, and its id carries the request id, so a second hand-off in
the same attempt is unread again. The parking-fast rule below depends on this
bell: a person who is coming now hears about it within a feed poll.

There is still no outbound channel (no mail or chat integration in
`apps/swarm-api` or `apps/reconciler`); Q8 asks whether one is wanted.

### 4.2 Take control, sign in, return control

1. **Take.** An eligible person (section 5.1) presses Take control. swarm-api
   runs one Firestore transaction on `live_sessions/{session_id}`: no current
   controller (or the caller may preempt, section 5.1), then sets
   `controller = {email, since, renew_by}` and appends to `live_audit` in the
   same transaction. It tells the relay with a `control` ticket for that
   person, session and generation, sent on the person's own proxied viewer
   connection (a `release` ticket ends it), and the relay tells the gate. The gate enters
   **control mode**: input from that one viewer connection is injected through
   XTEST; every other viewer's input is dropped; the agent's CDP stays paused.
   One controller at a time, always.
2. **Sign in.** The person sees the address bar and the console's origin strip
   (section 5.7), types, and completes the sign-in. Keys travel console ->
   swarm-api -> relay -> gate -> Xvfb: inside TLS to swarm-api's load
   balancer, then as plain ws inside the VPC (decision (b), section 3.3), and
   no hop logs their content (section 5.3.3).
3. **Return.** The person presses Return control. Before the gate resumes the
   agent's CDP it **clears every password input** in every frame through its
   own CDP session (a page left on a half-submitted form must not hand the
   value to the agent), restores the agent's page scripts (section 5.4.3),
   and then lets the agent's queued commands through. swarm-api clears
   `human_wait`, writes `human_control_returned` and `human_handoff_resolved`,
   and the agent's `wait` exits 0.

Control also ends without a return press, each case audited with its cause:
the controller's console stops renewing for 60 seconds (`renew_by` passed),
the controller is idle for `control_idle_seconds` (default 300; no input
event), the controller is preempted (section 5.1), their membership lapses
(section 5.2), the task is cancelled, or the hold expires.

**A person may also take control when the agent did not ask** (Q6). The gate
pauses the agent's CDP the same way. The agent's in-flight Playwright calls
block, and may time out and fail; on return the agent sees a failed or slow
action and looks again. It is not told why unless it calls `swarm-handoff
status`, which reports the last control episode in the same shape as `wait`.

### 4.3 The bounded wait, and what it costs

There are two phases, and the boundary between them is where capacity is
given back.

```
RUNNING (human_wait set, lease HELD) --hold expires, nobody in control-->
   worker: checkpoint("human-wait"), upload outputs, wipe browser,
           park(HUMAN_REQUIRED), bounded attempt refund, release lease, exit
PARKED (HUMAN_REQUIRED, NO capacity) --a person presses "I'm ready to sign in"-->
READY --admission--> LEASED -> ... -> RUNNING (new attempt, new generation)
   worker restores work/, tells the agent which hand-offs were done (resumed.md),
   starts it; console shows "starting your browser"
   agent carries on from its notes, re-opens the page and asks again only for
   what was not done; the person is already there
PARKED (HUMAN_REQUIRED) past human_park_max_seconds --> DEAD_LETTERED, end_cause human_wait_expired
```

**Phase H, the hold: `RUNNING` with `human_wait` set, lease held -- one
minute unless someone is watching, up to 30 while they are (HANDOFF-POLICY,
owner decision 2026-10-13, replacing Q3's flat 10 minutes).** The browser and
its half-finished state are alive, which is the whole point of the feature: a
sign-in has to happen in the browser that will use the session. Capacity is
exactly what it is for any `RUNNING` browser task: the lease holds the
`browser` resource class's 1 unit (2 until contract request 79) and the
backend and provider pools
(`apps/common/swarm_common/profiles.py::RESOURCE_CLASSES`), and the pod costs 4
vCPU and 8 GiB per minute (8 and 16 until request 79), idle or not, because `requests == limits`
(invariant 7).

*Why the flat 10 minutes went.* Measured over 26 live-browser tasks
(2026-10-13): 42 hand-offs, 16 returned, 25 expired (60%), 1 cancelled. 13 of
the expiries had nobody present (`person_waiting` false on the request event),
and each held its 2-unit lease and pod for the full 10 minutes -- about 4.2
pod-hours for nobody. And 7 expired AFTER the person had pressed "I'm ready":
they were there, on a slow or frozen view, and the clock ran out under them.
One fixed number was wrong in both directions.

*The rule.* swarm-api owns the deadline (`Task.human_wait.deadline`); the
worker reads it on every control poll and parks when it passes, exactly as
before (`agent_worker.handoff.HandoffPath._watch`).

* **Park fast.** A request opens the hold at `min(requested_at +
  human_unwatched_seconds, attempt deadline)`; `human_unwatched_seconds` is 60
  (`apps/swarm-api/swarm_api/handoff.py::opening_deadline`). If nobody opens
  the live view in that minute, the worker parks: wipe, checkpoint,
  `PARKED(HUMAN_REQUIRED)`, release the lease, exit (invariant 4) -- within a
  control poll (10 s) of the minute, instead of after ten.
* **Extend while watched.** swarm-api's proxied viewer socket is the "someone
  is watching" signal: on join and every `live_watch_flush_seconds` (15) it
  calls `LiveService.watched`, which moves the deadline to `now +
  human_unwatched_seconds` and stamps `human_wait.watched_at`. So a watched
  hold is always about a minute ahead of now, and parks about a minute after
  the last viewer leaves. Taking control and every flush of a controller's
  input move it to `now + control_idle_seconds` as before, so a person
  mid-sign-in is not parked under their fingers.
* **Capped.** Every extension, watched or controlled, stops at
  `requested_at + human_hold_max_seconds` (1800, 30 minutes) and the
  attempt's own deadline (`handoff.extended_deadline`), and never moves the
  deadline earlier. A tab left open on a walked-away desk costs half an hour,
  not an afternoon.
* **Ended by the worker's resolve, not by the clock (LB-RETURN).** This bullet
  said "never revived": a viewer who arrived after the deadline changed
  nothing, so as not to race the worker's park. MEASURED: the live-browser
  acceptance group parked on every run from 2026-10-14 00:24Z with its
  viewer connected and the person in control, because the person came within
  the worker's 10 s poll of the minute and the park went ahead under them. So
  the race is decided in Firestore instead. A viewer or controller moves an
  OPEN hold even past its deadline. The worker's expiry names the deadline
  it judged (`seen_deadline`), and swarm-api answers it `{"resolved":
  "revived"}`, writing nothing (`HandoffService.resolve`), when that deadline
  has since moved into the future. The worker then keeps holding and tells the agent nothing
  (`HandoffPath.confirm_expired`). Whichever transaction commits first
  decides.
* **A return is never lost to a park.** A person who returns control while the
  attempt is still RUNNING leaves `metadata.human_returned` (attempt,
  generation, who). The worker's park reads it in its own fenced transaction
  and writes READY instead of PARKED, with the same checkpoint, refund and
  lease release (`park_human_required`). A return that arrives after the park
  committed is that person's "I'm ready" (`LiveService.return_control`,
  `handoff.ready_writes`). Both count only for the attempt that parked, at its
  generation, and a new request forgets an older return. Anyone else presses
  "I'm ready" (below), which is unchanged; the console calls a parked
  hand-off "parked -- press I'm ready to resume", not expired.

Who is counted as watching is any member admitted to the view (section 5.2),
not only someone who may take control: the submitter watching from a second
device, or a member waiting for control to be opened to them, is a person the
pod is worth holding for. The ticket alone is not counted -- a ticket nobody
connects with is not a person at the screen.

**Phase P, the park: `PARKED` with `ParkReason.HUMAN_REQUIRED`, no capacity.**
Invariant 4 forbids sleeping through a long wait, and a person's lunch is a
long wait. The worker parks exactly as the quota path does
(`Worker._park_for_quota`, `apps/agent-worker/agent_worker/lifecycle.py::Worker._park_for_quota`;
`ControlPlane.park`, `apps/agent-worker/agent_worker/control.py::ControlPlane.park`): checkpoint,
upload, park, release, exit. `RUNNING -> PARKED` is already legal
(`apps/common/swarm_common/states.py::_ALLOWED`). The park cannot reuse an
existing reason: the scheduler promotes the existing ones by its own rules (a
cooldown ends, a dependency finishes), and nothing but a person should promote
this one. `MANUAL_PAUSE`, the obvious candidate, was considered and rejected
for exactly that reason: the scheduler returns it to `READY` once what paused
it clears (`Scheduler._promote_manual_pauses`,
`apps/scheduler/scheduler/loop.py::Scheduler._promote_manual_pauses`), so a hand-off parked under it
would be re-admitted with nobody there to sign in. So it is a new `ParkReason` (contract request LB-A, section 8).

**What the park costs.** The browser is wiped: its pod, its profile, its
history (section 5.5 says why the profile is never checkpointed). What comes
back is what §5.5.2 keeps: the open tabs, reopened before the next agent
starts, and -- in a personal tenant only -- their cookies and local storage,
through the saved-logins route. The claude-code
agent has no conversation resume (`docs/design/child-tasks.md` (`the CLI runners have no conversation resume`)), so the next attempt starts the agent again on
the restored `work/`. And the person waits for a cold start
-- admission plus a GKE pod start -- before the browser is back. That is the
price of giving the capacity back, and it is paid only when nobody opened the
view within the minute, or the last viewer left.

**What the resumed attempt is told (RESUME-PROGRESS, QA12-14).** Measured
2026-10-13 on task_4defa61c5eea4a5c87f5: an agent asked for QA1 (returned),
then QA2 (nobody came; parked); generation 2 asked for QA1 again, then QA2.
Two things were missing, and both are now in place:

* *The restore.* The park's refund takes `attempt_count` back down, so the
  next lease read 1 and the worker took it for the task's first attempt,
  which restores nothing (`Worker._recorded_checkpoint`). The agent's notes
  and work were never put back. `human_wait_resumes` is now counted back
  exactly as `child_await_resumes` is, so the attempt after a hand-off park
  restores the checkpoint the park wrote.
* *A record of what was done.* The worker keeps a ledger of every hand-off,
  `$SWARM_HANDOFF/history.json` (request, redacted reason, outcome, the
  §5.9 summary), which travels in the checkpoint with the rest of `work/`.
  On an attempt whose restored ledger holds an earlier attempt's hand-offs,
  the worker writes `$SWARM_HANDOFF/resumed.md` before the agent starts --
  each hand-off marked DONE or NOT DONE, oldest first, then the agent's own
  `$SWARM_HANDOFF/notes.md`, redacted -- and the claude-code runner puts it
  in the agent's prompt, after the caller's
  (`apps/agent-worker/agent_worker/handoff.py::resume_summary`,
  `apps/agent-worker/agent_worker/runners/claude_code.py::resume_summary`). The
  agent's guide asks it to keep `notes.md` current, and to write it on exit
  3. The ledger is in the agent's reach, so it is read as untrusted: shape-
  checked, bounded and redacted again, and only ever told to the same task's
  agent; a `resumed.md` the worker did not write this attempt is removed, and
  an earlier attempt's answers in `responses/` are cleared so a `wait` cannot
  read an old `expired` as its own.

Continuing the CLI's own conversation (`claude --resume`) across the park was
considered and not done: the transcript is in the checkpoint
(`work/.claude/`), but it is keyed by the working directory, which is
per-attempt (`<workspace_root>/<attempt_id>/work`), and every absolute path
the old conversation used points into a workspace that no longer exists. The
summary needs no CLI internals and no contract change. Invariants 4 and 8 are
untouched: the park still checkpoints, releases and exits; the resume only
changes what the next attempt restores and is told.

**Promotion is a person's.** `POST /v1/tasks/{task_id}/human-ready`
(eligible as for taking control, section 5.1) moves `PARKED(HUMAN_REQUIRED) ->
READY` and records that a person is waiting, so the next attempt's worker opens
the hand-off immediately and the console says "starting your browser". No
sweep promotes it. A sweep beside the scheduler's other park sweeps
(`Scheduler._promote_dependencies`,
`apps/scheduler/scheduler/loop.py::Scheduler._promote_dependencies`) dead-letters one parked
past `human_park_max_seconds` (default 24 hours, Q4) with `end_cause:
human_wait_expired`. `PARKED -> DEAD_LETTERED` is already legal; `PARKED ->
FAILED` is not, and adding it is not worth a contract change for a label.

**The park does not spend an attempt, up to a bound.** Admission increments
`attempt_count` on every lease
(`apps/common/swarm_common/admission.py::acquire_lease_in_transaction`). The park's fenced
transaction refunds one and counts `metadata.human_wait_resumes`, while fewer
than `max_human_resumes` (default 2) are used -- the shape the child-task await
uses (`docs/design/child-tasks.md` (`The await does not spend an attempt, up to a bound.`)). `human_wait_resumes` joins `RESERVED_METADATA_KEYS`
(`apps/swarm-api/swarm_api/validation.py::RESERVED_METADATA_KEYS`).

**Why not fail on timeout.** It is simpler, and it throws away the agent's work
because a person was in a meeting. **Why not hold indefinitely.** Invariant 4,
and an idle 8-vCPU pod.

#### Invariants 1-3, stated for the waiting pod

* **Invariant 1.** A holding pod is `RUNNING` and is real demand: it exists, so
  it is counted. A parked task is `PARKED` and creates none. No new state
  creates demand.
* **Invariant 2.** Unchanged. The re-admission after `human-ready` reserves
  every pool in one transaction, like any other.
* **Invariant 3.** Unchanged. The hold is counted from `LEASED` because it is
  inside an attempt that was leased; nothing about it is counted from
  `RUNNING`.

#### Why a field and not a new task state (Q2)

The brief suggested a state, `WAITING_FOR_HUMAN`. A state that holds a pod
must join `CONCURRENCY_STATES`
(`apps/common/swarm_common/states.py::CONCURRENCY_STATES`), get transitions to and from
`RUNNING`, `PARKED`, `READY`, `FAILED` and `CANCELLED`, and then every reader
that asks "is this attempt running?" must learn the new answer: the
reconciler's heartbeat and stale-generation checks, the worker's fence checks
in `control.py`, release, the UI's state lists and the contract-parity script
that holds shell restatements to the Python
(`scripts/lib/check-contract-parity.sh`). A reader that is missed treats a
holding task as not running and reclaims it, or as not holding capacity and
oversubscribes the pool -- invariant 3's exact failure. The field
(`Task.human_wait`) carries the same information to the console and to `sc
status`, and changes no reader that does not want it. The console shows
"Waiting for you" either way.

### 4.4 Cancellation while waiting

* **During the hold.** `request_cancel`
  (`apps/swarm-api/swarm_api/store.py`) sets `cancel_requested` as for any
  capacity-holding task. The worker sees it on its next control poll
  (`ControlSignals.cancel_requested`,
  `apps/agent-worker/agent_worker/control.py::ControlSignals`), tells the gate to end control
  mode, close the tunnel and wipe the browser; swarm-api closes the session
  (viewers see "cancelled"), revokes the controller with cause `cancelled`,
  and the worker takes its ordinary cancellation path. The agent's `wait`
  exits 4.
* **During the park.** `PARKED -> CANCELLED` is a direct API transition today.
  The session is already closed; nothing else holds anything.
* **A person in control when the cancel lands** loses control at once, and the
  console says why.

---

## 5. Security

### 5.1 Who may watch, and who may control

| Who | Watch | Take control | Why |
|---|---|---|---|
| A member of the task's tenant | **yes** | only if the submitter allowed it for this task | LB-2. Watching shows masked pixels of work the tenant already owns. |
| The task's submitter (`Task.submitted_by`, `apps/common/swarm_common/models.py::Task`) | yes | **yes**, and may preempt another controller | The person whose task it is; the credentials typed are theirs or their team's. |
| A workflow step's or schedule's task with a non-human submitter | yes | the workflow's or schedule's human owner; if there is none, any tenant member | A service account cannot press a button. |
| A platform admin who is not a tenant member | **no** | no | Invariant 9 and LB-2 say tenant members only. No break-glass in phase 1 (Q5). |
| Anyone else | no | no | |

**Recommendation (Q1): control defaults to the submitter.** A tenant may be a
whole team; a teammate taking control of someone else's agent and typing
credentials into it is a decision the submitter should make. The submitter can
open control to any member of the tenant for this task with one toggle,
audited. "Any member may control" as the default is simpler and is the
alternative offered.

**The toggle is the task's, and it goes both ways (owner decision 2026-10-13,
"Carry over, with a confirm"; QA12-11).** QA pass p12 found it one way, with no
word of what it did, and back off on a resumed attempt, because it lived on one
attempt's `live_sessions` document. swarm-api now keeps it on the task document
as well (`live_open_to_members`, `live_opened_to_members_by`; swarm-api's own
fields, which the frozen `Task` model does not read) and copies it into each new
attempt's session when the session opens, saying so in that session's
`session_opened` audit entry. Turning it on asks once, explaining that it
carries over; `DELETE /v1/tasks/{task_id}/live/open-to-members` turns it off,
submitter only, audited as `control_closed_to_members`. Turning it off takes no
control away: whoever holds it keeps it until they return it or it lapses, and
the submitter can take it over.

### 5.2 Every connection is checked, and keeps being checked

On every websocket connection to `/v1/live/{session_id}/stream`, swarm-api:

1. verifies the IAP assertion and resolves the tenant with the same
   `Authenticator` every route uses, so the Cloud Identity constraint applies
   unchanged: `searchTransitiveGroups` 403s in this project, so membership is
   checked **per registered group**, with `x-goog-user-project` on every call
   (`apps/swarm-api/swarm_api/groups.py::CloudIdentityGroups`);
2. reads the task and refuses unless `task.tenant_id == ctx.tenant_id` **and**
   the tenant document's principal matches `ctx.tenant_principal` (two groups
   with the same local part slug to the same `tenant_id`;
   `AuthContext.tenant_principal`, `apps/swarm-api/swarm_api/auth.py::AuthContext`). A
   refusal is a 404, the same answer as a task that does not exist, so a
   foreign task id is not confirmed;
3. verifies the one-use viewer ticket it minted for this caller (`role =
   viewer`, the caller's email, the session, the generation, 30-second expiry,
   sent as the socket's first message), refuses it unless it names this
   address's session, the session is the task's current one, open, at the
   ticket's generation, and presents it to the relay, which refuses a ticket
   used twice, expired, or naming another session, tenant or generation. A
   refusal by the relay closes the console's socket as a 404 (4404), as every
   other refusal does; the relay's viewer cap passes through as 4429.

**Membership is re-checked while the socket is open.** swarm-api re-runs step 1
every 5 minutes for each proxied socket and closes it when the answer changes.
Group answers are cached for 120 seconds (`CloudIdentityGroups`, `ttl_seconds`),
so a person removed from the tenant's group loses the view within about 7
minutes, and loses control at the same moment. A taking of control always runs
a fresh check, not the cached one.

**Nothing secret travels in a URL.** Load balancer, IAP and Cloud Run request
logs record URLs. Tickets travel as the first websocket message; the URL
carries only the session id, which is not secret (`live_<attempt id>`).

**A session that ends is ended at the relay too** (decision (a)). The relay
reads no session state, so swarm-api tells it: on every transaction that
closes a session (a cancel, an expired hold, an attempt that lost its lease),
when a worker of a stale generation asks for a tunnel ticket, when a new
attempt's session opens (every earlier one of that task is stale, watched or
not), and when a proxied socket sees its session closed or its generation
moved, swarm-api POSTs the relay's `/internal/close`. The relay drops the
tunnel and every viewer of that session and refuses, until they have expired,
every ticket for it minted before the close. A close the relay does not
receive is logged; the session's tunnel still ends with the stale worker's
pod, and no new ticket for a closed session is minted.

### 5.3 Masking before anything leaves the pod (LB-3)

#### 5.3.1 What is masked

The gate paints an opaque box, in the framebuffer, before encoding, over:

* every `input` whose type is or **has ever been** `password` on this page (a
  "show password" toggle switches the type to `text`; the set is sticky), and
  every input with `autocomplete` of `current-password`, `new-password`,
  `one-time-code`, `cc-number` or `cc-csc`;
* every element whose visible text matches a rule of the platform's redaction
  set (`apps/redaction/swarm_redaction/rules.py::RULES`): an API key on a dashboard, a
  token in a page;
* the address bar, when the top frame's URL matches a redaction rule (a token
  in a query string).

Password characters are already drawn as dots; the mask hides what dots do
not: the length, a revealed value, and secrets that are not in password fields
at all.

#### 5.3.2 How, and why it fails closed

The gate's own CDP session (on the pipe, invisible to the agent) creates an
**isolated world** in every frame (`Page.createIsolatedWorld`), which page
scripts and the agent's own world cannot see. A script there watches the
sensitive elements (mutation and resize observers) and reports their rectangles
and the visible text nodes to the gate. The gate runs the redaction rules on
that text **in Python, by importing `swarm_redaction`** -- not a JavaScript or
Go restatement of the rules, which is exactly the kind of second copy that has
drifted here before. It maps page rectangles to screen coordinates from the
window's bounds, and paints.

A rectangle report is always slightly older than the frame it is painted on.
So the gate **fails closed**:

* a frame is masked only with a report taken after the last layout-changing
  event (scroll, resize, navigation, a mutation inside a tracked element);
  otherwise the whole page area below the address bar is painted out for that
  frame -- after the short hold below, and only for a layout change. Scrolling
  flickers. That is the cost;
* while the gate's CDP session is down, or no report has arrived since a
  navigation, every frame is a blank "masking unavailable" frame;
* every box is dilated by 8 pixels.

**Three amendments, issue #1340 (owner decisions 2026-10-14).** MEASURED
2026-10-15 on task_2ac5d7e22a51460995a2, release b93ad6cb: the gate logged
`live mask: frame owner unresolved (CdpError)` twice at attempt start. Linking
an iframe to the element box its parent reports (`MaskCdp.link`) had failed for
both reCAPTCHA frames, and the only retry was a re-created parent observer
world, which never came. So both frames stayed covered for the whole hand-off
("Part of this page is covered: an embedded frame on it cannot be checked"),
and the person asked to solve the challenge could not see it. On the same
page, the whole page area went PAGE_BLANKED for 0.3-1.2 s in every metrics
window while the page kept changing.

* **A failed link is retried** on the parent frame's next fresh report and on
  the child's next navigation. Each retry waits at least a backoff after the
  last failure, 250 ms doubling to 2 s, and there are at most 5 tries in all.
  After that the frame stays covered as before. The log line carries the CDP
  error's message as well as its class (`CdpError: No frame for given id
  found`). That message is Chromium's protocol text and never page content;
  any other exception is still logged by class alone. Why: one transient
  failure at attempt start should not decide a whole hand-off. Bounded,
  because a frame that never links must still fail closed.
* **Challenge frames are shown while a person holds control.** Holding control
  means a `human_wait` hand-off (`pause` until `resume` or `end_control`) or
  take-control, and in either case the agent is held. During that time, a
  child frame whose URL is on an exact allowlist is not covered when it cannot
  vouch for itself. The allowlist (`mask.SHOWN_IN_CONTROL`) is `https`, an
  exact host and a path prefix, with no port and no userinfo:
  `www.google.com/recaptcha/`, `www.recaptcha.net/recaptcha/`,
  `*.hcaptcha.com/` (a subdomain, not `hcaptcha.com` itself) and
  `challenges.cloudflare.com/`. A lookalike stays covered: `recaptcha.evil.com`,
  `www.google.com.evil.com`, `www.google.com@evil.com`, or another path on an
  allowlisted host. Whenever the frame has reported on its current document,
  that last report's password and credential fields stay masked, and so does
  any of its text a redaction rule matches. The top
  frame is never shown this way, and nothing is shown this way while the agent
  drives: the gate reads `Gate.person_in_control` as each frame is masked, and
  a resume clears it before the agent's held commands are released. Each frame
  shown this way is recorded once per hand-off in the gate's event log as
  `mask_frame_shown`, carrying the host (never the path, the query or the
  content) and the reason `REASON_SHOWN_IN_CONTROL`. Why: a challenge is the
  thing the person was handed the page to do. It is a third party's widget,
  not the site's content, and while a person holds control what it shows is
  for that person. The frame's own content, including any frame inside it, is
  shown unchecked. That is the trade the owner chose, and §5.3.4 lists it.
* **Fewer whole-page blanks.** When the top frame's report is stale only
  because its observer announced a layout change (`REASON_CHANGING`), the gate
  keeps sending the last frame it masked from a fresh report, for up to 300 ms
  from the first announcement (`HOLD_CHANGING_SECONDS`), before it blanks.
  Further announcements do not extend the hold. A navigation (cross-document
  or same-document), a tab switch, a hidden tab, a session loss, a device
  change or a change of who drives drops the held frame, and each one blanks
  or goes UNAVAILABLE at once, as before. Why this keeps the rule: the held
  pixels were vouched for when they were first sent, so holding them sends
  nothing the mask has not checked. The viewer sees a page up to 300 ms old
  instead of a grey flash.

**Pages dev-browser opens, and iframe pages, issue #1344.** MEASURED
2026-10-15 by visual QA on release b93ad6cb. On task_f7ff5ae0d8ac4612b0ff
(browser tool dev-browser) the live view stayed the grey "masking
unavailable" frame for minutes, also while a person held control, with
"Can't show this page safely: the page on screen has not reported its layout
yet". Its timeline shows dev-browser opening a new page for every navigation.
On https://the-internet.herokuapp.com/iframe, a TinyMCE editor in an iframe,
the whole page area was blanked for all three browser tools. Two causes, both
in the mask's install on a newly followed page (`MaskCdp.install`), and one
rule:

* **The install's frame tree is not a navigation.** The install read
  `Page.getFrameTree` last and told the state every frame in it had just
  navigated. Puppeteer's `newPage()` is followed at once by its `goto()`, so
  the page often committed while the install was under way. Its observer had
  already reported on the new document, and that "navigation" retired it:
  every later report from the live document was dropped as one from a page
  navigated away from, until the next navigation. A person holding control
  makes no navigation, so the grey stayed. Now a frame the gate already heard
  of during the install keeps its document; a different URL only makes its
  report stale until the next one (at most the observer's one-second
  heartbeat).
* **The children the tree lists are linked.** A child frame was linked to its
  iframe element only when it attached, or when its parent's observer world
  was created, and the install creates that world before it reads the tree.
  So an iframe already on the page when the gate followed it stayed unlinked
  for good: a page loaded before a tab switch, a reconnect or a device change,
  and every page dev-browser opened. The same was true of an out-of-process
  frame open before the follow, which the page's session never sees attach.
  Now the install links every unlinked child of a frame it has a world for,
  and the follower does it again once each out-of-process frame's session is
  installed. The retries and their bound are unchanged.
* **An unlinked child covers the box left for it.** The rule above said that
  only a child the gate cannot place blanks the page area; an unlinked child
  was such a child. Now an unlinked child (its link not yet answered, or
  failed for good) whose parent's fresh report has an iframe box that no
  linked child claims, one box for every unlinked child, covers those boxes
  and the page is shown. Why this still fails closed: a frame's content is
  clipped to its element's box. The observer reports the box of every iframe
  and frame element it walks. The gate hears every child frame attach, on the
  parent's session, before the report that measured its element. So those
  unclaimed boxes are the unlinked children's boxes. With fewer unclaimed
  boxes than unlinked children, one child is drawn where no report places it,
  and the page area is blanked as before. The page area is also still blanked
  for a child linked to an element the report does not have (one in a closed
  shadow root, an `<object>`), and for two children linked to one element.
  Every other rule in this section is unchanged: the top frame unreported,
  stale or truncated, the session down, the tab not on screen.

**The mask is applied once, at the source.** The relay, the recorder and the
viewers all receive the masked frames; there is no unmasked stream anywhere to
leak.

#### 5.3.3 Keys never logged, anywhere

Typed characters exist in four places: the console's memory, swarm-api's and
the relay's buffers, and the gate on its way to XTEST. **No hop logs RFB
payloads**: every log line on the path carries byte and event counts only, and
a unit test per hop captures logs while a key event passes and asserts no
keysym appears. The audit records "control held 3m12s, 41 key events, 9
pointer events", never which keys.

#### 5.3.4 What the mask does not cover, stated plainly

* A secret drawn as an image (a QR code for 2FA setup, a screenshot of a key)
  is not text and is not masked.
* A secret in a canvas or a closed shadow root is not visible to the isolated
  world and is not masked.
* An unlinked child frame is matched to the iframe boxes its parent reports by
  count (§5.3.2, issue #1344), which assumes the gate has heard every child
  frame attach. One gap remains: an out-of-process frame already open when
  the page was followed is unknown until Chromium auto-attaches its session.
  If, in that window, another child is unlinked and held by an element the
  observer cannot see (a closed shadow root), the out-of-process frame's
  unclaimed box can stand in for it, and that child is shown unchecked until
  the auto-attach lands.
* An ordinary field the person types a secret into, which is not a password
  field, shows what they type. The console's take-control dialog says so.
* A challenge frame on the allowlist (`SHOWN_IN_CONTROL`, §5.3.2) is shown
  unchecked while a person holds control, when it cannot vouch for itself.
  Its last report on its current document is still applied: its password and
  credential fields, and any text a redaction rule matches. Whatever it drew
  after that report, or before it ever reported, is not checked.
* A viewer can photograph their screen. Masking reduces what a viewer sees; it
  is not DRM.

### 5.4 Keeping the agent out of the person's session

#### 5.4.1 The worker proves it is the worker: the attempt key

The agent can mint the tenant's ID token and read the container's environment,
so neither can authorise a worker-only route
(`docs/design/child-tasks.md` (`### 3.2 What makes "only the worker" true: the attempt key`)). The live session and the hand-off routes **reuse that
attempt key**: the Ed25519 key the worker generates after `make_non_dumpable`
(`apps/agent-worker/agent_worker/hardening.py::make_non_dumpable`) and registers before the agent
exists. They sign domain-separated messages (`swarm-live-session/v1`,
`swarm-handoff/v1`) so a signature for one is never a signature for another.
Today the key is registered only when the child path is configured
(`worker_env`, `apps/scheduler/scheduler/dispatch.py::worker_env`, mints the
nonce only when `child_key and api_url and not task.parent_task_id`); lane L4 makes it registered for every
live-browser attempt. **A worker that cannot protect its heap gets no live
path**: no tunnel ticket, no hand-off; the console says "live view unavailable
for this attempt" and the agent's `swarm-handoff` exits 5.

**A slow pod is not a dead lease (LB-KEYRACE, 2026-10-12).** The key is
registered, and the live session opened, before the worker's first heartbeat.
A lease is written with `heartbeat_at: None` and `expires_at = created_at +
lease_timeout_seconds` (120 s), so a GKE pod that took longer than that to
reach STARTING was refused `child_key_window_closed` on a lease the reconciler
still judged live by its dispatch deadline: task_23cc2e82178640709a45 took
154 s, registered no key, and ran with no live browser. swarm-api's
`_lease_is_live` (`apps/swarm-api/swarm_api/children.py`) now uses the
reconciler's own clock for a never-beaten lease — live until
`dispatch_deadline` — and nothing else about the fence moved: the task must be
STARTING, at this lease and generation, the lease unreleased, the slot unused.
Whatever still refuses the live path is never silent: the worker writes a
RUNNING event with `cause: live_browser_unavailable`, the code, the reason and
`message: "live browser unavailable: <reason>"`, which the console's event list
shows A refused key or heap costs the view and
the hand-off only: the gate is up, so the agent keeps its browser (§3.3,
NOLIVE-GUARD).

#### 5.4.2 The worker talks to the gate on a channel the agent cannot use

`ctl.sock` sits on a volume both containers mount, so the agent can connect to
it. The gate therefore accepts **one registration only**: the worker, after
`make_non_dumpable`, generates a key in its heap and registers it; the native
sidecar is up first, and the agent is spawned later
(`apps/agent-worker/agent_worker/lifecycle.py::Worker._prepare`), so the slot is taken before the
agent exists -- the same first-registration-wins argument as the attempt key.
Every later message is MAC'd with that key and carries a counter. A gate that
receives a second registration refuses it and records `gate_ctl_refused`.

**A gate that restarts refuses every registration.** A native sidecar has
`restartPolicy: Always`, so the gate can restart mid-attempt while the agent
is alive, and a fresh gate process would otherwise reopen the slot for
whoever connects first. The gate therefore writes a marker to its own
sidecar-only volume on its first start; a gate that finds the marker is a
restart, accepts no registration at all, keeps Chromium closed and records
`gate_restarted`. The live path ends for that attempt: the worker sees its MAC'd
channel drop, treats it as "live view unavailable for this attempt", and an
open hand-off ends as if the hold had expired (checkpoint, park,
section 4.3), so the next attempt gets a fresh pod and a fresh gate. The worker
does not re-key: re-keying would need the worker to prove itself to a gate
that cannot tell it from the agent, which is the problem the first
registration exists to avoid. The exposure in between is small anyway, since
the agent holds no tunnel ticket, but a slot that silently reopens is the
kind of gap this section exists to close.

#### 5.4.3 While a person is in control, the agent's CDP is held

The CDP proxy is the only way the worker container reaches Chromium. In
control mode it:

* **holds** every command from the agent side (the agent's Playwright calls
  block; nothing reaches Chromium), and refuses new CDP connections;
* **drops** every event to the agent side, and after return **strips
  `postData`** from any network event about a request that started during
  control, and refuses `Network.getRequestPostData` and
  `Network.getResponseBody` for those request ids. The sign-in POST is never
  shown to the agent;
* **answers `Fetch.requestPaused` itself** with `Fetch.continueRequest`: an
  agent that had enabled request interception would otherwise freeze the
  person's sign-in waiting for a handler that is held;
* **removes the agent's page scripts** -- every
  `Page.addScriptToEvaluateOnNewDocument` and `Runtime.addBinding` it has seen
  the agent make, tracked by identifier -- and restores them on return, so a
  page loaded during control runs no agent-installed listener;
* can **"start from a fresh page"** when asked for it by name (`pause` with
  `fresh_page: true`): the gate creates **its own browser context**
  (`Target.createBrowserContext` on the gate's session, with no proxy
  parameters and downloads set to `downloads/`) and opens the hand-off's
  `expect` host in it, so the sign-in happens on a document no agent script
  has touched, in a context whose network settings no agent call set. On
  return the gate hands that context to the agent (the proxy then forwards the
  agent's attach to its targets), and the agent continues there, signed in.
  **It is not the default** (owner decision 2026-10-13, LB-CURSOR2): opened on
  every hand-off with an `expect`, it put a second tab in front of the agent's
  -- measured on task_1d1785b21d5b4e1f894e, the agent's form tab sat hidden
  and empty while the stream and the person's input went to the new
  `https://httpbin.org/` tab. Taking control now keeps the agent's current
  tab, brings it to the front, and routes input to it. A page loaded before
  control, in the agent's own context, may carry listeners the agent
  installed; the scripts it added are removed for the window (above), and its
  held commands cannot act on what the person types.

Holding commands during control does not undo state the agent set **before**
it asked for help, so the proxy also enforces section 5.4.4 at all times.

This is enforcement, not a request. An agent that ignores `swarm-handoff wait`,
or an escaped agent process, still cannot act on the browser or observe the
person's session while control is held, because nothing it can reach leads to
Chromium except the proxy, and the proxy never let it set up the state that
would observe the session later (section 5.4.4).

#### 5.4.4 State the agent sets before control: a method policy, always on

The attack: before calling `swarm-handoff`, the agent calls
`Target.createBrowserContext({proxyServer: <its own proxy>})` and
`Security.setIgnoreCertificateErrors({ignore: true})`, navigates to the sign-in
page in that context, then asks for help. Every hold and strip in section 5.4.3
applies only from the moment control starts; the sign-in POST still travels
through the agent's proxy, certificate errors are ignored, so the proxy can
terminate TLS with its own certificate and read the password. The same shape
works with `Page.setBypassCSP` (a page's own defences off) or
`Network.setExtraHTTPHeaders`. So the defence cannot be "pause the agent
during control"; it has to be "the agent can never set that state".

**The proxy carries a method policy, applied to every agent command from the
first one, not only in control mode.** It is an allowlist of the CDP domains
and methods Playwright's `connectOverCDP` uses (`Page`, `Runtime`, `DOM`,
`Input`, `Emulation`, `Accessibility`, `Target` attach and create-target, and
the read side of `Network`, `Fetch` and `Storage`), and anything not on it is
refused with a CDP error and recorded as `cdp_method_refused` with the method
name (never the parameters, which may carry page text). Inside the allowlist a
**named denylist** is refused regardless, because each one changes how the
browser trusts or routes traffic:

| Refused | Why |
|---|---|
| `Target.createBrowserContext` with `proxyServer` or `proxyBypassList` | routes the context's traffic, a sign-in included, through a server the agent chose |
| every `Security.*` method (`setIgnoreCertificateErrors`, `handleCertificateError`, `setOverrideCertificateErrors`) | lets a man-in-the-middle certificate pass, so TLS no longer protects what the person types |
| `Page.setBypassCSP` | turns off the site's own script defences on the page the person will type into |
| `Network.setExtraHTTPHeaders` | adds headers the agent chose to every request, the person's included |
| `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` with a behaviour other than `deny`, `default`, `allow` or `allowAndName` | a behaviour Chromium does not name; with a path outside `downloads/` it would write files where the gate, not the worker, reads them |

**A download path outside `downloads/` is confined, not refused** (LB-POLISH,
part of #1030). Playwright's `connect_over_cdp` sends
`Browser.setDownloadBehavior` with its own artifacts directory, a mkdtemp on
the client's side, for the default context; refusing it refused the whole
connection, and the QA agent on 2026-10-13 (task_1f28d5bfa5a14f618f83) fell
back to raw CDP. The gate now rewrites the path of an `allow` or
`allowAndName` to `downloads/` before the command is held or forwarded
(`cdppolicy.confine`), so Chromium still writes only where the worker reads,
and the agent's path never reaches Chromium.

**Puppeteer opens pages through the policy too** (DEVB-GATE, part of #1030).
dev-browser v1 drives Chromium with puppeteer-core 25.8.0, and its `newPage()`
fails outright when `Performance.enable` or `Audits.enable` errors
(`CdpPage#initialize`, `FrameManager.initialize`; the sequence for connect,
newPage, goto, evaluate and screenshot is pinned in
`tests/unit/worker/fixtures/puppeteer-core-25.8.0-*.json`). So:

* `Performance.enable`, `getMetrics` and `disable` are allowed: the domain
  reports counters, a name and a number each, and nothing of a page's content;
* `Audits.enable` and `Audits.disable` are **answered by the gate** with `{}`
  and never reach Chromium, and no `Audits.*` event reaches the agent
  whatever enabled it. `Audits.issueAdded`'s cookie details carry
  `rawCookieLine`, which the protocol definition (devtools-protocol
  0.0.1666840, checked 2026-10-14) describes as "the raw Set-Cookie header
  string" when no valid cookie could be made from it -- a header with its
  value, which would undo the cookie rule below (LB-7). The domain's other
  methods stay refused: `getEncodedResponse` returns a response body;
* `WebMCP.enable` and `Network.emulateNetworkConditionsByRule` stay refused,
  but Puppeteer catches both and goes on, so recording them on every page was
  noise (ANALYSIS issue 19). They are counted and logged at debug instead of
  recorded as `cdp_method_refused`; the action log still shows them as
  refused.

The allowlist is the control and the denylist is its documentation: a method
Chromium adds next year is refused because it is not on the list, not because
someone remembered to deny it. A Playwright call that needs a refused method
fails loudly in the agent, which is the intended outcome; L2 lists the
methods `claude-code-browser`'s own tests use, and nothing else is opened
without a change to this table.

**The gate refuses control on a certificate error.** The gate's own CDP
session watches `Security.visibleSecurityStateChanged` (the gate's session,
not the agent's, so the denylist above does not apply to it). Control is not
entered, and ends if already held, while the page being controlled is not
`secure`: a certificate error, an interstitial, or a plain-`http:` page on the
hand-off's `expect` host. The console says why ("this page's connection is not
secure; control was refused") and the episode is audited with that cause.
The Chromium policy `SSLErrorOverrideAllowed: false` (section 5.6) means the
person cannot click through an interstitial either. With the denylist in
force a certificate error should not be the agent's doing; the refusal is for
the case where it is the network's, or where a refused-method check was
missed.

**What this does not cover.** The agent still sees the session after return,
by design (below), and still chose where the person was sent, which is
section 5.7's problem, not this one's.

**After return the agent has the session, by design.** It can read the site's
cookies through CDP and act as the signed-in user. That is the delegation the
person is making, and the take-control dialog says it in one sentence: "The
agent will be able to act as the account you sign in with, on this site, until
the attempt ends."

### 5.5 The session left behind

* **Cookies and storage are never checkpointed.** The profile lives on a
  sidecar-only memory volume, never under `work/`, which is all the checkpoint
  archives (`CheckpointManager.create`,
  `apps/agent-worker/agent_worker/checkpoint.py::CheckpointManager.create`).
* **They are wiped at attempt end.** On every exit -- success, failure, park,
  cancel, fence -- the worker tells the gate `wipe` before the final
  checkpoint: Chromium is closed and the profile directory removed. A pod that
  dies without that loses the volume with the pod anyway.
* **An agent that copies the session into `work/` is refused.** An agent can
  write `storageState()` JSON or copy a cookie database into its workspace,
  where the checkpoint and the outputs would pick it up. The checkpoint filter
  and the output upload drop files that have the shape of a Playwright storage
  state (a JSON object with `cookies` entries carrying `name`, `value` and
  `domain`) or of a Chromium `Cookies` SQLite database, and record
  `browser_state_refused` with the path. That is a shape check, not a proof;
  an agent determined to smuggle a cookie out in another encoding can. Its
  agent instructions forbid it, and the residual is stated here rather than
  hidden.
* **The checkpoint still never holds a session.** LB-7 reuses sessions
  through the gate and Secret Manager (§5.5.1), never through `work/`, the
  checkpoint or the outputs, and so does a park's (§5.5.2). The two bullets
  above hold unchanged.
* **Encrypting the session into the checkpoint, so it survives a park, is the
  option turned down for phase 1 (Q7).** It would make a park cheap -- no
  second sign-in -- at the price of a long-lived session stored in the tenant's
  bucket, released to whichever attempt can present the attempt key. Most
  sign-ins worth automating (GitHub, Google) also bind sessions to device and
  address, so a restored jar often fails anyway.

### 5.5.1 Reusing a signed-in session (LB-7)

The owner revised Q7 on 2026-10-11. Prompting a person on every browser step
made a seven-step visual QA run against a signed-in site ask seven times. The
owner then scoped reuse to the person (same day). **A session is stored or
carried only in a personal tenant**, `u-<user>`, whose provisioning record is
the person's workspace ([workspaces.md](../workspaces.md)). The reason is
identity: every worker of a group tenant such as `eng` runs as the same
service account (`swarm-agent-worker-eng`), so no IAM grant can let Alice's
agents read a secret while refusing Bob's. A personal tenant's worker identity
is that person's alone, so IAM is the whole boundary, with no broker to trust.

The session is a cookie jar plus origin storage. It moves **gate to gate**: the
gate reads it on its own pipe session (§3.2) and writes it into the next gate's
context before the agent connects. The agent never holds it.

**In a group tenant: nothing is kept.** The gate wipes at attempt end (§5.5).
No secret is written, and "Save as login" is not offered. A task in a group
tenant that names `input.browser_login` is refused at submission with
`browser_login_personal_only`.

**Workflow carry (automatic, personal tenants only).**
* When a browser step of a workflow in `u-<user>` ends with a session its
  hand-off produced, the gate exports the jar with `Storage.getCookies` and the
  local storage of the signed-in origins. It does this before the `wipe` of
  §5.5, on its own pipe session.
* The worker stores the jar as one Secret Manager secret,
  `swarm-tenant-u-<user>-browser-wf-<workflow>`, with a 24-hour TTL. swarm-api
  creates that secret and grants it (see "Who creates the secret" below). Only
  that tenant's worker identity can read it.
* The next browser step of the same workflow imports it before the agent's
  first CDP connection.
* swarm-api deletes the secret when the workflow reaches a terminal state. The
  TTL is the backstop.

**Named logins (deliberate, personal tenants only).**
* After the person returns control on a task in their own workspace, the
  Browser tab offers "Save as login", with a name (for example `github-qa`) and
  an expiry (default 14 days, at most 30).
* The worker stores the jar as `swarm-tenant-u-<user>-browser-login-<name>`,
  in a secret swarm-api creates with the login's expiry.
  swarm-api keeps its metadata (site, saved at, expires, last used) in its own
  collection `browser_logins`, keyed by tenant. That collection belongs to
  swarm-api, as `live_sessions` does, not to the frozen models.
* A task in that workspace opts in with `input.browser_login: "<name>"`: a
  name, never cookies, as invariant 10 asks of every caller choice. An unknown,
  expired or revoked name fails the task before the agent runs.
* Access › Browser logins lists only the signed-in person's own logins, never
  with a value, with Revoke. Revoke destroys every secret version.
* When a login's site answers with a sign-in page anyway, the gate raises a
  normal hand-off. The new sign-in refreshes the stored login (a new version),
  so an expiry costs one prompt, not one per task.

**Whose account (owner's answer).** Any account may be saved, at the saving
person's judgement, including a personal one. The save dialog states that every
agent the person runs in their workspace can act as that account on that site
until the login expires or is revoked.

**Who creates the secret, and who is granted on it (owner decision
2026-10-11, lane L8c).** swarm-api provisions each browser-session secret and
grants it per secret. Before the worker writes a session it asks swarm-api for
the slot: `POST /v1/tasks/<task>/browser-session/slot` with the secret's id,
signed with the attempt key and carrying the attempt's fencing generation, as
the other worker routes are. swarm-api:

* refuses any tenant that is not personal (`kind = "user"` and an id starting
  `u-`, whose principal derives to that id);
* refuses any name outside that tenant's own
  `swarm-tenant-<tenant>-browser-wf-<this task's workflow>` or
  `-browser-login-<name>`, before it calls Secret Manager;
* refuses a stale generation, and creates nothing;
* creates the secret (a carry with the 24-hour TTL, a login with its record's
  expiry, labelled `managed-by=swarm-api` and the tenant);
* sets that one secret's IAM policy to exactly `secretAccessor` and
  `secretVersionAdder`, each to `swarm-agent-worker-<tenant>` alone.

A secret that already exists is re-granted only if its `swarm-tenant` label is
the caller's. The worker then only adds a version. **No worker holds a
project-level grant for a `-browser-` secret, and no worker holds
`secrets.create`.** swarm-api holds `swarmBrowserSessionProvisioner`
(`terraform/bootstrap/browser_logins.tf`).

Why not the alternatives:

* **Terraform cannot name a person.** A person's workspace is made by the
  workspace job, outside Terraform ([workspaces.md](../workspaces.md) §3.2).
  L8b's per-worker project grants therefore reached only the `kind = "user"`
  tenants the release's tfvars names, and none of the people who use the
  feature.
* **A project-level create cannot be narrowed by name.** Secret Manager
  checks `secrets.create` against the project, and the project carries no
  secret name. L8b's unconditioned worker create let every personal worker
  create an empty secret anywhere in the shared project.
* **A `hasOnly()`-bounded project IAM grant on the workspace job would bound
  roles, not members.** Granting each person's worker at project level would
  need project `setIamPolicy`. `hasOnly()` limits WHICH roles the job may
  grant, never TO WHOM or under what condition.

What the provisioner's IAM condition can and cannot narrow. The condition is
`resource.type == "cloudresourcemanager.googleapis.com/Project" ||` the
revoker's own name test (`swarm-tenant-u-` and a non-empty tenant before the
first `-browser-`, by `extract()`). `secrets.get`, `secrets.getIamPolicy` and
`secrets.setIamPolicy` are checked against the secret, so the name test narrows
them to personal `-browser-` secrets. `secrets.create` is checked against the
project, where the name test can never hold. Without the project arm, create
would always be refused. With it, create is not narrowed: swarm-api can create
an empty secret with any name. That residual is swarm-api's, where every
worker used to hold it. swarm-api's own name check before the call is what
keeps create inside the browser names. Not verified live: that IAM evaluates
create with that resource type.

**What protects it, and what does not.**
* IAM. The only grant on a `swarm-tenant-u-<user>-browser-*` secret that
  reaches a worker is set on that secret, to `swarm-agent-worker-u-<user>`
  alone (lane L8c). No worker identity holds any project-level `-browser-`
  grant. Two residual readers remain. Platform admins with project-level
  Secret Manager roles can still read these secrets, the same residual as
  every other secret in the project. And `swarm-api` holds no direct
  `versions.access`, but its `secretmanager.secrets.setIamPolicy` on personal
  `-browser-` secrets (the provisioner role, `browser_logins.tf`) lets it
  grant read to any member, itself included, so it can make itself or anyone a
  reader of every personal `-browser-` secret. The name condition limits which
  secrets, not whom it may grant. Detection is deferred to the security epic
  [#476](https://github.com/bogdan-alexandrescu/SwarmCloud/issues/476#issuecomment-6114767079):
  an alert on `SetIamPolicy` whose new bindings name anything other than
  `swarm-agent-worker-u-*`.
* The agent's CDP cannot read or plant cookies. The method policy of §5.4.4
  refuses:
  - `Network.getCookies`, `Network.getAllCookies` and `Storage.getCookies`;
  - `Network.setCookie(s)` and `Storage.setCookies`;
  - `Network.clearBrowserCookies`;
  - `Storage.clearDataForOrigin`.

  Each refusal records `cdp_method_refused`.
* **Not covered, stated plainly:**
  - A cookie without `HttpOnly` is readable by page script, which the agent
    can run with `Runtime.evaluate`.
  - An agent driving a signed-in browser can act as the account whatever
    happens to the cookies.
* The values never reach a log, an event, a checkpoint, an output or the
  console. The audit (§5.8) records `browser_login_saved`, `_used`, `_refreshed`
  and `_revoked`, with names only.
* Secrets follow the forge-token rule: Secret Manager only, read at run time.

User-scoped forge tokens and provider keys are a separate question (they are
per tenant today), filed on its own issue.

### 5.5.2 The browser across a park: tabs, and storage under LB-7 (issue #1362)

MEASURED 2026-10-15 (QA batch full-1015, `u-bogdan`): S5-resume failed 6 of 6,
three on Playwright MCP and three on dev-browser. Part one opened
`https://example.com/?qa=<nonce>`, set `localStorage.qa_state`, asked for a
hand-off and parked; the task resumed as attempt 2 from `ckpt-00001`, and the
new browser had only `about:blank`.

**Why nothing came back.** Not Chromium's session files, and not the restore
order: there was nothing to restore. The profile directory is
`SWARM_BROWSER_PROFILE_DIR` on a sidecar-only memory `emptyDir` of the pod
(§3.1). The park sends `end_control` with `wipe` before its checkpoint
(`HandoffPath.end`, `LiveGate.wipe`), which closes Chromium and empties that
directory, and the checkpoint archives only `work/`
(`CheckpointManager.create`). The resumed attempt is a new pod whose volume
starts empty, and the gate launches Chromium with
`--user-data-dir=<that volume>` and `about:blank` and no
`--restore-last-session` (`livegate/__main__.py::chromium_args`). Chromium's
`Sessions/` files, `Current Session` and `restore_on_startup` were therefore
never in play. Making them so would mean checkpointing the profile, and the
profile IS the session that §5.5 keeps out of every checkpoint. So the platform
keeps what a resumed agent needs itself, and Chromium's own session restore is
not used.

**What a park keeps.** In `HandoffPath.end`, when a hold ends with
`expired` (the park of §4.3), before the wipe, and only on a gate this worker
registered with a protected heap (`agent_worker/browser_resume.py`):

* **The tabs, in every tenant.** The gate's `tabs_export`
  (`livegate/tabs.py`) lists the page targets in the order Chromium lists
  them, each `{url, active}`. The active one is the tab the page follower
  shows. The worker writes the list to `$SWARM_HANDOFF/browser-tabs.json`, so
  it travels in the checkpoint with `work/`. A URL is the agent's own working
  state: it navigated there, and the list goes back to nobody but the same
  task's next attempt. That Chromium lists pages in tab-strip order is not
  measured. A tab dragged elsewhere comes back where Chromium lists it.
* **The site storage, in a personal tenant only, under LB-7's rules.** The
  gate's `session_export` with `scope: park` covers cookies of every context,
  plus the local storage of the open https origins. Unlike a carry, it needs
  no returned hand-off: the jar goes only to the same task's resumed
  attempt. The worker asks swarm-api for the slot
  `swarm-tenant-u-<user>-browser-park-<task>` (`browser_slots.py`: this task's
  own name only, 24-hour TTL, granted to that tenant's worker alone) and adds a
  version. The TTL is its only delete. A hand-off park is dead-lettered after
  `human_park_max_seconds` (24 hours by default), so the secret outlives no
  park that could still resume. A group tenant keeps its tabs and no storage,
  for §5.5.1's reason.

**What is not kept or reopened** (`livegate/tabs.py::refusal`, applied by the
gate at export and again by the worker and the gate on the way back, since
the file is in the agent's reach):

* anything but http(s);
* a URL with userinfo, or one over 2,048 characters;
* a URL whose query or fragment names a credential or a sign-in's state
  (`token`, `code`, `state`, `session`, ...), or that the redaction filter
  would change;
* any URL on a host of the mask's challenge list (`mask.SHOWN_IN_CONTROL`:
  reCAPTCHA, hCaptcha, Cloudflare's challenge), at any path. A challenge is a
  person's to solve, and a stale one reopened would only fail.

A sign-in page is reopened like any other page. Nothing here forbids it: it is
the page the agent was on, the mask still covers its credential fields, and
the hand-off ledger tells the agent its sign-in was NOT DONE.

**What a resumed attempt gets.** In `HandoffPath.prepare`, after the gate's
registration and before the runner starts, on an attempt whose spool holds an
EARLIER attempt's tab list:

1. the park secret's cookies go to `session_import`;
2. `tabs_restore` opens each tab in order and brings the active one to the
   front. It closes the `about:blank` Chromium started on.

Both are refused by the gate once an agent has connected. The platform opening
a URL here is not the agent navigating: the agent does not exist yet. Local
storage goes WITH ITS TAB. Chromium's `DOMStorage` writes an origin's storage
only through a frame of that origin, so a blank page cannot take it.
For the first tab of each origin with carried storage, the gate therefore opens
the tab on `about:blank`. It adds a script to every new document in an
isolated world of its own session, then navigates. The script writes the
items when the URL's document is created, before any of the page's scripts
run. The gate's session is then detached, which removes the script, so a
later navigation of the agent's is untouched. The whole restore fits in one
`ctl.sock` call (7 s of the client's 10). A tab still loading at the end keeps
its page and loses its storage, recorded as `storage_skipped`.

The tab list is removed once read, so a later retry does not reopen a park it
did not make. The resume summary (`resumed.md`, §4.3) names the tabs that came
back, in order, marks the one in front, and says whether sign-ins and storage
came back.

**IndexedDB is not carried.** CDP's `IndexedDB` domain reads and deletes but
cannot write. A restore would mean replaying object stores from a page script
before the page's own scripts open the database, and that cannot be ordered
reliably. It is left for the owner to decide, not done quietly.

**Artifacts are not part of this, by design.** A parked attempt uploads its
`$SWARM_ARTIFACTS_DIR` at the park, and the attempt that finishes lists those
files by reference (`carried_from`; [workflows.md](../workflows.md), #166).
The artifacts directory is not checkpointed, so a resumed attempt starts with
an empty one ([checkpointing.md](../checkpointing.md)). The S5 run's
`qa-phase1.txt` missing from attempt 2's disk is that rule working, not a
lost file. The resume summary now says so, so an agent does not take the
empty directory for lost work.

### 5.6 Keyboard, clipboard and the browser itself

* **Clipboard.** Pod-to-console clipboard is **off**, always: it would hand a
  viewer whatever the agent copied, unmasked. Console-to-pod paste is **on
  only in control mode** (pasting a password from a password manager is how
  most people sign in), and is injected as typed keys, so it is masked and
  unlogged like typing (Q10).
* **Keyboard.** Input is injected only from the controller's connection and
  only in control mode; key events from viewers are discarded at the gate.
  The console captures keys only while the canvas has focus, so a person's
  typing elsewhere in the console never reaches the pod.
* **Chromium policy** (`/etc/opt/chrome/policies/managed/` in the sidecar):
  `URLBlocklist` for `file://*`, `chrome://*` (but `chrome://newtab`),
  `devtools://*`, `view-source:*` and the cluster-internal names
  (`*.svc.cluster.local`, `swarm-system`); `DeveloperToolsAvailability: 2`;
  `PasswordManagerEnabled: false`; `AutofillAddressEnabled` and
  `AutofillCreditCardEnabled: false`; `FullscreenAllowed: false` (section 5.7);
  `SSLErrorOverrideAllowed: false` (section 5.4.4);
  `BrowserSignin: 0`; `SyncDisabled: true`. A person in control therefore
  cannot read the sidecar's files, open devtools on the agent's pages, or
  save a password into a profile that is about to be wiped anyway.
* **Network reach** is the pod's: the internet minus every private range, and
  the metadata server, which answers only requests carrying
  `Metadata-Flavor: Google`, a header a navigation cannot set
  (`kubernetes/network-policies/allow-egress.yaml`). The new rule of section 8.3 opens
  only the relay's port; the relay answers nothing without a ticket.

### 5.7 Phishing through the agent's browser

The attack: a page the agent read tells it to go to `github-login.example`,
which looks like GitHub, and to ask for help signing in. The person sees a
GitHub sign-in page in their own trusted console, and types their password.

What stands in the way:

1. **The origin is shown from Chromium, not from the frame.** The console's
   strip shows the top frame's origin as the gate reads it over CDP, outside
   the canvas, with the registrable domain emphasised and any punycode shown
   decoded and flagged. A page cannot draw into that strip.
2. **The hand-off names the site it expects.** `--expect github.com` is stored
   with the request. When the top frame's registrable domain is not the
   expected one, the strip turns red and Take control asks for a second
   confirmation that names both domains. A hand-off with no `expect` shows a
   warning that the agent did not say which site it means.
3. **The address bar is real and stays on screen.** Chromium draws it, and
   `FullscreenAllowed: false` stops a page from going fullscreen to draw a
   fake one.
4. **Not over plain HTTP, not to an address.** The gate refuses to enter
   control mode while the top frame is `http:` or its host is an IP literal;
   the console says why.
5. **A submitter may pin allowed sign-in hosts** for the task (a list on the
   task, optional): a hand-off whose `expect` is not on it is refused before
   anyone is asked.

What remains: a person who confirms past a red warning. The take-control dialog
recommends a purpose-made account for any site an agent signs in to.

### 5.8 The audit trail

`live_audit/{id}`, append-only, written by swarm-api in the **same
transaction** as the change it records, as `admin_audit` is
(`apps/swarm-api/swarm_api/admins.py`, "THE AUDIT IS APPEND-ONLY"). Entries:
`session_opened`, `viewer_joined`, `viewer_left`, `handoff_requested` (reason,
redacted), `control_taken`, `control_returned`, `control_ended` (cause:
`idle`, `renew_lapsed`, `preempted`, `membership_lost`, `cancelled`,
`hold_expired`), `control_opened_to_members`, `handoff_resolved`,
`session_closed`. Each names the task, attempt, generation, the person's email,
and counts (keys, pointer events, duration), never content. The task's own
event stream carries the hand-off and control transitions so the console's
timeline shows them; that needs new `EventType` members (request LB-B). The
audit is readable by members of the task's tenant through the task routes, and
by nobody else.

### 5.9 What the agent sees of what the person did

`swarm-handoff wait` exits 0 with:

```json
{"outcome": "returned", "held_seconds": 192, "url": "https://github.com/", "title": "GitHub"}
```

`url` is the top frame's origin and path, redacted, with no query. Not who
the person was, not what they typed, not a frame. The agent can look at the
page itself afterwards, which is the point; it cannot see the sign-in that put
the page there (section 5.4.3).

### 5.10 Rate limits

| What | Limit (default) | Where enforced |
|---|---|---|
| Viewer tickets per person | 30 a minute | swarm-api, per principal (`TokenBucketLimiter`, `apps/swarm-api/swarm_api/ratelimit.py::TokenBucketLimiter`; in-process, so N instances allow N times this, as that module records) |
| Concurrent viewers per task | 10 | relay (single replica, so the count is global) |
| Concurrent viewers per tenant | 50 | relay |
| Take-control attempts per person per task | 6 a minute | swarm-api |
| Hand-off requests per attempt | 3 | worker, and swarm-api from the task document |
| Controller input events | 200 a second | gate (drops above) |
| Frame rate | 10 fps with a viewer, 1 fps recording-only | gate |

### 5.11 Abuse cases

| Case | What stops it |
|---|---|
| A member of tenant A watches or controls tenant B's agent | §5.2 step 2 on every connection; the relay refuses a ticket whose tenant is not the session's; a refusal is a 404. |
| Replaying a captured stream or ticket | TLS from the console to swarm-api's load balancer; tickets one-use, 30-60 s, bound to session, person, role and generation, and refused after their session closes. Past the load balancer the hops are plain ws inside the VPC (decision (b), section 3.3): only swarm-api's egress range and the tenant gates may connect to the relay, and a connection without a fresh ticket is closed. Capturing those hops needs a foothold inside the VPC; that is the accepted cost of having no certificate on the relay. A recording is a stored object behind the same tenant check, not a re-stream. |
| A stale worker (old generation) keeps streaming | The ticket carries the generation; swarm-api closes the session when the lease's generation moves (invariant 5) and POSTs the relay's `/internal/close`, and the relay drops the tunnel and its viewers (section 5.2). |
| The agent keylogs the person | No display access (§3.1); CDP held and scripts removed during control (§5.4.3); postData stripped after. |
| The agent sets traffic-routing or trust state before asking for help (a context with its own `proxyServer`, `Security.setIgnoreCertificateErrors`, `Page.setBypassCSP`, extra headers), so the sign-in passes through it | The proxy's method policy refuses those calls from the first command, not only in control (§5.4.4) -- since 2026-10-13 the only layer on the agent's own tab, which is now the one taken by default (§5.4.3); the fresh page, when asked for, opens in a gate-owned context; the gate refuses or ends control while the page has a certificate error. |
| The gate restarts mid-attempt and the agent registers on `ctl.sock` first | A restarted gate accepts no registration; the live path ends for that attempt (§5.4.2). |
| The agent or an escaped process drives the browser during control | The proxy holds every command; there is no other path. |
| The agent spams hand-offs to pester people or hold the pod | 3 per attempt; each hold bounded; the park gives capacity back. |
| A controller sits on control to hold capacity | Idle 300 s and renewal 60 s end control; the hold's deadline then applies. |
| Phishing through the agent's browser | §5.7. |
| A person in control reads the pod's files or the agent's environment | `file://`, devtools and `chrome://` blocked; Chromium runs as a uid that cannot read the worker's `/proc` or `work/`. |
| A person exfiltrates data through the pod's network | They are a member of the tenant whose agent it is, with the egress any of its agents has. Not a boundary this feature moves. |
| A hostile page attacks the console | noVNC draws pixels; no page content runs in the console's origin (§3.4). |

---

## 6. The console

The owner chose this presentation (LB-5) from five in-console alternatives. The
mockup of the chosen design is the owner's private artifact "Browser Tab and
Theater", linked from #1030. In summary:

* **A Browser tab on the agent's view** (`AgentSplit.tsx`, the underline tabs
  Details, Logs, Changes and so on). It sits before Logs, and it is the tab a
  running browser agent opens on, as Logs is today. It holds the noVNC canvas
  (§3.4), the origin strip and the controller. Its toolbar holds the device
  switcher (§6.1), rotate, touch, a **logs toggle** that splits the tab into
  logs beside the canvas, and **⤢ Theater**. Take control sits in the agent
  header beside Stop. The view is read-only until Take control.
* **Theater mode**, opened from ⤢ (or the key `T`), from a graph card, or from
  the hand-off banner. It shows the same session full screen:
  - a left column that switches between Timeline (the agent's actions) and Logs;
  - the canvas in the middle;
  - a right column with the device picker and the other live agents, with
    prev and next;
  - a bottom bar with replay over the session's recording (§7) and Take control.

  Esc returns to the tab at the same scroll. Theater is a view, not a second
  session: it reuses the tab's tunnel and ticket. On a phone, ⤢ goes native
  full screen, landscape preferred.
* **The hand-off** when `human_wait` is set:
  - a yellow banner across the Browser tab, with the reason, the expected site,
    the time left, Take control, and Open in theater;
  - a dot on Agents in the rail and a yellow row in the agent list;
  - the banner on Overview's "Needs a look" (`apps/swarm-ui/src/Overview.tsx`).

  On a parked hand-off the banner reads "I'm ready to sign in", which calls
  `human-ready` and opens the tab. In the theater, the left column becomes the
  hand-off checklist.
* **The workflow graph** (`WorkflowViews.tsx`). A running browser step's node
  expands an inline card with a low-rate (about 1 fps) live thumbnail. The card
  carries Open Browser tab and ⤢ Theater. In the theater, prev and next step
  through that workflow's live steps.
* **All live browsers**: a **Live** filter on the Agents list, with a device
  thumbnail per row and needs-you first. "Theater all" opens the theater with
  prev and next across them. There is no new nav tab.
* **On the person's own phone**, the console's existing two-page phone layout
  carries the same Browser tab. Take control works by touch, and the device
  switcher sits under the tab's ⋯ menu.
* **Recordings** (§7) on the attempt's view, as a player over the masked
  segments, for tenant members. Theater replay reads the same segments.
* No new nav tab: everything lives inside existing views. So the issue forms'
  "Where" list (`tests/unit/scripts/test_issue_forms.py`, reading `SECTIONS` in
  `apps/swarm-ui/src/App.tsx`) does not change.

#### Console decisions, 2026-10-12

A visual QA on 2026-10-11 found the shipped console diverging from the mockup.
The owner then went through the comparison ("Live Browser: Mockup vs Built",
2026-10-12 00:30 PDT) item by item. These are the 30 answers. They are
recorded here so a later lane does not revert one as a "fix". Where this list
and a bullet above disagree, this list wins.

**Matched to the mockup (26):**

1. The agent header reads "Copy link · Take control · Stop", with Stop capitalised.
2. The tabs are Details, Browser, Logs, Changes, Attempts, Artifacts. Browser
   comes second, not first.
3. The Browser tab carries a small "LIVE" marker while the session is open.
4. Browser is the default tab while a browser agent runs.
5. Browser has its own address: `/agents/<tab>/<id>/browser`.
6. The device switcher is chips, "Desktop | iPhone 15 | Pixel 8 | iPhone SE |
   iPad", with one selected. It is not a select.
7. Rotate reads "⟲ rotate".
8. Touch reads "touch: on" or "touch: off".
9. "◫ logs" splits the tab: logs on the LEFT, the device on the right.
10. "⤢ Theater" sits at the right of the toolbar.
11. Under the device frame there is a caption: "<device> · <W>×<H> · <path>".
12. The origin strip stays above the canvas.
13. The theater's top bar holds "LIVE <agent>", the device chips, "⟲ rotate",
    "‹ prev agent", "next ›" and "Esc · back to tab".
14. The theater's left column switches between Timeline and Logs.
15. The theater's right column holds the DEVICE list, then OTHER LIVE agents
    with device thumbnails.
16. The theater's bottom bar holds replay over the recording, and the control
    button.
17. The theater opens from ⤢, the key T, the graph card, the hand-off banner
    and the URL `?theater=1`.
18. A hand-off draws ONE yellow banner across the Browser tab. It shows the
    reason, the expected site and the time left, with [Take control] and
    [Open in theater]. Other tabs keep the strip, with the same two buttons.
19. A parked hand-off reads "I'm ready to sign in". It calls `human-ready`,
    then opens the Browser tab.
20. A hand-off is a yellow row in the agent list.
21. The hand-off banner also appears on Overview's "Needs a look".
22. In the theater, the left column becomes the hand-off checklist: ✓ opened
    login, ✓ you took control, the current step, and return control. It ends
    in a green "I'm done, return control" button.
23. On the workflow graph, a RUNNING browser step's node expands an inline
    card. The card has a live thumbnail at about 1 fps, "Open Browser tab" and
    "⤢ Theater".
24. A theater opened from a workflow uses prev and next to step through that
    workflow's live steps.
25. The Agents list has an "All | Live N" switch, a device thumbnail per row,
    needs-you first and "⤢ theater all". There is no new nav tab.
26. On a phone, the two-page layout carries the same Browser tab. Take control
    works by touch, the device switcher sits under the tab's "⋯" menu, and ⤢
    goes native full screen, landscape preferred.

**Kept as built (3):**

27. The "Let any member take control" checkbox stays in the Browser tab's
    toolbar, after ⤢ Theater. (Since 2026-10-13 it can be unticked, and
    ticking it asks first: §5.1.)
28. The rail has no needs-you dot. This supersedes the "dot on Agents in the
    rail" in the hand-off bullet above.
29. "Save as login" keeps its current offer logic, unchanged
    (`BrowserLogins.tsx`, §5.5.1).

**Added (1):**

30. The Overview "Needs a look" hand-off card also carries [Take control].

### 6.1 Mobile devices (LB-6)

The browser can be a phone or a tablet, for visual QA as a mobile browser and
for sign-ins that only work on mobile.

* **The gate applies the device, not the agent.** The gate owns Chromium's pipe
  (§3.2), so it sets the device on the gate-owned context with
  `Emulation.setDeviceMetricsOverride` (width, height, device scale factor,
  `mobile`), `Emulation.setUserAgentOverride` (the preset's user agent and
  client hints) and `Emulation.setTouchEmulationEnabled`. It also resizes the
  virtual display to the preset's frame, so the frames carry no letterbox. The
  agent's own CDP calls to these methods go through the method policy (§5.4.4).
  An agent picks a preset through the gate (`swarm-handoff device <preset>`),
  never by sending metrics of its own.
* **The presets** are a fixed, named list in the gate:
  - `desktop` 1440×900
  - `iphone-15` 393×852 @3
  - `phone-390` 390×844 @3 (QA-KIT, 2026-10-19; amendment below)
  - `pixel-8` 412×915 @2.625
  - `iphone-se` 375×667 @2
  - `ipad-7` 810×1080 @2

  Each has a portrait and a landscape orientation. A name outside the list is
  refused. A caller never sends dimensions, which keeps invariant 10's spirit:
  a choice by name, never a parameter.
* **Touch from the console.** When the preset is mobile and the person in
  control has touch on:
  - the gate turns the controller's pointer events into
    `Input.dispatchTouchEvent`: press, move and release become touchStart,
    touchMove and touchEnd;
  - a two-finger gesture on a phone becomes a pinch;
  - a wheel becomes a touch scroll.

  The RFB key rules (§5.6) are unchanged. In view mode, no input is sent at all.
* **Masking (§5.3) is device-independent.** It reads element boxes from the
  page in CSS pixels and scales them by the device scale factor. A test runs
  every preset (lane L3b).
* **Who switches the device** is question Q13. The recommendation is the agent,
  plus the person in control. A viewer's switcher shows the current device and
  is disabled.

**Amendment, 2026-10-19: the QA kit (QA-KIT, part of #1649).** The owner
decided on 2026-10-19 that a live-browser agent should be able to run a visual
QA sweep at 1440 and 390, light and dark, and that the model must see its
screenshots. Four things changed. docs/design/visual-qa.md §4.1 and §6 say what
QA-CAPTURE builds on them.

* **`phone-390`, 390×844 @3**, joins the list after `iphone-15`, with iPhone
  15's mobile flags, user agent and client hints. 390 is the width a QA sweep
  names (the iPhone 12-14 viewport), and none of the five presets was 390. The
  list is written out in three places: the gate (`livegate/presets.py`),
  swarm-api's restatement (`swarm_api.live.LIVE_PRESETS`) and the console's
  labels (`LiveBrowser.tsx`, `Theater.tsx`, `TheaterTimeline.tsx`, "Phone
  390"). A test holds the gate's and swarm-api's lists equal by name and size
  (`test_live_wire_shapes.py`), because a copy that drifts shows a preset the
  gate refuses, or hides one it has. Its display fits in the Xvfb already
  started for the desktop preset, so nothing about the sidecar changed.
* **The theme: `swarm-handoff theme dark|light|system`.** The gate sends
  `Emulation.setEmulatedMedia` with the one feature `prefers-color-scheme`, on
  the same per-page sessions that hold the device. That is why it holds:
  - across the page's navigations, because the override belongs to the target;
  - on a page opened later, and on a page whose session went (a renderer
    swap), which is attached again with the device and the theme together;
  - across a device switch.

  `system` lifts the override. The name is checked three times, in the
  command, by the worker and by the gate, and those three lists are held equal
  by a test. Anything beside the name is refused, so the agent never sends a
  media feature, which keeps invariant 10 in spirit, as a preset does. On an
  attempt without the live path it is refused as a device is: exit 5 with the
  reason. It is recorded the way an agent's device change is recorded: the
  gate's record writes `theme_set`, `theme_refused` or `theme_failed` with the
  name, and the worker logs `browser theme set`. Only the agent can set it.
  The console has no theme control, and the gate does not report the theme.
  Not measured: which session's override Chromium shows when the agent also
  calls `Emulation.setEmulatedMedia`, which the method policy allows on its
  own sessions. So the instructions name the command, not the method.
* **The agent's instructions name both commands**, one line each. They list
  every preset with its size, built from the gate's list. They say that
  `browser_resize` is refused and why: it sends
  `Emulation.setDeviceMetricsOverride`, which the method policy denies,
  because the gate owns the size so that the person watching sees the device
  the page is laid out for.
* **Screenshots land in the artifacts, and the model sees them.** Playwright
  MCP is registered with `--output-dir $SWARM_ARTIFACTS_DIR/screenshots`, so a
  `browser_take_screenshot` taken without a filename is saved there and
  uploaded. The console draws images up to 20 MiB inline. The following was
  read from @playwright/mcp 0.0.83's own code (playwright-core
  1.64.0-alpha-1790635538000), not measured on a live run:
  - a screenshot comes back to the model as an image only when it was taken
    without a filename, so the instructions ask for none;
  - a screenshot given a filename is resolved against the workspace root,
    which is the checkout;
  - `--image-responses` defaults to `allow`, and nothing here sets it;
  - with no `--output-max-size`, nothing is evicted before the upload.

  The runner counts, from the CLI's stream, the screenshot calls and those
  whose tool_result carried an image block, and writes the counts to its
  result as `browser_screenshots` (`taken`, `seen_as_image`, `complete`).
  `complete` is false when the stdout capture was cut. That a live run's
  stream shows the image block has not yet been seen on a deployed run. Each
  base64 image makes the stream larger, so a long sweep can pass the
  transcript's 4,000,000-character cap. The transcript is then skipped (the
  stdout capture stays whole up to its own cap), and QA-CAPTURE should expect
  that.

---

### 6.2 The console's wire (LB-SEAM, 2026-10-12)

This design names the fields of a live session, not their JSON, and lanes L9
(swarm-api) and L11 (the console) filled that gap differently and in parallel.
Measured on 2026-10-11, the session read answered `{"available": true, ...,
"you": {"may_take": ...}}` while the console read `live_browser` and
`session`, so a working live session drew **no Browser tab**. The return
button sent `DELETE .../live/control` to a route served only as `POST
.../control/return`, the take's `confirmed_mismatch` was dropped unaudited,
and recordings, the action timeline and the card thumbnail called routes
nothing served.

**Where the design is silent, the console's types are the wire**, because
they are what the screen draws (`LiveRead` in `apps/swarm-ui/src/api.ts`).
swarm-api states the same wire as `response_model`s (`swarm_api.live`), and
`tests/fixtures/live/*.json` holds one example per route that **both** suites
read: pytest validates each against the route's model and compares the real
routes' answers with it, and vitest feeds it through the console's own loaders
(`loadLiveSession`, `takeLiveControl`, `loadRecordings`, ...). A route the
index names that either side does not handle fails both. The path-only check
in `test_runtimes_screen.py` could never see a shape.

**The origin and the device are the gate's report (LB-DEV-A, 2026-10-12).**
LB-SEAM served both as null because nothing carried them to swarm-api. Now
the gate reports them over the tunnel it already holds
(`agent_worker/livegate/report.py`, tunnel op `REPORT`): the top frame's
origin, built from the URL Chromium committed and the registrable domain
Chromium computed from its own public suffix list (`domainAndRegistry`),
whenever the page on screen changes or its top frame commits a navigation,
and the device -- preset name, orientation, touch, never a size -- once
applied, at start and on every change. Path, query and any user name or
password in the URL never leave the gate. The relay keeps the latest report
per session in memory and passes it as text to every viewer link (after
`ready` to one that joins later); it may not write Firestore (decision (a)),
so **swarm-api writes it**: the viewer link is swarm-api's own proxied
socket, which checks every field against the others (scheme, host, port,
registrable domain a suffix of the host, punycode that encodes back to the
host, no control or bidi characters, a preset by name with its own touch)
and records it on `live_sessions/{id}` as `origin`, `device` and
`reported_at`. The session read checks the stored value again before serving
it. What this does not do, stated plainly: the document is written only while
someone watches (a viewer link is the only path in), so a session nobody has
opened shows null until the first viewer connects -- the console's own view
connects at once, and the next read carries it. `cert_error` is served null
(unknown): the gate's page follower does not watch the security state; the
gate still refuses control on a page that is not secure (§5.7 item 4).
`presets` is the gate's fixed list (section 6.1), restated in swarm-api and
held equal to `agent_worker.livegate.presets` by a test. A recording's
`started_at` is its object's write time, which is the END of the minute it
holds; the relay records no start.

**The display could not be resized at all before LB-DEV-A.** Xvfb starts
with one CRTC driving a mode as large as the whole screen (the largest
preset's), and RandR refuses (BadMatch) a screen size smaller than an active
CRTC, so the gate's bare `RRSetScreenSize` failed for every preset -- the
default `desktop` first, measured 2026-10-11 as `DisplayError` on
task_8cf26eb6e1f94e85adf8. `XRandrDisplay` now does what `xrandr` does:
a mode of the frame's size (created once, by Xvfb's own `<w>x<h>` name),
CRTC off, screen size, CRTC on in the new mode. The unit test drives every
preset in both orientations against an X server modelled on Xvfb's RandR;
no real Xvfb ran in that lane.

**The thumbnail's socket is the session's, ticketed.** The workflow card
opened `/v1/tasks/{task_id}/live` with no ticket. Section 5.2 refuses that, so
the console was changed, not swarm-api: the card reads the session, mints a
viewer ticket and opens `/v1/live/{session_id}/stream` like the tab does.

## 7. Recording and replay (LB-4)

**What is recorded.** The masked frames, and nothing unmasked, because nothing
unmasked exists outside the sidecar. The **relay** records, not the pod: it
already receives every masked frame, and a recording written by a platform
identity cannot be forged or padded by the agent, which shares the tenant's GSA
with everything else in the pod.

* `tenants/<t>/tasks/<task>/attempts/<a>/live/rec-<seq>.live.webm`: one-minute
  VP8 segments, 1 fps when nobody is watching and up to 10 fps while someone
  is (Q11 asks whether to record when nobody watches at all).
* `tenants/<t>/tasks/<task>/attempts/<a>/live/actions-<seq>.live.jsonl`: one
  line per agent CDP **method** the proxy forwarded (`Page.navigate` with the
  redacted URL; `Input.insertText` as a length, never text), and one per
  control event from the audit. Never a key, never a parameter that could hold
  typed text.

**Retention: 7 days, by a lifecycle rule of its own.** The artifact bucket's
Delete rule is keyed on `customTime` and keeps artifacts
`artifact_retention_days` (180 in prod) (`terraform/modules/storage/main.tf`,
"Artifacts and logs expire on a clock; checkpoints do not"). A
`matches_suffix = [".live.webm", ".live.jsonl"]`, `age = 7` Delete rule
removes recordings first, whatever their `customTime`. A suffix, not a prefix,
because a prefix rule cannot match the middle of `tenants/<t>/.../live/`. The
relay's service account holds `storage.objects.create` on the artifact bucket
under an IAM condition limiting it to names that contain `/live/` and end in
those suffixes, and nothing else: no read, no delete.

**Viewers.** Members of the task's tenant, through a swarm-api route that
checks the tenant as §5.2 does and streams the object. Not admins outside the
tenant. The 7 days are the same for every tenant; a tenant-specific retention
is not in phase 1.

---

## 8. Platform fit

### 8.1 GKE only

`claude-code-browser` runs on `GKE_AUTOPILOT` (lane PROFILE). The browser image
does not run on Cloud Run here: the `browser` profile is GKE-only because
Chromium needs a sized `/dev/shm`
(`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`, the `browser` entry), and this design
adds a native sidecar and a second uid on top. swarm-api refuses a live session
for a task whose resolved backend is not `GKE_AUTOPILOT`, and
`claude-code-browser` must not be added to the Cloud Run fallback list
(`cloud_run_fallback_profiles`, `terraform/infra/locals.tf`), because a fallback would silently run a
profile whose hand-off cannot work.

### 8.1.1 How the relay ships (lane LB-L8e)

Lanes L7 (the relay), L8 (its infrastructure) and L9 (swarm-api's routes) were
built in parallel and disagreed on the relay's wire. The owner settled it on
2026-10-11 (~14:35 PDT), and section 3.3 records the decisions in full
(lane L8d). The three this deploy is built on, each with its reason:

* **(a) The relay trusts a ticket, never Firestore.** swarm-api mints every
  ticket under `swarm-live-key` and closes sessions at the relay with a MAC'd
  `POST /internal/close`. *Why:* Firestore IAM cannot name a document, so the
  only faithful grant for "the session document only" is none
  (`terraform/infra/live.tf`). So the relay's manifest grants it no Firestore
  role, and its image carries no Firestore client.
* **(b) Plain ws on TCP 8080, proxied through swarm-api.** Browsers never
  reach the relay; swarm-api proxies them over the internal load balancer.
  *Why:* one public surface and one authenticator, and no certificate to issue,
  mount and rotate for a hop that never leaves the VPC. So the manifest mounts
  nothing but a memory `/tmp`, every port it names is 8080, and two
  NetworkPolicies admit only swarm-api's egress range and labelled tenant
  gates to it. The cost -- keystrokes in clear on the internal hops -- is
  stated in section 3.3.
* **(c) The key by Secret Manager version reference.** The relay reads
  `SWARM_LIVE_KEY_SECRET` with its own Workload Identity; there is no key
  file. *Why:* a mounted file needs a secret volume nothing here provisions,
  and L8 had already built the reference and the grant.

What ships, and why each piece is the way it is:

* **The image**, `apps/swarm-live/Dockerfile`, built by every release
  (`swarm-live` in `ALL_TARGETS` of `scripts/build-images.sh` and
  `scripts/push-images.sh`). Same posture as the control-plane images: bases
  by digest, third-party packages by hash, our own from the wheelhouse only,
  uid 10001 -- the uid the manifest runs it as under the `restricted` pod
  standard. It carries Debian's `ffmpeg`, and the build fails unless that has
  the `libvpx` encoder, because a relay without it serves every session and
  records none. swarm-live is not in the root uv project, so it has its own
  `apps/swarm-live/uv.lock`, resolved at the root lock's versions;
  `tests/unit/scripts/test_swarm_live_image.py` fails if it falls behind the
  relay's `pyproject.toml` or leaves those versions.
* **A deployed image only while the relay is on.** `terraform/infra/locals.tf`
  adds `swarm-live` to `deployed_images` when `enable_live_browser` is on.
  On, its digest must be in the `image_refs` output that `scripts/plan.sh`
  pins a non-deploy plan to. Off, a state applied before any release built it
  still plans.
* **Two NetworkPolicies admit the same sources, and nothing lets them differ.**
  The relay manifest carries `swarm-live-ingress`, rendered from
  `var.subnet_cidr`: every Cloud Run service here, swarm-api included, egresses
  from `module.network.subnetwork_id`, whose primary range that is. It sits
  before the StatefulSet, so the relay is closed from the moment it exists.
  `swarm-allow-live-ingress` (lane L6) is rendered from the range
  `kubernetes/apply.sh` reads from swarm-api's own service. apply.sh refuses
  when terraform's range is not that one, and
  `kubernetes/network_parity.py --live-ingress` judges both policies with one
  function after the apply.
* **One command applies it**, `kubernetes/apply.sh --swarm-system --confirm`.
  It reads `terraform output -raw swarm_live_manifest` only after the context
  checks (the deny-list in `scripts/lib/common.sh`, so never `agents-staging`),
  and applies it ahead of the namespace's policies in the same apply, because
  it creates the namespace they go into. It refuses when there is no manifest
  (`enable_live_browser` off), and when the manifest holds an object outside
  `swarm-system`, an RBAC object, or an unrendered placeholder.

### 8.2 Resource class

`browser`: 8 vCPU, 16 GiB, 2 units when this was written; 4 vCPU, 8 GiB,
1 unit since contract request 79 (owner, 2026-10-14), which also replaced the
starting split below with display 3 vCPU / 5 GiB and worker 1 vCPU / 3 GiB
(`apps/scheduler/scheduler/dispatch.py::LIVE_DISPLAY_SHARE`)
(`apps/common/swarm_common/profiles.py::RESOURCE_CLASSES`). **The class does not change;
it is split between the containers**, each with `requests == limits`
(invariant 7), summing to the class, so admission's unit arithmetic is
untouched. Proposed starting split, to be measured by lane L5: worker 3 vCPU /
6 GiB (claude-code, Node, the worker), display sidecar 5 vCPU / 10 GiB
(Chromium, Xvfb, encoding), with the existing 2 GiB memory `/dev/shm`
(`GkeJobDispatcher._manifest`, `apps/scheduler/scheduler/dispatch.py::GkeJobDispatcher._manifest`)
moved to the sidecar.

### 8.3 NetworkPolicy

* **Tenant namespaces: one new egress rule.** The existing egress policy cannot
  express it: its internet rule is an `ipBlock` that excepts the pod range, and
  "Pod traffic is never covered by an ipBlock rule"
  (`kubernetes/network-policies/allow-egress.yaml`). A second policy,
  `swarm-allow-live-egress`, selects pods labelled `swarm-live: "enabled"`
  (which the dispatcher sets only on live-browser pods) and allows TCP 8080 to
  pods `app.kubernetes.io/name=swarm-live` in namespace `swarm-system`.
  NetworkPolicies are unions, so it adds a path and removes nothing. A pod
  missing the label simply has no live path; unlike the trap that comment
  records, nothing else breaks, because DNS and the internet still come from
  the existing policy.
* **`swarm-system`: default-deny ingress, and allow** TCP 8080 to `swarm-live`
  from tenant namespaces (by the namespace label the tenant template sets) and
  from swarm-api's Direct VPC egress subnet range.
* The cluster's ranges are read from the live cluster, never written, as
  `kubernetes/apply.sh` and `kubernetes/network_parity.py` already require.
* **The port is 8080, plain ws, since owner decision (b) of 2026-10-11**
  (section 3.3). Lane L6 built both policies, and L8 the relay's Service, on
  TCP 8443; the relay now listens on 8080 and swarm-api dials 8080, and the
  manifest and policy files under `kubernetes/` move to it with lane LB-L8e
  (image and deploy). Until that lands, the files there still say 8443.

### 8.4 Frozen-contract touchpoints, as requests

Nothing in `apps/common/swarm_common/` is edited by this document. The requests
below are **not yet filed** in
[`docs/contract-change-requests.md`](../contract-change-requests.md): which of
them exist depends on the owner's answer to Q2, and lane PROFILE is filing its
own request for `claude-code-browser` in parallel, so numbers would collide.
They are filed, numbered, by lane L0 once the questions are answered.

| Request | File | The change | Invariants |
|---|---|---|---|
| **LB-A** | `states.py` | `ParkReason.HUMAN_REQUIRED`: parked by the worker when a hand-off's hold expires; promoted only by `human-ready`; never by a sweep. | 1 (costs nothing), 4 (the reason the park exists) |
| **LB-B** | `states.py` | `EventType` members `HUMAN_HANDOFF_REQUESTED`, `HUMAN_CONTROL_TAKEN`, `HUMAN_CONTROL_RETURNED`, `HUMAN_HANDOFF_RESOLVED`. | none |
| **LB-C** | `models.py` | `Task.human_wait: dict | None` (`reason`, `expect`, `requested_at`, `deadline`, `controller`), written only by swarm-api. **Or**, if the owner chooses the state (Q2), `TaskState.WAITING_FOR_HUMAN` in `CONCURRENCY_STATES` with transitions `RUNNING <-> WAITING_FOR_HUMAN`, `WAITING_FOR_HUMAN -> PARKED / CANCELLED / FAILED / READY`, and every reader of §4.3 updated in the same change. | 1-3: the state holds capacity and must be counted |
| **LB-D** | `profiles.py` | `RunnerProfile.live_browser: bool = False`; `True` on `claude-code-browser`. Validation: only on a `GKE_AUTOPILOT` profile. | 10: a caller still names a profile; whether it is live is the catalogue's, never the caller's |

The session and audit documents (`live_sessions`, `live_audit`) are
swarm-api's own, defined in swarm-api as `admin_roles` and `admin_audit` are,
not in the frozen models.

### 8.5 The ten invariants

1. Only leased tasks create demand: a holding task is `RUNNING`; a parked one
   is `PARKED(HUMAN_REQUIRED)` and creates none.
2. All-or-nothing reservation: unchanged; re-admission after `human-ready` is
   an ordinary admission.
3. Concurrency from `LEASED`: unchanged; no new capacity-holding state (unless
   Q2 chooses one, in which case LB-C adds it to `CONCURRENCY_STATES`).
4. No sleeping through a long wait: the hold is bounded -- one minute with
   nobody watching, at most 30 while someone is (section 4.3) -- then
   checkpoint, park, release, exit.
5. Fencing: the tunnel ticket, the gate's control channel and every worker
   route carry the generation; a stale generation's session is closed, and a
   stale worker's hand-off is refused.
6. No Spot: the pod is the same pod.
7. `requests == limits`: per container, summing to the class.
8. Checkpointing: unchanged and mandatory; a hold does not pause the periodic
   checkpoint, and the park writes one.
9. Tenant isolation: frames, control, recordings and the audit are served to
   the task's tenant only, checked on every connection; recordings live under
   the tenant's prefix.
10. Profiles by name: `live_browser` is a catalogue property; a caller cannot
    ask for a live browser on another profile, or pass display, browser or
    gate parameters.

---

## 9. Build plan

Lanes of at most ~6 files, split by territory, in phase order. A lane starts
only after the lanes it depends on have merged. Each lane pushes its tests
first, red, then the fix (CLAUDE.md, "Red first").

| Phase | Lane | Territory (files) | Depends on | Acceptance test |
|---|---|---|---|---|
| 0 | **L0 CONTRACT** | `apps/common/swarm_common/states.py`, `models.py`, `profiles.py`, `docs/contract-change-requests.md`, `scripts/lib/check-contract-parity.sh`, `tests/unit/common/test_live_browser_contract.py` | owner's answers; lane PROFILE merged | `HUMAN_REQUIRED` is not in any capacity set; `live_browser` is true only on `claude-code-browser` and refused on a non-GKE profile; parity script green |
| 1 | **L1 GATE-MASK** | `apps/agent-worker/agent_worker/livegate/mask.py`, `livegate/observer.js`, `livegate/capture.py`, `tests/unit/worker/test_live_mask.py` | L0 | a fixture frame plus rectangles comes out with those pixels painted; a field toggled from `password` to `text` stays masked; a stale report blanks the page area; a redaction-rule match in text is masked (fake built at runtime) |
| 1 | **L2 GATE-CDP** | `livegate/cdpproxy.py`, `livegate/cdppolicy.py`, `livegate/scripts.py`, `tests/unit/worker/test_live_cdp_proxy.py` | L0 | in control mode commands are held and released in order on return; events dropped; `postData` stripped for control-window requests; `Fetch.requestPaused` continued by the proxy; agent init scripts removed and restored; new connections refused; **outside control too**, `Target.createBrowserContext` with `proxyServer`, every `Security.*` method, `Page.setBypassCSP`, `Network.setExtraHTTPHeaders`, a download path outside `downloads/` and a method not on the allowlist are refused and `cdp_method_refused` recorded; the fresh page opens in a gate-owned context with no proxy; control is refused, and ended if held, while the page reports a certificate error |
| 1 | **L3 GATE-RFB** | `livegate/rfb.py`, `livegate/inject.py`, `livegate/ctl.py`, `tests/unit/worker/test_live_rfb.py` | L1 | key events from a viewer, or in view mode, are discarded; pod-to-console clipboard never sent; a second `ctl.sock` registration refused, and a restarted gate refuses even the first; captured logs contain no keysym |
| 1 | **L4 WORKER-HANDOFF** | `apps/agent-worker/agent_worker/handoff.py`, `lifecycle.py`, `workspace.py`, `images/agent-runtime-browser/swarm-handoff`, `tests/unit/worker/test_handoff.py` | L0, L3 | request -> gate paused -> signed route called; hold expiry -> checkpoint, wipe, park(`HUMAN_REQUIRED`), refund within the bound; cancel during hold -> exit code 4, wipe, cancel path; unprotected heap -> exit 5 and no tunnel |
| 1 | **L4b WORKER-STATE** | `apps/agent-worker/agent_worker/checkpoint.py`, `artifact_manifest.py`, `tests/unit/worker/test_browser_state_refused.py` | L0 | a storage-state JSON or a `Cookies` database under `work/` is left out of the checkpoint and the outputs, and `browser_state_refused` is recorded |
| 1 | **L5 DISPATCH** | `apps/scheduler/scheduler/dispatch.py`, `images/agent-runtime-browser/Dockerfile`, `images/agent-runtime-browser/chromium-policy.json`, `tests/unit/control_plane/test_dispatch_live_sidecar.py`, `docs/execution-backends.md` | L0 | a `claude-code-browser` manifest has a native sidecar with its own uid, `requests == limits` per container summing to `browser`, `/dev/shm` on the sidecar, the `swarm-live` label; every other profile's manifest unchanged |
| 2 | **L6 NETPOL** | `kubernetes/network-policies/allow-live-egress.yaml`, `kubernetes/network-policies/swarm-system-live.yaml`, `kubernetes/render.py`, `kubernetes/apply.sh`, `kubernetes/network_parity.py`, `tests/unit/scripts/test_live_netpol.py` | L5 | the rendered policy allows only TCP 8443 (8080 since owner decision 2026-10-11 (b), lane L8e) to `swarm-live` pods, only from labelled pods; the existing policy renders byte-identical |
| 2 | **L7 RELAY** | `apps/swarm-live/swarm_live/main.py`, `rendezvous.py`, `recorder.py`, `apps/swarm-live/pyproject.toml`, `tests/unit/live/test_relay.py` | L3 | a ticket used twice, expired, or for another tenant, session or generation is refused; one tunnel per session; viewer caps; segments written only under `.../live/` with the two suffixes |
| 2 | **L8 INFRA** | `terraform/infra/live.tf`, `terraform/modules/storage/main.tf`, `kubernetes/swarm-system/swarm-live.yaml`, `scripts/create-secrets.sh`, `tests/terraform/live.tftest.hcl` | L7 | `terraform test`: the 7-day suffix rule exists, the relay's grant is create-only and conditioned on `/live/`, every resource carries `managed-by=swarm-terraform`, no secret version |
| 2 | **L9 API-LIVE** | `apps/swarm-api/swarm_api/live.py`, `apps/swarm-api/swarm_api/routes/live.py`, `apps/swarm-api/swarm_api/settings.py`, `tests/unit/control_plane/test_live_api.py` | L7 | another tenant's member gets 404; a non-submitter can view, cannot take until opened; one controller at a time, preemption by the submitter only; the audit entry is in the control change's transaction; membership re-checked; no ticket in a URL |
| 2 | **L10 API-HANDOFF** | `apps/swarm-api/swarm_api/handoff.py`, `apps/swarm-api/swarm_api/routes/tasks.py`, `apps/swarm-api/swarm_api/validation.py`, `apps/scheduler/scheduler/loop.py`, `tests/unit/control_plane/test_handoff_api.py` | L0 | worker route refuses an unsigned or stale-generation call; `human-ready` moves `PARKED(HUMAN_REQUIRED) -> READY` for an eligible caller only; no sweep promotes it; past the maximum it is dead-lettered with `human_wait_expired`; `human_wait_resumes` is reserved |
| 1 | **L3b GATE-DEVICE** | `livegate/device.py`, `livegate/touch.py`, `livegate/presets.py`, `tests/unit/worker/test_live_device.py` | L2, L3 | each preset sets metrics, user agent and touch on the gate-owned context and resizes the display; an unknown preset name is refused; the agent's own `Emulation.*` calls are refused by the method policy; in control with touch on, a press/move/release arrives as touchStart/Move/End; in view mode no input is sent; the mask covers a password field on every preset at its device scale factor |
| 3 | **L11 UI-LIVE** | `apps/swarm-ui/src/LiveBrowser.tsx` (the Browser tab, device bar, logs toggle), `apps/swarm-ui/src/AgentSplit.tsx`, `apps/swarm-ui/src/api.ts`, `apps/swarm-ui/package.json`, `apps/swarm-ui/test/LiveBrowser.test.tsx` | L9, L3b | vitest: the Browser tab is first for a running browser agent; the ticket goes in the first message, never the URL; no Take control for a non-eligible viewer; a viewer's device switcher is disabled; a mismatched origin turns the strip red and asks twice; keys captured only with canvas focus; touch events sent only in control with touch on |
| 3 | **L11b UI-THEATER** | `apps/swarm-ui/src/Theater.tsx`, `apps/swarm-ui/src/TheaterTimeline.tsx`, `apps/swarm-ui/src/styles/theater.css`, `apps/swarm-ui/test/Theater.test.tsx` | L11 | vitest: ⤢ and `T` open it on the same session (no second ticket); Esc returns to the tab; Timeline/Logs switch; prev/next walk the live agents in order; replay reads only the caller's tenant's segments |
| 3 | **L12 UI-HANDOFF** | `apps/swarm-ui/src/Overview.tsx`, `apps/swarm-ui/src/Agents.tsx` (the banner row, the Live filter), `apps/swarm-ui/src/Recordings.tsx`, `apps/swarm-ui/test/Handoff.test.tsx` | L10, L11 | the tab banner and Overview card show reason, expected site and time left; "I'm ready to sign in" calls `human-ready`; the Live filter lists only running browser agents, needs-you first; recordings list only the caller's tenant's |
| 3 | **L12b UI-GRAPH** | `apps/swarm-ui/src/WorkflowViews.tsx`, `apps/swarm-ui/src/LiveThumb.tsx`, `apps/swarm-ui/test/LiveThumb.test.tsx` | L11b | a running browser node expands a card with a thumbnail at ≤1 fps; Open Browser tab and ⤢ Theater navigate to the same session; no thumbnail stream for a finished step |
| 1 | **L4c SESSION-CARRY** | `apps/agent-worker/agent_worker/livegate/session.py`, `apps/agent-worker/agent_worker/browser_login.py`, `apps/agent-worker/agent_worker/livegate/cdppolicy.py`, `tests/unit/worker/test_browser_session.py` | L2, L4 | in a personal tenant a workflow step's jar is exported on the gate's pipe and written to `swarm-tenant-u-<user>-browser-wf-<wf>`, then imported by the next step before the agent connects; in a group tenant nothing is written and no carry happens; a step outside a workflow carries nothing; `input.browser_login` loads the named login, and an unknown or expired name fails before the agent runs; the agent's cookie-reading and cookie-writing CDP methods are refused; no cookie value in any log, event, checkpoint or output (fake jar built at runtime) |
| 2 | **L8b VAULT-IAM** | `terraform/bootstrap/browser_logins.tf` (built in bootstrap, not infra: see its header), `tests/terraform/browser_logins.tftest.hcl` | L8 | as built: only a personal tenant's worker holds a grant, on its own `swarm-tenant-u-<user>-browser-` prefix; no group tenant's worker holds any `-browser-` grant; swarm-api may destroy those secrets; no secret version in Terraform; `managed-by=swarm-terraform` on every resource. **Superseded by L8c** (owner decision 2026-10-11): the worker's project-level reader, writer and unconditioned creator are removed, because Terraform cannot name a person whose workspace the job made, and a project-level create cannot be narrowed by name |
| 2 | **L8c SECRET-PROVISION** | `terraform/bootstrap/browser_logins.tf`, `terraform/modules/custom_role_ids/main.tf`, `tests/terraform/browser_logins.tftest.hcl`, `apps/swarm-api/swarm_api/browser_slots.py`, `apps/swarm-api/swarm_api/routes/browser_slots.py`, `apps/agent-worker/agent_worker/browser_login.py`, `tests/unit/control_plane/test_browser_slots.py`, `tests/unit/worker/test_browser_session.py` | L8b, L4c, L9b | a non-personal tenant is refused a slot; a name outside the caller's prefix is refused before Secret Manager is called; a stale generation is refused; a granted slot binds exactly one member on exactly one secret; no worker in Terraform holds any Secret Manager project grant for `-browser-`; swarm-api's provisioner role carries the condition; the worker asks for the slot before every write and never creates a secret |
| 2 | **L8e SHIP-RELAY** | `apps/swarm-live/Dockerfile`, `apps/swarm-live/uv.lock`, `scripts/build-images.sh`, `scripts/push-images.sh`, `terraform/infra/live.tf`, `terraform/infra/locals.tf`, `kubernetes/swarm-system/swarm-live.yaml`, `kubernetes/apply.sh`, the two live policies and `network_parity.py`, `tests/terraform/live.tftest.hcl`, `tests/unit/scripts/test_swarm_live_image.py`, `tests/unit/scripts/test_live_netpol.py` | L6, L7, L8 | build-images lists `swarm-live`; `terraform test`: the manifest is on 8080 with no TLS volume, `requests == limits`, and its NetworkPolicy admits only labelled tenant gates and `var.subnet_cidr`; `apply.sh --swarm-system` applies the manifest before the policies and refuses `agents-staging` (section 8.1.1) |
| 2 | **L9b LOGIN-VAULT** | `apps/swarm-api/swarm_api/browser_logins.py`, `apps/swarm-api/swarm_api/routes/browser_logins.py`, `apps/scheduler/scheduler/reconcile.py`, `tests/unit/control_plane/test_browser_logins.py` | L9 | only the workspace's own person can list or revoke its logins; anyone else gets 404; a group-tenant task naming `browser_login` is refused with `browser_login_personal_only`; a value is never returned; save takes a name and an expiry of at most 30 days; revoke destroys every version; a workflow's carry secret is deleted at its terminal state; the audit records names only |
| 3 | **L12c UI-LOGINS** | `apps/swarm-ui/src/BrowserLogins.tsx`, `apps/swarm-ui/src/Access.tsx`, `apps/swarm-ui/src/LiveBrowser.tsx`, `apps/swarm-ui/test/BrowserLogins.test.tsx` | L9b, L12 | after Return control on a personal-workspace task the tab offers "Save as login" with the account warning, and never on a group-tenant task; Access › Browser logins lists name, site, expiry and last use, never a value; Revoke asks for a typed confirmation |
| 4 | **L13 ACCEPT** | `.github/workflows/accept.yml`, `tests/acceptance/test_live_browser.py`, `tests/acceptance/fixtures/login.html`, `docs/acceptance.md` | all | on dev: a `claude-code-browser` task asks for help on a fixture sign-in page; a scripted viewer takes control and types a password generated at run time; the recording's frames have the field's pixels painted; the transcript, logs, checkpoint and outputs do not contain the password; control returns and the task succeeds; a member of another tenant is refused |

L1-L3 share `livegate/`, a new directory; they are three lanes because their
files do not overlap, and L3 waits for L1 because it encodes L1's frames.

### 9.1 Switching it on (LB-Lon)

The owner decided on 2026-10-11 (~15:00 PDT) that, once every lane above had
merged, ONE pull request turns the live browser on in dev. That pull request
sets `enable_live_browser = true` in `terraform/environments/dev/dev.tfvars`
and nothing else switches.

**One switch, both halves.** The relay (`terraform/infra/live.tf`: its grants,
its internal address, its manifest, swarm-api's `SWARM_LIVE_URL` and
`SWARM_LIVE_KEY`) and the pod (the scheduler's `LIVE_BROWSER_SIDECAR`, which
gives a `claude-code-browser` pod its `display` sidecar and live gate) follow
the same value. Before LB-Lon nothing in Terraform set `LIVE_BROWSER_SIDECAR`,
so the relay could have been turned on with no pod ever streaming to it.
`terraform/infra/locals.tf` now derives it from `local.live_relay_enabled`
rather than taking a second variable, because neither half is useful alone: a
relay with no sidecar streams nothing, and a sidecar with no relay costs every
browser pod a display's share of its resource class for a gate no ticket is
ever issued to. `tests/terraform/live.tftest.hcl` holds dev to both.

**The order is `live.tf`'s.** Steps 1-3 are the operator's and come first:
`make apply` with the switch false, `scripts/create-secrets.sh --live-key`, and
a `swarm-live` digest in `image_refs`. The switch is step 4. Step 5 applies
`terraform output -raw swarm_live_manifest` to swarm-autopilot. Until step 5
the relay's address answers nothing, and swarm-api's live routes fail closed.

**What stays off.** Saved browser logins (§5.5.1) are
`terraform/bootstrap`'s `enable_browser_logins` and are not part of this
switch. No swarm-api refusal switch is involved: every live-browser code
(`live_*`, `control_*`, `handoff_*`, `browser_*`) is in the not-switchable set
of `tests/unit/control_plane/test_new_refusals_ship_off.py`, raised only by
routes these lanes introduced. `claude-code-browser` already names
`GKE_AUTOPILOT` (§8.1), and `enable_gke_autopilot` is already true in dev.

---

## 10. Questions for the owner

**Answered 2026-10-11, recorded on #1030.** The owner took every
recommendation below: Q1 the submitter, with a per-task "open to the tenant"
toggle; Q2 the field; Q3 10 minutes (superseded 2026-10-13 by HANDOFF-POLICY:
one minute unwatched, extended while watched up to 30, section 4.3); Q4 24 hours; Q5 no; Q6 yes, the submitter
only; Q7 sign in again; Q8 console only in phase 1; Q9 R2; Q10 on; Q11 yes, at
1 fps; Q12 accept; Q13 the agent and the person in control. The owner also
approved the mockup of §6 as drawn.

The questions as they were asked, each written to `questions.json` with its
recommendation:

1. **Q1** Who may take control: the submitter (with a per-task "open to the
   tenant" toggle), or any tenant member. *Recommended: the submitter.*
2. **Q2** `Task.human_wait` while the pod holds, or a new task state
   `WAITING_FOR_HUMAN`. *Recommended: the field.*
3. **Q3** The hold before the park. *Recommended: 10 minutes.*
4. **Q4** How long a parked hand-off waits before it is dead-lettered.
   *Recommended: 24 hours.*
5. **Q5** May platform admins outside the tenant watch? *Recommended: no.*
6. **Q6** May a person take control when the agent did not ask?
   *Recommended: yes, the submitter only.*
7. **Q7** Keep the browser session across a park (encrypted), or sign in again.
   *Recommended: sign in again.* **Revised by LB-7**: sessions are reused
   through the gate (workflow carry, plus named tenant logins), still never
   through the checkpoint.
8. **Q8** Notify beyond the console? *Recommended: console only in phase 1.*
9. **Q9** The relay shape (R2 or R3). *Recommended: R2.*
10. **Q10** Console-to-pod paste in control mode. *Recommended: on.*
11. **Q11** Record when nobody is watching? *Recommended: yes, at 1 fps.*
12. **Q12** Chromium launched by the platform in a sidecar with its own uid,
    reached through a CDP proxy, with no VNC server listening even on
    localhost -- instead of the brief's agent-launched Chromium and localhost
    VNC (§3.1, §3.2). *Recommended: accept.*
13. **Q13** Who may switch the device (§6.1): the agent only; the agent and the
    person in control; or any viewer. *Recommended: the agent and the person in
    control.*

---

## 11. Not decided here, and not verified

* **No measurement backs the resource split, the frame rate or the relay's
  capacity.** They are starting values for L5 and L7 to measure.
* **Native sidecars on this cluster** are assumed from GKE's support for them
  on current Autopilot versions; L5 confirms on `swarm-autopilot` before it
  merges.
* **The path from swarm-api to the relay** (Direct VPC egress to an internal
  load balancer in front of `swarm-live`) is named, not measured; L8 measures
  it, as the verification job's egress setting was once measured the hard way
  (`terraform/infra/verify.tf`).
* **That Playwright's `connectOverCDP` picks up the gate-owned context** handed
  over on return (section 5.4.3), and that the method allowlist of section 5.4.4
  covers every call Playwright makes, are design; L2 proves both.
* **Playwright's `connectOverCDP` through the proxy**, downloads and file
  uploads through the shared volumes, are design; L2 and L5 prove them.
