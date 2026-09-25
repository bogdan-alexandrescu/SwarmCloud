// AG-14 (#82). THE WHY LINE IS --warn ONLY WHEN SOMEONE HAS TO ACT.
//
// THE DEFECT, measured on the live console on 2026-09-25: `.row .why` painted
// EVERY row's reason in `--warn` (#ffd60a, 12.6:1 in dark), and `.why-full`
// did the same in the inspector. So "Waiting on an earlier step in its
// workflow" -- which is what a healthy three-step chain says about two of its
// steps for its whole life -- was the same yellow as a failure, and a
// cancellation somebody asked for was the same yellow as a pool nobody can
// admit into. A colour that is on every line says nothing about any of them.
//
// THE OWNER'S DECISION, 2026-09-25: `--warn` only for why lines that need
// action -- failures, can-never-be-admitted, sign-in needed, stuck or silent
// workers. Routine waits (queued, parked on quota, waiting on a dependency)
// and cancellations are plain ink.
//
// "Needs action" is not a new taxonomy invented here. It is the one types.ts
// already keeps: `PARK_NEEDS_A_PERSON` for a park no timer ends, and
// `needsAPerson(blockerCeiling(...))` for a pool paused or capped at zero by
// a person, which is what "can never be admitted" means until someone acts.
//
// MUTATION: paint `.row .why` in `--warn` again, or give every row the
// modifier. The routine rows below then carry it and the cascade reads warn.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { BlockedEntry, Task, TaskPage } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { Run } from '../AgentDetail'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function waiting(id: string, over: Partial<Task>): Task {
  return task({ id, state: 'READY', started_at: null, completed_at: null, ...over })
}

function blocked(b: BlockedEntry): Partial<Task> {
  return { state: 'READY', blocked_by: [b] }
}

/**
 * Every reason `whyAgent` writes a line for, and whether it asks for a person.
 * The waiting rows land on the Waiting tab; the ended ones are on Recent.
 */
