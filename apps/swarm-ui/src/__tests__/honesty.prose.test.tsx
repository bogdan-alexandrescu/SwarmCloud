// THE ACCEPTANCE TEST FOR THE PROSE MIGRATION.
//
// The owner directive is that the app carries no prose: help lives in a Help
// section and a `?` sits where an explanation used to. The risk of executing
// that is not aesthetic. This UI's best property -- that a figure nobody
// measured never renders as a zero -- was IMPLEMENTED AS PROSE in several
// places, so a careless migration deletes the honesty rule rather than moving
// it, and the screen still looks tidier afterwards.
//
// So this file asks one question of every screen the migration touched:
//
//   WITH EVERY HELP CARD CLOSED, can a reader still tell that a number is
//   MISSING rather than ZERO?
//
// Every test below therefore renders the real screen and NEVER opens a card.
// `expectAllCardsClosed` asserts that, so a test cannot accidentally pass by
// reading text out of an open popup.
//
// IT RENDERS RATHER THAN GREPS, and that is the whole point of the file. A
// previous verifier in this repository neutered a guard with `false &&` and
// the suite stayed green, because the test only checked that a string appeared
// in the SOURCE. `vitest.config.ts` sets `css: true`, so a class this asserts
// on is a class the shipped stylesheet actually carries.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AccountsBoard, RuntimeTopology, SpendRollup } from '../api'
import type {
  Account,
  AccountsPage,
  Capacity,
  Pool,
  ResourceClassSpec,
  Runtime,
  Stats,
  TaskPage,
} from '../types'

// ---------------------------------------------------------------------------
// The api module, replaced wholesale
// ---------------------------------------------------------------------------
//
// The screens under test are the real ones; only their reads are stubbed,
// because the states that matter here are Results and driving them through
// HTTP would test `read` again rather than the screen.

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
  loadRuntimeTopology: vi.fn(),
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const { OverviewScreen } = await import('../Overview')
const { AccountsScreen } = await import('../Accounts')
const { RuntimesScreen } = await import('../Runtimes')

// ---------------------------------------------------------------------------
// Shared assertions
// ---------------------------------------------------------------------------

/**
 * NO CARD IS OPEN. `HelpCardView` renders the card only when `state.open`, with
 * `role="tooltip"` or `role="dialog"`, so their absence is the proof that
 * nothing below was read out of a popup.
 *
 * It also asserts that at least one `?` EXISTS, because a screen with no cards
 * at all would satisfy "no card is open" vacuously -- and that screen is one
 * where the explanation was deleted rather than moved.
 */
function expectAllCardsClosed(): void {
  const triggers = [...document.querySelectorAll('button[aria-label^="What "]')]
  expect(triggers.length, 'this screen carries no ? at all').toBeGreaterThan(0)
  for (const t of triggers) {
    expect(t.getAttribute('aria-expanded'), 'a card is open before anything was clicked').toBe(
      'false',
    )
  }
  expect(document.querySelectorAll('[role="tooltip"], [role="dialog"]').length).toBe(0)
}

/**
 * `findBy*` waits 1s by default, and these tests render entire screens with
 * several reads each. Measured under the full suite, three of them landed
 * between 1.1s and 1.6s -- so the default made them report the machine's load
 * rather than the product, which is the worst kind of red. Five seconds is
 * long enough that only a genuine failure to render reaches it.
 */
const WAIT = { timeout: 5000 } as const

/** The rendered text of an element, with whitespace collapsed. */
function textOf(el: Element | null | undefined): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/**
 * What a reader can actually SEE, which is not the body's whole `textContent`.
 *
 * Every `<HelpCard>` renders its short text into a visually hidden node so
 * that assistive technology gets the explanation at the label without opening
 * anything. That node is in `textContent` whether the card is open or shut --
 * so a test asserting on the raw body text would read the whole moved
 * explanation straight back out of it and report that nothing had moved, or
 * that a marker was present when only its help text was. Both directions are
 * wrong and neither is visible in a diff.
 *
 * `<style>` goes too: `Overview.tsx` injects its stylesheet as a child, and a
 * CSS rule is not something a reader reads.
 */
function visibleText(): string {
  const clone = document.body.cloneNode(true) as HTMLElement
  for (const el of [
    ...clone.querySelectorAll('[data-help-description], style, script'),
  ]) {
    el.remove()
  }
  return (clone.textContent ?? '').replace(/\s+/g, ' ').trim()
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
}

function account(over: Partial<Account>): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

