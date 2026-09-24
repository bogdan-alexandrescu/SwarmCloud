// Every pool refusing a runner profile, grouped by what would clear it, plus
// what relaxing each ceiling ONE AT A TIME would have bought.
//
// The shape is Nomad's job-overview placement rather than a capacity
// dashboard's: every failing reason listed, each carrying the numbers that
// made it fail, on the object that is stuck. A dashboard answers "what is the
// platform doing"; this answers "why is THIS not running", which is a
// different question and the one that gets asked at 3am.
//
// Nothing here computes admission. `profile.admission` is produced by
// swarm_api/headroom.py from `evaluate_capacity` -- the function the admission
// transaction itself calls -- so this file renders a decision rather than
// making a second one. See the header of headroom.py for why that is served
// rather than restated here.

import { timeAgo } from './Shell'
import {
  blockerCeiling,
  blockerGroup,
  ceilingCopy,
  headroomFor,
  needsAPerson,
  poolLabel,
  reasonCopy,
  type Capacity,
  type Ceiling,
  type Counterfactual,
  type Headroom,
  type ProfileBlocker,
  type RunnerProfile,
} from './types'

/** The number, or an em dash with the reason there is no number. */
export function headroomFigure(h: Headroom): { text: string; title: string } {
  if (h.agents === null) {
    return h.basis === 'uncapped'
      ? { text: '—', title: 'No pool in this profile is configured, so nothing caps it. That is not a count.' }
      : { text: '—', title: 'At least one required pool could not be read, so this cannot be counted. It is not zero.' }
  }
  return { text: String(h.agents), title: `${h.agents} more could have been admitted at the last read.` }
}

/**
 * The banner a partial read must carry.
 *
 * An incomplete blocker list presented as complete is worse than naming only
 * one pool: it tells an operator they have cleared everything when a pool
 * nobody read may still be refusing. So the list is never silently shortened
 * -- what was measured stays on screen, under this.
 */
export function IncompleteNote({ h }: { h: Headroom }) {
  if (h.complete) return null
  return (
    <p className="warn-text" role="status">
      This list is <strong>incomplete</strong>.{' '}
      {h.unread.length > 0 ? (
        <>
          {h.unread.length} pool{h.unread.length === 1 ? '' : 's'} could not be read
          ({h.unread.map(poolLabel).join(', ')}), and any of them may be refusing
          this profile as well. Everything below was measured; it is not the
          whole list, and the count is withheld rather than guessed.
        </>
      ) : (
        <>This build of the API did not send the admission block, so nothing below was measured.</>
      )}
    </p>
  )
}

/** The tag each ceiling is drawn with, and what hovering it says. */
const CEILING_TAG: Readonly<Record<Ceiling, { cls: string; word: string; title: string }>> = {
  paused: {
    cls: 'paused',
    word: 'paused',
    title: 'An operator paused this pool. It admits nothing until somebody resumes it — raising its limit changes nothing.',
  },
  'set-to-zero': {
    // The paused tone, because the remedy is the paused one: a person acts.
    cls: 'paused',
    word: 'limit 0',
    title: "An operator set this pool's limit to 0. It admits nothing until somebody raises it — waiting changes nothing.",
  },
  zero: {
    cls: 'capped',
    word: 'limit 0',
    title: "This pool's limit is 0, so it admits nothing. A provider pool's quota state lowers it as well as an operator does, so this does not say who set it.",
  },
  full: {
    cls: 'full',
    word: 'full',
    title: 'This pool is at its ceiling. Waiting clears it, and so does raising the ceiling.',
  },
}

