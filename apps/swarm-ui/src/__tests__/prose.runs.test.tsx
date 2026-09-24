// THE RUN GROUP'S PROSE BUDGET, AND THE ENCODINGS THAT REPLACED THE PROSE.
//
// WHAT THIS FILE IS FOR. `honesty.prose.test.tsx` asks the qualitative
// question of Overview, Accounts and Runtimes -- "with every help card closed,
// can a reader still tell missing from zero?". This file asks the same
// question of the RUN group (Agents, AgentDetail, AttemptTimeline,
// ArtifactViewer) and adds the quantitative one the owner's directive turns
// on: HOW MANY WORDS does each screen put on the glass?
//
// A BUDGET RATHER THAN A SNAPSHOT. A snapshot of rendered text goes stale the
// first time a fixture changes and teaches everyone to re-record it. A ceiling
// on the word count fails only when a screen grows a paragraph back, which is
// exactly the regression this group was rebuilt to prevent. The numbers below
// were measured on these fixtures, before and after, and both are recorded
// next to the ceiling so the next reader can see what the ceiling is made of.
//
// THE INVARIANT IS PRESERVED; THE MEDIUM IS FREE. Every assertion here that
// used to read a sentence now reads the encoding that carries the same fact --
// `.ctl-mark`, `.ctl-em`, `.ctl-facts .is-absent`, a hatched track, an
// `aria-label` -- plus the accessible route to the words. Where a sentence
// moved, the test says where it went.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AgentRun, ResourceClasses } from '../api'
import type {
  ArtifactContent,
  ArtifactRef,
  AttemptRow,
  Task,
  TaskEvent,
  TaskPage,
} from '../types'

// ---------------------------------------------------------------------------
// The api module, replaced wholesale
// ---------------------------------------------------------------------------

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { Run } from '../AgentDetail'
import { AttemptTimelineScreen } from '../AttemptTimeline'
import { ArtifactViewer } from '../ArtifactViewer'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
//
// Deliberately the HARD shape in every case: a retried run, an attempt with no
// peak memory, a runner that reports no spend, a checkpoint whose event is off
// the page, a truncated and redacted artifact. A prose budget measured on an
// empty screen measures nothing.

const NOW = '2026-09-23T12:00:00.000Z'
const THEN = '2026-09-23T10:00:00.000Z'

function task(over: Partial<Task> = {}): Task {
  return {
    id: 'tsk_0123456789abcdef',
    tenant_id: 'acme',
    state: 'RUNNING',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: THEN,
    updated_at: NOW,
    started_at: THEN,
    completed_at: null,
    submitted_by: 'ada@acme.test',
    attempt_count: 3,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_5e5ad3b6f7da',
    step_id: 'synthesise',
    depends_on: null,
    cancel_requested: false,
    repository_url: 'https://github.com/acme/widgets',
    model: 'claude-opus-4',
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: { ticket: 'ACME-41' },
    repository_ref: 'main',
    input: { prompt: 'Summarise the incident.' },
    last_error: null,
    result_summary: null,
    latest_checkpoint: 'gs://acme/ckpt/ck_9',
    current_generation: 3,
    current_lease_id: 'lse_3',
    ...over,
  }
}

