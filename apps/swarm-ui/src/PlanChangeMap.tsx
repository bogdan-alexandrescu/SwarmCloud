/**
 * WHERE A PLAN CHANGES, DRAWN (the approvals brief, BGD 6 November 2026): the
 * files the plan changes, boxed by area in the middle, what calls them on the
 * left and what tests them on the right, each edge labelled. Picking a file
 * lights its neighbours and opens its one-line what, what calls it, what it
 * uses and what tests it. The list under the map picks the same way, so the
 * map has a keyboard and screen-reader route. Below 640px the map is one
 * column, area by area, its edges down a lane gutter.
 *
 * Hand-drawn SVG, no diagram library. The nodes and edges are the plan's
 * `graph` when the planner serves one; ApprovalDecision builds them from the
 * steps' files otherwise.
 */
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import './styles/plan-brief.css'

export type ChangeKind = 'add' | 'modify' | 'delete' | 'test'
export type MapNodeKind = 'changed' | 'caller' | 'test'

/** One changed file, as the brief lists it. */
export interface MapChange {
  file: string
  area: string
  kind: ChangeKind
  what: string
}
export interface MapNode {
  id: string
  file: string
  label: string
  kind: MapNodeKind
  /** A changed node's area: the map boxes changed files by it. */
  area: string
  what: string
  /** The first step that names the file, drawn as a badge on a plan of several steps. */
  step?: number
}
export interface MapEdge {
  from: string
  to: string
  kind: string
}

export const base = (p: string) => p.slice(p.lastIndexOf('/') + 1)
export const dir = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/') + 1) : '')
const KIND_WORD: Record<ChangeKind, string> = { add: 'add', modify: 'modify', delete: 'delete', test: 'test' }
/** The frozen contract's area: drawn in a red dashed box. */
export const FROZEN_AREA = 'Frozen contract'


interface Placed extends MapNode {
  x: number
  y: number
  w: number
  h: number
}
interface Group {
  area: string
  x: number
  y: number
  w: number
  h: number
}
interface Layout {
  width: number
  height: number
  nodes: Placed[]
  groups: Group[]
  tall: boolean
}

const NH = 50
const VG = 12
const AH = 26
const AP = 10

/** How far each arc between two changed files bulges right: a longer span rides further out, so no two arcs share a lane. */
function arcLanes(nodes: MapNode[], edges: MapEdge[]): Map<number, number> {
  const changed = new Set(nodes.filter((n) => n.kind === 'changed').map((n) => n.id))
  const order = nodes.filter((n) => n.kind === 'changed').sort((a, b) => (a.step ?? 0) - (b.step ?? 0)).map((n) => n.id)
  const arcs = edges
    .map((e, i) => ({ i, lo: Math.min(order.indexOf(e.from), order.indexOf(e.to)), hi: Math.max(order.indexOf(e.from), order.indexOf(e.to)), ok: changed.has(e.from) && changed.has(e.to) }))
    .filter((x) => x.ok)
    .sort((x, y) => x.hi - x.lo - (y.hi - y.lo))
  const lanes = new Map<number, number>()
  const used: { lo: number; hi: number }[][] = []
  for (const a of arcs) {
    let lane = 0
    while ((used[lane] ?? []).some((u) => u.lo <= a.hi && a.lo <= u.hi)) lane++
    ;(used[lane] ??= []).push(a)
    lanes.set(a.i, lane)
  }
  return lanes
}

const ARC0 = 20
const ARCW = 24

