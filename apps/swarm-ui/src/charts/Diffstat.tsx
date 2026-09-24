// The work an agent produced, commit by commit (redesign-v2 §4 viz #7).
//
// Diverging bars on ONE axis whose zero is "no lines changed": deletions to
// the left, insertions to the right, one row per commit, in the order the
// worker harvested them. The commit table under this chart is not replaced;
// it carries the subjects and the exact counts as text.
//
// THE TWO THINGS THIS CHART MUST NOT SAY (redesign-v2 Panel 2):
//
//   * A BINARY CHANGE IS NOT 0/0. git prints `-` for both line counts on a
//     binary file, and `binary_files` exists precisely so that change does not
//     read as "changed nothing". A commit whose only changes are binary gets NO
//     zero mark -- the hollow ring on the zero rule is this product's mark for
//     a measured zero, and "changed nothing" is exactly the misreading -- and
//     it gets a diamond with the binary count instead. A commit with both gets
//     its bars AND the diamond.
//   * A COUNT THAT IS NOT A NUMBER IS NOT ZERO. `result_summary` is an untyped
//     Firestore dict; `GitCommit` describes what `_harvest_git` writes, not
//     what the field is guaranteed to hold. A row whose counts are missing or
//     not finite is hatched across the whole plot -- no bar, no zero ring.
//
// `patch_omitted` is not this chart's to state: the `patch` fact above the
// table already says "discarded at N bytes" in its own encoding, and nothing
// here draws or implies a patch.

import type { GitCommit } from '../types'
import { ChartTitle, HatchDef, useHatchId } from './parts'
import { ValueAxis, linearScale } from './TimeSeries'

const W = 640
const M = { top: 4, right: 104, bottom: 20, left: 80 }
const ROW = 16
const BAR = 10

