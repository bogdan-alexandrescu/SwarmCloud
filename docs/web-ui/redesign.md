# The web UI, rearranged — information architecture and design system

**Status.** The navigation, the routing and the shared primitives are in.
The eleven screens themselves are untouched: they belong to other tracks, and
every one of them still works and is still reachable. This file is the
argument for the shape, so that the shape can be disagreed with in one place
rather than re-litigated per screen.

Files changed: `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/styles.css`,
this document.

---

## 1. What the eleven screens actually were

Read, not guessed from the names. Line counts are the screen's own file.

| Nav item | File | The question it answers | Who asks it |
|---|---|---|---|
| Home | `ControlRoom.tsx` | Is the platform healthy right now, and what is the first thing to look at | Anyone, on arrival |
| Trouble | `Trouble.tsx` (+`Leases.tsx`) | What is wrong, derived from seven independent reads: dispatch paused, failures needing a human, parked work by reason, why the queue is not moving, provider health, silent workers, admitted-but-never-dispatched | An admin at 3am; a user whose task "isn't doing anything" |
| Capacity | `Capacity.tsx` | Which ceiling is the binding one, pool by pool, plus per-profile headroom | An operator whose work is not being admitted |
| Holders | `Holders.tsx` | What is holding capacity right now, and do the two records of that agree (lease sum vs pool counter) | An operator chasing a suspected leak |
| Agents | `Agents.tsx` → `AgentDetail.tsx` | What is running / waiting / finished, and for one agent: why, timeline, output, placement, input | Everyone. The main screen |
| Workflows | `Workflows.tsx` | The DAG, with step state joined from the tasks each step created | Someone running a multi-step job |
| Activity | `Activity.tsx` | What happened over a window of N rows: outcome chart, four tiles, runner split, per-engineer table | A lead, weekly |
| Quota | `QuotaDetail.tsx` | Whose provider quota is in trouble, per provider per tenant. Admin | An admin during a squeeze |
| Counts | `PlatformCounts.tsx` | Task counts by state, tenant and platform. Admin, behind a button because it is one Firestore `count()` per state | An admin, rarely |
| Tenants | `Activity.tsx` | The tenant roster: kind, principal, credentials, identity, enabled. Admin | An admin, rarely |
| Settings | `AdminSettings.tsx` + `Accounts.tsx` | Two unrelated things: the editable pool ceilings, and the Claude subscription account pool | An admin; and anyone whose account died |

**What overlapped.**

- **Capacity, Holders and Quota are three views of one question.** "Is there
  room" is answered by the pool ceilings (Capacity), by what currently holds
  them (Holders) and by the provider ceiling that binds before either
  (Quota) — and the account windows in Settings bind before all three. Four
  top-level items, one question, and the one that binds first was the one
  buried deepest.
- **Home and Trouble are the same screen at two magnifications.** Home shows
  pools needing attention and a dispatch pill; Trouble shows the same
  dispatch state plus six more checks. Home's value is that it is short.
- **Counts and Activity both count tasks.** Activity counts the rows it
  loaded and says so; Counts runs server-side aggregations and is expensive.
  They are not redundant, but they are the same question at two costs.
- **Tenants is a roster, not an activity view**, and sat under Activity's
  file while appearing in the nav between Counts and Settings.
- **Settings held two things with nothing in common.** Pool ceilings are an
  admin control. The account pool is capacity supply.

**Four finished screens that no route pointed at.** `Profiles.tsx` (the
runner-profile catalogue — what you may ask for, what it weighs, which pools
it touches), `Submit.tsx` (submit one task), `SubmitWorkflow.tsx` (submit a
DAG) and `AttemptTimeline.tsx` (every attempt with its generation, backend,
exit code, peak RSS, checkpoint count, and the events grouped under the
attempt that wrote them). `git log -S` confirms `App.tsx` has never imported
any of them. The product had no way to start work in it, and no way to see an
agent's attempt history, because of four missing lines of routing.

---

## 2. The new architecture

Five sections. Each is a question someone arrives with, not a route.

