# The chart layer, and the one rule it exists to enforce

`TimeSeries.tsx` is the only module in this application that imports a
charting library. Screens import `TimeSeries`, or a chart built on the three
primitives it exports (see "The inspector charts" below); a test fails if
anything else reaches past it.

## Why a wrapper and not a chart per screen

This console's best property is that it distinguishes an **absent measurement**
from a **measured zero**, and says which in words:

> `TOKEN COST — not reported. No attempt reported a cost. This is an absent
> measurement, not $0.00.`

two tiles along from a checkpoint count of `0` rendered as a **digit**, because
that zero was measured.

Every charting library takes `number | null` and draws the null as a gap, as a
zero, or as a line straight through it. All three are claims this platform
cannot support. The absence therefore never reaches the library: `ChartPoint`
(`series.ts`) is a discriminated union whose absent arm has **no `value` field
at all**, so there is no number to hand a scale and the compiler rejects the
code that would invent one.

Four things are made structurally impossible, in one place:

| Rule | How |
|---|---|
| An absent point drawn as 0 | The absent arm of `ChartPoint` carries no value. A screen that holds a `number \| null` must call `reading(at, label, value, why)`, whose `why` is a **required** parameter — the sentence explaining the null has to exist before the series can be built. |
| A line interpolated across an absence | `defined={(p) => p.measured}` is hard-coded on the line. There is no `defined`, `connectNulls`, `data` or `children` prop on `TimeSeries`. |
| An axis implying a range the data does not support | The value domain is the measurements (plus a zero anchor when the caller declares one). **No `.nice()`**, which rounds the top of an axis out to a number nobody recorded. With nothing measured there is no domain, so no axis is drawn at all — the component renders the sentence instead. |
| A total or average over a partial series, unqualified | The coverage line is rendered by `TimeSeries` from the same `Coverage` object the total comes from. There is no way to ask for the total alone. |

A measured zero still renders as a zero, and is separated from an absence by
**shape and position**, not by colour: a hollow dot sitting on the zero rule
versus a hatched band spanning the plot with no dot at all. That survives a
greyscale screenshot pasted into an incident channel, which colour does not.

## The library: visx 4.0.0, and what it was chosen against

`@visx/scale`, `@visx/shape`, `@visx/axis`, `@visx/group`.

Measured on this machine with the app's own esbuild, minified ESM, gzip -9,
against a react + react-dom baseline of **44.5 KB** (the audit's baseline was
44.6 KB, so the method agrees):

| Candidate | gzip-9 over baseline | Runtime dependencies |
|---|---|---|
| **visx 4.0.0** (the four packages above) | **+28.1 KB** | `@visx/vendor` (d3-scale, d3-shape, d3-array…), `classnames` |
| recharts 2.15.4 | +107.8 KB | 8, including all of `lodash`, `victory-vendor` and `react-smooth` |
| @nivo/line 0.99.0 | not measured | 12, including `@react-spring/web` (an animation runtime) and nine sibling `@nivo/*` packages |

**Why visx.** It is per-package, so only the four are bundled and nothing pulls
a barrel import; it is pure React SVG with no DOM measurement, so it renders
under `renderToStaticMarkup` and under Vitest's jsdom, where layout is 0×0; its
`LinePath` exposes d3-shape's `defined` accessor, which is the one capability
this product cannot do without; and it has **no default empty state** — it
draws exactly the marks it is given, so there is nothing to override.

**Why not recharts.** Measured, not asserted:

* `ResponsiveContainer` under `renderToStaticMarkup` emits
  `<div class="recharts-responsive-container" …></div>` and nothing else. The
  responsive path — the one every example uses — is untestable by rendering,
  in the runner this repository actually has.
* 3.8× the bundle, for a chart that is a line and two axes.
* Its honesty is a **per-call prop**. `connectNulls` defaults to `false`, which
  is right, but the next chart can set it to `true` and nothing stops it. This
  wrapper's job is to make that unreachable.

To be fair to it: with a fixed width and height it does break the line
correctly. `M65,5L141.67,65M295,25Z` — two subpaths, the null not drawn. The
rejection is about bundle, dependencies and the responsive path, not about that.

**Why not nivo.** Twelve runtime dependencies including an animation runtime,
against a 28 KB alternative that does the job. Not measured; the dependency
shape settles it.

## Where this disagrees with the audit, and why

