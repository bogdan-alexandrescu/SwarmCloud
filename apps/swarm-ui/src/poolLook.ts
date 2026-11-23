// Which pools need a look (Capacity.tsx's Pools) and when a pool is near its
// ceiling (AdminSettings.tsx's Pool limits), apart from both pages so the
// spine's badges reach them without loading either.
import type { ResourceClasses } from './api'
import { classUnits } from './Blockers'
import { poolProblemRank } from './capacityPoll'
import { poolGroup, poolKind, type Pool } from './types'

/**
 * NEAR A CEILING: 80% of it in use, or more. Under that a pool still has a
 * fifth of its ceiling free, which is room for the next tasks rather than a
 * reason to call an admin; the mock-up's lead (S10b) is a pool at 92%.
 */
export const NEAR_CEILING = 0.8

/**
 * True when a pool is one a person has to act on: `poolGroup` (types.ts)
 * files it under `needs_action` by `blockerGroup`, the rule the Agents list's
 * Waiting split uses -- paused, set to zero by a person, no limit set, over
 * its ceiling (drift), or below one task of its class. A `resource:` pool is
 * weighed against its class's `units` from the catalogue; no other pool has
 * one class. Every such pool is also in `needsALook`, so nothing Agents calls
 * needs-action is left down in a family here.
 */
export function needsAction(pool: Pool, classes: ResourceClasses | null): boolean {
  const units = poolKind(pool.name) === 'resource' ? classUnits(classes, pool.name.slice('resource:'.length)) : null
  return poolGroup(pool, units) === 'needs_action'
}

/**
 * True when a pool belongs in Pools' top group, "Needs a look": full,
 * lowered, paused or at `POOL_NEAR_CEILING` (80%) of its ceiling or more, the
 * group capacity.html A draws -- and every pool `needsAction` takes, so a
 * pool at 0, with no limit, over its ceiling or too small for its class leads
 * as well. `poolProblemRank` is the rule the marks and the family order
 * already use: anything it ranks above `ok` is something to look at.
 *
 * owner'S ANSWER, 2026-10-26 08:20-08:30Z (VQ-031, task_06d63267fd684e27a419):
 * "Needs a look, as the mock-up draws it". It REVERSES the 2026-10-01 note
 * that made this group "Needs action", filed by the Agents list's Waiting
 * split, which kept a full or AIMD-lowered pool in its family because
 * waiting clears it. That split answers a different question -- which of a
 * TASK's refusals a person must clear -- and Agents keeps it. Pools answers
 * "which pools should I look at": the 46-of-50 pool read `ok` at the foot of
 * Providers under that rule (pass 1), and the mock-up's group of full and
 * lowered pools is what the owner picked.
 */
/** `poolProblemRank`'s rank for a pool with nothing to look at (capacityPoll.ts). */
const POOL_RANK_OK = 6

export function needsALook(pool: Pool, classes: ResourceClasses | null): boolean {
  return needsAction(pool, classes) || poolProblemRank(pool) < POOL_RANK_OK
}
