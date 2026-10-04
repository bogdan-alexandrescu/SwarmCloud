// EACH FACT ON DETAILS HAS ONE HOME, AND EVERY TILE SAYS WHAT IT LEAVES OUT.
//
//   #102  `Output` stacked straight onto `Code`: two headings, nothing between.
//         Checkpoints were listed twice, per attempt and in the Checkpoints
//         section. The Headline's `run` repeated the Elapsed tile, and a
//         single attempt's `ran` repeated it a third time -- off by a second,
//         because it was measured between different instants.
//   #103  Tokens summed input + output and said nothing of the cache; the
//         Checkpoints tile counted attempt records only, beside a listing
//         that could say how many are still in the bucket.
//   #101  Details printed every event with its full JSON; the full timeline
//         lives on Attempts, and Details keeps the last event and a link.
//   #278  A step that changed nothing in the repository but wrote a
//         `change.diff` its dependant stages handed code on, and Details said
//         only "changed nothing".
//
// MUTATIONS, one per block: put the `Code` h2 back in GitOutcome; draw the
// per-attempt checkpoint table again, or the checkpoint panel; print `run` in the Headline; drop the
// cache sub-line; drop the listing from the Checkpoints tile; render the event
// list on Details; drop the handed-on block from the Code section.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun, WorkflowRead } from '../api'
import type { Result } from '../fetch'
import type { ArtifactContent, CheckpointRecord, CheckpointsPage, Task } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function page(ids: string[]): CheckpointsPage {
  const checkpoints: CheckpointRecord[] = ids.map((id) => ({
    checkpoint_id: id,
    attempt_id: 'att_1',
    attempt_known: true,
    attempt_created_at: null,
    attempt_completed_at: null,
    prefix: `tenants/acme/tasks/tsk_charts/attempts/att_1/checkpoints/${id}/`,
    uri: `gs://b/tenants/acme/tasks/tsk_charts/attempts/att_1/checkpoints/${id}/`,
    is_latest_pointer: false,
    objects: [],
    stored_bytes: 1024,
    manifest: 'present',
    manifest_detail: null,
    created_at: at(5),
    seq: null,
    generation: null,
    label: null,
    archive_bytes: null,
    archive_sha256: null,
    file_count: null,
    resumable: true,
    resumable_detail: null,
  }))
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    prefix: 'tenants/acme/tasks/tsk_charts/attempts/',
    checkpoints,
    count: checkpoints.length,
    total_found: checkpoints.length,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
  }
}

function run(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1, started_at: at(1), completed_at: at(10) }),
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

/** Mount the Details body with the checkpoint listing answering `listing`. */
async function mount(r: AgentRun, listing: Promise<Result<CheckpointsPage>> | null = null): Promise<HTMLElement> {
  api.loadCheckpoints.mockReturnValue(listing ?? Promise.resolve(ok(page([]))))
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
  return container as HTMLElement
}

/**
 * One cell of the stat strip (agent-details-v3.html A), or -- for
 * `Checkpoints`, which moved out of the figures and into the leading card --
 * that card's checkpoint fact.
 */
function tile(el: HTMLElement, label: string): HTMLElement {
  const found =
    label === 'Checkpoints'
      ? [...el.querySelectorAll<HTMLElement>('.dt-now .dt-nf')].find((t) => t.textContent?.startsWith('Checkpoints'))
      : [...el.querySelectorAll<HTMLElement>('.dt-sc')].find((t) => t.querySelector('.dt-sc-l')?.textContent?.startsWith(label))
  expect(found, `no ${label} tile`).toBeTruthy()
  return found!
}

/** The keys the leading card prints: the run's facts, said once. */
/** The leading Now card's own words: v3 draws no `.ctl-fact` in it, so the text is what can say `run 9m`. */
function nowText(el: HTMLElement): string {
  const now = el.querySelector('.run-stack > .dt-now')
  expect(now, 'no Now card leads the tab').not.toBeNull()
  return now!.textContent ?? ''
}

/** Every card heading on the tab. */
function cardHeads(el: HTMLElement): string[] {
  return [...el.querySelectorAll('.dt-card-head > b, section > h2')].map((h) => h.textContent?.trim() ?? '')
}

// ---------------------------------------------------------------------------
// #102
// ---------------------------------------------------------------------------

