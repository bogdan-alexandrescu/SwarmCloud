// THE APPROVAL PAGE'S THREE EXAMPLE PLANS, ON THE CONSOLE'S FIXTURES (owner,
// 6 November 2026: the page can be seen and tested on its fixtures).
//
// WHAT EACH CASE HOLDS:
//   * the fixture inbox lists the three example runs waiting for approval,
//     projected as the live API projects a PLANNED run (`run:<id>`);
//   * each opens on its brief with every section the run read serves drawn:
//     purpose, why, the change map, Risk, Validation and the steps;
//   * the small plan is one low-risk step over a handful of files;
//   * the high-risk plan names its invariants and the frozen contract;
//   * the three-step plan draws its steps in their stages and its graph's edges;
//   * Approve and Reject decide the example's run, which leaves the inbox.
//
// Runs on the fixtures, with the example plans' switch on (api.ts
// `FIXTURE_EXAMPLE_PLANS`, the `swarm_fixture_example_plans=on` cookie, read
// when api.ts is imported): no VITE_LIVE, no served routes. Off it the
// fixture inbox is empty, as the default fixtures' always was.

import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { visible } from './repofixture'

const WAIT = { timeout: 4000 }

const SMALL = 'run_5a1d3e70'
const HIGH = 'run_7e02b9c4'
const STEPS = 'run_3c8f6a15'

async function mount(view: string | null) {
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  render(<ApprovalsScreen view={view} go={vi.fn()} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
}

/** The run behind an example plan, as the fixture's run read serves it. */
async function run(id: string) {
  const { loadRun } = await import('../api')
  const r = await loadRun(id)
  if (r.status !== 'ok') throw new Error(`fixture run ${id} not served`)
  return r.data.run
}

async function open(id: string) {
  await mount(`approval=run:${id}`)
  const h = await screen.findByTestId('ab-head', undefined, WAIT)
  const plan = (await run(id)).plan as unknown as { purpose: string; why: string }
  await waitFor(() => expect(visible(within(h).getByTestId('ab-purpose'))).toBe(plan.purpose), WAIT)
  expect(visible(within(h).getByTestId('ab-why'))).toContain(plan.why)
  for (const name of ['Where it changes', 'Risk', 'Validation', 'Steps']) expect(screen.getByRole('region', { name })).toBeTruthy()
  return h
}

const SWITCH = 'swarm_fixture_example_plans'

beforeAll(() => {
  document.cookie = `${SWITCH}=on; path=/`
})

afterAll(() => {
  document.cookie = `${SWITCH}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('the fixture inbox', () => {
  it('lists the three example plans waiting for approval', async () => {
    const { USE_FIXTURES } = await import('../api')
    expect(USE_FIXTURES).toBe(true)
    await mount(null)
    // MUTATION: serve the fixture inbox empty again, and no plan can be opened.
    for (const id of [SMALL, HIGH, STEPS]) {
      const r = await run(id)
      expect(r.state).toBe('PLANNED')
      const card = await screen.findByRole('region', { name: `${r.issue.ref}: ${r.plan!.summary}` }, WAIT)
      const link = within(card).getByRole('link', { name: 'Review' })
      expect(link.getAttribute('href')).toContain(encodeURIComponent(`run:${id}`))
    }
  })
})

describe('the switch', () => {
  it('off, the fixture inbox is empty and no example run is served', async () => {
    document.cookie = `${SWITCH}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`
    try {
      vi.resetModules()
      const { loadApprovals, loadRun } = await import('../api')
      // MUTATION: load the example plans without the switch, and the default
      // fixtures' inbox and Runs list are no longer main's.
      expect((await loadApprovals()).status).toBe('empty')
      for (const id of [SMALL, HIGH, STEPS]) expect((await loadRun(id)).status).not.toBe('ok')
    } finally {
      document.cookie = `${SWITCH}=on; path=/`
    }
  })
})

describe('each example plan opens on its brief', () => {
  it('the small plan: one low-risk step over a handful of files', async () => {
    const h = await open(SMALL)
    expect(h.getAttribute('data-risk')).toBe('low')
    expect(within(screen.getByRole('region', { name: 'Steps' })).getByText('one step')).toBeTruthy()
    const files = within(screen.getByRole('list', { name: 'Files the plan changes' })).getAllByRole('button')
    expect(files.length).toBeGreaterThanOrEqual(3)
    expect(files.length).toBeLessThanOrEqual(6)
  })

  it('the high-risk plan: admission and the frozen contract, its invariants named', async () => {
    const h = await open(HIGH)
    expect(h.getAttribute('data-risk')).toBe('high')
    const band = await within(h).findByTestId('ab-touches', undefined, WAIT)
    const named = [...band.querySelectorAll('.ab-inv')].map((x) => visible(x))
    expect(named).toContain('Invariant 2')
    expect(named).toContain('Invariant 3')
    expect(named).toContain('Frozen contract · contract.py')
    const risk = screen.getByRole('region', { name: 'Risk' })
    expect(risk.className).toContain('is-high')
    expect(visible(within(risk).getByRole('list', { name: 'Invariants touched' }))).toContain('apps/common/swarm_common/contract.py')
    expect([...document.querySelectorAll('.ab-area.is-frozen text')].map((t) => t.textContent)).toEqual(['Frozen contract'])
  })

  it('the three-step plan: its steps in their order, and the change graph’s edges', async () => {
    await open(STEPS)
    const steps = screen.getByRole('region', { name: 'Steps' })
    expect(within(steps).getByText('3 steps in 3 stages')).toBeTruthy()
    expect([...steps.querySelectorAll('.ab-stage-h')].map((x) => visible(x))).toEqual(['Stage 1', 'Stage 2', 'Stage 3'])
    expect(visible(steps)).toContain('after step 1')
    expect(visible(steps)).toContain('after step 2')
    const graph = (await run(STEPS)).plan as unknown as { graph: { edges: unknown[] } }
    expect(document.querySelectorAll('.ab-edge').length).toBe(graph.graph.edges.length)
  })
})

describe('a decision on an example plan', () => {
  it('Approve starts its run and takes it out of the inbox', async () => {
    const h = await open(SMALL)
    fireEvent.click(within(h).getByRole('button', { name: 'Approve' }))
    // MUTATION: leave the fixture approve unserved, and the run stays PLANNED.
    await waitFor(async () => expect((await run(SMALL)).state).toBe('RUNNING'), WAIT)
    const { loadApprovals } = await import('../api')
    const inbox = await loadApprovals()
    const ids = inbox.status === 'ok' ? inbox.data.approvals.map((i) => i.approval_id) : []
    expect(ids).not.toContain(`run:${SMALL}`)
    expect(ids).toContain(`run:${HIGH}`)
  })

  it('Reject turns its run down with the reason typed', async () => {
    const h = await open(HIGH)
    fireEvent.click(within(h).getByRole('button', { name: 'Reject' }))
    fireEvent.change(within(h).getByRole('textbox', { name: /Reason/ }), { target: { value: 'split the contract change out first' } })
    fireEvent.click(within(h).getByRole('button', { name: 'Reject' }))
    await waitFor(async () => expect((await run(HIGH)).state).toBe('REJECTED'), WAIT)
    expect((await run(HIGH)).rejection_reason).toBe('split the contract change out first')
  })
})
