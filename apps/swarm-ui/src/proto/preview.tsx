// THE PROTOTYPE'S PREVIEW PAGE: today's renderers and the prototypes, side by
// side on the same fixtures, for the screenshots in docs/design/graph-rendering.md.
//
// DEV SERVER ONLY. Run `VITE_LIVE=1 npx vite` in apps/swarm-ui and open
//   /src/proto/preview.html?graph=dag|repo&r=today|layered|graphify&theme=light|dark
// and, for the repository graph, &n=<modules> (default 48) and &flat=1 (no package clusters).
// VITE_LIVE=1 turns the console's fixture mode off so the repository graph
// reads its route, which this page answers itself (`answerGraphRoute`) with the
// labelled fixture; nothing leaves the page. No route, no nav entry, and
// `vite build` never sees this file.

import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/inter-tight'
import '@fontsource/dm-mono/400.css'
import '@fontsource/dm-mono/500.css'
import '../styles.css'
import '../styles/repograph.css'
import { applyTheme } from '../theme'
import { WorkflowCard } from '../Workflows'
import { GraphTab } from '../RepoGraph'
import { graphView, normModuleGraph } from '../RepoGraphData'
import { normRepo } from '../RepositoriesData'
import { GraphifyRepoGraph, GraphifyWorkflow } from './GraphifyWorkflow'
import { protoGraphBody, protoWorkflow } from './fixtures'
import { barycentricOrder, drawnCrossings } from './layered'

const q = new URLSearchParams(location.search)
const graph = q.get('graph') === 'repo' ? 'repo' : 'dag'
const r = q.get('r') === 'graphify' ? 'graphify' : q.get('r') === 'layered' ? 'layered' : 'today'
applyTheme(q.get('theme') === 'light' ? 'light' : 'dark')

const REPO_ID = 'repo_0a1b2c3d4e5f6071'
const MODULES = Math.max(1, Math.min(5000, Number(q.get('n') ?? 48) || 48))
const BODY = protoGraphBody(MODULES)

/** Answer the one route the repository graph reads, from the fixture; every other read is "not served". */
function answerGraphRoute() {
  const real = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input instanceof Request ? input.url : input), location.origin)
    if (url.pathname === `/v1/repositories/${REPO_ID}/graph`) return new Response(JSON.stringify(BODY), { status: 200, headers: { 'content-type': 'application/json' } })
    if (url.pathname.startsWith('/v1/')) return new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404, headers: { 'content-type': 'application/json' } })
    return real(input, init)
  }) as typeof fetch
}

function DagToday({ reorder }: { reorder: boolean }) {
  const [{ workflow, taskById }] = useState(() => protoWorkflow())
  const shown = reorder ? { ...workflow, steps: barycentricOrder(workflow.steps) } : workflow
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [zoom, setZoom] = useState<'auto' | 'figures' | 'details' | 'names'>('auto')
  return (
    <>
      <p className="gfy-note">
        {reorder ? 'Option D: today’s renderer, levels ordered by the barycentric pass' : 'Today’s renderer (dag.ts layoutOf)'} · {drawnCrossings(shown.steps)} edge crossings between adjacent stages
      </p>
      <WorkflowCard
        workflow={shown}
        taskById={taskById}
        usage={{ kind: 'ready', usage: null }}
        openStages={stages}
        onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
        zoom={zoom}
        onZoom={(_id, z) => setZoom(z)}
        reload={() => undefined}
        page
      />
    </>
  )
}

function RepoToday() {
  const rec = normRepo({ repo_id: REPO_ID, owner: 'example-org', repo: 'example-api', default_branch: 'main', allowed_profiles: ['claude-code'], created_by: 'operator@swarm.example.com', created_at: '2026-10-07T12:00:00Z', index: {} })
  return rec === null ? <p>fixture repository did not normalise</p> : <GraphTab r={rec} />
}

function RepoGraphify() {
  // Clustered as today's canvas is by default; `flat=1` draws every module, as Graphify does up to its 5,000-node cap.
  const [view] = useState(() => graphView(normModuleGraph(BODY)!, new Set(), q.get('flat') !== '1'))
  return <GraphifyRepoGraph view={view} />
}

function DagGraphify() {
  const [{ workflow, taskById }] = useState(() => protoWorkflow())
  return <GraphifyWorkflow workflow={workflow} taskById={taskById} />
}

function Page() {
  return (
    <main className="gfy-page">
      <header className="gfy-head">
        <b>PROTOTYPE · FIXTURE DATA</b> {graph === 'dag' ? '48-step workflow wf_proto_graph48' : `${MODULES}-module repository example-org/example-api`} · renderer: {r}
      </header>
      {graph === 'dag' && r === 'graphify' && <DagGraphify />}
      {graph === 'dag' && r !== 'graphify' && <DagToday reorder={r === 'layered'} />}
      {graph === 'repo' && r === 'graphify' && <RepoGraphify />}
      {graph === 'repo' && r !== 'graphify' && <RepoToday />}
    </main>
  )
}

const style = document.createElement('style')
style.textContent = `
.gfy-page { padding: 16px; background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; box-sizing: border-box; }
.gfy-head { font: 12px var(--mono); color: var(--text-dim); margin-bottom: 12px; }
.gfy-head b { color: var(--warn-ink, var(--warn)); }
.gfy-note { font: 12px var(--mono); color: var(--text-dim); margin: 0 0 8px; }
.gfy-fig { margin: 0; }
.gfy-canvas { width: 100%; border: 1px solid var(--line); border-radius: var(--radius, 8px); }
.gfy-cap { font: 12px var(--mono); color: var(--text-dim); margin-top: 6px; }
.gfy-failed { color: var(--bad-ink, var(--bad)); }
.gfy-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
`
document.head.appendChild(style)

answerGraphRoute()
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Page />
  </StrictMode>,
)