/** Changed files grouped by area in the middle, callers to the left, tests to the right; one column on a phone. */
function layout(nodes: MapNode[], edges: MapEdge[], width: number, gapAfter: string | null, gapH: number): Layout {
  const changed = nodes.filter((n) => n.kind === 'changed').sort((a, b) => (a.step ?? 0) - (b.step ?? 0))
  const areas: string[] = []
  for (const n of changed) if (!areas.includes(n.area ?? '')) areas.push(n.area ?? '')
  const neighbours = (id: string) => edges.flatMap((e) => (e.from === id ? [e.to] : e.to === id ? [e.from] : []))
  const tall = width < 640
  if (tall) return tallLayout(nodes, changed, areas, neighbours, width, gapAfter, gapH)
  const pad = 2
  const gut = Math.max(84, Math.round(width * 0.09))
  // Arcs between changed files ride lanes to the right of their column: the test column moves over to make room.
  const lanes = Math.max(-1, ...arcLanes(nodes, edges).values()) + 1
  const gutR = gut + (lanes === 0 ? 0 : ARC0 + lanes * ARCW)
  const col = (width - gut - gutR - 2 * pad) / 3
  const xs = { caller: pad, changed: pad + col + gut, test: pad + 2 * col + gut + gutR }
  const placed: Placed[] = []
  const groups: Group[] = []
  let y = 0
  for (const area of areas) {
    const inArea = changed.filter((n) => (n.area ?? '') === area)
    const h = AH + inArea.length * NH + (inArea.length - 1) * VG + AP
    groups.push({ area, x: xs.changed, y, w: col, h })
    inArea.forEach((n, i) => placed.push({ ...n, x: xs.changed + AP, y: y + AH + i * (NH + VG), w: col - 2 * AP, h: NH }))
    y += h + VG + 4
  }
  const side = (kind: 'caller' | 'test', x: number) => {
    const want = nodes
      .filter((n) => n.kind === kind)
      .map((n) => {
        const ys = neighbours(n.id).flatMap((id) => placed.filter((p) => p.id === id).map((p) => p.y + p.h / 2))
        return { n, want: ys.length === 0 ? 0 : ys.reduce((a, b) => a + b, 0) / ys.length - NH / 2 }
      })
      .sort((a, b) => a.want - b.want)
    let floor = 0
    for (const { n, want: w } of want) {
      const top = Math.max(w, floor)
      placed.push({ ...n, x, y: top, w: col, h: NH })
      floor = top + NH + VG
    }
  }
  side('caller', xs.caller)
  side('test', xs.test)
  const height = Math.max(...placed.map((p) => p.y + p.h), ...groups.map((g) => g.y + g.h)) + 4
  return { width, height, nodes: placed, groups, tall: false }
}

const TH = 46
const TL = 22

/**
 * One column, area by area: the callers of the area's changed files, then the
 * area's box holding only its changed files, then their tests, so nothing
 * outside a box reads as part of it. Edges run down a gutter on the right. The
 * picked node opens room under itself for its strip.
 */
function tallLayout(nodes: MapNode[], changed: MapNode[], areas: string[], neighbours: (id: string) => string[], width: number, gapAfter: string | null, gapH: number): Layout {
  const gutter = 54
  const w = width - gutter - 2
  const seen = new Set<string>()
  const placed: Placed[] = []
  const groups: Group[] = []
  let y = 0
  const put = (n: MapNode, x: number, nw: number) => {
    seen.add(n.id)
    placed.push({ ...n, x, y, w: nw, h: TH })
    y += TH + 10
    if (n.id === gapAfter) y += gapH + 10
  }
  const near = (ids: string[], kind: 'caller' | 'test') =>
    ids.flatMap((id) => neighbours(id)).flatMap((id) => nodes.filter((n) => n.id === id && n.kind === kind && !seen.has(n.id))).filter((n, i, all) => all.findIndex((m) => m.id === n.id) === i)
  for (const area of areas) {
    const inArea = changed.filter((n) => (n.area ?? '') === area)
    const ids = inArea.map((n) => n.id)
    for (const n of near(ids, 'caller')) put(n, 2, w)
    const top = y
    y += TL + 2
    for (const n of inArea) put(n, 10, w - 16)
    groups.push({ area, x: 2, y: top, w, h: y - top - 2 })
    y += 8
    for (const n of near(ids, 'test')) put(n, 2, w)
  }
  for (const n of nodes) if (!seen.has(n.id)) put(n, 2, w)
  return { width, height: y, nodes: placed, groups, tall: true }
}

const EDGE_WORD: Record<string, string> = { calls: 'calls', imports: 'imports', tests: 'tests' }