function BlockerRow({ blocker }: { blocker: ProfileBlocker }) {
  // A pause and a full pool both stop everything and have OPPOSITE remedies:
  // resume it, versus wait or raise it. A pause is told apart by the reason
  // the server sent -- a paused pool can read 0 of 8 in use and still admit
  // nothing, which is the case that looks healthiest and is not. A pool at
  // ZERO is told apart by its ceiling, because its reason is the full pool's
  // (see `blockerCeiling`): "0 of 0 units in use" under a `full` tag is how
  // the live console came to call a switched-off pool busy.
  const ceiling = blockerCeiling(blocker)
  const tag = CEILING_TAG[ceiling]
  const held = `${blocker.active} unit${blocker.active === 1 ? '' : 's'} held`
  return (
    <li className={`blocker-row${needsAPerson(ceiling) ? ' is-paused' : ' is-full'}`}>
      <span className="blocker-head">
        <span className="tags">
          <span className={`tag ${tag.cls}`} title={tag.title}>
            {tag.word}
          </span>
        </span>
        <code className="blocker-pool" title={blocker.pool}>
          {poolLabel(blocker.pool)}
        </code>
        <strong className="blocker-reason">{blocker.reason}</strong>
        {/* The numbers that made it fail, on the entry that failed. "units",
            never "agents": admission increments by the profile's weight. A
            paused pool admits nothing at ANY ceiling -- and a drained one
            carries the unlimited sentinel as its limit -- so it prints what
            is held and no ceiling; a pool at zero says the zero in words. */}
        <span className="blocker-at">
          {ceiling === 'full'
            ? `${blocker.active} of ${blocker.limit} units in use`
            : ceiling === 'paused'
              ? held
              : `limit 0 · ${held}`}
        </span>
      </span>
      <span className="blocker-copy">{ceilingCopy(blocker, 'This pool') ?? reasonCopy(blocker.reason)}</span>
    </li>
  )
}

/**
 * Two groups, because the remedies differ and a mixed list buries the one
 * that needs a person between two that need nobody.
 *
 * The grouping is the SERVER'S -- `blocked_reason_groups` on the response, or
 * the `group` the server stamped on each blocker. A reason in neither group
 * gets its own section rather than being filed under "waiting is fine", which
 * would be a lie about the remedy.
 */
