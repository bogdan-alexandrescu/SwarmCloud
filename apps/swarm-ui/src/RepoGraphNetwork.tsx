import { useEffect, useRef, useState, type ReactNode } from 'react'
import { DataSet, Network, type Edge, type Node, type Options } from 'vis-network/standalone'
import { Button } from './components'
import { LOW_CONFIDENCE, displayLabel, fmt, heatOf, type ColourBy, type GraphView, type Heat, type ViewNode } from './RepoGraphData'
import { radiusOf } from './RepoGraphLayout'

/**
 * ONE REPOSITORY › GRAPH › Network: the module graph drawn by vis-network,
 * configured the way Graphify draws its graph.html (forceAtlas2Based physics,
 * a golden-angle seed, 200 stabilising iterations, then frozen), beside the
 * console's own Structure drawing (owner addition 2026-10-08,
 * docs/design/graph-rendering.md §8).
 *
 * THIS FILE IS A LAZY CHUNK. RepoGraph.tsx reaches it only through a dynamic
 * `import()` the first time a reader opens the Network view, so vis-network
 * (about 159 kB gz) never lands in the main bundle. Nothing else may import
 * it statically.
 *
 * IT DRAWS THE SAME VIEW, NOT ITS OWN. The nodes and edges are the
 * GraphView RepoGraph.tsx built -- the same clusters, the same aggregation
 * past COLLAPSE_AT, the same unique labels, the same legend filter -- so
 * neither view invents a node, and a click lands in the same inspector.
 *
 * WHERE GRAPHIFY'S CONFIGURATION IS NOT FOLLOWED, AND WHY:
 *   * keyboard navigation is ON (Graphify sets `keyboard: false`): the
 *     canvas takes focus, arrow keys pan, + and - zoom;
 *   * the plain mouse wheel is NOT a zoom (`zoomView: false`, visual QA V044):
 *     vis-network otherwise swallows every wheel event over the canvas, and
 *     with the pointer there the legend and the page below it could not be
 *     scrolled to. Zoom is the buttons, the keys, and Ctrl/⌘ + wheel about
 *     the pointer (a listener of ours, as Structure does: owner decision
 *     2026-10-16, Q3); the plain wheel scrolls;
 *   * labels are held at the console's 12px on screen, whatever the zoom
 *     (`labelFont`, V045): Graphify's fixed 12 canvas units printed at about
 *     8px once the drawing was fitted, and with a halo in the card's colour,
 *     so an edge or an arrowhead under a label is cut, not printed through;
 *   * a label that would print over another label is not drawn
 *     (`keptLabels`, V045, re-run 2026-10-16): vis-network places every label
 *     under its dot whatever is there, and in the dense middle they printed
 *     over each other. Kept busiest first, the selected node's always; the
 *     pass runs again on every zoom, so zooming in brings the rest back, and
 *     the hover title and the node list still name every node;
 *   * colours are the console's tokens, read off <html> at render time and
 *     read again when the theme changes: a canvas cannot read a CSS variable;
 *   * stabilisation yields to the page after every iteration
 *     (`updateInterval: 1`), and past NETWORK_MAX nodes nothing is built:
 *     see NETWORK_MAX for what that bound was measured against.
 *
 * A canvas has no per-node DOM, so every node is also a button in a list
 * beside it: hidden until it takes keyboard focus, and shown outright when
 * the canvas cannot be created (jsdom in vitest has no 2D context).
 */

/** The console's colours as concrete values, for a renderer that cannot read `var(--…)`. */
export interface Tokens {
  surface: string
  /** The canvas card's ground (`.rg-canvas`), behind every label: the labels' halo. */
  surface2: string
  line: string
  edge: string
  text: string
  textDim: string
  accent: string
  live: string
  warn: string
  bad: string
  font: string
}

