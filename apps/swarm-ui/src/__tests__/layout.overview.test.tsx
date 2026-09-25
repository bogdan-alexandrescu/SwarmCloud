// THE LANDING SCREEN'S STRUCTURE, AS A COUNT.
//
// WHY THIS FILE EXISTS, stated so nobody has to re-derive it.
//
// Two passes over this console corrected ATTRIBUTES -- figure size, shadow
// count, hue count, chip construction -- and the owner's verdict on the second
// was that the design "didn't really change much". The proof was in that pass's
// own report: bordered elements on the Overview went 41 -> 41. Values moved;
// structure did not. Nothing in the suite could have caught that, because every
// gate here measures a value (a type step, a word count, a contrast ratio) and
// none of them measures a LEVEL.
//
// So this file asserts levels. Each claim below is one a colour pass, a
// spacing pass or a type pass cannot satisfy by accident, and each was proved
// by mutation rather than by going green:
//
//   * making the lead a `.ctl-card` again           -> `is a region, not a panel` fails
//   * restoring the Attention tile                  -> `says the count once` fails
//   * giving `.ctl-metric` a four-sided border      -> `a fact is not a tile` fails
//   * re-adding a fourth or fifth panel             -> `holds three panels` fails
//
// WHAT THIS FILE DELIBERATELY DOES NOT DO. It does not assert a total bordered
// count. jsdom has no layout engine and the probe in `spacing.test.tsx` says so
// at `spaceprobe.ts:12`; a total would have to include another file's help
// glyphs and a shared primitive's track axes, so it would fail for reasons this
// screen cannot fix and pass for reasons it did not earn. Levels are the thing
// that is both checkable here and load-bearing.

import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

import STYLES from '../styles.css?raw'
// App.tsx as TEXT, not as a module: importing it would evaluate every screen
// against the api mock below, which declares only the eight reads Overview
// makes. The tab labels are string literals in `SECTIONS` -- the Python gate
// reads them the same way -- so the source is the one place they are stated.
import APP_SOURCE from '../App.tsx?raw'
import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import { elapsed, type Account, type AccountsPage, type Capacity, type Stats, type Task, type TaskPage } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
}))
vi.mock('../api', () => api)

const { OverviewScreen } = await import('../Overview')

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

function accountsPage(accounts: Account[]): AccountsPage {
  return { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 }
}

function spend(): SpendRollup {
  return {
    tenantId: 'eng',
    tasksOnPage: 0,
    tasksWithAttempts: 0,
    tasksSampled: 0,
    failedReads: 0,
    failedDetail: null,
    attempts: 0,
    attemptsWithCost: 0,
    attemptsWithTokens: 0,
    costUsd: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
    from: '2026-09-23T09:00:00Z',
    to: '2026-09-23T10:00:00Z',
  }
}

async function settle(): Promise<void> {
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
}

async function mountOverview(
  over: { tasks?: TaskPage; stats?: Stats } = {},
): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(ok(over.tasks ?? EMPTY_TASKS))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(over.stats ?? EMPTY_STATS))
  api.loadAccountPool.mockResolvedValue(ok(accountsPage([account({})])))
  api.loadSpend.mockResolvedValue(ok(spend()))

  const { container } = render(<OverviewScreen />)
  await settle()
  return container
}

