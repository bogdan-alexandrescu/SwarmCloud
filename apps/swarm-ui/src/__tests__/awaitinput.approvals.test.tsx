/**
 * LANE HW-UI (docs/design/human-steps.md §4.5): a workflow step waiting for
 * a member, in the approvals inbox.
 *
 * The inbox PROJECTS each waiting step for every member, as kind `input`
 * with id `input:<task>` (`approvals.project_input`). It has no record and no
 * decide path, so:
 *
 *   * the item is drawn from its inbox row, never read from
 *     `GET /v1/approvals/{id}` (which has no record of it and answers 404);
 *   * it offers no Approve and no Reject: its Answer opens the step's card on
 *     its workflow, so the step has one writer;
 *   * a link to one that is no longer listed says it no longer waits, and
 *     reads nothing.
 *
 * Each case fails without the change. Placeholders only.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'

const WAIT = { timeout: 4000 }
const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

const INPUT = {
  approval_id: 'input:task_review',
  tenant_id: 'eng',
  kind: 'input',
  subject: { task_id: 'task_review', workflow_id: 'wf_rv', step_id: 'review', form: 'verdict', artifact: 'review.json', max_files: 10 },
  digest: null,
  summary: 'review in wf_rv: Read draft.md and approve it',
  approvers: 'members',
  state: 'pending',
  requested_at: hoursFromNow(-1),
  expires_at: hoursFromNow(3),
  projected: true,
}

async function mount(view: string, inbox: unknown[] = [INPUT]) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: inbox, tenant_id: 'eng' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  render(<ApprovalsScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { calls, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('a waiting step in the inbox', () => {
  it('is listed as an Answer and drawn from its row, with no decide verbs', async () => {
    const { calls, go } = await mount('approval=input%3Atask_review')
    const detail = await waitFor(() => {
      const d = document.querySelector<HTMLElement>('[data-testid="ap-detail"][data-kind="input"]')
      expect(d).toBeTruthy()
      return d!
    }, WAIT)
    // #2417: the step opens on its own page, its kind said on the page's chip.
    expect(detail.querySelector('.ad-chip.is-kind')!.textContent).toBe('answer')
    expect(detail.textContent).toContain('any member of this tenant')
    expect(within(detail).queryByRole('button', { name: /Approve|Reject/ })).toBeNull()
    // Never read as a record: the route has none.
    expect(calls.some((c) => c.url.startsWith('/v1/approvals/input'))).toBe(false)
    const answer = within(detail).getByRole('link', { name: 'Answer ›' })
    answer.click()
    expect(go).toHaveBeenCalledWith('work/workflows?wf=wf_rv')
  })

  it('a link to one no longer listed says it no longer waits, and reads nothing', async () => {
    const { calls } = await mount('approval=input%3Atask_gone', [])
    const linked = await screen.findByTestId('ap-linked', {}, WAIT)
    expect(linked.textContent).toContain('no longer waiting for an answer')
    expect(calls.some((c) => c.url.startsWith('/v1/approvals/input'))).toBe(false)
  })
})
