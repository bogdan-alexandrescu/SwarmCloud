// OPTIONS A AND B, PROTOTYPED: the console's two graphs drawn the way Graphify
// draws graphs.
//
// WHAT "WITH GRAPHIFY" MEANS HERE. Graphify (github.com/Graphify-Labs/graphify,
// Apache-2.0, read at 6478eb7 on 2026-10-08) is a Python CLI that BUILDS a
// knowledge graph; it publishes no npm package and no React component. Its
// interactive view, graph.html, is vis-network 9.1.6 loaded from unpkg with the
// options in graphify/exporters/html.py `_html_script` (forceAtlas2Based
// physics, golden-angle seeding, stabilise 200 iterations then freeze). So the
// only way to "render with Graphify" in a React console is to render with its
// renderer: vis-network, here the current 10.1.2, pinned, with Graphify's own
// options for the repo graph and vis-network's hierarchical layout for the DAG
// (Graphify itself has no DAG view; its tree.html is a d3 tree and its
// callflow.html is Mermaid).
//
// NOT IMPORTED BY THE APP (see fixtures.ts). The preview page mounts it.

import { useEffect, useRef, useState } from 'react'
import { DataSet, Network, type Edge, type Node, type Options } from 'vis-network/standalone'
import { levelsOf } from '../dag'
import { STATE_MARK, type MarkHue } from '../marks'
import { heatOf, radiusOf, type ColourBy, type GraphView, type Heat } from '../RepoGraphData'
import type { Task, Workflow } from '../types'

/**
 * A canvas cannot read a CSS variable, so every token is read off <html> as a
 * concrete colour and read AGAIN when the theme changes. This is the first cost
 * of a canvas renderer here: the console's light/dark switch is one attribute
 * for every SVG and HTML graph, and a manual re-paint for a canvas one.
 */
export interface Tokens {
  bg: string
  surface: string
  surface2: string
  line: string
  text: string
  textDim: string
  accent: string
  live: string
  park: string
  bad: string
  warn: string
  neu: string
  font: string
  mono: string
}

export function readTokens(el: Element = document.documentElement): Tokens {
  const cs = getComputedStyle(el)
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback
  return {
    bg: v('--bg', '#06101e'),
    surface: v('--surface', '#0c1a2e'),
    surface2: v('--surface-2', '#12233b'),
    line: v('--line', '#2a3b55'),
    text: v('--text', '#e6eef7'),
    textDim: v('--text-dim', '#9fb2c8'),
    accent: v('--sk-ac', v('--info', '#7cc8f8')),
    live: v('--s-live', '#38bdf8'),
    park: v('--s-park', '#d9a54a'),
    bad: v('--s-bad', '#ef6b6b'),
    warn: v('--s-warn', '#e2b54a'),
    neu: v('--s-neu', '#8aa0b8'),
    font: v('--font', 'sans-serif'),
    mono: v('--mono', 'monospace'),
  }
}

/** Re-read the tokens whenever <html data-theme> or the OS scheme changes. */
function useTokens(): Tokens {
  const [tokens, setTokens] = useState<Tokens>(() => readTokens())
  useEffect(() => {
    const again = () => setTokens(readTokens())
    const mo = new MutationObserver(again)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const mq = window.matchMedia?.('(prefers-color-scheme: light)')
    mq?.addEventListener?.('change', again)
    return () => {
      mo.disconnect()
      mq?.removeEventListener?.('change', again)
    }
  }, [])
  return tokens
}

const hueOf = (t: Tokens, hue: MarkHue | 'none') => (hue === 'live' ? t.live : hue === 'park' ? t.park : hue === 'bad' ? t.bad : t.neu)

