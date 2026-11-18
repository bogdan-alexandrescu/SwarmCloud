/**
 * A HELP TOPIC ID THAT SPELLS A SECTION ID IS STILL A TOPIC.
 *
 * help.ts has a topic `capacity` (group `the-platform`); `capacity` is also a
 * section id, so `/help/the-platform#capacity` was read as a legacy route and
 * opened the Capacity screen. On `/help/...` a bare `#topic` is an in-page
 * anchor. The only topic id that collides with a LEGACY head today is
 * `capacity`; this also holds every topic id against the heads, so a new
 * collision is caught where it is added.
 *
 * MUTATION: drop the `/help/` guard in isLegacyHash and the first three cases
 * go red.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

import { App, LEGACY_HEADS, fromLocation } from '../App'
import { HELP } from '../help'
import { isLegacyHash } from '../paths'

afterEach(() => {
  cleanup()
  window.history.pushState(null, '', '/')
})

describe('a help topic named like a section', () => {
  it('is an anchor, not a legacy route, under /help/', () => {
    expect(isLegacyHash('#capacity', LEGACY_HEADS, '/help/the-platform')).toBe(false)
    // The same hash elsewhere is still the old route.
    expect(isLegacyHash('#capacity', LEGACY_HEADS, '/')).toBe(true)
    expect(isLegacyHash('#capacity', LEGACY_HEADS)).toBe(true)
  })

  it('resolves /help/the-platform#capacity to Help, group the-platform, topic capacity', () => {
    window.history.pushState(null, '', '/help/the-platform#capacity')
    const r = fromLocation()
    expect(r.sectionId).toBe('help')
    expect(r.tab).toBe('capacity')
  })

  it('stays on Help when an in-page #capacity anchor is clicked', () => {
    window.history.pushState(null, '', '/help/the-platform#capacity')
    render(<App />)
    const a = document.createElement('a')
    a.setAttribute('href', '#capacity')
    a.textContent = 'anchor'
    document.body.appendChild(a)
    fireEvent.click(a)
    expect(window.location.pathname).toBe('/help/the-platform')
    a.remove()
  })

  it('has no topic id that collides with a legacy head, `capacity` aside', () => {
    const collisions = Object.keys(HELP).filter((id) => LEGACY_HEADS.includes(id))
    expect(collisions).toEqual(['capacity'])
  })
})
