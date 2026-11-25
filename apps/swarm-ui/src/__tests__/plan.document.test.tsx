// THE PLAN AS A DOCUMENT ON ITS DECISION PAGE (docs/design/plans-pipeline.md
// §2.3, lane U3 DECISION-DOCUMENT). Red first on main, where PlanDocument.tsx
// does not exist.
//
// WHAT EACH CASE HOLDS:
//   * the document's parts: the summary, the requirements folded past the
//     first, each step with its files, tests, estimate and done-when
//     statements, the planner's reasons in the margin, each step's risk rows
//     (`plan_risk`, §3.7) and the expected cost (§3.8);
//   * the bar names the revision and the digest, and Approve sends exactly
//     that digest;
//   * a member who may not decide sees no Approve, Edit or Reject, and the
//     deciders;
//   * Approve refused with 409 `plan_changed` offers to read the new
//     revision, and reading it reads the item again;
//   * a poll with a new digest keeps the shown revision and removes Approve;
//   * a run read without `plan_revisions` (an older server) draws the plan
//     and today's buttons;
//   * plan text is data: markup in it is drawn as characters;
//   * the run read's states (loading, empty, stale, error) are kept apart;
//   * Back returns to Approvals, the board not being on main.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { PlanDocument } from '../PlanDocument'
import type { ApiError } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const RUN = 'run_0d0c0a0b0e0f41a2b3c4'
const ITEM = `run:${RUN}`
const D2 = 'sha256:' + 'ab12'.repeat(16)
const D3 = 'sha256:' + 'cd34'.repeat(16)

const PLAN = {
  summary: 'Serve each run lane’s overlaps on its plan read.',
  rationale: 'Plans are approved blind to the merge train.',
  estimate: '3 h',
  requirements: ['A run’s plan lists each train PR that shares a file', 'The read answers [] when none', 'Old plans keep their digest'],
  steps: [
    {
      step_id: 'overlaps-read',
      title: 'Serve plan.overlaps from the run read',
      prompt: 'Add overlap_refs() to plancontext.py and serve it from GET /v1/runs/{run_id}.',
      files: ['apps/swarm-api/swarm_api/plancontext.py', 'apps/swarm-api/swarm_api/routes/runs.py'],
      tests: ['tests/unit/control_plane/test_runs_overlap_ref.py'],
      estimate: '2 h',
      done_when: ['The run read serves overlaps', 'A run with none serves []'],
      requirements: [1, 2],
      rationale: 'The read is where the page already looks.',
    },
    {
      step_id: 'iam-binding',
      title: 'Grant the planner read on the train',
      prompt: 'Bind the planner’s account to the train bucket.',
      files: ['terraform/infra/iam.tf'],
      estimate: '1 h',
    },
  ],
}

function item(over: Json = {}): Json {
  return {
    approval_id: ITEM,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: RUN, issue: 'example-org/example-api#12' },
    digest: D2,
    summary: 'example-org/example-api#12: serve overlaps on the plan read',
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-1),
    expires_at: hoursFromNow(47),
    may_decide: { approve: true, reject: true, why: null, deciders: ['members'] },
    ...over,
  }
}

function runRead(over: Json = {}): Json {
  return {
    id: RUN,
    state: 'PLANNED',
    plan: PLAN,
    plan_digest: D2,
    plan_revision: 2,
    plan_revisions: [
      { revision: 2, digest: D2, replaces: 'sha256:' + '0f'.repeat(32), author: { kind: 'person', by: 'a@example.com' }, made_by: 'edit', changed: [{ path: 'steps.1.prompt', op: 'replace' }], at: hoursFromNow(-1) },
      { revision: 1, digest: 'sha256:' + '0f'.repeat(32), replaces: null, author: { kind: 'planner', by: 'planner' }, made_by: 'plan', changed: [], at: hoursFromNow(-2) },
    ],
    plan_history_complete: true,
    plan_risk: { digest: D2, steps: { 'iam-binding': [{ stop: 'iam', matched: ['terraform/infra/iam.tf'] }] }, run_hold: null },
    expected_cost_usd: { low: 3.1, high: 7.8, basis: '2 steps at their estimates', prices_at: hoursFromNow(0) },
    planner_documents: null,
    ...over,
  }
}

