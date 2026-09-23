// SPLIT FROM prose.budget.test.tsx ON MERGE, not authored separately.
//
// Two lanes of the design overhaul each wrote a file at that path -- one for
// the capacity group, one for the screens the other lanes rebuilt -- and git
// saw a whole-file add/add. They measure different screens against different
// ceilings, so neither is redundant and picking one would have silently
// dropped a budget. This is the capacity group's half, at its own path.
//
// THE PROSE BUDGET, FOR THE CAPACITY GROUP.
//
// The owner's directive is that these screens carry features, data and
// functionality -- not text. `honesty.prose.test.tsx` already guards the half
// of that which can go wrong DANGEROUSLY: that removing a sentence never
// removes the FACT it carried, so an absence is never rendered as a
// measurement. This file guards the other half, which can only go wrong
// SLOWLY: that the sentences do not come back.
//
// WHY A NUMBER AND NOT A REVIEW. Every previous pass at this either stalled or
// was reverted, and the audit's own account of why is that prose returns one
// reasonable line at a time. Nobody adds a paragraph; somebody adds a
// clarifying clause under a figure, and six months later the screen is an
// essay again. A count is the only thing that notices the fourteenth clause,
// because the fourteenth clause is indistinguishable from the first thirteen.
//
// WHAT IS COUNTED. Text nodes the reader can see, joined with a space and
// filtered to tokens containing a letter or a digit. Deliberately NOT
// `textContent`, which concatenates `<td>eng</td><td>4</td>` into `eng4` and
// therefore makes the number depend on how the DOM is NESTED rather than on
// how many words are in it -- worthless for comparing two different layouts.
// Excluded, because a reader cannot read them: `<style>`, `<script>`, the
// visually-hidden `[data-help-description]` node every `<HelpCard>` renders
// for assistive technology, and the body of a CLOSED `<details>`.
//
// WHAT THE CEILINGS ARE. The measured count at the end of the B4.5 pass, plus
// a small margin for fixture drift. They are ceilings, not targets: a screen
// that comes in under one is not failing anything. The BEFORE figures are
// recorded beside them so the next person can see which direction this has
// ever moved in.
//
//   screen     before   after   ceiling
//   Pools         280     195       215
//   Runtimes      338     233       255
//   Holders        94      83        95
//   Accounts      474     227       250
//
// A ceiling that has to be RAISED is not a failure either -- a screen that
// gains a genuinely new column gains words. It is a decision, and the point of
// the gate is that raising it is a decision somebody makes in a diff rather
// than an outcome nobody observes.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AccountsBoard, HoldersBoard, RuntimeTopology } from '../api'
import type {
  Account,
  AccountsPage,
  Capacity,
  LeasePage,
  LeaseRow,
  Pool,
  ResourceClassSpec,
  Runtime,
} from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadRuntimeTopology: vi.fn(),
  loadAccountsBoard: vi.fn(),
  loadHolders: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const { CapacityScreen } = await import('../Capacity')
const { RuntimesScreen } = await import('../Runtimes')
const { HoldersScreen } = await import('../Holders')
const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
}

function visibleText(): string {
  // TEXT NODES, JOINED WITH A SPACE -- not `textContent`.
  //
  // `textContent` concatenates adjacent elements with no separator, so
  // `<td>eng</td><td>4</td>` reads as the single token `eng4`. That makes the
  // count depend on how the DOM is NESTED, and this measurement compares two
  // different DOMs. Walking the text nodes and joining them gives each rendered
  // string its own boundary, so the number moves only when the words move.
  const clone = document.body.cloneNode(true) as HTMLElement
  for (const el of [...clone.querySelectorAll('[data-help-description], style, script')]) {
    el.remove()
  }
  // A CLOSED <details> IS IN `textContent` AND IS NOT ON THE SCREEN. Counting
  // it would credit this pass for collapsing prose that a reader never saw,
  // and would equally have inflated the baseline. Both numbers exclude it.
  for (const d of [...clone.querySelectorAll('details:not([open])')]) {
    for (const child of [...d.children]) {
      if (child.tagName.toLowerCase() !== 'summary') child.remove()
    }
  }
  const out: string[] = []
  const walk = document.createTreeWalker(clone, 4 /* SHOW_TEXT */)
  for (let n = walk.nextNode(); n; n = walk.nextNode()) out.push(n.nodeValue ?? '')
  return out.join(' ').replace(/\s+/g, ' ').trim()
}

function words(): number {
  const t = visibleText()
  if (t === '') return 0
  // A bare punctuation run is not a word: the separators this UI uses between
  // facts (`.`, `--`, `/`) would otherwise be counted as prose.
  return t.split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)).length
}

// --- fixtures --------------------------------------------------------------

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'backend:cloudrun',
    hard_limit: 8,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 8,
    active: 3,
    available: 5,
    enabled: true,
    updated_at: '2026-09-23T10:00:00Z',
    ...over,
  }
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
    available: true,
    disabled_reason: '',
    ...over,
  }
}

const CAPACITY: Capacity = {
  pools: [
    pool({ name: 'global:agents', active: 6, effective_limit: 16, available: 10 }),
    pool({ name: 'tenant:eng', active: 3, effective_limit: 8, available: 5 }),
    pool({ name: 'resource:standard', active: 3, effective_limit: 12, available: 9 }),
    pool({ name: 'runner:claude-code', active: 2, effective_limit: 6, available: 4 }),
    pool({ name: 'backend:cloudrun', active: 3, effective_limit: 8, available: 5 }),
    pool({ name: 'provider:anthropic', active: 1, effective_limit: 4, available: 3, enabled: false }),
  ],
  runner_profiles: {
    'claude-code': { units: 1, backend: 'cloudrun', provider: 'anthropic', resource_class: 'standard', pools: ['global:agents', 'tenant:eng'] },
    'claude-large': { units: 4, backend: 'gke', provider: 'anthropic', resource_class: 'large', pools: ['global:agents', 'resource:large'] },
  },
  tenant_id: 'eng',
  generated_at: '2026-09-23T10:00:00Z',
}

