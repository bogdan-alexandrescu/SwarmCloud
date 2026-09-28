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

import { useEffect, useState } from 'react'

import { loadResourceClasses, type ResourceClasses } from './api'
import type { Result } from './fetch'
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
 *
 * NOTHING OVER NO LIST (CP-6, #85). The banner qualifies the list below it --
 * "everything below was measured; it is not the whole list" -- so with no
 * refusal measured there is nothing below for it to qualify, and the Profile
 * headroom card's one sentence already says a pool could not be read, with
 * `not read` on that pool's row.
 */
export function IncompleteNote({ h, label = labelsFor(h) }: { h: Headroom; label?: Label }) {
  if (h.complete || h.blockers.length === 0) return null
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

/**
 * THE RESOURCE-CLASS CATALOGUE, AS `GET /v1/resource-classes` SERVES IT (#66).
 *
 * A blocker does not carry the weight of the task it refused, and one fact
 * needs it: a pool whose limit is above 0 but BELOW that weight refuses the
 * task on every drain, with nothing running at all. The weight is the task's
 * resource class's `units`, and the route serves `units` for every class, so
 * that is where it is read. `RESOURCE_UNITS` in types.ts is a bundled copy of
 * the same table and is deliberately not read here: #66 asks for no copy.
 *
 * A SECONDARY READ, AND A FAILED ONE CLAIMS NOTHING. The screens that call
 * this are drawn from the capacity read; this one only sharpens a verdict,
 * so until it answers -- or if it never does -- `units` is null and every
 * blocker is drawn exactly as it was before. Read once per mount: a class is
 * resized by a deploy, not between two refreshes. Called inside `then` so a
 * load that throws rather than rejects is swallowed the same way.
 */
export function useResourceClasses(): ResourceClasses | null {
  const [classes, setClasses] = useState<ResourceClasses | null>(null)
  useEffect(() => {
    let live = true
    Promise.resolve()
      .then(() => loadResourceClasses())
      .then(
        (r: Result<{ resource_classes: ResourceClasses }>) => {
          if (live && (r.status === 'ok' || r.status === 'stale')) setClasses(r.data.resource_classes)
        },
        () => undefined,
      )
    return () => {
      live = false
    }
  }, [])
  return classes
}

/** One task's weight for `resourceClass`, or null when the catalogue did not say. */
export function classUnits(classes: ResourceClasses | null, resourceClass: string): number | null {
  const units = classes?.[resourceClass]?.units
  return typeof units === 'number' && units > 0 ? units : null
}

/**
 * `blockerCeiling`/`ceilingCopy` (types.ts) ARE THE ONE VERDICT (#66).
 *
 * This file used to carry its own copy of the below-units arithmetic --
 * `blockerVerdict`/`verdictCopy`, threaded only into Capacity, Profiles and
 * Submit -- so the Agents list and the agent inspector, which read
 * `whyAgent`/`whyNeedsAction` in types.ts instead, never saw it: #66's own
 * repro (`resource:browser` at `hard_limit 1`, a 2-unit browser task) still
 * read "This resource class is busy platform-wide. (0/1)" there. `Verdict`,
 * `blockerVerdict` and `verdictCopy` stay exported -- Capacity.tsx,
 * Profiles.tsx and Submit.tsx call them by these names -- but they are now
 * thin wrappers over `blockerCeiling`/`ceilingCopy`, which carry the below-
 * units logic themselves, so every screen renders one verdict.
 */
export type Verdict = Ceiling

export function blockerVerdict(
  b: { reason: string; pool?: string; limit?: unknown },
  units: number | null,
): Verdict {
  return blockerCeiling(b, units)
}

/** The verdicts no amount of waiting clears: a person has to act. */
export function verdictNeedsAPerson(v: Verdict): boolean {
  return needsAPerson(v)
}

/**
 * `blockerGroup`, with a pool too small for one task filed where a pool set
 * to zero is: under `needs_action`, whatever group its reason arrived in.
 */
export function verdictGroup(
  b: ProfileBlocker,
  groups: Record<string, string[]> | undefined,
  units: number | null,
): 'needs_action' | 'no_room' | null {
  if (blockerVerdict(b, units) === 'below-units') return 'needs_action'
  return blockerGroup(b, groups)
}

/**
 * `ceilingCopy`, with `subject` in this file's own argument order (pools call
 * this with `units` before `subject`; types.ts's callers have no subject to
 * give and default it from the blocker instead).
 */
export function verdictCopy(
  b: { reason: string; pool?: string; limit?: unknown; active?: unknown },
  units: number | null,
  subject: string = b.pool ?? 'This pool',
): string | null {
  return ceilingCopy(b, subject, units)
}

/** The tag each ceiling is drawn with, and what hovering it says. */
const CEILING_TAG: Readonly<Record<Verdict, { cls: string; word: string; title: string }>> = {
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
  'below-units': {
    // The paused tone, for the reason `set-to-zero` has it: a person acts.
    cls: 'paused',
    word: 'too small',
    title: "This pool's limit is below what one task of this profile weighs, so it can never admit one at this limit. Waiting changes nothing — somebody has to raise it.",
  },
  'below-units-quota': {
    cls: 'capped',
    word: 'too small',
    title: "This pool's limit is below what one task of this profile weighs, so it can never admit one at this limit. A provider pool's quota state lowers it as well as an operator does, so this does not say who set it.",
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
export function CeilingTag({ blocker, units = null }: { blocker: ProfileBlocker; units?: number | null }) {
  const tag = CEILING_TAG[blockerVerdict(blocker, units)]
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
export function ceilingTitle(c: Verdict): string {
  return CEILING_TAG[c].title
}

/**
 * The mark a refusing pool is drawn with on Profile headroom -- in its Status
 * cell and in the blocker list under the card alike -- in Pools' vocabulary
 * (CP-12, #85): `paused`, `limit 0` in the paused tone when a person set it
 * and the bad tone when quota zeroed it, and `full` in warn, as Pools draws
 * full. `CeilingTag` above is the submit box's, which CP-12 did not touch.
 * ONE TABLE, so the card's two places cannot draw one pool two ways.
 */
const CEILING_MARK: Readonly<Record<Verdict, { cls: 'is-paused' | 'is-bad' | 'is-warn'; word: string }>> = {
  paused: { cls: 'is-paused', word: 'paused' },
  'set-to-zero': { cls: 'is-paused', word: 'limit 0' },
  zero: { cls: 'is-bad', word: 'limit 0' },
  full: { cls: 'is-warn', word: 'full' },
  // A limit of 0 and a limit below one task are one kind of fact (#66), so
  // they take the same tones: paused when a person set it, bad when quota may.
  'below-units': { cls: 'is-paused', word: 'too small' },
  'below-units-quota': { cls: 'is-bad', word: 'too small' },
}

export function ceilingMark(c: Verdict): { cls: 'is-paused' | 'is-bad' | 'is-warn'; word: string } {
  return CEILING_MARK[c]
}

/**
 * The numbers that made a pool refuse, as the row prints them. "units", never
 * "agents": admission increments by the profile's weight. A paused pool admits
 * nothing at ANY ceiling -- and a drained one carries the unlimited sentinel
 * as its limit -- so it prints what is held and no ceiling; a pool at zero
 * says the zero in words. Only a full pool gets the fraction, because it is
 * the only one the fraction is true of. Shared with the submit box for the
 * reason `CeilingTag` is.
 *
 * A pool too small for one task (#66) is not full either: "0 of 1 units in
 * use" is the busy reading of a pool that has nothing in it. It prints its
 * limit beside the weight it is below.
 */
export function ceilingFigure(blocker: ProfileBlocker, units: number | null = null): string {
  const ceiling = blockerVerdict(blocker, units)
  const held = `${blocker.active} unit${blocker.active === 1 ? '' : 's'} held`
  if (ceiling === 'full') return `${blocker.active} of ${blocker.limit} units in use`
  if (ceiling === 'below-units' || ceiling === 'below-units-quota') {
    return `limit ${blocker.limit} · one task is ${units} units · ${held}`
  }
  return ceiling === 'paused' ? held : `limit 0 · ${held}`
}

function BlockerRow({ blocker, label, units }: { blocker: ProfileBlocker; label: Label; units: number | null }) {
  // A pause and a full pool both stop everything and have OPPOSITE remedies:
  // resume it, versus wait or raise it. A pause is told apart by the reason
  // the server sent -- a paused pool can read 0 of 8 in use and still admit
  // nothing, which is the case that looks healthiest and is not. A pool at
  // ZERO is told apart by its ceiling, because its reason is the full pool's
  // (see `blockerCeiling`): "0 of 0 units in use" under a `full` tag is how
  // the live console came to call a switched-off pool busy. A pool whose
  // limit is below one task's weight is told apart by that weight (#66).
  const ceiling = blockerVerdict(blocker, units)
  // THE CARD'S OWN MARK, NOT `.tag` (CP-12, #85). This list is drawn on the
  // Profile headroom card, and the owner's decision left no `.tag` in that
  // card: each refusal carries the mark its row's Status cell carries, and the
  // row's left rule takes the mark's tone.
  const mark = ceilingMark(ceiling)
  return (
    <li className={`blocker-row ${mark.cls}`}>
      <span className="blocker-head">
        <span className={`ctl-chip ${mark.cls}`} title={ceilingTitle(ceiling)}>
          <i aria-hidden="true" />
          {mark.word}
        </span>
        <code className="blocker-pool" title={blocker.pool}>
          {label(blocker.pool)}
        </code>
        <strong className="blocker-reason">{blocker.reason}</strong>
        {/* The numbers that made it fail, on the entry that failed. */}
        <span className="blocker-at">{ceilingFigure(blocker, units)}</span>
      </span>
      <span className="blocker-copy">{verdictCopy(blocker, units, 'This pool') ?? reasonCopy(blocker.reason)}</span>
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
 *
 * `units` is one task's weight, from `classUnits` (#66): with it, a pool too
 * small for one task is filed under "somebody has to act", as a pool set to
 * zero is. Null when the catalogue was not read, and then nothing changes.
 */
export function BlockerList({
  h,
  groups,
  label = labelsFor(h),
  units = null,
}: {
  h: Headroom
  groups: Record<string, string[]> | undefined
  label?: Label
  units?: number | null
}) {
  const needsAction = h.blockers.filter((b) => verdictGroup(b, groups, units) === 'needs_action')
  const noRoom = h.blockers.filter((b) => verdictGroup(b, groups, units) === 'no_room')
  const ungrouped = h.blockers.filter((b) => verdictGroup(b, groups, units) === null)

  // NOTHING TO LIST, NOTHING DRAWN (CP-6, #85). This printed "No pool is
  // refusing this profile." -- or, on an incomplete read, "No refusal was
  // measured, but the list is incomplete" -- under every Profile headroom
  // card. The owner's decision leaves the card one fact sentence, and that
  // sentence already says it: what runs out first, or that a pool could not be
  // read. So an empty list is not a sentence of its own.
  if (h.blockers.length === 0) return null

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
              <BlockerRow key={b.pool} blocker={b} label={label} units={units} />
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
              <BlockerRow key={b.pool} blocker={b} label={label} units={units} />
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
              <BlockerRow key={b.pool} blocker={b} label={label} units={units} />
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
  const resume = c.action === 'resume'
  const act = `${resume ? 'Resume' : 'Lift'} ${label(c.pool)}`
  // A RESUME SAYS SO IN THE CELL, NOT ONLY IN ITS NAME (#159 review). The
  // server models a PAUSED pool's counterfactual as re-enabling it with its
  // limit left alone (`headroom.py` `_counterfactual`, `action: 'resume'`),
  // so resuming is not overstated as lifting. Under a header reading `if
  // lifted`, a bare `+4` on that row told an operator to raise a limit the
  // row's own Status title says "changes nothing". The words were in the
  // accessible name and the title only, which a phone never shows.
  const phrased = (figure: string) => (resume ? `${figure} if resumed` : figure)
  if (c.headroom_after === null) {
    // Nothing else configured would have bound. Not a number, so not drawn as
    // one.
    return { text: phrased('uncapped'), say: `${act}: nothing else would have been left to cap it.`, kind: 'measured' }
  }
  if (c.delta === null) {
    return { text: '—', say: `${act}: the change could not be measured.`, kind: 'unmeasured' }
  }
  if (c.delta > 0) {
    const next =
      c.next_binding.length > 0
        ? ` — then ${c.next_binding.map(label).join(' and ')} would have bound`
        : ''
    return { text: phrased(`+${c.delta}`), say: `${act}: ${c.delta} more would have started${next}.`, kind: 'measured' }
  }
  // THE VERB AGREES WITH ITS SUBJECT (CP-15). Two pools still in the way
  // "still bind"; it read "eng and anthropic still binds" for as long as
  // `next_binding` has been a list.
  const still =
    c.next_binding.length > 0
      ? `${c.next_binding.map(label).join(' and ')} still ${c.next_binding.length === 1 ? 'binds' : 'bind'}`
      : 'something else still binds'
  return { text: phrased('+0'), say: `${act}: nothing would have changed — ${still}.`, kind: 'pointless' }
}

/*
 * `ProfileAdmissionPanel` LIVED HERE AND IS GONE (CP-6, #85). It drew the
 * incomplete-read banner, the blocker list grouped by remedy, and the
 * counterfactual list under every Profile headroom card, and that card was its
 * only caller. The owner's decision made the counterfactual list a `+N if
 * lifted` column (`liftFigure` above) and left each card one fact sentence.
 *
 * THE OTHER TWO ARE DRAWN BY THE CARD ITSELF NOW (#159 review). The first cut
 * of CP-6 removed the banner and the grouped list as well, which the decision
 * did not ask for: the remedy -- somebody has to act, or waiting clears it --
 * and each refusal's reason and figures were left only in a status mark's
 * `title`, which a phone never shows. `Profiles.tsx` renders `IncompleteNote`
 * and `BlockerList` under its one sentence; both draw nothing when nothing
 * refuses, so the sentence stays the card's only one then.
 */
