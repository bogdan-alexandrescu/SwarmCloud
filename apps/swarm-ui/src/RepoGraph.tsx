import { memo, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { loadRepositoryGraph, loadSymbolGraph, loadSymbolTests, searchRepositorySymbols } from './api'
import { Button, Card, Chip, Dash, EmptyState } from './components'
import type { Result } from './fetch'
import { Mark } from './primitives'
import {
  DEPTH_DEFAULT, DEPTH_MAX, DEPTH_MIN, EVIDENCE_LEGEND, LEGEND, callColumns, clampDepth, degree, displayLabel, edgeLook, edgeWords, fmt,
  graphView, heatOf, linesWord, neighbours, short,
  type CallGraph, type ColourBy, type GraphView, type Heat, type ModuleGraph, type Neighbour, type SymbolRow, type ViewEdge, type ViewNode,
} from './RepoGraphData'
// Types only: the layout itself is a lazy import (see `loadLayout`), so it stays out of the main bundle.
import type { LayoutPlan, LayoutRequest } from './RepoGraphLayout'
import { repoName, type RepoRecord } from './RepositoriesData'
import { UrRadio, UrRegion, useUrRead } from './RepositoriesParts'
import './styles/repograph.css'

/**
 * ONE REPOSITORY › GRAPH, pick A (repositories.html screen 8): a
 * force-directed canvas with a side inspector. Three views on one surface,
 * switched by the toolbar: the MODULE dependency graph (clustered by directory,
 * coloured by hot-spots or test reach, zoom and pan), a symbol's CALL GRAPH
 * (callers left, callees right, a depth control, each edge's evidence and
 * confidence), and the TESTS REACHING A SYMBOL. That view was once called
 * "Test map", the name of the repository's own tab, which answers a
 * different question -- path -> tests, where this one is symbol -> tests --
 * so it is named for its question (QA G4-09), and each Test map row links
 * here (`?view=tests&q=<directory>`).
 *
 * EVERY FIGURE IS THE ROUTE'S. The canvas draws what `GET .../graph` serves
 * and nothing else: a module whose hot-spot count or reach was not served is
 * hatched ("not measured", components.html A), never drawn as cool; a folded
 * package's reach is a dash, because reach is served per module as a ratio.
 * The lines of a module are not served by the graph route, so the inspector
 * says so rather than counting them.
 *
 * TWO DRAWINGS OF ONE VIEW (lane GR3, docs/design/graph-rendering.md §3 and
 * §8). STRUCTURE, the default, is the console's own SVG: the force layout is
 * RepoGraphData.ts's, seeded by module path so the picture is the same on
 * every read, run in a Web Worker past IN_PLACE_MAX nodes, with soft bounds
 * and labels unique within the view. NETWORK is vis-network with Graphify's
 * physics, in a chunk loaded only when first opened (RepoGraphNetwork.tsx).
 * Both draw the same GraphView: clustered by directory, folded to a cluster
 * meta-graph past COLLAPSE_AT modules and opened on click, filtered by the
 * same legend, picked into the same inspector with its neighbour list.
 *
 * Classes are `rg-`, local to this section, so a later pass can swap each for
 * lane U0's canonical components by name.
 */

type View = 'modules' | 'calls' | 'tests'
type Direction = 'both' | 'callers' | 'callees'

const GRAPH_ROUTE = 'GET /v1/repositories/{repo_id}/graph'
const SYMBOLS_ROUTE = 'GET /v1/repositories/{repo_id}/symbols'

/** What the address opens the Graph tab on: `view=tests` and a symbol search `q` (a Test map row's link). */
export interface GraphOpen {
  view: string | null
  q: string | null
}

export function GraphTab({ r, open = null }: { r: RepoRecord; open?: GraphOpen | null }) {
  const graph = useUrRead(() => loadRepositoryGraph(r.repo_id), `graph:${r.repo_id}`)
  return (
    <div className="rg-graph">
      <GraphRegion state={graph.state} onRetry={graph.reload}>
        {(g) => <GraphSurface r={r} g={g} open={open} />}
      </GraphRegion>
    </div>
  )
}

/**
 * The graph read in its states. An index promoted WITHOUT a graph answers 404
 * `no_graph`: that is the route working, and it is said as "no graph", never
 * as "not served".
 */
function GraphRegion({ state, onRetry, children }: { state: Result<ModuleGraph | null>; onRetry: () => void; children: (g: ModuleGraph) => ReactNode }) {
  if (state.status === 'error' && state.error.code === 'no_graph') {
    return (
      <EmptyState kind="partial" heading="No graph for this index">
        {state.error.message}. The next index run that builds the AST and LSP passes draws it here.
      </EmptyState>
    )
  }
  return (
    <UrRegion
      state={state}
      route={GRAPH_ROUTE}
      what="The module dependency graph"
      onRetry={onRetry}
      lines={6}
      empty={<EmptyState kind="partial" heading="No graph to draw">The graph route answered with no modules.</EmptyState>}
    >
      {(g) =>
        g === null || g.modules.length === 0 ? (
          <EmptyState kind="partial" heading="No graph to draw">
            The graph route answered with no modules this page can draw.
          </EmptyState>
        ) : (
          children(g)
        )
      }
    </UrRegion>
  )
}

function GraphSurface({ r, g, open: opened }: { r: RepoRecord; g: ModuleGraph; open: GraphOpen | null }) {
  const [view, setView] = useState<View>(opened?.view === 'tests' ? 'tests' : opened?.view === 'calls' ? 'calls' : 'modules')
  // The search a Test map row asked for; dropped once a module is picked on the canvas.
  const [seed, setSeed] = useState<string | null>(opened?.q !== undefined && opened.q !== null && opened.q !== '' ? opened.q : null)
  const [colour, setColour] = useState<ColourBy>('hot-spots')
  const [cluster, setCluster] = useState(true)
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  const [picked, setPicked] = useState<string | null>(null)
  const [symbol, setSymbol] = useState<SymbolRow | null>(null)
  const [depth, setDepth] = useState(DEPTH_DEFAULT)
  const [direction, setDirection] = useState<Direction>('both')
  const [hiddenHeat, setHiddenHeat] = useState<ReadonlySet<Heat>>(new Set())
  const [hiddenClusters, setHiddenClusters] = useState<ReadonlySet<string>>(new Set())
  const [drawing, setDrawing] = useState<Drawing>(readDrawing)
  const [net, setNet] = useState<{ mod: NetworkModule | null; failed: string | null }>({ mod: null, failed: null })

  const gv = useMemo(() => graphView(g, open, cluster), [g, open, cluster])
  const phone = usePhoneWidth()
  const selected = gv.nodes.find((n) => n.id === picked) ?? hottest(gv.nodes)
  const shown = useMemo(
    () => new Set(gv.nodes.filter((n) => !hiddenHeat.has(heatOf(n, colour)) && !hiddenClusters.has(n.cluster)).map((n) => n.id)),
    [gv, colour, hiddenHeat, hiddenClusters],
  )

  // The Network view's chunk loads the first time it is opened, and only then.
  useEffect(() => {
    if (view !== 'modules' || drawing !== 'network' || net.mod !== null || net.failed !== null) return
    let live = true
    loadNetwork().then(
      (mod) => live && setNet({ mod, failed: null }),
      (e: unknown) => {
        if (!live) return
        // The switch says so in words and the canvas stays on Structure. The
        // remembered choice is kept: a chunk that failed once may load next visit.
        setNet({ mod: null, failed: e instanceof Error ? e.message : String(e) })
        setDrawing('structure')
      },
    )
    return () => {
      live = false
    }
  }, [view, drawing, net])

  function choose(d: Drawing) {
    setDrawing(d)
    rememberDrawing(d)
  }

  function select(n: ViewNode) {
    setPicked(n.id)
    setSeed(null)
    setSymbol(null)
  }

  function pick(n: ViewNode) {
    if (n.isPackage) {
      // Folded past COLLAPSE_AT: a cluster opens into its modules (or its own clusters) on click.
      setOpen(new Set([...open, n.cluster]))
      setPicked(null)
      setSeed(null)
      setSymbol(null)
    } else {
      select(n)
    }
  }

  function setColourBy(c: ColourBy) {
    setColour(c)
    // The bands are the colouring's own: a band hidden under hot-spots means nothing under test reach.
    setHiddenHeat(new Set())
  }

  const tools = (
    <div className="rg-tools">
      <UrRadio
        label="Graph view"
        value={view}
        onChange={setView}
        options={[
          { key: 'modules', label: 'Modules' },
          { key: 'calls', label: 'Call graph' },
          { key: 'tests', label: 'Tests reaching a symbol' },
        ]}
      />
      {view === 'modules' && (
        <>
          <Chip onClick={() => setCluster(!cluster)} pressed={cluster} title="Group modules by the directory they sit in, and fold a large repository into those groups">
            Cluster: directory
          </Chip>
          <UrRadio
            label="Colour"
            value={colour}
            onChange={setColourBy}
            options={[
              { key: 'hot-spots', label: 'hot-spots' },
              { key: 'test-reach', label: 'test reach' },
            ]}
          />
          {open.size > 0 && (
            <Button size="sm" onClick={() => setOpen(new Set())}>
              Fold clusters
            </Button>
          )}
        </>
      )}
    </div>
  )

  // The drawing switch sits in the canvas card's own control bar, opposite
  // zoom and Fit: it changes how THIS card draws, so it lives on the card.
  const bar = (
    <UrRadio
      label="Graph view: Structure / Network"
      value={drawing}
      onChange={choose}
      options={[
        { key: 'structure', label: 'Structure', title: 'Clusters, labels and soft bounds, drawn by the console' },
        net.failed === null
          ? { key: 'network', label: 'Network', title: 'The same graph drawn with force physics (vis-network)' }
          : { key: 'network', label: 'Network unavailable', title: `The network view could not load: ${net.failed}`, disabled: true },
      ]}
    />
  )

  const legend = (
    <GraphLegend
      gv={gv}
      g={g}
      colour={colour}
      shown={shown}
      hiddenHeat={hiddenHeat}
      hiddenClusters={hiddenClusters}
      onHeat={(h) => setHiddenHeat(toggled(hiddenHeat, h))}
      onCluster={(c) => setHiddenClusters(toggled(hiddenClusters, c))}
      onAll={() => {
        setHiddenHeat(new Set())
        setHiddenClusters(new Set())
      }}
    />
  )

  const depthControl = <DepthControl depth={depth} onDepth={(d) => setDepth(clampDepth(d))} />

  const canvas =
    drawing === 'network' && net.mod !== null ? (
      <net.mod.NetworkCanvas name={repoName(r)} gv={gv} colour={colour} shown={shown} selected={selected?.id ?? null} onPick={pick} bar={bar} legend={legend} />
    ) : drawing === 'network' ? (
      <div className="rg-canvas" data-drawing="network">
        <div className="rg-bar">{bar}</div>
        <div className="rg-net rg-wait" role="status">
          Loading the network view…
        </div>
        {legend}
      </div>
    ) : (
      <ModuleCanvas r={r} gv={gv} colour={colour} shown={shown} selected={selected?.id ?? null} onPick={pick} bar={bar} legend={legend} />
    )

  return (
    <>
      {tools}
      {view === 'modules' && (
        <div className="rg-split">
          <div className="rg-main">
            {canvas}
            {phone && <PhoneList gv={gv} colour={colour} selected={selected?.id ?? null} onPick={pick} />}
          </div>
          <Inspector
            r={r}
            gv={gv}
            node={selected}
            symbol={symbol}
            onSymbol={setSymbol}
            depth={depth}
            depthControl={depthControl}
            direction={direction}
            onOpen={(n) => pick(n)}
            onSelect={select}
          />
        </div>
      )}
      {view === 'calls' && (
        <CallView
          r={r}
          node={selected}
          symbol={symbol}
          onSymbol={setSymbol}
          depth={depth}
          depthControl={depthControl}
          direction={direction}
          onDirection={setDirection}
        />
      )}
      {view === 'tests' && <TestView r={r} node={selected} seed={seed} symbol={symbol} onSymbol={setSymbol} />}
      <GraphFoot r={r} g={g} />
    </>
  )
}

function toggled<T>(set: ReadonlySet<T>, v: T): ReadonlySet<T> {
  const next = new Set(set)
  if (next.has(v)) next.delete(v)
  else next.add(v)
  return next
}

/** The node the inspector opens on before any click: the hottest module, served counts first. */
function hottest(nodes: readonly ViewNode[]): ViewNode | null {
  let best: ViewNode | null = null
  for (const n of nodes) {
    if (best === null) best = n
    else if ((n.hot_spot_changes ?? -1) > (best.hot_spot_changes ?? -1)) best = n
  }
  return best
}

// ---------------------------------------------------------------------------
// Structure | Network: which drawing the card shows, remembered per browser
// ---------------------------------------------------------------------------

type Drawing = 'structure' | 'network'
export const DRAWING_KEY = 'swarm.repograph.drawing'

function readDrawing(): Drawing {
  try {
    return window.localStorage.getItem(DRAWING_KEY) === 'network' ? 'network' : 'structure'
  } catch {
    // Storage refused (a private window, a sandboxed frame): Structure, the default.
    return 'structure'
  }
}

function rememberDrawing(d: Drawing) {
  try {
    window.localStorage.setItem(DRAWING_KEY, d)
  } catch {
    // Not remembered; the choice still holds for this page.
  }
}

type NetworkModule = typeof import('./RepoGraphNetwork')
let networkChunk: Promise<NetworkModule> | null = null

/**
 * vis-network arrives ONLY through this dynamic import, so its ~159 kB gz is
 * a chunk of its own and the main bundle stays inside GR3's +5 kB gz. A
 * failed load is forgotten, so the next visit tries again.
 */
function loadNetwork(): Promise<NetworkModule> {
  networkChunk ??= import('./RepoGraphNetwork').catch((e: unknown) => {
    networkChunk = null
    throw e
  })
  return networkChunk
}

// ---------------------------------------------------------------------------
// The module canvas
// ---------------------------------------------------------------------------

const W = 640
const H = 440
const ZOOM_STEP = 1.25
/**
 * At or under this many nodes the layout runs in place: about 15 ms, less
 * than a worker takes to start. Above it, it runs in a Web Worker
 * (repoGraphLayout.worker.ts), so the page never freezes on a big view.
 */
export const IN_PLACE_MAX = 120
/** Nodes (or edges) per chunk: a big view mounts one chunk a frame, so no frame holds the page for long. */
const CHUNK = 100

interface Zoom {
  k: number
  tx: number
  ty: number
}
const FIT: Zoom = { k: 1, tx: 0, ty: 0 }

type PlanState = { status: 'ready'; plan: LayoutPlan } | { status: 'laying'; where: 'here' | 'worker' } | { status: 'failed'; why: string }

type LayoutModule = typeof import('./RepoGraphLayout')
let layoutModule: LayoutModule | null = null
let layoutChunk: Promise<LayoutModule> | null = null

/**
 * The layout code, loaded once and then held: a small view is laid out in
 * place from it, synchronously after the first load. A dynamic import keeps
 * it out of the main bundle (measured: it put GR3 0.4 kB gz over its +5 kB).
 */
function loadLayout(): Promise<LayoutModule> {
  layoutChunk ??= import('./RepoGraphLayout').then(
    (m) => (layoutModule = m),
    (e: unknown) => {
      layoutChunk = null
      throw e
    },
  )
  return layoutChunk
}

/**
 * The layout of a view: in place for a small one, in a worker for a big one.
 * A worker that cannot start or fails says so, and the reader may lay the
 * view out on the page instead -- the page pauses while it does, and is told
 * so first. Never a guessed position in the meantime.
 */
function usePlan(gv: GraphView): PlanState & { here: () => void } {
  const inPlace = gv.nodes.length <= IN_PLACE_MAX || typeof Worker === 'undefined'
  const [forced, setForced] = useState<GraphView | null>(null)
  const here = inPlace || forced === gv
  const [mod, setMod] = useState<{ m: LayoutModule | null; why: string | null }>({ m: layoutModule, why: null })
  useEffect(() => {
    if (!here || mod.m !== null) return
    let live = true
    loadLayout().then(
      (m) => live && setMod({ m, why: null }),
      (e: unknown) => live && setMod({ m: null, why: e instanceof Error ? e.message : String(e) }),
    )
    return () => {
      live = false
    }
  }, [here, mod.m])
  const local = useMemo(() => (here && mod.m !== null ? mod.m.layoutPlan(gv, W, H) : null), [gv, here, mod.m])
  const [off, setOff] = useState<{ gv: GraphView; plan: LayoutPlan | null; why: string | null } | null>(null)
  useEffect(() => {
    if (inPlace) return
    let worker: Worker
    try {
      worker = new Worker(new URL('./repoGraphLayout.worker.ts', import.meta.url), { type: 'module' })
    } catch (e) {
      setOff({ gv, plan: null, why: e instanceof Error ? e.message : String(e) })
      return
    }
    worker.onmessage = (e: MessageEvent<{ plan: LayoutPlan }>) => setOff({ gv, plan: e.data.plan, why: null })
    worker.onerror = (e: ErrorEvent) => setOff({ gv, plan: null, why: e.message || 'the layout worker stopped' })
    const req: LayoutRequest = { view: gv, width: W, height: H }
    worker.postMessage(req)
    // A newer view (a cluster opened, clustering turned off) drops the old job.
    return () => worker.terminate()
  }, [gv, inPlace])
  const again = () => setForced(gv)
  if (local !== null) return { status: 'ready', plan: local, here: again }
  if (here && mod.why !== null) return { status: 'failed', why: `the layout did not load: ${mod.why}`, here: again }
  if (!here && off?.gv === gv && off.plan !== null) return { status: 'ready', plan: off.plan, here: again }
  if (!here && off?.gv === gv && off.why !== null) return { status: 'failed', why: off.why, here: again }
  return { status: 'laying', where: here ? 'here' : 'worker', here: again }
}

/**
 * How many of `total` chunks to mount: one more every frame, so no frame
 * builds more than one chunk. All at once where there is no frame clock (jsdom).
 */
function useMounted(total: number, key: unknown): number {
  const [s, setS] = useState({ key, n: Math.min(total, 1) })
  const n = s.key === key ? s.n : Math.min(total, 1)
  useEffect(() => {
    if (total <= 1 || typeof requestAnimationFrame === 'undefined') {
      setS({ key, n: total })
      return
    }
    let at = 1
    let raf = 0
    const step = () => {
      at = Math.min(total, at + 1)
      setS({ key, n: at })
      if (at < total) raf = requestAnimationFrame(step)
    }
    setS({ key, n: at })
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [total, key])
  return Math.min(n, total)
}

function chunked<T>(xs: readonly T[]): T[][] {
  const out: T[][] = []
  for (let i = 0; i < xs.length; i += CHUNK) out.push(xs.slice(i, i + CHUNK))
  return out
}

/**
 * A chunk of nodes, memoised: a frame that mounts the next chunk, or a click
 * that moves the selection, re-renders only the chunks it changes. (Each
 * frame re-rendering every node mounted so far measured 90-132 ms tasks at
 * 2,000 nodes.) `selected` is passed only to the chunk that holds it.
 */
const NodeChunk = memo(function NodeChunk({ nodes, plan, colour, selected, onPick }: {
  nodes: readonly ViewNode[]
  plan: LayoutPlan
  colour: ColourBy
  selected: string | null
  onPick: (n: ViewNode) => void
}) {
  return (
    <>
      {nodes.map((n) => {
        const p = plan.pos.get(n.id)
        if (p === undefined) return null
        const heat = heatOf(n, colour)
        const on = n.id === selected
        const label = plan.labels.get(n.id)
        return (
          <g
            key={n.id}
            className={on ? 'rg-node is-sel' : 'rg-node'}
            data-id={n.id}
            data-heat={heat}
            role="button"
            tabIndex={0}
            aria-pressed={on}
            aria-label={n.isPackage ? `${n.label}, ${n.members} modules: open the cluster` : n.id}
            onClick={() => onPick(n)}
            onKeyDown={(e: KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onPick(n)
              }
            }}
          >
            <circle cx={p.x} cy={p.y} r={p.r} />
            <text
              x={p.x}
              y={label?.y ?? p.y + p.r + 13}
              textAnchor="middle"
              data-label={label?.side ?? 'below'}
              className={label?.side === 'hidden' ? 'is-hidden' : undefined}
            >
              {displayLabel(n)}
            </text>
          </g>
        )
      })}
    </>
  )
})

const EdgeChunk = memo(function EdgeChunk({ edges, plan, selected, maxWeight }: {
  edges: readonly ViewEdge[]
  plan: LayoutPlan
  selected: string | null
  maxWeight: number
}) {
  return (
    <>
      {edges.map((e) => {
        const a = plan.pos.get(e.from)
        const b = plan.pos.get(e.to)
        if (a === undefined || b === undefined) return null
        const on = selected !== null && (e.from === selected || e.to === selected)
        return (
          <line
            key={`${e.from}>${e.to}`}
            className={on ? 'rg-edge is-on' : 'rg-edge'}
            x1={a.x}
            y1={a.y}
            x2={b.x}
            y2={b.y}
            strokeWidth={round(1 + (2.5 * e.weight) / maxWeight)}
          >
            <title>{`${e.from} → ${e.to} · ${fmt(e.weight)} resolved ${e.weight === 1 ? 'call' : 'calls'}`}</title>
          </line>
        )
      })}
    </>
  )
})

function ModuleCanvas({ r, gv, colour, shown, selected, onPick, bar, legend }: {
  r: RepoRecord
  gv: GraphView
  colour: ColourBy
  shown: ReadonlySet<string>
  selected: string | null
  onPick: (n: ViewNode) => void
  bar: ReactNode
  legend: ReactNode
}) {
  const state = usePlan(gv)
  const plan = state.status === 'ready' ? state.plan : null
  const [zoom, setZoom] = useState<Zoom>(FIT)
  const drag = useRef<{ x: number; y: number; z: Zoom } | null>(null)
  const drawnNodes = useMemo(() => (plan === null ? [] : gv.nodes.filter((n) => shown.has(n.id))), [plan, gv, shown])
  const drawnEdges = useMemo(() => (plan === null ? [] : gv.edges.filter((e) => shown.has(e.from) && shown.has(e.to))), [plan, gv, shown])
  const maxWeight = drawnEdges.reduce((m, e) => Math.max(m, e.weight), 1)
  const nodeChunks = useMemo(() => chunked(drawnNodes), [drawnNodes])
  const edgeChunks = useMemo(() => chunked(drawnEdges), [drawnEdges])
  const mounted = useMounted(nodeChunks.length, plan)
  // Edges follow once every node is down, a chunk a frame of their own.
  const mountedEdges = useMounted(mounted === nodeChunks.length ? edgeChunks.length : 0, plan)
  const latestPick = useRef(onPick)
  latestPick.current = onPick
  const pickNode = useCallback((n: ViewNode) => latestPick.current(n), [])
  const mounting = plan !== null && (mounted < nodeChunks.length || mountedEdges < edgeChunks.length)

  function zoomBy(f: number) {
    const k = Math.max(0.4, Math.min(4, zoom.k * f))
    const real = k / zoom.k
    setZoom({ k, tx: W / 2 - (W / 2 - zoom.tx) * real, ty: H / 2 - (H / 2 - zoom.ty) * real })
  }
  function down(e: PointerEvent<SVGSVGElement>) {
    if ((e.target as Element).closest('.rg-node') !== null) return
    drag.current = { x: e.clientX, y: e.clientY, z: zoom }
  }
  function move(e: PointerEvent<SVGSVGElement>) {
    const d = drag.current
    if (d === null) return
    setZoom({ k: d.z.k, tx: d.z.tx + (e.clientX - d.x), ty: d.z.ty + (e.clientY - d.y) })
  }
  const up = () => {
    drag.current = null
  }

  const clustered = gv.clusters.length > 0
  return (
    <div className="rg-canvas" data-drawing="structure">
      <div className="rg-bar">{bar}</div>
      <div className="rg-zoom">
        <Button size="sm" aria-label="Zoom out" onClick={() => zoomBy(1 / ZOOM_STEP)}>
          −
        </Button>
        <Button size="sm" aria-label="Zoom in" onClick={() => zoomBy(ZOOM_STEP)}>
          +
        </Button>
        <Button size="sm" aria-label="Fit" onClick={() => setZoom(FIT)}>
          Fit
        </Button>
      </div>
      <svg
        className="rg-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Module dependency graph of ${repoName(r)}${clustered ? ', clustered by directory' : ''}`}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerLeave={up}
      >
        <g className={mounting ? 'rg-world is-mounting' : 'rg-world'} transform={`translate(${round(zoom.tx)} ${round(zoom.ty)}) scale(${round(zoom.k)})`}>
          {plan?.boxes
            .filter((b) => drawnNodes.some((n) => n.cluster === b.cluster))
            .map((b) => (
              <g key={b.cluster}>
                <rect className="rg-cluster" data-cluster={b.cluster} x={b.x} y={b.y} width={b.w} height={b.h} rx={14} />
                <text className="rg-clabel" x={b.x + 10} y={b.y + 16}>
                  {b.cluster}
                </text>
              </g>
            ))}
          {plan !== null &&
            edgeChunks.slice(0, mountedEdges).map((es, i) => (
              <EdgeChunk key={i} edges={es} plan={plan} maxWeight={maxWeight} selected={selected !== null && es.some((e) => e.from === selected || e.to === selected) ? selected : null} />
            ))}
          {plan !== null &&
            nodeChunks.slice(0, mounted).map((ns, i) => (
              <NodeChunk key={i} nodes={ns} plan={plan} colour={colour} onPick={pickNode} selected={selected !== null && ns.some((n) => n.id === selected) ? selected : null} />
            ))}
        </g>
      </svg>
      {state.status === 'laying' && state.where === 'worker' && (
        <p className="rg-wait" role="status">
          {`Laying out ${fmt(gv.nodes.length)} nodes off the page…`}
        </p>
      )}
      {mounting && (
        <p className="rg-wait" role="status">
          {`Drawing ${fmt(drawnNodes.length)} nodes…`}
        </p>
      )}
      {state.status === 'failed' && (
        <div className="rg-wait" role="alert">
          <p>{`The layout could not run off the page (${state.why}). Laying out ${fmt(gv.nodes.length)} nodes here pauses the page for a moment.`}</p>
          <Button size="sm" onClick={state.here}>
            Lay out here
          </Button>
        </div>
      )}
      {plan !== null && drawnNodes.length === 0 && (
        <p className="rg-wait" role="status">
          The legend hides every node. Show all to draw them again.
        </p>
      )}
      {legend}
    </div>
  )
}

/**
 * THE FILTERING LEGEND (§3.2 defect 11, Graphify's checkbox per community).
 * Each colour band and each cluster is a toggle: pressed is shown. Hiding is
 * drawing-only -- the layout keeps its places, the inspector and the phone
 * list keep every node -- and the legend says how many it hides.
 */
function GraphLegend({ gv, g, colour, shown, hiddenHeat, hiddenClusters, onHeat, onCluster, onAll }: {
  gv: GraphView
  g: ModuleGraph
  colour: ColourBy
  shown: ReadonlySet<string>
  hiddenHeat: ReadonlySet<Heat>
  hiddenClusters: ReadonlySet<string>
  onHeat: (h: Heat) => void
  onCluster: (c: string) => void
  onAll: () => void
}) {
  const hidden = gv.nodes.length - shown.size
  return (
    <div className="rg-legend">
      <div className="rg-lgrow" role="group" aria-label="Show by colour">
        {LEGEND[colour].map((l) => (
          <button key={l.heat} type="button" className="rg-lg" aria-pressed={!hiddenHeat.has(l.heat)} onClick={() => onHeat(l.heat)}>
            <b data-heat={l.heat} />
            {l.label}
          </button>
        ))}
      </div>
      <span>edge width = resolved calls between modules</span>
      {gv.collapsed && <span>{`${fmt(g.modules.length)} modules: each cluster is one node until it is opened`}</span>}
      {gv.clusters.length > 1 && (
        <div className="rg-lgrow" role="group" aria-label="Show clusters">
          {gv.clusters.map((c) => (
            <button key={c} type="button" className="rg-lgc" aria-pressed={!hiddenClusters.has(c)} onClick={() => onCluster(c)}>
              {c}
            </button>
          ))}
        </div>
      )}
      {hidden > 0 && (
        <span className="rg-lghid">
          {`${fmt(hidden)} of ${fmt(gv.nodes.length)} nodes hidden by the legend`}
          <Button size="sm" onClick={onAll}>
            Show all
          </Button>
        </span>
      )}
    </div>
  )
}

function round(n: number): number {
  return Math.round(n * 1000) / 1000
}

const PHONE_WIDTH = '(max-width: 640px)'

/**
 * Whether the phone's module list is on screen. Off it, the list is not even
 * mounted: hidden by CSS it was still 2,000 buttons built on the main thread
 * (measured, a 123 ms task). Where `matchMedia` is missing (jsdom) it is
 * mounted and the stylesheet alone decides, as before.
 */
function usePhoneWidth(): boolean {
  const mq = useMemo(() => (typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_WIDTH) : null), [])
  const [on, setOn] = useState(mq === null || mq.matches)
  useEffect(() => {
    if (mq === null) return
    const again = () => setOn(mq.matches)
    mq.addEventListener?.('change', again)
    return () => mq.removeEventListener?.('change', again)
  }, [mq])
  return on
}

