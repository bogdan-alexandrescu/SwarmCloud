# The SwarmCloud console design system

**Status: decided.** This is a specification a developer implements, not a menu.
Where it names a value, that value is in `apps/swarm-ui/src/styles.css` and a
screen may use it; where it names a component, that component exists as a `ctl-`
primitive and a screen may not build a second one.

**What this pass shipped.** The foundation only — the token block and the
primitives, in `styles.css` §B4.4. No screen was restyled. That is the next
phase's job, and the reason this pass exists is that the previous eight attempts
at it each invented their own card, their own bar and their own empty state
because the system did not have one. The audit counted the result: **719
top-level class selectors, 172 of them `ctl-` and 547 in 38 screen-private
namespaces; eight ways to draw a proportion, six metric tiles, five empty
states, four status chips, three tab bars.** `AgentDetail.tsx:380` states the
mechanism in its own comment — *"the `ctl-` block has no card primitive, so the
box is inline"*. A screen invents a primitive when the system does not have one.

**What it does not change.** The palette values, the two line weights, the four
spacing steps and the corner scale were all measured against gates that still
hold them (`test_ui_contrast.py`, `test_state_colour_discriminability.py`,
`typescale.test.ts`, `spaceprobe.ts`). A design pass that re-derives them buys a
different set of numbers and the same screens. Everything below either restates
a decision that is already load-bearing or fills a hole the audit named.

**AMENDED BY §13, THE LAYOUT PASS, WHICH IS STRUCTURAL RATHER THAN
COSMETIC.** §13 decides the page gutter, the casing rule and the box rule, and
it amends §2, §3.1, §3.3, §5.1, §5.2, §6.1, §6.7, §6.8, §6.13 and §6.14 in
place — each of those carries its own note. Read §13 before planning a screen:
the previous pass corrected attributes and its own report recorded the result
(bordered elements 41 → 41), which is why this one changed levels instead of
values.

**AMENDED BY §11 (tokens) AND §12 (the first screens).** §11 moved the token
layer and restyled no screen; §12 is the first section that restyles any, and
it covers the four number-dense ones — Pools, Runtimes, Holders, Accounts.
§12 *applies* §§6.2, 6.4 and 6.6 rather than changing them, and it
adds exactly one rule the system did not have (§12.3). **Read §12.4 with
§11.3**: together they are the complete list of what is still wrong.