function attempt(n: number, over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${n}`,
    task_id: 'tsk_0123456789abcdef',
    tenant_id: 'acme',
    generation: n,
    lease_id: `lse_${n}`,
    backend: 'cloudrun',
    execution_name: `exec-${n}`,
    created_at: `2026-09-23T0${n}:00:00.000Z`,
    started_at: `2026-09-23T0${n}:01:00.000Z`,
    completed_at: `2026-09-23T0${n}:40:00.000Z`,
    exit_code: 1,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...over,
  }
}

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 },
}

const EVENTS: TaskEvent[] = [
  {
    event_id: 'ev_1',
    task_id: 'tsk_0123456789abcdef',
    type: 'submitted',
    at: THEN,
    attempt_id: null,
    lease_id: null,
    generation: null,
    detail: null,
  },
  {
    event_id: 'ev_2',
    task_id: 'tsk_0123456789abcdef',
    type: 'checkpoint_completed',
    at: NOW,
    attempt_id: 'att_3',
    lease_id: 'lse_3',
    generation: 3,
    detail: { checkpoint_id: 'ck_9', uri: 'gs://acme/ckpt/ck_9', bytes: 4096 },
  },
]

/** The run the detail screen is hardest on: three attempts, nothing measured. */
function run(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: task(),
    events: EVENTS,
    eventsDetail: null,
    attempts: [attempt(1), attempt(2), attempt(3, { checkpoints: ['ck_9'], exit_code: null, completed_at: null })],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

function page(): TaskPage {
  return {
    tasks: [
      task(),
      task({ id: 'tsk_beta', state: 'PARKED', park_reason: 'QUOTA_EXHAUSTED', step_id: null, workflow_id: null }),
      task({ id: 'tsk_gamma', state: 'SUCCEEDED', completed_at: NOW }),
    ],
    tenant_id: 'acme',
    next_page_token: 'more',
  }
}

function artifact(over: Partial<ArtifactContent> = {}): ArtifactContent {
  return {
    task_id: 'tsk_0123456789abcdef',
    tenant_id: 'acme',
    attempt_id: 'att_3',
    artifact: { name: 'synthesis.md', bytes: 8192, uri: 'gs://acme/art/synthesis.md' },
    status: 'ok',
    detail: null,
    key: null,
    uri: 'gs://acme/art/synthesis.md',
    content: '# Findings\n\nOne short paragraph of the agent’s own output.\n',
    total_bytes: 8192,
    offset: 0,
    returned_bytes: 4096,
    next_offset: 4096,
    truncated: true,
    redacted: true,
    redaction_count: 4,
    redaction: { applied_at_read_time: true, rules: 12 },
    ...over,
  }
}

const REF: ArtifactRef = { name: 'synthesis.md', bytes: 8192, uri: 'gs://acme/art/synthesis.md' }

// ---------------------------------------------------------------------------
// The measure
// ---------------------------------------------------------------------------

/**
 * Rendered words, counted off `textContent`.
 *
 * WHAT COUNTS AS A WORD, and why it is not `split(/\s+/)`. A run of digits, a
 * `gs://` uri, an id and an em dash are DATA, and a measure that counted them
 * would move when a fixture gained a row rather than when a screen gained a
 * sentence. So a word here is a run of two or more letters -- which is what the
 * owner's directive is about -- and `4 of 7`, `tsk_0123`, `12m` and `$0.00`
 * contribute only the letters a reader has to read.
 *
 * `[data-help-description]` IS STRIPPED, exactly as `visibleText()` in
 * `honesty.prose.test.tsx` strips it and for the same reason: every `?` keeps
 * its topic's short text in the DOM at all times so `aria-describedby` can
 * point at it. That text is the accessible route to the words, not words on
 * the glass, and counting it would make MOVING a sentence into the `?` look
 * like keeping it. Stripping it also means this measure cannot be gamed the
 * other way: a screen that hid a paragraph behind that attribute would not
 * gain a single word.
 */
function words(el: HTMLElement): number {
  const copy = el.cloneNode(true) as HTMLElement
  for (const hidden of copy.querySelectorAll('[data-help-description]')) hidden.remove()
  const text = copy.textContent ?? ''
  return (text.match(/[A-Za-z][A-Za-z'’-]+/g) ?? []).length
}

/** No card is open, so nothing below can pass by reading an open popup. */
function expectAllCardsClosed(): void {
  expect(document.querySelector('.ctl-q-card[data-open="true"], .helpcard.is-open')).toBeNull()
}

async function renderAgents(rows: TaskPage = page()): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: rows,
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} />)
  await waitFor(() => expect(container.querySelector('.tabs, .ctl-seg')).not.toBeNull())
  return container as HTMLElement
}

/**
 * `RunFiles` owns two reads of its own and mounts inside `Run`. Both are
 * stubbed to a real zero rather than left undefined: an unstubbed loader
 * returns `undefined` and the panel throws on `.then`, which would make this
 * file measure a crashed subtree rather than the screen.
 */
async function renderDetail(over: Partial<AgentRun> = {}): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={run(over)} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull())
  return container as HTMLElement
}

