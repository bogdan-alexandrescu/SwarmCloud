// Visual QA re-run lane R11 (2026-10-16, part of #1427): the repository graph.
//
//   V044  over the Structure drawing a plain wheel is the page's: only
//         Ctrl/⌘ + wheel (and a trackpad pinch, which sets ctrlKey) zooms, as
//         over the Network drawing (owner decision Q3);
//   V045  no two cluster rectangles overlap (src/api printed over src/auth at
//         60 modules); the selected label is printed on top of every node, at
//         a place clear of the other discs; the Network view draws no label
//         over another;
//   V225  on a phone the module list opens with the open module's name and a
//         way to its inspector, and picking from the list goes there.
//
// MUTATIONS: drop the ctrlKey/metaKey test in either wheel listener (the
// plain-wheel cases are red); drop `partBoxes` from the layout loop (the
// rectangle case is red); print the selected label inside its node again
// (the on-top case is red); keep every Network label (the keptLabels and
// stabilisation cases are red); drop the `.rg-psel` bar or the jump (the
// phone cases are red).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor, within } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'
import { srcGraphBody } from './repographfixture'
import { displayLabel, graphView, normModuleGraph } from '../RepoGraphData'
import { layoutPlan } from '../RepoGraphLayout'

const WAIT = { timeout: 4000 }
const ID = 'repo_0a1b2c3d4e5f6071'
const DETAIL = { repository: repo({ repo_id: ID }), index_runs: [], used_by: [] }
const FRESH = { index_sha: sha('a1b2c3d'), head_sha: sha('a1b2c3d'), behind_by: 0, stale: false, freshness: { state: 'current' } }

const SMALL = {
  ...FRESH,
  modules: [
    { id: 'src/api/orders.py', modules: 1, symbols: 30, tests: 0, hot_spot_changes: 25, test_reach: 0.5, languages: ['python'] },
    { id: 'src/core/orders.py', modules: 1, symbols: 14, tests: 0, hot_spot_changes: 23, test_reach: 0.78, languages: ['python'] },
    { id: 'src/core/money.py', modules: 1, symbols: 6, tests: 0, hot_spot_changes: 2, test_reach: 0.9, languages: ['python'] },
    { id: 'workers/invoices.py', modules: 1, symbols: 9, tests: 0, hot_spot_changes: null, test_reach: null, languages: ['python'] },
  ],
  edges: [
    { from: 'src/api/orders.py', to: 'src/core/orders.py', weight: 12, kinds: { call: 12 }, max_confidence: 0.95 },
    { from: 'workers/invoices.py', to: 'src/core/orders.py', weight: 3, kinds: { call: 3 }, max_confidence: 0.6 },
    { from: 'src/core/orders.py', to: 'src/core/money.py', weight: 5, kinds: { call: 5 }, max_confidence: 0.95 },
  ],
  counts: { files: 20, symbols: 300, edges: 900 },
  truncated: [],
}

function routes(graph: unknown) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    const u = new URL(url, 'http://x')
    if (u.pathname === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
    if (u.pathname === `/v1/repositories/${ID}/graph`) return { status: 200, body: graph }
    if (u.pathname === `/v1/repositories/${ID}/symbols`) return { status: 200, body: { ...FRESH, q: u.searchParams.get('q'), symbols: [], more: false } }
    return null
  })
}