`docs/web-ui/ui-audit-and-build-prompt.md` §B5 recommends `d3-scale` (+12.6 KB)
with every mark hand-rolled, and rejects visx on the strength of a
`<BarStack>` rendering: given a null key, the unknown segment gets `height="0"`
and its neighbours abut, so the bar silently shortens.

That finding is correct and it is **about stacking**, not about visx. No
stacking function can give an absence an extent, because an absence has no
value to hand it — the audit says so itself. It does not transfer to a line,
where `defined` breaks the path and the absence is drawn as its own mark.

The owner's decision of 2026-09-22 was a charting library over hand-rolled SVG.
visx is `d3-scale` — literally, via `@visx/vendor` — plus typed React marks and
an axis component, so this is the audit's own arithmetic inside the owner's
decision rather than against it. The audit's §B5 rule still stands for stacked
bars, and nothing here stacks.

## The inspector charts, and the three primitives they share

Four more charts draw in the agent inspector (`AgentDetail.tsx`). None of
them imports visx. They reach it through three exports of `TimeSeries.tsx`,
which stays the only module that imports the library, so the guard test's
claim is unchanged:

| Primitive | The rule it carries |
|---|---|
| `linearScale(extent, range)` | The domain is an `Extent` built from measurements; no `.nice()`; a degenerate extent collapses to the middle instead of being padded to a span nobody recorded. |
| `ValueAxis` | Tick ends are the extent itself. `keep` forces a value (admission, zero) and `minGapPx` drops an interior tick before it lets two labels collide. |
| `StepLine` | `defined` hard-coded, over `ChartPoint`s, so an absence breaks the stroke. The step corners come from `stepAfter` in `series.ts`, which inserts a corner only between two measured neighbours. |

There is still no bar, stack or area primitive. A bar is a plain `<rect>`
between two **recorded** instants, drawn by the chart that owns the data. The
audit's §B5 finding (a stacked bar gives an unknown segment height 0 and the
bar silently shortens) stands, and nothing here stacks.

| Chart | Viz (redesign-v2 §4) | Where | What it must not imply, and how it is prevented |
|---|---|---|---|
| `AttemptPhases.tsx`: phase bars | #3 | Attempts panel | That a missing end is an end. Every segment is drawn between two recorded instants measured from **admission**, the attempt's `lease_acquired` event, so nothing's position depends on another segment's length. A segment with no recorded end is **open**: a wash up to the newest instant at which the phase was demonstrably still going, then a chevron. It is never closed at "now" and never at a guess. An attempt is over, and its open segment not live, when `completed_at` is set, a later attempt exists, the task is terminal, **or the task no longer holds its lease** (`attemptEnd` in `duration.ts`, shared with the attempt cards). |
| `AttemptPhases.tsx`: retry lollipop | #15 | same figure | That `task.started_at` is the task's start (it is overwritten per attempt). One stem per attempt, and an open attempt is an arrowhead at its lower bound, not a dot at a value. |
| `AttemptPhases.tsx`: the sum | #15 | same figure | That a total covers attempts it does not. The line prints the total with `k of n` from the same `WorkSum` object, where `n` is `task.attempt_count` and not the number of documents the read returned; the shortfall is printed as "not returned". An open attempt's lower bound is printed after the total and never added into it. |
| `PeakMemory.tsx` | #4 | each attempt's resources | That it is memory in use now. Titled "peak reached by T+n", drawn as a step that holds each reading until the next. The end-of-attempt figure is a separate diamond and the line does not run to it. |
| `CheckpointStrip.tsx` | #6 | above each attempt's checkpoint table | That a hollow dot is a broken checkpoint, or that a checkpoint with no event has a time. Dot **area** is bytes; hollow is "location not on this page"; a checkpoint whose event is off the page goes in a tray beside the axis, not on it. |
| `Diffstat.tsx` | #7 | above the commit table | That a binary change is 0/0. A binary-only commit gets a diamond and no zero mark; a non-numeric count is hatched, not zero. |

The tables these sit above are unchanged. Each chart adds what its table
cannot show; each table keeps the facts as text a reader can copy.

**Three measured decisions worth keeping:**

