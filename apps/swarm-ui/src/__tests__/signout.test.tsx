// @vitest-environment-options {"url": "https://swarm.example.test/overview"}
/**
 * SIGN OUT (owner request 2026-10-14): "a logout feature if I want to sign off
 * an account and use another. maybe right next to my username above the light
 * dark theme switch".
 *
 * This file runs on an IAP-shaped host (the docblock above): jsdom's default
 * `localhost` is the local-dev case, which hides the control, and is covered by
 * `signout.local.test.tsx`.
 *
 * MUTATIONS: drop the button from the panel's name row or the spine's foot;
 * drop a key from `clearUserState`'s list; navigate anywhere but the IAP
 * clear URL; show the control on a local host; remember nothing across the
 * round trip -- each turns a case red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'

import { SkyShell } from '../Spine'
import { TENANT_PREF, chooseTenant } from '../fetch'
import { THEME_KEY } from '../theme'
import {
  BROWSER_KEYS,
  SIGNED_OUT_KEY,
  SIGNOUT_PATH,
  USER_KEYS,
  accountChooserUrl,
  clearUserState,
  iapFronted,
  leave,
  returnedAs,
} from '../signoutState'
import { painted } from './marks'

const EMAIL = 'bogdan@saga.xyz' // the fixture `GET /v1/tenants/me` principal (api.ts fixtureMe)

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
})
afterEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

function shell() {
  return render(
    <SkyShell section="overview" tab="now" title="Overview" go={() => {}} foot={null}>
      <p>page</p>
    </SkyShell>,
  )
}

/** The shell once the fixture identity has landed. */
async function signedIn(): Promise<HTMLElement> {
  const { container } = shell()
  await waitFor(() => expect(container.querySelector('.sk-spine .sk-av')?.getAttribute('title')).toBe(EMAIL))
  return container
}

