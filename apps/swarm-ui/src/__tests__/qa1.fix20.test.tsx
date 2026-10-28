// CONSOLE VISUAL QA PASS 1, LANE QA1-FIX-20 (part of #2596): the findings
// whose main file is QuotaDetail.tsx or ReachableRepositories.tsx.
//
//   capacity-18  Provider quota's Feeds pool printed the raw pool id
//                (`provider:anthropic:tenant: / eng`, broken mid-id), and at
//                390 an empty line sat between the link and its ceiling note.
//   config-15    The reach table's Remove and a registered card's Remove were
//                one word for two writes (delete the grant; unregister).
//   config-19    The reach table's footnote, a sentence, was set in mono.
//
// Fixtures only; no network.

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Pool, QuotaState } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({ loadAdminQuota: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { QuotaDetailScreen } = await import('../QuotaDetail')
const { ReachableRepositories } = await import('../ReachableRepositories')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const minutesAgo = (m: number): string => new Date(Date.now() - m * 60_000).toISOString()

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function pool(over: Partial<Pool> = {}): Pool {
  return {
    name: 'provider:anthropic:tenant:eng', hard_limit: 5, adaptive_target: null, quota_derived_limit: 50,
    effective_limit: 5, active: 0, available: 5, enabled: true, updated_at: minutesAgo(2), ...over,
  }
}

function quota(over: Partial<QuotaState> = {}): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: minutesAgo(1),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
    retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, feeds_pool: pool(), ...over,
  }
}

function atPhone(): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(max-width: 560px)',
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }))
}

async function feedsCell(): Promise<HTMLElement> {
  api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota()] }))
  render(<QuotaDetailScreen />)
  const [th] = await screen.findAllByRole('rowheader', { name: 'eng' }, WAIT)
  return th!.closest('tr')!.querySelector<HTMLElement>('td[data-label="Feeds pool"]')!
}

describe('capacity-18: Feeds pool names the pool, and its note sits under the name', () => {
  it('prints the pool\'s display name as the link, with the id in its title', async () => {
    const cell = await feedsCell()
    const link = cell.querySelector('a')!
    // MUTATION: print `pool` (the raw id) as the link text again -- the link
    // reads `provider:anthropic:tenant:eng`.
    expect(link.textContent).toBe('anthropic · eng')
    expect(screen.getByRole('link', { name: 'anthropic · eng' })).toBe(link)
    // The id is one hover away, never dropped.
    expect(link.getAttribute('title')).toBe('provider:anthropic:tenant:eng')
    expect(link.getAttribute('href')).toBe(`#capacity/pools?pool=${encodeURIComponent('provider:anthropic:tenant:eng')}`)
  })

  it('keeps the link and its ceiling note one element of a 390 record, so no row gap falls between them', async () => {
    atPhone()
    const cell = await feedsCell()
    expect(cell.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
    // MUTATION: drop the `.quota-feeds-val` wrapper -- the link and the note
    // are two grid items of the stacked cell, the record's row gap between.
    expect(cell.children).toHaveLength(1)
    const val = cell.children[0]!
    expect(val.querySelector('a')!.textContent).toBe('anthropic · eng')
    expect(val.querySelector('.quota-ceiling')!.textContent).toContain('ceiling 5 (configured, below cap)')
  })
})

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`

const GRANT = (n: number, repository: string, mode: 'read' | 'write') => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: {},
  verified_at: null,
})

const GRANTS = [GRANT(1, 'example-org/example-docs', 'read'), GRANT(2, 'example-org/example-service', 'write')]
const rowOf = (repository: string) => document.querySelector(`li.ac-grant[data-repo="${repository}"]`) as HTMLElement

describe('config-15: the reach table\'s Remove says it removes access', () => {
  it('labels the grant row\'s action Remove access, and the ask holds the one Remove access while it is open', () => {
    render(<ReachableRepositories grants={GRANTS as never} onChange={vi.fn()} />)
    const svc = rowOf('example-org/example-service')
    // MUTATION: label it `Remove` again -- the same word as the registered
    // card's unregister below it.
    expect(within(svc).getAllByRole('button').map((b) => b.textContent)).toEqual(['Verify', 'Push test', 'Remove access'])
    fireEvent.click(within(svc).getByRole('button', { name: 'Remove access' }))
    // MUTATION: keep the trigger drawn while its ask is open -- the row then
    // has two `Remove access` buttons, one that asks and one that acts.
    expect(within(rowOf('example-org/example-service')).getAllByRole('button', { name: 'Remove access' })).toHaveLength(1)
    expect(within(rowOf('example-org/example-service')).getByRole('button', { name: 'Keep Write' })).toBeTruthy()
    fireEvent.click(within(rowOf('example-org/example-service')).getByRole('button', { name: 'Keep Write' }))
    expect(within(rowOf('example-org/example-service')).getAllByRole('button').map((b) => b.textContent)).toEqual(['Verify', 'Push test', 'Remove access'])
  })
})

describe('config-19: the reach table\'s footnote is set in the body face', () => {
  it('paints the foot in --font, not --mono', () => {
    render(<ReachableRepositories grants={GRANTS as never} onChange={vi.fn()} />)
    const foot = document.querySelector('.ac-grants > .c-card-foot')!
    expect(foot.textContent).toContain('An agent for a repository not on this list is refused at submission.')
    // MUTATION: drop the `.c-card.ac-grants > .c-card-foot` rule
    // (styles/reachable-repositories.css) -- components.css's mono `font`
    // shorthand wins.
    const face = painted(foot, ['font-family', 'font'], WIDE) ?? ''
    expect(face).toContain('var(--font)')
    expect(face).not.toContain('--mono')
  })
})
