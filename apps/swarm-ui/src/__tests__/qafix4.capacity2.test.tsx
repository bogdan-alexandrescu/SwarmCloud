// VISUAL QA RUN 2 (wf_b6e2441b419f423a84eb, 2026-10-20, the fixture build of
// main at 75148351), LANE QAFIX4-CAPACITY-2 (part of #1680; box comment
// 6298212867).
//
//   capacity-009  Provider quota at 390: a throttled tenant card's amber
//                 accent bar spanned only State to 429s. The warn tone's rule
//                 is the half rule (`3px 46%`, centred), which on a 30px table
//                 row marks that row -- and on a ~200px stacked record marks
//                 the middle of it, starting under the name and stopping above
//                 Last 429 and Reported. At 390 the quota record's warn rule
//                 now runs the record's full height, and stays a different
//                 FORM from the bad tone's solid rule (dashed, not solid), so
//                 throttled and exhausted stay apart in greyscale.
//
// Each case renders the real screen and asks the cascade, through every sheet
// in the bundle's order (`painted()`, marks.ts), what it gives the rendered
// record. jsdom lays nothing out, so "full height of the record" is held as:
// the rule is on the element that holds the name and every field, and its
// size is the whole of that element.

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { QuotaState } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted, stripColours } from './marks'

const api = vi.hoisted(() => ({ loadAdminQuota: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { QuotaDetailScreen } = await import('../QuotaDetail')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const PHONE_LIGHT: CascadeEnv = { width: 390, theme: 'light' }

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

function atPhone(): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(max-width: 560px)',
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }))
}

const minutesAgo = (m: number): string => new Date(Date.now() - m * 60_000).toISOString()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function quota(over: Partial<QuotaState>): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: minutesAgo(1),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: minutesAgo(3),
    retry_after_seconds: null, success_count: 10, rate_limit_count: 2, effective_limit: 50, ...over,
  }
}

const ROWS: QuotaState[] = [
  quota({ tenant_id: 'eng', state: 'THROTTLED', retry_after_seconds: 30 }),
  quota({ tenant_id: 'ops', state: 'AVAILABLE', rate_limit_count: 0, last_429_at: null }),
  quota({ tenant_id: 'u-bogdan', state: 'EXHAUSTED', effective_limit: 0 }),
]

async function records(): Promise<Record<string, HTMLElement>> {
  api.loadAdminQuota.mockResolvedValue(ok({ quota: ROWS }))
  render(<QuotaDetailScreen />)
  const out: Record<string, HTMLElement> = {}
  for (const q of ROWS) {
    const [th] = await screen.findAllByRole('rowheader', { name: q.tenant_id }, WAIT)
    out[q.tenant_id] = th!.closest('tr') as HTMLElement
  }
  return out
}

// `background-image` alone: the row's 8% wash is a `background` colour, and
// the question here is only where the 3px RULE is drawn.
const image = (el: Element, env: CascadeEnv): string => painted(el, 'background-image', env) ?? 'none'

describe('capacity-009: at 390 a throttled quota record carries its warn rule down the whole record', () => {
  it('draws the rule on the record that holds the name and every field, at its full height', async () => {
    atPhone()
    const r = await records()
    const eng = r.eng!
    // The rendered table is the phone record, not the scroll table.
    expect(eng.closest('.quota-table')?.classList.contains('is-stacked')).toBe(true)
    expect(eng.className).toContain('is-warn')
    // The element the rule is on holds the name and the two fields the bar
    // stopped above -- the record's box, not a group of its rows.
    expect(eng.querySelector('th[scope="row"]')?.getAttribute('aria-label')).toBe('eng')
    expect(eng.querySelector('td[data-label="Last 429"]')).not.toBeNull()
    expect(eng.querySelector('td[data-label="Reported"]')).not.toBeNull()
    for (const env of [PHONE, PHONE_LIGHT]) {
      // MUTATION: drop the `.quota-table.is-stacked ... tr.is-warn` rule from
      // styles/capacity.css -- styles.css's stacked `3px 46%` half rule wins
      // again and the bar marks State to 429s only.
      expect(painted(eng, 'background-size', env), 'the warn rule covers part of the record').toBe('3px 100%')
      // MUTATION: give the capacity.css rule a background-image without the
      // warn hue -- the record keeps a rule, in the wrong colour.
      expect(image(eng, env)).toMatch(/var\(--warn\)/)
      // MUTATION: move the rule back onto the first cell (the name line) --
      // a 3px rule under the first letters of the tenant id.
      expect(image(eng.firstElementChild!, env)).toBe('none')
    }
  })

  it('keeps the warn rule a different form from the bad rule, so the two read apart in greyscale', async () => {
    atPhone()
    const r = await records()
    // MUTATION: make the phone warn rule the solid
    // `linear-gradient(var(--warn), var(--warn))` at 100% -- throttled and
    // exhausted become one shape once the colour is taken away.
    const form = (el: Element): string => `${stripColours(image(el, PHONE))} ${painted(el, 'background-size', PHONE)}`
    expect(painted(r['u-bogdan']!, 'background-size', PHONE)).toBe('3px 100%')
    expect(form(r.eng!), 'throttled and exhausted draw one shape').not.toBe(form(r['u-bogdan']!))
    // The state is still named in words, so the rule is not the only signal.
    expect(r.eng!.querySelector('td[data-label="State"]')?.textContent).toContain('throttled')
  })

  it('draws no rule on a tenant that is not throttled', async () => {
    atPhone()
    const r = await records()
    const ops = r.ops!
    // MUTATION: key the capacity.css rule on `tr` rather than `tr.is-warn` --
    // every quota record, healthy ones too, carries an amber rule.
    expect(ops.className).not.toMatch(/is-(warn|bad|paused)/)
    expect(image(ops, PHONE)).toBe('none')
    expect(image(ops.firstElementChild!, PHONE)).toBe('none')
  })
})

describe('capacity-009: the desktop table rule is unchanged', () => {
  it('keeps the half rule on the throttled row\'s first cell at 1440', async () => {
    const r = await records()
    const eng = r.eng!
    expect(eng.closest('.quota-table')?.classList.contains('is-scroll')).toBe(true)
    const first = eng.firstElementChild!
    // MUTATION: let the phone rule escape its `.is-stacked` scope (or its
    // 560px media block) -- the desktop row grows a rule of its own and its
    // first cell's half rule is joined by a second one.
    expect(image(eng, WIDE)).toBe('none')
    // MUTATION: change styles.css's table `tr.is-warn > :first-child` half
    // rule -- the desktop look this lane must not move.
    expect(image(first, WIDE)).toMatch(/var\(--warn\)/)
    expect(painted(first, 'background-size', WIDE)).toBe('3px 46%')
  })
})
