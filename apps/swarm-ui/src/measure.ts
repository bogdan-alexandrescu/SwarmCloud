/**
 * THE ABSENT-MEASUREMENT RULE, in one place.
 *
 * The single most expensive misreading this product can produce is a figure
 * nobody measured rendered as a zero. `record_usage` in
 * `agent_worker/control.py` omits a key the runner did not report and says so
 * in its own docstring -- "None means not reported and zero means cost nothing,
 * and a mock task is genuinely the second while a result that failed to parse
 * is the first". Two different facts, two different remedies, and `0` collapses
 * them.
 *
 * So every number this UI prints for a usage field goes through `numberCell`,
 * and `numberCell` has exactly ONE guard:
 *
 *     typeof value !== 'number' || !Number.isFinite(value)
 *
 * It is deliberately NOT a falsiness test. `if (!value)` is the same defect
 * arriving from the other direction: it turns a MEASURED zero -- a real
 * reading, worth a digit -- into a sentence claiming nothing was measured.
 * Both directions are lies and the guard above is the only one that is neither.
 *
 * The consequence, which is the contract of this module:
 *
 *   * an ABSENT measurement renders as a SENTENCE, never as a numeral;
 *   * a MEASURED zero renders as a DIGIT, because that zero was measured.
 *
 * `AgentDetail.tsx` carries private copies of `usd` and `tokens` with the same
 * guard, written before this module existed; they should switch to these, which
 * is why this lives here rather than in a screen. Nothing here imports React:
 * the rule is about values, and a component cannot be reused by the screen that
 * needs the same decision for a `title` attribute.
 */

/**
 * One figure, with what to say about it.
 *
 * `text` is what goes in the value slot. `note` is the sentence underneath --
 * for an absent cell it explains WHY the measurement is missing, because
 * "not reported" without a cause sends an operator looking for a bug that is
 * not there.
 */
export type Cell =
  | { kind: 'measured'; text: string; note: string }
  | { kind: 'absent'; text: string; note: string }

/** The sentence and cause for one kind of absence. Never a number. */
export interface Absence {
  text: string
  note: string
}

export function measuredCell(text: string, note: string): Cell {
  return { kind: 'measured', text, note }
}

export function absentCell(absence: Absence): Cell {
  return { kind: 'absent', text: absence.text, note: absence.note }
}

/**
 * THE GUARD. The one place a nullable measurement becomes something printable.
 *
 * `format` is only ever reached with a real, finite number, so a formatter
 * cannot be written defensively and cannot invent a zero of its own.
 */
export function numberCell(
  value: number | null | undefined,
  format: (n: number) => string,
  absence: Absence,
  note: string,
): Cell {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return absentCell(absence)
  }
  return measuredCell(format(value), note)
}

// ---------------------------------------------------------------------------
// The absences, named
// ---------------------------------------------------------------------------
//
// Each is a DIFFERENT fact with a DIFFERENT remedy, so each gets its own
// sentence. Collapsing them into one "—" is the mistake this codebase keeps
// paying for: "the platform never recorded this", "this board did not read it"
// and "the read failed" send an operator to three different places.

/** No attempt of this task carried `cost_usd`. */
export const COST_NOT_REPORTED: Absence = {
  text: 'not reported',
  note: 'No attempt reported a cost. This is an absent measurement, not $0.00.',
}

/** No attempt of this task carried any token count. */
export const TOKENS_NOT_REPORTED: Absence = {
  text: 'not reported',
  note: 'No attempt reported a token count. Not the same as a run that used none.',
}

/**
 * The attempts were never fetched for this task.
 *
 * There is no route that aggregates usage across tasks (the audit files it as
 * S3), so a board can only sample -- and a step outside the sample has an
 * unknown cost, not a zero one.
 */
export const USAGE_NOT_SAMPLED: Absence = {
  text: 'not sampled',
  note: 'This board reads a bounded number of attempt sets per refresh, and this step was outside it. Open the step to read its own.',
}

/** The attempts read for this task failed. */
export const USAGE_NOT_READ: Absence = {
  text: 'not read',
  note: 'The attempt read for this step failed, so nothing about its usage can be concluded.',
}