function wideEdge(a: Placed, b: Placed, kind: string, lane: number | undefined, arcRoom: number): { d: string; lx: number; ly: number; rot: boolean } {
  // Draw left to right: a caller into a changed file, a changed file out to its test.
  if (kind === 'tests') [a, b] = [b, a]
  // Arcs attach below a changed file's middle and its test edges above, so the two never leave from one point.
  const off = arcRoom === 0 ? 0 : 9
  const ay = a.y + a.h / 2 + (lane !== undefined ? off : kind === 'tests' ? -off : 0)
  const by = b.y + b.h / 2 + (lane !== undefined ? off : 0)
  if (lane !== undefined) {
    // Two changed files: a square arc in its own lane right of the column, its label along the lane.
    const x = Math.max(a.x + a.w, b.x + b.w)
    const lx = x + ARC0 + lane * ARCW
    const s = by > ay ? 1 : -1
    return { d: `M ${x} ${ay} H ${lx - 6} Q ${lx} ${ay} ${lx} ${ay + 6 * s} V ${by - 6 * s} Q ${lx} ${by} ${lx - 6} ${by} H ${x}`, lx, ly: (ay + by) / 2, rot: true }
  }
  const x1 = a.x + a.w
  const x2 = b.x
  if (kind === 'tests') {
    // Out straight across the arc lanes first, then bend: any crossing is square, and the label sits by its test.
    const xs = x1 + arcRoom + 6
    const k = Math.min(48, (x2 - xs) / 2)
    return { d: `M ${x1} ${ay} H ${xs} C ${xs + k} ${ay}, ${xs + k} ${by}, ${xs + 2 * k} ${by} H ${x2}`, lx: x2 - 24, ly: by, rot: false }
  }
  const mx = (x1 + x2) / 2
  return { d: `M ${x1} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${x2} ${by}`, lx: mx, ly: (ay + by) / 2, rot: false }
}

function tallEdge(a: Placed, b: Placed, lane: number, width: number): { d: string; lx: number; ly: number; rot: boolean } {
  const ay = a.y + a.h / 2
  const by = b.y + b.h / 2
  const x0 = a.x + a.w
  const x1 = b.x + b.w
  const lx = width - 46 + Math.min(lane, 3) * 12
  return { d: `M ${x0} ${ay} H ${lx - 6} Q ${lx} ${ay} ${lx} ${ay + (by > ay ? 6 : -6)} V ${by + (by > ay ? -6 : 6)} Q ${lx} ${by} ${lx - 6} ${by} H ${x1}`, lx, ly: (ay + by) / 2, rot: true }
}

