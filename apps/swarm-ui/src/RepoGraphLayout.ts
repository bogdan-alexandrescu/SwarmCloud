/**
 * WORK › REPOSITORIES › GRAPH: the module canvas's layout -- the seeded force
 * layout, soft bounds, the cluster and collision passes, and the label
 * collision pass -- as pure functions of a GraphView (RepoGraphData.ts).
 *
 * A MODULE OF ITS OWN, AND NEVER IMPORTED STATICALLY BY THE PAGE (lane GR3).
 * repoGraphLayout.worker.ts imports it for a big view; RepoGraph.tsx reaches
 * it only through a dynamic `import()` for a small one. So none of it sits in
 * the main bundle, which GR3's +5 kB gz budget is measured on, and the worker
 * and the page still run the same code. Import types from here with
 * `import type`, which leaves no trace in the bundle.
 */

import { displayLabel, type GraphView } from './RepoGraphData'

// ---- the force layout ------------------------------------------------------

/** A 32-bit FNV-1a hash of a string: the seed of a node's first position. */
export function hash(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

export interface Placed {
  x: number
  y: number
  r: number
}

/** A node's label to place: where its node is, its words, and how connected it is. */
export interface LabelSpot {
  id: string
  x: number
  y: number
  r: number
  text: string
  /** Its degree: the busier node keeps the default place. */
  weight: number
}

/** Where a label went: below its node (the default), above it, beside it, or hidden until hover. */
export interface LabelPlace {
  side: 'below' | 'above' | 'right' | 'left' | 'hidden'
  /** The text's anchor point; with `anchor`, how it hangs off that point. */
  x: number
  /** The text's baseline. */
  y: number
  anchor: 'middle' | 'start' | 'end'
}

/** A label's box, in the canvas's units: the 12px micro type at its average glyph width. */
const LABEL_CHAR_W = 6.2
const LABEL_ASCENT = 11
const LABEL_DESCENT = 3

/** An edge as drawn: a straight segment between two node centres. */
export interface LabelSegment {
  x1: number
  y1: number
  x2: number
  y2: number
}

/** A rectangle a label may not print over, in the canvas's units. */
export type Box = { x0: number; x1: number; y0: number; y1: number }

/** Where RepoGraph.tsx prints a cluster's heading (`.rg-clabel`, at x + 10, y + 16): a box no node label may cover. */
export function clusterLabelBox(b: { cluster: string; x: number; y: number }): Box {
  const y = b.y + 16
  return { x0: b.x + 10, x1: b.x + 10 + b.cluster.length * LABEL_CHAR_W, y0: y - LABEL_ASCENT, y1: y + LABEL_DESCENT }
}

/** Whether a disc crosses a box: the box's nearest point to the centre is inside the disc. */
function discHitsBox(d: { x: number; y: number; r: number }, b: Box): boolean {
  const nx = Math.max(b.x0, Math.min(d.x, b.x1))
  const ny = Math.max(b.y0, Math.min(d.y, b.y1))
  return (d.x - nx) ** 2 + (d.y - ny) ** 2 < d.r * d.r
}

/** Whether a segment crosses a box (Liang-Barsky clipping). */
function segmentHitsBox(s: LabelSegment, b: Box): boolean {
  const dx = s.x2 - s.x1
  const dy = s.y2 - s.y1
  let t0 = 0
  let t1 = 1
  const clip = (p: number, q: number): boolean => {
    if (p === 0) return q >= 0
    const t = q / p
    if (p < 0) {
      if (t > t1) return false
      if (t > t0) t0 = t
    } else {
      if (t < t0) return false
      if (t < t1) t1 = t
    }
    return true
  }
  return clip(-dx, s.x1 - b.x0) && clip(dx, b.x1 - s.x1) && clip(-dy, s.y1 - b.y0) && clip(dy, b.y1 - s.y1) && t0 < t1
}

/**
 * THE LABEL COLLISION PASS (QA G4-11, then V045). At 1440 the package labels
 * of a tight cluster (`apps/agent-worker (2)`, `apps/quota-broker (1)`)
 * printed over each other; after that was fixed, labels still printed over
 * the NEIGHBOURING discs and through the edges (visual QA V045), because only
 * label boxes were obstacles, and over the cluster headings. Labels are
 * placed busiest first. A place is usable when its box crosses no placed
 * label, no cluster heading and no other node's disc; of
 * the usable places, below its node is preferred, then above, then right of
 * it and left of it, and a place an edge runs through is taken only when
 * every usable place has one (the text's halo in repograph.css keeps it
 * legible then). A label with no usable
 * place is hidden until its node is hovered, focused or picked -- the node
 * itself and its accessible name are always drawn.
 */
export function placeLabels(spots: readonly LabelSpot[], segments: readonly LabelSegment[] = [], reserved: readonly Box[] = []): Map<string, LabelPlace> {
  const order = [...spots].sort((a, b) => b.weight - a.weight || a.id.localeCompare(b.id))
  const boxes: Box[] = [...reserved]
  const out = new Map<string, LabelPlace>()
  for (const s of order) {
    const w = s.text.length * LABEL_CHAR_W
    const below = s.y + s.r + 13
    const above = s.y - s.r - 5
    // Beside the node, the text is centred on it: its box's middle sits on the node's.
    const level = s.y + (LABEL_ASCENT - LABEL_DESCENT) / 2
    const places: LabelPlace[] = [
      { side: 'below', x: s.x, y: below, anchor: 'middle' },
      { side: 'above', x: s.x, y: above, anchor: 'middle' },
      { side: 'right', x: s.x + s.r + 4, y: level, anchor: 'start' },
      { side: 'left', x: s.x - s.r - 4, y: level, anchor: 'end' },
    ]
    const boxOf = (p: LabelPlace): Box => {
      const x0 = p.anchor === 'middle' ? p.x - w / 2 : p.anchor === 'start' ? p.x : p.x - w
      return { x0, x1: x0 + w, y0: p.y - LABEL_ASCENT, y1: p.y + LABEL_DESCENT }
    }
    const free = (b: Box) =>
      boxes.every((o) => b.x1 <= o.x0 || o.x1 <= b.x0 || b.y1 <= o.y0 || o.y1 <= b.y0) && spots.every((d) => d === s || !discHitsBox(d, b))
    const clear = (b: Box) => segments.every((e) => !segmentHitsBox(e, b))
    const usable = places.map((p) => ({ p, box: boxOf(p) })).filter((c) => free(c.box))
    const pick = usable.find((c) => clear(c.box)) ?? usable[0]
    if (pick === undefined) {
      out.set(s.id, { side: 'hidden', x: s.x, y: below, anchor: 'middle' })
      continue
    }
    boxes.push(pick.box)
    out.set(s.id, pick.p)
  }
  return out
}

export interface Layout {
  pos: Map<string, Placed>
  /** One soft rectangle per cluster with more than one node. */
  boxes: { cluster: string; x: number; y: number; w: number; h: number }[]
  width: number
  height: number
}

/** A node's radius from its symbol count: 8 to 20, by square root; 10 when not served. */
export function radiusOf(symbols: number | null, isPackage: boolean): number {
  if (symbols === null) return isPackage ? 14 : 10
  return Math.max(8, Math.min(20, 6 + Math.sqrt(symbols) * 1.2))
}

/** The most a sparse drawing is grown to fill the canvas. */
const MAX_GROW = 1.25
/** The smallest a node is drawn when a big view is fitted: still a target to see. */
const MIN_R = 4

/** The clear margin between any node and the canvas edge, in canvas units. */
export const EDGE_CLEAR = 24
/** Extra room at the top, under the canvas's own controls and a cluster's name. */
/**
 * How hard a node is pulled toward its cluster's centre, against the pairwise
 * repulsion. The old 0.9 let an opened cluster of 25 spread over the whole
 * canvas and its rectangle swallow folded clusters; 6 keeps each cluster a
 * compact disc (chosen on the 48-, 200- and 2,000-module fixtures, opened and
 * folded: no node inside another cluster's rectangle, none touching).
 */
const HOME_PULL = 6
const TOP_CLEAR = 30
/** Room under the lowest node for its label. */
const LABEL_BELOW = 14

/**
 * SOFT BOUNDS (§3.2 defect 8). The old layout clamped every node into the box
 * on every round, so a cluster pushed outward came to rest in a row along the
 * floor. A wall force in its place still queued nodes along the wall where it
 * balanced the repulsion (measured: four nodes on one line at 48 modules). So
 * no wall acts while the layout runs -- the pull toward each cluster's centre
 * already holds the drawing together -- and when it settles the whole drawing
 * is scaled about its centre, radii with it (shrunk, or grown up to
 * MAX_GROW), into the box inset by EDGE_CLEAR: the picture is kept, zoomed,
 * and no node rests on the canvas edge.
 */
function fitInto(xs: Float64Array, ys: Float64Array, rs: Float64Array, width: number, height: number) {
  const n = xs.length
  if (n === 0) return
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (let i = 0; i < n; i++) {
    minX = Math.min(minX, xs[i]! - rs[i]!)
    maxX = Math.max(maxX, xs[i]! + rs[i]!)
    minY = Math.min(minY, ys[i]! - rs[i]!)
    maxY = Math.max(maxY, ys[i]! + rs[i]!)
  }
  const x0 = EDGE_CLEAR
  const x1 = width - EDGE_CLEAR
  const y0 = EDGE_CLEAR + TOP_CLEAR
  const y1 = height - EDGE_CLEAR - LABEL_BELOW
  // A similarity: positions AND radii scale together, so the picture is the
  // same picture zoomed, and what the passes before it made true (no overlap,
  // no node inside another cluster's rectangle) stays true.
  const s = Math.min(MAX_GROW, (x1 - x0) / Math.max(maxX - minX, 1e-6), (y1 - y0) / Math.max(maxY - minY, 1e-6))
  const cx = (minX + maxX) / 2
  const cy = (minY + maxY) / 2
  for (let i = 0; i < n; i++) {
    xs[i] = (x0 + x1) / 2 + (xs[i]! - cx) * s
    ys[i] = (y0 + y1) / 2 + (ys[i]! - cy) * s
    rs[i] = Math.max(MIN_R, rs[i]! * s)
  }
}

/** How hard an edge between two clusters pulls, against one inside a cluster. */
const CROSS_SPRING = 0.05

/** How far, per square root of its member count, an opened middle cluster spreads. */
const MIDDLE_PACK = 18

/** Past this many nodes the layout is a crowd: fewer rounds, no separation passes. */
export const CROWD_AT = 600

/** The least clear space between two drawn nodes, in canvas units. */
const NODE_GAP = 4

/**
 * Push apart every pair of nodes closer than their radii plus NODE_GAP, half
 * the shortfall each, for a bounded number of sweeps. Deterministic: a pair on
 * one point parts along a direction hashed from their ids. True when it moved
 * anything.
 */
function separate(xs: Float64Array, ys: Float64Array, rs: Float64Array, ids: readonly string[]): boolean {
  const n = xs.length
  let movedAny = false
  for (let sweep = 0; sweep < 12; sweep++) {
    let moved = false
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = xs[j]! - xs[i]!
        let dy = ys[j]! - ys[i]!
        const want = rs[i]! + rs[j]! + NODE_GAP
        if (Math.abs(dx) >= want || Math.abs(dy) >= want) continue
        let d = Math.sqrt(dx * dx + dy * dy)
        if (d >= want) continue
        if (d < 0.01) {
          const a = ((hash(ids[i]! + ids[j]!) % 360) * Math.PI) / 180
          dx = Math.cos(a)
          dy = Math.sin(a)
          d = 1
        }
        const push = (want - d) / 2 / d
        xs[i] = xs[i]! - dx * push
        ys[i] = ys[i]! - dy * push
        xs[j] = xs[j]! + dx * push
        ys[j] = ys[j]! + dy * push
        moved = true
      }
    }
    if (!moved) break
    movedAny = true
  }
  return movedAny
}

