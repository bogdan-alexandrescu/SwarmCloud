/**
 * AN ATTEMPT SAYS WHETHER IT RAN ON A PLAN OR AN API KEY, AND A MOVE BETWEEN
 * THEM (lane LP-U5, docs/design/llm-providers.md §5.4 and §8.1).
 *
 * The chip reads the attempt's own record (`credential_tier`, `account_id`,
 * contract request 152), its tooltip the account's Name from the accounts
 * read (`GET /v1/accounts`), the id when that cannot be read. An attempt from
 * before the fields shows nothing. On the attempt card a swap event that moved
 * between a plan and a key is one line, its reason in the tooltip; a move
 * within one kind is no line.
 *
 * BREAK IT: draw the chip for an attempt with no tier; print the tier's code
 * (`api_key`) instead of its words; put the id in the tooltip when the Name was
 * read; draw a line for a plan → plan swap, or none for a plan → key one; put
 * the reason on the glass; let the chip or the line force a width -- each case
 * fails.
 */
import { act, render, waitFor } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { AccountsPage, AttemptRow } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({ loadAccountPool: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptCredential, AttemptTierMoves, Run, tierMoves, useAccountNames } = await import('../AgentDetail')
const { normaliseAttemptCredential } = await import('../api')

const PLAN_ID = 'eng:plan-one'
const KEY_ID = 'eng:key-one'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function page(accounts: { account_id: string; label: string; display_name?: string | null }[]): AccountsPage {
  return { accounts, tenant_id: 'eng' } as unknown as AccountsPage
}

function text(el: Element | null): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/** The account-assigned event the worker writes on a swap (`_emit_swapped`). */
function swap(m: number, from: string, to: string, reason: string) {
  return ev('RUNNING', at(m), 'att_1', {
    cause: 'account_assigned',
    account_id: to === 'plan' ? PLAN_ID : KEY_ID,
    swapped_from: from === 'plan' ? PLAN_ID : KEY_ID,
    swap_reason: reason,
    tier: to,
    from_tier: from,
  })
}

afterEach(() => {
  api.loadAccountPool.mockReset()
})

describe('the chip, from the attempt\'s own record', () => {
  it('says Plan for a plan and API key for a key', () => {
    const plan = render(<AttemptCredential a={attempt(1, { account_id: PLAN_ID, credential_tier: 'plan' })} names={null} />)
    expect(text(plan.container.querySelector('.att-tier'))).toBe('Plan')
    const key = render(<AttemptCredential a={attempt(1, { account_id: KEY_ID, credential_tier: 'api_key' })} names={null} />)
    expect(text(key.container.querySelector('.att-tier'))).toBe('API key')
  })

  it('draws nothing for an attempt from before the fields, or one off the pool', () => {
    const before = render(<AttemptCredential a={attempt(1)} names={new Map()} />)
    expect(before.container.innerHTML).toBe('')
    const tenant = render(
      <AttemptCredential a={attempt(1, { account_id: null, credential_source: 'tenant_secret', credential_tier: null })} names={new Map()} />,
    )
    expect(tenant.container.innerHTML).toBe('')
  })

  it('its tooltip is the account\'s Name, and the id when the Name cannot be read', () => {
    const a = attempt(1, { account_id: KEY_ID, credential_tier: 'api_key' })
    const named = render(<AttemptCredential a={a} names={new Map([[KEY_ID, 'Shared key']])} />)
    expect(named.container.querySelector<HTMLElement>('.att-tier')!.title).toBe('Shared key')
    const unread = render(<AttemptCredential a={a} names={null} />)
    expect(unread.container.querySelector<HTMLElement>('.att-tier')!.title).toBe(KEY_ID)
    const missing = render(<AttemptCredential a={a} names={new Map([['eng:other', 'Other']])} />)
    expect(missing.container.querySelector<HTMLElement>('.att-tier')!.title).toBe(KEY_ID)
  })

  it('reads the Names from the accounts read, the label when no Name was given, and none when it fails', async () => {
    api.loadAccountPool.mockResolvedValue(ok(page([
      { account_id: PLAN_ID, label: 'plan-one', display_name: 'Main plan' },
      { account_id: KEY_ID, label: 'key-one', display_name: null },
    ])))
    const seen: (ReadonlyMap<string, string> | null)[] = []
    function Probe({ wanted }: { wanted: boolean }) {
      seen.push(useAccountNames(wanted))
      return null
    }
    render(<Probe wanted />)
    await waitFor(() => expect(seen.at(-1)).not.toBeNull())
    expect(seen.at(-1)!.get(PLAN_ID)).toBe('Main plan')
    expect(seen.at(-1)!.get(KEY_ID)).toBe('key-one')

    // Not asked for when no attempt carries a tier: a page of old attempts costs no read.
    api.loadAccountPool.mockClear()
    await act(async () => {
      render(<Probe wanted={false} />)
    })
    expect(api.loadAccountPool).not.toHaveBeenCalled()

    api.loadAccountPool.mockResolvedValue({ status: 'error', error: { kind: 'http', httpStatus: 403, code: 'forbidden', message: 'no' } })
    seen.length = 0
    await act(async () => {
      render(<Probe wanted />)
    })
    expect(api.loadAccountPool).toHaveBeenCalled()
    expect(seen.every((s) => s === null)).toBe(true)
  })
})

describe('a move between a plan and a key, on the attempt\'s timeline', () => {
  const a = attempt(1, { account_id: KEY_ID, credential_tier: 'api_key' })

  it('a cross-kind swap is one line, its reason in the tooltip, not on the glass', () => {
    const moves = tierMoves(a, [swap(3, 'plan', 'api_key', 'exhausted'), swap(6, 'api_key', 'plan', 'plan_available')])
    expect(moves.map((m) => m.words)).toEqual(['moved to API key', 'moved to plan'])
    const { container } = render(<AttemptTierMoves a={a} events={[swap(3, 'plan', 'api_key', 'exhausted')]} />)
    const lines = [...container.querySelectorAll<HTMLElement>('.att-tier-move')]
    expect(lines).toHaveLength(1)
    const line = lines[0]!
    expect(text(line.querySelector('.ev-type'))).toBe('moved to API key')
    expect(line.title).toMatch(/quota/i)
    expect(text(line)).not.toMatch(/quota|exhausted/i)
  })

  it('a swap within one kind draws no line, and nor does an event without a tier', () => {
    expect(tierMoves(a, [swap(3, 'plan', 'plan', 'exhausted'), swap(4, 'api_key', 'api_key', 'drain')])).toEqual([])
    const old = ev('RUNNING', at(3), 'att_1', { cause: 'account_assigned', account_id: PLAN_ID, swapped_from: KEY_ID, swap_reason: 'exhausted' })
    expect(tierMoves(a, [old])).toEqual([])
    const { container } = render(<AttemptTierMoves a={a} events={[swap(3, 'plan', 'plan', 'exhausted')]} />)
    expect(container.innerHTML).toBe('')
  })

  it('an assignment without `from_tier` is a move against the tier the attempt held before; another attempt\'s is not read', () => {
    const first = ev('RUNNING', at(1), 'att_1', { cause: 'account_assigned', account_id: PLAN_ID, tier: 'plan' })
    const again = ev('RUNNING', at(5), 'att_1', { cause: 'account_assigned', account_id: KEY_ID, tier: 'api_key' })
    expect(tierMoves(a, [first]).map((m) => m.words)).toEqual([])
    expect(tierMoves(a, [first, again]).map((m) => m.words)).toEqual(['moved to API key'])
    const elsewhere = { ...swap(3, 'plan', 'api_key', 'exhausted'), attempt_id: 'att_2' }
    expect(tierMoves(a, [elsewhere])).toEqual([])
    expect(tierMoves(a, null)).toEqual([])
  })
})

describe('the attempt card on Details', () => {
  function runOf(attempts: AttemptRow[], events: AgentRun['events']): AgentRun {
    return {
      task: task({ state: 'SUCCEEDED', started_at: at(1), completed_at: at(10) }),
      events,
      eventsDetail: null,
      attempts,
      attemptsDetail: null,
      classes: null,
      classesDetail: null,
      classesRouteMissing: false,
    }
  }

  function card(run: AgentRun): HTMLElement {
    const el = document.createElement('div')
    el.innerHTML = renderToStaticMarkup(<Run run={run} headed />)
    const found = el.querySelector<HTMLElement>('.att-card')
    expect(found).not.toBeNull()
    return found!
  }

  it('carries the chip in its head and the move on its timeline', () => {
    const c = card(runOf(
      [attempt(1, { account_id: KEY_ID, credential_source: 'account_pool', credential_tier: 'api_key' })],
      [swap(3, 'plan', 'api_key', 'exhausted'), swap(4, 'api_key', 'api_key', 'drain')],
    ))
    const chip = c.querySelector<HTMLElement>('.ctl-card-head .att-tier')
    expect(text(chip)).toBe('API key')
    expect(chip!.title).toBe(KEY_ID)
    expect([...c.querySelectorAll('.att-tier-move')].map(text).map((t) => t.split(' · ')[0])).toEqual(['moved to API key'])
  })

  it('an attempt from before the fields is the card it was', () => {
    const c = card(runOf([attempt(1)], []))
    expect(c.querySelector('.att-tier')).toBeNull()
    expect(c.querySelector('.att-tier-move')).toBeNull()
  })

  it('the prose guard: the chip and the line add their words and nothing else', () => {
    const before = text(card(runOf([attempt(1)], [])))
    const after = text(card(runOf(
      [attempt(1, { account_id: PLAN_ID, credential_tier: 'plan' })],
      [swap(3, 'api_key', 'plan', 'plan_available')],
    )))
    const added = after.split(/\s+/).length - before.split(/\s+/).length
    // `Plan`, `moved to plan`, `·` and the line's clock (`10:03 UTC`): no
    // sentence on the surface.
    expect(added).toBeLessThanOrEqual(7)
    expect(after).not.toMatch(/plan available|free again/i)
  })

  it('phone 390: the chip and the line wrap within the card and set no width', () => {
    const c = card(runOf(
      [attempt(1, { account_id: KEY_ID, credential_tier: 'api_key' })],
      [swap(3, 'plan', 'api_key', 'exhausted')],
    ))
    for (const el of [c.querySelector<HTMLElement>('.att-tier'), c.querySelector<HTMLElement>('.att-tier-move')]) {
      expect(el).not.toBeNull()
      expect(el!.getAttribute('style') ?? '').not.toMatch(/width|white-space/)
    }
    // A long account Name stays in the tooltip, never on the glass.
    const long = render(<AttemptCredential a={attempt(1, { account_id: KEY_ID, credential_tier: 'api_key' })} names={new Map([[KEY_ID, 'x'.repeat(400)]])} />)
    expect(text(long.container)).toBe('API key')
  })
})

describe('the attempt read\'s credential fields', () => {
  it('keeps a tier the contract names, and reads any other as not recorded', () => {
    expect(normaliseAttemptCredential(attempt(1, { account_id: KEY_ID, credential_tier: 'api_key' })).credential_tier).toBe('api_key')
    const odd = normaliseAttemptCredential(attempt(1, { account_id: ' ', credential_tier: 'voice_dedicated' as never }))
    expect(odd.credential_tier).toBeNull()
    expect(odd.account_id).toBeNull()
    // Absent stays absent: an older API.
    expect('credential_tier' in normaliseAttemptCredential(attempt(1))).toBe(false)
  })
})