/** An account nobody has ever polled. Its utilisation is UNKNOWN. */
const NEVER_POLLED = account({
  account_id: 'eng:never',
  label: 'never',
  observed_at: null,
  windows: {},
})

/** An account polled a moment ago whose windows really are empty. A real 0%. */
const MEASURED_ZERO = account({
  account_id: 'eng:fresh',
  label: 'fresh',
  observed_at: new Date().toISOString(),
  stale: false,
  windows: {
    five_hour: { utilization: 0, resets_at: '2099-01-01T00:00:00Z', reset: false },
    seven_day: { utilization: 0, resets_at: '2099-01-01T00:00:00Z', reset: false },
  },
})

function accountsPage(accounts: Account[]): AccountsPage {
  return {
    accounts,
    tenant_id: 'eng',
    unreadable_documents: [],
    unreadable_document_count: 0,
  }
}

function board(accounts: Account[]): AccountsBoard {
  return {
    page: accountsPage(accounts),
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  }
}

function spend(over: Partial<SpendRollup>): SpendRollup {
  return {
    tenantId: 'eng',
    tasksOnPage: 4,
    tasksWithAttempts: 4,
    tasksSampled: 4,
    failedReads: 0,
    failedDetail: null,
    attempts: 4,
    attemptsWithCost: 0,
    attemptsWithTokens: 0,
    costUsd: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
    from: '2026-09-23T09:00:00Z',
    to: '2026-09-23T10:00:00Z',
    ...over,
  }
}

const EMPTY_TASKS: TaskPage = { tasks: [], next_page_token: null }

const EMPTY_STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-09-23T10:00:00Z',
}

const CAPACITY: Capacity = {
  pools: [],
  runner_profiles: {},
  tenant_id: 'eng',
  generated_at: '2026-09-23T10:00:00Z',
}

function size(over: Partial<ResourceClassSpec> = {}): ResourceClassSpec {
  return { name: 'standard', cpu: 2, memory_gib: 4, disk_gib: 1, units: 1, ...over }
}

function runtime(over: Partial<Runtime>): Runtime {
  return {
    name: 'claude-code',
    image: 'ghcr.io/example/agent:1',
    backend: 'AUTO',
    resolved_backend: 'cloudrun',
    provider: 'anthropic',
    secrets: ['CLAUDE_CODE_OAUTH_TOKEN'],
    secrets_any_of: false,
    timeout_seconds: 3600,
    resource_class: 'standard',
    resources: size(),
    // Required since codex was disabled: `available`/`disabled_reason` are no
    // longer optional on Runtime, and a Partial<Runtime> spread cannot satisfy
    // a required field. Defaulted to the available case -- claude-code is the
    // profile this fixture stands for, and a fixture that defaulted to
    // unavailable would quietly exercise the refusal path in every test that
    // did not ask for it.
    available: true,
    disabled_reason: '',
    ...over,
  }
}

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'backend:cloudrun',
    hard_limit: 8,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 8,
    active: 0,
    available: 8,
    enabled: true,
    updated_at: '2026-09-23T10:00:00Z',
    ...over,
  }
}

function topology(over: Partial<RuntimeTopology>): RuntimeTopology {
  return {
    runtimes: { 'claude-code': runtime({}) },
    pools: [pool({})],
    poolsDetail: null,
    classes: { standard: size() },
    classesDetail: null,
    ...over,
  }
}

// ---------------------------------------------------------------------------
// Overview -- the default route, and the screen the owner linked
// ---------------------------------------------------------------------------

function renderOverview(over: { accounts?: Account[]; spend?: Partial<SpendRollup> } = {}) {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(ok(EMPTY_TASKS))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(EMPTY_STATS))
  api.loadAccountPool.mockResolvedValue(
    ok({ ...accountsPage(over.accounts ?? [NEVER_POLLED, MEASURED_ZERO]) }),
  )
  api.loadSpend.mockResolvedValue(ok(spend(over.spend ?? {})))
  return render(<OverviewScreen />)
}

