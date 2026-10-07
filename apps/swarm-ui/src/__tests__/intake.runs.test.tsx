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
//   * a poll that serves another plan is HELD, not drawn: the plan on screen
//     stays the one Approve and Edit name, an editor saves with the digest it
//     opened on, and a notice offers the new plan;
//   * Reject sends the shown digest and the reason;
//   * a staged plan shows the API's plan_shape and each step's depends_on, and
//     an edit carries every depends_on through, so saving never turns it back
//     into a chain;
//   * what the run has not got yet -- the PR, the plan and status comments --
//     is said with its reason, never hidden and never invented; cost, which
//     the run does not serve, is a dash; a workflow not yet created is
//     "none yet";
//   * the plan shows what #454's planner writes: mode, estimate, the
//     requirements, the OVERLAPS as links (the issue's acceptance test is that
//     the plan names them), and each step's files, tests and estimate; the
//     editor round-trips all of it;
//   * the CI loop: fix round n of N, the pull request, the green sha, Closes
//     against part-of, the failing excerpt AS TEXT, the comments on the issue
//     and a failed write-back.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { FULL_PLAN, digest, issueRun } from './runfixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Digests built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST_A = digest('a1')
const DIGEST_B = digest('b2')
const run = issueRun

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
  vi.useRealTimers()
})

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