describe('where the control is', () => {
  for (const theme of ['dark', 'light'] as const) {
    it(`sits on the name row of the expanded panel, above the theme switch (${theme})`, async () => {
      document.documentElement.setAttribute('data-theme', theme)
      const c = await signedIn()
      const row = c.querySelector('.sk-pfoot .sk-pfrow')!
      const btn = row.querySelector<HTMLButtonElement>('button.sk-out')
      expect(btn, 'no Sign out button on the name row').not.toBeNull()
      expect(btn!.getAttribute('aria-label')).toBe('Sign out')
      expect(btn!.textContent).toContain('Sign out')
      // Above the theme switch: the row comes first in the footer.
      const foot = c.querySelector('.sk-pfoot')!
      const theme_ = foot.querySelector('.sk-theme')!
      expect(row.compareDocumentPosition(theme_) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      // Painted in this theme's tokens: a colour and a hairline both resolve.
      expect(painted(btn!, 'color', { width: 1440, theme })).not.toBeNull()
      expect(painted(btn!, ['border-top-color', 'border-color', 'border'], { width: 1440, theme })).not.toBeNull()
    })
  }

  it('is an icon with an aria-label at phone width: the word is not drawn', async () => {
    const c = await signedIn()
    const label = c.querySelector('.sk-pfoot button.sk-out .sk-out-l')!
    expect(label.textContent).toBe('Sign out')
    expect(painted(label, 'display', { width: 1440 })).not.toBe('none')
    expect(painted(label, 'display', { width: 390 })).toBe('none')
    expect(c.querySelector('.sk-pfoot button.sk-out svg')).not.toBeNull()
  })

  for (const theme of ['dark', 'light'] as const) {
    it(`is an icon at the foot of the collapsed spine (${theme})`, async () => {
      document.documentElement.setAttribute('data-theme', theme)
      window.localStorage.setItem('swarm.shell.collapsed', '1')
      const c = await signedIn()
      expect(c.querySelector('.sk-app.is-collapsed')).not.toBeNull()
      expect(c.querySelector('.sk-panel'), 'collapsed, the panel is not drawn').toBeNull()
      const btn = c.querySelector<HTMLButtonElement>('.sk-spine button.sk-out')
      expect(btn, 'no Sign out on the collapsed spine').not.toBeNull()
      expect(btn!.getAttribute('aria-label')).toBe('Sign out')
      expect(btn!.querySelector('.sk-out-l'), 'the spine has no room for the word').toBeNull()
      // At the spine's foot, its last control: the avatar it sat beside is
      // the Profile item at the top since NAV-PROFILE (#1633).
      expect(btn!.nextElementSibling, 'something follows Sign out at the foot').toBeNull()
      // The spine is one blue in both themes, so its ink is a fixed light value.
      expect(painted(btn!, 'color', { width: 1440, theme })).not.toBeNull()
    })
  }

  it('has no keyboard shortcut: no key alone signs anyone out', async () => {
    const c = await signedIn()
    const assign = vi.spyOn(leave, 'to').mockImplementation(() => {})
    for (const key of ['s', 'S', 'o', 'l', 'q', 'Escape', 'Enter']) fireEvent.keyDown(document.body, { key })
    expect(assign).not.toHaveBeenCalled()
    expect(c.querySelector('button.sk-out')!.getAttribute('aria-keyshortcuts')).toBeNull()
  })
})

describe('what Sign out does', () => {
  it("clears this console's per-user keys, keeps the browser's display ones, then goes to IAP's clear URL", async () => {
    const c = await signedIn()
    chooseTenant('platform')
    const kept = BROWSER_KEYS.map((k) => [k, `kept:${k}`] as const)
    for (const [k, v] of kept) window.localStorage.setItem(k, v)
    for (const k of USER_KEYS) window.localStorage.setItem(k, 'mine')
    window.localStorage.setItem('swarm.workflows.recent:platform', '[]')
    window.localStorage.setItem('swarm.workflows.newest:platform', '[]')
    window.localStorage.setItem('another-app.session', 'theirs')
    window.sessionStorage.setItem('swarm.something', 'mine')
    window.sessionStorage.setItem('another-app.tab', 'theirs')
    const assign = vi.spyOn(leave, 'to').mockImplementation(() => {})

    fireEvent.click(c.querySelector('.sk-pfoot button.sk-out')!)

    expect(window.localStorage.getItem(TENANT_PREF), 'the tenant switcher choice survived').toBeNull()
    for (const k of USER_KEYS) expect(window.localStorage.getItem(k), k).toBeNull()
    expect(window.localStorage.getItem('swarm.workflows.recent:platform')).toBeNull()
    expect(window.localStorage.getItem('swarm.workflows.newest:platform')).toBeNull()
    expect(window.sessionStorage.getItem('swarm.something')).toBeNull()
    // Not ours: never touched.
    expect(window.localStorage.getItem('another-app.session')).toBe('theirs')
    expect(window.sessionStorage.getItem('another-app.tab')).toBe('theirs')
    for (const [k, v] of kept) expect(window.localStorage.getItem(k), k).toBe(v)
    // Who left, for the return trip (see `returnedAs`).
    expect(window.sessionStorage.getItem(SIGNED_OUT_KEY)).toBe(EMAIL)
    expect(assign).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledWith(SIGNOUT_PATH)
  })

  it('goes to the documented IAP query parameter, never to Google sign-out', () => {
    expect(SIGNOUT_PATH).toBe('/?gcp-iap-mode=CLEAR_LOGIN_COOKIE')
    expect(SIGNOUT_PATH).not.toContain('accounts.google.com')
    expect(SIGNOUT_PATH.toLowerCase()).not.toContain('logout')
  })

  it('clears even when storage refuses', () => {
    const refuse = () => {
      throw new Error('storage is blocked')
    }
    const blocked = { length: 0, key: refuse, getItem: refuse, setItem: refuse, removeItem: refuse, clear: refuse } as unknown as Storage
    expect(() => clearUserState(blocked, blocked)).not.toThrow()
  })
})

describe('the way back in', () => {
  it('builds the account chooser to return through the IAP clear URL', () => {
    const url = new URL(accountChooserUrl('https://swarm.example.test'))
    expect(url.origin).toBe('https://accounts.google.com')
    expect(url.pathname).toBe('/AccountChooser')
    expect(url.searchParams.get('continue')).toBe('https://swarm.example.test/?gcp-iap-mode=CLEAR_LOGIN_COOKIE')
  })

  it('tells the same account coming back from a different one', () => {
    window.sessionStorage.setItem(SIGNED_OUT_KEY, EMAIL)
    expect(returnedAs(EMAIL)).toBe('same')
    expect(returnedAs('someone@saga.xyz')).toBe('other')
    // A different account is the sign-out working: nothing more to say.
    expect(window.sessionStorage.getItem(SIGNED_OUT_KEY)).toBeNull()
    expect(returnedAs(EMAIL)).toBeNull()
  })

  it('offers another account when Google silently signed the same one back in', async () => {
    window.sessionStorage.setItem(SIGNED_OUT_KEY, EMAIL)
    const c = await signedIn()
    const banner = await waitFor(() => {
      const b = c.querySelector('.sk-back')
      expect(b).not.toBeNull()
      return b!
    })
    expect(banner.textContent).toContain(EMAIL)
    const link = banner.querySelector<HTMLAnchorElement>('a[href^="https://accounts.google.com/AccountChooser"]')
    expect(link, 'no way to pick another account').not.toBeNull()
    // Staying is a choice too, and it ends the question.
    const stay = Array.from(banner.querySelectorAll('button')).find((b) => b.textContent?.startsWith('Stay'))!
    fireEvent.click(stay)
    expect(c.querySelector('.sk-back')).toBeNull()
    expect(window.sessionStorage.getItem(SIGNED_OUT_KEY)).toBeNull()
  })
})

describe('the storage this console owns', () => {
  // Every `swarm.` key a source file declares is either per-user (cleared on
  // sign-out) or a per-browser display preference (kept). A new key that is
  // neither fails here, so nobody adds per-user state that outlives sign-out
  // by forgetting this list.
  const sources = import.meta.glob(['../**/*.ts', '../**/*.tsx', '!../__tests__/**', '!../signoutState.ts'], {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  it('classifies every swarm. key the source declares', () => {
    const declared = new Set<string>()
    for (const text of Object.values(sources)) {
      for (const m of text.matchAll(/(?:=|:)\s*'(swarm\.[a-z][\w.-]*)'/g)) declared.add(m[1]!)
    }
    expect(declared.size, 'the scan found no keys: it did not run').toBeGreaterThan(10)
    const known = new Set<string>([...USER_KEYS, ...BROWSER_KEYS])
    const unclassified = [...declared].filter((k) => !known.has(k))
    expect(unclassified).toEqual([])
    expect(USER_KEYS).toContain(TENANT_PREF)
    expect(BROWSER_KEYS).toContain(THEME_KEY)
  })
})

describe('iapFronted', () => {
  it('is false for this machine and bare addresses, true for a named host', () => {
    for (const h of ['localhost', '127.0.0.1', '0.0.0.0', '[::1]', 'box.local', 'ui.localhost', '192.168.1.20']) expect(iapFronted(h), h).toBe(false)
    for (const h of ['swarm.saga.xyz', 'swarm.example.test']) expect(iapFronted(h), h).toBe(true)
  })
})
