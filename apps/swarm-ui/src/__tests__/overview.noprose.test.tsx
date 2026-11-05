// NO EXPLANATORY PROSE ON THE SURFACE (BGD's standing rule, 2026-11-05; part
// of #2688). Overview's waiting reasons and lifecycle band, and the Platform
// counts' Over time, said their definitions as visible sentences. Each moved
// behind the label it explained: a status tick's tooltip, a name's tooltip,
// the section title's tooltip. The facts stay; only where they sit changed.
//
// Each test names the mutation that turns it red.

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Result } from '../fetch'
import { LifecycleBand, WaitingWhy, waitGroups } from '../OverviewRegions'
import type { Stats, TaskPage } from '../types'
import { task } from './runfixture'

const loadPlatformTime = vi.hoisted(() => vi.fn())
vi.mock('../platformTimeApi', async (orig) => ({ ...(await orig<typeof import('../platformTimeApi')>()), loadPlatformTime }))

const { PlatformOverTime, forgetOverTime } = await import('../PlatformOverTime')

const NOW = Date.parse('2026-11-05T09:00:00Z')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: NOW, serverAt: new Date(NOW).toISOString() }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

/** What a label's tooltip says: the `role="tooltip"` its host points at. */
function tipOf(label: Element): string {
  const host = label.closest('.c-tip-host')
  expect(host, `${text(label)} has no tooltip`).not.toBeNull()
  const id = host!.getAttribute('aria-describedby')!
  return text(document.getElementById(id))
}

const WAITING: TaskPage = {
  tasks: [
    task({ id: 'tsk_p', state: 'PARKED', park_reason: 'MANUAL_PAUSE', workflow_id: null }),
    task({ id: 'tsk_r', state: 'READY', workflow_id: null }),
    task({ id: 'tsk_q', state: 'QUEUED', workflow_id: null }),
  ],
  next_page_token: null,
} as TaskPage

afterEach(() => {
  cleanup()
  forgetOverTime()
  loadPlatformTime.mockReset()
})

describe('Waiting, and why: what a state holds is its tick’s tooltip', () => {
  it('keeps the reason out of every detail line and carries it as `why`', () => {
    const groups = waitGroups(WAITING.tasks, NOW)
    expect(groups.map((g) => g.state).sort()).toEqual(['PARKED', 'QUEUED', 'READY'])
    for (const g of groups) {
      // MUTATION: append `why` to the detail's clauses again.
      expect(g.detail).not.toMatch(/Holds nothing|Eligible now|In line/)
      expect(g.why).not.toBe('')
    }
    const by = (s: string) => groups.find((g) => g.state === s)!
    expect(by('PARKED').why).toBe('Checkpointed and released. Holds nothing.')
    expect(by('READY').why).toBe('Eligible now. Admitted when every pool it needs has room.')
    // MUTATION: put back `'In line for the scheduler'` as the QUEUED group's title.
    expect(by('QUEUED').title).toBe('Queued')
    expect(by('QUEUED').why).toBe('In line for the scheduler. Not yet eligible. Holds nothing.')
  })

  it('draws each row’s status tick with its reason as the tooltip, and no sentence on the row', () => {
    const { container } = render(<WaitingWhy tasks={ok(WAITING)} />)
    const rows = [...container.querySelectorAll('.ov-wr')]
    expect(rows.length, 'no waiting row was drawn, so nothing was checked').toBe(3)
    const said: string[] = []
    for (const r of rows) {
      const tick = r.querySelector('.ov-wst')!
      // MUTATION: draw the tick without its Tooltip.
      said.push(tipOf(tick))
      // The tick is reachable from the keyboard, so its tooltip is too.
      expect(tick.getAttribute('tabindex')).toBe('0')
      // MUTATION: render `why` in the row's small line.
      expect(text(r.querySelector('.ov-wt'))).not.toMatch(/Holds nothing|Eligible now|In line for/)
    }
    expect(said).toContain('Checkpointed and released. Holds nothing.')
    expect(said).toContain('In line for the scheduler. Not yet eligible. Holds nothing.')
  })
})

describe('the lifecycle band: Holding capacity’s definition is its name’s tooltip', () => {
  const STATS: Stats = {
    tenant_id: 'eng',
    tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 2 },
    dispatch_paused: false,
    limits: {},
    generated_at: new Date(NOW).toISOString(),
  }

  it('draws no "each holds a lease" caption, and says it behind the name', () => {
    const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={ok({ tasks: [] })} />)
    const holding = [...container.querySelectorAll('#ov-band .ov-lc')].find((c) => text(c.querySelector('.ov-lc-k')) === 'Holding capacity')
    expect(holding, 'the band has no Holding capacity cell').toBeDefined()
    // MUTATION: put `small: 'each holds a lease'` back on the Holding cell.
    expect(holding!.querySelector('.ov-lc-h small')).toBeNull()
    expect(tipOf(holding!.querySelector('.ov-lc-k')!)).toBe('Each holds a lease.')
    // "costs nothing" says who pays, so it stays drawn.
    expect(text(container.querySelector('#ov-band .ov-lc .ov-lc-h small'))).toBe('costs nothing')
  })
})

describe('Over time: one line drawn, the rest behind the title', () => {
  it('draws only "Nothing here refreshes on its own." and "Nothing has been read yet." before a Run', () => {
    loadPlatformTime.mockReturnValue(new Promise(() => {}))
    const { container } = render(<PlatformOverTime workspaces={3} />)
    // MUTATION: draw the `pot-sub` sentence under the title again.
    expect(container.querySelector('.pot-sub')).toBeNull()
    // MUTATION: keep the price and the zone sentences in `.pot-note`.
    expect(text(container.querySelector('.pot-note'))).toBe('Nothing here refreshes on its own.')
    // MUTATION: put the "Run reads …" sentence and the three-item list back in the empty card.
    expect(text(container.querySelector('.pot-notrun'))).toBe('Nothing has been read yet.')
    expect(container.querySelector('.pot-notrun-list')).toBeNull()
  })

  it('keeps every moved fact in the title’s tooltip', () => {
    loadPlatformTime.mockReturnValue(new Promise(() => {}))
    const { container } = render(<PlatformOverTime workspaces={3} />)
    const tip = tipOf(container.querySelector('#pot-title .pot-title-k')!)
    for (const fact of [
      'Sealed days answer from the rollup; the newest are derived at Run.',
      'A breakdown costs no read: it groups the same days.',
      'Weeks start Monday.',
      'this says nothing about the platform.',
      'Agents over time: agents per week, retries apart.',
      'Estimated cost (list price, not a bill): cost per week with its coverage, and cost per agent.',
      'Breakdown: every figure as a table.',
    ]) {
      // MUTATION: drop any one sentence from `explain` or `notRunWords`.
      expect(tip, `the tooltip lost "${fact}"`).toContain(fact)
    }
    expect(tip).toMatch(/The price is ~\d+ reads for /)
    // The section is still named by its title alone, never by the tooltip.
    expect(container.querySelector('section.pot')!.getAttribute('aria-labelledby')).toBe('pot-title')
  })
})