/** Read the tokens off <html>. A token the stylesheet did not set falls back to a CSS keyword, never a hex. */
export function readTokens(el: Element = document.documentElement): Tokens {
  const cs = getComputedStyle(el)
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback
  return {
    surface: v('--surface', 'canvas'),
    surface2: v('--surface-2', 'canvas'),
    line: v('--line', 'gray'),
    edge: v('--ctl-bd', 'gray'),
    text: v('--text', 'canvastext'),
    textDim: v('--text-dim', 'gray'),
    accent: v('--sk-ac', 'highlight'),
    live: v('--s-live', 'green'),
    warn: v('--s-warn', 'orange'),
    bad: v('--s-bad', 'red'),
    font: v('--font', 'sans-serif'),
  }
}

/** The tokens, read again when <html data-theme> or the OS scheme changes, and once the web fonts load. */
function useTokens(): Tokens {
  const [tokens, setTokens] = useState<Tokens>(() => readTokens())
  useEffect(() => {
    let live = true
    const again = () => live && setTokens(readTokens())
    const mo = new MutationObserver(again)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const mq = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: light)') : null
    mq?.addEventListener?.('change', again)
    // vis-network measures a label when it builds: rebuild once the face it names has loaded.
    void document.fonts?.ready.then(again)
    return () => {
      live = false
      mo.disconnect()
      mq?.removeEventListener?.('change', again)
    }
  }, [])
  return tokens
}

const HEAT_WORD: Readonly<Record<Heat, string>> = { hot: 'hot', warm: 'warm', cool: 'cool', unmeasured: 'not measured' }

/** The view as vis-network data: only the nodes the legend shows, and only edges between two of them. */
export function networkData(gv: GraphView, colour: ColourBy, shown: ReadonlySet<string>, t: Tokens): { nodes: Node[]; edges: Edge[] } {
  const fill: Record<Heat, string> = { hot: t.bad, warm: t.warn, cool: t.live, unmeasured: t.surface }
  const drawn = gv.nodes.filter((n) => shown.has(n.id))
  const edges = gv.edges.filter((e) => shown.has(e.from) && shown.has(e.to))
  const max = Math.max(1, ...edges.map((e) => e.weight))
  return {
    // Graphify's golden-angle spiral seed: spreads the nodes before physics, and is the same on every read.
    nodes: drawn.map((n, i) => {
      const heat = heatOf(n, colour)
      return {
        id: n.id,
        label: displayLabel(n),
        x: 30 * Math.sqrt(i) * Math.cos(i * 2.4),
        y: 30 * Math.sqrt(i) * Math.sin(i * 2.4),
        shape: 'dot',
        size: radiusOf(n.symbols, n.isPackage),
        borderWidth: n.isPackage ? 3 : 1.5,
        shapeProperties: { borderDashes: heat === 'unmeasured' ? [3, 2] : false },
        color: {
          background: fill[heat],
          border: heat === 'unmeasured' ? t.edge : t.surface,
          highlight: { background: fill[heat], border: t.accent },
          hover: { background: fill[heat], border: t.accent },
        },
        // A string title is drawn as text by vis-network 10, never parsed as HTML.
        title: nodeWords(n, colour),
      }
    }),
    edges: edges.map((e) => ({
      id: `${e.from}>${e.to}`,
      from: e.from,
      to: e.to,
      width: 1 + (2.5 * e.weight) / max,
      dashes: e.max_confidence !== null && e.max_confidence < LOW_CONFIDENCE,
      color: { color: t.edge, highlight: t.accent, hover: t.accent, opacity: 0.8 },
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
      title: `${e.from} → ${e.to} · ${fmt(e.weight)} resolved ${e.weight === 1 ? 'call' : 'calls'}`,
    })),
  }
}

/** The on-screen size of a Network label: the console's 12px micro floor. */
export const LABEL_PX = 12
/** The largest a label grows in canvas units when the drawing is zoomed far out, so it never swamps the nodes. */
const LABEL_MAX = 36

/**
 * The node label font for a view scale: LABEL_PX on screen at any scale
 * where that stays under LABEL_MAX canvas units, with a 3px halo in the
 * canvas card's colour and a 3px gap below the dot -- vis-network draws a
 * dot's label after the arrows, so the halo is what keeps an arrowhead
 * arriving from below from printing into the text.
 */
