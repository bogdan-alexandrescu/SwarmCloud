// CONSOLE VISUAL QA PASS 1, LANE QA1-FIX-19 (2026-10-28, part of #2596 and
// #2391): Admin › Tenants › QA identity, and Capacity › Pools › By runner
// profile's Runs out first.
//
//   admin-shell-09  An API without the QA identity routes (a 404 with no
//                   code -- an older swarm-api, or the fixture build) drew
//                   "This API serves no QA identity routes.": the server's
//                   view, with no next step. It now says the grants cannot
//                   be read from this deployment, why, and what to do.
//   chrome-C22      A grant's time was the raw ISO stamp with microseconds,
//                   `2026-10-27T11:41:37.874308+00:00`. It is now the
//                   console's date and time in the person's zone, labelled
//                   (`formatDateTime`), the stamp kept in `<time dateTime>`.
//   capacity-15     A Runs out first entry was held to 21ch and cut its pool
//                   name -- `CLOUD_R… (5/20)` -- while the table had room, and
//                   its `fits N` was set in DM Mono, whose f reads as ƒ. The
//                   entry now sizes to its content; when the table is
//                   cramped the NAME wraps and the entry wraps between its
//                   pieces (never inside its use or its fits), and `fits`
//                   is in the body font.
//
// MUTATIONS, each of which turns a case red: put the old sentence back in the
// 404 branch (admin-shell-09); print `grant.granted_at` as given
// (chrome-C22); delete styles/profile-matrix.css's entry rule, or set the
// name back to `overflow: hidden; text-overflow: ellipsis` (capacity-15,
// name); set the fits back to `var(--mono)` (capacity-15, font).
//
// Nothing here is credential-shaped: no token is needed, and none is built.

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, ProfileAdmission, RunnerProfile } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadQaIdentities: vi.fn(),
  grantQaIdentity: vi.fn(),
  revokeQaIdentity: vi.fn(),
  loadProfile: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { QaIdentityGrants } = await import('../QaIdentityGrants')
const { ProfileMatrix } = await import('../ProfileMatrix')

const WAIT = { timeout: 4000 } as const
const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const ADMIN = ['admin', 'example.com'].join('@')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function unread<T>(httpStatus: number, code: string | null, message: string): Result<T> {
  return { status: 'error', error: { kind: 'not_found', httpStatus, code, message } } as unknown as Result<T>
}

function panel(): HTMLElement {
  return document.querySelector<HTMLElement>('[data-testid="qa-identity"]')!
}

async function mount(answer: Result<unknown>): Promise<HTMLElement> {
  api.loadQaIdentities.mockResolvedValue(answer)
  api.loadProfile.mockResolvedValue(unread(503, null, 'unread'))
  api.loadMe.mockResolvedValue(unread(503, null, 'unread'))
  render(<QaIdentityGrants tenants={['eng', 'research']} />)
  await waitFor(() => expect(visible(panel())).not.toContain('Reading…'), WAIT)
  return panel()
}

describe('admin-shell-09: an API without the QA identity routes is said for a reader', () => {
  it('says the grants cannot be read here, why, and what to do -- not the server’s view', async () => {
    const p = await mount(unread(404, null, 'Not Found'))
    const text = visible(p)
    expect(text).not.toContain('This API serves no QA identity routes')
    expect(text).toContain('QA identity grants cannot be read from this deployment')
    // Why, and the next step: the routes come with a newer swarm-api release.
    expect(text).toMatch(/swarm-api/)
    expect(text).toMatch(/Deploy the current release/)
    // Information, not a failure: no red alert.
    expect(p.querySelector('[role="alert"]')).toBeNull()
  })

  it('still draws a coded 404 as the API’s own words (the control)', async () => {
    const p = await mount(unread(404, 'not_found', 'No such identity.'))
    expect(p.querySelector('[role="alert"]')?.textContent).toContain('No such identity.')
    expect(visible(p)).not.toContain('cannot be read from this deployment')
  })
})

