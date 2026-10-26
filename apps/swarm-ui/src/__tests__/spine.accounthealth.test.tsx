/**
 * CAPACITY › ACCOUNT HEALTH IS IN THE NAV (ACCOUNT-HEALTH-PAGE, part of #2302).
 *
 * App.tsx SECTIONS gives Capacity an `account-health` tab and routes
 * `capacity/account-health` to the page, but the spine's panel is drawn from
 * `PANEL_PAGES`, not SECTIONS. The review render of PR 2357 found the page
 * loading with no row for it under Capacity and nothing lit: the panel listed
 * Pools, Runtimes, Holders, Accounts and API reads only. Account health sits
 * under Accounts, after Subscription accounts, and opening it lights itself
 * and opens its group.
 *
 * MUTATION: drop the `account-health` kid from PANEL_PAGES.capacity and the
 * first and second cases go red; drop its `rowFor` line and the second does.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { PANEL_PAGES, SkyShell, litPage } from '../Spine'

afterEach(() => cleanup())

describe('Capacity › Account health is in the nav (part of #2302)', () => {
  it('lists Account health under Accounts, after Subscription accounts', () => {
    const accounts = PANEL_PAGES.capacity.find((p) => p.key === 'accounts')
    expect(accounts?.kids?.map((k) => k.key)).toEqual(['accounts', 'account-health', 'quota'])
    const kid = accounts!.kids!.find((k) => k.key === 'account-health')!
    expect(kid.label).toBe('Account health')
    expect(kid.to).toBe('capacity/account-health')
    expect(kid.admin).toBeUndefined()
  })

  it('draws the row and lights it while the page is open', () => {
    expect(litPage('capacity', 'account-health', 'live')).toEqual({ row: 'accounts', kid: 'account-health' })
    render(
      <SkyShell section="capacity" tab="account-health" title="x" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const lit = [...document.querySelectorAll('.sk-panel [aria-current="page"]')]
    expect(lit.map((a) => a.textContent)).toEqual(['Account health'])
    expect(lit[0]!.classList.contains('sk-kid')).toBe(true)
  })
})
