/**
 * THE WORKFLOW'S FACTS CARD (the picked workflow-clarity variant, part of
 * #2688; Bogdan, 2026-11-05). Under the page's state row: the description on
 * the left, in three labelled lines (Purpose, Goals, Expected output; BGD,
 * 2026-11-06), held to about five lines with More only when the clamp cut
 * text, a generated one marked by its chip, the dim ink and an (i); nothing
 * when none is served) and labelled facts on the right (Repository, Feature, Started by,
 * From, When). The count line and the inspector no longer repeat who and
 * when; the list gets a narrow Repository column; every surface names a
 * workflow by its served title, falling back to its label.
 *
 * The fields are read exactly as swarm-api serves them (`routes/workflows.py`
 * `workflow_about`, at the workflow's root). Each assertion fails against
 * main at 64230d3, where none of this is drawn.
 */
import STYLES from '../styles.css?raw'
import FACTS_CSS from '../styles/workflow-facts.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { cascade } from './cssgate'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')
const facts = await import('../WorkflowFacts')
const Spine = await import('../Spine')

const T0 = Date.parse('2026-11-05T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: null, ...over }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: iso(-500),
    completed_at: null,
    submitted_by: 'priya@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

/** A served row: the frozen fields, and `about` as `workflow_about` adds them at the root. */
function workflow(id: string, taskId: string, about: Record<string, unknown>): Workflow {
  return {
    workflow_id: id,
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 1 },
    created_at: iso(-2 * 86400),
    updated_at: iso(-60),
    submitted_by: 'priya@example.com',
    submitted_by_name: 'Priya Rao',
    priority: 0,
    on_step_failure: 'continue',
    cancel_requested: false,
    steps: [step('build', { task_id: taskId })],
    ...about,
  } as Workflow
}

const LONG = 'Moves the checkpoint writer onto the shared bucket. '.repeat(12).trim()

function board(): WorkflowBoard {
  const titled = workflow('wf_titled', 't_a', {
    repository_url: 'https://github.com/example-org/agent-swarm-infra',
    title: 'Move checkpoints to the shared bucket',
    description: LONG,
    description_source: 'submitter',
    feature: { title: 'Checkpoint hardening', url: 'https://github.com/example-org/agent-swarm-infra/issues/3201' },
    started_from: 'issue_run',
  })
  const generated = workflow('wf_gen', 't_b', {
    repository_url: 'git@github.com:example-org/nightly.git',
    title: null,
    description: 'Bumps every dependency the lockfile pins.',
    description_source: 'generated',
    about: {
      purpose: 'Bumps every dependency the lockfile pins.',
      goals: ['bump: Update the lockfile.', 'test: Run the suite.'],
      output: 'A pull request on example-org/nightly, merged by its merge step.',
    },
    feature: null,
    started_from: 'schedule',
  })
  // Submitted before these fields existed: no key is served.
  const old = workflow('wf_old', 't_c', {})
  // Submitted without a description, and none generated: served as null.
  const bare = workflow('wf_bare', 't_d', {
    repository_url: 'https://github.com/example-org/bare',
    title: 'Tidy the bucket',
    description: null,
    description_source: null,
    about: null,
    feature: null,
    started_from: 'console',
  })
  const tasks = new Map<string, Task>([
    ['t_a', task('t_a', 'RUNNING', { metadata: { unit: 'ckpt-mv', issue_run: { run_id: 'ir_1', issue: 'example-org/agent-swarm-infra#3201' } } })],
    ['t_b', task('t_b', 'RUNNING', { metadata: { unit: 'deps-n', schedule: { schedule_id: 'nightly-deps', firing_id: 'f1' } } })],
    ['t_c', task('t_c', 'RUNNING', { metadata: { unit: 'old-label' } })],
    ['t_d', task('t_d', 'RUNNING', { metadata: { unit: 'bare-label' } })],
  ])
  return { workflows: [titled, generated, old, bare], taskById: tasks, statesDetail: null }
}

