// A PLAN'S APPROVAL PAGE AS A ONE-PAGE BRIEF (owner, 6 November 2026: show
// super quickly what a plan does, why, what changes where, the risk and the
// test and QA plan). Built from the picked mock-up (direction "brief").
//
// WHAT EACH CASE HOLDS:
//   * the header card draws the purpose, why and the four facts (size,
//     estimate, risk, overlaps) from a plan the planner served sections for,
//     read off the run's own read (`GET /v1/runs/{id}`);
//   * a plan stored before the sections draws its summary and its steps'
//     files, the map boxed by directory, and no Risk, Validation or Overlaps
//     card rather than empty ones;
//   * picking a file in the map, or in the list under it, opens its one-line
//     what with what calls it and what tests it;
//   * a high-risk plan names its invariants and the frozen contract in the
//     header's band and in its Risk card;
//   * Approve, Edit and Reject still call today's routes: the approval's
//     approve with the digest the page read, its reject with the reason, and
//     Edit opens the run's page.
//
// The first plan is a copy of the run owner linked (one step, five files, three
// overlaps); the high-risk one is the mock-up's invented plan. Placeholders
// only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const API = 'apps/swarm-api/swarm_api'
const CP = 'tests/unit/control_plane'
const RUN = 'run_9b8af0113faa4165ab8e'
const ITEM = `run:${RUN}`

function item(over: Json = {}): Json {
  return {
    approval_id: ITEM,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: RUN, issue: 'example-org/example-api#3507' },
    digest: 'plan-digest-1',
    summary: 'example-org/example-api#3507: a run lane’s plan names no overlap with work already in the merge train',
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-1),
    expires_at: hoursFromNow(47),
    ...over,
  }
}

const STEP = {
  step_id: 'overlap-ref-for-run-lanes',
  title: 'Name the merge train’s overlapping PRs on each run lane’s plan',
  prompt: `Add overlap_refs(ctx, tenant_id, files) to ${API}/plancontext.py and serve plan.overlaps from GET /v1/runs/{run_id}.`,
  files: [`${API}/plancontext.py`, `${API}/issueruns.py`, `${API}/routes/runs.py`, `${CP}/test_plancontext_overlap.py`, `${CP}/test_runs_overlap_ref.py`],
  estimate: '2.5 h',
}

/** The plan as stored today: a summary and its steps, no sections. */
const LEGACY = {
  summary: 'Name, on each run plan, the merge train’s pull requests that change the same files.',
  steps: [STEP],
  requirements: ['A run’s plan lists each train PR that shares a file with a step'],
}

