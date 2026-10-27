/**
 * VISUAL QA PASS 1 (2026-10-26, epic #2391), lane VQ-OVERVIEW: the Overview
 * findings VQ-002 to VQ-006, VQ-023, and VQ-056 (the Overview's own words).
 *
 *   VQ-002  On a phone each Needs a look card printed its whole detail and
 *           two workflow-id links, and each Running now row wrapped to four
 *           or five lines. O1's 390 frame: one line a check (mark and
 *           title), one truncated line an agent, its runtime on the right.
 *   VQ-003  Needs a look printed the enums: "FAILED agents created since …",
 *           "Agents · FAILED →", "CI_PENDING ×2 —", "LEASED, DISPATCHED,
 *           STARTING or RUNNING".
 *   VQ-004  The head read "checks 8/8 · 3 found 3 problems".
 *   VQ-005  Needs a look and Recent failures named two cut-offs for one
 *           sample of failures, and Finished today a third, worded apart.
 *   VQ-006  Recent failures sat in the left column, the right blank beside
 *           it under Headroom.
 *   VQ-023  Times in three zones across the console. The Overview's share:
 *           every time it prints says UTC, so its "today" is the UTC day.
 *   VQ-056  The head read "tenant eng"; the console says workspace.
 *
 * Each assertion names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Capacity, ProvidersPage, QuotaState, Stats, Task, TaskPage, TaskState } from '../types'
import type { CascadeEnv } from './cssgate'
import { cascade } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import NAMES_CSS from '../styles/names.css?raw'
import STYLES_CSS from '../styles.css?raw'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTask: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
}))
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const { OverviewScreen } = await import('../Overview')

const WIDE: CascadeEnv = { width: 1440 }
const TABLET: CascadeEnv = { width: 820 }
const PHONE: CascadeEnv = { width: 390 }
/** The bundle's order (qa.qafix3.overview.test.tsx): overview.css, then names.css. */
const BUNDLE = [OVERVIEW_CSS, NAMES_CSS, STYLES_CSS].join('\n')

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const hhmm = (iso: string): string => `${new Date(iso).toISOString().slice(11, 16)} UTC`
const text = (el: Element | null | undefined): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}
const empty = { status: 'empty' as const, fetchedAt: Date.now() }

const CAPACITY: Capacity = { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: new Date().toISOString() }
const SPEND = { tenantId: 'eng', tasksOnPage: 0, tasksWithAttempts: 0, tasksSampled: 0, failedReads: 0, failedDetail: null, attempts: 0, attemptsWithCost: 0, attemptsWithTokens: 0, costUsd: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0, from: null, to: null } as unknown as SpendRollup

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 2, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 1, SUCCEEDED: 300, FAILED: 40, DEAD_LETTERED: 0, CANCELLED: 20 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

function quota(provider: string, state: string): QuotaState {
  return {
    provider,
    tenant_id: 'eng',
    state,
    updated_at: new Date().toISOString(),
    configured_hard_max: 4,
    adaptive_target: null,
    quota_derived_limit: null,
    requests_remaining: null,
    tokens_remaining: null,
    reset_at: null,
    cooldown_until: null,
    last_429_at: ago(2),
    retry_after_seconds: null,
    success_count: 10,
    rate_limit_count: 3,
    effective_limit: 2,
  } as QuotaState
}
const THROTTLED: ProvidersPage = {
  tenant_id: 'eng',
  providers: [{ provider: 'anthropic', credential_registered: true, runner_profiles: [], quota: quota('anthropic', 'THROTTLED') }],
  generated_at: new Date().toISOString(),
} as ProvidersPage

/**
 * The live shape of 2026-10-26: the 200 newest are succeeded agents created
 * in the last two hours and two merge steps parked on CI, so the page has more
 * behind it. The FAILED read
 * reaches back ten hours and has more behind it still; the CANCELLED read
 * reaches back only three. One agent of a long-titled workflow is running.
 */
