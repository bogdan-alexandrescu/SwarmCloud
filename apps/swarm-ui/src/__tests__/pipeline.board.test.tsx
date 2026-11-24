// AUTOMATE › PIPELINE, THE BOARD (docs/design/plans-pipeline.md §2.1, lane U1).
//
// WHAT EACH CASE HOLDS:
//   * each of the five stages draws the cards `GET /v1/pipeline` served in it,
//     under its own name and count;
//   * a card that waits on a person is edged amber (--s-warn) and sorts first
//     in its column, oldest first, with its time to expiry; a card that waits
//     on nobody shows no state word;
//   * a source filter rides on the address and on the read, keeps the five
//     columns and shows what each hides;
//   * a card opens the approval's own address carrying the board's filters,
//     and that page's Back returns to the board with them;
//   * a `partial` read fails only the columns it feeds;
//   * loading, a measured empty board, a failed read and a 404 are four
//     different screens;
//   * at 390px one stage is shown at a time, chosen from a row of stage
//     buttons, and nothing is laid out wider than the screen.
//
// The bodies are `pipeline.py`'s shapes. Placeholders only; nothing is
// token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function card(id: string, stage: string, over: Json = {}): Json {
  return {
    card_id: id,
    stage,
    source: { kind: 'sweep', schedule_type: 'issue-sweep', schedule_id: 'sch_a', firing_id: null, fingerprint: null, proposal_id: null },
    subject: { run_id: id.replace(/^run:/, ''), issue: 'example-org/example-api#12', title: `Title of ${id}` },
    state: 'running',
    waits_on_person: false,
    approval_kind: null,
    expires_at: null,
    since: hoursFromNow(-2),
    last_word: null,
    revision: null,
    plan_digest: null,
    pull_request: null,
    ...over,
  }
}

const waiting = (id: string, stage: string, kind: string, sinceH: number, over: Json = {}) =>
  card(id, stage, {
    state: kind === 'proposal' ? 'pending' : 'planned',
    waits_on_person: true,
    approval_kind: kind,
    expires_at: hoursFromNow(40),
    since: hoursFromNow(sinceH),
    subject: { run_id: id.replace(/^run:/, ''), issue: 'example-org/example-api#7', title: `Title of ${id}`, approval_id: id },
    ...over,
  })

const CARDS = [
  waiting('apr_prop', 'found', 'proposal', -5, { source: { kind: 'observer', schedule_type: 'observer' }, subject: { title: 'Title of apr_prop', approval_id: 'apr_prop' } }),
  card('run:run_planning', 'found', { state: 'planning' }),
  card('run:run_quiet', 'planned', { state: 'planned', since: hoursFromNow(-1) }),
  waiting('run:run_young', 'planned', 'plan', -3),
  waiting('run:run_old', 'planned', 'hold', -30, { source: { kind: 'failure_review', schedule_type: 'failure-review', fingerprint: 'fr-0001' } }),
  card('run:run_going', 'running', { state: 'running' }),
  waiting('run:run_pr', 'pr_ci', 'merge', -4, { state: 'checking', subject: { run_id: 'run_pr', title: 'Title of run:run_pr', approval_id: 'apr_merge' } }),
  card('run:run_done', 'ended', { state: 'done', since: hoursFromNow(-48) }),
]

function stages(cards: Json[], hidden: Json = {}): Json {
  const out: Json = {}
  for (const s of ['found', 'planned', 'running', 'pr_ci', 'ended']) {
    const mine = cards.filter((c) => c.stage === s)
    const since = mine.map((c) => String(c.since)).sort()
    out[s] = { count: mine.length, hidden: (hidden[s] as number | undefined) ?? 0, oldest_at: since[0] ?? null }
  }
  return out
}

function board(cards: Json[] = CARDS, over: Json = {}): Json {
  return {
    cards,
    by_stage: stages(cards),
    waits_on_person: cards.filter((c) => c.waits_on_person).length,
    partial: [],
    truncated: [],
    window_days: 14,
    tenant_id: 'eng',
    ...over,
  }
}

