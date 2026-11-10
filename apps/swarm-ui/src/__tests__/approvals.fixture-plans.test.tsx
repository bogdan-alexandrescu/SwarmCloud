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
//   * the three-step plan draws its steps in their stages and its graph's edges.
//
// Runs on the fixtures: no VITE_LIVE, no served routes.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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
  const plan = (await run(id)).plan as unknown as { purpose: string; why: { line: string } }
  await waitFor(() => expect(visible(within(h).getByTestId('ab-purpose'))).toBe(plan.purpose), WAIT)
  expect(visible(within(h).getByTestId('ab-why'))).toContain(plan.why.line)
  for (const name of ['Where it changes', 'Risk', 'Validation', 'Steps']) expect(screen.getByRole('region', { name })).toBeTruthy()
  return h
}

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
      const link = await screen.findByRole('link', { name: `${r.issue.ref}: ${r.plan!.summary}` }, WAIT)
      expect(link.getAttribute('href')).toContain(encodeURIComponent(`run:${id}`))
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
