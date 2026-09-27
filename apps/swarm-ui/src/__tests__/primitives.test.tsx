// THE SHARED PRIMITIVES, DRIVEN DIRECTLY.
//
// `primitives.screens.test.tsx` proves every screen still draws its states
// through these; this file pins the states themselves, one component at a
// time, so a change to the one track that every screen now shares fails here
// by name rather than as five screens failing at once for one reason.
//
// It also pins the two pure readers the U8 lane added beside them:
// `leaseCoverage` (Holders.tsx) and `classifyEnvironment`'s third source
// (Brand.tsx).
//
// This file imports code that did not exist before the change it tests, so it
// cannot fail first on anything but an import. It is proven instead by the
// mutation commits recorded in the pull request, which change one branch of
// the shared track at a time and watch these go red.

import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { classifyEnvironment, envTreatment } from '../Brand'
import { leaseCoverage } from '../Holders'
import { Absent, Mark, Metric, UtilRow, UtilTrack } from '../primitives'
import type { LeasePage } from '../types'

function one(node: ReactElement): HTMLElement {
  const { container } = render(node)
  return container.firstElementChild as HTMLElement
}

describe('UtilTrack keeps its four states apart', () => {
  it('hatches a reading nobody took, with no fill and no tick', () => {
    const t = one(<UtilTrack pct={null} />)
    expect(t.className).toBe('ctl-util-track is-unknown')
    expect(t.children).toHaveLength(0)
  })

  it('draws a measured zero as the baseline tick, never as a zero-width fill', () => {
    const t = one(<UtilTrack pct={0} zeroTitle="Measured: 0 of 4." />)
    expect(t.className).toBe('ctl-util-track is-zero')
    expect(t.getAttribute('title')).toBe('Measured: 0 of 4.')
    expect(t.querySelector('.ctl-util-zero')?.getAttribute('aria-hidden')).toBe('true')
    expect(t.querySelector('.ctl-util-fill')).toBeNull()
  })

  it('fills a reading, grey unless a verdict says otherwise', () => {
    const t = one(<UtilTrack pct={37.5} />)
    expect(t.className).toBe('ctl-util-track')
    const fill = t.querySelector<HTMLElement>('.ctl-util-fill')!
    expect(fill.className).toBe('ctl-util-fill')
    expect(fill.style.width).toBe('37.5%')
    expect(t.querySelector('.ctl-util-over')).toBeNull()
  })

  it('does not round a small reading down to a measured zero', () => {
    // 1 unit of 300. A rounded 0 would hand the track a zero it did not have.
    const t = one(<UtilTrack pct={(1 / 300) * 100} />)
    expect(t.classList.contains('is-zero')).toBe(false)
    expect(t.querySelector('.ctl-util-fill')).not.toBeNull()
  })

  it('draws over the ceiling as the ceiling share plus the hatched excess', () => {
    const t = one(<UtilTrack pct={125} tone="is-bad" />)
    expect(t.querySelector<HTMLElement>('.ctl-util-fill.is-bad')!.style.width).toBe('80%')
    expect(t.querySelector<HTMLElement>('.ctl-util-over')!.style.width).toBe('20%')
  })

  it('carries exactly the four fill classes a tone names', () => {
    for (const tone of ['is-warn', 'is-bad', 'is-paused', 'ov-projected'] as const) {
      const fill = one(<UtilTrack pct={50} tone={tone} />).querySelector('.ctl-util-fill')!
      expect(fill.className).toBe(`ctl-util-fill ${tone}`)
    }
  })

  it('is a meter only when asked to be one, in every state', () => {
    expect(one(<UtilTrack pct={50} />).getAttribute('role')).toBeNull()
    for (const pct of [null, 0, 50, 150]) {
      const t = one(<UtilTrack pct={pct} meter={{ label: 'global: 2 of 4 units in use', now: 2, max: 4 }} />)
      expect(t.getAttribute('role')).toBe('meter')
      expect(t.getAttribute('aria-label')).toBe('global: 2 of 4 units in use')
    }
  })
})

describe('UtilRow draws the primitive row in its grid order', () => {
  it('name, track, figure, by -- and only the titles it was given', () => {
    const r = one(<UtilRow name={<b>eng</b>} nameTitle="eng · 1 unit" track={{ pct: 10 }} figure="1 / 10" by="global" />)
    expect(r.className).toBe('ctl-util')
    expect([...r.children].map((c) => c.className)).toEqual([
      'ctl-util-name',
      'ctl-util-track',
      'ctl-util-figure',
      'ctl-util-by',
    ])
    expect(r.children[0]!.getAttribute('title')).toBe('eng · 1 unit')
    expect(r.children[2]!.hasAttribute('title')).toBe(false)
    expect(r.children[3]!.hasAttribute('title')).toBe(false)
  })
})

