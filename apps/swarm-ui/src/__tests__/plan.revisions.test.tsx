// THE REVISION PICKER AND THE DIFF (docs/design/plans-pipeline.md §2.3, §3.3,
// §5.4; lane U4 PLAN-THREAD-UI). Red first: PlanRevisions.tsx does not exist.
//
// WHAT EACH CASE HOLDS:
//   * the picker names every served revision, the shown one pressed, each
//     with its author and cost (a dash with its reason when unreported,
//     never $0);
//   * picking an older revision reads its whole plan and shows it read-only
//     (no Comment, no verbs) with its diff against the newest, while the
//     document and its Approve stay on the shown revision;
//   * a run whose history was not kept says so in a pill;
//   * `diffPlans` names the summary, requirement and per-step changes by
//     step id, and draws plan text as text.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { visible } from './repofixture'
import { D2, PLAN, PLAN_R1, PLAN_R3, REV1, REV2, RUN, WAIT, mount, routes, sent } from './planthreadfixture'
import { PlanDiff, diffPlans } from '../PlanRevisions'

const doc = () => screen.findByTestId('pd-doc', undefined, WAIT)

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the revision picker', () => {
  it('names every served revision with the shown one pressed, its author and cost', async () => {
    await mount(routes({ run: () => ({ plan_revisions: [REV2, { ...REV1, cost: { usd: null, task_id: 'task_planner1', model_class: 'opus', why_none: 'cost not reported' } }] }) }))
    const picker = await screen.findByTestId('pr-picker', undefined, WAIT)
    const r2 = within(picker).getByRole('button', { name: /Revision 2/ })
    const r1 = within(picker).getByRole('button', { name: /Revision 1/ })
    expect(r2.getAttribute('aria-pressed')).toBe('true')
    expect(r1.getAttribute('aria-pressed')).toBe('false')
    // An unreported cost is a dash with its reason, never $0.
    expect(visible(r1)).not.toContain('$0')
    expect(within(r1).getByLabelText('cost not reported')).toBeTruthy()
  })

  it('shows an older revision read-only with its diff against the newest, and keeps the document on the shown one', async () => {
    const { calls } = await mount(routes())
    const picker = await screen.findByTestId('pr-picker', undefined, WAIT)
    fireEvent.click(within(picker).getByRole('button', { name: /Revision 1/ }))
    const view = await screen.findByTestId('pr-view', undefined, WAIT)
    await waitFor(() => expect(sent(calls, 'GET', `/v1/runs/${RUN}/plan-revisions/1`)).toHaveLength(1), WAIT)
    expect(visible(view)).toContain('read-only')
    // Revision 1's own text, read-only.
    await waitFor(() => expect(visible(view)).toContain(PLAN_R1.steps[0]!.prompt), WAIT)
    expect(within(view).queryByRole('button', { name: 'Comment' })).toBeNull()
    expect(within(view).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(within(view).queryByRole('textbox')).toBeNull()
    // Its diff against the newest (revision 2): step 1's prompt changed.
    const diff = within(view).getByTestId('pr-diff')
    const row = within(diff).getAllByRole('listitem').find((r) => r.getAttribute('data-path') === 'steps.overlaps-read.prompt')!
    expect(row).toBeTruthy()
    expect(visible(within(row).getByRole('deletion'))).toBe(PLAN_R1.steps[0]!.prompt)
    expect(visible(within(row).getByRole('insertion'))).toBe(PLAN.steps[0]!.prompt)
    // The document and the bar still name revision 2.
    expect(visible(screen.getByTestId('pd-summary'))).toBe(PLAN.summary)
    expect(screen.getByTestId('pd-digest').getAttribute('data-digest')).toBe(D2)
    fireEvent.click(within(view).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('pr-view')).toBeNull(), WAIT)
  })

  it('says the read failed in the server’s words rather than drawing a blank', async () => {
    await mount(routes({ extra: (m, url) => (m === 'GET' && url.split('?')[0] === `/v1/runs/${RUN}/plan-revisions/1` ? { status: 404, body: { code: 'not_found', message: 'run has no plan revision 1' } } : null) }))
    const picker = await screen.findByTestId('pr-picker', undefined, WAIT)
    fireEvent.click(within(picker).getByRole('button', { name: /Revision 1/ }))
    const view = await screen.findByTestId('pr-view', undefined, WAIT)
    await waitFor(() => expect(visible(within(view).getByRole('alert'))).toContain('run has no plan revision 1'), WAIT)
  })

  it('says earlier revisions were not kept when the history is incomplete', async () => {
    await mount(routes({ run: () => ({ plan_revisions: [{ ...REV2, revision: 2 }], plan_history_complete: false }) }))
    await doc()
    const picker = await screen.findByTestId('pr-picker', undefined, WAIT)
    expect(within(picker).getByTestId('pr-partial')).toBeTruthy()
  })
})

describe('diffPlans', () => {
  it('names summary, requirement and per-step changes by step id', () => {
    const rows = diffPlans(PLAN, { ...PLAN_R3, requirements: [...PLAN.requirements, 'Cached per digest'], steps: [...PLAN_R3.steps, { step_id: 'docs', title: 'Document the cache', prompt: 'Write it down.' }] })
    const paths = rows.map((r) => `${r.op}:${r.path}`)
    expect(paths).toContain('replace:summary')
    expect(paths).toContain('add:requirements.3')
    expect(paths).toContain('replace:steps.iam-binding.files')
    expect(paths).toContain('add:steps.docs')
    expect(paths).not.toContain('replace:steps.overlaps-read.prompt')
    expect(diffPlans(PLAN, PLAN)).toEqual([])
  })

  it('draws plan text as text', () => {
    const markup = '<img src=x onerror="alert(1)">'
    render(<PlanDiff rows={diffPlans(PLAN, { ...PLAN, summary: markup })} />)
    const diff = screen.getByTestId('pr-diff')
    expect(visible(within(diff).getByRole('insertion'))).toBe(markup)
    expect(within(diff).queryByRole('img')).toBeNull()
    expect(diff.innerHTML).not.toContain('<img')
  })
})
