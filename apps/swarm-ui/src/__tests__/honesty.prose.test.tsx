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

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { cascade } from './cssgate'
import type { Result } from '../fetch'
import { HELP } from '../help'
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
// `TASK_PAGE_LIMIT` BESIDE THE READS: Overview names the full page it asks
// for (#168), and a factory mock throws on any export it does not declare.
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

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
  const triggers = [...document.querySelectorAll('button[aria-label^="Help: "]')]
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
 * `<style>` goes too: a CSS rule is not something a reader reads. (Overview
 * injected its stylesheet as a child until U8 folded it into styles.css; the
 * exclusion stays so a screen that injects one again is not counted.)
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

/**
 * `reads` replaces one of the eight after the healthy defaults are set.
 *
 * It exists because a screen that renders eight successful reads passes
 * against a provenance line hard-coded to "everything landed" -- which is the
 * mutation that got past the first draft of the two tests at the foot of this
 * block. The failure cases have to be rendered, not reasoned about.
 */
function renderOverview(
  over: {
    accounts?: Account[]
    spend?: Partial<SpendRollup>
    reads?: Partial<Record<keyof typeof api, Result<unknown>>>
  } = {},
) {
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
  for (const [name, result] of Object.entries(over.reads ?? {})) {
    api[name as keyof typeof api].mockResolvedValue(result)
  }
  return render(<OverviewScreen />)
}

/** The Headroom card's account line, which lands from its own read. */
async function accountLine(): Promise<void> {
  await waitFor(() => expect(document.querySelector('.ov-acc')).not.toBeNull(), WAIT)
}