/** The phone's view (390px): the canvas is hidden there and the inspector reads from this list. */
function PhoneList({ gv, colour, selected, onPick }: { gv: GraphView; colour: ColourBy; selected: string | null; onPick: (n: ViewNode) => void }) {
  const rows = [...gv.nodes].sort((a, b) => (b.hot_spot_changes ?? -1) - (a.hot_spot_changes ?? -1) || a.id.localeCompare(b.id))
  return (
    <div className="rg-phone" role="list" aria-label="Modules">
      {rows.map((n) => (
        <button key={n.id} type="button" role="listitem" className="rg-prow" aria-pressed={n.id === selected} onClick={() => onPick(n)}>
          <i data-heat={heatOf(n, colour)} aria-hidden="true" />
          <span className="rg-pname">{n.isPackage ? `${n.label} (${n.members} modules)` : n.id}</span>
          <small>{n.hot_spot_changes === null ? 'changes not measured' : `${fmt(n.hot_spot_changes)} changes`}</small>
        </button>
      ))}
    </div>
  )
}

/**
 * The index's module count beside the graph's, when they differ (QA G4-12):
 * the index lists every module its indexer named, docs and config included,
 * while a graph node is a directory holding at least one parsed code symbol
 * (impact.py `module_graph`). Without the sentence, 83 and 64 on one page
 * read as one of them being wrong.
 */