| Section | The question | Panes |
|---|---|---|
| **Overview** | Is the platform healthy right now, and if not, what is the first thing to look at? | Now (Control room) · Needs attention (Trouble) |
| **Agents** | What is running, what is waiting, what did it produce — and why has mine not moved? | Agents · Workflows · New agent · New workflow |
| **Pools** | Is there room to run more, which ceiling is the binding one, and what is holding what there is? | Pools · Runner profiles · Holders · Accounts · Provider quota |
| **History** | What has this platform done over time, who used it, and what did it cost? | Timeline · Platform counts |
| **Admin** | Change a ceiling, or see who is registered to use this platform. | Pool limits · Tenants |

Plus **Reference** — the API surface — in the nav's utility corner, not among
the sections.

The question is printed under each section's tabs. That line is load-bearing:
it is the test for whether a new screen belongs in a section. A screen that
does not help answer the printed question does not go there, and if it
answers a question none of the five ask, that is the argument for a sixth
section rather than for quietly widening one.

### Why these five

**Overview merges into one pane.** (Superseded 2026-09-21: see the note below.) The control room's
value is that it is short — a dispatch pill, four counters, and the pools that
need looking at. Trouble is seven independent reads that each have to be able
to fail alone. Merging them produces a long screen that is slow to load and
that nobody can scan in the five seconds the control room is for; keeping
them as two panes of one section preserves both and costs one click.

**Submitting moved next to the thing it creates.** The brief asked for "a one
stop shop for all that a user can see that is actionable". Submitting is the
only genuinely actionable thing most users do, and it was reachable from
nowhere. It sits beside the list of what is already running, which is also
where the answer to "will it start" lives — Agents and Capacity are one click
apart.

**Accounts moved from Settings to Capacity.** This is the change most likely
to be argued with, so the reasoning, in full: the Claude subscription pool's
five-hour and seven-day windows are the only used-against-available reading
this platform has that is not a pool counter, and a `REAUTH_REQUIRED` account
removes capacity exactly the way a lowered ceiling does. Someone asking "why
can nothing start" has to find it, and nobody looks under Settings for that.
The screen itself is unchanged, including its state changes and its danger
zone, and `#settings/accounts` still resolves — it just lands under Capacity.

**Quota and Counts stayed visible rather than being hidden from non-admins.**
Both render an "admin only" panel, which is information and not an error. A
tab hidden from a non-admin is indistinguishable from a tab that does not
exist, and the person then asks in Slack whether the feature was built. The
tabs carry a small `admin` marker instead, so the 403 is expected before the
click rather than explained after it.

**The API surface is one page, reached from the utility corner.** The brief:
"We don't need to show what apis are available, that can be one page reserved
for that, maybe not even a top lvl menu." It is not a section because it is a
thing you look up once, not a thing you work in.

### What was considered and rejected

- **Three sections (Work, Capacity, Admin).** Folds Activity into Work and
  Overview into Capacity. Rejected: "what is happening now" and "what
  happened over the last 500 tasks" are different reads at different costs
  with different failure modes, and the merged screen would have to explain
  which numbers were which in prose.
- **A single "Dashboard" with everything on it.** This is what the brief's
  "one stop shop" could be read as asking for. Rejected: every panel on it is
  an independent read that can fail alone, and a page of twelve reads takes
  the slowest one to appear. The control room is the summary; the sections
  are where you go when it points somewhere.
- **Keeping Trouble top-level as "Alerts".** Rejected because it would be a
  promise the platform cannot keep — see §5. It is derived state, and calling
  it Alerts implies delivery, history and acknowledgement, none of which
  exist.
- **A collapsible left sidebar.** Rejected for now: the app is one 1100px
  column that works at 390pt, and a sidebar costs either the width or a
  hamburger. Five items fit across the top on a phone without scrolling.

### Routing

The hash is still the router. Routes are `#<section>/<pane>`, the agent
drawer is `#agents/task/<id>` with `#agents/task/<id>/attempts` for the
second pane, and the API surface is `#reference`.

