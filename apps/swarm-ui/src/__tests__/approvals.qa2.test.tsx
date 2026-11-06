// AUTOMATE › APPROVALS, VISUAL QA PASS 2 (#2694, lane QA2-FIX-12).
//
// WHAT EACH CASE HOLDS:
//   * automate2-04: a held plan's numbered cards are in the DOM in their
//     numbers' order (1, 2, 3, 4), "Why it is held" before "What you approve";
//   * automate2-05: the hold's lead is in words, the code only as a title;
//   * automate2-06: the hold's approver line says "Approved by ... alone",
//     not "Approved only by ... alone";
//   * automate2-20: a merge digest naming no verdict draws no Verdict row.
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const HEAD = 'dbd00f0' + '6'.repeat(33)
const MAY = { approve: true, reject: true, why: null, deciders: ['members'] }

function item(id: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#2364' },
    digest: { plan: 'p1' },
    summary: `example-org/example-api#2364: a plan waits (${id})`,
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-6),
    expires_at: hoursFromNow(66),
    may_decide: MAY,
    ...over,
  }
}

const INBOX = [
  item('apr_merge', { kind: 'merge', requested_at: hoursFromNow(-30) }),
  item('apr_plan'),
  item('apr_run', { kind: 'run' }),
]

/** The inbox, filtered by `?kind=` as the route filters it. */
function routes(inbox: Json[], detail: Json | null = null): Handler {
  return (m, url) => {
    const [path, query] = url.split('?') as [string, string | undefined]
    if (m === 'GET' && path === '/v1/approvals') {
      const kind = new URLSearchParams(query ?? '').get('kind')
      return { status: 200, body: { approvals: kind === null ? inbox : inbox.filter((i) => i.kind === kind), tenant_id: 'eng' } }
    }
    if (m === 'GET' && path.startsWith('/v1/approvals/') && detail !== null) return { status: 200, body: detail }
    if (m === 'GET' && path.startsWith('/v1/approvals/')) return { status: 404, body: { detail: { code: 'not_found', message: 'no such approval' } } }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, go }
}

const page = () =>
  waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const PLAN = { summary: 'Rotate the deploy key reader', steps: [{ title: 'Edit the reader', prompt: 'Change the reader' }], files: ['terraform/iam.tf'] }
const held = (holdApprovers: unknown = 'owner_only') => ({
  approval: item('apr_hold', { kind: 'hold', approvers: 'members', hold: { code: 'NEEDS_OWNER', approvers: holdApprovers, reasons: ['iam'], matched: ['terraform/iam.tf'] } }),
  run: { id: 'run_8f2a', state: 'PLANNED', plan: PLAN },
})

describe('a held plan’s decision page (automate2-04, automate2-05, automate2-06)', () => {
  it('has its numbered cards in the DOM in their numbers’ order', async () => {
    await mount('approval=apr_hold', routes(INBOX, held()))
    const d = await page()
    await within(d).findByRole('region', { name: 'Why it is held' }, WAIT)
    const cards = [...d.querySelectorAll<HTMLElement>('.ad-card')]
    // MUTATION: put "Why it is held" back in the right column, and the DOM
    // reads 1, 3, 2, 4.
    expect(cards.map((c) => Number(c.getAttribute('data-n')))).toEqual(cards.map((_, i) => i + 1))
    expect(cards.map((c) => c.getAttribute('aria-label'))).toEqual(['The plan', 'Why it is held', 'What you approve', 'What asked for it'])
  })

  it('leads the hold with its words, the code only as a title', async () => {
    await mount('approval=apr_hold', routes(INBOX, held()))
    const d = await page()
    const note = await within(d).findByRole('note', undefined, WAIT)
    // MUTATION: draw "Held {code}" in the bold lead again.
    expect(visible(note)).not.toContain('NEEDS_OWNER')
    expect(visible(note)).toContain('Held for the platform owner.')
    expect(note.querySelector('b')!.getAttribute('title')).toBe('NEEDS_OWNER')
  })

  it('says the hold’s approvers without "only … alone"', async () => {
    await mount('approval=apr_hold', routes(INBOX, held()))
    const d = await page()
    const note = await within(d).findByRole('note', undefined, WAIT)
    // MUTATION: put "only" back before the owner_only words.
    expect(visible(note)).toContain('Approved by the platform owner alone, who may decide even as')
    expect(visible(note)).not.toContain('only by')
  })
})

describe('a merge’s digest (automate2-20)', () => {
  const merge = (digest: unknown) => ({
    approval: item('apr_merge', { kind: 'merge', digest }),
    merge: {
      green_sha: HEAD,
      pull_request: { number: 2392, url: 'https://github.com/example-org/example-api/pull/2392' },
      requirements_met: true,
      requirements_unmet: [],
      files_checked_head: HEAD,
    },
  })

  it('draws no Verdict row when the digest names no verdict', async () => {
    await mount('approval=apr_merge', routes(INBOX, merge({ head_sha: HEAD })))
    const d = await page()
    const card = await within(d).findByRole('region', { name: 'What you approve' }, WAIT)
    // MUTATION: draw "Verdict —, the digest names no verdict" again.
    expect(within(card).queryByText('Verdict')).toBeNull()
    expect(visible(card)).not.toContain('names no verdict')
    expect(visible(card)).toContain('dbd00f0')
  })

  it('keeps the Verdict row when the digest names one', async () => {
    await mount('approval=apr_merge', routes(INBOX, merge({ head_sha: HEAD, verdict: 'MERGE' })))
    const d = await page()
    const card = await within(d).findByRole('region', { name: 'What you approve' }, WAIT)
    expect(visible(within(card).getByText('Verdict').nextElementSibling)).toContain('MERGE')
  })
})