**THE NAV WAS RENAMED ON 2026-09-24 AND THIS FILE USES THE NEW NAMES
THROUGHOUT**, including in sections whose measurements predate it. `agents`
became `work` and `pools` became `capacity` — ids and labels together — and the
`Capacity holders` tab became `Holders`. Two sections had been named after
their own first tab, so the rail drew `Agents > Agents` and `Pools > Pools`;
[`redesign.md`](redesign.md#no-section-may-be-named-after-one-of-its-own-tabs)
holds the argument and the rule that came out of it. The *numbers* in every
before/after table below are untouched — only the route that reaches the screen
they were measured on is written in today's spelling, because a route label
exists so a reader can go and look. Where this file says **Pools** it means the
screen (`Capacity.tsx`, the `Pools` tab); where it says **Capacity** without a
qualifier it now means the section that holds five tabs. The dated records in
`docs/audits/` are *not* rewritten — see
[`README.md`](README.md#a-note-on-old-route-names-in-the-evidence).

**AMENDED BY §11, THE RESTRAINT PASS.** The owner's read of the shipped console
was *"the design looks almost cartoonish"*, and the measurement found specific,
countable causes rather than a general one. §11 is the record of what moved and
why, and it amends §1.3, §2, §3.4, §5.3, §6.2, §6.4, §6.5 and §6.6 in place —
each of those sections carries its own note. **Two of the six type steps moved**
(the top two); the rest of the scale is still the owner's. Read §11.3 before
planning the screen phase: it is the list of what this pass did *not* fix.

---

## 0. Four premises in the brief that are wrong

Established by the audit, restated here because planning against them wastes a
lane:

1. **There is no `Trouble.tsx`.** `App.tsx:265` maps `#trouble` → `overview/now`.
   The derived checks live in `Overview.tsx:2015 AttentionBody`.
2. **Charts are not all hand-rolled.** `visx` 4.0.0 ships as four runtime
   dependencies and the library ban (`test_workflow_graph_ui_surface.py:238`)
   covers `d3`, `recharts`, `reactflow`, `cytoscape` and `vis-network` only.
   `charts/README.md` records the owner's 2026-09-22 decision. **The DAG is
   hand-rolled and stays hand-rolled.** `chart.tokenspend.test.tsx:162` confines
   the visx import to `charts/TimeSeries.tsx`; the dial, the track and the
   sparkline in this document are hand-rolled because they are cheaper that way,
   not because a rule forbids the library.
3. **`spacing.test.tsx` is not a real browser.** It renders into jsdom with a
   hand-written CSS resolver and says so at `spaceprobe.ts:12`. It measures
   *declared CSS*. It cannot see overlap, overflow or wrapping — which is why
   the Dock overlap bug was green, and why fixing it (below) is invisible to
   every gate in the repository.
4. **Overview is not the worst offender by rendered words** — `Accounts.tsx`
   (1,766) and `AgentDetail.tsx` (1,565) are each about five times worse. It is
   the worst offender *that matters*, because it is the landing page and its 290
   words are unconditional: they ship on the healthy path.

---

## 1. Palette

### 1.1 The structure, and why light is primary but dark is written first

**Light is the theme this product is designed in.** Every colour decision below
was made for the light theme and the dark counterpart follows it.

**The CSS mechanism is the opposite way round, and stays that way.** `:root`
carries the dark values and `@media (prefers-color-scheme: light)
:root:not([data-theme='dark'])` overrides them. Three test suites parse exactly
that shape to reconstruct the two palettes
(`test_ui_contrast.py:_tokens`, `test_state_colour_discriminability.py`,
`spaceprobe.ts`'s two-theme sweep). Inverting the blocks changes no rendered
pixel and would rewrite three parsers. **Do not invert it.** Design in light;
declare dark first.

**Every colour is named by role.** There is no hue in a class name and no
`--blue-500` anywhere. A component reads `var(--line)` and never branches on
theme.

### 1.2 The roles

| Role | Light | Dark | What it is for |
|---|---|---|---|
| `--bg` | `#f6f8fa` | `#0b0d10` | the page |
| `--surface` | `#ffffff` | `#14181d` | a card, a panel, a tile |
| `--surface-2` | `#f0f3f6` | `#1b2027` | the step below or above a surface: a table head, a card foot, a selected row, a track |
| `--line` | `#698198` | `#5f7087` | **a component boundary** — a panel edge, an input, a button, the rail, a card |
| `--line-soft` | `#a9b7c4` | `#37414e` | **a repeated interior separator** — a table row rule, two stacked utilisation lines |
| `--text` | `#1f2328` | `#e8ecf1` | a value, a heading, the thing you came to read |
| `--text-dim` | `#59636e` | `#93a0b0` | a label, a unit, a connective word |
| `--text-faint` | `#606a77` | `#828d9b` | provenance, ages, the small print that says how much to trust the big number |
| `--ok` | `#1a7f37` | `#58c668` | healthy, succeeded, under the ceiling |
| `--warn` | `#6e4a00` | `#ffd60a` | approaching a limit, a read that failed, a partial total |
| `--bad` | `#681117` | `#f85149` | failed, over a ceiling, act on this |
| `--info` | `#0969da` | `#58a6ff` | **a fact or a link, never a verdict** |
| `--paused` | `#7139db` | `#b288f8` | parked, held by an operator — which is not "full" |
| `--*-ink` | see §1.4 | see §1.4 | the accent as *text on a tint of itself* |
| `--series-1..5` | one value, both themes | | a chart line's identity — never a severity |

Three surfaces and no fourth. Two line weights and no third. Three text tones
and no fourth. Five state hues and no sixth.

### 1.3 There is one accent, and selection has no hue

`--info` is the accent. It is the link colour, the focus ring and the mark for a
fact that is not a verdict. It is **not** a severity and is deliberately outside
the ok/warn/bad triad in `test_state_colour_discriminability.py`.

`styles.css:2971` already records that `--info` carries seven jobs, and fixed it
for exactly one of them. **This spec extends that fix to all of them: a
selected, active or current thing is marked by a surface step plus a 2px rule in
`--text`, never by the accent.** A selected tab drawn in the same blue as a live
agent is how a reader learns to stop trusting colour as a state channel.
`.ctl-nav-link.is-on` and `.ctl-seg > button[aria-pressed]` both do this.

The restraint budget, stated so it can be enforced: **one accent, three text
tones, two line weights, two elevations, one motion duration, four corner
values.** Hetzner runs a 1,758-class console on one accent; that is what makes a
dense screen still parse.

**§11 added a count to the budget: the accent is spent once or twice per screen,
not twenty-three times.** Counted on the live Overview, `--info` was painted 44
times — 23 as text, 16 as chip borders, 5 as backgrounds — plus the whole of
both dial rings. Against the reference set that is the single busiest thing
about the screen: **Railway's docs page paints zero saturated text colours**
(193 text elements in one muted grey, 18 in ink, and exactly one saturated
background on the page — the primary button). **Geist's own site paints zero
saturated text** and declares three text tones and no fourth. Northflank spends
two hues on a whole page. Every one of the five spends its accent once or twice,
on the primary action.

**So a link is ink plus an underline, and the accent is what happens when you
point at it** — `.ctl-link`. This is a *stronger* affordance than the blue was,
not a weaker one: WCAG 1.4.1 says colour may not be the only channel, and a
colour that is also the focus ring, the live-agent dot and a chart fill was
already too overloaded to read as "clickable" on its own.

`.ctl-link` ships as the primitive; **it is not yet universal and this document
does not claim it is.** `.ov-link`, `.wb-more a`, `.tile.blocked .t-sub a`,
`.node-links a` and `.art-md a` are five screen-private link treatments that each
paint `--info`, and folding them in means editing five screens — the screen
phase's job. What §11 shipped is the primitive they collapse into, so the screen
lanes cannot each invent a sixth answer.

### 1.4 The ink rule, and the trap under it

Text on a tint of its own accent spends the margin the accent had against the
plain surface. Measured, at the tint strengths this sheet uses:

| Pair | Accent as text | **Ink** as text |
|---|---|---|
| `--ok` on 18% `--ok` | 5.33 dark / **3.60** light | `--ok-ink` **4.84 / 5.01** |
| `--warn` on 10% `--warn` | 9.20 / 6.13 | `--warn-ink` 5.14 / 5.03 |
| `--bad` on 18% `--bad` | **3.93** / 7.89 | `--bad-ink` **5.02 / 4.69** |
| `--info` on 22% `--info` | **4.39** / **3.45** | `--info-ink` **5.01 / 5.05** |
| `--paused` on 20% `--paused` | **4.31** / **4.23** | `--paused-ink` **4.73 / 4.87** |

Four of the five fail AA as the accent. **Text on a tint of itself uses the
`-ink` variant. Always.**

**The trap.** Write the tint out in full at the point of use:

```css
background: color-mix(in srgb, var(--ok) 18%, transparent);
color: var(--ok-ink);
```

`test_ui_contrast.py` resolves *that exact form* and measures the text on it. A
token `--tint-ok` holding the same mix resolves to nothing, the rule lands in
the unresolvable pile, and the run fails as undeclared. **There are no tint
tokens and there will not be any.**

### 1.5 Unresolvable surfaces carry no text

A gradient, a hatch or a shimmer has no luminance the gate can measure.
`var(--ctl-hatch)` is in that test's allowlist and may carry text; a
`linear-gradient` is not and may not. So `.ctl-pending` and `.ctl-ghost` set a
background and no `color`, and the caption goes in a sibling — which is also
where a screen reader wants it.

### 1.6 The series palette

Five identities, one value each, both themes:

| | Hex | Worst contrast across all six surface/theme combinations |
|---|---|---|
| `--series-1` | `#3f86d8` blue | 3.36 |
| `--series-2` | `#1f948a` teal | 3.33 |
| `--series-3` | `#8a6ad6` violet | 3.69 |
| `--series-4` | `#9c7620` amber | 3.75 |
| `--series-5` | `#788492` neutral | 3.42 |

Every one clears 3:1 against `--bg`, `--surface` and `--surface-2` in both
themes, so a series never needs a second definition. Railway spends one of five
slots on a neutral grey to stop a chart reading as a fruit salad; `--series-5`
is that slot.

**Series colour is identity, not rotation.** A metric keeps its colour across
every chart in the product: spend is always one colour, latency always another.

**What this does not buy, stated rather than hoped:** the band those five sit in
is 1.36:1 from end to end, so **they are not separable in greyscale**. Hue alone
does not identify a series to a colour-blind reader. A multi-series chart
therefore carries a tabular legend naming every series — swatch, name, value,
right-aligned and `tabular-nums` — and never relies on the line's colour to say
which line it is. A single-series chart uses `--series-1` and needs no legend,
because the panel title already said what it is.

**Never a state colour in a chart.** A line drawn in `--ok` is read as a
verdict.

---

## 2. Type

**Six steps. The top two were retuned by the restraint pass (§11); the four
below them are the owner's and are unchanged.**

| Token | Size / leading | What it is |
|---|---|---|
| `--t-micro` | 12 / 1.45 | ages, raw ids, provenance, card feet. The hard floor. |
| `--t-meta` | 13 / 1.45 | column heads, eyebrows, labels — **a treatment as much as a size**: 600, mono, `--text-faint`. *No longer uppercase and no longer tracked: see §13.2. No longer the chip: see §6.6.* |
| `--t-body` | 14 / 1.50 | the workhorse: table cells, values, controls, state words, `body` itself |
| `--t-lead` | 16 / 1.55 | a card title; **the one sentence a screen is allowed** |
| `--t-title` | **18** / 1.30 | the screen `<h1>`, at weight **600** |
| `--t-figure` | **22** / 1.10 | the one number a card exists for |

**Why the top two moved, and the count cap that matters more than either.**
`--t-figure` was 30px — 2.14× body — and the Overview painted **eight** of them
above the fold. Measured against the five consoles this document takes as its
reference, that ratio has no precedent: Koyeb's entire shipped design system
tops out at 24/14 = 1.71:1 and their Overview does not spend it on a number at
all; Vercel — the only one of the five that puts a figure on a tile — draws it
at roughly 1.1–1.25× its own label; Railway's project dashboard has no figure
on it. 22/14 = 1.57:1 sits inside that band.

`--t-title` moved 20 → 18 for a structural reason rather than a visual one: six
steps have to stay six **distinct** steps, and 12/13/14/16/20/20 is five steps
wearing six names. The h1 also drops from weight 650 to 600, because this
section already said the ladder stops at 600 and the h1 was the one rule in the
sheet that ignored it.

**The cap is the real rule: one `--t-figure` per card, and one card per screen
carries the screen's figure.** A smaller step used eight times still reads as a
KPI wall. No reference screen shows its largest size more than once.
`apps/swarm-ui/src/__tests__/typescale.test.ts` states the six values and was
re-pointed, not worked around.

`--lh-flush: 1` is not a seventh step; it is legal only inside a `font:`
shorthand next to a size token, where a fixed box must not grow, and every use
names the box it is protecting.

**Hierarchy is carried by colour first, weight second, size last.** This is the
single change that buys most of "sleek" without touching the scale. On a Railway
page the muted ink outnumbers the heading ink 386 to 30; Vercel's own table
header is 14px at weight **400**, lighter than its own cells. Apply the same:

- a column head is `--text-faint`, its cells are `--text`, at the same size;
- a label is `--text-dim`, its value is `--text`, at the same size;
- weight moves 400 → 500 → 600 and stops. There is no 700 in this console.

**Mono is a semantic, not a decoration.** `var(--mono)` means *this string is an
identifier you may need to copy, or it is chrome rather than data*: ids, hashes,
timestamps, ages, units, axis ticks, column heads, eyebrows, fact keys. Sans at
full strength means *this is the datum*. Once a reader has learned that split —
which takes about two seconds — the grey mono layer can be read or skipped at
will, and **that is what buys a screen the right to have no paragraphs on it.**

`font-variant-numeric: tabular-nums` on every number that can change. Not
optional in a column of figures.

---

## 3. Spacing and page rhythm

### 3.1 The scale survives

`--ctl-s1` 4 · `--ctl-s2` 8 · `--ctl-s3` 12 · `--ctl-s5` 28. **Four steps.**
`--ctl-s4` was retired because 18px is 1.5× 12px and nobody can tell those apart
in a column of figures; `shell.test.tsx:377` holds it gone and holds the other
four at their values. `--ctl-s5` keeps its name rather than becoming s4, because
renaming it would silently change the meaning of every existing use.

| Step | Job |
|---|---|
| `--ctl-s1` 4px | inside a row: a chip's icon gap, a stacked pair |
| `--ctl-s2` 8px | inside a panel: between a label and its value, between tiles |
| `--ctl-s3` 12px | between regions: card grid gutter, toolbar to content |
| `--ctl-s5` 28px | the one large break: between top-level sections, between two cards in a grid, and around a region (§13.3) |

### 3.2 Chrome padding is not a rhythm step

`--ctl-pad-chrome: 16px`. **Compact data inside generous chrome** — D2, and the
resolution of "the console is very hard to read" against "make it dense". The
data is compact (`--row-h: 30px`, 13–14px type); the frame around it is not.
`--ctl-pad-chrome` is the only value in the sheet allowed to move *up* when the
row height moves down. **Nothing in a data region may use it**, and
`shell.test.tsx` asserts it is ≥ `--ctl-s3` and that `.ctl-empty`'s padding
resolves to it.

It does not rescale by breakpoint. 16px is Railway's mobile card padding and
Northflank's at both 1440 and 390; the gutter is the thing that shrinks, not the
card's interior.

### 3.3 The page

| Token | Value | |
|---|---|---|
| `--app-max` | 1880px | about the widest row that stays scannable at 14px; reached only above ~2000px |
| `--ctl-gutter` | 16 → 32 (≥1280) → 40 (≥1600) | **the only distance between two top-level regions** (§13.1): the page's side padding AND the rail-to-content gap |
| `--app-pad` | `var(--ctl-gutter)` | kept as a name because Brand.tsx lines its wordmark up through it; no longer a second ladder |
| `--rail-w` | 200px → 152px (≤1279) | |
| `--measure` | 74ch | **applied to running text only**, never to a table, a grid or a chart |

`--app-max` is deliberately enormous rather than absent: on an ultrawide, a 3000px
table row is one the eye cannot track from the name to the figure, which is a
different way of being unreadable.

### 3.4 The bottom gutter, and the Dock bug

`.app`'s bottom padding now reads `calc(var(--dock-h, 28px) + 48px)`.

It read the literal `28px + 48px`. `Dock.tsx:80` publishes `--dock-h` on
`documentElement` and two rules already consumed it — the rail's max-height and
the inspector's — while the page reserved only the collapsed bar. The dock is
resizable from 120px to 70vh **and remembers its height in `localStorage` across
sessions**, so an operator who once dragged it tall has the foot of every screen
permanently hidden from then on, on every visit, with nothing to indicate it.

The old comment called the overlay deliberate. It is deliberate at 28px and a
bug at 70vh: reserving the collapsed bar is a decision about a panel that opens
and closes, not one that opens and stays. No gate could see it (§0.3).

**AMENDED BY §11, AND THE AMENDMENT IS THAT THIS IS STILL NOT FIXED.** The
bottom padding reserves the dock's real height at the **end of the document**,
which works: scrolled to the foot at 390×844 there is clearance and nothing sits
under the bar. It does nothing at any **other** scroll position, because the
dock is an opaque `--surface` bar at `z-index: 45` and therefore paints over
whatever happens to rest at the viewport's bottom edge. Measured at
`scrollY = 0`, the Overview had 2 occluded elements at 390px, 18 at 1024px and
24 at 1280px.

§11 added `scroll-padding-bottom: calc(var(--dock-h, 28px) + var(--ctl-s3))` on
`html`, which is the part CSS can fix: an anchor jump, a `scrollIntoView` and
keyboard tabbing now stop with the dock's height still clear instead of parking
the element they just focused underneath it. **That is not the whole fix and
this document does not pretend it is.** A fixed bar over a scrolling document
always has content behind it at some offsets; the only complete answer is for
the dock to be a row of the frame's grid rather than an overlay.

**AMENDED AGAIN BY §12, WHICH DID THAT — and both reservations above are now
DELETED.** `.app`'s bottom padding is a plain `48px` and the `html`
`scroll-padding-bottom` is gone; the dock is row 2 of `.ctl-frame`. Neither
reservation was kept "just in case", because a reservation for a hazard that no
longer exists is how the next reader concludes the hazard still does — and with
the dock in flow they would have been dead space at the foot of every screen.
`--dock-h` survives, still measured from the real box, because `.ctl-rail` and
the inspector size sticky columns against the scrollport. See §12.

---

## 4. Corners

`0 · 2 · 6 · 10 · 14 · 999`, and `spaceprobe.ts:467` fails on anything else.

| Value | Token | What takes it |
|---|---|---|
| 0 | — | a square-ended axis, a segment inside a group, a joined edge |
| 2px | `--track-radius` | the track, and anything the width of a track |
| 6px | `--ctl-radius-sm` | chips, marks, cells, controls, rows, wells |
| 10px | `--radius` | panels and cards |
| 14px | `--ctl-radius-lg` | the large containers only — a drawer, a slide-over |
| 999px | `--ctl-radius-pill` | a pill or a dot, which is a shape rather than a radius |

**A mark is not a surface.** The probe exempts anything declared 12px or smaller
on both axes, because a 1px corner on a 6px diamond is part of the shape. That
exemption is for glyphs and is not a way to give a small box an off-scale corner.

Two off-scale corners were on the scale's own list of violations and are fixed
in this pass: `.wf-body` (8px → `--ctl-radius-sm`) and `.logwin-body` (8px →
`--ctl-radius-sm`).

---

## 5. Lines, fills and elevation

### 5.1 What carries separation, and what carries grouping

**Grouping is a surface step. Separation is a hairline. Prefer the step.**

**§13.3 turned that preference into a rule with three levels and named which
one may draw a border at all: a REGION separates with space plus one hairline,
a PANEL is the one box, and a ROW draws nothing.** The rail was the boundary
where this section's own advice was least applied, and it is now space (§13.1).


Northflank's dense tables have no row borders at all: a 40px bar on the surface
below, a 6px gutter, and a fill step measuring 1.06:1 that still reads perfectly
because it is *area* rather than *line*. Railway's light theme goes further —
their panel fill and their page differ by 1.00:1, and **all** separation is one
1px hairline, one value, everywhere.

We can take the structure but not their hairline value: Railway's `#d9d8e2`
measures 1.24:1 and fails both our floors. Ours:

| | Light | Dark | Floor |
|---|---|---|---|
| `--line` on the three surfaces | 4.04 / 3.80 / 3.63 | 3.85 / 3.53 / 3.24 | **3:1** (WCAG 1.4.11, component boundary) |
| `--line-soft` | 2.05 / 1.92 / 1.84 | 1.88 / 1.72 / 1.58 | **1.5:1** (separator) |

### 5.2 The rule that is easy to get wrong

`spaceprobe.ts:1156` decides separator-versus-boundary **structurally**, and its
test is narrow: *exactly one border drawn*, and an adjacent sibling with the same
signature (or a `<td>`/`<th>` bottom rule).

- `.ctl-util + .ctl-util` qualifies. A table row rule qualifies.
- **A card's header rule does not.** It has no twin, so it is graded as a
  boundary at 3:1, and `--line-soft` fails at 1.58–2.05.

**The right answer is almost never to promote it to `--line`. It is to draw no
rule.** `.ctl-card-head` separates by 16px of padding; `.ctl-card-foot`
separates by `--surface-2`. Both give the probe nothing to measure and read
airier than the rule would have. `.is-ruled` variants exist for the one case
that needs an edge — a chart whose plot has to start somewhere — at `--line`.

### 5.3 Two elevations, and almost nothing takes the first one

**Amended by the restraint pass (§11). `--ctl-shadow` came off `.ctl-card` and
off `.ctl-metric`.**

| Token | Value (light / dark) | What gets it |
|---|---|---|
| `--ctl-shadow` | `0 1px 2px rgb(31 35 40 / .08)` / `0 1px 2px rgb(0 0 0 / .30)` | **the DAG node, and nothing else by default.** A node has to read as sitting *on* a canvas the edges pass *under* — a genuine z-relationship. A card on a page does not. |
| `--ctl-shadow-pop` | `0 8px 24px rgb(31 35 40 / .12)` / `0 8px 24px rgb(0 0 0 / .28)` | a surface **over** the page: the help card, a menu, a drawer edge |

The count is the argument. The shipped Overview carried **16 shadowed elements
on one screen**. Railway's docs page: 1. Northflank's: 1. Hetzner's: 2. Koyeb
declares exactly one shadow in their whole design system — Tailwind's smallest,
on `.card` only, and their toast sets `box-shadow: none` explicitly. Vercel's 7
is a component gallery whose content *is* boxes, so it is the set's upper bound
rather than its norm. Railway's console cards and Northflank's panels carry no
shadow at all: **a step in the fill plus one 1px hairline is their whole
separation mechanism, and it is now ours.**

**A card inside a card draws no second box.** `.ctl-card .ctl-card` drops its
border, its radius and its background. The audit counted cards nested two and
three deep on Overview; none of the five references nests a bordered, rounded
surface inside another one. Written on the primitive, because every screen that
met this problem before solved it by inventing a fourth card.

Everything else separates with a hairline or a step. Focus and selection are a
ring, not a blur: `outline: 2px solid var(--info)` with an offset, which is the
only place the accent is allowed to mark a control state.

### 5.4 Motion

`--ctl-dur: .15s`, for every state change in the frame. Two exceptions, and they
are the only two: the track fill at `.3s` (a proportion that snaps reads as a
redraw rather than a change) and the live pulse at `2s`.
`prefers-reduced-motion` stops the pulse and the pending sweep; both carry a
second, static signal so neither is distinguishable by motion alone.

---

## 6. Component contracts

Every one exists in `styles.css`. A screen that needs a variant adds an `is-`
modifier **to the primitive**; a screen that builds its own is a regression.

### 6.1 Card — `.ctl-cards` / `.ctl-card`

```html
<section class="ctl-card">
  <div class="ctl-card-head">
    <h2 class="ctl-card-title">Headroom</h2>
    <span class="ctl-card-note">4 of 7 pools</span>
  </div>
  <div class="ctl-card-body"> … </div>
  <p class="ctl-card-foot">read 2m ago · 3 pools did not answer</p>
</section>
```

`.ctl-cards` is `repeat(auto-fit, minmax(min(100%, 340px), 1fr))` with a
`--ctl-s3` gap — 4-up on a wide display, 1-up at 390px, **no media query at any
width**. The `min(100%, 340px)` is what stops a 340px track overflowing a 358px
content column on a phone.

**`.ctl-card-note` is the answer to the prose problem, so it has a job, not a
slot.** It is the qualifier that would otherwise have been a paragraph under the
figure, right-aligned, muted, mono, and it does not wrap: `4 of 7 pools`,
`ceiling 8 vCPU`, `excludes parked`, `floor, not a total`. Railway puts
`Max 8 vCPU` here and writes nothing under the chart. A qualifier that wraps
under the title has become a subtitle, which is a paragraph with better manners.

`.ctl-card-body.is-flush` drops the padding for a body that holds a table or a
chart, which draws its own edges.

`.ctl-card-foot` is the provenance strip — the Koyeb footer, whose contents here
are *when this was read, from how many sources, and how many did not answer*. A
card whose figures all came from one read says so once here, instead of once per
figure.

### 6.2 Metric tile — `.ctl-metric` *(amended by §11: the tile lost its box)*

One fact, its unit, and what it does not include. Label (`--t-meta`, mono,
uppercase, `--text-faint`) → value (`--t-figure`) → `sub` → `foot`. **One
`--t-figure` per tile; a second figure is a second tile.**

**A BOX ON A TILE NOW MEANS SOMETHING IS WRONG WITH THE NUMBER.** The default
tile draws no border, no shadow and no radius of its own: a surface step on the
page and `--ctl-s2` of air between tiles, which is how Northflank separates the
rows inside a panel (no row rules at all) and how Railway separates its project
cards. Measured, the shipped strip was five 226×113px boxes each carrying their
own border, radius and drop shadow, to deliver five facts — about 570px of
width and 113px of height for what Northflank's equivalent panel buys in
fifteen.

The border is **declared and transparent** rather than absent, for two
load-bearing reasons: the absence states below carry their meaning *in* the
border, and §14 of the sheet says a thing that is absent occupies the space it
would have occupied — a border that appeared only on failure would move every
tile beside it at the moment the strip most needs to hold still. The rule also
becomes legible as a rule: **nothing healthy is boxed.**

The `foot`'s `border-top` is gone too. It was `--line` — the *component
boundary* weight — repeated five times across one strip, which is the inverted
ratio the audit found (78 paints of `--line` against 18 of `--line-soft` on one
screen). §5.2 already gave the answer: a rule with no twin is not promoted, it
is not drawn.

States: `.is-absent` (dashed border in `--ctl-absent`, value drops to `--t-body`
and becomes a phrase — nothing but a measured number gets the figure step),
`.is-unread` (dashed + `--warn`), `.is-good` / `.is-alert` (a corner mark in the
shape vocabulary, so tone is the second signal and not the only one;
`.is-alert` additionally paints the declared border solid in `--bad`).

**The `sub` and `foot` lines are where Overview's 100 words of tile prose come
from and the next phase deletes most of them.** A tile's `sub` may be a
qualifier, never a definition: `of 5 reads` is a qualifier; `LEASED,
DISPATCHED, STARTING, RUNNING — the states that reserve capacity` is a
definition and belongs in the `?`.

### 6.3 Figure — `.ctl-figure`

The tier the panels never had. `--t-figure` is used on exactly two selectors
today, both in the metric strip; underneath it, the panels holding the actual
operational data have no figure level at all. The audit found a capacity row
reading `3 / 8` at 14px directly below a 30px "Running now 4" that is the same
fact. One figure tier for the summary and none for the data is most of the
"there is no hierarchy" complaint.

```html
<b class="ctl-figure">4.2<span class="ctl-figure-unit">GB</span></b>
```

The unit is demoted **inside** the figure rather than hoisted into a header,
which deletes a column heading per card. `.ctl-figure.is-absent` drops to
`--t-body` and `--ctl-absent`: at 30px, `not reported` reads as a quantity.

### 6.4 Proportion — `.ctl-track` / `.ctl-util` *(geometry unchanged; fill amended by §11)*

One height (`--track-h` 8px), one radius (`--track-radius` 2px), one colour
rule, one axis. **Every proportion in the product is this** — including
`.ctl-dial`, as of §11; the audit found eight and this is the consolidation that
already happened once and must not grow back.

**THE GEOMETRY IS DELIBERATELY NOT TOUCHED.** 8px stays 8px. `.ctl-track.wf-meter`
on Workflows is the same 8px track, and Workflows is the screen the owner named
as already right; shrinking the token to 2px would have "fixed" the one screen
that did not need fixing.

**A UTILISATION BAR THAT IS FINE IS GREY. A HUE ON ONE IS A VERDICT.** The
capacity rows shipped as a solid saturated block across 85 of 88px — the owner's
*"chunky solid blocks, closer to a game health bar than to a utilisation
meter"*. The height was only half of it: the other half was that a bar with
nothing to report was painted in the accent, which §1.3 reserves for "a fact or
a link, never a verdict". When every bar is coloured, none of them is saying
anything by being coloured. So `.ctl-util-fill` defaults to `--text-dim`, and
`.is-warn` / `.is-bad` / `.is-paused` keep their hue **and** their texture
untouched — they are the one place on a proportion where colour earns its keep,
and `test_state_colour_discriminability.py` holds the textures apart in
greyscale.

> **Answered by the owner, 2026-09-24: `.wf-meter` goes grey.** The question
> held open here was this: `.wf-meter`'s fill carries
> `ctl-util-fill wf-meter-fill`, so the monochrome default reached straight
> into the frozen Workflows screen and turned its 54px meter grey, and one line
> (`.ctl-util-fill.wf-meter-fill { background: var(--info) }`) exempted it
> back to blue. That left the utilisation fills with one blue member and the
> rest grey, and resolving it meant changing a screen the owner had frozen.
>
> **The decision: the exemption is removed and the meter takes the monochrome
> default (`--text-dim`) like every other `.ctl-util-fill`.** `.is-warn` /
> `.is-bad` / `.is-paused` keep their hue and their texture. **Why:** one rule,
> not a rule and an exception; and **colour on a bar is a verdict.** A
> workflow's progress ("three of five steps done") is a fact, not a verdict, so
> painting it blue spent a hue on a bar for being a bar, which is exactly what
> this section exists to stop. Workflows therefore no longer renders
> pixel-identically to the version the owner froze; that is the accepted cost
> of the decision.
>
> **How it is held, corrected 2026-09-24.** The first guard for this,
> `test_state_colour_discriminability.py::
> test_a_proportion_fill_takes_a_hue_only_from_a_verdict`, went red against the
> exemption before the exemption was removed. But this paragraph first said it
> failed on "a second exemption under a new name", and that was false. It read
> `styles.css` only, and only rules whose subject literally named
> `.ctl-util-fill`. Mutation commit `232921d` painted the meter blue five ways
> and left it green (CI run 35977623224): through the fill's other class
> (`.wf-meter .wf-meter-fill`), later in the sheet at equal specificity
> (`.wf-meter-fill`), through the parent (`.wf-meter > span`), through
> `:not(.is-warn)`, and in the CSS Overview injects (`OVERVIEW_CSS`).
>
> The guard that replaced it asks what paints each fill the product actually
> renders. It finds every element in the `.tsx` whose className names
> `ctl-util-fill`, and tries each class that element can carry: its literals,
> both arms of its conditionals, the traced values of an identifier it
> interpolates, and any class a sheet names beside `.ctl-util-fill`. It reads
> the element's parent from the same JSX. It then matches every `background`
> rule in `styles.css` and in every sheet a screen injects, at every
> breakpoint, with real selector semantics, and fails on any rule that could
> paint an un-verdicted fill something other than grey unless a grey rule that
> certainly applies outranks it. It also fails on an inline `style`
> background. A `<style>` it cannot read, an identifier it cannot trace, or a
> spread on a fill fails a companion test instead of passing unseen.
>
> **What it still does not see:** a class that reaches a fill through a helper
> function or a prop set in another file; a fill whose className does not
> literally name `ctl-util-fill`; CSS injected by anything other than a
> `<style>` in a `.tsx`. It reads source text and does not render, so it says
> what the sheets would paint, not what a browser painted.
>
> **One named exception, and it is a grey.** Overview draws a *projected*
> utilisation reading (real, but its window reset or its poll is stale) with
> `.ctl-util-fill.ov-projected { background: var(--ctl-absent) }`. That rule
> shipped before this decision and the first guard never saw it. It is now
> listed by name in the test's `DOCUMENTED_GREYS`, and
> `test_a_documented_grey_is_a_grey` resolves `--ctl-absent` through
> `--text-faint` to a text grey. So "grey like every other proportion" holds
> for it, in a second grey that means "not current".
>
> **What this does not cover, found while answering it.** The decision was
> framed as "grey like every other proportion", and three bar fills outside the
> `.ctl-util-fill` family were still `--info` at `b0fff1b`: `.sr-bar > i`
> (the per-state split in `PlatformCounts.tsx` and the runner-profile split in
> `Activity.tsx`), `.coverage > i` (the usage-coverage strip in
> `Activity.tsx`), and `.ctl-track > i`, which the comment beside the default
> rule keeps `--info` on purpose as "the meter beside a number" (no screen
> currently renders a bare `<i>` inside `.ctl-track`). They share the
> `background: var(--info)` rule in `styles.css`. Whether the same "colour on a
> bar is a verdict" rule extends to them is a question for the owner and was
> not decided here. One consequence of the corrected guard: if a screen ever
> renders a `.ctl-util-fill` as an `<i>` inside `.ctl-track`, then
> `.ctl-track > i` (0,1,1) outranks the grey default (0,1,0) and paints it
> blue. The guard fails on that, because it reads the parent.


Four track states, and they must not converge:

| | Drawing |
|---|---|
| measured | fill, plus a 3px **axis** down the left edge, always drawn |
| `.is-zero` | no fill, the axis remains, plus a 1px inset hairline — *the scale starts here and the value is at the start of it* |
| `.is-unknown` | hatched, **no fill and no axis** — there is no scale to start |
| `.is-over` | the overflow segment hatched in `--ctl-hatch-bad` rather than clipped |

Fills carry a **texture** as well as a hue, because an 8px bar has no room for a
glyph: flat (under), 45° stripes (`--warn`, approaching), vertical ticks
(`--bad`, at it), wide back-diagonals (`--paused`, held).

### 6.5 The card's own proportion — `.ctl-dial` *(no longer a dial; §11)*

The proportion that *is* the card, rather than one row of a list: headroom,
quota, coverage.

**IT IS NOT A RING ANY MORE, AND THE MEASUREMENT IS UNAMBIGUOUS.** Not one of
Railway, Northflank, Koyeb, Vercel or Hetzner draws a ring anywhere. Across
every console screenshot read for this pass there are exactly three encodings
for a proportion: a line or area chart, a track behind or beside the number, or
no graphic at all. What shipped was a 96px conic-gradient ring with a hard-coded
10–11px band in saturated `--info`, two of them above the fold on the landing
page. The owner's verdict named it: *"the dials are infographic"*.

**It also could not hold its own label, and that was a defect rather than a
taste question.** `37 % left` ran 78px inside a 74px inner circle — measured
identically at 390, 414, 768, 1024, 1280, 1440 and 1920, so it was never a
responsive bug. Three numbers had to agree and none referenced the other two:
`--dial-size` set inline by a screen, an 11px ring literal in `styles.css`, and
`--t-figure` in the token block. `spacing.test.tsx` could not see it — jsdom has
no layout engine — which is why it shipped.

```html
<div class="ctl-dial is-partial" style="--pct:57; --measured:40"
     role="img" aria-label="40% of the 4 pools that reported. 3 pools did not answer, so this is not a platform total.">
  <b class="ctl-dial-figure ctl-figure">40<span class="ctl-figure-unit">%</span></b>
</div>
```

**What replaces it is the product's own proportion**, at the geometry
`.ctl-track` already uses everywhere else: the figure, and a `--track-h` track
beneath it. Same height, same radius, same axis, same hatch, same four states.
One proportion primitive in the product instead of two — the consolidation §6.4
already asked for, to which the dial was the last exception.

**The overflow fix is structural, not a tuned number.** The box is `max-content`
around its label with `nowrap` on the figure, floored at `--dial-size`: the
label defines the box instead of the box clipping the label, so the failure
cannot recur at a width nobody measured.

**The total is always drawn**, and the four states are unchanged in meaning —
only their geometry is linear instead of angular:

| State | Drawing |
|---|---|
| default | `--pct` filled in `--text-dim`, the rest `--surface-2`, the axis drawn at the origin |
| `.is-partial` | filled to `--measured`, **hatched** from there — the hole in the total drawn as a hole |
| `.is-unknown` | hatched, **no fill and no axis**. An empty track reads as 0%, which is a claim nobody made |
| `.is-zero` | empty, axis drawn, plus the inset hairline `.ctl-util-track.is-zero` uses — the same mark, meaning the same thing |

The `aria-label` route to the sentence is the caller's and is unchanged.

**The name stays `.ctl-dial` in this pass**, and that is a scoping decision, not
an oversight: renaming it means editing every screen that calls it, and §11 was
the token layer only. **The screen phase renames it** — `.ctl-gauge` is the
obvious candidate — in the same change that stops the screens calling it a dial.

### 6.6 Status chip — `.ctl-chip` *(rebuilt by §11)* and the bare dot — `.ctl-dot`

**A MARK AND A WORD. IT IS NOT A BADGE.**

Measured, what shipped was 102×23px to say "running": a 1px border in
`currentColor`, a 999px radius, 600-weight 13px mono, `.04em` tracking,
UPPERCASE, and a dot. Four of them stacked in one card is 408px of chrome spent
saying one word four times, and twelve pill radii on the Overview is as much of
the owner's *"almost cartoonish"* as the numerals are.

**Vercel is the precise answer, and it was measured in a browser rather than
guessed at**, because Geist ships `StatusDot` as a documented component: a
**10×10px solid circle, `border-width: 0`**, no ring and no tint, and beside it
the state word in sentence case at **14px/400 in primary ink** — even "Error" is
drawn in `#171717`. The hue never touches the word. Northflank agrees (a small
glyph, then `Job succeeded` in plain sentence case, no border, no fill). So does
Koyeb, whose one pill-shaped element is *metadata* in a grey hairline outline —
which is how they keep a pill from meaning "status". So does Railway (a 6px
legend dot and the row's own ink).

So the chip is now:

| | Shipped before | Now |
|---|---|---|
| border | 1px `currentColor` | none |
| radius | 999px | none |
| face | mono, 600, 13px | **sans, 500, `--t-body`** |
| case | UPPERCASE + `.04em` | `lowercase`, no tracking |
| the word | in the state hue | **`--text`, full ink** |
| the mark | 7px disc in `currentColor` | **10px, hued by `--chip-tone`** |

`lowercase` rather than sentence case because the states arrive from the API
already shouting (`RUNNING`, `REAUTH_REQUIRED`), and lowercase is what
`.wf-state` already renders on Workflows — the one screen the owner named as
already correct. Matching it is the point. The face is sans because §2 says mono
means *"an identifier or chrome"* and sans at full strength means *"this is the
datum"*: a state word is a **name** (§8.5, kind 1), not an id.

**This strengthens the honesty invariant rather than spending it**, and that is
worth stating plainly because the change looks like a removal. Before: colour on
the word, colour on the border, a shape on the dot. After: **the word at full
ink**, legible and no longer competing with the datum beside it, and the shape
vocabulary untouched. Nothing that carried information was removed — what went
was a duplicate of the tone channel (the border) and three typographic
amplifiers (case, weight, tracking).

**The tone moved off `color` and onto `--chip-tone`**, which is what lets the
word be `--text` while the mark stays hued. Every read of it carries a fallback,
for the reason `.ctl-dial` gives at length: `spaceprobe.ts` resolves the sheet
against `:root` alone and throws on a `var()` it cannot find there.

**The word is mandatory; the mark repeats it as a shape.** Seven silhouettes,
and it is the same vocabulary wherever a state is drawn —
`test_every_chip_state_has_its_own_silhouette` holds them apart and was not
touched:

| State | Mark | Meaning |
|---|---|---|
| ok | filled disc | present, and fine |
| warn | triangle, apex up | the universal caution shape |
| bad | diamond | a disc knocked off its axis — the one mark with corners |
| info | flat bar | a fact, not a verdict |
| paused | two bars | the pause glyph |
| unknown | hollow ring | an absence of information, drawn as one |
| underived | ring with a bar through it | the state exists; nobody computed it |
| live | disc with a halo | a dot that is broadcasting |

`.ctl-dot` is that mark **without** the chip, for a table cell, a DAG node or a
dense row — which is why there were four state chips: there was no way to get
the mark alone.

**`.ctl-dot`'s default is unknown.** A bare `<i class="ctl-dot">` is a hollow
ring in `--text-faint`; `is-ok` and the rest are opt-in modifiers. Northflank's
status dot does exactly this, and the consequence is the point: **a state nobody
derived renders as a present, legible, correctly-sized dot in a tone that is
deliberately outside the ok/warn/bad triad.** A dot that is not drawn is a dot
nobody can ask about.

`.ctl-chip.is-live` is the only motion on a data screen, and it carries a halo
so that `prefers-reduced-motion` cannot make it identical to `is-ok`.

**One duplicate survives this pass and it is a screen's, not the primitive's.**
`Overview.tsx:1452` renders `{stateGlyph(task.state)} {task.state}` *inside* the
chip, next to the `<i>` — a second shape encoding of the same fact, and unlike
`Agents.tsx:350` and `AgentDetail.tsx:481` it is **not** `aria-hidden`, so a
screen reader announces a bare `●` before the word. The pill was hiding it;
without the pill it is plainly two dots. Deleting that one expression is the
screen phase's first job and is the last piece of the owner's *"decorative
double dot"*.

### 6.7 Data table — `.ctl-table` *(row rules removed by §13.3)*

Scrolls sideways rather than reflowing into cards: these are numbers that only
mean anything beside each other in a row.

Rows `--row-h` (30px), cells `4px 10px`, **no row rule at all** (§13.3 — the
old comment beside it already argued that "twenty of these down one table
identify nothing the rows do not already identify", and then drew them anyway),
header sticky on `--surface-2` in the label treatment with the one `--line-soft`
hairline a panel's interior is allowed, under `thead`. `.is-num` is right-aligned mono `tabular-nums`. `.ctl-sub`
is the raw id under the readable name at `--t-micro`/`--lh-flush` so the row
keeps the height it was signed off at.

Row tones are a **wash plus a form**, never a text colour: `.is-bad` a
full-height 3px rule on the first cell, `.is-warn` a half-height one, `.is-paused`
a hatched one — because an 8% wash is a 1% change in tone and three washes are
one wash in greyscale.

**Severity is drawn only where it is abnormal.** Vercel's route table leaves a
0% error rate as plain ink and tints exactly one cell. Nothing is coloured for
being healthy; a healthy platform is a quiet grey screen, which is what an
operations console should look like at 3am. This also shrinks the
state-separability problem to the cases where it matters.

### 6.8 One-line expandable row — `.ctl-line` *(lost its box in §13.3)*

Generalised from `.wf-bar`, which the audit names as the best row in the
product: id · state · progress · shape · runner mix · spend · age · flags on one
grid line, expanding into the DAG.

```html
<button class="ctl-line is-open" style="grid-template-columns:[state] 8px [name] minmax(0,1fr) [age] 72px [actions] 28px">
  <i class="ctl-dot is-ok"></i> <span>…</span> <span>4m</span> <span>›</span>
</button>
<div class="ctl-line-body"> … </div>
```

**The column template is the screen's, and it is written with named lines.**
Hetzner declares every list as one `grid-template-columns` with named lines,
applies the *identical* declaration to the header row and the data row so the
two cannot drift, and redefines only that one line per breakpoint.

**Two columns never drop at any width:** `[state] 8px` and `[actions] 28px`.
Identity, state and one action is the irreducible row. At 390px every list in
the product therefore lands on `[state] 8px [name] 1fr [actions] 28px` — no
horizontal scroll, no wrapping, no per-screen phone markup.

**Columns are dropped, never relabelled into "Label: value".** A stacked record
is a different and worse thing than a row. **Cells do not wrap** — `nowrap` plus
`text-overflow: ellipsis` is what makes the row height a constant, which is what
makes the drops clean.

### 6.9 Empty state — `.ctl-empty` *(exists; unchanged)*

Four variants, because four different things look like an empty screen and this
platform's worst bug was drawing them alike: default (a real zero), `.is-failed`
(no number may appear anywhere), `.is-partial` (some arrived; the rest is
unknown, not zero), `.is-admin` (blue, no retry, never the word "failed" — a
non-admin genuinely cannot read `/v1/admin/*`).

**The shape is fixed: mark, heading, one sentence, a link out.** Any real
explanation is a `#help/<topic>` link, never a second paragraph.

### 6.10 Absence — `.ctl-mark`, `.ctl-hold`, `.ctl-ghost`, `.ctl-pending`

See §8, which is what the whole exercise turns on.

### 6.11 Toolbar — `.ctl-toolbar` and segmented control — `.ctl-seg`

**The only chrome a data screen gets above its content.** Railway's
Observability dashboard carries a time-range select at the far left and an Edit
button at the far right, and **zero words of product copy**. That is the target:
the count of sentences above a table in this product is zero.

`.ctl-toolbar > .is-end` pushes a group right; three groups is three children.
`.ctl-seg` is one bordered group with 1px internal dividers, active segment
marked by a surface step and weight — **not a filled pill**. Very low ink, and
it survives both themes without a fill that has to be re-tuned for each.

### 6.12 Page header — `.ctl-page-head`

Title left, actions right, nothing else. No subtitle, no description, no
breadcrumb duplication — the breadcrumb lives in `.ctl-head` one region up and is
never repeated. `flex-wrap` is the entire mobile strategy: the actions wrap under
the title at 390px instead of needing a second, phone-only header.

**The one sentence a screen is allowed lives behind the `?`.** `.ctl-q` already
ships with 82 help topics, hover-120ms / focus-immediate / click-to-pin, and
`#help/<topic>` deep links. The destination was already mature; what was missing
was a rule saying it is *the* destination.

### 6.13 Eyebrow — `.ctl-eyebrow` and facts strip — `.ctl-facts`

`.ctl-eyebrow` is one mono word where a section intro used to be: `Pools by
family`, `Blockers`, `attempts`, `This attempt`. It names a band *inside* a
pane, never the pane — `Capacity.tsx:120` is the shipped example. No rule, no
box, no background — the
device Northflank and Railway both use as their only in-panel section heading.
**It is not uppercase and not tracked (§13.2);** the label rank is mono plus
`--text-faint`, which is two channels on one distinction, and uppercase was a
third.

`.ctl-facts` replaces a definition list: a wrapping strip of unlabelled facts,
each a two-or-three-character mono key plus a full-strength sans value.

```html
<ul class="ctl-facts">
  <li class="ctl-fact"><b>pool</b>claude-code</li>
  <li class="ctl-fact"><b>gen</b><span class="mono">7</span></li>
  <li class="ctl-fact is-absent"><b>age</b><i class="ctl-em">—</i></li>
</ul>
```

**A fact whose value was not read keeps its slot and its key.** A missing row is
indistinguishable from a row that was never going to be there.

### 6.14 Nav rail — `.ctl-rail` *(lost its right border in §13.1)*

200px fixed, six sections, every tab always rendered, never reorders. Its value
is that **a position means one thing**; a list that grows under the cursor has a
geometry you re-read every visit. Below 1280px it narrows to 152px rather than
becoming a 56px icon column — this product has no icon set, and a letter is not
an icon when two sections start with the same one.

Selection: `--surface-2` plus a 2px left rule in `--text`. Hueless, §1.3 — and
with the rail's own edge gone, that fill is what anchors the column.

**No `border-right`, no negative margin, no reclaimed grid gap.** Those three
lines together measured a ZERO gutter between the rail and the content on every
route; what separates the two regions now is `--ctl-gutter`, undivided. §13.1.

### 6.15 Mobile nav

**The rail becomes a horizontally-scrolling strip at ≤899px. It does not become
a floating bottom bar**, because the dock already owns the bottom edge of the
viewport and two fixed bars stacked on a 390px screen is 25% of the glass spent
on chrome. The strip keeps every section visible and scrolls; nothing is hidden
behind a menu.

---

## 7. Responsive

### 7.1 Breakpoints: five, and what each one is

The sheet has thirteen — 420, 560, 561, 640, 720, 899, 900, 1100, 1279, 1280,
1600, plus a second private set in `Overview.tsx`'s injected sheet at 641, 720,
1400. **640 vs 641 and 899 vs 900 are the same boundary spelled twice, and 720
appears twice meaning different things.**

Decided:

| | Meaning |
|---|---|
| **≤560** | phone. One column. Every table on its irreducible row. |
| **≤899 / ≥900** | the rail is a horizontal strip below, a column above |
| **≥1100** | the inspector is a grid *column* rather than an overlay — a **capability test, not a device class**, and pinned by `shell.test.tsx` |
| **≥1280** | the rail widens to 200px; `--app-pad` → 24px |
| **≥1600** | `--app-pad` → 32px |

**420, 640, 720 and Overview's 641/1400 are retired.** Each existing use moves
to the nearest survivor. `test_workflow_step_measurements.py:596` pins
`OVERVIEW_CSS` verbatim and must be rewritten in the same change that folds
Overview's private sheet in — that is the single hardest edit in the next phase
and it is named here so it is planned rather than discovered.

**Prefer no breakpoint at all.** `.ctl-cards` reflows from 4-up to 1-up with
none. Use `repeat(auto-fit, minmax(min(100%, Npx), 1fr))` before reaching for a
media query.

### 7.2 What every screen does at 390px

| | At 390px |
|---|---|
| Frame | rail → horizontal strip; `--app-pad` 16px; header keeps its height and its environment bar |
| Overview | one column, five cards stacked; **the metric strip becomes a 2-up grid, not five stacked 30px figures** |
| Workflows | collapsed rows keep `[state] [id] [progress] [actions]`; the DAG scrolls horizontally inside its wrap and is **not** scaled to fit — scaling turns step names into texture |
| Agents / Holders / Timeline | `.ctl-line` on its irreducible template |
| Pools / Runtimes / Accounts / AdminSettings | `.ctl-table` scrolls sideways; **Pools' Cards toggle is promoted to all four**, since a side-scrolling table is the audit's worst mobile finding and four of the five screens have no escape from it |
| AgentDetail | a full-screen `role="dialog"` overlay, as today below 1100px |
| Dock | 28px collapsed; the page now reserves its actual height (§3.4) |

**Charts are authored twice, not scaled.** Vercel ships a 368×234 SVG with fewer
datapoints under the breakpoint and a 960×560 above, and does not render the
hover apparatus at all on a phone — there is no hover on a phone, so the
crosshair, the tooltip and the point circles are simply absent rather than made
touch-friendly.

**Touch targets are 44px at ≤560px.** Railway ships 32px icon buttons and has
taken public feedback on exactly that. The type does not grow with them.

---

## 8. The prose rule

**This is the section the exercise turns on.**

### 8.1 The invariant, unchanged

> This console must never present an absence as a measurement. A figure nothing
> reported is not zero. A state nobody derived is not QUEUED. A partial total is
> not a total.

That must remain **true** and remain **perceivable**.

### 8.2 The existing rule, kept verbatim

`prose-migration-table.md` already decided this and its acceptance test is not
weakened here:

> The **FACT** stays on the surface, always, and stays impossible to miss. The
> **EXPLANATION** moves into the `?` card.
>
> Can a reader tell, **without hovering anything**, that a number is missing
> rather than zero? A silent icon where a sentence used to be does not satisfy
> this directive.

### 8.3 The one amendment

**The fact may be carried by a visual encoding rather than by a sentence.**

A hatched dial, a dashed border, a hollow ring, an explicit `—` with its own
treatment and a two-word mark all satisfy "impossible to miss without hovering"
— and satisfy it *better* than a paragraph does, because **a paragraph can sit
next to a figure it does not describe, while an attribute on the figure cannot.**

The previous attempt failed the gate for a specific reason worth naming: it
demoted an explanation into a `title=`, which has **no visible anchor and no
keyboard route**. An encoding plus a focusable `?` plus a help topic has both.

### 8.4 Where explanation is allowed to live

Six places, in order of commitment. Nothing outside this list.

1. **The axis, the unit and the figure itself.** `0.8 vCPU`, `12h ago`, `now`,
   `4 of 7`. A well-chosen unit is the explanation; nothing in a chart needs to
   say what it is measuring because the axis already did.
2. **`.ctl-card-note` — the qualifier in the card header.** Right-aligned,
   muted, mono, one line, no verb required. Every explanatory sentence currently
   sitting under a figure collapses into this shape or into (3).
3. **A parenthetical on a column name.** `Date (GMT-4)`, `Spend (measured only)`.
   The caveat attaches to the column, not to a footnote.
4. **`.ctl-card-foot` — the provenance strip.** When it was read, from how many
   sources, how many did not answer. One line, mono, `--text-faint`.
5. **The `?` card (`.ctl-q` → `help.ts`).** The sentence, the paragraph, the
   worked example. 82 topics already exist, with `#help/<topic>` deep links.
   Hover after 120ms, focus immediately, click to pin.
6. **`docs/`.** The argument, the constraint, the thing that is true for six
   months. A docs link is a legitimate element of an empty state and of a help
   card; it is not an element of a data view.

### 8.5 What a main view may say

Five kinds of word, and no sixth:

1. **A name** — an entity, a metric, a column, a tab, a state word.
2. **A unit, fused to its figure** — `256 MB`, `4 of 7`, `$0.00`.
3. **A sentence that *is* the control** — a button's label, a field's label,
   `Alert me at most every [10] minutes`.
4. **A bare count stated as a fact** — `41 rows`, `3 pools did not answer`.
5. **A qualifier in one of the four slots above** (2, 3 and 4 of §8.4).

**Forbidden in a data view:** a definition, a rationale, a "what this means", a
"how to" line, a second sentence anywhere, a paragraph of reassurance on the
healthy path. `Overview.tsx:2086` currently renders
`checks.map(c => c.note).join(' · ')` — **eight full sentences, ~90 words, on a
healthy platform, saying nothing is wrong eight different ways.** That is the
shape being deleted.

### 8.6 The encoding table

**This is the deliverable.** Every kind of absence, its mark, where the words go,
and what a rewritten test pins.

| Kind of absence | The visible encoding | Where the words live | What the test pins |
|---|---|---|---|
| **A real zero** | the figure `0`, plus the track's **axis tick** at the origin; `.ctl-mark.is-zero` (solid border, 3px left axis, the word `real zero`) | `?` topic | `.ctl-util-zero` present, `.ctl-util-track.is-zero`, the mark's word and its `aria-label` |
| **Nothing was ever recorded** | `.ctl-em` (`—`, dimmed, non-tabular, `user-select:none`) in the figure slot; `.ctl-mark.is-absent` (**hatched**, `not measured`) | `?` topic + `docs/` | `.ctl-em` in the cell, **no digit**, `.ctl-mark.is-absent`, `aria-label` |
| **The read failed** | `.ctl-mark.is-unread` (**dashed** border, `--warn`, `not read`); the panel takes `.ctl-empty.is-failed`; **no number appears anywhere** | `?` topic; the error text in the empty state's one sentence | `.is-unread` present, `.ctl-util-fill` **absent**, no digit in the figure |
| **A partial total** | `.ctl-dial.is-partial` — measured filled, **unmeasured hatched**; `.ctl-mark.is-partial` (one dashed edge, `partial`); coverage as `.ctl-card-note` `4 of 7 pools` | `?` topic; the full sentence in `aria-label` | `data-partial="yes"`, the hatched arc, the note's text, `aria-label` |
| **No ceiling / no scale** | `.ctl-track.is-unknown` — **hatched, no fill, and no axis**: there is no scale to start | `?` topic | exactly one hatched track, `.ctl-util-fill` absent |
| **Measured and impossible** (over ceiling) | `.ctl-util-over` — `--ctl-hatch-bad`, the overflow drawn rather than clipped | `?` topic | the over segment present, the fill not pinned at 100% |
| **A state nobody derived** | `.ctl-dot.is-underived` — a ring with a bar through it. **Never coerced into a known state.** | `?` topic; `measure.ts`'s `word` | the modifier class, the word from `measure.ts` |
| **Not entitled** (admin gate) | `.ctl-mark.is-admin` + `.ctl-empty.is-admin` — **blue, solid, no retry, and never the word "failed"** | `?` topic | `.is-admin` present, `.is-failed` absent, no red |
| **Still reading** | `.ctl-pending` — a **moving, lighter** sweep, at the exact geometry the value will occupy | nothing; it is temporary | the class present and the box not collapsed |
| **A truncated list** | `.ctl-table caption` — `showing the 20 longest-running of 143` | `?` topic | the caption's figure, and that it names a total |
| **A stale reading** | `.ctl-stale-note` banner + `.ctl-stale-body` (dimmed, left rule); rows **stay** | the age, in the banner | the banner, the age, the body class |

### 8.7 The three distinctions that must never collapse

1. **Still reading ≠ nothing reported.** This was the one gap in the absence kit:
   `--ctl-hatch` said "nobody reported this" and there was no mark at all for "we
   are still asking". A screen that draws the hatch while a read is in flight has
   told the operator a falsehood it will silently correct a second later — the
   same class of bug as drawing an absence as a zero. **`--ctl-pending` is
   lighter than the hatch and it moves; the hatch is darker and static.** Both
   survive greyscale.
2. **A measured zero ≠ a widget that failed to paint.** A 0%-wide fill on a near
   white track is pixel-for-pixel a broken render. The axis tick is what makes a
   zero a reading.
3. **Not entitled ≠ broken.** A non-admin genuinely cannot read `/v1/admin/*`.
   Painting that red tells someone their platform is down when it is not, on
   every screen, every time they open the console.

### 8.8 How a test gets rewritten

**Never deleted, never weakened to "renders something".** The assertion is
re-pointed at the new encoding and the docstring says what moved and where the
words went. Concretely, `honesty.prose.test.tsx:350`:

```
- expect(visibleText()).toContain('no cost figure')
+ // The sentence moved to help topic `spend-coverage` and to the figure's
+ // aria-label. What the SCREEN now carries is the mark and the em dash, which
+ // is a stronger claim than the sentence was: a paragraph can sit beside a
+ // figure it does not describe; an attribute on the figure cannot.
+ const fig = document.querySelector('.ov-figure')!
+ expect(fig.querySelector('.ctl-em')).not.toBeNull()
+ expect(fig.textContent).not.toMatch(/\d/)
+ expect(fig.closest('[data-measured]')).toHaveAttribute('data-measured', 'false')
+ expect(screen.getByLabelText(/no attempt in this sample reported a cost/i)).toBeInTheDocument()
+ expect(document.querySelector('a[href="#help/spend-coverage"]')).not.toBeNull()
```

The audit counted the cost precisely: **Group A is 21 assertions across 6 files**
and most already assert a class rather than a sentence. **Group B — about 140
source-text assertions in `tests/unit/control_plane/` — is the real cost**, and
two of them fight hardest because they pin *implementation text* rather than
behaviour: `test_workflow_step_measurements.py:596` (Overview's grid, verbatim)
and `test_subscription_headroom_surface.py` (~25 exact code fragments in
`Overview.tsx`). Both must be rewritten in the same commit as the code they pin.

---

## 9. The five things the next phase does first

1. **Promote `ABSENT_MARK` into every screen.** It exists as `.ctl-mark` now;
   `AgentDetail.tsx:349` has the React half. `Overview.tsx:931 Nothing` gets it,
   and Overview's four empty-state paragraphs become four two-word marks.
2. **Give the panels a figure tier** — `.ctl-figure` in every card that has a
   headline number.
3. **Collapse the duplicates.** Eight proportions → `.ctl-track` + `.ctl-util`;
   six tiles → `.ctl-metric`; five empty states → `.ctl-empty`; four chips →
   `.ctl-chip` + `.ctl-dot`; three tab bars → `.ctl-seg` + the rail.
4. **Share the React half.** `Overview.Tile`/`AgentDetail.Metric`,
   `Overview.UtilTrack`/`AgentDetail.Util` and `Overview.Nothing`/
   `AgentDetail.Absent` are three pairs of independent wrappers around the same
   three primitives, and they have already diverged — Overview's track has the
   measured-zero baseline tick and AgentDetail's does not; AgentDetail's absence
   panel has the mark and Overview's has colour only.
5. **Fold `OVERVIEW_CSS` into `styles.css`** and rewrite
   `test_workflow_step_measurements.py:596` in the same commit.

---

## 10. Conflicts and requests

**Track ownership.** `styles.css` and the screens are Track A/B; `docs/` is
Track D per `CLAUDE.md`. This document is a new file in `docs/web-ui/`, which
already holds the UI track's own audit and redesign notes, and no existing Track
D file was edited. Flagged rather than assumed.

**Nothing here requests a change to `apps/common/swarm_common/`.**

**One request, not a change.** `Brand.tsx:22` records that no API route reports
`Settings.core.environment`, so the environment badge can only read
`VITE_SWARM_ENV` or the browser's own hostname and says `ENVIRONMENT UNKNOWN`
otherwise. The deployed console therefore shows the loudest possible badge on
every visit. The fix is one flag on the build — `VITE_SWARM_ENV=dev npm run
build` — and the build lives in `scripts/` and `.github/`, which is Track D.
Reported, not made.

---

## 11. The restraint pass — what changed, and why

**The owner's verdict, verbatim:** *"the design looks almost cartoonish...
please run a whole design workflow and completely revamp the current UI
design"*.

They named the reference set themselves: **Railway, Northflank, Koyeb, Vercel,
Hetzner.** Every one of those is restrained and dense — near-monochrome, small
type, hairline separation, colour reserved for status, numerals modest. This
section records what the measurement found, what moved in response, and — at
the end, because it is the part that gets skipped — what did **not** get fixed.

**This pass is the token layer only.** No screen was restyled. That is
deliberate: the previous eight attempts at this each invented their own card,
their own bar and their own answer to the same question, and the fix for that is
that the whole app moves when the tokens move. Everything below is in
`apps/swarm-ui/src/styles.css` and in this document.

### 11.1 What the measurement found

Both audits are quoted by count rather than by adjective, because "cartoonish"
is not actionable and 16 shadows on one screen is.

| | Ours, shipped | The reference band |
|---|---|---|
| figure : body | **2.14:1**, painted **8×** above the fold | Koyeb's system maximum 1.71:1, never on a tile; Vercel's tile ~1.2:1; Railway's dashboard has none |
| bordered elements, one screen | **54** | Railway 10 · Hetzner 17 · Northflank 20 · Geist 46 (a component gallery) |
| shadowed elements | **16** | Railway 1 · Northflank 1 · Hetzner 2 · Koyeb 1 |
| pill radii | **19** | Koyeb: pills are *metadata*, never status |
| a status, drawn | **102×23px** pill: border, 999px radius, 13px mono 600 UPPERCASE tracked, + a dot, + a second dot | Vercel: a 10×10px solid dot and the word at 14px/400 in **primary ink** |
| a proportion that is a card | a **96px conic ring**, 10–11px band, saturated `--info`, ×2 | **none of the five draws a ring anywhere** |
| `--info` paints | **44** (23 text, 16 border, 5 background) | Railway 0 saturated text · Geist 0 · Northflank 2 hues total |

### 11.2 What moved

1. **`--t-figure` 30 → 22, `--t-title` 20 → 18, h1 weight 650 → 600.** Six
   integer steps, the 12px floor respected, `typescale.test.ts` re-pointed.
   §2.
2. **The dial stopped being a ring** and became the product's own track. This
   also fixes the `37 % left` overflow *at its cause* rather than by tuning a
   number — the box is now `max-content` around the label. §6.5.
3. **The chip became a dot and a word**, at Vercel's measured geometry, with the
   word at full ink and the seven silhouettes intact. §6.6.
4. **The metric tile lost its box.** A visible border on a tile now *means*
   something: nothing healthy is boxed. §6.2.
5. **`--ctl-shadow` came off the card and the tile**, and a card inside a card
   draws no second box. §5.3.
6. **A proportion that is fine is grey**; a hue on a bar is a verdict. §6.4.
7. **A link is ink plus an underline** (`.ctl-link`); the accent is the hover
   and the focus ring. §1.3.
8. **`scroll-padding-bottom`** on the scroller, so a programmatic scroll does
   not park the thing it just focused under the dock. §3.4.

Measured on the Overview at 1440×900, before → after: **bordered 54 → 41,
shadowed 16 → 6, pill radii 19 → 13, 10px radii 10 → 5, largest type 30px → 22px,
document height 1127 → 1112px.**

### 11.3 What this pass did NOT fix

Stated here rather than discovered at 3am.

* ~~**The dock still overlays resting content.**~~ **CLOSED BY §12**, which
  made it a row of the frame's grid. Left here rather than deleted because this
  entry is the third time the item appeared and the first two both read
  "fixed": see §12.3 for what that cost and how the claim is checkable now.
* **Overview's spend bar is still four saturated hues in one 8px rule** with the
  legend on the line below carrying no swatch. `.ov-mix` and `.ov-s1..4` live in
  `Overview.tsx`'s injected sheet. Screen phase: either one hue with the four
  numbers beneath, or keep the four and put the swatch *on* the word — the rule
  all five references hold is that **a hue on a chart is a named series labelled
  next to it**.
* **18 accent-coloured links remain on the Overview** (`.ov-link`). `.ctl-link`
  exists for them to collapse into; the collapse is five screen edits.
* **The `?` glyphs are pills** — 15 of them on Accounts, 5 on Overview — and they
  come from `HelpCard.tsx` / `HelpSection.tsx` React inline styles, not from this
  sheet.
* **`Overview.tsx:1452`'s second state glyph**, which the pill was hiding. §6.6.
* **Accounts and Pools were not reached.** They are worse than Overview on the
  box axis (Accounts: 87 full boxes, 128 bordered; Pools: 186 bordered, 47 pill
  radii) and the owner only screenshotted Accounts. Their state pills are
  screen-private classes, so the chip rebuild did not reach them.
  **→ DONE IN §12.** The capacity-group screen pass collapsed `.tag.acct-state`
  into `.ctl-chip` and took the hue and the 50 cell borders off the five-cell
  bar; Pools lost its six nested family boxes. Measured in a browser, before →
  after: **Accounts bordered 90 → 39, saturated borders 25 → 6, saturated
  backgrounds 14 → 6, saturated text 19 → 13; Pools full boxes 15 → 9.** §12.

### 11.4 What must not be "improved"

**`#work/workflows` is the internal reference and stays untouched.** Measured,
it is already the target — 13 boxes, 1 shadow, 11 colours, zero type at or above
24px, state as a plain lowercase word beside an 8px dot, a 37px row carrying ten
facts. The DAG, the collapsed one-line row and the absence of a mini-map on that
row are settled owner decisions. The one line in `styles.css` that exempted
`.wf-meter-fill` from the monochrome default existed to keep that promise;
on 2026-09-24 the owner chose consistency over it and the meter went grey
(§6.4 records the decision and why).

### 11.5 What no gate can see, and how each claim here was proved

`spacing.test.tsx` renders into jsdom with a hand-written CSS resolver
(`spaceprobe.ts:12`) and **has no layout engine**. It cannot see overlap,
overflow or wrapping — which is exactly why the dock overlap and the dial-label
overflow were both green while both were shipping.

So every claim in §11.2 and §11.3 about overlap, overflow or visual weight was
proved with a **viewport-relative DOM measurement and a screenshot that was
read**, at 1440×900 and 390×844, before and after. A full-page capture is not
evidence for a `position: fixed` dock: it paints the bar at its scroll position
and will mislead you.

---

## 12. The frame — the dock stops being an overlay

§11.3 left one item open and named its fix: *"the complete fix is for the dock
to be a row of the frame's grid rather than an overlay."* This section is that
change, and — because this item has now been declared fixed three times — it
leads with the part that is easy to overstate.

### 12.1 What it does NOT change, measured before claiming what it does

**At steady state, with `--dock-h` correct, flow content renders
pixel-for-pixel the same.** This was checked, not assumed: Overview at 1440×900
and at 390×844, dock collapsed and dock open, before and after, screenshots read
side by side. They are the same image.

The arithmetic says they must be. With the old overlay, the viewport showed
`scrollTop … scrollTop + 844` and the bar painted over the last `--dock-h` of
it, so the operator saw `scrollTop … scrollTop + 604`. With the frame, the
scroller *is* 604px tall and shows exactly that. Max scroll matches too, because
`.app`'s old `padding-bottom: calc(var(--dock-h) + 48px)` contributed precisely
the height the scroller now loses.

**So this is a structural change, not a cosmetic one, and it should not be sold
as a visible redesign of anything.** Anyone re-reading this with a screenshot in
hand and finding no difference has found the truth, not a failure.

### 12.2 What it does change

The old guarantee held only while **two reservations stayed in step with a
measured custom property**. The new one holds by construction: `.ctl-scroll`'s
bottom edge *is* `.ctl-dock`'s top edge, because they are adjacent rows of the
same grid. Concretely:

All four counts below are `.app` leaf elements rendered beneath the bar on the
Overview, measured in the browser at `scrollTop = 0` unless stated.

| | Overlay | Frame |
|---|---|---|
| dock **open** (240px), 390×844 | **8** | **0** |
| dock collapsed (29px), 390×844 | **3** | **0** |
| dock collapsed (29px), 1440×900 | **1** | **0** |
| dock collapsed, 390×844, `scrollTop = 600` | — | **0** |
| what enforces it | `--dock-h` + `.app` padding + `scroll-padding-bottom` | one `grid-template-rows` |
| correct on first paint, before any effect runs | no — `--dock-h` is published by a `useEffect` | yes, no JS involved |
| covers a `position: absolute` popover near the bottom edge | yes, `z-index: 45` beats it | n/a, nothing is layered |

The frame's zeros are not a tuned result — they are the same zero at every
scroll offset and every width, because the scroller cannot paint outside its own
box. The `scrollTop = 600` row is there because it was checked, not because it
could have come out differently.

The row that matters is the first: those 8 elements were **in the DOM, reported
visible, focusable and hit-testable, while being invisible to the eye**. That is
the gap `scroll-padding-bottom` was patching. It is gone rather than patched,
and the honesty invariant is the reason to care — a figure an operator cannot
see is not a figure they were shown.

### 12.3 Why the claim is checkable now, when twice it was not

Both previous passes reported this fixed. Neither was lying; both were asserting
a property no gate could evaluate, and `spacing.test.tsx` cannot evaluate it
either (§11.5 — jsdom, no layout engine).

`shell.test.tsx`'s dock case is therefore **re-pointed rather than deleted**: it
asserted `position: fixed` and `bottom: 0`, which is the overlay encoding and
would now pin the bug. It asserts the frame instead — `display: grid`,
`grid-template-rows: minmax(0, 1fr) auto`, `height: 100%`, and the dock *not*
being `fixed`. Its docstring says what moved and why.

**Both assertions were proved by mutation, not by passing.** Restoring
`position: fixed` to `.ctl-dock` fails it; removing `min-width: 0` from
`.ctl-scroll` fails it. A test that passes against the broken code is what got
this item declared fixed twice.

### 12.4 The two `min-*: 0`, and the regression that found the second

A grid item defaults to `min-height: auto` / `min-width: auto` — *never shrink
below your content*. Both had to be overridden and each failure is a real one:

* without `min-height: 0`, the scroller sizes to the document and pushes the
  dock off the bottom of the viewport — the original overlap wearing a hat;
* without `min-width: 0`, the column takes the widest screen's min-content width
  and **the whole frame overflows sideways**. This shipped for one iteration of
  this very pass and was caught in a 390×844 screenshot — every card ran off the
  right edge — by nothing else. It is the §11.5 lesson arriving on schedule.

`height: 100%`, not `100dvh`: `body` carries the safe-area insets as padding, so
`100%` inherits the `html, body, #root { height: 100% }` chain and lands inside
the notch and the home indicator, where a viewport unit would ignore both.

### 12.5 Consequences, including one that belongs to another screen

* **The open dock's `box-shadow` is gone.** `0 -8px 24px rgb(0 0 0 / .22)` lifted
  a floating panel off a document sliding underneath it. Nothing slides
  underneath it now, so the shadow drew a depth the layout no longer has —
  §11.2 took `--ctl-shadow` off the card and the tile for the same reason. The
  hairline `border-top` is the separation, which is the reference set's answer.
* **`.drawer` now paints over the dock instead of being sliced by it.** Below
  1100px the agent detail drawer is `position: fixed; bottom: 0; z-index: 40`,
  and the dock used to win on `z-index: 45` — a chrome bar cutting the bottom
  29px off a `role="dialog"`. A modal covering the bar is the conventional
  outcome and the better one, but the dock is now hidden while that drawer is
  open at phone width. If that is wrong, the fix is `bottom: var(--dock-h, 0px)`
  on `.drawer`, which is the drawer's section, not the frame's, and was not
  edited from here.

### 12.6 Not fixed, and not by this pass

* **iOS URL-bar behaviour is unverified.** `height: 100%` resolves against the
  *large* viewport, so with the URL bar shown the frame can exceed the visible
  area. This is unchanged from before the pass and was tested in a headless
  browser that has no URL bar. `100dvh` is the candidate fix and it interacts
  with the safe-area padding above; neither was measured on a device.
* Everything else in §11.3 that is not the dock bullet still stands.
## 12. The Overview screen pass

§11 was the token layer and said so: *"No screen was restyled."* This section
is the first screen to follow it, and it is deliberately **only** the Overview —
the screen the owner reacted to. It changes `Overview.tsx` and nothing else. No
token moved, no primitive changed, and `styles.css` was not touched at all.

**It closes four of the six items §11.3 left open on this screen.** Those
bullets are superseded by name below rather than edited in place, because
several lanes are amending this document at once and an append conflicts where
an interleaved edit collides.

### 12.1 What moved, and what it was measured against

All counts are `main.work` at 1440×900 on the live dev server, at `scrollY = 0`,
before → after.

| | Before | After |
|---|---|---|
| `--info` painted on the screen | **23** (19 text, 4 background) | **4** (0 text) |
| …of which are affordances | 19 | **0** |
| state glyphs per Running row | 2 | 1 |
| token-series hues keyed to a name | 0 of 4 | **4 of 4** |
| document height | 1112px | 1108px |

**1. `.ov-link` collapsed into `.ctl-link`** — *supersedes §11.3's "18
accent-coloured links remain on the Overview".* The three call sites now carry
`ctl-link ov-link`; `.ov-link` keeps the layout (flex, mono `--t-micro`,
`nowrap`) and the primitive paints it. The colour, the `text-decoration: none`,
the transparent bottom border and the focus rule all had to be **deleted**
rather than overridden: `OVERVIEW_CSS` is injected as a `<style>` *after*
`styles.css`, so at equal specificity every one of them out-ranks the primitive
and would have quietly reinstated the blue. Anyone folding the remaining four
screen-private link treatments in (`.wb-more a`, `.tile.blocked .t-sub a`,
`.node-links a`, `.art-md a`) hits the same trap.

A side effect worth recording because it is the reason the page got shorter: the
1px transparent bottom border `.ov-link` carried existed so that hovering did
not move text (`spaceprobe.ts:994` documents it). `.ctl-link` hovers with
`text-decoration-color`, which does not affect layout, so the 1px was dead
space. Removing it took **4px** off the attention list.

**2. The `6 more` disclosure stopped being the nineteenth accent paint.** It is
`--text-dim`, `--text` on hover. It is the one control on the card that does not
navigate, and its affordance is the disclosure triangle — a shape, which
rotates on open, so its state is carried without colour at all.

**3. The token legend is keyed** — *supersedes §11.3's "Overview's spend bar is
still four saturated hues … with the legend on the line below carrying no
swatch".* §11.3 left two answers open; this is the one that keeps the
proportion. Each `.ctl-fact` now leads with an 8px `.ov-swatch` taking its fill
from **the same `.ov-sN` class the segment carries**, so a segment and its key
cannot drift apart. §1.6 is the reason this is not cosmetic: the five series sit
in a band 1.36:1 end to end and are **not separable in greyscale**, so the hue
never identifies the segment on its own.

**A series nobody reported draws a hollow swatch, not a solid one.** It has no
segment on the bar, so a solid key beside its em dash would index a colour that
is not there — an absence drawn as a measurement. A **measured zero** keeps its
solid key, because it is a reading. The swatch still occupies its space either
way, so the strip does not reflow when a count arrives (§14). All four are
`aria-hidden`: the bar's own `aria-label` already names every series and its
value, and a second reading of the same four facts is noise.

The legend wraps to two lines in a third-width card. That is accepted: §1.6 asks
for a legend that names every series, and two lines of named series is the cost
of not making the reader index the bar by position.

**4. The second state glyph is gone** — *supersedes §11.3's and §6.6's
"`Overview.tsx:1452` renders a second state glyph inside the chip"*. The row
rendered `{stateGlyph(task.state)} {task.state}` next to the `<i>` that already
draws the same state as a shape, and unlike `Agents.tsx:350` and
`AgentDetail.tsx:481` it was **not** `aria-hidden`, so a screen reader announced
a bare `●` before the word. The `<i>` keeps the shape vocabulary, so nothing
that carried information was removed. `stateGlyph` is no longer imported by this
screen; the other two callers are untouched and still need their own pass.

### 12.2 What this pass did NOT fix

* **The dock still overlays resting content, and this pass moved which rows sit
  under it.** Measured at `scrollY = 0`, before → after: **390px 3 → 3**
  (identical elements), **1440px 1 → 4**. The 1440 number is the 4px above
  arriving: the page got shorter, so the Subscription pool's last row crossed
  the band. It is not a new class of defect — §3.4 already records that an
  opaque `position: fixed` bar has content behind it at *some* offsets and that
  the only complete answer is for the dock to be a row of the frame's grid.
  Any layout change on any screen moves this number in one direction or the
  other. **It is still `Shell.tsx` and it is still not fixed.**
* **The `?` glyphs are still pills** — 5 on this screen, and 6 of the 13 pill
  radii `main.work` reports. They come from `HelpCard.tsx` / `HelpSection.tsx`
  React inline styles, which this pass did not own.
* **The bordered count did not move: 41 before, 41 after.** Broken down, it is
  5 `.ctl-card` boxes, 14 `.ctl-util` row rules, 12 table row rules, 6 `?`
  glyphs, the `.is-alert` tile, the refresh button and the head. Everything
  except the five cards and the alert tile is either a genuine repeated
  separator (§5.2) or another file's. **There is no box left on this screen for
  a screen-level change to remove**; getting below Hetzner's 17 means changing
  what `.ctl-util` and `.ctl-table` draw, which is the primitive's decision.
* **Accounts and Pools are still unreached**, as §11.3 said.

### 12.3 One thing the gates still cannot see

Both facts this section claims about occlusion were measured in a real browser
with `getBoundingClientRect()` against the dock's own rect at `scrollY = 0`, for
the reason §11.5 gives: `spacing.test.tsx` has no layout engine. The legend's
line wrap was read off a screenshot, not asserted.

What **is** pinned by a test is the honesty half, in
`honesty.prose.test.tsx`: the swatch-per-series encoding and the hollow swatch
for an unreported series. It was verified by mutation rather than by going
green — making the absent swatch solid, and deleting the swatch entirely, each
turn it red with a named message.
## 12. The run group — the screen phase's first lane

**Scope: `Agents.tsx`, `AgentDetail.tsx`, `AttemptTimeline.tsx`,
`ArtifactViewer.tsx` and their sections of `styles.css`.** §11 was the token
layer and restyled no screen. This is the first screen lane on top of it, and
it took the run group because that is where §6.6's chip decision lands in
quantity: a run list draws one status per row, forty rows at a time.

Nothing here re-decides anything in §1–§11. Where this lane needed something
§6 had not said, it is written below and marked as an addition.

### 12.1 What the measurement found, on these two screens

Measured in a browser at 1440×900 on the dev fixtures, before → after. The
overlap figures are DOM rectangles intersected with every clipping ancestor,
because an ellipsed cell reports its full text width to
`getBoundingClientRect` and two neighbours that never touch on screen read as
an overlap if you do not.

| The run list (`.work`, `#work/running`) | Before | After |
|---|---|---|
| bordered elements | 18 | **11** |
| uppercase elements | 3 | **2** |
| painted overlaps | 2 | **0** |
| rendered words | 114 | **106** |
| row height | 72px | **31px** (`--row-h`) |
| ways the state is drawn, per row | 3 | **1** |

| The run detail (`.drawer`) | Before | After |
|---|---|---|
| overlapping text pairs (unclipped, whole subtree) | 9 | **3** |
| type steps off the six-step scale | 21px ×5 | **none** |

The three overlaps that remain are in `Dispatch.tsx` (`.dsp-facts`, twice) and
`RunFiles.tsx` (`.ckpt-head`). Both are other lanes' components; §12.5 records
them with their measurements rather than reaching into them.

### 12.2 The run list is the Workflows row, because that is the one the owner kept

`#work/workflows` is the internal reference (§11.4) and the thing that makes
it right is measurable: **its rows are one line tall and they stack.**
`.wf-card + .wf-card` drops the duplicated top border, so ten rows draw one box
and nine hairlines. The run list drew `gap: var(--ctl-s1)` between bordered,
rounded, 72px rows: six rows were six separate floating objects, which is
"everything is a bordered rounded box" in the place this product repeats a box
the most.

**The list carries the box; the row carries one hairline.** That division is
not cosmetic and `spacing.test.tsx` is what settled it. The first attempt gave
each row the full border and softened only the shared edge, and the probe
rejected it twice:

```
divider-under-floor  div.clickable.row [top] 1.88 < 3
surfaces-touch       div.rows [row] 0 < 4 — 6 stacked surfaces with a 0px gutter
```

Both are the same mistake seen from two sides. §5.2: a rule is graded as a
SEPARATOR (1.5:1) only when the element draws exactly one border and has an
adjacent twin — a row drawing four is graded as a boundary at 3:1, which
`--line-soft` cannot meet and should not have to. So the surface and the
boundary moved up to `.rows`, and the row keeps a single `border-top` on
`.row + .row`. That is `.ctl-table`'s construction (§6.7) and it passes.

`.rows` is `display: block`, not `flex` with `gap: 0`. The flex column existed
to make the gutter; there is no gutter. **Stated plainly because it looks like
a dodge of `surfaces-touch` and is not**: that check only runs on a flex or
grid parent, its subject is a gutter between boxes, and its own stated reason —
zero between two boxes "reads as one wider element rather than two" — is the
intent here, the same one `isSegmentedGroup` already exempts `.ctl-seg` on.

### 12.3 Three additions to §6, each the smallest thing that worked

1. **A row's tracks must be width-derived, not content-derived.** Each row is
   its own grid, so an `auto` track resolves per row: the one row carrying a
   dispatch flag sized `[flags]` to its own 190px tag and pulled every column
   left of it out of line with the five rows above. **Ragged columns are what a
   list of independent grids gives you unless every track is stated**, so
   `[flags]` is an `fr` and keeps its width on the rows with nothing to put in
   it — §14, a thing that is absent occupies the space it would have occupied.
   This belongs in §6.8 and is offered for it.

2. **`[state]` is sized to the longest state word, not the common one.**
   112px is `dead_lettered` at `--t-body` plus the 10px mark and its gap. The
   word is mandatory (§6.6) and the mark is a TONE channel, not a state
   channel — PARKED, READY and QUEUED share a silhouette and are told apart by
   the word alone. A track that ellipses it would be the honesty invariant paid
   out for tidiness.

3. **The inspector is a breakpoint that no media query can see.** Opening an
   agent narrows the list from 1165px to ~670px without the viewport changing,
   and `[name]` resolved to 23px — the identity of every row clipped to
   nothing, on the one screen where the row you just opened is the row you are
   watching. `.app.has-inspector .row` drops owner, attempts-used, resource
   class and the model, and gives the width to the name. `.ctl-util`'s comment
   already states the principle: a column that loses 40px still does its job; a
   NAME that loses 40px stops saying which row it is.

### 12.4 What the chip decision cost and what it bought

Every status on these four screens is now `.ctl-chip`, and `Chip` is exported
from `AgentDetail.tsx` so there is one of it (§9.3 asked for this; there were
four).

* **The duplicate glyph is gone** from `Agents.tsx` and `AgentDetail.tsx` —
  §11.2 item 3's outstanding half. `{stateGlyph(state)} {state}` inside the
  chip was a second shape encoding of the fact the `<i>` carries, and the pill
  was the only thing hiding it. **Nothing that carried information left:** the
  `<i>` still carries the tone silhouette and the word — now at full ink — is
  what separates the states that share one.
* **`.roll` is deleted** (four rules). A workflow group's rolled-up state was
  the last filled pill on the list: 999px radius, an 18% tint of the state hue,
  the word in `--*-ink`, uppercase and tracked. It is the same kind of fact as
  every other state in the list and is drawn the same way.
* **`AttemptTimeline`'s outcome and `oom near miss` were `.tag`s** — bordered,
  uppercase, tracked, eight of them on one pane, and `OOM NEAR MISS` was wide
  enough to break its own box onto a second line. A chip whose label wraps is a
  paragraph with a border.
* **`latest` stays a `.tag`, deliberately.** It is metadata, not a state, and
  Koyeb's rule (§6.6) is that a pill-shaped thing should be metadata in a grey
  hairline — that is how a pill is kept from meaning "status". It is the only
  box left on an attempt card's heading, and it is faint.
* **An open pull request is `is-info`, not `is-ok`.** §1.3: the accent is a
  fact or a link, never a verdict. An open PR is a fact about the branch, not a
  judgement that the run went well.

**The chip has no box, so a title has to supply the gap the border used to
be.** `.att-card .ctl-card-title` is a flex row with `--ctl-s2`; without it
`Attempt · gen 1` ran straight into `never started`, because the markup has no
whitespace there. Worth writing down: every place a `.tag` becomes a `.ctl-chip`
inherits this.

**A new guard, and it was mutated to prove it.** `prose.runs.test.tsx` gained
`the run list draws a state once` — one mark per row, no bullet glyph beside
the word, the word present, and a live state never borrowing the healthy
silhouette. The word budget does **not** catch a duplicate glyph, because a
bullet is not a word; restoring the glyph, flattening the tone to `ok` and
restoring `.roll` each failed exactly one assertion and nothing else. No test
was deleted or weakened.

### 12.5 What this lane did NOT fix

* **`Dispatch.tsx`'s `DispatchChip` is now the loudest object on the run
  list** — `INTEGRATE/CONTRIBUTOR`, a bordered uppercase tracked `.tag`, 190px
  wide. It is a status and §6.6 says it is a chip. It is another lane's
  component, so the row only constrains where it sits: at 390px and with the
  inspector open it drops to its own line rather than being ellipsed, because
  `integrate/contrib…` loses the half that says which.
* **`.dsp-facts` overlaps its own note twice** (`collect` × 59px,
  `checkpoints` × 93px) and **`RunFiles.tsx`'s `.ckpt-head` overlaps the
  `<details>` above it** (76px). Measured, not inferred; `Dispatch.tsx` and
  `RunFiles.tsx`.
* **`Liveness.tsx` puts a 17-word sentence in the drawer's heading** — *"No
  event for 3m. Heartbeat events are only every ~150s, so this is not yet
  alarming."* §8.4 says that belongs behind the `?`.
* **`charts/TimeSeries.tsx`'s empty state is a 4-line, 40-word paragraph in a
  bordered box** (`Nothing was measured`), and it is the largest block of prose
  in the drawer. §6.9's shape is mark, heading, one sentence, a link out.
* **`.ctl-util`'s figure column is a flat `78px`** and overflowed six times in
  a 560px drawer. **Request, not a change:** `max-content` there is strictly
  safer than a fixed width for all five callers. The rule's track list is read
  verbatim by `shell.test.tsx:696` and the primitive is shared with Overview,
  Pools, Holders and Workflows, so this lane scoped the fix to
  `.drawer .ctl-util` instead.
* **`.ctl-subnav` draws the drawer's two panes as filled pills** (`Detail` /
  `Attempts`), which §6.11 settles as `.ctl-seg`'s job — one bordered group,
  not N pills. It is rendered by `App.tsx`.
* **The `?` glyph pills** (14 in this drawer) are still `HelpCard.tsx` inline
  styles, as §11.3 recorded.

### 12.6 One conflict found, and it was silently costing a column

`styles.css` carried **two `@media (max-width: 560px)` blocks styling `.row`** —
one in the run list's own region and one ~360 lines later in the workflow
board's, which does not own `.row`. Equal specificity, so the later template
won and the earlier block's `display: none` on `.when` survived unopposed:
**the run list shipped with no elapsed time at 390px**, which is the one column
that screen's own comment says a phone reader came for. Both `.row` rules in
the board's block are deleted; one element, one phone block, and it is the one
in the run list's region. `.row .meta` went with them — nothing has rendered a
`.meta` cell in a task row since the row was rewritten, so the two rules were a
selector pair keeping each other alive.

**Ownership, flagged rather than assumed.** `CLAUDE.md` puts `docs/` with
Track D; §10 already records the same flag for this file. This section is
appended rather than woven into §1–§11 so that two lanes amending the document
in the same pass conflict on nothing.
## 12. The capacity group — the first screen pass

**§11 was the token layer and said so: "No screen was restyled."** This is the
first section that restyles screens. It covers the four number-dense ones —
**Pools (`Capacity.tsx`), Runtimes, Holders, Accounts** — and it
amends §6.2, §6.4 and §6.6 by *applying* them rather than by changing them.

*(Three of those four now live under the **Capacity** section and Runtimes is a
section of its own, so "the capacity group" names a lane rather than a place in
the nav. The grouping is still the right one for a styling pass: they are the
four screens that draw dense figures, which is what the pass was about.)*
Nothing in §§1–11 was re-decided. Where §12 needed something the system did not
have, it is named below as an addition and it is screen-scoped.

### 12.1 What the measurement found, on this HEAD rather than on the owner's screenshots

The owner's screenshots predate §11, so every number below was re-measured in a
browser at 1440×900 against the shipped tokens — otherwise this pass would have
been fixing things §11 had already fixed. Two dev servers, the same DOM probe,
before and after:

| | Pools | Runtimes | Holders | **Accounts** |
|---|---|---|---|---|
| bordered elements | 26 → **20** | 29 → 29 | 11 → 11 | 90 → **39** |
| full boxes (border + ≥6px radius, >60×20px) | 15 → **9** | 8 → 8 | 5 → 5 | 14 → 14 |
| saturated borders | 0 | 0 | 0 | 25 → **6** |
| saturated backgrounds | 20 | 5 | 4 | 14 → **6** |
| saturated text | 1 | 1 | 1 | 19 → **13** |
| uppercase elements | 60 | 93 | 16 | 67 → **61** |
| largest type | 18px | 18px | 18px | 18px |

**Accounts was by far the worst screen in the product and it is no longer the
worst.** Pools' win does not show in `bordered` because what it lost was six
*containers*, which is the `boxes` row. Runtimes' and Holders' rows are flat on
purpose: what was wrong with them was not countable this way, and §12.4 says so
rather than inventing a metric that would have moved.

### 12.2 What moved

1. **An account's state is a mark and a word** (§6.6 applied). `.tag.acct-state`
   — a 1px border in the state hue, a 6px radius, 13px mono 500 UPPERCASE
   tracked, with the **word itself painted in the hue** — is now `.ctl-chip`.
   This is the collapse §9.3 asked for, not a new treatment: the primitive
   already existed and Accounts was one of the four screens that had not
   reached it. The tone moved from `color` to `--chip-tone`, so the word is
   full ink and the mark carries the hue *and* a per-state silhouette, which is
   a channel the bordered pill never had.

   **`CHIP_MOD` is a lookup table and that is load-bearing.** `accountTone`
   answers `ok | paused | wait | bad | unknown`; `.ctl-chip` ships
   `is-ok | is-paused | is-warn | is-bad | is-unknown`. **They are not the same
   vocabulary** — `wait` has no `is-wait` — and the obvious `is-${tone}` fails
   *silently*, because an unmatched modifier falls through to the primitive's
   default `--chip-tone: var(--text-faint)`, which is the **unknown** mark. A
   DRAINING account would have rendered as an account whose state nobody
   derived. That is §8.1's prohibition with the operands swapped and it is now
   pinned by a test (§12.3).

2. **A utilisation bar that is fine is grey** (§6.4 applied, to Accounts'
   five-cell bar). Ten bars of five cells render above the fold and every
   filled cell was `--info` — 50 saturated marks whose collective message was
   "these accounts are normal". `--info` is "a fact or a link, never a verdict"
   (§1.3). The fill is now `--text`; `--bad` survives for a fully spent window,
   which is the one verdict this bar is entitled to make.

   **The three readings separate by more than they did.** Measured was `--info`
   and projected `--text-faint` — a *hue* difference, which vanishes in
   greyscale. It is now full ink against faint, which is a *luminance*
   difference and does not. The amber `~` is unchanged and `.acct-unmeasured`
   still draws **no bar at all** (§8.7.2).

   **The 1px `--line` per cell is gone.** Fifty of Accounts' ninety bordered
   elements were spent outlining a 5×10px mark whose whole shape is its fill.
   The unfilled cell is `--line-soft`, the repeated-separator weight.

3. **The metric tile lost its box on Pools too, and gained a line** (§6.2
   applied). `.cap-pool` is the same thing as `.ctl-metric` under another name
   — one fact, its unit, a proportion — so it takes the same rule: **nothing
   healthy is boxed.** Its `--surface-2` well is gone, which also removes the
   last nested surface on the screen. It is now a **row** rather than a stack:
   name left, figure right and tabular, track underneath. Measured, the tile
   went **94px → 42px** high.

4. **A family is a section, not a card.** Six bordered 10px-radius boxes, each
   holding one table, stacked inside a page that is itself inside the frame.
   §6.13 names the device the references use instead — a heading with "no rule,
   no box, no background" — and §3.1 names the separator: `--ctl-s5`, "the one
   large break: between top-level sections". The `.ctl-card-head` **keeps its
   note**, because that note is Trap E (`platform-wide` vs `this tenant`) and
   removing the card to remove the box would have removed the only thing
   stopping a reader comparing a tenant figure with a platform one. Scoped to
   `.cap-families > .ctl-card`; the primitive is untouched for its nine other
   callers.

5. **The proportion stops at 320px, and this is the answer to the owner's item
   8.** The tile track was `1fr`, so a family holding one pool — Global does —
   drew a single 8px bar **1,106px wide**. That is the most literal possible
   "game health bar rather than a utilisation meter": at that length the fill's
   two ends are not on screen together and the proportion stops reading as a
   proportion. Measured, widest track **1106px → 320px**, and all nineteen
   tracks are now one width instead of ranging 196–1106px.

   **`--track-h` did not move and must not.** 8px is still 8px — §6.4 froze the
   geometry because Workflows was signed off at it. What was wrong was the
   *length*, which is the screen's, not the token's.

6. **A short card stops being stretched to a tall one's height.** `.ctl-cards`
   is a grid and a grid item stretches by default, so Holders' Class-mix card —
   which carries one two-word mark — was held at the height of the four-row
   table beside it. A mark alone in 200px of empty card reads as a panel that
   failed to load, which is §8.7.2's confusion in a different costume.
   `.hold-top { align-items: start }`. Measured **200px → 113px**.

### 12.3 One new addition to the system, and one honesty fix nobody could see

**THE ADDITION IS A SPACING RULE, AND IT EXISTS BECAUSE THE PILL WAS DOING A
JOB NOBODY HAD NOTICED.** `.tag.acct-state` was a bordered box, so its border
was the gap between the state and the `skipped here` mark beside it. `.ctl-chip`
has no border by design, and with nothing between them the cell rendered
`availableSKIPPED HERE` — one word to anyone skimming the column, and a second
fact lost. `.acct-statecell > .ctl-chip { margin-right: var(--ctl-s2) }`.

**A margin and not a flex gap, and the reason is the table.** `display: flex` on
a `<td>` takes the cell out of the table layout algorithm, and this column's
width is decided by that algorithm alongside four others that must stay aligned
with their `<th>`s. A gap is not worth re-deciding the column model for.

**THE HONESTY FIX: THE PHONE WAS DROPPING A PROPORTION.** `styles.css` drops
`.ctl-util-track` outright at ≤560px, and the rule is right *where it was
written* — inside `.ctl-util` the bar shares a line with the figure, so on a
phone it is a ~100px picture of the number beside it. `.cap-pool` is not that
shape: its track is on its own line with the tile's full width. Nothing was
being saved, and what was being lost is not decoration. **Three of §6.4's four
track states are the only place their fact is drawn** — `.is-unknown` (hatched,
no fill, no axis) is how "no ceiling was read" is said, and `.is-zero` (axis
plus inset hairline) is how a measured nought is told apart from a widget that
failed to paint. With the track gone at 390px both collapsed into blank space.
The figure's unit still carried the words, so the phone was not *lying* — but it
was down to one channel where every other width has two, and §8.3 is explicit
that the encoding **is** the fact. Re-shown, scoped to `.cap-pool`.

**HOW THE TEST WAS WRITTEN, AND WHY IT IS A NEW FILE RATHER THAN A RE-POINT.**
§8.8 says an assertion is re-pointed at the new encoding and never deleted.
Before changing the chip, the existing suite was **mutated** to find out what it
could see of the old one: duplicating the state word inside the chip —
`{account.state}{account.state}` — left both `prose.budget.capacity.test.tsx`
and `honesty.prose.test.tsx` green. **There was no assertion to re-point.** So
`src/__tests__/encoding.accounts.test.tsx` is the assertion that was missing,
and it was itself mutation-checked: replacing `CHIP_MOD[tone]` with
`` is-${tone} `` fails it with *"DRAINING did not reach is-warn; it drew
ctl-chip acct-state is-wait"*. It pins what this screen owns — that each state
reaches its own modifier, that exactly one row may carry the unknown mark, and
that the word and the mark never stand in for one another — and deliberately
**not** the colours, sizes or shapes, which belong to `.ctl-chip` and are
already held by `test_state_colour_discriminability.py`.

### 12.4 What this pass did NOT fix

* **Runtimes and Holders barely moved on any counter, and that is honest.**
  What was wrong with Runtimes was a **layout defect no counter sees**:
  `.ctl-fact` is `inline-flex`, so every child `Credential` returned became a
  flex item, and at the catalogue's card width the provider broke mid-token
  (`demo-` / `vendor`) and the flag broke mid-phrase — `any` on one line, `of`
  alone on the next, `one` squeezed out of view. It is fixed by one wrapper
  (`.rt-cred`) that makes the credential a single flex item laying out as
  ordinary text. **`spacing.test.tsx` is jsdom and has no layout engine (§0.3),
  so no gate in this repository could see it before or can confirm it now.** It
  was found in a screenshot and is fixed in a screenshot.
* **Runtimes still renders 93 uppercase elements on one screen.** They are
  `.ctl-fact > b` — thirteen fact keys per card across four cards — and that
  treatment is §6.13's decision, not this screen's. Changing it is a change to
  the primitive and belongs in a pass that owns it.
* **The `?` glyphs are still pills** (§11.3) and still come from
  `HelpCard.tsx` / `HelpSection.tsx` React inline styles, which this pass did
  not own. They are the largest remaining source of pill radii on all four
  screens: 9 on Pools, 19 on Runtimes, 16 on Accounts.
* **Accounts' `needs a person` banner is still a bordered, tinted box.**
  `.banner` lives in the Trouble-board block of the sheet, not in this group's,
  and one tinted box for the one thing that needs a human is defensible under
  §6.9. It is named here so the decision is visible rather than assumed.
* **`.ctl-mark` is unchanged**, so `NOT READ`, `SKIPPED HERE` and `PARTIAL` are
  still bordered uppercase boxes. They look like the pill that was just
  removed, and they are not the same thing: **the mark's border IS its
  encoding** — dashed for a failed read, hatched for never measured, solid for
  a real zero (§8.6). Softening them would spend the honesty channel to buy a
  lower box count. If they are to change it must be as a redesign of the
  absence vocabulary, with all eleven kinds moved together.
* **The dock still overlays resting content at 390px**, exactly as §11.3 says.
  Nothing here touches `Shell.tsx`.
* **Pools' document got 68px taller** (2050 → 2118 at 1440×900). Six boxes were
  traded for `--ctl-s5` of air between six sections, and air costs height. The
  Cards view went the other way, 1821 → **1686**.

### 12.5 One question this pass did not answer

**Pools' Cards view and its Table view now disagree about what a family is.**
In Cards the family is a section with a heading and no box; in Table it is the
same, but the `.ctl-table` inside still draws its own border, so a reader
toggling between them sees one boundary appear and disappear. Both are
defensible and the toggle is an owner-facing control, so which one is right is
a product judgement rather than a system one. Recorded, not decided — the same
way §6.4 recorded `.wf-meter`'s blue until the owner answered it on
2026-09-24.

---

## 13. The layout system — the pass that changed levels instead of values

**Why this section exists, stated so nobody repeats the mistake.** The restraint
pass (§11) was briefed as a correction of *attributes* — figure size, shadow
count, hue count, chip construction. It did all of it, correctly, and the
owner's verdict on the result was that the design *"didn't really change
much"*. The proof is in that pass's own report: **bordered elements on the
Overview went 41 → 41.** Values moved; structure did not. Same grid of boxes,
same information architecture, same layout.

So this section decides three things a colour pass cannot touch: **where the
page's air is, what case a label is written in, and which level of the hierarchy
is allowed to draw a border.** All three ship as tokens and primitives in
`styles.css`; no screen was restyled here, which is the build lanes' job.

**One thing deliberately NOT retuned: the six type steps.** The brief allowed it
and re-deriving them is exactly the trap above — a different set of numbers and
the same screens. `typescale.test.ts`'s `SCALE` is untouched.

### 13.1 The gutter — `--ctl-gutter`

**The defect, measured on the live dev server: the gutter was ZERO.** The rail's
right edge sat at x=252 and the first card's left edge sat at x=252, on every
route at every width. Content flush against the divider it stands beside.

The cause was three individually reasonable lines whose sum was not:

```css
.app       { gap: 0 var(--ctl-s5) }             /* 28px between the columns */
.ctl-rail  { margin-right: calc(var(--ctl-s5) * -1);   /* …take all 28 back */
             padding-right: var(--ctl-s5);             /* …spend it here    */
             border-right: 1px solid var(--ctl-hairline) }
```

The rail reclaimed the entire column gap with a negative margin, spent it as its
own right padding, and parked the hairline on the far side of it. Every pixel of
separation went to the left of the line and none to the right. The comment above
it — *"the gutter belongs to the rail, not to the grid gap"* — describes the
intent exactly, which is why nobody looked again: it reads as a decision about
where the line sits and is actually a decision to give the content no gutter.

**Two things changed, and the second is the structural one.**

**1. The page has one gutter token, not two ladders.** `--app-pad` ran
16 → 24 → 32 while the rail-to-content gap was a flat `--ctl-s5` (28px): two
unrelated numbers for one idea, so at 1280px the page's edge gutter (24) and its
interior gutter (28) disagreed. `--ctl-gutter` is now the **only** distance
between two top-level regions — the page's side padding *and* the rail-to-work
gap — and `--app-pad` is a constant alias kept by name because `Brand.tsx` lines
its wordmark up through it. There is nothing left for a screen to reinvent.

| | ≤1279 | ≥1280 | ≥1600 |
|---|---|---|---|
| `--ctl-gutter` | 16px | 32px | 40px |

It is a token named for its **job**, not a fifth step of the rhythm scale — the
same standing `--ctl-pad-chrome` has, and for the same reasons: it is the only
kind of value allowed to grow with the viewport, and nothing in a data region
may use it. §3.1's four steps are still four.

**2. The rail's hairline is deleted, and the gutter is the separation.** §5.1
already says *"grouping is a surface step, separation is a hairline, prefer the
step"*, and the rail was the one boundary on the page where that was not
applied — and the boundary with the most room to apply it. Railway, Vercel and
Northflank all separate nav from content with space alone; Koyeb uses a fill
step. A 32–40px void reads as two regions more clearly than a 1px line did, and
the rail's own selected-row fill (`--surface-2` plus a 2px `--text` rule) is what
anchors the column.

**Measured after, at 1440×900:** `railRight=232  mainLeft=264  gap=32`.

### 13.2 Casing — the rule is that nothing shouts

**The owner's question was "some helper text is all CAPS and others are regular
caps?? WHY?" and the honest answer was that there was no rule.** Twenty-two
separate declarations in `styles.css` forced `text-transform: uppercase` —
`.ctl-eyebrow`, `.ctl-metric-label`, `.ctl-table thead th`, `.ctl-fact > b`,
`.ctl-mark`, `.ctl-q-title`, `.ctl-dock-label`, `.ctl-subnav-admin`, `.brand-k`,
`.tag`, `.t-label`, `.dsp-default`, `.art-turn-role`, two `h3`s, two `h4`s and
more — each decided by whichever component was written that week. Labels of the
**same rank** shouted on one screen and whispered on the next. Runtimes alone
rendered 93 uppercase elements.

**THE RULE, and it is a ban rather than a budget:**

> `text-transform: uppercase` does not appear in this sheet. `text-transform`
> exists only to bring a string **the API chose** into this console's register,
> never to emphasise one we wrote.

So three values stay legal, and they share one property — each makes the string
quieter or leaves it alone:

| Value | Where | Why it is a normalisation |
|---|---|---|
| `lowercase` | `.ctl-chip` | the API shouts `RUNNING`; the console does not |
| `capitalize` | `.filters button` | a machine token (`queued`) read as a word |
| `none` | `.id`, `code`, `kbd`, `samp` | refuse an ancestor's casing on a string we did not author |

`uppercase` is the only value that can *only* ever be emphasis, so it is the
only one banned. `typescale.test.ts` holds the count at zero.

**Why the label layer survives without it.** The claim this document used to
make was that *"uppercase mono grey means this is a label, not data"* — and two
thirds of that sentence does the work. The label rank is **mono plus
`--text-faint`**, against a sans face at full strength for the datum (§2). That
is already two channels on one distinction. Uppercase was a third on the same
distinction, and a third channel buys volume rather than clarity. It also costs:
capitals are the least legible case at 12–13px; they force the 0.04–0.08em
tracking that went with them and that nothing else in this sheet uses; and they
make a label ~15% wider on a product whose complaint is density. Measured on
Runtimes at 390px, `IN USE (UNITS)` was clipped where `In use (units)` fits.

**The reference set settles it.** Of the five consoles this product is aimed at,
**four paint no uppercase at all**: Northflank's project dashboard is sentence
case end to end (`Services`, `Configuration values`, `Job succeeded`), Railway's
is too, Vercel's Geist declares none, and `grep -c uppercase` over Koyeb's
shipped design system returns **0**. Hetzner is the one that uses it, for exactly
**one** rank — the rail's section headers — and nothing else. Nobody spends it on
twenty-two things.

**The tracking went with it, everywhere.** Letter-spacing on a label exists to
open capitals up; on lowercase mono at `--t-meta` it reads as a loose word.
Thirty-two positive `letter-spacing` declarations were removed in the same edit
and a second assertion holds them gone. **Negative tracking stays and is a
different thing** — `--t-title` and `.ctl-figure` both tighten, which is what
large type needs.

### 13.3 The box — three levels, three separators, and the default is not a box

A count that will not move under a colour pass moves only when you decide **which
level of the hierarchy may draw a border at all.** Decided here, once:

| Level | What it is | Separated by |
|---|---|---|
| **Region** | a top-level block of a page (`.section`) | `--ctl-s5` of space, plus **one** `--line-soft` hairline between two of them. Never a box — a region is not an object, it is a change of subject. |
| **Panel** | `.ctl-card`, `.ctl-table`, screen equivalents | **the one box.** One `--line` hairline, `--radius`, `--surface`. The only border in the console. |
| **Row** | anything repeated inside a panel — a list row, a `.ctl-util`, a table row, a tile | **nothing.** No border, no rule, no per-row fill. `--row-h`, the mono/sans split and the fill step do all of it. |

**The one exception, named so it stays one:** `.ctl-table thead th` keeps a
`--line-soft` bottom rule. A column head is a different *kind* of row rather than
the next one, and it says where the data starts exactly once.

**What this pass deleted under that rule** — all primitives or frame, no screens:

* the rail's `border-right` (§13.1);
* `.ctl-head`'s bottom rule — it sat between the product header's own border and
  the page `<h1>`, three horizontal lines within 70px of the top of every screen,
  and a rule with no twin is a boundary with no reason (§5.2);
* `.ctl-q-glyph`'s circle — **six per screen**, one beside every panel title, the
  largest single non-panel contributor to the count. It is now a `--surface-2`
  disc, which is the same line→step substitution as everywhere else;
* `.ctl-line`'s per-row border — a stack of eight rows was eight boxes, in the
  *boundary* weight, inside a panel that was already a box. This is the
  construction none of the five references use;
* `.ctl-line-body`'s border, whose surface step was already doing the work;
* `.ctl-util + .ctl-util`'s separator. This one is the sheet's own example turned
  around: §5.2 cites it as the canonical case where `--line-soft` is *legal*, and
  that is still true. Legal is not wanted. A rule allowed to be faint enough to
  pass a separator floor is, by construction, a rule nobody needed;
* `.ctl-table`'s twenty row rules. The old comment beside them already argued the
  case — *"twenty of these down one table identify nothing the rows do not
  already identify"* — and drew them anyway.

**A card keeps its edge, and this pass deliberately did not take it.** In a dark
theme a fill step alone is enough — Railway's panel and page differ by 1.00:1 and
it still reads. But light is the theme this product is designed in (§1.1),
`--surface` on `--bg` is 1.03:1, and a panel with neither edge nor shadow on a
light page is a panel nobody can find. **One box per panel is the target the
references actually hit; zero is a different product.**

**Two rhythms moved with it**, because removing boxes without giving back the
space they were standing in just makes a flat page:

* `.ctl-cards` gutter `--ctl-s3` → `--ctl-s5`. Two panels 12px apart while the
  page's own gutter is 32–40px is a rhythm **inversion** — the gap inside the
  content read tighter than the gap around it, so a row of cards read as one
  striped object. Northflank's panel gutter measures 28–30px at 1440.
* `.section` and `.section + .section` padding `--ctl-s3` → `--ctl-s5`. A change
  of subject was separated by 24px while a card grid *inside* one region was
  separated by 12px: a 2:1 ratio is not enough to tell "next panel" from "next
  topic" at a glance.
* `.ctl-metrics` gutter `--ctl-s2` → `--ctl-s3`. The tiles have no edge, so the
  gap **is** the separation, and 8px of it read as one banded strip.

### 13.4 Two tests re-pointed, and what moved in each

Neither was deleted or weakened; both assert the same property against the new
encoding, and each says so in its own docstring.

* **`spacing.test.tsx` → `CENTRED_SIDEWAYS` is now `[]`.** It held
  `button.ctl-q-glyph [left]`/`[right]`: a 20px circle whose sideways inset is
  unmeasurable without a font, so the probe named it rather than exempting it
  silently. With `border: 0` there is no inset to fail to measure. It stays a
  named, typed constant rather than an inline `[]` so the next centred box still
  has to be argued for.
* **`brand.test.tsx` → the breakpoints widen `--ctl-gutter`, not `--app-pad`.**
  The claim is unchanged — *some* wide breakpoint grows the page gutter, and
  `.brand-row` and `.app` resolve the **same** token or the wordmark drifts away
  from the rail. A new assertion was added that `--app-pad` still derives from
  `--ctl-gutter`, so re-splitting the two ladders fails here.

**And one test added**, because the casing rule is the kind of claim a source
scan can make and the DOM cannot: `typescale.test.ts` asserts zero
`text-transform: uppercase` and zero positive `letter-spacing` in the sheet. A
rule on a screen this suite never mounts is exactly where the twenty-third would
have survived.

### 13.5 The honesty invariant, and what it cost

**Nothing in the absence vocabulary was softened.** Every kind of nothing still
has its own encoding — dashed for a failed read, hatched for never measured,
solid for a real zero (§8.6) — and every one of them was *already* carrying
shape, fill and words as its channels rather than case. What changed is that
`.ctl-mark` now reads `real zero` instead of `REAL ZERO` on the same hatched
pill. The distinction stays perceivable and the words stay reachable; the eleven
honesty suites pass untouched.

`.ctl-metric` keeps its **transparent declared border**, which is the pattern to
copy for any absence state: a border that appears only when a read failed would
move every neighbour by a pixel at the moment the strip most needs to hold still.

### 13.6 What this pass did NOT fix

* **`.ctl-util`'s name column ellipses inside a 340px card.** On Overview's
  three-up grid the four-column util row needs ~441px and gets ~348px, so
  `browser · 20 concurrent` renders as `browser …` — the exact failure the
  primitive's own comment says must not happen ("the name holds a floor and the
  bar gives way"). **This predates this pass**; the gutter change costs it a
  further ~11px per card. It is the capacity lane's to fix, and the fix is a
  column template, not a gutter.
* **A CSS rule cannot reach a shouted string literal, and three of them existed.**
  `Accounts.tsx` wrote `<th>ACCOUNT</th>`, `CLEARS` and `STATE` in capitals in
  the source, so removing `table.pools thead th`'s `text-transform` left one
  table on one screen still shouting — the exact inconsistency §13.2 removes.
  Those three (and `5H`/`7D`) were changed **in the source**, which is the only
  place they could be changed. It is the one edit this pass made outside the
  token and primitive layer, and it is five string literals with no styling
  attached.
* **`Brand.tsx`'s environment badge still shouted, deliberately not fixed in
  this pass — and answered by the owner on 2026-09-24.** The question as it
  was left: `label: 'ENVIRONMENT UNKNOWN'` and `label: env.name.toUpperCase()`
  rendered `DEV`, `LOCAL` and `PRODUCTION` in capitals from the source. Under
  §13.2 that is emphasis on a string we author and should go — but the prod
  badge is a **safety** signal with its own full-width bar, and whether the
  production banner is allowed to shout is a product judgement rather than a
  typographic one.

  **The decision (2026-09-24): `dev` and `local` render in sentence case like
  every other authored string; the PRODUCTION banner and ENVIRONMENT UNKNOWN
  stay in capitals, because they are safety signals.** In `Brand.tsx`,
  `envTreatment` upper-cases `env.name` only for `kind: 'production'`, keeps
  the literal `'ENVIRONMENT UNKNOWN'`, and writes every non-production
  declared name and the loopback case through `sentenceCase` — so the deployed
  dev console reads `Dev`, a laptop reads `Local`, and a `staging` build reads
  `Staging`. The rule that falls out is that **capitals are spent exactly where
  the header draws its full-width bar**: the two kinds where a mistake is
  expensive shout in both channels, and nothing else shouts in either.
  `brand.test.tsx` holds it as a property over `classifyEnvironment`'s inputs
  (loud kinds print in capitals, quiet kinds in sentence case, capitals
  coincide with the bar), not as a list of literal strings; it was pushed red
  against the old casing before `Brand.tsx` changed.
* **Screen-private boxes are untouched.** `.tile` (`border: 1px solid var(--line)`),
  `.wf-bar`'s row border and the other 38 screen namespaces still draw their own.
  §13.3 is the rule they collapse into; applying it is the build lanes' job, and
  the Overview count will not reach the 10–20 band until they do.
* **`.brand-word b` is still `font-weight: 700`**, against §2's "there is no 700
  in this console". It is a logotype and arguably the one legitimate display
  weight, but it is an exception nothing has written down. Two label-rank `h3`s
  that were also 700 were brought to 600 in this pass; the wordmark was left.
* **The dock still overlays resting content at 390px**, exactly as §11.3 and
  §12.4 say. Nothing here touches `Shell.tsx`.
