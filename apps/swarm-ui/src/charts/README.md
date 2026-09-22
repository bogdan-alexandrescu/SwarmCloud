# The chart layer, and the one rule it exists to enforce

`TimeSeries.tsx` is the only module in this application that imports a
charting library. Screens import `TimeSeries`; a test fails if anything else
reaches past it.

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
