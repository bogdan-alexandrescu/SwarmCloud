// AUTOMATE'S LANDING (proposal A, frame A1, with A6's empty states and A7's
// phone for this page; lane AUTO-A1, part of #2417).
//
// WHAT EACH CASE HOLDS:
//   * every schedule wears the API's `state_word` as the reader's words, a
//     disabled one reading paused (BGD, 2026-10-26), never the stored state;
//   * the ones that need a look (waiting, tripped, failing) sort first and
//     are tinted, and the filter counts each word from the served summary;
//   * the approvals card counts EVERY inbox item from the summary and gives
//     the oldest item's expiry; the lead names the oldest and opens its page;
//   * an inbox that could not be read is a dash with its reason, never 0;
//   * the landing draws no decision button at all: Approve and Reject live on
//     the item's own page, where `may_decide` says who may;
//   * no schedules is a read empty list (A6a), a 404 is "not served" (A6b);
//   * at 390 a schedule is a card: name, then the word, the next firing and
//     today's spend, the other cells left to its page.
//
// The bodies are `routes/schedules.py::list_schedules`'s and
// `routes/approvals.py::list_approvals`'s shapes. Placeholders only; every id
// is built at runtime and nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const approvalId = (n: number) => `apr_${String(n).repeat(4)}${'c0'.repeat(10)}`

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 2 * * *',
    timezone: 'UTC',
    params: {},
    gate: { merge: 'approve', approvers: 'members' },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 4 },
    policy: { overlap: 'skip', catch_up: 'skip', dry_run: false, max_consecutive_failures: 3 },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(5),
    last_firing: { firing_id: `${id}:1`, slot: hoursFromNow(-7), outcome: 'succeeded', ended_at: hoursFromNow(-6) },
    consecutive_failures: 0,
    revision: 1,
    words: 'every day at 02:00',
    tier: 'R2',
    spend_today: { day: '2026-10-27', reported_usd: 22.6, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'active',
    ...over,
  }
}

/** One schedule in each of the six words, and a disabled one, which reads paused. */
const EVERY_WORD: Json[] = [
  schedule('sch_active', { name: 'Active one' }),
  schedule('sch_paused', { name: 'Paused one', state: 'paused', next_run_at: null, pause: { by: 'a@example.com', reason: 'holiday week' }, state_word: 'paused' }),
  schedule('sch_trip', {
    name: 'Tripped one',
    state: 'auto_paused',
    next_run_at: null,
    pause: { by: 'tick', code: 'RUN_OVER_BUDGET', reason: 'a run cost $6.10, over its $5.00 per-run tripwire', at: hoursFromNow(-2) },
    state_word: 'paused_by_tripwire',
  }),
  schedule('sch_wait', { name: 'Waiting one', pending_approvals: 2, state_word: 'waiting_on_approval' }),
  schedule('sch_fail', { name: 'Failing one', consecutive_failures: 2, last_firing: { firing_id: 'f', slot: hoursFromNow(-1), outcome: 'failed', ended_at: hoursFromNow(-1) }, state_word: 'failing' }),
  schedule('sch_never', { name: 'Never one', last_firing: null, next_run_at: hoursFromNow(80), state_word: 'never_fired' }),
  schedule('sch_off', { name: 'Off one', state: 'disabled', next_run_at: null, pause: { by: 'admin@example.com', reason: 'too costly' }, state_word: 'paused' }),
]

const BY_STATE = { active: 1, paused: 2, paused_by_tripwire: 1, waiting_on_approval: 1, failing: 1, never_fired: 1 }

function item(n: number, over: Json = {}): Json {
  return {
    approval_id: approvalId(n),
    kind: 'merge',
    subject: { schedule_id: 'sch_wait' },
    summary: `Merge PR #23${90 + n}`,
    state: 'pending',
    requested_at: hoursFromNow(-10 + n),
    expires_at: hoursFromNow(65 + n),
    may_decide: { approve: false, reject: false, why: 'Only the owner may decide.', deciders: ['operator@example.com'] },
    ...over,
  }
}

/** Three waiting: two from a schedule (a merge and a held plan), one a plan of a run made by hand. */
const INBOX = [item(1), item(2, { kind: 'hold', summary: 'Plan for issue #2399' }), item(3, { kind: 'plan', subject: { run_id: 'run_by_hand' }, summary: 'Plan for issue #2410' })]

type Routes = Record<string, { status: number; body: unknown }>

