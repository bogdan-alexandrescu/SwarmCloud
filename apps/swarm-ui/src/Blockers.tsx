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

import {
  blockerCeiling,
  blockerGroup,
  ceilingCopy,
  needsAPerson,
  poolLabelAmong,
  reasonCopy,
  type Ceiling,
  type Counterfactual,
  type Headroom,
  type ProfileBlocker,
} from './types'

/** How a pool is named on screen. See `poolLabelAmong`. */
type Label = (pool: string) => string

/**
 * Every pool name this panel can print for one profile, so a label is decided
 * against all of them at once (CP-15). Printed separately, `resource:browser`
 * and `runner:browser` were both `browser`: two `Lift browser` rows, and
 * `browser and browser still binds`.
 */
function printed(h: Headroom): string[] {
  return [
    ...h.blockers.map((b) => b.pool),
    ...h.counterfactual.flatMap((c) => [c.pool, ...c.next_binding]),
    ...h.unread,
    ...h.missing,
  ]
}

/** Every pool named against `among`, the set the labels are read together in. */
function labelsAmong(among: readonly string[]): Label {
  return (pool) => poolLabelAmong(pool, among)
}

/** The default labeller for a panel drawn on its own, outside a profile card. */
function labelsFor(h: Headroom): Label {
  return labelsAmong(printed(h))
}

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
export function IncompleteNote({ h, label = labelsFor(h) }: { h: Headroom; label?: Label }) {
  if (h.complete) return null
  return (
    <p className="warn-text" role="status">
      This list is <strong>incomplete</strong>.{' '}
      {h.unread.length > 0 ? (
        <>
          {h.unread.length} pool{h.unread.length === 1 ? '' : 's'} could not be read
          ({h.unread.map(label).join(', ')}), and any of them may be refusing
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

/**
 * The tag a refusing pool is drawn with: `paused`, `limit 0` or `full`.
 *
 * EXPORTED BECAUSE THERE ARE TWO ROWS, and the second one is where this went
 * wrong. The submit box (`Submit.tsx` `ProfileFacts`) draws a compact row of
 * its own, and it kept deciding the tag on `reason === 'MANUAL_PAUSE'` after
 * this file stopped -- so a pool capped at zero was still `full` there after
 * it had become `limit 0` here. One definition, two callers.
 */
export function CeilingTag({ blocker }: { blocker: ProfileBlocker }) {
  const tag = CEILING_TAG[blockerCeiling(blocker)]
  return (
    <span className={`tag ${tag.cls}`} title={tag.title}>
      {tag.word}
    </span>
  )
}

/**
 * What a ceiling means for the pool behind it, as `CeilingTag`'s title says it.
 * Profile headroom's status marks (CP-12, #85) carry the same explanation the
 * tags did, so it is read from the one table rather than written twice.
 */
export function ceilingTitle(c: Ceiling): string {
  return CEILING_TAG[c].title
}

/**
 * The numbers that made a pool refuse, as the row prints them. "units", never
 * "agents": admission increments by the profile's weight. A paused pool admits
 * nothing at ANY ceiling -- and a drained one carries the unlimited sentinel
 * as its limit -- so it prints what is held and no ceiling; a pool at zero
 * says the zero in words. Only a full pool gets the fraction, because it is
 * the only one the fraction is true of. Shared with the submit box for the
 * reason `CeilingTag` is.
 */
export function ceilingFigure(blocker: ProfileBlocker): string {
  const ceiling = blockerCeiling(blocker)
  const held = `${blocker.active} unit${blocker.active === 1 ? '' : 's'} held`
  if (ceiling === 'full') return `${blocker.active} of ${blocker.limit} units in use`
  return ceiling === 'paused' ? held : `limit 0 · ${held}`
}

function BlockerRow({ blocker, label }: { blocker: ProfileBlocker; label: Label }) {
  // A pause and a full pool both stop everything and have OPPOSITE remedies:
  // resume it, versus wait or raise it. A pause is told apart by the reason
  // the server sent -- a paused pool can read 0 of 8 in use and still admit
  // nothing, which is the case that looks healthiest and is not. A pool at
  // ZERO is told apart by its ceiling, because its reason is the full pool's
  // (see `blockerCeiling`): "0 of 0 units in use" under a `full` tag is how
  // the live console came to call a switched-off pool busy.
  const ceiling = blockerCeiling(blocker)
  return (
    <li className={`blocker-row${needsAPerson(ceiling) ? ' is-paused' : ' is-full'}`}>
      <span className="blocker-head">
        <span className="tags">
          <CeilingTag blocker={blocker} />
        </span>
        <code className="blocker-pool" title={blocker.pool}>
          {label(blocker.pool)}
        </code>
        <strong className="blocker-reason">{blocker.reason}</strong>
        {/* The numbers that made it fail, on the entry that failed. */}
        <span className="blocker-at">{ceilingFigure(blocker)}</span>
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
  label = labelsFor(h),
}: {
  h: Headroom
  groups: Record<string, string[]> | undefined
  label?: Label
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
              <BlockerRow key={b.pool} blocker={b} label={label} />
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
              <BlockerRow key={b.pool} blocker={b} label={label} />
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
              <BlockerRow key={b.pool} blocker={b} label={label} />
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

/**
 * WHAT LIFTING ONE CEILING WOULD HAVE BOUGHT, as a figure and its sentence --
 * the `+N if lifted` cell on Profile headroom (CP-6, #85).
 *
 * It was `<Counterfactuals>`, a list of sentences under each card, with a row
 * for every configured pool: "4 more would have started", and five to seven
 * rows of "nothing would have changed". The owner's decision made it a column
 * on the rows of the pools that cap the card, and removed the rows for pools
 * whose lifting buys nothing. The figure is the cell; the sentence the row
 * used to be is the cell's accessible name, so nothing it said is lost.
 *
 * The zeros on a BINDING pool are still the valuable half. Under a
 * minimum-across-pools rule, raising a ceiling that is not the only binding
 * one changes nothing -- two pools tie, and lifting either alone buys +0 --
 * and a cell reading +0 beside a pool tagged `binding` is what stops an
 * operator raising it and watching nothing move.
 *
 * ON THE WORDING, unchanged. This is a PREDICTION and predictions are where a
 * screen like this starts lying: a lease can be released between the read and
 * the render, and then a number presented in the present tense is simply
 * false. So:
 *
 *  - everything is PAST CONDITIONAL -- "would have started", never "will
 *    start" and never "you can start". It is a statement about the counts at
 *    one instant, which is the only thing it is evidence for;
 *  - that instant is named, with the server's own `generated_at` rather than
 *    the browser's clock -- once, in the page foot, rather than once a card;
 *  - "lifted entirely" rather than "raised to N": the counterfactual removes
 *    the ceiling altogether, so a zero here means raising it to ANY value buys
 *    nothing, which is the stronger and more useful claim;
 *  - a zero always names what still binds. A bare "no change" reads as a
 *    glitch and gets ignored.
 *
 * `kind` is the cell's modifier (CP-13): `pointless` for a measured +0, dimmed
 * rather than hidden; `unmeasured` for a delta nobody could compute, and for a
 * read that missed a ceiling (the counterfactual is not computed then: a
 * ceiling never read cannot be compared against one that was); `measured` for
 * a change, including "nothing else would have been left to cap it", which is
 * an answer, not a gap -- drawn as the word `uncapped`, never as a number.
 */
export function liftFigure(
  c: Counterfactual | undefined,
  complete: boolean,
  pool: string,
  label: Label,
): { text: string; say: string; kind: 'measured' | 'pointless' | 'unmeasured' } {
  if (!complete) {
    return {
      text: '—',
      say: 'Not computed: a ceiling that was never read cannot be compared against one that was.',
      kind: 'unmeasured',
    }
  }
  if (c === undefined) {
    return {
      text: '—',
      say: `Not computed: the response carried no counterfactual for ${label(pool)}.`,
      kind: 'unmeasured',
    }
  }
  const act = `${c.action === 'resume' ? 'Resume' : 'Lift'} ${label(c.pool)}`
  if (c.headroom_after === null) {
    // Nothing else configured would have bound. Not a number, so not drawn as
    // one.
    return { text: 'uncapped', say: `${act}: nothing else would have been left to cap it.`, kind: 'measured' }
  }
  if (c.delta === null) {
    return { text: '—', say: `${act}: the change could not be measured.`, kind: 'unmeasured' }
  }
  if (c.delta > 0) {
    const next =
      c.next_binding.length > 0
        ? ` — then ${c.next_binding.map(label).join(' and ')} would have bound`
        : ''
    return { text: `+${c.delta}`, say: `${act}: ${c.delta} more would have started${next}.`, kind: 'measured' }
  }
  // THE VERB AGREES WITH ITS SUBJECT (CP-15). Two pools still in the way
  // "still bind"; it read "eng and anthropic still binds" for as long as
  // `next_binding` has been a list.
  const still =
    c.next_binding.length > 0
      ? `${c.next_binding.map(label).join(' and ')} still ${c.next_binding.length === 1 ? 'binds' : 'bind'}`
      : 'something else still binds'
  return { text: '+0', say: `${act}: nothing would have changed — ${still}.`, kind: 'pointless' }
}

/*
 * `ProfileAdmissionPanel` LIVED HERE AND IS GONE (CP-6, #85). It drew the
 * incomplete-read banner, the blocker list grouped by remedy, and the
 * counterfactual list under every Profile headroom card, and that card was its
 * only caller. The owner's decision leaves each card one fact sentence: every
 * refusing pool is on its own row with its status mark -- whose title carries
 * the reason, the figures and the remedy the blocker row printed -- and what
 * lifting it would buy is the `+N if lifted` cell (`liftFigure` above).
 * `IncompleteNote` and `BlockerList` stay exported: they answer "why is THIS
 * not running" for one object, which is the shape the header of this file
 * describes, and their tests pin that shape.
 */
