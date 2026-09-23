# Overflow, clipping and overlap — a measured inventory

**Date:** 2026-09-23 · **Branch:** `fix/silent-failures-env-parity-and-audits`
**Scope:** `apps/swarm-ui`, 15 routes × {1440×900, 390×844} × {light, dark}, plus
six interaction states. **Nothing was fixed.** This file is the deliverable; the
build lanes consume it.

The owner's report was "in many places inside the app, there are elements that
are overflowing or being partially hidden, covered by other elements". They are
right. This is the list, with the pixels that prove each one.

---

## How to read this, and what it cost to make it trustworthy

An earlier DOM probe over-reported badly: most of its hits had `clientWidth: 1`
and were collapsed help cards, not clipping. This pass was built to fail the
other way — **every finding below was seen in a screenshot before it was
written down**, and four whole classes of candidate were measured, investigated
and then *thrown away*. Those rejections are in §4, because knowing what not to
chase is half the value of an inventory.

The probe is committed beside this file at `overflow/probe.js`; the raw
findings are `overflow/findings.json` (134 baseline rows). Re-run it with
`bb eval overflow/probe.js` against `npm run dev`.

Four corrections the probe needed before its output could be believed, each
found by disagreeing with a screenshot:

1. **Ancestor walks must stop at the first clipping ancestor.** 408 of the
   first pass's 518 hits were elements "escaping" a grandparent while an
   intermediate `overflow-x: auto` already contained them. Geometry, not pixels.
2. **Content scrolled out of its own scroller is not covered.** Every `covered`
   hit in the first pass named `.ctl-dock`. The dock is a `position: static`
   grid row, not an overlay — it was simply standing where scrolled-away
   content's rect landed. Verified by scrolling to the extreme: at maximum
   scroll `main.work` ends at y=823 and the dock starts at y=871. **Nothing in
   this app is permanently covered by the dock.**
3. **Hit-test only the pixels an element actually paints.** Sampling an
   element's centre when its clipping ancestor cuts it in half answers a
   question nobody asked.
4. **Park the virtual cursor.** A cursor left resting on a `?` glyph re-opens
   its help card after every reload, which put one real defect (§F8) on the
   baseline of all 15 routes as if it were 15 defects.

### Two things this inventory is honest about

**The dev server was serving a stale bundle when this session started.** The
first screenshot taken showed `RUNNING / UNITS HELD / SCOPE / POLL` in full
caps — i.e. the pre-§B5.2 stylesheet — to a browser tab that had been open
since before that commit. Every measurement below was re-taken after a hard
reload, and the full sweep was run twice to confirm it reproduces. **Anyone
re-checking these findings must hard-reload first**, or they will measure a
build that is not on this branch.

**Screenshots are 1× PNGs of a headless Chromium.** They show what is painted,
not what a retina display or a different font stack would paint. Where a
finding turns on one or two pixels it says so.

### Two sections were renamed after this was measured (2026-09-24)

`#agents/*` is now `#work/*` and `#pools/*` is now `#capacity/*`. Only the two
**section** ids changed; every tab id below is unchanged, so `#agents/running`
is `#work/running` and `#pools/quota` is `#capacity/quota`. Both old spellings
still resolve through `SECTION_ALIASES` in `App.tsx`, so a route written the old
way still reaches the screen it reached — but it is the old spelling of a
decision that was made deliberately, and `nav.links.test.tsx` fails the build if
an internal href uses one.

**The `**Route**` lines in each finding below have been re-pointed. Nothing else
has.** The route names inside the measurement tables — `pools/pools`,
`pools/accounts`, `history/timeline` and the rest — are left exactly as they
were measured, because those rows are a dated record of what was on screen at
what width and rewriting a cell in one is falsifying it. Read them through the
map above.

---

## 1. Ranked findings

Ranked by how visible the defect is to someone using the console, not by how
large the number is. F1–F5 are visible at 1440 on a first look; F6–F8 need a
narrow window; F9–F12 are smaller.

---

### F1 — The Capacity card truncates the headroom number out of existence

| | |
|---|---|
| **Route** | `#overview/now` |
| **Viewport** | 1440×900 **and** 390×844 |
| **Theme** | both (identical) |
| **Element** | `section.ctl-card:nth(2) .ctl-util > span.ctl-util-name` (×5) |
| **Screenshot** | `overflow/01-capacity-labels-1440.png` |

