/**
 * BUILDING, NOW, ONE RESOURCES ROW (DETAILS-BUILDING, part of #2391).
 *
 * BGD, 2026-10-27, on the agent's Details page: "too much prose on this page,
 * we don't need so much text for showing resource consumption. Rather it
 * would be more useful to quickly understand exactly what the agent is
 * currently doing and building, maybe derived from the prompt the agent
 * started with". The shape he picked:
 *
 *     Building  Secret retention keeps 3 versions (DECIDED-A · step secret-retention · #1819)
 *     Now       running tests · edited secretstore.py, test_secret_retention.py · 12 min
 *     Resources 4/8 cores · 8 GiB · 412k tokens · $3.10   [details ▾]
 *
 * MUTATIONS, one per block: drop `DtBuilding` from `Run`, or read the title
 * before the label's facts are added; lead the Now card with the phase chips
 * again (drop `DtNowLine`); put `DtStrip` or `DtResources` back outside the
 * `.dt-res` disclosure. Each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, ResourceClasses } from '../api'
import type { Task, TaskInputCopy } from '../types'
import { attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run, buildingOf } = await import('../AgentDetail')

const MIN = 60_000

const CLASSES = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

const PROMPT =
  'Keep three versions of every tenant secret and destroy the older ones. ' +
  'The retention job runs nightly. '.repeat(20)

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: 'task_building0123456789',
    state: 'RUNNING',
    created_at: ago(13),
    started_at: ago(12),
    completed_at: null,
    attempt_count: 1,
    max_attempts: 3,
    workflow_id: 'wf_decided',
    step_id: 'secret-retention',
    metadata: { title: 'Secret retention keeps 3 versions', unit: 'DECIDED-A' },
    ...over,
  })
}

function runOf(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: running(),
    events: [],
    eventsDetail: null,
    attempts: [
      attempt(1, {
        backend: 'GKE_JOB',
        created_at: ago(13),
        started_at: ago(12),
        completed_at: null,
        exit_code: null,
        sized_cpu_request: 4,
        sized_cpu_limit: 8,
        input_tokens: 2_000,
        output_tokens: 10_000,
        cache_read_input_tokens: 400_000,
        cache_creation_input_tokens: 0,
        cost_usd: 3.1,
      } as Parameters<typeof attempt>[1]),
    ],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el.querySelector<HTMLElement>('.dt')!
}

function text(el: Element | null | undefined): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/** The masked copy `GET /v1/tasks/{id}/input` serves: the prompt, and the rest of the input as JSON. */
function copyOf(prompt: string | null, rest: Record<string, unknown> | null = null): TaskInputCopy {
  return {
    task_id: 'task_building0123456789',
    tenant_id: 'acme',
    read_at: new Date().toISOString(),
    prompt_key: prompt === null ? 'missing' : 'string',
    prompt: prompt === null ? null : { text: prompt, redaction_count: 0 },
    rest: rest === null ? null : { text: JSON.stringify(rest, null, 2), redaction_count: 0 },
    full: { text: JSON.stringify({ prompt, ...rest }, null, 2), redaction_count: 0 },
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 11 },
  } as TaskInputCopy
}

