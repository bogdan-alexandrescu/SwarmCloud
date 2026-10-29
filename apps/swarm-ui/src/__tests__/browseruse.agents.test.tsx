/**
 * LANE BROWSER-USE-UI (part of #2322). BROWSER-USE-FLAG records, on every
 * task, whether a browser runtime used its browser (`browser_used`, true,
 * false or null, and `browser_use`, its counts); BGD decided on 2026-10-25
 * that the Agents page draws it:
 *
 *   * a task on a browser runtime shows "browser used" (its call and image
 *     counts on hover in the list, on a press in the inspector) or a
 *     warning-toned "browser not used";
 *   * a non-browser task shows nothing;
 *   * a null shows a dash with its reason, never "not used";
 *   * Platform counts links to the Agents list filtered to the browser agents
 *     that did not use it (`?browser=unused`).
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { BrowserUse, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, task as runTask } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadResourceClasses: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { AgHeadMeta } = await import('../agent-split/head')
const { agentListQuery, parseAgentList, BROWSER_UNUSED_ADDRESS } = await import('../agentlist')
const { addressToPath } = await import('../paths')

const BROWSER = 'claude-code-browser'

function counts(over: Partial<BrowserUse> = {}): BrowserUse {
  return { tool_calls: 0, mcp_calls: 0, command_calls: 0, images: 0, seconds: 1800, complete: true, unknown_because: null, ...over }
}

let n = 0
function ended(over: Partial<Task> = {}): Task {
  n += 1
  return runTask({
    id: `task_${String(n).padStart(20, '0')}`,
    state: 'SUCCEEDED',
    started_at: at(1),
    completed_at: at(31),
    ...over,
  })
}

const USED = () =>
  ended({ step_id: 'review-used', runner_profile: BROWSER, browser_used: true, browser_use: counts({ tool_calls: 12, mcp_calls: 9, command_calls: 3, images: 4 }) })
const UNUSED = () => ended({ step_id: 'review-unused', runner_profile: BROWSER, browser_used: false, browser_use: counts() })
const CUT = () =>
  ended({ step_id: 'review-cut', runner_profile: BROWSER, browser_used: null, browser_use: counts({ complete: false, unknown_because: 'stream_cut' }) })
const OLD = () => ended({ step_id: 'review-old', runner_profile: BROWSER })
const PLAIN = () => ended({ step_id: 'implement-plain', runner_profile: 'claude-code', browser_used: null, browser_use: null })

async function list(tasks: Task[], props: Partial<Parameters<typeof AgentsScreen>[0]> = {}) {
  api.loadTasks.mockResolvedValue({ status: 'ok', data: { tasks, tenant_id: 'acme' }, fetchedAt: Date.now() } satisfies Result<TaskPage>)
  const view = render(<AgentsScreen onOpen={() => {}} list={{ tab: 'recent', state: null }} {...props} />)
  await waitFor(() => expect(view.container.querySelector('.row.is-compact')).not.toBeNull())
  return view
}

function rowOf(container: HTMLElement, step: string): HTMLElement {
  const row = [...container.querySelectorAll<HTMLElement>('.row.is-compact')].find((r) => r.textContent?.includes(step))
  expect(row, `no row for ${step}`).toBeDefined()
  return row!
}

describe('the Agents list draws whether a browser runtime used its browser', () => {
  it('a used task says "browser used", its counts on hover and in the row’s name', async () => {
    const { container } = await list([USED()])
    const row = rowOf(container, 'review-used')
    const mark = row.querySelector<HTMLElement>('[data-browser-use]')
    // MUTATION: draw nothing for a browser task, or draw "not used" for a true.
    expect(mark?.getAttribute('data-browser-use')).toBe('used')
    expect(mark!.textContent).toBe('browser used')
    // MUTATION: drop the counts from the hover.
    expect(mark!.getAttribute('title')).toMatch(/12 browser calls \(9 tool, 3 command\) · 4 images/)
    // A keyboard or phone reader has no hover: the opener's name carries them.
    expect(row.querySelector('.cr-open')!.getAttribute('aria-label')).toMatch(/browser used: 12 browser calls/)
  })

  it('an unused task says "browser not used" in the warning tone', async () => {
    const { container } = await list([UNUSED()])
    const mark = rowOf(container, 'review-unused').querySelector<HTMLElement>('[data-browser-use]')
    expect(mark?.getAttribute('data-browser-use')).toBe('unused')
    expect(mark!.textContent).toBe('browser not used')
    // MUTATION: draw it in the neutral or the ok tone.
    expect(mark!.querySelector('[data-tone]')?.getAttribute('data-tone')).toBe('warn')
  })

  it('a null verdict draws nothing on the row, never "not used", and its reason stays in the row’s name', async () => {
    const { container } = await list([CUT(), OLD()])
    for (const [step, reason] of [
      ['review-cut', /browser use: not known: the agent’s output was cut short/],
      ['review-old', /browser use: not recorded/],
    ] as const) {
      const row = rowOf(container, step)
      // Visual QA pass 2 (overview-agents-27): "browser —" between the model
      // and the owner said nothing. MUTATION: draw the unknown mark again.
      expect(row.querySelector('[data-browser-use]'), step).toBeNull()
      expect(row.querySelector('.cr-sub')!.textContent, step).not.toMatch(/browser —|browser use/)
      // MUTATION: read a null as false.
      expect(row.querySelector('.cr-open')!.getAttribute('aria-label'), step).not.toMatch(/not used/)
      expect(row.querySelector('.cr-open')!.getAttribute('aria-label'), step).toMatch(reason)
    }
  })

  it('a browser row keeps its owner and run time: the line wraps between its facts at every width', async () => {
    const { container } = await list([UNUSED(), PLAIN()])
    const sub = rowOf(container, 'review-unused').querySelector<HTMLElement>('.cr-sub')!
    // REVIEW (dark-desktop-02-inspector-used): beside the inspector the mark
    // pushed the owner and the run time past the one line's ellipsis.
    // MUTATION: leave the browser row one cut line on a wide screen.
    expect(sub.className).toMatch(/\bhas-bu\b/)
    expect(painted(sub, 'white-space', WIDE)).toBe('normal')
    for (const fact of sub.querySelectorAll(':scope > *')) expect(painted(fact, 'white-space', WIDE)).toBe('nowrap')
    // A row with no browser mark keeps the one line it had.
    const plain = rowOf(container, 'implement-plain').querySelector<HTMLElement>('.cr-sub')!
    expect(plain.className).not.toMatch(/\bhas-bu\b/)
    expect(painted(plain, 'white-space', WIDE)).toBe('nowrap')
  })

  it('a task on a runtime with no browser draws nothing', async () => {
    const { container } = await list([PLAIN()])
    // MUTATION: flag every claude-code task "browser not used".
    expect(rowOf(container, 'implement-plain').querySelector('[data-browser-use]')).toBeNull()
    expect(rowOf(container, 'implement-plain').textContent).not.toMatch(/browser/)
  })
})

describe('the inspector draws the same fact beside the profile', () => {
  it('opens the counts on a press, and warns on "not used"', () => {
    const { container } = render(<AgHeadMeta task={USED()} />)
    const fact = container.querySelector<HTMLElement>('[data-browser-use]')!
    expect(fact.getAttribute('data-browser-use')).toBe('used')
    const toggle = within(fact).getByRole('button', { name: /browser used/ })
    const detail = fact.querySelector<HTMLElement>('.bu-detail')!
    expect(detail.hidden).toBe(true)
    fireEvent.click(toggle)
    // MUTATION: a hover-only count, unreachable by a tap or a key.
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(detail.hidden).toBe(false)
    expect(detail.textContent).toMatch(/12 browser calls \(9 tool, 3 command\) · 4 images · 30m 0s on the browser runtime/)

    const unused = render(<AgHeadMeta task={UNUSED()} />).container.querySelector<HTMLElement>('[data-browser-use]')!
    expect(unused.getAttribute('data-browser-use')).toBe('unused')
    expect(unused.querySelector('[data-tone]')?.getAttribute('data-tone')).toBe('warn')
  })

  it('a running browser task is a dash until it ends; a non-browser task has no fact', () => {
    const running = runTask({ id: 'task_ffffffffffffffffffff', state: 'RUNNING', runner_profile: BROWSER })
    const fact = render(<AgHeadMeta task={running} />).container.querySelector<HTMLElement>('[data-browser-use]')!
    expect(fact.getAttribute('data-browser-use')).toBe('unknown')
    fireEvent.click(within(fact).getByRole('button'))
    expect(fact.querySelector('.bu-detail')!.textContent).toMatch(/recorded when the agent ends/)
    expect(render(<AgHeadMeta task={PLAIN()} />).container.querySelector('[data-browser-use]')).toBeNull()
  })

  it('says what is unknown beside the dash, not a bare "browser —" (QA1-FIX-13, chrome-C08)', () => {
    const running = runTask({ id: 'task_ffffffffffffffffffff', state: 'RUNNING', runner_profile: BROWSER })
    const words = (t: Task) =>
      (render(<AgHeadMeta task={t} />).container.querySelector('[data-browser-use] .bu-toggle')!.textContent ?? '').replace(/\s+/g, ' ').trim()
    // MUTATION: draw the head's fact as the list's bare "browser —" and both of these fail.
    expect(words(running)).toBe('browser use — recorded when the agent ends')
    expect(words(OLD())).toBe('browser use — not recorded')
  })
})

describe('the link from Platform counts filters the Agents list', () => {
  it('the address round-trips as browser=unused', () => {
    const parsed = parseAgentList(['recent'], 'browser=unused')
    expect(parsed?.browserUnused).toBe(true)
    expect(agentListQuery(parsed!)).toBe('browser=unused')
    expect(addressToPath(BROWSER_UNUSED_ADDRESS)).toBe('/agents/recent?browser=unused')
  })

  it('lists only the browser agents whose browser was not used, says so, and clears', async () => {
    const onList = vi.fn()
    const { container } = await list([USED(), UNUSED(), CUT(), OLD(), PLAIN()], {
      list: { tab: 'recent', state: null, browserUnused: true },
      onList,
    })
    const steps = () => [...container.querySelectorAll('.row.is-compact')].map((r) => r.querySelector('.cr-name b')?.textContent)
    // MUTATION: ignore the flag, or let a null verdict through as "not used".
    expect(steps()).toEqual(['review-unused'])
    const chip = container.querySelector<HTMLElement>('[data-filter="browser-unused"]')!
    expect(chip.textContent).toMatch(/browser not used/)
    fireEvent.click(within(chip).getByRole('button', { name: /Clear the browser filter/ }))
    expect(steps()).toHaveLength(5)
    expect(onList).toHaveBeenLastCalledWith({ tab: 'recent', state: null })
    expect(screen.queryByText(/browser not used/, { selector: '[data-filter] *' })).toBeNull()
  })

  it('the Recent state counts count what the filter lets through', async () => {
    const failedUnused = () => ended({ step_id: 'review-unused-failed', state: 'FAILED', runner_profile: BROWSER, browser_used: false, browser_use: counts() })
    await list([USED(), UNUSED(), failedUnused(), CUT(), OLD(), PLAIN()], { list: { tab: 'recent', state: null, browserUnused: true } })
    const seg = () => screen.getByRole('group', { name: 'Recent, by state' })
    const count = (word: string) => {
      const b = within(seg()).getAllByRole('button').find((x) => (x.textContent ?? '').startsWith(word))!
      return Number((b.textContent ?? '').replace(/\D+/g, ''))
    }
    // REVIEW (light-desktop-03-filtered): `all 6 · succeeded 6` over the two
    // rows drawn. MUTATION: leave the browser filter out of `stateCounts`.
    expect(count('all')).toBe(2)
    expect(count('failed')).toBe(1)
    expect(count('succeeded')).toBe(1)
  })
})