const realWidth = window.innerWidth
function atWidth(px: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: px })
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  atWidth(1440)
  const b = board()
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: b, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(async (id: string) => {
    const w = b.workflows.find((x) => x.workflow_id === id)
    if (w === undefined) return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' } }
    const tasks = w.steps.map((s) => (s.task_id ? b.taskById?.get(s.task_id) : undefined)).filter((t) => t !== undefined)
    return { status: 'ok', data: { workflow: w, tasks }, fetchedAt: T0 }
  })
  api.loadTask.mockImplementation(async (id: string) => {
    const t = b.taskById?.get(id)
    return t === undefined
      ? { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such task' } }
      : { status: 'ok', data: t, fetchedAt: T0 }
  })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})
afterEach(() => {
  atWidth(realWidth)
  vi.restoreAllMocks()
})

async function page(id: string): Promise<HTMLElement> {
  render(<WorkflowsScreen view={`wf=${id}`} />)
  return waitFor(() => {
    const card = document.querySelector<HTMLElement>('.wfp-head .wff-card')
    expect(card, 'no facts card under the state row').toBeTruthy()
    return card!
  })
}

/** The value beside a fact's label. */
function fact(card: HTMLElement, label: string): HTMLElement {
  const dt = [...card.querySelectorAll('dt')].find((d) => d.textContent === label)
  expect(dt, `no ${label} fact`).toBeTruthy()
  return dt!.nextElementSibling as HTMLElement
}

describe('the served fields, read as swarm-api serves them', () => {
  it('tells none (null) from not read (absent), and marks generated only beside a description', () => {
    const b = board()
    const [titled, generated, old] = b.workflows
    expect(facts.aboutOf(titled!)).toMatchObject({ title: 'Move checkpoints to the shared bucket', generated: false, startedFrom: 'issue_run' })
    expect(facts.aboutOf(generated!)).toMatchObject({ title: null, feature: null, generated: true, startedFrom: 'schedule' })
    expect(facts.aboutOf(old!)).toMatchObject({ title: undefined, description: undefined, parts: undefined, feature: undefined, startedFrom: undefined })
    // A bare description is the purpose; a served about is read part by part.
    expect(facts.aboutOf(titled!).parts).toEqual({ purpose: LONG, goals: [], output: null })
    expect(facts.aboutOf(generated!).parts).toEqual({
      purpose: 'Bumps every dependency the lockfile pins.',
      goals: ['bump: Update the lockfile.', 'test: Run the suite.'],
      output: 'A pull request on example-org/nightly, merged by its merge step.',
    })
    expect(facts.aboutOf(b.workflows[3]!)).toMatchObject({ parts: null, generated: false })
    // MUTATION: drop the description check and a source with no description reads as generated.
    expect(facts.aboutOf(workflow('wf_x', 't_x', { description: null, description_source: 'generated' })).generated).toBe(false)
  })

  it('names a workflow by its served title, else its label', () => {
    const b = board()
    const [titled, generated, old] = b.workflows
    expect(facts.workflowName(titled!, b.taskById)).toBe('Move checkpoints to the shared bucket')
    expect(facts.workflowName(generated!, b.taskById)).toBe('deps-n')
    expect(facts.workflowName(old!, b.taskById)).toBe('old-label')
    // The title is on the row itself: it names the workflow before any step is read.
    expect(facts.workflowName(titled!, null)).toBe('Move checkpoints to the shared bucket')
  })

  it('reads owner/repo off an https or an scp-form URL, and nothing off a host alone', () => {
    expect(facts.repoOwnerName('https://github.com/example-org/agent-swarm-infra')).toEqual({ owner: 'example-org', name: 'agent-swarm-infra' })
    expect(facts.repoOwnerName('git@github.com:example-org/nightly.git')).toEqual({ owner: 'example-org', name: 'nightly' })
    expect(facts.repoOwnerName('https://github.com/')).toBeNull()
    expect(facts.repoOwnerName(null)).toBeNull()
  })
})

