// QA-KIT (part of #1649): the console names the 390-wide phone.
//
// Owner decision 2026-10-19: the gate's preset list gains `phone-390`
// (390x844, iPhone 15's mobile flags), and every place the console names a
// preset reads it as "Phone 390" -- the Browser tab's chips and caption
// (LiveBrowser.tsx), the theater's device column (Theater.tsx) and its
// timeline (TheaterTimeline.tsx), which keep their own copies of the labels
// because of the import cycle TheaterTimeline.tsx explains. A copy left behind
// would show the bare name `phone-390` on one screen and "Phone 390" on another.

import { describe, expect, it } from 'vitest'

import { presetLabel } from '../LiveBrowser'
import { DEVICE_CHIPS } from '../Theater'
import { actionWords } from '../TheaterTimeline'

describe('QA-KIT: the phone-390 preset has one name on every screen', () => {
  it('the Browser tab labels it "Phone 390"', () => {
    expect(presetLabel('phone-390')).toBe('Phone 390')
  })

  it('the theater offers it as a chip, after iPhone 15, in the gate’s order', () => {
    expect(DEVICE_CHIPS.map(([name]) => name)).toEqual(['desktop', 'iphone-15', 'phone-390', 'phone-380', 'pixel-8', 'iphone-se', 'ipad-7'])
    expect(DEVICE_CHIPS.find(([name]) => name === 'phone-390')?.[1]).toBe('Phone 390')
  })

  it('the timeline says it by the same name', () => {
    expect(
      actionWords({ at: '2026-10-19T22:00:00Z', kind: 'control', event: 'device_changed', preset: 'phone-390', orientation: 'portrait', touch: true }),
    ).toBe('A person switched the device to Phone 390, portrait, touch on')
  })
})

// IMPLEMENT-RENDER (#2442, BGD 2026-10-26): `phone-380`, phone-390 ten pixels
// narrower, for a repository whose frames are drawn at 380. Same rule: one
// name on every screen.
describe('IMPLEMENT-RENDER: the phone-380 preset has one name on every screen', () => {
  it('the Browser tab, the theater and its timeline all say "Phone 380"', () => {
    expect(presetLabel('phone-380')).toBe('Phone 380')
    expect(DEVICE_CHIPS.find(([name]) => name === 'phone-380')?.[1]).toBe('Phone 380')
    expect(
      actionWords({ at: '2026-10-26T10:00:00Z', kind: 'control', event: 'device_changed', preset: 'phone-380', orientation: 'portrait', touch: true }),
    ).toBe('A person switched the device to Phone 380, portrait, touch on')
  })
})