function routes(over: Partial<Routes> = {}): Routes {
  return {
    '/v1/schedules': {
      status: 200,
      body: {
        schedules: EVERY_WORD,
        summary: { by_state: BY_STATE, pending_approvals: 3, oldest_expires_at: hoursFromNow(66), unread: null },
        tenant_id: 'eng',
      },
    },
    '/v1/approvals': { status: 200, body: { approvals: INBOX, tenant_id: 'eng' } },
    '/v1/repositories': { status: 200, body: { repositories: [] } },
    ...over,
  } as Routes
}

async function mount(r: Routes) {
  const calls = serve((m, url) => (m === 'GET' ? r[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={null} spy={go} />)
  await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const rowOf = (id: string) => document.querySelector<HTMLElement>(`tr[data-schedule="${id}"]`)
const stateOf = (id: string) => visible(rowOf(id)!.querySelector('td[data-label="State"]'))
const landingRows = () => [...document.querySelectorAll<HTMLElement>('tr[data-schedule]')].map((r) => r.dataset.schedule)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the schedules table', () => {
  it('draws the columns of frame A1', async () => {
    await mount(routes())
    await waitFor(() => expect(rowOf('sch_active')).not.toBeNull(), WAIT)
    const heads = [...document.querySelectorAll('.al-table:not(.is-inbox) thead th')].map((th) => th.textContent)
    expect(heads).toEqual(['Schedule', 'Target', 'When', 'Next firing', 'Last firing', 'Spend today', 'Waiting', 'State'])
  })

  it('names each state in its own word, a disabled schedule reading paused', async () => {
    // MUTATION: draw STATE_WORD[s.state] (enabled / auto-paused / disabled) instead of state_word.
    await mount(routes())
    await waitFor(() => expect(rowOf('sch_off')).not.toBeNull(), WAIT)
    const word = (id: string) => visible(rowOf(id)!.querySelector('.al-word'))
    expect(word('sch_active')).toBe('active')
    expect(word('sch_paused')).toBe('paused')
    expect(word('sch_trip')).toBe('paused by its tripwire')
    expect(word('sch_wait')).toBe('waiting on approval')
    expect(word('sch_fail')).toBe('failing')
    expect(word('sch_never')).toBe('never fired')
    expect(word('sch_off')).toBe('paused')
    // The stored state never stands in for the word; the disabled one's line
    // under its word may say an admin disabled it, but its word is paused.
    for (const id of ['sch_active', 'sch_paused', 'sch_trip']) {
      expect(stateOf(id)).not.toMatch(/\benabled\b|auto-paused|\bdisabled\b/)
    }
  })

  it('says under each word what the API served about it', async () => {
    await mount(routes())
    await waitFor(() => expect(rowOf('sch_fail')).not.toBeNull(), WAIT)
    expect(stateOf('sch_fail')).toContain('2 failed in a row; pauses itself at 3')
    expect(stateOf('sch_trip')).toContain('over its $5.00 per-run tripwire')
    expect(stateOf('sch_paused')).toContain('holiday week')
    expect(stateOf('sch_off')).toContain('An admin disabled it: too costly')
    expect(stateOf('sch_never')).toContain('its first firing is in 3d')
    await waitFor(() => expect(stateOf('sch_wait')).toContain('Merge PR #2391'), WAIT)
    const waiting = rowOf('sch_wait')!.querySelector('td[data-label="Waiting"]')!
    expect(visible(waiting.querySelector('.al-fig'))).toBe('2')
    expect(visible(waiting.querySelector('.sc-sub'))).toBe('merge, held plan')
  })

  it('draws a dash with its reason, not a word, when the API serves no state_word', async () => {
    const old = { ...schedule('sch_old') }
    delete old.state_word
    await mount(routes({ '/v1/schedules': { status: 200, body: { schedules: [old], tenant_id: 'eng' } } }))
    await waitFor(() => expect(rowOf('sch_old')).not.toBeNull(), WAIT)
    expect(stateOf('sch_old')).toBe('— not served')
  })

  it('puts what needs a look first, tinted, then the rest by name', async () => {
    await mount(routes())
    await waitFor(() => expect(rowOf('sch_active')).not.toBeNull(), WAIT)
    expect(landingRows()).toEqual(['sch_trip', 'sch_wait', 'sch_fail', 'sch_active', 'sch_paused', 'sch_never', 'sch_off'])
    for (const id of ['sch_trip', 'sch_wait', 'sch_fail']) expect(rowOf(id)!.classList.contains('is-need'), id).toBe(true)
    for (const id of ['sch_active', 'sch_paused', 'sch_never', 'sch_off']) expect(rowOf(id)!.classList.contains('is-need'), id).toBe(false)
  })

  it('filters by word with the summary’s counts, Paused holding both paused words', async () => {
    await mount(routes())
    await waitFor(() => expect(rowOf('sch_active')).not.toBeNull(), WAIT)
    const filter = screen.getByRole('group', { name: 'Schedules by state' })
    const segs = within(filter).getAllByRole('button').map((b) => visible(b))
    expect(segs).toEqual(['All7', 'Waiting on approval1', 'Active1', 'Failing1', 'Paused3', 'Never fired1'])
    fireEvent.click(within(filter).getByRole('button', { name: /^Paused/ }))
    expect(landingRows()).toEqual(['sch_trip', 'sch_paused', 'sch_off'])
  })
})

describe('the lead and the approvals card', () => {
  it('counts every inbox item from the summary and gives the oldest item’s expiry', async () => {
    // MUTATION: count only the inbox items that name a schedule (2), or the
    // soonest expiry rather than the summary's oldest.
    await mount(routes())
    const pending = await screen.findByTestId('al-pending', undefined, WAIT)
    expect(visible(pending)).toBe('Approvals · 3 waiting')
    expect(visible(screen.getByTestId('al-card-expiry'))).toContain('The oldest expires in 3d.')
    const card = pending.closest('section')!
    const rows = [...card.querySelectorAll<HTMLElement>('tr[data-approval]')]
    expect(rows.map((r) => r.dataset.approval)).toEqual([approvalId(1), approvalId(2), approvalId(3)])
    expect(visible(rows[0]!.querySelector('td[data-label="From"]'))).toBe('Waiting one')
    expect(visible(rows[2]!.querySelector('td[data-label="From"]'))).toBe('not from a schedule')
    expect(within(rows[0]!).getByRole('link', { name: 'Merge PR #2391' }).getAttribute('href')).toBe(`/automate/approvals/${approvalId(1)}`)
  })

  it('leads with what needs the reader and opens the oldest approval', async () => {
    const { go } = await mount(routes())
    const lead = await screen.findByTestId('al-lead', undefined, WAIT)
    await waitFor(() => expect(visible(lead)).toContain('3 approvals wait on you, Tripped one is paused by its tripwire and Failing one is failing.'), WAIT)
    expect(visible(lead)).toContain('The oldest, Merge PR #2391, expires in 3d.')
    const open = within(lead).getByRole('link', { name: 'Review the oldest approval' })
    expect(open.getAttribute('href')).toBe(`/automate/approvals/${approvalId(1)}`)
    fireEvent.click(open)
    expect(go).toHaveBeenCalledWith(`automate/approvals?approval=${approvalId(1)}`)
  })

  it('says nothing needs the reader when nothing waits and nothing is stopped', async () => {
    await mount(
      routes({
        '/v1/schedules': {
          status: 200,
          body: { schedules: [schedule('sch_active')], summary: { by_state: { active: 1 }, pending_approvals: 0, oldest_expires_at: null, unread: null }, tenant_id: 'eng' },
        },
        '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
      }),
    )
    const lead = await screen.findByTestId('al-lead', undefined, WAIT)
    expect(visible(lead)).toContain('Nothing needs you')
    expect(visible(screen.getByTestId('al-pending'))).toBe('Approvals · 0 waiting')
    expect(visible(screen.getByTestId('al-pending').closest('section'))).toContain('the inbox was read and is empty')
  })

  it('draws a dash with its reason, never 0, when the inbox could not be read', async () => {
    // MUTATION: draw `pending_approvals ?? 0`.
    await mount(
      routes({
        '/v1/schedules': {
          status: 200,
          body: {
            schedules: EVERY_WORD,
            summary: { by_state: BY_STATE, pending_approvals: null, oldest_expires_at: null, unread: 'the approvals inbox could not be read' },
            tenant_id: 'eng',
          },
        },
        '/v1/approvals': { status: 503, body: { code: 'unavailable', message: 'the API did not answer in time' } },
      }),
    )
    const pending = await screen.findByTestId('al-pending', undefined, WAIT)
    expect(visible(pending)).toBe('— not read: the approvals inbox could not be read')
    const card = pending.closest('section')!
    expect(visible(card)).toContain('— not read')
    expect(visible(card)).not.toMatch(/\b0 waiting\b/)
    const lead = screen.getByTestId('al-lead')
    expect(visible(lead)).toContain('Approvals could not be read')
    expect(visible(rowOf('sch_wait')!.querySelector('td[data-label="Waiting"] .sc-sub'))).toBe('which, not read')
  })

  it('draws no Approve, Reject or Deny on the landing: deciding is on the item’s own page', async () => {
    await mount(routes())
    await screen.findByTestId('al-pending', undefined, WAIT)
    await waitFor(() => expect(document.querySelectorAll('tr[data-approval]').length).toBe(3), WAIT)
    expect(screen.queryByRole('button', { name: /approve|reject|deny/i })).toBeNull()
    expect(document.body.textContent).not.toMatch(/\bden(y|ied)\b/i)
  })
})

describe('the empty and honest states', () => {
  it('says a read empty list has no schedules yet, and offers the first one (A6a)', async () => {
    await mount(
      routes({
        '/v1/schedules': {
          status: 200,
          body: { schedules: [], summary: { by_state: {}, pending_approvals: 0, oldest_expires_at: null, unread: null }, tenant_id: 'eng' },
        },
        '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
      }),
    )
    await screen.findByText('No schedules yet', undefined, WAIT)
    const lead = screen.getByTestId('al-lead')
    expect(visible(lead)).toContain('Nothing scheduled yet')
    expect(within(lead).getByRole('link', { name: 'New schedule' }).getAttribute('href')).toBe('/automate/schedules/new')
    expect(document.querySelector('tr[data-schedule]')).toBeNull()
    expect(screen.queryByRole('group', { name: 'Schedules by state' })).toBeNull()
  })

  it('says a deployment that answers 404 does not serve Automate, and counts nothing (A6b)', async () => {
    await mount(routes({ '/v1/schedules': { status: 404, body: { code: 'not_found', message: 'Not Found' } } }))
    const dash = await screen.findByTestId('al-not-served', undefined, WAIT)
    expect(visible(dash)).toBe('— not served by this deployment')
    expect(visible(screen.getByTestId('al-lead'))).toContain('This deployment does not serve Automate.')
    expect(document.body.textContent).not.toMatch(/\b0 waiting\b|Schedules · 0/)
  })
})

describe('the phone (A7a)', () => {
  it('draws each schedule as a card: the name, then the word, the next firing and the spend', async () => {
    // MUTATION: drop the `.al-table .sc-row.al-row` block from the 560px query.
    await mount(routes())
    await waitFor(() => expect(rowOf('sch_wait')).not.toBeNull(), WAIT)
    const row = rowOf('sch_wait')!
    expect(painted(document.querySelector<HTMLElement>('.al-table thead')!, 'display', PHONE)).toBe('none')
    expect(painted(row, 'display', PHONE)).toBe('grid')
    for (const label of ['Target', 'When', 'Last firing', 'Waiting']) {
      expect(painted(row.querySelector<HTMLElement>(`td[data-label="${label}"]`)!, 'display', PHONE), label).toBe('none')
    }
    for (const label of ['Schedule', 'State']) {
      expect(painted(row.querySelector<HTMLElement>(`td[data-label="${label}"]`)!, 'grid-column', PHONE), label).toBe('1 / -1')
    }
    expect(painted(row.querySelector<HTMLElement>('td[data-label="Next firing"]')!, 'content', PHONE, 'before')).toBe('attr(data-label)')
    expect(painted(row.querySelector<HTMLElement>('td[data-label="State"]')!, 'content', PHONE, 'before')).toBe('none')
    // The cadence rides under the name on a phone only.
    expect(painted(row.querySelector<HTMLElement>('.al-ph')!, 'display', PHONE)).toBe('inline')
    expect(painted(row.querySelector<HTMLElement>('.al-ph')!, 'display', { width: 1440 })).toBe('none')
    // At 1440 the row is a table row and the cards sit two across.
    expect(painted(row, 'display', { width: 1440 })).not.toBe('grid')
    expect(painted(document.querySelector<HTMLElement>('.al-g2')!, 'grid-template-columns', { width: 1440 })).toBe('repeat(2, minmax(0, 1fr))')
    expect(painted(document.querySelector<HTMLElement>('.al-g2')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })
})
