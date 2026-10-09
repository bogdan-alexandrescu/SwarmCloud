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
import { forgetProbes, noteFixtureProbe, route } from '../fetch'

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
    // The caveat is the note over the page's first card (#138: the head is
    // title left, actions right, no chip), said exactly once on the page.
    // MUTATION: put it back in the head, or say it a second time anywhere.
    const note = document.querySelector('.c-phead + .c-count-note')
    expect(note?.textContent).toBe('this tab only · not the API surface')
    expect((document.body.textContent ?? '').match(/this tab only/g) ?? []).toHaveLength(1)
    expect(within(document.querySelector('.c-phead') as HTMLElement).queryByText(/this tab only/)).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Routes called in this tab' })).toBeNull()
    expect(document.body.textContent ?? '').not.toMatch(/since this tab loaded/)
    expect(document.querySelector('.ctl-table caption')).toBeNull()
  })
})

describe('API reads with nothing read draws its real zero with the Mark primitive (#76)', () => {
  it('gives the empty state\'s mark an accessible sentence', async () => {
    forgetProbes()
    window.location.hash = '#reference'
    render(<App />)
    const empty = await waitFor(() => {
      const e = document.querySelector('.ctl-empty')
      expect(e, 'API reads drew no empty state').not.toBeNull()
      return e as HTMLElement
    })
    // A hand-drawn `<i className="ctl-mark is-zero">` says the two words with
    // nothing behind them. MUTATION: draw it by hand again; the role and the
    // name are gone and this fails.
    const marks = empty.querySelectorAll('.ctl-mark')
    expect(marks).toHaveLength(1)
    const mark = marks[0] as HTMLElement
    expect(mark.classList.contains('is-zero')).toBe(true)
    expect(mark.textContent).toBe('real zero')
    expect(mark.getAttribute('role')).toBe('img')
    expect(mark.getAttribute('aria-label') ?? '').toMatch(/nothing has been read/i)
  })
})
