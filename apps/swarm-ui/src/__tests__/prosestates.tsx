// THE STATES GUARD (issue 439): the prose rule held in every state a reader
// reaches only by a click.
//
// THE RULE (the owner's, 2026-11-05, standing; docs/web-ui/design-system.md
// section 8): no explanatory prose on the surface. A sentence that explains
// goes behind a tooltip, an (i) (src/InfoTip.tsx) or a status tick; only a
// must-stay disclosure stays visible, ONE short line each (a write to a live
// system, production mode, who pays, could-not-check, nothing-auto-applies);
// an empty state is a short line plus its action. A long run is what
// proseruns.ts `longRuns` returns: a visible fact of RUN_WORDS (9) words or
// more, or of RUN_SENTENCES (2) sentences or more.
//
// HOW PROSE MOVES (as prose81.states.test.tsx did it): the surface keeps a
// short label or line, and the sentence stays word for word inside
// `[data-help-description]`, the tip InfoTip draws. The state then names the
// sentence in `moved`, and the check holds it off the surface and inside a
// tip; the short line it left names itself in `lines`. A test that asserted
// the sentence on the surface is re-pointed at the tip, never deleted.
//
// WHY THIS EXISTS. The page ratchet (proseapp.tsx) mounts every nav page and
// clicks nothing, so a sheet, a menu, a dialog, an expanded row, or an error
// or empty state reached by an action was counted by no test outside the
// files issue 81 named. A state here is rendered after its click, in the
// light theme and again in the dark one, and held to:
//   - `shows` in the document after the click and not before it: a click
//     that opened nothing FAILS, it is never a pass;
//   - more than a handful of words drawn;
//   - no long run but the `data` it shows (a value, a name, a log line: the
//     page's content, not its explanation);
//   - no arrowed text link (an action drawn as a link, where a button goes);
//   - each `lines` entry visible, each `moved` sentence behind a mark.
// What is measured is the state's own element (the first match of `shows`,
// or the whole document when `shows` is text), with the rail's `.sk-side`
// read like the page: a menu that opens inside the rail is the state, and
// the page ratchet's reason to skip the rail (its labels are on every page)
// is not this one's.
// A `walk` state is the App on the fixture build, walked as a ProsePage; it
// is rendered on the change's base too (prosebase.ts) and fails through
// proseDelta when its words, arrow links or long runs went up. A base whose
// App does not draw the opener is a new state: measured, logged, passed.
// A `mount` state is one screen on hand-built props, clicked by its `act`.
//
// THE REGISTRY: prosestates.<group>.tsx, one file per section, each slice
// prose.states.<group>.test.tsx a few lines long. prose.states.test.tsx's
// cover test reads every screen file that draws an opener and fails one that
// no state names; PENDING below lists the ones not yet registered, and it
// only shrinks: a step registers a file's states, clears what they show, and
// takes the file out of PENDING. The last step deletes PENDING.

import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { THEME_KEY } from '../theme'
import { load, measure, openAt, proseDelta, settle, useBase, type Measure, type Persona } from './proseapp'
import { holds } from './prose81states'
import { longRuns } from './proseruns'
import { screenLoaded } from './screenLoaded'

export const THEMES = ['light', 'dark'] as const
export type Theme = (typeof THEMES)[number]

/** A state that draws this many words or fewer drew nothing worth measuring: a click that opened an empty box. */
export const HANDFUL = 2

interface StateCommon {
  /** The state's name in a failure: `<page or screen>/<what was opened>`. */
  key: string
  /** The slice (prose.states.<group>.test.tsx) that visits it. */
  group: string
  /** The screen files under src (`Spine.tsx`, `components/Overlay.tsx`) whose opener this state renders. */
  files: string[]
  /** A selector, or a text, present only after the click; its first match is what is measured. */
  shows: string
  /** Must-stay lines (one short line each) that stay visible. */
  lines?: string[]
  /** Sentences taken off the surface: each is absent there and present inside `[data-help-description]`. */
  moved?: string[]
  /** Text the state shows as content, not explanation: a long run containing one of these is not counted. */
  data?: string[]
}

export interface StateWalk {
  /** Where the page is opened, as ProsePage.open. */
  open: string
  persona?: Persona
  /** The click (or clicks) that reach the state, once the page has loaded. */
  then: () => void | Promise<void>
}