/** The DAG as vis-network data: one node per step on its `levelsOf` level, one edge per `depends_on`. Nothing invented. */
export function dagData(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null, t: Tokens): { nodes: Node[]; edges: Edge[] } {
  const levels = levelsOf(workflow.steps)
  const nodes: Node[] = []
  levels.forEach((lv, level) =>
    lv.forEach((s) => {
      const task = s.task_id ? taskById?.get(s.task_id) : undefined
      // A step with no task is UNSTARTED, and a task the read did not return is NOT READ: neither is given a state.
      const word = task ? STATE_MARK[task.state].mark : s.task_id ? 'not read' : 'not started'
      const hue = task ? STATE_MARK[task.state].hue : 'none'
      const edge = hueOf(t, hue)
      nodes.push({
        id: s.step_id,
        level,
        label: `${s.step_id}\n${word}`,
        shape: 'box',
        margin: { top: 8, right: 10, bottom: 8, left: 10 },
        borderWidth: task ? 2 : 1,
        shapeProperties: { borderDashes: task ? false : [4, 3], borderRadius: 6 },
        color: { background: t.surface, border: edge, highlight: { background: t.surface2, border: t.accent }, hover: { background: t.surface2, border: t.accent } },
        font: { color: t.text, face: t.mono, size: 13, multi: false },
        title: `${s.step_id} · ${s.runner_profile} · ${word}`,
      })
    }),
  )
  const edges: Edge[] = workflow.steps.flatMap((s) =>
    s.depends_on.map((d) => ({ id: `${d}>${s.step_id}`, from: d, to: s.step_id, dashes: s.input_from[d] !== undefined ? [6, 4] : false })),
  )
  return { nodes, edges }
}

export function dagOptions(t: Tokens): Options {
  return {
    layout: { hierarchical: { enabled: true, direction: 'UD', sortMethod: 'directed', shakeTowards: 'roots', levelSeparation: 96, nodeSpacing: 170, treeSpacing: 60, blockShifting: true, edgeMinimization: true, parentCentralization: true } },
    physics: { enabled: false },
    interaction: { hover: true, keyboard: { enabled: true, bindToWindow: false }, navigationButtons: false, zoomView: true, dragView: true, dragNodes: false },
    edges: { color: { color: t.line, highlight: t.accent, hover: t.accent }, width: 1.5, arrows: { to: { enabled: true, scaleFactor: 0.5 } }, smooth: { enabled: true, type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.5 } },
  }
}

/** The modules view as vis-network data, coloured by OUR heat bands (not Graphify's communities), grouped by package. */
export function repoData(view: GraphView, by: ColourBy, t: Tokens): { nodes: Node[]; edges: Edge[] } {
  const fill: Record<Heat, string> = { hot: t.bad, warm: t.warn, cool: t.live, unmeasured: t.surface }
  const max = Math.max(1, ...view.edges.map((e) => e.weight))
  // Graphify's golden-angle spiral seed (html.py, #3699): spreads nodes before physics so repulsion never divides by ~0.
  const nodes: Node[] = view.nodes.map((n, i) => {
    const heat = heatOf(n, by)
    return {
      id: n.id,
      label: n.label,
      x: 30 * Math.sqrt(i) * Math.cos(i * 2.4),
      y: 30 * Math.sqrt(i) * Math.sin(i * 2.4),
      shape: 'dot',
      size: radiusOf(n.symbols, n.isPackage),
      group: n.cluster,
      borderWidth: 1.5,
      shapeProperties: { borderDashes: heat === 'unmeasured' ? [3, 2] : false },
      color: { background: fill[heat], border: heat === 'unmeasured' ? t.textDim : t.surface, highlight: { background: fill[heat], border: t.accent } },
      font: { color: t.textDim, face: t.font, size: 12 },
      title: `${n.id} · ${n.symbols ?? 'no'} symbols · ${heat}`,
    }
  })
  const edges: Edge[] = view.edges.map((e, i) => ({
    id: i,
    from: e.from,
    to: e.to,
    width: 1 + (2.5 * e.weight) / max,
    dashes: e.max_confidence !== null && e.max_confidence < 0.4,
    color: { color: t.line, highlight: t.accent, hover: t.accent, opacity: 0.8 },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
  }))
  return { nodes, edges }
}

