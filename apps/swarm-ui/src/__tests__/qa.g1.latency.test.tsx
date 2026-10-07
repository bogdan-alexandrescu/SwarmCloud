/**
 * G1-15 (QA pass on swarm.saga.xyz, 2026-10-07): A READ'S LATENCY IS WRITTEN
 * AS A DURATION, AND THE ADMIN 403 NOTE IS FOR THE READER IT IS TRUE OF.
 *
 * The dock read `p95 4141ms` and API reads' Took column `1095ms`, `1493ms`:
 * no separator, and never seconds, beside screens that write `2m 17s`. Every
 * latency now goes through `fmtLatency` -- `812 ms` under a second, `4.14 s`
 * from one -- in the dock's strip, its cells and API reads.
 *
 * And the Outcome column's head said "(403 on /v1/admin is expected)" to an
 * administrator, who gets 200 there. The caveat explains a row: it is drawn
 * only while a row it explains is on the screen.
 *
 * MUTATION: put `${ms}ms` back in any of the three places, or draw the
 * parenthetical unconditionally, and a case below goes red.
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { Dock } from '../Dock'
import { forgetProbes, noteFixtureProbe, route } from '../fetch'
import { fmtLatency } from '../panes'

afterEach(() => {
  cleanup()
  forgetProbes()
  window.location.hash = ''
})

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('fmtLatency', () => {
  it('writes milliseconds under a second and seconds from one, with a space', () => {
    expect(fmtLatency(0)).toBe('0 ms')
    expect(fmtLatency(812)).toBe('812 ms')
    expect(fmtLatency(999)).toBe('999 ms')
    expect(fmtLatency(1000)).toBe('1.00 s')
    expect(fmtLatency(1095)).toBe('1.10 s')
    expect(fmtLatency(4141)).toBe('4.14 s')
  })

  it('rounds a fractional millisecond rather than printing its digits', () => {
    expect(fmtLatency(20.4)).toBe('20 ms')
  })
})

describe('the dock writes its latencies with fmtLatency', () => {
  it('p95 in the strip', () => {
    noteFixtureProbe(route('/v1/capacity'), 4141, true)
    const { container } = render(<Dock />)
    const p95 = [...container.querySelectorAll('.ctl-dock-fact')].map((f) => text(f)).find((t) => t.startsWith('p95'))
    expect(p95).toBe('p95 4.14 s ·')
  })
})

/** Open API reads with the given registry, and return its table. */
async function reference(): Promise<HTMLTableElement> {
  window.location.hash = '#reference'
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLTableElement>('.ctl-table.is-scroll table')
    expect(t, 'API reads drew no table').not.toBeNull()
    return t!
  })
}

/** The row whose route cell starts with `path`. */
function row(table: HTMLTableElement, path: string): HTMLTableRowElement {
  const th = [...table.querySelectorAll('tbody th[scope="row"]')].find((h) => h.firstChild?.textContent === path)
  expect(th, `no row for ${path}`).toBeDefined()
  return th!.closest('tr')!
}

describe('API reads', () => {
  it('writes Took with fmtLatency', async () => {
    noteFixtureProbe(route('/v1/g1-slow'), 1493, true, undefined, { frame: true })
    noteFixtureProbe(route('/v1/g1-fast'), 87, true, undefined, { frame: true })
    const table = await reference()
    expect(text(row(table, '/v1/g1-slow').querySelector('td[data-label="Took"]'))).toBe('1.49 s')
    expect(text(row(table, '/v1/g1-fast').querySelector('td[data-label="Took"]'))).toBe('87 ms')
  })

  it('draws no admin-403 caveat when no row is an admin 403 (an administrator’s tab)', async () => {
    noteFixtureProbe(route('/v1/admin/g1-pools'), 40, true, undefined, { frame: true })
    const table = await reference()
    // The fixture frame's own reads may land later; none of them is an admin 403.
    expect(table.querySelector('td[data-label="Outcome"]')).not.toBeNull()
    expect([...table.querySelectorAll('tbody tr')].some((r) => text(r).includes('403 admin only'))).toBe(false)
    expect(text(table.querySelector('thead th:nth-child(3)'))).toBe('Outcome')
  })

  it('says the 403 is expected while an admin-403 row is on screen', async () => {
    noteFixtureProbe(route('/v1/admin/g1-pools'), 40, false, 'admin_required', { frame: true })
    const table = await reference()
    expect(text(table.querySelector('thead th:nth-child(3)'))).toBe('Outcome (403 on /v1/admin is expected)')
  })
})