export function labelFont(t: Tokens, scale: number): { color: string; face: string; size: number; strokeWidth: number; strokeColor: string; vadjust: number } {
  const k = scale > 0 && Number.isFinite(scale) ? scale : 1
  const size = Math.min(LABEL_MAX, LABEL_PX / k)
  const unit = size / LABEL_PX
  return { color: t.textDim, face: t.font, size, strokeWidth: 3 * unit, strokeColor: t.surface2, vadjust: 3 * unit }
}

/** What `keptLabels` places: a dot and its label, in screen pixels. */
export interface NetLabelSpot {
  id: string
  x: number
  y: number
  r: number
  text: string
  /** Its degree: the busier node keeps its label. */
  weight: number
}

/** A label's average glyph width at LABEL_PX, and its line box: the Structure drawing's measures (RepoGraphLayout.ts). */
const NET_CHAR_W = 6.2
const NET_LINE = 14
/** The clear room kept between two labels, in screen pixels. */
const NET_LABEL_GAP = 2

/**
 * Which labels the Network view draws (V045): every label whose box under its
 * dot (vis-network's place for a dot's label, `vadjust` below it) crosses no
 * label already kept. The selected node first, then busiest first, then by
 * id, so the same picture keeps the same labels.
 */
export function keptLabels(spots: readonly NetLabelSpot[], selected: string | null): Set<string> {
  const order = [...spots].sort((a, b) => Number(b.id === selected) - Number(a.id === selected) || b.weight - a.weight || a.id.localeCompare(b.id))
  const boxes: { x0: number; x1: number; y0: number; y1: number }[] = []
  const kept = new Set<string>()
  for (const s of order) {
    if (s.text === '') continue
    const w = s.text.length * NET_CHAR_W
    const y0 = s.y + s.r + 3
    const b = { x0: s.x - w / 2 - NET_LABEL_GAP, x1: s.x + w / 2 + NET_LABEL_GAP, y0, y1: y0 + NET_LINE }
    if (boxes.some((o) => b.x0 < o.x1 && o.x0 < b.x1 && b.y0 < o.y1 && o.y0 < b.y1)) continue
    boxes.push(b)
    kept.add(s.id)
  }
  return kept
}

/** Graphify's graph.html options (graphify/exporters/html.py `_html_script`), with the changes named above. */
export function networkOptions(t: Tokens): Options {
  return {
    physics: {
      enabled: true,
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -60, centralGravity: 0.005, springLength: 120, springConstant: 0.08, damping: 0.4, avoidOverlap: 0.8 },
      stabilization: { enabled: true, iterations: 200, updateInterval: 1, fit: true },
    },
    interaction: {
      hover: true,
      tooltipDelay: 100,
      hideEdgesOnDrag: true,
      navigationButtons: false,
      keyboard: { enabled: true, bindToWindow: false },
      zoomView: false,
    },
    nodes: { shape: 'dot', borderWidth: 1.5, font: labelFont(t, 1) },
    edges: { smooth: { enabled: true, type: 'continuous', roundness: 0.2 }, selectionWidth: 3, color: { color: t.edge } },
  }
}

