// AUTOMATE › APPROVALS, visual QA pass 2 (QA2-FIX-07, automate2-01; part of
// #2694): `GET /v1/approvals` defaults to `state=pending`, so the list read
// with no `state` was served the inbox alone, and its Decided and Expired
// cards could draw nothing the server held. The list asks `state=all`; the
// filtered page's whole-inbox read beside it (the kind counts) keeps the
// route's default.
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

const PENDING = {
  approval_id: 'apr_wait',
  tenant_id: 'eng',
  kind: 'plan',
  subject: { run_id: 'run_wait', issue: 'example-org/example-api#2364' },
  digest: { plan: 'p1' },
  summary: 'example-org/example-api#2364: the plan that waits',
  approvers: 'members',
  state: 'pending',
  requested_at: hoursFromNow(-2),
  expires_at: hoursFromNow(46),
  may_decide: { approve: true, reject: true, why: null, deciders: ['members'] },
}
const DECIDED = {
  ...PENDING,
  approval_id: 'apr_done',
  subject: { run_id: 'run_done', issue: 'example-org/example-api#2365' },
  summary: 'example-org/example-api#2365: the plan already approved',
  state: 'approved',
  decided_by: 'owner@example.com',
  decided_at: hoursFromNow(-1),
  may_decide: { approve: false, reject: false, why: 'decided', deciders: ['members'] },
}
/** An await-input step a member answered, as `project_closed_input` serves it in the history. */
const ANSWERED = {
  approval_id: 'input:task_done',
  tenant_id: 'eng',
  kind: 'input',
  subject: { task_id: 'task_done', workflow_id: 'wf_rv', step_id: 'review', form: 'verdict', artifact: 'review.json', max_files: 10 },
  digest: null,
  summary: 'review in wf_rv: the step already answered',
  approvers: 'members',
  state: 'answered',
  requested_at: hoursFromNow(-4),
  expires_at: hoursFromNow(20),
  projected: true,
  decided_by: 'member@example.com',
  decided_at: hoursFromNow(-2),
  reason: null,
}

/** The route as served: `state` defaults to `pending`; `all` adds the history. */
async function mount(view: string, history: unknown[] = [DECIDED]) {
  const seen = serve((m, url) => {
    const [path, q = ''] = url.split('?') as [string, string?]
    if (m !== 'GET' || path !== '/v1/approvals') return null
    const query = new URLSearchParams(q)
    const state = query.get('state') ?? 'pending'
    const kind = query.get('kind')
    const rows = ((state === 'all' ? [PENDING, ...history] : [PENDING]) as { kind: string }[]).filter((r) => kind === null || r.kind === kind)
    return { status: 200, body: { approvals: rows, tenant_id: 'eng' } }
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  render(<ApprovalsScreen view={view} go={vi.fn()} />)
  return seen
}

const reads = (seen: { method: string; url: string }[]) =>
  seen.filter((c) => c.method === 'GET' && c.url.split('?')[0]!.endsWith('/v1/approvals')).map((c) => new URLSearchParams(c.url.split('?')[1] ?? ''))

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('automate2-01: the list reads the inbox and its history', () => {
  it('asks `state=all` and draws the decided item on the Decided card', async () => {
    // MUTATION: read the list with `{}` again and the Decided card never appears.
    const seen = await mount('')
    const card = await screen.findByTestId('apl-decided', undefined, WAIT)
    expect(visible(card)).toContain('the plan already approved')
    expect(reads(seen).map((q) => q.get('state'))).toEqual(['all'])
  })

  it('asks `state=all` with the kind on a filtered page, and the default beside it', async () => {
    // MUTATION: drop `state` from the filtered read and the first read has none.
    const seen = await mount('kind=plan')
    const card = await screen.findByTestId('apl-decided', undefined, WAIT)
    expect(visible(card)).toContain('the plan already approved')
    const asked = reads(seen)
    expect(asked.find((q) => q.get('kind') === 'plan')?.get('state')).toBe('all')
    expect(asked.filter((q) => q.get('kind') === null).map((q) => q.get('state'))).toEqual([null])
  })
})

describe('an answered await-input step in the history (QA2B-E review)', () => {
  it('is drawn on the Decided card, not under "nobody decided these"', async () => {
    // MUTATION: file `answered` with the ended rows again and the Decided card never appears.
    await mount('', [ANSWERED])
    const card = await screen.findByTestId('apl-decided', undefined, WAIT)
    expect(visible(card)).toContain('the step already answered')
    expect(screen.queryByTestId('apl-ended')).toBeNull()
  })

  it("its link says the step is no longer waiting, not 'waiting' with an Answer button", async () => {
    // MUTATION: find the row whatever its state and the page draws it as waiting.
    await mount('approval=input%3Atask_done', [ANSWERED])
    const page = await screen.findByTestId('ap-detail', undefined, WAIT)
    expect(page.textContent).toContain('no longer waiting for an answer')
    expect(screen.queryByRole('button', { name: /Answer/ })).toBeNull()
    expect(screen.queryByRole('link', { name: /Answer/ })).toBeNull()
  })
})
