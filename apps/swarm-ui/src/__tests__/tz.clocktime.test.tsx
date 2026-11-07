// TZ-CLOCKTIME (part of #2391; owner, 2026-10-26, "One types.ts lane"): every
// time `clockTime` prints is in the person's zone and carries its label, so
// the text a caller's column holds grew. `09:04:07` is `09:04:07 UTC`, and an
// older day's `10-24 14:05` is `Oct 24 14:05 PDT`. This holds the inspector's
// headline and Ran tile, sized to the old text, to the new.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { nowrap, textPx } from './tablefit'
import { clockTime } from '../types'
import { resetTimeZone } from '../timezone'

const hosts: HTMLElement[] = []
beforeEach(() => resetTimeZone())
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  resetTimeZone()
})

const NOW = Date.parse('2026-10-24T21:30:00.000Z')
const LA = 'America/Los_Angeles'
// The widest a time is: a two-digit day, a two-digit hour,
// and a three-letter label (and a long one, which takes a line of its own).
const OLDER = clockTime('2026-10-20T21:05:00.000Z', NOW, LA)!.text
const TODAY = clockTime('2026-10-24T19:04:07.000Z', NOW, LA)!.text
const LONG_LABEL = clockTime('2026-10-20T08:35:00.000Z', NOW, 'Asia/Kolkata')!.text

describe('clockTime prints a time with its zone label', () => {
  it('prints the times the inspector below is sized for', () => {
    expect(OLDER).toBe('Oct 20 14:05 PDT')
    expect(TODAY).toBe('12:04:07 PDT')
    expect(LONG_LABEL).toBe('Oct 20 14:05 GMT+5:30')
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

describe('the inspector strip’s Ran tile holds `<start> <label> → <end> <label>` whole', () => {
  // The tile as `DtStrip` draws an ended attempt: the sub-line is one span
  // titled `started … · ended …`. In a 480-639px pane (the 1440 split
  // inspector) every sub-line is clamped to two lines, and the labelled pair
  // needs three: the review render cut it to `→ Oct 19 04:47…`, label lost.
  function tile(sub: string): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML = `<div class="dt"><div class="dt-strip"><div class="dt-sc"><span class="dt-sc-l">Ran · attempt 1</span><span class="dt-sc-v">7m 0s</span><span class="dt-sc-s">${sub}</span></div></div></div>`
    document.body.appendChild(host)
    hosts.push(host)
    return host.querySelector<HTMLElement>('.dt-sc-s')!
  }
  const PANE: CascadeEnv = { width: 1440, container: 600 }
  const start = clockTime('2026-10-19T11:40:00.000Z', NOW, LA)!
  const end = clockTime('2026-10-19T11:47:00.000Z', NOW, LA)!

  it('is not clamped at a 600px pane', () => {
    expect(`${start.text} → ${end.text}`).toBe('Oct 19 04:40 PDT → Oct 19 04:47 PDT')
    const s = tile(`<span title="started ${start.title} · ended ${end.title}">${start.text} → ${end.text}</span>`)
    // MUTATION: drop the Ran tile's selector from details.css's `is-whole`
    // rule and the sub-line keeps the pane's two-line clamp.
    expect(painted(s, '-webkit-line-clamp', PANE)).toBe('none')
    expect(painted(s, 'line-clamp', PANE)).toBe('none')
    expect(painted(s, 'overflow', PANE)).toBe('visible')
  })

  it('leaves every other sub-line clamped to two, so this measured the rule', () => {
    const s = tile('<span title="started x">since 04:40:00 PDT</span>')
    expect(painted(s, '-webkit-line-clamp', PANE)).toBe('2')
  })
})