describe('Metric', () => {
  it('draws the tile as a plain element with its tone as a class', () => {
    const m = one(<Metric label="Peak memory" value="not recorded" tone="absent" />)
    expect(m.tagName).toBe('DIV')
    expect(m.className).toBe('ctl-metric is-absent')
    expect(m.hasAttribute('aria-label')).toBe(false)
    expect(m.querySelector('.ctl-metric-value')?.textContent).toBe('not recorded')
  })

  it('makes the whole fact the doorway when it has somewhere to go', () => {
    const m = one(<Metric label="Running" value={3} unit="agents" href="#work/running" say="In flight." className="ov-tile" />)
    expect(m.tagName).toBe('A')
    expect(m.getAttribute('href')).toBe('#work/running')
    expect(m.className).toBe('ctl-metric ov-tile')
    expect(m.getAttribute('aria-label')).toBe('Running. In flight.')
    expect(m.querySelector('.ctl-metric-value')?.textContent).toBe('3agents')
  })

  it('draws a measured zero as a digit', () => {
    const m = one(<Metric label="Checkpoints" value={0} />)
    expect(m.querySelector('.ctl-metric-value')?.textContent).toBe('0')
  })

  it('publishes an explanation at the label and draws no glyph for it', () => {
    const m = one(<Metric label="Token cost" value="not reported" tone="absent" explain="token-cost" />)
    const label = m.querySelector('.ctl-metric-label')!
    const id = label.getAttribute('aria-describedby')
    expect(id).toBeTruthy()
    expect(label.querySelector(`[id="${id}"][data-help-description]`)).not.toBeNull()
    expect(label.querySelector('button')).toBeNull()
  })
})

describe('Absent', () => {
  it('names each kind of nothing with its own mark, inside the heading', () => {
    const cases = [
      ['zero', 'real zero', ''],
      ['failed', 'not read', 'is-failed'],
      ['partial', 'partial', 'is-partial'],
      ['admin', 'admin only', 'is-admin'],
    ] as const
    for (const [kind, word, cls] of cases) {
      const e = one(<Absent kind={kind} heading="Nothing here" say={`A ${kind} empty state.`} />)
      expect(e.className.trim()).toBe(cls === '' ? 'ctl-empty' : `ctl-empty ${cls}`)
      const mark = e.querySelector('h3 > .ctl-mark')
      expect(mark?.textContent).toBe(word)
      expect(mark?.getAttribute('aria-label')).toBe(`A ${kind} empty state.`)
      // Only a real zero is quiet; anything else is news.
      expect(e.getAttribute('role')).toBe(kind === 'zero' ? null : 'status')
      expect(e.querySelector('p')).toBeNull()
    }
  })

  it('carries a screen class for the in-card variant, and at most one sentence', () => {
    const e = one(
      <Absent kind="zero" heading="No pools exist" say="A real zero." className="ov-empty" foot="read 2m ago">
        One sentence.
      </Absent>,
    )
    expect(e.className).toBe('ctl-empty ov-empty')
    expect(e.querySelectorAll('p')).toHaveLength(1)
    expect(e.querySelector('.ctl-empty-foot')?.textContent).toBe('read 2m ago')
  })

  /**
   * §6.9's SHAPE IS FOUR THINGS AND THIS DREW THREE. Mark, heading, one
   * sentence -- and "a link out", which had no slot, so every screen that
   * wanted one hand-built a panel instead (Shell.tsx's `.state` box, the Help
   * page's unknown-topic panel). CH-10, CP-21, AH-17.
   *
   * MUTATION: accept `link` and render nothing for it. No anchor in the panel.
   */
  it('ends in a link out, after the one sentence, in the link treatment', () => {
    const e = one(
      <Absent kind="zero" heading="No unreleased leases" say="A real zero." link={{ href: '#capacity/pools', label: 'Pools' }}>
        Across every tenant.
      </Absent>,
    )
    const a = e.querySelector('a')
    expect(a, 'the empty state drew no link out').not.toBeNull()
    expect(a!.getAttribute('href')).toBe('#capacity/pools')
    expect(a!.textContent).toBe('Pools')
    expect(a!.className).toContain('ctl-link')
    // Still ONE paragraph: the link closes the sentence rather than adding a
    // second one, which is the thing §6.9 forbids.
    expect(e.querySelectorAll('p')).toHaveLength(1)
    expect(e.querySelector('p')!.textContent).toMatch(/^Across every tenant\.\s+Pools$/)
  })

  it('draws the link out alone when the heading already carries the fact', () => {
    const e = one(<Absent kind="zero" heading="Nothing here" say="A real zero." link={{ href: '#help', label: 'Help' }} />)
    expect(e.querySelectorAll('p')).toHaveLength(1)
    expect(e.querySelector('p > a.ctl-link')?.getAttribute('href')).toBe('#help')
  })
})

