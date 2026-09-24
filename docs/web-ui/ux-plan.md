# The UX, as measured — and what to do about it

Written 2026-09-24 from a sweep of the running console, not from the source.
Evidence: 51 full-page screenshots across 15 routes at 1440×900 and 390×844 in
both themes, 588 interactive elements inventoried by accessibility tree, 82
help widgets opened one at a time and measured, 12 overflow defects traced to
their declarations, and one 30-step workflow run end to end on the live
platform.

Every claim below names what was measured. Where something is an opinion it
says so.

**What changed between the sweep and this file being committed**, so that a
reader can tell the sweep's own words from the corrections:

* The nav was renamed — `agents` → `work`, `pools` → `capacity`, ids and labels
  together, and the `Capacity holders` tab became `Holders`. §1.4 proposed
  *Work* and *Capacity* as names and two of them have landed, for a reason that
  has nothing to do with this plan; see the note in §1.4 before reading it as
  progress on the merge.
* The logo grew. §2 carries the figures.
* **Three of the items below were being worked by other lanes as this was
  filed**, not waiting for a decision: §1.1 (the canvas), §1.3 (the help count)
  and the overflow list in §3. Each was marked `IN FLIGHT` where it appears, and
  is marked `DONE` now that it has landed, because a proposal, a thing half-built
  and a thing shipped are read differently and this document would otherwise
  invite a second lane onto the same file. **As of 2026-09-24 none of the three
  is in flight**: §1.1 is `DONE` on both counts (stage-collapsing, then semantic
  zoom), §1.3 is `DONE` (#5), and the overflow list is done in source for eleven
  of its twelve findings and partly for the twelfth (#4, #3). What each did NOT
  verify is said where it is marked.
* One number was corrected rather than re-measured: §1.4 said "four top-level
  sections", and `SECTIONS` in `App.tsx` holds **six**. The 21 rail elements and
  the 24–67 per screen are the sweep's own measurements and are untouched.

§4, *What I did not check*, is unchanged in every word. It is the most useful
part of the document and the easiest to quietly soften.

---

## 1. What is actually wrong, in order of how much it costs a user

### 1.1 The console cannot show a real workflow — `DONE`

Measured on `wf_7e2ee6c3075d43228e5a`, 30 steps, widest stage 13 nodes:

| | |
|---|---|
| canvas | 1776 × 4134 px |
| visible wrapper | 1138 px wide |
| nodes clipped | **17 of 30** |
| nodes fully off-screen | 4 |
| vertical scroll to see one workflow | **4.6 screen-heights** |

This is the single largest failure in the product. A workflow of the size the
platform is built to run cannot be read at all. The transpose to top-to-bottom
helps the *shape* but not this: a 13-wide stage at 212px per node is 2,756px,
wider than any monitor.

**This needs a decision, not a tweak.** Three honest options:

1. **Fit-to-width with semantic zoom.** `DONE`. The canvas fits the column and
   nodes shed fields as they narrow — name and state at every size, profile and
   duration above a threshold, figures only when there is room. A minimap for
   position. This is what Railway does.
2. **Stage-collapsing.** `DONE`. A stage wider than N renders as one band —
   "8 scanning · 6 done · 2 running" — that expands on click. The graph stays
   the shape of the workflow rather than the shape of its widest moment.
3. **Two views.** A list that is always readable, and a canvas for when the
   shape matters. The collapsed row already is view one; the question is
   whether the canvas should try to be complete. **Not taken**, and no longer
   needed for this defect: 1 and 2 together make the canvas readable, and the
   collapsed row remains the always-readable list.

Recommendation was **2, then 1**, and that is the order they landed in.

### What 1 and 2 actually do, and what they cost

The recommendation called semantic zoom "the larger project that makes the
canvas good rather than merely possible", and the distinction held up: stage
collapsing replaced a 3,651px row with a band you have to open, and **opening
one put you straight back into the 3,651px row**. Semantic zoom is what makes
the stage drawable instead of summarised.

**It shrinks nothing.** The option above says "the canvas scales"; that half was
refused. Scaling shrinks type, `typescale.test.ts` holds a 12px floor under six
steps, and the owner has twice said this console is hard to read — a node scaled
to fit is texture. What narrows is the node, by dropping whole fields:

| tier | what a node says | width | steps per stage |
|---|---|---|---|
| `figures` | name, state, profile, duration, four run figures | 255px | 3 |
| `details` | name, state, profile, duration | 144px | 6 |
| `names` | name, state | 138px | 6 |

Widths are **derived, not chosen**: a tier's width is the widest row it still
draws, at a character advance taken from `measureText` in the browser
(`dag.ts`'s `MONO_ADVANCE_EM`, cross-checked against four separately measured
figures). They move with the workflow's own step names, so a long name widens
the node rather than wrapping onto a second line — which, on a card whose height
the layout has already committed to, overlapped the node beneath it.

Two things a reader should know about the table:

* `details` and `names` come out nearly the same width here because the **state
  word** (`dead_lettered`, 13 characters) is what sets both. That is information
  rather than a defect: it says no further zoom will help, and the two diverge
  as soon as a workflow's names or runner profiles are the binding constraint.
* **Thirteen steps still do not fit any tier** — 2,150px at the narrowest — so
  the measured run's widest stage is still a band. Zoom happens *instead of*
  collapsing, not instead of the band.

**What it cost, stated plainly.** A `details` node has no cost, token or
checkpoint figures on it; those are one click away on the step's own page, and a
mark beside the zoom control names what the current tier is not drawing so that
a missing field reads as a decision about the zoom rather than a fact about the
step. A four-segment control (`Auto · Figures · Details · Names`) puts any of
them back.

**One correction fell out of the arithmetic**, and it predates this pass:
`NODE_W` was 248, derived from the node's 24px of padding without its 4px of
border, and nothing had measured the row carrying the state word beside the
duration. At 248 the string `99.9k in · 99.9k out` ellipsed by 2.6px — an F1
truncation, a prefix of a number standing where the number was, in the very cell
that width was chosen to protect. It is 255 and computed now.

**Not measured.** jsdom has no layout engine, so the gate for this work asserts
the widths the components *emit* and which elements exist — it cannot see
overlap, wrapping or real text measurement. The before-figures at the top of
this section were taken in a browser; there is no matching after-sweep yet.

### 1.2 The submit flows ask for JSON

`INPUT (JSON OBJECT)` over a textarea containing `{}`, parsed with
`JSON.parse`. That is a developer's debug affordance presented as the product's
primary creation flow. It is already rebuilt in the structural pass; what
remains is verifying the rebuilt flow against the required-keys path, which
that lane reported it could not reach because the fixture does not serve it.

> **The fixture serves it now (PR #25, 2026-09-24).** `fixtureCapacity` carried
> no `input_contract` on any runner profile, so `requiredInputKeys` read null for
> all of them and a fixture session could only ever show the *unread* mark.
> `FIXTURE_INPUT_CONTRACTS` in `api.ts` is `swarm_api.runnerinputs.input_contract()`
> over the frozen catalogue — `claude-code` and `codex` require `prompt`, the
> other three require nothing, as a measured empty list — held to the server's
> function by `tests/unit/control_plane/test_ui_fixture_input_contract.py` and
> shown served by `src/__tests__/submit.fixture.test.ts`. **The verification
> itself has still not been done**: nobody has driven the rebuilt form against
> that fixture in a browser. That is §3 item 2, and it is now unblocked rather
> than finished.

The two tabs are now called **Submit a task** and **Submit a workflow**, not
"New agent" and "New workflow" — a task at `READY` is not a running agent, and
invariant 1 is that neither state holds capacity, so the old tab promised
something the page it opened had to take back.

### 1.3 Help is a widget, and it should mostly be a layout — `DONE`

82 help widgets. On Runtimes, 19 — on a single screen. Every one is a `?`
someone has to notice, hover, and read, to learn something the layout could
have said. Nine of them opened where they could not be read at all until today.

The number is the finding. A console needing 82 explanatory popovers is a
console whose labels are not carrying their weight. Cutting the count is a
design task, not a deletion task: each one is either (a) a label that should be
clearer, (b) a column that should have a unit, or (c) a genuine platform
concept that belongs in docs with a link.

Target: **under 20**, by fixing (a) and (b) and linking (c).

> `DONE` — #5 (`350c2e1`, merged 2026-09-23). **139 help anchors in the source
> became 16**, at most two in any one file; the 82 `?` widgets on screen are what
> those 139 anchors drew across the fifteen routes, and 82 remains the baseline —
> do not re-derive it from a later HEAD and conclude the finding was wrong. The
> rule is written down in [`help-density.md`](help-density.md): an explanation
> goes to the label, then the column head, then the screen's footer index, and
> only then a `?`, at most one per screen. `apps/swarm-ui/tests/help.test.ts`
> holds it — it fails over **20 glyphs in total or 2 in any file**, with a floor
> of 10 so deleting every explanation does not pass.
>
> **Not measured:** the on-screen count after the change. 139 → 16 is a count of
> source anchors, read by the same grep the finding used; nobody has re-run the
> 15-route sweep to count the `?` a reader actually sees.

### 1.4 The information architecture makes you hunt

Six top-level sections with 15 screens under them. Measured from the
accessibility tree, every screen carries 24–67 interactive elements, and the
rail alone is 21 of them on every single screen.

The user's actual questions are few:

- is anything broken right now?
- what is running?
- why is this one stuck?
- how much has this cost?
- can I start something?

Overview answers the first. The rest are spread across Agents, Workflows,
Pools, Runtimes, Holders, Accounts, Provider quota, Platform counts, Pool
limits and Timeline — ten destinations for four questions, organised by
*which subsystem owns the data* rather than by what anyone wants to know.

**Proposal:** collapse to three.

| Section | Answers | Absorbs |
|---|---|---|
| **Work** | what is running, what failed, why | Agents, Workflows, Timeline, submit flows |
| **Capacity** | what can run, what is holding it, what it costs | Pools, Runtimes, Holders, Accounts, Provider quota |
| **Admin** | what an operator changes | Pool limits, Tenants, Platform counts |

That is 3 rail entries instead of 21, and every screen the user reaches is the
screen that answers the question they had.

> **`LANDED 2026-09-24`, and with the standing objection intact rather than
> overruled.** The nav is now Overview (the landing screen) plus Work, Capacity
> and Admin. What collapsed is the SECTIONS; no screen was merged, removed or
> combined with another. Timeline is a pane of Work beside Agents, with its own
> route, its own read, its own empty state and its own failure state; Overview
> is still its own screen. `redesign.md`'s "What was considered and rejected"
> refused a three-section nav because it read this proposal as merging Activity
> into Work and Overview into Capacity — "what is happening now" and "what
> happened over the last 500 tasks" are different reads at different costs with
> different failure modes — and that objection still stands. Nothing above
> asked for the merge; the table's "Absorbs" column is about which SECTION a
> pane sits under.
>
> Two details this table does not say, recorded so a reader does not go looking
> for them. It omits **Runner profiles** from Capacity's absorb list (its ten
> destinations were the ten a reader hunts through, and that pane was not one
> of them) — the pane is under Capacity and is now labelled **Profile
> headroom**, because "Runtimes" and "Runner profiles" as adjacent tabs is how
> a per-tenant figure gets read as a platform one. And the count of rail
> entries is **four, not three**: Overview keeps its own, because it is the
> screen you land on rather than a question you navigate to.
>
> The rule the earlier rename established still holds and is in
> [`redesign.md`](redesign.md#no-section-may-be-named-after-one-of-its-own-tabs):
> **no section may be named after one of its own tabs.**

### 1.5 Two implementations of the same thing, repeatedly

This session found four:

- two help cards (`HelpCard` and `App.tsx`'s `SectionQuestion`) — the second
  missed every fix the first received
- two namespace spellings (`swarm-tenant-` and `swarm-`) — which broke GKE
  dispatch entirely and read as an IAM failure
- two `forgetProbes` implementations merged without conflict
- three restatements of the tenant namespace, one of them in the test fixture
  that was supposed to catch drift

This is the product's most expensive recurring defect and it is not a UI
problem. It wants a rule: **a value or a widget defined twice is a defect even
while both copies agree**, because agreement is not maintained by anything.

The nav rename is the fifth instance of the same shape, in a quieter place, and
the reason its *ids* moved with its labels: `SECTION_ALIASES` would have kept
every `#agents/…` href working, and *because* it would, nothing would ever have
made anyone update one. `nav.links.test.tsx` now fails the build if an internal
href uses an alias at all. An alias that internal links also use is a spelling
nothing can ever retire.

---

## 2. What is already fixed, with evidence

| | before | after |
|---|---|---|
| gutter, rail → content | 0 px | 32 px |
| uppercase elements (Overview) | 93 | 0 |
| bordered elements | 41 | 17 |
| drop shadows | 14 | 6 |
| `--t-figure` | 30 px | 22 px |
| accent-blue paints | 23 | 4 |
| help widgets opening off-screen | 9 | 0 |
| clipped text in the workflow row | 4 | 0 |
| genuine overflow at 390px | — | 0 |
| brand mark | 28 px | 36 px |
| wordmark | 14 px | 18 px (`--t-title`) |
| brand row height | 44 px | 52 px |

17 borders is inside the reference range (Railway 10, Hetzner 17, Northflank
20). The bordered count is the honest measure of whether a redesign was
structural: the previous pass moved values and left it at 41 → 41.

**The logo grew 30% because the owner asked for 30%, and the arithmetic is why
those are the three numbers.** 28 × 1.3 = 36.4, rounded *down* to a whole pixel
because the mark's strokes are on a 24-unit viewBox and a fractional width puts
them on half pixels. 14 × 1.3 = 18.2, and `--t-title` is already 18px — a
`--t-brand: 18px` was written first and `typescale.test.ts` refused it,
correctly, because a second declaration of a size the scale already has is how
seventeen type sizes came back last time. The row went 44 → 52 rather than to 64
because 44 left a 36px mark 4px of air, which reads as a logo jammed into a
strip; 52px of a 900px screen is still under 6% spent on chrome, which is the
density argument the whole header is held to.

---

## 3. Sequence

**Now, because they are defects rather than design:**

1. `DONE` in source for eleven of the 12, **partly** for F11 — the findings in
   [`overflow-inventory.md`](../audits/2026-09-23/overflow-inventory.md), of
   which the worst truncate measurements — `mock · 15 can start` rendering
   `mock · 1…` is a prefix of a number standing where the number was. Ten were
   closed by #4 (`9d23b82`), F9 by #3 (`c440e68`), and F11's escape and its
   progress string by #3 and `styles.css`; whether F11's `state not derived`
   still ellipses is open. The inventory's §7 is the per-finding record. **Not
   re-measured**: every row was established by reading source, and
   `overflow/probe.js` has not been re-run against any of it.
2. Verify the rebuilt submit flow against the required-keys path. **Unblocked,
   not done** — the fixture serves the required-keys path as of PR #25 (§1.2);
   the form has not been driven against it.

**Next, the decision above:**

3. `DONE` — stage-collapsing for wide fan-outs (§1.1, option 2).

**Then, the larger work:**

4. The three-section IA (§1.4). This is the change that makes the console feel
   different to use rather than merely look different. `LANDED 2026-09-24` —
   the nav is Overview plus Work, Capacity and Admin, and no screen was merged
   to get there. See the note in §1.4 for what that did and did not include.
5. `DONE` — drive the help count under 20 (§1.3) — every one removed is a
   label that started carrying its own weight. 139 source anchors → 16, held by
   a CI ceiling of 20; see §1.3 for what was not re-measured.
6. `DONE` — semantic zoom for the canvas (§1.1, option 1). See §1.1 for the
   tiers, what they cost and the one thing it does not fix (a 13-wide stage is
   still a band).

This paragraph used to say that **4** was the one item neither in flight, nor
done, nor blocked, and that **2** was a verification the fixture did not
support. As of 2026-09-24 neither is true: **4** has landed (see §1.4), and **2**
is the only item left, unblocked by the fixture and waiting on someone to drive
the form.

---

## 4. What I did not check, stated plainly

- **Keyboard traversal and focus order.** 588 interactive elements and I
  exercised them by click, not by Tab. Focus trapping in the drawer and the
  portalled help cards is unverified, and the portal moves cards out of DOM
  order, which is exactly what breaks tab sequence.
- **Screen-reader output.** Roles and labels exist and are asserted by tests;
  nobody has listened to a screen.
- **The submit flows end to end against the live API.** Rebuilt and rendered,
  not driven to a created task.
- **Error and empty states on every screen.** Fixtures cover several; I
  confirmed a few, not all 15.
- **Any width between 390 and 1440.** Two points, not a range. The drawer's
  1100px breakpoint is where one known defect lives.
