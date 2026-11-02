/**
 * AN AGENT'S DETAILS PANE, THE OWNER'S DESIGN CALLS (QAFIX4-AGENT-PANES-2,
 * part of #1680; Console Design Calls 2, 2026-10-22).
 *
 * c05, the Details part (box comment 6298220484): every count says what it
 * is. The over-time line's `of 2 cores` is the enforced limit, and says so:
 * `(cgroup limit)`.
 *
 * c06 (box comment 6314119814): Peak memory is the NEWEST attempt's own
 * figure, so it agrees with the Attempts tab. It showed the worst of every
 * measured attempt, even while a later one ran. When an earlier attempt was
 * worse, the caption names it -- `worst 7.20 GiB, attempt 2` -- in the alert
 * tone, so the sizing warning stays in view; Resources' Memory row says it
 * whole.
 *
 * c07 (box comment 6314120120): the two clocks are named. The Elapsed tile is
 * this attempt's run (`Run · attempt 3`, `Ran · attempt 3` once it ends); the
 * Progress bars' figure is the whole attempt, queue and cold start included.
 *
 * c08 (box comment 6314120811): the state card leads at every width. A phone
 * moved the five figures above it.
 *
 * Card titles are headings (the follow-up to QAFIX4-AGENT-PANES): Decision,
 * Why it's waiting and Questions for you were a bold `<b>`. They are `h2`,
 * the level of every other Details card, drawn as the `<b>` was.
 */
import { act, render, screen, within } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, ResourceClasses, TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, Waiting } from '../types'
import type { UtilisationAttempt, UtilisationRead } from '../utilisation'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadUtilisation: vi.fn(),
  loadQuestions: vi.fn(),
  loadWorkflow: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskInputOnce: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

// One snapshot object: `useSyncExternalStore` loops on a fresh one per call.
const feed = vi.hoisted(() => ({ snapshot: { status: 'loading', since: 0 } as unknown }))

vi.mock('../notificationFeed', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../notificationFeed')>()
  return {
    ...actual,
    notificationsSnapshot: () => feed.snapshot,
    subscribeNotifications: () => () => {},
    markRead: vi.fn(async () => {}),
  }
})

const { Run } = await import('../AgentDetail')
const { UtilisationPanel } = await import('../UtilisationPanel')
const { DecisionCard } = await import('../DecisionCard')
const { WaitPanel } = await import('../WaitPanel')
const { QuestionsCard } = await import('../QuestionsCard')

const ID = 'task_0123456789abcdef0124'
const GIB = 1024 ** 3
const MIN = 60_000
const WIDE: CascadeEnv = { width: 1440 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

const CLASSES = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'mock',
    resource_class: 'standard',
    created_at: ago(40),
    started_at: ago(3),
    completed_at: null,
    attempt_count: 3,
    max_attempts: 3,
    ...over,
  })
}

/** Three attempts: 1 and 2 ended, 3 the newest -- open unless `ended`. */
function threeAttempts(ended: boolean, newest: Partial<AttemptRow> = {}): AttemptRow[] {
  return [
    attempt(1, { task_id: ID, created_at: ago(40), started_at: ago(39), completed_at: ago(30), exit_code: 1, peak_rss_bytes: 2 * GIB }),
    attempt(2, {
      task_id: ID,
      created_at: ago(29),
      started_at: ago(28),
      completed_at: ago(5),
      exit_code: 1,
      peak_rss_bytes: 7.2 * GIB,
      oom_near_miss: true,
    }),
    attempt(3, {
      task_id: ID,
      created_at: ago(4),
      started_at: ago(3),
      completed_at: ended ? ago(0) : null,
      exit_code: ended ? 0 : null,
      ...newest,
    }),
  ]
}

