// WHAT THE GENERATED TYPES CANNOT HOLD, FOR THE OUTCOMES, RUNTIMES AND ACCOUNTS
// SCREENS (part of #72).
//
// Their response shapes are generated (./generated/api-types.ts, from
// `swarm_api/wire_types.py`) and `tsc` holds the console's types to them. A
// type says nothing about the REQUEST: which route a loader calls, which query
// parameters it sends, and which of the route's choices and limits the page
// offers. Those were held by Python tests that read outcomes.ts and api.ts as
// text; they are held here instead, by calling the console's own functions:
//
//   * the loaders, with fetch stubbed, request the route the model documents;
//   * `outcomesQuery` sends only parameters `GET /v1/outcomes` declares --
//     test_wire_types.py asserts the same list against the OpenAPI document;
//   * the Timeline's spans, buckets, kinds, groups and limits equal the
//     generated ./generated/constants/outcomes.json, which the drift check
//     holds to `swarm_api.outcomes`;
//   * `unreadableFor` asks the verdict (`unreadable_now`), not the record.

import { afterEach, describe, expect, it, vi } from 'vitest'

import outcomesConstants from '../generated/constants/outcomes.json'
import {
  BUCKET_CHOICES,
  DEFAULT_SPAN,
  DEFAULT_VIEW,
  GROUPS,
  KINDS,
  MAX_BUCKETS,
  MAX_SPAN_DAYS,
  MONTH_MIN_DAYS,
  OUTCOMES_CACHE_S,
  ROW_OUTCOMES,
  SPANS,
  outcomesQuery,
  rowsQuery,
  withRows,
} from '../outcomes'
import { unreadableFor, type Account } from '../types'
import { serve } from './repofixture'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

/** api.ts as the live build loads it: no fixtures, every read through fetch. */
async function liveApi(): Promise<typeof import('../api')> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../api')
}

/** The path and the query of each request, without the origin. */
function requested(calls: { url: string }[]): { path: string; query: URLSearchParams }[] {
  return calls.map(({ url }) => {
    const parsed = new URL(url, 'https://console.invalid')
    return { path: parsed.pathname, query: parsed.searchParams }
  })
}

// The query parameters `GET /v1/outcomes` declares that the Timeline may send.
// test_wire_types.py::test_the_outcomes_route_declares_every_parameter_the_timeline_sends
// asserts this same list is in the route's OpenAPI parameters.
const OUTCOMES_PARAMETERS = new Set([
  'tz', 'span', 'since', 'until', 'bucket', 'scope', 'tenant', 'exclude_tenant',
  'profile', 'submitted_by', 'kind', 'group', 'compare', 'section',
])

// The figure-rows read adds these three; test_outcomes_rows.py asserts the
// route's OpenAPI parameters declare them.
const ROWS_PARAMETERS = new Set(['rows', 'rows_at', 'rows_key'])

describe('the outcomes request', () => {
  it('sends only parameters the route declares, tz always among them', () => {
    const views = [
      DEFAULT_VIEW,
      { ...DEFAULT_VIEW, platform: true, exclude_tenant: ['verify'], group: 'tenant_id' as const },
      { ...DEFAULT_VIEW, platform: true, tenant: ['eng', 'research'] },
      { ...DEFAULT_VIEW, span: null, since: '2026-09-20', until: '2026-09-24', kind: 'standalone' as const,
        profile: ['mock'], submitted_by: ['alice@example.com'], bucket: 'day' as const },
    ]
    const sent = new Set<string>()
    for (const v of views) {
      const q = outcomesQuery(v, 'UTC')
      expect(q.get('tz'), 'the route requires tz and the page does not send it').toBe('UTC')
      for (const key of q.keys()) sent.add(key)
    }
    // Not vacuous: every branch of outcomesQuery was taken.
    for (const key of ['span', 'since', 'until', 'bucket', 'scope', 'tenant', 'exclude_tenant',
      'profile', 'submitted_by', 'kind', 'group', 'compare']) {
      expect(sent.has(key), `no view made outcomesQuery send ${key}`).toBe(true)
    }
    const undeclared = [...sent].filter((k) => !OUTCOMES_PARAMETERS.has(k))
    expect(undeclared, 'outcomesQuery sends parameters the route does not declare').toEqual([])
  })

  it('rowsQuery sends rows, rows_at and rows_key beside the ledger query, nothing undeclared', () => {
    expect(rowsQuery(DEFAULT_VIEW, 'UTC'), 'no figure open, no rows read').toBeNull()
    const open = withRows({ ...DEFAULT_VIEW, platform: true, group: 'tenant_id' as const },
      'failed', '2026-09-21T00:00:00Z', 'eng')
    const q = rowsQuery(open, 'UTC')!
    expect(q.get('rows')).toBe('failed')
    expect(q.get('rows_at')).toBe('2026-09-21T00:00:00Z')
    expect(q.get('rows_key')).toBe('eng')
    expect(q.get('tz')).toBe('UTC')
    const undeclared = [...q.keys()].filter((k) => !OUTCOMES_PARAMETERS.has(k) && !ROWS_PARAMETERS.has(k))
    expect(undeclared, 'rowsQuery sends parameters the route does not declare').toEqual([])
  })

  it('loadOutcomes requests GET /v1/outcomes with that query and its sections', async () => {
    const calls = serve((m, url) =>
      m === 'GET' && url.includes('/v1/outcomes') ? { status: 200, body: {} } : null)
    const { loadOutcomes } = await liveApi()
    await loadOutcomes(outcomesQuery(DEFAULT_VIEW, 'Europe/Bucharest'), ['coverage'])
    const reqs = requested(calls)
    expect(reqs.map((r) => r.path)).toEqual(['/v1/outcomes'])
    const query = reqs[0]!.query
    expect(query.get('tz')).toBe('Europe/Bucharest')
    expect(query.get('span')).toBe(DEFAULT_SPAN)
    expect(query.getAll('section')).toEqual(['coverage'])
    expect([...query.keys()].filter((k) => !OUTCOMES_PARAMETERS.has(k))).toEqual([])
  })
})

