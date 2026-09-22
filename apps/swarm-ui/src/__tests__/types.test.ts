// The pure logic every screen is built on, and which nothing executed.
//
// These functions decide whether a figure is a measurement or an absence,
// which pool an operator is sent to raise, and what a row says about a paused
// pool. All of it shipped untested: `npm test` did not exist, and the Python
// files that "cover" this read types.ts as text.

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  timeAgo,
  headroomFor,
  limitedBy,
  overCeiling,
  poolKind,
  poolLabel,
  poolScope,
  reasonCopy,
  setBy,
  REASON_COPY,
  type Pool,
  type ProfileAdmission,
  type ProfileBlocker,
  type RunnerProfile,
} from '../types'

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'global',
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 0,
    available: 10,
    enabled: true,
    updated_at: '2026-09-22T10:00:00Z',
    ...over,
  }
}

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 8, active: 8, group: 'no_room', ...over }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1,
    headroom: 3,
    basis: 'measured',
    blockers: [],
    binding: [],
    counterfactual: [],
    complete: true,
    unread: [],
    uncapped: [],
    ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return {
    resource_class: 'standard',
    backend: 'cloudrun',
    provider: 'anthropic',
    units: 1,
    pools: ['global', 'tenant:eng'],
    ...over,
  }
}

// ---------------------------------------------------------------------------
// headroomFor -- the function whose bug this repository already paid for
// ---------------------------------------------------------------------------

describe('headroomFor', () => {
  it('keeps EVERY pool that refused, not the tightest one', () => {
    // THE ORIGINAL BUG, as behaviour. headroomFor used to keep a running
    // minimum and overwrite `binding` on each new one, so with two pools at
    // their ceilings the screen named one, an operator raised it, and nothing
    // moved. The fix is that the server sends a LIST -- so the test that keeps
    // it fixed is that the list survives.
    const both = [
      blocker({ pool: 'resource:large', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4 }),
      blocker({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 6, active: 6 }),
    ]
    const h = headroomFor(profile({ admission: admission({ headroom: 0, blockers: both, binding: ['resource:large', 'provider:anthropic'] }) }))

    expect(h.blockers).toHaveLength(2)
    expect(h.blockers.map((b) => b.pool)).toEqual(['resource:large', 'provider:anthropic'])
    expect(h.agents).toBe(0)
  })

  it('names the first binding pool for the columns with room for one', () => {
    const h = headroomFor(profile({ admission: admission({ binding: ['resource:large', 'provider:anthropic'] }) }))
    expect(h.binding).toBe('resource:large')
  })

  it('falls back to the first blocker when the server sent no binding list', () => {
    const h = headroomFor(profile({ admission: admission({ binding: [], blockers: [blocker({ pool: 'tenant:eng' })] }) }))
    expect(h.binding).toBe('tenant:eng')
  })

  it('reports null with nothing to bind, never an invented pool name', () => {
    const h = headroomFor(profile({ admission: admission({ binding: [], blockers: [] }) }))
    expect(h.binding).toBeNull()
  })

  it('an API that sends no admission block is NOT measured, and NOT a zero', () => {
    // The single most valuable assertion in this file. A client-side sum here
    // would be a restatement of the admission rule, and a 0 would be a claim
    // this screen is in no position to make.
    const h = headroomFor(profile({ admission: undefined, pools: ['global', 'tenant:eng', 'resource:large'] }))

    expect(h.agents).toBeNull()
    expect(h.agents).not.toBe(0)
    expect(h.basis).toBe('unknown')
    expect(h.complete).toBe(false)
    // Every pool is unread, because none of them was read.
    expect(h.unread).toEqual(['global', 'tenant:eng', 'resource:large'])
    expect(h.blockers).toEqual([])
  })

  it('does not alias the profile’s own pool array when it substitutes it', () => {
    // `unread: [...profile.pools]` rather than `profile.pools`. Aliasing would
    // let a screen that sorts `h.unread` reorder the profile's pool list,
    // which is the order a task clears them in.
    const p = profile({ admission: undefined, pools: ['b', 'a'] })
    const h = headroomFor(p)
    h.unread.sort()
    expect(p.pools).toEqual(['b', 'a'])
  })

  it('passes a measured zero through as a zero', () => {
    const h = headroomFor(profile({ admission: admission({ headroom: 0, basis: 'measured' }) }))
    expect(h.agents).toBe(0)
    expect(h.basis).toBe('measured')
  })

  it('passes an incomplete read through with what could not be read', () => {
    const h = headroomFor(
      profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['provider:anthropic'] }) }),
    )
    expect(h.complete).toBe(false)
    expect(h.unread).toEqual(['provider:anthropic'])
    expect(h.agents).toBeNull()
  })

  it('reports uncapped pools separately from unread ones', () => {
    // Opposite meanings: `uncapped` is "nothing limits this", `unread` is
    // "nobody knows". Collapsing them would report an unknown as unlimited.
    const h = headroomFor(profile({ admission: admission({ basis: 'uncapped', headroom: null, uncapped: ['runner:claude'] }) }))
    expect(h.missing).toEqual(['runner:claude'])
    expect(h.unread).toEqual([])
    expect(h.basis).toBe('uncapped')
  })
})