* **Admission is the `lease_acquired` event, not `attempt.created_at`.** The
  scheduler writes admission into `created_at`, and then the worker's
  `record_attempt_start` (agent-worker `control.py`) replaces the whole
  attempt document with a non-merge `.set()` that writes `created_at =
  utcnow()` beside `started_at = utcnow()`. On every attempt whose worker
  started, `created_at` is the start. Read as admission, it drew §9's run as a
  recorded 0s cold start and a three-minute "queue". The document's
  `created_at` is used only for an attempt that never started, the one case
  in which nothing rewrote it. A started attempt whose `lease_acquired` is off
  the event page has its queue and cold start hatched, and its run -- still
  measured -- is not placed on the admission axis at all, because its
  distance from admission is the unknown cold start. The rewrite is reported
  to Track B (it also clobbers `execution_name`,
  `docs/web-ui/04-live-logs.md`); if it becomes a merge that leaves
  `created_at` alone, the document is admission again.

* **T+ is measured from `attempt.started_at`, not from the heartbeat's
  `elapsed_seconds`.** That field is the *agent child's* clock, and before the
  child exists the worker writes `0` (`lifecycle.py`, the `if self._child else
  0` in `_heartbeat`). A heartbeat taken during clone or restore would sit at
  T+0 whenever it was taken. The event's `at` minus `started_at` is a real
  position for every reading, and it is the origin the checkpoint strip uses.
* **An open segment's lower bound comes from evidence of life, not from the
  newest event.** A reconciler reclaiming a dead worker writes its event
  minutes after the worker stopped. `duration.ts` bounds a run only by events
  a live worker emits (`running`, `heartbeat`, `checkpoint_*`) and a cold
  start only by the scheduler's `lease_acquired` and `dispatched`.

**Every inspector chart is drawn twice, not scaled (AG-20).** Each root --
the phase bars, the retry lollipop, peak memory, the checkpoint strip, the
diffstat and `TimeSeries`' token-spend line -- is rendered once per entry in
`DRAWN` (`parts.tsx`): a 640-unit and a 300-unit SVG, each with its own
scale, its own hatch pattern and its own tick count (the narrow one asks for
fewer). The token-spend line was left out of the first pass and stayed one
scaled 640-unit SVG, with its ticks at about 8.4px in the default inspector;
`inspector.charts.test.tsx` now counts every chart root the inspector mounts
rather than a list of the ones known to be fixed. The checkpoint strip's
off-page tray is bounded by the width it is drawn at (half the plot at most,
the rest counted as `+N`), so neither drawing's time axis can be squeezed to
nothing. Both are in the markup; the figure is a size
container (`.ctl-chart.has-narrow`) and `@container ctl-chart (min-width:
640px)` in styles.css shows the wide one only where the chart is at least as
wide as it was drawn, while the narrow one has a `min-width` of its own
width. So neither is ever scaled down, and tick text never renders under
`--t-micro`. Before this, one 640-unit drawing was scaled into a 400-480px
inspector column and its ticks rendered at 6-8px. `chart.narrow.test.tsx`
holds the pairs, the tick counts and the sheet's switch; a test that counts a
chart's marks reads them off one drawing (`svg.is-wide`).

**Every chart root is `role="group"`, never `role="img"`.** An image's
children are presentational, so the per-mark sentences (an open segment's
lower bound, an absence band's reason, an off-page checkpoint's "not
evidence that it is broken") would be replaced by one label for the whole
chart. They are the accessible route to the explanations the owner moved off
the glass, so they must stay reachable. `inspector.charts.test.tsx` asserts it
for all six chart roots, `TimeSeries` included.

The event page these read is now requested at `limit=200` (`api.ts`,
`EVENT_PAGE_LIMIT`), and the attempt documents likewise (`ATTEMPT_PAGE_LIMIT`).
Before this, the run screens asked for no limit and got the API's **default**
page of 50, not its maximum of 200 -- for attempts, the 50 NEWEST.

## The tests

* `src/__tests__/honesty.chart.test.tsx` — the four rules, read back off the
  rendered SVG: the `d` attribute of the line, the `cy` of a dot, the tick
  labels the axis emitted.
* `src/__tests__/chart.tokenspend.test.tsx` — the real chart on the live
  deployment's shape of data (thirteen attempts, six with no cost, four
  different reasons), plus the one source-text assertion in the layer: that no
  other module imports the library.

Each was proved by mutation — the fix undone, the named test watched going red,
the fix restored. The mutations are listed in the commit message.

The inspector charts' tests, each read back off the rendered SVG:

* `src/__tests__/chart.phases.test.tsx` — every fixture in production shape
  (`created_at == started_at` for a started attempt, admission as its
  `lease_acquired` event). §9's measured run (3m 09s of cold start drawn about
  ten times 18s of run, a 2s queue, the `dispatched` cut); a started attempt
  with no `lease_acquired` on the page drawn with an absent cold start and an
  unplaced run, never a recorded 0s; an open run bounded by its newest
  heartbeat and unchanged when the clock moves a day; a reclaim event NOT
  taken as evidence of life; a parked attempt, a failed dispatch and a
  reclaim before its replacement all read as over; an absent retry queue that
  moves no other segment; a retry queue started at its `ready` event rather
  than the previous attempt's end; an open lower bound kept out of the total;
  "over 2 of 3" when the task counts three attempts and two came back.
* `src/__tests__/chart.peak.test.tsx` — only horizontal and vertical strokes,
  two subpaths around a missing reading, the at-exit figure not joined, and a
  reading placed by its `at` rather than the zeroed child clock.
* `src/__tests__/chart.checkpoints.test.tsx` — area, not radius; hollow for
  no location; an off-page checkpoint in the tray and never on the axis.
* `src/__tests__/chart.diffstat.test.tsx` — a binary-only commit never wears
  the zero mark; a non-numeric count is hatched; both sides share one zero.
* `src/__tests__/inspector.charts.test.tsx` — each chart is mounted on the
  real `Run`, above the table it does not replace; a parked attempt is not
  called running on its card or in the phase chart.
* `src/__tests__/api.events.test.ts` — the events read and the attempts read
  ask for `limit=200` on the live path.

These were proved the way CLAUDE.md asks: the defect committed on the branch
and the named tests watched going red in CI, then reverted. The run ids are in
the pull request.

## The outcome ledger (`OutcomeLedger.tsx`, #185)

The Timeline's chart: four aligned lanes on one time axis — success rate,
decided work, cancels on their own scale, and throughput — over the buckets
`GET /v1/outcomes` serves. It imports no charting library, so the guard above
is unchanged: every mark is a `<rect>` or a polyline between two values the
server measured, on a scale whose domain is those values with no `.nice()`,
and "not read" is the shared `HatchDef`.

What it must not imply, and how it is prevented:

| Rule | How |
|---|---|
| A bucket with nothing decided has a 0 % rate | the rate lane draws no point where `rate` is null, and the line breaks there; the y scale is fixed at 0–100 % |
| One of two reads as an outage | a point under five decided is hollow, and the Wilson band is drawn behind the line |
| An unread bucket is a quiet one | one hatched band across all four lanes, no mark, no digit, and its column is named `not read` |
| An empty bucket is missing data | a measured zero is the axis tick in each lane |
| 305 cancels hide 8 failures | cancels have their own lane and scale, with the max printed; failures hang from the decided lane's zero, never under 4px |
| Arrivals share the outcomes' time basis | the throughput lane's label says it is the only lane on the submission-time basis |
| A scale scrolls away from its lane | each drawing's ticks are in a gutter SVG and its lane labels are HTML over the plot's left edge, both outside `.ol-plot`, the one layer that scrolls; the plot's SVG keeps the drawing's coordinates (its viewBox starts at the left margin) |
| A legend key names one mark and prints another's number | the flat bars' key prints requested + other, the outline's after_failure + workflow_sweep; the cancelled total is unkeyed |
| A span's totals read as whole when a bucket was not read | the readout's `all N days` carries the partial mark and `read of n`, and with nothing read the legend is the not-read mark with no count; no lane prints a `max` over no bucket |

**Drawn three times, not scaled** (§7.2): 1080, 640 and 300 units
(`LEDGER_DRAWN`), each with its own geometry and label stride; the figure is
the `ol-chart` container and the sheet shows the drawing whose authored width
the box holds. The narrow drawing's column floor is 26px, past which it grows
and scrolls from the newest end. **The SVGs are `aria-hidden`**; over each sits
one HTML column per bucket, `role="img"`, named with its full time and every
count, and the columns are one tab stop with a roving tabindex. The roots of
the drawings' pick layers are `role="group"`, as every chart root is.

Tests: `activity.timeline.test.tsx` (the marks, the names, the readout and the
keyboard, on a payload in the contract's exact shape), `timeline.ledger.rules.test.ts`
(which drawing the cascade shows at a container width, and TS-4's forms), and
`outcomes.view.test.ts` (the axis labels in the server's zone).
