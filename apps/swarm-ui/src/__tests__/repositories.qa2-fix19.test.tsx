// CONSOLE VISUAL QA PASS 2, Config › Repositories (QA2-FIX-19; part of
// #2694 and #2391). Each case renders the fixture console's page (api.ts
// fixtures; USE_FIXTURES is on in this build) and holds one finding:
//
//   * config2-07: the read row's Push, Pull request and Push test say
//     "— read grant", the dash and its reason both seen (config-e.html S3a);
//   * config2-10: the reach table's actions sit on one line in every row at
//     1440 (the track holds Verify, Push test and Remove access; no wrap);
//   * config2-01: the card's permission line names the permission missing and
//     links where it is granted;
//   * config2-08: one primary add action, the lead's; the head draws no
//     second Register repository beside it;
//   * config2-09: the reach table and the cards list the repositories in one
//     order, by full name (a failure still leads the table);
//   * config2-11: Tests mapped's count is in the body face, not mono;
//   * config2-12: the tenant's index token is "the workspace token".
//
// Fixtures only; no network.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { AccessGrant } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { visible } from './repofixture'

const WAIT = { timeout: 6000 }
const WIDE: CascadeEnv = { width: 1440 }
const PAYMENTS = 'example-org/example-payments-reconciliation-service-monorepo'

const reach = () => document.querySelector<HTMLElement>('.ac-grants')
const rows = () => Array.from(document.querySelectorAll<HTMLElement>('li.ac-grant'))
const rowOf = (repository: string) => document.querySelector<HTMLElement>(`li.ac-grant[data-repo="${repository}"]`)
const cards = () => Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card:not(.is-chosen)'))
const cardOf = (name: string) => cards().find((c) => visible(c.querySelector('h2')) === name) ?? null

async function mount() {
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  const { RepositoriesScreen } = await import('../Repositories')
  const go = vi.fn()
  render(<RepositoriesScreen view={null} go={go} />)
  await waitFor(() => expect(rows()).toHaveLength(3), WAIT)
  await waitFor(() => expect(cards()).toHaveLength(3), WAIT)
  return go
}

describe('config2-07: a check a read grant does not need says so beside its dash', () => {
  it('the read row reads "— read grant" under Push, Pull request and Push test', async () => {
    await mount()
    const row = rowOf(PAYMENTS)!
    expect(row.dataset.repo).toBe(PAYMENTS)
    for (const check of ['push', 'pull_request', 'push_test']) {
      const cell = row.querySelector<HTMLElement>(`[data-check="${check}"]`)!
      // The dash keeps its reason as its name.
      expect(cell.querySelector('.c-dash')?.getAttribute('aria-label')).toMatch(/read grant/)
      // MUTATION: draw the bare Dash again (no words beside it) and this is "—".
      expect(visible(cell).replace(/^(Push test|Push|Pull request) ?/, '')).toBe('— read grant')
    }
    // A write row's cells are untouched: a mark and a word, no read-grant note.
    expect(rowOf('example-org/example-api')!.querySelector('[data-why]')).toBeNull()
  })
})