describe('#102 (a): Output and Code are one section, under one heading', () => {
  it('draws one heading over the result, never Output stacked on Code', async () => {
    const el = await mount(
      run({
        task: task({
          state: 'SUCCEEDED',
          started_at: at(1),
          completed_at: at(10),
          result_summary: {
            artifacts: [{ name: 'swarm-work.patch', bytes: 10, uri: 'gs://b/swarm-work.patch' }],
            git: { commits: [], dirty: ['a.ts'], dirty_count: 1, patch: 'swarm-work.patch', published: false, strategy: 'collect' },
          },
        }),
      }),
    )
    // A finished agent's code card leads the tab, headed Outcome
    // (agent-details-v3.html A3): still one heading over the result.
    const heads = cardHeads(el).filter((t) => /^(Output|Code|Outcome)\b/.test(t))
    expect(heads, `headings: ${heads.join(' | ')}`).toHaveLength(1)
    expect(heads[0]).toBe('Outcome')
    // The facts the section carried are all still there.
    expect(el.textContent).toContain('nothing pushed · collect')
    expect(el.textContent).toContain('gs://b/swarm-work.patch')
  })

  it('keeps the Output heading when there is no code to speak of', async () => {
    const el = await mount(run({ task: task({ state: 'RUNNING', started_at: at(1) }) }))
    // NO OUTPUT CARD BEFORE THERE IS OUTPUT (agent-details-v3.html A): the
    // Now card says so in one line, and nothing is headed Code.
    const heads = cardHeads(el)
    expect(heads.filter((t) => /^(Output|Code)\b/.test(t))).toHaveLength(0)
    expect(el.querySelector('.dt-now')?.textContent).toContain('Output none yet · written when the attempt ends')
  })
})

describe('#102 (b): checkpoints are listed once, in the Checkpoints tab', () => {
  // AGENTS V1 (agents.html, decided 2026-10-01): the checkpoint panel left
  // Details for the agent's Checkpoints tab (`CheckpointsPane` draws
  // `RunFiles`). Details keeps the attempt card's one-line count and the
  // Checkpoints tile, and draws no checkpoint panel or table of its own.
  // MUTATION: put `<RunFiles ... listing={listing} />` back in `Run`.
  it('draws no checkpoint table on the attempt card, and no Checkpoints panel on Details', async () => {
    const el = await mount(
      run({ attempts: [attempt(1, { checkpoints: ['ckpt-00001', 'ckpt-00002'] })] }),
      Promise.resolve(ok(page(['ckpt-00001', 'ckpt-00002']))),
    )
    const card = el.querySelector('.att-card')!
    expect(card.querySelector('table'), 'the attempt card still lists its checkpoints').toBeNull()
    // Condensed, not lost: the card still says how many it wrote.
    expect(card.querySelector('.att-ckpt-line')?.textContent).toMatch(/2 written/)
    // The listing HAS landed -- the tile reads it -- so an absent panel is
    // not a panel still loading.
    await waitFor(() => expect(tile(el, 'Checkpoints').textContent).toMatch(/^Checkpoints 2\b.* · 2 kept/))
    const panel = [...el.querySelectorAll<HTMLElement>('section')].find(
      (s) => s.querySelector('h2')?.textContent === 'Checkpoints',
    )
    expect(panel, 'Details still draws the checkpoint panel the Checkpoints tab owns').toBeUndefined()
    // Nor the listing's prefix, which only the panel draws.
    expect(el.querySelector('.ckpt-prefix'), 'the checkpoint prefix is drawn on Details').toBeNull()
  })
})