async function renderTimeline(): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue({
    status: 'ok',
    data: { attempts: [attempt(1), attempt(2)] },
    fetchedAt: Date.now(),
  })
  api.loadAgentDetail.mockResolvedValue({
    status: 'ok',
    data: { task: task(), events: EVENTS, eventsDetail: null },
    fetchedAt: Date.now(),
  })
  const { container } = render(<AttemptTimelineScreen taskId="tsk_0123456789abcdef" />)
  await waitFor(() => expect(container.querySelector('.ctl-facts, .timeline')).not.toBeNull())
  return container as HTMLElement
}

async function renderViewer(over: Partial<ArtifactContent> = {}): Promise<HTMLElement> {
  api.loadArtifactContent.mockResolvedValue({
    status: 'ok',
    data: artifact(over),
    fetchedAt: Date.now(),
  })
  const { container } = render(
    <ArtifactViewer taskId="tsk_0123456789abcdef" artifact={REF} onClose={() => {}} />,
  )
  await waitFor(() => expect(container.querySelector('.art-prov')).not.toBeNull())
  return container as HTMLElement
}

// ---------------------------------------------------------------------------
// 1. The budget
// ---------------------------------------------------------------------------

describe('the run group carries data, not prose', () => {
  /**
   * MEASURED ON THESE FIXTURES, 2026-09-23, with `[data-help-description]`
   * stripped both times.
   *
   *                    before   after   ceiling
   *   Agents               45      18       30
   *   AgentDetail         758     324      380
   *   AttemptTimeline     176      74      100
   *   ArtifactViewer       71      19       35
   *   -------------------------------------------
   *   total              1050     435
   *
   * The ceiling is above the measurement on purpose: it is a regression gate,
   * not a snapshot. A screen that grows a paragraph back blows it; a screen
   * that gains a column or a state word does not.
   *
   * WHY AgentDetail'S CEILING IS THE LOOSEST, stated so nobody reads it as
   * slack. About a hundred of its 324 words come from components this group
   * does not own and did not touch: `charts/TokenSpend.tsx`'s empty state
   * (~35), `RunFiles.tsx`'s two no-body panels (~35), `Dispatch.tsx`'s absent
   * dispatch (~15) and `Liveness.tsx`'s badge. A further ~30 are the six help
   * TITLES in the card foot, which are the index the deleted legend used to
   * be and are the one place §8.4 wants words. The screen's own remaining text
   * is keys, state words, marks and units.
   */
  const CEILING: Record<string, number> = {
    agents: 30,
    detail: 380,
    timeline: 100,
    viewer: 35,
  }

  it('Agents fits its word budget', async () => {
    const el = await renderAgents()
    expectAllCardsClosed()
    expect(words(el)).toBeLessThanOrEqual(CEILING.agents!)
  })

  it('AgentDetail fits its word budget on a three-attempt run with nothing measured', async () => {
    const el = await renderDetail()
    expectAllCardsClosed()
    expect(words(el)).toBeLessThanOrEqual(CEILING.detail!)
  })

  it('AttemptTimeline fits its word budget', async () => {
    const el = await renderTimeline()
    expect(words(el)).toBeLessThanOrEqual(CEILING.timeline!)
  })

  it('ArtifactViewer fits its word budget on a truncated, redacted artifact', async () => {
    const el = await renderViewer()
    expect(words(el)).toBeLessThanOrEqual(CEILING.viewer!)
  })
})

// ---------------------------------------------------------------------------
// 2. The encodings that replaced the sentences
// ---------------------------------------------------------------------------