/** Graphify's graph.html options, verbatim where they apply (html.py `_html_script`). */
export function repoOptions(t: Tokens): Options {
  return {
    physics: {
      enabled: true,
      solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -60, centralGravity: 0.005, springLength: 120, springConstant: 0.08, damping: 0.4, avoidOverlap: 0.8 },
      stabilization: { iterations: 200, fit: true },
    },
    interaction: { hover: true, tooltipDelay: 100, hideEdgesOnDrag: true, navigationButtons: false, keyboard: { enabled: true, bindToWindow: false } },
    nodes: { shape: 'dot', borderWidth: 1.5 },
    edges: { smooth: { enabled: true, type: 'continuous', roundness: 0.2 }, selectionWidth: 3, color: { color: t.line } },
  }
}

type Draw = (t: Tokens) => { data: { nodes: Node[]; edges: Edge[] }; options: Options }

/**
 * One vis-network canvas. A canvas has no per-node DOM, so the accessible name
 * is the container's and the steps are listed beside it for a screen reader --
 * the work adoption would add to every graph (design doc §3.3).
 */
function VisCanvas({ draw, label, height, list, onPick }: { draw: Draw; label: string; height: number; list: readonly string[]; onPick?: (id: string) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const tokens = useTokens()
  const [failed, setFailed] = useState<string | null>(null)
  const [ms, setMs] = useState<number | null>(null)
  const [settled, setSettled] = useState<number | null>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const { data, options } = draw(tokens)
    let net: Network | null = null
    const t0 = performance.now()
    try {
      net = new Network(el, { nodes: new DataSet(data.nodes), edges: new DataSet(data.edges) }, options)
      net.once('stabilizationIterationsDone', () => {
        setSettled(Math.round(performance.now() - t0))
        net?.setOptions({ physics: { enabled: false } })
      })
      net.on('click', (p: { nodes: (string | number)[] }) => {
        const id = p.nodes[0]
        if (id !== undefined) onPick?.(String(id))
      })
      setMs(Math.round(performance.now() - t0))
      setFailed(null)
    } catch (e) {
      // jsdom has no 2D canvas: say so in place instead of drawing nothing.
      setFailed(e instanceof Error ? e.message : String(e))
    }
    return () => net?.destroy()
  }, [draw, tokens, onPick])
  return (
    <figure className="gfy-fig">
      <div ref={ref} className="gfy-canvas" role="img" aria-label={label} style={{ height, background: tokens.bg }} data-failed={failed === null ? undefined : 'true'} />
      {failed !== null && <p className="gfy-failed">This drawing needs a 2D canvas, and this page has none: {failed}</p>}
      <figcaption className="gfy-cap">
        vis-network 10.1.2 (Graphify's renderer){ms === null ? '' : ` · constructed in ${ms} ms`}
        {settled === null ? '' : ` · physics settled in ${settled} ms`}
      </figcaption>
      <ul className="gfy-sr" aria-label={`${label}, as a list`}>
        {list.map((id) => (
          <li key={id}>{id}</li>
        ))}
      </ul>
    </figure>
  )
}

export function GraphifyWorkflow({ workflow, taskById, height = 720 }: { workflow: Workflow; taskById: ReadonlyMap<string, Task> | null; height?: number }) {
  const [draw] = useState<Draw>(() => (t: Tokens) => ({ data: dagData(workflow, taskById, t), options: dagOptions(t) }))
  return <VisCanvas draw={draw} height={height} label={`Workflow ${workflow.workflow_id}: ${workflow.steps.length} steps`} list={workflow.steps.map((s) => s.step_id)} />
}

export function GraphifyRepoGraph({ view, by = 'hot-spots', height = 560 }: { view: GraphView; by?: ColourBy; height?: number }) {
  const [draw] = useState<Draw>(() => (t: Tokens) => ({ data: repoData(view, by, t), options: repoOptions(t) }))
  return <VisCanvas draw={draw} height={height} label={`Module graph: ${view.nodes.length} nodes, ${view.edges.length} edges`} list={view.nodes.map((n) => n.id)} />
}
