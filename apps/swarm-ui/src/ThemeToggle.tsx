import { useRef, useState, type KeyboardEvent } from 'react'
import { THEME_CHOICES, readTheme, setTheme, type ThemeChoice } from './theme'

const LABEL: Readonly<Record<ThemeChoice, string>> = { light: 'Light', dark: 'Dark', system: 'System' }

/** The arrow keys a native radio group moves by, and which way. */
const STEP: Readonly<Record<string, number>> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }

/**
 * Light / dark / system, in the spine panel's user footer. A radio group: one
 * choice is on, arrow keys and tab behave as a native one would, and the choice
 * is remembered per browser (theme.ts). System is the default and follows the OS.
 *
 * ONE TAB STOP, NOT THREE (visual QA V074, 2026-10-11). "Behave as a native
 * one would" was claimed and not done: each `role="radio"` was a plain button,
 * so Tab walked Light, Dark and System in turn and no arrow key did anything.
 * A native group is one stop -- the chosen radio -- and the arrows move the
 * choice and the focus together, wrapping at the ends; Home and End jump.
 */
export function ThemeToggle() {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme)
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const pick = (next: ThemeChoice) => {
    setChoice(next)
    setTheme(next)
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const at = THEME_CHOICES.indexOf(choice)
    const n = THEME_CHOICES.length
    const to =
      e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : STEP[e.key] !== undefined ? (at + STEP[e.key]! + n) % n : null
    if (to === null) return
    e.preventDefault()
    pick(THEME_CHOICES[to]!)
    refs.current[to]?.focus()
  }
  return (
    <div className="sk-theme" role="radiogroup" aria-label="Colour theme" onKeyDown={onKeyDown}>
      {THEME_CHOICES.map((c, i) => (
        <button
          key={c}
          ref={(el) => {
            refs.current[i] = el
          }}
          type="button"
          role="radio"
          aria-checked={choice === c}
          tabIndex={choice === c ? 0 : -1}
          className={choice === c ? 'is-on' : undefined}
          onClick={() => pick(c)}
        >
          {LABEL[c]}
        </button>
      ))}
    </div>
  )
}