describe('Overview, with every help card closed', () => {
  /**
   * O1 DRAWS THE ACCOUNT POOL AS ONE LINE, and the rule moves with it: an
   * account nobody polled is NOT usable room -- it is left out of `N of M
   * usable` and named in that figure's accessible name -- while an account
   * measured at 0% is a reading, printed as a digit beside its name.
   *
   * MUTATION: count the never-polled account as usable, or print its
   * headroom as a percentage.
   */
  it('leaves an unpolled account out of the usable figure, and prints a measured 0% as a digit', async () => {
    renderOverview()
    await accountLine()
    expectAllCardsClosed()

    const line = document.querySelector('.ov-acc')!
    const usable = line.querySelector(':scope > span')!
    expect(textOf(usable)).toBe('1 of 2 usable')
    expect(usable.getAttribute('aria-label') ?? '').toMatch(/never has no reading, so its headroom is unknown rather than full/)
    // THE MEASURED ONE. A digit, and the word that says which account it is.
    expect(visibleText()).toMatch(/best fresh at 0% of its five-hour window/)
    // AND THE UNMEASURED ONE NEVER WEARS A FIGURE.
    expect(visibleText()).not.toMatch(/never at \d/)
  })

  // RE-POINTED, NOT WEAKENED. The two sentences this used to read off the
  // surface -- "N attempts carry no cost figure and are counted as unmeasured,
  // not as zero" and the phrase "not reported" in the figure slot -- are gone
  // from the screen. WHERE THEY WENT: the count is now the digit `N unmeasured`
  // in the card's provenance foot, the kind of nothing is the two-word
  // `.ctl-mark.is-absent`, and the full sentence is that mark's accessible name
  // and the `token-cost` topic.
  //
  // WHAT IS PINNED HERE IS A STRICTER CLAIM THAN THE SENTENCE WAS. A paragraph
  // can sit beside a figure it does not describe; these assertions are all on
  // the figure itself -- its text has no digit, it carries `data-measured
  // ="false"`, and the mark naming the absence is inside the same card. That
  // is the property the invariant actually needs and the prose never had.
  it('draws an unreported cost as an em dash and a mark, with no digit anywhere near it', async () => {
    renderOverview({ spend: { costUsd: null, attempts: 3, attemptsWithCost: 0 } })
    // WAITS ON THE SPEND CARD, not on the first `not measured` to appear. The
    // Units-held tile draws one too and its read lands first, so waiting on
    // the word asserted against a card that had not rendered yet -- green in
    // isolation, red under the full suite, which is the worst of both.
    await waitFor(() => expect(document.querySelector('.ov-figure')).not.toBeNull(), WAIT)
    expectAllCardsClosed()

    const figure = document.querySelector('.ov-figure')
    expect(figure, 'the spend card drew no figure slot at all').not.toBeNull()
    // THE EM DASH, AND NOT A DIGIT. `0` here is the lie the whole file exists
    // to stop, and so is `$0.00`.
    expect(figure!.querySelector('.ctl-em'), 'the absent cost is not marked absent').not.toBeNull()
    expect(textOf(figure), 'an absent cost rendered a digit').not.toMatch(/\d/)
    // The figure declares its own status, so nothing has to read the text to
    // know which of the two it is.
    expect(figure!.getAttribute('data-measured')).toBe('false')
    // It also must not wear the figure step: at 30px a dash reads as a
    // quantity, which is why `.ctl-figure.is-absent` drops it to --t-body.
    expect(figure!.className).toContain('is-absent')

    // THE KIND OF NOTHING, AS A WORD, WITHOUT HOVERING ANYTHING. Two words,
    // visible, greyscale-safe, in the same card as the figure.
    const marks = [...document.querySelectorAll('.ctl-mark.is-absent')]
    expect(marks.length, 'no absence mark was drawn beside the figure').toBeGreaterThan(0)
    expect(marks.some((m) => textOf(m) === 'not measured')).toBe(true)

    // AND THE SENTENCE IS STILL REACHABLE -- as an accessible name, which has
    // a keyboard route and survives a screenshot, unlike the `title=` that
    // turned this suite red the last time someone tried this.
    // BOTH the tile and the card carry it, because the strip summarises the
    // cards; the All variant is what keeps this from failing on the screen
    // agreeing with itself.
    expect(
      screen.getAllByLabelText(/no attempt in this sample reported a cost/i).length,
    ).toBeGreaterThan(0)

    // The count of what is missing stays on the surface as a DIGIT.
    expect(visibleText()).toContain('3 unmeasured')
  })

  it('writes a MEASURED zero cost as a digit, which is the other half of the rule', async () => {
    renderOverview({ spend: { costUsd: 0, attempts: 3, attemptsWithCost: 3 } })
    // The tile and the metric strip both carry it, so this waits on the set.
    // `$0.00`: two decimals at every size (#97), and an exact zero is a digit.
    expect((await screen.findAllByText('$0.00', undefined, WAIT)).length).toBeGreaterThan(0)
    expectAllCardsClosed()
    const figure = document.querySelector('.ov-figure')
    expect(textOf(figure)).toBe('$0.00')
    // THE OTHER HALF OF THE RULE, at the same attribute. A measured zero says
    // so on the figure rather than in a sentence under it, and the sentence
    // that used to be there -- "every attempt in the sample carried a cost
    // figure" -- is gone from the surface: what replaced it is the ABSENCE of
    // the `N unmeasured` count, which only appears when there is one.
    expect(figure!.getAttribute('data-measured')).toBe('true')
    expect(figure!.className).not.toContain('is-absent')
    expect(figure!.querySelector('.ctl-em'), 'a measured zero was marked absent').toBeNull()
    const card = figure!.closest('.ctl-card')
    expect(card, 'the figure is not in a card').not.toBeNull()
    expect(
      card!.querySelector('.ctl-mark.is-absent'),
      'a measured zero drew an absence mark',
    ).toBeNull()
    // `N unmeasured` only appears when there IS one, so its absence is the
    // positive claim that every attempt carried a figure.
    expect(textOf(card)).not.toContain('unmeasured')
  })

  /**
   * THE TOKEN COUNTS ARE FOUR FACTS WITH WORDS, AND AN ABSENT ONE IS A DASH.
   *
   * The proportion bar is gone (#97): on a real sample it was 90-95% cache
   * read, `in` drew under a pixel, and the only per-profile split that could
   * replace it is not in the rollup. Without the bar there is nothing for a
   * swatch to key, so the swatches went with it. What is left has to carry
   * the honesty rule alone: every count is labelled in words -- `cache read`
   * and `cache write`, never `c-rd` and `c-wr`, which nothing on the screen
   * explained -- and a count nobody reported is an em dash, not a digit.
   *
   * MUTATION: bring back `c-rd`, print `0` for the unreported count, or
   * restore the bar.
   */
  it('labels each token count in words, and draws an unreported one as a dash', async () => {
    renderOverview({
      spend: {
        attempts: 4,
        attemptsWithTokens: 3,
        inputTokens: 900,
        outputTokens: 100,
        // Never reported by any attempt in the sample.
        cacheReadTokens: null,
        // A MEASURED zero. It is a reading, so it is a digit.
        cacheCreationTokens: 0,
      },
    })
    await waitFor(() => expect(document.querySelector('.ov-spend .ov-kv')).not.toBeNull(), WAIT)
    expectAllCardsClosed()

    const facts = [...document.querySelectorAll('.ov-spend .ov-kv-row')]
    const keyOf = (f: Element) => textOf(f.querySelector('dt'))
    expect(facts.map(keyOf)).toEqual(['input tokens', 'output tokens', 'cache read tokens', 'cache write tokens'])

    const crd = facts.find((f) => keyOf(f) === 'cache read tokens')!
    expect(crd.querySelector('dd .ctl-em'), 'an unreported count drew no em dash').not.toBeNull()
    expect(textOf(crd.querySelector('dd')), 'an unreported count rendered a digit').not.toMatch(/\d/)
    const cwr = facts.find((f) => keyOf(f) === 'cache write tokens')!
    expect(textOf(cwr.querySelector('dd')), 'a measured zero is not a digit').toBe('0')

    const card = document.querySelector('.ov-spend')!
    expect(card.querySelector('.ov-mix'), 'the token-mix bar is back').toBeNull()
    expect(card.querySelector('.ov-swatch'), 'a swatch keys a bar that is not drawn').toBeNull()
    expect(textOf(card)).not.toMatch(/c-rd|c-wr/)
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
    await waitFor(() => expect(visibleText()).toContain('2 of 5 reads failed'), WAIT)
    expectAllCardsClosed()

    // RE-POINTED. The count is the part that must not need a hover and it is
    // still a digit on the surface, in the card's provenance foot. The clause
    // that followed it -- "so their spend is in none of these figures" -- plus
    // the error detail are now that element's accessible name, which is where
    // an explanation is allowed to live and where a screen reader gets it at
    // the figure rather than a paragraph away from it.
    const failed = screen.getByLabelText(/spend is in none of these figures/i)
    expect(textOf(failed)).toBe('2 of 5 reads failed')
    expect(failed.getAttribute('aria-label')).toContain('HTTP 503')
    // ONE MESSAGE IS ONE FAILURE'S. The rollup keeps only the first error it
    // saw, so the rest are named as unexplained rather than explained wrongly.
    expect(failed.getAttribute('aria-label')).toContain('unexplained')
  })

  // RE-POINTED. `.sub` -- the subtitle line under the page title -- is gone
  // from this screen entirely; the owner's directive was that a data view
  // carries no subtitle. The cadence moved into the page head's facts strip,
  // where it is a two-character mono value beside a three-letter key.
  //
  // WHAT IS PINNED IS THE SAME PROPERTY: the figure is INTERPOLATED FROM THE
  // TIMER CONSTANT and not typed out. That is what stopped the words "every 20
  // seconds" sitting three hundred lines from `POLL_MS` and drifting from it.
  // Both spellings are checked -- the visible `20s` and the accessible name
  // that says it in full -- because a constant rendered in one place and a
  // sentence hard-coded in the other is exactly the shape being prevented.
  it('states the cadence from the timer constant rather than in words', async () => {
    renderOverview()
    await accountLine()
    // The canonical page head (#503 Q2) draws its meta and freshness in a
    // `.sub` ON THE TITLE'S ROW (`.c-phead`, one flex row): that is the facts
    // strip, not a subtitle line under the title. Any other `.sub` is one.
    expect(document.querySelector('.sub:not(.c-phead > .sub)'), 'the screen grew a subtitle again').toBeNull()
    expect(document.querySelector('.c-phead > .sub'), 'the facts strip left the page head').not.toBeNull()

    const poll = screen.getByLabelText(/re-read every 20 seconds/i)
    expect(textOf(poll)).toContain('20s')
    // ...and the sentence is not on the surface. `textOf` would read the
    // HelpCard's visually-hidden copy straight back out, which is exactly the
    // trap `visibleText` exists for, so the claim is made against that.
    expect(visibleText()).not.toContain('re-read every')
  })

  // THE READ TALLY. "all 8 reads landed" used to be a clause in the subtitle,
  // and it is the sentence a reader checks to decide whether to trust the rest
  // of the screen -- so it may not be said before it is true. It is now a
  // fraction plus a dot whose SHAPE carries the outcome, with the sentence as
  // the accessible name.
  it('counts what landed as a fraction and a shape, never as a claim', async () => {
    renderOverview()
    await accountLine()
    expectAllCardsClosed()

    const tally = document.querySelector('.ov-tally')
    expect(tally, 'the page head carries no read tally').not.toBeNull()
    // WAITS FOR THE EIGHTH READ. The spend rollup is a fan-out keyed off the
    // task page, so it lands AFTER the accounts row this test woke on -- and
    // a tally read at that instant says 7/8 with the "still asking" ring,
    // which is correct and is not what this test is about. Asserting without
    // waiting made it pass in isolation and fail under the full suite.
    await waitFor(() => expect(textOf(tally)).toContain('8/8'), WAIT)
    // A landed-everything tally is the ok disc. The default `.ctl-dot` -- a
    // hollow ring -- means "still asking", and `is-bad` means a read failed;
    // the three must not be the same picture.
    expect(tally!.querySelector('.ctl-dot.is-ok'), 'the tally drew no outcome').not.toBeNull()
    expect(tally!.getAttribute('aria-label')).toContain('8 of 8 reads landed')
  })

  // THE OTHER HALF, AND THE ONE A MUTATION GOT PAST. The test above renders
  // the case where everything landed, so it passes just as happily against a
  // tally hard-coded to `reads.length/reads.length` -- which is the page
  // vouching for itself while a read is lying on the floor. This renders a
  // read that FAILED and pins the shortfall, the tone and the sentence.
  it('counts a failed read as failed, and never rounds it up to landed', async () => {
    renderOverview({
      reads: {
        loadStats: {
          status: 'error',
          error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' },
        },
      },
    })
    await accountLine()
    expectAllCardsClosed()

    const tally = document.querySelector('.ov-tally')
    // Same wait, same reason: seven of the eight have to land before the one
    // that failed is the only one outstanding.
    await waitFor(
      () => expect(textOf(tally), 'a failed read was counted as landed').toContain('7/8'),
      WAIT,
    )
    // The SHAPE, not only the hue: a diamond is the one mark with corners and
    // it survives the screenshot that a red pixel does not.
    expect(tally!.querySelector('.ctl-dot.is-bad'), 'a failed read drew no mark').not.toBeNull()
    expect(tally!.querySelector('.ctl-dot.is-ok'), 'a failed read drew the healthy mark').toBeNull()
    expect(tally!.getAttribute('aria-label')).toContain('1 of 8 reads failed')
  })

  // THE PARTIAL ALL-CLEAR, WHICH IS THE CASE THE WHOLE LEAD EXISTS FOR.
  //
  // A short row of check cards over blind checks and a short row over clear
  // ones must be different pictures before either is read. O1's lead draws
  // the blind share as a digit on its head line (`1 blind`) with the kit's
  // partial mark beside it, and the all-clear under it is the `partial`
  // absence -- or the `admin` one when every blind check was an admin gate --
  // never the `zero` that a real all-clear wears.
  //
  // MUTATION: draw the all-clear as `zero` whatever ran, or drop the partial
  // mark from the head.
  it('draws a partial all-clear as partial, never as a whole one', async () => {
    // A non-admin genuinely cannot read /v1/leases, so one check goes blind.
    renderOverview({
      reads: {
        loadLeases: {
          status: 'error',
          error: { kind: 'admin_required', httpStatus: 403, code: null, message: 'admin only' },
        },
      },
    })
    await accountLine()
    expectAllCardsClosed()

    const lead = document.querySelector('#ov-needs')!
    const mark = lead.querySelector('.ov-lh .ctl-mark.is-partial')
    expect(mark, 'a lead over a blind check drew no partial mark').not.toBeNull()
    expect(mark!.getAttribute('aria-label')).toMatch(/could not run/i)
    expect(lead.querySelector('.ctl-empty .ctl-mark.is-zero'), 'a partial all-clear drew the real-zero mark').toBeNull()

    // AND IT IS NOT PAINTED AS BREAKAGE. A non-admin's platform is not down.
    const marks = [...document.querySelectorAll('.ctl-mark')].map((m) => textOf(m))
    expect(marks, 'an admin gate was drawn as a failed read').not.toContain('not read')

    // The count of what was never looked at is a digit on the surface.
    expect(visibleText()).toContain('1 blind')
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

    // A1 RE-POINT (#503): the table became a list beside the chosen account.
    // The list item carries the 5h figure; the pane's tiles carry 5h, 7d and
    // Clears for the account it shows. The claims are unchanged.
    const item = chooseAccount('eng:never')
    const onItem = item.querySelector('.acct-window')!
    expect(onItem.classList.contains('acct-unmeasured')).toBe(true)
    expect(textOf(onItem)).toBe('—')

    // The 5h and 7d tiles of the account nobody has polled.
    const unmeasured = [...document.querySelectorAll('.acct-tiles > .acct-window.acct-unmeasured')]
    expect(unmeasured.length, 'the never-polled account drew no unmeasured window').toBe(2)
    for (const cell of unmeasured) {
      expect(textOf(cell.querySelector('.acct-pct'))).toBe('—')
      expect(textOf(cell.querySelector('.acct-pct'))).not.toMatch(/\d/)
      // THE BAR IS THE MARKER. An empty bar and a measured 0% are the same
      // picture, so an unmeasured window draws no bar at all.
      expect(cell.querySelector('.ctl-util-track'), 'an unmeasured window drew a bar').toBeNull()
      expect(cell.querySelector('.acct-bar'), 'an unmeasured window drew the old bar').toBeNull()
    }

    // ...and its Clears, which has no window to count down to, is an em dash
    // rather than a zero or a "now" -- on the tile and on the item.
    const clears = document.querySelector('.acct-tiles > [data-label="Clears"]')!
    expect(clears.classList.contains('acct-unmeasured'), 'the never-polled account drew a countdown').toBe(true)
    expect(textOf(clears.querySelector('b'))).toBe('—')
    expect(textOf(item.querySelector('[data-label="Clears"]'))).toBe('clears —')

    chooseAccount('eng:fresh')
    const measured = [...document.querySelectorAll('.acct-tiles > .acct-window')].filter(
      (td) => !td.classList.contains('acct-unmeasured'),
    )
    expect(measured.length, 'the measured-zero account drew no window tile').toBeGreaterThan(0)
    expect(textOf(measured[0]!.querySelector('.acct-pct'))).toBe('0%')
    // The shared track draws a measured 0% with its baseline tick (§8.7.2).
    const track = measured[0]!.querySelector('.ctl-util-track')
    expect(track, 'a measured window drew no bar').not.toBeNull()
    expect(track!.classList.contains('is-zero'), 'a measured 0% drew no baseline tick').toBe(true)
    expect(track!.querySelector('.ctl-util-zero')).not.toBeNull()
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
    expect(panel!.querySelector('button[aria-label^="Help: "]')).not.toBeNull()
  })

  it('carries the marks the deleted legend used to index', async () => {
    renderAccounts([NEVER_POLLED])
    await screen.findByText('eng:never', undefined, WAIT)
    expectAllCardsClosed()
    // The tilde rule is still stated where the figures are read -- and since
    // #127 it is stated as what is on screen: a never-polled row carries no
    // tilde, so the foot says there is none rather than explaining one.
    expect(document.querySelector('.acct-tilde')).toBeNull()
    expect(textOf(document.querySelector('.provenance'))).toContain('no projected figures')
    // ...and the eleven-paragraph legend is gone from the surface.
    expect(document.querySelector('.section.legend')).toBeNull()
    expect(visibleText()).not.toContain('How to run this pool')
  })

  /**
   * OV-1: ONE POLARITY, AND EVERY % CARRIES ITS WORD. The Overview's headline
   * said `% left` over rows of % used; the owner set % used everywhere
   * (Overview, Accounts, sc). This table already printed used, under column
   * heads that said only `5h` and `7d` -- so the word goes on the head, and on
   * the phone key that stands in for it. A1 (#503) took the heads away; the
   * word is now the tile's own label and every figure's `data-label`.
   *
   * MUTATION: label the tiles `5h` and `7d` again.
   */
  it('says which way every window percentage points, on every figure', async () => {
    renderAccounts([MEASURED_ZERO])
    await screen.findByText('eng:fresh', undefined, WAIT)
    const labels = [...document.querySelectorAll('.acct-tiles > .acct-window > small')].map((t) => textOf(t))
    expect(labels).toEqual(['5h used', '7d used'])
    const keys = [...document.querySelectorAll('.acct-window')].map((td) => td.getAttribute('data-label'))
    expect(keys.length, 'no window figure was drawn').toBeGreaterThan(0)
    for (const key of keys) expect(key, 'a figure drops the polarity').toMatch(/ used$/)
  })
})

