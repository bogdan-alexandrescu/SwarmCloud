"""The live-browser hand-off, the worker's half (docs/design/live-browser.md §4).

Lane LB-L4. Read §4.1 (the agent asks), §4.3 (the bounded wait), §4.4
(cancellation), §5.4.1 (the attempt key) and §5.4.2 (ctl.sock) before changing
anything here. swarm-api's half is `swarm_api.handoff` (lane L10); the gate's
end of ctl.sock is `agent_worker.livegate.ctl` (lane L3).

    $SWARM_HANDOFF = work/.swarm-handoff/
      requests/<id>.json   the agent's, through `swarm-handoff request` (.tmp, rename)
      devices/<id>.json    the agent's, through `swarm-handoff device <preset>`
      themes/<id>.json     the agent's, through `swarm-handoff theme dark|light|system`
      responses/<id>.json  this worker's: {"outcome": ..., "exit": N, ...}
      status.json          this worker's: the last control episode, for `status`
      history.json         this worker's: every hand-off of the task and its outcome
      notes.md             the agent's: where it stopped, for the attempt after a park
      resumed.md           this worker's: what a resumed attempt is told (below)
      unavailable          this worker's: present when this attempt has no live path
      egress.md            this worker's: present while the proxied browser has no
                           egress (egress_ticket.py; the agent's prompt carries it)
      qa-identity.md       this worker's: present while a task that named the QA
                           identity has no credential in the gate (qa_identity.py)
      browser.json         the runner's: the browser tools it registered for the
                           task's `browser_tool`, which this worker forwards to the
                           gate (`browser_tools` on ctl.sock; browser_tools.py)
      browser-tabs.json    this worker's: the tabs a park kept, reopened by the
                           next attempt before its agent starts (browser_resume.py)
      README.md            this worker's: the guide the agent reads

WHY A SPOOL. As the child-task spool (`children.py`): no listener in the
agent's reach, and `work/` is checkpointed, so a request survives a park.

WHAT THE WORKER DOES WITH A REQUEST (§4.1), on its control poll:

  1. validates it -- `reason` at most 200 characters and redacted before it
     goes anywhere, `expect` a bare host `url_refusal` accepts, at most
     `max_handoffs_per_attempt` per attempt, one at a time;
  2. tells the gate to PAUSE the agent's CDP (`pause` on ctl.sock), so from
     here the agent cannot act on the browser, cooperative or not;
  3. records the wait through swarm-api's worker route, signed with the
     attempt key under `swarm-handoff/v1`. swarm-api sets `Task.human_wait`;
     the task stays RUNNING and keeps exactly the capacity it holds.

Then, on every poll while the wait is open, it reads the task:

  * `human_wait` cleared by swarm-api (a person pressed Return control, lane
    L9) -> `resume` on the gate, and the agent's `wait` exits 0 with the
    summary of §5.9 (origin and path, redacted; never who or what was typed);
  * the hold's deadline passed with nobody in control, or the gate's channel
    dropped (§5.4.2: a restarted gate ends the live path) -> the agent's `wait`
    exits 3 and the lifecycle parks: wipe, checkpoint, upload,
    PARKED(HUMAN_REQUIRED), the attempt refunded while fewer than
    `max_human_resumes` have been, release, exit (§4.3, invariant 4);
  * a cancel (the lifecycle's control poll) -> the agent's `wait` exits 4,
    the gate ends control and wipes, and the ordinary cancel path runs (§4.4).

A RESUMED ATTEMPT CARRIES ON (RESUME-PROGRESS, QA12-14). The park wipes the
browser and ends the CLI, and the CLI has no conversation to resume across
attempts, so the next attempt's agent starts from its prompt and `work/`.
MEASURED 2026-10-13 on task_4defa61c5eea4a5c87f5: generation 2 asked for the
hand-off generation 1 had already finished, then the one that had parked it --
nothing told it what had been done, so without a person it would loop. So the
worker keeps a LEDGER of every hand-off in the spool (`history.json`: the
request, its redacted reason, its outcome), which travels in the checkpoint
with the rest of `work/`; and on an attempt whose ledger holds an earlier
attempt's hand-offs, `prepare` writes `resumed.md` -- which were done, which
were not, and the agent's own `notes.md`, redacted -- and the claude-code
runner puts it in the agent's prompt. The ledger is in the agent's reach, so
it is read as untrusted: shape-checked, bounded, redacted again, and only ever
told back to the same task's agent.

A WORKER THAT CANNOT PROTECT ITS HEAP GETS NO LIVE PATH (§5.4.1): no tunnel
ticket and no hand-off; the agent's `swarm-handoff` exits 5. It still takes
the gate's one registration slot, so the agent cannot take it instead: an
open slot is worse than a key in an unprotected heap.

NOTHING HERE LOGS a ticket, a signature, a key, the request's reason or a URL
the person visited. Log lines carry outcomes, codes and counts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple
from urllib.parse import quote, urlsplit

from swarm_common.models import utcnow
from swarm_common.profiles import RUNNER_PROFILES, url_refusal
from swarm_common.states import EventType, ParkReason, TaskState, assert_transition
from swarm_redaction import redact as redact_text

from . import browser_tools, cf_access
from .browser_resume import BrowserResume, Restored
from .egress_ticket import BrowserEgress
from .qa_identity import NOTICE_NAME as QA_NOTICE_NAME
from .qa_identity import WORKER_UNPROTECTED as QA_WORKER_UNPROTECTED
from .qa_identity import QaSignIn, requested
from .qa_target import CTL_OP as QA_TARGET_OP
from .qa_target import QaTargetSignIn
from .errors import ExitCode
from .hardening import PROTECTED
# The `swarm-handoff device` help names the gate's presets from the gate's own
# list (PHONE-380, part of #2136), so it cannot drift from what the gate takes.
from .livegate.presets import PRESETS
from .livegate.tunnel import (
    CLOSE_RESTARTING,
    RESOLVER_BUSY,
    RETRY_MAX_SECONDS,
    RETRY_SECONDS,
    reconnect_delay,
    relay_url,
    resolver_class,
)

#: CAPTCHA-HANDOFF (BGD, 2026-10-23; docs/design/live-browser.md §4.1, part
#: of #2058): a hand-off the gate raised itself, because it saw a challenge on
#: the agent's page (livegate `LiveGate._on_challenge`), is `origin: "gate"`
#: at swarm-api. An agent's own request names no origin: swarm-api's default
#: is "agent", and one from before the field would refuse it mid-rollout.
GATE_ORIGIN = "gate"
AGENT_ORIGIN = "agent"
#: What the agent reads in the answer to a gate-raised hand-off, and in the
#: answer to its own request while one is open (`joined`).
GATE_WAITING = (
    "A person has been asked to solve a CAPTCHA on {host}: the platform asked for you. "
    "Wait: run `swarm-handoff wait` in the foreground, and do not retry the page, poll "
    "the browser or try to solve or get around the CAPTCHA. Carry on once it returns."
)

#: The spool's directory under `work/`. `Workspace.handoff_dir` is the one
#: path; this is its name.
SPOOL_DIR_NAME = ".swarm-handoff"
#: The variable that tells an agent it may ask for a person, and where.
HANDOFF_ENV = "SWARM_HANDOFF"
#: Where the dispatcher puts ctl.sock (`scheduler.dispatch.LIVE_GATE_CTL`,
#: lane L5), passed to the worker container as SWARM_GATE_CTL.
GATE_CTL_ENV = "SWARM_GATE_CTL"
#: The gate's CDP proxy for the agent's Playwright (§3.2), and the upload
#: directory Chromium can read (§3.1). Passed on to the agent: a path and a
#: loopback address, never a credential. With them, the workspace's Jev
#: judgment backend and, for an internal one, the in-cluster decision API URL
#: the dispatcher set (`scheduler.judgment`, JUDGE-SELECT): a name and a
#: cluster address, never a credential, which jev-browser-mcp reads from the
#: CLI's environment.
AGENT_PASSTHROUGH_ENV = ("SWARM_CDP_ENDPOINT", "SWARM_BROWSER_UPLOADS",
                         "SWARM_JEV_BACKEND", "JEV_API_URL")

#: §5.4.1: each worker-only route signs a domain-separated message, so a
#: signature for one is never a signature for another. `swarm_api.handoff`
#: holds HANDOFF_PURPOSE; tests/unit/worker/test_handoff.py holds them equal.
HANDOFF_PURPOSE = "swarm-handoff/v1"
LIVE_SESSION_PURPOSE = "swarm-live-session/v1"

#: `swarm_api.validation.HUMAN_WAIT_RESUMES_METADATA_KEY`, restated (this image
#: does not carry swarm-api); the test holds them equal.
HUMAN_WAIT_RESUMES_METADATA_KEY = "human_wait_resumes"
#: `swarm_api.handoff.HUMAN_RETURNED_METADATA_KEY` and HUMAN_READY_METADATA_KEY,
#: restated (LB-RETURN): a person's return during this attempt, which the park
#: reads; and the "I'm ready" a re-queue records, as swarm-api's does.
HUMAN_RETURNED_METADATA_KEY = "human_returned"
HUMAN_READY_METADATA_KEY = "human_ready"
#: `swarm_api.handoff.REVIVED`: swarm-api's answer to this worker's expiry when
#: a viewer or a controller moved the deadline after it was read.
REVIVED = "revived"

#: `swarm_api.handoff.REQUEST_ID` and its `expect` host rule, restated.
REQUEST_ID = re.compile(r"^[a-z0-9-]{1,64}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
#: A device preset's NAME (§6.1). The list itself is the gate's: a name
#: outside it is refused there. A caller never sends dimensions.
_PRESET = re.compile(r"^[a-z0-9-]{1,32}$")
#: `livegate.presets.THEMES`, restated (QA-KIT): the colour schemes the agent
#: may name. Checked here, again by the gate, and by `swarm-handoff` itself;
#: tests/unit/worker/test_handoff.py holds the three equal.
THEMES = ("dark", "light", "system")

#: §4.1 step 1.
MAX_REASON_CHARS = 200
#: §5.10: hand-off requests per attempt. swarm-api enforces it as well, from
#: the task document, so a worker that miscounts cannot pester people.
MAX_HANDOFFS_PER_ATTEMPT = 3
#: §4.3: parks on HUMAN_REQUIRED that do not spend an attempt.
MAX_HUMAN_RESUMES = 2
#: How long a HUMAN_REQUIRED park waits for a person before the scheduler's
#: `_expire_human_waits` dead-letters it: the scheduler's
#: `human_park_max_seconds`, measured from the park instant. Restated from
#: `scheduler.loop.HUMAN_PARK_MAX_SECONDS`, which this image does not carry;
#: the scheduler reads the setting through `getattr` with that default and its
#: settings have no such field, so the default IS the setting.
#: tests/unit/worker/test_wait_record.py holds both, and fails the day the
#: setting becomes configurable without the worker being handed its value.
#: The park records `expires_at` from it (WAIT-RECORD, part of #1782).
HUMAN_PARK_MAX_SECONDS = 86_400
#: One request file, as read. swarm-api refuses a body over 4096 bytes.
MAX_REQUEST_BYTES = 4096

#: §4.2: a controller's console renews every 60 s and swarm-api moves the
#: deadline on input. While a person holds control the worker waits this long
#: past the deadline for swarm-api to end the idle control itself before it
#: parks under them.
CONTROLLED_GRACE_SECONDS = 60
#: §4.1 exit 3: "write its notes into work/ and stop; the worker ends it either
#: way". How long the agent gets to do that before it is stopped. Bounded, so
#: a hold's end is never the long wait invariant 4 forbids.
EXPIRY_NOTES_SECONDS = 30
#: §4.4: how long the agent's `wait` gets to read "cancelled" before the
#: ordinary cancel path stops it.
CANCEL_ANSWER_SECONDS = 3
#: §3.3: "A relay restart drops every live session for the seconds it takes
#: to come back; both ends reconnect." The gate's end reconnects through the
#: worker, the only holder of the attempt key that proves a tunnel ticket:
#: when the gate's `status` says its tunnel closed, the worker asks swarm-api
#: for a fresh ticket and hands it over (LB-R6). A refused ask is tried again
#: after TUNNEL_RETRY_SECONDS, doubling to TUNNEL_RETRY_MAX_SECONDS, so a relay
#: that stays down costs a route call a minute rather than one every poll.
TUNNEL_RETRY_SECONDS = int(RETRY_SECONDS)
TUNNEL_RETRY_MAX_SECONDS = int(RETRY_MAX_SECONDS)
#: RELAY-ROLL: a tunnel the relay closed with 1012 ("restarting, reconnect
#: now", livegate.tunnel.CLOSE_RESTARTING) is reopened in the same tick, and
#: a reopen the new relay is not up for yet is tried again after
#: livegate.tunnel.reconnect_delay (jittered, from ~250 ms) for up to this
#: long; past it, on every later poll (the delay's cap is under the poll's
#: 10 s) -- never the 5 s doubling to a minute above. A rolling relay is gone
#: for the seconds its new pod takes, and every viewer waits on this end.
#: Bounded so a relay that stays down holds the poll a few seconds, not more.
RESTART_BURST_SECONDS = 3.0
#: swarm-api's answers that end the live path for this attempt for good (a
#: closed session, a stale generation, a profile with no live browser): no
#: later ticket will come, so the worker stops asking.
TUNNEL_ENDED_CODES = frozenset({"live_session_closed", "live_fenced", "live_not_available"})

#: `swarm-handoff`'s exit codes (§4.1). The command restates them.
EXIT_RETURNED = 0
EXIT_EXPIRED = 3
EXIT_CANCELLED = 4
EXIT_REFUSED = 5
EXIT_STILL_WAITING = 75

#: Why an open hand-off ended without a person returning control. The
#: lifecycle parks on either.
HOLD_EXPIRED = "hold_expired"
GATE_LOST = "gate_lost"

#: Why this attempt has no live path, as the agent's refusal and the log say it.
NOT_LIVE = "not_a_live_browser_profile"
WORKER_UNPROTECTED = "worker_unprotected"
GATE_UNAVAILABLE = "gate_unavailable"
NO_ATTEMPT_KEY = "no_attempt_key"
NO_LIVE_SESSION = "no_live_session"

#: GATE-STAYS-UP (#2205): why a `swarm-handoff device` or `theme` answered
#: `gate_unavailable` -- the answer's `reason`, the worker's log line, and what
#: `swarm-handoff` prints. MEASURED 2026-10-24: two review steps got a bare
#: exit 5 for minutes and could not tell a browser coming back from one that
#: never would. The gate's own refusal codes (livegate.ctl.DOWN_CODES) plus the
#: two the worker sees itself:
#:
#: * `starting` / `restarting` -- Chromium is not up yet, or is being started
#:   again after it exited (BROWSER-RELAUNCH): waited for, GATE_WAIT_SECONDS;
#: * `crashed` -- the relaunch budget is spent, or the gate process itself
#:   restarted (its channel closed; a restarted gate never takes this worker
#:   back, ctl.py): it will not come back this attempt;
#: * `wiped` -- the attempt's browser was wiped as it ended;
#: * `unresponsive` -- the gate answered that the browser did not finish the
#:   switch in time (ctl.OP_SECONDS), or did not answer the worker at all;
#: * `not_enabled` -- this attempt never had the gate (no SWARM_GATE_CTL, a
#:   refused registration, not a live-browser profile).
GATE_STARTING = "starting"
GATE_RESTARTING = "restarting"
GATE_CRASHED = "crashed"
GATE_WIPED = "wiped"
GATE_UNRESPONSIVE = "unresponsive"
GATE_NOT_ENABLED = "not_enabled"
GATE_DOWN_REASONS = frozenset({GATE_STARTING, GATE_RESTARTING, GATE_CRASHED, GATE_WIPED,
                               GATE_UNRESPONSIVE})
#: The reasons worth waiting out: the browser is on its way.
GATE_WAIT_REASONS = frozenset({GATE_STARTING, GATE_RESTARTING})
#: How long one poll waits, in all, for a gate that is starting or restarting
#: before answering the agent with the reason. WHY 15 s: `swarm-handoff device`
#: and `theme` read the answer for 30 s (DEVICE_WAIT_SECONDS in
#: images/agent-runtime-browser/swarm-handoff), and the request sits up to one
#: control poll (10 s) before the worker reads it; 15 s is what is left with
#: margin, so the agent reads the reason rather than `no_answer`. A first
#: relaunch (livegate RELAUNCH_DELAYS 1 s, then the launch) fits inside it.
#: The task allows up to 60 s; more needs the command's own wait raised.
GATE_WAIT_SECONDS = 15.0
#: Between two tries while waiting.
GATE_WAIT_STEP_SECONDS = 1.0
#: The sentence each reason is told to the agent with.
GATE_REASON_TEXT = {
    GATE_STARTING: "the live browser is still starting; try again in a minute",
    GATE_RESTARTING: "the live browser is restarting after Chromium exited; try again in a minute",
    GATE_CRASHED: "the live browser is gone for this attempt (Chromium could not be started again, "
                  "or the gate restarted); carry on without it",
    GATE_WIPED: "the live browser was wiped as this attempt ended",
    GATE_UNRESPONSIVE: "the live browser did not answer in time; try again",
    GATE_NOT_ENABLED: "this attempt has no live browser path",
}

#: NOLIVE-GUARD (part of #1366): why the HUMAN's view has not opened, while the
#: agent's browser runs. The gate refused `tunnel_ticket`: its relay did not
#: answer (`tunnel_refused {"cause": "relay unreachable: gaierror"}` in the
#: gate's own record; ctl.sock carries only "failed").
TUNNEL_REFUSED = "tunnel_refused"
#: Before `setup` has tried.
VIEW_NOT_SET_UP = "not_set_up"
#: The jitter on a tunnel retry's backoff: 75-125 %, as `reconnect_delay`'s
#: restart schedule, so the ~60 pods of a burst that all met the same DNS
#: failure do not all knock on the relay again in the same instant.
TUNNEL_JITTER = (0.75, 0.5)
#: LB-DNS (part of #1366): the FIRST tunnel open, refused by the gate while
#: the relay's name does not resolve, is tried again after a TUNNEL_JITTER'd
#: delay from RELAY_LOOKUP_RETRY_FIRST_SECONDS, doubling to
#: RELAY_LOOKUP_RETRY_MAX_SECONDS, for up to RELAY_LOOKUP_RETRY_SECONDS; past
#: it, setup gives the first open up and the per-poll retry above takes over.
#: WHY 30 s: swarm-live is a headless Service (clusterIP: None, one replica),
#: so its name has no address while its pod is not ready, and devb-1015 met
#: the failure ~4 s after registration in a burst of 60 pod starts over 18 s:
#: a relay pod turning ready and a burst's resolver queue draining are both
#: tens of seconds. WHY NO MORE: setup runs before the agent starts, so this
#: is the most a lookup failure may delay the agent (plus the one try in
#: flight, which ctl.sock bounds at 10 s), and the agent's browser does not
#: need the tunnel (NOLIVE-GUARD) -- only the human's view does.
RELAY_LOOKUP_RETRY_SECONDS = 30.0
RELAY_LOOKUP_RETRY_FIRST_SECONDS = 1.0
RELAY_LOOKUP_RETRY_MAX_SECONDS = 8.0
#: How long the worker's own lookup of the relay's name may take. A resolver
#: that has not answered by then is a busy one (EAI_AGAIN's meaning: glibc's
#: own give-up is 5 s x 2 tries, too long to wait on before every retry).
RELAY_PROBE_SECONDS = 3.0

#: NOLIVE-GUARD: a live-browser attempt whose GATE is not ready (no ctl.sock,
#: or a registration the gate refused -- a restarted gate refuses every one)
#: has no CDP proxy, so it never starts its agent headless: `Worker._prepare`
#: fails the ATTEMPT retryably with this cause, before the agent, its lease
#: and capacity released, as an unreachable forge does. The next attempt is a
#: new pod with a new sidecar. Bounded by `max_attempts`, like every retry.
LIVE_GATE_UNAVAILABLE = "live_gate_unavailable"
#: How long that task waits before it is eligible again: long enough for the
#: burst of pod starts that broke this one to have passed, short enough that a
#: retried attempt does not look stuck.
LIVE_GATE_RETRY_DELAY_SECONDS = 30

#: BROWSER-START (part of #1366): the agent's browser is the gate's CDP proxy,
#: and `setup` checks it is answering before the agent can start, rather than
#: trusting a registration alone. Why the agent has no browser when it has a
#: gate: no SWARM_CDP_ENDPOINT was passed to this worker (the runner would
#: then register no Playwright MCP and leave SWARM_CDP_ENDPOINT empty, the
#: devb-1015 symptom), or the endpoint never answered `/json/version`.
NO_CDP_ENDPOINT = "no_cdp_endpoint"
CDP_UNREACHABLE = "cdp_unreachable"
#: How long `setup` waits, in all, for a registration whose channel failed (a
#: gate restarting: ctl.sock present but not yet accepting) and, separately,
#: for the CDP proxy to answer. WHY 20 s: the native sidecar's startup probe
#: only tests that ctl.sock exists, so this is the margin for a gate that
#: restarts or a proxy that is slow under a burst of pod starts -- seconds,
#: not minutes; past it a new pod is the better bet (LIVE_GATE_UNAVAILABLE).
#: A registration the gate REFUSED is never retried: a restarted gate refuses
#: every one.
BROWSER_WAIT_SECONDS = 20.0
BROWSER_WAIT_FIRST_SECONDS = 0.5
BROWSER_WAIT_MAX_SECONDS = 4.0
#: One `/json/version` probe's bound.
CDP_PROBE_SECONDS = 2.0

#: The ctl.sock operations this module sends. Every one must be in the gate's
#: `livegate.ctl.OPS`; the test holds that once L3 is importable.
#:
#: THERE IS NO `wipe` OPERATION IN L3's OPS. §5.5 says the worker tells the
#: gate `wipe` at every attempt end; the closest faithful version on L3's
#: channel is `end_control` with `wipe: true`, which the gate's handler must
#: honour by closing Chromium and removing the profile. Reported on the PR.
#: `tabs_export` / `session_export` (scope park) are sent at park, before the
#: wipe; `session_import` / `tabs_restore` on the resumed attempt, before the
#: agent exists (browser_resume.py, §5.5.2).
GATE_OPS = frozenset({"tunnel_ticket", "pause", "resume", "end_control", "device", "theme", "status",
                      "egress_ticket", "browser_tools", "tabs_export", "tabs_restore",
                      "session_export", "session_import", "qa_identity", QA_TARGET_OP})

#: EGRESS-GATE: the agent's notice that its proxied browser has no egress
#: (`egress_ticket.notice`), in the spool while that is so; the claude-code
#: runner puts it in the prompt.
EGRESS_NOTICE_NAME = "egress.md"
#: QI-WORKER: the agent's notice that the console's QA sign-in is unavailable
#: (`qa_identity.notice`), in the spool while that is so, beside the egress one.
QA_IDENTITY_NOTICE_NAME = QA_NOTICE_NAME

#: The browser's start-up steps the gate dates in its `status` (`startup`, in
#: epoch seconds), in order, for the console's progress bar (STARTUP-API,
#: contract request 74). They follow the pod's four, which the reconciler
#: writes (`reconciler.startsteps.POD_STARTUP_STEPS`); the worker writes these
#: three, because the gate holds no credential to write anything.
#:
#: `first_frame` is the first frame the gate's capture published to its
#: stream hub, which it does only while an RFB stream -- a viewer, or the
#: relay's recorder -- is open: the nearest point to "the live stream sent its
#: first frame" either side of the tunnel can observe.
GATE_STARTUP_STEPS: tuple[str, ...] = ("browser_launching", "browser_ready", "first_frame")

#: The guide the agent reads, written into the spool by `prepare`.
GUIDE_NAME = "README.md"
AGENT_GUIDE = """\
# Asking a person for help with the browser