/** A cluster rectangle's padding round its members: as `forceLayout` draws it, plus NODE_GAP. */
const BOX_PAD = { left: 14, top: 24, right: 14, bottom: 22 }

/**
 * Move every node that sits inside ANOTHER cluster's rectangle out of it,
 * along the ray from that rectangle's centre, to just past its edge. A round
 * cluster's rectangle has corners, and a folded cluster on the ring round it
 * came to rest in one (measured: 12 of 16 on the 2,000 fixture, opened two
 * levels). Biggest cluster first; true when it moved anything.
 */
function evict(xs: Float64Array, ys: Float64Array, rs: Float64Array, clusterOf: readonly string[]): boolean {
  const members = new Map<string, number[]>()
  clusterOf.forEach((c, i) => {
    if (c === '') return
    const m = members.get(c)
    if (m === undefined) members.set(c, [i])
    else m.push(i)
  })
  let moved = false
  const groups = [...members.entries()].filter(([, m]) => m.length > 1).sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
  for (const [c, m] of groups) {
    const x0 = Math.min(...m.map((i) => xs[i]! - rs[i]!)) - BOX_PAD.left
    const x1 = Math.max(...m.map((i) => xs[i]! + rs[i]!)) + BOX_PAD.right
    const y0 = Math.min(...m.map((i) => ys[i]! - rs[i]!)) - BOX_PAD.top
    const y1 = Math.max(...m.map((i) => ys[i]! + rs[i]!)) + BOX_PAD.bottom
    const mx = (x0 + x1) / 2
    const my = (y0 + y1) / 2
    for (let i = 0; i < xs.length; i++) {
      if (clusterOf[i] === c) continue
      const r = rs[i]! + NODE_GAP
      if (xs[i]! <= x0 - r || xs[i]! >= x1 + r || ys[i]! <= y0 - r || ys[i]! >= y1 + r) continue
      let dx = xs[i]! - mx
      let dy = ys[i]! - my
      if (Math.abs(dx) < 1e-6 && Math.abs(dy) < 1e-6) dy = -1
      // The least stretch of the ray that clears the rectangle on either axis.
      const sx = dx === 0 ? Infinity : ((dx > 0 ? x1 + r : x0 - r) - mx) / dx
      const sy = dy === 0 ? Infinity : ((dy > 0 ? y1 + r : y0 - r) - my) / dy
      const s = Math.min(sx, sy) + 1e-3
      xs[i] = mx + dx * s
      ys[i] = my + dy * s
      moved = true
    }
  }
  return moved
}

