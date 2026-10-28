/**
 * LANE QAFIX-REFRESH (part of #1680, the first console visual-QA run,
 * wf_9e9bf74834634db7bcc3, 2026-10-20): the head's refresh control.
 *
 *   QA-004  `⟳ reading…` measured 2.16-2.2:1 in light and 3.0:1 in dark: the
 *           control is a disabled button while the first read is in flight,
 *           and `.c-refresh[disabled] { opacity: .5 }` halved its
 *           `--text-dim` ink. Every page shows it on every first read.
 *   QA-031  axe label-content-name-mismatch: the button showed
 *           `⟳ every 5 s · read 0 s ago` and was named
 *           `Refresh · read 0 s ago · re-reads every 5 s`, so a voice user
 *           saying what they see did not reach it.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { RefreshControl } from '../Shell'
import type { CascadeEnv } from './cssgate'
import { painted, resolveColour, THEMES } from './marks'
import { contrast } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const NOW = 1_000_000

afterEach(() => {
  document.body.innerHTML = ''
})

/** What the reader sees, without the glyph a screen reader is not given. */
function seen(button: HTMLElement): string {
  return (button.textContent ?? '').replace(/^⟳\s*/, '')
}

describe('QA-004: a refresh that cannot be pressed yet is still legible', () => {
  it('paints `⟳ reading…` and `paused N` at full strength, at 4.5:1 on the page in both themes', () => {
    // Inside `.app`, as every page head is (App.tsx), so the sheet's
    // `:where(.app button:disabled)` fade is in the cascade being measured.
    render(
      <div className="app">
        <RefreshControl readAt={null} now={NOW} reading onRefresh={() => {}} />
        <RefreshControl readAt={null} now={NOW} pausedUntil={Date.now() + 30_000} onRefresh={() => {}} />
      </div>,
    )
    const marks = screen.getAllByRole('button')
    expect(marks.map(seen)).toEqual(['reading…', 'paused 30 s'])
    // `aria-disabled`, not `disabled` (a11y-keyboard-001, qafix3.focus.test.tsx):
    // a disabled button is blurred to <body> under the press that caused it.
    for (const el of marks) expect(el.getAttribute('aria-disabled')).toBe('true')
    for (const el of marks) {
      for (const theme of THEMES) {
        for (const width of [WIDE, PHONE]) {
          const env = { ...width, theme }
          // MUTATION: `.c-refresh[aria-disabled='true'] { opacity: .5 }` in
          // styles.css (the dim ink halved to 2.2:1 in light, 3.0:1 in dark), or
          // the control `disabled` again, so the `.app button:disabled` fade
          // paints it at .45.
          const opacity = painted(el, 'opacity', env)
          expect(opacity === null || Number(opacity) === 1, `${theme} ${width.width}: ${seen(el)} is faded to ${opacity}`).toBe(true)
          // MUTATION: a disabled `.c-refresh` given `--text-faint` (or any ink under
          // 4.5:1 on the page) instead of the fade.
          const ink = painted(el, 'color', env)
          expect(ink, `${theme}: no rule gives the refresh its ink`).not.toBeNull()
          const ratio = contrast(resolveColour(ink!, theme), resolveColour('var(--bg)', theme))
          expect(ratio, `${theme} ${width.width}: ${seen(el)} at ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5)
        }
      }
    }
  })
})

describe('QA-031: the refresh is named with the words it shows, first', () => {
  const cases: { label: string; props: Partial<Parameters<typeof RefreshControl>[0]>; shows: string }[] = [
    { label: 'a polling screen', props: { cadence: { base: 5_000, wait: 5_000 } }, shows: 'every 5 s · read 0 s ago' },
    { label: 'a screen read once', props: {}, shows: 'read once · 0 s ago' },
    { label: 'a backing-off poll', props: { cadence: { base: 5_000, wait: 20_000 } }, shows: 'every 20 s, backing off · read 0 s ago' },
    { label: 'a stale poll', props: { cadence: { base: 30_000, wait: 30_000 }, stale: true }, shows: 'not refreshed · every 30 s · read 0 s ago' },
  ]
  for (const c of cases) {
    it(`${c.label}: the accessible name starts with \`${c.shows}\``, () => {
      render(<RefreshControl readAt={NOW} now={NOW} onRefresh={() => {}} {...c.props} />)
      const button = screen.getByRole('button')
      expect(seen(button)).toBe(c.shows)
      // MUTATION: the name back to `Refresh · read N ago · re-reads every N` (the visible
      // words are not in it in order: axe label-content-name-mismatch, serious).
      expect(
        screen.queryByRole('button', { name: (name) => name.startsWith(c.shows) }),
        `named ${JSON.stringify(button.getAttribute('aria-label'))}`,
      ).not.toBeNull()
      // MUTATION: drop the press from the name (`aria-label={said}`): a screen reader
      // hears an age, not a control that renews it.
      expect(screen.getByRole('button', { name: `${c.shows} · refresh` })).toBe(button)
      // MUTATION: the glyph read aloud, ahead of the words (`⟳` not aria-hidden and
      // the name taken from the content): the name no longer starts with what is said.
      const glyph = [...button.querySelectorAll('[aria-hidden="true"]')].map((n) => n.textContent)
      expect(glyph).toEqual(['⟳'])
    })
  }

  it('the unread and reading states start their names with their visible word too', () => {
    // MUTATION: `aria-label` on the unread state back to anything not starting `Refresh`.
    const { unmount } = render(<RefreshControl readAt={null} now={NOW} onRefresh={() => {}} />)
    expect(seen(screen.getByRole('button'))).toBe('refresh')
    expect(screen.getByRole('button', { name: /^refresh\b/i })).not.toBeNull()
    unmount()
    // MUTATION: `aria-label` on the reading state back to anything not starting `Reading`.
    render(<RefreshControl readAt={null} now={NOW} reading onRefresh={() => {}} />)
    expect(seen(screen.getByRole('button'))).toBe('reading…')
    expect(screen.getByRole('button', { name: /^reading\b/i })).not.toBeNull()
  })
})