function count(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

/** One commit's counts, each checked rather than trusted. */
export interface DiffRow {
  readonly sha: string
  readonly ins: number | null
  readonly del: number | null
  /** Null when the field is missing: unknown, which is not "no binary files". */
  readonly bin: number | null
}

export function diffRows(commits: readonly unknown[]): DiffRow[] {
  const rows: DiffRow[] = []
  for (const c of commits) {
    if (typeof c !== 'object' || c === null) continue
    const g = c as Partial<GitCommit>
    rows.push({
      sha: typeof g.sha === 'string' ? g.sha : '?',
      ins: count(g.insertions),
      del: count(g.deletions),
      bin: count(g.binary_files),
    })
  }
  return rows
}

export function DiffstatChart({
  commits,
  commitCount,
}: {
  commits: readonly unknown[]
  /** `git.commit_count`: how many exist, which can exceed how many were harvested. */
  commitCount: number | undefined
}) {
  const hatchId = useHatchId()
  const rows = diffRows(commits)
  if (rows.length === 0) return null

  let maxIns = 0
  let maxDel = 0
  for (const r of rows) {
    if (r.ins !== null && r.del !== null) {
      maxIns = Math.max(maxIns, r.ins)
      maxDel = Math.max(maxDel, r.del)
    }
  }
  // Anchored at zero on both sides, and each side ends at its own largest
  // RECORDED count -- so a lopsided change looks lopsided.
  const extent = { lo: -maxDel, hi: maxIns, degenerate: maxDel === 0 && maxIns === 0 }
  const innerW = W - M.left - M.right
  const height = M.top + rows.length * ROW + M.bottom
  const x = linearScale(extent, [0, innerW])
  const zeroX = x(0)
  const more = typeof commitCount === 'number' && commitCount > rows.length ? commitCount : null

  return (
    <figure className="ctl-chart ctl-diffstat" aria-label="Lines changed per commit">
      <ChartTitle>Lines changed per commit</ChartTitle>
      <ul className="ctl-chart-legend">
        <li>
          <span className="ctl-swatch is-del" aria-hidden="true" />
          deleted
        </li>
        <li>
          <span className="ctl-swatch is-ins" aria-hidden="true" />
          inserted
        </li>
        {rows.some((r) => r.bin !== null && r.bin > 0) && (
          <li>
            <span className="ctl-swatch is-binary" aria-hidden="true" />
            binary files
          </li>
        )}
      </ul>
      <svg
        className="ctl-chart-svg"
        width={W}
        height={height}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label="Lines deleted and inserted by each commit"
      >
        <HatchDef id={hatchId} />
        <g transform={`translate(${M.left},${M.top})`}>
          <line className="ctl-chart-zeroline" x1={zeroX} x2={zeroX} y1={0} y2={rows.length * ROW} />
          {rows.map((r, i) => (
            <DiffRowMark
              key={`${r.sha}-${i}`}
              r={r}
              y={i * ROW + (ROW - BAR) / 2}
              x={x}
              innerW={innerW}
              hatchId={hatchId}
            />
          ))}
          <ValueAxis
            side="bottom"
            top={rows.length * ROW}
            scale={x}
            extent={extent}
            format={(v) => (v > 0 ? `+${v}` : v < 0 ? `−${-v}` : '0')}
            keep={[0]}
            minGapPx={40}
          />
        </g>
      </svg>
      {more !== null && (
        <figcaption className="ctl-chart-cov" data-testid="diff-window" data-partial="yes">
          newest <strong>{rows.length}</strong> of {more}
        </figcaption>
      )}
    </figure>
  )
}

function DiffRowMark({
  r,
  y,
  x,
  innerW,
  hatchId,
}: {
  r: DiffRow
  y: number
  x: ReturnType<typeof linearScale>
  innerW: number
  hatchId: string
}) {
  const sha = (
    <text className="ctl-chart-rowlabel is-mono" x={-8} y={y + BAR - 1} textAnchor="end">
      {r.sha.slice(0, 10)}
    </text>
  )

  if (r.ins === null || r.del === null) {
    return (
      <g data-testid="diff-row" data-kind="absent" data-sha={r.sha}>
        {sha}
        <rect
          className="ctl-chart-absent"
          x={0}
          y={y - 2}
          width={innerW}
          height={BAR + 4}
          fill={`url(#${hatchId})`}
          role="img"
          aria-label={`Commit ${r.sha}: its line counts were not recorded as numbers, so nothing is drawn — not zero lines.`}
        >
          <title>{`${r.sha.slice(0, 10)} · lines —`}</title>
        </rect>
      </g>
    )
  }

  const zeroX = x(0)
  const binary = r.bin !== null && r.bin > 0
  const zeroLines = r.ins === 0 && r.del === 0
  return (
    <g data-testid="diff-row" data-kind={zeroLines ? (binary ? 'binary-only' : 'zero') : 'lines'} data-sha={r.sha}>
      {sha}
      {r.del > 0 && (
        <rect
          className="ctl-diff is-del"
          data-testid="diff-del"
          x={x(-r.del)}
          y={y}
          width={Math.max(1, zeroX - x(-r.del) - 1)}
          height={BAR}
        >
          <title>{`${r.sha.slice(0, 10)} · −${r.del}`}</title>
        </rect>
      )}
      {r.ins > 0 && (
        <rect
          className="ctl-diff is-ins"
          data-testid="diff-ins"
          x={zeroX + 1}
          y={y}
          width={Math.max(1, x(r.ins) - zeroX - 1)}
          height={BAR}
        >
          <title>{`${r.sha.slice(0, 10)} · +${r.ins}`}</title>
        </rect>
      )}
      {zeroLines && !binary && (
        // A MEASURED ZERO: git counted the commit and it changed no lines (an
        // empty or mode-only commit). The product's zero mark, on the zero rule.
        <circle className="ctl-chart-dot is-zero" data-testid="diff-zero" cx={zeroX} cy={y + BAR / 2} r={3.5}>
          <title>{`${r.sha.slice(0, 10)} · 0 lines`}</title>
        </circle>
      )}
      <text className="ctl-chart-rowlabel is-figure" x={innerW + 10} y={y + BAR - 1}>
        +{r.ins} −{r.del}
      </text>
      {binary && (
        <g data-testid="diff-binary" transform={`translate(${innerW + 72},${y + BAR / 2})`}>
          <path className="ctl-diff-binary" d="M0,-5 L5,0 L0,5 L-5,0 Z">
            <title>{`${r.bin} binary`}</title>
          </path>
          <text className="ctl-chart-rowlabel is-figure" x={8} y={4}>
            {r.bin}
          </text>
        </g>
      )}
    </g>
  )
}