async function mount(graph: unknown) {
  vi.resetModules()
  routes(graph)
  vi.stubEnv('VITE_LIVE', '1')
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={`repo=${ID}&tab=graph`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-api'), WAIT)
  return utils
}

const card = () => document.querySelector<HTMLElement>('.rg-canvas')!
const svg = () => card().querySelector<SVGSVGElement>('svg.rg-svg')!
const world = () => card().querySelector('.rg-world')!.getAttribute('transform')
const nodes = () => Array.from(document.querySelectorAll<SVGGElement>('.rg-canvas .rg-node'))
const node = (id: string) => document.querySelector<SVGGElement>(`.rg-canvas .rg-node[data-id="${id}"]`)!
const inspector = () => document.querySelector<HTMLElement>('.rg-inspector')!

afterEach(() => {
  vi.doUnmock('vis-network/standalone')
  vi.resetModules()
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe('V044: over the Structure drawing a plain wheel scrolls the page', () => {
  it('leaves a plain wheel alone: the drawing keeps its zoom and the event its default', async () => {
    await mount(SMALL)
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    const fit = world()
    // fireEvent returns false when a listener called preventDefault.
    expect(fireEvent.wheel(svg(), { deltaY: 100, clientX: 10, clientY: 10 })).toBe(true)
    expect(fireEvent.wheel(svg(), { deltaY: -100, clientX: 10, clientY: 10 })).toBe(true)
    expect(world()).toBe(fit)
  })

  it('zooms on Ctrl + wheel and on ⌘ + wheel, and takes the event from the page', async () => {
    await mount(SMALL)
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    expect(fireEvent.wheel(svg(), { deltaY: -100, clientX: 0, clientY: 0, ctrlKey: true })).toBe(false)
    expect(world()).toMatch(/scale\(1\.1\)/)
    expect(fireEvent.wheel(svg(), { deltaY: 100, clientX: 0, clientY: 0, metaKey: true })).toBe(false)
    expect(world()).toMatch(/scale\(1\)/)
  })

  it('says so on the zoom controls', async () => {
    await mount(SMALL)
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    expect(within(card()).getByRole('group', { name: 'Zoom' }).getAttribute('title')).toMatch(/^Ctrl or ⌘ and the wheel zoom/)
  })
})

describe('V045: the Structure drawing', () => {
  it('draws no two cluster rectangles over each other, at 24, 48 and 60 modules', () => {
    let visited = 0
    for (const n of [24, 48, 60]) {
      const plan = layoutPlan(graphView(normModuleGraph(srcGraphBody(n))!, new Set(), true), 640, 440)
      const bs = plan.boxes
      expect(bs.length).toBeGreaterThan(4)
      for (let i = 0; i < bs.length; i++) {
        for (let j = i + 1; j < bs.length; j++) {
          const a = bs[i]!
          const b = bs[j]!
          const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
          expect(overlap, `${n} modules: ${a.cluster} over ${b.cluster}`).toBe(false)
          visited++
        }
      }
    }
    expect(visited).toBeGreaterThan(30)
  })

  it('places every node\'s selected label inside the canvas, and clear of every other disc at 60 modules', () => {
    const gv = graphView(normModuleGraph(srcGraphBody(60))!, new Set(), true)
    const plan = layoutPlan(gv, 640, 440)
    expect(plan.picked.size).toBe(gv.nodes.length)
    let clear = 0
    for (const n of gv.nodes) {
      const p = plan.picked.get(n.id)!
      expect(p.side).not.toBe('hidden')
      const w = displayLabel(n).length * 6.8
      const x0 = p.anchor === 'middle' ? p.x - w / 2 : p.anchor === 'start' ? p.x : p.x - w
      const box = { x0, x1: x0 + w, y0: p.y - 11, y1: p.y + 3 }
      expect(box.x0).toBeGreaterThanOrEqual(0)
      expect(box.x1).toBeLessThanOrEqual(640)
      const hits = [...plan.pos.entries()].filter(([id, d]) => {
        if (id === n.id) return false
        const nx = Math.max(box.x0, Math.min(d.x, box.x1))
        const ny = Math.max(box.y0, Math.min(d.y, box.y1))
        return (d.x - nx) ** 2 + (d.y - ny) ** 2 < d.r * d.r
      })
      if (hits.length === 0) clear++
    }
    expect(clear).toBe(gv.nodes.length)
  })

  it('prints the selected label as the drawing\'s last child, so no node drawn after it cuts it', async () => {
    await mount(SMALL)
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    fireEvent.click(node('src/core/money.py'))
    await waitFor(() => expect(node('src/core/money.py').getAttribute('aria-pressed')).toBe('true'), WAIT)
    const top = card().querySelector('.rg-world')!.lastElementChild!
    expect(top.classList.contains('rg-toplabel')).toBe(true)
    expect(top.getAttribute('data-id')).toBe('src/core/money.py')
    expect(top.textContent).toBe(node('src/core/money.py').querySelector('text')!.textContent)
    expect(top.getAttribute('aria-hidden')).toBe('true')
    // The node's own copy is hidden, so the label is printed once.
    expect((node('src/core/money.py').querySelector('text') as SVGTextElement).style.visibility).toBe('hidden')
    expect((node('src/api/orders.py').querySelector('text') as SVGTextElement).style.visibility).toBe('')
    expect(card().querySelectorAll('.rg-toplabel')).toHaveLength(1)
  })
})

describe('V045: the Network drawing keeps no label over another', () => {
  it('keeps the busier of two colliding labels, the selected one before both, and both when apart', async () => {
    const { keptLabels } = await import('../RepoGraphNetwork')
    const a = { id: 'a', x: 100, y: 100, r: 8, text: 'src/api/orders.py', weight: 1 }
    const b = { id: 'b', x: 110, y: 104, r: 8, text: 'src/core/orders.py', weight: 5 }
    const c = { id: 'c', x: 400, y: 300, r: 8, text: 'src/db/models.py', weight: 0 }
    expect([...keptLabels([a, b, c], null)].sort()).toEqual(['b', 'c'])
    expect([...keptLabels([a, b, c], 'a')].sort()).toEqual(['a', 'c'])
    expect([...keptLabels([a, { ...b, y: 200 }, c], null)].sort()).toEqual(['a', 'b', 'c'])
  })
})

/** A vis-network stand-in: records the calls the Network view makes on it. */
function fakeVis() {
  const calls: { moveTo: { scale: number; position?: { x: number; y: number } }[]; updates: { id: string; label: string }[][] } = { moveTo: [], updates: [] }
  const handlers = new Map<string, () => void>()
  let scale = 1
  class DataSet<T extends { id: string }> {
    items: T[]
    constructor(items: T[]) {
      this.items = items
    }
    update(changes: { id: string; label: string }[]) {
      calls.updates.push(changes)
    }
  }
  class Network {
    nodes: { id: string }[]
    constructor(_el: HTMLElement, data: { nodes: DataSet<{ id: string }> }) {
      this.nodes = data.nodes.items
    }
    once(ev: string, f: () => void) {
      handlers.set(ev, f)
    }
    on() {}
    setOptions() {}
    selectNodes() {}
    destroy() {}
    fit() {}
    getScale() {
      return scale
    }
    // Every node on one point: every label but one collides.
    getPositions() {
      return Object.fromEntries(this.nodes.map((n) => [n.id, { x: 0, y: 0 }]))
    }
    getViewPosition() {
      return { x: 0, y: 0 }
    }
    DOMtoCanvas(p: { x: number; y: number }) {
      return p
    }
    moveTo(o: { scale: number; position?: { x: number; y: number } }) {
      calls.moveTo.push(o)
      scale = o.scale
    }
  }
  vi.doMock('vis-network/standalone', () => ({ DataSet, Network }))
  return { calls, settle: () => handlers.get('stabilizationIterationsDone')?.() }
}

async function mountNetwork() {
  vi.resetModules()
  const vis = fakeVis()
  const { NetworkCanvas } = await import('../RepoGraphNetwork')
  const gv = graphView(normModuleGraph(SMALL)!, new Set(), false)
  render(<NetworkCanvas name="example-api" gv={gv} colour="hot-spots" shown={new Set(gv.nodes.map((n) => n.id))} selected="src/core/money.py" onPick={vi.fn()} bar={null} legend={null} />)
  const box = document.querySelector<HTMLElement>('.rg-net')!
  // The network is built in a task of its own.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 5))
  })
  return { ...vis, box }
}

describe('V044 / V045: the Network view, over a stand-in vis-network', () => {
  it('zooms on Ctrl + wheel about the pointer, and leaves a plain wheel to the page', async () => {
    const { calls, box } = await mountNetwork()
    expect(fireEvent.wheel(box, { deltaY: -100, clientX: 20, clientY: 20 })).toBe(true)
    expect(calls.moveTo).toHaveLength(0)
    expect(fireEvent.wheel(box, { deltaY: -100, clientX: 20, clientY: 20, ctrlKey: true })).toBe(false)
    expect(calls.moveTo).toHaveLength(1)
    expect(calls.moveTo[0]!.scale).toBeCloseTo(1.1)
    // The point under the pointer (20, 20) stays there: the centre moves toward it.
    expect(calls.moveTo[0]!.position!.x).toBeCloseTo(20 - 20 / 1.1)
    expect(fireEvent.wheel(box, { deltaY: 100, clientX: 20, clientY: 20, metaKey: true })).toBe(false)
    expect(calls.moveTo).toHaveLength(2)
  })

  it('once settled, blanks every label that collides and keeps the selected one', async () => {
    const { calls, settle } = await mountNetwork()
    act(() => settle())
    const changed = calls.updates.flat()
    const blanked = changed.filter((c) => c.label === '').map((c) => c.id)
    expect(blanked.sort()).toEqual(['src/api/orders.py', 'src/core/orders.py', 'workers/invoices.py'])
    expect(blanked).not.toContain('src/core/money.py')
  })
})

describe('V225: on a phone the open module says so at the top of the list', () => {
  it('names the open module before the list, and its button lands on the inspector', async () => {
    const scrolled: Element[] = []
    const was = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this)
    }
    try {
      await mount(srcGraphBody(60))
      const list = await waitFor(() => {
        const el = document.querySelector<HTMLElement>('.rg-phone')
        expect(el?.querySelectorAll('.rg-prow').length).toBe(60)
        return el!
      }, WAIT)
      const bar = document.querySelector<HTMLElement>('.rg-psel')!
      expect(bar).not.toBeNull()
      // Before the list in document order, and naming what the inspector shows.
      expect(bar.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      const open = visible(inspector().querySelector('h2'))
      expect(visible(bar)).toContain(`Open: ${open}`)
      fireEvent.click(within(bar).getByRole('button', { name: 'Go to its inspector' }))
      expect(scrolled).toContain(inspector())
      expect(document.activeElement).toBe(inspector().querySelector('h2'))
    } finally {
      Element.prototype.scrollIntoView = was
    }
  })

  it('goes to the inspector when a module is picked from the list', async () => {
    const scrolled: Element[] = []
    const was = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this)
    }
    try {
      await mount(srcGraphBody(60))
      const list = await waitFor(() => {
        const el = document.querySelector<HTMLElement>('.rg-phone')
        expect(el?.querySelectorAll('.rg-prow').length).toBe(60)
        return el!
      }, WAIT)
      // Queried by class: the stylesheet hides the phone list at jsdom's desktop width, so roles do not see it.
      const rows = Array.from(list.querySelectorAll<HTMLButtonElement>('.rg-prow'))
      expect(rows.length).toBe(60)
      const last = rows[rows.length - 1]!
      const id = last.querySelector('.rg-pname')!.textContent!
      fireEvent.click(last)
      await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe(id), WAIT)
      await waitFor(() => expect(document.activeElement).toBe(inspector().querySelector('h2')), WAIT)
      expect(scrolled).toContain(inspector())
      expect(visible(document.querySelector('.rg-psel'))).toContain(`Open: ${id}`)
    } finally {
      Element.prototype.scrollIntoView = was
    }
  })
})
