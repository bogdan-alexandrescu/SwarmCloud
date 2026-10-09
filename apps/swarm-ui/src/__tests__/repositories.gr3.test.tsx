// ONE REPOSITORY › GRAPH, lane GR3 (docs/design/graph-rendering.md §3, §7 and
// the owner addition of 2026-10-08): what the reader sees and does.
//
// WHAT EACH CASE HOLDS:
//   * the Structure | Network switch is a two-option radio group in the
//     canvas card's own bar, named "Graph view: Structure / Network",
//     Structure by default and remembered per browser;
//   * Network loads its chunk only when first opened (the import is mocked
//     here), shows a quiet loading state in the same box meanwhile, keeps the
//     selected node and the inspector, and a click in it lands in the same
//     inspector;
//   * a chunk that fails to load leaves the card on Structure and the switch
//     saying "Network unavailable" in words;
//   * the real vis-network view in jsdom (no 2D canvas) says so and lists
//     every node as a button instead, which a click picks; past NETWORK_MAX
//     nodes it builds nothing and says what draws them;
//   * the legend filters: a hidden band or cluster leaves the canvas, the
//     legend counts what it hides, Show all brings it back;
//   * the inspector lists neighbours by their unique labels, and a click on
//     one moves the inspector there; a measured none is the real-zero mark;
//   * a view past IN_PLACE_MAX nodes is laid out in a Web Worker: the card
//     says it is laying out (no guessed positions), then draws; a worker that
//     fails says so and offers to lay out on the page.
//
// MUTATIONS: import RepoGraphNetwork statically (the guard on the source
// reads red); drop the localStorage write (the remembered case is red);
// switch to Network after a failed load (the unavailable case is red); filter
// by the wrong band (the legend case is red); lay a big view out in place (the
// worker case never sees its status, red).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor, within } from '@testing-library/react'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { repo, serve, sha, visible } from './repofixture'
import { srcGraphBody } from './repographfixture'
import { graphView, normModuleGraph } from '../RepoGraphData'
import { layoutPlan, type LayoutRequest } from '../RepoGraphLayout'

const WAIT = { timeout: 4000 }
const ID = 'repo_0a1b2c3d4e5f6071'
const SRC = resolve(__dirname, '..')
const DETAIL = { repository: repo({ repo_id: ID }), index_runs: [], used_by: [] }
const FRESH = { index_sha: sha('a1b2c3d'), head_sha: sha('a1b2c3d'), behind_by: 0, stale: false, freshness: { state: 'current' } }

const GRAPH = {
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

function routes(graph: unknown = GRAPH) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    const u = new URL(url, 'http://x')
    if (u.pathname === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
    if (u.pathname === `/v1/repositories/${ID}/graph`) return { status: 200, body: graph }
    if (u.pathname === `/v1/repositories/${ID}/symbols`) return { status: 200, body: { ...FRESH, q: u.searchParams.get('q'), symbols: [], more: false } }
    return null
  })
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={`repo=${ID}&tab=graph`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-api'), WAIT)
  return utils
}

const card = () => document.querySelector<HTMLElement>('.rg-canvas')!
const drawSwitch = () => within(card()).getByRole('radiogroup', { name: 'Graph view: Structure / Network' })
const inspector = () => document.querySelector<HTMLElement>('.rg-inspector')!
const nodes = () => Array.from(document.querySelectorAll<SVGGElement>('.rg-canvas .rg-node'))
const node = (id: string) => document.querySelector<SVGGElement>(`.rg-canvas .rg-node[data-id="${id}"]`)!

