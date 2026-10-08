/**
 * THE OWNER'S VISUAL QA OF THE LIVE CONSOLE (2026-10-02 21:50Z, swarm-ui-00111,
 * 1440x900 light + dark + 390px), items Q1, Q2 and Q10, as behaviour.
 *
 * Q1  MONO IS FOR IDS, VALUES, CODE AND TIMESTAMPS. Table heads, the panel's
 *     group labels, the tenant block's words, the page head's actions (its
 *     refresh with its age) and the screen's count note, form labels,
 *     placeholders and the honesty marks are the sans
 *     UI face (components.html A). Asked of the cascade the app ships
 *     (`cssgate.cascade` through `marks.painted`), on elements `<App />`
 *     actually rendered, so a rule that never matches proves nothing.
 * Q2  ONE PAGE HEAD: the title and the `?` by it on the left, the freshness
 *     right-aligned in the head's actions on the same row -- and no
 *     breadcrumb row on a section page. A breadcrumb only inside an open
 *     object. Since #138 (owner ruling 2026-10-07) the meta chip is gone from
 *     the row (the count is `.c-count-note` over the first card) and the
 *     freshness is the refresh control itself (`.c-refresh`, `⟳ 12 s`).
 *     "just now·refresh" and "nowrefresh": the control's words are inline
 *     text, not a flex box whose anonymous items drop the spaces between them.
 * Q10 The same head at 390px: no breadcrumb, no stacked line under the title.
 *
 * MUTATIONS: put `var(--mono)` on `.c-refresh`, `.c-count-note`, `.sk-pt` or
 * `.c-tbl th` and Q1 goes red; render `Head`'s crumb row on a section page, or
 * take a page's freshness out of its head's actions, and Q2 does; make
 * `.c-refresh` a flex box and the spacing case does.
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

  it('draws every table head, page-head action, freshness and count note in the sans face', async () => {
    const seen: string[] = []
    for (const path of ['/capacity/pools', '/admin/limits', '/workflows', '/capacity/accounts']) {
      const { unmount } = at(path)
      await waitFor(() => expect(document.querySelector('main.work th, main.work .c-phead .c-refresh')).not.toBeNull(), WAIT)
      const heads = [...document.querySelectorAll('main.work th')]
      const meta = [...document.querySelectorAll('main.work .c-phead .c-acts > *, main.work .c-count-note')]
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
      '<div class="ctl-empty"><span class="ctl-empty-foot">Checked just now.</span></div>' +
      // A card's qualifier ("6 accounts") and Overview's card notes (review of #503).
      '<span class="ctl-card-note">6 accounts</span>' +
      '<div class="ov-card"><span class="ctl-card-note">2 waiting</span></div>'
    document.body.appendChild(host)
    try {
      expect(monoAmong([...host.children, ...host.querySelectorAll('.ov-card > *')])).toEqual([])
    } finally {
      host.remove()
    }
  })
})

describe('Q2/Q10: one page head, no breadcrumb row on a section page', () => {
  // Every section page with a page head (review of #503: /admin/counts kept
  // its own stacked head and lost the Admin `?`, and nothing here visited it).
  const PAGES = [
    '/capacity/pools', '/workflows', '/admin/limits', '/timeline', '/capacity/accounts', '/submit', '/submit/task', '/runs',
    '/admin/counts', '/capacity/holders', '/capacity/runtimes', '/admin/tenants', '/overview', '/agents',
  ]

  it('draws no breadcrumb row, and the title, its ? and the freshness share one head', async () => {
    // The freshness is the head's own, in its actions: a Screen's, Overview's
    // and the Timeline's refresh control (`.c-refresh`), Platform counts'
    // provenance (`.counts-prov`), or -- on a head with no age of its own,
    // the Submit chooser -- the frame's (`.ctl-head-age`).
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
      await waitFor(() => {
        // AN ADMIN GATE IS NOT A READ TO RENEW (#138): a Screen that met it
        // draws no refresh control, and its panel says `admin only` instead
        // (the fixture API answers some admin routes with the gate). Then
        // the actions hold nothing at all -- not the frame's age either.
        if (document.querySelector('main.work .admin-gate') !== null) {
          expect(head!.querySelector(':scope > .c-acts')?.children.length ?? 0, `${path}: an admin gate with a control`).toBe(0)
          return
        }
        expect(
          head!.querySelector(':scope > .c-acts > .c-refresh, :scope > .c-acts > .ctl-head-age, :scope > .c-acts > .counts-prov'),
          `${path}: the freshness is not in the title row's actions`,
        ).not.toBeNull()
      }, WAIT)
      expect(head!.querySelector('.sub'), `${path}: a line under the title`).toBeNull()
      unmount()
    }
    expect(visited).toHaveLength(PAGES.length)
  })

  it('keeps the breadcrumb inside an open object', async () => {
    window.location.hash = '#work/task/t-1'
    render(<App />)
    await waitFor(() => expect(document.querySelector('nav[aria-label="Breadcrumb"]')).not.toBeNull(), WAIT)
  })

  it('keeps the breadcrumb inside an open workflow and an open run, and names the object last', async () => {
    // MUTATION: guard `Head` on `taskId` alone again -- an open workflow is
    // an object too, and lost its trail back to the list (review of #503).
    for (const [path, id] of [['/workflows/wf_broker', 'wf_broker'], ['/runs/run_4c1e09d2', 'run_4c1e09d2']] as const) {
      const { unmount } = at(path)
      await waitFor(() => expect(document.querySelector('.ctl-crumb'), path).not.toBeNull(), WAIT)
      expect(document.querySelector('.ctl-crumb [aria-current="page"]')?.textContent, path).toBe(id)
      unmount()
    }
    // And the list the workflow was opened from still draws none.
    const { unmount } = at('/workflows')
    await screen.findByRole('heading', { level: 1 }, WAIT)
    expect(document.querySelector('.ctl-crumb')).toBeNull()
    unmount()
  })

  it('lays the freshness out as inline text, so the spaces in its words survive', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="c-phead"><div class="head"><h1>x</h1></div><div class="c-acts"><button type="button" class="c-refresh">⟳ every 30 s · read 4 s ago</button></div></div>' +
      '<p class="c-count-note">3 pools</p>'
    document.body.appendChild(host)
    try {
      const age = host.querySelector('.c-refresh')!
      for (const env of [WIDE, PHONE]) {
        expect(painted(age, ['display'], env) ?? 'inline', `${env.width}px`).not.toMatch(/flex|grid/)
      }
      // The head holds one row at desktop width; at 390 it may wrap the
      // freshness under the title, but it is still the same head.
      expect(painted(host.querySelector('.c-phead')!, ['flex-wrap'], WIDE)).toBe('nowrap')
      expect(painted(host.querySelector('.c-phead')!, ['flex-direction'], PHONE) ?? 'row').toBe('row')
      expect(familyOf(age, PHONE)).toBe('sans')
      expect(familyOf(host.querySelector('.c-count-note')!, PHONE)).toBe('sans')
    } finally {
      host.remove()
    }
  })
})
