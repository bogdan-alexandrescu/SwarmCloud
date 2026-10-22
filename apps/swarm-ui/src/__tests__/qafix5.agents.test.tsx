/**
 * LANE QAFIX5-AGENTS (part of #1680, visual QA run 5b, wf_025ae735511546d99eff,
 * the fixture build of main at 58cd0c1e, 1440 and 390 px, light and dark). An
 * agent's pane tabs and Logs toolbar, and the Agents list's rows.
 *
 *   5b:QA-007         every pane tab with a count failed axe
 *                     label-content-name-mismatch (WCAG 2.5.3): its text ran
 *                     `Changesnot yet` / `Attempts3` against the name
 *                     `Changes, not yet` / `Attempts, 3`.
 *   5b:ff-desktop-07  the Logs error control drew `Error –/1` while it was
 *                     named `No error matched` and disabled: the count is read
 *                     off the log's DOM, and a line a child drew on its own
 *                     (the transcript's next window) was never counted.
 *   r4:flow-find-failure-desktop-D6  a finished agent's chip read `Complete`
 *                     on the narrow bar, saying nothing of what is complete.
 *   5b:ff-desktop-08  the attempt picker said `running` for an attempt of a
 *                     task that had ended.
 *   r2:work-a-007     at 390 a live row's meta line was one ellipsized line,
 *                     so the profile, model and owner were cut.
 *
 * 5b:ff-desktop-01 + ff-phone-01 (both failures `34m ago`) is the fixture's
 * data: its two failed tasks end six seconds apart. No test here; the PR body
 * and questions.json say so.
 *
 * Layout through `painted` (marks.ts), as access.page.test.tsx does: jsdom
 * applies no `@media` block.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgTabsEdge, paneTabs } = await import('../agent-split/tabs')
const { AgentLogs } = await import('../AgentLogs')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390, height: 844 }
const ID = 'task_0123456789abcdef0123'
const MIN = 60_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code', completed_at: null, ...over })
}
const failed = () => row({ state: 'FAILED', completed_at: ago(30), attempt_count: 3, current_generation: 3 })

/** What axe compares (label-content-name-mismatch): lower case, no punctuation, one space. */
function words(s: string | null | undefined): string {
  return (s ?? '')
    .toLowerCase()
    .replace(/[\p{P}\p{S}]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

// ---------------------------------------------------------------- QA-007

// THE BAR AS A 390 PHONE LAYS IT OUT, so some tabs go to More. jsdom lays
// nothing out, so the widths are given (as qafix4.agent.head does).
const WIDTH: Readonly<Record<string, number>> = {
  Details: 62,
  Logs: 46,
  Changes: 130,
  Children: 90,
  Attempts: 84,
  Artifacts: 130,
  Checkpoints: 118,
}
let room = 358
const proto = HTMLElement.prototype
const had = {
  client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
  offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
}
function sizeTabs() {
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-edge') ? room : 0
    },
  })
  Object.defineProperty(proto, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (this.classList.contains('ag-tabs-more-host')) return 110
      if (this.getAttribute('role') !== 'tab' || this.closest('.ag-tabs-edge') === null) return 0
      return WIDTH[this.querySelector('.c-tab-label')?.textContent ?? ''] ?? 0
    },
  })
}
function unsizeTabs() {
  for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset]] as const) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
}

function bar(t: Task) {
  const tabs = paneTabs({ task: t, base: '/agents/live/x', hasBrowser: false, sessionOpen: false, childCount: null, runs: 3, ckpt: null, ckptCount: 2 })
  render(
    <div className="drawer ctl-drawer ag-split">
      <AgTabsEdge
        current="detail"
        onSelect={() => {}}
        tabs={tabs.map((x) => ({
          key: x.id,
          label: x.label,
          ...((x.id === 'detail' && x.count === null) || x.id === 'logs' ? {} : { count: x.count, why: x.say ?? undefined }),
        }))}
      />
    </div>,
  )
  act(() => {
    fireEvent(window, new Event('resize'))
  })
  return screen.getByRole('tablist', { name: 'Agent panes' })
}