// ---------------------------------------------------------------------------
// Accounts: the owner's decisions on #85, 2026-09-25 (CP-25, CP-26). Each
// block was pushed before the change it pins.
// ---------------------------------------------------------------------------

/** A window far enough out that nothing here resets it. */
const FAR = '2099-01-01T00:00:00Z'

/**
 * Choose an account in the list by the id printed under it, and return its
 * list item. The pane then shows that account.
 */
function chooseAccount(id: string): HTMLElement {
  const raw = [...document.querySelectorAll('.acct-li span.raw')].find((s) => s.textContent === id)
  expect(raw, `no list item for ${id}`).toBeTruthy()
  const li = raw!.closest('li') as HTMLElement
  fireEvent.click(li.querySelector('button.acct-open')!)
  return li
}

/**
 * One account's window tile, by its label -- the label carries OV-1's
 * polarity (`5h used`, `7d used`). A1 RE-POINT (#503): the figure lived in a
 * table cell per row; it is now a tile in the pane, so the account is chosen
 * first.
 */
function accountCell(id: string, label: string): HTMLElement {
  chooseAccount(id)
  const cell = document.querySelector(`.acct-tiles > [data-label="${label}"]`)
  expect(cell, `${id} has no ${label} tile`).not.toBeNull()
  return cell as HTMLElement
}