function moduleGap(r: RepoRecord, g: ModuleGraph): string | null {
  const listed = r.index.coverage?.modules ?? null
  const drawn = g.modules.reduce((n, m) => n + (m.modules ?? 1), 0)
  if (listed === null || listed === drawn) return null
  return `the index lists ${fmt(listed)} module${listed === 1 ? '' : 's'}; the graph draws the ${fmt(drawn)} director${drawn === 1 ? 'y that holds' : 'ies that hold'} parsed code symbols`
}

function GraphFoot({ r, g }: { r: RepoRecord; g: ModuleGraph }) {
  const parts: string[] = []
  for (const key of ['files', 'symbols', 'edges'] as const) {
    const n = g.counts[key]
    if (n !== undefined) parts.push(`${fmt(n)} ${key}`)
  }
  const gap = moduleGap(r, g)
  return (
    <p className="ur-sub rg-foot">
      Index <code className="ur-sha">{g.index_sha === null ? '—' : g.index_sha.slice(0, 7)}</code>
      {parts.length > 0 ? ` · ${parts.join(' · ')}` : null}
      {parts.length === 0 && (
        <>
          {' · '}
          <Dash why="The graph's file, symbol and edge counts were not served" />
        </>
      )}
      {g.truncated.length > 0 && ` · over the size ceiling, so these were cut: ${g.truncated.join(', ')}`}
      {gap !== null && ` · ${gap}`}
    </p>
  )
}