const FAILED_FROM = ago(600)
const CANCELLED_FROM = ago(180)
const SUCCEEDED: Task[] = Array.from({ length: 198 }, (_, i) =>
  task({ id: `tsk_ok_${i}`, tenant_id: 'eng', state: 'SUCCEEDED', created_at: ago(5 + i / 2), updated_at: ago(2 + i / 2), started_at: ago(4 + i / 2), completed_at: ago(2 + i / 2), attempt_count: 1 }),
)
const FAILED: Task[] = Array.from({ length: 5 }, (_, i) =>
  task({ id: `tsk_bad_${i}`, tenant_id: 'eng', state: 'FAILED', workflow_id: null, last_error: `exit ${i}`, created_at: i === 4 ? FAILED_FROM : ago(200 + i), updated_at: ago(30 + i), started_at: ago(190 + i), completed_at: ago(30 + i), attempt_count: 1, max_attempts: 1 }),
)
const CANCELLED: Task[] = [
  task({ id: 'tsk_cx_0', tenant_id: 'eng', state: 'CANCELLED', created_at: CANCELLED_FROM, updated_at: ago(40), completed_at: ago(40) }),
]
const PARKED: Task[] = ['merge-a', 'merge-b'].map((step, i) =>
  task({ id: `tsk_park_${i}`, tenant_id: 'eng', state: 'PARKED', park_reason: 'CI_PENDING', step_id: step, workflow_id: `wf_merge_${i}`, created_at: ago(20 + i), updated_at: ago(10), next_eligible_at: ago(-30) }),
)
/** The page: the two parked merge steps among the newest, then succeeded agents. Nothing reads PARKED by state. */
const PAGE = (): Task[] => [...PARKED, ...SUCCEEDED]
const RUNNING: Task[] = [
  task({ id: 'tsk_run_0', tenant_id: 'eng', state: 'RUNNING', step_id: 'scan-terraform', workflow_id: 'wf_audit_01', created_at: ago(30), updated_at: ago(1), started_at: ago(20), attempt_count: 1, metadata: null }),
]

const WF_TITLE = 'Audit every script and Terraform module in the repository for drift against main'

function serve(): void {
  api.loadTasks.mockImplementation(async (limit: number) => ok({ tasks: PAGE().slice(0, limit), next_page_token: 'more', tenant_id: 'eng' } satisfies TaskPage))
  const reads: Partial<Record<TaskState, { tasks: Task[]; more: boolean }>> = {
    FAILED: { tasks: FAILED, more: true },
    CANCELLED: { tasks: CANCELLED, more: true },
    RUNNING: { tasks: RUNNING, more: false },
  }
  api.loadTasksInState.mockImplementation(async (state: TaskState) => {
    const r = reads[state]
    return r === undefined ? empty : ok({ tasks: r.tasks, next_page_token: r.more ? 'more' : null, tenant_id: 'eng' } satisfies TaskPage)
  })
}