describe('Accounts draws a window with the shared track, not the five-cell bar (CP-25)', () => {
  const now = () => new Date().toISOString()
  const board2 = () => [
    account({
      account_id: 'eng:live',
      label: 'live',
      observed_at: now(),
      windows: {
        // 0.4375 is exact in binary, so the width is exactly 43.75% and the
        // assertion below is about rounding, not about floating point.
        five_hour: { utilization: 0.4375, resets_at: FAR, reset: false },
        seven_day: { utilization: 1, resets_at: FAR, reset: false },
      },
    }),
    account({
      account_id: 'eng:old',
      label: 'old',
      observed_at: now(),
      stale: true,
      windows: {
        five_hour: { utilization: 1, resets_at: FAR, reset: false },
        seven_day: { utilization: 0.3, resets_at: FAR, reset: false },
      },
    }),
  ]

  it('draws the exact percentage in the default grey', async () => {
    renderAccounts(board2())
    await screen.findByText('eng:live', undefined, WAIT)
    expect(document.querySelector('.acct-bar'), 'the five-cell bar is still drawn').toBeNull()
    const fill = accountCell('eng:live', '5h used').querySelector<HTMLElement>('.ctl-util-fill')
    expect(fill, 'a live reading drew no track').not.toBeNull()
    // Unrounded: 43.75, not the nearest fifth (40) and not the figure's 44.
    expect(fill!.style.width).toBe('43.75%')
    // No verdict below a spent window: there is no amber band on Accounts.
    expect(fill!.className.trim()).toBe('ctl-util-fill')
  })

  it('draws bad only for a live window that is fully spent', async () => {
    renderAccounts(board2())
    await screen.findByText('eng:live', undefined, WAIT)
    expect(accountCell('eng:live', '7d used').querySelector('.ctl-util-fill.is-bad')).not.toBeNull()
    // Spent, but the reading is stale: projected, never the verdict.
    const old = accountCell('eng:old', '5h used')
    expect(old.querySelector('.ctl-util-fill.is-bad'), 'a stale reading drew the spent verdict').toBeNull()
    expect(old.querySelector('.ctl-util-fill.ov-projected')).not.toBeNull()
    expect(old.querySelector('.acct-tilde')?.textContent).toBe('~')
    expect(accountCell('eng:old', '7d used').querySelector('.ctl-util-fill.ov-projected')).not.toBeNull()
  })

  it('keeps the track a fixed 40px inline beside the figure, and hides it at 560px and below', async () => {
    renderAccounts(board2())
    await screen.findByText('eng:live', undefined, WAIT)
    const track = accountCell('eng:live', '5h used').querySelector('.acct-window > .ctl-util-track')
    expect(track, 'the track is not the window cell’s own child').not.toBeNull()
    const won = (prop: string, width: number) => {
      const r = cascade(STYLES, track!, prop, { width })
      expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
      return r.winner?.value ?? null
    }
    expect(won('width', 1440)).toBe('40px')
    expect(won('display', 1440)).toBe('inline-flex')
    // The phone block's own rule governs, as it does every other track.
    expect(won('display', 390)).toBe('none')
  })
})

