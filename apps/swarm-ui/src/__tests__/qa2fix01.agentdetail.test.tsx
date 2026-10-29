// VISUAL QA PASS 2, THE AGENT'S DETAILS TAB (QA2-FIX-01; part of #2694).
//
// Each finding below was seen on the fixture console by sweep lane
// QA2-OVERVIEW-AGENTS (2026-10-29) and is pinned here as it reads in the DOM:
//
//   ovag2-06  the wait line ran `… 5/5 units(holds no capacity)` and named
//             the pool by its raw key, where Headroom says `anthropic · u-bogdan`
//   ovag2-16  the failed card's last log line sat right-aligned in a narrow
//             centre column between its label and `Open logs ›`
//   oa-23     `No event for …` was said twice, above Now and inside it
//   oa-24     the head's `4 vCPU` (the class) beside a bare `2 cores` (the
//             limit) and `4 cores (class)`: one figure, with its basis
//   oa-25     the Resources fold restated itself under a second `Resources`
//   oa-26     the ticker said `Building` for a ready, a failed and a done agent
//   oa-28     `timeout 3600s` beside a run of days, nothing reconciling them
//   oa-29     `one page, cap unknown` and `no prompt key`: plain sentences
//   oa-52     a failed agent offered no way to run it again
//
// The ready agent is the fixture's own (`task_950b8e4a`, api.ts); the rest are
// built here, so each case says what it draws. Unit tests, no credentials.

import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DETAILS_CSS from '../styles/details.css?raw'
import AGENTS_CSS from '../styles/agents.css?raw'
import type { AgentRun } from '../api'
import type { Task, TaskInputCopy } from '../types'
import { cascade } from './cssgate'
import { attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTaskInputOnce: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run, resourceFigures } = await import('../AgentDetail')
const { loadAgentRun } = await vi.importActual<typeof import('../api')>('../api')
const { clearTaskPrefill, peekTaskPrefill } = await import('../submitPrefill')

const CLASSES = { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } }
const DAY = 86_400_000

function copyOf(id: string, prompt: string | null): TaskInputCopy {
  const masked = (text: string) => ({ text, redaction_count: 0 }) as unknown as TaskInputCopy['full']
  return {
    task_id: id,
    tenant_id: 'acme',
    read_at: new Date().toISOString(),
    prompt_key: prompt === null ? 'missing' : 'string',
    prompt: prompt === null ? null : masked(prompt),
    rest: null,
    full: masked(JSON.stringify(prompt === null ? {} : { prompt })),
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 0 },
  }
}

function runOf(t: Task, over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  } as AgentRun
}

let prompt: string | null = 'Fix the flaky lease heartbeat test.'