function trim(s: string, px: number, perChar: number): string {
  const n = Math.max(4, Math.floor(px / perChar))
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`
}

function ChangeMap({ nodes, edges, multi, selected, onSelect }: { nodes: MapNode[]; edges: MapEdge[]; multi: boolean; selected: string | null; onSelect: (id: string | null) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(960)
  const pickRef = useRef<HTMLDivElement>(null)
  const [pickH, setPickH] = useState(0)
  useLayoutEffect(() => {
    const el = box.current
    if (el === null) return
    // A box not laid out yet (or a test's DOM) measures 0: the desktop width stands.
    const measure = (w: number) => w > 0 && setWidth(Math.round(w))
    measure(el.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(([e]) => e && measure(e.contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const tallNow = width < 640
  const L = useMemo(() => layout(nodes, edges, width, tallNow ? selected : null, pickH), [nodes, edges, width, tallNow, selected, pickH])
  // On a phone the picked node's strip opens in the map under it, so it covers nothing.
  useLayoutEffect(() => {
    const el = pickRef.current
    if (el === null) {
      setPickH(0)
      return
    }
    setPickH(Math.round(el.getBoundingClientRect().height))
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(([e]) => e && setPickH(Math.round(e.borderBoxSize?.[0]?.blockSize ?? e.contentRect.height)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [selected, tallNow])
  const arcs = useMemo(() => (tallNow ? new Map<number, number>() : arcLanes(nodes, edges)), [nodes, edges, tallNow])
  const arcRoom = arcs.size === 0 ? 0 : ARC0 + Math.max(...arcs.values()) * ARCW
  const byId = (id: string) => L.nodes.find((n) => n.id === id)
  const lit = new Set<string>()
  if (selected !== null) {
    lit.add(selected)
    for (const e of edges) {
      if (e.from === selected) lit.add(e.to)
      if (e.to === selected) lit.add(e.from)
    }
  }
  const sel = selected === null ? null : (byId(selected) ?? null)
  // On a phone each edge runs down its own lane of the gutter: edges whose
  // spans overlap never share one, so neither the lines nor the labels meet.
  const tallLanes = new Map<number, number>()
  if (L.tall) {
    const spans = edges
      .map((e, i) => {
        const a = byId(e.from)
        const c = byId(e.to)
        return a && c ? { i, lo: Math.min(a.y, c.y), hi: Math.max(a.y, c.y) + TH, pref: e.kind === 'tests' ? 0 : 1 } : null
      })
      .filter((x): x is { i: number; lo: number; hi: number; pref: number } => x !== null)
      .sort((x, y) => x.pref - y.pref || x.hi - x.lo - (y.hi - y.lo))
    const used: { lo: number; hi: number }[][] = []
    for (const sp of spans) {
      let lane = 0
      while ((used[lane] ?? []).some((u) => u.lo < sp.hi && sp.lo < u.hi)) lane++
      ;(used[lane] ??= []).push(sp)
      tallLanes.set(sp.i, lane)
    }
  }
  const drawn = edges.flatMap((e, i) => {
    const a = byId(e.from)
    const c = byId(e.to)
    if (!a || !c) return []
    const lane = tallLanes.get(i) ?? 0
    const g = L.tall ? tallEdge(a, c, lane, L.width) : wideEdge(a, c, e.kind, arcs.get(i), arcRoom)
    const on = selected !== null && (e.from === selected || e.to === selected)
    return [{ e, g, on, key: `${e.from}-${e.to}-${e.kind}-${i}` }]
  })
  const callers = sel === null ? [] : edges.filter((e) => e.to === sel.id && e.kind !== 'tests').map((e) => byId(e.from)).filter((n): n is Placed => n !== undefined)
  const uses = sel === null ? [] : edges.filter((e) => e.from === sel.id && e.kind !== 'tests').map((e) => byId(e.to)).filter((n): n is Placed => n !== undefined)
  const tests = sel === null ? [] : edges.filter((e) => (e.to === sel.id && e.kind === 'tests') || (e.from === sel.id && e.kind === 'tests')).map((e) => byId(e.from === sel.id ? e.to : e.from)).filter((n): n is Placed => n !== undefined)
  const per = L.tall ? 7 : 7.4
  return (
    <div className={`ab-map${L.tall ? ' is-tall' : ''}`} ref={box}>
      {!L.tall && (
        <div className="ab-cols" aria-hidden="true">
          <span>Callers</span>
          <span>Changes</span>
          <span>Tests</span>
        </div>
      )}
      <svg className={`ab-svg${selected === null ? '' : ' has-sel'}`} width={L.width} height={L.height + 4} viewBox={`0 -2 ${L.width} ${L.height + 4}`} role="img" aria-label={`Change map: ${nodes.filter((n) => n.kind === 'changed').length} changed files, their callers and tests`}>
        <defs>
          <marker id="ab-arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L8 4 L0 8 z" className="ab-arrhead" />
          </marker>
          <marker id="ab-arr-on" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L8 4 L0 8 z" className="ab-arrhead is-on" />
          </marker>
        </defs>
        {L.groups.map((g) => (
          <g key={g.area} className={`ab-area${g.area === FROZEN_AREA ? ' is-frozen' : ''}`}>
            <rect x={g.x} y={g.y} width={g.w} height={g.h} rx={10} />
            <text x={g.x + (L.tall ? 8 : AP)} y={g.y + 17}>
              {g.area}
            </text>
          </g>
        ))}
        {drawn.map(({ e, g, on, key }) => (
          <path key={key} d={g.d} className={`ab-edge is-${e.kind}${on ? ' is-on' : selected !== null ? ' is-off' : ''}`} markerEnd={e.kind === 'tests' && !L.tall ? undefined : `url(#${on ? 'ab-arr-on' : 'ab-arr'})`} markerStart={e.kind === 'tests' && !L.tall ? `url(#${on ? 'ab-arr-on' : 'ab-arr'})` : undefined} />
        ))}
        {L.nodes.map((n) => {
          const isSel = n.id === selected
          const dim = selected !== null && !lit.has(n.id)
          return (
            <g
              key={n.id}
              className={`ab-node is-${n.kind}${n.area === FROZEN_AREA ? ' is-frozen' : ''}${isSel ? ' is-sel' : ''}${lit.has(n.id) && !isSel ? ' is-lit' : ''}${dim ? ' is-dim' : ''}`}
              transform={`translate(${n.x} ${n.y})`}
              role="button"
              tabIndex={0}
              aria-pressed={isSel}
              aria-label={`${base(n.file)}, ${n.kind}: ${n.what}`}
              onClick={() => onSelect(isSel ? null : n.id)}
              onKeyDown={(ev) => {
                if (ev.key === 'Enter' || ev.key === ' ') {
                  ev.preventDefault()
                  onSelect(isSel ? null : n.id)
                }
              }}
            >
              <title>{`${n.file}\n${n.what}`}</title>
              <rect width={n.w} height={n.h} rx={8} />
              {multi && n.step !== undefined && (
                <g className="ab-badge" transform={`translate(${n.w - 14} 14)`}>
                  <circle r={10} />
                  <text y={4}>{n.step}</text>
                </g>
              )}
              <text className="ab-n-f" x={12} y={20}>
                {trim(base(n.file), n.w - (multi ? 44 : 24), per)}
              </text>
              <text className="ab-n-s" x={12} y={37}>
                {trim(n.kind === 'changed' ? n.label : `${n.label}`, n.w - 24, 6.6)}
              </text>
            </g>
          )
        })}
        {drawn.map(({ e, g, on, key }) => (
          <text key={`l-${key}`} x={g.lx} y={g.ly + 4} className={`ab-elabel${on ? ' is-on' : selected !== null ? ' is-off' : ''}`} transform={g.rot ? `rotate(-90 ${g.lx} ${g.ly})` : undefined}>
            {EDGE_WORD[e.kind]}
          </text>
        ))}
      </svg>
      {sel !== null ? (
        <div className="ab-pick" aria-live="polite" data-testid="ab-pick" ref={pickRef} style={L.tall && sel !== null ? { top: sel.y + sel.h + 12, width: L.width - 58 } : undefined}>
          <p className="ab-pick-h">
            <span className={`ab-kind is-${sel.kind}`}>{sel.kind === 'changed' ? 'changes' : sel.kind === 'caller' ? 'caller' : 'test'}</span>
            <b className="mono">{base(sel.file)}</b>
            <span className="ab-dim mono">{dir(sel.file)}</span>
            <button type="button" className="c-btn is-ghost is-sm ab-pick-x" aria-label="Clear the selection" onClick={() => onSelect(null)}>
              ×
            </button>
          </p>
          {sel.what !== '' && <p className="ab-pick-w">{sel.what}</p>}
          <dl className="ab-pick-r">
            {callers.length > 0 && (
              <div>
                <dt>{sel.kind === 'changed' ? 'Called by' : 'Uses'}</dt>
                <dd>{callers.map((n) => `${base(n.file)} · ${n.label}`).join(', ')}</dd>
              </div>
            )}
            {uses.length > 0 && (
              <div>
                <dt>{sel.kind === 'caller' ? 'Reaches' : 'Uses'}</dt>
                <dd>{uses.map((n) => `${base(n.file)} · ${n.label}`).join(', ')}</dd>
              </div>
            )}
            {tests.length > 0 && (
              <div>
                <dt>{sel.kind === 'test' ? 'Tests' : 'Tested by'}</dt>
                <dd>{tests.map((n) => base(n.file)).join(', ')}</dd>
              </div>
            )}
          </dl>
        </div>
      ) : null}
    </div>
  )
}