// ---------------------------------------------------------------------------
// Pool names
// ---------------------------------------------------------------------------

describe('poolKind', () => {
  it.each([
    ['global', 'global'],
    ['tenant:eng', 'tenant'],
    ['provider:anthropic', 'provider'],
    ['provider:anthropic:tenant:u-bogdan', 'provider'],
    ['resource:large', 'resource'],
    ['runner:claude-code', 'runner'],
    ['backend:cloudrun', 'backend'],
    // An unrecognised prefix must not crash and must not be filed under a
    // family whose scope wording would then be wrong.
    ['something:else', 'global'],
    ['', 'global'],
  ] as ReadonlyArray<[string, string]>)('%s -> %s', (name, kind) => {
    expect(poolKind(name)).toBe(kind)
  })
})

describe('poolLabel', () => {
  it.each([
    ['global', 'global'],
    ['tenant:eng', 'eng'],
    ['resource:large', 'large'],
    ['provider:anthropic:tenant:u-bogdan', 'anthropic · u-bogdan'],
    // Three segments: the fourth is absent, so the third stands in rather than
    // the label reading "anthropic · undefined".
    ['provider:anthropic:tenant', 'anthropic · tenant'],
  ] as ReadonlyArray<[string, string]>)('%s -> %s', (name, label) => {
    expect(poolLabel(name)).toBe(label)
  })

  it('never renders the string "undefined"', () => {
    for (const name of ['a:b:c', 'a:b:c:d', ':::', 'x', '']) {
      expect(poolLabel(name)).not.toContain('undefined')
    }
  })
})

describe('poolScope', () => {
  it.each([
    ['global', 'platform'],
    ['resource:large', 'platform'],
    ['provider:anthropic', 'platform'],
    ['tenant:eng', 'tenant'],
    // Trap E: the per-tenant slice of a provider pool is TENANT scope. Putting
    // it beside a platform figure is a scope error dressed as a comparison.
    ['provider:anthropic:tenant:u-bogdan', 'tenant'],
  ] as ReadonlyArray<[string, string]>)('%s -> %s', (name, scope) => {
    expect(poolScope(name)).toBe(scope)
  })
})

// ---------------------------------------------------------------------------
// What is holding a pool down
// ---------------------------------------------------------------------------

describe('setBy', () => {
  it('names the provider quota when the quota is the binding value', () => {
    const by = setBy(pool({ hard_limit: 10, quota_derived_limit: 4, effective_limit: 4 }))
    expect(by.term).toBe('provider quota')
    expect(by.detail).toContain('4')
    expect(by.detail).toContain('10')
  })

  it('names AIMD when back-off is the binding value', () => {
    const by = setBy(pool({ hard_limit: 10, adaptive_target: 3, effective_limit: 3 }))
    expect(by.term).toBe('AIMD back-off')
  })

  it('prefers the quota when quota and AIMD agree', () => {
    // Both at 4. The remedy differs -- ask the provider for more, versus wait
    // for AIMD to recover -- so the order is a decision, not an accident.
    const by = setBy(pool({ hard_limit: 10, adaptive_target: 4, quota_derived_limit: 4, effective_limit: 4 }))
    expect(by.term).toBe('provider quota')
  })

  it('says configured when nothing is holding it below the operator’s number', () => {
    expect(setBy(pool({ hard_limit: 10, effective_limit: 10 })).term).toBe('configured')
    // A quota EQUAL to the hard limit is not capping anything.
    expect(setBy(pool({ hard_limit: 10, quota_derived_limit: 10, effective_limit: 10 })).term).toBe('configured')
  })

  it('never returns an empty term or detail', () => {
    for (const p of [
      pool({}),
      pool({ effective_limit: 0, hard_limit: 0 }),
      pool({ adaptive_target: 0, effective_limit: 0 }),
      pool({ quota_derived_limit: 0, effective_limit: 0 }),
    ]) {
      const by = setBy(p)
      expect(by.term.trim()).not.toBe('')
      expect(by.detail.trim()).not.toBe('')
    }
  })
})

describe('limitedBy', () => {
  it('is null when the ceiling is the configured one', () => {
    expect(limitedBy(pool({ hard_limit: 8, effective_limit: 8 }))).toBeNull()
  })

  it('names quota, then adaptive', () => {
    expect(limitedBy(pool({ hard_limit: 8, quota_derived_limit: 2, effective_limit: 2 }))).toBe('quota')
    expect(limitedBy(pool({ hard_limit: 8, adaptive_target: 2, effective_limit: 2 }))).toBe('adaptive')
  })

  it('is null when something lowered the ceiling and neither field explains it', () => {
    // Better an honest null than a confident wrong diagnosis.
    expect(limitedBy(pool({ hard_limit: 8, effective_limit: 3 }))).toBeNull()
  })
})