/**
 * The attempt read SUCCEEDED and returned nothing.
 *
 * A real answer, and not one of the two above: the step holds a task, the
 * subcollection was read, and no attempt document exists yet. Nothing was
 * reported because nothing has run, which is different from a runner that ran
 * and reported no figures.
 */
export const NO_ATTEMPT_YET: Absence = {
  text: 'no attempt yet',
  note: 'The attempt read succeeded and returned none. This step holds a task and nothing has run under it.',
}

/** The step has no task, so nothing has ever run. */
export const NEVER_RAN: Absence = {
  text: 'not started',
  note: 'This step has no task yet, so there is nothing to measure.',
}

/**
 * The step has a task id that was not in the task read.
 *
 * Its `text` is deliberately NOT "not read", which belongs to `USAGE_NOT_READ`:
 * two absences that can land in the same slot and print the same word are two
 * absences a reader cannot tell apart, which is the whole failure this file
 * exists to prevent.
 */
export const STATE_UNREAD: Absence = {
  text: 'task unread',
  note: 'This step’s task was not in the task read, so none of its figures are available.',
}

/** A task that reached a terminal state without a `completed_at`. */
export const FINISH_NOT_RECORDED: Absence = {
  text: 'not recorded',
  note: 'Written when an attempt ends. This task ended without one, so its duration cannot be computed.',
}

// ---------------------------------------------------------------------------
// Formatters. Reached only with a finite number.
// ---------------------------------------------------------------------------

/**
 * Money.
 *
 * Two decimal places is the readable form, but a real cost of $0.004 printed
 * as "$0.00" is the absent-measurement lie arriving by a different door -- it
 * claims a run was free when it was not. Anything under a cent therefore keeps
 * four places. An EXACT zero is a measurement and renders as a digit.
 */
export function usd(n: number): string {
  if (n === 0) return '$0.00'
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`
  return `$${n.toFixed(2)}`
}

/**
 * Token counts, compacted.
 *
 * A step node is ~150px wide and "96,000" spends most of it, so thousands are
 * abbreviated. Zero stays "0": it is a measurement.
 */
export function tokenText(n: number): string {
  if (n === 0) return '0'
  const abs = Math.abs(n)
  if (abs < 1_000) return `${Math.round(n)}`
  if (abs < 1_000_000) return `${(n / 1_000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(2)}M`
}

/** A plain count. Zero is a digit -- it was counted. */
export function countText(n: number): string {
  return `${Math.round(n)}`
}

/**
 * A span of time, in words.
 *
 * Deliberately the same ladder as `duration` in types.ts, which is not
 * exported and is reached through `elapsed`. A second spelling of the same
 * ladder would drift, so this file imports nothing and types.ts keeps its own;
 * if a third caller appears, one of them should move.
 */
export function durationText(msSpan: number): string {
  const s = Math.max(0, Math.round(msSpan / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

// ---------------------------------------------------------------------------
// The cells the screens ask for
// ---------------------------------------------------------------------------

/** `cost_usd`, summed or single. Absent is a sentence; zero is `$0.00`. */
export function costCell(value: number | null | undefined, note: string, absence: Absence = COST_NOT_REPORTED): Cell {
  return numberCell(value, usd, absence, note)
}

/** A token count. Absent is a sentence; zero is `0`. */
export function tokenCell(value: number | null | undefined, note: string, absence: Absence = TOKENS_NOT_REPORTED): Cell {
  return numberCell(value, tokenText, absence, note)
}

/** Anything that was COUNTED rather than reported -- checkpoints, attempts. */
export function countCell(value: number | null | undefined, absence: Absence, note: string): Cell {
  return numberCell(value, countText, absence, note)
}

/**
 * Sum a nullable field over rows, keeping "nothing reported it" distinct from
 * "it summed to zero".
 *
 * `rows.reduce((t, r) => t + (pick(r) ?? 0), 0)` is the shape this exists to
 * replace: over rows that all report nothing it returns 0, and 0 is then
 * printed as a measurement. Null until at least one row actually carried the
 * figure -- the same rule `loadSpend` in api.ts states for the Overview.
 */
export function sumReported<T>(rows: readonly T[], pick: (row: T) => number | null | undefined): number | null {
  let total: number | null = null
  for (const row of rows) {
    const v = pick(row)
    if (typeof v !== 'number' || !Number.isFinite(v)) continue
    total = (total ?? 0) + v
  }
  return total
}