function lease(over: Partial<LeaseRow>): LeaseRow {
  return {
    lease_id: 'lease-aaaaaaaaaa',
    task_id: 'task-bbbbbbbbbb',
    attempt_id: 'att-1',
    tenant_id: 'eng',
    generation: 3,
    pools: ['global:agents', 'resource:standard'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-09-23T09:00:00Z',
    dispatch_deadline: '2026-09-23T09:05:00Z',
    expires_at: '2026-09-23T11:00:00Z',
    heartbeat_at: '2026-09-23T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 60,
    heartbeat_ever: true,
    last_error: null,
    ...over,
  } as LeaseRow
}

const LEASES: LeaseRow[] = [
  lease({}),
  lease({ lease_id: 'lease-cccccccccc', task_id: 'task-dddddddddd', units: 4, pools: ['global:agents', 'resource:large'], dispatch_state: 'LEASED' }),
  lease({ lease_id: 'lease-eeeeeeeeee', task_id: 'task-ffffffffff', units: 2, pools: ['global:agents', 'resource:browser'] }),
]

const HOLDERS: HoldersBoard = {
  page: {
    leases: LEASES,
    units_held: 7,
    tenant_id: 'eng',
  } as unknown as LeasePage,
  pools: [
    pool({ name: 'global:agents', active: 9 }),
    pool({ name: 'resource:standard', active: 1 }),
  ],
  poolsDetail: null,
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

const ACCOUNTS: Account[] = [
  account({ account_id: 'eng:never', label: 'never', observed_at: null, windows: {} }),
  account({
    account_id: 'eng:fresh',
    label: 'fresh',
    observed_at: new Date().toISOString(),
    stale: false,
    assigned: 2,
    windows: {
      five_hour: { utilization: 0, resets_at: '2099-01-01T00:00:00Z', reset: false },
      seven_day: { utilization: 0.42, resets_at: '2099-01-01T00:00:00Z', reset: false },
    },
  }),
  account({
    account_id: 'eng:broken',
    label: 'broken',
    state: 'REAUTH_REQUIRED',
    reason: 'the refresh token was rejected',
    observed_at: new Date().toISOString(),
    windows: {
      five_hour: { utilization: 0.91, resets_at: '2099-01-01T00:00:00Z', reset: false },
    },
  }),
  account({ account_id: 'other:lent', label: 'lent', owner_tenant: 'other', lend_to: ['eng'] }),
]

function accountsBoard(accounts: Account[]): AccountsBoard {
  return {
    page: {
      accounts,
      tenant_id: 'eng',
      unreadable_documents: [],
      unreadable_document_count: 0,
    } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

function topology(over: Partial<RuntimeTopology> = {}): RuntimeTopology {
  return {
    runtimes: {
      'claude-code': runtime({}),
      'claude-large': runtime({
        name: 'claude-large',
        resource_class: 'large',
        resources: size({ name: 'large', cpu: 8, memory_gib: 16, disk_gib: 4, units: 4 }),
        resolved_backend: 'gke',
        backend: 'gke',
        timeout_seconds: 14400,
      }),
    },
    pools: [pool({}), pool({ name: 'backend:gke', active: 1, effective_limit: 4, available: 3 })],
    poolsDetail: null,
    classes: {
      standard: size(),
      large: size({ name: 'large', cpu: 8, memory_gib: 16, disk_gib: 4, units: 4 }),
    },
    classesDetail: null,
    ...over,
  }
}

// --- the budget ------------------------------------------------------------

/** Rendered-word ceilings. See the table in this file's header. */
const BUDGET = {
  Pools: 215,
  Runtimes: 255,
  Holders: 95,
  Accounts: 250,
} as const

function report(screenName: keyof typeof BUDGET): void {
  const n = words()
  // PRINTED BEFORE IT IS ASSERTED, the way `spacing.test.tsx` prints its sweep:
  // the useful artefact is the number, and a run that only says "failed" makes
  // whoever is raising or lowering a ceiling guess at what to put.
  console.log(`${screenName}: ${n} rendered words (ceiling ${BUDGET[screenName]})`)
  expect(n, `${screenName} renders more words than its budget allows`).toBeLessThanOrEqual(
    BUDGET[screenName],
  )
  // A SCREEN THAT RENDERS ALMOST NOTHING IS NOT A WIN, it is a screen that
  // failed to load -- and a budget with no floor passes hardest exactly then.
  expect(n, `${screenName} rendered almost nothing; it probably did not load`).toBeGreaterThan(40)
}

describe('the capacity group stays inside its prose budget', () => {
  it('Pools', async () => {
    api.loadCapacity.mockResolvedValue(ok(CAPACITY))
    render(<CapacityScreen />)
    await screen.findAllByText(/claude-code/, undefined, WAIT)
    report('Pools')
  })

  it('Runtimes', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findAllByText('claude-code', undefined, WAIT)
    report('Runtimes')
  })

  it('Holders', async () => {
    api.loadHolders.mockResolvedValue(ok(HOLDERS))
    render(<HoldersScreen />)
    await waitFor(() => screen.getByText(/Every holder/i), WAIT)
    report('Holders')
  })

  it('Accounts', async () => {
    api.loadAccountsBoard.mockResolvedValue(ok(accountsBoard(ACCOUNTS)))
    render(<AccountsScreen />)
    await screen.findByText('eng:never', undefined, WAIT)
    report('Accounts')
  })
})
