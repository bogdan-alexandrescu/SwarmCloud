/**
 * THE PHONE BAR'S HIT AREAS STAY INSIDE THE BAR (lane
 * PHONE-TOP-BAR-HIT-AREAS; QA, seen again 2026-11-09).
 *
 * At 390 every console page was 392px wide: the 44px `::after` boxes under
 * the bar's controls (CH-19) are centred on each control, and `Top`'s, at
 * the end of the row, stood past the bar's 6px padding; a long workspace
 * chip pushed the row, and it, further. The bar now clips on x alone: it is
 * the glass's width, so only what is off the glass is cut, and its 44px-tall
 * boxes still reach above and below the 36px compact bar.
 *
 * jsdom lays nothing out and has no scroll width, so the shell is rendered
 * at 390 with a long chip and the rule the browser applies to that bar and
 * its controls is read off the shipped cascade (cssgate). Each `it` names
 * the mutation that turns it red.
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 } as const

/** A group workspace whose key, drawn in the chip, is long. */
const LONG = { tenant_id: 'platform-reliability-and-research-engineering', display_name: 'Platform reliability and research engineering', kind: 'group' }
const OTHER = { tenant_id: 'research', display_name: 'Research', kind: 'group' }

const realFetch = globalThis.fetch
afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

/** The console shell at 390 for an admin in the long workspace; its phone bar. */
async function bar(): Promise<HTMLElement> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.stubEnv('VITE_SWARM_ENV', 'local')
  vi.resetModules()
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return ok([LONG, OTHER])
    if (url.includes('/v1/tenants/me')) {
      return ok({
        tenant: LONG,
        principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: true },
        environment: 'local',
        environment_declared: true,
      })
    }
    return ok({ pools: [] })
  }) as unknown as typeof fetch
  const { SkyShell } = await import('../Spine')
  window.history.replaceState(null, '', '/config/people')
  const { container } = render(
    <SkyShell section="config" tab="people" title="People and roles" go={() => {}} foot={null}>
      <p>page</p>
    </SkyShell>,
  )
  await waitFor(() => expect(container.querySelector('.sk-pbar .sk-tchip')?.textContent).toBe(LONG.tenant_id), WAIT)
  return container.querySelector<HTMLElement>('.sk-pbar')!
}

/** The bar's controls that draw a 44px `::after` hit area. */
function targets(header: HTMLElement): HTMLElement[] {
  return [...header.querySelectorAll<HTMLElement>(':scope > .sk-ibtn, :scope > .sk-tchip, .sk-top')]
}

describe('the phone bar at 390 with a long workspace chip', () => {
  it('draws the menu, the long chip and Top, each with a hit area', async () => {
    const header = await bar()
    expect(header.classList.contains('has-chip')).toBe(true)
    const names = targets(header).map((el) => el.className.split(' ')[0])
    expect(names).toEqual(['sk-ibtn', 'sk-tchip', 'sk-top'])
  })

  it('clips what stands past its sides, so no page is wider than 390px', async () => {
    // MUTATION: drop `.sk-pbar { overflow-x: clip }` (the hit areas widen the page again).
    const header = await bar()
    expect(painted(header, ['overflow-x', 'overflow'], PHONE)).toBe('clip')
  })

  it('clips on x alone: nothing is cut above or below, and the bar is no scroller', async () => {
    // MUTATION: `overflow: hidden` or `overflow: clip` on `.sk-pbar` (cuts the
    // 44px-tall boxes to the 36px compact bar), or `overflow-x: hidden`/`auto`.
    const header = await bar()
    expect(painted(header, ['overflow-y', 'overflow'], PHONE) ?? 'visible').toBe('visible')
    expect(painted(header, ['overflow-x', 'overflow'], PHONE) ?? 'visible').not.toMatch(/hidden|auto|scroll/)
  })

  it('keeps every hit area 44px wide and 44px tall', async () => {
    // MUTATION: shrink a hit area (`width: 100%`, `height: 36px`) to fit the bar instead.
    const header = await bar()
    for (const el of targets(header)) {
      const at = el.className
      expect(painted(el, 'content', PHONE, 'after'), at).toBe("''")
      expect(painted(el, 'position', PHONE, 'after'), at).toBe('absolute')
      expect(painted(el, 'width', PHONE, 'after'), at).toBe('max(100%, 44px)')
      expect(painted(el, 'height', PHONE, 'after'), at).toBe('44px')
    }
  })
})
