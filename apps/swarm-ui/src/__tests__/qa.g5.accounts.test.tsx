// CAPACITY › ACCOUNTS, THE 2026-10-07 QA PASS (G5-07, G5-16, G5-17).
//
//   G5-07  the "5h used" / "7d used" tiles drew their track in the tile's own
//          colour (`--surface-2` on `--sk-hv`, the same pixel in both themes),
//          40px wide in a 122px tile: a lone fill nobody could read as a share.
//   G5-16  Holding now was a bulleted list of `task_…` ids; the mock-up
//          (capacity.html §D) draws Agent · Attempt · Since, the agent by name.
//          Last given out said "9h ago" where the mock-up gives the clock.
//   G5-17  on the chosen row, dark, the muted clauses were `--text-faint` on
//          `--sk-acs`: 4.33:1, under the 4.5:1 floor for text this size.
//
// Colours are read through `cascade` (marks.ts), not getComputedStyle: jsdom
// orders rules by source alone and applies no @media block.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AccountsBoard } from '../api'
import type { Account, AccountsPage, Task } from '../types'
import type { AccountHolders } from '../Accounts'
import { THEMES, build, painted, resolveColour, sameColour } from './marks'
import { contrast, over } from './spaceprobe'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => api)

const reads = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: reads.read,
}))

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const WIDE = { width: 1440 } as const
const MINUTES = 60_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-07T10:00:00Z' }
}

function account(over: Partial<Account> = {}): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 2,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

