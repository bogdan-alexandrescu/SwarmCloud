/**
 * LANE QAFIX3-FOCUS-LOOK (part of #1680; the follow-up QAFIX3-FOCUS, PR 1864,
 * asked for).
 *
 * a11y-keyboard-001 made two controls `aria-disabled="true"` instead of
 * `disabled` while a read is in flight, so they keep keyboard focus
 * (qafix3.focus.test.tsx). `:disabled` stopped matching them, and with it the
 * look the disabled state had: the head's refresh drew a pointer and lit up on
 * hover, and Accounts' `exchanging…` drew at full strength with a pointer.
 *
 * What holds now: an `aria-disabled` control draws exactly as its `:disabled`
 * rule drew -- the head's refresh at full strength (QA-004) with
 * `cursor: not-allowed` and no hover ink, the Accounts action at .4 with
 * `cursor: not-allowed` and no hover change.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { RefreshControl } from '../Shell'
import type { CascadeEnv } from './cssgate'
import { painted, THEMES } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const NOW = 1_000_000

afterEach(() => {
  document.body.innerHTML = ''
})

describe("the head's refresh, aria-disabled, draws as `.c-refresh[disabled]` did", () => {
  it('`⟳ reading…` and `paused 30 s`: full strength, `cursor: not-allowed`, and no hover ink', () => {
    // Inside `.app`, as every page head is, so the sheet's button floor is in
    // the cascade being measured.
    render(
      <div className="app">
        <RefreshControl readAt={null} now={NOW} reading onRefresh={() => {}} />
        <RefreshControl readAt={null} now={NOW} pausedUntil={Date.now() + 30_000} onRefresh={() => {}} />
      </div>,
    )
    const marks = screen.getAllByRole('button')
    expect(marks).toHaveLength(2)
    for (const el of marks) {
      expect(el.getAttribute('aria-disabled')).toBe('true')
      expect(el.hasAttribute('disabled')).toBe(false)
      for (const theme of THEMES) {
        for (const width of [WIDE, PHONE]) {
          const env = { ...width, theme }
          const at = `${theme} ${width.width}: ${el.textContent}`
          // MUTATION: drop `.c-refresh[aria-disabled='true']` from the
          // `.c-refresh[disabled]` rule: the cursor falls back to the base
          // rule's `pointer`, inviting a press that does nothing.
          expect(painted(el, 'cursor', env), at).toBe('not-allowed')
          // MUTATION: `opacity: .5` on the aria-disabled refresh (QA-004's
          // fade, which halved `--text-dim` to 2.2:1 in light).
          const opacity = painted(el, 'opacity', env)
          expect(opacity === null || Number(opacity) === 1, `${at} is faded to ${opacity}`).toBe(true)
          // MUTATION: drop `:not([aria-disabled='true'])` from
          // `.c-refresh:not(:disabled):hover`: the control lights up to
          // `--text` under the pointer as if a press would be taken.
          const resting = painted(el, 'color', env)
          expect(resting, `${at}: no rule gives the refresh its ink`).not.toBeNull()
          expect(painted(el, 'color', { ...env, states: ['hover'] }), `${at} on hover`).toBe(resting)
        }
      }
    }
  })

  it('a refresh that can be pressed still points and still lights up on hover', () => {
    // The control: the rule above is keyed on aria-disabled, not on every refresh.
    render(
      <div className="app">
        <RefreshControl readAt={null} now={NOW} onRefresh={() => {}} />
      </div>,
    )
    const el = screen.getByRole('button')
    expect(el.hasAttribute('aria-disabled')).toBe(false)
    // MUTATION: `.c-refresh { cursor: not-allowed }` for every refresh.
    expect(painted(el, 'cursor', WIDE)).toBe('pointer')
    // MUTATION: the hover rule removed outright rather than narrowed.
    expect(painted(el, 'color', { ...WIDE, states: ['hover'] })).toBe('var(--text)')
  })
})

/**
 * Accounts' `Refresh this account`, drawn as Accounts.tsx draws it: the
 * panel's direct-child button, `aria-disabled` while it exchanges
 * (qafix3.focus.test.tsx asserts the attribute on the rendered screen).
 */
function accountAction(sending: boolean): HTMLElement {
  const app = document.createElement('div')
  app.className = 'app'
  const action = document.createElement('div')
  action.className = 'acct-action'
  const button = document.createElement('button')
  button.type = 'button'
  if (sending) button.setAttribute('aria-disabled', 'true')
  button.textContent = sending ? 'exchanging…' : 'Refresh this account'
  action.appendChild(button)
  app.appendChild(action)
  document.body.appendChild(app)
  return button
}

describe("Accounts' `exchanging…`, aria-disabled, draws as `.acct-action > button:disabled` did", () => {
  it('opacity .4, `cursor: not-allowed`, and no hover change', () => {
    const el = accountAction(true)
    for (const theme of THEMES) {
      for (const width of [WIDE, PHONE]) {
        const env = { ...width, theme }
        const at = `${theme} ${width.width}`
        // MUTATION: drop `.acct-action > button[aria-disabled='true']` from the
        // `:disabled` rule: `exchanging…` draws at full strength.
        expect(painted(el, 'opacity', env), at).toBe('.4')
        // MUTATION: the same, seen as the cursor: back to the panel button's `pointer`.
        expect(painted(el, 'cursor', env), at).toBe('not-allowed')
        // MUTATION: drop `:not([aria-disabled='true'])` from
        // `.acct-action > button:not(:disabled):hover`: the faded button's ink
        // and border light up under the pointer.
        const hover = { ...env, states: ['hover'] }
        expect(painted(el, 'color', hover), `${at} ink on hover`).toBe(painted(el, 'color', env))
        expect(painted(el, ['border-color', 'border'], hover), `${at} border on hover`).toBe(painted(el, ['border-color', 'border'], env))
      }
    }
  })

  it('`Refresh this account`, pressable, is not faded and still lights up on hover', () => {
    const el = accountAction(false)
    // MUTATION: the fade keyed on every panel button rather than on aria-disabled.
    expect(painted(el, 'opacity', WIDE)).toBeNull()
    expect(painted(el, 'cursor', WIDE)).toBe('pointer')
    // MUTATION: the hover rule removed outright rather than narrowed.
    expect(painted(el, 'color', { ...WIDE, states: ['hover'] })).toBe('var(--text)')
  })
})