/**
 * A deterministic force layout: clusters start on a ring, members around their
 * cluster's centre at a hashed angle, then `iterations` rounds of pairwise
 * repulsion, edge springs and a pull toward the cluster centre, cooled
 * linearly. No Math.random anywhere: the same view gives the same picture.
 */
export function forceLayout(view: GraphView, width = 640, height = 440, iterations?: number): Layout {
  const pos = new Map<string, Placed>()
  const n = view.nodes.length
  // A crowd settles in fewer rounds, and every round is O(n^2): half the
  // rounds halves the worker's wait on an unclustered 2,000-module view.
  const rounds = iterations ?? (n > CROWD_AT ? 80 : 160)
  if (n === 0) return { pos, boxes: [], width, height }
  const clusters = view.clusters.length === 0 ? [''] : view.clusters
  const cx = width / 2
  const cy = height / 2
  // The cluster centres sit on an ellipse the canvas's shape, not a circle
  // its short side allows: a dozen clusters on a 640x440 canvas otherwise
  // crowd the middle third and their labels print over each other.
  // An opened cluster that holds most of the view (a drill-in: its modules
  // beside a ring of folded clusters) takes the middle, and the rest the ring.
  const size = new Map<string, number>()
  for (const v of view.nodes) size.set(v.cluster, (size.get(v.cluster) ?? 0) + 1)
  const biggest = [...clusters].sort((a, b) => (size.get(b) ?? 0) - (size.get(a) ?? 0) || a.localeCompare(b))[0]!
  const middle = clusters.length > 1 && (size.get(biggest) ?? 0) >= n * 0.3 ? biggest : null
  const ring = clusters.filter((c) => c !== middle)
  const one = ring.length === 1 && middle === null
  // Round a middle cluster the ring keeps clear of it: its members pack to a
  // disc about MIDDLE_PACK * sqrt(members) across (the fit rescales after).
  const clear = middle === null ? 0 : MIDDLE_PACK * Math.sqrt(size.get(middle) ?? 1) + 40
  const ringX = one ? 0 : Math.max(width * (middle === null ? 0.34 : 0.4), clear * 1.2)
  const ringY = one ? 0 : Math.max(height * (middle === null ? 0.32 : 0.38), clear)
  const centre = new Map<string, { x: number; y: number }>()
  if (middle !== null) centre.set(middle, { x: cx, y: cy })
  ring.forEach((c, i) => {
    const a = (2 * Math.PI * i) / ring.length - Math.PI / 2
    centre.set(c, { x: cx + ringX * Math.cos(a), y: cy + ringY * Math.sin(a) })
  })
  const ids = view.nodes.map((v) => v.id)
  const xs = new Float64Array(n)
  const ys = new Float64Array(n)
  const rs = new Float64Array(n)
  const home: { x: number; y: number }[] = []
  view.nodes.forEach((v, i) => {
    const c = centre.get(v.cluster) ?? { x: cx, y: cy }
    const h = hash(v.id)
    const a = ((h % 3600) / 3600) * 2 * Math.PI
    const d = 20 + ((h >>> 12) % 50)
    xs[i] = c.x + d * Math.cos(a)
    ys[i] = c.y + d * Math.sin(a)
    rs[i] = radiusOf(v.symbols, v.isPackage)
    home.push(c)
  })
  const index = new Map(ids.map((id, i) => [id, i]))
  // An edge inside a cluster pulls at full strength; one across clusters
  // barely: a folded cluster with forty edges into an opened one was dragged
  // into the middle of it, inside its rectangle (measured on the 2,000 fixture).
  const springs = view.edges
    .map((e) => [index.get(e.from), index.get(e.to)] as const)
    .filter((p): p is readonly [number, number] => p[0] !== undefined && p[1] !== undefined)
    .map(([a, b]) => [a, b, view.nodes[a]!.cluster === view.nodes[b]!.cluster ? 1 : CROSS_SPRING] as const)
  const k = Math.sqrt((width * height) / Math.max(n, 1)) * 0.55
  for (let it = 0; it < rounds; it++) {
    const t = 1 - it / rounds
    const fx = new Float64Array(n)
    const fy = new Float64Array(n)
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = xs[i]! - xs[j]!
        let dy = ys[i]! - ys[j]!
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) {
          // Two nodes on one point: part them along a hashed direction.
          const a = ((hash(ids[i]! + ids[j]!) % 360) * Math.PI) / 180
          dx = Math.cos(a)
          dy = Math.sin(a)
          d2 = 1
        }
        const d = Math.sqrt(d2)
        const f = (k * k) / d
        fx[i] = fx[i]! + (dx / d) * f
        fy[i] = fy[i]! + (dy / d) * f
        fx[j] = fx[j]! - (dx / d) * f
        fy[j] = fy[j]! - (dy / d) * f
      }
    }
    for (const [a, b, w] of springs) {
      const dx = xs[b]! - xs[a]!
      const dy = ys[b]! - ys[a]!
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01)
      const f = ((d * d) / k / 4) * w
      fx[a] = fx[a]! + (dx / d) * f
      fy[a] = fy[a]! + (dy / d) * f
      fx[b] = fx[b]! - (dx / d) * f
      fy[b] = fy[b]! - (dy / d) * f
    }
    const step = 12 * t + 0.5
    for (let i = 0; i < n; i++) {
      const h = home[i]!
      fx[i] = fx[i]! + (h.x - xs[i]!) * HOME_PULL
      fy[i] = fy[i]! + (h.y - ys[i]!) * HOME_PULL
      const m = Math.sqrt(fx[i]! * fx[i]! + fy[i]! * fy[i]!)
      if (m > 0) {
        xs[i] = xs[i]! + (fx[i]! / m) * Math.min(m, step)
        ys[i] = ys[i]! + (fy[i]! / m) * Math.min(m, step)
      }
    }
  }
  // Fitting shrinks distances but not radii: part any two nodes the shrink
  // pressed together, and fit again, a few times over. Each round also moves
  // a node out of any other cluster's rectangle it came to rest in.
  const clusterOf = view.nodes.map((v) => v.cluster)
  // Past CROWD_AT nodes there is no room on 640x440 for every node to stand
  // clear (2,000 discs of radius 4 want most of it), and the passes only churn.
  for (let round = 0; round < (n > CROWD_AT ? 0 : 6); round++) {
    fitInto(xs, ys, rs, width, height)
    const evicted = evict(xs, ys, rs, clusterOf)
    const parted = separate(xs, ys, rs, ids)
    if (!evicted && !parted) break
  }
  fitInto(xs, ys, rs, width, height)
  view.nodes.forEach((v, i) => pos.set(v.id, { x: Math.round(xs[i]! * 10) / 10, y: Math.round(ys[i]! * 10) / 10, r: rs[i]! }))
  const boxes: Layout['boxes'] = []
  for (const c of view.clusters) {
    const members = view.nodes.filter((v) => v.cluster === c).map((v) => pos.get(v.id)!)
    if (members.length < 2 || c === '') continue
    const x0 = Math.min(...members.map((p) => p.x - p.r)) - 14
    const y0 = Math.min(...members.map((p) => p.y - p.r)) - 24
    const x1 = Math.max(...members.map((p) => p.x + p.r)) + 14
    const y1 = Math.max(...members.map((p) => p.y + p.r)) + 22
    boxes.push({ cluster: c, x: x0, y: y0, w: x1 - x0, h: y1 - y0 })
  }
  return { pos, boxes, width, height }
}

