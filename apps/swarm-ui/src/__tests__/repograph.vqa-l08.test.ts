/**
 * Visual QA lane L08 (part of #1038): the repository graph.
 *
 *   V044  the Network canvas no longer takes the mouse wheel as a zoom, so
 *         the page scrolls with the pointer over it;
 *   V045  Structure labels avoid the other nodes' discs and the edges, and
 *         carry a halo; Network labels are 12px on screen at any zoom, with
 *         a halo in the canvas card's colour;
 *   V113  vis-network's hover title is drawn in the console's tooltip colours.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { displayLabel, graphView, normModuleGraph } from '../RepoGraphData'
import { clusterLabelBox, layoutPlan, placeLabels, type LabelSpot } from '../RepoGraphLayout'
import { LABEL_PX, labelFont, networkData, networkOptions, type Tokens } from '../RepoGraphNetwork'
import { srcGraphBody } from './repographfixture'

const GRAPH_CSS = readFileSync(resolve(__dirname, '../styles/repograph.css'), 'utf8')

const T: Tokens = {
  surface: 'rgb(1, 1, 1)', surface2: 'rgb(2, 2, 2)', line: 'gray', edge: 'gray', text: 'white', textDim: 'silver',
  accent: 'blue', live: 'green', warn: 'orange', bad: 'red', font: 'Inter',
}

function rule(selector: string): string {
  const at = GRAPH_CSS.indexOf(`${selector} {`)
  expect(at, `${selector} has a rule in repograph.css`).toBeGreaterThanOrEqual(0)
  return GRAPH_CSS.slice(at, GRAPH_CSS.indexOf('}', at))
}

describe('V044: the Network canvas leaves the wheel to the page', () => {
  it('turns vis-network wheel zoom off and keeps the keys and the buttons', () => {
    const o = networkOptions(T)
    expect(o.interaction.zoomView).toBe(false)
    expect(o.interaction.keyboard).toEqual({ enabled: true, bindToWindow: false })
  })
})

describe('V045: Network labels read at 12px over edges and arrowheads', () => {
  it('holds a label at LABEL_PX on screen at the fitted scale, and at any zoom up to the cap', () => {
    for (const k of [0.66, 1, 2.5]) {
      const f = labelFont(T, k)
      expect(f.size * k).toBeCloseTo(LABEL_PX)
      expect(f.strokeWidth * k).toBeCloseTo(3)
    }
    // Zoomed far out the label stops growing in canvas units.
    expect(labelFont(T, 0.05).size).toBeLessThan(LABEL_PX / 0.05)
    expect(labelFont(T, 0).size).toBe(LABEL_PX)
  })

  it('haloes labels in the canvas card colour and lets the global font reach every node', () => {
    const f = labelFont(T, 1)
    expect(f.strokeColor).toBe(T.surface2)
    expect(f.strokeWidth).toBeGreaterThan(0)
    expect(networkOptions(T).nodes!.font).toEqual(f)
    const g = normModuleGraph(srcGraphBody(30))!
    const gv = graphView(g, new Set(), false)
    const { nodes } = networkData(gv, 'hot-spots', new Set(gv.nodes.map((n) => n.id)), T)
    // A per-node font would override the zoom-following global one.
    expect(nodes.every((n) => n.font === undefined)).toBe(true)
  })
})

describe('V045: Structure labels avoid discs and edges', () => {
  const spot = (id: string, x: number, y: number, weight: number, r = 10): LabelSpot => ({ id, x, y, r, text: id, weight })

  it('moves a label above its node when the place below would print over another node', () => {
    const p = placeLabels([spot('alpha', 200, 100, 1), spot('beta', 200, 128, 0, 6)])
    expect(p.get('alpha')!.side).toBe('above')
  })

  it('hangs a label off the right of its node when nodes sit above and below it', () => {
    const p = placeLabels([spot('alpha', 200, 100, 2), spot('up', 200, 70, 0, 6), spot('down', 200, 128, 0, 6)])
    expect(p.get('alpha')).toEqual({ side: 'right', x: 214, y: 104, anchor: 'start' })
  })

  it('keeps a label off a cluster heading', () => {
    const heading = clusterLabelBox({ cluster: 'src/core', x: 170, y: 100 })
    const p = placeLabels([spot('alpha', 200, 90, 1)], [], [heading])
    expect(p.get('alpha')!.side).toBe('above')
  })

  it('moves a label above its node when its own edge runs through the place below', () => {
    const p = placeLabels([spot('alpha', 200, 100, 1)], [{ x1: 200, y1: 100, x2: 200, y2: 300 }])
    expect(p.get('alpha')!.side).toBe('above')
    const none = placeLabels([spot('alpha', 200, 100, 1)])
    expect(none.get('alpha')!.side).toBe('below')
  })

  it('moves beside its node when an edge runs through it top to bottom, and stays below when every place has an edge', () => {
    const p = placeLabels([spot('alpha', 200, 100, 1)], [{ x1: 200, y1: 0, x2: 200, y2: 300 }])
    expect(p.get('alpha')!.side).toBe('right')
    const cross = placeLabels([spot('alpha', 200, 100, 1)], [{ x1: 200, y1: 0, x2: 200, y2: 300 }, { x1: 0, y1: 100, x2: 400, y2: 100 }])
    expect(cross.get('alpha')!.side).toBe('below')
  })

  it('draws no shown label over another node or a cluster heading in a laid-out view', () => {
    // Forty modules in eight packages: every cluster open, each with its heading.
    const g = normModuleGraph(srcGraphBody(40))!
    const gv = graphView(g, new Set(), true)
    const plan = layoutPlan(gv)
    const discs = [...plan.pos.entries()]
    const headings = plan.boxes.map(clusterLabelBox)
    expect(headings.length).toBeGreaterThan(0)
    let shown = 0
    for (const n of gv.nodes) {
      const place = plan.labels.get(n.id)!
      if (place.side === 'hidden') continue
      shown++
            const w = displayLabel(n).length * 6.2
      const x0 = place.anchor === 'middle' ? place.x - w / 2 : place.anchor === 'start' ? place.x : place.x - w
      const box = { x0, x1: x0 + w, y0: place.y - 11, y1: place.y + 3 }
      for (const h of headings) {
        expect(box.x1 <= h.x0 || h.x1 <= box.x0 || box.y1 <= h.y0 || h.y1 <= box.y0, `${n.id}'s label over a cluster heading`).toBe(true)
      }
      for (const [id, d] of discs) {
        if (id === n.id) continue
        const nx = Math.max(box.x0, Math.min(d.x, box.x1))
        const ny = Math.max(box.y0, Math.min(d.y, box.y1))
        expect((d.x - nx) ** 2 + (d.y - ny) ** 2, `${n.id}'s label over ${id}`).toBeGreaterThanOrEqual(d.r * d.r)
      }
    }
    expect(shown).toBeGreaterThan(0)
  })

  it('haloes a label so an edge under it is cut, not printed through', () => {
    const r = rule('.rg-node text')
    expect(r).toMatch(/paint-order: stroke/)
    expect(r).toMatch(/stroke: var\(--surface\)/)
  })
})

describe('V113: the Network hover title is themed', () => {
  it('draws .vis-tooltip in the console tooltip pair, more specific than vis-network div.vis-tooltip', () => {
    const r = rule('.rg-netwrap .vis-tooltip')
    expect(r).toMatch(/background: var\(--ctl-tip\)/)
    expect(r).toMatch(/color: var\(--ctl-tip-ink\)/)
    expect(r).toMatch(/font-family: var\(--font\)/)
    expect(r).toMatch(/border: 0/)
    // Position and visibility are vis-network's own, set inline.
    expect(r).not.toMatch(/visibility|position/)
  })
})