describe('5b:QA-007: each pane tab’s accessible name holds its visible text, in order', () => {
  beforeEach(() => {
    room = 2000
    sizeTabs()
  })
  afterEach(unsizeTabs)

  it('on the bar: `Changes not yet` is in `Changes, not yet`, `Attempts 3` in `Attempts, 3`', () => {
    const strip = bar(row())
    const counted = within(strip)
      .getAllByRole('tab')
      .filter((t) => t.querySelector('em') !== null)
    expect(counted.length).toBeGreaterThanOrEqual(4)
    for (const t of counted) {
      const name = t.getAttribute('aria-label')
      // MUTATION: drop the space Tabs.tsx draws between the label and its count -- the text runs `Changesnot yet`.
      expect(words(name), `${t.textContent} in ${name}`).toContain(words(t.textContent))
    }
    const changes = within(strip).getByRole('tab', { name: 'Changes, not yet' })
    // MUTATION: drop the separator and `Changes` and `not yet` touch in the DOM text again.
    expect(changes.textContent).toBe('Changes not yet')
    expect(within(strip).getByRole('tab', { name: 'Attempts, 3' }).textContent).toBe('Attempts 3')
    // The badge stays its own element, holding only the count.
    // MUTATION: put the space inside the `<em>` and every test reading the badge reads ` 3`.
    expect(changes.querySelector('em')?.textContent).toBe('not yet')
  })

  it('in More: an item’s text is in its name too', () => {
    room = 358
    bar(failed())
    fireEvent.click(screen.getByRole('button', { name: /^\d+ more tabs:/ }))
    const menu = screen.getByRole('group', { name: 'More agent panes' })
    const items = within(menu)
      .getAllByRole('button')
      .filter((b) => b.querySelector('em') !== null)
    expect(items.length).toBeGreaterThan(0)
    for (const b of items) {
      const name = b.getAttribute('aria-label')
      // MUTATION: drop the space MoreTabs draws between the label and its count -- `Artifactsnot counted`.
      expect(words(name), `${b.textContent} in ${name}`).toContain(words(b.textContent))
    }
  })

  it('draws the separator nowhere on the glass: the tab is a flex row with its own gap', () => {
    const tab = build('.c-tabs > button', hosts)
    // MUTATION: make the tab an inline box -- the space would add to the 6px gap.
    expect(painted(tab, 'display', WIDE)).toBe('flex')
  })
})

// ---------------------------------------------------------- Logs fixtures

function step(n: number, over: Partial<TranscriptStep> = {}): TranscriptStep {
  return {
    id: `L${n}:0`,
    line_offset: n * 100,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: `step ${n}`,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: null,
    ...over,
  }
}

function transcript(steps: TranscriptStep[], next: number | null): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_3',
    attempt: { status: 'latest', known: true, generation: 3, created_at: ago(40), completed_at: ago(30), exit_code: 1 },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: 'final',
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_3/final/agent_stdout.log`,
      object_updated_at: ago(30),
      age_seconds: 1800,
      total_bytes: 2000,
      offset: 0,
      returned_bytes: 1000,
      next_offset: next,
      truncated: false,
      tail_window: null,
    },
    format: 'claude-stream-json',
    steps,
    complete: next === null,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  } as TaskTranscript
}

function stream(name: LogStream['stream']): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/att_3/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_3/logs/${name}.log`,
    content: '',
    total_bytes: 0,
    offset: 0,
    returned_bytes: 0,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: ago(30),
    age_seconds: 1800,
  } as LogStream
}

function logs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_3',
    attempt: { status: 'latest', known: true, generation: 3, created_at: ago(40), completed_at: ago(30), exit_code: 1 },
    read_at: new Date().toISOString(),
    streams: [stream('agent_stderr'), stream('stdout'), stream('stderr')],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_3/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  } as TaskLogs
}