describe('Agents, with every help card closed', () => {
  it('states a tab is a real zero with a mark rather than a paragraph', async () => {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: { tasks: [task({ state: 'SUCCEEDED', completed_at: NOW })], tenant_id: 'acme' },
      fetchedAt: Date.now(),
    })
    render(<AgentsScreen onOpen={() => {}} />)
    await screen.findByText('claude-code')
    // The screen LANDS on Recent now -- the first tab with rows -- so the empty
    // Live tab is the one a reader asks for. Asked for, it still says so.
    fireEvent.click(screen.getByRole('tab', { name: /^Live/ }))
    // The `live` tab holds nothing, and its emptiness is a MEASURED zero.
    // WHAT MOVED: "No agent is holding a pool slot right now -- a real zero
    // from a successful read" was a sentence in the middle of the screen. The
    // fact is now `.ctl-mark.is-zero`, whose word is `real zero` and whose
    // accessible name is the sentence; the explanation of which states cost
    // nothing lives in help topic `capacity`, linked from the empty state.
    const empty = document.querySelector('.ctl-empty')
    expect(empty).not.toBeNull()
    const mark = empty!.querySelector('.ctl-mark.is-zero')
    expect(mark, 'a real zero is not marked as one').not.toBeNull()
    expect(mark!.textContent).toMatch(/real zero/i)
    // THE ROUTE TO THE WORDS. `.ctl-q` is a button while the card is shut and
    // becomes an anchor to `#help/capacity` when it opens, which is exactly
    // the property that makes this assertion mean something: the topic is
    // reachable by keyboard and the topic's text is NOT on the glass.
    expect(
      empty!.querySelector('button[aria-expanded]'),
      'the capacity topic is unreachable from the empty tab',
    ).not.toBeNull()
  })

  it('says the counts describe the loaded page only, as a qualifier not a sentence', async () => {
    const el = await renderAgents()
    // WHAT MOVED: "filters apply to the N loaded rows" and "More rows exist
    // beyond this page. Counts and grouping above describe only what is
    // loaded." were two sentences. Both are now one `.ctl-card-note`-shaped
    // qualifier carrying the two figures, with the full sentence in its
    // accessible name.
    const note = el.querySelector('.ag-scope')
    expect(note, 'the client-side scope qualifier is gone').not.toBeNull()
    expect(note!.textContent).toMatch(/\d/)
    expect(note!.getAttribute('aria-label') ?? '').toMatch(/loaded/i)
  })

  it('keeps the reason an agent has not moved on the surface', async () => {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: {
        tasks: [task({ state: 'PARKED', park_reason: 'QUOTA_EXHAUSTED' })],
        tenant_id: 'acme',
      },
      fetchedAt: Date.now(),
    })
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(container.querySelector('.ctl-seg')).not.toBeNull())
    // A PARKED agent is in the `waiting` tab, not the `live` one -- parked
    // work holds no slot, which is the whole point of the split. (The screen
    // lands on Waiting here by itself; the click says which tab is meant.)
    fireEvent.click(screen.getByRole('tab', { name: /Waiting/ }))
    // UNCHANGED BY THE PASS, and asserted so it stays that way: the "why" line
    // is the one piece of running text a row is allowed, because it is the
    // answer somebody opened the screen for.
    await waitFor(() => expect(container.querySelector('.why')).not.toBeNull())
  })
})