/** Everything the module canvas needs placed: the layout and where each label went. */
export interface LayoutPlan extends Layout {
  labels: Map<string, LabelPlace>
}

/** What the layout worker is sent (repoGraphLayout.worker.ts): a structured clone of these. */
export interface LayoutRequest {
  view: GraphView
  width: number
  height: number
}

/**
 * The whole O(n^2) part of drawing the module canvas -- the force layout and
 * the label collision pass -- as one pure function of the view, so the page
 * can run it in a Web Worker and the main thread stays responsive (§3.2
 * defect 9: 2,000 modules froze the page for 3.4 s of layout alone).
 */
export function layoutPlan(view: GraphView, width = 640, height = 440): LayoutPlan {
  const layout = forceLayout(view, width, height)
  const deg = new Map<string, number>()
  for (const e of view.edges) {
    deg.set(e.from, (deg.get(e.from) ?? 0) + 1)
    deg.set(e.to, (deg.get(e.to) ?? 0) + 1)
  }
  const labels = placeLabels(
    view.nodes.flatMap((n) => {
      const p = layout.pos.get(n.id)
      return p === undefined ? [] : [{ id: n.id, x: p.x, y: p.y, r: p.r, text: displayLabel(n), weight: deg.get(n.id) ?? 0 }]
    }),
    view.edges.flatMap((e) => {
      const a = layout.pos.get(e.from)
      const b = layout.pos.get(e.to)
      return a === undefined || b === undefined ? [] : [{ x1: a.x, y1: a.y, x2: b.x, y2: b.y }]
    }),
    layout.boxes.map(clusterLabelBox),
  )
  return { ...layout, labels }
}
