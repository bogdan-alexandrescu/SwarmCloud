/**
 * THE CONSOLE'S WORDS FOR THE PLATFORM'S MACHINE TOKENS (QA G2-25, 2026-10-07).
 *
 * The inspector and the run page printed the API's enums as they came:
 * `DEPENDENCY_INCOMPLETE` as a banner title, `from PLANNING` in a run's
 * Progress, `29 ev · CLOUD_RUN_JOB` on an attempt card, `lease_acquired` on
 * the Attempts tab two tabs from Details' `lease released`. Each screen had
 * its own copy of the conversion, or none. This is the one map; a token it
 * does not know is printed lower case with its underscores as spaces, never
 * dropped and never guessed at.
 *
 * `stateWord` spells every task state exactly as the state chip does
 * (components/StatePill.tsx `stateWord`, which takes a `TaskState` only;
 * words.test.ts holds the two together), and takes a run's states too.
 */

import { PARK_WORD } from './components/StatePill'
import type { TaskEvent } from './types'
import { eventKind } from './events'

/** A task or run state as a word: lower case, `DEAD_LETTERED` as `dead-lettered`. */
export function stateWord(state: string): string {
  return state.toLowerCase().replace(/_/g, '-')
}

/** A park reason as the parked pill says it (`PARK_WORD`); one this build does not know, in plain lower case. */
export function parkWord(reason: string): string {
  return (PARK_WORD as Readonly<Record<string, string>>)[reason] ?? reason.toLowerCase().replace(/_/g, ' ')
}

/** The API's `Backend` enum in this console's register, as the runner picker names it. */
const BACKEND_WORD: Readonly<Record<string, string>> = {
  CLOUD_RUN_JOB: 'Cloud Run',
  GKE_AUTOPILOT: 'GKE Autopilot',
  AUTO: 'backend chosen at dispatch',
}

export function backendWord(backend: string): string {
  return BACKEND_WORD[backend] ?? backend.toLowerCase().replace(/_/g, ' ')
}

/**
 * ONE EVENT IN WORDS, AND BY WHAT IT RECORDS RATHER THAN ONLY ITS TYPE
 * (QA G2-29). The frozen `EventType` has no account events, so the worker
 * writes the account it gives back as a `lease_released` with `detail.cause:
 * "account_released"` (agent_worker/lifecycle.py `_emit_account_released`),
 * and `account_assigned` / `account_unreadable` ride other types the same way
 * (swarm_api/task_accounts.py). Read by type alone, every attempt showed two
 * `lease released` a few hundred ms apart; the second was the account.
 */
const CAUSE_WORD: Readonly<Record<string, string>> = {
  account_assigned: 'account assigned',
  account_released: 'account released',
  account_unreadable: 'account unreadable',
}

export function eventWord(e: Pick<TaskEvent, 'type' | 'detail'>): string {
  const cause = e.detail?.['cause']
  if (typeof cause === 'string' && Object.prototype.hasOwnProperty.call(CAUSE_WORD, cause)) return CAUSE_WORD[cause]!
  return eventKind(e).replace(/_/g, ' ')
}

/**
 * THE STATE TOKENS A SERVER-WRITTEN SENTENCE CAN CARRY: the task states and
 * the issue run's. `the run's workflow wf_… ended FAILED` is written by
 * swarm_api/routes/runs.py and stored on the run, so history keeps the
 * capitals whatever the API writes next; the console words it at read.
 */
const PROSE_STATES =
  /\b(SUBMITTED|QUEUED|PARKED|READY|LEASED|DISPATCHED|STARTING|RUNNING|SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED|PLANNING|PLANNED|APPROVED|CHECKING|FIXING|DONE|REJECTED)\b/g

/** A sentence with every whole-word state token in it worded; nothing else in it changes. */
export function proseWords(text: string): string {
  return text.replace(PROSE_STATES, (s) => stateWord(s))
}
