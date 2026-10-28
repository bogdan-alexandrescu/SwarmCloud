// QA1B-11 (part of #2596): QA1-FIX-24's Profiles.tsx half (capacity-13),
// applied by pass 1b.
//
// By runner profile still ended in the `Reading this screen:` row of
// underlined monospace help links (`<HelpLinks>`) that VQ-033 took off Pools,
// Accounts and Provider quota. The row is gone; the one `?` after the title
// opens every topic it listed, `pools-all-at-once` among them.
//
// BREAK IT: put `<HelpLinks topics={PROFILE_TOPICS} />` back after the
// matrix, or set `help="pools-all-at-once"` on the `Screen` again -- a case
// below turns red.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import { HELP, type TopicId } from '../help'
import type { Capacity } from '../types'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
vi.mock('../api', () => ({ loadCapacity }))

const { ProfilesScreen } = await import('../Profiles')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const PROFILE_TOPICS: readonly TopicId[] = ['units-not-agents', 'runner-profile-by-name', 'pools-all-at-once', 'tenant-scope']

function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-28T10:00:00Z',
    pools_complete: true,
    pools: [{
      name: 'global', hard_limit: 40, adaptive_target: null, quota_derived_limit: null,
      effective_limit: 40, active: 1, available: 39, enabled: true, updated_at: '2026-10-28T10:00:00Z',
    }],
    runner_profiles: {
      merge: {
        resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'git', units: 1, pools: ['global'],
        admission: {
          units: 1, headroom: 39, basis: 'measured', blockers: [], binding: ['global'],
          counterfactual: [], complete: true, unread: [], uncapped: [],
        },
      },
    },
  } as Capacity
}

async function shown() {
  loadCapacity.mockResolvedValue({ status: 'ok', data: capacity(), fetchedAt: Date.now(), serverAt: '2026-10-28T10:00:00Z' })
  render(<ProfilesScreen />)
  await waitFor(() => expect(screen.getAllByText('merge').length).toBeGreaterThan(0), WAIT)
}

describe('capacity-13: By runner profile ends in no help-link footer; its topics sit behind the title’s ?', () => {
  it('draws no `Reading this screen:` row', async () => {
    // MUTATION: restore `<HelpLinks topics={PROFILE_TOPICS} />` and the row is back.
    await shown()
    expect(screen.queryByText('Reading this screen:')).toBeNull()
    const head = document.querySelector('.c-phead')!
    const strays = [...document.querySelectorAll('a[href^="#help/"]')].filter((a) => !head.contains(a))
    expect(strays.map((a) => a.getAttribute('href'))).not.toContain(`#${HELP['units-not-agents'].anchor}`)
  })

  it('opens every topic the footer listed, in its order, from the one ? by the title', async () => {
    // MUTATION: `help="pools-all-at-once"` again opens that one topic, not this list.
    await shown()
    const head = document.querySelector<HTMLElement>('.c-phead')!
    const glyphs = within(head).getAllByRole('button', { name: /^Help:/ })
    expect(glyphs.length).toBe(1)
    fireEvent.click(glyphs[0]!)
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    const hrefs = [...dialog.querySelectorAll('a[href^="#help/"]')].map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(PROFILE_TOPICS.map((id) => `#${HELP[id].anchor}`))
  })
})
