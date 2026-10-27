/**
 * DETAILS SAYS AN ATTEMPT RAN THE AGENT MORE THAN ONCE (SWAP-RUNS, #2457).
 *
 * On 2026-10-26 two 7,200 s timeouts read as "agent ran 4,600 s": an account
 * swap at minute 42 restarted the agent, and nothing on the page said so. The
 * worker now records every run in `result_summary.runs` and each swap in
 * `.swaps` -- only when there was more than one run -- and the Run tile's
 * sub-line says it beside the duration: `2 runs · 1 account swap (ai-eng-2
 * exhausted → ai-eng-1-max at 09:50)`. A one-run attempt's tile is unchanged.
 *
 * THE CLOCK IS PINNED TO THE FIXTURE'S DAY (MAIN-RED-SWAPRUNS, #2391). The
 * tile writes its start and end with `clockTime`, which reads times alone for
 * an instant on today's date and `MM-DD HH:MM` for an older one. The fixture
 * is dated 2026-10-26 and the clock was the machine's, so at midnight on
 * 10-27 main turned red on '10-26 09:07 → 10-26 11:06' with no code change:
 * the test, not the tile, was wrong. `NOW` is half an hour after the attempt
 * ended, on the same day. The pin is set before the first render because the
 * tile's `now` is `useNow`'s shared module clock, taken once at the first
 * render; a static render never subscribes, so it never ticks after that.
 *
 * MUTATIONS: drop the `runs !== null` branch from DtStrip's first cell -- the
 * first case turns red; draw it for one run (`runs.length < 1`) -- the last;
 * drop the pin in `beforeEach` -- the first two, from the day after onwards.
 */
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { Task } from '../types'
import { Run, runsLine } from '../AgentDetail'
import { attempt, task as runTask } from './runfixture'

const STARTED = '2026-10-26T09:07:00Z'
const SWAPPED = '2026-10-26T09:50:13Z'
const ENDED = '2026-10-26T11:06:52Z'
/** Half an hour after the attempt ended, on the fixture's own day. */
const NOW = '2026-10-26T11:36:52Z'

const TWO_RUNS = {
  artifacts: [],
  exit_code: -9,
  duration_seconds: 7189.6,
  last_run_duration_seconds: 4657.8,
  runs: [
    { run: 1, started_at: STARTED, ended_at: '2026-10-26T09:49:03Z', duration_s: 2531.8, account: 'eng:ai-eng-2' },
    { run: 2, started_at: '2026-10-26T09:50:15Z', ended_at: ENDED, duration_s: 4657.8, account: 'eng:ai-eng-1-max', after_swap: true },
  ],
  swaps: [{ at: SWAPPED, from: 'eng:ai-eng-2', to: 'eng:ai-eng-1-max', reason: 'exhausted', after_run: 1 }],
  swap_cost: { reread_seconds: 212.4, reread_tokens: { input_tokens: 300, cache_read_input_tokens: 1_500_000 }, measured_runs: 1, reached_action: 1 },
}

/** Local HH:MM, as the tile writes an instant. */
function hm(iso: string): string {
  const t = new Date(iso)
  return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`
}

function ended(summary: Record<string, unknown>): Task {
  return runTask({
    id: 'task_0a5cac0cd0fc45db8c6b',
    state: 'FAILED',
    created_at: '2026-10-26T09:05:00Z',
    started_at: STARTED,
    completed_at: ENDED,
    result_summary: summary,
  })
}

function runOf(t: Task): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { created_at: '2026-10-26T09:05:00Z', started_at: STARTED, completed_at: ENDED, exit_code: -9 })],
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

const hosts: HTMLElement[] = []
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(NOW))
})
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  vi.useRealTimers()
})

function runTile(t: Task): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={runOf(t)} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  const tile = el.querySelector<HTMLElement>('.dt-strip > .dt-sc')
  expect(tile).not.toBeNull()
  return tile!
}

function text(el: Element | null): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

describe('the Run tile says every run and the account swap', () => {
  it('a two-run attempt: the runs and the swap beside the duration, the cost in the title', () => {
    const tile = runTile(ended(TWO_RUNS))
    expect(text(tile.querySelector('.dt-sc-l'))).toBe('Ran · attempt 1')
    const runs = tile.querySelector<HTMLElement>('.dt-runs')
    expect(text(runs)).toBe(`2 runs · 1 account swap (ai-eng-2 exhausted → ai-eng-1-max at ${hm(SWAPPED)})`)
    // Beside the start → end span, which is drawn exactly as on a one-run tile.
    const span = text(runTile(ended({ artifacts: [] })).querySelector('.dt-sc-s'))
    expect(span).toMatch(new RegExp(`^${hm(STARTED)}(:\\d\\d)? → ${hm(ENDED)}(:\\d\\d)?$`))
    expect(text(tile.querySelector('.dt-sc-s'))).toBe(`${span} · ${text(runs)}`)
    expect(runs!.title).toContain('the duration counts every run')
    expect(runs!.title).toContain('3m 32s and 1,500,300 tokens before it acted again')
    // Drawn whole, so a clamp cannot cut the accounts off.
    expect(tile.classList.contains('is-whole')).toBe(true)
  })

  it('a one-run attempt: the tile is what it was', () => {
    const tile = runTile(ended({ artifacts: [], exit_code: 0, duration_seconds: 60.6 }))
    expect(tile.querySelector('.dt-runs')).toBeNull()
    expect(text(tile.querySelector('.dt-sc-s'))).toMatch(new RegExp(`^${hm(STARTED)}(:\\d\\d)? → ${hm(ENDED)}(:\\d\\d)?$`))
    expect(tile.classList.contains('is-whole')).toBe(false)
  })


  it('runsLine: null for no summary, no runs or one run; several swaps listed in order', () => {
    expect(runsLine(null)).toBeNull()
    expect(runsLine({})).toBeNull()
    expect(runsLine({ runs: [{ run: 1 }] })).toBeNull()
    expect(runsLine({ runs: 'two' })).toBeNull()
    // Two runs and no swap: an in-place restart, said without a swap.
    expect(runsLine({ runs: [{ run: 1 }, { run: 2 }] })?.text).toBe('2 runs')
    const three = runsLine({
      runs: [{ run: 1 }, { run: 2 }, { run: 3 }],
      swaps: [
        { from: 'eng:a', to: 'eng:b', reason: 'exhausted' },
        { from: 'eng:b', to: 'eng:c', reason: 'drain' },
      ],
    })
    expect(three?.text).toBe('3 runs · 2 account swaps (a exhausted → b, b drain → c)')
  })
})