`.ctl-util-name` resolves to `width: 79.33px` with `flex: 0 1 auto` and
`text-overflow: ellipsis`, against content that needs 160–211px:

| string | needs | gets | lost | renders as |
|---|---|---|---|---|
| `browser · 0 can start` | 177px | 79px | **55%** | `browser …` |
| `claude-code · 0 can start` | 211px | 79px | **63%** | `claude-c…` |
| `codex · 10 can start` | 169px | 79px | **53%** | `codex · …` |
| `generic · 10 can start` | 185px | 79px | **57%** | `generic …` |
| `mock · 15 can start` | 160px | 79px | **51%** | `mock · 1…` |

This is ranked first because of *what* is lost, not how much. The string carries
a measurement — how many agents can still start — and the ellipsis eats it in
three different ways on one card:

* `codex · …` and `generic …` lose the figure **entirely**;
* `mock · 1…` renders **a prefix of a number as if it were the number**. The
  value is 15. The screen says 1;
* `claude-c…` loses the runner-profile identity.

The card is 400px wide. The bar, the `5 / 5` and the backend name to its right
all have room. The 79px is not a space constraint; it is a fixed basis that
nothing recomputes.

**This one is not width-specific.** It is the same at 390.

---

### F2 — In the agent drawer, the words that say "this was never measured" are the truncated ones

| | |
|---|---|
| **Route** | `#work/running` → click any row (drawer) |
| **Viewport** | 1440×900, drawer at its 480px default |
| **Theme** | both |
| **Element** | `div.ctl-util > span.ctl-util-name`, `> span.ctl-util-by` |
| **Screenshot** | `overflow/02-drawer-util-rows-1440.png` |

The resource rows draw a hatched bar — the sheet's own mark for "nobody
reported this" — and put the explanation beside it. Both halves truncate:

| element | string | needs | gets | lost | renders as |
|---|---|---|---|---|---|
| `.ctl-util-name` | `cpu not sampled` | 126px | 106px | 16% | `cpu not sam…` |
| `.ctl-util-name` | `memory peak RSS` | 126px | 106px | 16% | `memory peak…` |
| `.ctl-util-name` | `workspace peak` | 118px | 106px | 10% | `workspace p…` |
| `.ctl-util-by` | `never measured` | 101px | 79px | **22%** | `never mea…` |
| `.ctl-util-by` | `never written` | — | — | **29%** | cut |
| `.ctl-util-by` | `not yet written` | — | — | **38%** | cut |
| `.ctl-util-by` | `at exit` | 51px | 38px | **25%** | cut |
| `.ctl-util-by` | `latest heartbeat 3m ago` | — | — | **77%** | cut |

The honesty invariant says an absence must never render as a measurement and
must keep an accessible route to the words. Here the hatch survives and **the
words do not**. `cpu not sam…` is not a sentence a reader completes correctly,
and `never mea…` is one glyph away from reading as a value.

The row is 413px wide and the bar is fixed-width; the two text boxes are the
only things being squeezed.

---

### F3 — Below ~1115px with the drawer open, the list hard-clips identity with no ellipsis

| | |
|---|---|
| **Route** | `#work/running` → drawer open |
| **Viewport** | 1101–1115 × 900 (the drawer is a grid column down to 1100) |
| **Theme** | both |
| **Element** | `div.row.clickable > span.agent` |
| **Screenshot** | `overflow/03-drawer-list-clip-1110.png` |

`span.agent` is `overflow: hidden; text-overflow: clip` — **not** ellipsis. So
when it runs out of room it does not signal; it simply stops.

| viewport | `clientWidth` | `scrollWidth` | cut |
|---|---|---|---|
| 1440 | 278 | 278 | 0 |
| 1200 | 126 | 126 | 0 |
| 1140 | 66 | 66 | 0 |
| **1120** | 70 | 70 | 0 |
| **1115** | 65 | 66 | 1 |
| **1110** | 36 | 66 | **30** |
| **1101** | 51 | 66 | **15** |

In the screenshot at 1110 the damage is worse than the number suggests:

* the `claude-code` runner-profile column has **disappeared entirely** — the
  rows show a state and an id and nothing else, with no indication a column was
  dropped;
* the task ids are cut mid-character: `460409c`, `92eb480`, `19d91b8`,
  `8ea5c26`, `a073aff`, `f_scan_`. **A truncated id is not an unreadable id —
  it is a different, plausible id**, and `text-overflow: clip` removes the one
  glyph that would have said so;
* the rail truncates alongside: `Submit a workfl…`, `Provider quota…`.

The brief flagged this as a regression that shipped once before. It is clean at
1440 and at ≥1120 — it comes back in a 20px band immediately above the drawer's
own 1100px breakpoint.

---

### F4 — The drawer's ✕ is sticky with `z-index: auto` and sits on the text at every scroll position

| | |
|---|---|
| **Route** | `#work/running` → drawer open, drawer scrolled |
| **Viewport** | 1440×900 |
| **Theme** | both |
| **Element** | `div.drawer.ctl-drawer > button.drawer-close` |
| **Screenshot** | `overflow/04-drawer-close-overlap-1440.png` |

`position: sticky`, `z-index: auto`, 32×32 parked at (1360, 63) inside the
drawer's own scroll container (`scrollHeight − clientHeight = 5155`). Drawer
content scrolls under it and it has an opaque background, so it paints over
whatever is passing.

At `scrollTop: 720`, `elementFromPoint` at the button's centre returns
`drawer-close` while the box underneath is the empty-state paragraph
`None of the 3 attempts reported a value, so there is no axis and no line to
draw. an absent measurement, not $0.00.` The screenshot shows the ✕ sitting on
the first line between *"there is no"* and *"axis"*.

It is not one unlucky offset. The same probe caught it covering three different
things at three different scroll positions in one session:

* `None of the 3 attempts reported a value, so there is no…`
* `Recorded on the task and returned by the API, but no wo…`
* `8.7 MiB in 1 object`

There is no gutter reserved for it and no background on the scrolling content
to stop it.

---

### F5 — The dock's route list truncates paths until different routes are indistinguishable

| | |
|---|---|
| **Route** | any → open the dock (`API reads`), drag it tall |
| **Viewport** | 1440×900, dock at its 70vh ceiling (630px) |
| **Theme** | both |
| **Element** | `div.source > span.s-path > span.s-path-t` |
| **Screenshot** | `overflow/05-dock-route-paths-1440.png` |

| string | lost | renders as |
|---|---|---|
| `/v1/admin/dispatch` | **65%** | `/v1/a…` |
| `/v1/admin/tenants` | **63%** | `/v1/a…` |
| `/v1/tasks/{id}/checkpoints` | **54%** | `/v1/tasks/…` |
| `/v1/tasks/{id}/attempts` | **48%** | `/v1/tasks/…` |
| `/v1/tasks/{id}/logs` | **43%** | `/v1/tasks/…` |
| `/v1/resource-classes` | **41%** | `/v1/resour…` |
| `/v1/admin/leases` | 32% | `/v1/admin…` |

In the screenshot, `/v1/a…` appears **twice**, both `403 · admin only`, for two
different routes. `/v1/tasks/…` appears **three times** with three different
latencies. `/v1/admin…` appears twice.

The panel's entire job is to say which route did what. It is also, in that
screenshot, occupying the top half of a 630px dock with the bottom half
completely empty — the truncation is not buying space for anything.

Worth noting for whoever fixes it: **this is worse at 1440 than at 390.** At
390 the dock goes full-width and the same paths render in full.

---

### F6 — At 390 every data table hides a fifth to two thirds of its columns behind a scrollbar nobody can see

| | |
|---|---|
| **Routes** | all 15 (70 instances) |
| **Viewport** | 390×844 |
| **Theme** | both |
| **Element** | `div.ctl-table`, `div.table-wrap` (`overflow-x: auto`) |
| **Screenshot** | `overflow/06-pools-tables-390.png` |

`overflow-x: auto` with a real scrollbar is excluded by construction from this
inventory. These do not have one: the reserved gutter measures ≤2px and no
scrollbar appears in any screenshot, because this platform paints overlay
scrollbars that exist only during a scroll.

Worst offenders:

| route | table | client | scroll | hidden |
|---|---|---|---|---|
| `pools/pools` | Headroom (`.cap-headroom`) | 354 | 787 | **55%** |
| `pools/pools` | pools by family ×6 | 356 | 502–704 | 29–49% |
| `pools/accounts` | the pool | 358 | 909 | **61%** |
| `runtimes/catalogue` | class sizing | 354 | 714 | **50%** |
| `admin/limits` | pool ceilings | 356 | 658 | **46%** |
| `pools/profiles` | pool-clearing ×5 | 358 | 543–615 | 34–42% |
| `pools/quota` | tenant quota ×2 | 356 | 550–557 | 35–36% |
| `history/timeline` | per-engineer | 358 | 512 | 30% |
| `pools/holders` | capacity holders | 354 | 453 | 22% |

**The Headroom table is the one to look at first.** Of its five columns
(`Runner profile`, `Could start (min across pools)`, `Weight`, `Held back by`,
`Backend`) exactly one is on screen. The screenshot shows five rows —
`browser`, `claude-code`, `codex`, `generic`, `mock` — **with no values at
all**, above a footer that reads `5 of 5 measured`.

That is the honesty invariant inverted. The app measured five things, says so,
and renders a table that looks like it has nothing in it. A reader's correct
inference from that picture is "no data", and it is wrong.

---

### F7 — At 390 the navigation rail hides up to two thirds of the navigation

| | |
|---|---|
| **Routes** | all 15 |
| **Viewport** | 390×844 |
| **Theme** | both |
| **Element** | `nav.ctl-rail` (`overflow-x: auto`, `clientWidth: 358`) |
| **Screenshot** | `overflow/06-pools-tables-390.png`, `overflow/07-helpcard-offscreen-390.png` |

The rail becomes a horizontal strip and overflows it:

| route | scrollWidth | hidden |
|---|---|---|
| `pools/*` | 1128 | **68%** |
| `agents/*` | 983 | **64%** |
| `history/*`, `admin/*` | 819–822 | **56%** |
| `overview`, `runtimes` | 565 | 37% |

Same missing affordance as F6 — no scrollbar is painted. In the screenshots the
strip reads `Overview  Agents  Runtimes  Pools  Histo` / `Overview  Agents
Runtimes  Pools  Pool`, cut mid-word, and `Admin` is never reachable-looking.
`document.documentElement.scrollWidth` equals `clientWidth` on every route, so
the page itself gives no hint either.

---

### F8 — At 390 the page-level help card opens 299px off-screen, and drags the whole app frame sideways with it

| | |
|---|---|
| **Routes** | all 15 (any page-level `?`) |
| **Viewport** | 390×844 |
| **Theme** | both |
| **Element** | `span.ctl-q > span.ctl-q-card` |
| **Screenshot** | `overflow/07-helpcard-offscreen-390.png` |

Measured on hover/focus of the breadcrumb `?`:

```
.ctl-q      anchor   x=354  w=20
.ctl-q-card          x=354  w=335.4   → right edge 689, viewport 390
                     computed: left: 0px; right: -315.391px
                     299px (89%) off-screen; display:block, opacity:1,
                     pointer-events:auto, not aria-hidden, not inert
.ctl-scroll          scrollWidth 689 vs clientWidth 390, overflow-x: auto
```

The screenshot shows the result: a white panel entering from the right edge and
showing three word fragments — `Ove`, `Is t`, `wh`. The help text is not
readable at all, and the card simultaneously makes the **entire application
frame** horizontally scrollable by 299px, again with no visible scrollbar.

**Root cause, named because it is one line.** `src/styles.css:5517`, inside the
narrow-viewport block:

```css
.ctl-q-card { right: auto; left: 0; max-width: min(44ch, 86vw); }
```

The base rule at `:5304` is `position: absolute; top: calc(100% + 6px);
right: 0; width: max-content; max-width: 44ch`. The narrow override flips the
card to open *rightward* from a 20px anchor that is itself pinned to the right
edge of the header, and it constrains **width** (`86vw` = 335.4px at 390)
without constraining **position**. 354 + 335.4 = 689.

A reader re-checking this must move the pointer off the glyph first: the card
is not in the DOM at all when closed, so a parked cursor makes it look like a
default-open panel. It is not one.