/** The fixture's failed agent: attempt 1 refused at dispatch (never started, no end), 2 and 3 exited 1. */
function threeAttempts() {
  return [
    attempt(1, { attempt_id: 'att_an_3_1', task_id: ID, started_at: null, completed_at: null, exit_code: null, error: 'BACKEND_REJECTED' }),
    attempt(2, { attempt_id: 'att_an_3_2', task_id: ID, exit_code: 1 }),
    attempt(3, { attempt_id: 'att_an_3_3', task_id: ID, exit_code: 1 }),
  ]
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

/** The attempt picker: on the bar, since jsdom lays nothing out and the bar never spills. */
function picker(): Promise<HTMLElement> {
  return within(dock()).findByRole('combobox', { name: 'Attempt, by generation' })
}

describe('Logs toolbar', () => {
  beforeEach(() => {
    localStorage.clear()
    api.loadAttempts.mockResolvedValue(ok({ attempts: threeAttempts() }))
    api.loadTaskLogs.mockResolvedValue(ok(logs()))
    api.loadTranscript.mockImplementation(async (_id: string, opts: { offset?: number } = {}) =>
      opts.offset === 1000
        ? ok(
            transcript(
              [
                step(5, { kind: 'tool_call', tool: { id: 'toolu_1', name: 'Bash', input: '{}' } }),
                step(6, { kind: 'tool_result', role: 'user', text: null, tool_result: { tool_use_id: 'toolu_1', is_error: true, content: 'exit 1', images: 0 } }),
              ],
              null,
            ),
          )
        : ok(transcript([step(1, { text: 'reading the repository' }), step(2, { text: 'running the suite' })], 1000)),
    )
  })

  describe('5b:ff-desktop-07: what the error control shows, its name and its disabled state agree', () => {
    it('counts an error line the transcript’s next window draws, without a redraw of the bar', async () => {
      render(<AgentLogs task={failed()} />)
      const next = await within(dock()).findByRole('button', { name: 'next window' })
      const err = () => dock().querySelector<HTMLButtonElement>('button[aria-keyshortcuts="E"]')!
      expect(err().disabled).toBe(true)
      fireEvent.click(next)
      await waitFor(() => expect(dock().querySelectorAll('[data-log-key].is-err')).toHaveLength(1))
      // MUTATION: drop the MutationObserver that recounts the targets in AgentLogs.tsx -- the error the
      // next window drew is on the glass while the control still says `No error matched` and is disabled.
      await waitFor(() => expect(err().disabled).toBe(false))
      const b = err()
      expect(b.querySelector('.ag-logs-long')?.textContent).toBe('Error – of 1')
      expect(b.querySelector('.ag-logs-short')?.textContent).toBe('Error –/1')
      // MUTATION: name the control from a different count than it draws -- the name says `No error matched`.
      expect(screen.getByRole('button', { name: /^Error – of 1/ })).toBe(b)
    })

    it('keeps the owner’s `0 err ln`, disabled, while nothing matched', async () => {
      render(<AgentLogs task={failed()} />)
      await within(dock()).findByRole('button', { name: 'next window' })
      const b = dock().querySelector<HTMLButtonElement>('button[aria-keyshortcuts="E"]')!
      // MUTATION: change the zero count's short words -- the owner kept `0 err ln` (N13).
      expect(b.querySelector('.ag-logs-short')?.textContent).toBe('0 err ln')
      expect(b.querySelector('.ag-logs-long')?.textContent).toBe('No error matched')
      expect(b.disabled).toBe(true)
    })
  })

  describe('r4:flow-find-failure-desktop-D6: the finished chip says what is complete', () => {
    it('reads `Log done` on the narrow bar, in eight characters, and says the log on the wide one', async () => {
      render(<AgentLogs task={failed()} />)
      const chip = await waitFor(() => dock().querySelector<HTMLElement>('.ag-logs-final')!)
      const short = chip.querySelector('.ag-logs-short')?.textContent ?? ''
      // MUTATION: put `Complete` back -- it says nothing of what is complete.
      expect(short).not.toBe('Complete')
      expect(short).toMatch(/\blog\b/i)
      // MUTATION: give the short label a ninth character -- it spills Error under More at the 380px split.
      expect(short.length).toBeLessThanOrEqual(8)
      expect(chip.querySelector('.ag-logs-long')?.textContent).toMatch(/complete log/i)
    })
  })

  describe('5b:ff-desktop-08: an attempt of an ended task never reads `running`', () => {
    it('says the never-started attempt never started and no attempt is running', async () => {
      render(<AgentLogs task={failed()} />)
      const box = await picker()
      await waitFor(() => expect(within(box).getAllByRole('option')).toHaveLength(3))
      const said = within(box)
        .getAllByRole('option')
        .map((o) => o.textContent ?? '')
      // MUTATION: say `running` again for any attempt with no `completed_at` (attemptEnd).
      for (const s of said) expect(s, s).not.toMatch(/running/)
      const first = said.find((s) => s.includes('att_an_3_1'))!
      expect(first).toMatch(/gen 1 · att_an_3_1 · never started$/)
      expect(said.find((s) => s.includes('att_an_3_3'))).toMatch(/exit 1$/)
    })

    it('says an attempt that ran with no end recorded is `end not recorded`', async () => {
      api.loadAttempts.mockResolvedValue(
        ok({ attempts: [...threeAttempts().slice(0, 2), attempt(3, { attempt_id: 'att_an_3_3', task_id: ID, completed_at: null, exit_code: null })] }),
      )
      render(<AgentLogs task={failed()} />)
      const box = await picker()
      await waitFor(() => expect(within(box).getAllByRole('option')[0]?.textContent).toMatch(/att_an_3_3/))
      // MUTATION: drop the ended-task branch of attemptEnd -- the current attempt reads `running`.
      expect(within(box).getAllByRole('option')[0]?.textContent).toMatch(/gen 3 · att_an_3_3 · end not recorded$/)
    })

    it('still says `running` for a running agent’s open attempt', async () => {
      api.loadAttempts.mockResolvedValue(
        ok({ attempts: [attempt(1, { attempt_id: 'att_live_1', task_id: ID, completed_at: null, exit_code: null })] }),
      )
      render(<AgentLogs task={row({ attempt_count: 1, current_generation: 1 })} />)
      const box = await picker()
      // MUTATION: never say `running` -- a live attempt reads as ended.
      await waitFor(() => expect(within(box).getAllByRole('option')[0]?.textContent).toMatch(/att_live_1 · running$/))
    })
  })
})

// ------------------------------------------------------------ work-a-007

describe('r2:work-a-007: at 390 a row’s meta line wraps between its facts, so none is cut', () => {
  it('wraps the line at 390 and keeps each fact whole', () => {
    const sub = build('.row.is-compact > .cr-sub', hosts)
    // MUTATION: drop the 560px `white-space: normal` on `.row.is-compact > .cr-sub` in agents.css --
    // the line is one ellipsized line and `claude-opus…` cuts the owner off.
    expect(painted(sub, 'white-space', PHONE)).toBe('normal')
    const fact = build('.row.is-compact > .cr-sub > .cr-owner', hosts)
    // MUTATION: drop `white-space: nowrap` on the line's facts -- `attempt 1 of 3` breaks inside itself.
    expect(painted(fact, 'white-space', PHONE)).toBe('nowrap')
  })

  it('keeps the wide list’s one line', () => {
    const sub = build('.row.is-compact > .cr-sub', hosts)
    // MUTATION: let the line wrap at every width -- a wide row grows a second meta line it has room for.
    expect(painted(sub, 'white-space', WIDE)).toBe('nowrap')
  })
})