**Every old hash still resolves.** `#home`, `#trouble`, `#capacity`,
`#holders`, `#agents`, `#agents/<task-id>`, `#workflows`, `#activity`,
`#quota`, `#counts`, `#tenants`, `#settings`, `#settings/accounts` and
`#settings/limits` each land on the pane that answers what the old screen
answered. Those hashes are in runbooks and in the links people paste at 3am;
a redesign that sends them all to Home is a redesign that loses the one link
someone needed. The address bar is rewritten to the new spelling with
`history.replaceState`, so the next copy of the link is current and Back does
not walk the user through aliases they never typed.

`#agents/task/<id>` is explicit so that a task whose id happens to spell a
pane name cannot be mistaken for one; the old bare `#agents/<id>` is still
accepted, after the pane names have had their chance to match.

---

## 3. The design system

Everything new in `styles.css` is prefixed `ctl-`. That sheet grew as global
single-word class names — `.tile`, `.tag`, `.row`, `table.pools` — and a rule
added late silently restyled two screens that were already shipping. A prefix
is the cheapest guarantee that nothing new can reach a screen that did not ask
for it. Tokens are added in a second `:root` block rather than by editing the
first, so no existing value can change — only new names appear.

Six primitives, one per lane the screens keep re-inventing:

| Primitive | Class | What it is for |
|---|---|---|
| Metric tile | `.ctl-metric` in `.ctl-metrics` | One fact, its unit, and what it excludes |
| Utilisation bar | `.ctl-util` | used against ceiling, as a row that survives 390pt |
| Status chip | `.ctl-chip` | A state, as a word plus a decorative dot |
| Table | `.ctl-table` | Rows of figures that only mean something beside each other |
| Empty state | `.ctl-empty` | The four things that look like an empty screen |
| Stale reading | `.ctl-stale-note`, `.ctl-stale-body`, `.ctl-stale-mark` | Real data that is no longer current |

**The rule they encode, so that no screen has to remember it.** Each
primitive has a state for *not measured* that is visually distinct from zero,
and a state for *measured, but not recently*:

- `.ctl-metric.is-absent` — dashed, and the value is a **sentence**, not a
  figure, at a smaller size. A short phrase at 24px reads as a headline
  number, which is the confusion the state exists to prevent.
- `.ctl-metric.is-unread` — also dashed, but amber. "The platform does not
  record this" and "we failed to read it" are different claims about the
  world and must not be told apart only by reading the caption.
- `.ctl-util-track.is-unknown` — hatched, with **no fill**. An unfilled plain
  track reads as "0% used", which is a claim nobody made.
- `.ctl-util-over` — the segment above the ceiling is hatched in the failure
  colour rather than clipped. A bar pinned at 100% hides the one thing worth
  seeing.
- `.ctl-chip.is-unknown` — a hollow dot and faint text. UNKNOWN is not
  healthy and must not borrow a healthy colour.
- `.ctl-empty` has four variants: a real zero, `.is-failed`, `.is-partial`
  and `.is-admin`. The admin gate is blue with no retry button, because
  nothing failed.
- `.ctl-em` — the em dash that stands for an absent measurement, dimmed and
  non-tabular so it cannot be skimmed as a digit down a column.

Two smaller decisions worth recording. **The word on a chip is mandatory and
the dot is decoration**, because a colour-only badge fails for a colour-blind
operator and in the greyscale screenshot that gets pasted into an incident
channel — which is where these screens are actually read. And **row tones are
a background wash, never a text colour**: the cells still have to be legible
at 3am, and a red row of red text is not.

Dark and light both still work: every new token either derives from an
existing one (so it flips with the theme automatically) or is redefined in the
existing light-mode block. Only `--ctl-shadow` needed a second definition — a
30%-black shadow that reads as depth on `#0b0d10` reads as dirt on `#f6f8fa`.

**Two rules are scoped to `.ctl-drawer` and deserve their own note.** The
agent drawer gained a tab strip so the attempt timeline is reachable at all.
`AgentDetail.tsx` draws its own `.drawer` and its own close button and belongs
to another track, so rendering it inside the new drawer would nest two fixed
panels and two close buttons. The inner one is flattened in CSS, scoped, with
a comment — if `AgentDetail` ever stops drawing its own drawer those two rules
become no-ops rather than breakage. The alternative was a two-line edit to
another track's file, which the working rules forbid.