export type ProseState = StateCommon &
  (
    | { walk: StateWalk; mount?: never; act?: never }
    | {
        walk?: never
        mount: () => ReactElement | Promise<ReactElement>
        /** The click that reaches the state; none for a state the mount draws at once (an error boundary's). */
        act?: () => void | Promise<void>
      }
  )

/**
 * The screen files that draw an opener (prose.states.test.tsx's cover test
 * says which) and that no state names yet, by the section whose step will
 * register them. It only shrinks; the last step deletes it.
 */
export const PENDING: readonly string[] = [
  // admin (Admin and Voice it)
  'AdminSchedules.tsx',
  'AdminSettings.tsx',
  'AdminSetupHealth.tsx',
  'AdminSetupSteps.tsx',
  'AdminSetupTeam.tsx',
  'RunnerSizes.tsx',
  'voice/CostPill.tsx',
  'voice/DebugTrace.tsx',
  'voice/Floating.tsx',
  'voice/Projects.tsx',
  'voice/Roster.tsx',
  'voice/SessionRecord.tsx',
  'voice/Settings.tsx',
  'voice/Stage.tsx',
  'voice/WorkingCard.tsx',
]

/** The first element `shows` selects, or null; null too when it is not a selector at all (it is text). */
function asSelector(shows: string): HTMLElement | null {
  try {
    return document.querySelector<HTMLElement>(shows)
  } catch {
    return null
  }
}

/** The first element matching `shows`, the body when it is text the body shows, or null. */
export function found(shows: string): HTMLElement | null {
  const el = asSelector(shows)
  if (el !== null) return el
  const text = (document.body.textContent ?? '').replace(/\s+/g, ' ')
  return text.includes(shows) ? document.body : null
}

/** `el` cloned with the rail's class taken off, so measure() and longRuns() read a state that opened inside the rail. */
function readable(el: HTMLElement): HTMLElement {
  const clone = el.cloneNode(true) as HTMLElement
  for (const node of [clone, ...clone.querySelectorAll<HTMLElement>('.sk-side')]) node.classList.remove('sk-side')
  return clone
}

/** What the state's element shows, the rail read as the page. */
export function measureState(el: HTMLElement): Measure {
  return measure(readable(el))
}

/** Every rule but the base's, on the state as it is drawn now; throws, quoting all it found, when one is broken. */
export function judge(state: ProseState, theme: Theme): Measure {
  const where = `${state.key} (${theme})`
  const el = found(state.shows)
  if (el === null) throw new Error(`${where}: the click opened nothing: "${state.shows}" is not in the document`)
  const shown = readable(el)
  const now = measure(shown)
  const broken: string[] = []
  // What the click drew counts its help copy too: a fold whose words all sit behind marks still opened.
  const drawn = (shown.textContent ?? '').split(/\s+/).filter(Boolean)
  if (drawn.length <= HANDFUL) broken.push(`${where} drew ${drawn.length} words (${JSON.stringify(drawn.join(' '))}): a click that opened an empty box`)
  const runs = longRuns(shown).filter((r) => !(state.data ?? []).some((d) => r.includes(d)))
  if (runs.length > 0)
    broken.push(
      `${where} shows ${runs.length} long runs: put each behind an (i) or cut it to one short line:\n` + runs.map((r) => `  ${JSON.stringify(r)}`).join('\n'),
    )
  if (now.arrowLinks.length > 0) broken.push(`${where} draws a text link with an arrow, make each a button: ${now.arrowLinks.map((t) => JSON.stringify(t)).join(', ')}`)
  try {
    holds(shown, { lines: state.lines, moved: state.moved, data: state.data })
  } catch (e) {
    if (runs.length === 0) broken.push(`${where}: ${e instanceof Error ? e.message : String(e)}`)
  }
  console.log(`PROSE STATE ${state.key} (${theme}): ${now.words} words, ${now.runs.length} long runs`)
  if (broken.length > 0) throw new Error(broken.join('\n'))
  return now
}

function setTheme(theme: Theme): void {
  localStorage.setItem(THEME_KEY, theme)
  document.documentElement.setAttribute('data-theme', theme)
}

