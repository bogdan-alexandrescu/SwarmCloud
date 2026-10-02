// WORK › TIMELINE IS TWO PAGES (timeline.html pick A): Lanes at /timeline,
// the default, and Outcomes -- today's ledger, unchanged -- at
// /timeline/outcomes. Every old address of the Timeline still lands.

import { describe, expect, it } from 'vitest'

import { canonical, fromAddress } from '../App'
import { addressToPath, pathToAddress } from '../paths'

describe('the Timeline pages', () => {
  it('serves /timeline/outcomes as the Outcomes page, never as an agent called "outcomes"', () => {
    const p = pathToAddress('/timeline/outcomes')
    expect(p, '/timeline/outcomes is not a path this console serves').not.toBeNull()
    const r = fromAddress(p!.address)
    expect(r.taskId).toBeNull()
    expect(r.sectionId).toBe('work')
    expect(r.tab).toBe('timeline')
    expect(r.page).toBe('outcomes')
    expect(addressToPath(canonical(r))).toBe('/timeline/outcomes')
  })

  it('keeps the Outcomes view on its address, both ways', () => {
    const p = pathToAddress('/timeline/outcomes', '?span=30d&table=1')!
    const r = fromAddress(p.address)
    expect(r.page).toBe('outcomes')
    expect(r.view).toBe('span=30d&table=1')
    expect(addressToPath(canonical(r))).toBe('/timeline/outcomes?span=30d&table=1')
  })

  it('opens /timeline on Lanes, and every old address of the Timeline still lands there', () => {
    for (const address of ['work/timeline', 'history/timeline', 'activity/timeline']) {
      const r = fromAddress(address)
      expect(r.tab, address).toBe('timeline')
      expect(r.page ?? null, address).toBeNull()
      expect(addressToPath(canonical(r)), address).toBe('/timeline')
    }
    const r = fromAddress(pathToAddress('/timeline', '?span=7d')!.address)
    expect(r.page ?? null).toBeNull()
    expect(addressToPath(canonical(r))).toBe('/timeline?span=7d')
  })
})
