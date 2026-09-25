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

    // THE MEASURED ONE. A digit, on a track that is drawn -- and, since OV-1,
    // the word that says which way the percentage points.
    expect(textOf(measured!.querySelector('.ctl-util-figure'))).toBe('0% used')
    expect(measured!.querySelector('.ctl-util-track')?.className).not.toContain('is-unknown')

    // AND THE TWO ARE NOT THE SAME PICTURE. This is the assertion the whole
    // migration is answerable to: exactly one of the two tracks is hatched.
    expect(document.querySelectorAll('.ctl-util-track.is-unknown').length).toBe(1)
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
    expect((await screen.findAllByText('$0.0000', undefined, WAIT)).length).toBeGreaterThan(0)
    expectAllCardsClosed()
    const figure = document.querySelector('.ov-figure')
    expect(textOf(figure)).toBe('$0.0000')
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
   * THE LEGEND IS NEW AND IT CARRIES AN ABSENCE, so it gets pinned like one.
   *
   * design-system.md sec 1.6: the five series sit in a band 1.36:1 from end to
   * end and are NOT separable in greyscale, so a multi-series chart names every
   * series beside its value and never relies on the segment's colour to say
   * which segment it is. sec 11.3 listed Overview's spend bar -- "four
   * saturated hues in one 8px rule with the legend on the line below carrying
   * no swatch" -- as unfixed, with two allowed answers; this is the one that
   * keeps the proportion.
   *
   * What makes it an HONESTY assertion rather than a decoration one: a series
   * that reported nothing draws no segment on the bar, so a solid key beside it
   * would index a colour that is not there -- an absence drawn as a
   * measurement. The hollow swatch is the encoding for that, and this test
   * pins BOTH halves, because a rule only one half of which is checked is the
   * mutation that survives.
   */
  it('keys each token series to its own swatch, and draws no solid key for a series nobody reported', async () => {
    renderOverview({
      spend: {
        attempts: 4,
        attemptsWithTokens: 3,
        inputTokens: 900,
        outputTokens: 100,
        // Never reported by any attempt in the sample. No segment on the bar.
        cacheReadTokens: null,
        // A MEASURED zero. It is a reading, so it keeps its identity key.
        cacheCreationTokens: 0,
      },
    })
    await waitFor(() => expect(document.querySelector('.ov-mix')).not.toBeNull(), WAIT)
    expectAllCardsClosed()

    const facts = [...document.querySelectorAll('.ov-mix-facts .ctl-fact')]
    expect(facts.length, 'the legend lost a series').toBe(4)

    // EVERY series is keyed, so the strip cannot reflow when a count arrives
    // and no row is left indexing the bar by position alone.
    for (const f of facts) {
      expect(f.querySelector('.ov-swatch'), `no swatch beside ${textOf(f)}`).not.toBeNull()
    }

    const swatchFor = (key: string) =>
      facts.find((f) => textOf(f.querySelector('b')) === key)!.querySelector('.ov-swatch')!

    // A drawn segment gets the SAME .ov-sN class its segment carries, so the
    // key and the bar cannot drift apart.
    expect(swatchFor('in').className).toBe('ov-swatch ov-s1')
    expect(swatchFor('out').className).toBe('ov-swatch ov-s2')
    // A measured zero is a reading: solid key, and a digit beside it.
    expect(swatchFor('c-wr').className).toBe('ov-swatch ov-s4')

    // THE ABSENCE. No hue, and the em dash rather than a digit -- the same
    // pairing this file pins on the cost figure, at the legend's scale.
    const absent = swatchFor('c-rd')
    expect(absent.className, 'an unreported series was keyed to a colour').toBe(
      'ov-swatch is-absent',
    )
    const crd = facts.find((f) => textOf(f.querySelector('b')) === 'c-rd')!
    expect(crd.className).toContain('is-absent')
    expect(crd.querySelector('.ctl-em'), 'an unreported series drew no em dash').not.toBeNull()
    expect(textOf(crd), 'an unreported series rendered a digit').not.toMatch(/\d/)

    // THE SWATCHES SAY NOTHING A SCREEN READER NEEDS. The bar's own label
    // already names every series and its value, so a second reading of the
    // same four facts is noise, and an <i> with no text has nothing to say.
    for (const f of facts) {
      expect(f.querySelector('.ov-swatch')!.getAttribute('aria-hidden')).toBe('true')
    }
    expect(
      document.querySelector('.ov-mix')!.getAttribute('aria-label'),
    ).toContain('c-rd not measured')
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
    await screen.findByText('never', undefined, WAIT)
    expect(document.querySelector('.sub'), 'the screen grew a subtitle again').toBeNull()

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
    await screen.findByText('never', undefined, WAIT)
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
    await screen.findByText('never', undefined, WAIT)
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

  // THE PARTIAL ALL-CLEAR, WHICH IS THE CASE THE WHOLE CARD EXISTS FOR.
  //
  // WHAT MOVED: two paragraphs -- "Nothing wrong in the N checks that ran" and
  // "N of M could not run — a partial all-clear, and what it did not look at
  // is listed below" -- plus `checks.map(c => c.note).join(' · ')`, which was
  // eight full sentences on the healthy path.
  //
  // WHERE THE WORDS LIVE NOW: the ring's filled arc is the checks that RAN and
  // its hatched arc is the ones that could not, so a short problem list over
  // blind checks and a short list over clear ones are different pictures
  // before either is read. The sentences are the dial's accessible name, the
  // `N blind` figure's accessible name, and the `all-clear-basis` topic.
  //
  // A MUTATION GOT PAST THE FIRST DRAFT OF THIS FILE by pinning `dialKind` to
  // 'measured' unconditionally -- a full ring over checks that never ran, the
  // absence-as-measurement lie in its purest form. That is what this kills.
  it('draws a partial all-clear as a partial ring, never as a full one', async () => {
    // A non-admin genuinely cannot read /v1/leases, so one check goes blind.
    renderOverview({
      reads: {
        loadLeases: {
          status: 'error',
          error: { kind: 'admin_required', httpStatus: 403, code: null, message: 'admin only' },
        },
      },
    })
    await screen.findByText('never', undefined, WAIT)
    expectAllCardsClosed()

    const dial = document.querySelector('.ov-dial')
    expect(dial, 'the attention card drew no coverage dial').not.toBeNull()
    // THE HOLE IN THE TOTAL IS DRAWN AS A HOLE.
    expect(dial!.getAttribute('data-partial'), 'a partial coverage drew a full ring').toBe('yes')
    expect(dial!.className).toContain('is-partial')
    // The ring is filled to the checks that RAN, not to 100%.
    expect(dial!.getAttribute('style')).not.toContain('--pct: 100')
    expect(dial!.getAttribute('aria-label')).toMatch(/could not run/i)

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
    expect(panel!.querySelector('button[aria-label^="Help: "]')).not.toBeNull()
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

  /**
   * OV-1: ONE POLARITY, AND EVERY % CARRIES ITS WORD. The Overview's headline
   * said `% left` over rows of % used; the owner set % used everywhere
   * (Overview, Accounts, sc). This table already printed used, under column
   * heads that said only `5h` and `7d` -- so the word goes on the head, and on
   * the phone key that stands in for it.
   *
   * MUTATION: head the columns `5h` and `7d` again.
   */
  it('says which way every window percentage points, in its column head', async () => {
    renderAccounts([MEASURED_ZERO])
    await screen.findByText('eng:fresh', undefined, WAIT)
    const heads = [...document.querySelectorAll('table.accounts thead th')].map((th) => textOf(th))
    expect(heads).toContain('5h used')
    expect(heads).toContain('7d used')
    const keys = [...document.querySelectorAll('td.acct-window')].map((td) => td.getAttribute('data-label'))
    expect(keys.length, 'no window cell was drawn').toBeGreaterThan(0)
    for (const key of keys) expect(key, 'a phone key drops the polarity').toMatch(/ used$/)
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
    expect(card!.querySelector('button[aria-label^="Help: "]')).not.toBeNull()

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