describe('config2-10: the reach table\'s actions on one line in every row', () => {
  it('paints the action cell nowrap in a track that holds Verify, Push test and Remove access', async () => {
    await mount()
    const acts = rowOf('example-org/example-api')!.querySelector('.ac-grant-acts')!
    expect([...acts.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Verify', 'Push test', 'Remove access'])
    // MUTATION: drop `.ac-reach.has-registered .ac-grant-acts { flex-wrap: nowrap }` (repositories.css)
    // and onboarding.css' `wrap` wins: Remove access drops under the other two.
    expect(painted(acts, 'flex-wrap', WIDE)).toBe('nowrap')
    // MUTATION: put the 216px track back and the three buttons (~244px) no longer fit it.
    const track = painted(rowOf('example-org/example-api')!, 'grid-template-columns', WIDE) ?? ''
    expect(track.trim().split(/\s+/).pop()).toBe('230px')
    expect(painted(document.querySelector('.ac-grid-h')!, 'grid-template-columns', WIDE)).toBe(track)
  })

  it('keeps the name track as wide as main gave it at 1440, so "example-org/example-api" stays on one line', async () => {
    await mount()
    const row = rowOf('example-org/example-api')!
    const tracks = (painted(row, 'grid-template-columns', WIDE) ?? '').replace(/repeat\((\d+),\s*(\d+px)\)/g, (_, n: string, w: string) => Array(Number(n)).fill(w).join(' '))
    const fixed = tracks.trim().replace(/^minmax\([^)]*\)\s*/, '').split(/\s+/).reduce((sum, t) => sum + Number.parseFloat(t), 0)
    const gap = Number.parseFloat(painted(row, 'column-gap', WIDE) ?? '10')
    // Main's tracks: 112 + 4 x 84 + 104 + 216 and seven 10px gaps = 838px
    // beside the name, which left it 173px at 1440 (the review measured it);
    // "example-org/example-api" needs that to sit on one line. The actions
    // track's width comes from tightening its buttons, never from the name.
    // MUTATION: put back the 256px track with its 8px gaps (864px) and the
    // name drops to 147px and breaks mid-name ('example-org/example-' / 'api').
    expect(fixed + 7 * gap).toBeLessThanOrEqual(838)
    // The three buttons fit 230px only at their tightened padding and gap
    // (Verify, Push test, Remove access at 12px: ~163px of words, 36px of
    // padding, 6px of borders, 8px of gaps).
    const acts = row.querySelector('.ac-grant-acts')!
    // MUTATION: drop the 6px padding rule and the buttons take 24px more than the track.
    for (const b of acts.querySelectorAll('button')) expect(painted(b, ['padding-inline', 'padding'], WIDE)).toMatch(/^0(px)? 6px$/)
    expect(painted(acts, ['column-gap', 'gap'], WIDE)).toBe('4px')
  })
})

describe('config2-01: the permission line names the permission and where it is granted', () => {
  it("example-api's card says Actions (read) is not granted, and links to Workspace setup", async () => {
    const go = await mount()
    const card = () => cardOf('example-org/example-api')
    await waitFor(() => expect(card()?.querySelector('[data-perm-word]')).not.toBeNull(), WAIT)
    const line = card()!.querySelector<HTMLElement>('[data-perm-word]')!
    // MUTATION: say "1 GitHub permission missing" again and the permission is unnamed.
    expect(visible(line)).toBe('Not granted on GitHub: Actions (read) Grant it in Workspace setup')
    const link = line.querySelector<HTMLAnchorElement>('a')!
    // MUTATION: drop the link and this is null.
    expect(link.getAttribute('href')).toBe('/config/workspace/setup')
    link.click()
    expect(go).toHaveBeenLastCalledWith('config/workspace?part=setup')
  })
})

describe('config2-08: one primary add action', () => {
  it('every chosen repository registered: the lead carries Choose more on GitHub, and the head no Register repository', async () => {
    await mount()
    const lead = () => document.querySelector<HTMLElement>('section[aria-label="Next step"]')
    await waitFor(() => expect(visible(lead())).toContain('Every chosen repository is registered'), WAIT)
    const head = document.querySelector<HTMLElement>('.c-phead')!
    // MUTATION: draw the head's Register repository whatever the lead says and this finds it.
    expect([...head.querySelectorAll('a, button')].map((e) => visible(e))).not.toContain('Register repository')
    expect([...lead()!.querySelectorAll('a')].map((e) => visible(e))).toEqual(['Choose more on GitHub'])
  })
})

describe('config2-09: one order on the page, by full name', () => {
  it('the reach table and the cards list the three repositories alike', async () => {
    await mount()
    const want = ['example-org/example-api', 'example-org/example-payments-reconciliation-service-monorepo', 'example-org/example-web']
    // MUTATION: sort the table by every rank again and example-web (not verified yet) leads it.
    expect(rows().map((r) => r.dataset.repo)).toEqual(want)
    // MUTATION: draw the cards in the order served and this is api, web, payments.
    expect(cards().map((c) => visible(c.querySelector('h2')))).toEqual(want)
    expect(reach()).not.toBeNull()
  })

  it('a failure still leads the table', async () => {
    const { reachOrder } = await import('../ReachableRepositories')
    const g = (repository: string, push: string) =>
      ({ repository, mode: 'write', checks: { clone: { state: 'ok' }, push: { state: push }, pull_request: { state: 'ok' } } }) as unknown as AccessGrant
    const sorted = [g('example-org/a', 'ok'), g('example-org/c', 'missing'), g('example-org/b', 'unknown')].sort(reachOrder)
    // MUTATION: sort by name only and example-org/c (the failure) goes last.
    expect(sorted.map((x) => x.repository)).toEqual(['example-org/c', 'example-org/a', 'example-org/b'])
  })
})

describe('config2-11: Tests mapped is set in the body face', () => {
  it("paints the card's modules count in --font, with tabular figures", async () => {
    await mount()
    const count = cardOf('example-org/example-api')!.querySelector('.ur-tm > b')!
    expect(visible(count)).toBe('4 of 6 modules')
    // MUTATION: put `font-family: var(--mono)` back on `.ur-tm > b` (repositories.css).
    const face = painted(count, ['font-family', 'font'], WIDE) ?? ''
    expect(face).toContain('var(--font)')
    expect(face).not.toContain('--mono')
    expect(painted(count, 'font-variant-numeric', WIDE)).toBe('tabular-nums')
  })
})

describe('config2-12: the tenant token is named by its scope', () => {
  it('every card says "Indexing uses the workspace token", never "team"', async () => {
    await mount()
    for (const c of cards()) {
      // MUTATION: say "the team token" again in indexCredentialWords (RepositoriesData.ts).
      expect(visible(c.querySelector('.ur-cred'))).toBe('Indexing uses the workspace token')
    }
  })
})
