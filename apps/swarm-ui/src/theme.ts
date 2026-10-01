/**
 * THE THEME CHOICE (owner decision 2026-10-01): light, dark or system, remembered
 * per browser, system by default.
 *
 * System writes no attribute at all, so `styles.css` follows the OS through its
 * `prefers-color-scheme` block. Light and dark write `data-theme` on `<html>`:
 * `:root[data-theme='light']` carries the light palette on any OS, and dark is the
 * base `:root`, which the light media block excludes under
 * `:root:not([data-theme='dark'])`.
 *
 * Every storage access is inside try/catch: storage throws in a private window and
 * is absent in a preview, and the console must render, in system mode, without it.
 */
export const THEME_KEY = 'swarm.theme'

export const THEME_CHOICES = ['light', 'dark', 'system'] as const
export type ThemeChoice = (typeof THEME_CHOICES)[number]

export function isThemeChoice(value: unknown): value is ThemeChoice {
  return typeof value === 'string' && (THEME_CHOICES as readonly string[]).includes(value)
}

/** The remembered choice, or `system` for nothing, a stale value, or storage that throws. */
export function readTheme(): ThemeChoice {
  try {
    const stored = globalThis.localStorage?.getItem(THEME_KEY)
    return isThemeChoice(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

/** Put the choice on the document. `system` removes the attribute. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
}

/** Remember the choice and apply it. A refused write still applies for this page. */
export function setTheme(choice: ThemeChoice): void {
  try {
    if (choice === 'system') globalThis.localStorage?.removeItem(THEME_KEY)
    else globalThis.localStorage?.setItem(THEME_KEY, choice)
  } catch {
    /* kept in memory for this page only */
  }
  applyTheme(choice)
}
