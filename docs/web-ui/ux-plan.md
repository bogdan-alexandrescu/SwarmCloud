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
* **Three of the items below are being worked right now by other lanes**, not
  waiting for a decision: §1.1 (the canvas), §1.3 (the help count) and the
  overflow list in §3. Each is marked `IN FLIGHT` where it appears, because a
  proposal and a thing already half-built are read differently and this
  document would otherwise invite a second lane onto the same file.
* One number was corrected rather than re-measured: §1.4 said "four top-level
  sections", and `SECTIONS` in `App.tsx` holds **six**. The 21 rail elements and
  the 24–67 per screen are the sweep's own measurements and are untouched.

§4, *What I did not check*, is unchanged in every word. It is the most useful
part of the document and the easiest to quietly soften.

---

## 1. What is actually wrong, in order of how much it costs a user

### 1.1 The console cannot show a real workflow — `IN FLIGHT`

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

1. **Fit-to-width with semantic zoom.** The canvas scales to the column and
   nodes shed fields as they shrink — name and state at every size, profile and
   duration above a threshold, figures only when there is room. A minimap for
   position. This is what Railway does.
2. **Stage-collapsing.** A stage wider than N renders as one band —
   "8 scanning · 6 done · 2 running" — that expands on click. The graph stays
   the shape of the workflow rather than the shape of its widest moment.
3. **Two views.** A list that is always readable, and a canvas for when the
   shape matters. The collapsed row already is view one; the question is
   whether the canvas should try to be complete.

Recommendation: **2, then 1.** Collapsing is the smaller change and addresses
the actual complaint — that a fan-out destroys the view — while semantic zoom
is the larger project that makes the canvas good rather than merely possible.

> `IN FLIGHT`. A lane is building the canvas fix now, so the figures above are
> the *before* side of a comparison rather than a standing description. Read the
> three options as the argument that lane is implementing, and read its own
> report for which one it took and what it measured after.

### 1.2 The submit flows ask for JSON

`INPUT (JSON OBJECT)` over a textarea containing `{}`, parsed with
`JSON.parse`. That is a developer's debug affordance presented as the product's
primary creation flow. It is already rebuilt in the structural pass; what
remains is verifying the rebuilt flow against the required-keys path, which
that lane reported it could not reach because the fixture does not serve it.

The two tabs are now called **Submit a task** and **Submit a workflow**, not
"New agent" and "New workflow" — a task at `READY` is not a running agent, and
invariant 1 is that neither state holds capacity, so the old tab promised
something the page it opened had to take back.

### 1.3 Help is a widget, and it should mostly be a layout — `IN FLIGHT`

82 help widgets. On Runtimes, 19 — on a single screen. Every one is a `?`
someone has to notice, hover, and read, to learn something the layout could
have said. Nine of them opened where they could not be read at all until today.

The number is the finding. A console needing 82 explanatory popovers is a
console whose labels are not carrying their weight. Cutting the count is a
design task, not a deletion task: each one is either (a) a label that should be
clearer, (b) a column that should have a unit, or (c) a genuine platform
concept that belongs in docs with a link.

Target: **under 20**, by fixing (a) and (b) and linking (c).

> `IN FLIGHT`. A lane is driving the count down now. The 82 is the baseline it
> is measured against; do not re-derive it from a later HEAD and conclude the
> finding was wrong.

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

> **Two of those three names have landed, and the merge has not. Do not read
> the rename as this proposal shipping.** The sections called Agents and Pools
> were each named after their own first tab, so the rail and the breadcrumb both
> drew `Agents > Agents` and `Pools > Pools`. Renaming the parents fixed that,
> and *Work* and *Capacity* were the right names for the same reason this
> proposal picked them: each covers all of its children instead of one of them.
> The section count is unchanged at six. `redesign.md`'s "What was considered
> and rejected" holds the standing objection to the merge itself — that "what is
> happening now" and "what happened over the last 500 tasks" are different reads
> at different costs with different failure modes — and that objection is not
> answered by this measurement. The rule the rename did establish is in
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

1. `IN FLIGHT` — the 12 findings in
   [`overflow-inventory.md`](../audits/2026-09-23/overflow-inventory.md), of
   which the worst truncate measurements — `mock · 15 can start` rendering
   `mock · 1…` is a prefix of a number standing where the number was.
2. Verify the rebuilt submit flow against the required-keys path.

**Next, the decision above:**

3. `IN FLIGHT` — stage-collapsing for wide fan-outs (§1.1, option 2).

**Then, the larger work:**

4. The three-section IA (§1.4). This is the change that makes the console feel
   different to use rather than merely look different. **Still a proposal**, and
   the section rename did not start it — see the note in §1.4.
5. `IN FLIGHT` — drive the help count under 20 (§1.3) — every one removed is a
   label that started carrying its own weight.
6. Semantic zoom for the canvas (§1.1, option 1).

Three of those six are being built as this is filed. The two that are not in
flight and not blocked are **2** and **4**: one is a verification the fixture
does not currently support, and one is a product decision.

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