/** The run owner linked, with the planner's sections. */
const SECTIONS = {
  ...LEGACY,
  estimate: '2.5 h',
  purpose: 'Show, on every run plan, which pull requests already in the merge train change the same files.',
  why: 'Plans are approved blind to the merge train: one run clashed with a train PR and sat 41 minutes on a merge hold.',
  changes: [
    { file: `${API}/plancontext.py`, area: 'Planner context', kind: 'modify', what: 'Adds overlap_refs(): train PRs that share the step’s files' },
    { file: `${API}/issueruns.py`, area: 'Issue runs', kind: 'modify', what: 'PlanOverlap gains pr, position and shared files' },
    { file: `${API}/routes/runs.py`, area: 'API routes', kind: 'modify', what: 'GET /v1/runs/{id} serves plan.overlaps, [] when none' },
    { file: `${CP}/test_plancontext_overlap.py`, area: 'Tests', kind: 'test', what: 'Red first: overlap_refs() keeps only PRs that share a file' },
    { file: `${CP}/test_runs_overlap_ref.py`, area: 'Tests', kind: 'test', what: 'Red first: the route serves the overlaps, [] when none' },
  ],
  risk: {
    level: 'low',
    factors: ['Adds one field to a served plan; nothing reads it to decide', 'A new read of the merge train while planning'],
    blast_radius: 'Issue-run planning and GET /v1/runs/{id}. No lease, capacity or dispatch path.',
    invariants: [],
    mitigations: ['The train read has a 2 s budget and falls back to no overlaps with a reason'],
    rollback: 'Revert the pull request. Stored plans keep an ignored overlaps field.',
  },
  validation: {
    tests: [
      { file: `${CP}/test_plancontext_overlap.py`, proves: 'Only train PRs sharing a file are kept, with position and files' },
      { file: `${CP}/test_plancontext_overlap.py`, proves: 'A train read that times out plans on, with no overlaps and a reason' },
      { file: `${CP}/test_runs_overlap_ref.py`, proves: 'GET /v1/runs/{id} serves plan.overlaps, and [] when none' },
    ],
    qa: [{ check: 'An issue run whose files overlap a train PR lists that PR on its approval', where: 'staging · /automate/approvals', when: 'after deploy' }],
  },
  graph: {
    nodes: [
      { id: 'c1', label: 'in_flight_section()', kind: 'changed', file: `${API}/plancontext.py` },
      { id: 'c2', label: 'PlanOverlap', kind: 'changed', file: `${API}/issueruns.py` },
      { id: 'c3', label: 'GET /v1/runs/{run_id}', kind: 'changed', file: `${API}/routes/runs.py` },
      { id: 'k1', label: 'plan_issue()', kind: 'caller', file: `${API}/planner.py` },
      { id: 't1', label: '3 tests · new', kind: 'test', file: `${CP}/test_plancontext_overlap.py` },
      { id: 't2', label: '2 tests · new', kind: 'test', file: `${CP}/test_runs_overlap_ref.py` },
    ],
    edges: [
      { from: 'k1', to: 'c1', kind: 'calls' },
      { from: 'c1', to: 'c2', kind: 'imports' },
      { from: 'c3', to: 'c2', kind: 'imports' },
      { from: 't1', to: 'c1', kind: 'tests' },
      { from: 't2', to: 'c3', kind: 'tests' },
    ],
  },
  overlaps: [
    { ref: 'example-org/example-api#3489', kind: 'pull_request', action: 'none', note: 'Planner context carries recent merges by territory' },
    { ref: 'example-org/example-api#3496', kind: 'pull_request', action: 'required', note: 'Runs route serves lane progress: same function edited' },
    { ref: 'example-org/example-api#3501', kind: 'pull_request', action: 'none', note: 'PlanSpec refuses a plan with no steps' },
  ],
}

/** The mock-up's invented high-risk plan: admission across two pools, and the frozen contract. */
const HIGH = {
  summary: 'Reserve capacity across two GPU pools in one transaction.',
  purpose: 'Reserve capacity across two GPU pools in one transaction when a runner profile names both.',
  why: 'A two-pool profile reserved its first pool and then waited on the second, holding capacity no one could use.',
  estimate: '6 h',
  steps: [{ step_id: 'multipool', title: 'Reserve every pool in one transaction', prompt: 'Make admission.reserve() take every pool in one transaction.' }],
  changes: [
    { file: 'apps/control-plane/control_plane/admission.py', area: 'Admission', kind: 'modify', what: 'reserve() takes every pool in one transaction' },
    { file: 'apps/common/swarm_common/contract.py', area: 'Contract', kind: 'modify', what: 'RunnerProfile.pools becomes a list' },
    { file: 'tests/unit/control_plane/test_admission_multipool.py', area: 'Tests', kind: 'test', what: 'Red first: a two-pool reserve is all or nothing' },
  ],
  risk: {
    level: 'high',
    factors: ['Changes how every task is admitted'],
    blast_radius: 'Every admission in every tenant.',
    invariants: ['2: Capacity is reserved all-or-nothing, in one Firestore transaction', 'Invariant 3: Concurrency counts from LEASED, not RUNNING'],
    mitigations: ['Behind a flag, off by default'],
    rollback: 'Turn the flag off.',
  },
}

function routes(approvalPlan: Json, runPlan: Json | null, over: Json = {}, extra: (m: string, url: string, body: unknown) => Reply = () => null) {
  return (m: string, url: string, body: unknown): Reply => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [item(over)], tenant_id: 'eng' } }
    if (m === 'GET' && path === `/v1/approvals/${encodeURIComponent(ITEM)}`) return { status: 200, body: { approval: item(over), run: { id: RUN, state: 'PLANNED', plan: approvalPlan } } }
    if (m === 'GET' && path === `/v1/runs/${RUN}` && runPlan !== null) return { status: 200, body: { run: { id: RUN, state: 'awaiting_approval', plan: runPlan, planner_documents: null } } }
    return null
  }
}