// ---------------------------------------------------------------------------
// 2b. The state, drawn once
// ---------------------------------------------------------------------------
//
// NEW, AND IT PINS THE RESTRAINT PASS'S CHIP DECISION WHERE IT LANDS HARDEST.
// design-system.md §6.6 rebuilt `.ctl-chip` as A MARK AND A WORD, and §11.2
// names the screens' half of it: `{stateGlyph(state)} {state}` inside the chip
// was a SECOND shape encoding of the same fact, which the pill was hiding. A
// run list draws forty of these, so this is the screen where a regression
// would cost the most and be noticed the least.
//
// WHAT MOVED, AND WHY THIS IS A STRONGER CLAIM THAN WHAT IT REPLACES. Nothing
// here used to be asserted at all -- the run row's state was covered only by
// the word budget, which a duplicate glyph does not move because a bullet is
// not a word. The assertions below are on the ENCODING: one mark, the word at
// full ink beside it, and the tone derived rather than guessed. The honesty
// invariant is in the last two: the WORD is what separates PARKED from READY
// from QUEUED (the mark is a tone channel, not a state channel), and a live
// state must never borrow the healthy silhouette.
describe('the run list draws a state once', () => {
  it('gives a row exactly one mark, and the word beside it', async () => {
    const container = await renderAgents()
    const chip = container.querySelector('.row .ctl-chip')
    expect(chip, 'the run row draws no state chip').not.toBeNull()

    // ONE MARK. The chip's own `<i>` is the mark; a second `<i>`, or a
    // `stateGlyph` bullet rendered beside it, is the "decorative double dot"
    // the owner named and is what this assertion exists to catch.
    expect(chip!.querySelectorAll('i').length, 'a second mark is drawn inside the chip').toBe(1)
    expect(chip!.textContent, 'a bullet glyph survives beside the word').not.toMatch(
      /[●○✓✗⌀⏸]/,
    )

    // THE WORD IS MANDATORY (§6.6) and it is the row's accessible route to the
    // state: the mark is `aria-hidden`, so if the word goes, a screen reader
    // is told nothing at all. Rendered uppercase by the API and lowercased by
    // the sheet, so the assertion is case-insensitive on purpose.
    expect(chip!.textContent?.trim()).toMatch(/^running$/i)
  })

  it('never lets a live state borrow the healthy mark', async () => {
    const container = await renderAgents()
    // RUNNING holds a pool slot and costs money; SUCCEEDED does not. The two
    // must not resolve to the same silhouette, which is what would happen if a
    // screen mapped the tone by hand instead of through `stateTone`.
    const live = container.querySelector('.row .ctl-chip')
    expect(live!.classList.contains('is-live'), 'a RUNNING agent is not marked live').toBe(true)
    expect(live!.classList.contains('is-ok'), 'a RUNNING agent borrowed the healthy mark').toBe(
      false,
    )

    // The `recent` tab holds the SUCCEEDED row, and it is the other half of
    // the same claim.
    fireEvent.click(screen.getByRole('tab', { name: /Recent/ }))
    await waitFor(() => expect(container.querySelector('.row .ctl-chip.is-ok')).not.toBeNull())
    expect(
      container.querySelector('.row .ctl-chip.is-live'),
      'a finished agent is still drawn as live',
    ).toBeNull()
  })

  it('draws a workflow group rollup with the same chip, not a filled pill', async () => {
    // `.roll` was a 999px pill with a tinted background and the word in
    // `--*-ink`, uppercase and tracked -- the silhouette §6.6 measured at
    // 102x23px and replaced. WHAT MOVED: the fact is unchanged and is still
    // the rolled-up word; what went is a second way of drawing a state.
    const container = await renderAgents()
    fireEvent.click(screen.getByRole('tab', { name: /Recent/ }))
    fireEvent.click(screen.getByLabelText(/group by workflow/i))
    await waitFor(() => expect(container.querySelector('.section.group')).not.toBeNull())
    const head = container.querySelector('.section.group > h2')
    expect(head!.querySelector('.roll'), 'the rollup is still a filled pill').toBeNull()
    const rollChip = head!.querySelector('.ctl-chip')
    expect(rollChip, 'the rollup lost its state word').not.toBeNull()
    expect(rollChip!.textContent?.trim()).toMatch(/^(running|succeeded|failed|waiting)$/)
  })
})