describe('chrome-C22: a grant’s time is the console’s date and time, not a raw ISO stamp', () => {
  const STAMP = '2026-10-27T11:41:37.874308+00:00'

  it('prints `Oct 27 11:41:37 UTC` and keeps the stamp in the element’s dateTime', async () => {
    const p = await mount(
      ok({
        identities: [
          {
            identity: 'console',
            host: 'console.example.com',
            account: 'qa',
            configured: true,
            grants: [{ tenant_id: 'eng', state: 'granted', granted_by: ADMIN, granted_at: STAMP, reason: 'visual QA' }],
          },
        ],
      }),
    )
    await screen.findByText('visual QA', { exact: false }, WAIT)
    const row = p.querySelector<HTMLElement>('[data-grant-tenant="eng"]')!
    expect(visible(row)).not.toContain(STAMP)
    expect(visible(row)).not.toMatch(/T\d\d:\d\d/)
    expect(visible(row)).toContain(`granted by ${ADMIN} at Oct 27 11:41:37 UTC`)
    expect(row.querySelector('time')?.getAttribute('dateTime')).toBe(STAMP)
  })
})

// ---------------------------------------------------------------------------
// capacity-15
// ---------------------------------------------------------------------------

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-28T10:00:00Z',
  }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 1, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: null, units: 1, pools: [], ...over }
}

/** The fixture console's two cut entries: a provider pool at 5/5 and the Cloud Run backend at 5/20. */
const CAPACITY: Capacity = {
  tenant_id: 'eng',
  generated_at: '2026-10-28T10:00:00Z',
  pools: [pool('global', 3, 40), pool('provider:anthropic:tenant:u-bogdan', 5, 5), pool('backend:CLOUD_RUN_JOB', 5, 20)],
  runner_profiles: {
    'claude-code': profile({
      pools: ['global', 'provider:anthropic:tenant:u-bogdan'],
      admission: admission({ headroom: 0, binding: ['provider:anthropic:tenant:u-bogdan'] }),
    }),
    codex: profile({
      pools: ['global', 'backend:CLOUD_RUN_JOB'],
      admission: admission({ headroom: 15, binding: ['backend:CLOUD_RUN_JOB'] }),
    }),
  },
}

function entries(): HTMLElement[] {
  render(<ProfileMatrix capacity={CAPACITY} />)
  const found = [...document.querySelectorAll<HTMLElement>('.cap-mx td.cap-mx-first > .cap-mx-bind')]
  expect(found.length, 'the matrix drew no Runs out first entries').toBe(2)
  return found
}

describe('capacity-15: Runs out first names the whole pool and sets fits in the body font', () => {
  it('sizes each entry to its content, not a 21ch box, at 400 and 1440', () => {
    let visited = 0
    for (const env of [PHONE, WIDE]) {
      for (const e of entries()) {
        expect(painted(e, 'inline-size', env), `${e.textContent}: the entry is held to a fixed width`).toBe('auto')
        expect(painted(e, 'max-inline-size', env)).toBe('100%')
        visited++
      }
      cleanup()
    }
    expect(visited).toBe(4)
  })

  it('wraps a pool name that does not fit instead of cutting it, and never wraps the use and fits', () => {
    let visited = 0
    for (const env of [PHONE, WIDE]) {
      for (const e of entries()) {
        const name = e.querySelector<HTMLElement>(':scope > .cap-mx-bind-pool')!
        expect(painted(name, 'overflow', env), `${name.textContent} is clipped`).toBe('visible')
        expect(painted(name, 'text-overflow', env)).toBe('clip')
        expect(painted(name, 'white-space', env)).toBe('normal')
        expect(painted(name, 'overflow-wrap', env)).toBe('anywhere')
        // The whole name is in the words, not only in a title.
        expect(name.textContent).not.toContain('…')
        // The use and the fits: each one unbroken piece that never gives way;
        // a cramped entry wraps between pieces, never inside one.
        expect(painted(e, 'white-space', env)).toBe('nowrap')
        expect(painted(e, 'flex-wrap', env)).toBe('wrap')
        for (const tail of e.querySelectorAll<HTMLElement>(':scope > .cap-mx-bind-use, :scope > small')) {
          expect(painted(tail, 'flex-shrink', env), `${tail.textContent} gives way to the name`).toBe('0')
        }
        visited++
      }
      cleanup()
    }
    expect(visited).toBe(4)
  })

  it('sets `fits N` in the body font, upright, at the 12px micro size', () => {
    let visited = 0
    for (const e of entries()) {
      const fits = e.querySelector<HTMLElement>(':scope > small')!
      expect(fits.textContent).toMatch(/^fits \d+$/)
      const font = painted(fits, 'font', WIDE) ?? ''
      expect(font, 'fits is set in DM Mono, whose f reads as ƒ').not.toContain('--mono')
      // The shorthand resets font-style to normal: nothing italic is inherited.
      expect(font).toBe('var(--t-micro)/var(--lh-micro) var(--font)')
      visited++
    }
    expect(visited).toBe(2)
  })
})