async function mount(handler: (m: string, url: string, body: unknown) => Reply) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={`approval=${encodeURIComponent(ITEM)}`} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const head = () => screen.findByTestId('ab-head', undefined, WAIT)
const facts = (h: HTMLElement) => [...h.querySelectorAll('[data-fact]')].map((li) => li.getAttribute('data-fact'))
const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) => calls.filter((c) => c.method === method && c.url.split('?')[0] === path)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the header card', () => {
  it('draws the purpose, why and the four facts from the run’s planner sections', async () => {
    // The approval serves the stored plan; the run's own read carries the sections.
    await mount(routes(LEGACY, SECTIONS))
    const h = await head()
    // MUTATION: read only the approval's plan, and the purpose is the summary.
    await waitFor(() => expect(visible(within(h).getByTestId('ab-purpose'))).toBe(SECTIONS.purpose), WAIT)
    expect(visible(within(h).getByTestId('ab-why'))).toContain('Plans are approved blind to the merge train')
    expect(facts(h)).toEqual(['size', 'estimate', 'risk', 'overlaps'])
    const fact = (k: string) => visible(h.querySelector(`[data-fact="${k}"]`))
    expect(fact('size')).toContain('Small')
    expect(fact('size')).toContain('1 step · 5 files')
    expect(fact('estimate')).toContain('2.5 h')
    expect(fact('risk')).toContain('Low risk')
    expect(fact('overlaps')).toContain('3')
    expect(h.getAttribute('data-risk')).toBe('low')
    // A low-risk plan draws no Touches band.
    expect(within(h).queryByTestId('ab-touches')).toBeNull()
    // The Risk and Validation cards follow, with the tests grouped by file and the QA check.
    expect(screen.getByRole('region', { name: 'Risk' })).toBeTruthy()
    const val = screen.getByRole('region', { name: 'Validation' })
    expect(within(val).getByRole('list', { name: 'Red-first tests' }).children).toHaveLength(2)
    expect(visible(within(val).getByRole('list', { name: 'QA checks' }))).toContain('staging · /automate/approvals · after deploy')
    expect(screen.getByRole('region', { name: 'Overlaps' })).toBeTruthy()
  })
})