describe('AgentDetail, with every help card closed', () => {
  it('writes an unmeasured peak as a mark and an em dash, never as a figure', async () => {
    const el = await renderDetail()
    expectAllCardsClosed()
    // WHAT MOVED: the paragraphs under the utilisation bars -- "This attempt is
    // over, so the memory figure is the last heartbeat reading...", "the final
    // high-water mark is written at the end of an attempt" -- moved to help
    // topics `peak-memory` and `oom-near-miss`. What the SCREEN carries is the
    // hatched track with no fill, the em dash in the figure slot, and the
    // `by` column's two words. That is a stronger claim than the sentence: a
    // paragraph can sit beside a bar it does not describe; a hatch cannot.
    const track = el.querySelector('.ctl-util-track.is-unknown')
    expect(track, 'an unknown utilisation is not hatched').not.toBeNull()
    expect(track!.querySelector('.ctl-util-fill'), 'a hatched track must carry no fill').toBeNull()
    const figure = el.querySelector('.ctl-util-figure')
    expect(figure!.querySelector('.ctl-em')).not.toBeNull()
  })

  it('writes an unreported cost as a phrase on a dashed tile, never as $0.00', async () => {
    const el = await renderDetail()
    expectAllCardsClosed()
    const strip = el.querySelector('.ctl-metrics')!
    const absent = strip.querySelector('.ctl-metric.is-absent')
    expect(absent, 'an absent metric has lost its dashed treatment').not.toBeNull()
    // SCOPED TO THE STRIP ON PURPOSE. `charts/TokenSpend.tsx` -- which this
    // group does not own -- prints the literal string "$0.00" inside its own
    // empty state, as part of the sentence saying an absent measurement is not
    // one. The rule being pinned here is that no TILE renders a figure the
    // platform never reported.
    expect(strip.textContent).not.toContain('$0.00')
    expect(strip.textContent).toContain('not reported')
  })

  it('renders a MEASURED zero as a digit, which is the other half of the rule', async () => {
    const el = await renderDetail()
    const tiles = [...el.querySelectorAll('.ctl-metric')]
    const ckpt = tiles.find((t) => (t.textContent ?? '').toLowerCase().includes('checkpoint'))
    expect(ckpt).toBeDefined()
    // Measured: the attempt documents were read and one lists a checkpoint.
    expect(ckpt!.querySelector('.ctl-metric-value')!.textContent).toMatch(/^\d/)
    expect(ckpt!.classList.contains('is-absent')).toBe(false)
  })

  it('states a failed attempt read as a mark, with no number anywhere near it', async () => {
    const el = await renderDetail({ attempts: null, attemptsDetail: 'The attempt query timed out.' })
    expectAllCardsClosed()
    // WHAT MOVED: nothing. The detail sentence IS the fact -- which read failed
    // and how -- and §8.4 keeps it as the empty state's one sentence. What is
    // asserted is the encoding beside it: the mark says `not read`, and no
    // figure from the attempt read appears.
    const mark = el.querySelector('.ctl-mark.is-unread')
    expect(mark, 'a failed read is not marked as unread').not.toBeNull()
    expect(mark!.textContent).toMatch(/not read/i)
    const peak = [...el.querySelectorAll('.ctl-metric')].find((t) =>
      (t.textContent ?? '').toLowerCase().includes('peak memory'),
    )
    expect(peak!.classList.contains('is-unread')).toBe(true)
    expect(peak!.querySelector('.ctl-metric-value')!.textContent).not.toMatch(/\d/)
  })

  it('keeps the standing rules reachable as links rather than printing them', async () => {
    const el = await renderDetail()
    // WHAT MOVED: `AttemptLegend`'s six help titles were an inline paragraph of
    // links under every run. They are now the card foot's link row, and the
    // topics themselves are unchanged -- the words still live in `help.ts`.
    const legend = el.querySelector('.ctl-card-foot a[href^="#"]')
    expect(legend, 'the help topics the cards depend on are unreachable').not.toBeNull()
  })

  it('carries the dispatch instruction that prevents a second pull request', async () => {
    const el = await renderDetail({
      task: task({
        state: 'SUCCEEDED',
        completed_at: NOW,
        result_summary: {
          git: {
            published: true,
            role: 'contributor',
            strategy: 'integrate',
            branch: 'agent/acme-41',
            publish_reason: 'this step is a contributor',
          },
          artifacts: [],
          logs: {},
        },
      }),
    })
    // NOT SOFTENED. §8.5 allows an empty state one sentence, and this is the
    // one sentence that stops an operator doing the expensive wrong thing.
    expect(el.textContent).toContain('Do not open one from this branch')
  })
})