---

## 4. What the brief asked for that the platform records

Present, and now grouped where someone will look for it:

- **Available and used capacity** — pool ceilings, units in use, headroom,
  per-profile "could start", who set the ceiling, and which pool binds:
  Capacity → Pools and Runner profiles.
- **Current agents and runtimes** — state, elapsed, attempts used, resource
  class and weight, why it has not moved: Agents. Per attempt: generation,
  backend, execution name, started/completed, exit code, peak RSS, peak disk,
  OOM near miss: Agents → the drawer's Attempts pane.
- **Inputs and outputs** — the input on the agent detail's Input panel;
  artifacts (name, bytes, GCS URI), log URIs, the git outcome (commits,
  patch, pull request, publish reason) on its Output panel.
- **Metadata** — task metadata, runner profile, provider, model, priority,
  blockers, park reason, fencing generation per event.
- **Errors** — `last_error`, per-attempt `error`, failures needing a human,
  parked work by reason: Overview → Needs attention.
- **Token cost** — `cost_usd`, `input_tokens`, `output_tokens` and both cache
  counters exist per attempt. See §5 for why they are not yet on a screen.

---

## 5. What the brief asked for that this platform cannot show

Recorded here rather than faked. Nothing below is rendered as a placeholder,
a zero, or a "coming soon" panel.

**GCP infrastructure cost.** There is no billing integration of any kind. Per
attempt token cost is recorded; dollars of Cloud Run, Firestore, GCS or
network are not, and there is no source to derive them from. A "costs"
screen that showed token dollars under a heading implying total spend would
be worse than no screen. `monthly_budget_usd` is additionally a permanent
`null` for every tenant — the only write path 422s — which is why the Tenants
table omits the column rather than leaving it blank.

**Notifications and alerts.** No alert model, no delivery, no storage, no
acknowledgement, no history. What *is* legitimate, and is what Overview →
Needs attention already does, is **derived** trouble: overdue leases, silent
workers, quota `EXHAUSTED`, accounts needing re-auth, failures, parked work,
and stale readings. That is computed from current state on every load. It is
not an inbox, it cannot tell you what fired while you were asleep, and it
must not be labelled "Alerts" — which is why the pane is called "Needs
attention".

**Checkpoint contents.** Only the id, the GCS URI and the byte size are
recorded. An attempt's `checkpoints` field is a list of strings. Listing what
is *inside* a checkpoint needs a new route that reads the manifest, and
showing a file's bytes needs an artifact byte-proxy that does not exist
either. Until both land, the honest rendering is what ships today: the
checkpoint's identity and size, and the URI to fetch it with. The brief asked
for "a way to see checkpoints and content of those" — half of that is
available and half of it is a route nobody has written.

**Resources requested against resources used — half available.** The *used*
side exists: `peak_rss_bytes` and `peak_disk_bytes` per attempt, plus
`oom_near_miss`. The *requested* side lives in `RESOURCE_CLASSES` in the
frozen contract, is read by swarm-api, and **is not returned by any route**.
So the comparison the brief asked for cannot be drawn today, and hand-copying
the vCPU and memory figures into TypeScript is exactly the restatement
`check-contract-parity.sh` exists to catch — and it does not cover
TypeScript, so the copy would drift silently. **This is the cheapest of the
gaps to close**: one route returning the frozen catalogue, and the comparison
becomes real. See §6.

**Live logs while an agent runs.** A live tail is published every few
seconds, but the runners shipped today write nothing to stdout until they
finish. A "live output" pane would therefore be empty for the entire time
anyone would want to look at it, and an empty pane is read as "the agent is
stuck". Terminal logs are reachable as GCS URIs on the Output panel. This is
a runner-image change, not a UI one.

**Utilisation over time.** There is no utilisation history anywhere — not in
Firestore, not in the API. Throughput would need Cloud Monitoring, and
`roles/monitoring.viewer` is not granted to swarm-api. Everything on the
capacity screens is an instantaneous reading, which is why none of them draws
a line chart.

