/**
 * THE WORKFLOW GRAPH'S LINEAGE (the owner's pick of the workflow-graph
 * mock-ups, direction B, 2026-11-05): lineage in the chip and a lineage strip.
 *
 *  - Each chip carries one short line: what it starts after and what takes its
 *    work next ("after review + implement · feeds re-review"); arrows and
 *    counts ("↑16") where the chip is narrow.
 *  - The grid draws light connectors between ADJACENT stages only: one lane
 *    per parent, one rail for a fan-in. A step that reads from two stages up
 *    (fix reads implement's patch past review) is named in its chip line and
 *    in the strip, never drawn across the chip between them.
 *  - Picking a step opens the strip above the grid: what it starts after on
 *    the left, the step in the middle, what takes its work on the right, each
 *    with its state and the file it passes or waits for. With nothing picked
 *    the strip is one line, or nothing when no step is running or held.
 *
 * EVERY EDGE IS THE SERVED WORKFLOW'S: `depends_on` is the line, `input_from`
 * the file on it, and whether the file arrived is `edgeProvenance` (dag.ts)
 * over the consumer's reported `staged_inputs`, the rule the older Graph view
 * paints its three edge kinds with.
 */
import { useLayoutEffect, useMemo, useState, type ReactNode } from 'react'

import { DECLARED_WORDS, edgeProvenance, inputsByStep, type EdgeProvenance } from '../dag'
import { NamedMark, StateMark } from '../marks'
import { stepState, type StepState, type Task, type Workflow, type WorkflowStep } from '../types'
import { chipKindOf, chipTimeOf, type ChipKind, type StageGridShape } from './StageGrid'
import './lineage.css'

// ---------------------------------------------------------------------------
// The data: every edge, its kind and its file
// ---------------------------------------------------------------------------

export type EdgeLook = 'order' | 'staged' | 'declared'

export interface LinEdge {
  readonly from: string
  readonly to: string
  readonly look: EdgeLook
  readonly prov: EdgeProvenance
  /** The file `input_from` names on this edge, or null for order only. */
  readonly file: string | null
}

export interface Lineage {
  readonly parents: ReadonlyMap<string, readonly LinEdge[]>
  readonly children: ReadonlyMap<string, readonly LinEdge[]>
  readonly states: ReadonlyMap<string, StepState>
  readonly kinds: ReadonlyMap<string, ChipKind>
}

export function lineageOf(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): Lineage {
  const inputs = inputsByStep(workflow.steps, taskById)
  const parents = new Map<string, LinEdge[]>()
  const children = new Map<string, LinEdge[]>()
  const states = new Map<string, StepState>()
  const kinds = new Map<string, ChipKind>()
  for (const s of workflow.steps) {
    parents.set(s.step_id, [])
    children.set(s.step_id, [])
    const st = stepState(s, taskById)
    states.set(s.step_id, st)
    kinds.set(s.step_id, chipKindOf(st))
  }
  for (const s of workflow.steps) {
    // Once per parent: a name listed twice is still one dependency.
    for (const p of new Set(s.depends_on)) {
      const prov = edgeProvenance({ from: p, to: s.step_id }, inputs)
      const e: LinEdge = { from: p, to: s.step_id, prov, look: prov.kind, file: prov.kind === 'order' ? null : prov.file }
      parents.get(s.step_id)?.push(e)
      children.get(p)?.push(e)
    }
  }
  return { parents, children, states, kinds }
}

const KIND_WORD: Readonly<Record<ChipKind, string>> = {
  done: 'done',
  failed: 'failed',
  running: 'running',
  waiting: 'waiting',
  parked: 'parked',
  stopped: 'cancelled',
  unknown: 'not known',
}

/** A step's state in a word: `not started` for a step the workflow has not reached. */
export function stateWord(st: StepState, kind: ChipKind): string {
  if (st.kind === 'unstarted') return 'not started'
  return KIND_WORD[kind]
}

/** The step that makes the edge's file failed or was cancelled: the file will not come unless it is retried. */
function makerStopped(e: LinEdge, lin: Lineage): string | null {
  if (e.prov.kind !== 'declared') return null
  const k = lin.kinds.get(e.from)
  return k === 'failed' ? 'failed' : k === 'stopped' ? 'cancelled' : null
}

