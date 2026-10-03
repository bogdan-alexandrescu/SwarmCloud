/**
 * VISUAL QA Q6 (owner, 2026-10-02): over 24h the Lanes page drew attempt
 * bars that were nearly invisible -- a short attempt is a fraction of a
 * percent of the track, and the 18px end mark, centred on the bar's end,
 * covered what was left. Per timeline.html A a bar spans its attempt at the
 * 20px height in the brand's hue, keeps a visible minimum width, and the end
 * mark sits after the bar rather than on it. Asked of the shipped cascade.
 * MUTATION: drop the bars' min-width, or centre `.tl-mkr` on the end again.
 */
import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function track(): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML =
    '<div class="tl-row"><div class="tl-lab"></div><div class="tl-trk">' +
    '<span class="tl-seg is-hold"></span><span class="tl-seg is-cut"></span><span class="tl-seg is-park"></span>' +
    '<span class="tl-mkr is-neu is-end"></span><span class="tl-mkr is-warn"></span></div></div>'
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

describe('Q6: an attempt bar is visible at any span', () => {
  it('keeps a pixel floor and the 20px height on every bar that holds or parked', () => {
    const h = track()
    for (const k of ['is-hold', 'is-cut', 'is-park']) {
      const seg = h.querySelector(`.tl-seg.${k}`)!
      const min = painted(seg, 'min-width', WIDE) ?? '0'
      expect(parseFloat(min), `${k} has no minimum width`).toBeGreaterThanOrEqual(6)
      expect(min, `${k}'s floor is not in pixels`).toMatch(/px$/)
      expect(painted(seg, 'height', WIDE)).toBe('20px')
    }
    // The brand's capacity hue (timeline.html A), not a wash too pale to see.
    expect(painted(h.querySelector('.tl-seg.is-hold')!, ['background', 'background-color'], WIDE)).toMatch(/--s-liveb/)
  })

  it('sets the end mark after the bar instead of centring it over the bar', () => {
    const h = track()
    const t = painted(h.querySelector('.tl-mkr.is-end')!, 'transform', WIDE) ?? ''
    expect(t).not.toMatch(/translate\(\s*-50%/)
    // A mark inside the lane (a cancel request) stays centred on its instant
    // (review of #503: the end rule had moved every mark right by its width).
    const mid = painted(h.querySelector('.tl-mkr:not(.is-end)')!, 'transform', WIDE) ?? ''
    expect(mid).toMatch(/translate\(\s*-50%/)
  })
})
