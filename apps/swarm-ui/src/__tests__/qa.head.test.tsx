/**
 * THE OWNER'S VISUAL QA OF THE LIVE CONSOLE (2026-10-02 21:50Z, swarm-ui-00111,
 * 1440x900 light + dark + 390px), items Q1, Q2 and Q10, as behaviour.
 *
 * Q1  MONO IS FOR IDS, VALUES, CODE AND TIMESTAMPS. Table heads, the panel's
 *     group labels, the tenant block's words, the page head's meta chip and
 *     freshness, form labels, placeholders and the honesty marks are the sans
 *     UI face (components.html A). Asked of the cascade the app ships
 *     (`cssgate.cascade` through `marks.painted`), on elements `<App />`
 *     actually rendered, so a rule that never matches proves nothing.
 * Q2  ONE PAGE HEAD: the title, the meta chip beside it, the freshness
 *     right-aligned on the same row, the `?` by the title -- and no breadcrumb
 *     row on a section page. A breadcrumb only inside an open object.
 *     "just now·refresh" and "nowrefresh": the freshness group is inline text,
 *     not a flex box whose anonymous items drop the spaces between them.
 * Q10 The same head at 390px: no breadcrumb, no stacked meta line.
 *
 * MUTATIONS: put `var(--mono)` back on `.c-meta`, `.sk-pt` or `.c-tbl th` and
 * Q1 goes red; render `Head`'s crumb row on a section page and Q2 does; make
 * `.c-age` an inline-flex box again and the spacing case does.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 6000 }

afterEach(() => {
  window.history.replaceState(null, '', '/')
})


function at(path: string) {
  window.history.replaceState(null, '', path)
  return render(<App />)
}

function label(el: Element): string {
  const text = (el.textContent ?? '').trim().slice(0, 40)
  return `${el.tagName.toLowerCase()}.${(el.getAttribute('class') ?? '').split(' ')[0]} "${text}"`
}

function monoAmong(els: readonly Element[], env: CascadeEnv = WIDE): string[] {
  return els.filter((el) => familyOf(el, env) === 'mono').map(label)
}

describe('Q1: mono only for ids, values, code and timestamps', () => {
  it('draws the panel group label and the tenant block words in the sans face', async () => {
    at('/capacity/pools')
    await waitFor(() => expect(document.querySelector('.sk-panel .sk-pt')).not.toBeNull(), WAIT)
    const labels = [...document.querySelectorAll('.sk-panel .sk-pt')]
    expect(labels.length).toBeGreaterThan(0)
    expect(monoAmong(labels)).toEqual([])
    const tenantWords = [...document.querySelectorAll('.sk-tenant small, .sk-tenant button, .sk-tenant .sk-copy')]
    expect(monoAmong(tenantWords)).toEqual([])
  })

  it('draws every table head, page-head meta and freshness in the sans face', async () => {
    const seen: string[] = []
    for (const path of ['/capacity/pools', '/admin/limits', '/workflows', '/capacity/accounts']) {
      const { unmount } = at(path)
      await waitFor(() => expect(document.querySelector('main.work th, main.work .c-meta')).not.toBeNull(), WAIT)
      const heads = [...document.querySelectorAll('main.work th')]
      const meta = [...document.querySelectorAll('main.work .c-phead .c-meta, main.work .c-phead .c-age')]
      seen.push(`${path}: ${heads.length} heads, ${meta.length} head parts`)
      expect(monoAmong([...heads, ...meta]), path).toEqual([])
      unmount()
    }
    // Empty output is not success: every route drew something to examine.
    expect(seen.every((s) => !/: 0 heads, 0 head parts/.test(s)), seen.join('\n')).toBe(true)
  })

  it('draws form labels and placeholders in the sans face', async () => {
    at('/submit/issue')
    const box = await screen.findByPlaceholderText('owner/repo#N or an issue URL', undefined, WAIT)
    const labels = [...document.querySelectorAll('main.work label, main.work .ctl-fact > b')]
    expect(labels.length).toBeGreaterThan(6)
    expect(monoAmong(labels)).toEqual([])
    // The typed value is an issue reference (an id, mono); its placeholder is
    // an instruction and is sans.
    const ph = painted(box, ['font-family', 'font'], WIDE, 'placeholder')
    expect(ph, 'no rule gives the placeholder its face').not.toBeNull()
    expect(ph).not.toMatch(/--mono/)
  })

  it('draws the honesty marks and the small labels in the sans face', () => {
    const host = document.createElement('div')
    host.className = 'app'
    // The six honesty marks ("real zero", "not reported", "partial" ...), the
    // fact keys ("attempt", "gen", "took", "exit", "cost", "serves"), the
    // toolbar eyebrows ("same step"), the "reason" label, a metric's label
    // and foot, and the empty state's "Checked just now." foot.
    host.innerHTML =
      ['zero', 'absent', 'unread', 'partial', 'admin', 'pending'].map((k) => `<i class="ctl-mark is-${k}">x</i>`).join('') +
      '<ul class="ctl-facts"><li class="ctl-fact"><b>serves</b></li></ul>' +
      '<span class="ctl-eyebrow wf-scrub-key">same step</span><span class="t-label">reason</span>' +
      '<span class="ctl-metric-label">agents</span><span class="ctl-metric-foot">last read</span>' +
      '<div class="ctl-empty"><span class="ctl-empty-foot">Checked just now.</span></div>'
    document.body.appendChild(host)
    try {
      expect(monoAmong([...host.children])).toEqual([])
    } finally {
      host.remove()
    }
  })
})

describe('Q2/Q10: one page head, no breadcrumb row on a section page', () => {
  const PAGES = ['/capacity/pools', '/workflows', '/admin/limits', '/timeline', '/capacity/accounts', '/submit', '/submit/task', '/runs']

  it('draws no breadcrumb row, and the title, its ? and the freshness share one head', async () => {
    const visited: string[] = []
    for (const path of PAGES) {
      const { unmount } = at(path)
      const h1 = await screen.findByRole('heading', { level: 1 }, WAIT)
      visited.push(path)
      expect(document.querySelector('.ctl-crumb, .c-crumb, nav[aria-label="Breadcrumb"]'), `${path} draws a breadcrumb`).toBeNull()
      expect(document.querySelector('.ctl-head'), `${path} draws the head row`).toBeNull()
      const head = h1.closest('.c-phead')
      expect(head, `${path}: the h1 is not in the page head`).not.toBeNull()
      expect(head!.querySelector('.ctl-q-glyph, .helpcard-trigger, button[aria-label^="Help"]'), `${path}: no ? by the title`).not.toBeNull()
      expect(head!.querySelector('.c-age'), `${path}: the freshness is not on the title row`).not.toBeNull()
      unmount()
    }
    expect(visited).toHaveLength(PAGES.length)
  })

  it('keeps the breadcrumb inside an open object', async () => {
    window.location.hash = '#work/task/t-1'
    render(<App />)
    await waitFor(() => expect(document.querySelector('nav[aria-label="Breadcrumb"]')).not.toBeNull(), WAIT)
  })

  it('lays the freshness out as inline text, so the spaces before refresh survive', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="c-phead"><div class="head"><h1>x</h1></div><p class="sub"><span class="c-meta">m</span><span class="c-age">a · <button>refresh</button></span></p></div>'
    document.body.appendChild(host)
    try {
      const age = host.querySelector('.c-age')!
      for (const env of [WIDE, PHONE]) {
        expect(painted(age, ['display'], env) ?? 'inline', `${env.width}px`).not.toMatch(/flex|grid/)
      }
      // The head holds one row at desktop width; at 390 it may wrap the
      // freshness under the title, but it is still the same head.
      expect(painted(host.querySelector('.c-phead')!, ['flex-wrap'], WIDE)).toBe('nowrap')
      expect(painted(host.querySelector('.c-phead')!, ['flex-direction'], PHONE) ?? 'row').toBe('row')
      expect(familyOf(host.querySelector('.c-meta')!, PHONE)).toBe('sans')
    } finally {
      host.remove()
    }
  })
})