async function mount(providers: Result<ProvidersPage> = empty): Promise<HTMLElement> {
  serve()
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(providers)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(empty)
  api.loadSpend.mockResolvedValue(ok(SPEND))
  // The running step's workflow has a long title, read once a session.
  api.loadTask.mockImplementation(async (id: string) =>
    id === 'tsk_run_0'
      ? ok({ ...RUNNING[0]!, metadata: { title: WF_TITLE, unit: 'vq' } })
      : { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'gone' } },
  )
  const { container } = render(<OverviewScreen />)
  for (let i = 0; i < 60; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

afterEach(() => {
  cleanup()
  for (const fn of Object.values(api)) fn.mockReset()
})

/** Every state and park-reason enum, as a whole word in capitals. */
const ENUM = /\b(QUEUED|READY|PARKED|LEASED|DISPATCHED|STARTING|RUNNING|SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED|THROTTLED|EXHAUSTED|DISABLED|CI_PENDING|PROVIDER_[A-Z_]+|DEPENDENCY_INCOMPLETE|MANUAL_PAUSE|SCHEDULED_RETRY|BUDGET_EXHAUSTED|CREDENTIAL_MISSING|CHILDREN_INCOMPLETE|HUMAN_REQUIRED|AWAITING_INPUT)\b/

/** What a person reads or hears in a region: its text and every name and tooltip in it. */
function said(root: Element): string[] {
  const out = [text(root)]
  for (const el of [root, ...root.querySelectorAll('*')]) {
    for (const a of ['aria-label', 'title']) {
      const v = el.getAttribute(a)
      if (v) out.push(v)
    }
  }
  return out
}

describe('VQ-004: the Needs a look head says one count, then what it is over', () => {
  it('reads "N problems · R of T checks ran", N the cards drawn', async () => {
    const el = await mount(ok(THROTTLED))
    const cnt = el.querySelector('#ov-needs .ov-lh .ov-cnt')!
    // MUTATION: put back `checks ${ran}/${checks.length} · ${found} found ${problems.length} problems`.
    const m = /^(\d+) (problems?) · (\d+) of (\d+) checks ran$/.exec(text(cnt))
    expect(m, `the head reads ${JSON.stringify(text(cnt))}`).not.toBeNull()
    const cards = el.querySelectorAll('#ov-needs .ov-att:not(.is-skel)').length
    expect(cards, 'no card was drawn, so this measured nothing').toBeGreaterThan(1)
    // MUTATION: count the checks that fired as the figure.
    expect(Number(m![1])).toBe(cards)
    expect(m![2]).toBe(cards === 1 ? 'problem' : 'problems')
    expect(Number(m![3])).toBeLessThanOrEqual(Number(m![4]))
  })
})

describe('VQ-003: Needs a look says states and park reasons as words', () => {
  it('prints no state or reason enum in the cards, their names or their tooltips', async () => {
    const el = await mount(ok(THROTTLED))
    const lead = el.querySelector('#ov-needs')!
    expect(lead.querySelectorAll('.ov-att:not(.is-skel)').length, 'no card was drawn').toBeGreaterThanOrEqual(3)
    // MUTATION: put back 'Agents · FAILED', `FAILED agents created since`, `is ${q.state}` or the raw `${reason}` in whyEach's card.
    for (const s of said(lead)) expect(s).not.toMatch(ENUM)
  })

  it('names the failures link "Agents · failed", the parked reason "waiting for CI", the quota "throttled"', async () => {
    const el = await mount(ok(THROTTLED))
    const lead = el.querySelector('#ov-needs')!
    // MUTATION: put back `linkLabel: 'Agents · FAILED'` in checks.ts.
    const list = [...lead.querySelectorAll('.ov-att-go')].find((a) => text(a).startsWith('Agents'))
    expect(list, 'the failures card has no list link').toBeDefined()
    expect(text(list)).toBe('Agents · failed →')
    // MUTATION: drop `parkProse` from CheckCard.
    expect(text(lead)).toContain('waiting for CI ×2 —')
    // MUTATION: put back `is ${q.state}` in checks.ts's quota check.
    expect(text(lead)).toContain('anthropic is throttled')
  })
})

describe('VQ-005: one sample of failures, one window, the same words', () => {
  it('gives Needs a look and Recent failures the same count and the same cut-off', async () => {
    const el = await mount()
    const card = [...el.querySelectorAll('#ov-needs .ov-att')].find((c) => /failed agents? in the last 24 hours/.test(text(c)))
    expect(card, 'no failures card').toBeDefined()
    const head = card!.querySelector('b')!.getAttribute('title') ?? ''
    const note = text(el.querySelector('#ov-failures .ctl-card-note'))
    // Both off the FAILED read, which has more behind it: a lower bound.
    expect(head).toMatch(/^≥5 failed agents in the last 24 hours\b/)
    expect(note).toMatch(/· ≥5 failed ·/)
    // THE CANCELLED READ REACHES LESS FAR, so it bounds the card's sample.
    // MUTATION: put back `stateView(tasks.data, ['FAILED']).since` as the check's reach.
    const reach = `partial: agents created since ${hhmm(CANCELLED_FROM)} read`
    expect(note).toContain(reach)
    expect(head).toContain(reach)
    expect(head).not.toContain(hhmm(FAILED_FROM))
  })

  it('words Finished today’s partial sample as every other card does', async () => {
    const el = await mount()
    const foot = el.querySelectorAll('#ov-band .ov-lc')[2]!.querySelector('.ov-lc-foot.is-page')!
    // MUTATION: put back ` · created since ${hhmm(finished.since)}` in LifecycleBand.
    expect(text(foot)).toMatch(/^of the 200 newest read · partial: agents created since \d\d:\d\d UTC read$/)
  })
})

describe('VQ-006: Recent failures is a full-width row under both columns', () => {
  it('sits outside the columns, across both, before the pools', async () => {
    const el = await mount()
    const fails = el.querySelector('#ov-failures')!
    // MUTATION: put the section back inside the left `.ov-col`.
    expect(fails.parentElement!.classList.contains('ov-cols')).toBe(true)
    expect(fails.closest('.ov-col')).toBeNull()
    // MUTATION: drop `.ov-cols > .ov-failures` from the 1100px rule's `grid-column`.
    expect(painted(fails, 'grid-column', WIDE)).toBe('1 / -1')
    const pools = el.querySelector('#ov-pools')!
    // MUTATION: put the section after the pools.
    expect(fails.compareDocumentPosition(pools) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // One column below 1100px: still before the pools.
    const order = (n: Element) => Number(painted(n, 'order', TABLET) ?? '0')
    // MUTATION: leave `.ov-failures` at order 6 and the pools at 5 under 1100px.
    expect(order(fails)).toBeLessThan(order(pools))
    expect(order(el.querySelector('#ov-headroom')!)).toBeLessThan(order(fails))
  })
})

describe('VQ-002: a phone draws a check as one line and an agent as one line', () => {
  it('steps the detail, the ids and the link words aside at 390, and keeps them at 1440', async () => {
    const el = await mount(ok(THROTTLED))
    const cards = [...el.querySelectorAll('#ov-needs .ov-att:not(.is-skel)')]
    expect(cards.length).toBeGreaterThanOrEqual(3)
    for (const c of cards) {
      for (const small of c.querySelectorAll('.ov-att-t small')) {
        // MUTATION: drop `.ov-att-t small { display: none }` from the 560px block.
        expect(painted(small, 'display', PHONE)).toBe('none')
        // MUTATION: move the phone rule out of the 560px block, onto every width.
        expect(painted(small, 'display', WIDE)).not.toBe('none')
      }
      // The arrow stays and its words step aside visually, never from the name.
      const words = c.querySelector('.ov-att-go-w')
      if (words !== null) {
        // MUTATION: drop `.ov-att-go-w` from the 560px block's visually-hidden rule.
        expect(painted(words, 'clip', PHONE)).toBe('rect(0 0 0 0)')
        expect(painted(words, 'display', PHONE)).not.toBe('none')
        expect(painted(words, 'clip', WIDE)).toBeNull()
      }
    }
    // A card that names a workflow keeps one way out, the card's cover.
    const split = el.querySelector('#ov-needs .ov-att.is-split')
    if (split !== null) {
      const cover = split.querySelector('.ov-att-wf.is-cover')!
      expect(painted(cover, 'display', PHONE)).not.toBe('none')
      const list = [...split.querySelectorAll('a.ov-att-go')].find((a) => !a.classList.contains('ov-att-wf'))!
      // MUTATION: drop the split card's list link from the 560px block's `display: none`.
      expect(painted(list, 'display', PHONE)).toBe('none')
      expect(painted(list, 'display', WIDE)).not.toBe('none')
    }
  })

  it('cuts a Running row’s workflow title on one line and keeps its step whole', async () => {
    const el = await mount()
    const name = el.querySelector('#ov-running .ov-prun li a.ov-prun-n')!
    expect(name, 'no phone Running row').not.toBeNull()
    const at = (n: Element, prop: string | readonly string[]) => {
      const r = cascade(BUNDLE, n, prop, PHONE)
      expect(r.unsupported).toEqual([])
      return r.winner?.value ?? null
    }
    // MUTATION: put back `white-space: normal` on `.ov-prun-n` (ffp-002's wrap).
    expect(at(name, 'white-space')).toBe('nowrap')
    expect(at(name, 'display')).toBe('flex')
    const part = name.querySelector('.ov-prun-t')!
    // MUTATION: drop the ellipsis from `.ov-prun-t`.
    expect(at(part, 'text-overflow')).toBe('ellipsis')
    expect(at(part, ['overflow', 'overflow-x'])).toBe('hidden')
    // The title gives way first: the name keeps the step that tells rows apart.
    expect(text(part)).toBe(WF_TITLE)
    const step = name.querySelector('.ov-prun-s')!
    // MUTATION: draw `rowLabel` as one text again.
    expect(step, 'the step is not a part of its own').not.toBeNull()
    // MUTATION: let `.ov-prun-s` shrink with the title.
    expect(at(step, 'flex')).toBe('none')
    expect(text(name)).toBe(`${WF_TITLE} · scan-terraform`)
  })
})

describe('VQ-056: the Overview says workspace, never tenant', () => {
  it('names the workspace in the head and nowhere prints "tenant"', async () => {
    const el = await mount(ok(THROTTLED))
    const scope = el.querySelector('.c-count-note .ov-scope')!
    // MUTATION: put back `tenant ${tenant}` in the head's scope.
    expect(text(scope)).toBe('workspace eng ·')
    // MUTATION: put back `quota, all tenants · admin` in checks.ts.
    expect(text(el)).toContain('quota, all workspaces · admin')
    // MUTATION: put back any reader-facing "tenant" on this page.
    expect(text(el)).not.toMatch(/\btenants?\b/i)
  })
})

describe('VQ-023: every time the Overview prints names its zone, and it is UTC', () => {
  it('follows each clock time on the page with " UTC"', async () => {
    const el = await mount(ok(THROTTLED))
    const page = text(el)
    const times = [...page.matchAll(/\b\d{1,2}:\d{2}(?::\d{2})?\b(.{0,4})/g)]
    // The partial reads name their cut-off, so the page prints at least one time.
    expect(times.length, 'no clock time on the page, so this measured nothing').toBeGreaterThan(0)
    // MUTATION: print a cut-off with `clockTime` (the browser's zone, unlabelled).
    for (const m of times) expect(m[0], `"${m[0]}" carries no zone`).toMatch(/ UTC/)
    // "today" is the UTC day on this page, said where it is counted.
    expect(page).not.toMatch(/\b(PDT|PST|America\/)/)
  })
})
