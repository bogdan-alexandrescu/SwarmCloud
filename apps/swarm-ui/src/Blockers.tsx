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
import { probeSnapshot, subscribeProbes, type Result } from './fetch'
import {
  blockerCeiling,
  ceilingCopy,
  needsAPerson,
  type Ceiling,
  type ProfileBlocker,
} from './types'

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
 * blocker is drawn exactly as it was before. READ ONCE FOR THE TAB, NOT
 * ONCE PER MOUNT (#227): a class is resized by a deploy, not between two
 * refreshes, and Profiles, Agents and Submit each mounting their own
 * read was three GETs of one table. `sharedRead` keeps a success and drops a
 * failure. Called inside `then` so a load that throws rather than rejects is
 * swallowed the same way.
 */
const readResourceClasses = sharedRead(() => loadResourceClasses())

/**
 * ONE READ, HANDED TO EVERY CALLER THAT ASKS FOR IT (#227).
 *
 * The first call starts the request, a call while it is in flight joins it,
 * and a call after it answered is handed the same answer.
 *
 * ONLY A SUCCESS IS KEPT. An error claims nothing, so it is dropped the moment
 * it lands and the next caller asks again: a tab that once failed the read
 * must not keep failing it until reload.
 *
 * KEPT FOR AS LONG AS THE TAB'S READ REGISTRY. `forgetProbes` (fetch.ts) is
 * "forget what this tab has read"; nothing in the running app calls it, and
 * `setup.ts` calls it between tests. An answer that outlived it would carry one
 * test's catalogue into the next, so an emptied registry drops it.
 *
 * HERE AND NOT IN api.ts, on purpose. Eleven test files replace the api module
 * with a factory that declares only the loaders they stub, so a helper
 * exported from it would be undefined at import in every screen that reaches
 * this file. `load` is called through, never captured, so a test that mocks
 * `loadResourceClasses` still replaces what this calls.
 */
function sharedRead<T>(load: () => Promise<Result<T>>): { (): Promise<Result<T>>; peek(): Result<T> | null } {
  let pending: Promise<Result<T>> | null = null
  let kept: Result<T> | null = null
  subscribeProbes(() => {
    if (probeSnapshot().length === 0) {
      pending = null
      kept = null
    }
  })
  const get = (): Promise<Result<T>> => {
    if (kept !== null) return Promise.resolve(kept)
    if (pending !== null) return pending
    const p: Promise<Result<T>> = Promise.resolve()
      .then(load)
      .then(
        (r) => {
          if (pending === p) {
            pending = null
            if (r.status === 'ok' || r.status === 'stale') kept = r
          }
          return r
        },
        (err: unknown) => {
          if (pending === p) pending = null
          throw err
        },
      )
    pending = p
    return p
  }
  return Object.assign(get, { peek: () => kept })
}

function classesOf(r: Result<{ resource_classes: ResourceClasses }> | null): ResourceClasses | null {
  return r !== null && (r.status === 'ok' || r.status === 'stale') ? r.data.resource_classes : null
}

export function useResourceClasses(): ResourceClasses | null {
  const [classes, setClasses] = useState<ResourceClasses | null>(() => classesOf(readResourceClasses.peek()))
  useEffect(() => {
    let live = true
    Promise.resolve()
      .then(() => readResourceClasses())
      .then(
        (r: Result<{ resource_classes: ResourceClasses }>) => {
          const got = classesOf(r)
          if (live && got !== null) setClasses(got)
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
