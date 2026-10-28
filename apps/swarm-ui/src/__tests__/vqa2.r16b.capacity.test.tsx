// VISUAL QA RE-RUN, LANE R16, STEP r16-capacity-profiles (2026-10-27, part of
// #1427): Pools' two views, Ceilings and By runner profile, before their data
// and when their data is an empty pool list. Each case asserts the property
// the finding was about -- the words a reader is given, and which controls
// are still on the page -- not the markup's shape.
//
//   V199  One empty capacity read is worded once, on both views, and both
//         keep the Ceilings / By runner profile switch.
//   V063  (capacity half) While the first read is in flight both views draw
//         the switch and the count note's place, so neither jumps on landing.
//   V197  By runner profile's count and read age sit above the matrix, not
//         under its inner scroller: nothing about the read is below the box.
//
// BREAK IT: give either screen its own `empty` wording again -- the V199
// wording case fails; drop `<CapSeg>` from `NoPools`'s caller -- the switch
// case fails; drop `skeleton=` from either screen -- the V063 case fails on
// that screen; put the `.provenance` line back under `<ProfileMatrix>` -- the
// V197 case fails.

import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadLeases: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { ProfilesScreen } = await import('../Profiles')

const WAIT = { timeout: 5000 }

const VIEWS = [
  { name: 'Ceilings', screen: () => <CapacityScreen />, current: '#capacity/pools' },
  { name: 'By runner profile', screen: () => <ProfilesScreen />, current: '#capacity/profiles' },
] as const

function empty(): Result<Capacity> {
  return { status: 'empty', fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-27T10:00:00Z',
  }
}

function profile(pools: string[]): RunnerProfile {
  return { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'git', units: 1, pools }
}

function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: new Date().toISOString(),
    pools_complete: true,
    pools: [pool('global', 1, 40), pool('tenant:eng', 1, 20)],
    runner_profiles: {
      merge: profile(['global', 'tenant:eng']),
      probe: profile(['global']),
    },
  }
}

/** The view strip's links, as `href -> aria-current`. */
function viewSwitch(root: ParentNode): Record<string, string | null> {
  const out: Record<string, string | null> = {}
  for (const a of root.querySelectorAll<HTMLAnchorElement>('.cap-seg a[href^="#capacity/"]')) {
    out[a.getAttribute('href')!] = a.getAttribute('aria-current')
  }
  return out
}

afterEach(() => {
  cleanup()
  api.loadCapacity.mockReset()
  api.loadLeases.mockReset()
})

describe('V199: one empty capacity read is one empty state on both views', () => {
  async function emptyPanel(view: (typeof VIEWS)[number]): Promise<{ heading: string; body: string; root: HTMLElement }> {
    api.loadCapacity.mockResolvedValue(empty())
    api.loadLeases.mockResolvedValue(empty())
    const { container, unmount } = render(view.screen())
    let panel: Element | null = null
    await waitFor(() => {
      panel = container.querySelector('.ctl-empty')
      expect(panel, `${view.name} drew no empty state`).not.toBeNull()
    }, WAIT)
    const el = panel as unknown as Element
    const heading = el.querySelector('h3')?.textContent ?? ''
    const body = el.querySelector('p')?.textContent ?? ''
    const root = container.cloneNode(true) as HTMLElement
    unmount()
    return { heading, body, root }
  }

  it('words the empty read the same way on Ceilings and on By runner profile', async () => {
    const ceilings = await emptyPanel(VIEWS[0])
    const profiles = await emptyPanel(VIEWS[1])
    expect(ceilings.heading, 'the two views head one empty read differently').toBe(profiles.heading)
    expect(ceilings.body, 'the two views explain one empty read differently').toBe(profiles.body)
    expect(ceilings.body, 'the shared wording has no sentence').not.toBe('')
  })

  for (const view of VIEWS) {
    it(`${view.name}: the empty state keeps the view switch, on its own view`, async () => {
      const { root } = await emptyPanel(view)
      const links = viewSwitch(root)
      expect(Object.keys(links).sort(), `${view.name}'s empty state dropped the view switch`).toEqual([
        '#capacity/pools',
        '#capacity/profiles',
      ])
      expect(links[view.current]).toBe('page')
      // The switch sits ABOVE the panel, where it is on a loaded page.
      const seg = root.querySelector('.cap-seg')!
      const panel = root.querySelector('.ctl-empty')!
      expect(seg.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
  }
})

describe('V063 (capacity half): the first read draws the page it will land as', () => {
  for (const view of VIEWS) {
    it(`${view.name}: while loading, the view switch and the count note's place are drawn`, async () => {
      // A read that never lands: the screen stays on its first `loading`.
      api.loadCapacity.mockReturnValue(new Promise(() => {}))
      api.loadLeases.mockReturnValue(new Promise(() => {}))
      const { container } = render(view.screen())
      await waitFor(() => expect(container.querySelector('.ctl-skel-card'), `${view.name} drew no skeleton`).not.toBeNull(), WAIT)
      const links = viewSwitch(container)
      expect(Object.keys(links).sort(), `${view.name}'s skeleton has no view switch`).toEqual([
        '#capacity/pools',
        '#capacity/profiles',
      ])
      expect(links[view.current]).toBe('page')
      const note = container.querySelector('.c-count-note')
      expect(note, `${view.name}'s skeleton leaves out the count note, so the page jumps when it lands`).not.toBeNull()
      // Note, then switch, then body: the order the loaded page draws them in.
      const seg = container.querySelector('.cap-seg')!
      const card = container.querySelector('.ctl-skel-card')!
      expect(note!.compareDocumentPosition(seg) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(seg.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
  }
})

describe('V197: By runner profile says what it read above the matrix, not under its scroller', () => {
  it('carries the catalogue count and the read age in the count note, and nothing of them after the matrix', async () => {
    api.loadCapacity.mockResolvedValue({ status: 'ok', data: capacity(), fetchedAt: Date.now(), serverAt: new Date().toISOString() })
    const { container } = render(<ProfilesScreen />)
    await waitFor(() => expect(container.querySelector('.cap-mx tr[data-profile="merge"]')).not.toBeNull(), WAIT)
    const note = container.querySelector('.c-count-note')!
    expect(note.textContent).toContain('2 profiles')
    expect(note.textContent).toContain('the whole catalogue')
    expect(note.querySelector('time'), 'the read age is not in the count note').not.toBeNull()
    const mx = container.querySelector('.cap-mx')!
    expect(note.compareDocumentPosition(mx) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const after = [...container.querySelectorAll('.provenance, time')].filter(
      (el) => mx.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING,
    )
    expect(after.map((el) => el.textContent), 'the read is described under the matrix').toEqual([])
  })
})