When the browser needs something only a person can do -- a sign-in, a
two-factor prompt, a CAPTCHA -- ask for help instead of guessing:

    swarm-handoff request --reason "Sign in to GitHub as the bot account" --expect github.com
    swarm-handoff wait

`--reason` (at most 200 characters) is shown to the person; never put a
secret in it. `--expect` is the bare host they should sign in on. The person
is notified at once. While you wait, your browser connection is paused: a
person is using it.

WAIT, DO NOT POLL. `swarm-handoff wait` blocks until the hand-off is
resolved, for up to 9 minutes per call; run it in the foreground and do
nothing else meanwhile. Do not poll the browser or CDP (`curl /json/version`,
snapshots, screenshots) in a loop: it is paused, and every poll is wasted.
If nobody opens the live view within a minute the hold ends and the task
parks; while someone watches or is in control it keeps holding (up to 30
minutes).

`wait` exits:

  0   control was returned; a JSON summary (url, title, held_seconds) is on stdout.
      The person may have changed the page: read it again, once, from the tab
      at that url -- never a page handle, snapshot or value from
      before the hand-off. From Python, connect again with `swarm_browser.live()` and take
      `swarm_browser.handoff_page(context)`, not `context.pages[0]`; with the
      browser_* tools, browser_tabs, select that tab, then browser_snapshot.
  75  still waiting (someone is watching): run `swarm-handoff wait` again, at once.
  3   nobody came in time. Write where you stopped into $SWARM_HANDOFF/notes.md
      now (what is done, what is next); the task is parked until a person is
      ready, then started again on your restored files, and your next attempt
      is told which hand-offs were done and given those notes. The browser
      session is NOT kept.
  4   the task is being cancelled: stop.
  5   refused (no live path, the limit of 3 requests, or an invalid request):
      carry on without help, or fail with that reason.

THE PLATFORM ASKS FOR A CAPTCHA ITSELF. When the browser shows a CAPTCHA,
the platform asks a person for you: your browser is paused, and
`swarm-handoff wait` with no argument waits on that hand-off. Never try to
solve or get around a CAPTCHA. A request of your own while that hand-off is
open is answered by it.

Keep $SWARM_HANDOFF/notes.md current as you go, and always before you ask:
it is what you continue from if this attempt is parked.