describe('the facts card under the state row', () => {
  it('draws the description and the five facts, the feature and the repository as links, From as a chip with its reference', async () => {
    const card = await page('wf_titled')
    // A spec's bare description is drawn as the purpose, the one labelled line.
    expect([...card.querySelectorAll('.wff-part-label')].map((l) => l.textContent)).toEqual(['Purpose'])
    expect(card.querySelector('.wff-part .wff-text')!.textContent).toBe(LONG)
    expect(card.querySelector('.wff-gen')).toBeNull()
    expect(within(card).queryByRole('button', { name: /About the generated/ })).toBeNull()
    expect([...card.querySelectorAll('dt')].map((d) => d.textContent)).toEqual(['Repository', 'Feature', 'Started by', 'From', 'When'])
    const repo = fact(card, 'Repository').querySelector('a')!
    expect(repo.getAttribute('href')).toBe('https://github.com/example-org/agent-swarm-infra')
    expect(repo.textContent).toBe('example-org/agent-swarm-infra')
    const feature = fact(card, 'Feature').querySelector('a')!
    expect(feature.textContent).toBe('Checkpoint hardening')
    expect(feature.getAttribute('href')).toBe('https://github.com/example-org/agent-swarm-infra/issues/3201')
    expect(fact(card, 'Started by').textContent).toContain('Priya Rao')
    expect(fact(card, 'From').querySelector('.c-chip')!.textContent).toBe('Issue run')
    expect(fact(card, 'From').textContent).toContain('example-org/agent-swarm-infra#3201')
    expect(fact(card, 'When').textContent).toBe('Nov 3 12:00Z')
  })

  it('draws the three labelled lines, the goals as a list, and marks a generated block by its chip, the dim ink and an (i); no feature reads none', async () => {
    const card = await page('wf_gen')
    expect([...card.querySelectorAll('.wff-part-label')].map((l) => l.textContent)).toEqual(['Purpose', 'Goals', 'Expected output'])
    expect([...card.querySelectorAll('.wff-goals li')].map((l) => l.textContent)).toEqual(['bump: Update the lockfile.', 'test: Run the suite.'])
    expect(within(card).getByText('A pull request on example-org/nightly, merged by its merge step.')).toBeTruthy()
    expect(card.querySelector('.wff-label .c-chip')!.textContent).toBe('generated')
    expect(card.querySelector('.wff-parts')!.classList.contains('is-generated')).toBe(true)
    // THE EXPLANATION IS BEHIND THE (i), never a line of prose on the card.
    const info = within(card).getByRole('button', { name: 'About the generated description' })
    const tip = document.getElementById(info.getAttribute('aria-describedby')!)!
    expect(tip.textContent).toBe(facts.GENERATED_TIP)
    expect(tip.hidden).toBe(true)
    fireEvent.click(info)
    expect(tip.hidden).toBe(false)
    expect(within(card).queryByText(/A model wrote this/)).toBeNull()
    expect(fact(card, 'Feature').querySelector('.ctl-mark.is-zero')!.textContent).toBe('none')
    expect(fact(card, 'From').querySelector('.c-chip')!.textContent).toBe('Schedule')
    expect(fact(card, 'From').textContent).toContain('nightly-deps')
    expect(fact(card, 'Repository').textContent).toBe('example-org/nightly')
  })

  it('keeps every slot for a workflow submitted before the fields existed, each reading not recorded', async () => {
    const card = await page('wf_old')
    for (const label of ['Repository', 'Feature', 'From']) {
      expect(fact(card, label).querySelector('.ctl-mark.is-absent')!.textContent).toBe('not recorded')
    }
    expect(card.querySelector('.wff-desc .ctl-mark.is-absent')!.textContent).toBe('not recorded')
  })

  it('draws nothing of a description when none is served: the title stands alone, nothing invented', async () => {
    const card = await page('wf_bare')
    expect(card.querySelector('.wff-desc')).toBeNull()
    expect(card.querySelector('.wff-parts')).toBeNull()
    expect(card.classList.contains('is-facts-only')).toBe(true)
    expect(within(card).queryByText(/none submitted|Purpose|generated/)).toBeNull()
    expect([...card.querySelectorAll('dt')].map((d) => d.textContent)).toEqual(['Repository', 'Feature', 'Started by', 'From', 'When'])
  })

  it('offers More only when the clamp cut the text, and Less once open', async () => {
    // jsdom lays nothing out: a clamped paragraph reports a taller scroll box than its client box only when told.
    const tall = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(200)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(120)
    const card = await page('wf_titled')
    const more = await within(card).findByRole('button', { name: 'More' })
    expect(card.querySelector('.wff-parts')!.classList.contains('is-clamped')).toBe(true)
    fireEvent.click(more)
    expect(card.querySelector('.wff-parts')!.classList.contains('is-clamped')).toBe(false)
    expect(within(card).getByRole('button', { name: 'Less' }).getAttribute('aria-expanded')).toBe('true')
    tall.mockReturnValue(120)
    fireEvent.click(within(card).getByRole('button', { name: 'Less' }))
    await waitFor(() => expect(within(card).queryByRole('button', { name: /^(More|Less)$/ })).toBeNull())
  })

  it('draws no More when the whole description fits', async () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(60)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(60)
    const card = await page('wf_titled')
    expect(within(card).queryByRole('button', { name: /^(More|Less)$/ })).toBeNull()
  })
})