describe("the Timeline's choices and limits are the route's", () => {
  it('equals the generated outcomes constants', () => {
    expect([...SPANS]).toEqual(outcomesConstants.spans)
    expect(DEFAULT_SPAN).toBe(outcomesConstants.default_span)
    expect([...BUCKET_CHOICES]).toEqual(['auto', ...outcomesConstants.buckets])
    expect([...KINDS]).toEqual(outcomesConstants.kinds)
    expect([...GROUPS]).toEqual(outcomesConstants.groups)
    expect([...ROW_OUTCOMES]).toEqual(outcomesConstants.row_outcomes)
    expect(MAX_BUCKETS).toBe(outcomesConstants.max_buckets)
    expect(MONTH_MIN_DAYS).toBe(outcomesConstants.month_min_days)
    expect(MAX_SPAN_DAYS).toBe(outcomesConstants.max_span_days)
    expect(OUTCOMES_CACHE_S).toBe(outcomesConstants.outcomes_cache_s)
  })
})

describe('the runtimes request', () => {
  it('loadRunnerProfiles reads the catalogue from GET /v1/runtimes', async () => {
    const calls = serve((m, url) =>
      m === 'GET' && url.endsWith('/v1/runtimes')
        ? { status: 200, body: { runtimes: { 'profile-a': { name: 'profile-a', available: true } } } }
        : null)
    const { loadRunnerProfiles } = await liveApi()
    const r = await loadRunnerProfiles()
    expect(requested(calls).map((c) => c.path)).toEqual(['/v1/runtimes'])
    expect(r.status).toBe('ok')
  })
})

describe('the account listing', () => {
  it('loadAccountsBoard reads GET /v1/accounts', async () => {
    const calls = serve((m, url) => {
      if (m !== 'GET') return null
      if (url.endsWith('/v1/accounts')) {
        return { status: 200, body: { accounts: [], tenant_id: 'eng', unreadable_documents: [],
          unreadable_document_count: 0 } }
      }
      return { status: 200, body: { tenants: [] } }
    })
    const { loadAccountsBoard } = await liveApi()
    await loadAccountsBoard()
    expect(requested(calls).map((c) => c.path)).toContain('/v1/accounts')
  })

  it('unreadableFor asks the verdict, never the record', () => {
    const base = {
      account_id: 'eng:main', owner_tenant: 'eng', label: 'main', provider: 'anthropic',
      state: 'AVAILABLE', reason: '', lend_to: [], assigned: 0, windows: {}, observed_at: null,
      stale: false, last_assigned_at: null,
    }
    // An aged-out report: on the record, no longer the verdict. The pool assigns it again.
    const recordOnly = { ...base, unreadable_by: ['eng'], unreadable_now: [] } as Account
    expect(unreadableFor(recordOnly, 'eng')).toBe(false)
    // A live report: the pool is skipping this account for eng, and only for eng.
    const verdict = { ...base, unreadable_by: ['eng'], unreadable_now: ['eng'] } as Account
    expect(unreadableFor(verdict, 'eng')).toBe(true)
    expect(unreadableFor(verdict, 'research')).toBe(false)
    expect(unreadableFor(verdict, null)).toBe(false)
  })
})
