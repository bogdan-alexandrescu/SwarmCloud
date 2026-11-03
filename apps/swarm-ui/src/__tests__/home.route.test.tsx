// HOME IS THE CONSOLE'S ROOT AND THE RAIL'S FIRST ENTRY, ABOVE OVERVIEW (the
// owner's pick of the 'desk' mock-up; part of #2688). Bookmarked /overview
// still opens Overview.

import { describe, expect, it } from 'vitest'

import { HOME, canonical, fromAddress, fromLocation, spineOf } from '../App'
import { HOME_JUMPS } from '../Home'
import { hrefOf } from '../Spine'

describe('Home is the root and the first menu entry; Overview keeps its route', () => {
  it('the root opens Home and /overview still opens Overview', () => {
    window.history.replaceState(null, '', '/')
    expect(fromLocation().sectionId).toBe(HOME)
    window.history.replaceState(null, '', '/overview')
    expect(fromLocation().sectionId).toBe('overview')
    expect(canonical(fromAddress(HOME))).toBe(HOME)
    expect(spineOf(HOME)).toBe('home')
    expect(hrefOf('home')).toBe('/')
    window.history.replaceState(null, '', '/')
  })

  it("Home's panel jumps to the page's own regions, in the order it draws them", () => {
    expect(HOME_JUMPS.map((j) => j.id)).toEqual(['home-needs', 'home-work', 'home-finished', 'home-start'])
  })
})