function routes(o: { item?: Json; run?: Json | null; extra?: (m: string, url: string, body: unknown) => Reply } = {}) {
  return (m: string, url: string, body: unknown): Reply => {
    const hit = o.extra?.(m, url, body) ?? null
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    const it = item(o.item ?? {})
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [it], tenant_id: 'eng' } }
    if (m === 'GET' && path === `/v1/approvals/${encodeURIComponent(ITEM)}`) return { status: 200, body: { approval: it, run: { id: RUN, state: 'PLANNED', plan: PLAN } } }
    if (m === 'GET' && path === `/v1/runs/${RUN}` && o.run !== null) return { status: 200, body: { run: runRead(o.run ?? {}) } }
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

const doc = () => screen.findByTestId('pd-doc', undefined, WAIT)
const bar = () => screen.findByTestId('pd-bar', undefined, WAIT)
const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) => calls.filter((c) => c.method === method && c.url.split('?')[0] === path)
const APPROVE = `/v1/approvals/${encodeURIComponent(ITEM)}:approve`

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the document', () => {
  it('draws the summary, requirements, steps with files, tests, estimate and done-when, the margin’s reasons, risk rows and the expected cost', async () => {
    await mount(routes())
    const d = await doc()
    expect(visible(within(d).getByTestId('pd-summary'))).toBe(PLAN.summary)
    // The requirements: the first open, the rest folded.
    expect(visible(within(d).getByTestId('pd-reqs'))).toBe(PLAN.requirements[0])
    const fold = within(d).getByRole('list', { name: 'More requirements' }).closest('details')!
    expect(fold.open).toBe(false)
    expect(fold.querySelectorAll('li')).toHaveLength(2)
    // Step 1: files, tests, estimate, done-when and its reason in the margin.
    const s1 = within(d).getByRole('region', { name: 'Step 1' })
    expect(visible(s1)).toContain('Serve plan.overlaps from the run read')
    expect(visible(s1)).toContain('2 h')
    expect([...within(s1).getByRole('list', { name: 'Files' }).children].map(visible)).toEqual(PLAN.steps[0]!.files)
    expect([...within(s1).getByRole('list', { name: 'Tests' }).children].map(visible)).toEqual(['tests/unit/control_plane/test_runs_overlap_ref.py'])
    expect([...within(s1).getByRole('list', { name: 'Done when' }).children].map(visible)).toEqual(['The run read serves overlaps', 'A run with none serves []'])
    expect(visible(within(s1).getByRole('complementary'))).toBe('The read is where the page already looks.')
    expect(within(s1).queryByRole('list', { name: 'Risk' })).toBeNull()
    // Step 2: its risk row from plan_risk, with the matched path.
    const s2 = within(d).getByRole('region', { name: 'Step 2' })
    const risk = within(s2).getByRole('list', { name: 'Risk' })
    expect(visible(risk)).toContain('IAM')
    expect(visible(risk)).toContain('terraform/infra/iam.tf')
    // The plan's reason sits beside the summary.
    expect(visible(within(d).getByRole('region', { name: 'Summary' }))).toContain(PLAN.rationale)
    // The expected cost, as served.
    expect(visible(within(d).getByTestId('pd-cost'))).toContain('$3.10–$7.80')
  })

  it('draws a dash for a cost the platform could not compute, never $0', async () => {
    await mount(routes({ run: { expected_cost_usd: { low: null, high: null, why_none: 'step iam-binding has no estimate' } } }))
    const d = await doc()
    const cost = within(d).getByTestId('pd-cost')
    expect(visible(cost)).not.toContain('$')
  })

  it('draws plan text as text, never as HTML', async () => {
    const markup = '<img src=x onerror="alert(1)"><b>bold</b>'
    await mount(routes({ run: { plan: { ...PLAN, summary: markup } } }))
    const d = await doc()
    expect(visible(within(d).getByTestId('pd-summary'))).toBe(markup)
    expect(d.querySelector('img')).toBeNull()
    expect(within(d).getByTestId('pd-summary').querySelector('b')).toBeNull()
  })
})