describe('#102 (c): one duration, said once', () => {
  it('drops the Headline run fact, which is the Elapsed tile', async () => {
    const el = await mount(run())
    // The Elapsed figure is the strip's alone: the Now card repeats neither
    // a `run <duration>` fact nor the figure itself.
    const elapsed = tile(el, 'Elapsed').querySelector('.dt-sc-v')?.textContent
    expect(elapsed).toBe('9m 0s')
    expect(nowText(el)).not.toMatch(/\brun\s+\d/)
    expect(nowText(el)).not.toContain(elapsed!)
  })

  it('keeps a wait, which the tile does not carry', async () => {
    const el = await mount(run({ task: task({ state: 'QUEUED', started_at: null }), attempts: [] }))
    // In the Now card's phase line: the whole age of an unstarted task is a wait.
    expect(el.querySelector('.dt-now .dt-phase')?.textContent).toMatch(/^no attempt yet · waiting \d/)
  })

  it('prints a per-attempt ran only when there are several attempts', async () => {
    const one = await mount(run())
    const oneCard = one.querySelector('.att-card')!
    expect([...oneCard.querySelectorAll('.ctl-fact > b')].map((b) => b.textContent)).not.toContain('ran')

    const two = await mount(
      run({
        task: task({ state: 'SUCCEEDED', attempt_count: 2, started_at: at(1), completed_at: at(20) }),
        attempts: [
          attempt(1, { exit_code: 1 }),
          // 48.6s: one formatter, rounding once, whichever pane draws it.
          attempt(2, { created_at: at(11), started_at: at(12), completed_at: new Date(Date.parse(at(12)) + 48_600).toISOString() }),
        ],
      }),
    )
    const cards = [...two.querySelectorAll('.att-card')]
    expect(cards).toHaveLength(2)
    const ran = cards.map((c) =>
      [...c.querySelectorAll('.ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'ran')?.textContent,
    )
    expect(ran).toEqual(['ran9m 0s', 'ran49s'])
  })
})

// ---------------------------------------------------------------------------
// #103
// ---------------------------------------------------------------------------

describe('#103: the Tokens tile says what it leaves out', () => {
  it('counts the cache, summed across attempts', async () => {
    const el = await mount(
      run({
        task: task({ state: 'SUCCEEDED', attempt_count: 2, started_at: at(1), completed_at: at(20) }),
        attempts: [
          attempt(1, { input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 600_000, cache_creation_input_tokens: 10_000 }),
          attempt(2, { created_at: at(11), input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 400_000, cache_creation_input_tokens: null }),
        ],
      }),
    )
    // Tokens are Cost's sub-line (agent-details-v3.html A); #322: the headline
    // is all four kinds, and the caption says each one.
    const t = tile(el, 'Cost')
    expect(t.querySelector('.dt-sc-s')?.textContent).toMatch(/^1\.01M tokens/)
    expect(t.textContent).toContain('cache read 1M · write 10k')
  })

  it('draws no cache line when no attempt reported one: absent stays absent, never 0', async () => {
    const el = await mount(run({ attempts: [attempt(1, { input_tokens: 100, output_tokens: 50 })] }))
    expect(tile(el, 'Cost').textContent).not.toMatch(/cache/)
  })
})

describe('#103: the Checkpoints tile says what is still in the bucket', () => {
  it('reads "N written, M kept", marked partial when they differ', async () => {
    const el = await mount(
      run({ attempts: [attempt(1, { checkpoints: ['ckpt-00001', 'ckpt-00002'] })] }),
      Promise.resolve(ok(page([]))),
    )
    await waitFor(() => expect(tile(el, 'Checkpoints').textContent).toMatch(/^Checkpoints 2\b.* · 0 kept/))
    expect(tile(el, 'Checkpoints').querySelector('.ctl-mark.is-partial')).not.toBeNull()
  })

  it('draws no mark when the bucket holds what the records name', async () => {
    const el = await mount(
      run({ attempts: [attempt(1, { checkpoints: ['ckpt-00001'] })] }),
      Promise.resolve(ok(page(['ckpt-00001']))),
    )
    await waitFor(() => expect(tile(el, 'Checkpoints').textContent).toMatch(/^Checkpoints 1\b.* · 1 kept/))
    expect(tile(el, 'Checkpoints').querySelector('.ctl-mark.is-partial')).toBeNull()
  })

  it('claims nothing about the bucket before the listing is read', async () => {
    const el = await mount(
      run({ attempts: [attempt(1, { checkpoints: ['ckpt-00001'] })] }),
      new Promise(() => {}),
    )
    expect(tile(el, 'Checkpoints').textContent).not.toMatch(/ kept/)
  })
})

// ---------------------------------------------------------------------------
// #101, the Details half
// ---------------------------------------------------------------------------

describe('#101: Details carries the compact timeline and a link to the full one', () => {
  const events = [
    ev('submitted', at(-1), null),
    ev('started', at(1), 'att_1', { execution: 'exec-1' }),
    ev('succeeded', new Date(Date.parse(at(1)) + 189_000).toISOString(), 'att_1', { exit_code: 0 }),
  ]

  it('shows the last event with its gap, the absolute time in its title, and no JSON', async () => {
    const el = await mount(run({ events }))
    // PROGRESS CARRIES THE LAST EVENTS, newest first (agent-details-v3.html A).
    const section = el.querySelector<HTMLElement>('.dt-progress')!
    expect(section, 'the Timeline section is gone').toBeTruthy()
    expect(section.querySelector('ol.timeline'), 'Details still draws the full event list').toBeNull()
    expect(section.querySelector('.ev-detail')).toBeNull()
    expect(section.textContent).toContain('succeeded')
    expect(section.textContent).toContain('+3m 09s')
    const when = section.querySelector('.dt-ev time')!
    expect(when.getAttribute('title')).toContain('2026-09-22 10:04:09')
    const link = section.querySelector<HTMLAnchorElement>('a[href="#work/task/tsk_charts/attempts"]')
    expect(link?.textContent).toBe('Attempts tab ›')
  })
})

// ---------------------------------------------------------------------------
// #278
// ---------------------------------------------------------------------------

const DIFF = [
  'diff --git a/src/a.ts b/src/a.ts',
  '--- a/src/a.ts',
  '+++ b/src/a.ts',
  '@@ -1,2 +1,3 @@',
  ' keep',
  '-gone',
  '+added',
  '+more',
  'diff --git a/b.md b/b.md',
  '--- a/b.md',
  '+++ b/b.md',
  '@@ -1 +1 @@',
  '+line',
  '',
].join('\n')

function diffContent(): ArtifactContent {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    attempt_id: 'att_1',
    artifact: { name: 'change.diff', bytes: DIFF.length, uri: 'gs://b/change.diff' },
    status: 'ok',
    detail: null,
    key: 'k',
    uri: 'gs://b/change.diff',
    content: DIFF,
    total_bytes: DIFF.length,
    offset: 0,
    returned_bytes: DIFF.length,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
  }
}

const IMPLEMENT: Task = task({
  state: 'SUCCEEDED',
  started_at: at(1),
  completed_at: at(10),
  workflow_id: 'wf_1',
  step_id: 'implement',
  result_summary: {
    artifacts: [{ name: 'change.diff', bytes: DIFF.length, uri: 'gs://b/change.diff' }],
    git: {
      commits: [],
      dirty: [],
      published: false,
      publish_reason: 'the agent changed nothing in the repository',
    },
  },
})

function workflow(pr: boolean, fixStages: string | null = 'change.diff'): WorkflowRead {
  const step = (step_id: string, depends_on: string[], input_from: Record<string, string>, task_id: string) => ({
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from,
    task_id,
  })
  const fix = task({
    id: 'tsk_fix',
    workflow_id: 'wf_1',
    step_id: 'fix',
    state: pr ? 'SUCCEEDED' : 'RUNNING',
    dispatch: { strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: [] },
    result_summary: pr
      ? { git: { pull_request: { number: 42, url: 'https://github.com/o/r/pull/42', state: 'open', created: true } } }
      : null,
  } as Partial<Task>)
  return {
    workflow: {
      workflow_id: 'wf_1',
      steps: [
        step('implement', [], {}, IMPLEMENT.id),
        step('fix', ['implement'], fixStages === null ? {} : { implement: fixStages }, 'tsk_fix'),
      ],
    },
    tasks: [IMPLEMENT, fix],
  } as unknown as WorkflowRead
}

async function codeSection(wf: WorkflowRead): Promise<HTMLElement> {
  api.loadWorkflow.mockResolvedValue(ok(wf))
  api.loadArtifactContent.mockResolvedValue(ok(diffContent()))
  const el = await mount(run({ task: IMPLEMENT }))
  return el.querySelector<HTMLElement>('.run-output')!
}

describe('#278: a step that changed nothing but handed on a diff says so', () => {
  it('keeps "changed nothing in the repository" and adds the handed-on code, the diff and the PR', async () => {
    const section = await codeSection(workflow(true))
    expect(section, 'no Code section').toBeTruthy()
    expect(section.textContent).toContain('changed nothing in the repository')
    await waitFor(() => expect(section.textContent).toContain('+3 −1 in 2 files, handed to fix as change.diff'))
    await waitFor(() => expect(section.querySelector('.art-viewer .diff')).not.toBeNull())
    const pr = section.querySelector<HTMLAnchorElement>('a[href="https://github.com/o/r/pull/42"]')
    expect(pr, 'no link to the integrator’s pull request').not.toBeNull()
    expect(pr!.textContent).toContain('#42')
  })

  it('links no pull request before the integrator has opened one', async () => {
    const section = await codeSection(workflow(false))
    await waitFor(() => expect(section.textContent).toContain('handed to fix as change.diff'))
    expect(section.querySelector('a[href^="https://github.com"]')).toBeNull()
  })

  it('says nothing is handed on when no dependant stages the diff', async () => {
    const section = await codeSection(workflow(true, null))
    await waitFor(() => expect(api.loadWorkflow).toHaveBeenCalled())
    expect(section.textContent).toContain('changed nothing in the repository')
    expect(section.textContent).not.toContain('handed to')
  })
})