const EDITED = { summary: 'Someone else\'s plan.', steps: [{ step_id: 'one', title: 'one step', prompt: 'do it' }] }

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
    expect(visible(rows[0]!)).toContain('Running')
    expect(visible(rows[1]!)).toContain('Rejected')
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
    expect(visible(container.querySelector('.rn-state'))).toContain('Planned')
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
    expect(visible(links)).toMatch(/Pull request.*none yet/)
    expect(visible(links)).toMatch(/Plan comment.*not posted/)
    // A plan from before the planner read open work does not say: the region says so.
    expect(visible(container.querySelector('.rn-overlaps'))).toMatch(/does not say/)
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
    fireEvent.click((await screen.findAllByRole('button', { name: 'Approve and run' }, WAIT))[0]!)
    await waitFor(() => expect(visible(container.querySelector('.rn-state'))).toContain('Running'), WAIT)
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
    fireEvent.click((await screen.findAllByRole('button', { name: 'Approve and run' }, WAIT))[0]!)
    await screen.findByText(/The plan changed since you opened it/, undefined, WAIT)
    await waitFor(() => expect(visible(container.querySelector('.rn-plan'))).toContain('An edited plan.'), WAIT)
    expect(reads).toBe(2)
    expect(container.querySelector('.rn-digest')!.getAttribute('title')).toBe(DIGEST_B)
    // Still PLANNED: approving again approves the plan now on screen.
    expect(screen.getAllByRole('button', { name: 'Approve and run' })[0]!).not.toBeNull()
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
    fireEvent.click((await screen.findAllByRole('button', { name: 'Reject' }, WAIT))[0]!)
    fireEvent.change(screen.getByLabelText('Why (optional)'), { target: { value: 'wrong repo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reject this plan' }))
    await waitFor(() => expect(visible(container.querySelector('.rn-state'))).toContain('Rejected'), WAIT)
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
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit plan' }, WAIT))[0]!)
    fireEvent.change(screen.getByLabelText('Summary'), { target: { value: 'A shorter plan.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('plan:edit'))).toBe(true), WAIT)
    const post = calls.find((c) => c.url.endsWith('plan:edit'))!
    expect(post.body).toEqual({
      plan_digest: DIGEST_A,
      plan: { summary: 'A shorter plan.', steps: run().plan.steps },
    })
  })

  const STAGED = { summary: 'Two independent halves, then the wiring.', steps: [
    { step_id: 'api', title: 'api: sum step spend', prompt: 'Add the sum.', depends_on: [] },
    { step_id: 'ui', title: 'ui: the cost column', prompt: 'Draw it.', depends_on: [] },
    { step_id: 'wire', title: 'wire the column to the sum', prompt: 'Wire it.', depends_on: ['api', 'ui'] },
  ] }

  it('a staged plan states its shape and each step\'s dependencies', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET'
      ? { status: 200, body: { run: run({ plan: STAGED,
          plan_shape: '3 steps in 2 stages (2 → 1), then review and fix' }) } }
      : null))
    await mount('run=run_4c1e09d2')
    const plan = await screen.findByRole('region', { name: 'The plan' }, WAIT)
    await waitFor(() => expect(visible(plan)).toContain('3 steps in 2 stages (2 → 1), then review and fix gated on its verdict'), WAIT)
    expect(visible(plan)).not.toContain('then a review and a fix')
    const steps = plan.querySelectorAll('.rn-step')
    expect(visible(steps.item(0))).toContain('starts at once')
    expect(visible(steps.item(2))).toContain('after api, ui')
  })

  it('Edit keeps every step\'s depends_on, so a staged plan does not become a chain', async () => {
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run({ plan: STAGED }) } }
      if (url.endsWith('plan:edit')) return { status: 200, body: { run: run({ plan: STAGED, plan_digest: DIGEST_B, plan_revision: 2 }) } }
      return null
    })
    await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit plan' }, WAIT))[0]!)
    fireEvent.change(screen.getByLabelText('Summary'), { target: { value: 'A shorter plan.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('plan:edit'))).toBe(true), WAIT)
    const post = calls.find((c) => c.url.endsWith('plan:edit'))!
    expect(post.body).toEqual({ plan_digest: DIGEST_A, plan: { summary: 'A shorter plan.', steps: STAGED.steps } })
    expect((post.body as { plan: { steps: { depends_on?: string[] }[] } }).plan.steps.map((s) => s.depends_on))
      .toEqual([[], [], ['api', 'ui']])
  })

  it('a FAILED run says why, and offers no action', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'FAILED', terminal: true, plan: null, plan_digest: null,
        error: 'the planner task task_planner1 ended FAILED' }) } }
      : null)
    await mount('run=run_4c1e09d2')
    // The API's `FAILED` is worded at read, as every state in prose is (QA G2-25).
    await screen.findByText(/the planner task task_planner1 ended failed/, undefined, WAIT)
    expect(screen.queryByRole('button', { name: 'Approve and run' })).toBeNull()
  })

  it('the CI states (#454) are named as the API names them, with a mark and what they mean', async () => {
    const cases: [Record<string, unknown>, RegExp][] = [
      [{ state: 'CHECKING', workflow_id: 'wf_8b21d0e4', pull_request: { number: 57, url: null, checks: 'pending' } },
        /Holds no capacity: its CI is read/],
      [{ state: 'FIXING', workflow_id: 'wf_8b21d0e4', ci_fix_round: 2, ci_fix_workflows: ['wf_a', 'wf_b'] },
        /fix round 2 of 3/],
    ]
    for (const [over, line] of cases) {
      serve((_m, url) => url === '/v1/runs/run_4c1e09d2' ? { status: 200, body: { run: run(over) } } : null)
      const { container, unmount } = await mount('run=run_4c1e09d2')
      // Named as the API names it, in sentence case (no all-caps rule, U10b D11).
      const named = String(over.state).charAt(0) + String(over.state).slice(1).toLowerCase()
      await waitFor(() => expect(visible(container.querySelector('.rn-state'))).toContain(named), WAIT)
      expect(container.querySelector('.rn-state [data-mark]'), `${over.state} has no mark`).not.toBeNull()
      expect(visible(container.querySelector('.rn-state-t'))).toMatch(line)
      expect(screen.queryByRole('button', { name: 'Approve and run' })).toBeNull()
      unmount()
    }
  })

  it('the plan names its overlaps as links, with kind and note, ahead of the steps', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2' ? { status: 200, body: { run: run({ plan: FULL_PLAN }) } } : null)
    const { container } = await mount('run=run_4c1e09d2')
    const overlaps = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-overlaps')
      expect(el).not.toBeNull()
      expect(el!.querySelectorAll('.rn-overlap')).toHaveLength(2)
      return el!
    }, WAIT)
    const pr = within(overlaps).getByRole('link', { name: 'example-org/infra#498' })
    expect(pr.getAttribute('href')).toBe('https://github.com/example-org/infra/pull/498')
    const issue = within(overlaps).getByRole('link', { name: 'example-org/infra#507' })
    expect(issue.getAttribute('href')).toBe('https://github.com/example-org/infra/issues/507')
    expect(visible(overlaps)).toContain('pull request')
    expect(visible(overlaps)).toContain('Already edits routes/workflows.py')
    expect(visible(overlaps)).toContain('Asks for the same column')
    // Prominent: the overlaps come before the plan's steps in the page.
    const plan = container.querySelector('.rn-plan')!
    expect(overlaps.compareDocumentPosition(plan) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('an overlap ref that is not owner/repo#N is drawn as text, never a link', async () => {
    const plan = { ...FULL_PLAN, overlaps: [{ ref: 'javascript:alert(1)#1', kind: 'issue', note: 'odd' }] }
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2' ? { status: 200, body: { run: run({ plan }) } } : null)
    const { container } = await mount('run=run_4c1e09d2')
    await waitFor(() => expect(container.querySelector('.rn-overlap')).not.toBeNull(), WAIT)
    expect(container.querySelector('.rn-overlaps a')).toBeNull()
    expect(visible(container.querySelector('.rn-overlaps'))).toContain('javascript:alert(1)#1')
  })

  it('a plan that found no overlaps says so, with the open work it read', async () => {
    const plan = { ...FULL_PLAN, overlaps: [] }
    const open_work = { repository: 'example-org/infra', read_at: '2026-10-02T14:03:00Z',
      issues: [{ number: 1, title: 'a' }, { number: 2, title: 'b' }], issues_truncated: false,
      pull_requests: [{ number: 3, title: 'c', files: [], files_truncated: false }], pull_requests_truncated: false }
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2' ? { status: 200, body: { run: run({ plan, open_work }) } } : null)
    const { container } = await mount('run=run_4c1e09d2')
    await waitFor(() => expect(visible(container.querySelector('.rn-overlaps'))).toMatch(/None found/), WAIT)
    expect(visible(container.querySelector('.rn-overlaps'))).toMatch(/2 open issues and 1 open pull request/)
  })

  it('the plan shows its mode, estimate, requirements, and each step\'s files, tests and estimate', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2' ? { status: 200, body: { run: run({ plan: FULL_PLAN }) } } : null)
    const { container } = await mount('run=run_4c1e09d2')
    const plan = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-plan')
      expect(visible(el)).toContain('about 3 agent-hours')
      return el!
    }, WAIT)
    expect(visible(plan)).toMatch(/workflow/)
    const reqs = container.querySelector<HTMLElement>('.rn-reqs')!
    expect(reqs.querySelectorAll('li')).toHaveLength(2)
    expect(visible(reqs)).toContain('The list draws a cost column')
    const steps = [...plan.querySelectorAll<HTMLElement>('.rn-step')]
    expect(visible(steps[0]!)).toContain('apps/swarm-api/swarm_api/routes/workflows.py')
    expect(visible(steps[0]!)).toContain('test_the_read_sums_step_spend')
    expect(visible(steps[0]!)).toContain('1 hour')
    expect(visible(steps[1]!)).toContain('apps/swarm-ui/src/Workflows.tsx')
    expect(visible(steps[1]!)).toContain('2 hours')
  })

  it('the editor round-trips every new field and saves with the digest it opened on', async () => {
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run({ plan: FULL_PLAN }) } }
      if (url.endsWith('plan:edit')) return { status: 200, body: { run: run({ plan_digest: DIGEST_B, plan_revision: 2 }) } }
      return null
    })
    await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit plan' }, WAIT))[0]!)
    // Unchanged, it sends the plan it opened on, every field included.
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('plan:edit'))).toHaveLength(1), WAIT)
    expect(calls.find((c) => c.url.endsWith('plan:edit'))!.body).toEqual({ plan_digest: DIGEST_A, plan: FULL_PLAN })
  })

  it('the editor edits requirements, the estimate and a step\'s files, tests and estimate', async () => {
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run({ plan: FULL_PLAN }) } }
      if (url.endsWith('plan:edit')) return { status: 200, body: { run: run({ plan_digest: DIGEST_B, plan_revision: 2 }) } }
      return null
    })
    await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit plan' }, WAIT))[0]!)
    fireEvent.change(screen.getByLabelText('Requirements, one per line'), { target: { value: 'Only this one\n\n' } })
    fireEvent.change(screen.getByLabelText('Estimate'), { target: { value: '' } })
    fireEvent.change(screen.getAllByLabelText('Files, one per line')[1]!, { target: { value: 'a.tsx\n b.tsx ' } })
    fireEvent.change(screen.getAllByLabelText('Tests, one per line')[1]!, { target: { value: '' } })
    fireEvent.change(screen.getAllByLabelText('Step estimate')[0]!, { target: { value: '90 minutes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('plan:edit'))).toBe(true), WAIT)
    const { estimate: _dropped, ...rest } = FULL_PLAN
    const { tests: _cleared, ...ui } = FULL_PLAN.steps[1]!
    expect(calls.find((c) => c.url.endsWith('plan:edit'))!.body).toEqual({
      plan_digest: DIGEST_A,
      plan: {
        ...rest,
        requirements: ['Only this one'],
        steps: [{ ...FULL_PLAN.steps[0]!, estimate: '90 minutes' }, { ...ui, files: ['a.tsx', 'b.tsx'] }],
      },
    })
  })

  it('a CHECKING run links its pull request and names the fix rounds left', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'CHECKING', workflow_id: 'wf_8b21d0e4', ci_fix_round: 1,
        pull_request: { number: 57, url: 'https://github.com/example-org/infra/pull/57', head_sha: 'abc1234def', checks: 'red' } }) } }
      : null)
    const { container } = await mount('run=run_4c1e09d2')
    const ci = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-ci')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(ci)).toMatch(/fix round 1 of 3/)
    expect(visible(ci)).toContain('abc1234')
    expect(visible(ci)).toMatch(/red/)
    const links = container.querySelector<HTMLElement>('.rn-links')!
    expect(within(links).getByRole('link', { name: /#57/ }).getAttribute('href'))
      .toBe('https://github.com/example-org/infra/pull/57')
  })

  it('a DONE run shows the green sha and Closes when every requirement was confirmed', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'DONE', terminal: true, plan: FULL_PLAN, workflow_id: 'wf_8b21d0e4',
        green_sha: 'f00dfeed1234567', requirements_met: true, requirements_unmet: [],
        pull_request: { number: 57, url: 'https://github.com/example-org/infra/pull/57', head_sha: 'f00dfeed1234567', checks: 'green' } }) } }
      : null)
    const { container } = await mount('run=run_4c1e09d2')
    const ci = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-ci')
      expect(el).not.toBeNull()
      expect(el!.querySelector('code[title="f00dfeed1234567"]')).not.toBeNull()
      return el!
    }, WAIT)
    // Short, like the checks' sha; the whole one is its title (lane U14 item 6).
    const green = [...ci.querySelectorAll('.ctl-fact')].find((f) => visible(f.querySelector('b')) === 'green at')!
    expect(visible(green.querySelector('code'))).toBe('f00dfee')
    expect(visible(ci)).toContain('Closes #512')
    expect(visible(ci)).not.toContain('part of')
  })

  it('a run whose review left requirements open says part of, and names them', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'CHECKING', plan: FULL_PLAN, workflow_id: 'wf_8b21d0e4',
        requirements_met: false, requirements_unmet: ['The list draws a cost column'], requirements_note: 'the UI half is missing',
        pull_request: { number: 57, url: 'https://github.com/example-org/infra/pull/57', head_sha: 'abc', checks: 'pending' } }) } }
      : null)
    const { container } = await mount('run=run_4c1e09d2')
    const ci = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-ci')
      expect(visible(el)).toContain('part of #512')
      return el!
    }, WAIT)
    expect(visible(ci)).not.toContain('Closes')
    expect(visible(ci)).toContain('the UI half is missing')
    // The unmet requirement is marked in the requirements list.
    const unmet = container.querySelector<HTMLElement>('.rn-reqs .is-unmet')
    expect(visible(unmet)).toContain('The list draws a cost column')
  })

  it('a FAILED run shows the failing excerpt as text, never as HTML', async () => {
    const excerpt = 'FAILED tests/unit/test_x.py::test_y\n<img src=x onerror=alert(1)><b>bold</b>\nassert 1 == 2'
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ state: 'FAILED', terminal: true, workflow_id: 'wf_8b21d0e4', ci_fix_round: 3,
        error: 'checks still red after 3 fix rounds', failure_excerpt: excerpt,
        pull_request: { number: 57, url: 'https://github.com/example-org/infra/pull/57', head_sha: 'abc', checks: 'red' } }) } }
      : null)
    const { container } = await mount('run=run_4c1e09d2')
    const block = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-excerpt')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(block.querySelector('pre')).not.toBeNull()
    expect(block.querySelector('pre img')).toBeNull()
    expect(block.querySelector('pre b')).toBeNull()
    expect(block.textContent).toContain('<img src=x onerror=alert(1)><b>bold</b>')
    expect(block.textContent).toContain('assert 1 == 2')
    expect(visible(container.querySelector('.rn-ci'))).toMatch(/3 of 3/)
  })

  it('links the plan and status comments on the issue, and shows a failed write-back', async () => {
    serve((_m, url) => url === '/v1/runs/run_4c1e09d2'
      ? { status: 200, body: { run: run({ plan_comment_id: 9001, status_comment_id: 9002,
        writeback_error: 'writeback_forbidden: the credential lacks issues: write' }) } }
      : null)
    const { container } = await mount('run=run_4c1e09d2')
    const links = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.rn-links')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(within(links).getByRole('link', { name: 'plan comment' }).getAttribute('href'))
      .toBe('https://github.com/example-org/infra/issues/512#issuecomment-9001')
    expect(within(links).getByRole('link', { name: 'status comment' }).getAttribute('href'))
      .toBe('https://github.com/example-org/infra/issues/512#issuecomment-9002')
    expect(visible(container.querySelector('.rn-writeback'))).toContain('the credential lacks issues: write')
  })

  it('an unknown run is not found, not an empty page', async () => {
    serve(() => ({ status: 404, body: { code: 'not_found', message: "run 'run_x' not found" } }))
    await mount('run=run_x')
    await screen.findByText("run 'run_x' not found", undefined, WAIT)
  })

  it('a poll that serves another plan under an open editor is held; Save sends the digest the editor opened on', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    let reads = 0
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') {
        reads += 1
        return { status: 200, body: { run: reads === 1 ? run()
          : run({ plan_digest: DIGEST_B, plan_revision: 2, plan_edited_by: 'someone@example.com', plan: EDITED }) } }
      }
      if (url.endsWith('plan:edit')) {
        return { status: 409, body: { code: 'plan_changed', message: 'the plan has changed since it was shown',
          detail: { plan_digest: DIGEST_B } } }
      }
      return null
    })
    const { container, RUN_POLL_MS } = await (async () => {
      const m = await mount('run=run_4c1e09d2')
      const { RUN_POLL_MS: ms } = await import('../Runs')
      return { ...m, RUN_POLL_MS: ms }
    })()
    await advance(0)
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit plan' })[0]!)
    fireEvent.change(screen.getByLabelText('Summary'), { target: { value: 'My edit of the first plan.' } })

    // Someone else edits; the next poll reads their plan.
    await advance(RUN_POLL_MS)
    expect(reads).toBe(2)
    // The editor and the digest on screen are still the first plan's, and the change is offered, not swapped in.
    expect((screen.getByLabelText('Summary') as HTMLTextAreaElement).value).toBe('My edit of the first plan.')
    expect(container.querySelector('.rn-digest')!.getAttribute('title')).toBe(DIGEST_A)
    expect(visible(container.querySelector('.rn-held'))).toContain('The plan changed since this page drew it')
    expect(visible(container.querySelector('.rn-held'))).toContain('someone@example.com')

    fireEvent.click(screen.getByRole('button', { name: 'Save the plan' }))
    await advance(0)
    const post = calls.find((c) => c.url.endsWith('plan:edit'))!
    expect(post.body).toEqual({
      plan_digest: DIGEST_A,
      plan: { summary: 'My edit of the first plan.', steps: run().plan.steps },
    })
    // The API refuses it: the run is read again and the page says why.
    await advance(0)
    expect(reads).toBe(3)
    expect(screen.getByText(/The plan changed since you opened it/)).not.toBeNull()
    expect(visible(container.querySelector('.rn-plan'))).toContain("Someone else's plan.")
    expect(container.querySelector('.rn-digest')!.getAttribute('title')).toBe(DIGEST_B)
  })

  it('a poll never swaps the plan under Approve: it approves the digest shown until the reader asks for the new plan', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    let reads = 0
    const calls = serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') {
        reads += 1
        return { status: 200, body: { run: reads === 1 ? run()
          : run({ plan_digest: DIGEST_B, plan_revision: 2, plan_edited_by: 'someone@example.com', plan: EDITED }) } }
      }
      if (url.endsWith('plan:approve')) {
        return { status: 200, body: { run: run({ state: 'APPROVED', plan_digest: DIGEST_B, plan: EDITED,
          approved_by: 'operator@example.com', approved_at: '2026-10-02T14:20:00Z', approved_digest: DIGEST_B }) } }
      }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    const { RUN_POLL_MS } = await import('../Runs')
    await advance(0)
    await advance(RUN_POLL_MS)
    expect(reads).toBe(2)
    expect(visible(container.querySelector('.rn-plan'))).toContain('Sum step spend into the workflow read')

    // Asking for it draws it, and only then does Approve name it.
    fireEvent.click(screen.getByRole('button', { name: 'Show the plan now' }))
    expect(container.querySelector('.rn-held')).toBeNull()
    expect(visible(container.querySelector('.rn-plan'))).toContain("Someone else's plan.")
    fireEvent.click(screen.getAllByRole('button', { name: 'Approve and run' })[0]!)
    await advance(0)
    const post = calls.find((c) => c.url.endsWith('plan:approve'))!
    expect(post.body).toEqual({ plan_digest: DIGEST_B })
  })

  it('planMovedUnder holds only a changed plan on a run still PLANNED', async () => {
    const { planMovedUnder } = await import('../Runs')
    type R = Parameters<typeof planMovedUnder>[0]
    const a = run() as unknown as R
    expect(planMovedUnder(a, run({ plan_digest: DIGEST_B }) as unknown as R)).toBe(true)
    expect(planMovedUnder(a, run() as unknown as R)).toBe(false)
    // The first plan arriving is drawn at once.
    expect(planMovedUnder(run({ state: 'PLANNING', plan: null, plan_digest: null }) as unknown as R, a)).toBe(false)
    // A run that left PLANNED is drawn at once: no action is left naming the old plan.
    expect(planMovedUnder(a, run({ state: 'APPROVED', plan_digest: DIGEST_B }) as unknown as R)).toBe(false)
  })
})