// ---------------------------------------------------------------------------
// The inspector
// ---------------------------------------------------------------------------

function pctWord(r: number): string {
  return `${Math.round(r * 100)}%`
}

function Inspector({ r, gv, node, symbol, onSymbol, depth, depthControl, direction, onOpen, onSelect }: {
  r: RepoRecord
  gv: GraphView
  node: ViewNode | null
  symbol: SymbolRow | null
  onSymbol: (s: SymbolRow | null) => void
  depth: number
  depthControl: ReactNode
  direction: Direction
  onOpen: (n: ViewNode) => void
  onSelect: (n: ViewNode) => void
}) {
  if (node === null) {
    return (
      <Card className="rg-inspector" title="Inspector">
        <p className="ur-none">Pick a module on the canvas.</p>
      </Card>
    )
  }
  const deg = degree(gv, node.id)
  return (
    <Card className="rg-inspector" title={node.isPackage ? node.cluster : node.id} action={<Chip>{node.isPackage ? `cluster · ${node.members} modules` : 'module'}</Chip>}>
      <p className="rg-meta">
        <span>
          {node.symbols === null ? <Dash why="Its symbol count was not served" /> : <b>{fmt(node.symbols)}</b>} symbols
        </span>
        <span>
          <Dash why="Lines per module are not served by the graph route" /> lines
        </span>
        <span>
          {node.hot_spot_changes === null ? <Dash why="Its change count was not measured" /> : <b>{fmt(node.hot_spot_changes)}</b>} changes in 90 days
        </span>
      </p>
      <p className="rg-meta">
        <span>
          called from <b>{deg.callers}</b> {deg.callers === 1 ? 'module' : 'modules'}
        </span>
        <span>
          calls <b>{deg.callees}</b> {deg.callees === 1 ? 'module' : 'modules'}
        </span>
        <span>
          test reach{' '}
          {node.test_reach === null ? (
            <Dash why={node.isPackage ? 'Test reach is served per module; open the package to see each' : 'It has no symbol a test could reach, or reach was not measured'} />
          ) : (
            <b>{pctWord(node.test_reach)}</b>
          )}{' '}
          of symbols
        </span>
        {node.languages.length > 0 && <span>{node.languages.join(', ')}</span>}
      </p>
      <Neighbours key={node.id} gv={gv} node={node} onSelect={onSelect} />
      {node.isPackage ? (
        <Button size="sm" onClick={() => onOpen(node)}>
          Open the cluster
        </Button>
      ) : (
        <SymbolSection r={r} module={node.id} symbol={symbol} onSymbol={onSymbol} depth={depth} depthControl={depthControl} direction={direction} />
      )}
    </Card>
  )
}