export function Legend({ multi }: { multi: boolean }) {
  return (
    <ul className="ab-legend" aria-label="Legend">
      <li>
        <i className="is-changed" /> changes
      </li>
      <li>
        <i className="is-caller" /> caller
      </li>
      <li>
        <i className="is-test" /> test
      </li>
      {multi && (
        <li>
          <i className="is-step">1</i> step
        </li>
      )}
    </ul>
  )
}

/** The changed files as a list, picking the map's node for each. */
function Changes({ changes, nodes, selected, onSelect }: { changes: MapChange[]; nodes: MapNode[]; selected: string | null; onSelect: (id: string | null) => void }) {
  const nodeOf = (file: string) => nodes.find((n) => n.file === file && n.kind !== 'caller')
  return (
    <ul className="ab-changes" aria-label="Files the plan changes">
      {changes.map((c) => {
        const n = nodeOf(c.file)
        const on = n !== undefined && n.id === selected
        return (
          <li key={c.file} data-on={on ? 'y' : 'n'}>
            <span className={`ab-ck is-${c.kind}`}>{KIND_WORD[c.kind]}</span>
            <button type="button" className="ab-cfile mono" title={c.file} aria-pressed={on} disabled={n === undefined} onClick={() => n && onSelect(on ? null : n.id)}>
              <span className="ab-dim">{dir(c.file)}</span>
              {base(c.file)}
            </button>
            {c.what !== '' && <span className="ab-cwhat">{c.what}</span>}
          </li>
        )
      })}
    </ul>
  )
}

/** The map and the list under it, sharing one selection. */
export function PlanChangeMap({ nodes, edges, changes, multi }: { nodes: MapNode[]; edges: MapEdge[]; changes: MapChange[]; multi: boolean }) {
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <>
      {nodes.length > 0 && <ChangeMap nodes={nodes} edges={edges} multi={multi} selected={selected} onSelect={setSelected} />}
      {changes.length > 0 && <Changes changes={changes} nodes={nodes} selected={selected} onSelect={setSelected} />}
    </>
  )
}