describe('a plan stored before the sections', () => {
  it('draws its summary and its steps’ files, the map by directory, and no empty card', async () => {
    // The run's read fails (404): the approval's plan stands.
    await mount(routes(LEGACY, null))
    const h = await head()
    await waitFor(() => expect(visible(within(h).getByTestId('ab-purpose'))).toBe(LEGACY.summary), WAIT)
    expect(within(h).queryByTestId('ab-why')).toBeNull()
    // MUTATION: draw a Risk tile reading "unknown" for a plan that served none.
    expect(facts(h)).toEqual(['size'])
    const list = screen.getByRole('list', { name: 'Files the plan changes' })
    const rows = [...list.children].map((li) => visible(li))
    expect(rows).toHaveLength(5)
    // A path under tests/ is a test; the rest are changes.
    expect(rows.filter((r) => r.startsWith('test'))).toHaveLength(2)
    expect(rows.filter((r) => r.startsWith('modify'))).toHaveLength(3)
    // The map boxes the files by directory.
    const areas = [...document.querySelectorAll('.ab-area text')].map((t) => t.textContent)
    expect(areas).toEqual([API, `${API}/routes`])
    // MUTATION: draw the Risk, Validation and Overlaps cards empty.
    for (const name of ['Risk', 'Validation', 'Overlaps']) expect(screen.queryByRole('region', { name })).toBeNull()
    expect(screen.getByRole('region', { name: 'Where it changes' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Steps' })).toBeTruthy()
  })
})

describe('the change map', () => {
  it('opens a picked file’s what, what calls it and what tests it', async () => {
    await mount(routes(LEGACY, SECTIONS))
    await head()
    const node = await screen.findByRole('button', { name: /^plancontext\.py, changed:/ }, WAIT)
    expect(screen.queryByTestId('ab-pick')).toBeNull()
    fireEvent.click(node)
    const pick = screen.getByTestId('ab-pick')
    // MUTATION: open the strip without the file's what.
    expect(visible(pick)).toContain('Adds overlap_refs(): train PRs that share the step’s files')
    expect(visible(pick)).toContain('Called by')
    expect(visible(pick)).toContain('planner.py · plan_issue()')
    expect(visible(pick)).toContain('Tested by')
    expect(visible(pick)).toContain('test_plancontext_overlap.py')
    expect(node.getAttribute('aria-pressed')).toBe('true')
    // The list under the map picks the same file, and picking it again clears it.
    const row = within(screen.getByRole('list', { name: 'Files the plan changes' })).getByRole('button', { name: /plancontext\.py$/ })
    expect(row.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(row)
    expect(screen.queryByTestId('ab-pick')).toBeNull()
  })

  it('draws two edges between the same pair without a duplicate key', async () => {
    const twice = { ...SECTIONS, graph: { ...SECTIONS.graph, edges: [...SECTIONS.graph.edges, { from: 'k1', to: 'c1', kind: 'imports' }] } }
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mount(routes(LEGACY, twice))
    await head()
    await screen.findByRole('button', { name: /^plancontext\.py, changed:/ }, WAIT)
    // MUTATION: key an edge by its ends alone, and React warns of two children with one key.
    expect(err.mock.calls.map((c) => String(c[0])).filter((m) => m.includes('same key'))).toEqual([])
    err.mockRestore()
  })
})

describe('a high-risk plan', () => {
  it('names its invariants and the frozen contract in the header’s band and its Risk card', async () => {
    await mount(routes(HIGH, null))
    const h = await head()
    expect(h.getAttribute('data-risk')).toBe('high')
    const band = await within(h).findByTestId('ab-touches', undefined, WAIT)
    // MUTATION: draw the band only for a frozen file, and the invariants are read nowhere in the header.
    expect([...band.querySelectorAll('.ab-inv')].map((x) => visible(x))).toEqual(['Invariant 2', 'Invariant 3', 'Frozen contract · contract.py'])
    expect(band.querySelector('.ab-inv.is-hot')!.getAttribute('title')).toBe('Capacity is reserved all-or-nothing, in one Firestore transaction')
    const risk = screen.getByRole('region', { name: 'Risk' })
    expect(risk.className).toContain('is-high')
    const inv = within(risk).getByRole('list', { name: 'Invariants touched' })
    expect(visible(inv)).toContain('Invariant 3 Concurrency counts from LEASED, not RUNNING')
    expect(visible(inv)).toContain('apps/common/swarm_common/contract.py')
    // The frozen file sits in the map's frozen-contract box, whatever area the planner gave it.
    expect([...document.querySelectorAll('.ab-area.is-frozen text')].map((t) => t.textContent)).toEqual(['Frozen contract'])
  })
})

describe('the decision', () => {
  it('approves with the digest the page read, through the approval’s route', async () => {
    const { calls } = await mount(routes(LEGACY, SECTIONS, {}, (m, url) => (m === 'POST' && url.split('?')[0] === `/v1/approvals/${encodeURIComponent(ITEM)}:approve` ? { status: 200, body: { approval: item({ state: 'approved' }) } } : null)))
    const h = await head()
    fireEvent.click(within(h).getByRole('button', { name: 'Approve' }))
    // MUTATION: approve through the run's own route, or without the digest.
    await waitFor(() => expect(sent(calls, 'POST', `/v1/approvals/${encodeURIComponent(ITEM)}:approve`)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', `/v1/approvals/${encodeURIComponent(ITEM)}:approve`)[0]!.body).toEqual({ digest: 'plan-digest-1' })
  })

  it('rejects with the reason typed once Reject is pressed, through the approval’s route', async () => {
    const { calls } = await mount(routes(LEGACY, SECTIONS, {}, (m, url) => (m === 'POST' && url.split('?')[0] === `/v1/approvals/${encodeURIComponent(ITEM)}:reject` ? { status: 200, body: { approval: item({ state: 'rejected' }) } } : null)))
    const h = await head()
    fireEvent.click(within(h).getByRole('button', { name: 'Reject' }))
    fireEvent.change(within(h).getByRole('textbox', { name: /Reason/ }), { target: { value: 'the plan reads the wrong train' } })
    fireEvent.click(within(h).getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(sent(calls, 'POST', `/v1/approvals/${encodeURIComponent(ITEM)}:reject`)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', `/v1/approvals/${encodeURIComponent(ITEM)}:reject`)[0]!.body).toEqual({ reason: 'the plan reads the wrong train' })
  })

  it('opens the run’s page on Edit, where its plan editor is', async () => {
    const { go } = await mount(routes(LEGACY, SECTIONS))
    const h = await head()
    const edit = within(h).getByRole('link', { name: 'Edit' })
    expect(edit.getAttribute('href')).toBe(`/runs/${RUN}`)
    fireEvent.click(edit, { button: 0 })
    // MUTATION: make Edit a button that does nothing.
    expect(go).toHaveBeenCalledWith(`work/runs?run=${RUN}`)
  })
})

describe('the planner’s design doc', () => {
  it('is drawn folded, in an empty-sandbox frame, from the run’s planner_documents', async () => {
    const docs = { task_id: 'task_planner1', design_docs: [{ name: 'design.md', bytes: 40, content_type: 'text/markdown', read_url: '/v1/tasks/task_planner1/artifacts/raw?name=design.md' }], diagrams: [] }
    await mount((m, url) => {
      const path = url.split('?')[0]!
      if (m === 'GET' && path === `/v1/runs/${RUN}`) return { status: 200, body: { run: { id: RUN, state: 'awaiting_approval', plan: SECTIONS, planner_documents: docs } } }
      if (m === 'GET' && path === '/v1/tasks/task_planner1/artifacts/content')
        return { status: 200, body: { task_id: 'task_planner1', tenant_id: 'eng', attempt_id: 'a1', artifact: { name: 'design.md', bytes: 40, uri: null }, status: 'ok', detail: null, key: null, uri: null, content: '# Overlap refs\n\nRead the train.', truncated: false } }
      return routes(LEGACY, SECTIONS)(m, url, null)
    })
    await head()
    const fold = await screen.findByTestId('ab-design', undefined, WAIT)
    // MUTATION: draw the doc open on the surface, or in a frame that may run script.
    expect(fold.tagName).toBe('DETAILS')
    expect(fold.hasAttribute('open')).toBe(false)
    const frame = within(fold).getByTitle('Design doc: design.md')
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toContain('Overlap refs')
  })
})

describe('the overlaps', () => {
  it('compare an overlapping pull request’s shared files, read off the run’s open work', async () => {
    const openWork = { repository: 'example-org/example-api', read_at: hoursFromNow(-1), issues: [], issues_truncated: false, pull_requests: [{ number: 3496, title: 'Runs route serves lane progress', files: [`${API}/routes/runs.py`, 'docs/runs.md'], files_truncated: false }], pull_requests_truncated: false }
    await mount((m, url) => {
      if (m === 'GET' && url.split('?')[0] === `/v1/runs/${RUN}`) return { status: 200, body: { run: { id: RUN, state: 'awaiting_approval', plan: SECTIONS, planner_documents: null, open_work: openWork } } }
      return routes(LEGACY, SECTIONS)(m, url, null)
    })
    await head()
    const over = screen.getByRole('region', { name: 'Overlaps' })
    // Only the pull request whose files the run read can be compared.
    // MUTATION: offer Compare on every row, and two of them open nothing.
    const compare = within(over).getAllByRole('button', { name: /^Show the files shared with/ })
    expect(compare.map((b) => b.getAttribute('aria-label'))).toEqual(['Show the files shared with example-org/example-api#3496'])
    // The ref column draws #N, the whole ref in its title and name, so a long owner/repo never wraps over the note.
    // MUTATION: draw the whole owner/repo#N as the link's text.
    const link = within(over).getByRole('link', { name: 'example-org/example-api#3496' })
    expect(link.textContent).toBe('#3496')
    expect(link.getAttribute('title')).toBe('example-org/example-api#3496')
    fireEvent.click(compare[0]!)
    const d = within(over).getByTestId('ab-over-d')
    expect(visible(d)).toContain(`${API}/routes/runs.py`)
    expect(visible(d)).toContain('GET /v1/runs/{id} serves plan.overlaps, [] when none')
    expect(visible(d)).not.toContain('docs/runs.md')
  })

  it('compare what the other pull request does to each shared file, from the theirs the overlap serves', async () => {
    const plan = {
      ...SECTIONS,
      overlaps: [
        { ref: 'example-org/example-api#3489', kind: 'pull_request', action: 'none', note: 'Planner context carries recent merges by territory', theirs: [{ file: `${API}/plancontext.py`, what: 'Adds recent_merges(): the train’s merges by territory' }, { file: 'docs/train.md', what: 'Documents the train' }] },
        SECTIONS.overlaps[1]!,
      ],
    }
    await mount(routes(LEGACY, plan))
    await head()
    const over = screen.getByRole('region', { name: 'Overlaps' })
    // A served theirs offers Compare with no open work read; a file this plan does not change is not shared.
    // MUTATION: compare only the files the run's open work lists, and #3489 offers nothing.
    const compare = within(over).getAllByRole('button', { name: /^Show the files shared with/ })
    expect(compare.map((b) => b.getAttribute('aria-label'))).toEqual(['Show the files shared with example-org/example-api#3489'])
    fireEvent.click(compare[0]!)
    const d = within(over).getByTestId('ab-over-d')
    // MUTATION: draw only this plan's side, and theirs is lost.
    expect(visible(d)).toContain('Theirs')
    expect(visible(d)).toContain('Adds recent_merges(): the train’s merges by territory')
    expect(visible(d)).toContain('Adds overlap_refs(): train PRs that share the step’s files')
    expect(visible(d)).not.toContain('docs/train.md')
  })

  it('folds the plan’s requirements under Validation as Done when', async () => {
    await mount(routes(LEGACY, SECTIONS))
    await head()
    const fold = within(screen.getByRole('region', { name: 'Validation' })).getByText(/^Done when/).closest('details')!
    expect(fold.hasAttribute('open')).toBe(false)
    expect(fold.textContent).toContain('A run’s plan lists each train PR that shares a file with a step')
  })
})

describe('the validation', () => {
  it('ties each red-first test to the ids of the requirements it proves', async () => {
    const plan = {
      ...SECTIONS,
      requirements: ['A run’s plan lists each train PR that shares a file with a step', 'GET /v1/runs/{id} serves the overlaps'],
      validation: {
        ...SECTIONS.validation,
        tests: [
          { file: `${CP}/test_plancontext_overlap.py`, proves: 'Only train PRs sharing a file are kept, with position and files', requirements: ['R1'] },
          { file: `${CP}/test_plancontext_overlap.py`, proves: 'A train read that times out plans on, with no overlaps and a reason', requirements: [1] },
          { file: `${CP}/test_runs_overlap_ref.py`, proves: 'GET /v1/runs/{id} serves plan.overlaps, and [] when none', requirements: ['2', 'R7'] },
        ],
      },
    }
    await mount(routes(LEGACY, plan))
    await head()
    const val = screen.getByRole('region', { name: 'Validation' })
    // R1 is named twice on T1's file and drawn once; R7 names no requirement and is dropped.
    // MUTATION: draw the tests without their requirement ids.
    expect(within(val).getByLabelText('T1 proves R1').textContent).toBe('R1')
    expect(within(val).getByLabelText('T2 proves R2').textContent).toBe('R2')
    expect(within(val).getByLabelText('T2 proves R2').firstElementChild!.getAttribute('title')).toBe('GET /v1/runs/{id} serves the overlaps')
    const fold = within(val).getByText(/^Done when/).closest('details')!
    expect(fold.textContent).toContain('proved by T1')
    expect(fold.textContent).toContain('proved by T2')
  })
})