/** Neighbours listed before "show all": enough to read at a glance, never a silent cut. */
export const NEIGHBOURS_SHOWN = 8

/**
 * THE NEIGHBOUR LIST (§3.2 defect 11, Graphify's inspector): the drawn nodes
 * that call this one and that it calls, as buttons, heaviest first. A click
 * moves the inspector to that node, so a reader walks the graph by name
 * instead of hunting for a dot. A measured none is the real-zero mark.
 */
function Neighbours({ gv, node, onSelect }: { gv: GraphView; node: ViewNode; onSelect: (n: ViewNode) => void }) {
  const nb = useMemo(() => neighbours(gv, node.id), [gv, node.id])
  return (
    <div className="rg-nbs">
      <NeighbourList heading="Called from" rows={nb.callers} none="No node drawn here calls it" onSelect={onSelect} />
      <NeighbourList heading="Calls" rows={nb.callees} none="It calls no node drawn here" onSelect={onSelect} />
    </div>
  )
}

function NeighbourList({ heading, rows, none, onSelect }: { heading: string; rows: readonly Neighbour[]; none: string; onSelect: (n: ViewNode) => void }) {
  const [all, setAll] = useState(false)
  const list = all ? rows : rows.slice(0, NEIGHBOURS_SHOWN)
  return (
    <section className="rg-nb" aria-label={heading}>
      <h3>
        {heading} {rows.length === 0 ? <Mark kind="zero" say={none} /> : <small>{fmt(rows.length)}</small>}
      </h3>
      {rows.length > 0 && (
        <ul>
          {list.map((x) => (
            <li key={x.node.id}>
              <button type="button" className="rg-nbrow" title={x.node.id} onClick={() => onSelect(x.node)}>
                <code>{displayLabel(x.node)}</code>
                <small>{`${fmt(x.weight)} ${x.weight === 1 ? 'call' : 'calls'}`}</small>
              </button>
            </li>
          ))}
        </ul>
      )}
      {rows.length > NEIGHBOURS_SHOWN && (
        <button type="button" className="ri-more" onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : `Show all ${fmt(rows.length)}`}
        </button>
      )}
    </section>
  )
}

