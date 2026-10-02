// WORK › RUNS (intake-tenants.html 1A): /runs, the tenant's issue runs newest
// first, and /runs/<id>, one run with its plan, against the live shapes of
// #511 (issueruns.IssueRun.to_api, routes/runs.py).
//
// WHAT EACH CASE HOLDS:
//   * the list is drawn in the order the API served it (newest first is the
//     server's ordering, never re-sorted here), with each state as the API
//     names it, and an empty tenant is an empty state, not a blank table;
//   * Approve sends THE DIGEST OF THE PLAN SHOWN (owner decision D3), and a
//     409 `plan_changed` re-reads the run and says the plan changed;
//   * Reject sends the shown digest and the reason;
//   * what the run document does not serve -- the PR, the plan and status
//     comments, overlaps, cost -- is a dash with its reason, never hidden and
//     never invented; a workflow not yet created is "none yet".

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Digests built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST_A = 'sha256:' + 'a1'.repeat(32)
const DIGEST_B = 'sha256:' + 'b2'.repeat(32)

function run(over: Record<string, unknown> = {}) {
  return {
    id: 'run_4c1e09d2', tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: 'example-org/infra#512', owner: 'example-org', repo: 'infra', number: 512,
      url: 'https://github.com/example-org/infra/issues/512', repository_url: 'https://github.com/example-org/infra' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner1',
    plan: { summary: 'Sum step spend into the workflow read, then draw a cost column.', steps: [
      { step_id: 'api', title: 'api: sum step spend', prompt: 'Add the sum to routes/workflows.py.' },
      { step_id: 'ui', title: 'ui: the cost column', prompt: 'Draw it in Workflows.tsx.' },
    ] },
    plan_digest: DIGEST_A, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-10-02T14:02:00Z', updated_at: '2026-10-02T14:09:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [
      { at: '2026-10-02T14:02:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' },
      { at: '2026-10-02T14:09:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
    ],
    ...over,
  }
}

type Handler = (method: string, url: string, body: unknown) => { status: number; body: unknown } | null

function serve(handler: Handler) {
  const calls: { method: string; url: string; body: unknown }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ method, url, body })
    const r = handler(method, url, body) ?? { status: 404, body: { code: 'not_found', message: `no stub for ${url}` } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  return calls
}

async function mount(view: string | null, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={view} go={go} />)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('/runs: the tenant\'s runs, newest first as served', () => {
  it('draws one row per run in the served order, with the state the API names', async () => {
    serve((m, url) => url.startsWith('/v1/runs') && m === 'GET'
      ? { status: 200, body: { runs: [
        run({ id: 'run_new', state: 'RUNNING', workflow_id: 'wf_8b21d0e4', created_at: '2026-10-02T14:30:00Z' }),
        run({ id: 'run_old', state: 'REJECTED', terminal: true, created_at: '2026-10-01T09:00:00Z' }),
      ], next_page_token: null, tenant_id: 'eng' } }
      : null)
    const { container, go } = await mount(null)
    await screen.findByRole('heading', { name: 'Runs', level: 1 }, WAIT)
    const rows = await waitFor(() => {
      const r = [...container.querySelectorAll<HTMLElement>('.rn-row')]
      expect(r).toHaveLength(2)
      return r
    }, WAIT)
    expect(visible(rows[0]!)).toContain('run_new')
    expect(visible(rows[0]!)).toContain('RUNNING')
    expect(visible(rows[1]!)).toContain('REJECTED')
    expect(rows[0]!.querySelector('[data-mark]'), 'a state with no mark').not.toBeNull()
    fireEvent.click(within(rows[0]!).getByRole('link', { name: 'run_new' }))
    expect(go).toHaveBeenCalledWith('work/runs?run=run_new')
  })

  it('a tenant with no runs is an empty state, not a blank table', async () => {
    serve((_m, url) => url.startsWith('/v1/runs') ? { status: 200, body: { runs: [], next_page_token: null, tenant_id: 'eng' } } : null)
    const { container } = await mount(null)
    await screen.findByText('No runs yet', undefined, WAIT)
    expect(container.querySelector('.rn-row')).toBeNull()
  })

  it('a failed read is a failure, never an empty list', async () => {
    serve(() => ({ status: 500, body: { code: 'internal', message: 'boom' } }))
    await mount(null)
    await screen.findByText('boom', undefined, WAIT)
    expect(screen.queryByText('No runs yet')).toBeNull()
  })

  it('offers the older page the API says exists, and appends it', async () => {
    const calls = serve((_m, url) => {
      if (!url.startsWith('/v1/runs')) return null
      const token = new URL(url, 'http://x').searchParams.get('page_token')
      return token === null
        ? { status: 200, body: { runs: [run({ id: 'run_a' })], next_page_token: 'tok1', tenant_id: 'eng' } }
        : { status: 200, body: { runs: [run({ id: 'run_b' })], next_page_token: null, tenant_id: 'eng' } }
    })
    const { container } = await mount(null)
    fireEvent.click(await screen.findByRole('button', { name: 'Show older runs' }, WAIT))
    await waitFor(() => expect(container.querySelectorAll('.rn-row')).toHaveLength(2), WAIT)
    expect(calls.some((c) => c.url.includes('page_token=tok1'))).toBe(true)
    expect(screen.queryByRole('button', { name: 'Show older runs' })).toBeNull()
  })
})

