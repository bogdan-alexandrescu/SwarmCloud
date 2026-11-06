// AUTOMATE › APPROVALS, visual QA pass 2 (QA2-FIX-03; part of #2694): the
// decision page's share of two Schedules findings.
//
//   * automate2-15: "What asked for it" names the schedule by its name, its id
//     beside it, where it read "Its schedule sch_a";
//   * automate2-16: for the busy window after Reject is pressed, focus stays
//     on the pressed (aria-disabled) button, and a second press sends nothing.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

const ITEM = {
  approval_id: 'apr_run',
  tenant_id: 'eng',
  kind: 'run',
  subject: { schedule_id: 'sch_a', firing_id: 'sch_a:1' },
  digest: {},
  summary: 'Nightly issue sweep: a firing waits',
  approvers: 'members',
  state: 'pending',
  requested_at: hoursFromNow(-6),
  expires_at: hoursFromNow(66),
  may_decide: { approve: true, reject: true, why: null, deciders: ['members'] },
}

async function mount() {
  const seen = serve((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [ITEM], tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/approvals/apr_run') return { status: 200, body: { approval: ITEM } }
    if (m === 'GET' && path === '/v1/schedules/sch_a') return { status: 200, body: { schedule: { schedule_id: 'sch_a', name: 'Nightly issue sweep' }, firings: [] } }
    if (m === 'POST' && path === '/v1/approvals/apr_run:reject') return { status: 200, body: { approval: { ...ITEM, state: 'rejected' } } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  render(<ApprovalsScreen view="approval=apr_run" go={vi.fn()} />)
  await screen.findByTestId('ap-detail', undefined, WAIT)
  return seen
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the decision page (automate2-15, automate2-16)', () => {
  it('names the asking schedule by its name, its id beside it', async () => {
    // MUTATION: draw "Its schedule" again whatever the name and this fails.
    await mount()
    const link = await screen.findByRole('link', { name: 'Nightly issue sweep' }, WAIT)
    expect(link.getAttribute('href')).toBe('/automate/schedules/sch_a')
    expect(visible(link.parentElement)).toContain('sch_a')
    expect(screen.queryByRole('link', { name: 'Its schedule' })).toBeNull()
  })

  it('keeps focus on Reject while its request is in flight', async () => {
    // MUTATION: draw Reject with `Button`'s `busy` (disabled) again and focus drops to <body>.
    const seen = await mount()
    let answer: (() => void) | null = null
    const served = globalThis.fetch
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST') await new Promise<void>((done) => (answer = done))
      return served(input, init)
    }) as unknown as typeof fetch
    fireEvent.change(await screen.findByPlaceholderText('Say why it is rejected', undefined, WAIT), { target: { value: 'Not this week.' } })
    const reject = await waitFor(() => {
      const b = within(screen.getByTestId('ap-detail')).getByRole('button', { name: 'Reject' })
      expect(b.getAttribute('aria-describedby')).toBeNull()
      return b
    }, WAIT)
    reject.focus()
    fireEvent.click(reject)
    const busy = await within(screen.getByTestId('ap-detail')).findByRole('button', { name: 'Rejecting…' }, WAIT)
    expect(busy).toBe(reject)
    expect(busy.hasAttribute('disabled')).toBe(false)
    expect(busy.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(busy)
    fireEvent.click(busy)
    expect(answer).not.toBeNull()
    await act(async () => answer!())
    expect(seen.filter((c) => c.method === 'POST')).toHaveLength(1)
  })
})