const ROWS: { t: Task; act: boolean; why: string }[] = [
  // ROUTINE WAITS -- plain ink.
  {
    t: waiting('tsk_queued00', blocked({ pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 8, active: 8 })),
    act: false,
    why: 'queued behind a full pool',
  },
  {
    t: waiting('tsk_quota000', { state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED' }),
    act: false,
    why: 'parked on quota',
  },
  {
    t: waiting('tsk_depends0', { state: 'PARKED', park_reason: 'DEPENDENCY_INCOMPLETE' }),
    act: false,
    why: 'waiting on a dependency',
  },
  // NEEDS A PERSON -- --warn.
  {
    t: waiting('tsk_signin00', { state: 'PARKED', park_reason: 'CREDENTIAL_MISSING' }),
    act: true,
    why: 'sign-in needed: no provider key',
  },
  {
    t: waiting('tsk_zeroed00', blocked({ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 0, active: 0 })),
    act: true,
    why: 'can never be admitted: pool set to zero',
  },
  {
    t: waiting('tsk_paused00', { state: 'PARKED', park_reason: 'MANUAL_PAUSE' }),
    act: true,
    why: 'can never be admitted: pool paused',
  },
  // ENDED.
  {
    t: task({ id: 'tsk_failed00', state: 'FAILED', last_error: 'exit 1: the agent crashed', completed_at: '2026-09-22T11:00:00.000Z' }),
    act: true,
    why: 'a failure',
  },
  {
    t: task({ id: 'tsk_cancel00', state: 'CANCELLED', cancel_requested: true, completed_at: '2026-09-22T11:00:00.000Z' }),
    act: false,
    why: 'cancelled by request',
  },
  {
    t: task({
      id: 'tsk_cascade0',
      state: 'CANCELLED',
      cancel_requested: false,
      depends_on: ['tsk_upstream'],
      completed_at: '2026-09-22T11:00:00.000Z',
    }),
    act: false,
    why: 'cancelled because an upstream step did not succeed',
  },
]

function page(): TaskPage {
  return { tasks: ROWS.map((r) => r.t), tenant_id: 'acme' }
}

/** The why line of the row that draws task `id`. */
function whyOf(id: string): HTMLElement {
  const cell = document.querySelector(`.row .id[title="${id}"]`)
  expect(cell, `no row draws ${id}`).not.toBeNull()
  const why = cell!.closest('.row')!.querySelector<HTMLElement>('.why')
  expect(why, `the row for ${id} has no why line`).not.toBeNull()
  return why!
}

describe('the Agents list paints a why line --warn only when it asks for a person', () => {
  it('marks exactly the rows that need action, on both tabs that carry reasons', async () => {
    api.loadTasks.mockResolvedValue({ status: 'ok', data: page(), fetchedAt: Date.now() })
    render(<AgentsScreen onOpen={() => {}} />)
    // It lands on Waiting: nothing here holds a slot, so Live is empty.
    await waitFor(() => expect(document.querySelector('.row .why')).not.toBeNull())

    const visited: string[] = []
    const check = (r: (typeof ROWS)[number]) => {
      const why = whyOf(r.t.id)
      visited.push(r.t.id)
      expect(why.classList.contains('is-warn'), `${r.why} (${r.t.id}): warn is ${!r.act ? 'not ' : ''}its ink`).toBe(r.act)
    }

    for (const r of ROWS.filter((x) => x.t.state === 'READY' || x.t.state === 'PARKED')) check(r)
    fireEvent.click(screen.getByRole('tab', { name: /Recent/ }))
    await waitFor(() => expect(document.querySelector('.row .id[title="tsk_failed00"]')).not.toBeNull())
    for (const r of ROWS.filter((x) => x.t.state === 'FAILED' || x.t.state === 'CANCELLED')) check(r)

    // Every row above was looked at; a filter that dropped one would pass.
    expect(visited).toHaveLength(ROWS.length)
  })
})

describe('the inspector paints its why sentence by the same rule', () => {
  function run(t: Task): AgentRun {
    return {
      task: t,
      events: [],
      eventsDetail: null,
      attempts: [attempt(1)],
      attemptsDetail: null,
      classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
      classesDetail: null,
      classesRouteMissing: false,
    }
  }

  /** Whether the inspector's why sentence for `t` carries the warn modifier. */
  async function warns(t: Task): Promise<boolean> {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const { container, unmount } = render(<Run run={run(t)} />)
    const warn = await waitFor(() => {
      const p = container.querySelector<HTMLElement>('.why-full')
      expect(p, 'the inspector drew no why sentence').not.toBeNull()
      return p!.classList.contains('is-warn')
    })
    unmount()
    return warn
  }

  it('is plain for a park on quota and --warn for a missing credential', async () => {
    const quota = waiting('tsk_quota000', { state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED' })
    expect(await warns(quota), 'a quota park is a routine wait').toBe(false)
    const key = waiting('tsk_signin00', { state: 'PARKED', park_reason: 'CREDENTIAL_MISSING' })
    expect(await warns(key), 'a missing key needs a person').toBe(true)
  })
})

describe('the sheet draws the two inks the markup asks for', () => {
  function fragment(html: string): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML = html
    document.body.appendChild(host)
    hosts.push(host)
    return host
  }

  function ink(el: Element, env: CascadeEnv): string | null {
    const r = cascade(STYLES, el, 'color', env)
    expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
    return r.winner?.value ?? null
  }

  it('the list row: plain ink by default, --warn with the modifier, at both widths', () => {
    const f = fragment(
      '<div class="rows"><div class="row"><span class="why">plain</span></div>' +
        '<div class="row"><span class="why is-warn">act</span></div></div>',
    )
    const [plain, act] = [...f.querySelectorAll('.why')]
    for (const env of [WIDE, PHONE]) {
      expect(ink(plain!, env), 'a routine why line is still painted a state colour').toBe('var(--text)')
      expect(ink(act!, env)).toBe('var(--warn)')
    }
  })

  it('the inspector sentence: plain ink by default, --warn with the modifier', () => {
    const f = fragment('<p class="why-full">plain</p><p class="why-full is-warn">act</p>')
    const [plain, act] = [...f.querySelectorAll('.why-full')]
    expect(ink(plain!, WIDE)).toBe('var(--text)')
    expect(ink(act!, WIDE)).toBe('var(--warn)')
  })
})
