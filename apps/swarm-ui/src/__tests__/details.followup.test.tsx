// #184 FOLLOW-UP: WHAT DETAILS NO LONGER DRAWS, AND WHAT IT NOW DRAWS MASKED.
//
// The owner's decisions of 2026-09-25, recorded on #184:
//
//   * "The runner (platform) log lives in Artifacts › Logs only, and is
//     removed from Details." Details drew it as a panel of its own, titled
//     `Runner log (platform)`, and read `/logs` for it on every poll.
//   * "Details' own artifact list, which duplicates Artifacts › Outputs, is
//     removed." Details listed `result_summary.artifacts` with a viewer and a
//     `copy gsutil` per row, the same files the Artifacts pane lists.
//   * "Inputs and Details show a read-time-redacted copy of the task's input,
//     with 'masked N', like every other output." Details drew `task.input`
//     straight off the task document -- the prompt and the full input -- with
//     no masking and no word that there was none.
//
// MUTATIONS: put `<LogsPanel>` back in `RunFiles`; put `<Artifacts>` back in
// `Output`; draw `task.input.prompt` again, or fall back to it when the copy
// was not read.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Task } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const WAIT = { timeout: 5000 }

/** Shaped like a key `redaction.RULES` masks. Not a real credential. */
const SECRET = 'sk-proj0123456789abcdefghijklmnopqrstuv'
const RAW_PROMPT = `Deploy with ${SECRET} and report.`
/** What the API serves for it: the `sk-` rule keeps its prefix and masks the rest. */
const MASKED_PROMPT = 'Deploy with sk-proj01******** and report.'

function inputCopy(over: Record<string, unknown> = {}) {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    read_at: new Date().toISOString(),
    prompt_key: 'string',
    prompt: { text: MASKED_PROMPT, redaction_count: 1 },
    rest: { text: '{\n  "steps": 2\n}', redaction_count: 0 },
    full: { text: `{\n  "prompt": "${MASKED_PROMPT}",\n  "steps": 2\n}`, redaction_count: 1 },
    redacted: true,
    redaction_count: 1,
    redaction: { applied_at_read_time: true, rules: 11 },
    ...over,
  }
}

function agentRun(t: Partial<Task> = {}, input: unknown = { status: 'ok', data: inputCopy(), fetchedAt: Date.now() }): AgentRun {
  const run = {
    task: task({
      state: 'SUCCEEDED',
      attempt_count: 1,
      input: { prompt: RAW_PROMPT, steps: 2 },
      result_summary: {
        artifacts: [{ name: 'report.md', bytes: 120, uri: 'gs://swarm-artifacts/tenants/acme/tasks/tsk_charts/attempts/att_1/artifacts/report.md' }],
        logs: { stdout: 'gs://swarm-artifacts/tenants/acme/tasks/tsk_charts/attempts/att_1/logs/stdout.log' },
      },
      ...t,
    }),
    events: [],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
  // The copy rides on the run the drawer reads (`loadAgentRun`).
  return (input === undefined ? run : Object.assign(run, { input })) as unknown as AgentRun
}

async function mount(run: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={run} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull(), WAIT)
  return container as HTMLElement
}

function section(root: HTMLElement, title: string): HTMLElement | null {
  return [...root.querySelectorAll<HTMLElement>('section')].find((s) => s.querySelector('h2')?.textContent === title) ?? null
}

describe('the runner log lives in Artifacts › Logs only', () => {
  it('draws no runner log panel in Details, and reads no log for one', async () => {
    const root = await mount(agentRun())
    const titles = [...root.querySelectorAll('section > h2')].map((h) => h.textContent)
    expect(titles, 'Details still draws the runner log').not.toContain('Runner log (platform)')
    expect(titles).not.toContain('Output, as the agent wrote it')
    // A panel that is gone must not keep its read either: every poll of the
    // drawer read `/logs` for it.
    await new Promise((r) => setTimeout(r, 50))
    expect(api.loadTaskLogs, 'Details still reads the runner log').not.toHaveBeenCalled()
  })
})

describe('Details has no artifact list of its own', () => {
  it('lists no file the Artifacts pane lists, and opens no viewer for one', async () => {
    const root = await mount(agentRun())
    const output = section(root, 'Output')
    expect(output, 'no Output section').not.toBeNull()
    const eyebrows = [...output!.querySelectorAll('.ctl-eyebrow')].map((e) => e.textContent?.trim())
    expect(eyebrows, 'Details still lists the artifacts').not.toContain('artifacts')
    expect(output!.querySelector('button.art-open'), 'a file in Details still opens a viewer').toBeNull()
    expect(output!.textContent, 'the file the Artifacts pane lists is listed here too').not.toContain('report.md')
  })
})

describe('Details shows the task’s input as the API masked it, with its count', () => {
  it('draws the served copy of the prompt and the full input, never the raw document', async () => {
    const root = await mount(agentRun())
    const input = section(root, 'Input')
    expect(input, 'no Input section').not.toBeNull()
    expect(root.textContent, 'the raw prompt reached the screen').not.toContain(SECRET)
    const pres = [...input!.querySelectorAll('pre')].map((p) => p.textContent ?? '')
    expect(pres, 'the masked prompt is not drawn').toContain(MASKED_PROMPT)
    expect(pres.some((p) => p.includes('"steps": 2')), 'the full input is not drawn').toBe(true)
  })

  it('says how much was masked beside the prompt, in the ink a finding takes', async () => {
    const root = await mount(agentRun())
    const input = section(root, 'Input')!
    const counts = [...input.querySelectorAll('.art-masked')]
    expect(counts.length, 'no masked count in Input').toBeGreaterThan(0)
    expect(counts[0]!.textContent).toBe('1')
    expect(counts[0]!.classList.contains('is-warn'), 'a count above zero is drawn as nothing to see').toBe(true)
    expect(counts[0]!.parentElement?.textContent ?? '').toMatch(/masked\s*1/)
  })

  it('never falls back to the raw input when the copy could not be read', async () => {
    const failed = { status: 'error', error: { kind: 'upstream_degraded', httpStatus: 503, code: 'upstream_unavailable', message: 'Busy.' } }
    const root = await mount(agentRun({}, failed))
    expect(root.textContent, 'the raw prompt was drawn when the copy failed').not.toContain(SECRET)
    expect(root.textContent, 'the raw prompt was drawn when the copy failed').not.toContain('Deploy with')
    expect(section(root, 'Input')!.querySelector('.ctl-mark.is-unread'), 'a failed read is not marked as one').not.toBeNull()
  })
})