function board(a: Account): AccountsBoard {
  return {
    page: { accounts: [a], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

async function openRow(a: Account) {
  api.loadAccountsBoard.mockResolvedValue(ok(board(a)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', undefined, WAIT)
  const open = document.querySelector<HTMLButtonElement>('.acct-li > button.acct-open')!
  if (open.getAttribute('aria-current') !== 'true') fireEvent.click(open)
}

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

beforeEach(() => {
  reads.read.mockReset()
  api.loadTask.mockReset()
})

describe('G5-07: a window tile draws its track across the tile, in a colour the tile is not', () => {
  const hosts: HTMLElement[] = []
  const draw = () => build('div.acct-tile.acct-window > span.ctl-util-track', hosts)

  it('fills the tile width and sits under the figure', () => {
    const track = draw()
    // MUTATION: put back `width: 40px` on `.acct-window > .ctl-util-track`.
    expect(painted(track, 'width', WIDE)).toBe('100%')
    expect(painted(track, ['margin', 'margin-left'], WIDE)).toBe('4px 0 0')
  })

  for (const theme of THEMES) {
    it(`contrasts with the tile in ${theme}`, () => {
      const track = draw()
      const tile = track.parentElement!
      const env = { ...WIDE, theme }
      const trackBg = resolveColour(painted(track, ['background', 'background-color'], env)!, theme)
      const tileBg = resolveColour(painted(tile, ['background', 'background-color'], env)!, theme)
      // The QA reading: rgb(238,244,250) on rgb(238,244,250) light, rgb(17,35,59)
      // on rgb(17,35,59) dark.
      expect(sameColour(trackBg, tileBg), `the ${theme} track is the tile's own colour`).toBe(false)
      // And an edge, so the track's extent reads even where the two are close.
      const edge = painted(track, 'box-shadow', env) ?? ''
      expect(edge).toMatch(/^inset 0 0 0 1px var\(--line\)$/)
      expect(sameColour(resolveColour('var(--line)', theme), tileBg)).toBe(false)
    })
  }
})

describe('G5-17: the chosen row keeps its muted clauses at 4.5:1 in both themes', () => {
  const hosts: HTMLElement[] = []
  // The three the QA pass measured -- "5h", "eng:team", "given out 3m ago" --
  // and the state note that shares their line.
  const CLAUSES = [
    'li.acct-li.is-chosen > button.acct-open > small.acct-li-line > span.acct-clears > span.acct-clears-win',
    'li.acct-li.is-chosen > span.acct-li-meta > span.raw',
    'li.acct-li.is-chosen > span.acct-li-meta > span.acct-given',
    'li.acct-li.is-chosen > span.acct-li-meta > span.acct-why',
  ]
  for (const theme of THEMES) {
    for (const selector of CLAUSES) {
      it(`${selector.split('> ').at(-1)} in ${theme}`, () => {
        const el = build(selector, hosts)
        const env = { ...WIDE, theme }
        // Inherited when the element declares nothing of its own.
        let at: Element | null = el
        let ink: string | null = null
        while (at !== null && ink === null) {
          ink = painted(at, 'color', env)
          at = at.parentElement
        }
        const row = el.closest('li')!
        const bg = resolveColour(painted(row, ['background', 'background-color'], env)!, theme)
        const fg = over(resolveColour(ink!, theme), bg)
        // MUTATION: drop the `--text-dim-on-selected` rule: dark reads 4.33.
        expect(contrast(fg, bg), `${ink} on the chosen row`).toBeGreaterThanOrEqual(4.5)
      })
    }
  }
})

describe('G5-16: Holding now is the mock-up’s Agent · Attempt · Since table', () => {
  it('names each agent from its task, with its attempt and since', async () => {
    const since = new Date(Date.now() - 21 * MINUTES).toISOString()
    const holders: AccountHolders = {
      account_id: 'eng:laptop',
      viewer: 'owner',
      total: 2,
      holders: [
        { since, task_id: 'task_0123456789abcdef', attempt: 1, recorded: true, verified: true },
        { since, recorded: false, verified: false },
      ],
      others: 0,
    }
    reads.read.mockImplementation(async (target: { url: string }) => {
      if (target.url.includes('/holders')) return ok(holders)
      throw new Error(`unexpected read ${target.url}`)
    })
    api.loadTask.mockResolvedValue(
      ok({ id: 'task_0123456789abcdef', step_id: 'fix-lease-heartbeat', runner_profile: 'claude-code' } as Task),
    )
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: 'Holding now (2)' }, WAIT))

    const link = await screen.findByRole('link', { name: 'fix-lease-heartbeat' }, WAIT)
    expect(link.getAttribute('href')).toBe('#work/task/task_0123456789abcdef')
    // The whole id is one hover away.
    expect(link.getAttribute('title')).toBe('task_0123456789abcdef')
    expect(api.loadTask).toHaveBeenCalledWith('task_0123456789abcdef')

    const table = document.querySelector('table.acct-holding-now')
    expect(table, 'Holding now is not a table').not.toBeNull()
    expect(document.querySelector('.acct-holder-list'), 'the bulleted list is still drawn').toBeNull()
    expect([...table!.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['Agent', 'Attempt', 'Since'])
    const rows = [...table!.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent))
    expect(rows[0]).toEqual(['fix-lease-heartbeat', '1', `${hhmm(since)} (21m)`])
    // A hold that named no task still says so, and has no attempt to give.
    expect(rows[1]).toEqual(['task not recorded', '—', `${hhmm(since)} (21m)`])
    expect(screen.getByRole('heading', { name: 'Holding now · 2' })).toBeTruthy()
  })

  it('keeps the id as the agent when the task cannot be read', async () => {
    const since = new Date(Date.now() - 5 * MINUTES).toISOString()
    reads.read.mockImplementation(async () =>
      ok({
        account_id: 'eng:laptop',
        viewer: 'owner',
        total: 1,
        holders: [{ since, task_id: 'task_gone', attempt: 2, recorded: true, verified: true }],
        others: 0,
      } satisfies AccountHolders),
    )
    api.loadTask.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'gone' },
    })
    await openRow(account({ assigned: 1 }))
    fireEvent.click(await screen.findByRole('tab', { name: 'Holding now (1)' }, WAIT))
    const link = await screen.findByRole('link', { name: 'task_gone' }, WAIT)
    expect(link.getAttribute('href')).toBe('#work/task/task_gone')
  })

  it('gives Last given out the clock it happened at, with its age under it', async () => {
    const at = new Date(Date.now() - 3 * 60 * MINUTES).toISOString()
    await openRow(account({ last_assigned_at: at }))
    const tile = [...document.querySelectorAll('.acct-tiles > .acct-tile')].find(
      (t) => t.querySelector('small')?.textContent === 'Last given out',
    )!
    // MUTATION: print `timeAgo` as the figure again.
    expect(tile.querySelector('b')!.textContent).toBe(hhmm(at))
    expect(tile.querySelector('i')!.textContent).toBe('3h ago')
    expect(tile.querySelector('b')!.getAttribute('title')).toBe(new Date(at).toLocaleString())
  })
})