describe('the bar', () => {
  it('names the revision and the digest, and Approve sends exactly that digest', async () => {
    const { calls } = await mount(routes({ extra: (m, url) => (m === 'POST' && url.split('?')[0] === APPROVE ? { status: 200, body: { approval: item({ state: 'approved' }) } } : null) }))
    const b = await bar()
    expect(visible(within(b).getByTestId('pd-revision'))).toBe('Revision 2')
    const digest = within(b).getByTestId('pd-digest')
    expect(digest.getAttribute('data-digest')).toBe(D2)
    expect(visible(digest)).toBe(D2.slice(7, 19))
    fireEvent.click(within(b).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(sent(calls, 'POST', APPROVE)).toHaveLength(1), WAIT)
    expect((sent(calls, 'POST', APPROVE)[0]!.body as Json).digest).toBe(digest.getAttribute('data-digest'))
  })

  it('shows a member who may not decide no Approve, Edit or Reject, and who decides', async () => {
    await mount(routes({ item: { may_decide: { approve: false, reject: false, why: 'Only an owner decides a held plan.', deciders: ['owners'] } } }))
    const b = await bar()
    for (const name of ['Approve', 'Reject']) expect(within(b).queryByRole('button', { name })).toBeNull()
    expect(within(b).queryByText('Edit')).toBeNull()
    expect(visible(within(b).getByTestId('ap-no-decide'))).toContain('only')
  })

  it('offers to read the new revision when Approve is refused because the plan changed, and reads it', async () => {
    const { calls } = await mount(
      routes({ extra: (m, url) => (m === 'POST' && url.split('?')[0] === APPROVE ? { status: 409, body: { code: 'plan_changed', message: 'the plan is now ' + D3 } } : null) }),
    )
    const b = await bar()
    fireEvent.click(within(b).getByRole('button', { name: 'Approve' }))
    const alert = await within(b).findByRole('alert', undefined, WAIT)
    expect(alert.getAttribute('data-code')).toBe('plan_changed')
    const reads = sent(calls, 'GET', `/v1/approvals/${encodeURIComponent(ITEM)}`).length
    fireEvent.click(within(alert).getByRole('button', { name: 'Read the new revision' }))
    await waitFor(() => expect(sent(calls, 'GET', `/v1/approvals/${encodeURIComponent(ITEM)}`).length).toBeGreaterThan(reads), WAIT)
  })

  it('keeps the shown revision and removes its Approve when a poll brings a new digest', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let current: Json = {}
    await mount((m, url, body) => routes({ run: current })(m, url, body))
    const b = await bar()
    expect(within(b).getByRole('button', { name: 'Approve' })).toBeTruthy()
    current = {
      plan: { ...PLAN, summary: 'A different plan' },
      plan_digest: D3,
      plan_revision: 3,
      plan_revisions: [{ revision: 3, digest: D3, replaces: D2, author: { kind: 'planner', by: 'planner' }, made_by: 'revise', changed: [{ path: 'summary', op: 'replace' }], at: hoursFromNow(0) }, ...(runRead().plan_revisions as Json[])],
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(21_000)
    })
    const after = await screen.findByTestId('pd-bar', undefined, WAIT)
    await waitFor(() => expect(after.getAttribute('data-superseded')).toBe('true'), WAIT)
    expect(within(after).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(visible(within(after).getByTestId('pd-revision'))).toBe('Revision 2')
    expect(visible(within(after).getByTestId('pd-arrived'))).toContain('Revision 3')
    expect(within(after).getByRole('button', { name: 'Read revision 3' })).toBeTruthy()
    // The plan on screen is still revision 2's.
    expect(visible(screen.getByTestId('pd-summary'))).toBe(PLAN.summary)
  })

  it('draws the plan and today’s buttons for a run read without plan_revisions', async () => {
    await mount(routes({ run: { plan_revisions: undefined, plan_history_complete: undefined, plan_risk: undefined, expected_cost_usd: undefined } }))
    const b = await bar()
    expect(within(b).queryByTestId('pd-revision')).toBeNull()
    expect(within(b).getByTestId('pd-digest').getAttribute('data-digest')).toBe(D2)
    expect(within(b).getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(within(b).getByRole('button', { name: 'Reject' })).toBeTruthy()
    expect(within(b).getByText('Edit')).toBeTruthy()
    const d = await doc()
    expect(visible(within(d).getByTestId('pd-summary'))).toBe(PLAN.summary)
    expect(within(d).queryByTestId('pd-cost')).toBeNull()
  })
})

