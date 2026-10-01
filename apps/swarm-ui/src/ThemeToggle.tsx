import { useState } from 'react'
import { THEME_CHOICES, readTheme, setTheme, type ThemeChoice } from './theme'

const LABEL: Readonly<Record<ThemeChoice, string>> = { light: 'Light', dark: 'Dark', system: 'System' }

/**
 * Light / dark / system, in the spine panel's user footer. A radio group: one
 * choice is on, arrow keys and tab behave as a native one would, and the choice
 * is remembered per browser (theme.ts). System is the default and follows the OS.
 */
export function ThemeToggle() {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme)
  const pick = (next: ThemeChoice) => {
    setChoice(next)
    setTheme(next)
  }
  return (
    <div className="sk-theme" role="radiogroup" aria-label="Colour theme">
      {THEME_CHOICES.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={choice === c}
          className={choice === c ? 'is-on' : undefined}
          onClick={() => pick(c)}
        >
          {LABEL[c]}
        </button>
      ))}
    </div>
  )
}
