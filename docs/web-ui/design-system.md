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
| `--ok` | `#1a7f37` | `#58c668` | healthy, succeeded, under the ceiling — **as a figure or a fill, not on a state mark**: the ok disc is grey (§6.6, CH-17) |
| `--warn` | `#6e4a00` | `#ffd60a` | approaching a limit, a read that failed, a partial total |
| `--bad` | `#681117` | `#f85149` | failed, over a ceiling, act on this |
| `--info` | `#0969da` | `#58a6ff` | **the link's hover, the focus ring and the live mark — never a verdict, and no longer the fact mark**: the info flat bar is grey (§6.6, CH-17) |
| `--paused` | `#7139db` | `#b288f8` | parked, held by an operator — which is not "full" |
| `--*-ink` | see §1.4 | see §1.4 | the accent as *text on a tint of itself* |
| `--series-1..5` | one value, both themes | | a chart line's identity — never a severity |

Three surfaces and no fourth. Two line weights and no third. Three text tones
and no fourth. Five state hues and no sixth.

### 1.3 There is one accent, and selection has no hue

`--info` is the accent. It is the link's hover colour, the focus ring and the
live mark. It is **not** a severity and is deliberately outside the ok/warn/bad
triad in `test_state_colour_discriminability.py`.

*(Amended 2026-09-25, CH-17.)* It used to be "the mark for a fact that is not a
verdict" as well. It is not any more: the fact mark is the flat bar, drawn in
`--text-faint` (§6.6), so a fact carries no hue at all. On a state mark, hue is
spent only on warn, bad, paused and live.

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

**So a link is ink plus a `--line` underline, the boundary token at 3:1 or
better, and the accent when you point at it** — `.ctl-link`. This is a
*stronger* affordance than the blue was, not a weaker one: WCAG 1.4.1 says
colour may not be the only channel, and a colour that is also the focus ring,
the live-agent dot and a chart fill was already too overloaded to read as
"clickable" on its own.

*(Amended 2026-09-25, CH-23.)* The resting underline was `--line-soft`, chosen
so it would not compete with the word on it. At rest the underline is the whole
affordance, so it is a component boundary and is held to §1.2's 3:1 floor for
one; `--line-soft` measured 2.05:1 (light) and 1.72:1 (dark) on `--surface` —
about 1.3:1 once antialiased — which is why Overview's row links read as plain
text. `--line` measures 4.04 / 3.80 / 3.63:1 in light and 3.53 / 3.85 / 3.24:1
in dark on `--surface`, `--bg` and `--surface-2`, and is drawn at a 1px
thickness floor. The `.ctl-link` list, the `:where(a)` fallback and the fact
strip's hover cue all use it; `encoding.hues.test.ts` holds every
`text-decoration-color` in the sheet to 3:1 on all three grounds, so the next
underline in `--line-soft` fails wherever it is written.

