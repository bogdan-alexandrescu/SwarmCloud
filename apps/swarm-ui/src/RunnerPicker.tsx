import { useEffect, useState } from 'react'
import { loadProviders } from './api'
import type { Result } from './fetch'
import { HELP } from './help'
import { Mark } from './primitives'
import { headroomFor, type ProvidersPage, type RunnerProfile } from './types'

/**
 * THE RUNNER CHOICE, drawn by both Submit forms (#114), in the two shapes the
 * owner picked on submit.html (2026-10-01).
 *
 * WHAT IT REPLACES. The task form's radios printed each runner's class, units,
 * backend and "key needed", and the room for the chosen runner only; a
 * workflow step picked its runner from a bare `<select>` that printed nothing.
 * The two facts that decide whether a submission moves -- is there room for
 * it, and does this tenant hold the key it needs -- were on screen for one
 * runner at a time, or for none.
 *
 * TWO SHAPES, ONE SET OF FACTS. The task form draws every runner as a card
 * (`RunnerPicker`, F1): size, backend, key and how many can start now. A
 * workflow step draws a select (`RunnerSelect`, G1) whose options carry the
 * room, and the chosen runner's size, room and key under it
 * (`StepRunnerFacts`) -- the card list in every step made a step ~680px tall
 * (#503). Both read `headroomFor` and `/v1/providers` through the same
 * helpers below, so the two forms cannot word the same runner two ways.
 *
 * INVARIANT 10 IS STILL THE SHAPE: a caller picks a `runner_profile` by name
 * from the catalogue, and a name that is not in it cannot be chosen. A
 * DISABLED PROFILE STAYS IN THE LIST with the platform's own reason, because
 * the catalogue says it is known and refused, and a list that dropped it would
 * send somebody hunting a typo that is not there.
 *
 * RADIOS ON THE CARDS: a native radio carries the pick-one affordance, the
 * keyboard behaviour and the accessible role. `group` is the radio name.
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

/**
 * What a runner selects, in the words the cards and the step line use:
 * `standard · 1 unit · Cloud Run`. The class and the weight are the frozen
 * catalogue's, read off the response; the backend is the API's enum brought
 * into this console's register, and one this build does not know is printed
 * as sent rather than guessed at.
 */
const BACKEND_LABEL: Readonly<Record<string, string>> = {
  CLOUD_RUN_JOB: 'Cloud Run',
  GKE_AUTOPILOT: 'GKE Autopilot',
  AUTO: 'backend chosen at dispatch',
}

export function sizeOf(profile: RunnerProfile): string {
  return `${profile.resource_class} · ${profile.units} unit${profile.units === 1 ? '' : 's'} · ${BACKEND_LABEL[profile.backend] ?? profile.backend}`
}

/**
 * How many more of this runner can start right now (submit.html F1's "can
 * start now"), off `headroomFor`, as `RoomFact` is.
 *
 * A MEASURED ZERO IS A DIGIT AND NAMES THE POOL HOLDING IT DOWN; a room
 * nobody could measure is the unread mark and the count of pools that did not
 * answer, never a 0 (TS-23).
 */
export function CanStartFact({ profile }: { profile: RunnerProfile }) {
  const room = headroomFor(profile)
  if (room.agents !== null) {
    if (room.agents > 0) return <>{room.agents} more can start now</>
    return <>0 can start now{room.binding !== null && <> · held by <span className="mono">{room.binding}</span></>}</>
  }
  if (room.basis === 'uncapped') return <>no pool caps it</>
  return (
    <>
      <Mark kind="unread" say={HELP['room-unknown-not-zero'].short} /> room unknown ·{' '}
      {room.unread.length} of {profile.pools.length} pools could not be read
    </>
  )
}

/** `CanStartFact` as plain text, for an `<option>`, which holds no markup. */
function canStartText(profile: RunnerProfile): string {
  const room = headroomFor(profile)
  if (room.agents !== null) return room.agents > 0 ? `${room.agents} more can start now` : '0 can start now'
  return room.basis === 'uncapped' ? 'no pool caps it' : 'room unknown'
}

// `?? true` and not `|| true`: an older API omits the field, and
// `false || true` is true, which would offer a profile we know is refused.
const isOff = (p: RunnerProfile) => (p.available ?? true) === false

/**
 * ONLY THE USABLE OPTIONS, WHEN MOST ARE NOT (walkthrough D, owner
 * 2026-10-03). The issue form drew seven disabled cards with long orange
 * explanations around the one runner it can use. When more entries are
 * disabled than usable, the list shows the usable ones -- and the chosen
 * one, whatever it is -- and holds the rest behind "Other runners (N
 * unavailable)". A list with most entries usable keeps every one in place.
 * One rule for the issue form, the task form and the workflow form's steps.
 */