/** The file on an edge in words: passed on, or why it has not been reported arriving. */
function fileWord(e: LinEdge, lin: Lineage): string {
  const stopped = makerStopped(e, lin)
  if (stopped !== null) return `not staged: ${e.from} ${stopped}`
  if (e.prov.kind === 'staged') return 'passed on'
  if (e.prov.kind === 'declared') return DECLARED_WORDS[e.prov.why].text
  return ''
}

function fileNote(e: LinEdge, lin: Lineage): string {
  const stopped = makerStopped(e, lin)
  if (stopped !== null) return `${e.file}: ${e.from} ${stopped} before making it, so nothing will stage it into ${e.to} unless ${e.from} is retried.`
  if (e.prov.kind === 'staged') return `${e.file} was reported staged into ${e.to}.`
  if (e.prov.kind === 'declared') return `${e.file}: ${DECLARED_WORDS[e.prov.why].note}`
  return `${e.to} runs after ${e.from}; no file passes.`
}

// ---------------------------------------------------------------------------
// The chip line
// ---------------------------------------------------------------------------

/** How the chip line names a neighbour: by its stage when it sits in the same column, else by its id. */
function nameIn(shape: StageGridShape, self: string, other: string): string {
  const a = shape.at.get(self)
  const b = shape.at.get(other)
  if (a && b && a.column === b.column) {
    const label = shape.stages[b.stage]?.label
    if (label && !/^Stage \d+$/.test(label)) return label
  }
  return other
}

function listIn(shape: StageGridShape, self: string, ids: readonly string[], noun: string): string {
  if (ids.length <= 2) return ids.map((id) => nameIn(shape, self, id)).join(' + ')
  const labels = new Set(ids.map((id) => shape.stages[shape.at.get(id)?.stage ?? -1]?.label ?? ''))
  const one = labels.size === 1 ? [...labels][0]! : null
  return one && !/^Stage \d+$/.test(one) ? `${ids.length} ${one}` : `${ids.length} ${noun}`
}

export interface ChipLine {
  readonly after: string
  readonly feeds: string
  /** The same for a chip of middling width: names only while they are short, else a count. */
  readonly afterShort: string
  readonly feedsShort: string
  /** A parent failed or was cancelled, so this step cannot start. */
  readonly blocked: string | null
  /** The same, in its two parts, so the chip never breaks a step id at its hyphen. */
  readonly blockedBy: { readonly ids: string; readonly verb: string } | null
  /** The full sentence, for the tooltip and the accessible name. */
  readonly tip: string
  /** How many steps it starts after and how many take its work: the narrow chip's form. */
  readonly ins: number
  readonly outs: number
}

export function chipLineOf(shape: StageGridShape, lin: Lineage, stepId: string): ChipLine {
  const ps = lin.parents.get(stepId) ?? []
  const cs = lin.children.get(stepId) ?? []
  const kind = lin.kinds.get(stepId)
  const failedParents = ps.filter((e) => {
    const k = lin.kinds.get(e.from)
    return k === 'failed' || k === 'stopped'
  })
  const waits = kind === 'waiting' || kind === 'parked'
  const blockedBy =
    waits && failedParents.length > 0
      ? { ids: failedParents.map((e) => e.from).join(', '), verb: lin.kinds.get(failedParents[0]!.from) === 'failed' ? 'failed' : 'cancelled' }
      : null
  const blocked = blockedBy === null ? null : `${blockedBy.ids} ${blockedBy.verb}`
  const after = ps.length === 0 ? 'starts first' : `after ${listIn(shape, stepId, ps.map((e) => e.from), 'steps')}`
  const feeds = cs.length === 0 ? 'last step' : `feeds ${listIn(shape, stepId, cs.map((e) => e.to), 'steps')}`
  const short = (ids: readonly string[], noun: string) => {
    const full = listIn(shape, stepId, ids, noun)
    return full.length <= 16 ? full : `${ids.length} ${ids.length === 1 ? 'step' : 'steps'}`
  }
  const afterShort = ps.length === 0 ? 'starts first' : `after ${short(ps.map((e) => e.from), 'steps')}`
  const feedsShort = cs.length === 0 ? 'last step' : `feeds ${short(cs.map((e) => e.to), 'steps')}`
  const tip = [
    ps.length === 0 ? 'starts first' : `starts after ${ps.map((e) => (e.file ? `${e.from} (${e.file})` : e.from)).join(', ')}`,
    cs.length === 0 ? 'nothing takes its work' : `its work goes to ${cs.map((e) => (e.file ? `${e.to} (${e.file})` : e.to)).join(', ')}`,
    blocked ? `cannot start: ${blocked}` : null,
  ]
  return { after, feeds, afterShort, feedsShort, blocked, blockedBy, tip: tip.filter((t): t is string => t !== null).join(' · '), ins: ps.length, outs: cs.length }
}

