/**
 * VISUAL QA RE-RUN 2026-10-16 (#1427), lane R17, the shell's half: the
 * failure panel, the loading skeleton and the page head.
 *
 * V198  A 403 on a read offered Try again, which can only be refused the same
 *       way; V007 removed it for a 404 but not for a 403.
 * V008  The nested failure panel speaks at 13px, but `FailedPanel`'s
 *       admin-gate branch carried no scale class, so the Members admin-only
 *       notice inside its card stayed at the 16px lead size.
 * V063  The loading skeleton was one generic list card on every screen: it
 *       resembled none of Accounts (list + detail), the two-column Submit
 *       forms or People's table, so each jumped when its data landed. A
 *       `Screen` names its shape now (`shape`), and the skeleton draws it.
 * V240  A long page title pushed the head's freshness stamp down to
 *       "read 0 s …". The title block already shrinks and clamps to two lines
 *       (styles/names.css, walkthrough C), but the actions shrank beside it,
 *       in proportion, so the stamp was ellipsized too. The actions keep
 *       their width now and only the title gives way.
 *
 * Each `it` names the mutation that turns it red.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ApiError, Result } from '../fetch'
import { FailedPanel, PageHead, Screen, SkeletonRows } from '../Shell'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

const refused: ApiError = { kind: 'forbidden', httpStatus: 403, code: 'forbidden', message: 'not allowed to read pools' }
const gated: ApiError = { kind: 'admin_required', httpStatus: 403, code: 'admin_required', message: 'admin group membership required' }

/** A read that never lands, so a `Screen` stays in its loading state. */
const pending = <T,>(): Promise<Result<T>> => new Promise<Result<T>>(() => undefined)

/** Whether a `flex` shorthand lets the box shrink: `none`, `auto` or the three-value form. */
function shrinks(v: string | null): boolean {
  if (v === null) return true // no rule: the initial `flex: 0 1 auto`
  if (v === 'none') return false
  if (v === 'auto') return true
  const parts = v.trim().split(/\s+/)
  if (parts.length !== 3) throw new Error(`write \`flex\` as three values, not \`${v}\``)
  return parts[1] !== '0'
}

describe('V198: a 403 on a read is an answer, not a failed read', () => {
  it('offers no Try again on a forbidden read', () => {
    render(<FailedPanel error={refused} onRetry={() => undefined} />)
    expect(document.querySelector('.state.failed')!.textContent).toContain('not allowed to read pools')
    // MUTATION: `answered = !wrote && error.kind === 'not_found'` (main) draws it.
    expect(screen.queryByRole('button', { name: 'Try again' }), 'a retry that can only be refused again').toBeNull()
  })

  it('offers none on a Screen whose read was refused either', async () => {
    const failed: Result<{ n: number }> = { status: 'error', error: refused }
    render(
      <Screen<{ n: number }> title="Pools" load={async () => failed}>
        {() => null}
      </Screen>,
    )
    await screen.findByText('not allowed to read pools')
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })
})

describe('V008: the admin gate speaks at the size of what holds it', () => {
  it('is nested-scaled by default and page-scaled under a Screen', () => {
    render(<FailedPanel error={gated} onRetry={() => undefined} />)
    // MUTATION: `<div className="state admin-gate" ...>` without `${scale}` (main).
    expect(document.querySelector('.state.admin-gate')!.classList.contains('is-nested')).toBe(true)
    cleanup()
    render(<FailedPanel error={gated} onRetry={() => undefined} page />)
    expect(document.querySelector('.state.admin-gate')!.classList.contains('is-page')).toBe(true)
  })

  it('paints the nested gate’s sentences at 13px and its HTTP line at micro', () => {
    const p = build('div.state.admin-gate.is-nested > p', hosts)
    expect(painted(p, 'font-size', WIDE)).toBe('var(--t-meta)')
    const page = build('div.state.admin-gate.is-page > p', hosts)
    expect(painted(page, 'font-size', WIDE)).toBe('var(--t-lead)')
    const http = build('div.state.admin-gate.is-nested > p.checked-at', hosts)
    expect(painted(http, ['font-size', 'font'], WIDE)).toContain('var(--t-micro)')
  })
})

