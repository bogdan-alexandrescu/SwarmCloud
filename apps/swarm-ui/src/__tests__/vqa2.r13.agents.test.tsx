// VQA2-R13 (part of #1427): the Agents list, split and drawer, from the
// 2026-10-16 visual QA re-run. One describe per finding; each pins the rule,
// the class, the text or the focus call whose absence the finding measured.
//
//   V060  the park bar printed `toLocaleString()` beside a row in `clockTime`
//   V147  `Started` dropped alone to a second toolbar row; `«` took a row
//   V260  a Details sub-line wrapped to a line opening with an orphaned `·`
//   V261  Attempts drew one absent value twice: `exec — none`
//   V262  a split opened by its URL on a phone was focused with a ring

import { describe, expect, it, vi } from 'vitest'
import { render, renderHook, waitFor } from '@testing-library/react'
import type { RefObject } from 'react'

import AGENTS_CSS from '../styles/agents.css?raw'
import DETAILS_CSS from '../styles/details.css?raw'
import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskPage } from '../types'
import { clockTime } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run, ParkEligible, glueSeparators } = await import('../AgentDetail')
const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { AgentsScreen } = await import('../Agents')
const { useSplitFocus, openedByAddress, focusSplitOnOpen } = await import('../agent-split/focus')

function run(t: Task, attempts: AttemptRow[]): AgentRun {
  return {
    task: t,
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function draw(r: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
  return container
}

/** One CSS rule's body, by its exact selector. */
function rule(css: string, selector: string): string | null {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(?:^|\\n|\\})\\s*${esc}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? null
}

describe('V060: the park bar prints the instant the way the Waiting row does', () => {
  const PAST = '2026-10-16T18:28:17Z'

  it('says `eligible since <clockTime>` on the Details bar, never toLocaleString()', async () => {
    const t = task({
      state: 'PARKED',
      park_reason: 'PROVIDER_QUOTA_EXHAUSTED',
      started_at: at(1),
      completed_at: null,
      next_eligible_at: PAST,
    })
    const c = await draw(run(t, [attempt(1)]))
    const bar = [...c.querySelectorAll<HTMLElement>('.bar.amber')].find((b) => b.textContent?.includes('eligible'))
    expect(bar, 'the park bar is gone').toBeTruthy()
    expect(bar!.textContent).toContain(`eligible since ${clockTime(PAST)!.text}`)
    expect(bar!.textContent).not.toContain(new Date(PAST).toLocaleString())
  })

  it('says `at` for an instant still to come and `since` for one gone, with the UTC instant as its title', () => {
    const now = Date.parse('2026-10-16T18:00:00Z')
    const later = '2026-10-16T18:10:00Z'
    const { container: a } = render(<ParkEligible iso={later} now={now} />)
    expect(a.textContent).toBe(` · eligible at ${clockTime(later, now)!.text}`)
    expect(a.querySelector('span')!.title).toContain('2026-10-16T18:10:00.000Z')
    const { container: b } = render(<ParkEligible iso={PAST} now={Date.parse('2026-10-16T19:00:00Z')} />)
    expect(b.textContent).toMatch(/^ · eligible since \d{2}:\d{2}:\d{2}$/)
    const { container: none } = render(<ParkEligible iso={null} />)
    expect(none.textContent).toBe('')
  })
})

describe('V147: the list toolbar wraps its controls together', () => {
  const NOW = Date.parse('2026-10-01T12:00:00.000Z')
  const iso = (m: number) => new Date(NOW - m * 60_000).toISOString()

  async function land(): Promise<HTMLElement> {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: {
        tasks: [
          task({ id: 'task_a', state: 'SUCCEEDED', created_at: iso(60), updated_at: iso(1), completed_at: iso(1) }),
          task({ id: 'task_b', state: 'FAILED', created_at: iso(60), updated_at: iso(2), completed_at: iso(2) }),
        ],
        tenant_id: 'acme',
      },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    const { container } = render(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await waitFor(() => expect(container.querySelector('.rows .row.clickable')).not.toBeNull())
    return container as HTMLElement
  }

  it('puts every control after the search in one `.ag-tools` item, Started in its unbreakable tail', async () => {
    const c = await land()
    const head = c.querySelector('.ag-list-head')!
    const items = [...head.children].map((e) => e.className)
    expect(items).toEqual(['ag-find', 'ag-tools'])
    const sort = head.querySelector('.ag-sort')!
    expect(sort.parentElement!.className).toBe('ag-tools-tail')
    for (const check of head.querySelectorAll('.ag-filter.check')) {
      expect(check.parentElement, 'a checkbox outside the tail').toBe(sort.parentElement)
    }
  })

  it('sizes the group as one item and never breaks its tail', () => {
    const tools = rule(AGENTS_CSS, '.ag-tools')
    expect(tools).toMatch(/flex:\s*0 1 auto/)
    expect(tools).toMatch(/flex-wrap:\s*wrap/)
    const tail = rule(AGENTS_CSS, '.ag-tools-tail')
    expect(tail).toMatch(/flex:\s*none/)
    expect(tail).not.toMatch(/flex-wrap:\s*wrap/)
  })

  it('beside an open agent gives `«` the search line and the controls the next', () => {
    expect(rule(AGENTS_CSS, '.app.has-inspector > .work .ag-list-head > .ag-tools')).toMatch(/flex-basis:\s*100%/)
    expect(rule(AGENTS_CSS, '.app.has-inspector > .work .ag-list-head > .ag-find'), 'the search takes a row from «').toBeNull()
  })
})

describe('V260: a Details sub-line never opens a line with its separator', () => {
  it('glues each ` · ` to the words before it and keeps the text whole', () => {
    const { container } = render(<span>{glueSeparators('of 8 GiB · worst of 2 · OOM near miss')}</span>)
    expect(container.textContent).toBe('of 8 GiB · worst of 2 · OOM near miss')
    const dots = container.querySelectorAll('.dt-dot')
    expect(dots).toHaveLength(2)
    for (const d of dots) expect(d.textContent).toBe(' ·')
    // No text node outside a `.dt-dot` holds a separator a line could open with.
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n !== null; n = walker.nextNode()) {
      if (n.parentElement?.classList.contains('dt-dot')) continue
      expect(n.textContent).not.toContain('·')
    }
  })

  it('walks a node sub-line too (Cost’s), through fragments and spans', () => {
    const { container } = render(
      <span>
        {glueSeparators(
          <>
            summed by the API{' · '}
            <span className="dt-sc-k">in 52 · out 9</span>
          </>,
        )}
      </span>,
    )
    expect(container.textContent).toBe('summed by the API · in 52 · out 9')
    expect(container.querySelectorAll('.dt-dot')).toHaveLength(2)
    expect(container.querySelector('.dt-sc-k .dt-dot')).not.toBeNull()
  })

  it('draws the strip’s memory sub-line with glued separators', async () => {
    const c = await draw(
      run(task({ state: 'FAILED', started_at: at(1), completed_at: at(10) }), [
        attempt(1, { peak_rss_bytes: 7 * 1024 ** 3, oom_near_miss: true, exit_code: 137 }),
      ]),
    )
    const sub = [...c.querySelectorAll<HTMLElement>('.dt-sc-s')].find((s) => s.textContent?.includes('OOM near miss'))
    expect(sub, 'no memory sub-line').toBeTruthy()
    expect(sub!.querySelectorAll('.dt-dot').length).toBeGreaterThan(0)
    expect(sub!.title).toBe(sub!.textContent)
  })

  it('holds the dot on the line before: `.dt-dot` cannot break', () => {
    expect(rule(DETAILS_CSS, '.dt-dot')).toMatch(/white-space:\s*nowrap/)
  })
})

describe('V261: an absent attempt fact is drawn once', () => {
  it('draws the mark alone for execution, ran and peak memory; exit keeps its em dash', async () => {
    const a = attempt(1, {
      execution_name: null,
      started_at: null,
      completed_at: null,
      exit_code: null,
      peak_rss_bytes: null,
    })
    const t = task({ state: 'FAILED', started_at: null, completed_at: at(10) })
    api.loadAttempts.mockResolvedValue({ status: 'ok', data: { attempts: [a] }, fetchedAt: Date.now() })
    api.loadAgentDetail.mockResolvedValue({ status: 'ok', data: { task: t, events: [], eventsDetail: null }, fetchedAt: Date.now() })
    const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
    await waitFor(() => expect(container.querySelector('.att-card .ctl-fact')).not.toBeNull())
    const fact = (k: string) =>
      [...container.querySelectorAll<HTMLElement>('.att-card .ctl-fact')].find((li) => li.querySelector('b')?.textContent === k)!
    for (const [k, word] of [
      ['execution', 'none'],
      ['ran', 'none'],
      ['peak memory', 'not measured'],
    ] as const) {
      const f = fact(k)
      expect(f, `no ${k} fact`).toBeTruthy()
      expect(f.textContent, `${k} draws its absence twice`).toBe(`${k}${word}`)
      expect(f.querySelector('.ctl-mark')).not.toBeNull()
    }
    expect(fact('exit').textContent).toBe('exit—')
  })
})

describe('V262: a split opened by its address takes focus without a ring', () => {
  function mountPanel(): RefObject<HTMLDivElement> {
    const el = document.createElement('div')
    el.tabIndex = -1
    document.body.appendChild(el)
    return { current: el }
  }

  it('asks for no focus ring when nothing held focus as it opened', () => {
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)
    const panel = mountPanel()
    const focus = vi.spyOn(panel.current!, 'focus')
    renderHook(() => useSplitFocus(panel, () => {}))
    // MUTATION: drop `...STILL` from the address open in agent-split/focus.ts -> the
    // panel's focus scrolls the page column under the breadcrumb (overview-agents-55).
    expect(focus).toHaveBeenCalledWith({ focusVisible: false, preventScroll: true })
    panel.current!.remove()
  })

  it('keeps the AG-31 ring when a row opened it', () => {
    const row = document.createElement('button')
    document.body.appendChild(row)
    row.focus()
    const panel = mountPanel()
    const focus = vi.spyOn(panel.current!, 'focus')
    renderHook(() => useSplitFocus(panel, () => {}))
    expect(focus).toHaveBeenCalledTimes(1)
    // The ring is kept; the page column still does not move (overview-agents-55).
    // MUTATION: pass `undefined` again for a row's open in agent-split/focus.ts.
    expect(focus.mock.calls[0]![0]).toEqual({ preventScroll: true })
    panel.current!.remove()
    row.remove()
  })

  it('takes a key-opened focus without scrolling the page column (overview-agents-55)', () => {
    // MUTATION: call `panel.current?.focus()` with no options in the taker in agent-split/focus.ts.
    const row = document.createElement('button')
    document.body.appendChild(row)
    row.focus()
    const panel = mountPanel()
    const focus = vi.spyOn(panel.current!, 'focus')
    focusSplitOnOpen('task_r13_key', row)
    renderHook(() => useSplitFocus(panel, () => {}, 'task_r13_key'))
    expect(focus.mock.calls.length).toBeGreaterThan(0)
    for (const [opts] of focus.mock.calls) expect(opts).toMatchObject({ preventScroll: true })
    panel.current!.remove()
    row.remove()
  })

  it('reads an open by address as focus on <body> or nowhere', () => {
    expect(openedByAddress(null)).toBe(true)
    expect(openedByAddress(document.body)).toBe(true)
    expect(openedByAddress(document.createElement('button'))).toBe(false)
  })
})