describe('overCeiling', () => {
  it('is drift, not fullness', () => {
    // Admission cannot produce active > effective_limit, so this means a limit
    // was lowered under running work or a slot was never released.
    expect(overCeiling(pool({ active: 9, effective_limit: 8 }))).toBe(true)
    expect(overCeiling(pool({ active: 8, effective_limit: 8 }))).toBe(false)
    expect(overCeiling(pool({ active: 0, effective_limit: 0 }))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

describe('reasonCopy', () => {
  it('translates every reason the platform actually writes', () => {
    for (const [reason, copy] of Object.entries(REASON_COPY)) {
      expect(reasonCopy(reason)).toBe(copy)
      expect(copy.trim()).not.toBe('')
    }
  })

  it('prints an unknown reason VERBATIM rather than blanking the cell', () => {
    // `codec.blocked_reason_values()` is wired to no route, so this table is
    // shipped client-side and will go stale. Printing the raw value is what
    // makes that visible instead of silent.
    expect(reasonCopy('A_REASON_ADDED_AFTER_THIS_SHIPPED')).toBe('A_REASON_ADDED_AFTER_THIS_SHIPPED')
    expect(reasonCopy('')).toBe('')
  })

  it('keeps the tenant limit distinct from the platform being busy', () => {
    // Different people fix them: only an admin can raise a tenant limit, and
    // nobody can do anything about the platform being busy.
    expect(reasonCopy('TENANT_LIMIT')).toContain('admin')
    expect(reasonCopy('GLOBAL_CONCURRENCY_LIMIT')).not.toContain('admin')
  })

  it('says a paused pool needs resuming rather than waiting', () => {
    expect(reasonCopy('MANUAL_PAUSE')).toContain('resumed')
  })
})

// ---------------------------------------------------------------------------
// timeAgo
// ---------------------------------------------------------------------------

describe('timeAgo', () => {
  afterEach(() => vi.useRealTimers())

  function at(now: string): void {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(now))
  }

  it.each([
    ['2026-09-22T12:00:00Z', 'just now'],
    ['2026-09-22T11:59:30Z', '30s ago'],
    ['2026-09-22T11:55:00Z', '5m ago'],
    ['2026-09-22T09:00:00Z', '3h ago'],
  ] as ReadonlyArray<[string, string]>)('%s -> %s', (when, expected) => {
    at('2026-09-22T12:00:00Z')
    expect(timeAgo(when)).toBe(expected)
  })

  it('accepts an epoch, a Date and an ISO string alike', () => {
    at('2026-09-22T12:00:00Z')
    const iso = '2026-09-22T11:55:00Z'
    expect(timeAgo(iso)).toBe('5m ago')
    expect(timeAgo(new Date(iso))).toBe('5m ago')
    expect(timeAgo(Date.parse(iso))).toBe('5m ago')
  })

  it('says so rather than printing NaN when the timestamp is unreadable', () => {
    // An unparseable timestamp rendered as "NaNs ago" is a failure showing as
    // a measurement -- smaller than the headline bug, same shape.
    at('2026-09-22T12:00:00Z')
    expect(timeAgo('not a date')).toBe('at an unknown time')
    expect(timeAgo(Number.NaN)).toBe('at an unknown time')
  })

  it('clamps a future timestamp to "just now" rather than counting backwards', () => {
    at('2026-09-22T12:00:00Z')
    expect(timeAgo('2026-09-22T12:05:00Z')).toBe('just now')
  })
})

// -- timeAgo arithmetic ----------------------------------------------------
//
// A verifier found this unguarded on 2026-09-22: changing `h / 24` to `h / 12`
// left the whole suite green, so a four-day-old reading could print "8d ago"
// and ship. The test that existed only asserted the literal tokens 'd ago' and
// 'h ago' APPEARED IN THE SOURCE -- which a comment satisfies.
//
// These assert the arithmetic, which is possible because timeAgo takes `now`.
describe('timeAgo reports the right unit and the right number', () => {
  const NOW = Date.UTC(2026, 8, 22, 12, 0, 0)
  const ago = (ms: number) => timeAgo(NOW - ms, NOW)

  it('counts seconds, then minutes, then hours', () => {
    expect(ago(2_000)).toBe('just now')
    expect(ago(30_000)).toBe('30s ago')
    expect(ago(5 * 60_000)).toBe('5m ago')
    expect(ago(3 * 3_600_000)).toBe('3h ago')
  })

  it('stays in hours right up to 48, then switches to days', () => {
    expect(ago(47 * 3_600_000)).toBe('47h ago')
    expect(ago(48 * 3_600_000)).toBe('2d ago')
  })

  it('divides by 24, not by anything else', () => {
    // The exact mutation that went undetected: h/12 would make this '8d ago'.
    expect(ago(4 * 24 * 3_600_000)).toBe('4d ago')
    expect(ago(10 * 24 * 3_600_000)).toBe('10d ago')
  })

  it('refuses to invent a time it cannot read', () => {
    expect(timeAgo('not a date', NOW)).toBe('at an unknown time')
  })
})
