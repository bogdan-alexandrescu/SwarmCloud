/**
 * THE SIDEBAR'S FINDINGS FROM THE FIRST CONSOLE VISUAL-QA RUN (QAFIX-SIDEBAR,
 * part of #1680; run wf_9e9bf74834634db7bcc3, 2026-10-20, the offline preview
 * build at 1440 and 390 px, light and dark). Each `describe` is one finding,
 * held by the property that was false:
 *
 *   QA-028  The tenant card's `copy id` button was named `Copy tenant id
 *           u-bogdan`, which does not contain its visible words (axe
 *           label-content-name-mismatch, serious): a speech user saying
 *           "click copy id" matched nothing.
 *   QA-040  Under the Global pool meter, a quarter full, an amber
 *           `provider:anthropic:tenant:u-bogdan full` read as the global pool
 *           being full, in a raw pool key.
 *   QA-055  The active sub-item's highlight ran to the panel's right border;
 *           the top-level rows keep 10px.
 *   QA-071  The active Submit rail item drew a white ring that reads as a
 *           focus outline left behind.
 *   QA-078  Agents › Recent said `not totalled` beside a tab that says
 *           `Recent 17`.
 *   QA-096  A workflow whose state was not derived read `state not recorded`
 *           in Recent (the table says `state not derived`), with no mark, so
 *           its name sat 12px out of line with the rows that have one.
 *   QA-022  (from QAFIX-HELP) the content scroller keeps the sticky bar's
 *           height clear at its top, so anything focused or scrolled into
 *           view stops below the bar, not only the `?` glyphs.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SkyShell, meterOf, rememberWorkflow } from '../Spine'
import type { Capacity } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
})

function shell(section: 'work' | 'capacity', tab: string, agentTab: 'live' | 'waiting' | 'recent' = 'live') {
  return render(
    <SkyShell section={section} tab={tab} agentTab={agentTab} title="x" go={vi.fn()} foot={null}>
      {null}
    </SkyShell>,
  )
}

describe('QA-028: the copy id button is named with the words it shows', () => {
  it('its accessible name contains its visible text, and still names the tenant', async () => {
    const { container } = shell('work', 'running')
    const btn = await waitFor(() => {
      const b = container.querySelector<HTMLButtonElement>('.sk-tenant .sk-cp')
      expect(b, 'no copy id button').not.toBeNull()
      return b!
    }, WAIT)
    const shown = (btn.textContent ?? '').trim().toLowerCase()
    const name = (btn.getAttribute('aria-label') ?? btn.textContent ?? '').toLowerCase()
    expect(shown).toBe('copy id')
    // MUTATION: put back aria-label={`Copy tenant id ${id}`} on `.sk-cp` in
    // Spine.tsx and the name no longer contains "copy id".
    expect(name, 'axe label-content-name-mismatch').toContain(shown)
    // MUTATION: drop the id from the label and a reader cannot tell whose id it copies.
    expect(name).toContain((btn.getAttribute('title') ?? '').toLowerCase())
    expect(screen.getByRole('button', { name: /^copy id/i })).toBe(btn)
  })
})

describe('QA-040: the global meter does not wear another pool’s warning as its own', () => {
  const pool = (name: string, active: number, limit: number, enabled = true) => ({
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: limit - active,
    enabled,
    updated_at: '2026-10-20T00:00:00Z',
  })

  it('names the binding pool as another pool, in its label, with the raw key in the title', () => {
    const cap = { pools: [pool('global', 5, 20), pool('provider:anthropic:tenant:u-bogdan', 3, 3)], runner_profiles: {} } as unknown as Capacity
    const m = meterOf(cap)!
    // MUTATION: put back `${hot.name} full` in meterOf and the line under
    // the 5 / 20 meter reads as the global pool being full, in a raw key.
    expect(m.warn).toBe('another pool is full: anthropic · u-bogdan')
    expect(m.warn).not.toContain('provider:')
    // MUTATION: drop `warnPool` and the exact pool is nowhere on the card.
    expect(m.warnPool).toBe('provider:anthropic:tenant:u-bogdan')
  })

  it('says the global pool itself is full when it is', () => {
    const cap = { pools: [pool('global', 20, 20), pool('tenant:eng', 0, 5, false)], runner_profiles: {} } as unknown as Capacity
    // MUTATION: name the first hot pool whichever it is and this reads "another pool".
    expect(meterOf(cap)?.warn).toBe('this pool is full')
  })
})

describe('QA-055: an active sub-item keeps the panel’s right margin', () => {
  it('the sub-list ends where a top-level row does', () => {
    const kids = build('div.sk-panel div.sk-kids', hosts)
    const row = build('div.sk-panel a.sk-pk', hosts)
    for (const env of [WIDE, PHONE]) {
      const right = (painted(kids, 'margin', env) ?? '').trim().split(/\s+/)[1]
      const rowRight = (painted(row, 'margin', env) ?? '').trim().split(/\s+/)[1]
      // MUTATION: put back `margin: 2px 0 6px 38px` on `.sk-kids` and the
      // lit `.sk-kid` fill runs to the panel border again.
      expect(right, `at ${env.width}px`).toBe(rowRight)
      expect(right).not.toBe('0')
    }
  })
})

describe('QA-071: the lit Submit item is marked as Work is, with no ring', () => {
  it('draws no box-shadow ring, and keeps the marker bar', () => {
    const nav = document.createElement('nav')
    nav.className = 'sk-spine'
    const on = document.createElement('a')
    on.className = 'sk-ri sk-cta'
    on.setAttribute('aria-current', 'page')
    nav.appendChild(on)
    document.body.appendChild(nav)
    hosts.push(nav)
    // MUTATION: put back `box-shadow: 0 0 0 2px #0b3a7a, 0 0 0 4px #fff` on
    // `.sk-ri.sk-cta[aria-current='page']` and this reads the ring.
    expect(painted(on, 'box-shadow', WIDE)).toBeNull()
    // MUTATION: drop the `::before` rule and the lit Submit looks like the resting one.
    expect(painted(on, 'content', WIDE, 'before')).not.toBeNull()
  })
})

describe('QA-078: Agents › Recent does not deny the count its tab shows', () => {
  it('says what Recent is, never "not totalled"', async () => {
    const { container } = shell('work', 'running', 'recent')
    const recent = await waitFor(() => {
      const k = [...container.querySelectorAll('.sk-panel .sk-kid')].find((x) => x.querySelector('span')?.textContent === 'Recent')
      expect(k, 'no Recent row').toBeDefined()
      return k!
    }, WAIT)
    // MUTATION: put back `not totalled` in the `.sk-knote` and it contradicts "Recent 17".
    expect(recent.textContent).not.toMatch(/not totalled/)
    expect(recent.querySelector('.sk-knote')?.textContent).toBe('newest first')
    // MUTATION: put back the title "a window, so it is not counted".
    expect(recent.getAttribute('title')).not.toMatch(/not counted/)
  })
})

describe('QA-096: a workflow with no derived state reads as it does in the table, and stays in line', () => {
  it('says "state not derived" with the table’s amber mark', () => {
    rememberWorkflow('wf_underived1', null, 'underived-flow')
    const { container } = shell('work', 'workflows')
    const group = container.querySelector<HTMLElement>('[aria-label="Recent workflows"]')!
    const row = within(group).getByText('underived-flow').closest('a')!
    // MUTATION: put back 'state not recorded' in RecentWorkflows.
    expect(row.textContent).toContain('state not derived')
    expect(row.textContent).not.toContain('state not recorded')
    const mark = row.querySelector('.sk-st')!
    // MUTATION: draw no mark for a null state and the name sits 12px left of its neighbours.
    expect(mark.querySelector('svg'), 'no mark in the slot').not.toBeNull()
    expect(mark.getAttribute('data-hue')).toBe('warn')
  })

  it('a row whose mark is withheld keeps the mark’s slot', () => {
    const slot = build('div.sk-kids.sk-recent span.sk-st', hosts)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop `min-width: 12px` on `.sk-recent .sk-st` and a
      // stale row's name starts where the others' marks do.
      expect(painted(slot, 'min-width', env), `at ${env.width}px`).toBe('12px')
    }
  })
})

describe('QA-022: the page scroller keeps the sticky bar clear', () => {
  it('scroll-padding-top is the bar’s height, so a focused control stops below it', () => {
    const scroller = build('div.ctl-frame.has-ctl-bar div.ctl-scroll', hosts)
    for (const [env, bar] of [[WIDE, '48px'], [PHONE, '44px']] as const) {
      // MUTATION: drop `scroll-padding-top: var(--ctl-bar-h, 0px)` from
      // `.ctl-scroll` in styles.css and Tab parks the first stop under the bar.
      expect(painted(scroller, 'scroll-padding-top', env), `at ${env.width}px`).toBe(bar)
    }
    const bare = build('div.ctl-frame div.ctl-scroll', hosts)
    // MUTATION: hard-code 48px and a page with no bar loses 48px at its top.
    expect(painted(bare, 'scroll-padding-top', WIDE)).toBe('0px')
  })
})