**FIXED — verified by reading, 2026-09-24. No code was changed for it here.**
`HelpCard.tsx` gained `useEdgeSafePlacement`, which measures the anchor on open
and on every scroll and resize, portals the card to `document.body`, and writes
`position: fixed` with a `left` that is clamped on BOTH branches:
`Math.min(Math.max(MARGIN, wanted), window.innerWidth - CARD_MAX - MARGIN)`.
That upper clamp is this finding: the comment beside it names the 299px and
names the cause, which is that the rail becomes a horizontal scroller below
900px so a glyph scrolled off to the right reports an `anchor.right` of ~1049
inside a 390px viewport. `SectionQuestion` in `App.tsx` — the app's *other*
help card, the one this finding's `span.ctl-q > span.ctl-q-card` selector
actually names — imports the same hook rather than reimplementing it, which is
the part worth checking on any future pass: two implementations of one widget
is how the first one's fixes stopped reaching the second.

The stylesheet's `.ctl-q-card` rules that §F8 called the root cause are still
in the file and are now inert: the inline `position: fixed` and `left` from the
hook beat them, and `right: 0` is ignored once a `left` and a non-`auto` width
are both present. They are left alone deliberately — they still size the card
(`max-width: min(44ch, 86vw)`), and it is the *position* half that had to stop
coming from CSS.

---

### F9 — The workflow DAG's `stop` button hangs out of its node

| | |
|---|---|
| **Route** | `#work/workflows` → expand a workflow |
| **Viewport** | 1440×900 |
| **Theme** | both |
| **Element** | `div.node.wait > div.node-stop > button.stop-btn.inline` |
| **Screenshot** | `overflow/08-wf-stop-button-1440.png` |

Node box bottom `808`, button bottom `820` — a **12px spill**, with the node at
`overflow: visible` and `.node-stop` at `position: static; margin: 0`. The
node's rounded border runs horizontally through the middle of the button.

Small, but it is the most obviously "broken-looking" thing on the screen,
because a border crossing a control reads as a rendering fault rather than a
tight layout.

---

### F10 — The dock summary line loses a fifth to two fifths of itself at 390

| | |
|---|---|
| **Routes** | all 15 |
| **Viewport** | 390×844 |
| **Element** | `button.ctl-dock-line > span.ctl-dock-facts` (`width: 279.6px`) |
| **Screenshot** | `overflow/06-pools-tables-390.png` (bottom edge) |

`13 routes · p95 400ms · 0 failed · 2 admin-only · newest just now` loses
**40%**, rendering `… · 0 failed · 2 …`. What falls off the end is the
`admin-only` count and the read age — the two facts that distinguish a console
that is fine from one that is stale or half-blind. `0 failed` survives, which
is the reassuring half.

---

### F11 — Workflow header strings truncate at 1440

| | |
|---|---|
| **Route** | `#work/workflows` |
| **Screenshot** | `overflow/10-workflows-expanded-1440.png` |

| viewport | element | string | lost | renders as |
|---|---|---|---|---|
| 1440 | `span.wf-state.unknown` | `state not derived` | **25%** | `state not de…` |
| 1440 | `span.wf-progress-text` | `1/5 done · 1 not started` | **22%** | `1/5 done · 1 not …` |
| 1440 | `dd` in `dl.node-nums` | `21.4k in · 3.2k out` | 4% | `21.4k in · 3.2k o…` |
| 390 | `span.wf-shape-text` | `1 → 2 → 1 → 1` | escapes its 56px box by **38px** | spills |
| 390 | `span.id` | `wf_5e5ad3b6f7da4299a839` | 9% | id cut |

`state not derived` is the same class as F2: the phrase exists precisely to say
that no state was derived, and it is the phrase being cut. `1 not …` loses the
word `started`, leaving a count with no noun.

`span.wf-shape-text` is the only genuine *escape* in the baseline set — a 93.9px
box of content inside a 56px parent with `overflow-x: visible`, so it paints
outside rather than clipping.

---

### F12 — `scan-terraform` truncates at every width

`#work/running`, `div.row.clickable > span.wf > span.wf-step`, `width: 104px`,
content 118px — **12% lost**, `scan-terraf…`, at 1440 as well as 390. Visible in
`overflow/09-overview-baseline-1440.png` and the drawer screenshot. Lowest
ranked because the ellipsis is present and honest and the string is a step name
the reader can recover one click away.