`swarm-handoff status` prints the last control episode in the same shape as
`wait`. `swarm-handoff device <preset>` switches the browser to a device
preset by name ({presets}).
`swarm-handoff theme dark|light|system` sets the colour scheme every page is
told to prefer (prefers-color-scheme); it holds across navigation and new
tabs, and `system` lifts it. browser_resize is refused: the size is a preset's.
A refused call changes nothing else: the next device, theme or CDP connection
works. If either answers `gate_unavailable`, its `reason` says why: `starting`
or `restarting` (the worker already waited, and says how long; try again in a
minute), `unresponsive` (try again), `crashed` or `not_enabled` (carry on
without the live browser, and say so).

A flow as a GIF, a mockup beside the app, and a render against its frame (one line each):

    swarm-handoff record start <name>   then the flow, then   swarm-handoff record stop   -> $SWARM_ARTIFACTS_DIR/flows/<name>.gif
    swarm-handoff serve <dir>   -> prints http://127.0.0.1:<port>/ for a new tab of the live browser (file:// is blocked)
    swarm-handoff compare <shot.png> <frame.png>   -> a score (0 = the same picture) and $SWARM_ARTIFACTS_DIR/compare/<name>.diff.png; exit 1 over the threshold

Never copy cookies or a storage state into your files: they are refused.
""".replace("{presets}", ", ".join(PRESETS))

#: The worker's ledger of the task's hand-offs, every attempt's (RESUME-PROGRESS).
HISTORY_NAME = "history.json"
#: The agent's notes, which the guide asks it to keep; told back on a resume.
NOTES_NAME = "notes.md"
#: What a resumed attempt is told, written by `prepare` on every attempt that
#: has an earlier one's hand-offs to tell, and removed on every other: a file
#: at this name that `prepare` did not write this attempt is never read. The
#: claude-code runner puts it in the agent's prompt (`runners.claude_code`).
RESUME_NAME = "resumed.md"
#: The ledger keeps the newest entries; a task asks at most
#: `max_handoffs_per_attempt` per attempt over a few attempts.
MAX_HISTORY = 20
#: How much of the agent's notes a summary carries, and how much it reads.
MAX_NOTES_CHARS = 4000
MAX_HISTORY_BYTES = 64 * 1024
#: The summary's first line, and how each hand-off is marked in it.
RESUME_HEADLINE = (
    "YOU ARE RESUMING THIS TASK. DO NOT START IT OVER: carry on from where the "
    "earlier attempt stopped."
)
MARK_DONE = "DONE"
MARK_NOT_DONE = "NOT DONE"
#: Each outcome the ledger records, as the summary says it.
_OUTCOME_TEXT = {
    "returned": (
        f"{MARK_DONE} -- a person took control and returned it. Do not ask for "
        "this again; carry on from the step after it."
    ),
    "expired": (
        f"{MARK_NOT_DONE} -- nobody took control before the hold ran out, and "
        "the task was parked until a person said they were ready. If this step "
        "still needs a person, ask for it again when you reach it."
    ),
    "cancelled": f"{MARK_NOT_DONE} -- the attempt was being cancelled while it waited.",
    "unavailable": f"{MARK_NOT_DONE} -- it ended without a person ({{cause}}).",
    "waiting": f"{MARK_NOT_DONE} -- the attempt ended while it was still waiting.",
}


def _clean(value: Any, limit: int) -> str:
    """One line of ledger text, bounded and redacted: the ledger is in the
    agent's reach, so what it holds is never told back as it was found."""
    text = " ".join(str(value or "").split())[:limit]
    return redact_text(text).text[:limit]


def _load_history(root: Path) -> list[dict[str, Any]]:
    """The ledger's entries, oldest first; [] for a missing or broken one."""
    path = root / HISTORY_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HISTORY_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        entry for entry in data
        if isinstance(entry, dict)
        and isinstance(entry.get("request_id"), str)
        and isinstance(entry.get("attempt_id"), str)
    ][-MAX_HISTORY:]


def _read_notes(root: Path) -> str:
    path = root / NOTES_NAME
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_NOTES_CHARS + 1)
    except OSError:
        return ""
    clipped = len(text) > MAX_NOTES_CHARS
    text = redact_text(text[:MAX_NOTES_CHARS].strip()).text
    return f"{text}\n[... cut at {MAX_NOTES_CHARS} characters]" if clipped else text


#: What a resumed agent is told of the artifacts directory (issue #1362): by
#: design it starts empty (docs/checkpointing.md); the parked attempt's files
#: were uploaded at the park and are listed with the task's result by reference.
ARTIFACTS_NOTE = (
    "Files the earlier attempt wrote to $SWARM_ARTIFACTS_DIR were uploaded when it "
    "parked and stay listed with this task's result; this attempt's artifacts "
    "directory starts empty, so they are not on disk here. Do not write them again "
    "unless they must change; anything you need to read again belongs in your "
    "working directory."
)


def _browser_lines(restored: Restored | None) -> list[str]:
    """What the resumed agent is told of its browser (§5.5.2)."""
    if restored is None or not restored.tabs:
        lines = [
            "The browser is a new one: the earlier attempt's pages are not open, so "
            "open again what you need."
        ]
    else:
        lines = [
            "The browser was reopened on the tabs the earlier attempt had open, in "
            "order (the one marked FRONT is the tab in front). These pages were "
            "loaded again, so check each is in the state you need:",
            "",
        ]
        lines += [
            f"{number}. {url}" + (" (FRONT)" if url == restored.active else "")
            for number, url in enumerate(restored.tabs, start=1)
        ]
    lines.append("")
    if restored is not None and restored.storage:
        lines.append(
            "Their sign-ins and site storage (cookies, localStorage) were put back as "
            "they were at the park. If a page asks you to sign in anyway, ask for help then."
        )
    else:
        lines.append(
            "The earlier attempt's sign-ins and site storage are gone, so if a page you "
            "need now asks you to sign in again, ask for help then."
        )
    return lines


def resume_summary(
    entries: list[dict[str, Any]], attempt_id: str, notes: str = "", restored: Restored | None = None
) -> str | None:
    """What a resumed attempt's agent is told, or None when no earlier attempt
    asked for help (a first attempt, or one resumed from a park that had
    nothing to do with a person)."""
    earlier = [entry for entry in entries if entry.get("attempt_id") != attempt_id]
    if not earlier:
        return None
    lines = [
        RESUME_HEADLINE,
        "",
        "An earlier attempt of this task stopped part-way and was parked; this "
        "attempt starts on its working files, restored as they were.",
        "",
        *_browser_lines(restored),
        "",
        ARTIFACTS_NOTE,
        "",
        "The hand-offs to a person so far, oldest first, as the platform recorded them:",
        "",
    ]
    for number, entry in enumerate(earlier, start=1):
        outcome = str(entry.get("outcome") or "waiting")
        text = _OUTCOME_TEXT.get(outcome, _OUTCOME_TEXT["waiting"])
        text = text.replace("{cause}", _clean(entry.get("cause"), 40) or "no cause given")
        line = f'{number}. "{_clean(entry.get("reason"), MAX_REASON_CHARS)}": {text}'
        url = public_url(entry.get("url")) if outcome == "returned" else None
        if url:
            title = _clean(entry.get("title"), 200)
            line += f" It ended on {url}" + (f' ("{title}").' if title else ".")
        lines.append(line)
    if notes:
        lines += ["", f"Your notes from before you stopped ($SWARM_HANDOFF/{NOTES_NAME}):", "", notes]
    lines += [
        "",
        "Before you act, check your working files for what else is already done, "
        "and continue from the first step that is not.",
    ]
    return "\n".join(lines) + "\n"


