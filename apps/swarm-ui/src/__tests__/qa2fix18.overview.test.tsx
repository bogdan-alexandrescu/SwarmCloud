// QA2-FIX-18 (visual QA pass 2, part of #2694): Overview, the findings whose
// change is in this lane's files.
//
// WHAT EACH CASE HOLDS:
//   * overview-agents-08 (recheck): at 390 a phone Running row's workflow
//     title takes two lines before it is cut, and the step after it is still
//     whole (ffp-002) and still a part of its own. Read in the bundle's order
//     (overview.css, then names.css, then styles.css), as qa.qafix3 reads it.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { cascade } from './cssgate'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import NAMES_CSS from '../styles/names.css?raw'
import STYLES_CSS from '../styles.css?raw'

const PHONE: CascadeEnv = { width: 390 }
const BUNDLE = [OVERVIEW_CSS, NAMES_CSS, STYLES_CSS].join('\n')

afterEach(() => {
  document.body.innerHTML = ''
})

function phoneRow(): { name: Element; title: Element; step: Element } {
  document.body.innerHTML =
    '<main class="ov-page"><section id="ov-running"><ul class="ov-prun"><li><span class="ov-prun-m">' +
    '<a class="ov-prun-n" href="/agents/live/task_wf_scan_b"><span class="ov-prun-t">Audit the operator scripts and the Terraform</span>' +
    '<span class="ov-prun-s"> · scan-terraform</span></a></span></li></ul></section></main>'
  return {
    name: document.querySelector('a.ov-prun-n')!,
    title: document.querySelector('.ov-prun-t')!,
    step: document.querySelector('.ov-prun-s')!,
  }
}

const at = (n: Element, prop: string | readonly string[]) => {
  const r = cascade(BUNDLE, n, prop, PHONE)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

describe('overview-agents-08: a phone Running row wraps its title to two lines and keeps the step', () => {
  it('lets the title wrap, clamped at two lines with an ellipsis', () => {
    const { title } = phoneRow()
    // MUTATION: drop `white-space: normal` from `.ov-prun-t` (the anchor's nowrap is inherited: one line again).
    expect(at(title, 'white-space')).toBe('normal')
    // MUTATION: drop `-webkit-line-clamp: 2` from `.ov-prun-t`.
    expect(at(title, '-webkit-line-clamp')).toBe('2')
    // MUTATION: drop `display: -webkit-box` from `.ov-prun-t`.
    expect(at(title, 'display')).toBe('-webkit-box')
    expect(at(title, '-webkit-box-orient')).toBe('vertical')
    expect(at(title, 'text-overflow')).toBe('ellipsis')
    expect(at(title, ['overflow', 'overflow-x'])).toBe('hidden')
  })

  it('keeps the step whole, on the title’s last line', () => {
    const { name, step } = phoneRow()
    // ffp-002: the step that tells a workflow's rows apart never shrinks.
    expect(at(step, 'flex')).toBe('none')
    // MUTATION: drop `align-items: flex-end` from `.ov-prun-n` (the step sits beside the title's first line).
    expect(at(name, 'align-items')).toBe('flex-end')
    // The anchor itself is still the flex row, unclamped.
    expect(at(name, 'display')).toBe('flex')
    expect(at(name, '-webkit-line-clamp') ?? 'none').toBe('none')
  })
})