function SymbolSection({ r, module, symbol, onSymbol, depth, depthControl, direction }: {
  r: RepoRecord
  module: string
  symbol: SymbolRow | null
  onSymbol: (s: SymbolRow | null) => void
  depth: number
  depthControl: ReactNode
  direction: Direction
}) {
  return (
    <>
      {symbol === null ? (
        <SymbolPicker r={r} module={module} onSymbol={onSymbol} />
      ) : (
        <>
          <div className="rg-ch">
            <b>Call graph</b>
            {depthControl}
          </div>
          <PickedSymbol symbol={symbol} onClear={() => onSymbol(null)} />
          <CallRead r={r} symbol={symbol} depth={depth} direction={direction}>
            {(cg) => (
              <>
                <CallDrawing cg={cg} depth={depth} mini />
                <EvidenceLegend />
              </>
            )}
          </CallRead>
          <TestsRead r={r} symbol={symbol} />
        </>
      )}
    </>
  )
}

function PickedSymbol({ symbol, onClear }: { symbol: SymbolRow; onClear: () => void }) {
  const lines = linesWord(symbol)
  return (
    <p className="rg-picked">
      <code>{short(symbol.id)}</code>
      {symbol.path !== null && (
        <small>
          {symbol.path}
          {lines !== null ? `:${lines}` : ''}
        </small>
      )}
      <Button size="sm" kind="ghost" onClick={onClear}>
        Another symbol
      </Button>
    </p>
  )
}

/** The depth control: −, the depth, +; bounded 1-6, the route's own bound. */
function DepthControl({ depth, onDepth }: { depth: number; onDepth: (d: number) => void }) {
  return (
    <span className="rg-dctl">
      <span className="ur-mu">depth</span>
      <Button size="sm" aria-label="Less depth" disabled={depth <= DEPTH_MIN} onClick={() => onDepth(depth - 1)}>
        −
      </Button>
      <b className="rg-depth">{depth}</b>
      <Button size="sm" aria-label="More depth" disabled={depth >= DEPTH_MAX} onClick={() => onDepth(depth + 1)}>
        +
      </Button>
    </span>
  )
}

/** Search as you type once this many characters are typed; Find searches any length (QA G4-09). */
export const SEARCH_MIN_CHARS = 3
/** How long typing pauses before the search runs: one read per pause, not per keystroke. */
export const SEARCH_DEBOUNCE_MS = 300