describe('Mark', () => {
  it('is two words on the surface and the sentence as its accessible name', () => {
    const m = one(<Mark kind="unread" say="The read failed." />)
    expect(m.className).toBe('ctl-mark is-unread')
    expect(m.getAttribute('role')).toBe('img')
    expect(m.getAttribute('aria-label')).toBe('The read failed.')
    expect(m.textContent).toBe('not read')
  })
})

// ---------------------------------------------------------------------------
// The readers
// ---------------------------------------------------------------------------

function page(over: Record<string, unknown>): LeasePage {
  return {
    leases: [],
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: '2026-09-24T10:00:00Z',
    active_only: true,
    tenant_id: null,
    units_held: 0,
    ...over,
  } as unknown as LeasePage
}

describe('leaseCoverage reads the count, and never takes an absence for a zero', () => {
  it('is complete only at a counted 0', () => {
    expect(leaseCoverage(page({ active_beyond_window: 0, truncated: false }))).toEqual({ kind: 'complete' })
  })

  it('is cut, with the count, when live leases were left out', () => {
    expect(leaseCoverage(page({ active_beyond_window: 14, truncated: true }))).toEqual({ kind: 'cut', beyond: 14 })
  })

  it('is cut without a count when only the flag says so', () => {
    expect(leaseCoverage(page({ truncated: true }))).toEqual({ kind: 'cut', beyond: null })
    // The flag outranks a contradicting 0 on an active-only read.
    expect(leaseCoverage(page({ truncated: true, active_beyond_window: 0 }))).toEqual({ kind: 'cut', beyond: null })
  })

  it('does not call a history window cut just because older documents exist', () => {
    // `truncated` on a history read is true for ever; the count is the answer.
    expect(
      leaseCoverage(page({ active_only: false, truncated: true, active_beyond_window: 0 })),
    ).toEqual({ kind: 'complete' })
  })

  it('is unreported when neither field arrived, or the count is not a count', () => {
    expect(leaseCoverage(page({}))).toEqual({ kind: 'unreported' })
    expect(leaseCoverage(page({ active_beyond_window: null }))).toEqual({ kind: 'unreported' })
    expect(leaseCoverage(page({ active_beyond_window: '3' }))).toEqual({ kind: 'unreported' })
    expect(leaseCoverage(page({ active_beyond_window: -1 }))).toEqual({ kind: 'unreported' })
  })
})

describe("classifyEnvironment's third source is the API's declared environment", () => {
  it('believes a declared API over the build and over the host', () => {
    expect(classifyEnvironment('dev', 'swarm.saga.xyz', { name: 'prod', declared: true })).toEqual({
      kind: 'production',
      name: 'prod',
      source: 'api',
      build: 'dev',
    })
    expect(classifyEnvironment(undefined, 'localhost', { name: 'Dev', declared: true })).toEqual({
      kind: 'nonprod',
      name: 'dev',
      source: 'api',
    })
  })

  it('ignores an undeclared API: a defaulted dev is not a dev', () => {
    expect(classifyEnvironment(undefined, 'swarm.saga.xyz', { name: 'dev', declared: false })).toEqual({
      kind: 'unknown',
      host: 'swarm.saga.xyz',
    })
    expect(classifyEnvironment(undefined, 'localhost', { name: 'dev', declared: false }).kind).toBe('local')
    expect(classifyEnvironment('staging', 'x', { name: '  ', declared: true })).toEqual({
      kind: 'nonprod',
      name: 'staging',
      source: 'build',
    })
  })

  it('keeps the casing rule: capitals exactly where the bar is', () => {
    const loud = envTreatment(classifyEnvironment(undefined, 'x', { name: 'production-eu', declared: true }))
    expect(loud.label).toBe('PRODUCTION-EU')
    expect(loud.bar).toBe(true)
    const quiet = envTreatment(classifyEnvironment('prod', 'x', { name: 'staging', declared: true }))
    expect(quiet.label).toBe('Staging')
    expect(quiet.bar).toBe(false)
    expect(quiet.explain).toContain('The build declared prod')
  })
})
