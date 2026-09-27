// THE TIMELINE'S VIEW, ITS QUERY AND ITS AXIS, AS PURE FUNCTIONS (#185).
//
// The view is the address: every filter is written to the hash and read back,
// so a link reproduces the page. And the view becomes the route's query in
// exactly one place, so the page cannot send a combination the contract
// refuses (422): month under 60 days, hourly past 2,000 buckets, a tenant
// filter in tenant scope, or a tenant grouping outside platform scope.
//
// MUTATIONS these catch: default the span to anything but 14d; drop
// `compare=previous`; send `tenant` in tenant scope; let `bucket=month`
// through on a 14-day span; label an hourly phone axis off the three-hour
// clock; read a bucket's wall clock in the browser's zone instead of the
// response's.

import { describe, expect, it } from 'vitest'

import {
  DEFAULT_VIEW,
  MAX_SPAN_DAYS,
  axisLabels,
  bucketName,
  bucketSaid,
  cacheable,
  dateLabel,
  dayDocWords,
  filtersSet,
  interval,
  outcomesQuery,
  parseView,
  pct,
  rangeRefusal,
  rateClause,
  secs,
  serializeView,
  wallOf,
} from '../outcomes'
import { ledgerFixture, wilsonFixture } from '../outcomes.fixture'

describe('the view in the hash', () => {
  it('is empty for the default page, so a bare #work/timeline is the default', () => {
    expect(serializeView(DEFAULT_VIEW)).toBe('')
    expect(parseView('')).toEqual(DEFAULT_VIEW)
    expect(parseView(null).span).toBe('14d')
  })

  it('round-trips every filter in one canonical order', () => {
    const q = 'span=30d&bucket=hour&scope=platform&profile=claude-code&profile=mock&kind=steps&group=tenant_id&table=1'
    expect(serializeView(parseView(q))).toBe(q)
  })

  it('reads a range only when it has both ends, and then carries no span', () => {
    const v = parseView('since=2026-09-01&until=2026-09-11')
    expect(v.span).toBeNull()
    expect(v.since).toBe('2026-09-01')
    expect(parseView('since=2026-09-01').span).toBe('14d')
  })

  it('keeps an instant’s offset through the hash, where a bare + would turn into a space', () => {
    const v = { ...DEFAULT_VIEW, span: null, since: '2026-09-22T00:00:00+03:00', until: '2026-09-23T00:00:00+03:00' }
    expect(parseView(serializeView(v)).since).toBe('2026-09-22T00:00:00+03:00')
  })

  it('treats a tenant filter as platform scope, and drops tenant grouping outside it', () => {
    expect(parseView('exclude_tenant=verify').platform).toBe(true)
    expect(parseView('group=tenant_id').group).toBe('runner_profile')
    expect(parseView('scope=platform&group=tenant_id').group).toBe('tenant_id')
  })

  it('drops what the route could never be sent: an unknown span, a malformed tenant id, month under 60 days', () => {
    expect(parseView('span=13d').span).toBe('14d')
    expect(parseView('scope=platform&tenant=Eng%20Team').tenant).toEqual([])
    expect(parseView('bucket=month').bucket).toBe('auto')
    expect(parseView('span=90d&bucket=month').bucket).toBe('month')
  })

  it('counts the secondary filters for the phone’s Filters · N', () => {
    expect(filtersSet(DEFAULT_VIEW)).toBe(0)
    expect(filtersSet(parseView('bucket=day&profile=mock&kind=steps'))).toBe(3)
  })
})

describe('the route’s query', () => {
  it('always names the zone and asks for the previous span', () => {
    const q = outcomesQuery(DEFAULT_VIEW, 'Europe/Bucharest')
    expect(q.get('tz')).toBe('Europe/Bucharest')
    expect(q.get('span')).toBe('14d')
    expect(q.get('compare')).toBe('previous')
    expect(q.get('group')).toBe('runner_profile')
  })

  it('never sends a tenant filter in tenant scope: the route takes the tenant from the caller', () => {
    const v = { ...DEFAULT_VIEW, tenant: ['eng'], exclude_tenant: ['verify'] }
    const q = outcomesQuery(v, 'UTC')
    expect(q.has('tenant')).toBe(false)
    expect(q.has('exclude_tenant')).toBe(false)
    expect(q.has('scope')).toBe(false)
  })

  it('sends platform scope with its exclusion, and a range instead of a span', () => {
    const q = outcomesQuery(parseView('since=2026-09-01&until=2026-09-11&exclude_tenant=verify'), 'UTC')
    expect(q.get('scope')).toBe('platform')
    expect(q.getAll('exclude_tenant')).toEqual(['verify'])
    expect(q.get('since')).toBe('2026-09-01')
    expect(q.has('span')).toBe(false)
  })

  it('never asks for hourly buckets past 2,000 or monthly under 60 days', () => {
    expect(outcomesQuery({ ...DEFAULT_VIEW, span: '90d', bucket: 'hour' }, 'UTC').get('bucket')).toBe('auto')
    expect(outcomesQuery({ ...DEFAULT_VIEW, span: '30d', bucket: 'hour' }, 'UTC').get('bucket')).toBe('hour')
    expect(outcomesQuery({ ...DEFAULT_VIEW, span: '14d', bucket: 'month' }, 'UTC').get('bucket')).toBe('auto')
  })
})

