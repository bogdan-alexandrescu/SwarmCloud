import { NamedMark } from './components'

/**
 * An ACCOUNT's state, drawn (brand.html §3; audit #503).
 *
 * AN ACCOUNT STATE IS NOT A TASK STATE, so it never borrows a task's mark. The
 * task marks answer "how far along is this attempt, and does it hold
 * capacity"; an account is not an attempt and holds nothing. So:
 *
 *   AVAILABLE, DRAINING  grey, no mark. Both are healthy: draining still
 *                        serves what it holds, it only stops taking more. A
 *                        hue here would be a verdict nobody reached.
 *   PAUSED               the park hue's pause bars, as capacity.html A1 draws
 *                        it. The one task state it may echo is PARKED, and
 *                        the echo is exact: a paused account, like a parked
 *                        task, holds nothing and waits for a person.
 *   REAUTH_REQUIRED      the amber warning triangle. It is a CONDITION -- the
 *                        refresh token is gone and the sweep has stopped
 *                        retrying -- and conditions are `WarnMark`.
 *   anything else        the platform's own word, faint, with no mark: a state
 *                        this console never heard of is drawn as unrecognised
 *                        rather than defaulted into a healthy one.
 *
 * The word is lower case with spaces, the way every other state word in the
 * console reads; the platform's spelling is the `title`. REAUTH_REQUIRED is
 * the one said in plain words (QA2-FIX-14, capacity-21): `reauth required`
 * named the broker's step, and what a reader has to do is sign the account
 * in again.
 */
export function AcctState({ state }: { state: string }) {
  const word = state === 'REAUTH_REQUIRED' ? 'needs sign-in' : state.toLowerCase().replace(/_/g, ' ')
  if (state === 'REAUTH_REQUIRED') return <NamedMark className="acct-state" mark="warn" hue="warn" word={word} title={state} />
  if (state === 'PAUSED') return <NamedMark className="acct-state" mark="parked" hue="park" word={word} title={state} />
  const known = state === 'AVAILABLE' || state === 'DRAINING'
  return (
    <NamedMark
      className="acct-state"
      mark={null}
      hue={known ? 'neu' : 'unknown'}
      word={word}
      title={known ? state : `${state}: a state this console does not recognise`}
    />
  )
}