beforeEach(() => {
  prompt = 'Fix the flaky lease heartbeat test.'
  api.loadTaskInputOnce.mockImplementation((id: string) =>
    Promise.resolve({ status: 'ok', fetchedAt: Date.now(), data: copyOf(id, prompt) }),
  )
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => clearTaskPrefill())

/** A RUNNING task started `ranMs` ago whose newest event is `quietMin` minutes old. */
function running(ranMs: number, quietMin: number, over: Partial<Task> = {}): AgentRun {
  const started = new Date(Date.now() - ranMs).toISOString()
  const newest = new Date(Date.now() - quietMin * 60_000).toISOString()
  return runOf(
    task({ id: 'tsk_running0', state: 'RUNNING', started_at: started, completed_at: null, current_lease_id: 'lse_1', ...over }),
    {
      attempts: [attempt(1, { created_at: started, started_at: started, completed_at: null, exit_code: null })],
      events: [ev('lease_acquired', started, 'att_1'), ev('heartbeat', newest, 'att_1')],
    },
  )
}

function failed(over: Partial<Task> = {}): AgentRun {
  return runOf(
    task({
      id: 'tsk_failed00',
      state: 'FAILED',
      started_at: new Date(Date.now() - 20 * 60_000).toISOString(),
      completed_at: new Date(Date.now() - 10 * 60_000).toISOString(),
      last_error: 'runner exited 1',
      repository_url: 'https://github.com/acme/widgets.git',
      ...over,
    }),
    { attempts: [attempt(1, { exit_code: 1 })] },
  )
}

describe('ovag2-06: the wait line names the pool as Headroom does, then a space, then the aside', () => {
  it('reads `waiting for: anthropic · u-bogdan 5/5 units (holds no capacity)` on the fixture ready agent', async () => {
    const r = await loadAgentRun('task_950b8e4a')
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    const { container } = render(<Run run={r.data} />)
    const bar = await waitFor(() => {
      const b = container.querySelector<HTMLElement>('[data-waiting-for]')
      expect(b, 'the fixture ready agent drew no wait line').not.toBeNull()
      return b!
    })
    const said = (bar.textContent ?? '').replace(/\s+/g, ' ')
    // MUTATION: drop the `{' '}` before the aside -- `units(holds` is back.
    expect(said).not.toMatch(/units\(/)
    // MUTATION: print `waitingLine(task)` as it was -- the raw pool key is back.
    expect(said).toBe('waiting for: anthropic · u-bogdan 5/5 units (holds no capacity)')
    // The raw key stays one hover away.
    expect(bar.querySelector('[title="provider:anthropic:tenant:u-bogdan"]')).not.toBeNull()
  }, 30_000)
})

describe('ovag2-16: the failed card stacks its last log line under its label, full width, left-aligned', () => {
  it('gives the line its own row and left alignment', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<section class="dt-card dt-fail"><button class="ag-loglast"><b>Last log line (the error, as logged)</b>' +
      '<span class="ag-loglast-line mono">x</span><span class="ag-loglast-open">Open logs ›</span></button></section>'
    document.body.appendChild(host)
    const line = host.querySelector('.ag-loglast-line')!
    const sheet = `${AGENTS_CSS}\n${DETAILS_CSS}`
    const env = { width: 1440 }
    // MUTATION: put `flex: 1 1 12em` back -- the line shares a row with its label and the link again.
    expect(cascade(sheet, line, 'flex', env).winner?.value).toBe('1 0 100%')
    // MUTATION: drop `text-align: left` from the line -- a centred button can centre it.
    expect(cascade(sheet, line, 'text-align', env).winner?.value).toBe('left')
    // MUTATION: drop the line's `order` -- `Open logs ›` falls to a row of its own.
    expect(cascade(sheet, line, 'order', env).winner?.value).toBe('2')
    host.remove()
  })
})

describe('overview-agents-23: a silent worker is said once, inside Now', () => {
  it('draws `No event for …` once', async () => {
    const { container } = render(<Run run={running(60 * 60_000, 20)} />)
    await waitFor(() => expect(container.querySelector('.dt-now')).not.toBeNull())
    const lines = [...container.querySelectorAll('[data-why="silent"]')]
    // MUTATION: return `<SilentWorker>` from `Why` again -- two lines.
    expect(lines.length).toBe(1)
    expect(lines[0]!.closest('.dt-now'), 'the one line is not in the Now card').not.toBeNull()
  })
})

describe('overview-agents-24: the Resources row says which CPU figure it prints', () => {
  it('names a recorded limit as a limit, beside the class the head prints', () => {
    const r = running(60 * 60_000, 1)
    r.attempts = [{ ...r.attempts![0]!, cpu_limit_cores: 2 } as never]
    const cpu = resourceFigures(r)[0]!
    // MUTATION: print `${n(recorded)} cores` again -- a bare `2 cores` beside the head's `4 vCPU`.
    expect(cpu.text).toBe('2-core limit · 4 vCPU class')
  })

  it('names the class in the head\'s unit when nothing was recorded', () => {
    const r = runOf(task({ id: 'tsk_ready000', state: 'READY' }))
    // MUTATION: print `4 cores (class)` again.
    expect(resourceFigures(r)[0]!.text).toBe('4 vCPU class')
  })
})

describe('overview-agents-25: the Resources fold is not titled Resources twice', () => {
  it('heads its card with what the card adds', async () => {
    const { container } = render(<Run run={running(60 * 60_000, 1)} />)
    const head = await waitFor(() => {
      const h = container.querySelector('.dt-resources .dt-card-head')
      expect(h).not.toBeNull()
      return h!
    })
    // MUTATION: title the card `Resources` again.
    expect(head.textContent ?? '').not.toMatch(/^Resources/)
    expect(head.textContent ?? '').toMatch(/^Against each limit/)
  })
})

describe('overview-agents-26: the ticker says a word that fits the state', () => {
  const cases: [string, AgentRun, string][] = [
    ['running', running(60 * 60_000, 1), 'Building'],
    ['ready', runOf(task({ id: 'tsk_ready000', state: 'READY' })), 'To build'],
    ['failed', failed(), 'Was building'],
    ['succeeded', runOf(task({ id: 'tsk_done0000', state: 'SUCCEEDED', started_at: new Date(Date.now() - 9 * 60_000).toISOString(), completed_at: new Date().toISOString() })), 'Built'],
  ]
  it.each(cases)('a %s agent', async (_, r, word) => {
    const { container } = render(<Run run={r} />)
    // MUTATION: hard-code `Building` again -- the ready, failed and done cases go red.
    await waitFor(() => expect(container.querySelector('.dt-building-l')?.textContent).toBe(word))
  })
})

describe('overview-agents-28: a run past its timeout says so beside the timeout', () => {
  it('reads `3600s · over by …` for a run of nine days', async () => {
    const { container } = render(<Run run={running(9 * DAY, 1, { timeout_seconds: 3600 })} />)
    const fact = await waitFor(() => {
      const f = [...container.querySelectorAll<HTMLElement>('.dt-ifacts .ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'timeout')
      expect(f).toBeTruthy()
      return f!
    })
    // MUTATION: drop the `over by` words -- `3600s` alone beside a nine-day run.
    expect(fact.textContent).toMatch(/^timeout3600s · over by 8d 23h/)
  })

  it('says nothing more inside the timeout', async () => {
    const { container } = render(<Run run={running(10 * 60_000, 1, { timeout_seconds: 3600 })} />)
    const fact = await waitFor(() => {
      const f = [...container.querySelectorAll<HTMLElement>('.dt-ifacts .ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'timeout')
      expect(f).toBeTruthy()
      return f!
    })
    expect(fact.textContent).toBe('timeout3600s')
  })
})

describe('overview-agents-29: the Progress footer and the missing prompt are plain sentences', () => {
  it('says `Newest first · first page only`', async () => {
    const { container } = render(<Run run={running(60 * 60_000, 1)} />)
    // MUTATION: put `newest first · one page, cap unknown` back.
    await waitFor(() => expect(container.querySelector('.dt-progress')?.textContent ?? '').toContain('Newest first · first page only'))
    expect(container.textContent).not.toMatch(/cap unknown/)
  })

  it('says `No prompt was recorded`', async () => {
    prompt = null
    const { container } = render(<Run run={running(60 * 60_000, 1)} />)
    // MUTATION: put `no prompt key` back.
    await waitFor(() => expect(container.querySelector('.dt-input')?.textContent ?? '').toContain('No prompt was recorded'))
    expect(container.textContent).not.toMatch(/no prompt key/)
  })
})

describe('overview-agents-52: a failed agent can be run again', () => {
  it('opens Submit with its profile, prompt and repository filled in', async () => {
    const { container } = render(<Run run={failed()} />)
    const again = await waitFor(() => {
      const a = container.querySelector<HTMLAnchorElement>('a[data-run-again]')
      // MUTATION: drop the Run again link from the failure card.
      expect(a, 'a failed agent offers no Run again').not.toBeNull()
      expect(a!.getAttribute('aria-disabled')).not.toBe('true')
      return a!
    })
    expect(again.textContent).toBe('Run again')
    expect(again.closest('.dt-fail')).not.toBeNull()
    // Submit's own path (`addressToPath('work/new')`).
    expect(again.getAttribute('href')).toBe('/submit/task')
    // jsdom navigates no paths; the app's own click handler does that.
    again.addEventListener('click', (e) => e.preventDefault())
    fireEvent.click(again)
    // MUTATION: drop the `prefillTask` call -- Submit opens empty.
    expect(peekTaskPrefill()).toEqual({
      profile: 'claude-code',
      prompt: 'Fix the flaky lease heartbeat test.',
      repositoryUrl: 'https://github.com/acme/widgets.git',
    })
  })

  it('is not offered on a running agent', async () => {
    const { container } = render(<Run run={running(60 * 60_000, 1)} />)
    await waitFor(() => expect(container.querySelector('.dt-now')).not.toBeNull())
    expect(container.querySelector('a[data-run-again]')).toBeNull()
  })
})
