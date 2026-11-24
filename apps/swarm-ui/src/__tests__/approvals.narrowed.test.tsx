// AUTOMATE › APPROVALS, NARROWED (docs/design/plans-pipeline.md §2.2, lane U2).
//
// WHAT EACH CASE HOLDS:
//   * a plan, a held plan, a merge and a proposal are the Pipeline board's
//     cards: none is listed here, and no kind chip offers them;
//   * a firing waiting to run, a spec and an agent's input wait stay;
//   * one button counts what waits on a person on the board: the pipeline
//     read's `waits_on_person`, never a count of its own, and it opens the
//     board filtered to those cards; a failed board read draws no figure;
//   * a link to `/automate/approvals/<id>` of a board kind still opens its page;
//   * loading, an empty list and a failed read stay the three states they were.
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const MAY = { approve: true, reject: true, why: null, deciders: ['members'] }

function item(id: string, kind: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind,
    subject: { run_id: 'run_8f2a', schedule_id: 'sch_a', firing_id: 'sch_a:1', workflow_id: 'wf_1' },
    digest: {},
    summary: `The ${kind} item ${id}`,
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-6),
    expires_at: hoursFromNow(40),
    may_decide: MAY,
    ...over,
  }
}

const INBOX = [
  item('run:run_plan', 'plan'),
  item('run:run_hold', 'hold'),
  item('apr_merge', 'merge'),
  item('apr_prop', 'proposal'),
  item('apr_run', 'run'),
  item('apr_spec', 'spec'),
  item('input:task_1', 'input'),
]

function pipeline(waits: number): Json {
  const col = { count: 0, hidden: 0, oldest_at: null }
  return {
    cards: [],
    by_stage: { found: col, planned: { ...col, count: waits }, running: col, pr_ci: col, ended: col },
    waits_on_person: waits,
    partial: [],
    window_days: 14,
    tenant_id: 'eng',
  }
}

function routes(inbox: Json[] | null, board: Reply, extra: Handler = () => null): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals' && inbox !== null) return { status: 200, body: { approvals: inbox, tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/pipeline') return board
    return null
  }
}

async function mount(view: string, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const listed = () => [...document.querySelectorAll<HTMLElement>('.apc-card[data-approval]')].map((c) => c.dataset.approval)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Approvals keeps what is not on the board (U2)', () => {
  it('lists a firing, a spec and an input wait, and no plan, held plan, merge or proposal', async () => {
    await mount('', routes(INBOX, { status: 200, body: pipeline(4) }))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: list every kind the inbox serves, as before the board.
    expect(listed().sort()).toEqual(['apr_run', 'apr_spec', 'input:task_1'])
    expect(visible(screen.getByTestId('apl-waiting').querySelector('.apc-panel-h'))).toMatch(/^Waiting\s*3, oldest first$/)
    const chips = within(screen.getByRole('group', { name: 'Kind' }))
    for (const gone of [/^Plan/, /^Held plan/, /^Merge/, /^Proposal/]) expect(chips.queryByRole('button', { name: gone })).toBeNull()
    expect(visible(chips.getByRole('button', { name: /^Every kind/ }))).toBe('Every kind · 3')
  })

  it('counts what waits on the board from the pipeline read, and opens the board filtered to it', async () => {
    const { go, calls } = await mount('', routes(INBOX, { status: 200, body: pipeline(5) }))
    const line = await screen.findByTestId('apl-board', undefined, WAIT)
    // MUTATION: count the inbox's board kinds (4) rather than the board's own figure (5).
    expect(visible(line)).toBe('5 waiting on the Pipeline board')
    expect(line.tagName).toBe('BUTTON')
    expect(calls.some((c) => c.url.split('?')[0] === '/v1/pipeline')).toBe(true)
    fireEvent.click(line)
    expect(go).toHaveBeenCalledWith('automate/pipeline?waits=person')
  })

  it('draws no figure when the board read fails, and the list still draws', async () => {
    await mount('', routes(INBOX, { status: 503, body: { code: 'unavailable', message: 'not now' } }))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    expect(screen.queryByTestId('apl-board')).toBeNull()
    expect(listed()).toHaveLength(3)
  })

  it('is one line when only board kinds wait, with the board’s count beside it', async () => {
    await mount('', routes(INBOX.slice(0, 4), { status: 200, body: pipeline(4) }))
    const nothing = await screen.findByTestId('apl-nothing', undefined, WAIT)
    expect(visible(nothing)).toMatch(/^Nothing waiting/)
    expect(document.querySelector('.apc-card')).toBeNull()
    expect(visible(await screen.findByTestId('apl-board', undefined, WAIT))).toBe('4 waiting on the Pipeline board')
  })

  it('still opens a board kind at its own address', async () => {
    const detail = {
      approval: item('apr_merge', 'merge'),
      merge: { green_sha: 'abc1234', pull_request: { number: 12, url: 'https://github.com/example-org/example-api/pull/12' }, requirements_met: true, requirements_unmet: [], checks: { passed: 3, failed: 0, pending: 0 } },
    }
    const { calls } = await mount(
      'approval=apr_merge',
      routes(INBOX, { status: 200, body: pipeline(4) }, (m, url) => (m === 'GET' && url.split('?')[0] === '/v1/approvals/apr_merge' ? { status: 200, body: detail } : null)),
    )
    // MUTATION: refuse a kind the list no longer draws.
    await waitFor(() => expect(screen.getByTestId('ap-detail').tagName).toBe('SECTION'), WAIT)
    expect(calls.some((c) => c.url.split('?')[0] === '/v1/approvals/apr_merge')).toBe(true)
    expect(screen.getByRole('link', { name: '‹ Approvals' })).toBeTruthy()
  })

  it('keeps loading and a failed read apart from the empty list', async () => {
    await mount('', routes(null, { status: 200, body: pipeline(0) }, (m, url) => (url.split('?')[0] === '/v1/approvals' ? { status: 500, body: { code: 'internal', message: 'the inbox read broke' } } : null)))
    await waitFor(() => expect(document.querySelector('.state.failed')).not.toBeNull(), { timeout: 20000 })
    expect(screen.queryByTestId('apl-nothing')).toBeNull()
    expect(screen.queryByTestId('apl-board')).toBeNull()

    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
    vi.resetModules()
    const { ApprovalsScreen } = await import('../Approvals')
    document.body.innerHTML = ''
    render(<ApprovalsScreen view="" go={vi.fn()} />)
    await waitFor(() => expect(document.querySelectorAll('.skeleton').length).toBeGreaterThan(0), WAIT)
    expect(screen.queryByTestId('apl-nothing')).toBeNull()
    expect(screen.queryByTestId('apl-waiting')).toBeNull()
  })
})
