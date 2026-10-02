/**
 * EVERY TASK STATE AND EVERY PARK REASON, DRAWN ONE WAY (components.html A,
 * "Task states" and "PARKED reasons"; brand.html §3's marks).
 *
 * Three forms of a state, one vocabulary (`STATE_MARK` in marks.tsx):
 *
 *   table  the mark in its hue and the word in ink          (`StateMark`)
 *   pill   a tinted pill, mark and word in the hue           (headers, detail)
 *   mark   the mark alone, the word as its accessible name   (the 64px strip)
 *
 * Shape names the state on its own; hue names the family -- teal for the four
 * states that hold capacity (CONTRACT invariant 1), violet for parked, red for
 * failed and dead-lettered, grey for the rest. Succeeded and cancelled stay
 * grey: a healthy platform is quiet.
 *
 * A PARK SAYS WHY AND UNTIL WHEN, in plain words, with the reason code in the
 * tooltip. The three reasons that need a person (`PARK_NEEDS_A_PERSON`) carry
 * the amber flag, so they are found without reading every row.
 */
import { MarkGlyph, StateMark, STATE_MARK } from '../marks'
import { PARK_NEEDS_A_PERSON, reasonCopy, type ParkReason, type TaskState } from '../types'
import { WarnGlyph } from './glyphs'

export { StateMark }

/** The state as a word: lower case, `DEAD_LETTERED` as `dead-lettered`. */
export function stateWord(state: TaskState): string {
  return state.toLowerCase().replace('_', '-')
}

export type StateForm = 'table' | 'pill' | 'mark'

export function StatePill({ state, form = 'pill', label, small = false }: { state: TaskState; form?: StateForm; label?: string; small?: boolean }) {
  if (form === 'table') return <StateMark state={state} label={label} />
  if (form === 'mark') return <StateMark state={state} label={label} bare />
  const { mark, hue } = STATE_MARK[state]
  return (
    <span className={`c-pill is-${hue}${small ? ' is-sm' : ''}`} data-mark={mark} data-hue={hue}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={mark} />
      </svg>
      {label ?? stateWord(state)}
    </span>
  )
}

/** The pill's words for each reason: what a reader is waiting on. */
export const PARK_WORD: Readonly<Record<ParkReason, string>> = {
  PROVIDER_QUOTA_EXHAUSTED: 'provider quota',
  PROVIDER_COOLDOWN: 'provider cooldown',
  PROVIDER_OUTAGE: 'provider outage',
  SCHEDULED_RETRY: 'retry scheduled',
  DEPENDENCY_INCOMPLETE: 'waiting on a step',
  MANUAL_PAUSE: 'paused by someone',
  BUDGET_EXHAUSTED: 'budget used up',
  CREDENTIAL_MISSING: 'credential missing',
}

function isParkReason(v: string): v is ParkReason {
  return Object.prototype.hasOwnProperty.call(PARK_WORD, v)
}

/**
 * A PARKED pill with its reason, and `until` when the writer knew it.
 *
 * A reason this client does not know is printed VERBATIM rather than hidden
 * (`reasonCopy`'s rule): the table is shipped client-side and will go stale,
 * and printing the raw value is what makes that visible. A park with no
 * reason recorded says so; it is never drawn as a reasonless pill.
 */
export function ParkPill({ reason, until }: { reason: ParkReason | string | null; until?: string | null }) {
  const word = reason === null ? 'reason not recorded' : isParkReason(reason) ? PARK_WORD[reason] : reason
  const person = reason !== null && PARK_NEEDS_A_PERSON.has(reason)
  const { mark, hue } = STATE_MARK.PARKED
  return (
    <span className="c-park" data-reason={reason ?? 'none'}>
      <span className={`c-pill is-${hue}`} data-mark={mark} data-hue={hue} title={reason === null ? undefined : `${reason} · ${reasonCopy(reason)}`}>
        <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
          <MarkGlyph mark={mark} />
        </svg>
        parked
        <span className="c-pr">{word}</span>
      </span>
      {person && (
        <span className="c-flag" data-flag="needs-a-person" title="needs a person">
          <WarnGlyph />
          <span className="sk-vh">needs a person</span>
        </span>
      )}
      {until != null && until !== '' && <span className="c-until">{until}</span>}
    </span>
  )
}