/**
 * A module's symbols, from `?q=<module>`, or a typed search; picking one
 * draws its call graph. The search runs as you type -- SEARCH_DEBOUNCE_MS
 * after the last keystroke, from SEARCH_MIN_CHARS characters -- and Find (or
 * Enter) runs it at once. It used to need Enter on a form with no button,
 * under a hint that promised results while typing (QA G4-09).
 */
function SymbolPicker({ r, module, onSymbol }: { r: RepoRecord; module: string | null; onSymbol: (s: SymbolRow) => void }) {
  const [typed, setTyped] = useState('')
  const [q, setQ] = useState<string | null>(null)
  useEffect(() => {
    const t = typed.trim()
    if (t === '') {
      setQ(null)
      return
    }
    if (t.length < SEARCH_MIN_CHARS) return
    const timer = setTimeout(() => setQ(t), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [typed])
  const query = q ?? module
  const tooShort = typed.trim().length > 0 && typed.trim().length < SEARCH_MIN_CHARS && q !== typed.trim()
  return (
    <div className="rg-picker">
      <div className="rg-ch">
        <b>Symbols</b>
        <form
          className="rg-find"
          role="search"
          onSubmit={(e) => {
            e.preventDefault()
            setQ(typed.trim() === '' ? null : typed.trim())
          }}
        >
          <input type="search" className="ur-search" aria-label="Find a symbol" placeholder="Find a symbol" value={typed} onChange={(e) => setTyped(e.target.value)} />
          <Button size="sm" type="submit">
            Find
          </Button>
        </form>
      </div>
      {tooShort && <p className="ur-hint">{`Type ${SEARCH_MIN_CHARS} or more characters to search as you type, or press Find.`}</p>}
      {query === null ? (
        <p className="ur-none">{`Type ${SEARCH_MIN_CHARS} or more characters of a symbol's name: matches appear as you type.`}</p>
      ) : (
        <SymbolList key={query} r={r} q={query} onSymbol={onSymbol} />
      )}
    </div>
  )
}

/** Where a symbol is, as `path:line`; the path alone when no line was served. */
function whereWord(s: SymbolRow): string | null {
  if (s.path === null) return null
  return s.start_line === null ? s.path : `${s.path}:${s.start_line}`
}

function SymbolList({ r, q, onSymbol }: { r: RepoRecord; q: string; onSymbol: (s: SymbolRow) => void }) {
  const read = useUrRead(() => searchRepositorySymbols(r.repo_id, q), `${r.repo_id}:q:${q}`)
  return (
    <UrRegion state={read.state} route={SYMBOLS_ROUTE} what="Symbol search" onRetry={read.reload} lines={2}>
      {(s) =>
        s === null || s.symbols.length === 0 ? (
          <p className="ur-none">No symbol in this index matches {q}.</p>
        ) : (
          <div className="rg-syms">
            {s.symbols.map((sym) => {
              const lines = linesWord(sym)
              return (
                <button key={sym.id} type="button" className="rg-sym" onClick={() => onSymbol(sym)} title={lines === null ? undefined : `lines ${lines}`}>
                  <code>{short(sym.id)}</code>
                  <small>
                    {whereWord(sym) ?? <Dash why="The symbol's file was not served" />}
                    {sym.kind !== null ? ` · ${sym.kind}` : ''}
                  </small>
                </button>
              )
            })}
            {s.more && <p className="ur-hint">More match than the route lists: type more of the name.</p>}
          </div>
        )
      }
    </UrRegion>
  )
}

function CallRead({ r, symbol, depth, direction, children }: { r: RepoRecord; symbol: SymbolRow; depth: number; direction: Direction; children: (cg: CallGraph) => ReactNode }) {
  const read = useUrRead(() => loadSymbolGraph(r.repo_id, symbol.id, depth, direction), `${r.repo_id}:cg:${symbol.id}:${depth}:${direction}`)
  return (
    <UrRegion state={read.state} route={SYMBOLS_ROUTE} what="The call graph" onRetry={read.reload} lines={4}>
      {(cg) => (cg === null ? <p className="ur-none">The route answered with no call graph this page can draw.</p> : children(cg))}
    </UrRegion>
  )
}

function TestsRead({ r, symbol, full = false }: { r: RepoRecord; symbol: SymbolRow; full?: boolean }) {
  const read = useUrRead(() => loadSymbolTests(r.repo_id, symbol.id), `${r.repo_id}:tm:${symbol.id}`)
  const LIMIT = 5
  return (
    <>
      <UrRegion state={read.state} route={SYMBOLS_ROUTE} what="The tests reaching this symbol" plural onRetry={read.reload} lines={2}>
        {(t) => {
          const tests = t?.tests ?? []
          const shown = full ? tests : tests.slice(0, LIMIT)
          return (
            <>
              <div className="rg-ch rg-tests-h">
                <b>Tests reaching it</b>
                <span className="ur-mu">{tests.length === 1 ? '1 test reaches it' : `${fmt(tests.length)} tests reach it`}</span>
              </div>
              {tests.length === 0 ? (
                <p className="ur-none">No test reaches this symbol in the index's graph.</p>
              ) : (
                <div className="rg-tests">
                  {shown.map((x) => (
                    <div key={x.test} className="rg-test">
                      <code>{x.test}</code>
                      <small>
                        {x.depth === null ? 'depth not served' : `depth ${x.depth}`}
                        {' · '}
                        {x.confidence === null ? 'confidence not served' : `confidence ${x.confidence}`}
                      </small>
                    </div>
                  ))}
                  {!full && tests.length > LIMIT && <p className="ur-hint">{`${tests.length - LIMIT} more in the Tests reaching a symbol view`}</p>}
                </div>
              )}
            </>
          )
        }}
      </UrRegion>
    </>
  )
}

function EvidenceLegend() {
  return (
    <div className="rg-evlegend">
      {EVIDENCE_LEGEND.map((l) => (
        <span key={l.key} className="rg-lg" data-look={l.key}>
          <i data-look={l.key} aria-hidden="true" />
          {l.label}
        </span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The call graph drawing
// ---------------------------------------------------------------------------

function CallDrawing({ cg, depth, mini = false }: { cg: CallGraph; depth: number; mini?: boolean }) {
  const cols = callColumns(cg)
  const width = mini ? 300 : 940
  const rowH = mini ? 30 : 56
  const nodeW = mini ? 0 : 140
  const groups = new Map<number, string[]>()
  for (const n of cg.nodes) {
    const c = cols.get(n.id) ?? { side: 'callee' as const, depth: n.distance ?? 1 }
    const at = c.side === 'caller' ? -c.depth : c.depth
    groups.set(at, [...(groups.get(at) ?? []), n.id])
  }
  const span = Math.max(1, ...[...groups.keys()].map(Math.abs))
  const tallest = Math.max(1, ...[...groups.values()].map((g) => g.length))
  const height = Math.max(mini ? 150 : 200, tallest * rowH + (mini ? 40 : 70))
  const colW = (width - (mini ? 60 : nodeW + 20)) / (2 * span)
  const pos = new Map<string, { x: number; y: number }>()
  for (const [at, ids] of groups) {
    ids.sort()
    const x = Math.round(width / 2 + at * colW)
    ids.forEach((id, i) => pos.set(id, { x, y: Math.round((mini ? 30 : 50) + ((i + 0.5) * (height - (mini ? 40 : 60))) / ids.length) }))
  }
  const centre = short(cg.symbol.id)
  const label = mini
    ? `Call graph of ${centre} at depth ${depth}`
    : `Call graph centred on ${centre}, callers left, callees right, depth ${depth}`
  const half = nodeW / 2
  return (
    <svg className={mini ? 'rg-csvg rg-mini' : 'rg-csvg'} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      {!mini &&
        [...groups.keys()]
          .filter((at) => at !== 0)
          .sort((a, b) => a - b)
          .map((at) => (
            <text key={at} className="rg-colh" x={width / 2 + at * colW} y={20} textAnchor="middle">
              {at < 0 ? `callers, depth ${-at}` : `callees, depth ${at}`}
            </text>
          ))}
      {cg.edges.map((e) => {
        const a = pos.get(e.from)
        const b = pos.get(e.to)
        if (a === undefined || b === undefined) return null
        const look = edgeLook(e)
        const x1 = a.x + half
        const x2 = b.x - half
        const mx = (x1 + x2) / 2
        return (
          <g key={`${e.from}>${e.to}>${e.kind ?? ''}`}>
            <path className="rg-cedge" data-look={look} d={`M${x1} ${a.y} C${mx} ${a.y} ${mx} ${b.y} ${x2} ${b.y}`}>
              <title>{edgeWords(e)}</title>
            </path>
            {look !== 'lsp' && (
              <text className="rg-elabel" x={mx} y={(a.y + b.y) / 2 - 4} textAnchor="middle" aria-hidden="true">
                {`${e.evidence ?? '?'}${e.confidence === null ? '' : ` ${e.confidence}`}`}
              </text>
            )}
          </g>
        )
      })}
      {cg.nodes.map((n) => {
        const p = pos.get(n.id)
        if (p === undefined) return null
        const isCentre = n.id === cg.symbol.id
        const cls = `rg-cnode${isCentre ? ' is-sel' : ''}${n.kind === 'external' ? ' is-ext' : ''}`
        return (
          <g key={n.id} className={cls} data-id={n.id} data-x={p.x}>
            <title>{`${n.id}${n.kind === null ? '' : ` · ${n.kind}`}`}</title>
            {mini ? (
              <circle cx={p.x} cy={p.y} r={isCentre ? 9 : 6} />
            ) : (
              <rect x={p.x - half} y={p.y - 16} width={nodeW} height={32} rx={8} />
            )}
            <text x={p.x} y={mini ? p.y + (isCentre ? 20 : 16) : p.y + 4} textAnchor="middle">
              {clip(short(n.id), mini ? 18 : 20)}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

function clip(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`
}

// ---------------------------------------------------------------------------
// The Call graph and Test map views, full width
// ---------------------------------------------------------------------------

function CallView({ r, node, symbol, onSymbol, depth, depthControl, direction, onDirection }: {
  r: RepoRecord
  node: ViewNode | null
  symbol: SymbolRow | null
  onSymbol: (s: SymbolRow | null) => void
  depth: number
  depthControl: ReactNode
  direction: Direction
  onDirection: (d: Direction) => void
}) {
  const module = node !== null && !node.isPackage ? node.id : null
  if (symbol === null) {
    return (
      <Card className="rg-callview" title="Call graph">
        <p className="ur-sub">Pick a symbol to centre the call graph on.</p>
        <SymbolPicker r={r} module={module} onSymbol={onSymbol} />
      </Card>
    )
  }
  return (
    <section className="rg-callview" aria-label="Call graph">
      <div className="rg-chead">
        <PickedSymbol symbol={symbol} onClear={() => onSymbol(null)} />
        <span className="ur-acts">
          {depthControl}
          <UrRadio
            label="Direction"
            value={direction}
            onChange={onDirection}
            options={[
              { key: 'both', label: 'both' },
              { key: 'callers', label: 'callers' },
              { key: 'callees', label: 'callees' },
            ]}
          />
        </span>
      </div>
      <div className="rg-ccanvas">
        <CallRead r={r} symbol={symbol} depth={depth} direction={direction}>
          {(cg) => {
            const cols = callColumns(cg)
            const callers = [...cols.values()].filter((c) => c.side === 'caller').length
            const callees = [...cols.values()].filter((c) => c.side === 'callee').length
            const low = cg.edges.filter((e) => edgeLook(e) === 'low').length
            return (
              <>
                <CallDrawing cg={cg} depth={depth} />
                <EvidenceLegend />
                <p className="ur-sub">
                  {`${callers} ${callers === 1 ? 'caller' : 'callers'} and ${callees} ${callees === 1 ? 'callee' : 'callees'} to depth ${depth}`}
                  {low > 0 && ` · ${low} ${low === 1 ? 'edge' : 'edges'} below 0.4 confidence, drawn dotted: the impact query flags them`}
                  {cg.truncated && ' · cut at the route’s node bound, so not every symbol at this depth is drawn'}
                  {' · hover an edge for its evidence and confidence'}
                </p>
              </>
            )
          }}
        </CallRead>
      </div>
    </section>
  )
}

function TestView({ r, node, seed, symbol, onSymbol }: {
  r: RepoRecord
  node: ViewNode | null
  /** The search a Test map row's link asked for, before any module is picked. */
  seed: string | null
  symbol: SymbolRow | null
  onSymbol: (s: SymbolRow | null) => void
}) {
  const module = seed ?? (node !== null && !node.isPackage ? node.id : null)
  return (
    <Card className="rg-testview" title={symbol === null ? 'Tests reaching a symbol' : `Tests reaching ${short(symbol.id)}`}>
      {symbol === null ? (
        <>
          <p className="ur-sub">Pick a symbol to list the tests that reach it.</p>
          <SymbolPicker r={r} module={module} onSymbol={onSymbol} />
        </>
      ) : (
        <>
          <PickedSymbol symbol={symbol} onClear={() => onSymbol(null)} />
          <TestsRead r={r} symbol={symbol} full />
        </>
      )}
    </Card>
  )
}