describe('V063: the loading skeleton is the shape the screen loads into', () => {
  it('keeps the one list card by default', () => {
    render(<SkeletonRows />)
    expect(document.querySelectorAll('.ctl-skel-card')).toHaveLength(1)
  })

  it('draws a list beside a detail for a split screen (Accounts)', () => {
    // MUTATION: `SkeletonRows` ignoring `shape` (main) draws one card.
    render(<SkeletonRows shape="split" />)
    const wrap = document.querySelector('.ctl-skel.is-split')
    expect(wrap, 'the split wrapper').not.toBeNull()
    expect(wrap!.querySelectorAll(':scope > .ctl-skel-card')).toHaveLength(2)
    expect(wrap!.querySelector('.ctl-skel-card.is-detail'), 'the detail pane').not.toBeNull()
  })

  it('lays the split out as Accounts does: 380px list and the rest, the detail alone off a phone', () => {
    const wrap = build('div.ctl-skel.is-split', hosts)
    // MUTATION: drop the `.ctl-skel.is-split` grid rule.
    expect(painted(wrap, 'grid-template-columns', WIDE)).toBe('minmax(0, 380px) minmax(0, 1fr)')
    const detail = build('div.ctl-skel.is-split > section.ctl-card.ctl-skel-card.is-detail', hosts)
    expect(painted(detail, 'display', PHONE), 'a phone shows the list first, never both').toBe('none')
    expect(painted(detail, 'display', WIDE)).not.toBe('none')
  })

  it('draws a form beside its send panel for a Submit form', () => {
    render(<SkeletonRows shape="form" />)
    const wrap = document.querySelector('.ctl-skel.is-form')
    expect(wrap, 'the form wrapper').not.toBeNull()
    expect(wrap!.querySelectorAll('.ctl-skel-field').length, 'label and field pairs').toBeGreaterThanOrEqual(3)
    expect(wrap!.querySelector('.ctl-skel-card.is-send'), 'the send panel').not.toBeNull()
    const grid = build('div.ctl-skel.is-form', hosts)
    expect(painted(grid, 'grid-template-columns', WIDE)).toBe('minmax(0, 1fr) 300px')
  })

  it('draws a column head over the rows for a table screen (People)', () => {
    render(<SkeletonRows shape="table" />)
    const card = document.querySelector('.ctl-skel.is-table .ctl-skel-card')
    expect(card).not.toBeNull()
    expect(card!.querySelectorAll('.ctl-skel-row.is-head'), 'one column head').toHaveLength(1)
    expect(card!.querySelector('.ctl-skel-row.is-head')!.querySelectorAll('.ctl-skel-bar').length).toBeGreaterThan(1)
  })

  it('is chosen by a Screen’s `shape` while its first read is in flight', () => {
    render(
      <Screen<{ n: number }> title="Accounts" shape="split" load={pending}>
        {() => null}
      </Screen>,
    )
    // MUTATION: `Screen` drawing `<SkeletonRows />` whatever its `shape`.
    expect(document.querySelector('.ctl-skel.is-split')).not.toBeNull()
  })
})

describe('V240: a long title gives way before the freshness stamp does', () => {
  it('renders the whole title in its tooltip', () => {
    const long = 'A schedule named at length '.repeat(5).trim()
    render(<PageHead title={long}>{null}</PageHead>)
    expect(screen.getByRole('heading', { level: 1 }).getAttribute('title')).toBe(long)
  })

  it('lets the title block shrink and keeps the actions at their width', () => {
    const head = build('div.c-phead > div.head', hosts)
    // MUTATION: `.c-phead > div.head { flex: none }` (names.css).
    expect(shrinks(painted(head, 'flex', WIDE)), 'the title block shrinks').toBe(true)
    expect(painted(head, 'min-width', WIDE)).toBe('0')
    const acts = build('div.c-phead > div.c-acts', hosts)
    // MUTATION: `.c-phead > .c-acts { flex: 0 1 auto }` (main).
    expect(shrinks(painted(acts, 'flex', WIDE)), 'the actions keep their width').toBe(false)
    // ...and never outgrow the row on a phone, where they wrap under the title.
    expect(painted(acts, 'max-width', PHONE)).toBe('100%')
  })

  it('clamps the title to two lines with an ellipsis rather than clipping the stamp', () => {
    const h1 = build('div.c-phead > div.head > h1', hosts)
    expect(painted(h1, 'min-width', WIDE)).toBe('0')
    expect(painted(h1, 'overflow', WIDE)).toBe('hidden')
    expect(painted(h1, 'display', WIDE)).toBe('-webkit-box')
    expect(painted(h1, '-webkit-box-orient', WIDE)).toBe('vertical')
    expect(painted(h1, '-webkit-line-clamp', WIDE)).toBe('2')
    expect(painted(h1, 'overflow-wrap', WIDE), 'an unbroken title breaks inside its box').toBe('anywhere')
  })
})
