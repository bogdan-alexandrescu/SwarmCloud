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

**What it does not change.** The palette values, the two line weights, the six
type steps, the four spacing steps and the corner scale were all measured
against gates that still hold them (`test_ui_contrast.py`,
`test_state_colour_discriminability.py`, `typescale.test.ts`, `spaceprobe.ts`).
A design pass that re-derives them buys a different set of numbers and the same
screens. Everything below either restates a decision that is already load-bearing
or fills a hole the audit named.

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

**Unchanged. Six steps, set by the owner, not this document's to move.**

| Token | Size / leading | What it is |
|---|---|---|
| `--t-micro` | 12 / 1.45 | ages, raw ids, provenance, card feet. The hard floor. |
| `--t-meta` | 13 / 1.45 | column heads, chips, eyebrows, state words — **a treatment as much as a size**: 600, uppercase, tracked |
| `--t-body` | 14 / 1.50 | the workhorse: table cells, values, controls, `body` itself |
| `--t-lead` | 16 / 1.55 | a card title; **the one sentence a screen is allowed** |
| `--t-title` | 20 / 1.30 | the screen `<h1>` |
| `--t-figure` | 30 / 1.10 | the one number a card exists for |

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
| `--ctl-s5` 28px | the one large break: between top-level sections |

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
| `--app-pad` | 16 → 24 (≥1280) → 32 (≥1600) | the gutter grows with the viewport; the content is not centred in a lane |
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
and closes, not one that opens and stays. **This is the only screen-visible
change in this pass**, and no gate could see it (§0.3).

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

### 5.3 Two elevations and no third

| Token | Value (light / dark) | What gets it |
|---|---|---|
| `--ctl-shadow` | `0 1px 2px rgb(31 35 40 / .08)` / `0 1px 2px rgb(0 0 0 / .30)` | a **raised surface**: a card, a tile, a panel on the page |
| `--ctl-shadow-pop` | `0 8px 24px rgb(31 35 40 / .12)` / `0 8px 24px rgb(0 0 0 / .28)` | a surface **over** the page: the help card, a menu, a drawer edge |

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

### 6.2 Metric tile — `.ctl-metric` *(exists; unchanged)*

One fact, its unit, and what it does not include. Label (`--t-meta`, mono,
uppercase, `--text-faint`) → value (`--t-figure`) → `sub` → `foot`. **One
`--t-figure` per tile; a second figure is a second tile.**

States: `.is-absent` (dashed border, value drops to `--t-body` and becomes a
phrase — nothing but a measured number gets the figure step), `.is-unread`
(dashed + `--warn`), `.is-good` / `.is-alert` (a corner mark in the shape
vocabulary, so tone is the second signal and not the only one).

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

### 6.4 Proportion — `.ctl-track` / `.ctl-util` *(exists; unchanged)*

One height (`--track-h` 8px), one radius (`--track-radius` 2px), one colour
rule, one axis. **Every proportion in the product is this**; the audit found
eight and this is the consolidation that already happened once and must not grow
back.

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

### 6.5 Dial — `.ctl-dial`

The proportion that *is* the card, rather than one row of a list: headroom,
quota, coverage.

```html
<div class="ctl-dial is-partial" style="--pct:57; --measured:40"
     role="img" aria-label="40% of the 4 pools that reported. 3 pools did not answer, so this is not a platform total.">
  <b class="ctl-dial-figure ctl-figure">40<span class="ctl-figure-unit">%</span></b>
</div>
```

`conic-gradient` plus a radial mask. No dependency, both themes from one
declaration, `--dial-size` for the size.

**The total is always drawn.** Hetzner paints the unfilled arc at full opacity
in every meter they ship; a partial total then *looks* partial, because the
unmeasured remainder is visibly present and visibly not filled. That is the
invariant rendered rather than narrated.

| State | Drawing |
|---|---|
| default | `--pct` filled in `--info`, the rest in `--surface-2` |
| `.is-partial` | measured to `--measured` in `--info`, **hatched** from there — the hole in the total is drawn as a hole |
| `.is-unknown` | the whole ring hatched, nothing filled. An empty ring reads as 0%, which is a claim |
| `.is-zero` | empty, plus a tick at twelve o'clock in `--text-dim` — the same axis mark, meaning the same thing |

### 6.6 Status chip — `.ctl-chip` *(exists)* and the bare dot — `.ctl-dot` *(new)*

**The word is mandatory in a chip; the mark repeats it as a shape.** Seven
silhouettes, and it is the same vocabulary wherever a state is drawn:

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

`.ctl-dot` is that mark **without** the pill, for a table cell, a DAG node or a
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

### 6.7 Data table — `.ctl-table` *(exists; unchanged)*

Scrolls sideways rather than reflowing into cards: these are numbers that only
mean anything beside each other in a row.

Rows `--row-h` (30px), cells `4px 10px`, row rule `--line-soft` (a genuine
repeated separator), header sticky on `--surface-2` in the label treatment, last
row's rule removed. `.is-num` is right-aligned mono `tabular-nums`. `.ctl-sub`
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

### 6.8 One-line expandable row — `.ctl-line` *(new)*

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

`.ctl-eyebrow` is one uppercase mono word where a section intro used to be:
`CAPACITY`, `HOLDERS`, `BLOCKERS`, `THIS ATTEMPT`. No rule, no box, no
background — the device Northflank and Railway both use as their only in-panel
section heading.

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

### 6.14 Nav rail — `.ctl-rail` *(exists; unchanged)*

200px fixed, six sections, every tab always rendered, never reorders. Its value
is that **a position means one thing**; a list that grows under the cursor has a
geometry you re-read every visit. Below 1280px it narrows to 152px rather than
becoming a 56px icon column — this product has no icon set, and a letter is not
an icon when two sections start with the same one.

Selection: `--surface-2` plus a 2px left rule in `--text`. Hueless, §1.3.

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
| Agents / Holders / Activity | `.ctl-line` on its irreducible template |
| Capacity / Runtimes / Accounts / AdminSettings | `.ctl-table` scrolls sideways; **Capacity's Cards toggle is promoted to all four**, since a side-scrolling table is the audit's worst mobile finding and four of the five screens have no escape from it |
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