class NoTunnelTicket(Exception):
    """swarm-api gave no tunnel ticket. `code` is its error code, or why there was none."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GateLost(Exception):
    """ctl.sock failed: closed, timed out, or answered by something that is not the gate.

    `reason` is GATE_UNRESPONSIVE for a call the gate did not answer in time
    (the channel stays open) and GATE_CRASHED otherwise (it does not)."""

    def __init__(self, message: str = "", reason: str = GATE_CRASHED) -> None:
        super().__init__(message)
        self.reason = reason


class GateRefused(GateLost):
    """The gate answered and refused the operation.

    `code` is the gate's refusal word (`restarting`, `qa_identity_host`, ...)
    when it gave one, else None. A word from the gate's fixed list, never
    anything the request carried."""

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message, GATE_UNRESPONSIVE if code == GATE_UNRESPONSIVE else GATE_CRASHED)
        self.code = code


class _GateDown(Exception):
    """A device or theme the browser was not there for: why, and how long the worker waited."""

    def __init__(self, reason: str, waited: float) -> None:
        super().__init__(reason)
        self.reason = reason
        self.waited = waited


def _is(exc: BaseException, name: str) -> bool:
    return any(klass.__name__ == name for klass in type(exc).__mro__)


class Gate:
    """The worker's use of ctl.sock, over L3's `CtlClient`.

    L3's errors (`CtlRefused`, `CtlError`) become `GateRefused` and `GateLost`
    here, matched by name, so this module imports nothing of the gate's at
    import time and runs where the gate's module is not installed.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.registered = False

    def register(self) -> None:
        try:
            self._client.register()
        except Exception as exc:
            raise self._translate(exc) from None
        self.registered = True

    def call(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if op not in GATE_OPS:
            raise GateRefused(f"{op} is not a gate operation")
        if not self.registered:
            raise GateLost("not registered")
        try:
            return dict(self._client.call(op, dict(args)) or {})
        except Exception as exc:
            raise self._translate(exc) from None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        self.registered = False

    @staticmethod
    def _translate(exc: Exception) -> GateLost:
        # The exception's TYPE only: a message could quote what was sent. The
        # gate's error word is its own (`refused:<code>`, ctl.OpRefused).
        if _is(exc, "CtlRefused"):
            error = getattr(exc, "error", None)
            code = None
            if isinstance(error, str) and error.startswith("refused:") and _REFUSAL_CODE.fullmatch(error[8:]):
                code = error[8:]
            return GateRefused(type(exc).__name__, code)
        if _is(exc, "CtlTimeout"):
            return GateLost(type(exc).__name__, GATE_UNRESPONSIVE)
        return GateLost(type(exc).__name__)


_REFUSAL_CODE = re.compile(r"[a-z0-9_]{1,64}")

#: The gate's refusals of one hand-off request, not of the live path, and
#: what the agent is told. `qa_identity_host` (livegate/__main__.py, `pause`;
#: Q7): the page is on the console the browser is signed in to as the QA
#: identity, where a person's own sign-in would hand the agent their
#: workspaces. Before #1333 it was caught as a lost gate: answered
#: `gate_unavailable`, and every later hand-off of the attempt refused.
#: Any other refusal of `pause` still ends the live path, as it did.
REQUEST_REFUSALS: dict[str, str] = {
    "qa_identity_host": (
        "a hand-off is refused on the console this browser is signed in to as the QA identity; "
        "carry on without help"
    ),
}


def _resolve_relay(host: str, port: int) -> Any:
    """The pod's resolver, asked for the relay's address (LB-DNS). A module
    function so the unit suite's conftest can stand in for it."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _relay_address(environ: Mapping[str, str]) -> tuple[str, int] | None:
    """The host and port the gate dials (livegate.tunnel.relay_url), or None if unset-able."""
    try:
        parts = urlsplit(relay_url(environ))
    except ValueError:
        return None
    return (parts.hostname or "", parts.port or 80) if parts.hostname else None


def _probe_cdp(endpoint: str, timeout: float) -> bool:
    """Whether the gate's CDP proxy answers `/json/version` with a websocket URL
    (BROWSER-START). The same discovery Playwright's `connectOverCDP` makes
    first, and side-effect free at the proxy: no client is attributed and
    Chromium is not reached (livegate.cdpproxy `_serve_one`). A module
    function so the unit suite's conftest can stand in for it. Never raises."""
    from urllib.request import ProxyHandler, Request, build_opener

    try:
        request = Request(endpoint.rstrip("/") + "/json/version", method="GET")
        # No proxy: the endpoint is the pod's loopback, never an egress.
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 -- the dispatcher's loopback
            body = json.loads(response.read(64 * 1024).decode("utf-8"))
    except Exception:
        return False
    return isinstance(body, dict) and isinstance(body.get("webSocketDebuggerUrl"), str)


def _default_gate_client(path: str) -> Any:
    """L3's client. Imported here, not at module load, so a worker image
    built before L3 merged still starts; it then has no live path."""
    from .livegate.ctl import CtlClient

    return CtlClient(path)


def signed_message(purpose: str, method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for one worker-only live route (§5.4.1).

    The child path's shape (`children.request_message`) under the route's own
    purpose, so no signature verifies for another route.
    """
    digest = hashlib.sha256(body).hexdigest()
    return f"{purpose}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign_headers(
    key: Any, purpose: str, method: str, path: str, body: bytes, *, now: float
) -> dict[str, str]:
    """The proof headers for one request, signed by the attempt key in this heap.

    `key` is the child path's `AttemptKey`; the private half is read where it
    lives and never leaves this call.
    """
    from .children import PROOF_HEADER, TIMESTAMP_HEADER

    timestamp = str(int(now))
    signature = key._private.sign(signed_message(purpose, method, path, body, timestamp))
    return {PROOF_HEADER: _b64url(signature), TIMESTAMP_HEADER: timestamp}


def expect_refusal(value: str) -> str:
    """Why `value` is not a host a hand-off may name, or "" when it is."""
    if not _HOST.fullmatch(value):
        return "expect is a bare host name: lower-case letters, digits, dots and hyphens"
    return url_refusal(f"https://{value}/")


def public_url(value: Any) -> str | None:
    """§5.9: the top frame's origin and path, redacted, with no query or fragment."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return redact_text(f"{parts.scheme}://{host}{parts.path or '/'}").text


def _write_atomic(path: Path, document: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.write_text(json.dumps(document, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_request(path: Path) -> Any:
    """A spooled JSON object, or None. Never follows a link the agent made."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


class HandoffPath:
    """One attempt's live path: the gate registration, the tunnel, the hand-off spool."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        memory: Any,
        children: Any,
        live: bool | None = None,
        gate_client: Callable[[str], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = utcnow,
        wall: Callable[[], float] = time.time,
        progress: Callable[[str, datetime], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        vault: Callable[[], Any] | None = None,
        resolve: Callable[[str, int], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._monotonic = monotonic
        #: When this poll stops waiting for a starting or restarting gate
        #: (GATE_WAIT_SECONDS, one budget per poll); None until it first waits.
        self._gate_wait_until: float | None = None
        #: Why the agent's browser is not ready although the gate took the
        #: registration (NO_CDP_ENDPOINT, CDP_UNREACHABLE); None while it is.
        self._cdp_missing: str | None = None
        self._sleep = sleep
        self._rand = rand
        #: The pod's resolver (LB-DNS); None is `_resolve_relay`, looked up per call.
        self._resolve = resolve
        #: Writes one `startup_progress` step; True once it is settled (written,
        #: or not this attempt's to write), False to try again on a later tick.
        self._progress = progress
        #: The gate's start-up steps already settled, each once per attempt.
        self.startup_noted: set[str] = set()
        self.log = logger
        self._memory = memory
        self._children = children
        env = os.environ if environ is None else environ
        if live is None:
            profile = RUNNER_PROFILES.get(getattr(cfg, "runner_profile", ""))
            live = bool(getattr(profile, "live_browser", False))
        self.live = live
        self._ctl_path = env.get(GATE_CTL_ENV, "")
        #: The relay the gate dials: the worker shares the gate's pod, so its
        #: environment and its resolver (the pod's dnsConfig) are the gate's.
        self._relay = _relay_address(env)
        #: The class of the relay lookup failure the first open last met
        #: (livegate.tunnel.resolver_class), or None (LB-DNS).
        self.relay_lookup: str | None = None
        self._passthrough = {k: env[k] for k in AGENT_PASSTHROUGH_ENV if env.get(k)}
        self._gate_client = gate_client or _default_gate_client
        self._now = now
        self._wall = wall
        self.gate: Gate | None = None
        #: Why this attempt cannot ask a person for help (no hand-off); None
        #: while it can. NOT why the agent has no browser: that is
        #: `gate_missing`, and only that (NOLIVE-GUARD).
        self.unavailable: str | None = NOT_LIVE if not live else VIEW_NOT_SET_UP
        #: Why the human's live view is not open now; None while it is.
        self.view: str | None = NOT_LIVE if not live else VIEW_NOT_SET_UP
        #: Whether this attempt may open the tunnel at all: set once `setup`
        #: has a protected heap and an attempt key (§5.4.1).
        self._tunnel_allowed = False
        #: Tunnel tries that failed since it was last open, for the retry's log.
        self.tunnel_failures = 0
        #: The open hand-off: request id, when it was opened, its deadline.
        self.active: dict[str, Any] | None = None
        self.requests_made = 0
        #: The gate's requests already raised (or refused) this attempt: its
        #: `status` carries one until a `pause` takes it off.
        self._gate_raised: set[str] = set()
        self.max_handoffs = int(getattr(cfg, "max_handoffs_per_attempt", MAX_HANDOFFS_PER_ATTEMPT))
        self.max_resumes = int(getattr(cfg, "max_human_resumes", MAX_HUMAN_RESUMES))
        self._wiped = False
        #: The tunnel's upkeep (§3.3, LB-R6): reopened how often, when it may
        #: next be asked for, the wait after the next refusal, and whether
        #: swarm-api has said the session is over.
        self.tunnel_reopens = 0
        self._tunnel_retry_at = 0.0
        self._tunnel_backoff = float(TUNNEL_RETRY_SECONDS)
        self._tunnel_ended = False
        #: Reopens tried since the relay said it was restarting (RELAY-ROLL).
        self._restart_tries = 0
        #: The proxied browser's egress ticket (EGRESS-GATE; egress_ticket.py),
        #: and the note `setup` was given, for a failure found on a later poll.
        self.egress = BrowserEgress(cfg, logger=logger, wall=wall)
        self._note: Callable[[dict[str, Any]], Any] | None = None
        #: The QA identity's credential (QI-WORKER; qa_identity.py), asked for
        #: only when the verified task names one (`start_qa_identity`).
        self.qa = QaSignIn(cfg, logger=logger, wall=wall,
                           register_secret=getattr(logger, "register_secret", None))
        #: The QA target's sign-in (VQ-4; qa_target.py): asked for in `prepare`
        #: by a `claude-code-browser` attempt; swarm-api answers 204 without one.
        self.qa_target = QaTargetSignIn(cfg, logger=logger, wall=wall,
                                        register_secret=getattr(logger, "register_secret", None))
        #: The browser-tool state last forwarded to the gate (BSTATUS-API).
        self._browser_tools_sent: dict[str, Any] | None = None
        #: What a park keeps of the browser and a resumed attempt gets back
        #: (§5.5.2, issue #1362). Secret Manager is reached only in a
        #: personal tenant, and built only when it is.
        self._vault_instance: Any = None
        self._vault_factory = vault
        self.browser = BrowserResume(
            cfg, logger=logger, call=self._browser_call, vault=self._vault,
            slots=self._slots, register_secret=getattr(logger, "register_secret", None),
        )
        #: What this attempt's browser got back, once `prepare` has asked.
        self.restored: Restored | None = None

    # -- what the lifecycle asks ---------------------------------------------

    @property
    def offered(self) -> bool:
        """Whether this attempt has a live path an agent may ask through."""
        return self.live and self.unavailable is None

    @property
    def browser_ready(self) -> bool:
        """Whether the agent gets its browser: the gate took this worker's
        registration AND its CDP proxy answered at SWARM_CDP_ENDPOINT
        (BROWSER-START: a registration alone left an attempt with no endpoint
        free to start headless). Independent of the tunnel, the hand-off and
        the attempt key: those are the HUMAN's view (NOLIVE-GUARD, part of
        #1366)."""
        return self.live and self.browser_missing is None

    @property
    def browser_missing(self) -> str | None:
        """Why this live-browser attempt's agent has no browser, or None: the
        gate's refusal code while there is no gate, else NO_CDP_ENDPOINT or
        CDP_UNREACHABLE. What `LIVE_GATE_UNAVAILABLE` names as `live_path`."""
        if not self.live:
            return None
        if self.gate is None:
            return self.unavailable or GATE_UNAVAILABLE
        return self._cdp_missing

    @property
    def gate_missing(self) -> bool:
        """A live-browser attempt whose browser is not ready -- no gate, no
        CDP endpoint, or one that never answered: the agent must not start,
        headless or otherwise (`LIVE_GATE_UNAVAILABLE`)."""
        return self.browser_missing is not None

    @property
    def spooled(self) -> bool:
        """Whether the agent gets a spool at all: every live-browser attempt does,
        so `swarm-handoff` can say WHY it is refused rather than not exist."""
        return self.live

    def setup(self, note: Callable[[dict[str, Any]], Any] | None = None) -> None:
        """Take the gate's slot, then open the tunnel (§3.3, §5.4.2). Never raises.

        Called once the task is RUNNING (the live-session route checks the
        fence on RUNNING) and before the agent exists, so the registration is
        the first the gate sees.
        """
        if not self.live:
            return
        # FIRST, whatever else is wrong: the slot is first-registration-wins,
        # and a worker that leaves it open leaves it to the agent.
        if not self._ctl_path:
            return self._refuse(note, GATE_UNAVAILABLE, "no SWARM_GATE_CTL for this attempt")
        try:
            gate = self._register()
        except (GateLost, ImportError, OSError) as exc:
            return self._refuse(note, GATE_UNAVAILABLE, type(exc).__name__)
        self.gate = gate
        self._note = note
        # BROWSER-START: before anything of the human's view, so the agent's
        # browser is known to answer, or the attempt is known to have none.
        self._await_cdp(note)
        if getattr(self._memory, "status", None) != PROTECTED:
            # §5.4.1: no tunnel ticket and no hand-off. The slot stays taken.
            # No egress ticket either; a proxied browser fails closed.
            self._egress_failed(note, self.egress.unavailable(gate, WORKER_UNPROTECTED))
            return self._refuse(note, WORKER_UNPROTECTED, "the worker's heap is not protected")
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None:
            why = (
                getattr(self._children, "unavailable_reason", None)
                or getattr(self._children, "unavailable", None)
                or "no attempt key registered"
            )
            self._egress_failed(note, self.egress.unavailable(gate, NO_ATTEMPT_KEY))
            return self._refuse(note, NO_ATTEMPT_KEY, str(why))
        self._tunnel_allowed = True
        failure: str | None = None
        try:
            self._open_first_tunnel(gate, key, api)
        except NoTunnelTicket as exc:
            if exc.code in TUNNEL_ENDED_CODES:
                # swarm-api says no session will come: no view and no
                # hand-off. The browser is the agent's all the same, and its
                # egress is set up all the same.
                self._tunnel_ended = True
                self.view = exc.code
                self._egress_failed(note, self.egress.start(gate, key, api))
                return self._refuse(note, NO_LIVE_SESSION, exc.code)
            failure = exc.code
        except GateRefused:
            # The gate is up and answered: its tunnel to the relay did not
            # open (a relay that did not resolve, #1366). The VIEW's failure.
            failure = TUNNEL_REFUSED
        except GateLost as exc:
            # The channel itself failed after the registration: no hand-off
            # can be paused through it. The CDP proxy is the gate's, not the
            # channel's, so the agent keeps its browser.
            self.view = GATE_UNAVAILABLE
            return self._refuse(note, GATE_UNAVAILABLE, type(exc).__name__)
        # EGRESS-GATE: a proxied browser's first ticket, still before the agent exists.
        self._egress_failed(note, self.egress.start(gate, key, api))
        # NOLIVE-GUARD: a hand-off is offered either way. A tunnel that did not
        # open is retried on every poll past its backoff (`_keep_tunnel`), the
        # hand-off's own poll included, which is exactly when a person needs it.
        self.unavailable = None
        if failure is None:
            self.view = None
            self.log.info("live path open for this attempt")
            return
        self.view = failure
        self._tunnel_failed(failure)
        self._view_unavailable(note, failure)

    def _register(self) -> Gate:
        """Take the gate's slot, waiting (bounded) out a channel that failed.

        BROWSER-START (part of #1366). A `GateLost` -- ctl.sock there but not
        accepting, as while a gate restarts, or a connect that failed under a
        burst of pod starts -- is tried again, with a fresh client, on a
        jittered delay from BROWSER_WAIT_FIRST_SECONDS doubling to
        BROWSER_WAIT_MAX_SECONDS, for up to BROWSER_WAIT_SECONDS. A
        `GateRefused` raises at once: the gate answered no, and a restarted
        gate refuses every registration. Raises the last failure.
        """
        started = self._wall()
        step = BROWSER_WAIT_FIRST_SECONDS
        tries = 0
        while True:
            try:
                gate = Gate(self._gate_client(self._ctl_path))
                gate.register()
                return gate
            except GateRefused:
                raise
            except (GateLost, OSError) as exc:
                tries += 1
                low, spread = TUNNEL_JITTER
                delay = step * (low + spread * self._rand())
                if self._wall() + delay - started > BROWSER_WAIT_SECONDS:
                    self.log.warning("the live gate did not take the registration in time",
                                     error=type(exc).__name__, tries=tries)
                    raise
                self.log.warning("the live gate did not take the registration; retrying",
                                 error=type(exc).__name__, tries=tries, retry_seconds=round(delay, 1))
                self._sleep(delay)
                step = min(step * 2, BROWSER_WAIT_MAX_SECONDS)

    def _await_cdp(self, note: Callable[[dict[str, Any]], Any] | None) -> None:
        """Wait, bounded, for the CDP proxy at SWARM_CDP_ENDPOINT to answer (BROWSER-START).

        Sets `_cdp_missing` when there is no endpoint to give the agent, or
        when it has not answered `/json/version` within BROWSER_WAIT_SECONDS
        (the same jittered, doubling delay as `_register`). Either makes
        `gate_missing` true, and `Worker._prepare` then fails the attempt
        retryably before the agent exists: never a headless run. Said on the
        task's events through `note`. Never raises.
        """
        endpoint = self._passthrough.get("SWARM_CDP_ENDPOINT", "")
        if not endpoint:
            self._cdp_missing = NO_CDP_ENDPOINT
        else:
            started = self._wall()
            step = BROWSER_WAIT_FIRST_SECONDS
            tries = 0
            while True:
                tries += 1
                try:
                    # Looked up per call, so the unit suite's conftest stands in.
                    answered = bool(_probe_cdp(endpoint, CDP_PROBE_SECONDS))
                except Exception:
                    answered = False
                if answered:
                    if tries > 1:
                        self.log.info("the live browser's CDP endpoint answered", tries=tries)
                    return
                low, spread = TUNNEL_JITTER
                delay = step * (low + spread * self._rand())
                if self._wall() + delay - started > BROWSER_WAIT_SECONDS:
                    self._cdp_missing = CDP_UNREACHABLE
                    break
                self.log.warning("the live browser's CDP endpoint did not answer; retrying",
                                 tries=tries, retry_seconds=round(delay, 1))
                self._sleep(delay)
                step = min(step * 2, BROWSER_WAIT_MAX_SECONDS)
        self.log.warning("the live browser is not ready; the agent will not be started",
                         live_path=self._cdp_missing)
        if note is None:
            return
        try:
            note({"phase": "live_browser", "live_path": self._cdp_missing, "reason": self._cdp_missing,
                  "message": f"live browser unavailable: {self._cdp_missing}; the agent was not "
                             "started, and the attempt is retried"})
        except Exception:  # a note must never stop the attempt
            pass

    def _view_unavailable(self, note: Callable[[dict[str, Any]], Any] | None, code: str) -> None:
        """The human's view did not open; the agent's browser runs. Never raises."""
        if note is None:
            return
        lookup = f" (relay name lookup: {self.relay_lookup})" if self.relay_lookup else ""
        try:
            note({"phase": "live_view", "live_path": "view_unavailable", "reason": code,
                  "message": f"live view unavailable: {code}{lookup}; the agent's browser runs, "
                             "and the view is retried"})
        except Exception:  # a note must never stop the attempt
            pass

    def _open_first_tunnel(self, gate: Gate, key: Any, api: Any) -> None:
        """`_open_tunnel`, retrying a refusal the relay's name lookup explains (LB-DNS, part of #1366).

        ctl.sock answers every failed `tunnel_ticket` with the same "failed"
        (the gate's `tunnel_refused` record names the cause; the worker never
        sees it), so on a refusal the worker asks the resolver itself, which
        is the gate's (same pod). While the relay's name does not resolve the
        open is tried again -- a fresh ticket each time -- on the jittered,
        doubling delay, for up to RELAY_LOOKUP_RETRY_SECONDS, each try logged
        with the failure's class. A refusal while the name resolves, and
        every other failure, raises at once, as before. Raises what
        `_open_tunnel` raises.
        """
        started = self._wall()
        step = RELAY_LOOKUP_RETRY_FIRST_SECONDS
        tries = 0
        while True:
            try:
                self._open_tunnel(gate, key, api)
            except GateRefused:
                lookup = self._relay_lookup()
                self.relay_lookup = lookup
                if lookup is None:
                    raise
                tries += 1
                low, spread = TUNNEL_JITTER
                delay = step * (low + spread * self._rand())
                if self._wall() + delay - started > RELAY_LOOKUP_RETRY_SECONDS:
                    self.log.warning("relay name still does not resolve; the live view is retried on later polls",
                                     resolver=lookup, tries=tries)
                    raise
                self.log.warning("relay name did not resolve; retrying the live tunnel",
                                 resolver=lookup, tries=tries, retry_seconds=round(delay, 1))
                self._sleep(delay)
                step = min(step * 2, RELAY_LOOKUP_RETRY_MAX_SECONDS)
            else:
                self.relay_lookup = None
                return

    def _relay_lookup(self) -> str | None:
        """The class of the relay name's lookup failure now (livegate.tunnel
        `resolver_class`), or None when it resolves, fails some other way, or
        there is no relay address to ask for. Never raises; never longer than
        RELAY_PROBE_SECONDS: a lookup still unanswered then is `resolver_busy`."""
        if self._relay is None:
            return None
        host, port = self._relay
        resolve = self._resolve or _resolve_relay
        found: list[str | None] = []

        def probe() -> None:
            try:
                resolve(host, port)
            except Exception as exc:  # anything but a gaierror is not a lookup failure
                found.append(resolver_class(exc))
            else:
                found.append(None)

        # A thread, because getaddrinfo cannot be cancelled: one left blocked
        # past the bound ends with the resolver's own give-up, and holds nothing.
        thread = threading.Thread(target=probe, name="relay-lookup", daemon=True)
        thread.start()
        thread.join(RELAY_PROBE_SECONDS)
        if thread.is_alive():
            return RESOLVER_BUSY
        return found[0] if found else None

    def _open_tunnel(self, gate: Gate, key: Any, api: Any) -> None:
        """§3.3 steps 1-2: a tunnel ticket from swarm-api, handed to the gate.

        The route checks the fence (RUNNING under this lease and generation,
        no cancel) each time, and answers an attempt's open session with a
        fresh one-use ticket for it -- which is what a reopen needs. Raises
        `NoTunnelTicket` (the route's code) or `GateLost`.
        """
        body = json.dumps(
            {
                "tenant_id": self.cfg.tenant_id,
                "task_id": self.cfg.task_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
            }
        ).encode("utf-8")
        path = f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/live-session"
        try:
            headers = sign_headers(key, LIVE_SESSION_PURPOSE, "POST", path, body, now=self._wall())
            _, payload = api.call("POST", path, body=body, headers=headers)
        except Exception as exc:
            raise NoTunnelTicket(str(getattr(exc, "code", None) or type(exc).__name__)) from None
        ticket = payload.get("ticket") if isinstance(payload, dict) else None
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise NoTunnelTicket("no ticket in the answer")
        try:
            # The ticket goes to the gate and nowhere else: not a log line,
            # not the environment, not a file (§3.3 step 2).
            gate.call("tunnel_ticket", {"ticket": ticket, "session_id": session_id})
        finally:
            ticket = None

    def _egress_failed(self, note: Callable[[dict[str, Any]], Any] | None, cause: str | None) -> None:
        """A proxied browser has no egress: say so on the attempt's events. Never raises."""
        if cause is None or note is None:
            return
        try:
            note({"phase": "browser_egress", "live_path": "egress_failed_closed",
                  "reason": f"browser egress {cause}: pages do not load, and there is no direct fallback"})
        except Exception:  # a note must never stop the attempt
            pass

    def give_cf_access(self, items: list[dict[str, Any]]) -> str:
        """The attempt's Cloudflare Access tokens to the gate, before the agent
        exists (CF-ACCESS-TOKEN; `cf_access.hand_over`). Never raises; `items`
        is emptied. A heap the agent could read hands nothing over."""
        gate = self.gate if self.live else None
        protected = getattr(self._memory, "status", None) == PROTECTED
        return cf_access.hand_over(gate, items, self.log, protected=protected)

    def start_qa_identity(self, task: Any) -> str | None:
        """The QA identity's first credential, before the agent exists (§4.5). Never raises.

        `task` is the VERIFIED task document: a task whose input names no
        identity asks nothing at all. The lifecycle calls it (`_prepare`,
        STEP 4b) once the gate is registered and before the agent exists,
        rather than `prepare` as §4.5 has it: `prepare` returns the agent's
        variables, and only `_prepare` can end the attempt before the agent
        starts. A second call asks nothing again. Returns
        the cause that must fail the attempt before its agent starts
        (`qa_identity.FAILS_THE_ATTEMPT`: the first ask refused as not
        granted or unconfigured), else None. Any other failure leaves the
        browser signed out of the console and the agent told why
        (`QA_IDENTITY_NOTICE_NAME`); the task is not failed for it.
        """
        try:
            identity = requested(task)
            gate = self.gate if self.live else None
            unavailable = None
            if getattr(self._memory, "status", None) != PROTECTED:
                unavailable = QA_WORKER_UNPROTECTED
            key = getattr(self._children, "_key", None)
            api = getattr(self._children, "_api", None)
            fails = self.qa.start(gate, key, api, identity, unavailable)
        except Exception as exc:  # the QA identity never crashes the attempt
            self.log.warning("qa identity start failed", error=type(exc).__name__)
            return None
        self._qa_failed(self._note, self.qa.cause if self.qa.state == "failed" else None)
        return fails

    def _qa_failed(self, note: Callable[[dict[str, Any]], Any] | None, cause: str | None) -> None:
        """The browser holds no QA credential: say so on the attempt's events. Never raises."""
        if cause is None or note is None:
            return
        try:
            note({"phase": "qa_identity", "live_path": "qa_identity_signed_out",
                  "reason": f"qa identity {cause}: the browser is signed out of the console"})
        except Exception:  # a note must never stop the attempt
            pass

    def _qa_target_channel(self) -> tuple[Any, Any, Any, str | None]:
        """(gate, attempt key, swarm-api, why nothing may be asked) for the QA target."""
        gate = self.gate if self.live and not self._wiped else None
        unavailable = None if getattr(self._memory, "status", None) == PROTECTED else WORKER_UNPROTECTED
        return gate, getattr(self._children, "_key", None), getattr(self._children, "_api", None), unavailable

    def _keep_qa_identity(self, root: Path) -> None:
        """Renew the QA credential when due, and keep the agent's notice current."""
        if self.gate is None or self._wiped:
            return
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        self._qa_failed(self._note, self.qa.keep(self.gate, key, api))
        self._write_qa_notice(root)

    def _write_qa_notice(self, root: Path) -> None:
        target = root / QA_IDENTITY_NOTICE_NAME
        text = self.qa.notice()
        if text is None:
            target.unlink(missing_ok=True)
            return
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".{QA_IDENTITY_NOTICE_NAME}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)

    def _keep_egress(self, root: Path) -> None:
        """Renew the egress ticket when due, and keep the agent's notice current."""
        if self.gate is None or self._wiped:
            return
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        self._egress_failed(self._note, self.egress.keep(self.gate, key, api))
        self._write_egress_notice(root)

    def _write_egress_notice(self, root: Path) -> None:
        target = root / EGRESS_NOTICE_NAME
        text = self.egress.notice()
        if text is None:
            target.unlink(missing_ok=True)
            return
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".{EGRESS_NOTICE_NAME}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)

    def _keep_tunnel(self, status: Mapping[str, Any]) -> None:
        """Reopen the gate's tunnel when its `status` says it closed (§3.3, LB-R6). Never raises.

        The relay restarting, a rollout, or a link the relay dropped ends the
        tunnel while the session stays open: swarm-api still serves its last
        origin and device, the console draws LIVE over them, and every viewer
        the relay is asked to admit has no tunnel to meet (re-QA pass 8,
        2026-10-12). Reads the status the poll already made; at most one route
        call, and only past the backoff.
        """
        # "none": a tunnel that never opened this attempt (NOLIVE-GUARD).
        if (not self._tunnel_allowed or self.gate is None or self._wiped or self._tunnel_ended
                or status.get("tunnel") not in ("closed", "none") or status.get("browser") != "running"
                or self._wall() < self._tunnel_retry_at):
            return
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None:
            return
        # RELAY-ROLL: the relay said it is restarting. Reopen now, and while
        # the new relay is not up yet, again on the short jittered schedule.
        restart = status.get("tunnel_close_code") == CLOSE_RESTARTING
        started = self._wall()
        while True:
            try:
                self._open_tunnel(self.gate, key, api)
            except NoTunnelTicket as exc:
                if exc.code in TUNNEL_ENDED_CODES:
                    self._tunnel_ended = True
                    self.view = exc.code
                    self.log.info("live tunnel not reopened: the session has ended", code=exc.code)
                    return
                failure = exc.code
            except GateRefused:
                failure = TUNNEL_REFUSED
            except GateLost as exc:
                failure = type(exc).__name__
            else:
                break
            if not restart:
                self._tunnel_failed(failure)
                return
            delay = reconnect_delay(CLOSE_RESTARTING, self._restart_tries)
            self._restart_tries += 1
            if self._wall() + delay - started > RESTART_BURST_SECONDS:
                self._tunnel_retry_at = self._wall() + delay
                self.log.warning("live tunnel not reopened after the relay restarted; trying again",
                                 code=failure, tries=self._restart_tries)
                return
            self._sleep(delay)
        self._tunnel_retry_at = 0.0
        self._tunnel_backoff = float(TUNNEL_RETRY_SECONDS)
        self._restart_tries = 0
        late = self.view is not None and self.view != VIEW_NOT_SET_UP
        self.view = None
        if late and status.get("tunnel") == "none":
            # The first open, late: the relay resolved after the agent started.
            self.log.info("live view opened", after_failures=self.tunnel_failures)
            self.tunnel_failures = 0
            return
        self.tunnel_failures = 0
        self.tunnel_reopens += 1
        if restart:
            self.log.info("live tunnel reopened", reopened=self.tunnel_reopens, after="relay_restart")
        else:
            self.log.info("live tunnel reopened", reopened=self.tunnel_reopens)

    def _tunnel_failed(self, code: str) -> None:
        """Schedule the next tunnel try: the backoff, jittered, doubling to its
        cap, for the attempt's life (only swarm-api's ended codes stop it).
        Logged by `code` -- a swarm-api error code, `tunnel_refused` or an
        exception's class name -- never a ticket."""
        self.view = code
        self.tunnel_failures += 1
        low, spread = TUNNEL_JITTER
        delay = self._tunnel_backoff * (low + spread * self._rand())
        self._tunnel_retry_at = self._wall() + delay
        self.log.warning("live view not open; retrying", code=code, tries=self.tunnel_failures,
                         retry_seconds=round(delay, 1))
        self._tunnel_backoff = min(self._tunnel_backoff * 2, float(TUNNEL_RETRY_MAX_SECONDS))

    def _refuse(self, note: Callable[[dict[str, Any]], Any] | None, code: str, why: str) -> None:
        self.unavailable = code
        if self.gate is None:
            self.log.warning("no live browser for this attempt: the gate is not ready",
                             live_path=code, reason=why)
        else:
            # NOLIVE-GUARD: the gate is up, so the agent keeps its browser;
            # only the human's side (the view, the hand-off) is missing.
            self.log.warning("no hand-off for this attempt; the agent keeps its browser",
                             live_path=code, reason=why)
        if note is not None:
            try:
                note({"phase": "live_session", "live_path": code, "reason": why})
            except Exception:  # a note must never stop the attempt
                pass

    def spool(self, work: Path) -> Path:
        return Path(work) / SPOOL_DIR_NAME

    # -- the browser across a park (§5.5.2, browser_resume.py) -----------------

    def _browser_call(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.gate is None:
            raise GateLost("no gate")
        return self.gate.call(op, args)

    def _vault(self) -> Any:
        if self._vault_instance is None:
            if self._vault_factory is not None:
                self._vault_instance = self._vault_factory()
            else:
                from .browser_login import SecretManagerVault

                self._vault_instance = SecretManagerVault(self.cfg.project_id)
        return self._vault_instance

    def _slots(self) -> Any:
        from .browser_login import SlotApi

        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None:
            raise RuntimeError(NO_ATTEMPT_KEY)
        return SlotApi(
            api, tenant_id=self.cfg.tenant_id, task_id=self.cfg.task_id, attempt_id=self.cfg.attempt_id,
            lease_id=self.cfg.lease_id, generation=self.cfg.generation, sign=key._private.sign,
            clock=self._wall,
        )

    def _browser_reachable(self) -> bool:
        """A gate this worker registered on, and a heap it can hold a session in (§5.4.1)."""
        return (self.gate is not None and self.gate.registered and not self._wiped
                and getattr(self._memory, "status", None) == PROTECTED)

    def _keep_browser(self, work: Path) -> None:
        """At park, before the wipe: keep the tabs and, in a personal tenant, the session. Never raises."""
        if not self._browser_reachable():
            return
        try:
            self.browser.capture(self.spool(work))
        except Exception as exc:
            self.log.warning("could not keep the browser for the resumed attempt", error=type(exc).__name__)

    def _restore_browser(self, root: Path) -> Restored | None:
        """Before the runner starts: reopen what the parked attempt kept. Never raises."""
        if not self._browser_reachable():
            return None
        try:
            return self.browser.restore(root)
        except Exception as exc:
            self.log.warning("could not put the earlier attempt's browser back", error=type(exc).__name__)
            return None

    def prepare(self, work: Path) -> dict[str, str]:
        """Create the spool and return the agent's variables, or nothing."""
        if not self.spooled:
            return {}
        root = self.spool(work)
        for name in ("requests", "devices", "themes", "responses"):
            (root / name).mkdir(parents=True, exist_ok=True)
        marker = root / "unavailable"
        if self.offered:
            marker.unlink(missing_ok=True)
        else:
            # Rewritten every attempt: a restored checkpoint's marker is not
            # this attempt's answer.
            _write_atomic(marker, {"code": self.unavailable})
        guide = root / GUIDE_NAME
        tmp = root / f".{GUIDE_NAME}.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(AGENT_GUIDE, encoding="utf-8")
        os.replace(tmp, guide)
        # Before the summary, which names what came back; before the runner,
        # whose agent must find the tabs open on its first connection.
        if self.restored is None:
            self.restored = self._restore_browser(root)
        self._prepare_resume(root)
        # Rewritten every attempt, as `unavailable` is: present only while
        # THIS attempt's proxied browser has no egress.
        self._write_egress_notice(root)
        # Likewise present only while THIS attempt's QA credential is missing.
        self._write_qa_notice(root)
        # VQ-4: the QA target's sign-in, after the gate registered and before
        # the runner starts (visual-qa-any-app.md §2.2). Once per attempt.
        gate, key, api, unavailable = self._qa_target_channel()
        self.qa_target.start(gate, key, api, root, unavailable)
        # This attempt's runner writes its own; a restored checkpoint's is
        # an earlier attempt's registration, never forwarded as this one's.
        (root / browser_tools.STATE_NAME).unlink(missing_ok=True)
        env = {HANDOFF_ENV: str(root)}
        # NOLIVE-GUARD: the agent's browser follows the GATE, never the
        # tunnel or the hand-off (#1366: 8 of 60 attempts lost it to a relay
        # that did not resolve).
        if self.browser_ready:
            env.update(self._passthrough)
        return env

    def _prepare_resume(self, root: Path) -> None:
        """RESUME-PROGRESS: tell a resumed attempt what the earlier ones did.

        Writes `resumed.md` when the restored ledger holds an earlier
        attempt's hand-offs, and removes it otherwise, so the file is this
        attempt's answer or absent. And an earlier attempt's answers in
        `responses/` are removed: they are in the ledger, and a `wait` on the
        same id must not read an old `expired` as its own. Never raises.
        """
        try:
            entries = _load_history(root)
            summary = resume_summary(entries, str(self.cfg.attempt_id), _read_notes(root), self.restored)
            target = root / RESUME_NAME
            if summary is None:
                target.unlink(missing_ok=True)
            else:
                tmp = root / f".{RESUME_NAME}.tmp"
                tmp.unlink(missing_ok=True)
                tmp.write_text(summary, encoding="utf-8")
                os.replace(tmp, target)
            stale = {
                entry["request_id"] for entry in entries
                if entry["attempt_id"] != self.cfg.attempt_id and REQUEST_ID.fullmatch(entry["request_id"])
            }
            for request_id in stale:
                (root / "responses" / f"{request_id}.json").unlink(missing_ok=True)
            last = root / ".last"
            if last.is_file() and not last.is_symlink() and last.read_text(encoding="utf-8").strip() in stale:
                last.unlink()
            if summary is not None:
                self.log.info("resumed attempt told of earlier hand-offs",
                              handoffs=sum(1 for e in entries if e["attempt_id"] != self.cfg.attempt_id))
        except Exception as exc:  # never costs the attempt
            self.log.warning("could not prepare the resume summary", error=type(exc).__name__)

    def _record(self, root: Path, request_id: str, fields: Mapping[str, Any]) -> None:
        """Add or update this attempt's entry for `request_id` in the ledger. Never raises."""
        try:
            entries = _load_history(root)
            attempt_id = str(self.cfg.attempt_id)
            entry = next(
                (e for e in entries if e["attempt_id"] == attempt_id and e["request_id"] == request_id),
                None,
            )
            if entry is None:
                entry = {
                    "request_id": request_id,
                    "attempt_id": attempt_id,
                    "generation": self.cfg.generation,
                }
                entries.append(entry)
            entry.update(
                {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fields.items()}
            )
            _write_atomic(root / HISTORY_NAME, entries[-MAX_HISTORY:])
        except Exception as exc:  # the ledger never costs the attempt
            self.log.warning("could not record the hand-off in the ledger", error=type(exc).__name__)

    # -- the poll --------------------------------------------------------------

    def tick(self, work: Path, fetch_task: Callable[[], Mapping[str, Any]]) -> str | None:
        """Answer the spool and watch the open hand-off. Never raises.

        Returns HOLD_EXPIRED or GATE_LOST when the lifecycle must park, None
        otherwise. Bounded: one gate call per spooled file plus one status
        call, and at most one route call per request.
        """
        if not self.spooled:
            return None
        root = self.spool(work)
        self._gate_wait_until = None
        try:
            self._keep_egress(root)
        except Exception as exc:  # egress upkeep never crashes the attempt
            self.log.warning("browser egress upkeep failed", error=type(exc).__name__)
        try:
            self._keep_qa_identity(root)
        except Exception as exc:  # nor does the QA identity's
            self.log.warning("qa identity upkeep failed", error=type(exc).__name__)
        # VQ-4: a running sign-in followed, `swarm-handoff sign-in` answered. Never raises.
        self.qa_target.keep(*self._qa_target_channel()[:3], root)
        try:
            self._answer_devices(root)
            self._answer_themes(root)
            self._forward_browser_tools(root)
            if self.active is None:
                self._answer_requests(root)
            elif self.active.get("origin") == GATE_ORIGIN:
                self._join_requests(root)
            if self.active is not None:
                return self._watch(root, fetch_task)
            if self.offered:
                self._raise_for_gate(root, self._note_last_episode(root))
            else:
                self._poll_startup()
        except Exception as exc:  # the hand-off never crashes the attempt
            self.log.warning("hand-off poll failed", error=type(exc).__name__)
        return None

    def _respond(self, root: Path, request_id: str, document: Mapping[str, Any]) -> None:
        _write_atomic(root / "responses" / f"{request_id}.json", {"request_id": request_id, **document})

    def _refusal(self, root: Path, request_id: str, code: str, message: str) -> None:
        self._respond(
            root, request_id,
            {"outcome": "refused", "exit": EXIT_REFUSED, "code": code, "message": message},
        )
        self.log.info("hand-off request refused", code=code)

    def _answer_requests(self, root: Path) -> None:
        requests = root / "requests"
        if not requests.is_dir():
            return
        for path in sorted(requests.glob("*.json")):
            request_id = path.stem
            data = _read_request(path)
            path.unlink(missing_ok=True)
            if not REQUEST_ID.fullmatch(request_id):
                continue
            self._open(root, request_id, data)
            if self.active is not None:
                # One at a time: a request behind an open one is answered
                # once that one ends, on a later poll.
                break

    def _raise_for_gate(self, root: Path, status: Mapping[str, Any]) -> None:
        """CAPTCHA-HANDOFF: the gate's request in its `status`, raised as the agent's would be.

        The same `_open` -- validation, the redaction filter, the per-attempt
        cap, the pause, the route -- with `origin: "gate"`. `.last` names it,
        so the agent's `swarm-handoff wait` waits on it, and a refusal (the
        cap) is answered there like any other. Each request once.
        """
        challenge = status.get("challenge")
        if not isinstance(challenge, Mapping):
            return
        request_id = challenge.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) \
                or request_id in self._gate_raised:
            return
        self._gate_raised.add(request_id)
        root.mkdir(parents=True, exist_ok=True)
        last = root / ".last"
        tmp = root / ".last.tmp"
        tmp.unlink(missing_ok=True)
        tmp.write_text(request_id, encoding="utf-8")
        os.replace(tmp, last)
        self.log.info("the gate saw a challenge on the agent's page; asking a person")
        self._open(root, request_id, {"reason": challenge.get("reason"), "expect": challenge.get("expect")},
                   origin=GATE_ORIGIN)

    def _join_requests(self, root: Path) -> None:
        """The agent's own requests while the gate's hand-off is open: answered by it.

        The runner's note still tells an agent to ask on a CAPTCHA; one that
        does, after the gate already asked, waits on the same person rather
        than queueing a second hand-off for a challenge they will have solved.
        """
        active = self.active
        assert active is not None
        requests = root / "requests"
        if not requests.is_dir():
            return
        for path in sorted(requests.glob("*.json")):
            request_id = path.stem
            path.unlink(missing_ok=True)
            if not REQUEST_ID.fullmatch(request_id) or request_id == active["request_id"]:
                continue
            active.setdefault("joined", []).append(request_id)
            deadline = active.get("deadline")
            self._respond(root, request_id, {
                "outcome": "waiting", "origin": GATE_ORIGIN, "joined": active["request_id"],
                "message": active.get("message"),
                "deadline": deadline.isoformat() if isinstance(deadline, datetime) else None})
            self.log.info("the agent's request joins the gate's open hand-off")

    def _answer_all(self, root: Path, active: Mapping[str, Any], document: Mapping[str, Any]) -> None:
        """Answer the open hand-off's request and every request that joined it."""
        for request_id in [active["request_id"], *active.get("joined", [])]:
            self._respond(root, request_id, document)

    def _open(self, root: Path, request_id: str, data: Any, *, origin: str = AGENT_ORIGIN) -> None:
        if not self.offered:
            return self._refusal(
                root, request_id, self.unavailable or NO_LIVE_SESSION,
                "this attempt has no live browser path; carry on without help",
            )
        if not isinstance(data, dict):
            return self._refusal(root, request_id, "request_invalid", "the request is not a JSON object")
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return self._refusal(root, request_id, "reason_invalid", "reason is required")
        reason = reason.strip()
        if len(reason) > MAX_REASON_CHARS:
            return self._refusal(
                root, request_id, "reason_invalid", f"reason is at most {MAX_REASON_CHARS} characters"
            )
        # Redacted BEFORE it leaves this process (§4.1 step 1).
        reason = redact_text(reason).text[:MAX_REASON_CHARS]
        expect = data.get("expect")
        if expect is not None:
            if not isinstance(expect, str):
                return self._refusal(root, request_id, "expect_invalid", "expect is a host name")
            expect = expect.strip().lower() or None
            if expect:
                why = expect_refusal(expect)
                if why:
                    return self._refusal(root, request_id, "expect_invalid", why)
        if self.requests_made >= self.max_handoffs:
            return self._refusal(
                root, request_id, "handoff_limit",
                f"this attempt has asked for help {self.requests_made} times; the cap is "
                f"{self.max_handoffs}",
            )
        gate = self.gate
        assert gate is not None
        # §4.1 step 2: the agent's CDP is held BEFORE the wait is recorded, so
        # there is no moment a person is told to come while the agent can act.
        try:
            gate.call("pause", {"request_id": request_id, "expect": expect})
        except GateRefused as exc:
            if exc.code not in REQUEST_REFUSALS:
                self.unavailable = GATE_UNAVAILABLE
                self.log.warning("the gate did not pause the agent", error=type(exc).__name__)
                return self._refusal(root, request_id, GATE_UNAVAILABLE, "the live browser is unavailable")
            # The gate said no to THIS request (#1333): answered with its own
            # word, and the live path stays open. Not counted: no hold opened.
            return self._refusal(root, request_id, exc.code, REQUEST_REFUSALS[exc.code])
        except GateLost as exc:
            self.unavailable = GATE_UNAVAILABLE
            self.log.warning("the gate did not pause the agent", error=type(exc).__name__)
            return self._refusal(root, request_id, GATE_UNAVAILABLE, "the live browser is unavailable")
        self.requests_made += 1
        fields: dict[str, Any] = {"request_id": request_id, "reason": reason, "expect": expect}
        if origin == GATE_ORIGIN:
            fields["origin"] = GATE_ORIGIN
        try:
            _, payload = self._route(f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/handoff", fields)
        except Exception as exc:
            code = str(getattr(exc, "code", None) or type(exc).__name__)
            self._resume_quietly()
            return self._refusal(root, request_id, code, "swarm-api did not record the request")
        wait = payload.get("human_wait") if isinstance(payload, dict) else None
        deadline = _as_datetime((wait or {}).get("deadline")) if isinstance(wait, dict) else None
        now = self._now()
        self.active = {
            "request_id": request_id,
            "opened_at": now,
            "opened_wall": self._wall(),
            "deadline": deadline,
            "origin": origin,
        }
        answer: dict[str, Any] = {"outcome": "waiting", "deadline": deadline.isoformat() if deadline else None}
        if origin == GATE_ORIGIN:
            self.active["message"] = GATE_WAITING.format(host=expect or "this page")
            answer.update(origin=GATE_ORIGIN, message=self.active["message"])
        self._respond(root, request_id, answer)
        self._record(
            root, request_id,
            {"reason": reason, "expect": expect, "requested_at": now, "outcome": "waiting",
             "origin": origin},
        )
        self.log.info("hand-off opened; the agent's browser is paused for a person")

    def _route(self, path: str, fields: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None:
            raise RuntimeError(NO_ATTEMPT_KEY)
        body = json.dumps(
            {
                "tenant_id": self.cfg.tenant_id,
                "attempt_id": self.cfg.attempt_id,
                "lease_id": self.cfg.lease_id,
                "generation": self.cfg.generation,
                **fields,
            }
        ).encode("utf-8")
        headers = sign_headers(key, HANDOFF_PURPOSE, "POST", path, body, now=self._wall())
        return api.call("POST", path, body=body, headers=headers)

    def _watch(self, root: Path, fetch_task: Callable[[], Mapping[str, Any]]) -> str | None:
        active = self.active
        assert active is not None and self.gate is not None
        status: Mapping[str, Any] = {}
        try:
            status = self.gate.call("status", {})
        except GateRefused:
            pass
        except GateLost:
            # §5.4.2: the channel dropped (a restarted gate refuses us for
            # good). The live path is over; the open hand-off ends as if the
            # hold had expired.
            self.unavailable = GATE_UNAVAILABLE
            return GATE_LOST
        self.note_startup(status)
        # A person is being waited for: exactly when the view must be there.
        self._keep_tunnel(status)
        task = fetch_task() or {}
        wait = task.get("human_wait")
        if not isinstance(wait, dict) or wait.get("request_id") != active["request_id"]:
            self._returned(root)
            return None
        deadline = _as_datetime(wait.get("deadline")) or active.get("deadline")
        if deadline is None:
            return None
        active["deadline"] = deadline
        grace = CONTROLLED_GRACE_SECONDS if wait.get("controller") else 0
        if (self._now() - deadline).total_seconds() >= grace:
            return HOLD_EXPIRED
        return None

    def _returned(self, root: Path) -> None:
        """§4.2 step 3: control came back. Resume the agent; answer `wait`."""
        active = self.active
        assert active is not None and self.gate is not None
        result: dict[str, Any] = {}
        try:
            # The gate clears every password input and restores the agent's
            # page scripts before it lets the agent's commands through.
            result = self.gate.call("resume", {"request_id": active["request_id"]})
        except GateLost as exc:
            self.log.warning("the gate did not confirm the resume", error=type(exc).__name__)
        summary = {
            "outcome": "returned",
            "exit": EXIT_RETURNED,
            "held_seconds": int(max(0.0, self._wall() - active["opened_wall"])),
            "url": public_url(result.get("url")),
            "title": redact_text(str(result.get("title") or "")[:200]).text,
        }
        self._answer_all(root, active, summary)
        _write_atomic(root / "status.json", summary)
        self._record(root, active["request_id"], {**summary, "ended_at": self._now()})
        self.active = None
        self.log.info("hand-off returned; the agent's browser is resumed",
                      held_seconds=summary["held_seconds"])

    def _note_last_episode(self, root: Path) -> Mapping[str, Any]:
        """§4.2 (Q6): a person may take control unasked. `status` reports the
        last episode the gate knows of, in `wait`'s shape. Returns the status
        read ({} with none), for the gate's own request (`_raise_for_gate`)."""
        if not self.offered or self.gate is None:
            return {}
        try:
            result = self.gate.call("status", {})
        except GateLost:
            return {}
        if not isinstance(result, Mapping):
            return {}
        self.note_startup(result)
        self._keep_tunnel(result)
        episode = result.get("last_episode")
        if not isinstance(episode, dict):
            return result
        _write_atomic(
            root / "status.json",
            {
                "outcome": "returned",
                "held_seconds": int(episode.get("held_seconds") or 0),
                "url": public_url(episode.get("url")),
                "title": redact_text(str(episode.get("title") or "")[:200]).text,
            },
        )
        return result

    # -- the browser's start-up steps (STARTUP-API) -----------------------------

    def _startup_outstanding(self) -> list[str]:
        """The gate's steps still to settle. Without a live path there is no
        tunnel, so no stream and no first frame: only the browser's two."""
        wanted = GATE_STARTUP_STEPS if self.offered else GATE_STARTUP_STEPS[:2]
        return [step for step in wanted if step not in self.startup_noted]

    def _poll_startup(self) -> None:
        """One `status` read for the start-up steps, on a gate with no live
        path (whose tick reads no status otherwise). Stops once they are settled."""
        if self.gate is None or self._progress is None or not self._startup_outstanding():
            return
        try:
            self.note_startup(self.gate.call("status", {}))
        except GateLost:
            return

    def note_startup(self, status: Any) -> None:
        """Write each start-up step the gate's `status` dates, once per attempt. Never raises.

        The time written is the gate's own (`startup`, epoch seconds), so a
        tick that reads it late still records when the step happened. A step
        whose write fails is tried again on the next tick's status.
        """
        if self._progress is None or not isinstance(status, Mapping):
            return
        times = status.get("startup")
        if not isinstance(times, Mapping):
            return
        for step in self._startup_outstanding():
            value = times.get(step)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                at = datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
            try:
                settled = self._progress(step, at)
            except Exception as exc:  # a progress mark never costs the attempt
                self.log.warning("could not record a start-up step", step=step, error=type(exc).__name__)
                return
            if not settled:
                return
            self.startup_noted.add(step)

    def _forward_browser_tools(self, root: Path) -> None:
        """The runner's `browser.json`, to the gate, once per change (BSTATUS-API).

        The file is in the agent's reach, so it is checked
        (`browser_tools.read_state`) and a file that does not hold together
        is not sent. A refused or lost gate is tried again on the next poll."""
        if not self.offered or self.gate is None:
            return
        state = browser_tools.read_state(root)
        if state is None or state == self._browser_tools_sent:
            return
        try:
            self.gate.call(browser_tools.CTL_OP, state)
        except (GateRefused, GateLost):
            return
        self._browser_tools_sent = state
        self.log.info("browser tools told to the gate", tool_selected=state["tool_selected"],
                      tools=",".join(state["tools_available"]))

    def _answer_devices(self, root: Path) -> None:
        devices = root / "devices"
        if not devices.is_dir():
            return
        for path in sorted(devices.glob("*.json")):
            request_id = path.stem
            data = _read_request(path)
            path.unlink(missing_ok=True)
            if not REQUEST_ID.fullmatch(request_id):
                continue
            preset = data.get("preset") if isinstance(data, dict) else None
            if not self._gate_open():
                self._not_enabled(root, request_id)
                continue
            if not isinstance(preset, str) or not _PRESET.fullmatch(preset):
                self._refusal(root, request_id, "preset_invalid", "a preset is named, never sized")
                continue
            try:
                waited = self._gate_op("device", {"preset": preset})
            except GateRefused:
                self._refusal(root, request_id, "preset_refused", f"the gate refused {preset!r}")
                continue
            except _GateDown as down:
                self._gate_down(root, request_id, "device", down)
                continue
            self._respond(root, request_id, {"outcome": "applied", "exit": 0, "preset": preset,
                                             **self._waited(waited)})

    def _answer_themes(self, root: Path) -> None:
        """`swarm-handoff theme <name>` (QA-KIT): the gate's `theme` operation, as
        a device switch is answered -- by name, on a live attempt only, and
        logged by name, never more."""
        themes = root / "themes"
        if not themes.is_dir():
            return
        for path in sorted(themes.glob("*.json")):
            request_id = path.stem
            data = _read_request(path)
            path.unlink(missing_ok=True)
            if not REQUEST_ID.fullmatch(request_id):
                continue
            if not self._gate_open():
                self._not_enabled(root, request_id)
                continue
            theme = data.get("theme") if isinstance(data, dict) else None
            if not isinstance(data, dict) or set(data) != {"theme"} or theme not in THEMES:
                self._refusal(root, request_id, "theme_invalid", "a theme is dark, light or system")
                continue
            try:
                waited = self._gate_op("theme", {"theme": theme})
            except GateRefused:
                self._refusal(root, request_id, "theme_refused", f"the gate refused {theme!r}")
                continue
            except _GateDown as down:
                self._gate_down(root, request_id, "theme", down)
                continue
            self.log.info("browser theme set", theme=theme)
            self._respond(root, request_id, {"outcome": "applied", "exit": 0, "theme": theme,
                                             **self._waited(waited)})

    # -- the gate's browser operations: refused, down, or waited for (GATE-STAYS-UP) --

    def _gate_open(self) -> bool:
        """Whether a device or theme can be asked of the gate: this worker holds
        its registration. NOT `offered`, which is the HAND-OFF's: an attempt
        whose human view or hand-off is missing (an unprotected heap, no live
        session, one `pause` the gate did not confirm) still has the gate and
        the agent's browser (NOLIVE-GUARD), and a device switch needs no more."""
        return self.gate is not None and self.gate.registered and not self._wiped

    def _not_enabled(self, root: Path, request_id: str) -> None:
        reason = GATE_WIPED if self._wiped else GATE_NOT_ENABLED
        self._respond(root, request_id, {
            "outcome": "refused", "exit": EXIT_REFUSED,
            "code": GATE_UNAVAILABLE if self._wiped else (self.unavailable or NO_LIVE_SESSION),
            "reason": reason, "message": GATE_REASON_TEXT[reason]})
        self.log.info("hand-off request refused", code=GATE_UNAVAILABLE, reason=reason,
                      task_id=self.cfg.task_id)

    def _gate_op(self, op: str, args: Mapping[str, Any]) -> float:
        """`op` through the gate, waiting out a gate that is starting or
        restarting until this poll's GATE_WAIT_SECONDS are spent. Returns the
        seconds waited. `GateRefused` for a refusal of the request itself;
        `_GateDown` (reason, seconds waited) when the browser is not there."""
        assert self.gate is not None
        started = self._monotonic()
        waited = False
        while True:
            try:
                self.gate.call(op, args)
                # Only a gate that was waited for is said to have been: a
                # switch that is merely slow is not a restart.
                return self._monotonic() - started if waited else 0.0
            except GateRefused as exc:
                if exc.code not in GATE_DOWN_REASONS:
                    raise
                reason = exc.code
            except GateLost as exc:
                reason = exc.reason
            now = self._monotonic()
            if self._gate_wait_until is None:
                self._gate_wait_until = now + GATE_WAIT_SECONDS
            if reason not in GATE_WAIT_REASONS or now + GATE_WAIT_STEP_SECONDS > self._gate_wait_until:
                raise _GateDown(reason, now - started if waited else 0.0)
            self._sleep(GATE_WAIT_STEP_SECONDS)
            waited = True

    def _gate_down(self, root: Path, request_id: str, op: str, down: "_GateDown") -> None:
        waited = int(round(down.waited))
        message = GATE_REASON_TEXT.get(down.reason, GATE_REASON_TEXT[GATE_CRASHED])
        if waited:
            message += f" (waited {waited} s for it)"
        self._respond(root, request_id, {
            "outcome": "refused", "exit": EXIT_REFUSED, "code": GATE_UNAVAILABLE,
            "reason": down.reason, "message": message, "waited_seconds": waited})
        self.log.warning("the live browser is unavailable", op=op, code=GATE_UNAVAILABLE,
                         reason=down.reason, waited_seconds=waited, task_id=self.cfg.task_id)

    def _waited(self, seconds: float) -> dict[str, Any]:
        """What an applied answer adds when the gate was waited for: the seconds, said."""
        waited = int(round(seconds))
        if waited <= 0:
            return {}
        self.log.info("the live browser came back", waited_seconds=waited, task_id=self.cfg.task_id)
        return {"waited_seconds": waited,
                "message": f"the live browser was starting or restarting; waited {waited} s for it"}

    # -- the ends ----------------------------------------------------------------

    def confirm_expired(self) -> bool:
        """LB-RETURN: tell swarm-api the open hold expired, BEFORE anything is
        told to the agent or parked. Never raises.

        False when swarm-api answered it `REVIVED`: a
        viewer or a controller moved the deadline after `_watch` read it, so
        the person came first and the hold goes on -- nothing is answered,
        wiped or parked, and the next poll reads the new deadline. True
        otherwise, and `end` does not send the resolve again. Any other failure
        is True too, as it always was: swarm-api clears a stale wait when the
        task moves on, and a hold that cannot be confirmed must still end
        (invariant 4).

        The expiry names the deadline it judged (`seen_deadline`), so the
        refusal is exact: a deadline that merely lies in the future because
        the worker's clock is behind is not "revived".
        """
        active = self.active
        if active is None:
            return True
        deadline = active.get("deadline")
        fields: dict[str, Any] = {"request_id": active["request_id"], "outcome": "expired"}
        if isinstance(deadline, datetime):
            fields["seen_deadline"] = deadline.isoformat()
        try:
            _, payload = self._route(
                f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/handoff/resolve", fields)
        except Exception as exc:
            self.log.warning("the hand-off's expiry was not resolved at swarm-api",
                             error=str(getattr(exc, "code", None) or type(exc).__name__))
        else:
            if isinstance(payload, dict) and payload.get("resolved") == REVIVED:
                self.log.info("a person came as the hand-off's hold ran out; holding on")
                return False
        active["resolved"] = True
        return True

    def end(self, work: Path | None, *, outcome: str, cause: str) -> bool:
        """End the open hand-off, if any: answer the agent, resolve the route,
        end control and wipe. Never raises. Returns whether one was open.

        `outcome` is swarm-api's (`expired`, `cancelled`, `unavailable`);
        the agent's exit follows from it. The route is resolved while the
        task is still RUNNING: swarm-api refuses it once the task has moved.
        """
        active = self.active
        self.active = None
        if active is not None:
            code = {"expired": EXIT_EXPIRED, "cancelled": EXIT_CANCELLED}.get(outcome, EXIT_REFUSED)
            if work is not None:
                try:
                    root = self.spool(work)
                    document = {"outcome": outcome, "exit": code, "cause": cause}
                    self._answer_all(root, active, document)
                    _write_atomic(root / "status.json", document)
                    self._record(root, active["request_id"], {**document, "ended_at": self._now()})
                except OSError as exc:
                    self.log.warning("could not answer the agent's wait", error=type(exc).__name__)
            try:
                if not active.get("resolved"):  # `confirm_expired` resolved it already
                    self._route(
                        f"/v1/tasks/{quote(self.cfg.task_id, safe='')}/handoff/resolve",
                        {"request_id": active["request_id"], "outcome": outcome},
                    )
            except Exception as exc:
                # swarm-api clears a stale wait itself when the task moves on;
                # the park or the cancel below still happens.
                self.log.warning("the hand-off was not resolved at swarm-api",
                                 error=str(getattr(exc, "code", None) or type(exc).__name__))
            self.log.warning("hand-off ended", outcome=outcome, cause=cause)
        if outcome == "expired" and cause != GATE_LOST and work is not None:
            # The park (§4.3): what the resumed attempt gets back, read
            # before the wipe below closes the browser (§5.5.2).
            self._keep_browser(work)
        if cause != GATE_LOST:
            self.wipe(cause)
        return active is not None

    def wipe(self, cause: str) -> None:
        """§5.5: end control, close Chromium and remove the profile. Never raises.

        Sent once per attempt. Before the hand-off's own checkpoint on the
        paths that park or cancel from here; from `_cleanup` on every other
        exit. The profile is on a sidecar-only volume either way, never in
        `work/`, so no checkpoint has ever held it.
        """
        if self._wiped or self.gate is None or not self.gate.registered:
            return
        self._wiped = True
        try:
            self.gate.call("end_control", {"cause": cause, "wipe": True})
        except GateLost as exc:
            self.log.warning("the gate did not confirm the wipe", error=type(exc).__name__)

    def agent_exited(self, work: Path | None) -> None:
        """The agent ended on its own with a hand-off open: nobody is waited
        for any more. Resolved as `unavailable`, while the task is RUNNING."""
        if self.active is not None:
            self.end(work, outcome="unavailable", cause="agent_exited")

    def close(self) -> None:
        """The `_cleanup` backstop: wipe on every exit, then drop the channel."""
        if self.gate is None:
            return
        self.wipe("attempt_end")
        self.gate.close()

    def _resume_quietly(self) -> None:
        if self.gate is None:
            return
        try:
            self.gate.call("resume", {})
        except GateLost as exc:
            self.log.warning("the gate did not resume the agent", error=type(exc).__name__)


class HumanPark(NamedTuple):
    """What `park_human_required` wrote. `returned_by` is the person whose
    return re-queued the task READY instead of parking it (LB-RETURN), or None
    when it parked."""

    refunded: bool
    returned_by: str | None


def park_human_required(
    control: Any,
    *,
    max_resumes: int,
    detail: dict[str, Any] | None = None,
    uploads: list[Any] | None = None,
) -> HumanPark:
    """§4.3: PARKED on HUMAN_REQUIRED, slot given back, the attempt refunded
    while fewer than `max_resumes` have been.

    The await park's shape exactly (`ControlPlane.park_awaiting_children`).
    ONE FENCED TRANSACTION: a superseded attempt gets `FencedWriteRefused`
    with nothing written, so it can neither park the task nor refund an
    attempt (invariant 5). `next_eligible_at` is the park instant, which the
    scheduler's dead-letter sweep measures `human_park_max_seconds` from; NO
    sweep promotes it, only swarm-api's `human-ready`. `human_wait` is NOT
    written here: it is swarm-api's field alone (`Task.human_wait`). The
    worker's resolve route cleared it before this park, and `human-ready`
    clears one a failed resolve left behind.

    THE ATTEMPT'S END, THE EVENT AND THE LEASE RELEASE ARE IN THE SAME
    TRANSACTION (`ControlPlane._end_in_transaction`, #2064), as in every
    park. They used to follow it: the attempt's end best effort, the event,
    and the release as a second transaction. A contention abort on that
    release (2026-10-23 09:05:41Z, on a quota park) crashed the worker, its
    crash path met its own park as a fence, and the PARKED task held its
    lease (invariant 1). Now PARKED or READY and the pools coming back down
    commit together or not at all, an abort is run again by the runner, and
    `control._ended` records the committed end so the crash path knows it
    as this attempt's own (`Worker._own_end_exit`).

    Built on the control plane's own fenced-transaction helpers rather than as
    a `ControlPlane` method because `control.py` is outside lane L4's files;
    the transaction is the same one the other parks run.

    A PERSON'S RETURN RE-QUEUES INSTEAD (LB-RETURN, part of #1030). A person
    who took control and returned it while this attempt was parking left
    `HUMAN_RETURNED_METADATA_KEY` naming this attempt and generation
    (swarm-api's `LiveService.return_control`). Read in the same fenced
    transaction, it turns the park into their "I'm ready": READY rather than
    PARKED, with everything else the park does -- checkpoint first, attempt
    refunded within the bound, lease released, exit. The next attempt is
    admitted all-or-nothing under a new generation like any READY task
    (invariants 2, 4, 5). A mark naming another attempt or generation answers
    nothing here. A task with no attempt left parks as before: READY would
    lease attempt N+1 of N, and the scheduler dead-letters it instead.

    Returns whether the attempt was refunded and who re-queued it, if anyone.

    A TENANT SWITCHED TO THE STATE ROUTES (`control.state_via_api`,
    docs/design/tenant-settings-integrity.md §4.2) has no transaction to run
    this in: `RemoteControlPlane.park_human_required` sends ONE `state/park`,
    and swarm-api writes everything above -- the fence, the bounded refund,
    the person's re-queue, the carry, the attempt's end, the event and the
    lease release -- in one transaction there. No Firestore call is made
    here. The answer says whether a person's return re-queued the task, not
    who: their name is in the READY event and `human_ready` swarm-api wrote,
    so `returned_by` is "" then, as it is on this path for a mark that names
    nobody.
    """
    if getattr(control, "state_via_api", False):
        # swarm-api writes the park at its own instant, read after this one;
        # `expires_at` is earlier than its park instant plus the bound by the
        # request's latency, never later. It is merged into the event swarm-api
        # writes, the READY one of a person's return included.
        expires = utcnow() + timedelta(seconds=HUMAN_PARK_MAX_SECONDS)
        refunded, requeued = control.park_human_required(
            detail={**(detail or {}), "expires_at": expires.isoformat()}, uploads=uploads)
        return HumanPark(bool(refunded), "" if requeued else None)
    reason = ParkReason.HUMAN_REQUIRED
    now = utcnow()
    returned: dict[str, Any] = {}
    # Minted outside the body, which a contended transaction runs again, so
    # every run writes the same event ids; its type, PARKED or READY, is
    # decided inside it (`ControlPlane._filled`).
    ended = control._event_write(EventType.PARKED)
    released_slot = control._event_write(EventType.LEASE_RELEASED)

    def _apply(txn: Any) -> tuple[bool, TaskState, list[dict[str, Any]]]:
        returned.clear()  # a retried transaction starts again
        task = control._fenced_task(txn, write="human park")
        current = TaskState(task.get("state"))
        assert_transition(current, TaskState.PARKED)
        index = control._read_carry_index(txn) if uploads else None
        metadata = dict(task.get("metadata") or {})
        used = metadata.get(HUMAN_WAIT_RESUMES_METADATA_KEY)
        used = used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else 0
        attempt_count = int(task.get("attempt_count", 0))
        refund = used < max_resumes and attempt_count > 0
        resumes = used + (1 if refund else 0)
        payload: dict[str, Any] = {
            "state": TaskState.PARKED.value,
            "park_reason": reason.value,
            "next_eligible_at": now,
            "current_lease_id": None,
            "blocked_by": [{"reason": reason.value, **(detail or {})}],
            "updated_at": now,
        }
        if refund:
            metadata[HUMAN_WAIT_RESUMES_METADATA_KEY] = used + 1
            payload["metadata"] = metadata
            payload["attempt_count"] = attempt_count - 1
        mark = metadata.get(HUMAN_RETURNED_METADATA_KEY)
        left = int(task.get("max_attempts") or 1) - int(payload.get("attempt_count", attempt_count))
        if (
            isinstance(mark, dict)
            and mark.get("attempt_id") == control.attempt_id
            and mark.get("generation") == control.generation
            and left > 0
        ):
            assert_transition(current, TaskState.READY)
            by = str(mark.get("by") or "")
            metadata.pop(HUMAN_RETURNED_METADATA_KEY, None)
            metadata[HUMAN_READY_METADATA_KEY] = {"by": by, "at": now.isoformat()}
            payload.update({"state": TaskState.READY.value, "park_reason": None,
                            "next_eligible_at": None, "blocked_by": [], "metadata": metadata})
            returned["by"] = by
            state = TaskState.READY
            release = "requeued:human_returned"
            # swarm-api's "I'm ready" event's shape (`handoff.ready_writes`).
            event = control._filled(ended, EventType.READY, {
                "reason": "human_returned",
                "park_reason": reason.value,
                "by": by,
                "attempt_refunded": refund,
                HUMAN_WAIT_RESUMES_METADATA_KEY: resumes,
                **(detail or {}),
            })
        else:
            state = TaskState.PARKED
            release = f"parked:{reason.value}"
            event = control._filled(ended, EventType.PARKED, {
                "reason": reason.value,
                "next_eligible_at": now,
                "attempt_refunded": refund,
                HUMAN_WAIT_RESUMES_METADATA_KEY: resumes,
                **(detail or {}),
                # When the scheduler dead-letters it, from the park's own
                # instant (WAIT-RECORD, part of #1782). An event detail only:
                # the task document is not given a field for it.
                "expires_at": now + timedelta(seconds=HUMAN_PARK_MAX_SECONDS),
            })
        # After every read above (the fence, the task, the carry index) and
        # before any write: the release reads the lease and its pools, and
        # Firestore refuses a read after a write.
        written = control._end_in_transaction(
            txn,
            payload=payload,
            release=release,
            exit_code=ExitCode.PARKED,
            error=reason.value,
            events=[event],
            released_slot=released_slot,
            now=now,
        )
        if uploads:
            control._record_parked_uploads(txn, index, uploads, at=now)
        return refund, state, written

    refunded, state, written = control._run_transaction(_apply)
    control._ended(state, written)
    return HumanPark(refunded, returned.get("by"))