describe('the run read’s states', () => {
  const error: ApiError = { kind: 'server', status: 500, code: null, message: 'boom' } as unknown as ApiError

  it('keeps the approval’s plan and says the run was not read when the read fails', async () => {
    await mount(routes({ run: null }))
    const d = await doc()
    expect(d.getAttribute('data-state')).toBe('error')
    expect(within(d).getByRole('alert')).toBeTruthy()
    expect(within(d).getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(visible(within(d).getByTestId('pd-summary'))).toBe(PLAN.summary)
  })

  it('says stale, loading and empty apart', () => {
    const onRead = vi.fn()
    const { rerender } = render(<PlanDocument plan={PLAN} run={runRead()} read={{ status: 'stale', data: {}, fetchedAt: Date.now() - 120_000, error }} digest={D2} onRead={onRead} />)
    expect(screen.getByTestId('pd-state').getAttribute('data-state')).toBe('stale')
    expect(screen.queryByRole('alert')).toBeNull()
    rerender(<PlanDocument plan={PLAN} run={null} read={{ status: 'loading', since: Date.now() }} digest={D2} onRead={onRead} />)
    expect(screen.getByTestId('pd-state').getAttribute('data-state')).toBe('loading')
    rerender(<PlanDocument plan={null} run={null} read={{ status: 'empty', fetchedAt: Date.now() }} digest={D2} onRead={onRead} />)
    expect(screen.getByTestId('pd-doc').getAttribute('data-state')).toBe('empty')
    expect(screen.getByTestId('pd-empty')).toBeTruthy()
    rerender(<PlanDocument plan={PLAN} run={null} read={{ status: 'error', error }} digest={D2} onRead={onRead} />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRead).toHaveBeenCalledTimes(1)
  })
})

describe('at 390 pixels', () => {
  const PHONE: CascadeEnv = { width: 390 }
  it('folds each part’s margin under it, one column, and wraps long paths', async () => {
    await mount(routes())
    const d = await doc()
    const part = within(d).getByRole('region', { name: 'Step 1' })
    // MUTATION: keep the margin's column at 390, and the page scrolls sideways.
    expect(painted(part, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(part, 'grid-template-columns', { width: 1440 })).toBe('minmax(0, 1fr) minmax(0, 18rem)')
    expect(painted(part.querySelector<HTMLElement>('.pd-list li')!, 'overflow-wrap', PHONE)).toBe('anywhere')
    expect(painted(screen.getByTestId('pd-bar'), 'flex-wrap', PHONE)).toBe('wrap')
  })
})

describe('Back', () => {
  it('returns to Approvals while the board is not served', async () => {
    const { go } = await mount(routes())
    await doc()
    const back = screen.getByText(/‹ Approvals/)
    fireEvent.click(back)
    expect(go).toHaveBeenCalledWith('automate/approvals')
  })
})