describe('Building: what the agent is building, in one line, at the top of the page', () => {
  it('reads the workflow title, then its label, the step and the issue', () => {
    const b = buildingOf(running(), copyOf(PROMPT, { issue: 1819 }))
    expect(b.text).toBe('Secret retention keeps 3 versions')
    expect(b.facts).toEqual(['DECIDED-A', 'step secret-retention', '#1819'])
    // Its hover carries the prompt's first 300 characters.
    expect(b.hover).toBe(PROMPT.slice(0, 300))
  })

  it('takes the issue from a #N in the prompt’s first line when the input names none', () => {
    const b = buildingOf(running(), copyOf('Fix the lease heartbeat (part of #2401).\nThen the rest.'))
    expect(b.facts).toEqual(['DECIDED-A', 'step secret-retention', '#2401'])
  })

  it('falls back to the prompt’s first sentence, cut at about 120 characters, when there is no title', () => {
    const lone = running({ metadata: null, step_id: null, workflow_id: null })
    expect(buildingOf(lone, copyOf(PROMPT)).text).toBe('Keep three versions of every tenant secret and destroy the older ones.')

    const long = 'Rewrite ' + 'the very long retention clause '.repeat(10) + 'so it reads well. Then stop.'
    const cut = buildingOf(lone, copyOf(long)).text!
    expect(cut.endsWith('…')).toBe(true)
    expect(cut.length).toBeLessThanOrEqual(120)
    expect(long.startsWith(cut.slice(0, -1).trimEnd())).toBe(true)
  })

  it('is the page’s first line, and draws the masked copy’s prompt, never task.input', async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskInputOnce.mockResolvedValue({ status: 'ok', data: copyOf('Masked: keep three versions.', { issue: 7 }), fetchedAt: Date.now() })
    // What an API older than read-time masking serves on the task: drawn nowhere.
    const raw = 'Raw: ' + 'x'.repeat(12) + ' keep three versions.'
    const t = running({ metadata: null, step_id: null, workflow_id: null, input: { prompt: raw, issue: 9 } })
    const { container } = render(<Run run={runOf({ task: t })} />)
    const dt = container.querySelector<HTMLElement>('.dt')!
    await waitFor(() => expect(text(dt.querySelector('.dt-building-v'))).toBe('Masked: keep three versions. (#7)'))
    expect(dt.firstElementChild).toBe(dt.querySelector('.dt-building'))
    expect(text(dt.querySelector('.dt-building-l'))).toBe('Building')
    expect(dt.querySelector('.dt-building-v')!.getAttribute('title')).toBe('Masked: keep three versions.')
    expect(dt.textContent).not.toContain(raw)
  })

  it('reads as the title with its facts at desktop, on one line', () => {
    const dt = host(runOf())
    const v = dt.querySelector('.dt-building-v')!
    // Before the copy is read: the title, the label and the step.
    expect(text(v)).toBe('Secret retention keeps 3 versions (DECIDED-A · step secret-retention)')
    expect(dt.firstElementChild).toBe(dt.querySelector('.dt-building'))
  })
})