export function ChipLineView({ line }: { line: ChipLine }) {
  return (
    <span className="lg-line" aria-hidden="true">
      {line.blockedBy !== null ? (
        <span className="lg-line-part is-blocked">
          blocked: <span className="lg-nowrap">{line.blockedBy.ids}</span> {line.blockedBy.verb}
        </span>
      ) : (
        <>
          <span className="lg-line-part is-long">{line.after}</span>
          <span className="lg-line-part is-mid">{line.afterShort}</span>
        </>
      )}
      <span className="lg-line-part is-feeds is-long">{line.feeds}</span>
      <span className="lg-line-part is-feeds is-mid">{line.feedsShort}</span>
      {/* A chip too narrow for names: how many lines come in and go out, or that it is blocked. */}
      <span className="lg-line-short">
        {line.blocked !== null ? (
          <span className="is-blocked">blocked</span>
        ) : (
          <>
            {line.ins > 0 && <span>↑{line.ins}</span>}
            {line.outs > 0 && <span>↓{line.outs}</span>}
          </>
        )}
      </span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// The connectors between adjacent stages
// ---------------------------------------------------------------------------

export interface GapLanes {
  /** `from->to` -> its lane in the gap below `from`'s stage; absent for a straight drop. */
  readonly lane: ReadonlyMap<string, number>
  /** `from->to` -> the lane's sharing key: `c:<child>` a fan-in, `p:<parent>` a fan-out, else the edge's own. */
  readonly group: ReadonlyMap<string, string>
  /** Lanes per gap, by the upper stage's index. */
  readonly count: readonly number[]
}

/**
 * Which horizontal lane each bent connector runs on. A shared lane would read
 * as every parent on it feeding every child on it, so a lane is shared only
 * where that is true: a pure fan-in (every parent on it feeds only that child
 * in the stage below) or one parent's lane to its children. A child read by
 * several of those lanes takes each drop at its own x (Connectors).
 */
export function gapLanesOf(shape: StageGridShape, lin: Lineage): GapLanes {
  const lane = new Map<string, number>()
  const group = new Map<string, string>()
  const count: number[] = shape.stages.map(() => 0)
  shape.stages.forEach((_, k) => {
    const edges: LinEdge[] = []
    for (const [, es] of lin.parents) {
      for (const e of es) {
        const a = shape.at.get(e.from)
        const b = shape.at.get(e.to)
        if (a && b && a.stage === k && b.stage === k + 1 && a.column !== b.column) edges.push(e)
      }
    }
    const adjChildren = (p: string) => (lin.children.get(p) ?? []).filter((e) => shape.at.get(e.to)?.stage === k + 1)
    const adjParents = (c: string) => (lin.parents.get(c) ?? []).filter((e) => shape.at.get(e.from)?.stage === k)
    const keys: { key: string; x: number }[] = []
    const keyOf = new Map<string, string>()
    for (const e of edges) {
      const fanIn = adjParents(e.to).length > 1 && adjParents(e.to).every((pe) => adjChildren(pe.from).length === 1)
      // A parent feeding several children runs ONE lane to them all (true of every child on it); a lane per
      // edge drew a closed box under a parent with two children.
      const key = fanIn ? `c:${e.to}` : adjChildren(e.from).length > 1 ? `p:${e.from}` : `e:${e.from}->${e.to}`
      keyOf.set(`${e.from}->${e.to}`, key)
      group.set(`${e.from}->${e.to}`, key)
      if (!keys.some((x) => x.key === key)) keys.push({ key, x: shape.at.get(e.from)!.column })
    }
    // The longest span on the top lane: a drop into a nearer child then starts below it and crosses nothing.
    const spanOfKey = (key: string) => {
      const xs = edges.filter((e) => keyOf.get(`${e.from}->${e.to}`) === key).flatMap((e) => [shape.at.get(e.from)!.column, shape.at.get(e.to)!.column])
      return Math.max(...xs) - Math.min(...xs)
    }
    keys.sort((a, b) => spanOfKey(b.key) - spanOfKey(a.key) || a.x - b.x)
    keys.forEach((x, i) => {
      for (const [ek, kk] of keyOf) if (kk === x.key) lane.set(ek, i)
    })
    count[k] = keys.length
  })
  return { lane, group, count }
}

const LANE_PX = 8

/** The extra room a gap needs for its lanes, px. */
export function gapPx(lanes: number): number {
  // Straight drops only: no lane to make room for.
  return lanes === 0 ? 12 : 8 + lanes * LANE_PX
}

interface Drawn {
  readonly key: string
  readonly d: string
  readonly look: EdgeLook
  readonly lit: boolean
  /** Paint order: stubs first, then each lane's run over a halo, so a crossing shows a gap, never a joint. */
  readonly layer: number
  readonly halo?: boolean
}

/**
 * The connectors, measured off the chips the grid drew: from the bottom of a
 * parent chip, down to its lane, across, and down into the top of the child.
 * Only between adjacent stages, and nothing crosses a chip: every segment is
 * inside a gap between two rows.
 */
export function Connectors({
  grid,
  shape,
  lin,
  lanes,
  selected,
  version,
}: {
  grid: HTMLElement | null
  shape: StageGridShape
  lin: Lineage
  lanes: GapLanes
  selected: string | null
  version: string
}) {
  const [paths, setPaths] = useState<{ w: number; h: number; drawn: Drawn[] }>({ w: 0, h: 0, drawn: [] })
  useLayoutEffect(() => {
    if (grid === null) return
    const measure = () => {
      const g = grid.getBoundingClientRect()
      const chips = new Map<string, HTMLElement>()
      for (const el of grid.querySelectorAll<HTMLElement>('[data-step]')) chips.set(el.dataset.step ?? '', el)
      const rect = (id: string) => chips.get(id)?.getBoundingClientRect() ?? null
      const drawn: Drawn[] = []
      const push = (key: string, d: string, look: EdgeLook, lit: boolean, layer = 0, halo = false) => drawn.push({ key, d, look, lit, layer, halo })
      // A shared lane is painted once, in the weakest look on it (the older Graph view's rule): a
      // trunk carrying one file nothing has reported is dashed even beside files that arrived.
      const rank: Readonly<Record<EdgeLook, number>> = { declared: 0, order: 1, staged: 2 }
      const trunks = new Map<string, { look: EdgeLook; x: number; y: number; laneY: number; xs: number[]; end: number; lit: boolean }>()
      for (const [, es] of lin.parents) {
        for (const e of es) {
          const a = shape.at.get(e.from)
          const b = shape.at.get(e.to)
          if (!a || !b || b.stage !== a.stage + 1) continue
          const ra = rect(e.from)
          const rb = rect(e.to)
          if (ra === null || rb === null) continue
          const k = `${e.from}->${e.to}`
          const x1 = Math.round(ra.left + ra.width / 2 - g.left) + 0.5
          const y1 = Math.round(ra.bottom - g.top)
          // A child read by several parents in the stage above (and not one fan-in rail) takes each at its own
          // x, left to right as the parents sit, so two drops never overlap into one.
          const grp0 = lanes.group.get(k) ?? `e:${k}`
          const ins = grp0.startsWith('c:')
            ? []
            : (lin.parents.get(e.to) ?? []).map((pe) => pe.from).filter((f) => shape.at.get(f)?.stage === a.stage).sort((p, q) => (shape.at.get(p)?.column ?? 0) - (shape.at.get(q)?.column ?? 0))
          const slot = ins.length > 1 ? (ins.indexOf(e.from) + 1) / (ins.length + 1) : 0.5
          const x2 = Math.round(rb.left + rb.width * slot - g.left) + 0.5
          const y2 = Math.round(rb.top - g.top)
          const l = lanes.lane.get(k)
          const lit = selected !== null && (e.from === selected || e.to === selected)
          if (l === undefined || x1 === x2) {
            push(k, x1 === x2 ? `M${x1} ${y1} V${y2}` : `M${x1} ${y1} V${y2 - 5} H${x2} V${y2}`, e.look, lit)
            continue
          }
          const laneY = y1 + 4 + l * LANE_PX
          const grp = lanes.group.get(k) ?? `e:${k}`
          if (grp.startsWith('e:')) {
            // The stub down to the lane, then the run across and down over a halo that starts 3px clear of
            // the stub's corner: another edge's line under this run is cut either side, a bridge.
            const off = x2 > x1 ? 3 : -3
            push(`${k}:stub`, `M${x1} ${y1} V${laneY} H${x1 + off}`, e.look, lit)
            push(`${k}:halo`, `M${x1 + off} ${laneY} H${x2} V${y2}`, e.look, lit, 1, true)
            push(`${k}:run`, `M${x1 + off} ${laneY} H${x2} V${y2}`, e.look, lit, 1)
            continue
          }
          const fanIn = grp.startsWith('c:')
          // The shared end: the child of a fan-in, the parent of a fan-out. Every trunk piece starts there,
          // so the dashes of one trunk line up.
          const sx = fanIn ? x2 : x1
          const t = trunks.get(grp) ?? { look: e.look, x: sx, y: fanIn ? y2 : y1, laneY, xs: [], end: fanIn ? y2 : y1, lit: false }
          if (rank[e.look] < rank[t.look]) t.look = e.look
          t.xs.push(fanIn ? x1 : x2)
          t.lit = t.lit || (selected !== null && (fanIn ? e.to === selected : e.from === selected))
          trunks.set(grp, t)
          // The edge's own piece: a parent's stub up to the lane, or a drop from the lane into a child.
          push(`${k}:own`, fanIn ? `M${x1} ${y1} V${laneY}` : `M${x2} ${laneY} V${y2}`, e.look, lit)
          // A branch picked: its run along the trunk to the shared end, lit over the trunk.
          if (lit && !(fanIn ? e.to === selected : e.from === selected)) {
            push(`${k}:run`, `M${sx} ${t.y} V${laneY} H${fanIn ? x1 : x2}`, e.look, true)
          }
        }
      }
      for (const [grp, t] of trunks) {
        const lo = Math.min(t.x, ...t.xs)
        const hi = Math.max(t.x, ...t.xs)
        const d = `M${t.x} ${t.end} V${t.laneY}${lo < t.x ? ` M${t.x} ${t.laneY} H${lo}` : ''}${hi > t.x ? ` M${t.x} ${t.laneY} H${hi}` : ''}`
        push(`trunk:${grp}`, d, t.look, t.lit)
      }
      // The lit ones last, so the picked step's lines are painted over the rest.
      drawn.sort((p, q) => Number(p.lit) - Number(q.lit) || p.layer - q.layer)
      setPaths({ w: Math.ceil(g.width), h: Math.ceil(g.height), drawn })
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(grid)
    return () => ro.disconnect()
  }, [grid, shape, lin, lanes, selected, version])
  return (
    <svg
      className={`lg-wires${selected !== null ? ' has-pick' : ''}`}
      width={paths.w}
      height={paths.h}
      viewBox={`0 0 ${paths.w} ${paths.h}`}
      aria-hidden="true"
      focusable="false"
    >
      {paths.drawn.map((p) => (
        <path key={p.key} d={p.d} className={p.halo ? 'lg-halo' : `lg-wire is-${p.look}${p.lit ? ' is-lit' : ''}`} />
      ))}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// The key and the strip
// ---------------------------------------------------------------------------

const KEY: readonly { look: EdgeLook; word: string; title: string }[] = [
  { look: 'order', word: 'runs after', title: 'Thin: the lower step waits for the upper one; no file passes.' },
  { look: 'staged', word: 'file passed on', title: "Heavier, solid: the upper step's file was reported staged into the lower step." },
  { look: 'declared', word: 'file expected, not arrived', title: 'Heavier, dashed: a file is declared and nothing has reported it arriving.' },
]

function Sample({ look }: { look: EdgeLook }) {
  return (
    <svg className="lg-sample" width="16" height="8" viewBox="0 0 16 8" aria-hidden="true" focusable="false">
      <path className={`lg-wire is-${look}`} d="M1 4 H15" />
    </svg>
  )
}

export function WireKey() {
  return (
    <ul className="lg-key" aria-label="Line key">
      {KEY.map((k) => (
        <li key={k.look} title={k.title}>
          <Sample look={k.look} />
          {k.word}
        </li>
      ))}
    </ul>
  )
}

function Mark({ st, kind }: { st: StepState; kind: ChipKind }) {
  if (st.kind === 'state') return <StateMark state={st.task.state} bare />
  if (st.kind === 'unknown') return <NamedMark mark="warn" hue="warn" word="not known" bare />
  return <NamedMark mark="queued" hue="neu" word={stateWord(st, kind)} bare />
}

function Pick({ id, onPick, children, className }: { id: string; onPick: (id: string) => void; children: ReactNode; className?: string }) {
  return (
    <button type="button" className={`lg-pick${className ? ` ${className}` : ''}`} onClick={() => onPick(id)} title={`Pick ${id}`}>
      {children}
    </button>
  )
}

/** One neighbour: its mark, its id, its state, and the file on the edge between it and the picked step. */
function Neighbour({ e, side, lin, onPick, now }: { e: LinEdge; side: 'up' | 'down'; lin: Lineage; onPick: (id: string) => void; now: number }) {
  const id = side === 'up' ? e.from : e.to
  const st = lin.states.get(id)!
  const kind = lin.kinds.get(id)!
  const time = chipTimeOf(st, now)
  return (
    <li className={`lg-card is-${kind}`}>
      <Pick id={id} onPick={onPick} className="lg-card-head">
        <Mark st={st} kind={kind} />
        <b className="lg-id">{id}</b>
        <span className="lg-state">
          {stateWord(st, kind)}
          {time !== null && ` ${time}`}
        </span>
      </Pick>
      <span className="lg-edge" title={fileNote(e, lin)}>
        <Sample look={e.look} />
        {e.file === null ? (
          <span>{side === 'up' ? 'runs after it' : 'runs after this'}, no file</span>
        ) : (
          <span>
            <code className="lg-file">{e.file}</code> {fileWord(e, lin)}
          </span>
        )}
      </span>
    </li>
  )
}

/** Many neighbours in one stage: one card, a mark per step, and how many files arrived. */
function Group({ label, edges, side, lin, onPick }: { label: string; edges: readonly LinEdge[]; side: 'up' | 'down'; lin: Lineage; onPick: (id: string) => void }) {
  const ids = edges.map((e) => (side === 'up' ? e.from : e.to))
  const count = (k: ChipKind) => ids.filter((id) => lin.kinds.get(id) === k).length
  const notStarted = ids.filter((id) => lin.states.get(id)?.kind === 'unstarted').length
  const parts = [
    count('done') > 0 ? `${count('done')} done` : null,
    count('running') > 0 ? `${count('running')} running` : null,
    count('failed') > 0 ? `${count('failed')} failed` : null,
    notStarted > 0 ? `${notStarted} not started` : null,
  ].filter((p): p is string => p !== null)
  const files = edges.filter((e) => e.file !== null)
  const passed = files.filter((e) => e.look === 'staged').length
  return (
    <li className="lg-card is-group">
      <span className="lg-card-head">
        <b className="lg-id">
          {label} × {ids.length}
        </b>
        <span className="lg-state">{parts.join(' · ')}</span>
      </span>
      <span className="lg-marks">
        {ids.map((id) => (
          <Pick key={id} id={id} onPick={onPick} className="lg-mini">
            <Mark st={lin.states.get(id)!} kind={lin.kinds.get(id)!} />
          </Pick>
        ))}
      </span>
      {files.length > 0 && (
        <span className="lg-edge">
          <Sample look={passed === files.length ? 'staged' : 'declared'} />
          <span>
            {files.length} files: {passed} passed on{passed < files.length ? `, ${files.length - passed} not arrived` : ''}
          </span>
        </span>
      )}
    </li>
  )
}

function stageLabelOf(shape: StageGridShape, id: string): string {
  const at = shape.at.get(id)
  return at === undefined ? '' : (shape.stages[at.stage]?.label ?? '')
}

/** Neighbours grouped by stage: four or more in one stage are one group card. */
function Side({ edges, side, shape, lin, onPick, now }: { edges: readonly LinEdge[]; side: 'up' | 'down'; shape: StageGridShape; lin: Lineage; onPick: (id: string) => void; now: number }) {
  const byStage = new Map<number, LinEdge[]>()
  for (const e of edges) {
    const k = shape.at.get(side === 'up' ? e.from : e.to)?.stage ?? -1
    byStage.set(k, [...(byStage.get(k) ?? []), e])
  }
  // Nearest stage first, in reading order: upstream ends at the step, downstream starts at it.
  const stages = [...byStage.keys()].sort((a, b) => (side === 'up' ? b - a : a - b))
  return (
    <ul className="lg-cards">
      {stages.flatMap((k) => {
        const es = byStage.get(k)!
        if (es.length >= 4) {
          return [<Group key={`g${k}`} label={stageLabelOf(shape, side === 'up' ? es[0]!.from : es[0]!.to)} edges={es} side={side} lin={lin} onPick={onPick} />]
        }
        return es.map((e) => <Neighbour key={`${e.from}->${e.to}`} e={e} side={side} lin={lin} onPick={onPick} now={now} />)
      })}
    </ul>
  )
}

/** Further along the chain, past the direct neighbours, as plain names: where the line goes after. */
function farther(lin: Lineage, start: string, dir: 'up' | 'down'): string[] {
  const direct = new Set((dir === 'up' ? lin.parents : lin.children).get(start)?.map((e) => (dir === 'up' ? e.from : e.to)) ?? [])
  const seen = new Set<string>([start, ...direct])
  const out: string[] = []
  let frontier = [...direct]
  while (frontier.length > 0) {
    const next: string[] = []
    for (const id of frontier) {
      for (const e of (dir === 'up' ? lin.parents : lin.children).get(id) ?? []) {
        const o = dir === 'up' ? e.from : e.to
        if (seen.has(o)) continue
        seen.add(o)
        out.push(o)
        next.push(o)
      }
    }
    frontier = next
  }
  return out
}

/** Why the picked step stands where it does, in a few words. */
function headline(lin: Lineage, id: string, now: number): { text: string; tone: 'bad' | 'neu' } {
  const st = lin.states.get(id)!
  const kind = lin.kinds.get(id)!
  const ps = lin.parents.get(id) ?? []
  const time = chipTimeOf(st, now)
  if (kind === 'waiting' || kind === 'parked') {
    const failed = ps.filter((e) => lin.kinds.get(e.from) === 'failed' || lin.kinds.get(e.from) === 'stopped')
    if (failed.length > 0) return { text: `cannot start: ${failed.map((e) => e.from).join(', ')} ${lin.kinds.get(failed[0]!.from) === 'failed' ? 'failed' : 'cancelled'}`, tone: 'bad' }
    const open = ps.filter((e) => lin.kinds.get(e.from) !== 'done')
    if (open.length === 0) return { text: ps.length === 0 ? stateWord(st, kind) : 'every step it waits for is done', tone: 'neu' }
    return { text: open.length === 1 ? `waits for ${open[0]!.from}` : `waits for ${open.length} of ${ps.length}`, tone: 'neu' }
  }
  if (kind === 'running') return { text: `running${time ? ` ${time}` : ''}`, tone: 'neu' }
  if (kind === 'done') return { text: `done${time ? ` in ${time}` : ''}`, tone: 'neu' }
  if (kind === 'failed') return { text: 'failed', tone: 'bad' }
  return { text: stateWord(st, kind), tone: 'neu' }
}

export function LineageStrip({
  workflow,
  shape,
  lin,
  selected,
  onPick,
  now,
  stacked = false,
}: {
  workflow: Workflow
  shape: StageGridShape
  lin: Lineage
  selected: string | null
  onPick: (id: string) => void
  now: number
  stacked?: boolean
}) {
  const step: WorkflowStep | null = selected === null ? null : (workflow.steps.find((s) => s.step_id === selected) ?? null)
  if (step === null) return <RestStrip workflow={workflow} lin={lin} onPick={onPick} />
  const id = step.step_id
  const ups = lin.parents.get(id) ?? []
  const downs = lin.children.get(id) ?? []
  const before = farther(lin, id, 'up')
  const after = farther(lin, id, 'down')
  const st = lin.states.get(id)!
  const kind = lin.kinds.get(id)!
  const head = headline(lin, id, now)
  return (
    <section className={`lg-strip${stacked ? ' is-stacked' : ''}`} aria-label={`${id}: what it starts after and what takes its work`}>
      <div className="lg-side is-up">
        <h3 className="lg-eyebrow">Starts after</h3>
        {ups.length === 0 ? <p className="lg-none">nothing: it starts first</p> : <Side edges={ups} side="up" shape={shape} lin={lin} onPick={onPick} now={now} />}
        {before.length > 0 && (
          <p className="lg-far">
            before that:{' '}
            {before.map((b, i) => (
              <span key={b}>
                <Pick id={b} onPick={onPick} className="lg-far-id">{b}</Pick>
                {i < before.length - 1 ? ', ' : ''}
              </span>
            ))}
          </p>
        )}
      </div>
      {stacked ? (
        <p className="lg-thru">
          <span aria-hidden="true">↓</span>
          {head.tone === 'bad' || kind === 'waiting' || kind === 'parked' ? <span className={`lg-head is-${head.tone}`}>{head.text}</span> : null}
        </p>
      ) : (
        <>
          <span className="lg-arrow" aria-hidden="true">→</span>
          <div className={`lg-self is-${kind}`}>
            <h3 className="lg-eyebrow">Picked</h3>
            <span className="lg-card-head">
              <Mark st={st} kind={kind} />
              <b className="lg-id" title={id}>{id}</b>
            </span>
            <span className={`lg-head is-${head.tone}`}>{head.text}</span>
            {kind === 'failed' && st.kind === 'state' && st.task.last_error && <code className="lg-err">{st.task.last_error.split('\n')[0]}</code>}
          </div>
          <span className="lg-arrow" aria-hidden="true">→</span>
        </>
      )}
      <div className="lg-side is-down">
        <h3 className="lg-eyebrow">Its work goes to</h3>
        {downs.length === 0 ? <p className="lg-none">nothing: it is the last step</p> : <Side edges={downs} side="down" shape={shape} lin={lin} onPick={onPick} now={now} />}
        {after.length > 0 && (
          <p className="lg-far">
            then:{' '}
            {after.map((b, i) => (
              <span key={b}>
                <Pick id={b} onPick={onPick} className="lg-far-id">{b}</Pick>
                {i < after.length - 1 ? ', ' : ''}
              </span>
            ))}
          </p>
        )}
      </div>
    </section>
  )
}

/** One thing the strip says at rest: a step, and the steps it holds. */
export interface RestRow {
  readonly id: string
  readonly kind: ChipKind
  readonly verb: string
  readonly then: readonly string[]
  readonly thenVerb: string
}

/**
 * Where work stands along the lines: each failed step and what it keeps from
 * starting, each running step and what waits on it. Failures first: they are
 * what stops the line.
 */
export function restRowsOf(workflow: Workflow, lin: Lineage): RestRow[] {
  const out: RestRow[] = []
  for (const s of workflow.steps) {
    const kind = lin.kinds.get(s.step_id)!
    const waiting = (lin.children.get(s.step_id) ?? []).map((e) => e.to).filter((c) => {
      const k = lin.kinds.get(c)
      return k === 'waiting' || k === 'parked'
    })
    if (kind === 'failed' && waiting.length > 0) out.push({ id: s.step_id, kind, verb: 'failed', then: waiting, thenVerb: 'cannot start' })
    else if (kind === 'running' && waiting.length > 0) out.push({ id: s.step_id, kind, verb: 'running', then: waiting, thenVerb: waiting.length === 1 ? 'waits for it' : 'wait for it' })
  }
  return out.sort((a, b) => Number(b.kind === 'failed') - Number(a.kind === 'failed'))
}

/**
 * AT REST THE STRIP IS ONE SHORT LINE (the owner's change to the picked
 * design, 2026-11-05): the first thing that holds work, and how many more.
 * Nothing at all when no step is running or held by a failed one. Every name
 * picks its step.
 */
function RestStrip({ workflow, lin, onPick }: { workflow: Workflow; lin: Lineage; onPick: (id: string) => void }) {
  const rows = useMemo(() => restRowsOf(workflow, lin), [workflow, lin])
  const r = rows[0]
  if (r === undefined) return null
  return (
    <section className="lg-strip is-rest" aria-label="Where work stands">
      <p className={`lg-rest is-${r.kind}`}>
        <Mark st={lin.states.get(r.id)!} kind={r.kind} />
        <Pick id={r.id} onPick={onPick} className="lg-far-id">{r.id}</Pick>
        <span className="lg-rest-verb">{r.verb}</span>
        <span className="lg-rest-arrow" aria-hidden="true">→</span>
        {r.then.length <= 2 ? (
          r.then.map((t, i) => (
            <span key={t}>
              <Pick id={t} onPick={onPick} className="lg-far-id">{t}</Pick>
              {i < r.then.length - 1 ? ',' : ''}
            </span>
          ))
        ) : (
          <span>{r.then.length} steps</span>
        )}
        <span className="lg-rest-verb">{r.thenVerb}</span>
        {rows.length > 1 && <span className="lg-rest-more">+{rows.length - 1} more</span>}
      </p>
    </section>
  )
}
