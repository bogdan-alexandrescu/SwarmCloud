/**
 * G1-14 (QA pass on swarm.saga.xyz, 2026-10-07): OVERVIEW'S "ON THIS PAGE"
 * LINKS CAN BE SHARED, AND LIST EVERY CARD.
 *
 * A click scrolled (or, in a hidden tab, did not: a smooth scroll is not
 * animated there) but `preventDefault` kept the hash off the address, so a
 * section could not be linked. And Cost so far (`#ov-spend`) and Pools
 * (`#ov-pools`) were on the page and not in the list.
 *
 * MUTATION: drop the `replaceState`, scroll smoothly in a hidden tab, or
 * take a card out of `OVERVIEW_JUMPS`, and a case below goes red.
 */
import OVERVIEW from '../Overview.tsx?raw'
import REGIONS from '../OverviewRegions.tsx?raw'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OVERVIEW_JUMPS, SkyShell } from '../Spine'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

/** The `id="ov-…"` of every section Overview draws, in source order. */
function sectionIds(): string[] {
  const ids = [...`${OVERVIEW}\n${REGIONS}`.matchAll(/<section[^>]*\sid="(ov-[a-z]+)"/g)].map((m) => m[1]!)
  // The band is drawn by `LifecycleBand` in OverviewRegions, between the lead and the columns.
  const inPage = (id: string) => OVERVIEW.indexOf(id === 'ov-band' ? '<LifecycleBand stats=' : `id="${id}"`)
  return [...new Set(ids)].sort((a, b) => inPage(a) - inPage(b))
}

describe('G1-14: On this page', () => {
  it('lists every section Overview draws, in the order it draws them', () => {
    const ids = sectionIds()
    expect(ids).toContain('ov-spend')
    expect(ids).toContain('ov-pools')
    expect(OVERVIEW_JUMPS.map((j) => j.id)).toEqual(ids)
    expect(OVERVIEW_JUMPS.find((j) => j.id === 'ov-spend')!.label).toBe('Cost so far')
    expect(OVERVIEW_JUMPS.find((j) => j.id === 'ov-pools')!.label).toBe('Pools')
  })

  function shell() {
    window.history.replaceState(null, '', '/overview')
    const go = vi.fn()
    render(
      <SkyShell section="overview" tab="now" title="Overview" go={go} foot={null}>
        {OVERVIEW_JUMPS.map((j) => (
          <section key={j.id} id={j.id} />
        ))}
      </SkyShell>,
    )
    return go
  }

  function link(label: string): HTMLAnchorElement {
    const a = [...document.querySelectorAll<HTMLAnchorElement>('a.sk-pk')].find((x) => x.textContent === label)
    expect(a, `no "${label}" link`).toBeDefined()
    return a!
  }

  it('puts the section on the address, in place, so it can be linked', () => {
    const go = shell()
    const scroll = vi.fn()
    const target = document.getElementById('ov-failures')!
    target.scrollIntoView = scroll
    const before = window.history.length
    fireEvent.click(link('Recent failures'))
    expect(window.location.pathname).toBe('/overview')
    expect(window.location.hash).toBe('#ov-failures')
    expect(window.history.length, 'a jump added a history entry').toBe(before)
    expect(scroll).toHaveBeenCalledTimes(1)
    expect(go).not.toHaveBeenCalled()
  })

  it('jumps without animating in a hidden tab, where a smooth scroll does not run', () => {
    shell()
    const scroll = vi.fn()
    document.getElementById('ov-pools')!.scrollIntoView = scroll
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    fireEvent.click(link('Pools'))
    expect(scroll).toHaveBeenCalledWith({ block: 'start', behavior: 'auto' })
    expect(window.location.hash).toBe('#ov-pools')
  })

  it('animates in a tab that can be seen', () => {
    shell()
    const scroll = vi.fn()
    document.getElementById('ov-spend')!.scrollIntoView = scroll
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    fireEvent.click(link('Cost so far'))
    expect(scroll).toHaveBeenCalledWith({ block: 'start', behavior: 'smooth' })
  })
})