describe('Now: one line of what the agent is doing, ahead of the phases', () => {
  it('leads with the phase, the newest event in its own words and the elapsed time', () => {
    const t = running()
    const events = [ev('running', ago(12), 'att_1'), ev('checkpoint_completed', ago(3), 'att_1'), ev('heartbeat', ago(1), 'att_1')]
    const dt = host(runOf({ task: t, events }))
    const card = dt.querySelector<HTMLElement>('.dt-now')!
    const line = card.querySelector<HTMLElement>('.dt-nowline')
    expect(line, 'no Now line').not.toBeNull()
    // The first thing under the card's head, before the phase chips.
    expect(card.children[1]).toBe(line)
    const parts = [...line!.querySelectorAll('.dt-nowline-p')].map(text)
    // A heartbeat is not news: the newest event that is something is.
    expect(parts).toEqual(['running', 'checkpoint completed', expect.stringMatching(/^12m( \d+s)?$/)])
    // The elapsed figure is the strip's own.
    const tile = [...dt.querySelectorAll('.dt-strip > .dt-sc')].find((c) => /^Run/.test(text(c.querySelector('.dt-sc-l'))))!
    expect(parts[2]).toBe(text(tile.querySelector('.dt-sc-v')))
    // The phases still follow it.
    expect(line!.compareDocumentPosition(card.querySelector('.dt-phases')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says the phase and the elapsed time alone when no event says more', () => {
    const dt = host(runOf({ events: [ev('heartbeat', ago(1), 'att_1')] }))
    const parts = [...dt.querySelectorAll('.dt-nowline .dt-nowline-p')].map(text)
    expect(parts).toHaveLength(2)
    expect(parts[0]).toBe('running')
  })
})

describe('Resources: one row, the prose behind “details”', () => {
  it('is one row of cores as request/limit, memory, tokens and cost, with the old content folded under it', () => {
    const dt = host(runOf())
    const res = dt.querySelector<HTMLDetailsElement>('details.dt-res')
    expect(res, 'no Resources row').not.toBeNull()
    expect(res!.hasAttribute('open')).toBe(false)
    const row = res!.querySelector('summary')!
    expect(text(row.querySelector('.dt-res-l'))).toBe('Resources')
    expect([...row.querySelectorAll('.dt-res-f')].map(text)).toEqual(['4/8 cores', '8 GiB', '412k tokens', '$3.10'])
    expect(text(row.querySelector('.dt-res-more'))).toBe('details')
    // Today's strip and Resources card, unchanged, are inside the disclosure and nowhere else.
    for (const sel of ['.dt-strip', '.dt-resources']) {
      const n = dt.querySelector(sel)
      expect(n, sel).not.toBeNull()
      expect(n!.closest('details.dt-res'), sel).toBe(res)
    }
    // The sized prose is folded: nothing outside the disclosure says it.
    expect(text(res!)).toContain('4 cores guaranteed, bursts to 8')
    const outside = dt.cloneNode(true) as HTMLElement
    outside.querySelector('details.dt-res')!.remove()
    expect(text(outside)).not.toMatch(/cores guaranteed|bursts to/)
  })

  it('comes after Building and the lead card, and before Progress', () => {
    const dt = host(runOf())
    const order = ['.dt-building', '.dt-now', 'details.dt-res', '.dt-progress']
    const at = order.map((s) => dt.querySelector(s)!)
    for (let i = 1; i < at.length; i++) {
      expect(at[i - 1]!.compareDocumentPosition(at[i]!) & Node.DOCUMENT_POSITION_FOLLOWING, order[i]).toBeTruthy()
    }
  })

  it('reads the limit an attempt recorded when it has no sized record, never the class’s cores unlabelled', () => {
    // An attempt from before contract request 108: a cgroup limit of 2 cores
    // and no sized_* fields, in a class of 4. The strip and the card say 2;
    // the row must not say 4 (FF-D5a, #1680).
    const four = { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 10, units: 4 } } as unknown as ResourceClasses
    const legacy = attempt(1, {
      backend: 'GKE_JOB',
      created_at: ago(13),
      started_at: ago(12),
      completed_at: null,
      exit_code: null,
      peak_cpu_cores: 1.6,
      cpu_limit_cores: 2,
      cpu_limit_source: 'cgroup',
    } as Parameters<typeof attempt>[1])
    const dt = host(runOf({ classes: four, attempts: [legacy] }))
    const cpu = dt.querySelector('details.dt-res > summary .dt-res-f')!
    // Named a limit, beside the class the head prints (QA2 overview-agents-24).
    expect(text(cpu)).toBe('2-core limit · 4 vCPU class')
    expect(cpu.getAttribute('title')).toContain('cgroup limit (class 4 vCPU)')
    // With no attempt figure at all, the class's cores say whose they are.
    const bare = attempt(1, { backend: 'GKE_JOB', completed_at: null, exit_code: null } as Parameters<typeof attempt>[1])
    const dt2 = host(runOf({ classes: four, attempts: [bare] }))
    expect(text(dt2.querySelector('details.dt-res > summary .dt-res-f'))).toBe('4 vCPU class')
  })

  it('names what it could not read with a dash, never a 0', () => {
    const dt = host(runOf({ attempts: [], classes: null, task: running({ attempt_count: 0, started_at: null, state: 'QUEUED' }) }))
    const figures = [...dt.querySelectorAll('details.dt-res > summary .dt-res-f')].map(text)
    expect(figures).toEqual(['— cores', '— GiB', '— tokens', '— cost'])
  })
})