export function BlockerList({
  h,
  groups,
}: {
  h: Headroom
  groups: Record<string, string[]> | undefined
}) {
  const needsAction = h.blockers.filter((b) => blockerGroup(b, groups) === 'needs_action')
  const noRoom = h.blockers.filter((b) => blockerGroup(b, groups) === 'no_room')
  const ungrouped = h.blockers.filter((b) => blockerGroup(b, groups) === null)

  if (h.blockers.length === 0) {
    return (
      <p className="muted">
        {h.complete
          ? 'No pool is refusing this profile.'
          : 'No refusal was measured — but see above: the list is incomplete.'}
      </p>
    )
  }

  return (
    <>
      {needsAction.length > 0 && (
        <div className="blocker-group needs-action">
          <h3>
            Somebody has to act
            <span className="blocker-group-note">waiting does not clear these</span>
          </h3>
          <ul className="blocker-list">
            {needsAction.map((b) => (
              <BlockerRow key={b.pool} blocker={b} />
            ))}
          </ul>
        </div>
      )}
      {noRoom.length > 0 && (
        <div className="blocker-group no-room">
          <h3>
            Eligible, no room
            <span className="blocker-group-note">waiting is a valid answer</span>
          </h3>
          <ul className="blocker-list">
            {noRoom.map((b) => (
              <BlockerRow key={b.pool} blocker={b} />
            ))}
          </ul>
        </div>
      )}
      {ungrouped.length > 0 && (
        <div className="blocker-group">
          <h3>
            Not grouped
            <span className="blocker-group-note">
              this API did not say which remedy applies
            </span>
          </h3>
          <ul className="blocker-list">
            {ungrouped.map((b) => (
              <BlockerRow key={b.pool} blocker={b} />
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

function counterfactualText(c: Counterfactual): string {
  if (c.headroom_after === null) {
    // Nothing else configured would have bound. Not a number, so not phrased
    // as one.
    return 'nothing else would have been left to cap it'
  }
  if (c.delta === null) return 'the change could not be measured'
  if (c.delta > 0) {
    const next =
      c.next_binding.length > 0
        ? ` — then ${c.next_binding.map(poolLabel).join(' and ')} would have bound`
        : ''
    return `${c.delta} more would have started${next}`
  }
  const still =
    c.next_binding.length > 0
      ? `${c.next_binding.map(poolLabel).join(' and ')} still binds`
      : 'something else still binds'
  return `nothing would have changed — ${still}`
}

/**
 * The counterfactual, and the zeros are the valuable half.
 *
 * Under a minimum-across-pools rule, raising a ceiling that is not the binding
 * one changes nothing, and the numbers on a capacity screen give no way to
 * tell which ones those are. That is the specific mistake the rule invites,
 * and a row reading "nothing would have changed" is what prevents it.
 *
 * ON THE WORDING. This is a PREDICTION and predictions are where a screen like
 * this starts lying: a lease can be released between the read and the render,
 * and then a number presented in the present tense is simply false. So:
 *
 *  - everything is PAST CONDITIONAL -- "would have started", never "will
 *    start" and never "you can start". It is a statement about the counts at
 *    one instant, which is the only thing it is evidence for;
 *  - that instant is named, with the server's own `generated_at` rather than
 *    the browser's clock;
 *  - "lifted entirely" rather than "raised to N": the counterfactual removes
 *    the ceiling altogether, so a zero here means raising it to ANY value buys
 *    nothing, which is the stronger and more useful claim;
 *  - a zero always names what still binds. A bare "no change" reads as a
 *    glitch and gets ignored.
 */
export function Counterfactuals({
  h,
  generatedAt,
}: {
  h: Headroom
  generatedAt: string
}) {
  if (h.counterfactual.length === 0) {
    if (!h.complete) {
      return (
        <p className="muted small">
          Not computed: a ceiling that was never read cannot be compared against
          one that was.
        </p>
      )
    }
    return null
  }

  return (
    <div className="counterfactual">
      <h3>If one ceiling were lifted</h3>
      <ul className="cf-list">
        {h.counterfactual.map((c) => {
          const none = c.delta === 0
          return (
            <li key={c.pool} className={none ? 'cf-row is-pointless' : 'cf-row'}>
              <span className="cf-action">
                {c.action === 'resume' ? 'Resume' : 'Lift'}{' '}
                <code title={c.pool}>{poolLabel(c.pool)}</code>
              </span>
              <span className="cf-effect">{counterfactualText(c)}</span>
            </li>
          )
        })}
      </ul>
      {/* THE AGE OF THE COUNTS IS THE MARKER: a conclusion drawn from a
          snapshot is only as current as the snapshot, and that is the one
          thing a reader cannot recover from the lines themselves. */}
      <p className="muted small">
        From pool counts read{' '}
        <time dateTime={generatedAt} title={generatedAt}>
          {timeAgo(generatedAt)}
        </time>
        {/* NO `?` (B7.4). `blockers-at-an-instant` says these counts were read
            one pool at a time and are therefore not a simultaneous snapshot --
            and "one pool at a time" is already the last four words of the line
            it was attached to, beside the timestamp of the read. A glyph here
            opened a card to say the sentence it was standing next to. This
            panel is drawn inside Overview and inside the agent detail, both of
            which carry their own glyph; the topic is in the rail's Help
            section. */}
        , one pool at a time.
      </p>
    </div>
  )
}

/**
 * The whole block for one profile. Used by the Capacity board and the runner
 * profile cards so the two cannot drift into two different answers.
 */
export function ProfileAdmissionPanel({
  profile,
  capacity,
}: {
  profile: RunnerProfile
  capacity: Capacity
}) {
  const h = headroomFor(profile)
  return (
    <div className="admission-panel">
      <IncompleteNote h={h} />
      <BlockerList h={h} groups={capacity.blocked_reason_groups} />
      <Counterfactuals h={h} generatedAt={capacity.generated_at} />
    </div>
  )
}