describe('the Accounts foot says how old the readings are (CP-26)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /**
   * Renders the screen under a fake clock, one row per reading age in
   * SECONDS (null: never read), and returns the foot's text.
   */
  async function footFor(ages: (number | null)[]): Promise<{ foot: () => string; advance: (ms: number) => Promise<void> }> {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    const t = Date.now()
    const rows = ages.map((s, i) =>
      s === null
        ? account({ account_id: `eng:n${i}`, label: `n${i}`, observed_at: null })
        : account({
            account_id: `eng:r${i}`,
            label: `r${i}`,
            observed_at: new Date(t - s * 1000).toISOString(),
            windows: { five_hour: { utilization: 0.1, resets_at: FAR, reset: false } },
          }),
    )
    api.loadAccountsBoard.mockResolvedValue(ok(board(rows)))
    const { container } = render(<AccountsScreen />)
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    await advance(0)
    return { foot: () => textOf(container.querySelector('.provenance')), advance }
  }

  it('prints the range after the row count, leaving out a row with no reading', async () => {
    const { foot } = await footFor([2 * 60, 14 * 60, null])
    // Every reading here is current, so no `~` is drawn and the legend says
    // so (#127).
    expect(foot()).toMatch(/^3 rows · readings 2m–14m old · no projected figures/)
  })

  it('moves the range on its own clock, without a reload', async () => {
    const { foot, advance } = await footFor([20, 40])
    expect(foot()).toContain('readings 20s–40s old')
    // Ten seconds later, with nothing re-read, the range is ten seconds older.
    // The table's per-render clock would still say 20s-40s: Accounts does not
    // re-render on its own.
    const calls = api.loadAccountsBoard.mock.calls.length
    await advance(10_000)
    expect(foot()).toContain('readings 30s–50s old')
    expect(api.loadAccountsBoard.mock.calls.length, 'the range moved because the board was re-read').toBe(calls)
  })

  it('prints one age when every reading agrees', async () => {
    const { foot } = await footFor([4 * 60, 4 * 60])
    expect(foot()).toContain('readings 4m old')
    expect(foot()).not.toMatch(/readings \S+–/)
  })

  it('drops the clause when no row has a reading', async () => {
    const { foot } = await footFor([null])
    expect(foot()).toContain('1 row')
    expect(foot()).not.toContain('readings')
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
    // `#help/absent-vs-zero` on the `?` after the same card's heading (AH-24).
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
    expect(card!.querySelector('button[aria-label^="Help: "]')).not.toBeNull()
    // AFTER THE HEADING, NEVER AFTER A VALUE (AH-24). The glyph trailed the
    // server's own words, inside the one-line `.rt-unread-detail` that clips
    // with an ellipsis -- after a value, and cut off with it when the message
    // ran long. #161's first version moved it to LEAD the row, which is not
    // after a label either. It goes after the card's heading, `Backends`, in
    // the slot Pools' `Headroom ?` uses, and only while the counters are
    // unread. MUTATION: put it back in `.rt-unread`, at either end.
    const heading = card!.querySelector('h2.ctl-card-title')
    expect(heading, 'the backends card has no heading').not.toBeNull()
    expect(heading!.firstChild?.textContent, 'the heading does not start with its word').toBe('Backends')
    expect(
      heading!.querySelector('button[aria-label^="Help: "]'),
      'the `?` does not follow the Backends heading',
    ).not.toBeNull()
    expect(card!.querySelector('.rt-unread button'), 'the `?` is still in the unread row').toBeNull()

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

  // #126. The screen's one always-present `?` opened `catalogue-from-route`,
  // which the footer index already carries; what a reader of a card cannot
  // work out from it is what `Sets it apart` is measured against and what the
  // `disabled` chip means. The `?` goes to that topic, still after a label.
  it('points its one `?` at what "Sets it apart" and "disabled" mean, after a label, and keeps catalogue-from-route in the footer', async () => {
    renderRuntimes()
    await screen.findAllByText('claude-code', undefined, WAIT)
    const eyebrow = document.querySelector('.rt-eyebrow')
    expect(eyebrow, 'the catalogue label is gone').not.toBeNull()
    const glyph = eyebrow!.querySelector('button[aria-label^="Help: "]')
    expect(glyph, 'the always-present `?` does not follow a label').not.toBeNull()
    expect(glyph!.getAttribute('aria-label')).toBe(`Help: ${HELP['what-sets-it-apart-is-arithmetic'].title}`)
    expect(eyebrow!.firstChild?.textContent, 'the `?` leads its label').not.toBe('')
    // The topic explains both words the card draws.
    const topic = HELP['what-sets-it-apart-is-arithmetic']
    expect(topic.short).toMatch(/rest of the catalogue|the others/)
    expect(topic.short).toMatch(/[Dd]isabled/)
    // catalogue-from-route stays reachable, from the footer index.
    const footer = [...document.querySelectorAll('a[href^="#help/"]')].map((a) => a.getAttribute('href'))
    expect(footer).toContain('#help/catalogue-from-route')
  })

  it('makes "Pools" in the Backends caption a link to the pool board', async () => {
    renderRuntimes()
    await screen.findAllByText('claude-code', undefined, WAIT)
    const caption = document.querySelector('.rt-backends caption')
    expect(caption, 'the Backends table has no caption').not.toBeNull()
    const link = caption!.querySelector('a.ctl-link')
    expect(link, '"Pools" in the caption is plain text').not.toBeNull()
    expect(link!.getAttribute('href')).toBe('#capacity/pools')
    expect(link!.textContent).toBe('Pools')
  })

  it('holds only the disabled chip in the card head: no resolved-backend note', async () => {
    renderRuntimes({
      runtimes: {
        'claude-code': runtime({}),
        codex: runtime({ name: 'codex', available: false, disabled_reason: 'switched off. Use claude-code.' }),
      },
    })
    await screen.findAllByText('codex', undefined, WAIT)
    const heads = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')]
    expect(heads).toHaveLength(2)
    for (const head of heads) {
      expect(head.querySelector('.ctl-card-note'), 'the head still repeats the backend the card lists under `runs on`').toBeNull()
    }
    const off = heads.find((h) => h.textContent?.includes('codex'))!
    expect(off.querySelector('.ctl-chip.is-bad')?.textContent).toBe('disabled')
    const on = heads.find((h) => h.textContent?.includes('claude-code'))!
    expect(on.querySelector('.ctl-chip'), 'an available runtime carries a chip').toBeNull()
  })
})
