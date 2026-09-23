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
| `--t-meta` | 13 / 1.45 | column heads, eyebrows, uppercase labels — **a treatment as much as a size**: 600, uppercase, tracked. *No longer the chip: see §6.6.* |
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

> **An open question for the owner, held open on purpose.** `.wf-meter`'s fill
> carries `ctl-util-fill wf-meter-fill`, so the monochrome default reached
> straight into the frozen screen and turned its 54px meter grey. It is exempted
> back to `--info` by one line, because Workflows was explicitly frozen. That
> leaves the product with **one blue proportion and every other proportion
> grey**. Resolving the inconsistency means changing a screen the owner said not
> to change, so it is recorded here rather than decided.


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

### 11.4 What must not be "improved"

**`#agents/workflows` is the internal reference and stays untouched.** Measured,
it is already the target — 13 boxes, 1 shadow, 11 colours, zero type at or above
24px, state as a plain lowercase word beside an 8px dot, a 37px row carrying ten
facts. The DAG, the collapsed one-line row and the absence of a mini-map on that
row are settled owner decisions. The one line in `styles.css` that exempts
`.wf-meter-fill` from the monochrome default exists to keep that promise, and
§6.4 records the inconsistency it leaves rather than hiding it.

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