describe('AttemptTimeline, with every help card closed', () => {
  it('states an attempt with no events as a mark rather than a paragraph', async () => {
    const el = await renderTimeline()
    // WHAT MOVED: "The events endpoint orders oldest-first, caps the page
    // server-side and returns no page token -- so past one page the newest
    // events ... cannot be fetched at all" moved to help topic `event-paging`.
    // The screen carries the count and a `.ctl-mark.is-partial`.
    const mark = el.querySelector('.ctl-mark.is-pending, .ctl-mark.is-partial')
    expect(mark, 'the page-coverage caveat has no encoding at all').not.toBeNull()
    expect(mark!.getAttribute('aria-label') ?? '').toMatch(/page token|no events/i)
    // `#help/partial-read` is the destination the paragraph went to, reached
    // through the toolbar's `?`.
    expect(
      el.querySelector('.ctl-toolbar button[aria-expanded]'),
      'the paging topic is unreachable',
    ).not.toBeNull()
  })

  it('keeps every attempt figure in a facts strip, absences included', async () => {
    const el = await renderTimeline()
    // WHAT MOVED: the `<dl class="kv">` of Attempt / Duration / Exit code /
    // Peak RSS, and the trailing paragraph about `result_summary` describing
    // only the last attempt, which is now help topic `attempt-documents`.
    // A fact whose value was not read KEEPS ITS SLOT AND ITS KEY.
    const facts = el.querySelector('.ctl-facts')
    expect(facts, 'the attempt facts strip is missing').not.toBeNull()
    const absent = facts!.querySelector('.ctl-fact.is-absent')
    expect(absent, 'an unread fact has been dropped instead of kept as a slot').not.toBeNull()
    expect(absent!.querySelector('.ctl-em')).not.toBeNull()
  })
})

describe('ArtifactViewer', () => {
  it('draws a truncated window as a partial mark carrying both figures', async () => {
    const el = await renderViewer()
    // WHAT MOVED: "This is a window, not the whole artifact. Showing N of M
    // bytes. The rest was not read. Download below, or read the object from
    // its uri." -- four sentences -- became `.ctl-mark.is-partial` with the
    // word `partial`, the two figures beside it, and the full sentence as the
    // mark's accessible name. Help topic `artifact-window` carries the why.
    const mark = el.querySelector('.ctl-mark.is-partial')
    expect(mark, 'a truncated window is not marked partial').not.toBeNull()
    expect(el.querySelector('.art-prov')!.textContent).toMatch(/\d/)
    expect(mark!.getAttribute('aria-label') ?? '').toMatch(/not the whole artifact/i)
  })

  it('draws masked credentials as a warning mark with the count', async () => {
    const el = await renderViewer()
    // WHAT MOVED: "N credential-shaped values were masked in this artifact when
    // it was served. They are in the object in the bucket; masking here does
    // not remove them from there, and anything recognisable should be rotated."
    // The COUNT is the fact and stays on the glass; the rotation advice is help
    // topic `artifact-redaction`.
    const fact = el.querySelector('.ctl-fact.art-redacted')
    expect(fact, 'read-time redaction is no longer surfaced').not.toBeNull()
    expect(fact!.querySelector('.ctl-mark.is-unread')).not.toBeNull()
    expect(fact!.textContent).toContain('4')
    // The `?` is the route to the words. It is a button rather than an anchor
    // while the card is shut, which is what keeps this assertion honest: the
    // topic's text is reachable and is NOT on the glass.
    expect(
      el.querySelector('[aria-label*="Credential"], [aria-label*="credential"]'),
      'the redaction topic is unreachable',
    ).not.toBeNull()
  })

  it('tells a zero-byte artifact from a missing one, without a paragraph for either', async () => {
    const empty = await renderViewer({ content: '', truncated: false, redacted: false, redaction_count: 0 })
    // A MEASURED ZERO, NOT AN EM DASH. `''` is a file the agent created and
    // left blank; `.ctl-mark.is-zero` is the only mark in the kit that means
    // "the read succeeded and the answer is nothing".
    expect(empty.querySelector('.art-empty .ctl-mark.is-zero')).not.toBeNull()
    expect(empty.querySelector('.ctl-em')).toBeNull()
  })

  it('draws an absent object as a hatched mark and never as an empty document', async () => {
    api.loadArtifactContent.mockResolvedValue({
      status: 'ok',
      data: artifact({ status: 'absent', content: null, detail: 'The object is not in the bucket.' }),
      fetchedAt: Date.now(),
    })
    const { container } = render(
      <ArtifactViewer taskId="tsk_0123456789abcdef" artifact={REF} onClose={() => {}} />,
    )
    await waitFor(() => expect(container.querySelector('.ctl-empty')).not.toBeNull())
    expect(container.querySelector('.ctl-mark.is-absent')).not.toBeNull()
  })
})