describe('Overview, with every help card closed', () => {
  it('draws an unpolled account as an em dash on a hatched track and a measured 0% as a digit', async () => {
    renderOverview()
    // The rows arrive from a promise, so wait for the one that must be there.
    expect(await screen.findByText('never', undefined, WAIT)).toBeTruthy()
    expectAllCardsClosed()

    const rows = [...document.querySelectorAll('.ctl-util')]
    const unpolled = rows.find((r) => textOf(r.querySelector('.ctl-util-name')).startsWith('never'))
    const measured = rows.find((r) => textOf(r.querySelector('.ctl-util-name')).startsWith('fresh'))
    expect(unpolled, 'the never-polled account is not on the panel').toBeTruthy()
    expect(measured, 'the measured-zero account is not on the panel').toBeTruthy()

    // THE ABSENT ONE. A phrase, not a quantity, and NO fill on the track.
    const unpolledFigure = textOf(unpolled!.querySelector('.ctl-util-figure'))
    expect(unpolledFigure).toBe('—')
    expect(unpolledFigure).not.toMatch(/\d/)
    expect(unpolled!.querySelector('.ctl-util-track')?.className).toContain('is-unknown')
    expect(unpolled!.querySelector('.ctl-util-fill')).toBeNull()

    // THE MEASURED ONE. A digit, on a track that is drawn.
    expect(textOf(measured!.querySelector('.ctl-util-figure'))).toBe('0%')
    expect(measured!.querySelector('.ctl-util-track')?.className).not.toContain('is-unknown')

    // AND THE TWO ARE NOT THE SAME PICTURE. This is the assertion the whole
    // migration is answerable to: exactly one of the two tracks is hatched.
    expect(document.querySelectorAll('.ctl-util-track.is-unknown').length).toBe(1)
  })

  it('writes an unreported cost as a phrase and a measured zero cost as a figure', async () => {
    renderOverview({ spend: { costUsd: null, attempts: 3, attemptsWithCost: 0 } })
    expect(await screen.findByText('not reported', undefined, WAIT)).toBeTruthy()
    expectAllCardsClosed()

    const figure = [...document.querySelectorAll('.ov-figure')][0]
    expect(textOf(figure)).toBe('not reported')
    expect(textOf(figure), 'an absent cost rendered a digit').not.toMatch(/\d/)
    // The count of what is missing stays on the surface beside it.
    expect(visibleText()).toContain('no cost figure')
    expect(visibleText()).toContain('unmeasured, not as zero')
  })

  it('writes a MEASURED zero cost as a digit, which is the other half of the rule', async () => {
    renderOverview({ spend: { costUsd: 0, attempts: 3, attemptsWithCost: 3 } })
    // The tile and the metric strip both carry it, so this waits on the set.
    expect((await screen.findAllByText('$0.0000', undefined, WAIT)).length).toBeGreaterThan(0)
    expectAllCardsClosed()
    expect(textOf(document.querySelector('.ov-figure'))).toBe('$0.0000')
    expect(visibleText()).not.toContain('not reported')
    expect(visibleText()).toContain('every attempt in the sample carried a cost figure')
  })

  it('keeps the count of failed attempt reads on the surface, not behind the ?', async () => {
    renderOverview({
      spend: { failedReads: 2, tasksSampled: 5, failedDetail: 'HTTP 503', costUsd: 1.5 },
    })
    // WAITS ON THE SPEND PANEL, not on the accounts one. They are separate
    // reads, and the accounts row arriving says nothing about whether the
    // rollup has -- which is how this assertion first read "reading…".
    //
    // The sentence is assembled from several text nodes (`{n}` of `{n}`), so
    // it is read off the rendered page rather than matched as one string.
    await waitFor(
      () => expect(visibleText()).toContain('2 of 5 attempt reads failed'),
      WAIT,
    )
    expectAllCardsClosed()
    expect(visibleText()).toContain('spend is in none of these figures')
  })

  it('states the cadence from the timer constant rather than in words', async () => {
    renderOverview()
    await screen.findByText('never', undefined, WAIT)
    expect(textOf(document.querySelector('.sub'))).toContain('re-read every 20s')
  })
})

// ---------------------------------------------------------------------------
// Accounts -- the file the essay and the eleven-entry legend came out of
// ---------------------------------------------------------------------------

function renderAccounts(accounts: Account[]) {
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  return render(<AccountsScreen />)
}