/** A stand-in for the lazy chunk: renders its bar and a button per node, and records what it was given. */
function mockNetwork(gate: Promise<void> = Promise.resolve()) {
  const seen: { selected: string | null; nodes: string[] }[] = []
  vi.doMock('../RepoGraphNetwork', async () => {
    await gate
    return {
      NetworkCanvas: (p: { gv: { nodes: { id: string }[] }; shown: ReadonlySet<string>; selected: string | null; onPick: (n: unknown) => void; bar: React.ReactNode; legend: React.ReactNode }) => {
        seen.push({ selected: p.selected, nodes: p.gv.nodes.filter((n) => p.shown.has(n.id)).map((n) => n.id) })
        return (
          <div className="rg-canvas" data-drawing="network" data-mock="true">
            <div className="rg-bar">{p.bar}</div>
            {p.gv.nodes.map((n) => (
              <button key={n.id} type="button" data-net={n.id} onClick={() => p.onPick(n)}>
                {n.id}
              </button>
            ))}
            {p.legend}
          </div>
        )
      },
    }
  })
  return seen
}

afterEach(() => {
  vi.doUnmock('../RepoGraphNetwork')
  vi.resetModules()
  window.localStorage.clear()
})

describe('the Structure | Network switch', () => {
  it('sits in the canvas card, not the tool row, as a two-option radio group with Structure on', async () => {
    vi.resetModules()
    routes()
    await mount()
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    const radios = within(drawSwitch()).getAllByRole('radio')
    expect(radios.map((r) => [visible(r), r.getAttribute('aria-checked')])).toEqual([['Structure', 'true'], ['Network', 'false']])
    expect(drawSwitch().closest('.rg-bar')).not.toBeNull()
    expect(within(document.querySelector<HTMLElement>('.rg-tools')!).queryByRole('radiogroup', { name: /Structure/ })).toBeNull()
    // The console's segmented control, not a new component.
    expect(drawSwitch().classList.contains('c-seg')).toBe(true)
  })

  it('loads Network on first open with a quiet loading state, keeps the selection, and remembers the choice', async () => {
    vi.resetModules()
    let open!: () => void
    const seen = mockNetwork(new Promise<void>((r) => (open = r)))
    routes()
    await mount()
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    fireEvent.click(node('src/core/orders.py'))
    await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe('src/core/orders.py'), WAIT)
    fireEvent.click(within(drawSwitch()).getByRole('radio', { name: 'Network' }))
    // The chunk is in flight: the same card, saying so, with the switch still in it.
    expect(card().getAttribute('data-drawing')).toBe('network')
    expect(within(card()).getByRole('status').textContent).toBe('Loading the network view…')
    expect(within(drawSwitch()).getByRole('radio', { name: 'Network' }).getAttribute('aria-checked')).toBe('true')
    expect(window.localStorage.getItem('swarm.repograph.drawing')).toBe('network')
    await act(async () => open())
    await waitFor(() => expect(document.querySelector('[data-mock="true"]')).not.toBeNull(), WAIT)
    expect(seen.at(-1)!.selected).toBe('src/core/orders.py')
    expect(visible(inspector().querySelector('h2'))).toBe('src/core/orders.py')
    // A click in the network view lands in the same inspector.
    fireEvent.click(document.querySelector('[data-net="src/api/orders.py"]')!)
    await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe('src/api/orders.py'), WAIT)
    // And back: Structure draws again, with the node still picked.
    fireEvent.click(within(drawSwitch()).getByRole('radio', { name: 'Structure' }))
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    expect(node('src/api/orders.py').getAttribute('aria-pressed')).toBe('true')
    expect(window.localStorage.getItem('swarm.repograph.drawing')).toBe('structure')
  })

  it('opens on Network when the browser remembers it', async () => {
    vi.resetModules()
    window.localStorage.setItem('swarm.repograph.drawing', 'network')
    mockNetwork()
    routes()
    await mount()
    await waitFor(() => expect(document.querySelector('[data-mock="true"]')).not.toBeNull(), WAIT)
    expect(within(drawSwitch()).getByRole('radio', { name: 'Network' }).getAttribute('aria-checked')).toBe('true')
  })

  it('stays on Structure and says Network is unavailable when its chunk fails to load', async () => {
    vi.resetModules()
    vi.doMock('../RepoGraphNetwork', () => {
      throw new Error('Failed to fetch dynamically imported module')
    })
    routes()
    await mount()
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    fireEvent.click(within(drawSwitch()).getByRole('radio', { name: 'Network' }))
    await waitFor(() => expect(within(drawSwitch()).getByRole('radio', { name: 'Network unavailable' })).toBeTruthy(), WAIT)
    const off = within(drawSwitch()).getByRole('radio', { name: 'Network unavailable' })
    expect(off.hasAttribute('disabled')).toBe(true)
    expect(off.getAttribute('title')).toMatch(/could not load/)
    expect(within(drawSwitch()).getByRole('radio', { name: 'Structure' }).getAttribute('aria-checked')).toBe('true')
    expect(card().getAttribute('data-drawing')).toBe('structure')
    expect(nodes()).toHaveLength(4)
  })

  it('keeps vis-network and the layout out of the main bundle: only a dynamic import reaches them', () => {
    const graph = readFileSync(resolve(SRC, 'RepoGraph.tsx'), 'utf8')
    expect(graph).toContain("import('./RepoGraphNetwork')")
    expect(graph).not.toMatch(/from ['"]\.\/RepoGraphNetwork['"]/)
    expect(graph).not.toMatch(/from ['"]vis-network/)
    const statics = readdirSync(SRC)
      .filter((f) => /\.(ts|tsx)$/.test(f) && f !== 'RepoGraphNetwork.tsx')
      .filter((f) => /from ['"](\.\/RepoGraphNetwork|vis-network)/.test(readFileSync(resolve(SRC, f), 'utf8')))
    expect(statics).toEqual([])
    // The layout leaves the main bundle the same way: the worker and the lazy chunk import it, the page only by `import()` or `import type`.
    expect(graph).toContain("import('./RepoGraphLayout')")
    const layoutStatics = readdirSync(SRC)
      .filter((f) => /\.(ts|tsx)$/.test(f))
      .filter((f) => /^import (?!type )[^'"]*from ['"]\.\/RepoGraphLayout['"]/m.test(readFileSync(resolve(SRC, f), 'utf8')))
      .sort()
    expect(layoutStatics).toEqual(['RepoGraphNetwork.tsx', 'repoGraphLayout.worker.ts'])
    const pkg = JSON.parse(readFileSync(resolve(SRC, '../package.json'), 'utf8')) as { dependencies: Record<string, string> }
    expect(pkg.dependencies['vis-network']).toMatch(/^\d+\.\d+\.\d+$/)
  })
})

describe('the real Network view in jsdom', () => {
  it('says the canvas could not be created and lists every node as a button that picks it', async () => {
    const { NetworkCanvas, networkOptions, readTokens } = await import('../RepoGraphNetwork')
    const g = normModuleGraph(GRAPH)!
    const gv = graphView(g, new Set())
    const onPick = vi.fn()
    render(
      <NetworkCanvas name="example-org/example-api" gv={gv} colour="hot-spots" shown={new Set(gv.nodes.map((n) => n.id))} selected="src/core/orders.py" onPick={onPick} bar={<span />} legend={null} />,
    )
    await waitFor(() => expect(document.querySelector('.rg-netfail')).not.toBeNull(), WAIT)
    expect(visible(document.querySelector('.rg-netfail'))).toMatch(/could not create one/)
    const list = document.querySelector<HTMLElement>('.rg-netlist')!
    // Shown outright when the canvas failed; out of sight only over a working canvas.
    expect(list.classList.contains('is-quiet')).toBe(false)
    const buttons = within(list).getAllByRole('button')
    expect(buttons.map((b) => b.querySelector('span')!.textContent)).toEqual(['api/orders', 'core/orders', 'money', 'invoices'])
    expect(buttons[1]!.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(buttons[0]!)
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: 'src/api/orders.py' }))
    // Graphify's physics, with keyboard navigation ON.
    const o = networkOptions(readTokens())
    expect(o.physics.solver).toBe('forceAtlas2Based')
    expect(o.interaction.keyboard).toEqual({ enabled: true, bindToWindow: false })
  })

  it(`builds nothing past NETWORK_MAX nodes, and says what draws them instead`, async () => {
    const { NetworkCanvas, NETWORK_MAX } = await import('../RepoGraphNetwork')
    const g = normModuleGraph(srcGraphBody(NETWORK_MAX + 1))!
    const gv = graphView(g, new Set(), false)
    expect(gv.nodes).toHaveLength(NETWORK_MAX + 1)
    render(<NetworkCanvas name="x/y" gv={gv} colour="hot-spots" shown={new Set(gv.nodes.map((n) => n.id))} selected={null} onPick={vi.fn()} bar={<span />} legend={null} />)
    const status = document.querySelector<HTMLElement>('.rg-netwrap [role="status"]')!
    expect(status.textContent).toContain(`This view has ${NETWORK_MAX + 1} nodes; the network drawing builds at most ${NETWORK_MAX} at once`)
    expect(status.textContent).toContain('switch to Structure')
    expect(document.querySelector('.rg-netlist')).toBeNull()
    expect(document.querySelector('.rg-netfail')).toBeNull()
  })

  it('reads its colours from the theme tokens, at the time it draws', async () => {
    const { readTokens } = await import('../RepoGraphNetwork')
    const root = document.documentElement
    root.style.setProperty('--s-bad', 'rgb(1, 2, 3)')
    expect(readTokens().bad).toBe('rgb(1, 2, 3)')
    root.style.setProperty('--s-bad', 'rgb(4, 5, 6)')
    expect(readTokens().bad).toBe('rgb(4, 5, 6)')
    root.style.removeProperty('--s-bad')
  })
})

describe('the filtering legend', () => {
  it('hides a colour band and a cluster from the canvas, counts what it hides, and shows all again', async () => {
    vi.resetModules()
    routes()
    await mount()
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    const legend = () => document.querySelector<HTMLElement>('.rg-legend')!
    const hot = within(within(legend()).getByRole('group', { name: 'Show by colour' })).getByRole('button', { name: /hot-spot/ })
    expect(hot.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(hot)
    expect(hot.getAttribute('aria-pressed')).toBe('false')
    expect(nodes().map((n) => n.getAttribute('data-id')).sort()).toEqual(['src/core/money.py', 'workers/invoices.py'])
    expect(visible(legend())).toContain('2 of 4 nodes hidden by the legend')
    // The inspector and the phone list keep every node: hiding is drawing-only.
    expect(document.querySelectorAll('.rg-phone .rg-prow')).toHaveLength(4)
    fireEvent.click(within(legend()).getByRole('button', { name: 'Show all' }))
    expect(nodes()).toHaveLength(4)
    const clusters = within(legend()).getByRole('group', { name: 'Show clusters' })
    expect(within(clusters).getAllByRole('button').map(visible)).toEqual(['src', 'workers'])
    fireEvent.click(within(clusters).getByRole('button', { name: 'workers' }))
    expect(nodes().map((n) => n.getAttribute('data-id'))).not.toContain('workers/invoices.py')
    expect(document.querySelectorAll('.rg-canvas .rg-edge')).toHaveLength(2)
  })
})

describe('the inspector neighbour list', () => {
  it('lists callers and callees by unique label with their calls, and walks the graph on click', async () => {
    vi.resetModules()
    routes()
    await mount()
    await waitFor(() => expect(nodes()).toHaveLength(4), WAIT)
    fireEvent.click(node('src/core/orders.py'))
    await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe('src/core/orders.py'), WAIT)
    const from = within(inspector()).getByRole('region', { name: 'Called from' })
    expect(within(from).getAllByRole('button').map((b) => [b.querySelector('code')!.textContent, b.querySelector('small')!.textContent])).toEqual([
      ['api/orders', '12 calls'],
      ['invoices', '3 calls'],
    ])
    const calls = within(inspector()).getByRole('region', { name: 'Calls' })
    expect(within(calls).getAllByRole('button').map((b) => b.querySelector('code')!.textContent)).toEqual(['money'])
    fireEvent.click(within(calls).getByRole('button', { name: /money/ }))
    await waitFor(() => expect(visible(inspector().querySelector('h2'))).toBe('src/core/money.py'), WAIT)
    // money calls nothing drawn: a measured none, the real-zero mark.
    const none = within(inspector()).getByRole('region', { name: 'Calls' }).querySelector('.ctl-mark.is-zero')!
    expect(none.getAttribute('aria-label')).toBe('It calls no node drawn here')
    // The canvas labels are unique too: the two orders modules climb one directory.
    expect(nodes().map((n) => n.querySelector('text')!.textContent).sort()).toEqual(['api/orders', 'core/orders', 'invoices', 'money'])
  })
})

describe('the layout runs in a Web Worker past IN_PLACE_MAX nodes', () => {
  class FakeWorker {
    static made: FakeWorker[] = []
    static fail = false
    onmessage: ((e: MessageEvent) => void) | null = null
    onerror: ((e: ErrorEvent) => void) | null = null
    terminated = false
    constructor(
      public url: URL | string,
      public opts?: WorkerOptions,
    ) {
      FakeWorker.made.push(this)
    }
    postMessage(req: LayoutRequest) {
      setTimeout(() => {
        if (this.terminated) return
        if (FakeWorker.fail) this.onerror?.(new ErrorEvent('error', { message: 'worker script blocked' }))
        else this.onmessage?.(new MessageEvent('message', { data: { plan: layoutPlan(req.view, req.width, req.height) } }))
      }, 20)
    }
    terminate() {
      this.terminated = true
    }
  }

  async function unclustered() {
    vi.resetModules()
    FakeWorker.made = []
    vi.stubGlobal('Worker', FakeWorker)
    routes(srcGraphBody(200))
    await mount()
    // Clustered, the 200 modules are 8 folded clusters: laid out in place.
    await waitFor(() => expect(nodes()).toHaveLength(8), WAIT)
    expect(FakeWorker.made).toHaveLength(0)
    fireEvent.click(within(document.querySelector<HTMLElement>('.rg-tools')!).getByRole('button', { name: 'Cluster: directory' }))
  }

  it('says it is laying out, draws nothing guessed meanwhile, then draws every node with a unique label', async () => {
    FakeWorker.fail = false
    await unclustered()
    expect(within(card()).getByRole('status').textContent).toBe('Laying out 200 nodes off the page…')
    expect(nodes()).toHaveLength(0)
    expect(FakeWorker.made).toHaveLength(1)
    expect(FakeWorker.made[0]!.opts).toEqual({ type: 'module' })
    expect(String(FakeWorker.made[0]!.url)).toMatch(/repoGraphLayout\.worker\.ts/)
    await waitFor(() => expect(nodes()).toHaveLength(200), WAIT)
    const labels = nodes().map((n) => n.querySelector('text')!.textContent)
    expect(new Set(labels).size).toBe(200)
    // Back to clustered: the old job is dropped.
    fireEvent.click(within(document.querySelector<HTMLElement>('.rg-tools')!).getByRole('button', { name: 'Cluster: directory' }))
    await waitFor(() => expect(nodes()).toHaveLength(8), WAIT)
    expect(FakeWorker.made[0]!.terminated).toBe(true)
  })

  it('says a failed worker failed, and lays out on the page only when asked', async () => {
    FakeWorker.fail = true
    await unclustered()
    await waitFor(() => expect(within(card()).getByRole('alert').textContent).toContain('worker script blocked'), WAIT)
    expect(nodes()).toHaveLength(0)
    fireEvent.click(within(card()).getByRole('button', { name: 'Lay out here' }))
    await waitFor(() => expect(nodes()).toHaveLength(200), WAIT)
    FakeWorker.fail = false
  })
})