function runOf(task: Task, attempts: AttemptRow[], events: AgentRun['events'] = []): AgentRun {
  return {
    task,
    events,
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

const hosts: HTMLElement[] = []
function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}

function tile(el: HTMLElement, label: RegExp): HTMLElement {
  const t = [...el.querySelectorAll<HTMLElement>('.dt-strip > .dt-sc')].find((c) => label.test(c.querySelector('.dt-sc-l')?.textContent ?? ''))
  expect(t, `no tile labelled ${label}`).toBeDefined()
  return t!
}

function resourcesNote(el: HTMLElement, name: string): string {
  const row = [...el.querySelectorAll<HTMLElement>('.dt-resources .dt-rr')].find((r) => r.textContent?.startsWith(name))
  expect(row, `no Resources row ${name}`).toBeDefined()
  return row!.textContent ?? ''
}

// ONE INSTANT FOR THE WHOLE FILE (PR #2305's CI, 2026-10-25). `useNow`'s
// clock is module state, and under `renderToStaticMarkup` nothing subscribes
// to it, so it keeps the instant of the file's first static render. Every
// fixture after that was built on a later `Date.now()`, and on a slow runner
// c07's `Run` tile read `2m 58s` for a start three minutes back. Only `Date`
// is faked: the timers the client-rendered cases wait on stay real.
const T0 = Date.now()

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'], now: T0 })
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
  api.loadWorkflow.mockReturnValue(new Promise(() => {}))
  api.loadUtilisation.mockReturnValue(new Promise(() => {}))
  api.loadQuestions.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('c05: the over-time line names the limit it counts against', () => {
  function util(): UtilisationAttempt {
    const p = (i: number, cpu: number) => ({ at: at(i / 12), cpu_cores: cpu, memory_bytes: 2 * GIB, throttled_share: 0 })
    return {
      attempt_id: 'att_1',
      generation: 1,
      cpu_limit_cores: 2,
      memory_limit_bytes: 8 * GIB,
      throttled_seconds: 0,
      peak_throttled_share: 0,
      mean_throttled_share: 0,
      points: [p(0, 1), p(1, 1), p(2, 1)],
    }
  }

  it('prints no second `of N cores` on the fold’s title: the limit is named once, in Resources (QA1 overview-agents-24/25)', async () => {
    const data: UtilisationRead = { task_id: ID, read_at: at(1), attempts: [util()] }
    api.loadUtilisation.mockResolvedValue(ok(data))
    const { container } = render(<UtilisationPanel taskId={ID} live={false} />)
    await act(async () => {})
    const head = container.querySelector('[data-testid="util-headline"]')
    // MUTATION: put back the mean and its limit on the title -- a second, differently named count.
    expect(head?.textContent).not.toMatch(/cores|cgroup/)
    expect(head?.textContent).toBe('CPU and memory over time · at the limit 0% of the time · throttled 0%')
  })
})

describe('c06: Peak memory is the newest attempt’s figure, with the worst named under it', () => {
  it('shows attempt 3’s running 3 GiB, and names attempt 2’s 7.20 GiB in the alert tone', () => {
    const beat = ev('heartbeat', ago(1), 'att_3', { peak_rss_bytes: 3 * GIB })
    const el = host(runOf(running(), threeAttempts(false), [{ ...beat, task_id: ID }]))
    const t = tile(el, /^Peak memory/)
    // MUTATION: keep memoryOf's `Math.max` over every measured attempt -- the tile reads 7.20 GiB.
    expect(t.querySelector('.dt-sc-v')?.textContent).toMatch(/^3\.00 GiB/)
    const sub = t.querySelector('.dt-sc-s')?.textContent ?? ''
    // MUTATION: drop the `worst …, attempt N` words from the Peak memory caption.
    expect(sub).toContain('worst 7.20 GiB, attempt 2')
    expect(sub).toMatch(/^of 8 GiB · worst 7\.20 GiB, attempt 2 · OOM near miss$/)
    // MUTATION: leave the live tone on the tile when an earlier attempt was worse -- the caption is not the alert's.
    expect(t.classList.contains('is-alert')).toBe(true)
    expect(painted(t.querySelector('.dt-sc-s')!, 'color', WIDE)).toBe('var(--s-warn)')
    // MUTATION: leave Resources' Memory row on its own words -- the worst is said nowhere whole.
    expect(resourcesNote(el, 'Memory')).toContain('worst 7.20 GiB, attempt 2 · OOM near miss')
    expect(resourcesNote(el, 'Memory')).toMatch(/^Memory3\.00 GiB/)
  })

  it('shows the newest attempt’s peak once it ends, and no `worst` caption when no earlier attempt was worse', () => {
    const el = host(
      runOf(
        running({ state: 'SUCCEEDED', completed_at: ago(0) }),
        threeAttempts(true, { peak_rss_bytes: 7.5 * GIB }).map((a) => (a.attempt_id === 'att_2' ? { ...a, oom_near_miss: false } : a)),
      ),
    )
    const t = tile(el, /^Peak memory/)
    expect(t.querySelector('.dt-sc-v')?.textContent).toMatch(/^7\.50 GiB/)
    // MUTATION: name an earlier attempt as `worst` whenever there are several -- attempt 3 is the worst itself.
    expect(t.querySelector('.dt-sc-s')?.textContent ?? '').not.toContain('worst')
    expect(t.classList.contains('is-alert')).toBe(false)
    expect(resourcesNote(el, 'Memory')).not.toContain('worst')
  })
})

describe('c07: the two clocks are named', () => {
  it('labels the Elapsed tile `Run · attempt 3` while it runs, over `since …`', () => {
    const el = host(runOf(running(), threeAttempts(false)))
    const t = tile(el, /^Run/)
    // MUTATION: leave the label `Elapsed` while the task runs.
    expect(t.querySelector('.dt-sc-l')?.textContent).toBe('Run · attempt 3')
    // Started three minutes before the fixture was built, on the file's one frozen instant.
    expect(t.querySelector('.dt-sc-v')?.textContent).toMatch(/^(2m 59s|3m 0s)$/)
    expect(t.querySelector('.dt-sc-s')?.textContent).toMatch(/^since /)
  })

  it('labels it `Ran · attempt 3` once the task has ended', () => {
    const el = host(runOf(running({ state: 'SUCCEEDED', completed_at: ago(0) }), threeAttempts(true)))
    // MUTATION: keep `Run` once the attempt is over -- the tense says it is still going.
    expect(tile(el, /^Ran/).querySelector('.dt-sc-l')?.textContent).toBe('Ran · attempt 3')
  })

  it('heads the Progress bars’ figures `whole attempt: queue + cold start + run`', () => {
    const el = host(runOf(running(), threeAttempts(false)))
    const head = el.querySelector('.dt-progress .dt-phrow.is-head > span:last-child')
    // MUTATION: leave the column head `total incl. queue`.
    expect(head?.textContent).toBe('whole attempt: queue + cold start + run')
  })

  it('lets the tile label wrap in the 115px tile at 1440', () => {
    const el = host(runOf(running(), threeAttempts(false)))
    const label = tile(el, /^Run/).querySelector('.dt-sc-l')!
    // MUTATION: keep `white-space: nowrap` on `.dt-sc-l` -- `Run · attempt 3` runs out of its tile.
    expect(painted(label, 'white-space', { width: 1440, container: 700 })).not.toBe('nowrap')
  })
})

describe('c08: the state card comes first at every width', () => {
  const cases: Array<[string, AgentRun, string]> = [
    ['a failed agent', runOf(running({ state: 'FAILED', completed_at: ago(0), last_error: 'exit 1' }), threeAttempts(true, { exit_code: 1 })), 'failed'],
    ['a running agent', runOf(running(), threeAttempts(false)), 'now'],
    ['a finished agent', runOf(running({ state: 'SUCCEEDED', completed_at: ago(0) }), threeAttempts(true)), 'outcome'],
  ]
  const widths: Array<[string, CascadeEnv]> = [
    ['390', { width: 390, container: 380 }],
    ['1440', { width: 1440, container: 700 }],
  ]
  for (const [who, r, lead] of cases) {
    for (const [w, env] of widths) {
      it(`at ${w}, ${who}: the ${lead} card, then the figures`, () => {
        const el = host(r)
        const pane = el.querySelector<HTMLElement>('.dt')!
        const kids = [...pane.children] as HTMLElement[]
        const card = kids.find((k) => k.getAttribute('data-lead') === lead)
        // The figures are the Resources row now, the strip folded inside it (DETAILS-BUILDING).
        const strip = kids.find((k) => k.classList.contains('dt-res'))
        expect(card, `no ${lead} card`).toBeDefined()
        expect(strip, 'no Resources row').toBeDefined()
        expect(strip!.querySelector('.dt-strip'), 'the strip is not under the Resources row').not.toBeNull()
        // The flex order a reader sees: `order`, then the source order.
        const place = (k: HTMLElement) => [Number(painted(k, 'order', env) ?? '0'), kids.indexOf(k)] as const
        const [co, ci] = place(card!)
        const [so, si] = place(strip!)
        // MUTATION: put `.dt > .dt-strip { order: -1 }` back in details.css's phone block -- the figures lead at 390.
        expect(co < so || (co === so && ci < si), `${lead} card before the figures at ${w}`).toBe(true)
      })
    }
  }
})

describe('the Decision, Why it’s waiting and Questions for you titles are headings', () => {
  function waiting(): Task {
    const w: Waiting = {
      reason: 'CI_PENDING',
      summary: 'CI on PR #1779: 5 checks pending',
      since: at(60),
      next_check_at: at(130),
      resumes_when: 'its pull request checks settle',
      detail: {},
      history: [],
      history_read: true,
      read_at: at(120),
    } as Waiting
    return runTask({ id: ID, state: 'PARKED', park_reason: 'CI_PENDING' as Task['park_reason'], next_eligible_at: at(130), waiting: w })
  }

  function gated(): Task {
    return runTask({
      id: ID,
      workflow_id: 'wf_decide',
      step_id: 'fix',
      state: 'SUCCEEDED',
      result_summary: {
        verdict_gate: { task_id: 'task_review00000000000002', file: 'verdict.json', verdict: 'MERGE', verdict_in: ['NOT_YET'], agent_ran: false },
      },
    })
  }

  function asked(): Task {
    return runTask({ id: ID, state: 'SUCCEEDED', result_summary: { artifacts: [{ name: 'questions.json' }], questions: 1 } })
  }

  const QUESTIONS: TaskQuestions = {
    task_id: ID,
    count: 1,
    status: 'ok',
    attempt_id: 'att_1',
    redacted: false,
    redaction_count: 0,
    questions: [{ question: 'Per task or per workflow?', context: 'one file today', recommended: 'per task', options: [{ label: 'per task', description: 'one per task' }] }],
  }

  const cards: Array<[string, () => JSX.Element]> = [
    ['Decision', () => <DecisionCard task={gated()} readAt={null} />],
    ['Why it’s waiting', () => <WaitPanel task={waiting()} now={Date.parse(at(120))} />],
    ['Questions for you', () => <QuestionsCard task={asked()} />],
  ]

  for (const [name, draw] of cards) {
    it(`titles ${name} with an h2 drawn as the bold title was`, async () => {
      api.loadQuestions.mockResolvedValue(ok(QUESTIONS))
      render(draw())
      await act(async () => {})
      const region = screen.getByRole('region', { name: name.replace('’', "'") })
      // MUTATION: leave the card's title a `<b>` -- the pane's headings outline does not list it.
      const h = within(region).getByRole('heading', { level: 2, name })
      expect(h.parentElement?.classList.contains('dt-card-head')).toBe(true)
      // MUTATION: drop `.dt-card-head > h2` from details.css's title rule -- the h2 takes the browser's size.
      expect(painted(h, ['font-size', 'font'], WIDE), name).toBe('var(--t-body)')
      // MUTATION: drop `font-weight: 650` from the title rule -- the h2 is the browser's bold.
      expect(painted(h, ['font-weight', 'font'], WIDE), name).toBe('650')
      // MUTATION: drop `margin: 0` -- the h2 keeps the browser's margins and the card head grows.
      expect(painted(h, ['margin-top', 'margin'], WIDE), name).toBe('0')
    })
  }
})
