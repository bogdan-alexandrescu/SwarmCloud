// LANE QA2-FIX-13 (part of #2694, the console visual QA pass 2 epic):
// automate2-08. The Decided card's note read `as the read served them` --
// how the console got the rows, not what order they are in. The route sorts
// the inbox by `requested_at`, oldest first (swarm_api/approvals.py), so the
// card orders its rows by `decided_at`, newest first, and says so.
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const MAY = { approve: false, reject: false, why: 'decided', deciders: ['members'] }

function decided(id: string, requestedH: number, decidedH: number): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: `run_${id}`, issue: 'example-org/example-api#2364' },
    digest: { plan: 'p1' },
    summary: `example-org/example-api#2364: plan ${id}`,
    approvers: 'members',
    state: 'approved',
    requested_at: hoursFromNow(requestedH),
    expires_at: hoursFromNow(48),
    decided_by: 'owner@example.com',
    decided_at: hoursFromNow(decidedH),
    may_decide: MAY,
  }
}

// As the route serves them: by `requested_at`, oldest first. The one asked
// for first was decided last.
const INBOX = [decided('apr_a', -30, -1), decided('apr_b', -20, -10), decided('apr_c', -10, -5)]

async function mount() {
  serve((m, url) => {
    const [path] = url.split('?') as [string]
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: INBOX, tenant_id: 'eng' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  render(<ApprovalsScreen view="" go={vi.fn()} />)
  return screen.findByTestId('apl-decided', undefined, WAIT)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('automate2-08: the Decided card', () => {
  it('says `newest first`, not how the read served them', async () => {
    const card = await mount()
    // MUTATION: put back `note="as the read served them"`.
    expect(visible(card)).toContain('newest first')
    expect(visible(card)).not.toContain('as the read served them')
  })

  it('orders its rows by when they were decided, newest first', async () => {
    const card = await mount()
    const rows = within(card).getAllByRole('row').slice(1)
    // MUTATION: drop the sort -> the route's order, apr_a, apr_b, apr_c.
    expect(rows.map((r) => /plan (apr_\w)/.exec(visible(r))?.[1])).toEqual(['apr_a', 'apr_c', 'apr_b'])
  })
})