describe('Accounts, with every help card closed', () => {
  it('draws an unmeasured window as an em dash with NO bar, and a measured 0% with one', async () => {
    renderAccounts([NEVER_POLLED, MEASURED_ZERO])
    expect(await screen.findByText('eng:never', undefined, WAIT)).toBeTruthy()
    expectAllCardsClosed()

    // The 5H and 7D columns of the row nobody has polled.
    const unmeasured = [...document.querySelectorAll('td.acct-window.acct-unmeasured')]
    expect(unmeasured.length, 'the never-polled row drew no unmeasured window').toBe(2)
    for (const cell of unmeasured) {
      expect(textOf(cell.querySelector('.acct-pct'))).toBe('—')
      expect(textOf(cell)).not.toMatch(/\d/)
      // THE BAR IS THE MARKER. An empty five-cell bar and a measured 0% are
      // the same picture, so an unmeasured cell draws no bar at all.
      expect(cell.querySelector('.acct-bar'), 'an unmeasured cell drew a bar').toBeNull()
    }

    // ...and its CLEARS cell, which has no window to count down to, is an em
    // dash rather than a zero or a "now".
    const clears = [...document.querySelectorAll('td.acct-unmeasured')].filter(
      (td) => !td.classList.contains('acct-window'),
    )
    expect(clears.length, 'the never-polled row drew a countdown').toBe(1)
    expect(textOf(clears[0])).toBe('—')

    const measured = [...document.querySelectorAll('td.acct-window')].filter(
      (td) => !td.classList.contains('acct-unmeasured'),
    )
    expect(measured.length, 'the measured-zero row drew no window cell').toBeGreaterThan(0)
    expect(textOf(measured[0]!.querySelector('.acct-pct'))).toBe('0%')
    expect(measured[0]!.querySelector('.acct-bar'), 'a measured cell drew no bar').not.toBeNull()
  })

  it('marks the empty pool as a real zero rather than leaving an empty table', async () => {
    // B4.5 RE-POINT. WHAT MOVED: the sentence "The read succeeded and returned
    // nothing -- a real zero, not a failed query" is gone. WHERE THE WORDS
    // LIVE NOW: `.ctl-mark.is-zero` renders the two-word phrase `real zero`
    // from §6.10's fixed six-word vocabulary, and what a zero here COSTS --
    // that work parks rather than fails -- is the topic
    // `park-on-missing-credential`, on the `?` in the heading.
    //
    // WHY THIS IS THE STRONGER ASSERTION. The old one searched the WHOLE
    // screen's text for a substring, so it passed if that sentence appeared
    // anywhere at all -- including, on this codebase's house style, inside a
    // comment that got rendered, or beside a different absence. This one pins
    // the mark to the empty state it describes, pins the modifier that says
    // WHICH kind of nothing it is, and pins that no digit is drawn: a real
    // zero and a failed read must not converge, and `.is-zero` versus
    // `.is-unread` is the distinction the class carries.
    renderAccounts([])
    const heading = await screen.findByText('No accounts registered', undefined, WAIT)
    expectAllCardsClosed()

    const panel = heading.closest('.state')
    expect(panel, 'the empty state is not a panel').not.toBeNull()
    const mark = panel!.querySelector('.ctl-mark')
    expect(mark, 'the empty pool carries no absence mark').not.toBeNull()
    expect(mark!.className).toContain('is-zero')
    expect(mark!.className).not.toContain('is-unread')
    expect(textOf(mark)).toBe('real zero')
    // A measured zero may print a 0; it may never print a figure that was not
    // measured. Nothing here counted anything, so nothing here is a digit.
    expect(textOf(panel)).not.toMatch(/\d/)
    // The route to the sentence is focusable and in the heading.
    expect(panel!.querySelector('button[aria-label^="What "]')).not.toBeNull()
  })

  it('carries the marks the deleted legend used to index', async () => {
    renderAccounts([NEVER_POLLED])
    await screen.findByText('eng:never', undefined, WAIT)
    expectAllCardsClosed()
    // The tilde rule is still stated where the figures are read...
    expect(textOf(document.querySelector('.provenance'))).toContain('~ is projected, not measured')
    // ...and the eleven-paragraph legend is gone from the surface.
    expect(document.querySelector('.section.legend')).toBeNull()
    expect(visibleText()).not.toContain('How to run this pool')
  })
})

// ---------------------------------------------------------------------------
// Runtimes -- five legend entries and two lead paragraphs
// ---------------------------------------------------------------------------

function renderRuntimes(over: Partial<RuntimeTopology> = {}) {
  api.loadRuntimeTopology.mockResolvedValue(ok(topology(over)))
  return render(<RuntimesScreen />)
}

