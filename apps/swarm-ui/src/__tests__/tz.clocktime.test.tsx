// TZ-CLOCKTIME (part of #2391; BGD, 2026-10-26, "One types.ts lane"): every
// time `clockTime` prints is in the person's zone and carries its label, so
// the text a caller's column holds grew. `09:04:07` is `09:04:07 UTC`, and an
// older day's `10-24 14:05` is `Oct 24 14:05 PDT`. This holds the one column
// sized to the old text, the workflow board's Submitted, to the new.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, clips, fixedColumns, lengthPx, nowrap, textPx } from './tablefit'
import { clockTime } from '../types'
import { resetTimeZone } from '../timezone'

const hosts: HTMLElement[] = []
beforeEach(() => resetTimeZone())
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  resetTimeZone()
})

/** The board's head and one row, as Workflows.tsx draws them from 900 up (Shape and Runners hidden below 1680). */
function list(): HTMLElement {
  const cols = ['state', 'workflow', 'done', 'cost', 'owner', 'submitted', 'duration']
  const host = document.createElement('div')
  host.innerHTML =
    '<div class="wfl"><div class="table-wrap is-scroll wfl-scroll"><table class="wfl-table"><thead><tr>' +
    cols.map((c) => `<th data-col="${c}">${c}</th>`).join('') +
    '</tr></thead><tbody><tr class="wfl-row">' +
    cols.map((c) => (c === 'submitted' ? '<td class="wfl-submitted" data-col="submitted">x</td>' : `<td data-col="${c}">x</td>`)).join('') +
    '</tr></tbody></table></div></div>'
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

/** The list's box: the viewport less the 84px spine, the 236px nav panel open, and two 32px gutters. */
const listAt = (viewport: number) => viewport - 84 - 236 - 64

const NOW = Date.parse('2026-10-24T21:30:00.000Z')
const LA = 'America/Los_Angeles'
// The widest the column is asked to hold: a two-digit day, a two-digit hour,
// and a three-letter label (and a long one, which takes a line of its own).
const OLDER = clockTime('2026-10-20T21:05:00.000Z', NOW, LA)!.text
const TODAY = clockTime('2026-10-24T19:04:07.000Z', NOW, LA)!.text
const LONG_LABEL = clockTime('2026-10-20T08:35:00.000Z', NOW, 'Asia/Kolkata')!.text

describe('the workflow board’s Submitted column holds a labelled time', () => {
  it('prints what the column is sized for', () => {
    expect(OLDER).toBe('Oct 20 14:05 PDT')
    expect(TODAY).toBe('12:04:07 PDT')
    expect(LONG_LABEL).toBe('Oct 20 14:05 GMT+5:30')
  })

  for (const width of [1440, 1280]) {
    it(`at ${width}: today's time on one line, an older one's date and time on one and its label under, nothing cut`, () => {
      const host = list()
      const env: CascadeEnv = { width }
      const th = host.querySelector('th[data-col="submitted"]')!
      const td = host.querySelector('td[data-col="submitted"]')!
      const st = cellStyle(td, env)
      const room = (lengthPx(painted(th, 'width', env), 0) ?? 0) - st.pl - st.pr
      // MUTATION: put back `width: 96px` -- the content box is 76px and
      // `Oct 20 14:05` (12 characters of 13px sans) is 87px.
      expect(room).toBeGreaterThanOrEqual(textPx(TODAY, td, env))
      const [dateTime, label] = [OLDER.slice(0, OLDER.lastIndexOf(' ')), OLDER.slice(OLDER.lastIndexOf(' ') + 1)]
      expect(room).toBeGreaterThanOrEqual(textPx(dateTime, td, env))
      expect(room).toBeGreaterThanOrEqual(textPx(label, td, env))
      expect(room).toBeGreaterThanOrEqual(textPx(LONG_LABEL.slice(LONG_LABEL.lastIndexOf(' ') + 1), td, env))
      // The cell wraps at its spaces, so the label drops under the time
      // rather than being cut.
      // MUTATION: drop `white-space: normal` from the cell and `.wfl-submitted`'s
      // nowrap holds `Oct 20 14:05 PDT` on one line, cut at the label.
      expect(nowrap(td, env), 'the Submitted cell is held to one line').toBe(false)
      expect(textPx(OLDER, td, env), 'an older time fits on one line, so this measured no wrap').toBeGreaterThan(room)
      // `overflow: hidden` stays (the table's cells all clip); with the wrap
      // nothing reaches it.
      expect(clips(td, env)).toBe(true)
    })
  }

  it('leaves the name the 168px D8 and R10 hold, at 1280 with the nav open', () => {
    const host = list()
    const env: CascadeEnv = { width: 1280 }
    const { cols } = fixedColumns(host.querySelector('table')!, listAt(1280), env)
    const name = cols.find((c) => c.col === 'workflow')!
    // MUTATION: widen Submitted by more than the 28px the name had over its floor.
    expect(name.end - name.start).toBeGreaterThanOrEqual(168)
  })
})

describe('the agent inspector’s headline holds `ended <time> <label>`', () => {
  function headline(): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ag-head"><div class="ag-head-row"><span class="ag-head-hl"><b>1h 3m</b> · attempt 10 of 10<span> · ended x</span></span></div></div>'
    document.body.appendChild(host)
    hosts.push(host)
    return host.querySelector<HTMLElement>('.ag-head-hl')!
  }

  it('wraps at its spaces where it has a line of its own (under 640), so a long label is not cut at 390', () => {
    const hl = headline()
    const text = `1h 3m · attempt 10 of 10 · ended ${LONG_LABEL}`
    // A 390 phone: the pane's column is 358px, and the headline is 13px sans.
    const env: CascadeEnv = { width: 390, container: 358 }
    expect(textPx(text, hl, env), 'the headline fits one line, so this measured no wrap').toBeGreaterThan(358)
    // MUTATION: drop the container rule's `white-space: normal` and the
    // headline keeps its one line past the column's edge.
    expect(nowrap(hl, env)).toBe(false)
  })

  it('keeps its one line beside the title from 640 up, as agent-details-v3.html A draws it', () => {
    expect(nowrap(headline(), { width: 1440, container: 720 })).toBe(true)
  })
})
