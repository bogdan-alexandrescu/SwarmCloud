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
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it } from 'vitest'

import type { AgentRun, ResourceClasses } from '../api'
import { Run } from '../AgentDetail'
import type { Task } from '../types'
import { attempt, ev, task as runTask } from './runfixture'

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
    input: { prompt: PROMPT, issue: 1819 },
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

describe('Building: what the agent is building, in one line, at the top of the page', () => {
  it('reads the workflow title, then its label, the step and the issue', () => {
    const dt = host(runOf())
    const b = dt.querySelector<HTMLElement>('.dt-building')
    expect(b, 'no Building line').not.toBeNull()
    // It is the page body's first line.
    expect(dt.firstElementChild).toBe(b)
    expect(text(b!.querySelector('.dt-building-l'))).toBe('Building')
    expect(text(b!.querySelector('.dt-building-v'))).toBe('Secret retention keeps 3 versions (DECIDED-A · step secret-retention · #1819)')
    // Its hover carries the prompt's first 300 characters.
    expect(b!.querySelector('.dt-building-v')!.getAttribute('title')).toBe(PROMPT.slice(0, 300))
  })

  it('takes the issue from a #N in the prompt’s first line when the input names none', () => {
    const t = running({ input: { prompt: 'Fix the lease heartbeat (part of #2401).\nThen the rest.' } })
    const dt = host(runOf({ task: t }))
    expect(text(dt.querySelector('.dt-building-v'))).toBe('Secret retention keeps 3 versions (DECIDED-A · step secret-retention · #2401)')
  })

  it('falls back to the prompt’s first sentence, cut at about 120 characters, when there is no title', () => {
    const t = running({ metadata: null, step_id: null, workflow_id: null, input: { prompt: PROMPT } })
    const dt = host(runOf({ task: t }))
    expect(text(dt.querySelector('.dt-building-v'))).toBe('Keep three versions of every tenant secret and destroy the older ones.')

    const long = 'Rewrite ' + 'the very long retention clause '.repeat(10) + 'so it reads well. Then stop.'
    const cut = text(host(runOf({ task: running({ metadata: null, step_id: null, input: { prompt: long } }) })).querySelector('.dt-building-v'))
    expect(cut.endsWith('…')).toBe(true)
    expect(cut.length).toBeLessThanOrEqual(121)
    expect(long.startsWith(cut.slice(0, -1).trimEnd())).toBe(true)
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

  it('names what it could not read with a dash, never a 0', () => {
    const dt = host(runOf({ attempts: [], classes: null, task: running({ attempt_count: 0, started_at: null, state: 'QUEUED' }) }))
    const figures = [...dt.querySelectorAll('details.dt-res > summary .dt-res-f')].map(text)
    expect(figures).toEqual(['— cores', '— GiB', '— tokens', '— cost'])
  })
})