**Which agent is using which account.** Nothing assigns an account to an
agent and `Lease` carries no account field, so "this account's quota is being
burned by that agent" is unanswerable. The account pool and the agent list
cannot be joined.

**Cross-tenant agent list.** Every list method starts from an equality filter
on `tenant_id`; there is no admin branch. An admin sees their own tenant's
agents, and the Capacity screen's headroom figures are likewise the calling
tenant's — including for an admin. The screens say so in words, repeatedly,
because an admin reading their own four running agents as the platform total
is a truth bug rather than a layout preference.

---

## 6. Requests, not changes

These belong to other tracks or to the frozen contract. They are written down
here rather than acted on.

1. **A route that returns `RESOURCE_CLASSES`.** The requested cpu/memory/disk
   per class. The single highest-value item in this list: it turns
   "resources requested vs utilised" from impossible into a two-column table,
   and it removes the standing temptation to hand-copy the frozen contract
   into the UI. The brief already judges adding one legitimate.
2. **Render token and cost columns on the attempts table.**
   `AgentDetail.tsx` omits them deliberately, with a comment saying they are
   null on every attempt that ran before the worker's usage capture was
   fixed. Everywhere else in this UI an absent measurement is an em dash — the
   omission is the one place the codebase's own rule is not followed, and it
   hides the numbers on new attempts that *do* carry them. `AttemptRow`
   already types all five fields.
3. **Put `current_generation` and `current_lease_id` on the task API.**
   `task_to_api` drops both although `Task` carries them, so the fencing
   generation cannot appear on the agents table — only inside the event
   stream.
4. **`Nav` in `Shell.tsx` is now unused.** `App.tsx` no longer imports it.
   It is dead code in Track A's file; removing it is theirs to do.
5. **A `logs` or `artifact` byte proxy.** Without it, artifacts and
   checkpoints are a list of filenames and sizes. With it, the Output panel
   becomes the thing the brief asked for.

---

## 7. Verified, and not

Verified:

- `cd apps/swarm-ui && npx tsc --noEmit` — clean.
- `npm run build` — `tsc -b && vite build`, 55 modules, succeeded.
- Every one of the eleven old nav items resolves to a pane, by reading
  `LEGACY`, `LEGACY_SETTINGS` and `SectionBody` against the old `SCREENS`
  list and the old `#settings/<tab>` handling.

Not verified, and worth someone's eye before this is called done:

- Nothing was opened in a browser. The nav, the drawer tab strip and the
  `.ctl-drawer` flattening rules are unexercised visually, in either theme,
  at any width.
- The primitives in §3 are not yet used by any screen. They typecheck as CSS
  and are unreferenced, so nothing can have regressed — but nothing proves
  they render as described either.
- Two write screens, `Submit` and `SubmitWorkflow`, are reachable from the
  product for the first time. They were written to ship and have never been
  clicked through against a live API.


---

## Superseded, 2026-09-21

Three decisions in this document were changed by the owner after it was
written, and the code is the authority for all three. Recorded here rather
than silently edited, because a reader who followed a citation to this file
deserves to see what moved and why.

**Capacity is called Pools; Activity is called History.** Researched against
Temporal (Namespaces, Workflows, Schedules, Settings, Archive) and Nomad
(Jobs, Clients, Servers, Topology, Storage), both of which name sections after
the OBJECT they contain rather than after the question they answer. "Capacity"
and "Activity" are questions; "Pools" and "History" are things you can click
expecting to find a list of exactly that.

**There is no problem section, at any level, and nothing is called Trouble.**
Neither reference console has one: failures surface as FILTERS inside the
object list. The derived checks are a pane at the top of Overview. The owner's
objection to the name was that it "sounds more confusing than it should be",
and the research agreed with the instinct -- the conventional words are earned
ones. "Alerts" means an alerting engine, "Incidents" an incident model,
"Issues" a tracker, "Events" an event stream. This platform has none of those,
so using any of them would claim machinery that does not exist.

**Overview is one pane, not two.** The argument above for keeping "Now" and
"Needs attention" separate was that Home's value is being short. That held
while both were top-level; once the problem surface stopped being a
destination, two panes on one section was a split with nothing on either side
of it.