describe('figures as words', () => {
  it('prints the contract’s own check: 272 of 300 is 90.7 %, 86.8–93.5 %', () => {
    const r = wilsonFixture(272, 300)!
    expect(r).toEqual({ k: 272, n: 300, p: 0.9067, lo: 0.8684, hi: 0.9346 })
    expect(pct(r.p)).toBe('90.7 %')
    expect(interval(r)).toBe('86.8–93.5 %')
    expect(rateClause(r)).toBe('90.7 % (272 of 300 · 95 % 86.8–93.5 %)')
  })

  it('has no rate at all over nothing decided', () => {
    expect(wilsonFixture(0, 0)).toBeNull()
  })

  it('prints durations at the unit a reader uses', () => {
    expect(secs(48)).toBe('48s')
    expect(secs(660)).toBe('11m')
    expect(secs(7200)).toBe('2h')
    expect(secs(90_000)).toBe('1d 1h')
  })
})

describe('time in the server’s zone', () => {
  it('reads a bucket’s wall clock in the zone the server bucketed in, not the browser’s', () => {
    // Midnight in Bucharest is 21:00 the day before in UTC.
    expect(wallOf('2026-09-22T00:00:00+03:00', 'Europe/Bucharest')).toEqual({ date: '2026-09-22', hour: 0 })
    expect(wallOf('2026-09-22T00:00:00+03:00', 'UTC')).toEqual({ date: '2026-09-21', hour: 21 })
  })

  it('names a bucket in full: a day with its weekday, a week by its Monday', () => {
    const iso = '2026-09-21T00:00:00+03:00'
    expect(bucketName(iso, 'day', 'Europe/Bucharest')).toBe(`${dateLabel(iso, 'Europe/Bucharest')} · ${new Date(iso).toLocaleString(undefined, { timeZone: 'Europe/Bucharest', weekday: 'short' })}`)
    expect(bucketName(iso, 'week', 'Europe/Bucharest')).toBe(`week of ${dateLabel(iso, 'Europe/Bucharest')}`)
  })

  it('carries the date at each day boundary on an hourly axis, and thins the rest to its stride', () => {
    const start = Date.parse('2026-09-23T22:00:00+03:00')
    const keys = Array.from({ length: 30 }, (_, i) => new Date(start + i * 3_600_000).toISOString())
    const wide = axisLabels(keys, 'hour', 'Europe/Bucharest', 2)
    const midnight = keys.findIndex((k) => wallOf(k, 'Europe/Bucharest').hour === 0)
    expect(wide[midnight]).toBe(dateLabel(keys[midnight]!, 'Europe/Bucharest'))
    for (let i = 1; i < wide.length; i++) {
      expect(wide[i] !== '' && wide[i - 1] !== '', `columns ${i - 1} and ${i} are both labelled`).toBe(false)
    }
    // A phone: every third hour on the clock, and the days.
    const phone = axisLabels(keys, 'hour', 'Europe/Bucharest', 3, 3)
    phone.forEach((l, i) => {
      if (l === '') return
      const w = wallOf(keys[i]!, 'Europe/Bucharest')
      const isDate = l === dateLabel(keys[i]!, 'Europe/Bucharest')
      expect(isDate || w.hour % 3 === 0, `column ${i} (${w.hour}:00) is labelled "${l}"`).toBe(true)
    })
  })

  it('names an unread bucket with no count at all', () => {
    const b = { ...ledgerFixture().buckets[5]!, state: 'unread' as const, unread_reason: 'too_large' as const, succeeded: null, failed: null, cancelled: null, rate: null, submitted: null, ended: null }
    const said = bucketSaid(b, 'day', 'Europe/Bucharest')
    expect(said).toContain('not read, too large to read')
    expect(said.replace(bucketName(b.start, 'day', 'Europe/Bucharest'), '')).not.toMatch(/\d/)
  })
})

describe('what the route means by its payload (#196)', () => {
  it('refuses the ranges the route refuses: a future start and a span over its limit', () => {
    const today = '2026-09-25'
    expect(rangeRefusal('2026-09-01', '2026-09-10', today)).toBeNull()
    // A range that ends in the future is the route's to clamp, not a refusal.
    expect(rangeRefusal('2026-09-20', '2026-10-05', today)).toBeNull()
    expect(rangeRefusal('2026-09-26', '2026-09-27', today)).toBe('from is in the future')
    expect(rangeRefusal('2026-09-10', '2026-09-01', today)).toBe('from is after to')
    expect(rangeRefusal('', '2026-09-01', today)).toBe('choose both days')
    // Inclusive days: 22 Aug 2025 to 25 Sep 2026 is 400 of them; one more is refused.
    expect(MAX_SPAN_DAYS).toBe(400)
    expect(rangeRefusal('2025-08-22', '2026-09-25', today)).toBeNull()
    expect(rangeRefusal('2025-08-21', '2026-09-25', today)).toBe('at most 400 days')
  })

  it('names coverage.days in the rollup’s unit, by scope', () => {
    const d = ledgerFixture()
    expect(dayDocWords(d, 15)).toBe('UTC days')
    expect(dayDocWords(d, 1)).toBe('UTC day')
    const p = { ...d, scope: { kind: 'platform' as const, tenants: ['eng', 'research'], excluded: [], tenants_complete: true } }
    expect(dayDocWords(p, 30)).toBe('tenant-days')
    expect(dayDocWords(p, 1)).toBe('tenant-day')
  })

  it('calls a payload cached only when the route would have cached it', () => {
    const d = ledgerFixture()
    expect(cacheable(d)).toBe(true)
    expect(cacheable({ ...d, previous: null })).toBe(true)
    expect(cacheable({ ...d, totals: { ...d.totals, complete: false } })).toBe(false)
    expect(cacheable({ ...d, previous: { ...d.previous!, complete: false } })).toBe(false)
  })
})