describe('Runtimes, with every help card closed', () => {
  it('draws unread pool counters as dashes and says so, rather than as zeros', async () => {
    // B4.5 RE-POINT. WHAT MOVED: the banner heading "Pool counters could not
    // be read" and its paragraph ending "Those columns are dashes, not zeros."
    // WHERE THE WORDS LIVE NOW: `.ctl-mark.is-unread` renders `not read`
    // INSIDE the card that holds the affected columns, the server's own detail
    // sits beside it in `.rt-unread-detail`, and the argument is
    // `#help/absent-vs-zero` on the `?` in that same row.
    //
    // WHY THIS IS THE STRONGER ASSERTION. "Those columns are dashes, not
    // zeros" is a sentence ABOUT the cells, and a banner can be scrolled away
    // from the cells while they stay on screen -- a reader who lands on row
    // nine of a backend table has the claim nowhere in view. So the test no
    // longer looks for the sentence anywhere on the page. It pins the mark to
    // the card that contains the table, and then pins the thing the sentence
    // was asserting: every unread cell is an em dash, carries `.ctl-em`, and
    // contains NO DIGIT. That last check is the whole invariant and it is
    // unchanged.
    renderRuntimes({ pools: null, poolsDetail: 'the capacity read did not complete.' })
    // `findAllBy`, because the mark is drawn TWICE on purpose: once on the
    // card, for the columns as a whole, and once per affected row's status
    // cell. A reader scrolled past the card head still has it in view.
    expect((await screen.findAllByText('not read', undefined, WAIT)).length).toBeGreaterThan(0)
    expectAllCardsClosed()

    // The mark is in the same card as the columns it is about.
    const card = document.querySelector('.rt-backends')
    expect(card, 'no backends card was drawn').not.toBeNull()
    const mark = card!.querySelector('.ctl-mark')
    expect(mark, 'the unread counters carry no mark').not.toBeNull()
    expect(mark!.className).toContain('is-unread')
    expect(textOf(mark)).toBe('not read')
    // A failed read is NOT an absence of data and NOT a measured zero: the
    // three marks must not converge.
    expect(mark!.className).not.toContain('is-zero')
    expect(mark!.className).not.toContain('is-absent')
    // The server's own reason is on the surface beside it, unhovered.
    expect(textOf(card!.querySelector('.rt-unread-detail'))).toContain(
      'the capacity read did not complete',
    )
    expect(card!.querySelector('button[aria-label^="What "]')).not.toBeNull()

    const row = card!.querySelector('.ctl-table tbody tr')
    expect(row, 'no backend row was drawn').not.toBeNull()
    const cells = [...row!.querySelectorAll('td.is-num')]
    // The first is the number of runtimes -- a measured count. The three after
    // it are the pool figures, and none of them may be a digit.
    expect(textOf(cells[0])).toBe('1')
    for (const cell of cells.slice(1)) {
      expect(textOf(cell)).toBe('—')
      expect(cell.querySelector('.ctl-em'), 'an unread cell is not marked absent').not.toBeNull()
    }
    // The row itself is marked too, not only the card: the status cell of a
    // backend whose counters are unread carries the same mark, so a reader
    // scanning rows rather than headers still cannot read the dash as a zero.
    expect(textOf(row!.querySelector('.ctl-mark.is-unread'))).toBe('not read')
  })

  it('draws a MEASURED zero in the same columns as a digit', async () => {
    renderRuntimes({ pools: [pool({ active: 0, effective_limit: 0, available: 0 })] })
    expect((await screen.findAllByText('claude-code', undefined, WAIT)).length).toBeGreaterThan(0)
    expectAllCardsClosed()

    // B4.5 RE-POINT -- SELECTOR ONLY. `table.pools` became `.ctl-table`, the
    // design system's one table (§6.7), and `td.n` became `td.is-num`. The
    // claim is untouched and is the other half of the test above: a MEASURED
    // zero is a digit, in the same columns where an unread one is an em dash,
    // and the two are told apart by which of them draws `.ctl-em`. Asserting
    // either one alone keeps the rule by accident.
    const cells = [...document.querySelectorAll('.ctl-table tbody tr td.is-num')]
    expect(textOf(cells[1])).toBe('0')
    expect(textOf(cells[2])).toBe('0')
    expect(textOf(cells[3])).toBe('0')
    expect(document.querySelectorAll('.ctl-table .ctl-em').length).toBe(0)
    // A ceiling of zero admits nothing however empty it looks, and that is a
    // measured fact rather than an absence.
    expect(screen.getByText('admits nothing')).toBeTruthy()
  })

  it('keeps the legend titles as links after the paragraphs moved', async () => {
    renderRuntimes()
    await screen.findAllByText('claude-code', undefined, WAIT)
    expectAllCardsClosed()
    expect(document.querySelector('.section.legend')).toBeNull()
    const links = [...document.querySelectorAll('a[href^="#help/"]')].map((a) =>
      a.getAttribute('href'),
    )
    expect(links).toContain('#help/catalogue-from-route')
    expect(links).toContain('#help/units-not-agents')
    expect(links).toContain('#help/workspace-memory')
  })
})
