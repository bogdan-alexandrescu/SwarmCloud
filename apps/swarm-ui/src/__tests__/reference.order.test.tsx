// ADMIN › API READS PUTS THE BROKEN ROUTE FIRST (#140).
//
// The registry is kept in route order (`probeSnapshot`, fetch.ts), which is
// right for a registry and wrong for the screen: a reader opens API reads
// because something did not load, and with twenty routes read the failing one
// was wherever its name sorted. The screen orders by outcome -- failing, then
// paused or degraded, then the expected admin 403, then ok -- and within each,
// the payload that is oldest (or never arrived) first.
//
// Every block here was written before the change it demands.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { noteFixtureProbe, route } from '../fetch'

afterEach(() => {
  vi.useRealTimers()
  window.location.hash = ''
})

const T0 = Date.parse('2026-09-29T10:00:00Z')

/** Register one read at `minutes` after T0, so the ages are known. */
function read(path: string, minutes: number, ok: boolean, kind?: Parameters<typeof noteFixtureProbe>[3]): void {
  vi.setSystemTime(T0 + minutes * 60_000)
  noteFixtureProbe(route(path), 20, ok, kind, { frame: true })
}

async function referenceRows(): Promise<string[]> {
  window.location.hash = '#reference'
  render(<App />)
  const table = await waitFor(() => {
    const t = document.querySelector('.ctl-table.is-scroll table')
    expect(t, 'API reads drew no table').not.toBeNull()
    return t as HTMLElement
  })
  return [...table.querySelectorAll('tbody th[scope="row"]')].map((th) => th.firstChild?.textContent ?? '')
}

function registry(): void {
  vi.useFakeTimers({ toFake: ['Date'] })
  // ok, newest payload at +9
  read('/v1/a-fresh', 9, true)
  // ok, newest payload at +1: the older ok sorts before the fresher one
  read('/v1/b-old', 1, true)
  // the expected 403 for a non-admin
  read('/v1/admin/c', 2, false, 'admin_required')
  // paused by a 429
  read('/v1/d-paused', 3, false, 'rate_limited')
  // failing now, with a payload from +0 still on screen somewhere
  read('/v1/e-failing', 0, true)
  read('/v1/e-failing', 4, false, 'unreachable')
  // failing, and never once loaded
  read('/v1/z-never', 5, false, 'unreachable')
  vi.setSystemTime(T0 + 10 * 60_000)
}

describe('API reads orders routes by what went wrong (#140)', () => {
  it('draws a failing route as the first row', async () => {
    registry()
    const rows = await referenceRows()
    expect(rows[0], `the first row is ${rows[0]}`).toBe('/v1/z-never')
    // MUTATION: draw the registry's own alphabetical order.
    expect(rows).toEqual(['/v1/z-never', '/v1/e-failing', '/v1/d-paused', '/v1/admin/c', '/v1/b-old', '/v1/a-fresh'])
  })

  it('shows failures only on request, and says how many of how many', async () => {
    registry()
    await referenceRows()
    const toggle = screen.getByRole('button', { name: 'failures only' })
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    expect(document.querySelector('.ref-count')?.textContent).toBe('6 of 6')
    act(() => {
      fireEvent.click(toggle)
    })
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    const rows = [...document.querySelectorAll('tbody th[scope="row"]')].map((th) => th.firstChild?.textContent)
    // The admin 403 is the expected answer, not a failure.
    expect(rows).toEqual(['/v1/z-never', '/v1/e-failing', '/v1/d-paused'])
    expect(document.querySelector('.ref-count')?.textContent).toBe('3 of 6')
  })

  it('says it is this tab only once', async () => {
    registry()
    await referenceRows()
    // The canonical page head (visual QA Q2): the caveat is its meta chip.
    const head = document.querySelector('main .c-phead, .c-phead')!
    expect(within(head as HTMLElement).getByText(/this tab only/)).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Routes called in this tab' })).toBeNull()
    expect(document.body.textContent ?? '').not.toMatch(/since this tab loaded/)
    expect(document.querySelector('.ctl-table caption')).toBeNull()
  })
})