---

## 2. Also present, in named states

| state | how to reach it | what it adds |
|---|---|---|
| drawer open, 1440 | click a row on `#work/running` | F2, F4; checkpoint table hides **57%**, log window **18%** |
| drawer open, ≤1115 | same, narrow window | F3 |
| drawer open, 390 | same at 390 | drawer becomes a **full-screen overlay** — intended (`panes.ts`: two panes do not fit below 1100px). ~450 `covered` hits behind it are the overlay doing its job, not defects |
| workflow expanded | `#work/workflows`, click the bar | F9, F11 |
| dock dragged tall | click the dock line, drag the grip | F5 |
| help card open | hover/focus any `?` | F8 at 390; clean at 1440 |
| account row expanded | `#capacity/accounts`, click a row | see §4, rejected |
| error panel | open a `FAILED` task's drawer | **clean** — see §3 |

---

## 3. What was checked and found clean

Stated because a negative result stops the next person re-deriving it.

* **No theme-specific defect anywhere.** 134 baseline findings, and the light
  and dark sets are byte-identical after normalising. The two themes differ in
  colour tokens only; no metric changes. Four full sweeps, both themes.
* **No stacking/z-index overlap at first paint** on any of the 15 routes at
  either width. The only true overlaps in the app are F4 (sticky ✕) and F8
  (help card), and both need a state.
* **Nothing is permanently covered by the dock.** It is a `position: static`
  grid row. At maximum scroll, `main.work` bottom = 823 and dock top = 871.
* **No horizontal document scroll** on any route at either width:
  `document.documentElement.scrollWidth === clientWidth` everywhere. The
  sideways overflow in F6/F7/F8 is inside nested scrollers, which is why it is
  invisible rather than obvious.
* **The error panel is clean.** `pre.err.full` on a `FAILED` task:
  `white-space: pre-wrap`, `overflow: auto`, `scrollWidth === clientWidth`
  (413px at 1440, 332px at 390), no spill past its parent. Nothing to fix.
* **The agent list at 1440 with the drawer open does not clip identity.** The
  regression named in the brief is fixed above 1120px; F3 is a different, much
  narrower band.

---

## 4. Rejected candidates — measured, then thrown away

**These are not findings. Do not chase them.** Each was a probe hit that a
screenshot refuted.

| candidate | count | why it was rejected |
|---|---|---|
| Children "escaping" a grandparent box | 408 | An intermediate ancestor with `overflow-x: auto` already contained them. Geometric fact, zero painted pixels outside. |
| Text "covered" by `.ctl-dock` | 26 | Content scrolled out of its own scroller; its rect landed on the dock row. Confirmed by scrolling to the extreme — nothing is behind the dock. |
| Everything "covered" by the drawer at 390 | ~450 | The drawer is a deliberate full-screen overlay at that width. |
| `pools/accounts` expanded detail, `p.muted.small` | 4 | The paragraph is `white-space: nowrap` and its inline content runs 336px past its own box — but `.acct-action` is 1113px wide, so nothing is clipped and nothing collides. Verified in an image at both widths. **Geometrically ugly, visually fine.** |
| `span.ctl-util-name > span.ov-stop` "covered" by the bar | 2 | A restatement of F1: the text is gone because the ellipsis cut it, so the hit test finds the bar behind it. One defect, not two. |

---

## 5. The casing question — answered, and already fixed

The owner asked, in the words that triggered this round: *"also some helper text
is all CAPS and others are regular caps?? WHY?"*

**It is fixed on this branch, and the answer is in the sheet.** `styles.css`
§B5.2 records that there had been **no rule** — twenty-two independent
`text-transform: uppercase` declarations (`.ctl-eyebrow`, `.ctl-metric-label`,
`.ctl-table thead th`, `.ctl-fact > b`, `.ctl-mark`, `.ctl-q-title`,
`.ctl-dock-label`, `.brand-k`, `.tag`, `.t-label` and more), each decided by
whichever component was written that week, so labels of the same rank shouted on
one screen and whispered on the next. Runtimes alone rendered 93 uppercase
elements.

The rule now is a ban rather than a budget: `text-transform: uppercase` does not
appear in the sheet at all, and `typescale.test.ts` holds the count at zero.