describe('nothing on the page is drawn twice', () => {
  it('the count line says no by and no started, and the Overview has no Owner or Started, only Changed', async () => {
    await page('wf_titled')
    const note = (document.querySelector<HTMLElement>('.c-count-note')?.textContent ?? '').replace(/\s+/g, ' ')
    expect(note).toMatch(/step/)
    expect(note).not.toMatch(/\bby\b/)
    expect(note).not.toMatch(/started/)
    const ov = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"]')
      expect(el).toBeTruthy()
      return el!
    })
    const terms = [...ov.querySelectorAll('.wfp-dl > dt')].map((d) => d.textContent)
    expect(terms).not.toContain('Owner')
    expect(terms).not.toContain('Started')
    expect(terms).toContain('Changed')
  })
})

describe('one name everywhere', () => {
  it('heads the page and records Recent by the served title, not the label', async () => {
    await page('wf_titled')
    await waitFor(() => expect(Spine.recentName('wf_titled')).toBe('Move checkpoints to the shared bucket'))
  })

  it('the Spine refresh of Recent keeps the served title', async () => {
    Spine.rememberWorkflow('wf_titled', null, 'ckpt-mv')
    Spine.refreshRecentWorkflows([{ id: 'wf_titled', state: 'RUNNING', name: facts.aboutOf(board().workflows[0]!).title ?? null }])
    expect(Spine.recentName('wf_titled')).toBe('Move checkpoints to the shared bucket')
  })
})

describe('the Workflows list', () => {
  it('names each row by its title and gives it a Repository cell, name over owner', async () => {
    render(<WorkflowsScreen />)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('tr[data-workflow="wf_titled"]')
      expect(r).toBeTruthy()
      return r!
    })
    expect(row.querySelector('td[data-col="workflow"] > a')!.textContent).toBe('Move checkpoints to the shared bucket')
    const repo = row.querySelector<HTMLElement>('td[data-col="repo"]')!
    expect(repo.querySelector('.wff-repo-name')!.textContent).toBe('agent-swarm-infra')
    expect(repo.querySelector('.wff-repo-by')!.textContent).toBe('example-org')
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toContain('Repository')
    // A row with no repository served keeps its cell, drawn as a dash.
    expect(document.querySelector('tr[data-workflow="wf_old"] td[data-col="repo"]')!.textContent).toBe('—')
  })
})

describe('on a phone', () => {
  const SHEET = `${FACTS_CSS}\n${STYLES}`
  it('stacks the facts under the description, the rule above them, and hides the Repository column', () => {
    document.body.innerHTML =
      '<section class="wff-card"><div class="wff-desc"></div><dl class="wff-facts"></dl></section>' +
      '<table class="wfg-table"><tbody><tr><td data-col="repo"></td></tr></tbody></table>'
    const card = document.querySelector('.wff-card')!
    const dl = document.querySelector('.wff-facts')!
    const td = document.querySelector('td[data-col="repo"]')!
    const at = (el: Element, prop: string, width: number) => cascade(SHEET, el, prop, { width, theme: 'light' }).winner?.value ?? null
    expect(at(card, 'grid-template-columns', 1440)).toBe('minmax(0, 1fr) minmax(0, max-content)')
    expect(at(card, 'grid-template-columns', 390)).toBe('minmax(0, 1fr)')
    expect(at(dl, 'border-top', 390)).toBe('1px solid var(--line)')
    expect(at(td, 'display', 390)).toBe('none')
  })
})