export function runnerSplit(
  profiles: ReadonlyArray<readonly [string, RunnerProfile]>,
  chosen: string,
  /** Hold back every unusable entry, however few: a form that decides the runner itself (the issue form). */
  always = false,
): { shown: ReadonlyArray<readonly [string, RunnerProfile]>; held: ReadonlyArray<readonly [string, RunnerProfile]> } {
  const off = profiles.filter(([n, p]) => isOff(p) && n !== chosen)
  const shown = profiles.filter(([n, p]) => !isOff(p) || n === chosen)
  // Nothing usable at all: the list is the reasons, so it stays whole.
  if ((!always && off.length * 2 <= profiles.length) || off.length === 0 || shown.length === 0) return { shown: profiles, held: [] }
  return { shown, held: off }
}

/**
 * A DISABLED RUNNER'S ONE-LINE REASON (walkthrough E). The platform's
 * `disabled_reason` is written for operators -- "the merge chain (#295) is
 * disabled for every tenant until signed step specs (#342)…" -- so on the
 * form it is a short reason a submitter can act on, and the platform's own
 * words are the line's tooltip and the list's `?`. A reason that is already
 * one short plain sentence is shown as it is.
 */
export function shortReason(reason: string | null | undefined): string {
  const r = (reason ?? '').trim()
  if (r === '') return 'Not available'
  if (r.length <= 48 && !/[#/()_]|\d{3}/.test(r)) return r.replace(/\.$/, '')
  const why = /\b(disabled|until|not (yet )?enabled)\b/i.test(r)
    ? 'Not enabled yet'
    : /\b(credential|key|token|sign[- ]?in)\b/i.test(r)
      ? 'Its credential was refused'
      : 'Not available'
  // What to do instead, when the platform says: "… Use claude-code."
  const instead = /\bUse ([A-Za-z0-9][\w.-]*?)\.?$/.exec(r)
  return instead === null ? why : `${why}. Use ${instead[1]}`
}

/**
 * A refused runner's line, as every list draws it: the card's reason, the held
 * list's, and a workflow option's after its name (QA G4-30). The option used
 * to lowercase it -- "not enabled yet. use claude-code" beside a card reading
 * "Not enabled yet. Use claude-code" -- which also lowercased the runner it
 * names. One helper, so the two shapes cannot word one runner two ways.
 */
export function offLine(profile: RunnerProfile): string {
  return shortReason(profile.disabled_reason)
}

/**
 * Why a list holds its other runners back, as its disclosure says it. The
 * default is the platform's refusal; the issue form passes its own reason,
 * because the runners it holds back are refused for ISSUE RUNS, not by the
 * platform -- `browser` or `mock` can start now, and "8 unavailable" said
 * otherwise (QA G4-30).
 */
export function heldSummary(count: number, heldAs = 'unavailable'): string {
  return heldAs === 'unavailable' ? `Other runners (${count} unavailable)` : `Other runners (${count}, ${heldAs})`
}

/**
 * THE PLATFORM'S OWN RUNNERS (QA G4-33). Each is a catalogue entry a caller
 * may still name (invariant 10 is untouched: these are names, not specs), but
 * the platform submits them for itself -- `merge` lands a workflow's pull
 * request, `post-verdict` and `claude-code-review` are the review chain's
 * steps, `indexer` is swarm-api's repository-index run
 * (`swarm_api.repoindex.INDEXER_PROFILE`), and `mock` is the smoke tests' --
 * so the task form marks them "platform" and lists them after the runners
 * people submit, rather than beside `claude-code` as if they were peers.
 *
 * A LIST OF NAMES HERE because the API serves no such flag: the frozen
 * `RunnerProfile` has no audience field. A name missing from this list is
 * drawn as an ordinary runner, which is the old behaviour, never a hidden one.
 */
const PLATFORM_RUNNERS: ReadonlySet<string> = new Set(['claude-code-review', 'indexer', 'merge', 'mock', 'post-verdict'])

export function isPlatformRunner(name: string): boolean {
  return PLATFORM_RUNNERS.has(name)
}

/** The runners a list holds back, behind a disclosure, each with its reason. */
function HeldRunners({ held, heldAs }: { held: ReadonlyArray<readonly [string, RunnerProfile]>; heldAs?: string }) {
  if (held.length === 0) return null
  return (
    <details className="sbf-runners-more">
      <summary>{heldSummary(held.length, heldAs)}</summary>
      {/* THE WHY IS ONE CLICK AWAY, in Help (walkthrough E). A link, not a
          `?`: the console's glyphs are rationed (tests/help.test.ts, B7.4),
          and this list is drawn on two forms. */}
      <a className="sbf-runners-why ctl-link" href={`#${HELP['runner-unavailable'].anchor}`}>
        Why a runner is unavailable
      </a>
      <ul>
        {held.map(([n, p]) => (
          <li key={n} title={p.disabled_reason || undefined}>
            <span className="sbf-runner-name mono">{n}</span>
            <span className="sbf-runner-why">{offLine(p)}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

/**
 * THE TASK FORM'S RUNNERS, AS TWO-COLUMN CARDS (submit.html F1, picked
 * 2026-10-01). Each card is the name, then its size and backend and the key
 * it needs, then how many can start now -- or, for a disabled runner, the
 * platform's reason in that line's place.
 */
export function RunnerPicker({ group, label, profiles, chosen, keys, onPick, onlyUsable = false, heldAs }: {
  /** The radio group's name. Unique per picker on the page. */
  group: string
  /** The group's accessible name. */
  label: string
  /** The whole catalogue, disabled runners included. */
  profiles: ReadonlyArray<readonly [string, RunnerProfile]>
  chosen: string
  keys: ProviderKeys
  onPick: (name: string) => void
  /** Show only the usable runners however few are held back (the issue form, which decides the runner itself). */
  onlyUsable?: boolean
  /** Why the held-back runners are held back, when it is not the platform's refusal. */
  heldAs?: string
}) {
  const split = runnerSplit(profiles, chosen, onlyUsable)
  // The runners people submit first, the platform's own after them (G4-33).
  // A stable partition, so each half keeps the catalogue's order.
  const shown = [...split.shown.filter(([n]) => !isPlatformRunner(n)), ...split.shown.filter(([n]) => isPlatformRunner(n))]
  const held = split.held
  return (
    <>
    <ul className="sbf-runners is-cards" role="radiogroup" aria-label={label}>
      {shown.map(([n, p]) => {
        const off = isOff(p)
        return (
          <li key={n} role="none">
            <label className={`sbf-runner${chosen === n ? ' is-on' : ''}${off ? ' is-off' : ''}`}>
              <input type="radio" name={group} value={n} checked={chosen === n} disabled={off} onChange={() => onPick(n)} />
              <span className="sb-runner-body">
                <span className="sbf-runner-name mono">{n}</span>
                {isPlatformRunner(n) && (
                  <span className="sb-runner-platform" title="The platform submits this runner for its own steps; it is not one people usually choose.">platform</span>
                )}
                <span className="sbf-runner-facts">
                  <span className="sb-runner-size">{sizeOf(p)}</span>
                  {' · '}
                  <span className="sbf-runner-key"><KeyFact provider={p.provider} keys={keys} /></span>
                </span>
                {off
                  ? <span className="sbf-runner-off" title={p.disabled_reason || undefined}>{offLine(p)}</span>
                  : <span className="sbf-runner-room"><CanStartFact profile={p} /></span>}
              </span>
            </label>
          </li>
        )
      })}
    </ul>
    <HeldRunners held={held} heldAs={heldAs} />
    </>
  )
}

/**
 * A WORKFLOW STEP'S RUNNER, AS A SELECT (submit.html G1, picked 2026-10-01).
 * The full card list in every step made each step about 680px tall (#503).
 * The select still offers the whole catalogue with each runner's room, and a
 * disabled runner stays in it, refused and saying so; the chosen runner's
 * size, room and key are drawn under it by `StepRunnerFacts`.
 */
export function RunnerSelect({ id, label, profiles, chosen, onPick }: {
  id: string
  label: string
  profiles: ReadonlyArray<readonly [string, RunnerProfile]>
  chosen: string
  onPick: (name: string) => void
}) {
  return (
    <select id={id} className="mono sb-runner-select" aria-label={label} value={chosen}
      aria-invalid={chosen === '' || undefined} onChange={(e) => onPick(e.target.value)}>
      <option value="" disabled>choose a runner</option>
      {runnerSplit(profiles, chosen).shown.map(([n, p]) => (
        <option key={n} value={n} disabled={isOff(p)} title={isOff(p) ? p.disabled_reason || undefined : undefined}>
          {isOff(p) ? `${n} · ${offLine(p)}` : `${n} · ${canStartText(p)}`}
        </option>
      ))}
      {/* The held runners, in a group of their own: a select has no disclosure. */}
      {(() => {
        const { held } = runnerSplit(profiles, chosen)
        return held.length === 0 ? null : (
          <optgroup label={heldSummary(held.length)}>
            {held.map(([n, p]) => (
              <option key={n} value={n} disabled title={p.disabled_reason || undefined}>
                {`${n} · ${offLine(p)}`}
              </option>
            ))}
          </optgroup>
        )
      })()}
    </select>
  )
}

/** The chosen step runner's size, room and key, under its select.
 *
 *  EACH SEPARATOR ENDS ITS ITEM (VQA V127), the issue preview's rule (N20):
 *  the dot is held to the word before it by a no-break space, so a wrapped
 *  line can end on one but never start with one. The size is one unbroken
 *  run (`.wfb-cost .sb-runner-size`, submit.css): its own dots wrapped too. */
export function StepRunnerFacts({ profile, keys }: { profile: RunnerProfile; keys: ProviderKeys }) {
  return (
    <p className="wfb-cost">
      <span className="sb-runner-size">{sizeOf(profile)}</span>
      {'\u00a0· '}
      <span className="sbf-runner-room"><CanStartFact profile={profile} /></span>
      {'\u00a0· '}
      <span className="sbf-runner-key"><KeyFact provider={profile.provider} keys={keys} /></span>
    </p>
  )
}