**Verified against the live DOM, not the comment.** After a hard reload at
1440×900 on `#overview/now`:

```
elements with computed text-transform: uppercase   0
                                       capitalize  0
                                       lowercase   4   (.ctl-chip only)
elements with font-variant-caps != normal          0
```

The four `lowercase` uses are `.ctl-chip` bringing the API's `RUNNING` and
`DISPATCHED` down into the console's register — the one legal direction, since
it makes a string we did not author quieter rather than emphasising one we did.

**The caveat that matters more than the answer.** The first screenshot taken in
this session still showed the old all-caps labels, because `:5173` was serving a
pre-§B5.2 bundle to an already-open tab. If the owner is still seeing mixed
casing, that is almost certainly the same stale bundle and not the branch. A
hard reload is the first thing to try.

---

## 6. Method

```
cd apps/swarm-ui && npm run dev            # :5173, fixtures (api.ts:30)
~/.gstack/bin/bb viewport 1440x900 | 390x844
~/.gstack/bin/bb goto "http://localhost:5173/#<route>"
~/.gstack/bin/bb eval docs/audits/2026-09-23/overflow/probe.js
```

Dark theme is forced with `document.documentElement.setAttribute('data-theme',
'dark')`; the headless browser reports `prefers-color-scheme: light`, and the
sheet's light block is written `@media (prefers-color-scheme: light) {
:root:not([data-theme='dark']) { … } }`, so the attribute is the app's own
escape hatch rather than a hack. `bb` in this build has **no** media emulation
(`Emulation.setEmulatedMedia` is not on its CDP allow-list), so this is the only
route to dark.

The probe scans at every screenful of scroll and unions by finding identity,
because one scan sees one screenful. It is synchronous: assigning `scrollTop`
forces layout in Chrome, and `bb eval` does **not** await a Promise — an async
probe returns empty output with exit code 0, which is how this pass briefly
"found" nothing on all 15 routes.

### What no gate here can see, and what this pass still could not

* `spacing.test.tsx` is jsdom with a hand-written CSS resolver and no layout
  engine. It cannot see any of the above. Every finding here came from pixels.
* **Not verified: a server-error panel.** Dev builds read fixtures
  (`USE_FIXTURES = import.meta.env.DEV && !VITE_LIVE`, `api.ts:30`), there is no
  fixture failure switch, and this `bb` build has neither `route` interception
  nor `throttle`. Patching `window.fetch` at runtime does nothing because the
  fixture path never calls it. The `pre.err.full` panel in §3 was reached
  through a genuinely `FAILED` fixture task, which is the real component but not
  the network-error path.
* **Not verified: real devices, retina, or a non-Chromium engine.** All
  measurements are 1× headless Chromium.
* **Not verified: widths between 391 and 1100, or above 1440.** F3 was found by
  sweeping 1440→1101 on one route; no other route was swept for width bands, and
  more probably exist there.
* **Not verified: keyboard-only traversal.** F8's card is
  focus-openable, so it is reachable by <kbd>Tab</kbd> at 390 and lands
  off-screen — but the tab order itself was not audited.

---

## 7. Disposition, as of 2026-09-24

**Added by the lane that consumed this inventory. Nothing above was altered
except the `**Route**` lines (see the rename note in §How to read this) and the
FIXED paragraph inside §F8.** This section is the record of what has since
happened to each finding and how that was established — by READING the source
named in each row, because this machine authors code and does not run it.

| # | state | where it is answered |
|---|---|---|
| F1 | fixed before this lane | `styles.css` §B6.2: `.ctl-util` is `display: flex; flex-wrap: wrap` and `.ctl-util-name` carries no `text-overflow`. The row takes a second line rather than fewer characters. |
| F2 | **fixed by this lane** | The two-line `.drawer .ctl-util` template was already written — and was **inert**, because it declared `grid-template-areas` on a rule whose `display` came from `.ctl-util` and was `flex`. `display: grid` added. |
| F3 | fixed before this lane, backstop added here | `.app.has-inspector .row` drops three columns, and a second stage at `max-width: 1200px` drops the step; `.row .agent .id` ellipses with a `3ch` floor. Added here: `text-overflow: ellipsis` on `.row .agent` itself, which is the property this finding named. |
| F4 | **fixed by this lane** | `.drawer::before` is a sticky, full-width, zero-space band of `--bg` as tall as the drawer's top padding plus its close button; `.drawer-close` stops floating, takes a line of its own and sits on the band at `z-index: 2`. Content scrolls under a header instead of under a floating square. The band is five literals that have to agree — a height, three margins and the drawer's own top padding — and `shell.test.tsx` checks the three relationships between them rather than the numbers, because `spacing.test.tsx` rejects both a custom property declared off `:root` and a `calc(var(…) * -1)`. |
| F5 | **fixed by this lane** | `.source-cells`' track minimum is 260px, derived from the longest route this registry holds, and `.s-path` spans both columns so the `auto` status track stops eating the widening. `.s-path-t` wraps instead of ellipsing. |
| F6 | fixed in seven screens before this lane, four more added here | §B6.3 stacks a table below 900px, opt-in via `.is-stacked` + `data-label`. Added here: `Profiles.tsx`, both tables in `Activity.tsx`, three tables in `AgentDetail.tsx` (including the checkpoint table §2 measures at 57%), and `App.tsx`'s `#reference` table. |
| F7 | fixed before this lane | The narrow rail hides every unopened section's tabs (`.ctl-rail-group:not(.is-on) .ctl-rail-tabs`) and carries a 24px mask fade at its right edge in place of the scrollbar this platform does not paint. |
| F8 | fixed before this lane | See the FIXED paragraph in §F8. |
| F9 | **Lane 1** | Not this lane's. Untouched. |
| F10 | **fixed by this lane** | `.ctl-dock-facts` no longer ellipses; each fact is a `.ctl-dock-fact` with `white-space: nowrap`, so the strip breaks on the ` · ` between facts and the admin-only count and the read age survive at 390. |
| F11 | **Lane 1** | Not this lane's. Untouched. |
| F12 | fixed before this lane | `.row`'s `[step]` track is `minmax(0, 120px)`, sized to the 118px `scan-terraform` needs rather than to the 104px it had; `.wf-step` is no longer a `.tag` and ellipses cleanly. |

