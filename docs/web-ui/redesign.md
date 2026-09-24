# The web UI, rearranged — information architecture and design system

**Status.** The navigation, the routing and the shared primitives are in.
The eleven screens themselves are untouched: they belong to other tracks, and
every one of them still works and is still reachable. This file is the
argument for the shape, so that the shape can be disagreed with in one place
rather than re-litigated per screen.

Files changed: `apps/swarm-ui/src/App.tsx`, `apps/swarm-ui/src/styles.css`,
this document.

**Amended three times since.** The body below describes the nav as it is
*today*, not as it was when this was first written; the three notes at the end
of the file — [Superseded, 2026-09-21](#superseded-2026-09-21),
[Changed, 2026-09-24](#changed-2026-09-24--the-two-sections-named-after-their-own-first-tab)
and [Six sections to three](#changed-2026-09-24--six-sections-to-three) — record
what moved and why. Read them if you followed a citation here and the text does
not match what you remember.

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

Three sections and a landing screen. Each section is a question someone arrives
with, not a route. The id in the first column is the first hash segment, so it
is also the address people paste — the two are given together because they have
drifted apart twice, and both times the drift routed a saved link somewhere the
label did not name.

| Section | id | The question | Panes |
|---|---|---|---|
| **Overview** | `overview` | Is the platform healthy right now, and if not, what is the first thing to look at? | Overview |
| **Work** | `work` | What is running, what has already run, what did it produce — and why has mine not moved? | Agents · Workflows · Timeline · Submit a task · Submit a workflow |
| **Capacity** | `capacity` | What kinds of agent can run here, is there room for another, which ceiling is the binding one, and what is holding what there is? | Pools · Runtimes · Profile headroom · Holders · Accounts · Provider quota |
| **Admin** | `admin` | Change a ceiling, see who is registered to use this platform, and count what it has done. | Pool limits · Tenants · Platform counts |

It was six on 2026-09-24 — the same fifteen panes under Overview, Work,
**Runtimes**, Capacity, **History** and Admin.
[Six sections to three](#changed-2026-09-24--six-sections-to-three) at the end
of this file is the argument for the collapse, what it deliberately did *not*
do, and the two tab renames that paid for it.

Plus two things in the nav's utility corner rather than among the sections:
**API reads** (`#reference`) and **Help** (`#help/<topic>`). Both are things
you look up once, not things you work in. *Why these sections* below gives the
argument for keeping them out of the sections, and why "API reads" is neither
of the two names that screen shipped with.

The question is **not** printed on the pane. It was, on all fifteen routes,
which is six copies of one sentence for Capacity alone and a line of vertical
on every screen to say something nobody re-reads after the first visit. It now
lives behind the head's `?`, written once. What did not change is what the
question is *for*: it is the membership test for whether a screen belongs in a
section.

**With three sections that test has to carry more, and that is the price.** A
question six panes pass is a weaker instrument than one two panes pass, so the
failure to watch for is a question quietly *widened* to admit a screen someone
wanted to place. Two were widened deliberately in the collapse and both are
argued at the value in `App.tsx`: Capacity now asks what *can* run before it
asks whether there is room, which is the order those two facts are read in;
Admin now names the count, because one of its three panes changes nothing.
A third widening needs an argument of that kind, in this file, before it
happens — and a screen that answers a question none of the three ask is still
the argument for another section rather than for stretching one.

### NO SECTION MAY BE NAMED AFTER ONE OF ITS OWN TABS

This is the rule the 2026-09-24 rename established, and it is the one thing in
this section worth reading twice.

Two sections were named after their own first tab. The rail draws the tab strip
whenever a section has more than one tab (`App.tsx:694`) and the breadcrumb
draws the tab under the same condition (`App.tsx:793`), so both levels rendered
and both drew the same word: **`Agents > Agents`** and **`Pools > Pools`**.

Renaming the *tab* would have been the wrong half. `Work > Agents`,
`Work > Workflows`, `Work > Submit a task` and `Work > Submit a workflow` each
say something the section name does not — which is the test a tab has to pass.
The parent was what was wrong: the section is not the agent list, it holds the
agent list, the workflow list, and the two screens that create one of each. A
name that covers one of four children is a name that has quietly promoted a
child. The same argument names Capacity: pools, runner profiles, holders,
accounts and provider quota are five different ceilings on one thing, and
`Capacity` is that section's own question in one word.

**The holders tab went back to "Holders" in the same change**, and the earlier
argument for "Capacity holders" is what makes that right rather than what it
overrules. That argument was: "Holders" alone does not say holders of *what*,
and one tab away from "Accounts" it reads as people. True — while the section
was called Pools. The section is called Capacity now, so the parent supplies
the noun the tab was carrying for it, and `Capacity > Capacity holders` says it
twice. The requirement never changed; what changed is which level meets it.

The rule bites only where both levels render, and that is stated deliberately
rather than generalised. Overview is a single-pane section whose one tab
carries the section's own label; no tab strip and no crumb tab is drawn for it
at all, so nothing is said twice. A second pane added to Overview is the moment
the rule applies, and the reviewer who adds it should expect to rename the
section rather than the pane. (Runtimes was the other single-pane section and
is now a *tab* of Capacity, which is the same rule seen from the other side:
a section with one pane is a rail entry spent on a name said twice.)

**The ids moved with the labels, and that was the argued half.** `SECTION_ALIASES`
would have kept every `#agents/…` and `#pools/…` href working, and *because* it
would, nothing would ever have made anyone update one — an alias that internal
links also use is a spelling nothing can retire. `nav.links.test.tsx` reads
every `#`-href literal under `apps/swarm-ui/src` and fails the build if one
starts with an alias key or names a section or tab that does not exist. Aliases
are for hashes someone else saved, never a second name this app may write.

### Why these sections

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
where the answer to "will it start" lives — Work and Capacity are one click
apart.

**The two submit tabs are called "Submit a task" and "Submit a workflow", not
"New agent" and "New workflow".** `Submit.tsx` creates a task at `READY` or
`PARKED` and then says, in the panel it renders on success, "That is not a
running agent" — because invariant 1 is that neither state holds capacity. A
tab promising an agent over a page explaining you have not got one is a
contradiction, and the tab was the side that was wrong. `task` is also the noun
the API uses (`POST /v1/tasks`, `TaskCreate`), which is the same test that
named the Runtimes section after `/v1/runtimes`.

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

It is labelled **API reads**, and neither of its two earlier names survived.
The button said "Reference" and the page it opened was headed "API surface";
that page's own first paragraph says, in bold, that it is *not* a list of the
endpoints SwarmCloud offers — it is every route this browser tab has called
since it loaded. So "Reference" promised documentation the screen does not
hold, and "API surface" promised completeness the screen disclaims in its own
first sentence. Copying either onto the other would have made the app agree
with itself about something untrue. The hash stays `#reference`, because that
is an address people have saved.

**A tab's label and its screen's heading are one name, and a test holds it
there.** Seven of the sixteen routes disagreed: "Pools" opened a page headed
"Capacity", "Timeline" opened "Activity", "Holders" opened "Capacity holders",
"Pool limits" opened "Admin settings". A reader cannot tell a rename from a
redirect, so each of those is a question about whether the click went where it
said — and it breaks every external reference, because a runbook step "go to
Pools" named nothing on the screen it landed you on. The fix went both ways on
purpose: where the heading was the better name it became the tab, where the tab
was better the heading gave way, and where neither was honest both were
replaced. `tests/unit/control_plane/test_nav_headings_agree.py` reads the
`SECTIONS` array, the `SectionBody` switch and every screen's `Screen title=` /
`<h1>`, and fails on any route where the two differ — including a route added
later, which is the case a one-off audit does not cover.

### What was considered and rejected

- **Three sections (Work, Capacity, Admin) *by merging screens*.** Folding
  Activity into Work and Overview into Capacity. Rejected, and **still
  rejected**: "what is happening now" and "what happened over the last 500
  tasks" are different reads at different costs with different failure modes,
  and the merged screen would have to explain which numbers were which in
  prose. Overview and Timeline are two screens and remain two screens.

  **The three-section NAV landed on 2026-09-24 without doing any of that**, and
  the distinction is the whole of why this paragraph did not have to be
  withdrawn: what collapsed is the sections, not the screens. Timeline is a
  pane of Work with its own route, its own read and its own failure state;
  Overview is still its own screen on its own route. See
  [Six sections to three](#changed-2026-09-24--six-sections-to-three), and
  [the UX plan](ux-plan.md) §1.4 for the measurement that prompted it.
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
drawer is `#work/task/<id>` with `#work/task/<id>/attempts` for the
second pane, the API surface is `#reference` and help is `#help/<topic>`.

**Every old hash still resolves**, through two mechanisms that are not the
same and must not be merged.

`LEGACY` maps a whole old hash to one destination, which is right for a
top-level item that became a *tab*: `#home`, `#trouble`, `#holders`, `#quota`,
`#workflows`, `#counts`, `#tenants`, `#settings`, `#settings/accounts` and
`#settings/limits` each land on the pane that answers what the old screen
answered.

`SECTION_ALIASES` rewrites a renamed section's **head** and keeps the **tail**:
`agents → work`, `pools → capacity`, `activity → history`. That distinction is
load-bearing. `#capacity/holders` has a tail, and folding it through `LEGACY`
would drop the tail and land on the section's first pane — so every
`#agents/<tab>`, `#pools/<tab>` and `#activity/<tab>` link written before a
rename still opens the pane it named. `capacity` is in that map twice over:
it was the original id, an earlier pass renamed it to `pools`, and the
2026-09-24 rename put it back, so the middle spelling had a life of its own in
saved links and resolves too.

Those hashes are in runbooks and in the links people paste at 3am; a redesign
that sends them all to Home is a redesign that loses the one link someone
needed. The address bar is rewritten to the new spelling with
`history.replaceState`, so the next copy of the link is current and Back does
not walk the user through aliases they never typed.

**The alias is resolved once, before anything else looks at the head.** The
drawer branch used to test the *unresolved* head against `'agents'`; the moment
the section was renamed, that test would have been the only place still
answering to the old name, and `#work/task/<id>` — the address in every
workflow node and every saved deep link — would have resolved to the section
and dropped the task id, opening the list instead of the agent.

`#work/task/<id>` is explicit so that a task whose id happens to spell a
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
  Capacity → Pools and Profile headroom.
- **Current agents and runtimes** — state, elapsed, attempts used, resource
  class and weight, why it has not moved: Work → Agents. Per attempt:
  generation, backend, execution name, started/completed, exit code, peak RSS,
  peak disk, OOM near miss: Work → Agents → the drawer's Attempts pane. What a
  runner profile *is*, and how big one is, is the pane next to it:
  Capacity → Runtimes.
- **Inputs and outputs** — the input on the agent detail's Input panel;
  artifacts (name, bytes, GCS URI), log URIs, the git outcome (commits,
  patch, pull request, publish reason) on its Output panel.
- **Metadata** — task metadata, runner profile, provider, model, priority,
  blockers, park reason, fencing generation per event.
- **Errors** — `last_error`, per-attempt `error`, failures needing a human,
  parked work by reason: Overview. (That was a second pane called "Needs
  attention" when this was written. It is now a pane at the *top* of the one
  Overview screen — see the 2026-09-21 note at the end of this file.)
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
acknowledgement, no history. What *is* legitimate, and is what the checks pane
at the top of Overview already does, is **derived** trouble: overdue leases,
silent workers, quota `EXHAUSTED`, accounts needing re-auth, failures, parked
work, and stale readings. That is computed from current state on every load. It
is not an inbox, it cannot tell you what fired while you were asleep, and it
must not be labelled "Alerts". There is consequently **no problem section at
any level** — not "Trouble", not "Alerts", not "Incidents", not "Issues" — and
a failed agent is a row in the agents list like any other row.

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

Verified, as this pass verified it in 2026-09:

- `cd apps/swarm-ui && npx tsc --noEmit` — clean.
- `npm run build` — `tsc -b && vite build`, 55 modules, succeeded.
- Every one of the eleven old nav items resolves to a pane, by reading
  `LEGACY`, `LEGACY_SETTINGS` and `SectionBody` against the old `SCREENS`
  list and the old `#settings/<tab>` handling.

Both of those first two commands are now CI jobs and are **not** run on a
workstation: `application.yml`'s `ui` job is the typecheck and the component
suite, and nothing in this repository is called finished on a local exit code.
See [where the gates run](../ci.md). The line above is left as the dated record
of what that pass did, not as an instruction.

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

> **Half of this was undone on 2026-09-24 and half of it stands.** History is
> still History. Pools went back to Capacity, because naming that section after
> the object it contains named it after its own first tab and the rail drew
> `Pools > Pools`. See the 2026-09-24 note below: the object rule is not
> wrong, it is just outranked when the object it names is already a child.

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

---

## Changed, 2026-09-24 — the two sections named after their own first tab

The body of this document was brought up to date in place rather than left to
rot behind a note, because `App.tsx` points every reader here ("that file is
the place to argue with this, not this array") and a pointer into a stale
description is worse than no pointer. What moved is recorded here so a reader
who followed a citation can see it.

| | was | is |
|---|---|---|
| section id | `agents` | `work` |
| section label | Agents | **Work** |
| section id | `pools` | `capacity` |
| section label | Pools | **Capacity** |
| tab label | Capacity holders | **Holders** |

Both sections were named after their own first tab, so the rail and the
breadcrumb each drew the name twice: `Agents > Agents` and `Pools > Pools`. The
rule that came out of it is in §2 above — **no section may be named after one
of its own tabs** — along with why renaming the tab would have been the wrong
half, and why the ids moved with the labels instead of hiding behind an alias.

Three consequences a reader should expect to meet elsewhere:

- **`#agents/…`, `#pools/…` and `#activity/…` all still resolve**, head
  rewritten and tail kept. The address bar is rewritten to the new spelling, so
  a link copied after the rename is a current link.
- **The evidence and audit files keep their old route names.** The filename
  `audit-agents-running.json`, `#agents/workflows` in the overflow inventory,
  and the pane names in every dated report are records of what was true on a
  date; a
  measurement rewritten to match today is a measurement that has been
  falsified. [`docs/web-ui/README.md`](README.md#a-note-on-old-route-names-in-the-evidence)
  carries the translation table once, which is where a reader who opens a
  filename will look.
- **`ui-audit-and-build-prompt.md` proposed the opposite trade on two pairs** —
  shortening the heading to "Holders" rather than lengthening the tab, and
  heading the Timeline pane "History", which is the *section's* name and would
  have left tab and heading still disagreeing. The first of those has since
  landed for a different reason (the parent now supplies the noun); the second
  has not and should not.

---

## Changed, 2026-09-24 — six sections to three

The second amendment of the day, and the larger one. The nav went from six
sections to **three plus a landing screen**: Overview, Work, Capacity, Admin.
`App.tsx` points every reader here, so the argument is here and not in that
array.

### The measurement, not the taste

From [the UX plan](ux-plan.md) §1.4, taken off the accessibility tree rather
than off an opinion: fifteen screens under six sections, every screen carrying
24–67 interactive elements, **and the rail alone was 21 of them on every single
screen**. The grouping was by *which subsystem owns the data* — Runtimes had a
section because `/v1/runtimes` is a route, History had one because two screens
read the past — rather than by what anyone arrives wanting to know.

A reader arrives with five questions. Overview answers the first. The other
four were spread over **ten destinations**:

| the question | where the answer was |
|---|---|
| is anything broken right now? | Overview |
| what is running? | Agents, Workflows, Timeline |
| why is this one stuck? | Agents → drawer, Holders, Pools, Provider quota |
| how much has this cost? | Timeline, Accounts, Platform counts |
| can I start something? | Submit a task, Submit a workflow, Runtimes, Profile headroom |

### What moved

| pane | was | is |
|---|---|---|
| Timeline | `#history/timeline` | `#work/timeline` |
| Platform counts | `#history/counts` | `#admin/counts` |
| Runtimes | `#runtimes/catalogue` | `#capacity/catalogue` |
| Runner profiles → **Profile headroom** | `#capacity/profiles` | `#capacity/profiles` (label and `<h1>` only) |

Every other route is byte-for-byte what it was. **Nothing was merged, nothing
was removed**: all fifteen screens keep their own route, their own read, their
own empty state and their own failure state. That is what makes this compatible
with the standing objection in *What was considered and rejected* above, which
refused a three-section nav *that merged Activity into Work and Overview into
Capacity*. It still refuses that. What it objected to was two reads becoming
one screen, and no two reads became one screen.

### The two renames that paid for it, and why neither was optional

**`Runner profiles` → `Profile headroom`.** The old `App.tsx` note on the
Runtimes *section* explained why that section existed rather than being a tab
under Pools, and the reason was not the count:

> those two would then be adjacent tabs whose labels are near-synonyms while
> answering different questions, which is how someone ends up reading
> per-tenant headroom as a platform figure.

That hazard is real and putting a tab between them does not touch it — a reader
scanning six labels does not measure distance, they read words. So the words
changed. `Runtimes.tsx` renders the runtime topology from `GET /v1/runtimes`:
what kinds of agent exist, which backend each resolves to, how big one is.
Every figure on it is platform-wide and cannot be otherwise — the catalogue
route reads no tenant document at all. `Profiles.tsx` renders
`capacity.runner_profiles`, whose pool lists are built with
`pool_names_for(tenant_id=ctx.tenant_id)` unconditionally, admin included
(`service.py:313-329`); every count on it answers *how many more could I
submit*. One is a catalogue, the other is a measurement of one tenant against
it, and **headroom** is the word this product already uses for that
measurement — `headroomFor`, `headroomFigure`, the Pools table's own column.

The **id stays `profiles`**: `runner_profile` is the contract's field name and
invariant 10 is the reason the screen exists, so the address keeps the
contract's noun while the label says which of the two questions the screen
answers. `test_nav_headings_agree.py` moved the `<h1>` with the tab, because a
tab and its heading are one name.

**Capacity's and Admin's questions were widened, once each, and said so.**
Capacity now asks what *can* run before it asks whether there is room, which is
the order a reader who does not yet know what a profile weighs has to take them
in. Admin now names the count, because Platform counts changes nothing and the
old sentence ("change a ceiling, or see who is registered") would have been
false about one of its three panes. Both widenings are written beside the value
in `App.tsx`. **A third one needs an argument in this file before it happens** —
with three sections the membership question is the only thing keeping a screen
out of a section, and a question stretched to fit is that instrument quietly
being disconnected.

### The routing case that a head alias cannot express

`SECTION_ALIASES` rewrites the *head* of a hash and keeps the tail, which is
right when every pane of a retired section landed in one new section:
`#runtimes/catalogue` → `capacity/catalogue`, `#history/timeline` →
`work/timeline`. History's two panes did **not** go to one place, and the
failure mode is worse than a dead link:

`history` aliases to `work`. `work` is the section that owns the agent drawer,
and `fromHash` reads a Work tail matching no tab as a **task id** — deliberately,
so that `#agents/<id>` from the old nav still opens the agent. Put those
together and `#history/counts` opens the agent inspector for a task called
"counts": a spinner, then a not-found panel, on a hash that used to be a
working link to the platform's own ledger.

`MOVED_PANES` is the fix and it runs **first**, on the unaliased hash, because
it is the only lookup in `fromHash` that sees both halves of the address at
once. It also absorbed the hand-rolled `#settings/<tail>` branch, which was the
same shape one rename earlier — Settings' two panes split between Admin and
Capacity — so there is now one mechanism for it rather than two.
`apps/swarm-ui/tests/route.test.ts` asserts the task-id reading specifically,
because every other test in that file stays green through it.

### What is still true after the collapse

- **No section may be named after one of its own tabs.** Work, Capacity and
  Admin each cover all of their panes and none of their panes' names.
  `shell.test.tsx` asserts it for every section in the rail.
- **Every old hash resolves, with its tail, and the address bar is rewritten**
  to the current spelling. `#agents/…`, `#pools/…`, `#activity/…`,
  `#history/…`, `#runtimes/…` and `#settings/…` all land. A *bare* retired head
  (`#runtimes`, `#history`) lands on the new section's first pane, as an
  unrecognised tail always has — `canonical` has never written a bare section
  hash, so one exists only where somebody typed it.
- **An alias is for hashes this app did not write.** `nav.links.test.tsx` fails
  the build if an internal href uses one, which is why the two `#history/timeline`
  links in `Overview.tsx` are now `#work/timeline` and the one that said
  `cta="history"` says `cta="timeline"`.
- **The evidence and audit files keep their old route names**, for the reason
  given in the previous note: a measurement rewritten to match today is a
  measurement that has been falsified. The translation table in
  [`README.md`](README.md) gained the four new rows.