/** A top-level rule's body, by exact selector — never by substring. */
function ruleFor(selector: string): string {
  const at = new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{`, 'm').exec(
    STYLES,
  )
  expect(at, `styles.css must declare a top-level ${selector} rule`).not.toBeNull()
  return STYLES.slice(at!.index).split('}')[0] ?? ''
}

describe('the landing screen is a lead and two regions, not a grid of boxes', () => {
  /**
   * THE LEAD IS A REGION.
   *
   * "What is wrong" was the first card of a five-card grid: a bordered,
   * rounded --surface panel with a head, a body and a full-bleed foot, at
   * exactly the weight of "Spend". §13.3 of design-system.md: a REGION is a
   * change of subject and is never a box; a PANEL is an object and draws the
   * one box there is. The page's subject is not one of its objects.
   */
  it('draws the attention list as a region, not as a panel', async () => {
    const el = await mountOverview()

    const lead = el.querySelector('.ov-lead')
    expect(lead, 'the overview drew no lead region').not.toBeNull()
    expect(lead!.tagName).toBe('SECTION')
    expect(lead!.className, 'the lead is a .section, so it separates by the region rule').toContain(
      'section',
    )
    expect(
      lead!.className,
      'the lead went back to being a card, which is the thing this pass removed',
    ).not.toContain('ctl-card')
    // And nothing wrapped it in one a level up, either.
    expect(lead!.closest('.ctl-card'), 'the lead is nested inside a panel').toBeNull()
  })

  /**
   * THE COUNT IS SAID ONCE.
   *
   * The strip used to carry `Attention · 10 things` one line above a card
   * headed `Needs attention` holding the same ten. A fact drawn twice is not
   * emphasis: it is a reader checking whether the two numbers agree. The strip
   * has four facts now and none of them is the problem count.
   */
  it('says the attention count once, at page rank, and not again in the fact strip', async () => {
    const el = await mountOverview()

    const title = el.querySelector('.ov-lead-title')
    expect(title, 'the lead has no title').not.toBeNull()

    const labels = [...el.querySelectorAll('.ctl-metrics .ctl-metric-label')].map((n) =>
      (n.textContent ?? '').trim(),
    )
    expect(labels.length, 'the fact strip lost or gained a figure').toBe(4)
    expect(
      labels.some((l) => /attention/i.test(l)),
      'the attention figure is in the strip AND in the lead',
    ).toBe(false)
  })

  /**
   * THREE PANELS, NOT FIVE.
   *
   * Five equal cards never fill a three-track row, which is why the old grid
   * carried ten nth-child parity rules to widen whichever card landed last.
   * "Capacity" and "Subscription pool" were two of the five answering ONE
   * question -- can I start more work, and what stops me -- so they are one
   * panel, and the orphan they created is gone with them.
   */
  it('holds three panels and no more', async () => {
    const el = await mountOverview()

    const panels = [...el.querySelectorAll('.ctl-card')].filter(
      (c) => c.closest('.ctl-card') === c,
    )
    // The title is read with `<HelpCard>`'s visually-hidden copy taken out --
    // it is inside `.ctl-card-title` so that assistive technology gets the
    // explanation at the label, and it is in `textContent` whether the card is
    // open or shut. `prose.budget.test.tsx` makes the same exclusion.
    const titles = panels.map((p) => {
      const clone = p.querySelector('.ctl-card-title')!.cloneNode(true) as HTMLElement
      for (const n of [...clone.querySelectorAll('[data-help-description], button')]) n.remove()
      return (clone.textContent ?? '').trim()
    })
    expect(titles.length, `the overview drew ${titles.length} panels: ${titles.join(', ')}`).toBe(3)
    expect(titles).toContain('Running')
    expect(titles).toContain('Spend')
    // One panel, two groups: the two ceilings bind in sequence and are read
    // together or not at all.
    expect(titles).toContain('Headroom')
    expect(el.querySelectorAll('.ov-headroom .ov-group').length).toBe(2)
  })
})

describe('§B6.1: a fact is not a tile', () => {
  /**
   * THE BORDER IS DECLARED ON ONE EDGE AND PAINTED ON NONE.
   *
   * §13.5 keeps the declared transparent border -- an absence state that
   * ADDS a border would move every neighbour by a pixel at the moment the
   * strip most needs to hold still. What this pass decided is which edge may
   * ever be painted: a box around a fact says "this is an object", a rule
   * under it says "there is something to say about this reading". Nothing
   * healthy is ruled.
   */
  it('declares the border on the bottom edge alone', () => {
    const rule = ruleFor('.ctl-metric')
    expect(rule, '.ctl-metric declares no border colour').toMatch(/border-color:\s*transparent/)
    const width = /border-width:\s*([^;]+);/.exec(rule)
    expect(width, '.ctl-metric must declare its border widths explicitly').not.toBeNull()
    // `0 0 1px` — top, sides zero; bottom reserved. Anything with a non-zero
    // first or second value is a box again.
    const parts = (width?.[1] ?? '').trim().split(/\s+/)
    expect(parts.length, 'the widths are written as a four-value shorthand').toBe(3)
    expect(parts[0], 'the top edge is reserved, so the fact is a box again').toBe('0')
    expect(parts[1], 'the side edges are reserved, so the fact is a box again').toBe('0')
    expect(parts[2]).toBe('1px')
    // And no fill: a --surface patch on a --surface panel is either invisible
    // or a box with a soft edge, and neither is a separation.
    expect(rule).toMatch(/background:\s*none/)
  })

  /**
   * THE TWO ABSENCES PAINT THAT ONE EDGE, AND ONLY THAT ONE.
   *
   * `is-absent` (hatched: the platform has no such figure) and `is-unread`
   * (the read failed) are told apart by tone, but neither may be told apart
   * from a healthy fact by tone alone -- the `.ctl-mark` beside the value
   * carries the fill and the words. What is asserted here is that the rule
   * they paint is a rule and not a box.
   */
  it('paints an absence as a rule under the fact, never as a box around it', () => {
    for (const state of ['.ctl-metric.is-absent', '.ctl-metric.is-unread']) {
      const rule = ruleFor(state)
      expect(rule, `${state} draws nothing at all`).toMatch(/border-bottom-style:\s*dashed/)
      expect(
        /border-style:\s*dashed/.test(rule),
        `${state} went back to a four-sided dashed box`,
      ).toBe(false)
    }
  })
})

// ---------------------------------------------------------------------------
// What the cards say about what they link to and what they count
// ---------------------------------------------------------------------------

function liveTask(over: Partial<Task> = {}): Task {
  return {
    id: 'task_0123456789abcdef0123',
    tenant_id: 'eng',
    state: 'RUNNING',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: '2026-09-23T09:00:00Z',
    updated_at: '2026-09-23T09:30:00Z',
    started_at: '2026-09-23T09:01:00Z',
    completed_at: null,
    submitted_by: 'ada@eng.test',
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
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: 'lease_1',
    ...over,
  }
}

/**
 * The label App.tsx's `SECTIONS` gives the tab a `#<section>/<tab>` hash
 * opens, read out of the source. Null when the hash names no declared tab, so
 * a link to nowhere fails here as well as in `nav.links.test.tsx`.
 */
function tabLabel(hash: string): string | null {
  const tab = hash.replace(/^#/, '').split('/')[1]
  if (tab === undefined) return null
  const escaped = tab.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const decl = new RegExp(`\\{\\s*id:\\s*'${escaped}',\\s*label:\\s*'([^']+)'`).exec(APP_SOURCE)
  return decl?.[1] ?? null
}

describe('a card says what it opens, and a count is not a verdict', () => {
  /**
   * OV-11. THE LINK WORD IS THE NAME OF THE TAB IT OPENS.
   *
   * The Headroom card's link read `pools →` and opened Profile headroom -- a
   * reader who wanted the Pools tab got a different one, and the word gave no
   * warning. Every card-head link is held to the label its destination tab
   * carries in `SECTIONS`, DERIVED from App.tsx rather than restated here, so
   * a renamed tab fails this rather than leaving a card pointing at an old
   * name.
   *
   * MUTATION: put `cta="pools"` back on the Headroom card. This goes red on
   * `#capacity/profiles`.
   */
  it('names every card-head link after the tab it opens', async () => {
    const el = await mountOverview()
    const links = [...el.querySelectorAll<HTMLAnchorElement>('.ctl-card-head a.ov-link')]
    // A sweep that found no links would pass over the one that is wrong.
    expect(links.length, 'no card-head link was found, so nothing was checked').toBeGreaterThanOrEqual(3)
    for (const a of links) {
      const href = a.getAttribute('href') ?? ''
      const label = tabLabel(href)
      expect(label, `${href} names no tab declared in App.tsx SECTIONS`).not.toBeNull()
      const word = (a.textContent ?? '').replace('→', '').trim().toLowerCase()
      expect(word, `the link to ${href} reads "${word}", not the tab's name`).toBe(label!.toLowerCase())
    }
  })

  /**
   * OV-5. A RUNNING COUNT CARRIES NO VERDICT TONE.
   *
   * The Running tile was `is-good` -- --ok green, with the verdict disc --
   * whenever anything ran, while the table under it drew the same agents with
   * the blue `is-live` mark. Five agents running is neither good nor bad; it
   * is a fact (design-system.md §6.7), and green was the one place the screen
   * called it healthy.
   *
   * MUTATION: restore `tone={inFlight > 0 ? 'good' : undefined}` on the tile.
   */
  it('draws a non-zero running count with no verdict tone', async () => {
    const el = await mountOverview({
      tasks: { tasks: [liveTask()], next_page_token: null },
      stats: { ...EMPTY_STATS, tasks_by_state: { RUNNING: 5, LEASED: 0 } },
    })
    const tile = [...el.querySelectorAll('.ctl-metrics .ctl-metric')].find((m) =>
      /^running/i.test((m.querySelector('.ctl-metric-label')?.textContent ?? '').trim()),
    )
    expect(tile, 'the Running figure is not on the strip').toBeDefined()
    // The count IS there -- this is the running case, not an empty one.
    expect(tile!.querySelector('.ctl-metric-value')!.textContent).toMatch(/5/)
    expect(tile!.classList.contains('is-good'), 'a running count is painted as healthy').toBe(false)
    expect(tile!.classList.contains('is-alert'), 'a running count is painted as a problem').toBe(false)
  })

  /**
   * AG-3. A COLUMN THAT HOLDS NO RUN DOES NOT CLAIM RUN TIME.
   *
   * A LEASED task has no `started_at` -- the worker writes it on DISPATCHED ->
   * STARTING -- so its cell is not a run. The column was headed "Runtime",
   * which labelled whatever sat there as the agent's run. The property pinned
   * is the one the finding is about: the heading over a not-started row's
   * cell makes no claim that anything ran.
   *
   * THE CELL IS NO LONGER A DURATION, and this used to find it by its digit.
   * It held the task's age after the state word, `leased 3h 0m`, which read as
   * three hours held in a lease taken a second ago; `elapsed()` now prints the
   * state word alone for LEASED and DISPATCHED (types.test.ts pins why). So
   * the cell is found as `elapsed()`'s text for this task, which is what the
   * column renders, and it must not read as time in the lease.
   *
   * MUTATION: head the column "Runtime" again.
   */
  it('does not head a not-started agent’s cell as run time', async () => {
    const leased = liveTask({ id: 'task_leased00000000000000', state: 'LEASED', started_at: null })
    const el = await mountOverview({
      tasks: { tasks: [leased], next_page_token: null },
      stats: { ...EMPTY_STATS, tasks_by_state: { LEASED: 1 } },
    })
    const table = el.querySelector('.ov-running table')
    expect(table, 'the running table is not drawn for a leased agent').not.toBeNull()
    const heads = [...table!.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
    const row = table!.querySelector('tbody tr')!
    const cells = [...row.children]
    const figure = cells[cells.length - 1]!
    // The row's last cell is `elapsed()`'s answer for this task, and it does
    // not put the task's age after the state word.
    expect(figure.textContent ?? '').toBe(elapsed(leased, Date.now()).text)
    expect(figure.textContent ?? '', 'the age reads as time held in the lease').not.toMatch(/^leased\s+\d/)
    const head = heads[heads.length - 1] ?? ''
    expect(head, 'the duration column has no heading').not.toBe('')
    expect(head, `a not-started agent's wait sits under "${head}"`).not.toMatch(/run/i)
  })
})