### What this disposition does NOT claim

Every row above was established by reading source, not by measuring pixels.
**No number in §1 has been re-measured**, and the fixes for F2, F4, F5, F6 and
F10 have not been seen in a screenshot at any width. The probe in
`overflow/probe.js` is still the instrument that would settle them; re-running
it against this branch is the honest next step, and until somebody does, the
right reading of this table is "the cause named in the finding is no longer in
the source", not "the pixels were checked".

**Five have a structural assertion in `src/__tests__/shell.test.tsx` that fails
if the fix is reverted** — F2, F3's backstop, F4, F5 and F10 — and each one
names, in its own comment, the mutation it catches. Two are worth singling out:

* **F2's reads the parsed CSSOM, not the source and not `getComputedStyle`.**
  A source regex passes with the declaration deleted, because the rule's comment
  now contains the words `display: grid` several times — it explains why the
  declaration has to be there. And `getComputedStyle` cannot answer it either:
  jsdom applies matching rules in source order without weighing specificity, and
  `.ctl-util` is declared ~2,300 lines after `.drawer .ctl-util`, so it reports
  `flex` for an element a browser computes `grid` for. This was measured, not
  reasoned about — the first version of the assertion used `getComputedStyle`
  and CI returned `expected 'flex' to be 'grid'` against a correct stylesheet.
  `CSSStyleRule.style` has no comments in it and no cascade.
* **F10's has a DOM half as well as a stylesheet half.** Taking the ellipsis off
  `.ctl-dock-facts` is inert if `Dock.tsx` renders the facts as one bare text
  run, so the test renders the dock and counts `.ctl-dock-fact` boxes.

**F6 is the one with a gap.** Its stacking is asserted by the existing §B6.3
tests for the screens that already had it; the five tables added here — in
`Profiles.tsx`, `Activity.tsx`, `AgentDetail.tsx` and `App.tsx` — are covered by
that same CSS and are not separately asserted, so a future edit could take
`is-stacked` off one of them without a test noticing. F1, F7, F8 and F12 were
fixed before this lane and are covered by whatever their own lanes left behind;
this lane did not audit that.
