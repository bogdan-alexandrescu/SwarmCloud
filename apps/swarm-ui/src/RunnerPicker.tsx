import { useEffect, useState } from 'react'
import { loadProviders } from './api'
import type { Result } from './fetch'
import { HELP } from './help'
import { Mark } from './primitives'
import { headroomFor, type ProvidersPage, type RunnerProfile } from './types'

/**
 * THE ONE RUNNER PICKER, drawn by both Submit forms (#114).
 *
 * WHAT IT REPLACES. The task form's radios printed each runner's class, units,
 * backend and "key needed", and the room for the chosen runner only; a
 * workflow step picked its runner from a bare `<select>` that printed nothing.
 * The two facts that decide whether a submission moves -- is there room for
 * it, and does this tenant hold the key it needs -- were on screen for one
 * runner at a time, or for none. Every row here carries both. The class, the
 * weight and the backend of the CHOSEN runner are still drawn by each form,
 * under the list.
 *
 * INVARIANT 10 IS STILL THE SHAPE: a caller picks a `runner_profile` by name
 * from the catalogue, and a name that is not in it cannot be chosen. A
 * DISABLED PROFILE STAYS IN THE LIST with the platform's own reason, because
 * the catalogue says it is known and refused, and a list that dropped it would
 * send somebody hunting a typo that is not there.
 *
 * RADIOS, NOT BUTTONS: a native radio carries the pick-one affordance, the
 * keyboard behaviour and the accessible role without a box. `group` is the
 * radio name, so two steps of one workflow are two groups.
 */

/** What `/v1/providers` said about the calling tenant's keys. */
export type ProviderKeys =
  | { kind: 'reading' }
  | { kind: 'unread'; detail: string }
  /** provider name -> `credential_registered`. */
  | { kind: 'read'; registered: ReadonlyMap<string, boolean> }

/**
 * One read of `/v1/providers` per form, handed to every picker on it. A
 * workflow with six steps draws six pickers, and six reads of one answer
 * would be the #227 defect again. Called inside `then` so a load that throws
 * rather than rejects lands as unread, like one that rejects.
 */
export function useProviderKeys(): ProviderKeys {
  const [keys, setKeys] = useState<ProviderKeys>({ kind: 'reading' })
  useEffect(() => {
    let live = true
    Promise.resolve()
      .then(() => loadProviders())
      .then(
        (r: Result<ProvidersPage>) => {
          if (!live) return
          if (r.status === 'ok' || r.status === 'stale') {
            setKeys({ kind: 'read', registered: new Map(r.data.providers.map((p) => [p.provider, p.credential_registered])) })
          } else if (r.status === 'empty') {
            // A measured empty list: every provider below reads as "not
            // listed", which is unread, never "missing".
            setKeys({ kind: 'read', registered: new Map() })
          } else if (r.status === 'error') {
            setKeys({ kind: 'unread', detail: r.error.message })
          }
        },
        (err: unknown) => {
          if (live) setKeys({ kind: 'unread', detail: err instanceof Error ? err.message : 'The read did not complete.' })
        },
      )
    return () => {
      live = false
    }
  }, [])
  return keys
}

/**
 * How many more of this runner fit right now, off `headroomFor`.
 *
 * NEVER A ZERO FOR "NOT KNOWN" (TS-23). A measured zero is a digit; a room
 * nobody could measure is the unread mark, whose accessible name is
 * `room-unknown-not-zero`'s rule, then `room unknown` and the count of pools
 * that did not answer -- the only numeral in the slot. Exported because the
 * task form's send panel states the same room from the same call.
 */
export function RoomFact({ profile }: { profile: RunnerProfile }) {
  const room = headroomFor(profile)
  if (room.agents !== null) return <>room for {room.agents} more</>
  if (room.basis === 'uncapped') return <>no pool caps it</>
  return (
    <>
      <Mark kind="unread" say={HELP['room-unknown-not-zero'].short} /> room unknown ·{' '}
      {room.unread.length} of {profile.pools.length} pools could not be read
    </>
  )
}

/**
 * Whether the tenant holds the key this runner needs.
 *
 * `<provider> key present`, not `has a <provider> key`: the article would be
 * chosen before the name is known (TS-19). A provider the read did not list,
 * or a read that failed, is the unread mark -- never "missing", which is a
 * claim that sends somebody to register a key they may already hold.
 */
function KeyFact({ provider, keys }: { provider: string | null; keys: ProviderKeys }) {
  if (provider === null) return <>no key needed</>
  if (keys.kind === 'reading') {
    return <><Mark kind="pending" say={`Reading whether this tenant has registered its ${provider} key.`} /> {provider} key</>
  }
  if (keys.kind === 'unread') {
    return <><Mark kind="unread" say={`Whether this tenant holds its ${provider} key could not be read. ${keys.detail}`} /> {provider} key</>
  }
  const held = keys.registered.get(provider)
  if (held === undefined) {
    return <><Mark kind="unread" say={`The providers read did not list ${provider}, so whether its key is held is unknown.`} /> {provider} key</>
  }
  return held ? <>{provider} key present</> : <span className="sbf-runner-miss">{provider} key missing</span>
}

export function RunnerPicker({ group, label, profiles, chosen, keys, onPick, compact = false }: {
  /** The radio group's name. Unique per picker on the page. */
  group: string
  /** The group's accessible name. */
  label: string
  /** The whole catalogue, disabled runners included. */
  profiles: ReadonlyArray<readonly [string, RunnerProfile]>
  chosen: string
  keys: ProviderKeys
  onPick: (name: string) => void
  /** Inside a workflow step card, which is a narrow column. */
  compact?: boolean
}) {
  return (
    <ul className={compact ? 'sbf-runners is-compact' : 'sbf-runners'} role="radiogroup" aria-label={label}>
      {profiles.map(([n, p]) => {
        // `?? true` and not `|| true`: an older API omits the field, and
        // `false || true` is true, which would offer a profile we know is refused.
        const off = (p.available ?? true) === false
        return (
          <li key={n} role="none">
            <label className={`sbf-runner${chosen === n ? ' is-on' : ''}${off ? ' is-off' : ''}`}>
              <input type="radio" name={group} value={n} checked={chosen === n} disabled={off} onChange={() => onPick(n)} />
              <span className="sbf-runner-name mono">{n}</span>
              <span className="sbf-runner-facts">
                <span className="sbf-runner-room"><RoomFact profile={p} /></span>
                {' · '}
                <span className="sbf-runner-key"><KeyFact provider={p.provider} keys={keys} /></span>
              </span>
              {off && <span className="sbf-runner-off">{p.disabled_reason || 'refused by the platform'}</span>}
            </label>
          </li>
        )
      })}
    </ul>
  )
}