describe('/runs/<id>: one run', () => {
  it('shows the state, the plan and its digest, and the links it has', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2' ? { status: 200, body: { run: run() } } : null)
    const { container } = await mount('run=run_4c1e09d2')
    const plan = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-plan')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(container.querySelector('.rn-state'))).toContain('PLANNED')
    expect(visible(plan)).toContain('Sum step spend into the workflow read')
    expect(visible(plan)).toContain('api: sum step spend')
    expect(visible(plan)).toContain('ui: the cost column')
    // The digest, shortened on screen and whole in its title.
    const digest = plan.querySelector<HTMLElement>('.rn-digest')!
    expect(digest.getAttribute('title')).toBe(DIGEST_A)
    const links = container.querySelector<HTMLElement>('.rn-links')!
    expect(within(links).getByRole('link', { name: /example-org\/infra#512/ }).getAttribute('href'))
      .toBe('https://github.com/example-org/infra/issues/512')
    expect(visible(links)).toMatch(/Workflow.*none yet/)
    expect(visible(links)).toMatch(/Pull request.*not served/)
    expect(visible(links)).toMatch(/Plan comment.*not served/)
    // Overlaps are not on the run document: a region that says so.
    expect(visible(container.querySelector('.rn-overlaps'))).toMatch(/not served/)
  })

  it('links the workflow once there is one', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'RUNNING', workflow_id: 'wf_8b21d0e4', approved_by: 'operator@example.com', approved_at: '2026-10-02T14:11:00Z' }) } }
      : null)
    const { container, go } = await mount('run=run_4c1e09d2')
    const links = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-links')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const wf = await within(links).findByRole('link', { name: 'wf_8b21d0e4' }, WAIT)
    fireEvent.click(wf)
    expect(go).toHaveBeenCalledWith('work/workflows?wf=wf_8b21d0e4')
    expect(screen.queryByRole('button', { name: 'Approve and run' }), 'a RUNNING run offered approval').toBeNull()
  })

  it('Approve sends the digest of the plan shown, and draws the answer', async () => {
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run() } }
      if (url === '/v1/runs/run_4c1e09d2/plan:approve') {
        return { status: 200, body: { run: run({ state: 'RUNNING', workflow_id: 'wf_new' }) } }
      }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve and run' }, WAIT))
    await waitFor(() => expect(visible(container.querySelector('.rn-state'))).toContain('RUNNING'), WAIT)
    const post = calls.find((c) => c.url.endsWith('plan:approve'))!
    expect(post.method).toBe('POST')
    expect(post.body).toEqual({ plan_digest: DIGEST_A })
  })

  it('a stale-digest refusal re-reads the run and says the plan changed', async () => {
    let reads = 0
    serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') {
        reads += 1
        return { status: 200, body: { run: reads === 1 ? run() : run({ plan_digest: DIGEST_B, plan_revision: 2,
          plan_edited_by: 'someone@example.com',
          plan: { summary: 'An edited plan.', steps: [{ step_id: 'one', title: 'one step', prompt: 'do it' }] } }) } }
      }
      if (url.endsWith('plan:approve')) {
        return { status: 409, body: { code: 'plan_changed', message: 'the plan has changed since it was shown',
          detail: { plan_digest: DIGEST_B } } }
      }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve and run' }, WAIT))
    await screen.findByText(/The plan changed since you opened it/, undefined, WAIT)
    await waitFor(() => expect(visible(container.querySelector('.rn-plan'))).toContain('An edited plan.'), WAIT)
    expect(reads).toBe(2)
    expect(container.querySelector('.rn-digest')!.getAttribute('title')).toBe(DIGEST_B)
    // Still PLANNED: approving again approves the plan now on screen.
    expect(screen.getByRole('button', { name: 'Approve and run' })).not.toBeNull()
  })

  it('Reject sends the shown digest and the reason', async () => {
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run() } }
      if (url.endsWith('plan:reject')) {
        return { status: 200, body: { run: run({ state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com', rejection_reason: 'wrong repo' }) } }
      }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }, WAIT))
    fireEvent.change(screen.getByLabelText('Why (optional)'), { target: { value: 'wrong repo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reject this plan' }))
    await waitFor(() => expect(visible(container.querySelector('.rn-state'))).toContain('REJECTED'), WAIT)
    const post = calls.find((c) => c.url.endsWith('plan:reject'))!
    expect(post.body).toEqual({ plan_digest: DIGEST_A, reason: 'wrong repo' })
  })

  it('Edit sends the edited plan with the digest it replaces', async () => {
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run() } }
      if (url.endsWith('plan:edit')) return { status: 200, body: { run: run({ plan_digest: DIGEST_B, plan_revision: 2 }) } }
      return null
    })
    await mount('run=run_4c1e09d2')
    fireEvent.click(await screen.findByRole('button', { name: 'Edit plan' }, WAIT))
    fireEvent.change(screen.getByLabelText('Summary'), { target: { value: 'A shorter plan.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('plan:edit'))).toBe(true), WAIT)
    const post = calls.find((c) => c.url.endsWith('plan:edit'))!
    expect(post.body).toEqual({
      plan_digest: DIGEST_A,
      plan: { summary: 'A shorter plan.', steps: run().plan.steps },
    })
  })

  it('a FAILED run says why, and offers no action', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'FAILED', terminal: true, plan: null, plan_digest: null,
        error: 'the planner task task_planner1 ended FAILED' }) } }
      : null)
    await mount('run=run_4c1e09d2')
    await screen.findByText(/the planner task task_planner1 ended FAILED/, undefined, WAIT)
    expect(screen.queryByRole('button', { name: 'Approve and run' })).toBeNull()
  })

  it('an unknown run is not found, not an empty page', async () => {
    serve(() => ({ status: 404, body: { code: 'not_found', message: "run 'run_x' not found" } }))
    await mount('run=run_x')
    await screen.findByText("run 'run_x' not found", undefined, WAIT)
  })
})
