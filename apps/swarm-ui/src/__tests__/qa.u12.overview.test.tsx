/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04): the Overview.
 *
 *   R12      A phone said "8 failed among the 50 most recent" while the desktop
 *            said "26 among the 200" at the same moment. Each counts the page
 *            its own agent list reads (OV-10); each figure now says the other
 *            width reads a different window.
 *   N17/D19  ~388px blank beside Headroom and ~157px beside Cost: two rows of
 *            pairs, each as tall as its taller card. Each column now stacks its
 *            own cards; the pools span both under them.
 *
 * MUTATIONS: drop `windowNote` from the headline or the clear note; put the
 * cards back into rows of two; stretch a column's cards -- each turns a case
 * red.
 */
import { describe, expect, it } from 'vitest'

import { deriveChecks, windowNote, type CheckInputs } from '../checks'
import type { Result } from '../fetch'
import type { Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const NOW = Date.parse('2026-10-04T12:00:00Z')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: NOW }
}
const empty = { status: 'empty', fetchedAt: NOW } as const

function inputs(tasks: Task[], phonePage?: boolean): CheckInputs {
  const page: TaskPage = { tasks, next_page_token: null } as unknown as TaskPage
  return {
    capacity: empty, tasks: ok(page), leases: empty, providers: empty, accounts: empty, stats: empty, workflows: empty,
    ...(phonePage === undefined ? {} : { phonePage }),
  } as unknown as CheckInputs
}

function failures(c: CheckInputs): string {
  const check = deriveChecks(c, NOW).find((x) => x.label === 'Failures')!
  if (check.status === 'found') return check.problems.map((p) => p.headline).join(' | ')
  if (check.status === 'clear') return check.note
  return check.status
}

describe('R12: each width says which window it counts', () => {
  const failed = Array.from({ length: 8 }, (_, i) =>
    task({ id: `task_f${i}`, state: 'FAILED', completed_at: new Date(NOW - 60_000).toISOString(), last_error: 'boom' }))
  const page = [...failed, ...Array.from({ length: 42 }, (_, i) => task({ id: `task_ok${i}`, state: 'SUCCEEDED' }))]

  it('says on a phone that a wider screen reads a larger window', () => {
    const h = failures(inputs(page, true))
    expect(h).toMatch(/^8 failed tasks among the 50 most recent/)
    expect(h).toContain('a phone reads the newest 50, a wider screen the newest 200')
  })

  it('says on a wide screen that a phone reads a smaller one', () => {
    expect(failures(inputs(page, false))).toContain('a phone reads only the newest 50')
    expect(failures(inputs(page))).toContain('a phone reads only the newest 50')
  })

  it('says it in the clear note too', () => {
    const none = Array.from({ length: 50 }, (_, i) => task({ id: `task_ok${i}`, state: 'SUCCEEDED' }))
    expect(failures(inputs(none, true))).toContain('a wider screen the newest 200')
    expect(windowNote(true, 50, 200)).not.toBe(windowNote(false, 50, 200))
  })
})

describe('N17/D19: no column is left blank beside a taller card', () => {
  function frame(): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ov-page"><div class="ov-g21 ov-cols">' +
      '<div class="ov-col"><section class="ctl-card ov-card ov-running"></section><section class="ctl-card ov-card ov-waiting"></section><section class="ctl-card ov-card ov-failures"></section></div>' +
      '<div class="ov-col"><section class="ctl-card ov-card ov-spend"></section><section class="ctl-card ov-card ov-headroom"></section></div>' +
      '<section class="ctl-card ov-card ov-pools"></section></div></div>'
    document.body.appendChild(host)
    return host
  }

  it('stacks each column\'s cards at their own heights at 1440, the pools under both', () => {
    const host = frame()
    const grid = host.querySelector('.ov-cols')!
    expect(painted(grid, 'grid-template-columns', WIDE)).toBe('minmax(0, 1.7fr) minmax(0, 1fr)')
    const cols = [...grid.querySelectorAll(':scope > .ov-col')]
    expect(cols).toHaveLength(2)
    for (const col of cols) {
      // A stack, not a cell of a row: the next card follows a short one.
      expect(painted(col, 'display', WIDE)).toBe('grid')
      expect(painted(col, 'align-content', WIDE)).toBe('start')
      for (const card of col.querySelectorAll(':scope > .ov-card')) {
        expect(painted(card, 'height', WIDE) ?? 'auto', card.className).toBe('auto')
      }
    }
    // Cost so far and Headroom are one column; Running now, Waiting and the
    // failures the other -- never a row that pairs Waiting with Headroom.
    expect(cols[1]!.querySelector('.ov-spend + .ov-headroom')).not.toBeNull()
    expect(cols[0]!.querySelector('.ov-running + .ov-waiting + .ov-failures')).not.toBeNull()
    expect(painted(grid.querySelector(':scope > .ov-pools')!, 'grid-column', WIDE)).toBe('1 / -1')
    host.remove()
  })

  it('keeps O1\'s order in one column on a phone', () => {
    const host = frame()
    const grid = host.querySelector('.ov-cols')!
    for (const col of grid.querySelectorAll(':scope > .ov-col')) expect(painted(col, 'display', PHONE)).toBe('contents')
    const order = (sel: string) => Number(painted(host.querySelector(sel)!, 'order', PHONE) ?? '0')
    const seq = ['.ov-running', '.ov-spend', '.ov-waiting', '.ov-headroom', '.ov-pools', '.ov-failures'].map(order)
    expect([...seq].sort((a, b) => a - b)).toEqual(seq)
    expect(new Set(seq).size).toBe(6)
    host.remove()
  })
})
