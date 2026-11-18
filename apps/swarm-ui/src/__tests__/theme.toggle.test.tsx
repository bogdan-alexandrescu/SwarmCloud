// THE THEME TOGGLE (owner decision 2026-10-01): light / dark / system, in the
// spine panel's user footer, remembered per browser, system by default.
//
// Asserted on a rendered DOM and the real storage, not on the source: the
// component is mounted, clicked, and what it wrote to <html> and to
// localStorage is read back. Storage that throws is simulated by replacing
// `localStorage` with an object whose methods throw, which is what a private
// window does.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ThemeToggle } from '../ThemeToggle'
import { THEME_KEY, readTheme } from '../theme'

function root(): string | null {
  return document.documentElement.getAttribute('data-theme')
}

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.documentElement.removeAttribute('data-theme')
})

describe('the theme toggle', () => {
  it('offers light, dark and system as one radio group, with system on by default', () => {
    render(<ThemeToggle />)
    const group = screen.getByRole('radiogroup', { name: 'Colour theme' })
    expect(group).not.toBeNull()
    const radios = screen.getAllByRole('radio')
    expect(radios.map((r) => r.textContent)).toEqual(['Light', 'Dark', 'System'])
    expect(radios.map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'false', 'true'])
    // System writes nothing: the stylesheet follows the OS by itself.
    expect(root()).toBeNull()
  })

  it('puts the choice on <html> and remembers it', () => {
    render(<ThemeToggle />)
    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }))
    expect(root()).toBe('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(screen.getByRole('radio', { name: 'Dark' }).getAttribute('aria-checked')).toBe('true')

    fireEvent.click(screen.getByRole('radio', { name: 'Light' }))
    expect(root()).toBe('light')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light')
    expect(screen.getByRole('radio', { name: 'Dark' }).getAttribute('aria-checked')).toBe('false')
  })

  it('returns to the OS by removing the attribute and the memory', () => {
    window.localStorage.setItem(THEME_KEY, 'light')
    render(<ThemeToggle />)
    expect(screen.getByRole('radio', { name: 'Light' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'System' }))
    expect(root()).toBeNull()
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull()
  })

  it('opens on the remembered choice, and treats a stale value as system', () => {
    window.localStorage.setItem(THEME_KEY, 'dark')
    expect(readTheme()).toBe('dark')
    window.localStorage.setItem(THEME_KEY, 'solarized')
    expect(readTheme()).toBe('system')
  })

  it('still renders and still applies the choice when storage throws', () => {
    const refuse = () => {
      throw new Error('storage is blocked')
    }
    vi.stubGlobal('localStorage', { getItem: refuse, setItem: refuse, removeItem: refuse })
    expect(readTheme()).toBe('system')
    render(<ThemeToggle />)
    expect(screen.getByRole('radio', { name: 'System' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('radio', { name: 'Light' }))
    expect(root()).toBe('light')
  })
})