`.ctl-link` ships as the primitive; **it is not yet universal and this document
does not claim it is.** `.ov-link`, `.wb-more a`, `.node-links a` and `.art-md a`
are screen-private link treatments that each paint `--info` (a fifth,
`.tile.blocked .t-sub a`, went with the Timeline's boxed tiles in TS-11), and
folding them in means editing those screens — the screen phase's job. What §11 shipped is the primitive they collapse into, so the screen
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

### 1.5 Text never sits on a hatch

*(Corrected 2026-09-25, CH-4. This section used to say that `var(--ctl-hatch)`
"is in that test's allowlist and may carry text". That was an exemption resting
on a premise nobody had measured, and the premise was false.)*

`--ctl-hatch` is two stripes, `--surface-2` and `--line`, and `--line` is a
**component boundary** colour (§1.2). `--text-dim` over it measures **1.51:1 in
light and 1.90:1 in dark** — across half of every glyph. Three marks shipped
their words on it: `.ctl-mark.is-absent` ("not measured", the phrase the mark
exists to make legible), `.ctl-stale-mark` (the age that says how far to
distrust a value) and the unknown-environment badge.

**The rule is now: text sits on a solid fill, and the hatch is a band, a swatch
or a rim beside it.** `.ctl-mark.is-absent` and `.ctl-stale-mark` put the word
on `--surface-2` with a 6px hatched band at the leading edge; the badge keeps
its hatch and puts each word on a `--surface-2` plate inside it. The shape
channel survives greyscale exactly as before — hatched still means "not a
measurement" — it just no longer runs under the letters.

**And the gate measures it rather than trusting it.** `test_ui_contrast.py`
resolves a gradient stop by stop — a token that holds a gradient is expanded to
the gradient first — and holds the text to AA against the **worst** stripe.
Its allowlist of unresolvable backgrounds is empty. A gradient whose stripes all
clear AA (`.banner.pill.unknown`, `--surface`/`--surface-2`) passes on its
ratio; a hatch under text fails on its ratio. `.ctl-pending` and `.ctl-ghost`
still set a background and no `color`, and their caption goes in a sibling —
which is also where a screen reader wants it.

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
| `--t-lead` | 16 / 1.55 | a card title; **the one sentence a screen is allowed**; and, at 600, **every section, panel and step heading** (TS-18) |
| `--t-title` | **18** / 1.30 | the screen `<h1>` (`.head h1`, `.ctl-page-head > h1`), at weight **600** — and only two other things: the product wordmark (`.brand-word`, a logotype, not a heading) and a rendered document's own h1 (`.art-md .art-h[data-level="1"]`), which follows the document's ladder |
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

**The cap is the real rule: one `--t-figure` per card.** On a screen that
summarises, one card carries the screen's figure. In a grid of peer cards that
each state the same measure in the same unit, every card carries its own
figure, because the figures are compared card to card, not read as a KPI wall.
The peer grids are Pools' pool tiles (occupancy), Pool limits' profile cards
(agent ceiling) and Platform counts' scope cards (task total per scope). A
smaller step used eight times on a summary still reads as a KPI wall; no
reference screen shows its largest size more than once on one. *(Amended
2026-09-25, AH-22 in #86, so this section and §6.2 agree;
`honesty.admin.test.tsx` holds the per-card half on Pool limits.)*
`apps/swarm-ui/src/__tests__/typescale.test.ts` states the six values and was
re-pointed, not worked around.

**The Overview is the one named exception, and its terms are exact** (OV-12,
owner decision 2026-09-25). It has five figure-bearing regions — the lead's
count, the strip's two facts (Running, Units held), the Spend card's figure and
the Headroom group's headline — with **one figure-step number per fact, and
never the same fact twice**. Until then the strip also drew `Account headroom`
and `Token spend`, which are the Headroom group's and the Spend card's own
figures, so seven figure-size numbers stood above the fold for five facts. The
document no longer states a cap that screen breaks; what the exception does not
allow is a second copy of any fact at the figure step.

**The heading ladder** (OV-15, owner decision 2026-09-25). `--t-title` belongs
to the page `<h1>` alone. In-page region and card headings sit at `--t-lead`
and weight 600 — the Overview's attention lead title is an `<h2>` at that step,
and stays distinct from the card titles by its position, its track and its
unboxed region, which is how the paragraph below ranks hierarchy. **An in-page
heading that ties with the h1 is a defect.** The in-page headings the sheet
used to draw at `--t-title` — `.section > h2` and `.section > .ctl-toolbar > h2`
(§B4.1 of the sheet), `.sbf-move-h`, `.state h3`, `.ctl-empty > h3`,
`.art-head h3` and `.ckb-head h3` — are TS-18's (epic #84), which moves them to
`--t-lead`/600 (`timeline.submit.rules.test.ts`). TS-18's first draft kept
`.ov-lead-title` at `--t-title` as an exception; OV-15 wins on that element
(resolved on #84, 2026-09-25). A rendered document's own h1
(`.art-md .art-h[data-level="1"]`) follows the document's ladder, not the
console's.

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
| `--ctl-shadow` | `0 1px 2px rgb(31 35 40 / .08)` / `0 1px 2px rgb(0 0 0 / .30)` | **the DAG node, and nothing else by default.** A node has to read as sitting *on* a canvas the edges pass *under* — a genuine z-relationship. A card on a page does not. *(Amended below: no edge passes under a node any more.)* |
| `--ctl-shadow-pop` | `0 8px 24px rgb(31 35 40 / .12)` / `0 8px 24px rgb(0 0 0 / .28)` | a surface **over** the page: the help card, a menu, a drawer edge |

> **Amended 2026-09-25 (WF-4, epic #83): an edge never passes under a card.**
> The row above let a DAG edge run *under* a node, and on the workflow canvas
> that is what happened to every edge that skips a level: one straight curve
> from its parent to its child, crossing the level between wherever the line
> fell — and the cards are opaque HTML over the edge layer. On the measured
> 30-step run, `synthesis`'s six direct dependencies on a 13-step stage ran
> collinear with that stage's edges into `rollup-b` and disappeared behind
> it. The owner's decision is that every dependency is visible, so an edge that
> skips a level now runs on an **offset lane of its own**: a column clear of
> every card and band on the levels it passes (8px, `LANE_CLEAR`) and of every
> other lane over them (6px, `LANE_SEP`), reached and left through the gaps
> between levels where nothing is drawn — in a gutter between two cards where
> one is free, otherwise beside them, widening the canvas if it must. Edges
> that share both ends (every member of a collapsed band) share one lane.
> `dag.ts`'s `laneRouter` does it and `workflow.board.test.tsx` samples every
> drawn edge and fails on any point inside a card or a collapsed band. The
> node's shadow went earlier, to the edge's halo; the halo now separates one
> edge from another where two cross in a gap.

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

**The live pulse has a floor of `.8` (CH-19, 2026-09-25):**
`@keyframes ctl-live-pulse { 0%, 100% { opacity: 1 } 50% { opacity: .8 } }`,
one keyframe for all three live marks (`.ctl-dot.is-live`, `.ctl-chip.is-live >
i`, `.liveness.live > i`). It was `.35`, which blended a live mark to **1.69:1
(light) and 1.95:1 (dark)** for about half of every cycle — the running dot
under the 3:1 a graphical object needs. Measured, blending the mark over each
ground: at `.7` light `--info` is about 2.9:1 on `--surface-2` and `--bg`; at
`.75` the light `--ok` liveness dot is 3.00:1 on `--surface-2`; at **`.8` every
live mark clears 3:1 on `--bg`, `--surface` and `--surface-2` in both themes**,
the lowest being light `--ok` at 3.26:1 (light `--info` 3.39–3.66:1, dark
4.65:1 or higher). `encoding.hues.test.ts` finds every rule that names the
keyframe — not a list of three — and holds it to 3:1 at the keyframe's lowest
stop.

**And reduced motion actually stops it.** The global rule sets
`animation-iteration-count: 1 !important` beside the `.01ms` duration. The
duration alone left `infinite` running, so every frame caught the mark at an
arbitrary point of its fade; one iteration plays once, invisibly, and rests at
the mark's base opacity of 1, and the static halo is then what keeps live
apart from ok, as this section claims.

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

**A foot's clauses are separate `nowrap` elements in a `.ctl-foot-run`, and the
separator is drawn by CSS, never typed** (OV-14, owner decision 2026-09-25). A
typed " · " left a dot at the end of a wrapped line and let a clause break in
half; the run draws each clause's dot in the gap to its left and clips the one
that would start a line. The Overview's feet use it; other screens adopt it
when they are next touched.

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

**A strip is a wrapping run of facts, not a grid** (OV-6, owner decision
2026-09-25; `styles.css` §B6.1): each fact is as wide as what it says, and a
wrapped line holding a single fact is accepted. The same rule governs the
inspector's strip.

**A strip carries only facts no panel on the screen repeats, and a fact is drawn
at the figure step once** (OV-12). The Overview's strip is Running and Units
held; headroom and spend are drawn by the panels that hold their context.

**A linked tile underlines its label** (OV-8): ink plus a resting underline,
the accent on hover and focus — §1.3's rule, on `a.ctl-metric` so every linked
fact gets it. **The label is a branch of the `.ctl-link` rule itself** (and of
its hover rule, for hover and focus), not a rule restating its values, so
whatever the link's underline token, thickness or offset becomes applies to the
label unchanged; the metric block adds only the label's faint ink at rest. The
figure is never underlined: the fact's bottom edge is reserved for the absent,
unread and alert rules, and the label is present in all four renderings while
the figure is not.

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

> **The workflow Timeline's part of the ruling (WF-11, epic #83; the #122 hue
> ruling), 2026-09-25.** The Timeline outlined every wait in `--warn-ink` and
> filled every successful run `--ok`, so a screen of healthy work was a screen
> of caution and success hues. Now: **a wait (waited or still waiting) is a 1px
> `--text-faint` outline; a finished run is a `--text-dim` fill whether it
> succeeded or was cancelled; and hue appears only on a failed run or a run in
> flight.** A run in flight keeps `--info` and its sweep. A failed run keeps its
> `--bad` fill and gains TS-4's failed mark (redesign-v2 §5.5 Tier 1, "solid
> fill + a 2px left rule") in the shape §6.7 gives bad: a `--bad` post at the
> run's start standing 8px above and below the bar — it has to stand past the
> bar, because a `--bad` rule on a `--bad` fill is invisible. Against
> `--surface` the post is 5.3:1 dark and 12.5:1 light, so in greyscale a
> failed run keeps an outline a grey run does not. The dashed open edge, the
> `--info` now line and the `--warn` "task unread" word are unchanged, and the
> row's state dot still carries every state. `workflow.views.test.tsx` resolves
> the sheet in both themes and holds all of it, the post read from the sheet
> because jsdom computes no pseudo-element.

> **A finished workflow row draws its outcome composition, not a progress
> meter (WF-1, settled on #83, 2026-09-25).** `9/30 done` as a 30% bar said the
> work was still going. In the same 8px track, a terminal row draws one `.wf-seg`
> per outcome at its share of the steps: succeeded solid in the meter's
> `--text-dim`, failed and dead-lettered TS-4's solid `--bad` with the 2px rule,
> cancelled TS-4's flat "ended" bars, by selector on TS-4's own rules (§15.3). A
> row that has not ended keeps the meter (`workflow.board.test.tsx`).
> **A failed or dead-lettered segment is at least 4px wide**: TS-4's 2px cut is
> drawn inside the segment, and one step of thirty is 1.7px of the meter's 51px,
> which the cut painted entirely `--surface` — a failure drawn as nothing. 4px
> is the cut plus 2px of `--bad`; the other segments shrink to give it up.
> Succeeded in `--text-dim` rather than TS-4's `--ok`, and dead-lettered in the
> failed form, are this build's reading of "TS-4's vocabulary" and are open for
> the owner's confirmation (#83).

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
> `.ctl-util-fill.ov-projected { background: var(--text-faint) }`. That rule
> shipped before this decision and the first guard never saw it. It is now
> listed by name in the test's `DOCUMENTED_GREYS`, and
> `test_a_documented_grey_is_a_grey` resolves it to a text grey. So "grey like
> every other proportion" holds for it, in a second grey that means "not
> current".
>
> It was spelled `var(--ctl-absent)` while `--ctl-absent` was
> `var(--text-faint)`. The ui-hygiene lane (PR #26) then split `--ctl-absent`
> into its own warm stone so an absence no longer matches the CANCELLED fill,
> and on merge that would have turned this bar warm: a colour picked to differ
> from grey by its hue, on a fill this decision says is grey.
> `test_a_documented_grey_is_a_grey` failed on exactly that. The bar now names
> `--text-faint`, the pixel it always painted.
>
> **The tilde beside it is `--text-faint` too, corrected after review.** The
> merge fix (`45b507d`) left `.ov-tilde` on `--ctl-absent`, calling it an
> absence mark. It is not one: it marks a figure that *was* measured and is no
> longer current, and `accountHeadroom` in `Overview.tsx` says that "old
> information" and "no information" are opposite facts. `--ctl-absent` now
> means only "nobody measured this", so the projection glyph was drawn dE76 0
> from the em dash on a never-polled row in the same column. The tilde now
> paints the same grey as its bar, which is the pixel both had before #26.
> `test_a_projected_reading_is_not_drawn_in_the_absence_colour` holds every
> mark of a projected reading at least dE76 10 from `--ctl-absent`, in both
> themes. It finds the marks the way the fill guard finds a fill: the element
> as the JSX renders it (tag, classes, parent), every rule in every sheet
> matched against it, through the cascade. Its first version looked rules up
> by exact selector text, so only a rule written `.ov-tilde` was measured and
> `.ctl-util-figure .ov-tilde { color: var(--ctl-absent) }` passed (CI run
> 36040434721, on a mutation of `Overview.tsx` alone).
>
> **"A second grey" is only true in the dark theme.** The projected bar's
> `--text-faint` is 1.27:1 (dE76 7.2) from the live bar's `--text-dim` in the
> dark theme and 1.11:1 (dE76 3.1) in the light one. The light figure is
> under the 1.2 that `test_state_colour_discriminability.py` calls "not a
> visible step", so in the light theme the bar alone does not tell a projected
> reading from a live one. The `~` does. What
> `test_a_documented_grey_is_not_the_default_grey` holds is that the two greys
> are different, not that they are visibly different, and it holds it of what
> the sheets paint on a fill carrying `ov-projected`, not of the test's own
> table. Its first version compared `DOCUMENTED_GREYS` with the default and
> never read a sheet: pointing only the shipped rule at `var(--text-dim)`
> passed (CI run 36040434721). `test_a_documented_grey_is_what_the_sheet_paints`
> now also fails whenever the table and the sheet disagree, so the checks that
> read the table are checks of the product. Separating the two greys visibly
> without a hue would take a texture or another grey, and that has not been
> decided.
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
| `.is-partial` | filled to `--pct`, plain track on to `--measured`, **hatched** from there — the hole in the total drawn as a hole *(split by OV-2; see below)* |
| `.is-unknown` | hatched, **no fill and no axis**. An empty track reads as 0%, which is a claim nobody made |
| `.is-zero` | empty, axis drawn, plus the inset hairline `.ctl-util-track.is-zero` uses — the same mark, meaning the same thing |

The `aria-label` route to the sentence is the caller's and is unchanged.

**Amended by the 2026-09-25 QA decisions (epic #81).**

* **The track draws the figure itself (OV-2).** It drew coverage — usable
  accounts over all accounts, checks that ran over all checks — under a figure
  that said something else, so it was full whenever coverage was complete. The
  headroom track is the headline's % used; the checks track is open checks over
  all checks (checks, not problems: one check can raise several problems and a
  track cannot fill past its total). **Coverage appears only when it is
  partial, as the kit's `.ctl-mark.is-partial` under the track**; a complete
  population draws no coverage at all.
* **A partial track hatches only what was not measured** (OV-2, corrected in
  review of #157). `.is-partial` is three segments now: the figure filled to
  `--pct`, the plain track on to `--measured`, the hatch from `--measured` to
  the end. The attention lead sets `--measured` to the checks that ran, so a
  check that came back clear is plain track and only a blind one is hatched:
  one blind check of eight is an eighth of hatch (`--measured: 88`), where the
  first version of OV-2 hatched everything past the open count and drew seven
  clear checks as unmeasured. The headroom headline sets no `--measured`, so its
  hatch starts at its figure — the hatched remainder OV-12 (b) names; the
  table's `.is-partial` row above keeps its meaning, the hole drawn as a hole.
* **A fifth state, `.is-pending` (OV-9)**: the reads are in flight. The figure
  slot holds the pending mark and no digit; the track is `.ctl-pending`'s moving
  surface, with no fill, no axis and **no hatch** — the hatch means a read
  failed, and drawing it over a request still out is §8.7.1's falsehood.
* **`.is-warn` / `.is-bad` (OV-12)**: the headroom headline takes the verdict of
  the account row it names, computed by the same function the row uses, so the
  two cannot disagree. The measured part of the track takes
  `.ctl-util-fill.is-warn/.is-bad`'s colour and stripe; a partial track keeps
  its hatched remainder; the figure stays in `--text`. No new colour role.
* **One polarity: a subscription window's percentage is % used, everywhere**
  (OV-1) — the Overview's headline and rows, the Accounts table and `sc`. The
  headline names its account (`28 % used · laptop`), and **every % carries its
  word**: on the figure where there is no column head, on the head (and the
  phone key standing in for it) where there is. The headline was % left over
  rows of % used, both printed as a bare `%`, and the fullest account's `74`
  sat one line under a `72` that meant the opposite.

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

| State | Mark | Hue | Meaning |
|---|---|---|---|
| ok | filled disc | **none** — `--text-faint` | present, and fine |
| warn | triangle, apex up | `--warn` | the universal caution shape |
| bad | diamond | `--bad` | a disc knocked off its axis — the one mark with corners |
| info | flat bar, 10×3 | **none** — `--text-faint` | a fact, not a verdict; and **ended** (CANCELLED) |
| paused | two bars | `--paused` | the pause glyph |
| unknown | hollow ring | none | an absence of information, drawn as one |
| underived | ring with a bar through it | none | the state exists; nobody computed it |
| live | disc with a halo | `--info` | a dot that is broadcasting |

**THE HUE RULING (CH-17, 2026-09-25): on a state mark, hue is spent only on
warn, bad, paused and live.** `ok` and `info` keep their shapes and lose their
hue: the ok disc and the info flat bar are drawn in `--text-faint`, and their
word or figure is in plain or `--text-dim` ink. The hue never touches the word
(above), and nothing is coloured for being healthy (§6.7). In greyscale the
marks stay distinct by shape alone: a filled disc (ok), a flat bar (a fact, or
ended), a hollow ring (unknown), a triangle (warn), a diamond (bad).
`--info` stays the link's hover colour, the focus ring and the live mark. The
primitives this moved: `.ctl-dot.is-ok`, `.ctl-dot.is-info`, `.ctl-chip.is-ok`,
`.ctl-chip.is-info`, `.ctl-metric.is-good` (a neutral disc, its value in
`--text`), and the dock's read cells (§15.4). Healthy-state hue that remains has
an owner and is not this ruling's: `.pool .ctl-track > i`, `.pool.prov.ok`,
`.state.acct-ok`, `.tag.ok` (CP-14 — neutral since the 2026-09-25 consistency
sweep, §15.7, whose follow-up puts the success heading and the legacy ok tag,
two words, in `--text-faint` rather than this paragraph's "plain or
`--text-dim`": it is later and names them); the Profile headroom status (CP-12);
`.wf-tl-span.is-ok` and the Workflows graph's `.node.ok` (WF-11). This ruling
does not reach chart segment fills: TS-4's outcome stack keeps its solid hues
and gained a shape for each outcome (§15.3) — failed is still solid `--bad`,
with redesign-v2 §5.5 Tier 1's 2px left rule.
*Workflows' two, settled on #87 (2026-09-25):* the graph's `.node.ok` and the
row's `.wf-state.ok` are neutral too — a succeeded node keeps the node's own
`--text-faint` rule and a succeeded row's word is `--text-dim` — because WF-11,
named above as their owner, ruled only the Timeline.

**THE ENDED MARK (CH-22, 2026-09-25).** CANCELLED is `stateTone`'s fifth tone,
`ended`: terminal, not a verdict, and drawn as the grey flat bar — the **one**
`is-info` modifier, not a second one. It was `wait`, so Agents drew a cancelled
task as the caution triangle and Workflows as a blue disc with an amber word.
`.ctl-dot.is-info` is the flat bar this table always specified (it had been a
filled disc, so at 390, where the word is hidden, a cancelled and a queued
workflow were one dot). The waiting states keep the wait mark: `chipTone` draws
`wait` as the triangle, and Workflows' `dotClass` now does too, so a queued
workflow and a cancelled one no longer share a mark. The vocabulary, for a new
screen to reuse rather than extend:

| `stateTone` | States | Chip / dot |
|---|---|---|
| `ok` | SUCCEEDED | `is-ok` — grey disc |
| `bad` | FAILED, DEAD_LETTERED | `is-bad` — diamond |
| `live` | LEASED, DISPATCHED, STARTING, RUNNING | `is-live` — haloed disc |
| `wait` | QUEUED, READY, PARKED | `is-warn` — triangle |
| `ended` | CANCELLED | `is-info` — grey flat bar |

**The chip's base mark is the hollow ring** (owner ruling, 2026-09-25, CP-14):
a chip whose modifier matched no rule draws the unknown mark, never the ok disc,
and every other state declares its own fill.

> **Owner ruling, 2026-09-25 (CP-14, #85): healthy carries no hue.** The ok mark
> keeps its filled disc and its word at full ink, and paints a text grey, not
> `--ok`, on `.ctl-chip.is-ok`, `.ctl-dot.is-ok` and the legacy `.tag.ok`. Hue
> on a state mark is left to the verdicts (warn, bad, paused) and to live.
> Because the change is in the primitive, Pools, Runtimes, Accounts'
> `AVAILABLE`, Provider quota's `AVAILABLE` and every other ok mark went grey
> with no screen edit. The `--ok` token stays defined, and the dock (CH-17) and
> the Timeline (WF-11) apply the ruling in their own boxes.
>
> **Which grey, where CP-14 and CH-17 meet.** CP-14 chose `--text-dim` — the
> grey §6.4 uses for a proportion that is fine. CH-17, decided after it, names
> the chip and dot primitives and draws the ok disc and the info bar in
> `--text-faint` (the hue ruling above), so the later ruling sets the grey of
> `.ctl-chip.is-ok` and `.ctl-dot.is-ok`. `.tag.ok`, which CH-17 leaves to
> CP-14, is a word and stays `--text-dim` (§6.6's "plain or `--text-dim`" for
> a word), as does Accounts' success heading; the 2026-09-25 follow-up on #85
> gives `--text-faint` to marks and fills only (§15.7). CP-14's ruling — no
> hue on a healthy mark — holds under either grey, and
> `test_the_ok_mark_is_a_text_grey` accepts both.
>
> **The cost, recorded so it is not rediscovered:** in greyscale the ok disc is
> now told from bad and warn **by its silhouette alone**. `--text-faint`, the
> primitives' grey, is 1.01:1 from `--bad` in the dark
> theme and 1.45:1 from `--warn` in the light one; `--text-dim`, CP-14's first
> choice, is 1.26:1 and 1.30:1
> (WCAG relative luminance of the theme tokens in `styles.css`). The
> 1.5:1 triad floor (`MIN_STATE_RATIO` in `test_state_colour_discriminability.py`)
> still governs the `--ok` / `--warn` / `--bad` tokens that fills use and is not
> relaxed; `test_every_chip_state_has_its_own_silhouette` is what holds the ok
> mark apart, and its "may equal the base" exemption moved from `is-ok` to
> `is-unknown`. `test_the_ok_mark_is_a_text_grey` and
> `test_a_chip_whose_modifier_matches_nothing_draws_the_unknown_ring` pin the two
> halves of the ruling on the cascade, in both themes.

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
mean anything beside each other in a row. **Below 900px a data table is
`.is-scroll` and holds its first column in view; a record of four columns or
fewer is `.is-stacked`** — the one rule for tables below 900px is §7.3.

Rows `--row-h` (30px), cells `4px 10px`, **no row rule at all** (§13.3 — the
old comment beside it already argued that "twenty of these down one table
identify nothing the rows do not already identify", and then drew them anyway),
header on `--surface-2` in the label treatment with the one `--line-soft`
hairline a panel's interior is allowed, under `thead`. `.is-num` is right-aligned mono `tabular-nums`. `.ctl-sub`
is the raw id under the readable name at `--t-micro`/`--lh-flush` so the row
keeps the height it was signed off at.

**The head is not sticky (WF-21, 2026-09-25).** It declared `position: sticky;
top: 0; z-index: 1`, on `.ctl-table thead th` and on `table.pools thead th`, and
the declaration never took effect: `.ctl-table` and `.table-wrap` are
`overflow-x: auto`, which makes the wrapper the head's scroll container on both
axes, and no wrapper ever scrolls vertically. Below 899px the stacked tables
hide the head anyway. What it did do was draw every head cell as a positioned
layer of its own — the likely cause of the faint vertical seams the Workflows
Table showed at fractional column edges. Not yet verified rendered: the next
release is to be looked at at 1440 in light and dark, and if a seam survives,
the head's fill moves from each `th` to `thead`, painted once. `shell.test.tsx`
holds that no `thead th` rule is sticky, and that any positioned table cell
with a z-index (CH-13's sticky first column, when it lands) ranks under the
drawer's band.

Row tones are a **wash plus a form**, never a text colour: `.is-bad` a
full-height 3px rule on the first cell, `.is-warn` a half-height one, `.is-paused`
a hatched one — because an 8% wash is a 1% change in tone and three washes are
one wash in greyscale.

**Severity is drawn only where it is abnormal.** Vercel's route table leaves a
0% error rate as plain ink and tints exactly one cell. Nothing is coloured for
being healthy; a healthy platform is a quiet grey screen, which is what an
operations console should look like at 3am. This also shrinks the
state-separability problem to the cases where it matters. A healthy row's
status is the ok mark — a filled grey disc beside its word (`--text-faint` on
the chip and the dot since CH-17; CP-14 first drew it `--text-dim`, §6.6) —
which is present and legible and carries no hue (§6.6, owner ruling 2026-09-25).

*(Extended 2026-09-25, CH-17.)* The same ruling now holds for the marks
themselves (§6.6): the ok disc and the info flat bar are grey. And the dock's
read cells take this section's forms: a failing cell is in the
`tr.is-bad > :first-child` / `tr.is-warn > :first-child` selector lists, so a
failed read and a failed row draw one left-edge rule, and its status is `--text`
— never a text colour.

### 6.8 One-line expandable row — `.ctl-line` *(lost its box in §13.3)*

Generalised from `.wf-bar`, which the audit names as the best row in the
product: id · state · progress · shape · runner mix · spend · age · flags on one
grid line, expanding into the DAG. `.wf-bar` itself keeps its box: it is the
second of §13.3's two named exceptions (WF-17), and `.ctl-line` does not
inherit that exception.

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

*(Scoped 2026-09-25, CH-13.)* This is the rule for a `.ctl-line` LIST. A
`.ctl-table` below 900px follows §7.3 instead: a data table scrolls with its
first column held, and only a record of four columns or fewer — an inspector
fact — is relabelled into a stacked "Label: value" record, which is §B6.3's
construction kept for the one case it fits.

### 6.9 Empty state — `.ctl-empty` *(exists; unchanged)*

Four variants, because four different things look like an empty screen and this
platform's worst bug was drawing them alike: default (a real zero), `.is-failed`
(no number may appear anywhere), `.is-partial` (some arrived; the rest is
unknown, not zero), `.is-admin` (blue, no retry, never the word "failed" — a
non-admin genuinely cannot read `/v1/admin/*`).

**The shape is fixed: mark, heading, one sentence, a link out.** Any real
explanation is a `#help/<topic>` link, never a second paragraph.

**One mark per empty state, and it is the primitive's** *(amended 2026-09-25,
§15.6)*. `Absent` draws the mark inside the heading; a heading or a sentence that
says `real zero` again, or a hand-drawn `.ctl-mark` span in the body, is a
second silhouette with no sentence behind it. Five screens shipped one each
until #170; `emptystate.onemark.test.tsx` holds all five to one.

**`Screen`'s empty state ends in its `Checked …` line, and it ticks**
*(CH-1/CH-10, settled on #87, 2026-09-25)*: the primitive's foot at the micro
step, on the shared clock, reading the sub-line's instant so the two never
disagree; and a screen's way out is `empty.link`, never an anchor typed into
the sentence (CP-21).

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

### 6.12 Page header — `PageHead` (`.head` + `.sub`), and `.ctl-page-head`

*(Amended 2026-09-25, AH-25 in #86: this section said "title left, actions
right, nothing else", while fourteen routes drew a title over a line of
provenance and the code called that line the house standard. The rule below is
the head that ships.)*

**A title, then one line of provenance.** The line says what was read, how old
it is, and the screen's read control: `4 tenants · read 2m ago · refresh`. It
carries no description sentence. **A control that costs something prints its
cost immediately before it on that line**, and the two sit in one unit that does
not wrap apart: `not counted yet · 24 count() per run · Run the count`. The
control is `.sub button`, the link-style read-now control, not a boxed button.

`PageHead` in `Shell.tsx` is the markup, once: `.head > h1` over `p.sub`.
`Screen` renders it on fourteen routes, and Platform counts and Help render it
with their own lines. Every head has a line.

**The one exception is Help.** It reads nothing, so it has no provenance to
print: its line says what the page is and which topic is showing —
`<n> topics in <m> groups · showing <topic title>` — every part read from
`help.ts` at render time, so no count is written down here to go stale. It is
the head-line shape, facts joined by `·`, and not the description sentence
AH-15 deleted ("why a figure on these screens looks the way it does"), which
was true of about one topic in eight.

A screen whose one `?` explains the whole screen puts it after the title, in
`.head` and outside the `<h1>` (`PageHead`'s `help`): the Workflows board's
absent figures, and Profile headroom's every-pool-at-once, whose words are the
pool column's name on every card (`Pools it must clear (all at once)`, CP-5,
as Pools names its `Could start (min across pools)`), are the two cases.

`.ctl-page-head` is the wrapper for the heads `PageHead` does not describe —
Overview's facts row and the API reads page. There it stays title left, actions
right. No breadcrumb duplication anywhere — the breadcrumb lives in `.ctl-head`
one region up and is never repeated. `flex-wrap` is the entire mobile strategy:
the line wraps under the title at 390px instead of needing a second, phone-only
header.

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

**A fact that WAS read and needs attention is ink, not a mark** *(amended
2026-09-25, AG-5, §15.6)*. The six marks are six kinds of nothing, and there is
no seventh. A measured figure a reader should act on -- the artifact viewer's
masked-credential count above zero, the log panel's "not applied at read
time" -- is drawn in `--warn` ink with no mark and no `.is-absent` dimming,
and in plain ink when it is healthy (at zero). The words behind it are its
`?` topic.

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

**The strip is two rows (CH-21, 2026-09-25), so each section keeps a fixed
position.** It was one row with the open section's tabs inserted inline after
their section, which moved every later section whenever one opened — §6.14's "a
position means one thing" held on the desktop column and nowhere else.

| Row | Class | Holds |
|---|---|---|
| 1 | `.ctl-rail-main` | the sections and the utility corner (API reads, `?`) — the same items in the same places on every route |
| 2 | `.ctl-rail-sub` (`role="tablist"`) | the open section's tabs, drawn only when it has more than one (Overview draws no second row) |

Each row scrolls on its own, with the `--rail-fade` mask from **one** rule
naming both rows, and each ends on the fade's width of empty space (row 1
through the utility corner, row 2 on its own). `.ctl-rail` is the column that
holds them. The open section's tabs are drawn twice by one component
(`RailTabs`): inline for the desktop column and as row 2; the sheet displays
exactly one copy at any width (`.ctl-rail-main` is `display: contents` at 900px
and up, so the desktop rail is unchanged), and the scroll-into-view of CH-14
targets the displayed copy.

**A section's selection is a rule and a tab's is a fill.** Below 900px the
selected section keeps its 2px `--text` bottom rule and loses its fill; the
selected tab takes the `--surface-2` fill in `--text` ink and no rule;
unselected tabs are `--text-faint`. The two levels had drawn a rule each and
read alike; now they differ in greyscale and need no hue — the phone case of
§1.3's "surface step plus a 2px rule", split between the two levels. At 390,
the 52px header, the two strip rows at the 44px phone target (about 97px) and
the ~32px collapsed dock are about 180px on load, around 21% of an 844px
screen; only the dock is fixed (the strip is `position: static` below 900px),
so the two-fixed-bars concern above does not arise. The sticky strip and a
"top" affordance are not part of this and stay tracked on #139.

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
| Frame | rail → the two-row strip (§6.15); `--app-pad` 16px; header keeps its height and its environment bar. **The header is one row that does not wrap at ≤560px (CH-20):** in order, the mark (the home link, named "SwarmCloud" by the mark's title; the wordmark is not drawn), the environment badge, the tenant key, the tenant id and the admin tag. Only the tenant id gives way — it ellipsizes, whole in its `title` and in a copy: **the id is its own copy control** (`.brand-id`, a button that draws nothing of its own, 44px tall at ≤560px), which copies the whole id and says in a status whether the copy landed or the browser refused it. It is the id rather than a button beside it because at 390 the id keeps only 60–70px, which a separate 44px control would take. Not drawn but still announced (visually hidden, never removed): the tenant's display name, the "signed in" key and principal, the unknown badge's host, and the word ENVIRONMENT — the unknown badge reads "env UNKNOWN", about 122px, keeping its capitals, hatch and left rule. The admin tag is a grey hairline tag (1px `--line`, `--surface-2`, `--text-dim`, the non-production badge's box), and above 560px it shares one nowrap unit with the principal, so it can never wrap onto a line of its own. Pending is the tenant key and "reading…"; failed is the tenant key and the `not read` mark, with the error heading as its accessible name. |
| Overview | one column: the lead, the fact strip as a wrapping run of facts (`styles.css` §B6.1, no grid), then the three panels stacked. *Amended by OV-6, 2026-09-25: this row said "five cards stacked; the metric strip becomes a 2-up grid", which §B6.1 had removed on purpose. Since OV-12 the strip holds two facts and they fit on one line at 390.* An account row's status note (sign in again, pool skipping, paused, draining, the binding pool) wraps to its own line under the row, only when there is one (OV-7). A paused or draining note leads the reading word rather than giving way to it (`paused · stale 3h ago`), so a stale, cleared or never-polled reading cannot hide it |
| Workflows | collapsed rows keep `[state] [id] [progress] [actions]`; the DAG scrolls horizontally inside its wrap and is **not** scaled to fit — scaling turns step names into texture |
| Agents / Holders / Timeline | `.ctl-line` on its irreducible template |
| Pools / Runtimes / Accounts / AdminSettings | `.ctl-table.is-scroll` scrolls sideways with its first column held in view (§7.3). *(Amended 2026-09-25, CH-13: the Cards-toggle promotion this row proposed was not built; the held first column is the owner's answer to the same finding.)* |
| AgentDetail | a full-screen `role="dialog"` overlay, as today below 1100px; its tables are records and stack (§7.3) |
| Dock | 28px collapsed; the page now reserves its actual height (§3.4) |

**Charts are authored twice, not scaled.** Vercel ships a 368×234 SVG with fewer
datapoints under the breakpoint and a 960×560 above, and does not render the
hover apparatus at all on a phone — there is no hover on a phone, so the
crosshair, the tooltip and the point circles are simply absent rather than made
touch-friendly.

*How this is built for the inspector charts (AG-20, owner decision
2026-09-25, §15.6):* each chart root is drawn once per entry in `DRAWN`
(`charts/parts.tsx`) — a 640-unit and a 300-unit SVG, the narrow one asking
its axis for fewer ticks — and both are in the markup, so nothing measures the
DOM and server rendering draws exactly what the browser will. The figure is a
size container (`.ctl-chart.has-narrow`, `container: ctl-chart / inline-size`)
and `@container ctl-chart (min-width: 640px)` swaps the wide drawing in. The
threshold equals the wide drawing's own width, so it is only ever scaled up
(to the 720px cap); the narrow one has `min-width: 300px`, so it is never
scaled below the width it was drawn at either. **No tick label renders below
`--t-micro`.** 640 here is a drawing's width, not the retired 640 viewport
breakpoint of §7.1.

**Touch targets are 44px at ≤560px.** Railway ships 32px icon buttons and has
taken public feedback on exactly that. The type does not grow with them.

### 7.3 One rule for tables below 900px *(CH-13, owner decision 2026-09-25)*

`styles.css` §B6.3 stacked **every** opted-in table into "Label: value" records
below 900px, while §6.7 and §6.8 said tables scroll or drop columns, and
neither document named the other. Stacked, a data table lost what it is for — a
comparison down a column — and a great deal of height: Pools was 5,924px tall
at 390 and Profile headroom 9,882px, and an attempt's gs:// uri wrapped over
nine lines. **One rule now, for every table:**

| Kind | Class | Below 900px |
|---|---|---|
| **data table** — five or more columns, read across rows (Pools, Profile headroom, Runtimes, Accounts, Pool limits, Tenants, API reads, the workflow step table) | `.ctl-table.is-scroll` / `.table-wrap.is-scroll` | scrolls sideways; the first column — the row's name — is `position: sticky; left: 0` on an opaque `--surface` (`--surface-2` in the head), so a value is always beside the name it belongs to |
| **record** — four columns or fewer, an inspector fact (checkpoints, artifacts, commits, staged inputs, a pool's counter delta) | `.ctl-table.is-stacked` | §B6.3's stacked record, unchanged: a name at lead rank, a key column from `data-label`, explicit ARIA roles |
| **a long value in a record** — a gs:// uri | `.uri` in a stacked cell, or under a record's name in its row header (the log stream's, since the 2026-09-25 sweep) | one line, ellipsized; the whole value is in its `title` and in the copy action beside it (`copy gsutil`) — cut on screen, whole on hover and in the copy (the AH-11 precedent) |

§B6.3 existed because of F6 — columns hidden behind an `overflow-x: auto` that
paints no scrollbar here, with nothing to say they existed. The held first
column answers the half of F6 that made the hidden columns unreadable, and the
column cut at the right edge says the row goes on. A louder cue (the
`background-attachment: local` scroll shadow) was tried and reverted earlier
because it blinds `spaceprobe.ts`'s border grading; that note stands in the
sheet. A row tone's left-edge rule is a background image on the held cell and
still shows; its wash shows in the cells that scroll, not under the held one.
The inspector's tables are all records, so the container-query mirror of
§B6.3 (§14.3) is unchanged apart from the `.uri` rule, which both blocks carry.
`chrome.shared.test.tsx` scans every table in the source: any of five or more
columns must be `is-scroll`, and no `is-stacked` table may have more than four.

**What a scrolling table needs to actually scroll**, each learned from a table
that did not:

- **It is sized by its content.** CP-18 gives Pools' six family tables and
  every Profile headroom table `table-layout: fixed` at `width: 100%`, with
  percentage widths on the head row, so that their columns line up from one
  table to the next. A fixed-layout table at 100% is sized to its wrapper and
  never overflows — at 390 each figure column was ~25px of content, "In use
  (units)" wrapped to three lines and figures broke mid-number, and there was
  nothing to scroll. Below 900px both are `table-layout: auto`, `width:
  max-content` (at least the wrapper), and their cells beside the held column
  do not wrap. CP-18 still holds at 900px and up; below it each table scrolls
  on its own, so there is no shared x to keep.
- **The held column has a ceiling:** `width` and `max-width` of
  `min(45vw, 20ch)`, wrapping at spaces and, for an id with none, anywhere. A
  **width**, not only a max-width, because a max-width on a table cell is
  ignored by more than one engine, and a wrapping cell with no width is
  squeezed to one character when the table overflows. Without it a held cell
  was as wide as its longest line: on API reads, a task read's concrete URL is
  ~53 characters, ~400px of mono in a 356px scrollport, and a sticky cell wider
  than the scrollport covers it at every offset — every other column scrolled
  under it, never visible. A raw-id line that is a locator rather than a name
  is cut instead of wrapped: API reads' `lastUrl` (up to 110 characters for a
  checkpoint file) is one line, ellipsized, whole in its `title`, and adds
  nothing to the column's width (`width: 0; min-width: 100%`).
- **A cell that spans the row is not the held column** (`:not([colspan])`),
  and what it holds stays in view. An expanded account's detail is one cell
  across the account table's five columns — 909px in a 358px phone — so under
  `is-scroll` its prose ran off the right edge and its controls were off
  screen until the reader panned the table. The detail is `position: sticky;
  left: 0` and one scrollport wide (`calc(100vw - 2 * var(--app-pad) - 2px)`:
  the viewport less the page's gutters and the wrapper's borders), and wraps.

`tables.scroll.test.tsx` renders Pools, Profile headroom and Accounts and asks
the cascade at 390 about the elements they drew; `chrome.shared.test.tsx`
renders API reads over a real task URL. A bare `.is-scroll` fixture is not
enough: the first one passed while Pools and Profile headroom did not scroll.

Tables not yet classified, and why: Overview's Running rows (three columns
inside a card, which fit) and the inspector's metadata key/value table (two
columns, already a record in shape) carry neither class and keep the plain
`overflow-x: auto`.

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
   **One slot for the glyph (AH-24, 2026-09-25): after the label or heading
   it explains, never after a value** — `Headroom ?`, `reads ?`,
   `never written: ?`, not `masked 4 … ?` or `● running ? ● live`. The rule
   names no exception. Where a line had no label of its own, the 2026-09-25
   pass gave the value its missing key (`state ? ● running` on the agent
   headline) or moved the glyph to the heading the line sits under
   (`Backends ?` over Runtimes' unread row; `Workflows ?` for the board's
   absent figures). `HelpCard.tsx`'s header states the same rule.
   **The topic is about what the glyph or link sits beside, and a topic linked
   from two screens is written for both** (AG-19, AH-13): the Timeline
   window's `Why →` opens `event-paging`, not `partial-read`, and
   `event-paging` names the Timeline screen as well as the attempt timeline —
   in its title too, which the Help page prints as its heading and in its
   `showing <title>` line. "One page of events, oldest first" was false of the
   Timeline, which reads newest first; the title is "Paged reads: a page of
   events, a window of tasks" and claims no order, and the topic opens by
   naming both reads. *(Superseded by #185, §16: the Timeline no longer reads
   a window of tasks, so nothing links it to `event-paging`, and the topic is
   written for the attempt timeline alone again, titled "One page of events,
   not the whole history". The rule this paragraph states is unchanged.)*
6. **`docs/`.** The argument, the constraint, the thing that is true for six
   months. A docs link is a legitimate element of an empty state and of a help
   card; it is not an element of a data view.

> **Recorded 2026-09-25 (WF-5, epic #83; corrected on #160's review): a
> figure's source is a qualifier, and where there is no room in its slot the
> node gives it a line.** A workflow step's cost or tokens may be its result
> summary's rather than its attempt telemetry's. That happens only for a
> finished step (`finishedResultOf`, the one rule for the node, the Table,
> the row's total and the inspector). The figure then carries `from result`
> (`.wf-src`: faint, micro, mono, no hue). In the Table and the inspector
> that sits beside the figure. On a graph node the value column is budgeted
> for a 20-character figure and nothing more. Sharing it, the note
> ellipsed to `…`, and the override that made room clipped wider figures
> with no mark. So the node draws `cost · tokens from result` on
> `.node-src`, a line under the four figures that names them. Every
> Figures-tier node reserves that line, and it is counted in
> `nodeHeightAt('figures')` (246). The alternative was about 84px more width
> on every node, which takes `STAGE_FITS` from 3 to 2 and draws a three-wide
> stage without figures at all. The figure's note also says what the view
> read. Only the inspector, and the board inside its sample, have read the
> attempt documents, so only they say those documents carry no typed figure.
> Outside the sample, the board says it did not read them.

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
healthy path.

**Forms too (TS-23, owner decision 2026-09-25).** The five kinds of word apply
to a form — Submit a task, Submit a workflow — with one allowance: a field, or
an offer to add one, may carry **one `--t-micro` note stating what the runner
does with that key**, read from the runner source. These are the `SUGGESTED` /
`ANY_PROFILE` notes on `.sbf-offer-note` and `.sbf-note`, kept on purpose.
Forms get **no how-to lines and no second sentence**: a step with nothing to
ask yet says `no runner chosen`, not "Choose a runner first — what it reads is
what this asks for"; an empty input says `` `mock` requires no input `` or `no
settings`; the room box states its cost as a fact on its head and keeps one
sentence; and an unread room is the unread mark with a pool count (§8.6), not a
paragraph saying it is not zero. `Overview.tsx:2086` currently renders
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
   **Done (U8, ui-followups lane).** `src/primitives.tsx` holds `Mark`,
   `Metric`, `UtilTrack`, `UtilRow` and `Absent`; Overview, AgentDetail,
   Capacity and Holders call them, and the six hand-written `.ctl-util-track`s
   are one. What each screen keeps is its policy — Overview's strip draws a
   mark in an absent tile's value slot where the run panel writes a phrase.
   Three states moved, on purpose: AgentDetail's measured zero now draws the
   baseline tick; Overview's over-ceiling profile bar draws the excess to scale
   (AgentDetail's geometry) instead of a fixed 14% stub; and Overview's empty
   states carry the mark inside the heading, with `role="status"` on the
   non-zero kinds, as AgentDetail's did. `primitives.screens.test.tsx` records
   every other state as unchanged, and `test_workflow_step_measurements.py`
   now asserts `.ctl-util-track` is drawn by `primitives.tsx` alone.
5. **Fold `OVERVIEW_CSS` into `styles.css`** and rewrite
   `test_workflow_step_measurements.py:596` in the same commit.
   **Done (U8).** The block is the last one in `styles.css`, appended so it
   keeps the cascade position the injected `<style>` gave it; the two grid
   tests, `encoding.hues.test.ts` and `stylesheet.gate.test.ts` read it there,
   and the gate now fails on any screen that injects a `<style>` again.

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

**Both halves have since landed.** The build passes `VITE_SWARM_ENV`
(`scripts/build-images.sh`), and PR #19 made `/v1/tenants/me` serve
`environment` and `environment_declared`. `Brand.tsx` now believes a DECLARED
API environment first, then the build, then a loopback host: the API is the one
source that says where the requests go, which is what the badge is for (a
laptop running the dev proxy against a deployed API used to read `Local`). A
defaulted `dev` (`environment_declared: false`) is ignored. The casing and bar
rules of §13.6 are unchanged.

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

**Amended 2026-09-25 (OV-12, owner decision): the Overview's strip carries only
facts that no panel repeats, and a fact is drawn at the figure step once.** The
strip is Running and Units held. `Account headroom` and `Token spend` left it,
because each was a panel's own figure drawn a second time; the link to Accounts
moved to the account group's head, the low-headroom alert became the Headroom
headline's verdict (§6.5), and the absent, unread and pending renderings are
the panels' own. §2 names the resulting five figure-bearing regions.

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
it right is measurable: **its rows are one line tall.** *(Corrected
2026-09-25, WF-17: this said they stack, that "`.wf-card + .wf-card` drops the
duplicated top border, so ten rows draw one box and nine hairlines". It does
not, and the board never drew that. The board draws ten one-line rows, each
`.wf-bar` its own bordered box, 8px apart — `.wf-board`'s gap is `--ctl-s2` —
and `.wf-card + .wf-card` only removes the `.section + .section` region
hairline. That is what §11.4 measured, "13 boxes", and froze; §13.3 now names
it as an exception. The run list's construction below does not rest on it: it
stands on §13.3 alone.)* The run list drew `gap: var(--ctl-s1)` between
bordered, rounded, 72px rows: six rows were six separate floating objects,
which is "everything is a bordered rounded box" in the place this product
repeats a box the most.

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

   > **Collapsed, 2026-09-25 (owner decision, CP-25, #85).** The five-cell bar
   > is gone: `Bar`, `barFilled`, `BAR_CELLS` and the `.acct-bar` rules were
   > deleted, and the 5h and 7d cells draw §6.4's shared `UtilTrack` — the
   > exact, unrounded percentage (the cells rounded 42% to three fifths), the
   > default grey fill, `ov-projected` for a stale or reset reading beside the
   > existing `~`, `is-bad` only for a live reading at 100%, and the baseline
   > tick for a measured 0%, which five empty cells could not draw. There is no
   > amber band, and no over segment because the percentage is clamped. The
   > track is a fixed 40px inline beside the figure (`.acct-window >
   > .ctl-util-track`), and at 560px and below the phone block hides it as it
   > hides every track but a pool tile's; there the figure, the `~` and the em
   > dash carry every state. Unmeasured cells are unchanged: the em dash, `not
   > measured`, no track. **What remains of the `cs status` parity is the
   > figure, the `~` and the window labels.** One disagreement is left open for
   > its own box: Overview colours the same reading warn above 75% and bad
   > above 90%, while Accounts draws bad only at 100%.

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

   > **Amended 2026-09-25 (owner decision, CP-2, #85): the scope moved from the
   > family head to every row.** The note was computed from the family's first
   > row, and a family is not one scope — an admin's Tenants family printed
   > "this tenant" above four tenants' pools, and Providers printed
   > "platform-wide" above per-tenant slices. Each family table now has a
   > `Scope` column reading `platform`, `this tenant` or `tenant X`, each card
   > in the Cards view carries the same word (`.cap-pool-scope`), and the
   > family note is removed. Trap E is held more strongly than before: the
   > declaration is on the figure's own row.

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

**Two exceptions, named so they stay two** *(the second added 2026-09-25,
WF-17)*:

1. `.ctl-table thead th` keeps a `--line-soft` bottom rule. A column head is a
   different *kind* of row rather than the next one, and it says where the data
   starts exactly once.
2. **The Workflows row, `.wf-bar`, keeps a panel's box.** It sits directly on
   the page rather than inside a panel, it opens in place into its own Graph,
   Timeline or Table canvas, and it is the owner's frozen reference (§11.4:
   "13 boxes"). So the board is ten boxed rows 8px apart, and that is correct.
   **`.ctl-line`, the expandable-row primitive, keeps the no-box rule this
   section gave it.** This is not a general rule that "rows that open in place
   are panels": that rule would bring back exactly the per-row boxes this
   section deleted from `.ctl-line`.

**Help topics are rows of their region, with no box** (AH-18): a group is the
`.section`, each `.help-topic` inside it is separated by `--ctl-s5` and draws
nothing, and a deep-linked topic takes the §1.3 selection treatment — a
`--surface-2` fill and a 2px `--text` inline-start rule declared transparent on
every topic, so marking one moves nothing. A deep link to **any** topic lands with its group
heading in view (AH-16, settled against AH-18 on #86, 2026-09-25). The group
heading is the sticky head: `.help-group > h2` sticks to the top of the
scroller while its group is in view, in the page ground `--bg`, because no
scroll margin can bring back a heading a screenful above a topic halfway down
its group. The topic's scroll margin clears that heading — its line box and
the 10px under it (padding here, so a stuck heading keeps the gap opaque) —
with `--ctl-s5` above it, and a topic sitting under the stuck heading does not
count as already in view. Nothing else sticks over the Help column.
`shell.test.tsx` "AH-16" resolves both sides on the page Help renders, and
`helpcard.placement.test.tsx` holds the in-view band.

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

---

## 14. The 2026-09-25 visual QA pass — the stylesheet's half

The QA pass of the live console at `ea1355d` (1440 and 390, light and dark)
filed its findings as boxes on epics #81–#87. Forty-two of them were the
sheet's, and this section records what each changed and the constraint behind
it, so that none of them is quietly reverted. Every one carries its box id in a
comment beside the rule, and an assertion in `shell.test.tsx`'s *"the
2026-09-25 visual QA"* block (or the file named below) that states the mutation
turning it red.

### 14.1 Why the assertions read a cascade and not `getComputedStyle`

jsdom orders matching rules by **source position alone** — its own source says
specificity "is only implemented by the order in which the matching rules
appear" — and applies no `@media` block that does not name `screen`. Four of
these defects were exactly that shape: a phone rule written ~2,000 lines
**above** the base rule it had to beat (`.ctl-seg > button`, CH-8), `.state p`
out-ranking `.checked-at` on `font-size` (CH-10), `.limit-edit input`
out-ranking `.acct-wide` on `width` (CP-19), and `.ctl-table .is-num`
out-ranking a stacked key's alignment (CH-11). `getComputedStyle` reports the
wrong winner for all four, so a test built on it would pass on the broken
sheet. `cssgate.ts` now carries `cascade`: importance, then Selectors-4
specificity, then order, with media and container conditions evaluated against
a stated width, and jsdom used only for `Element.matches`.
`stylesheet.gate.test.ts` proves it on fixtures with known answers first.

### 14.2 What moved

| Box | Rule | The constraint |
|---|---|---|
| CH-4 | `.ctl-mark.is-absent`, `.ctl-stale-mark`, `.brand-env.is-unknown` | text on a solid fill, hatch as a band (§1.5) |
| CH-5 | `.ctl-link` list + `:where(a)` | five screen-private link rules painted `--info`; they are branches of the primitive now, and an unclassed anchor is ink rather than the browser's blue or visited purple |
| CH-6, CP-23, AG-17, TS-13 | `.ctl-nav-util button.is-on`, `.acct-detail`, `.row[aria-current]`, `.dsp-option.is-on` | §1.3: selection is a surface step plus a 2px `--text` rule, never a hue |
| CH-7 | `.ctl-q-glyph { min-height: 20px }` | `:where(.app button)`'s 28px minimum beat the disc's 20px height |
| CH-8 | the touch-target block at the **foot** of the sheet | §7.2's 44px, by height where a box can grow and by an empty centred `::after` where a word or disc cannot; at the foot because a media query adds no specificity |
| CH-9 | `.ctl-q-card { box-shadow: var(--ctl-shadow-pop) }` | the token was named from this declaration and then used nowhere, so light mode got the dark shadow |
| CH-10 | `.state p.checked-at` | the micro step inside a state panel |
| CH-11, CH-12 | the stacked-record key, `.tag`/`.scope` width, identity wrap | every key left-aligned (the comment promised one edge; the cascade gave two), words as wide as themselves, identities breaking rather than cut |
| CH-14 | `scroll-margin-inline-end: var(--rail-fade)` | an item scrolled into view is clear of the fade mask |
| CH-15 | one-line crumb, `min-width: 20ch` on the age | the age changing width every 5s moved the crumb across its wrap point |
| CH-16 | `flex: none` on the dock's line and grip | only the body may shrink |
| OV-3 | `display: grid` on Overview's ≥900 util override; 19ch provenance | a grid template on a flex box is inert; 128px was 19 characters at an 11px step that no longer exists |
| OV-13 | every metric label reserves its mark | the strip slid 8–9px when the Running tile's mark appeared |
| AG-11, AG-13 | `[age]` in `ch` | at least `elapsed()`'s longest form over every state, started or not; at 1101–1200px and 390px the duration holds the track and "waiting" wraps above it, because the width would come out of the name |
| AG-15 | `.try.spent` / `.is-over` | an attempt count over its ceiling is a fault; two class names because the markup halves were written in parallel |
| AG-16 | `.row.is-head` | the head is a `.row`, so it inherits the grid and the breakpoints; only its register is new |
| AG-18 | `.lv-word` in ink | the mark carries the tone (§6.6) |
| AG-22 | `.art-md` capped at 60vh | the same slot as `.art-text` |
| AG-26 | `.ctl-drawer` is a size container; the §B6.3 stacked block is restated as `@container ctl-inspector` | see 14.3 |
| AG-27 | the phone why-line clamp | `-webkit-line-clamp` is inert without its box, orient and clip |
| AG-30, AG-31 | band `top: -18px`, z 3/4/5, `.ctl-drawer:focus-visible` | a sticky inset is from the content edge; the table head tied the band at z 1 and came later |
| AG-32 | `.row` row-gap `--ctl-s1` | a why line read as the heading of the row below |
| WF-8 | `.wf-graph { width: max-content }` | a sticky rail sticks only within its containing block |
| WF-16 | `[flags] minmax(0, 140px)`, wrapping one-line `.wf-mix` | a content-sized track in per-row grids misaligns every row that has a flag; a chip is whole or absent |
| TS-7 | `.wfb-step .sbf-offer` on `--surface` | the offers were the card's own fill |
| TS-21 | `.sub`, `.window-bar`, `.tiles`, `.tile`, `.dsp`, `.dsp-options`, `.wfb-stage + .wfb-stage` | each value moved to the nearest step of §3.1 |
| TS-22 | one rule for the "still open" segment and its legend key | the key cannot drift from the bar |
| TS-24 | `.wfb-more > summary` marker, hover, ring | the only way to narrow dependencies read as a caption |
| CP-13 | `.scope.tenant` neutral, `.cf-effect` in ink | a state hue is a verdict; metadata and hypotheticals are neither |
| CP-18 | `table-layout: fixed` on the family and profile tables | one schema, one set of columns |
| CP-19, CP-20 | the open account's fields; the fixed provider's chip | the fields stretch; the confirmation sizes to its label (`field-sizing`) |
| CP-22 | `.ctl-em { font-family: var(--font) }` | one dash width in every cell |
| AH-8 | `[aria-invalid]` border, message on its own line | the message widened the column |
| AH-19 | operand figures in a right-aligned 7ch track | a column of figures aligns like one |

### 14.3 The one mirrored block, and the test that holds it

CSS cannot put one set of rules under "this media condition **or** that
container condition". The inspector's tables need the stacked layout at 1440,
where the viewport query does not fire, so the §B6.3 block is restated as
`@container ctl-inspector (max-width: 899px)` right after it, against
`.ctl-drawer`'s own width. The alternatives were worse: making the page a
container too changes what the threshold means (the work column is not the
window) and makes the fixed overlay drawer position against the container.
**`shell.test.tsx` compares the two blocks rule by rule and declaration by
declaration**, so a change to one without the other fails by name — the
mirrored copy is held, not hoped for. The comments live only in the first
block.

### 14.4 What this pass did NOT do, and whose it is

* **The markup halves.** Twenty-two of these boxes have a TSX half in another
  lane (the class hooks, `aria-current`, `aria-invalid`, `scrollIntoView`, the
  `+N` fold, the HelpCard glyph and card). Every CSS half here is additive and
  inert until its markup lands. The class names this sheet expects are
  `.row.is-head`, `.try.spent`/`.is-over`, and `aria-current` / `aria-invalid`
  as attributes; the head row also matches structurally so the two halves do
  not have to agree on a spelling.
* **`HelpCard.tsx`'s `CARD_TITLE` no longer shouts.** It carried
  `textTransform: 'uppercase'` and `letterSpacing: '.04em'`; the shell/help
  lane (#145) removed both and emptied the pending list in
  `typescale.test.ts` that excused them, so CH-3's inline-style scan now
  excuses nothing.
* **`.scope.platform` is still an `--info` tint.** CP-13 named the tenant pill;
  whether the platform pill should be neutral too is a design question, not a
  mechanical one.
* **Nothing here was seen rendered.** The assertions prove which rule wins and
  what it says; whether 19ch holds "waiting 99d 23h" in a given font is
  arithmetic in the sheet's comments, and only a browser at 1440 and 390 can
  confirm it.

---

## 15. The 2026-09-25 QA decisions: the shared vocabulary, and the agents and inspector lane

The owner decided the QA pass's decision boxes on 2026-09-25 (#81–#87). Two
lanes record theirs here. The chrome-shared lane (#162) built the ones every
screen draws on, and this section is where a later screen finds them rather
than inventing a second answer (15.1–15.5). The agents and inspector lane
(#170) delivered four boxes on #82 and two merged leftovers, and 15.6 records
what each changed and the constraint behind it, so none of them is quietly
reverted. Each box is recorded where it lives above; the table is the index,
plus the sub-sections that have no other home.

| Box | The primitive | Where |
|---|---|---|
| CH-17 | the hue ruling: ok and info marks are grey; hue only on warn, bad, paused, live | §1.2, §1.3, §6.6, §6.7 |
| CP-14 | healthy carries no hue (ruled first; CH-17 set the primitives' grey, `--text-faint`, for marks and fills; `.tag.ok` and the Accounts success heading are words and take `--text-dim`, §6.6 and §15.7); the chip's base mark is the hollow ring, so a modifier that matches nothing draws unknown — built by the Capacity lane (#159) | §6.6, §6.7 |
| CH-22 | `stateTone`'s `ended` tone for CANCELLED, drawn as the grey flat bar (`is-info`) | §6.6 |
| CH-23 | a link's resting underline is `--line` at 1px | §1.3 |
| CH-19 | the live pulse's `.8` floor; reduced motion rests the pulse | §5.4 |
| CH-21 | the two-row phone strip; a section selection is a rule, a tab's a fill | §6.15 |
| CH-20 | the one-row phone header | §7.2 |
| CH-13 | one rule for tables below 900px: `is-scroll` for data, `is-stacked` for records | §7.3 |
| TS-4 | the outcome stack's four forms | 15.3 |
| CH-2 | the head's read age is the screen's own | 15.1 |
| CH-18 | the probe registry is keyed by route template | 15.2 |
| AG-5 | a count that was read is a plain fact: `--warn` ink above zero, plain at zero, never a seventh mark | §6.13, 15.6 |
| AG-14 | a why line is ink, and `--warn` only when a person has to act | 15.6 |
| AG-20 | an inspector chart is drawn twice, at 640 and 300 units, and its own width picks one | §7.2, 15.6 |
| AG-23 | a checkpoint or log panel is a facts strip and one table: one level of box | 15.6 (§13.3) |
| #170 leftovers | one mark per empty state; the events route pages and this screen does not follow its token | §6.9, 15.6 |

### 15.1 The head's read age is the screen's own (CH-2)

The head used to show the newest successful payload of **any** route the tab
had called, so it said "just now" beside a page that was still loading — the
frame's identity read had landed and the page's had not — and the dock a few
hundred pixels below said the same tab-wide thing. The head now shows the
newest success among the reads **the current screen** started; the dock keeps
the tab-wide view, as its label already says.

A screen is a route of the app (`canonical()`), begun in a layout effect on
every route change, so it has begun before any screen issues a read. A read
belongs to the screen that was open when it **started** (`fetch.ts`
`beginScreenReads`), so a slow read from the screen you just left cannot land
as "newest read just now" on the one you opened; a fixture read, which
registers when it lands, is attributed by its start time. The product header's
identity read passes `frame: true` and belongs to no screen. Four sentences,
each a measurement or the plain absence of one: **`reading…`** (nothing the
screen asked for has settled — CH-20's pending identity uses the same word),
**`newest read 4s ago`**, **`not read`** (every read failed), **`admin only`**
(every read met the admin gate). Help and API reads issue no reads of their
own and say "reads nothing". `chrome.shared.test.tsx` holds it on the live
path with a stubbed API: the frame's read lands, the screen's does not, and the
head says "reading…" while the dock says "newest".

**A route can show two screens: the agent inspector over the Agents list.**
The list never unmounts while the inspector opens, switches pane and closes,
and it re-reads only on its next poll — so beginning an empty scope when the
inspector closed left the head saying "reading…" beside a list fully drawn,
with nothing in flight, for up to 30s (for good, once polling had stopped on an
answer only a person can change). A route therefore has a **page** — the
screen the rail points at — and at most one **inspector** over it
(`beginScreenReads(key, page)`). The page's reads carry on across the
inspector when the page is the one already open, and start from nothing
otherwise (a screen re-entered through another page is a screen re-read); the
inspector's always start from nothing, because each pane mounts and reads. A
read the page asked for stays the page's while an inspector is open: `Screen`
runs its load inside `pageReads` when it is inside `RoutedPage` (Shell.tsx),
which App provides around the routed section, so the list's polls neither pass
for the inspector's reads nor go missing from the list's own age. With that,
"nothing settled" only ever means a screen that has just mounted, whose read is
starting; `chrome.shared.test.tsx` opens and closes the inspector against a
stubbed API and holds the head off "reading…" with no request in flight. A
fixture read cannot say which of the two asked, and counts to the inspector
when one was open — development only.

### 15.2 A route is a path template (CH-18)

The probe registry was keyed by the concrete URL, so every task anyone opened
added its own `/v1/tasks/<id>/attempts?limit=50` "route" — 13 of the 29 dock
cells on the QA screenshot — which inflated the count, shaped the p95 and made
the help text's "one record per route" untrue. `fetch.ts` exports
`route(template, params?, query?)`, which returns `{ url, template }`: each
`{name}` is substituted with `encodeURIComponent(params.name)` (or verbatim for
an `encoded()` value — the checkpoint member path, which arrives encoded
segment by segment), the query is appended, and the registry key is the
template with any literal `?…` removed. **`read()`, `write()` and
`noteFixtureProbe()` accept only that value, not a string**, so the typecheck
refuses a call that would key a probe by a URL; a hand-kept list of call sites
could not (the first draft of this decision's list missed four). Templates keep
their literal paths and placeholders because three Python seam tests read those
literals' shapes against the routers; account routes spell `{account_id}`, the
router's own name, because one of them compares exactly.

Each record carries `lastUrl`, the concrete URL of its last attempt: the dock
cell's title and the API reads table's `.ctl-sub` line under the route (the
raw-id slot of §6.7) show it, so a failure can still be traced to its task. The
"routes" count and "p95 over the last attempt of each route" are now true as
written. A route's status is the last attempt of any call to it and its age
the newest successful payload of any call to it; each panel still carries its
own age and its own failure.

### 15.3 The outcome stack is four forms (TS-4)

Timeline's outcome segments were three flat fills and a hatch, apart by hue
alone: failed against cancelled 1.01:1 in dark, succeeded against cancelled
1.08:1 in light.

| Outcome | Form |
|---|---|
| succeeded | solid `--ok` |
| failed | solid `--bad` with a 2px left rule — redesign-v2 §5.5 Tier 1's "solid fill + a 2px left rule", as written. The rule is drawn in `--surface`, the panel's own ground: a 2px cut down the segment's left edge, because it must show against solid `--bad` and only the grounds do — `--surface` 5.32:1 (dark), 12.49:1 (light), where `--bad` is 1:1, `--bad-ink` 1.28:1 / 1.68:1 and `--text` 2.83:1 / 1.26:1 |
| cancelled | flat bars — `repeating-linear-gradient(to bottom, --text-faint 0 3px, transparent 3px 5px)`, CH-22's grey "ended" bar stacked |
| open | the 45° hatch, unchanged |

Every segment and its legend key are one rule (TS-22's construction), so a key
cannot drift from its bar. `encoding.hues.test.ts` holds the four apart with
the colour stripped, holds failed to a solid fill, and holds its rule at 3:1
against that fill in both themes. *(A 60% wash under a `--bad` rule was built
first, on the reasoning that a same-hue rule cannot show on a solid fill; it
drew a day of failures lighter than the successes beside it, and was not the
form Tier 1 names. The rule changed colour instead of the fill changing
strength.)*

### 15.4 The dock's read cells (CH-17)

Twenty-nine bordered, green cards on a healthy platform. A cell is a row of the
dock's panel now (§13.3): no border, no rule, no radius, no fill, with the grid
gap grown to `--ctl-s3` because the gap is what separates cells with no edge. A
healthy cell is the grey disc with a `--text-dim` status; an admin-only 403 the
grey flat bar with a `--text-dim` status; only a warn or bad cell carries tone —
the table row's own left-edge rule (it is in those selector lists), its
triangle or diamond, and its status in full ink. The collapsed line's private
`.ctl-dock-dot` is gone; the line draws the shared `.ctl-dot`, so an expired
session is the diamond rather than the unknown ring.

### 15.5 What 15.1–15.4 did NOT verify

Nothing here was seen rendered: every claim is a rule the cascade chooses, a
DOM the components produced, or a ratio computed from the tokens. Whether the
phone header fits one row at 390 with a long tenant id, how the held first
column reads under a thumb, and how the flat-bar segments look at a one-task
height are for the next release's screenshots. So are: whether a 20ch held
column reads well on Pools and Accounts; whether the failed segment's 2px
ground-coloured cut reads as a rule at a 26px column width; and the expanded
account's width in a desktop window narrower than 900px with a classic
scrollbar, where `100vw` counts the scrollbar and the detail would overhang
the scrollport by its width (a phone's overlay scrollbar takes none).

### 15.6 The agents and inspector lane (#170)

Four boxes on #82 were the owner's to decide; the decisions are recorded on
the epic, and this sub-section records what each changed and the constraint
behind it, so none of them is quietly reverted. Two merged leftovers rode
along. Every one has a test that states the mutation turning it red, and the
red run is in the pull request.

| Box | What changed | The constraint | Test |
|---|---|---|---|
| AG-5 | The artifact viewer's `masked N` is a plain fact: no mark, no `.is-absent`, `--warn` ink above zero, plain at zero, still opening `masking-is-serve-time` | a count that was read is a measurement; the six marks are kinds of *nothing* and there is no seventh (§6.13 amended) | `artifact.masked`, `prose.runs` (re-pointed) |
| AG-14 | `.row .why` and `.why-full` are `--text`; `.is-warn` only on a line that needs a person: a **failure** (FAILED); work that **can never be admitted** until someone acts — a pool paused or set to zero, whether admission wrote it as a blocker or the task parked on `MANUAL_PAUSE`, and a spent budget (`BUDGET_EXHAUSTED`); **sign-in needed** (`CREDENTIAL_MISSING`); and a **stuck or silent worker** — in the inspector, a slot-holding task with no event for seven minutes gets `livenessOf`'s sentence as its why line (`SilentWorker`, AgentDetail.tsx). Queued, parked on quota or a provider, waiting on a dependency, and cancelled are ink | a colour on every line marks none of them; "needs a person" is the partition types.ts already keeps (`PARK_NEEDS_A_PERSON`, `needsAPerson`), and "silent" is the one `Liveness.tsx` already draws | `whyline.tone` |
| AG-20 | Every inspector chart root — peak memory, phase bars, retry lollipop, checkpoint strip, diffstat **and the token-spend line** (`TimeSeries`) — drawn at 640 and at 300 units; the figure is the container; the wide drawing only where the chart is ≥ 640px; the narrow one never below 300px. The checkpoint strip's off-page tray takes at most half of each drawing's plot and counts what it has no room to draw as `+N` | tick text at `--t-micro` rendered at 6–8px when a 640 drawing was scaled into a 400–480px column (§7.2 amended); a tray sized by its count left the 300 drawing no axis at 21 off-page checkpoints | `chart.narrow`, `inspector.charts` (every chart root in the inspector, not a list) |
| AG-23 | The checkpoint and log panels are a facts strip and one `.ctl-table` each, **three columns**: Checkpoint · Size · Age, and Stream · Size · Age. Whether a retry would restore from a checkpoint is a `resume` line under its name; a checkpoint's objects open from its Size cell as rows of the same table, with a name, a size and an age. No `.ckpt` or `.logwin` card, no `dl.kv`; `real zero`, `not measured`, `partial` and `not read` in the cells; the server's lowercase detail on its own line or after a dash. The strip's `restore` fact is what a retry would restore from | one level of box (§13.3); keep only the sentences a table cannot state — a checkpoint written then reclaimed, and a restore pointer a resume would ignore | `runfiles.flat`, `checkpoint.reclaimed` |
| leftover A | `Agents`, `AttemptTimeline`, `Capacity`, `Holders`, `Runtimes`: the second `real zero` in each empty state removed | one mark per empty state (§6.9 amended) | `emptystate.onemark` |
| leftover B | Every string that said the events route "returns no page token" now says this screen reads one page and does not follow the token | `GET /v1/tasks/{id}/events` has returned `next_page_token` since #19; the limit is the screen's (help topic `event-paging`) | `eventpaging.strings` |

**Where the decisions and the code did not line up exactly**, recorded rather
than smoothed over:

* **AG-5 names "the credential-names help topic".** The `?` beside `masked N`
  opened `credential-names-not-values` when the box was filed; AG-19 (#152)
  moved it to `masking-is-serve-time`, because the credential-names topic is
  about how the runtime catalogue names secrets, not about a value found in an
  artifact. The two decisions cannot both hold for one `?`. The count keeps
  its link to the masking topic, which is the topic about these credentials,
  which needs the owner's confirmation; pointing it back would undo AG-19's
  fix.
* **AG-14 names "stuck or silent workers".** `whyAgent` writes no line for a
  task that holds a slot, so the first pass had nothing to colour. The
  inspector reads the task's events, and now writes `livenessOf`'s `silent`
  sentence as a `--warn` why line. The **Agents list** cannot: a row is a task
  document, and a worker's heartbeat is written to its lease, which no
  tenant-scoped route serves (the admin leases route is the only reader). That
  half is a backend change, #179, and the list draws no line for a silent
  worker until it lands. The list also counts `MANUAL_PAUSE` and
  `BUDGET_EXHAUSTED` parks as "can never be admitted": nothing ends either one
  but a person, exactly as for a pool paused or set to zero.
* **AG-23's log age.** The route serves no per-object time (#172 asks for
  it), so a `final` stream's age is its attempt's end — when the worker
  uploads it — and the em dash with `not measured` when that end is not
  recorded. The word `live` is shown only while the attempt has no end **and**
  the task holds a slot. `source=auto` also serves the live tail when the
  final log is absent, which is what a worker killed before its upload leaves
  behind, and a quota-parked attempt never records an end; either tail is the
  em dash with `partial` — the last tail published, not a stream still being
  written. The first pass wrote "`live` for a tail still being written" here
  and called every tail `live`.
* **AG-23's restore fact.** It printed the pointer, or "a retry starts from
  the beginning" when none was set. The worker tries the pointer and, when
  that resolves to nothing, restores the newest committed checkpoint it can
  read (`_restore_checkpoint` → `find_latest`), so the fact is now `newest
  committed` and the listing's newest resumable row — marked `partial` where
  the listing is cut or a newer row is committed but not resumable, and `not
  read` where a manifest could not be read.

**What this pass did NOT verify.** Nothing here was seen rendered. The tests
prove which drawing the sheet picks at a stated container width, which marks
the cells carry and which rule wins; whether the 300-unit drawings, the
tray's `+N` and the object rows read well at 390 and 1440 in each theme is
for the next release's screenshots.

### 15.7 The consistency sweep (#85 and #87 follow-ups, 2026-09-25)

Three follow-ups the epics recorded after the decision PRs merged, one line
each. Each is held by a test committed red first.

* **CP-14 / CH-17, the last healthy greens.** `.pool .ctl-track > i` is
  `--text-faint`, the primitives' ok grey; an ok `.pool.prov` card draws no
  edge rule, because a grey 3px rule is `.pool.prov.unknown`'s and would draw
  a provider nobody read as one that is fine; `.state.acct-ok` is the plain
  `.state` box; its heading and `.tag.ok` are words, so they take
  `--text-dim`, §6.6's word grey. The follow-up's `--text-faint` is for marks
  and fills (the primitives), not words; this was clarified on #85 after a
  first cut painted both words `--text-faint` (`encoding.hues.test.ts`, one
  case each for the fill, the heading and the tag). So `.tag.ok` stays one
  step apart from `.tag.unknown`, and the success heading matches its own
  paragraph (`.state p` is `--text-dim`).
* **CH-13, the log stream's uri.** Under the stream's name in the row header,
  it takes §7.3's long-value rule — the rule names `th[scope='row'] > .uri`
  beside `td[data-label] > .uri`, in both copies of the stacked block — and is
  whole in its `title` and in a `copy gsutil` that copies `gsutil cat <uri>`
  (`runfiles.flat.test.tsx`).
* **The workflow node between attempts.** `stepDuration` gives a step whose
  earlier attempt ran, and which is not STARTING or RUNNING, its state word
  and no figure — `elapsed()`'s wording for the same task (#145) — because
  its age includes that run and nothing records when its current state began
  (`workflow.views.test.tsx`).

---

## 16. The Timeline as an outcome ledger (#185, owner decisions 2026-09-25)

The Timeline read the newest 200–2,000 tasks and labelled whatever span they
happened to cover ("bound by rows, label by the span"), because the task list
could not filter by time. Dev's 730 tasks already exceeded the default window,
so the page was partial on the day it was redesigned, and one column stacked
outcomes by `completed_at` on top of still-open work by `created_at`, so its
height measured nothing: 416 cancels flattened 28 failures. The owner accepted
the synthesis of a three-concept design round with one change (a throughput
lane), and the page now reads `GET /v1/outcomes` over a real span. This
section records what each part must keep, so none of it is quietly reverted.

| Part | What it keeps | Why | Test |
|---|---|---|---|
| **The span** | 24h · 7d · **14d (default)** · 30d · 90d · from–to, applied by the server; the server buckets (hour ≤ 48h, day ≤ 60d, week beyond) unless overridden; month only at ≥ 60 days; hourly never past 2,000 buckets | the Rows control's premise (the list cannot filter by time) is gone; a combination the route refuses (422) is one the page never sends | `outcomes.view`, `activity.timeline` "the read" |
| **The address** | every filter is the hash's query (`#work/timeline?span=30d&table=1`); the last view is remembered per browser inside try/catch; a filter change is a route change with `replaceState` | a link reproduces the page; `fromHash` splits the query off first, because `timeline?span=30d` otherwise matched no tab and opened an **agent drawer** named after it | `route.test.ts` #185 |
| **One figure** | the success rate at `--t-figure`, `k of n decided`, the Wilson 95 % interval **printed beside it** (`95 % interval 86.8–93.5 %`) as well as in its accessible name, `excludes N cancelled` in the card-note; `no finished work` (never 0 %) at a measured n = 0; the partial mark and `k of n days` when a bucket is unread; **the not-read mark and no digit when no bucket was read**; the previous-span delta **dropped, with the reason**, when that span is partial or decided nothing | cancels are left out of the denominator (owner decision); a delta over part of a span compares two different things; an interval only in an aria-label is one no sighted reader sees, and under Table the readout that also carries it is not drawn | `activity.timeline` "the headline" |
| **Four lanes, one axis** | rate (`--series-1`, y fixed 0–100 %, Wilson band on `--surface-2` with ruled edges, hollow under 5 decided, a gap at n = 0); decided (succeeded up in TS-4's solid `--ok`, failed and dead-lettered down in solid `--bad` with the 2px `--surface` cut, ≥ 4px, one scale through zero); cancelled (its own scale, max printed; requested and other as TS-4's flat bars, the cancels a failure caused as a 1px `--text-dim` outline); throughput (submitted as a `--series-2` step line over finished as `--series-5` columns, **labelled as the only lane on the submission-time basis**) | small multiples, never a dual axis; failures hang from a common zero; cancels stop drowning them; throughput answers "are we keeping up" | `activity.timeline` "the ledger" |
| **Every bucket** | a column per bucket from `since` to `until`; a measured zero is the axis tick in each lane; the current bucket has a dashed right edge and reads `so far`; a just-ended one reads `settling`; an unread bucket is one hatched band across all four lanes with no mark and no digit | an absence is never a zero, and a zero is never an absence (§8.6) | same |
| **Drawn three times** | 1080, 640 and 300 units, each with its own geometry and label stride; `.ol-chart` is the size container; `@container ol-chart (min-width: 640px / 1080px)` shows the drawing whose authored width the box holds; the narrow drawing's column floor is 26px, past which it grows and scrolls, opening at the newest end | AG-20's mechanism plus a page-width entry: the 640 drawing scaled across a 1,144px column would carry ~21px ticks, and scaled into a phone ~6px ones | `timeline.ledger.rules` |
| **The scale does not scroll** | each drawing is a positioned frame of three layers: `.ol-gutter` (an SVG as wide as the left margin, every tick), the lane labels (HTML over the plot's left edge, on `--bg`, `pointer-events: none`, one line), and `.ol-plot`, the only layer in the scroller; each drawing has its own scroller, opened at its newest end the first time it is shown | wireframe_390 pins `100┤ … 0┤ … ok ┤ … cx ┤` with older days behind the fade. Drawn as one SVG inside the scroller, the ticks scrolled off on open (at 390 and 14 days the plot opens 52px in) and, at 24h or 30d, every lane label with them, the throughput lane's `by created_at` among them | `activity.timeline` "keeps each lane's scale…", `timeline.ledger.rules` "pins the scale column…" |
| **The readout is the legend** (TS-9) | span totals by default, one bucket's on hover, tap or focus; `all`, Escape and leaving restore; one tab stop with a roving tabindex starting on the newest bucket; not a live region; 44px ‹ › steps on a phone; `zoom to <day>` with a chip back; `N failed that day →` says `not limited to <day>`. **One number per key, and it is the number the key's mark draws**: the flat bars' key prints requested + other, the outline's after_failure + workflow_sweep (`incl. workflow sweep N`), and the cancelled total, which no single mark draws, stands unkeyed; the column names and the Table's `requested or other / after a failure` use the same two sums. The span's totals carry the partial mark and `read of n` when a bucket is unread, and the not-read mark with no count when none was | the SVG marks are `aria-hidden`; each column is an HTML `role="img"` named with its full time and every count. The readout printed 5 beside the outline on 22 Sep while the outline, the column name and the Table said 8 | `activity.timeline` "the readout" |
| **Table** | the same buckets as a `.ctl-table.is-scroll`, with `submitted` | the keyboard and screen-reader route to every value | "the Table toggle" |
| **Eight cards** | Why tasks failed (the server's fixed class order) · Retries and attempts (admissions, not runs) · Time to result by profile (no all-profiles row) · Reliability by profile, tenant or person · Workflows that failed, and where (its step composition in the ledger's TS-4 forms, succeeded solid `--ok`) · **Reported cost · not a bill** (renamed from "Token spend"; per attempt, by task end) · Not finished yet (live, span not applied) · Why tasks were cancelled | every card but the live one draws the same payload on the same basis under the same filters | "the eight cards" |
| **Every card says what it covers** | the route sums every card over the buckets it READ. One unread: each card-note carries the partial mark and `read of n <unit>` (§8.6's coverage note; the card head wraps rather than overflow), and an empty list is the partial empty state `… in the 13 of 14 days read`, never `a real zero`. None read: every card draws the not-read mark and the reason, and **no digit** — no count, no real-zero tick, no `$`, and no lane prints a `max` | a zero summed over no bucket is not a measurement; drawn as the real-zero tick it is the absence-as-zero this system exists to prevent | `activity.timeline` "partial and not read" |
| **The live card carries its age** | "Not finished yet" prints `read <age>` from its own `/v1/stats` read (never `now`), is re-read on every filter change and on the Agents screen's idle cadence (`IDLE_POLL_MS`) while the page is visible, keeps its last counts between reads, and names every set filter it does not apply: `not filtered by profile, tenant, person, kind` | the head's age is the ledger's `generated_at`, a different read; left open 40 minutes and then filtered, the card said `now` over 40-minute-old counts that included the tenant the toolbar had just excluded | "the eight cards" |
| **Strips only where they fit** | a mini strip is 6px a bucket beside a row of 220px (name ≥ 104 · track ≥ 48 · count 44 · three 8px gaps); the card body is the `ol-card` size container and each strip carries its band (`is-n14` … `is-n60`), shown at ≥ 900px once the body is 220 + 6 × band wide: 304, 364, 406, 490, 580px | at 1440 the cards are 3-up and a body is ~329px: a 24-hour or 30-day strip spilled 20–70px over the card's border into the next card; 14 days fits | `timeline.ledger.rules` "a card's mini strips…" |
| **One glyph** | the only `?` is after "Success rate"; the cards publish their topics through `explain` (a `HelpNote`, no glyph) and the screen ends in a `Reading this screen:` index | B7.4's ceiling is twenty glyphs and two per screen; the console was at nineteen | `tests/help.test.ts` |

**The throughput lane's encoding is recorded as chosen, not as decided.** The
Flow concept drew `submitted` as the 1px outline column that lane 3 already
uses for "after a failure" cancels, so one mark would have meant two things on
one chart. The contract's recommended option — a `--series-2` step line over
neutral `--series-5` columns, because these are counts and not outcomes — is
what shipped; it is surfaced on #185 for the owner's confirmation.

**TS-4's forms on an SVG.** The segments of §15.3 are CSS backgrounds, which an
SVG shape cannot take, so the ledger restates them as fills (`.ol-m-ok`,
`.ol-m-bad` with `.ol-m-cut`, the `.ol-flat` pattern) with the same tokens. The
legend keys and the cards' outcome tracks (`.ol-k.is-*`, `.ol-meter > .ol-seg.*`)
are added to TS-4's own rules by selector, so those cannot drift.

**What retired with the window.** The Rows control and its rule; the window
bar's `Last N tasks` / `All N tasks`; the People table (grouping by person is
now server-side over the whole span, in Reliability); the metric strip; AG-19's
link from the window to `event-paging`, whose topic is written for the attempt
timeline alone again (§8.4 item 5 describes the state before #185). The old
Activity rules in `styles.css` (`.window-bar`, `.chart .col`, `.stackcol`,
`.chart-legend`) are no longer rendered by the Timeline and are kept: TS-4's
failed and cancelled rules are shared with `.wf-meter`, and the rest are held by
tests that read the sheet (`shell.test.tsx`, `timeline.submit.rules.test.ts`,
`test_workflow_step_measurements.py`), which a removal would have to re-point.

**How the tests were proven red.** The first test commit (run 36190161144)
failed at `tsc`, so it proved no assertion. The ledger's honesty rules were
then broken one line each in a MUTATION commit (0.0 % for nothing decided in
the headline and the Table, no partial mark, an unread bucket drawn as zeroes,
no real-zero tick) and the Timeline's tests went red on exactly those five in
vitest; the review fix-up's cases were pushed before the code that satisfies
them and went red in vitest too. One of those, the live card's `not filtered
by tenant`, went red on a gap in its fixture before its assertion, so a second
mutation dropped the disclosure and the fixed case went red on the assertion
itself. The pull request names every run.

**What this did NOT verify.** Nothing here was seen rendered: the tests prove
which marks the ledger draws for a contract-shaped payload, which drawing the
sheet picks at a container width, and which rule wins. The route itself is
built in a parallel lane; the UI is held to the contract by a fixture in its
exact shape (`outcomes.fixture.ts`), not by a live read. Whether the 1080
drawing reads well at 1440, how a 26px column reads under a thumb, and whether
the Wilson band's ruled edges are enough in the light theme are for the next
release's screenshots.