function routes(body: Json | null, status = 200, extra: Handler = () => null): Handler {
  return (m, url, b) => {
    const hit = extra(m, url, b)
    if (hit !== null) return hit
    if (m === 'GET' && url.split('?')[0] === '/v1/pipeline' && body !== null) return { status, body }
    return null
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Pipeline')
}

async function mount(view: string, handler: Handler) {
  const calls = serve(handler)
  const { PipelineScreen } = await load()
  const go = vi.fn()
  const utils = render(<PipelineScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Pipeline', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const column = (stage: string) => document.querySelector<HTMLElement>(`.pl-col[data-stage="${stage}"]`)!
const cardIds = (stage: string) => [...column(stage).querySelectorAll<HTMLElement>('.pl-card')].map((c) => c.dataset.card)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Pipeline board (U1)', () => {
  it('draws the five stages in order, each with the cards served in it and its count', async () => {
    await mount('', routes(board()))
    await screen.findByTestId('pl-board', undefined, WAIT)
    const heads = [...document.querySelectorAll('.pl-col > .pl-col-h > h2')].map((h) => h.textContent)
    expect(heads).toEqual(['Found', 'Planned or waiting', 'Approved and running', 'Pull request and CI', 'Merged or ended'])
    expect(cardIds('found')).toEqual(['apr_prop', 'run:run_planning'])
    expect(cardIds('running')).toEqual(['run:run_going'])
    expect(cardIds('pr_ci')).toEqual(['run:run_pr'])
    expect(cardIds('ended')).toEqual(['run:run_done'])
    // MUTATION: count the cards drawn rather than the served `by_stage`.
    expect(visible(screen.getByTestId('pl-count-planned'))).toBe('3')
    // Every card names its source in words (§3.1).
    expect(visible(column('found').querySelector('[data-card="apr_prop"] .pl-src'))).toBe('Observer')
    expect(visible(column('planned').querySelector('[data-card="run:run_old"] .pl-src'))).toBe('Failure review')
  })

  it('edges a card that waits on a person amber and sorts it first, oldest first, with its expiry', async () => {
    await mount('', routes(board()))
    await screen.findByTestId('pl-board', undefined, WAIT)
    // MUTATION: keep the served order of the planned column (quiet card first).
    expect(cardIds('planned')).toEqual(['run:run_old', 'run:run_young', 'run:run_quiet'])
    const old = column('planned').querySelector<HTMLElement>('[data-card="run:run_old"]')!
    const quiet = column('planned').querySelector<HTMLElement>('[data-card="run:run_quiet"]')!
    expect(old.classList.contains('is-waits')).toBe(true)
    // MUTATION: draw the edge in the info hue, or not at all.
    expect(painted(old, 'box-shadow', WIDE)).toContain('var(--s-warn)')
    expect(painted(quiet, 'box-shadow', WIDE) ?? 'none').not.toContain('--s-warn')
    // In whatever column it is: the merge in pull request and CI is amber too.
    expect(column('pr_ci').querySelector('[data-card="run:run_pr"]')!.classList.contains('is-waits')).toBe(true)
    expect(visible(old)).toMatch(/expires in \d+[hd]/)
    // A card that waits on nobody shows no kind or state word.
    expect(quiet.querySelector('.c-tag, .pl-due')).toBeNull()
    // The lead names what waits across the whole board.
    expect(visible(screen.getByTestId('pl-waits'))).toBe('4 waiting on a person')
  })

  it('reads the source filter off the address, keeps every column and says what each hides', async () => {
    const only = CARDS.filter((c) => (c.source as Json).kind === 'failure_review')
    const body = board(only, { by_stage: stages(only, { found: 2, planned: 2, running: 1, pr_ci: 1, ended: 1 }), waits_on_person: 4 })
    const { calls, go } = await mount('source=failure_review', routes(body))
    await screen.findByTestId('pl-board', undefined, WAIT)
    // MUTATION: drop the filter from the read.
    const read = calls.find((c) => c.url.startsWith('/v1/pipeline'))!
    expect(new URLSearchParams(read.url.split('?')[1]).get('source')).toBe('failure_review')
    expect(document.querySelectorAll('.pl-col')).toHaveLength(5)
    expect(visible(screen.getByTestId('pl-hidden-found'))).toBe('+2 hidden')
    expect(visible(screen.getByTestId('pl-hidden-ended'))).toBe('+1 hidden')
    const chips = within(screen.getByRole('group', { name: 'Source' }))
    expect(chips.getByRole('button', { name: 'Failure review' }).getAttribute('aria-pressed')).toBe('true')
    // A filter change is an address, so the board can be linked to as filtered.
    fireEvent.click(within(screen.getByRole('group', { name: 'State' })).getByRole('button', { name: 'Waiting' }))
    expect(go).toHaveBeenCalledWith('automate/pipeline?source=failure_review&state=waiting')
    fireEvent.click(chips.getByRole('button', { name: 'Every source' }))
    expect(go).toHaveBeenLastCalledWith('automate/pipeline')
  })

  it('turns a state filter that leaves one column into that column alone', async () => {
    const ended = CARDS.filter((c) => c.stage === 'ended')
    await mount('state=ended', routes(board(ended)))
    await screen.findByTestId('pl-board', undefined, WAIT)
    expect([...document.querySelectorAll<HTMLElement>('.pl-col')].map((c) => c.dataset.stage)).toEqual(['ended'])
    expect(document.querySelector('.pl-cols')!.classList.contains('is-one')).toBe(true)
  })

  it('opens a card at the approval’s own address with the board’s filters, and Back returns to the board with them', async () => {
    await mount('source=failure_review&waits=person', routes(board()))
    await screen.findByTestId('pl-board', undefined, WAIT)
    const link = column('planned').querySelector<HTMLAnchorElement>('[data-card="run:run_old"] a')!
    const href = link.getAttribute('href')!
    // MUTATION: open the item without the board it came from.
    expect(href.split('?')[0]).toBe('/automate/approvals/run%3Arun_old')
    const q = new URLSearchParams(href.split('?')[1])
    expect(q.get('from')).toBe('pipeline')
    expect(q.get('source')).toBe('failure_review')
    expect(q.get('waits')).toBe('person')
    // A merge card opens the merge approval, not the run.
    expect(column('pr_ci').querySelector('a')!.getAttribute('href')!.split('?')[0]).toBe('/automate/approvals/apr_merge')
    // A card with no approval opens its run in Work › Runs.
    expect(column('running').querySelector('a')!.getAttribute('href')).toBe('/runs/run_going')
  })

  it('carries the board’s filters through the item’s path and back (paths.ts)', async () => {
    vi.resetModules()
    const { addressToPath, pathToAddress } = await import('../paths')
    const { approvalAddress, boardOf, pipelineAddress } = await import('../approvalPaths')
    const at = approvalAddress('run:run_old', null, 'source=failure_review&state=waiting')
    const path = addressToPath(at)
    expect(path.split('?')[0]).toBe('/automate/approvals/run%3Arun_old')
    const back = pathToAddress(path.split('?')[0]!, path.split('?')[1] ?? '')!.address
    const view = back.split('?')[1] ?? ''
    // MUTATION: drop the board's keys when the path is read back.
    expect(boardOf(view)).toBe('source=failure_review&state=waiting')
    expect(addressToPath(pipelineAddress(boardOf(view)))).toBe('/automate/pipeline?source=failure_review&state=waiting')
    // An item opened from Approvals has no board.
    expect(boardOf('approval=apr_1&kind=run')).toBeNull()
  })

  it('draws Back to the board, with its filters, on a decision page opened from it', async () => {
    serve((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/approvals/apr_merge' ? { status: 404, body: { detail: 'Not Found' } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ApprovalsScreen } = await import('../Approvals')
    render(<ApprovalsScreen view="approval=apr_merge&from=pipeline&source=failure_review&state=waiting" go={vi.fn()} />)
    const back = await screen.findByRole('link', { name: '‹ Pipeline' }, WAIT)
    // MUTATION: Back to Approvals, or to the board without its filters.
    expect(back.getAttribute('href')).toBe('/automate/pipeline?source=failure_review&state=waiting')
  })

  it('shows a failed read only in the columns that read feeds', async () => {
    const runsOnly = CARDS.filter((c) => c.approval_kind !== 'proposal' && c.approval_kind !== 'merge')
    await mount('', routes(board(runsOnly, { partial: [{ read: 'approvals', status: 503 }] })))
    await screen.findByTestId('pl-board', undefined, WAIT)
    // MUTATION: fail the whole board, or every column.
    for (const s of ['found', 'pr_ci', 'ended']) expect(screen.getByTestId(`pl-error-${s}`)).toBeTruthy()
    for (const s of ['planned', 'running']) expect(screen.queryByTestId(`pl-error-${s}`)).toBeNull()
    expect(cardIds('planned')).toEqual(['run:run_old', 'run:run_young', 'run:run_quiet'])
  })

  it('is one line and its action on a measured empty board', async () => {
    const { go } = await mount('', routes(board([])))
    const empty = await screen.findByTestId('pl-empty', undefined, WAIT)
    expect(visible(empty)).toMatch(/^Nothing on the board\s*Schedules$/)
    expect(document.querySelector('.pl-col')).toBeNull()
    fireEvent.click(within(empty).getByRole('button', { name: 'Schedules' }))
    expect(go).toHaveBeenCalledWith('automate/schedules')
  })

  it('says not served on a 404, with Approvals one press away', async () => {
    const { go } = await mount('', routes(null))
    const ns = await screen.findByTestId('pl-not-served', undefined, WAIT)
    expect(document.querySelector('.pl-col')).toBeNull()
    fireEvent.click(within(ns).getByRole('button', { name: 'Approvals' }))
    expect(go).toHaveBeenCalledWith('automate/approvals')
  })

  it('draws a failed read as a failure, never as an empty board', async () => {
    await mount('', routes({ code: 'internal', message: 'the pipeline read broke' }, 500))
    // MUTATION: turn a failed read into the empty board's line.
    await waitFor(() => expect(document.querySelector('.state.failed')).not.toBeNull(), { timeout: 20000 })
    expect(screen.queryByTestId('pl-empty')).toBeNull()
    expect(screen.queryByTestId('pl-board')).toBeNull()
  })

  it('draws the loading state while the read is in flight, and no board or empty line', async () => {
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
    const { PipelineScreen } = await load()
    render(<PipelineScreen view="" go={vi.fn()} />)
    await screen.findByRole('heading', { name: 'Pipeline', level: 1 }, WAIT)
    await waitFor(() => expect(document.querySelectorAll('.skeleton').length).toBeGreaterThan(0), WAIT)
    expect(screen.queryByTestId('pl-board')).toBeNull()
    expect(screen.queryByTestId('pl-empty')).toBeNull()
  })

  it('shows one stage at a time at 390px, the waiting stage first, and nothing wider than the screen', async () => {
    const noFound = CARDS.filter((c) => c.stage !== 'found')
    await mount('', routes(board(noFound)))
    await screen.findByTestId('pl-board', undefined, WAIT)
    // Hidden on a wide screen, so read off the DOM rather than the accessibility tree.
    const stagesRow = document.querySelector<HTMLElement>('.pl-stages[aria-label="Stage"]')!
    expect(painted(stagesRow, 'display', PHONE)).toBe('flex')
    expect(painted(stagesRow, 'display', WIDE)).toBe('none')
    // MUTATION: show the first column (Found, empty) rather than the waiting one.
    expect(column('planned').classList.contains('is-shown')).toBe(true)
    expect(painted(column('found'), 'display', PHONE)).toBe('none')
    expect(painted(column('planned'), 'display', PHONE)).toBe('grid')
    expect(painted(column('found'), 'display', WIDE)).toBe('grid')
    expect(painted(document.querySelector<HTMLElement>('.pl-cols')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    fireEvent.click([...stagesRow.querySelectorAll('button')].find((b) => /^Merged or ended/.test(b.textContent ?? ''))!)
    expect(column('ended').classList.contains('is-shown')).toBe(true)
    expect(column('planned').classList.contains('is-shown')).toBe(false)
  })
})