function clearTheme(): void {
  document.documentElement.removeAttribute('data-theme')
}

function setPersona(persona: Persona | undefined): void {
  document.cookie =
    persona === undefined ? 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/' : `swarm_qa_persona=${persona}; path=/`
}

/** Before the click `shows` must not be there: a state already drawn is not a click-only state. */
function notYet(state: ProseState): void {
  if (found(state.shows) !== null) throw new Error(`${state.key}: "${state.shows}" is in the document before the click`)
}

/**
 * Mount `state` (one screen on hand-built props), click it, and judge it in
 * `theme`. Exported so prose.states.test.tsx can hold the harness to its own
 * rules on hand-built fixtures.
 */
export async function visitMount(state: ProseState, theme: Theme): Promise<Measure> {
  if (state.mount === undefined) throw new Error(`${state.key} is a walk state`)
  localStorage.clear()
  sessionStorage.clear()
  setTheme(theme)
  const { unmount } = render(await state.mount())
  try {
    await screenLoaded()
    await settle()
    if (state.act !== undefined) {
      notYet(state)
      // Not inside an outer act(): fireEvent acts on its own, and an act
      // around it would hold a two-click walk's first update back.
      await state.act()
      await screenLoaded()
      await settle()
    }
    return judge(state, theme)
  } finally {
    unmount()
    clearTheme()
  }
}

/**
 * Walk `state` on `tree`'s App in `theme` and measure what the click opened;
 * null when it opened nothing. With `rules`, it is also judged.
 */
async function visitWalk(src: string | null, state: ProseState, theme: Theme, rules: boolean): Promise<Measure | null> {
  const walk = state.walk
  if (walk === undefined) throw new Error(`${state.key} is a mount state`)
  // The persona first: the fixture build reads its cookie as its modules load.
  setPersona(walk.persona)
  const tree = await load(src)
  localStorage.clear()
  sessionStorage.clear()
  setTheme(theme)
  // The tree's own fixture clock, as proseapp.tsx's renderPage sets it.
  vi.setSystemTime(new Date(tree.clock))
  openAt(walk.open)
  const Shown = tree.App
  const { unmount } = render(<Shown />)
  try {
    await screenLoaded()
    await settle()
    if (rules) notYet(state)
    try {
      await walk.then()
    } catch (e) {
      // On the base the opener may not be there yet: a new state.
      if (rules) throw e
      return null
    }
    await screenLoaded()
    await settle()
    if (rules) return judge(state, theme)
    const el = found(state.shows)
    return el === null ? null : measureState(el)
  } finally {
    unmount()
    clearTheme()
    setPersona(undefined)
  }
}

/** Register one slice: an `it` per state, each judged in both themes and, for a walk, held to the base. */
export function proseStateGroup(group: string, states: readonly ProseState[]): void {
  const mine = states.filter((s) => s.group === group)
  if (mine.length === 0) throw new Error(`no click-only states in group ${group}`)
  if (mine.length !== states.length) throw new Error(`group ${group} was handed states of another group: ${states.filter((s) => s.group !== group).map((s) => s.key).join(', ')}`)

  describe(`the states guard: ${group}`, () => {
    const on = useBase(undefined)
    const visited: string[] = []

    for (const state of mine) {
      it(`${state.key} shows no long run, no arrow link and no new words, in both themes`, async () => {
        for (const theme of THEMES) {
          if (state.mount !== undefined) {
            await visitMount(state, theme)
            continue
          }
          const { base, chosen } = on()
          const now = await visitWalk(null, state, theme, true)
          if (now === null) throw new Error(`${state.key} (${theme}): the click opened nothing`)
          const was = await visitWalk(base.src, state, theme, false)
          const then = was === null ? 'new state' : `${was.words} words, ${was.arrows} arrows, ${was.runs.length} long runs`
          console.log(`PROSE STATE ${state.key} (${theme}) on ${chosen.sha.slice(0, 12)}: ${then}`)
          const up = proseDelta(`${state.key} (${theme})`, was, now)
          expect(up, up.join('\n')).toEqual([])
        }
        visited.push(state.key)
        console.log(`PROSE STATES ${group}: visited ${visited.length} of ${mine.length}: ${visited.join(', ')}`)
      }, 240_000)
    }
  })
}