function nodeWords(n: ViewNode, colour: ColourBy): string {
  const heat = HEAT_WORD[heatOf(n, colour)]
  return n.isPackage ? `${n.label}: ${fmt(n.members)} modules, ${heat}` : `${n.id}, ${heat}`
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

const ZOOM_STEP = 1.25
/** One Ctrl/⌘ + wheel notch: Structure's WHEEL_STEP (RepoGraph.tsx). */
const WHEEL_STEP = 1.1

/**
 * The most nodes this view builds a canvas for. vis-network builds on the
 * main thread, and in headless Chromium 141 in a SwarmCloud container
 * (2026-10-08, the src/ fixture unclustered, web fonts loaded) its longest
 * task was 61-79 ms at 100 nodes, 62-77 at 150, 69-73 at 200, 98 at 300,
 * 144 at 500 and 343 at 2,000 -- the GR3 rule is under 100 ms. Past the bound the view says so and points at
 * what does draw it: clustering, whose first view is a few dozen nodes, or
 * Structure, which lays out off the main thread. Graphify's own answer past
 * its limit is the same: do not draw the hairball.
 */
export const NETWORK_MAX = 200

export function NetworkCanvas({ name, gv, colour, shown, selected, onPick, bar, legend }: {
  name: string
  gv: GraphView
  colour: ColourBy
  shown: ReadonlySet<string>
  selected: string | null
  onPick: (n: ViewNode) => void
  bar: ReactNode
  legend: ReactNode
}) {
  const box = useRef<HTMLDivElement>(null)
  const net = useRef<Network | null>(null)
  const tokens = useTokens()
  const [failed, setFailed] = useState<string | null>(null)
  const pick = useRef(onPick)
  pick.current = onPick
  const chosen = useRef(selected)
  chosen.current = selected
  // Set while a network is built: resizes its labels to the current zoom, and
  // chooses again which of them are drawn.
  const relabel = useRef<() => void>(() => {})

  const drawn = gv.nodes.filter((n) => shown.has(n.id))
  const tooMany = drawn.length > NETWORK_MAX

  useEffect(() => {
    const el = box.current
    if (el === null || tooMany) return
    let n: Network | null = null
    // Built in a task of its own: after a click React runs this effect in the
    // click's task, and the render plus the build measured 108 ms together.
    const build = () => {
      try {
        const data = networkData(gv, colour, shown, tokens)
        const nodes = new DataSet(data.nodes as (Node & { id: string })[])
        n = new Network(el, { nodes, edges: data.edges }, networkOptions(tokens))
        const built = n
        const degree = new Map<string, number>()
        for (const e of data.edges) {
          degree.set(String(e.from), (degree.get(String(e.from)) ?? 0) + 1)
          degree.set(String(e.to), (degree.get(String(e.to)) ?? 0) + 1)
        }
        // Labels follow the zoom so they stay LABEL_PX on screen (V045).
        let at = 1
        const drawnLabel = new Map(data.nodes.map((x) => [String(x.id), String(x.label ?? '')]))
        const fitLabels = () => {
          const k = built.getScale()
          if (!(k > 0)) return
          if (Math.abs(k - at) / at >= 0.02) {
            at = k
            built.setOptions({ nodes: { font: labelFont(tokens, k) } })
          }
          // Overlap is measured on screen, where a label is LABEL_PX at any zoom; a pan moves them all alike.
          const xy = built.getPositions()
          const keep = keptLabels(
            data.nodes.flatMap((x) => {
              const id = String(x.id)
              const p = xy[id]
              return p === undefined ? [] : [{ id, x: p.x * k, y: p.y * k, r: (x.size ?? 10) * k, text: String(x.label ?? ''), weight: degree.get(id) ?? 0 }]
            }),
            chosen.current,
          )
          const changes = data.nodes.flatMap((x) => {
            const id = String(x.id)
            const want = keep.has(id) ? String(x.label ?? '') : ''
            if (drawnLabel.get(id) === want) return []
            drawnLabel.set(id, want)
            return [{ id, label: want }]
          })
          if (changes.length > 0) nodes.update(changes)
        }
        // Freeze after settling: the picture never drifts while it is being read.
        built.once('stabilizationIterationsDone', () => {
          built.setOptions({ physics: { enabled: false } })
          fitLabels()
          // The keys and a pinch emit 'zoom'; the buttons call it themselves.
          built.on('zoom', fitLabels)
          built.on('dragEnd', fitLabels)
          relabel.current = fitLabels
        })
        built.on('click', (p: { nodes: (string | number)[] }) => {
          const id = p.nodes[0]
          const v = id === undefined ? undefined : gv.nodes.find((x) => x.id === String(id))
          if (v !== undefined) pick.current(v)
        })
        const sel = chosen.current
        if (sel !== null && shown.has(sel)) built.selectNodes([sel])
        net.current = built
        setFailed(null)
      } catch (e) {
        n?.destroy()
        n = null
        setFailed(message(e))
      }
    }
    const later = setTimeout(build, 0)
    return () => {
      clearTimeout(later)
      relabel.current = () => {}
      n?.destroy()
      net.current = null
    }
  }, [gv, colour, shown, tokens, tooMany])

  useEffect(() => {
    const n = net.current
    if (n === null) return
    n.selectNodes(selected !== null && shown.has(selected) ? [selected] : [])
    // The selected node's label is always drawn.
    relabel.current()
  }, [selected, gv, colour, shown, tokens])

  // Ctrl/⌘ + wheel zooms about the pointer, as over the Structure drawing; a
  // plain wheel is left to the page (V044). A trackpad pinch sets ctrlKey.
  useEffect(() => {
    const el = box.current
    if (el === null || tooMany) return
    const onWheel = (e: WheelEvent) => {
      const n = net.current
      if (n === null || e.deltaY === 0 || !(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const k = n.getScale()
      const next = Math.max(0.1, Math.min(6, k * (e.deltaY < 0 ? WHEEL_STEP : 1 / WHEEL_STEP)))
      const p = n.DOMtoCanvas({ x: e.clientX - rect.left, y: e.clientY - rect.top })
      const c = n.getViewPosition()
      // The canvas point under the pointer stays under it.
      n.moveTo({ scale: next, position: { x: p.x + ((c.x - p.x) * k) / next, y: p.y + ((c.y - p.y) * k) / next } })
      relabel.current()
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [tooMany])

  const zoomBy = (f: number) => {
    const n = net.current
    if (n === null) return
    n.moveTo({ scale: Math.max(0.1, Math.min(6, n.getScale() * f)) })
    relabel.current()
  }
  const fit = () => {
    net.current?.fit()
    relabel.current()
  }
  if (tooMany) {
    return (
      <div className="rg-canvas rg-netwrap" data-drawing="network">
        <div className="rg-bar">{bar}</div>
        <div className="rg-net rg-wait" role="status">
          {`This view has ${fmt(drawn.length)} nodes; the network drawing builds at most ${fmt(NETWORK_MAX)} at once, so the page never freezes. Turn clustering on to start from its clusters, hide some with the legend, or switch to Structure, which draws them all.`}
        </div>
        {legend}
      </div>
    )
  }
  return (
    <div className="rg-canvas rg-netwrap" data-drawing="network">
      <div className="rg-bar">{bar}</div>
      {failed === null && (
        <div className="rg-zoom">
          <Button size="sm" aria-label="Zoom out" onClick={() => zoomBy(1 / ZOOM_STEP)}>
            −
          </Button>
          <Button size="sm" aria-label="Zoom in" onClick={() => zoomBy(ZOOM_STEP)}>
            +
          </Button>
          <Button size="sm" aria-label="Fit" onClick={fit}>
            Fit
          </Button>
        </div>
      )}
      <div
        ref={box}
        className="rg-net"
        tabIndex={failed === null ? 0 : -1}
        role="group"
        aria-roledescription="network drawing"
        aria-label={`Module network of ${name}: ${fmt(drawn.length)} nodes. Arrow keys pan, plus and minus zoom; the list after it picks a node.`}
        data-failed={failed === null ? undefined : 'true'}
      />
      {failed !== null && (
        <p className="rg-netfail" role="note">
          This view draws on a canvas, and this page could not create one ({failed}). Its nodes are listed below; Structure draws the same graph.
        </p>
      )}
      <ul className={failed === null ? 'rg-netlist is-quiet' : 'rg-netlist'} aria-label={`Nodes of the module network of ${name}`}>
        {drawn.map((n) => (
          <li key={n.id}>
            <button type="button" aria-pressed={n.id === selected} data-id={n.id} onClick={() => onPick(n)}>
              <span>{displayLabel(n)}</span>
              <small>{nodeWords(n, colour)}</small>
            </button>
          </li>
        ))}
      </ul>
      {legend}
    </div>
  )
}
